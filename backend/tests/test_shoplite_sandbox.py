"""Fixed-target ShopLite sandbox policy and API approval tests."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import Settings, get_settings
from app.patch_schemas import (
    PatchEdit,
    PatchFile,
    PatchProposal,
    PatchProposalRecord,
    PatchProposalStatus,
)
from app.patch_storage import PatchStore, get_patch_store
from app.shoplite_sandbox import (
    SANDBOX_IMAGE,
    SandboxFailure,
    _docker_command,
    _stage_demo,
    run_shoplite_sandbox,
)


def _proposal(*, path: str = "styles/checkout.css") -> PatchProposal:
    return PatchProposal(
        finding_id="finding-demo",
        rationale="Use a responsive checkout button.",
        generator_label="GEMINI",
        files=[
            PatchFile(
                path=path,
                edits=[PatchEdit(old_text="width: 520px;", new_text="width: 100%;")],
            )
        ],
    )


def _settings(**kwargs) -> Settings:
    values = {
        "enable_shoplite_sandbox": True,
        "shoplite_sandbox_token": "a-local-test-token",
        "approved_patch_paths": ["styles/checkout.css"],
    }
    values.update(kwargs)
    return Settings(**values)


def test_stages_exact_css_edit_without_mutating_repository(tmp_path):
    from app import shoplite_sandbox

    source = shoplite_sandbox.DEMO_ROOT / "styles" / "checkout.css"
    before = source.read_bytes()
    _stage_demo(_proposal(), tmp_path, _settings())

    if os.name != "nt":
        assert tmp_path.stat().st_mode & 0o777 == 0o755
    baseline = (tmp_path / "baseline" / "styles" / "checkout.css").read_text()
    candidate_path = tmp_path / "candidate" / "styles" / "checkout.css"
    candidate = candidate_path.read_text()
    assert "width: 520px;" in baseline
    assert "width: 520px;" not in candidate
    assert "width: 100%;" in candidate
    assert source.read_bytes() == before


def test_staging_rejects_any_target_other_than_fixed_shoplite_css(tmp_path):
    with pytest.raises(SandboxFailure, match="Only demo-app"):
        _stage_demo(_proposal(path="../backend/app/main.py"), tmp_path, _settings())


def test_docker_command_is_restricted_and_never_mounts_socket(monkeypatch, tmp_path):
    monkeypatch.setattr("app.shoplite_sandbox.shutil.which", lambda command: "docker.exe")
    command = _docker_command(tmp_path, "omni-shoplite-test")

    assert command[0] == "docker.exe"
    assert "--network=none" in command
    assert "--init" in command
    assert "--pull=never" in command
    assert "--cpus=1" in command
    assert "--memory=512m" in command
    assert "--pids-limit=128" in command
    assert "--shm-size=128m" in command
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "--security-opt=no-new-privileges" in command
    assert "--user=10001:10001" in command
    assert SANDBOX_IMAGE in command
    assert not any("docker.sock" in item.lower() for item in command)
    mount = command[command.index("--mount") + 1]
    assert mount.endswith("target=/workspace,readonly")


def test_run_returns_verified_report_from_bounded_container(monkeypatch):
    from app import shoplite_sandbox

    checks = {
        "baseline_mobile_overflow_px": 167.0,
        "patched_mobile_overflow_px": 0.0,
        "patched_desktop_overflow_px": 0.0,
        "order_confirmation": True,
        "browser_error_count": 0,
        "blocked_external_request_count": 0,
    }
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"passed": True, "checks": checks}),
            stderr="",
        )

    monkeypatch.setattr("app.shoplite_sandbox.shutil.which", lambda _: "docker")
    monkeypatch.setattr("app.shoplite_sandbox.subprocess.run", fake_run)
    report = run_shoplite_sandbox(
        _proposal(), proposal_id="proposal-demo", settings=_settings()
    )

    assert report.status == "verified"
    assert report.checks.baseline_mobile_overflow_px == 167
    assert report.checks.patched_mobile_overflow_px == 0
    assert observed["kwargs"]["shell"] is False
    assert observed["kwargs"]["timeout"] == 45
    assert "--network=none" in observed["command"]


def test_sandbox_surfaces_only_safe_harness_failure_diagnostics(monkeypatch):
    from app import shoplite_sandbox

    def fake_run(command, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout=json.dumps({"passed": False, "error": "PermissionError", "stage": "load_baseline_mobile"}),
            stderr="",
        )

    monkeypatch.setattr("app.shoplite_sandbox.shutil.which", lambda _: "docker")
    monkeypatch.setattr("app.shoplite_sandbox.subprocess.run", fake_run)
    with pytest.raises(SandboxFailure, match="load_baseline_mobile \\(PermissionError\\)") as failure:
        run_shoplite_sandbox(_proposal(), proposal_id="proposal-demo", settings=_settings())

    assert failure.value.code == "SANDBOX_BROWSER_FAILED"


def test_timeout_explicitly_removes_named_container(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1] == "run":
            raise shoplite_sandbox.subprocess.TimeoutExpired(command, 45)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    from app import shoplite_sandbox

    monkeypatch.setattr("app.shoplite_sandbox.shutil.which", lambda _: "docker")
    monkeypatch.setattr("app.shoplite_sandbox.subprocess.run", fake_run)
    with pytest.raises(SandboxFailure) as failure:
        run_shoplite_sandbox(_proposal(), proposal_id="proposal-timeout", settings=_settings())

    assert failure.value.code == "SANDBOX_TIMEOUT"
    assert calls[1][1:3] == ["rm", "--force"]
    assert calls[1][3].startswith("omni-shoplite-")


def test_sandbox_api_requires_loopback_approval_token_and_stored_proposal(monkeypatch):
    settings = _settings()
    store = PatchStore()
    proposal = _proposal()
    record = PatchProposalRecord(
        proposal_id="proposal-demo",
        finding_id=proposal.finding_id,
        status=PatchProposalStatus.proposed,
        generator_label="GEMINI",
        proposal=proposal,
    )
    store.add(record, proposal)
    main.app.dependency_overrides[get_patch_store] = lambda: store
    main.app.dependency_overrides[get_settings] = lambda: settings

    def fake_sandbox(proposed, *, proposal_id, settings):
        assert proposed == proposal
        return {
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
        }

    monkeypatch.setattr("app.routers.patches.run_shoplite_sandbox", fake_sandbox)
    try:
        async def loopback_app(scope, receive, send):
            if scope["type"] == "http":
                scope["client"] = ("127.0.0.1", 50001)
            await main.app(scope, receive, send)

        with TestClient(loopback_app, raise_server_exceptions=False) as client:
            endpoint = "/api/v1/demo/shoplite/patch-proposals/proposal-demo/validate"
            headers = {"X-OmniSight-Demo-Approval": "a-local-test-token"}
            assert client.post(endpoint, json={"approval": "approve-demo-only"}).status_code == 403
            response = client.post(
                endpoint,
                json={"approval": "approve-demo-only"},
                headers=headers,
            )
            assert response.status_code == 200
            assert response.json()["status"] == "verified"
    finally:
        main.app.dependency_overrides.clear()


def test_sandbox_api_rejects_non_loopback_client(monkeypatch):
    settings = _settings()
    main.app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            response = client.post(
                "/api/v1/demo/shoplite/patch-proposals/proposal-demo/validate",
                json={"approval": "approve-demo-only"},
                headers={"X-OmniSight-Demo-Approval": "a-local-test-token"},
            )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"
    finally:
        main.app.dependency_overrides.clear()
