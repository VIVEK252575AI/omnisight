from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import main
from app.config import Settings, get_settings
from app.db import get_session
from app.db_models import Base, GitHubOperation, PullRequest, ValidationReport
from app.patch_schemas import (
    PatchEdit,
    PatchFile,
    PatchFileSummary,
    PatchProposal,
    PatchProposalRecord,
    PatchProposalStatus,
)
from app.patch_storage import PatchStore, get_patch_store
from app.routers import github_publish
from app.shoplite_sandbox import ShopLiteSandboxReport


def test_live_demo_proposal_can_be_validated_and_published_as_draft(monkeypatch):
    project_id = "39d0a95d-951c-4d52-9ad1-e128cc7e6ce0"
    owner_token = "local-owner-test-token-" + "x" * 32
    base_sha = "a" * 40
    proposal_id = "proposal-shoplite-demo"
    settings = Settings(
        enable_github_publish_api=True,
        enable_github_app=True,
        enable_shoplite_sandbox=True,
        auth_mode="local_single_user",
        local_auth_token=owner_token,
        local_project_id=project_id,
        github_app_id="123",
        github_app_installation_id="456",
        github_target_owner="VIVEK252575AI",
        github_target_repository="omnisight",
        approved_patch_paths=["styles/checkout.css"],
    )
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    patch_store = PatchStore()
    proposal = PatchProposal(
        finding_id="finding-shoplite-demo",
        files=[PatchFile(path="styles/checkout.css", edits=[
            PatchEdit(old_text="width: 520px;", new_text="width: 100%; max-width: 100%;")
        ])],
        rationale="Keep the checkout action inside the mobile viewport.",
        generator_label="MOCK",
    )
    patch_store.add(
        PatchProposalRecord(
            proposal_id=proposal_id,
            finding_id=proposal.finding_id,
            status=PatchProposalStatus.proposed,
            files=[PatchFileSummary(path="styles/checkout.css", edits=1, bytes=len("width: 520px;" + "width: 100%; max-width: 100%;"))],
            generator_label="MOCK",
            is_mock=True,
            proposal=proposal,
        ),
        proposal,
    )

    class FakeGitHubApp:
        published_calls = 0

        async def verify_repository(self, owner, repository, *, base_branch=None):
            assert (owner, repository) == ("VIVEK252575AI", "omnisight")
            assert base_branch in {None, "main"}
            return SimpleNamespace(
                repository_id=42,
                full_name="VIVEK252575AI/omnisight",
                default_branch="main",
                base_branch="main",
                base_sha=base_sha,
                verified_at=datetime.now(timezone.utc),
            )

        async def read_shoplite_source(self, owner, repository, *, base_sha):
            assert (owner, repository, base_sha) == ("VIVEK252575AI", "omnisight", "a" * 40)
            return {
                "index.html": "<html></html>",
                "checkout/index.html": "<html></html>",
                "styles/checkout.css": "#submit { width: 520px; }",
                "app.js": "",
            }

        async def publish_shoplite_draft_pr(self, owner, repository, **kwargs):
            self.published_calls += 1
            assert kwargs["proposal_id"] == proposal_id
            assert kwargs["expected_base_sha"] == base_sha
            assert kwargs["confirmed"] is True
            return SimpleNamespace(
                repository_id=42,
                proposal_id=proposal_id,
                branch="omnisight/fix/proposal-shoplite-demo",
                commit_sha="b" * 40,
                pull_request_number=123,
                pull_request_url="https://github.com/VIVEK252575AI/omnisight/pull/123",
                draft=True,
                base_branch="main",
                base_sha=base_sha,
                diff_sha256="c" * 64,
            )

    fake_github = FakeGitHubApp()
    monkeypatch.setattr(github_publish, "_github", lambda _settings: fake_github)

    def fake_sandbox(_proposal, *, proposal_id, settings, base_revision=None, source_files=None):
        assert proposal_id == "proposal-shoplite-demo"
        assert base_revision == base_sha
        assert set(source_files) == {"index.html", "checkout/index.html", "styles/checkout.css", "app.js"}
        return ShopLiteSandboxReport.model_validate({
            "status": "verified",
            "proposal_id": proposal_id,
            "report_id": str(uuid4()),
            "base_revision": base_revision,
            "diff_hash": "c" * 64,
            "checks": {
                "baseline_mobile_overflow_px": 167,
                "patched_mobile_overflow_px": 0,
                "patched_desktop_overflow_px": 0,
                "order_confirmation": True,
                "browser_error_count": 0,
                "blocked_external_request_count": 0,
            },
        })

    monkeypatch.setattr(github_publish, "run_shoplite_sandbox", fake_sandbox)
    main.app.dependency_overrides[get_settings] = lambda: settings
    main.app.dependency_overrides[get_patch_store] = lambda: patch_store

    def session_override():
        with Session(engine, expire_on_commit=False) as session:
            yield session

    main.app.dependency_overrides[get_session] = session_override

    async def local_peer(scope, receive, send):
        if scope["type"] == "http":
            scope["client"] = ("127.0.0.1", 50123)
        await main.app(scope, receive, send)

    try:
        with TestClient(local_peer, base_url="http://127.0.0.1:8000", raise_server_exceptions=False) as client:
            headers = {"X-OmniSight-Local-Token": owner_token}
            denied = client.post("/api/v1/local/github/connection/verify", json={}, headers={})
            verified = client.post("/api/v1/local/github/connection/verify", json={}, headers=headers)
            assert denied.status_code == 401
            assert verified.status_code == 200
            assert verified.json()["repository"] == "VIVEK252575AI/omnisight"

            validation = client.post(
                f"/api/v1/local/shoplite/proposals/{proposal_id}/validate",
                json={}, headers=headers,
            )
            assert validation.status_code == 200
            report = validation.json()
            assert report["status"] == "verified"
            assert report["base_revision"] == base_sha

            pr_response = client.post(
                "/api/v1/local/github/draft-prs",
                json={"validation_report_id": report["report_id"], "confirmation": "create-draft-pr"},
                headers={**headers, "Idempotency-Key": "shoplite-test-key-123"},
            )
            assert pr_response.status_code == 200
            assert pr_response.json()["status"] == "succeeded"
            assert pr_response.json()["draft"] is True
            assert pr_response.json()["pull_request_url"].endswith("/pull/123")

            replay = client.post(
                "/api/v1/local/github/draft-prs",
                json={"validation_report_id": report["report_id"], "confirmation": "create-draft-pr"},
                headers={**headers, "Idempotency-Key": "shoplite-test-key-123"},
            )
            assert replay.status_code == 200
            assert replay.json()["operation_id"] == pr_response.json()["operation_id"]

            operation = client.get(
                f"/api/v1/local/github/operations/{pr_response.json()['operation_id']}",
                headers=headers,
            )
            assert operation.status_code == 200
            assert operation.json()["status"] == "succeeded"
            assert operation.json()["validation_report_id"] == report["report_id"]
            assert operation.json()["idempotency_key"] == "shoplite-test-key-123"

            denied_state = client.get("/api/v1/local/github/state")
            saved_state = client.get("/api/v1/local/github/state", headers=headers)
            assert denied_state.status_code == 401
            assert saved_state.status_code == 200
            assert saved_state.json()["connection"]["repository"] == "VIVEK252575AI/omnisight"
            assert saved_state.json()["connection"]["base_sha"] == base_sha
            assert saved_state.json()["validations"][0]["report_id"] == report["report_id"]
            assert saved_state.json()["operations"][0]["operation_id"] == pr_response.json()["operation_id"]
            assert saved_state.json()["operations"][0]["idempotency_key"] == "shoplite-test-key-123"

        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(ValidationReport)) == 1
            assert session.scalar(select(func.count()).select_from(GitHubOperation)) == 1
            assert session.scalar(select(func.count()).select_from(PullRequest)) == 1
        assert fake_github.published_calls == 1
    finally:
        main.app.dependency_overrides.clear()
        engine.dispose()
