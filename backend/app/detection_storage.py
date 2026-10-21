"""Durable detection attempts and findings; raw model text remains memory-only."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import timezone
from uuid import UUID, uuid4

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.contracts import FindingStatus, VlmOutcome
from app.config import Settings, get_settings
from app.db import get_session_factory
from app.db_models import DetectionAttempt as DetectionAttemptRow
from app.db_models import FindingRecordRow, QARun
from app.detection_schemas import DetectionRun, FindingRecord
from app.ids import utcnow
from app.schemas import ScanStatus


def _as_utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class DetectionStore:
    """Store validated finding records and attempt metadata, never raw output."""

    def __init__(self, database: Callable[[], Session] | None = None, *, settings: Settings | None = None) -> None:
        self._database = database
        self._settings = settings or get_settings()
        self._findings: dict[str, FindingRecord] = {}
        self._by_scan: dict[UUID, list[str]] = {}
        self._runs: dict[UUID, DetectionRun] = {}
        self._raw: dict[UUID, str] = {}
        self._lock = threading.Lock()

    def put_run(self, run: DetectionRun) -> DetectionRun:
        """Append a detection attempt's safe metadata; raw text stays in memory."""
        with self._lock:
            self._runs[run.scan_id] = run.model_copy(update={"raw_output": None})
            if run.raw_output is not None:
                self._raw[run.scan_id] = run.raw_output
            if self._database is None:
                return run

            with self._database() as session:
                qa_run = session.scalar(select(QARun).where(
                    QARun.id == str(run.scan_id),
                    QARun.project_id == self._settings.local_project_id,
                ))
                if qa_run is None:
                    raise KeyError(f"QA run {run.scan_id} does not exist")
                session.add(DetectionAttemptRow(
                    id=str(uuid4()),
                    project_id=qa_run.project_id,
                    run_id=qa_run.id,
                    status=run.status.value,
                    adapter_label=run.adapter_label,
                    is_mock=run.is_mock,
                    model_label=run.model_label,
                    prompt_version=run.prompt_version,
                    schema_version=run.schema_version,
                    attempts=run.attempts,
                    duration_ms=run.duration_ms,
                    outcome=run.outcome.value if run.outcome is not None else None,
                    reason_code=run.reason_code,
                    reason_message=run.reason_message,
                    findings_count=run.findings_count,
                    warnings=run.warnings,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                ))
                session.commit()
        return run

    @staticmethod
    def _run_from_row(row: DetectionAttemptRow) -> DetectionRun:
        return DetectionRun(
            scan_id=UUID(row.run_id),
            status=ScanStatus(row.status),
            adapter_label=row.adapter_label,
            is_mock=row.is_mock,
            model_label=row.model_label,
            prompt_version=row.prompt_version,
            schema_version=row.schema_version,
            attempts=row.attempts,
            duration_ms=row.duration_ms,
            outcome=VlmOutcome(row.outcome) if row.outcome else None,
            reason_code=row.reason_code,
            reason_message=row.reason_message,
            findings_count=row.findings_count,
            warnings=row.warnings or [],
            started_at=_as_utc(row.started_at),
            finished_at=_as_utc(row.finished_at) if row.finished_at else None,
        )

    def get_run(self, scan_id: UUID) -> DetectionRun | None:
        with self._lock:
            run = self._runs.get(scan_id)
            raw = self._raw.get(scan_id)
        if self._database is not None:
            with self._database() as session:
                row = session.scalar(select(DetectionAttemptRow).where(
                    DetectionAttemptRow.run_id == str(scan_id),
                    DetectionAttemptRow.project_id == self._settings.local_project_id,
                ).order_by(DetectionAttemptRow.created_at.desc(), DetectionAttemptRow.id.desc()).limit(1))
                run = self._run_from_row(row) if row is not None else None
        if run is None:
            return None
        return run.model_copy(update={"raw_output": raw}) if raw is not None else run.model_copy(update={"raw_output": None})

    def set_raw(self, scan_id: UUID, text: str) -> None:
        with self._lock:
            self._raw[scan_id] = text

    def clear_raw(self, scan_id: UUID) -> None:
        with self._lock:
            self._raw.pop(scan_id, None)

    def get_raw(self, scan_id: UUID) -> str | None:
        with self._lock:
            return self._raw.get(scan_id)

    @staticmethod
    def _record_from_row(row: FindingRecordRow) -> FindingRecord:
        return FindingRecord.model_validate(row.record_json)

    def replace_findings(self, scan_id: UUID, records: list[FindingRecord]) -> list[FindingRecord]:
        """Mark prior findings superseded and atomically insert the new current set."""
        if self._database is None:
            with self._lock:
                for old_id in self._by_scan.get(scan_id, []):
                    self._findings.pop(old_id, None)
                ids: list[str] = []
                for record in records:
                    self._findings[record.finding.finding_id] = record.model_copy(update={"raw_output": None})
                    ids.append(record.finding.finding_id)
                self._by_scan[scan_id] = ids
            return list(records)

        now = utcnow()
        with self._database() as session:
            qa_run = session.scalar(select(QARun).where(
                QARun.id == str(scan_id),
                QARun.project_id == self._settings.local_project_id,
            ).with_for_update())
            if qa_run is None:
                raise KeyError(f"QA run {scan_id} does not exist")
            current = session.scalars(select(FindingRecordRow).where(
                FindingRecordRow.run_id == qa_run.id,
                FindingRecordRow.is_current.is_(True),
            ).with_for_update()).all()
            for row in current:
                row.is_current = False
                row.updated_at = now
                row.version += 1
            attempt_id = session.scalar(select(DetectionAttemptRow.id).where(
                DetectionAttemptRow.run_id == qa_run.id,
                DetectionAttemptRow.project_id == qa_run.project_id,
            ).order_by(DetectionAttemptRow.created_at.desc(), DetectionAttemptRow.id.desc()).limit(1))
            for record in records:
                finding = record.finding
                safe_record = record.model_dump(mode="json", exclude={"raw_output"})
                session.add(FindingRecordRow(
                    id=finding.finding_id,
                    project_id=qa_run.project_id,
                    run_id=qa_run.id,
                    detection_attempt_id=attempt_id,
                    category=finding.category.value,
                    severity=finding.severity.value if finding.severity else None,
                    confidence=finding.confidence,
                    status=finding.status.value,
                    is_current=True,
                    record_json=safe_record,
                    created_at=record.created_at,
                    updated_at=record.created_at,
                ))
            session.commit()
        with self._lock:
            self._by_scan[scan_id] = [record.finding.finding_id for record in records]
            self._findings.update({record.finding.finding_id: record.model_copy(update={"raw_output": None}) for record in records})
        return list(records)

    def list_for_scan(self, scan_id: UUID) -> list[FindingRecord]:
        if self._database is not None:
            with self._database() as session:
                rows = session.scalars(select(FindingRecordRow).where(
                    FindingRecordRow.run_id == str(scan_id),
                    FindingRecordRow.project_id == self._settings.local_project_id,
                    FindingRecordRow.is_current.is_(True),
                ).order_by(FindingRecordRow.created_at, FindingRecordRow.id)).all()
                return [self._record_from_row(row) for row in rows]
        with self._lock:
            return [self._findings[item] for item in self._by_scan.get(scan_id, []) if item in self._findings]

    def get_finding(self, finding_id: str) -> FindingRecord | None:
        if self._database is not None:
            with self._database() as session:
                row = session.scalar(select(FindingRecordRow).where(
                    FindingRecordRow.id == finding_id,
                    FindingRecordRow.project_id == self._settings.local_project_id,
                    FindingRecordRow.is_current.is_(True),
                ))
                return self._record_from_row(row) if row is not None else None
        with self._lock:
            return self._findings.get(finding_id)

    def compare_and_set_finding_status(
        self,
        finding_id: str,
        *,
        expected: FindingStatus | frozenset[FindingStatus],
        to_status: FindingStatus,
    ) -> FindingRecord | None:
        expected_set = frozenset({expected}) if isinstance(expected, FindingStatus) else expected
        if self._database is not None:
            with self._database() as session:
                row = session.scalar(select(FindingRecordRow).where(
                    FindingRecordRow.id == finding_id,
                    FindingRecordRow.project_id == self._settings.local_project_id,
                    FindingRecordRow.is_current.is_(True),
                ).with_for_update())
                if row is None or FindingStatus(row.status) not in expected_set:
                    return None
                row.status = to_status.value
                row.version += 1
                row.updated_at = utcnow()
                data = dict(row.record_json)
                data["finding"] = {**data["finding"], "status": to_status.value}
                row.record_json = data
                result = self._record_from_row(row)
                session.commit()
                return result
        with self._lock:
            record = self._findings.get(finding_id)
            if record is None or record.finding.status not in expected_set:
                return None
            updated = record.model_copy(update={"finding": record.finding.model_copy(update={"status": to_status})})
            self._findings[finding_id] = updated
            return updated


def get_detection_store(settings: Settings = Depends(get_settings)) -> DetectionStore:
    """Create a PostgreSQL-backed detection store for the configured project."""
    return DetectionStore(lambda: get_session_factory(settings)(), settings=settings)
