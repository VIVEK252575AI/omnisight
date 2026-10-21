from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app.config import Settings, get_settings
from app.demo_run_store import DemoRunStore, get_demo_run_store
from app.db_models import Base, Project, QARun, TargetEnvironment, TestFlow as _TestFlowRow, TestFlowVersion as _TestFlowVersionRow
from app.evidence_storage import EvidenceStore, get_evidence_store
from app.patch_schemas import PatchProposal, PatchProposalRecord, PatchProposalStatus
from app.patch_storage import PatchStore, get_patch_store
from app.routers import demo_runs
from app.shoplite_sandbox import ShopLiteSandboxReport


def _local_client(app=main.app):
    async def loopback_app(scope, receive, send):
        if scope["type"] == "http":
            scope["client"] = ("127.0.0.1", 50123)
        await app(scope, receive, send)

    return TestClient(
        loopback_app,
        base_url="http://127.0.0.1:8000",
        raise_server_exceptions=False,
    )


def test_demo_run_rejects_missing_or_untrusted_origin():
    settings = Settings(allowed_origins=["http://127.0.0.1:8000"])
    main.app.dependency_overrides[get_settings] = lambda: settings
    try:
        with _local_client() as client:
            missing = client.post("/api/v1/demo/shoplite/runs", json={})
            untrusted = client.post(
                "/api/v1/demo/shoplite/runs",
                json={},
                headers={"Origin": "https://attacker.example"},
            )
        assert missing.status_code == 403
        assert untrusted.status_code == 403
    finally:
        with demo_runs._CANCEL_EVENTS_LOCK:
            demo_runs._RUN_CANCEL_EVENTS.clear()
        main.app.dependency_overrides.clear()


def test_shoplite_workspace_exposes_persisted_fixed_project_and_flow():
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    database = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(allowed_origins=["http://127.0.0.1:8000"])
    store = DemoRunStore(database, settings=settings)
    main.app.dependency_overrides[get_settings] = lambda: settings
    main.app.dependency_overrides[get_demo_run_store] = lambda: store
    try:
        with _local_client() as client:
            response = client.get("/api/v1/demo/shoplite/workspace")
            published = client.put(
                "/api/v1/demo/shoplite/workspace/test-flow",
                json={
                    "expected_version": 1,
                    "viewports": [
                        {"width": 400, "height": 844},
                        {"width": 1440, "height": 900},
                    ],
                    "steps": [
                        {"key": "open-checkout", "label": "Open the local checkout", "type": "navigate", "url": "/checkout"},
                        {"key": "checkout-heading", "label": "Check heading", "type": "assert_visible", "locator": {"strategy": "role", "role": "heading", "value": "Checkout", "exact": True}},
                    ],
                },
                headers={"Origin": "http://127.0.0.1:8000"},
            )
            stale = client.put(
                "/api/v1/demo/shoplite/workspace/test-flow",
                json={
                    "expected_version": 1,
                    "viewports": [
                        {"width": 390, "height": 844},
                        {"width": 1440, "height": 900},
                    ],
                },
                headers={"Origin": "http://127.0.0.1:8000"},
            )
            unsupported = client.put(
                "/api/v1/demo/shoplite/workspace/test-flow",
                json={
                    "expected_version": 2,
                    "route": "https://attacker.example/",
                    "viewports": [
                        {"width": 400, "height": 844},
                        {"width": 1440, "height": 900},
                    ],
                },
                headers={"Origin": "http://127.0.0.1:8000"},
            )
        assert response.status_code == 200
        assert published.status_code == 200
        assert published.json()["test_flow"]["version"] == 2
        assert published.json()["test_flow"]["definition"]["viewports"][0]["width"] == 400
        assert [step["key"] for step in published.json()["test_flow"]["definition"]["steps"]] == [
            "open-checkout", "checkout-heading"
        ]
        assert stale.status_code == 409
        assert unsupported.status_code == 422
        workspace = response.json()
        assert workspace["project"]["name"] == settings.local_project_name
        assert workspace["target"] == {
            "name": "ShopLite local demo",
            "base_url": "http://127.0.0.1",
            "environment_kind": "demo",
            "authorization_status": "approved",
        }
        flow = workspace["test_flow"]
        assert flow["name"] == "ShopLite checkout visual QA"
        assert flow["version"] == 1
        assert flow["version_status"] == "published"
        assert flow["definition"]["viewports"] == [
            {"width": 390, "height": 844}, {"width": 1440, "height": 900}
        ]
        assert len(flow["definition_hash"]) == 64
        run = store.create()
        assert run is not None
        assert store.flow_definition_for_run(run.run_id)["viewports"][0]["width"] == 400
        assert store.flow_definition_for_run(run.run_id)["steps"][0]["url"] == "/checkout"
        with database() as session:
            assert session.get(Project, settings.local_project_id) is not None
            assert session.query(TargetEnvironment).count() == 1
            assert session.query(_TestFlowRow).count() == 1
            versions = session.query(_TestFlowVersionRow).order_by(_TestFlowVersionRow.version_number).all()
            assert len(versions) == 2
            assert versions[0].status == "retired"
            assert versions[1].status == "published"
            qa_run = session.get(QARun, run.run_id)
            assert qa_run is not None
            assert qa_run.test_flow_version_id == versions[1].id
            assert qa_run.viewport_matrix_json[0]["width"] == 400
        cancellation = store.request_cancel(run.run_id)
        assert cancellation is not None
        assert cancellation.cancellation_requested is True
        assert store.cancellation_requested(run.run_id) is True
        settled = store.update(run.run_id, status="running")
        assert settled.status == "canceled"
        assert settled.cancellation_requested is False
        with database() as session:
            qa_run = session.get(QARun, run.run_id)
            assert qa_run.status == "canceled"
            assert qa_run.cancellation_requested_at is not None
    finally:
        with demo_runs._CANCEL_EVENTS_LOCK:
            demo_runs._RUN_CANCEL_EVENTS.clear()
        main.app.dependency_overrides.clear()
        engine.dispose()


