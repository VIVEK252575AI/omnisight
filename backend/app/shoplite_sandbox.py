"""Bounded Docker verification for the repository's fixed ShopLite demo only.

This module accepts one already-validated CSS edit, applies it to a temporary
copy, and asks a prebuilt Playwright image to compare the original and patched
demo. It never writes to the checkout and never runs candidate code in the API
process. The container has no network, no Docker socket, and read-only access
to the staged files.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError

from app.config import Settings
from app.patch_schemas import PatchProposal
from app.patch_validation import validate_patch_proposal

SANDBOX_IMAGE = "omnisight-shoplite-sandbox:1"
SANDBOX_TIMEOUT_SECONDS = 45
SANDBOX_WORKSPACE_LIMIT = 2 * 1024 * 1024
DEMO_ROOT = Path(__file__).resolve().parents[2] / "demo-app"
_RUN_LOCK = threading.Lock()


class SandboxChecks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline_mobile_overflow_px: float
    patched_mobile_overflow_px: float
    patched_desktop_overflow_px: float
    order_confirmation: bool
    browser_error_count: int
    blocked_external_request_count: int


class ShopLiteSandboxReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["verified", "failed"]
    target: str = "ShopLite checkout demo"
    proposal_id: str
    checks: SandboxChecks


class SandboxFailure(RuntimeError):
    """Safe, user-facing sandbox failure with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _tree_size(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SandboxFailure("SANDBOX_UNSAFE_SOURCE", "ShopLite source contains a symlink")
        if path.is_file():
            total += path.stat().st_size
            if total > SANDBOX_WORKSPACE_LIMIT:
                raise SandboxFailure("SANDBOX_SOURCE_TOO_LARGE", "ShopLite demo exceeds the sandbox size limit")
    return total


def _stage_demo(proposal: PatchProposal, stage_root: Path, settings: Settings) -> None:
    """Copy the fixed demo twice and apply one exact-match CSS edit to candidate."""
    if not DEMO_ROOT.is_dir():
        raise SandboxFailure("SANDBOX_SOURCE_MISSING", "The local ShopLite demo directory is missing")
    _tree_size(DEMO_ROOT)

    if len(proposal.files) != 1 or proposal.files[0].path != "styles/checkout.css":
        raise SandboxFailure("SANDBOX_TARGET_REJECTED", "Only demo-app/styles/checkout.css is permitted")
    patch_file = proposal.files[0]
    if len(patch_file.edits) != 1:
        raise SandboxFailure("SANDBOX_TARGET_REJECTED", "The ShopLite demo accepts exactly one CSS edit")
    reasons = validate_patch_proposal(proposal, settings=settings)
    if reasons:
        raise SandboxFailure("SANDBOX_PATCH_REJECTED", "The proposal failed patch safety validation")

    baseline = stage_root / "baseline"
    candidate = stage_root / "candidate"
    shutil.copytree(DEMO_ROOT, baseline, symlinks=True)
    shutil.copytree(DEMO_ROOT, candidate, symlinks=True)
    # TemporaryDirectory uses mode 0700. The sandbox runs as UID 10001, so it
    # cannot traverse the bind-mounted workspace unless the staged root is
    # readable/searchable by that user. Its contents are only the fixed demo.
    stage_root.chmod(0o755)
    _tree_size(baseline)
    _tree_size(candidate)
    target = candidate / "styles" / "checkout.css"
    if target.is_symlink() or not target.is_file():
        raise SandboxFailure("SANDBOX_UNSAFE_SOURCE", "The approved CSS target is not a regular file")
    source = target.read_text(encoding="utf-8")
    edit = patch_file.edits[0]
    if source.count(edit.old_text) != 1:
        raise SandboxFailure("SANDBOX_EDIT_NOT_APPLICABLE", "The approved CSS edit must match exactly once")
    target.write_text(source.replace(edit.old_text, edit.new_text, 1), encoding="utf-8")


def _docker_command(stage_root: Path, container_name: str) -> list[str]:
    docker = shutil.which("docker")
    if not docker:
        raise SandboxFailure("SANDBOX_DOCKER_UNAVAILABLE", "Docker CLI was not found on the API host")
    source = str(stage_root.resolve())
    if "," in source:
        raise SandboxFailure("SANDBOX_INVALID_STAGE_PATH", "The temporary sandbox path contains unsupported characters")
    return [
        docker,
        "run",
        "--rm",
        "--name",
        container_name,
        "--init",
        "--pull=never",
        "--network=none",
        "--cpus=1",
        "--memory=512m",
        "--memory-swap=512m",
        "--pids-limit=128",
        "--shm-size=128m",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=10001:10001",
        "--mount",
        f"type=bind,source={source},target=/workspace,readonly",
        SANDBOX_IMAGE,
    ]


def _container_start_failure(stderr: str) -> SandboxFailure:
    """Classify Docker startup errors without returning raw host diagnostics."""
    diagnostic = (stderr or "").lower()
    if "no such image" in diagnostic:
        return SandboxFailure("SANDBOX_IMAGE_MISSING", "Build the documented ShopLite sandbox image first")
    if "mount" in diagnostic and any(word in diagnostic for word in ("denied", "invalid", "failed", "error")):
        category = "mount failure"
    elif "permission denied" in diagnostic or "operation not permitted" in diagnostic:
        category = "permission failure"
    elif "executable file not found" in diagnostic or "no such file or directory" in diagnostic:
        category = "missing container executable or path"
    else:
        category = "Docker runtime failure"
    detail = next((line.strip() for line in reversed((stderr or "").splitlines()) if line.strip()), "")
    detail = re.sub(r"/tmp/omnisight-shoplite-[^/\s]+", "<staged-demo>", detail)
    detail = detail[:180]
    suffix = f": {detail}" if detail else ""
    return SandboxFailure(
        "SANDBOX_CONTAINER_START_FAILED",
        f"Docker could not start the ShopLite container ({category}){suffix}",
    )


def run_shoplite_sandbox(
    proposal: PatchProposal,
    *,
    proposal_id: str,
    settings: Settings,
) -> ShopLiteSandboxReport:
    """Run the isolated browser check after validating the fixed demo edit."""
    if not settings.enable_shoplite_sandbox:
        raise SandboxFailure("SANDBOX_DISABLED", "The ShopLite Docker sandbox is disabled")
    if not settings.shoplite_sandbox_token or not settings.shoplite_sandbox_token.get_secret_value().strip():
        raise SandboxFailure("SANDBOX_NOT_CONFIGURED", "Set the local ShopLite sandbox approval token first")
    if not _RUN_LOCK.acquire(blocking=False):
        raise SandboxFailure("SANDBOX_BUSY", "A ShopLite sandbox run is already in progress")
    try:
        with tempfile.TemporaryDirectory(prefix="omnisight-shoplite-") as temporary:
            stage_root = Path(temporary)
            _stage_demo(proposal, stage_root, settings)
            container_name = f"omni-shoplite-{uuid4().hex[:16]}"
            command = _docker_command(stage_root, container_name)
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=min(settings.shoplite_sandbox_timeout_seconds, SANDBOX_TIMEOUT_SECONDS),
                    shell=False,
                )
            except subprocess.TimeoutExpired:
                # If the CLI is interrupted while attached, explicitly remove
                # the named container so the bounded worker cannot linger.
                try:
                    subprocess.run(
                        [command[0], "rm", "--force", container_name],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=5,
                        shell=False,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
                raise SandboxFailure("SANDBOX_TIMEOUT", "ShopLite sandbox exceeded its time limit") from None
            except OSError:
                raise SandboxFailure("SANDBOX_DOCKER_UNAVAILABLE", "Docker could not start the ShopLite sandbox") from None
            if completed.returncode not in {0, 1}:
                # Keep provider/container diagnostics out of API responses.
                raise _container_start_failure(completed.stderr or "")
            if not completed.stdout.strip():
                raise _container_start_failure(completed.stderr or "")
            try:
                payload = json.loads(completed.stdout)
            except ValueError:
                raise SandboxFailure(
                    "SANDBOX_INVALID_RESULT", "The ShopLite container did not return JSON"
                ) from None
            if not isinstance(payload, dict):
                raise SandboxFailure(
                    "SANDBOX_INVALID_RESULT", "The ShopLite container returned a non-object report"
                )
            try:
                if isinstance(payload, dict) and isinstance(payload.get("error"), str):
                    stage = payload.get("stage")
                    error_type = payload["error"]
                    if (
                        isinstance(stage, str)
                        and stage.replace("_", "").isalnum()
                        and isinstance(error_type, str)
                        and error_type.isalnum()
                    ):
                        raise SandboxFailure(
                            "SANDBOX_BROWSER_FAILED",
                            f"The ShopLite browser check failed during {stage} ({error_type})",
                        )
                    raise SandboxFailure(
                        "SANDBOX_INVALID_RESULT", "The ShopLite sandbox returned an invalid report"
                    )
                if "checks" not in payload:
                    keys = ",".join(sorted(str(key) for key in payload.keys())[:8]) or "none"
                    raise SandboxFailure(
                        "SANDBOX_INVALID_RESULT",
                        f"The ShopLite report omitted checks (report fields: {keys})",
                    )
                checks = SandboxChecks.model_validate(payload["checks"])
                passed = (
                    payload["passed"] is True
                    and checks.baseline_mobile_overflow_px > 0
                    and checks.patched_mobile_overflow_px <= 1
                    and checks.patched_desktop_overflow_px <= 1
                    and checks.order_confirmation
                    and checks.browser_error_count == 0
                    and checks.blocked_external_request_count == 0
                )
            except SandboxFailure:
                raise
            except ValidationError as exc:
                fields = sorted({".".join(str(part) for part in error.get("loc", ())) for error in exc.errors()})
                summary = ",".join(fields[:8]) or "unknown fields"
                raise SandboxFailure(
                    "SANDBOX_INVALID_RESULT",
                    f"The ShopLite report had invalid checks ({summary})",
                ) from None
            except (ValueError, KeyError, TypeError):
                raise SandboxFailure("SANDBOX_INVALID_RESULT", "The ShopLite sandbox returned an invalid report") from None
            if checks.baseline_mobile_overflow_px <= 0:
                raise SandboxFailure("SANDBOX_BASELINE_INVALID", "The demo no longer reproduces its expected mobile defect")
            if not passed:
                return ShopLiteSandboxReport(status="failed", proposal_id=proposal_id, checks=checks)
            return ShopLiteSandboxReport(status="verified", proposal_id=proposal_id, checks=checks)
    finally:
        _RUN_LOCK.release()
