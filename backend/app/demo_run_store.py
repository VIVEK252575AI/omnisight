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
from app.db_models import PatchAttempt, Project, QARun, RunStep, TargetEnvironment, TestFlow, TestFlowVersion
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
DEMO_FLOW_HASH = sha256(json.dumps(DEMO_FLOW_DEFINITION, sort_keys=True).encode()).hexdigest()


@dataclass
class DemoRun:
    run_id: str
    status: str = "queued"
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def response(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "result": self.result,
            "error": self.error,
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
                name="ShopLite checkout visual QA",
                description="Capture the fixed checkout at desktop and mobile sizes and verify the checkout action.",
                status="active",
            )
            session.add(flow)
            session.flush()

        flow_version = session.scalar(select(TestFlowVersion).where(
            TestFlowVersion.test_flow_id == flow.id,
            TestFlowVersion.version_number == 1,
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
                    viewport_matrix_json=DEMO_FLOW_DEFINITION["viewports"],
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

    @staticmethod
    def _api_status(run: QARun, result: dict[str, Any], patch: PatchAttempt | None) -> str:
        if run.status == "queued":
            return "queued"
        if run.status == "running":
            return "running"
        if run.status in {"failed", "timed_out", "canceled"}:
            return "failed"
        if patch:
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
            result.update({"proposal": patch.proposal_record_json})
        if run.failure_code:
            result["failure_code"] = run.failure_code
        return DemoRun(
            run_id=run.id,
            status=self._api_status(run, result, patch),
            result=result,
            error=run.failure_message_safe,
            created_at=run.created_at,
            updated_at=run.updated_at,
        )

    def get(self, run_id: str) -> DemoRun | None:
        if self._database is None:
            with self._lock:
                return self._runs.get(run_id)
        with self._database() as session:
            run = session.scalar(select(QARun).options(selectinload(QARun.steps)).where(QARun.id == run_id))
            if run is None:
                return None
            return self._from_database_run(session, run)

    def list(self) -> list[DemoRun]:
        if self._database is None:
            with self._lock:
                return sorted(self._runs.values(), key=lambda run: run.created_at, reverse=True)
        with self._database() as session:
            runs = session.scalars(select(QARun).where(
                QARun.project_id == self._settings.local_project_id,
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
                    run.status = status
                if result is not None:
                    run.result = result
                run.error = error
                run.updated_at = utcnow()
                return run

        now = utcnow()
        with self._database() as session:
            run = session.scalar(select(QARun).where(QARun.id == run_id).with_for_update())
            if run is None:
                return None
            if status is not None:
                patch_status = {
                    "awaiting_sandbox_approval": "proposed",
                    "patch_rejected": "rejected_by_policy",
                    "validating": "validating",
                    "verified": "validation_passed",
                    "validation_failed": "validation_failed",
                }.get(status)
                if status == "running":
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
                    if not patch_id:
                        patch_id = session.scalar(select(PatchAttempt.id).where(PatchAttempt.run_id == run.id).order_by(PatchAttempt.created_at.desc()).limit(1))
                    if patch_id:
                        attempt = session.get(PatchAttempt, patch_id)
                        if attempt:
                            attempt.run_id = run.id
                            attempt.status = patch_status
                            attempt.updated_at = now
                            attempt.finished_at = now if patch_status in {"rejected_by_policy", "validation_passed", "validation_failed"} else attempt.finished_at
                if status == "needs_inspection":
                    run.outcome = "analysis_inconclusive"
                    run.status = "completed"
                    run.current_stage = "finished"
                    run.finished_at = run.finished_at or now

            if result is not None:
                result_steps = [
                    (2, "browser_capture", "browser_execution", "Capture ShopLite browser evidence", {"capture": result.get("capture")}, "passed"),
                    (3, "visual_analysis", "visual_analysis", "Analyze checkout evidence", {key: result[key] for key in ("findings", "detection_adapter", "detection_is_mock", "detection_status") if key in result}, "passed" if result.get("findings") else "inconclusive"),
                    (4, "patch_generation", "patch_generation", "Generate a safe patch proposal", {"proposal_attempts": result.get("proposal_attempts", [])}, "failed" if result.get("status") == "patch_rejected" else "passed"),
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
