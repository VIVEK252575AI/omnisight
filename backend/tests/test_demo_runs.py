from __future__ import annotations

from fastapi.testclient import TestClient

from app import main
from app.config import Settings, get_settings
from app.demo_run_store import DemoRunStore, get_demo_run_store
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
        main.app.dependency_overrides.clear()


def test_demo_run_starts_fixed_pipeline_and_exposes_status(monkeypatch):
    settings = Settings(allowed_origins=["http://127.0.0.1:8000"])
    store = DemoRunStore()
    main.app.dependency_overrides[get_settings] = lambda: settings
    main.app.dependency_overrides[get_demo_run_store] = lambda: store

    def fake_pipeline(run_id, api_url, injected_store):
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
        main.app.dependency_overrides.clear()


def test_demo_run_requires_explicit_sandbox_approval(monkeypatch):
    settings = Settings(
        allowed_origins=["http://127.0.0.1:8000"],
        enable_shoplite_sandbox=True,
        shoplite_sandbox_token="test-approval-token",
    )
    run_store = DemoRunStore()
    run = run_store.create()
    run_store.update(
        run.run_id,
        status="awaiting_sandbox_approval",
        result={"proposal": {"proposal_id": "proposal-demo"}},
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

    def fake_sandbox(proposed, *, proposal_id, settings):
        assert proposed == proposal
        return ShopLiteSandboxReport.model_validate({
            "status": "verified",
            "proposal_id": proposal_id,
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
                json={"confirmation": "approve-demo-only"},
                headers={"Origin": "http://127.0.0.1:8000"},
            )
        assert response.status_code == 200
        assert response.json()["status"] == "verified"
        assert response.json()["result"]["sandbox"]["checks"]["patched_mobile_overflow_px"] == 0
    finally:
        main.app.dependency_overrides.clear()
