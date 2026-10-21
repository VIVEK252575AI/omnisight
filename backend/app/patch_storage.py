"""Storage for validated patch proposals.

The app uses PostgreSQL for durable records. A small in-memory mode remains
available for isolated unit tests. Safe proposal summaries and exact validated
edits occupy separate columns; raw model output is never persisted.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_session_factory
from app.db_models import FindingRecordRow, PatchAttempt, Project
from app.ids import utcnow
from app.patch_schemas import PatchProposal, PatchProposalRecord, PatchProposalStatus


class PatchStore:
    """Thread-safe store for patch proposal records and validated payloads."""

    def __init__(self, database: Callable[[], Session] | None = None, *, settings: Settings | None = None) -> None:
        self._database = database
        self._settings = settings or get_settings()
        self._records: dict[str, PatchProposalRecord] = {}
        self._by_finding: dict[str, list[str]] = {}
        self._edits: dict[str, PatchProposal] = {}
        self._lock = threading.Lock()

    def _ensure_project(self, session) -> Project:
        settings = self._settings
        project = session.get(Project, settings.local_project_id)
        if project is None:
            project = Project(
                id=settings.local_project_id,
                name=settings.local_project_name,
                slug=f"local-{settings.local_project_id[:8]}",
                owner_actor_id="local-owner",
            )
            session.add(project)
            session.flush()
        return project

    def add(self, record: PatchProposalRecord, proposal: PatchProposal | None = None) -> PatchProposalRecord:
        """Persist one append-only attempt and its validated payload."""
        with self._lock:
            payload = proposal or record.proposal
            if self._database is not None:
                now = record.created_at or utcnow()
                safe_record = record.model_dump(mode="json", exclude={"proposal"})
                proposal_json = payload.model_dump(mode="json") if payload is not None else None
                changed_files = [item.model_dump(mode="json") for item in record.files]
                diff_hash = hashlib.sha256(
                    json.dumps(proposal_json or {}, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                with self._database() as session:
                    project = self._ensure_project(session)
                    finding = session.scalar(select(FindingRecordRow).where(
                        FindingRecordRow.id == record.finding_id,
                        FindingRecordRow.project_id == project.id,
                        FindingRecordRow.is_current.is_(True),
                    ))
                    if finding is not None and record.scan_id is not None and str(record.scan_id) != finding.run_id:
                        raise ValueError("patch proposal scan id does not match its finding")
                    session.add(PatchAttempt(
                        id=record.proposal_id,
                        project_id=project.id,
                        finding_id=record.finding_id,
                        run_id=finding.run_id if finding is not None else str(record.scan_id) if record.scan_id else None,
                        status="proposed" if record.status == PatchProposalStatus.proposed else "rejected_by_policy",
                        attempt_number=record.attempt,
                        model_provider=record.generator_label,
                        changed_files_json=changed_files,
                        summary=record.rationale or "Patch proposal",
                        diff_hash=diff_hash,
                        policy_check_result={"status": record.status.value, "rejection_reasons": record.rejection_reasons},
                        validated_patch_json=proposal_json,
                        proposal_record_json=safe_record,
                        requested_by="local-owner",
                        created_at=now,
                        updated_at=now,
                        finished_at=now if record.status == PatchProposalStatus.rejected else None,
                    ))
                    session.commit()
            self._records[record.proposal_id] = record.model_copy(update={"proposal": None})
            self._by_finding.setdefault(record.finding_id, []).append(record.proposal_id)
            if payload is not None:
                self._edits[record.proposal_id] = payload
            return record

    def _record_from_row(self, row: PatchAttempt) -> PatchProposalRecord:
        data = dict(row.proposal_record_json)
        data["proposal_id"] = row.id
        data["finding_id"] = row.finding_id
        data["attempt"] = row.attempt_number
        data["created_at"] = row.created_at
        data["status"] = row.policy_check_result.get("status", "rejected")
        data["proposal"] = None
        return PatchProposalRecord.model_validate(data)

    def list_for_finding(self, finding_id: str) -> list[PatchProposalRecord]:
        if self._database is not None:
            with self._database() as session:
                rows = session.scalars(select(PatchAttempt).where(
                    PatchAttempt.finding_id == finding_id,
                    PatchAttempt.project_id == self._settings.local_project_id,
                ).order_by(PatchAttempt.created_at, PatchAttempt.id)).all()
                return [self._record_from_row(row) for row in rows]
        with self._lock:
            return [self._records[pid] for pid in self._by_finding.get(finding_id, []) if pid in self._records]

    def count_for_finding(self, finding_id: str) -> int:
        return len(self.list_for_finding(finding_id))

    def get(self, proposal_id: str) -> PatchProposalRecord | None:
        with self._lock:
            record = self._records.get(proposal_id)
            if record is not None:
                return record
        if self._database is None:
            return None
        with self._database() as session:
            row = session.get(PatchAttempt, proposal_id)
            if row is None or row.project_id != self._settings.local_project_id:
                return None
            record = self._record_from_row(row)
        with self._lock:
            self._records[proposal_id] = record
            self._by_finding.setdefault(record.finding_id, []).append(proposal_id)
        return record

    def get_proposal(self, proposal_id: str) -> PatchProposal | None:
        with self._lock:
            proposal = self._edits.get(proposal_id)
            if proposal is not None or self._database is None:
                return proposal
        with self._database() as session:
            row = session.get(PatchAttempt, proposal_id)
            if row is None or row.project_id != self._settings.local_project_id or row.validated_patch_json is None:
                return None
            proposal = PatchProposal.model_validate(row.validated_patch_json)
        with self._lock:
            self._edits[proposal_id] = proposal
        return proposal


def get_patch_store(settings: Settings = Depends(get_settings)) -> PatchStore:
    """Build the PostgreSQL-backed proposal store for the configured project."""
    # Resolve the database only when a handler actually uses the store. This
    # lets authentication/origin guards reject requests before database access.
    return PatchStore(lambda: get_session_factory(settings)(), settings=settings)