def test_demo_run_starts_fixed_pipeline_and_exposes_status(monkeypatch):
    settings = Settings(allowed_origins=["http://127.0.0.1:8000"])
    store = DemoRunStore()
    main.app.dependency_overrides[get_settings] = lambda: settings
    main.app.dependency_overrides[get_demo_run_store] = lambda: store

    def fake_pipeline(run_id, api_url, injected_store, _cancellation_event=None):
        assert api_url == "http://127.0.0.1:8000"
        injected_store.update(
            run_id,
            status="awaiting_sandbox_approval",
            result={"status": "awaiting_sandbox_approval", "proposal": {"proposal_id": "proposal-demo"}},
        )

    monkeypatch.setattr(demo_runs, "_run_demo_pipeline", fake_pipeline)
    try:
        with _local_client() as client:
            started = client.post(
                "/api/v1/demo/shoplite/runs",
                json={},
                headers={"Origin": "http://127.0.0.1:8000"},
            )
            assert started.status_code == 202
            run_id = started.json()["run_id"]
            current = client.get(
                f"/api/v1/demo/shoplite/runs/{run_id}",
                headers={"Origin": "http://127.0.0.1:8000"},
            )
            history = client.get("/api/v1/demo/shoplite/runs")
            duplicate = client.post(
                "/api/v1/demo/shoplite/runs",
                json={},
                headers={"Origin": "http://127.0.0.1:8000"},
            )
        assert current.status_code == 200
        assert current.json()["status"] == "awaiting_sandbox_approval"
        assert current.json()["result"]["proposal"]["proposal_id"] == "proposal-demo"
        assert history.status_code == 200
        assert [item["run_id"] for item in history.json()] == [run_id]
        assert history.json()[0]["status"] == "awaiting_sandbox_approval"
        assert duplicate.status_code == 409
    finally:
        with demo_runs._CANCEL_EVENTS_LOCK:
            demo_runs._RUN_CANCEL_EVENTS.clear()
        main.app.dependency_overrides.clear()


