from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from app import main
from app.config import Settings, get_settings
from app.demo_run_store import DemoRunStore, get_demo_run_store
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
