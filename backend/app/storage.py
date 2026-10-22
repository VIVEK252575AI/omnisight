"""Scan lifecycle storage using the canonical QA run domain records."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from uuid import UUID

from fastapi import Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.db import get_session_factory
from app.db_models import Project, QARun, TargetEnvironment, TestFlow, TestFlowVersion, WorkflowEvent
from app.ids import new_uuid, utcnow
from app.schemas import ScanCreateRequest, ScanResponse, ScanStatus


LEGACY_FLOW_NAME = "Ad-hoc scan API requests"
LEGACY_FLOW_KEY = "legacy-scan-api-v1"
_RUNNING_SCAN_STATES = {
    ScanStatus.evidence_collection,
    ScanStatus.detecting,
    ScanStatus.analysis,
    ScanStatus.sandbox_validation,
    ScanStatus.visual_verification,
    ScanStatus.review_preparation,
}
_FAILED_SCAN_STATES = {ScanStatus.failed, ScanStatus.detection_failed}
_COMPLETED_SCAN_STATES = {
    ScanStatus.detected,
    ScanStatus.needs_inspection,
    ScanStatus.patch_proposed,
    ScanStatus.validation_passed,
    ScanStatus.validation_failed,
    ScanStatus.patch_verified,
    ScanStatus.verification_failed,
    ScanStatus.ready_for_review,
}
_SCAN_STAGE = {
    ScanStatus.created: "preflight",
    ScanStatus.evidence_collection: "evidence_persistence",
    ScanStatus.detecting: "visual_analysis",
    ScanStatus.detected: "finalization",
    ScanStatus.detection_failed: "visual_analysis",
    ScanStatus.needs_inspection: "finalization",
    ScanStatus.analysis: "visual_analysis",
    ScanStatus.patch_proposed: "finalization",
    ScanStatus.sandbox_validation: "finalization",
    ScanStatus.validation_passed: "finalization",
    ScanStatus.validation_failed: "finalization",
    ScanStatus.visual_verification: "finalization",
    ScanStatus.patch_verified: "finished",
    ScanStatus.verification_failed: "finished",
    ScanStatus.review_preparation: "finalization",
    ScanStatus.ready_for_review: "finished",
    ScanStatus.failed: "finished",
    ScanStatus.cancelled: "finished",
}


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class ScanStore:
    """Thread-safe scan repository; the app uses PostgreSQL, tests may use memory."""

    def __init__(self, database: Callable[[], Session] | None = None, *, settings: Settings | None = None) -> None:
        self._database = database
        self._settings = settings or get_settings()
        self._scans: dict[UUID, ScanResponse] = {}
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
        session.scalar(select(Project.id).where(Project.id == project.id).with_for_update())
        return project

    def _ensure_flow(self, session, project: Project) -> TestFlow:
        flow = session.scalar(select(TestFlow).where(
            TestFlow.project_id == project.id,
            TestFlow.name == LEGACY_FLOW_NAME,
        ).with_for_update())
        if flow is None:
            flow = TestFlow(
                id=str(new_uuid()),
                project_id=project.id,
                name=LEGACY_FLOW_NAME,
                description="Versioned input records submitted through the legacy scan API.",
                status="active",
            )
            session.add(flow)
            session.flush()
        return flow

    @staticmethod
    def _run_query(scan_id: UUID, project_id: str):
        return (
            select(QARun)
            .join(TestFlow, TestFlow.id == QARun.test_flow_id)
            .options(
                selectinload(QARun.target_environment),
                selectinload(QARun.test_flow_version),
                selectinload(QARun.workflow_events),
            )
            .where(
                QARun.id == str(scan_id),
                QARun.project_id == project_id,
                TestFlow.name == LEGACY_FLOW_NAME,
            )
        )

    @staticmethod
    def _scan_from_run(run: QARun) -> ScanResponse:
        definition = run.test_flow_version.definition_json.get("legacy_scan_request", {})
        latest_legacy_event = next(
            (event for event in reversed(run.workflow_events) if "legacy_scan_status" in (event.payload or {})),
            None,
        )
        scan_status = (
            latest_legacy_event.payload["legacy_scan_status"]
            if latest_legacy_event is not None else ScanStatus.created.value
        )
        return ScanResponse(
            scan_id=UUID(run.id),
            status=ScanStatus(scan_status),
            target_url=definition["target_url"],
            viewports=definition.get("viewports", []),
            routes=definition.get("routes"),
            error=run.failure_message_safe,
            created_at=_as_utc(run.created_at),
            updated_at=_as_utc(run.updated_at),
        )

    @staticmethod
    def _run_status(scan_status: ScanStatus) -> str:
        if scan_status is ScanStatus.created:
            return "created"
        if scan_status is ScanStatus.cancelled:
            return "canceled"
        if scan_status in _FAILED_SCAN_STATES:
            return "failed" if scan_status is ScanStatus.failed else "partial"
        if scan_status in _RUNNING_SCAN_STATES:
            return "running"
        if scan_status in _COMPLETED_SCAN_STATES:
            return "completed" if scan_status is ScanStatus.ready_for_review else "partial"
        return "partial"

    def create(self, request: ScanCreateRequest) -> ScanResponse:
        """Create a durable QA run and an immutable version of its scan input."""
        now = utcnow()
        record = ScanResponse(
            scan_id=new_uuid(),
            status=ScanStatus.created,
            target_url=request.target_url,
            viewports=request.viewports,
            routes=request.routes,
            created_at=now,
            updated_at=now,
        )
        if self._database is None:
            with self._lock:
                self._scans[record.scan_id] = record
            return record

        origin = f"{request.target_url.scheme}://{request.target_url.host}"
        if request.target_url.port is not None:
            origin += f":{request.target_url.port}"
        target_name = f"Ad-hoc {origin}"[:120]
        definition = {
            "schema_version": 1,
            "flow_key": LEGACY_FLOW_KEY,
            "legacy_scan_request": {
                "target_url": str(request.target_url),
                "viewports": [viewport.model_dump(mode="json") for viewport in request.viewports],
                "routes": request.routes,
            },
        }
        definition_hash = hashlib.sha256(
            json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        with self._database() as session:
            project = self._ensure_project(session)
            target = session.scalar(select(TargetEnvironment).where(
                TargetEnvironment.project_id == project.id,
                TargetEnvironment.base_url == origin,
            ).limit(1))
            if target is None:
                target = TargetEnvironment(
                    id=str(new_uuid()),
                    project_id=project.id,
                    name=target_name,
                    base_url=origin,
                    environment_kind="other",
                    authorization_status="pending",
                    allowed_hosts=[request.target_url.host],
                )
                session.add(target)
                session.flush()
            flow = self._ensure_flow(session, project)
            version_number = (session.scalar(select(func.max(TestFlowVersion.version_number)).where(
                TestFlowVersion.test_flow_id == flow.id,
            )) or 0) + 1
            flow_version = TestFlowVersion(
                id=str(new_uuid()),
                test_flow_id=flow.id,
                version_number=version_number,
                schema_version=1,
                definition_json=definition,
                definition_hash=definition_hash,
                status="published",
                created_by="local-owner",
            )
            session.add(flow_version)
            session.flush()
            run = QARun(
                id=str(record.scan_id),
                project_id=project.id,
                target_environment_id=target.id,
                test_flow_id=flow.id,
                test_flow_version_id=flow_version.id,
                status="created",
                outcome="not_applicable",
                current_stage=_SCAN_STAGE[ScanStatus.created],
                trigger_source="manual",
                requested_by="local-owner",
                viewport_matrix_json=definition["legacy_scan_request"]["viewports"],
                correlation_id=str(new_uuid()),
                created_at=now,
                updated_at=now,
            )
            session.add(run)
            session.flush()
            self._append_status_event(session, run, ScanStatus.created, now)
            session.commit()
        return record

    def get(self, scan_id: UUID) -> ScanResponse | None:
        if self._database is None:
            with self._lock:
                return self._scans.get(scan_id)
        with self._database() as session:
            run = session.scalar(self._run_query(scan_id, self._settings.local_project_id))
            return self._scan_from_run(run) if run is not None else None

    def list(self) -> list[ScanResponse]:
        if self._database is None:
            with self._lock:
                return sorted(self._scans.values(), key=lambda item: item.created_at)
        query = (
            select(QARun)
            .join(TestFlow, TestFlow.id == QARun.test_flow_id)
            .options(
                selectinload(QARun.target_environment),
                selectinload(QARun.test_flow_version),
                selectinload(QARun.workflow_events),
            )
            .where(QARun.project_id == self._settings.local_project_id, TestFlow.name == LEGACY_FLOW_NAME)
            .order_by(QARun.created_at, QARun.id)
            .limit(1000)
        )
        with self._database() as session:
            return [self._scan_from_run(run) for run in session.scalars(query).all()]

    def replace(self, record: ScanResponse) -> None:
        """Persist updated API status and safe failure details."""
        if self._database is None:
            with self._lock:
                if record.scan_id not in self._scans:
                    raise KeyError(f"scan {record.scan_id} does not exist")
                self._scans[record.scan_id] = record
            return
        with self._database() as session:
            run = session.scalar(self._run_query(record.scan_id, self._settings.local_project_id).with_for_update())
            if run is None:
                raise KeyError(f"scan {record.scan_id} does not exist")
            self._update_run(run, record)
            self._append_status_event(session, run, record.status, record.updated_at)
            session.commit()

    def _update_run(self, run: QARun, record: ScanResponse) -> None:
        run.status = self._run_status(record.status)
        run.current_stage = _SCAN_STAGE[record.status]
        run.failure_message_safe = record.error
        run.updated_at = record.updated_at
        run.version += 1
        if run.status in {"running", "partial"} and run.started_at is None:
            run.started_at = record.updated_at
        if record.status in {ScanStatus.failed, ScanStatus.cancelled, ScanStatus.ready_for_review}:
            run.finished_at = record.updated_at

    @staticmethod
    def _legacy_scan_status(run: QARun) -> ScanStatus:
        for event in reversed(run.workflow_events):
            value = (event.payload or {}).get("legacy_scan_status")
            if value is not None:
                return ScanStatus(value)
        return ScanStatus.created

    def _append_status_event(self, session, run: QARun, scan_status: ScanStatus, now: datetime) -> None:
        run_status = ScanStore._run_status(scan_status)
        if scan_status is ScanStatus.failed:
            event_type = "run.failed"
        elif scan_status is ScanStatus.cancelled:
            event_type = "run.canceled"
        elif scan_status is ScanStatus.ready_for_review:
            event_type = "run.completed"
        elif run_status == "running":
            event_type = "run.stage_started"
        elif run_status == "partial":
            event_type = "run.partial"
        else:
            event_type = "run.created"
        run.workflow_events.append(WorkflowEvent(
            id=str(new_uuid()),
            project_id=run.project_id,
            event_type=event_type,
            event_version=1,
            actor_type="system" if scan_status is ScanStatus.created else "local_owner",
            actor_id="local-owner",
            correlation_id=run.correlation_id,
            payload={
                "legacy_scan_status": scan_status.value,
                "current_stage": _SCAN_STAGE[scan_status],
                "run_status": run_status,
            },
            occurred_at=now,
        ))

    def compare_and_transition(
        self,
        scan_id: UUID,
        *,
        expected: frozenset[ScanStatus],
        to_status: ScanStatus,
        error: str | None = None,
    ) -> ScanResponse | None:
        """Atomically compare and update a scan's durable workflow state."""
        if self._database is None:
            with self._lock:
                current = self._scans.get(scan_id)
                if current is None or current.status not in expected:
                    return None
                updated = current.model_copy(update={"status": to_status, "error": error, "updated_at": utcnow()})
                self._scans[scan_id] = updated
                return updated
        now = utcnow()
        with self._database() as session:
            run = session.scalar(self._run_query(scan_id, self._settings.local_project_id).with_for_update())
            if run is None or self._legacy_scan_status(run) not in expected:
                return None
            run.current_stage = _SCAN_STAGE[to_status]
            run.status = self._run_status(to_status)
            run.failure_message_safe = error
            run.updated_at = now
            run.version += 1
            if run.started_at is None and run.status in {"running", "partial", "completed"}:
                run.started_at = now
            if to_status in {ScanStatus.failed, ScanStatus.cancelled, ScanStatus.ready_for_review}:
                run.finished_at = now
            self._append_status_event(session, run, to_status, now)
            session.commit()
            return self._scan_from_run(run)


def get_store(settings: Settings = Depends(get_settings)) -> ScanStore:
    """Create a PostgreSQL-backed scan repository for the configured project."""
    return ScanStore(lambda: get_session_factory(settings)(), settings=settings)