def test_active_shoplite_run_can_be_canceled_and_cancellation_is_persisted(monkeypatch):
    settings = Settings(allowed_origins=["http://127.0.0.1:8000"])
    store = DemoRunStore()
    main.app.dependency_overrides[get_settings] = lambda: settings
    main.app.dependency_overrides[get_demo_run_store] = lambda: store
    monkeypatch.setattr(demo_runs, "_run_demo_pipeline", lambda *_args: None)
    try:
        with _local_client() as client:
            headers = {"Origin": "http://127.0.0.1:8000"}
            started = client.post("/api/v1/demo/shoplite/runs", json={}, headers=headers)
            run_id = started.json()["run_id"]
            canceled = client.post(
                f"/api/v1/demo/shoplite/runs/{run_id}/cancel", json={}, headers=headers
            )
            current = client.get(f"/api/v1/demo/shoplite/runs/{run_id}", headers=headers)
            repeated = client.post(
                f"/api/v1/demo/shoplite/runs/{run_id}/cancel", json={}, headers=headers
            )
        assert started.status_code == 202
        assert canceled.status_code == 202
        assert canceled.json()["cancellation_requested"] is True
        assert current.json()["cancellation_requested"] is True
        assert repeated.status_code == 202

        finished = store.update(run_id, status="running")
        assert finished.status == "canceled"
        assert store.get(run_id).status == "canceled"
        assert store.get(run_id).cancellation_requested is False
    finally:
        with demo_runs._CANCEL_EVENTS_LOCK:
            demo_runs._RUN_CANCEL_EVENTS.clear()
        main.app.dependency_overrides.clear()


def test_cancel_shoplite_run_requires_local_allowed_origin(monkeypatch):
    settings = Settings(allowed_origins=["http://127.0.0.1:8000"])
    store = DemoRunStore()
    run = store.create()
    main.app.dependency_overrides[get_settings] = lambda: settings
    main.app.dependency_overrides[get_demo_run_store] = lambda: store
    try:
        with _local_client() as client:
            response = client.post(f"/api/v1/demo/shoplite/runs/{run.run_id}/cancel", json={})
        assert response.status_code == 403
    finally:
        main.app.dependency_overrides.clear()


def test_cancel_finished_shoplite_run_returns_conflict(monkeypatch):
    settings = Settings(allowed_origins=["http://127.0.0.1:8000"])
    store = DemoRunStore()
    run = store.create()
    store.update(run.run_id, status="verified")
    main.app.dependency_overrides[get_settings] = lambda: settings
    main.app.dependency_overrides[get_demo_run_store] = lambda: store
    try:
        with _local_client() as client:
            response = client.post(
                f"/api/v1/demo/shoplite/runs/{run.run_id}/cancel",
                json={},
                headers={"Origin": "http://127.0.0.1:8000"},
            )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CONFLICT"
    finally:
        main.app.dependency_overrides.clear()


