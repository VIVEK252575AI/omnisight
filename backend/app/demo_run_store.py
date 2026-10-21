"""PostgreSQL repository for the fixed ShopLite QA workflow."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Any
from collections.abc import Callable
from uuid import uuid4

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.db import get_session_factory
from app.db_models import FindingRecordRow, PatchAttempt, Project, QARun, RunStep, TargetEnvironment, TestFlow, TestFlowVersion
from app.ids import utcnow


ACTIVE_RUN_STATUSES = frozenset({"queued", "running"})
ACTIVE_PATCH_STATUSES = frozenset({"proposed", "validating"})
RUN_TTL = timedelta(minutes=30)
DEMO_FLOW_DEFINITION = {
    "schema_version": 1,
    "flow_key": "shoplite-checkout-visual-v1",
    "route": "/checkout",
    "viewports": [{"width": 390, "height": 844}, {"width": 1440, "height": 900}],
    "assertions": ["checkout_button_visible", "mobile_overflow_measured", "order_confirmation"],
}


def _flow_definition_hash(definition: dict[str, Any]) -> str:
    canonical = json.dumps(definition, sort_keys=True)
    return sha256(canonical.encode()).hexdigest()


DEMO_FLOW_HASH = _flow_definition_hash(DEMO_FLOW_DEFINITION)
DEMO_FLOW_NAME = "ShopLite checkout visual QA"


class FlowVersionConflict(Exception):
    """The published flow changed since the caller loaded its version."""


@dataclass
class DemoRun:
    run_id: str
    status: str = "queued"
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    cancellation_requested: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def response(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "cancellation_requested": self.cancellation_requested,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class DemoRunStore:
    """Persist the fixed ShopLite run, step summaries, and linked patch state."""

    def __init__(
        self,
        database: Callable[[], Session] | None = None,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._database = database
        self._settings = settings or get_settings()
        self._runs: dict[str, DemoRun] = {}
        self._lock = threading.Lock()

    def workspace(self) -> dict[str, Any]:
        """Return the persisted, fixed ShopLite project and published flow."""
        if self._database is None:
            raise RuntimeError("The ShopLite workspace requires the configured database")
        with self._database() as session:
            project, target, flow, flow_version = self._ensure_demo_records(session)
            session.commit()
            return {
                "project": {"id": project.id, "name": project.name, "status": project.status},
                "target": {
                    "name": target.name,
                    "base_url": target.base_url,
                    "environment_kind": target.environment_kind,
                    "authorization_status": target.authorization_status,
                },
                "test_flow": {
                    "name": flow.name,
                    "description": flow.description,
                    "status": flow.status,
                    "version": flow_version.version_number,
                    "version_status": flow_version.status,
                    "definition": flow_version.definition_json,
                    "definition_hash": flow_version.definition_hash,
                },
            }

    def publish_flow_viewports(
        self, *, expected_version: int, viewports: list[dict[str, int]]
    ) -> dict[str, Any]:
        """Publish viewport changes as a new immutable ShopLite flow version."""
        if self._database is None:
            raise RuntimeError("The ShopLite workspace requires the configured database")
        with self._database() as session:
            project, _target, flow, _current = self._ensure_demo_records(session)
            session.scalar(select(Project).where(Project.id == project.id).with_for_update())
            session.refresh(flow)
            if flow.version != expected_version:
                raise FlowVersionConflict
            current = session.scalar(select(TestFlowVersion).where(
                TestFlowVersion.test_flow_id == flow.id,
                TestFlowVersion.version_number == flow.version,
            ))
            if current is None or current.status != "published":
                raise RuntimeError("The current ShopLite flow version is unavailable")
            definition = dict(current.definition_json)
            definition["viewports"] = viewports
            next_version = flow.version + 1
            current.status = "retired"
            flow.version = next_version
            flow.updated_at = utcnow()
            next_flow_version = TestFlowVersion(
                id=str(uuid4()), test_flow_id=flow.id, version_number=next_version,
                schema_version=current.schema_version, definition_json=definition,
                definition_hash=_flow_definition_hash(definition),
                status="published", created_by="local-owner",
            )
            session.add(next_flow_version)
            session.commit()
            return {
                "project": {"id": project.id, "name": project.name, "status": project.status},
                "target": {
                    "name": _target.name, "base_url": _target.base_url,
                    "environment_kind": _target.environment_kind,
                    "authorization_status": _target.authorization_status,
                },
                "test_flow": {
                    "name": flow.name, "description": flow.description,
                    "status": flow.status, "version": next_version,
                    "version_status": next_flow_version.status,
                    "definition": definition,
                    "definition_hash": next_flow_version.definition_hash,
                },
            }

    def flow_definition_for_run(self, run_id: str) -> dict[str, Any]:
        """Load the immutable definition already pinned to a durable run."""
        if self._database is None:
            return DEMO_FLOW_DEFINITION
        with self._database() as session:
            run = session.get(QARun, run_id)
            if run is None:
                raise ValueError("ShopLite run not found")
            version = session.get(TestFlowVersion, run.test_flow_version_id)
            if version is None or version.status not in {"published", "retired"}:
                raise ValueError("ShopLite run flow version is unavailable")
            return version.definition_json

    def _ensure_demo_records(self, session) -> tuple[Project, TargetEnvironment, TestFlow, TestFlowVersion]:
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

        target = session.scalar(select(TargetEnvironment).where(
            TargetEnvironment.project_id == project.id,
            TargetEnvironment.name == "ShopLite local demo",
        ))
        if target is None:
            target = TargetEnvironment(
                id=str(uuid4()), project_id=project.id, name="ShopLite local demo",
                base_url="http://127.0.0.1", environment_kind="demo",
                authorization_status="approved", allowed_hosts=["127.0.0.1", "localhost"],
            )
            session.add(target)
            session.flush()

        flow = session.scalar(select(TestFlow).where(
            TestFlow.project_id == project.id, TestFlow.name == "ShopLite checkout visual QA",
        ))
        if flow is None:
            flow = TestFlow(
                id=str(uuid4()), project_id=project.id,
                name=DEMO_FLOW_NAME,
                description="Capture the fixed checkout at desktop and mobile sizes and verify the checkout action.",
                status="active",
            )
            session.add(flow)
            session.flush()

        flow_version = session.scalar(select(TestFlowVersion).where(
            TestFlowVersion.test_flow_id == flow.id,
            TestFlowVersion.version_number == flow.version,
        ))
        if flow_version is None:
            flow_version = TestFlowVersion(
                id=str(uuid4()), test_flow_id=flow.id, version_number=1,
                schema_version=1, definition_json=DEMO_FLOW_DEFINITION,
                definition_hash=DEMO_FLOW_HASH, status="published", created_by="local-owner",
            )
            session.add(flow_version)
        return project, target, flow, flow_version

    @staticmethod
    def _expire_stale(session, project_id: str, now: datetime) -> None:
        cutoff = now - RUN_TTL
        stale_runs = session.scalars(select(QARun).where(
            QARun.project_id == project_id,
            QARun.status.in_(ACTIVE_RUN_STATUSES),
            QARun.updated_at < cutoff,
        )).all()
        for run in stale_runs:
            run.status = "timed_out"
            run.outcome = "mixed"
            run.current_stage = "finished"
            run.failure_code = "SHOPLITE_RUN_EXPIRED"
            run.failure_message_safe = "The demo run expired. Start a new ShopLite run."
            run.finished_at = now
            run.updated_at = now
        stale_patches = session.scalars(select(PatchAttempt).join(QARun).where(
            QARun.project_id == project_id,
            PatchAttempt.status.in_(ACTIVE_PATCH_STATUSES),
            PatchAttempt.updated_at < cutoff,
        )).all()
        for patch in stale_patches:
            patch.status = "blocked"
            patch.failure_code = "SHOPLITE_APPROVAL_EXPIRED"
            patch.failure_message_safe = "The demo approval window expired. Start a new ShopLite run."
            patch.updated_at = now

    def create(self) -> DemoRun | None:
        now = utcnow()
        with self._lock:
            if self._database is None:
                for run in self._runs.values():
                    if run.status in {"queued", "running", "awaiting_sandbox_approval", "validating"} and now - run.updated_at > RUN_TTL:
                        run.status = "failed"
                        run.error = "The demo run expired. Start a new ShopLite run."
                        run.updated_at = now
                if any(run.status in {"queued", "running", "awaiting_sandbox_approval", "validating"} for run in self._runs.values()):
                    return None
                run = DemoRun(run_id=f"demo_{uuid4().hex}")
                self._runs[run.run_id] = run
                return run

            with self._database() as session:
                project, target, flow, flow_version = self._ensure_demo_records(session)
                # Lock the project row so concurrent API workers serialize the
                # single-active-demo check for this local project.
                session.scalar(select(Project).where(Project.id == project.id).with_for_update())
                self._expire_stale(session, project.id, now)
                active_run = session.scalar(select(QARun.id).where(
                    QARun.project_id == project.id,
                    QARun.status.in_(ACTIVE_RUN_STATUSES),
                ).limit(1))
                active_patch = session.scalar(select(PatchAttempt.id).join(QARun).where(
                    QARun.project_id == project.id,
                    PatchAttempt.status.in_(ACTIVE_PATCH_STATUSES),
                ).limit(1))
                if active_run or active_patch:
                    session.commit()
                    return None
                run_id = f"demo_{uuid4().hex}"
                run = QARun(
                    id=run_id, project_id=project.id,
                    target_environment_id=target.id, test_flow_id=flow.id,
                    test_flow_version_id=flow_version.id, status="queued",
                    outcome="not_applicable", current_stage="preflight",
                    trigger_source="manual", requested_by="local-owner",
                    viewport_matrix_json=flow_version.definition_json["viewports"],
                    correlation_id=str(uuid4()), created_at=now, updated_at=now,
                )
                session.add(run)
                session.add(RunStep(
                    id=str(uuid4()), run_id=run_id, ordinal=1, step_key="preflight",
                    step_type="preflight", display_label="Validate ShopLite demo setup",
                    status="passed", outcome="passed", started_at=now,
                    finished_at=now, input_summary_json={}, result_summary_json={"flow_version_id": flow_version.id},
                ))
                session.commit()
                return DemoRun(run_id=run_id, status="queued", created_at=now, updated_at=now)

    def request_cancel(self, run_id: str) -> DemoRun | None:
        """Persist local-owner cancellation intent for an active browser pipeline."""
        now = utcnow()
        if self._database is None:
            with self._lock:
                run = self._runs.get(run_id)
                if run is None or run.status not in {"queued", "running"}:
                    return None
                run.cancellation_requested = True
                run.updated_at = now
                return run

        with self._database() as session:
            run = session.scalar(
                select(QARun)
                .join(TestFlow)
                .where(
                    QARun.id == run_id,
                    QARun.project_id == self._settings.local_project_id,
                    QARun.status.in_(ACTIVE_RUN_STATUSES),
                    TestFlow.id == QARun.test_flow_id,
                    TestFlow.name == DEMO_FLOW_NAME,
                )
                .with_for_update()
            )
            if run is None:
                return None
            if run.cancellation_requested_at is None:
                run.cancellation_requested_at = now
                run.updated_at = now
                session.commit()
            return self._from_database_run(session, run)

    def cancellation_requested(self, run_id: str) -> bool:
        """Read persisted cancellation intent so worker restarts remain safe."""
        if self._database is None:
            with self._lock:
                run = self._runs.get(run_id)
                return bool(run and run.cancellation_requested)
        with self._database() as session:
            run = session.scalar(
                select(QARun.cancellation_requested_at).where(
                    QARun.id == run_id,
                    QARun.project_id == self._settings.local_project_id,
                )
            )
            return run is not None

    @staticmethod
    def _api_status(run: QARun, result: dict[str, Any], patch: PatchAttempt | None) -> str:
        if run.status == "queued":
            return "queued"
        if run.status == "running":
            return "running"
        if run.status == "canceled":
            return "canceled"
        if run.status in {"failed", "timed_out"}:
            return "failed"
        if run.outcome == "analysis_inconclusive":
            return "needs_inspection"
        if patch:
            if patch.status == "canceled" and "human_rejected" in patch.policy_check_result.get("rejection_reasons", []):
                return "proposal_rejected"
            return {
                "proposed": "awaiting_sandbox_approval",
                "validating": "validating",
                "validation_passed": "verified",
                "validation_failed": "validation_failed",
                "validation_inconclusive": "validation_failed",
                "rejected_by_policy": "patch_rejected",
                "blocked": "validation_failed",
            }.get(patch.status, run.status)
        return "needs_inspection" if result.get("status") == "needs_inspection" else run.status

    def _from_database_run(self, session, run: QARun) -> DemoRun:
        steps = session.scalars(select(RunStep).where(RunStep.run_id == run.id).order_by(RunStep.ordinal)).all()
        result: dict[str, Any] = {}
        for step in steps:
            result.update(step.result_summary_json or {})
        patch = session.scalar(select(PatchAttempt).where(PatchAttempt.run_id == run.id).order_by(PatchAttempt.created_at.desc()).limit(1))
        if patch is not None:
            proposal_summary = dict(patch.proposal_record_json)
            proposal_summary["status"] = patch.policy_check_result.get("status", proposal_summary.get("status", "proposed"))
            proposal_summary["rejection_reasons"] = patch.policy_check_result.get("rejection_reasons", [])
            proposal_summary["human_review_reason"] = patch.policy_check_result.get("human_review_reason")
            result.update({"proposal": proposal_summary})
            if "human_rejected" in proposal_summary["rejection_reasons"]:
                result["proposal_review"] = {
                    "decision": "rejected",
                    "reason": proposal_summary["human_review_reason"],
                    "reviewed_at": patch.finished_at.isoformat() if patch.finished_at else None,
                }
        if run.failure_code:
            result["failure_code"] = run.failure_code
        return DemoRun(
            run_id=run.id,
            status=self._api_status(run, result, patch),
            result=result,
            error=run.failure_message_safe,
            cancellation_requested=(
                run.cancellation_requested_at is not None
                and run.status in ACTIVE_RUN_STATUSES
            ),
            created_at=run.created_at,
            updated_at=run.updated_at,
        )

    def get(self, run_id: str) -> DemoRun | None:
        if self._database is None:
            with self._lock:
                return self._runs.get(run_id)
        with self._database() as session:
            run = session.scalar(select(QARun).options(selectinload(QARun.steps)).join(TestFlow).where(
                QARun.id == run_id,
                QARun.project_id == self._settings.local_project_id,
                TestFlow.id == QARun.test_flow_id,
                TestFlow.name == DEMO_FLOW_NAME,
            ))
            if run is None:
                return None
            return self._from_database_run(session, run)

    def list(self) -> list[DemoRun]:
        if self._database is None:
            with self._lock:
                return sorted(self._runs.values(), key=lambda run: run.created_at, reverse=True)
        with self._database() as session:
            runs = session.scalars(select(QARun).join(TestFlow).where(
                QARun.project_id == self._settings.local_project_id,
                TestFlow.id == QARun.test_flow_id,
                TestFlow.name == DEMO_FLOW_NAME,
            ).order_by(QARun.created_at.desc()).limit(100)).all()
            return [self._from_database_run(session, run) for run in runs]

    def update(
        self,
        run_id: str,
        *,
        status: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> DemoRun | None:
        if self._database is None:
            with self._lock:
                run = self._runs.get(run_id)
                if run is None:
                    return None
                if status is not None:
                    if run.cancellation_requested and status != "canceled":
                        status = "canceled"
                        result = None
                    run.status = status
                if result is not None:
                    run.result = result
                run.error = error
                if run.status == "canceled":
                    run.cancellation_requested = False
                run.updated_at = utcnow()
                return run

        now = utcnow()
        with self._database() as session:
            run = session.scalar(select(QARun).where(QARun.id == run_id).with_for_update())
            if run is None:
                return None
            linked_patches = session.scalars(select(PatchAttempt).where(
                PatchAttempt.run_id == run.id,
                PatchAttempt.project_id == run.project_id,
            )).all()
            if result is not None:
                proposal_ids: set[str] = set()
                proposal = result.get("proposal")
                if isinstance(proposal, dict) and isinstance(proposal.get("proposal_id"), str):
                    proposal_ids.add(proposal["proposal_id"])
                attempts = result.get("proposal_attempts")
                if isinstance(attempts, list):
                    proposal_ids.update(
                        attempt["proposal_id"] for attempt in attempts
                        if isinstance(attempt, dict) and isinstance(attempt.get("proposal_id"), str)
                    )
                source_scan_id = result.get("scan_id")
                finding_ids: set[str] = set()
                if isinstance(source_scan_id, str):
                    finding_ids = set(session.scalars(select(FindingRecordRow.id).where(
                        FindingRecordRow.run_id == source_scan_id,
                        FindingRecordRow.project_id == run.project_id,
                    )).all())
                for proposal_id in proposal_ids:
                    attempt = session.get(PatchAttempt, proposal_id)
                    if (
                        attempt is not None
                        and attempt.project_id == run.project_id
                        and attempt.finding_id in finding_ids
                    ):
                        attempt.run_id = run.id
                        if attempt not in linked_patches:
                            linked_patches.append(attempt)
            if status is not None:
                if run.cancellation_requested_at is not None and status != "canceled":
                    status = "canceled"
                    result = None
                patch_status = {
                    "awaiting_sandbox_approval": "proposed",
                    "patch_rejected": "rejected_by_policy",
                    "proposal_rejected": "canceled",
                    "validating": "validating",
                    "verified": "validation_passed",
                    "validation_failed": "validation_failed",
                    "canceled": "canceled",
                }.get(status)
                if status == "canceled":
                    run.status = "canceled"
                    run.outcome = "not_applicable"
                    run.current_stage = "finished"
                    run.failure_code = None
                    run.failure_message_safe = None
                    run.finished_at = now
                elif status == "running":
                    run.status = "running"
                    run.current_stage = "browser_execution"
                    run.started_at = run.started_at or now
                elif status == "queued":
                    run.status = "queued"
                    run.current_stage = "preflight"
                elif status in {"failed", "validation_failed"} and status == "failed":
                    run.status = "failed"
                    run.outcome = "mixed"
                    run.current_stage = "finished"
                    run.failure_code = "SHOPLITE_PIPELINE_FAILED"
                    run.failure_message_safe = error
                    run.finished_at = now
                else:
                    run.status = "completed"
                    run.current_stage = "finished"
                    run.outcome = "issues_found" if status not in {"needs_inspection"} else "analysis_inconclusive"
                    run.finished_at = run.finished_at or now
                if patch_status:
                    patch_id = (result or {}).get("proposal", {}).get("proposal_id")
                    targets = [
                        attempt for attempt in linked_patches
                        if patch_id is None or attempt.id == patch_id
                    ]
                    for attempt in targets:
                        attempt.status = patch_status
                        attempt.updated_at = now
                        attempt.finished_at = now if patch_status in {"rejected_by_policy", "validation_passed", "validation_failed", "canceled"} else attempt.finished_at
                if status == "needs_inspection":
                    run.outcome = "analysis_inconclusive"
                    run.status = "completed"
                    run.current_stage = "finished"
                    run.finished_at = run.finished_at or now
                if status == "canceled":
                    pending_steps = session.scalars(select(RunStep).where(
                        RunStep.run_id == run.id,
                        RunStep.status.in_(["queued", "running"]),
                    )).all()
                    for step in pending_steps:
                        step.status = "canceled"
                        step.finished_at = now

            if result is not None:
                result_steps = [
                    (2, "browser_capture", "browser_execution", "Capture ShopLite browser evidence", {"capture": result.get("capture")}, "passed"),
                    (3, "visual_analysis", "visual_analysis", "Analyze checkout evidence", {key: result[key] for key in ("scan_id", "findings", "detection_adapter", "detection_is_mock", "detection_status", "detection_reason_code", "detection_reason_message") if key in result}, "passed" if result.get("findings") else "inconclusive"),
                    (4, "patch_generation", "patch_generation", "Generate a safe patch proposal", {"proposal_attempts": result.get("proposal_attempts", []), "proposal_review": result.get("proposal_review")}, "failed" if result.get("status") == "patch_rejected" else "passed"),
                    (5, "patch_validation", "patch_validation", "Validate the candidate patch", {"sandbox": result.get("sandbox")} if result.get("sandbox") else {}, "passed" if result.get("status") == "verified" else "failed" if result.get("status") == "validation_failed" else "skipped"),
                ]
                for ordinal, key, step_type, label, summary, step_status in result_steps:
                    if not any(value is not None for value in summary.values()):
                        continue
                    step = session.scalar(select(RunStep).where(RunStep.run_id == run.id, RunStep.ordinal == ordinal))
                    if step is None:
                        step = RunStep(
                            id=str(uuid4()), run_id=run.id, ordinal=ordinal,
                            step_key=key, step_type=step_type, display_label=label,
                            status=step_status, started_at=now, finished_at=now,
                            input_summary_json={}, result_summary_json=summary,
                        )
                        session.add(step)
                    else:
                        step.status = step_status
                        step.finished_at = now
                        step.result_summary_json = summary
                run.updated_at = now
            session.commit()
            return self.get(run_id)


def get_demo_run_store(settings: Settings = Depends(get_settings)) -> DemoRunStore:
    """FastAPI dependency factory; each store uses PostgreSQL sessions per operation."""
    return DemoRunStore(lambda: get_session_factory(settings)(), settings=settings)