def test_process_tree_termination_reaps_a_running_worker():
    process_options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **process_options,
    )
    try:
        demo_runs._terminate_demo_process(process)
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_canceled_worker_process_is_terminated_and_run_is_settled(tmp_path, monkeypatch):
    store = DemoRunStore()
    run = store.create()
    demo_root = tmp_path / "demo-app"
    demo_root.mkdir()
    pipeline = tmp_path / "run_demo_pipeline.py"
    pipeline.write_text("pass", encoding="utf-8")
    event = threading.Event()

    class FakeProcess:
        pid = 123
        returncode = None
        terminated = False
        calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                event.set()
                raise subprocess.TimeoutExpired("demo", timeout)
            return "", ""

        def poll(self):
            return self.returncode

    process = FakeProcess()

    def fake_terminate(child):
        child.terminated = True
        child.returncode = -1
        return "", ""

    monkeypatch.setattr(demo_runs, "DEMO_APP_ROOT", demo_root)
    monkeypatch.setattr(demo_runs, "PIPELINE_SCRIPT", pipeline)
    monkeypatch.setattr(demo_runs.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(demo_runs, "_terminate_demo_process", fake_terminate)

    demo_runs._run_demo_pipeline(run.run_id, "http://127.0.0.1:8000", store, event)

    assert process.terminated is True
    assert store.get(run.run_id).status == "canceled"


def test_patch_preview_returns_safe_diff_and_blocks_stale_source(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    css_path = source_root / "styles" / "checkout.css"
    css_path.parent.mkdir(parents=True)
    css_path.write_text("#place-order {\n  width: 520px;\n}\n", encoding="utf-8")
    settings = Settings(
        allowed_origins=["http://127.0.0.1:8000"],
        patch_source_root=str(source_root),
        approved_patch_paths=["styles/checkout.css"],
        patch_allowed_extensions=[".css"],
    )
    monkeypatch.setattr(demo_runs, "DEMO_APP_ROOT", source_root)
    run_store = DemoRunStore()
    run = run_store.create()
    proposal = PatchProposal(
        finding_id="finding-preview",
        files=[{"path": "styles/checkout.css", "edits": [{"old_text": "width: 520px;", "new_text": "width: 100%;"}]}],
        rationale="Keep the checkout action within the viewport.",
        generator_label="MOCK",
    )
    patch_store = PatchStore()
    patch_store.add(
        PatchProposalRecord(
            proposal_id="proposal-preview",
            finding_id=proposal.finding_id,
            status=PatchProposalStatus.proposed,
            generator_label="MOCK",
            proposal=proposal,
        ),
        proposal,
    )
    run_store.update(
        run.run_id,
        status="awaiting_sandbox_approval",
        result={
            "findings": [{"finding_id": proposal.finding_id}],
            "proposal": {"proposal_id": "proposal-preview"},
        },
    )
    main.app.dependency_overrides[get_settings] = lambda: settings
    main.app.dependency_overrides[get_demo_run_store] = lambda: run_store
    main.app.dependency_overrides[get_patch_store] = lambda: patch_store
    try:
        with _local_client() as client:
            response = client.get(f"/api/v1/demo/shoplite/runs/{run.run_id}/patch-preview")
            assert response.status_code == 200
            body = response.json()
            assert body["proposal_id"] == "proposal-preview"
            assert body["file_path"] == "demo-app/styles/checkout.css"
            assert "-  width: 520px;" in body["diff"]
            assert "+  width: 100%;" in body["diff"]
            assert len(body["base_sha256"]) == 64
            assert len(body["candidate_sha256"]) == 64
            assert len(body["diff_sha256"]) == 64

            css_path.write_text("#place-order {\n  width: 480px;\n}\n", encoding="utf-8")
            stale = client.get(f"/api/v1/demo/shoplite/runs/{run.run_id}/patch-preview")
        assert stale.status_code == 409
        assert "start a new scan" in stale.json()["error"]["message"]
    finally:
        main.app.dependency_overrides.clear()


def test_reviewer_can_reject_proposal_with_a_persisted_reason():
    settings = Settings(allowed_origins=["http://127.0.0.1:8000"])
    run_store = DemoRunStore()
    run = run_store.create()
    proposal = PatchProposal(
        finding_id="finding-review",
        files=[{"path": "styles/checkout.css", "edits": [{"old_text": "width: 520px;", "new_text": "width: 100%;"}]}],
        rationale="Make the action fit on mobile.",
        generator_label="MOCK",
    )
    patch_store = PatchStore()
    patch_store.add(
        PatchProposalRecord(
            proposal_id="proposal-review",
            finding_id=proposal.finding_id,
            status=PatchProposalStatus.proposed,
            generator_label="MOCK",
            proposal=proposal,
        ),
        proposal,
    )
    run_store.update(
        run.run_id,
        status="awaiting_sandbox_approval",
        result={
            "findings": [{"finding_id": proposal.finding_id}],
            "proposal": {"proposal_id": "proposal-review"},
        },
    )
    main.app.dependency_overrides[get_settings] = lambda: settings
    main.app.dependency_overrides[get_demo_run_store] = lambda: run_store
    main.app.dependency_overrides[get_patch_store] = lambda: patch_store
    try:
        with _local_client() as client:
            rejected = client.post(
                f"/api/v1/demo/shoplite/runs/{run.run_id}/reject-proposal",
                json={"reason": "The proposed selector is too broad."},
                headers={"Origin": "http://127.0.0.1:8000"},
            )
            duplicate = client.post(
                f"/api/v1/demo/shoplite/runs/{run.run_id}/reject-proposal",
                json={"reason": "Second decision must be blocked."},
                headers={"Origin": "http://127.0.0.1:8000"},
            )
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "proposal_rejected"
        assert rejected.json()["result"]["proposal_review"]["decision"] == "rejected"
        assert rejected.json()["result"]["proposal_review"]["reason"] == "The proposed selector is too broad."
        assert patch_store.get("proposal-review").status == PatchProposalStatus.rejected
        assert patch_store.get("proposal-review").rejection_reasons == ["human_rejected"]
        assert patch_store.get("proposal-review").human_review_reason == "The proposed selector is too broad."
        assert duplicate.status_code == 409
    finally:
        main.app.dependency_overrides.clear()


def test_demo_run_requires_explicit_sandbox_approval_and_shows_before_after_evidence(monkeypatch, tmp_path):
    source_root = tmp_path / "shoplite-source"
    css_path = source_root / "styles" / "checkout.css"
    css_path.parent.mkdir(parents=True)
    css_path.write_text(".checkout { color: old; }\n", encoding="utf-8")
    monkeypatch.setattr(demo_runs, "DEMO_APP_ROOT", source_root)
    settings = Settings(
        allowed_origins=["http://127.0.0.1:8000"],
        enable_shoplite_sandbox=True,
        shoplite_sandbox_token="test-approval-token",
        artifacts_dir=tmp_path,
        patch_source_root=str(source_root),
        approved_patch_paths=["styles/checkout.css"],
        patch_allowed_extensions=[".css"],
    )
    evidence_store = EvidenceStore(settings=settings, artifacts_root=tmp_path)
    run_store = DemoRunStore()
    run = run_store.create()
    run_store.update(
        run.run_id,
        status="awaiting_sandbox_approval",
        result={
            "findings": [{"finding_id": "finding-demo"}],
            "proposal": {"proposal_id": "proposal-demo"},
        },
    )
    patch_store = PatchStore()
    proposal = PatchProposal(
        finding_id="finding-demo",
        files=[{"path": "styles/checkout.css", "edits": [{"old_text": "old", "new_text": "new"}]}],
        rationale="Keep the checkout action within the mobile card.",
        generator_label="GEMINI",
    )
    patch_store.add(
        PatchProposalRecord(
            proposal_id="proposal-demo",
            finding_id="finding-demo",
            status=PatchProposalStatus.proposed,
            generator_label="GEMINI",
            proposal=proposal,
        ),
        proposal,
    )
    main.app.dependency_overrides[get_settings] = lambda: settings
    main.app.dependency_overrides[get_demo_run_store] = lambda: run_store
    main.app.dependency_overrides[get_patch_store] = lambda: patch_store
    main.app.dependency_overrides[get_evidence_store] = lambda: evidence_store

    def fake_sandbox(proposed, *, proposal_id, settings, screenshot_store, expected_base_sha256):
        assert proposed == proposal
        assert expected_base_sha256 == hashlib.sha256(css_path.read_bytes()).hexdigest()
        image = b"\x89PNG\r\n\x1a\nshoplite-evidence"
        baseline_id = screenshot_store(image, "baseline_screenshot_artifact_id")
        candidate_id = screenshot_store(image, "candidate_screenshot_artifact_id")
        return ShopLiteSandboxReport.model_validate({
            "status": "verified",
            "proposal_id": proposal_id,
            "baseline_screenshot_artifact_id": baseline_id,
            "candidate_screenshot_artifact_id": candidate_id,
            "checks": {
                "baseline_mobile_overflow_px": 167,
                "patched_mobile_overflow_px": 0,
                "patched_desktop_overflow_px": 0,
                "order_confirmation": True,
                "browser_error_count": 0,
                "blocked_external_request_count": 0,
            },
        })

    monkeypatch.setattr(demo_runs, "run_shoplite_sandbox", fake_sandbox)
    try:
        with _local_client() as client:
            response = client.post(
                f"/api/v1/demo/shoplite/runs/{run.run_id}/validate",
                json={
                    "confirmation": "approve-demo-only",
                    "base_sha256": hashlib.sha256(css_path.read_bytes()).hexdigest(),
                },
                headers={"Origin": "http://127.0.0.1:8000"},
            )
            baseline_image = client.get(
                f"/api/v1/artifacts/{response.json()['result']['sandbox']['baseline_screenshot_artifact_id']}"
            )
        assert response.status_code == 200
        assert response.json()["status"] == "verified"
        assert response.json()["result"]["sandbox"]["checks"]["patched_mobile_overflow_px"] == 0
        assert baseline_image.status_code == 200
        assert baseline_image.content.startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        main.app.dependency_overrides.clear()
