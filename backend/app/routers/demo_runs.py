"""Loopback-only API for the fixed ShopLite end-to-end demo."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from pydantic import BaseModel, ConfigDict

from app.config import Settings, get_settings
from app.demo_run_store import DemoRunStore, get_demo_run_store
from app.errors import DomainError, ErrorCode
from app.patch_storage import PatchStore, get_patch_store
from app.shoplite_sandbox import SandboxFailure, run_shoplite_sandbox

router = APIRouter(prefix="/api/v1/demo/shoplite", tags=["ShopLite demo"])

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = BACKEND_ROOT.parent
DEMO_APP_ROOT = REPOSITORY_ROOT / "demo-app"
PIPELINE_SCRIPT = BACKEND_ROOT / "scripts" / "run_demo_pipeline.py"
_RUN_LOCK = threading.Lock()
_SENSITIVE_ENV_NAMES = ("API_KEY", "_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


class SandboxConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation: Literal["approve-demo-only"]


def _require_local_ui(
    request: Request, settings: Settings, *, require_origin: bool = True
) -> None:
    client_host = request.client.host if request.client else ""
    try:
        if not ipaddress.ip_address(client_host).is_loopback:
            raise ValueError
    except ValueError:
        raise DomainError(
            ErrorCode.FORBIDDEN,
            "The ShopLite demo is available only from this computer",
            status_code=status.HTTP_403_FORBIDDEN,
        ) from None

    origin = request.headers.get("origin")
    if (require_origin and not origin) or (origin and origin not in settings.allowed_origins):
        raise DomainError(
            ErrorCode.FORBIDDEN,
            "Open OmniSight from an allowed local application address",
            status_code=status.HTTP_403_FORBIDDEN,
        )


def _worker_environment() -> dict[str, str]:
    """Keep provider and publishing credentials out of the local runner child."""
    return {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in _SENSITIVE_ENV_NAMES)
    }


class _QuietDemoHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


def _run_demo_pipeline(run_id: str, api_url: str, store: DemoRunStore) -> None:
    """Capture the fixed target, then use the existing API pipeline for AI and patching."""
    if not _RUN_LOCK.acquire(blocking=False):
        store.update(run_id, status="failed", error="Another ShopLite run is already active.")
        return

    server: ThreadingHTTPServer | None = None
    server_thread: threading.Thread | None = None
    try:
        store.update(run_id, status="running")
        if not DEMO_APP_ROOT.is_dir() or not PIPELINE_SCRIPT.is_file():
            raise RuntimeError("The fixed ShopLite demo files are missing from this checkout.")

        handler = partial(_QuietDemoHandler, directory=str(DEMO_APP_ROOT))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        checkout_url = f"http://127.0.0.1:{server.server_port}"
        command = [
            sys.executable,
            str(PIPELINE_SCRIPT),
            "--checkout-url",
            checkout_url,
            "--api-url",
            api_url,
            "--timeout",
            "180",
        ]
        completed = subprocess.run(
            command,
            cwd=BACKEND_ROOT,
            env=_worker_environment(),
            capture_output=True,
            text=True,
            timeout=240,
            shell=False,
            check=False,
        )
        try:
            result = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError):
            detail = (completed.stderr or "").strip().splitlines()
            message = detail[-1][:400] if detail else "The ShopLite runner returned an invalid result."
            raise RuntimeError(message) from None
        if not isinstance(result, dict):
            raise RuntimeError("The ShopLite runner returned an invalid result.")
        result.pop("summary_file", None)
        capture_result = result.get("capture")
        if isinstance(capture_result, dict):
            capture_result.pop("artifact_root", None)

        safe_status = result.get("status")
        if safe_status not in {"awaiting_sandbox_approval", "patch_rejected", "needs_inspection", "validation_failed", "verified"}:
            safe_status = "failed"
        error = None
        if completed.returncode not in {0, 2}:
            safe_status = "failed"
            detail = (completed.stderr or "").strip().splitlines()
            error = detail[-1][:400] if detail else "The ShopLite pipeline failed. Check the backend logs."
        store.update(run_id, status=safe_status, result=result, error=error)
    except subprocess.TimeoutExpired:
        store.update(run_id, status="failed", error="The ShopLite pipeline exceeded its four-minute time limit.")
    except Exception as exc:
        message = str(exc)
        message = re.sub(r"(?i)(key|token|secret|password)\s*[:=]\s*\S+", r"\1=[redacted]", message)
        store.update(run_id, status="failed", error=message[:400])
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=2)
        _RUN_LOCK.release()


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
def start_shoplite_run(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
    store: DemoRunStore = Depends(get_demo_run_store),
) -> dict:
    """Start a fixed-target browser capture, Gemini detection, and safe proposal."""
    _require_local_ui(request, settings)
    parsed = urlsplit(str(request.base_url))
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise DomainError(
            ErrorCode.FORBIDDEN,
            "The ShopLite demo API must be reached over local HTTP",
            status_code=status.HTTP_403_FORBIDDEN,
        )
    api_url = f"http://127.0.0.1:{parsed.port or 80}"
    run = store.create()
    if run is None:
        raise DomainError(
            ErrorCode.CONFLICT,
            "A ShopLite run is already active or awaiting sandbox approval",
            status_code=status.HTTP_409_CONFLICT,
        )
    background_tasks.add_task(_run_demo_pipeline, run.run_id, api_url, store)
    return run.response()


@router.get("/runs")
def list_shoplite_runs(
    request: Request,
    settings: Settings = Depends(get_settings),
    store: DemoRunStore = Depends(get_demo_run_store),
) -> list[dict]:
    """List fixed-target ShopLite runs for the loopback dashboard history."""
    _require_local_ui(request, settings, require_origin=False)
    return [run.response() for run in store.list()]


@router.get("/runs/{run_id}")
def get_shoplite_run(
    run_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    store: DemoRunStore = Depends(get_demo_run_store),
) -> dict:
    _require_local_ui(request, settings, require_origin=False)
    run = store.get(run_id)
    if run is None:
        raise DomainError(ErrorCode.NOT_FOUND, "ShopLite run not found", status_code=status.HTTP_404_NOT_FOUND)
    return run.response()


@router.post("/runs/{run_id}/validate")
def validate_shoplite_run(
    run_id: str,
    confirmation: SandboxConfirmation,
    request: Request,
    settings: Settings = Depends(get_settings),
    store: DemoRunStore = Depends(get_demo_run_store),
    patch_store: PatchStore = Depends(get_patch_store),
) -> dict:
    """Require an explicit local UI confirmation before starting Docker."""
    _require_local_ui(request, settings)
    run = store.get(run_id)
    if run is None:
        raise DomainError(ErrorCode.NOT_FOUND, "ShopLite run not found", status_code=status.HTTP_404_NOT_FOUND)
    if run.status != "awaiting_sandbox_approval":
        raise DomainError(
            ErrorCode.CONFLICT,
            "This ShopLite run is not waiting for sandbox approval",
            status_code=status.HTTP_409_CONFLICT,
        )
    proposal = run.result.get("proposal")
    proposal_id = proposal.get("proposal_id") if isinstance(proposal, dict) else None
    stored_proposal = patch_store.get_proposal(proposal_id) if isinstance(proposal_id, str) else None
    record = patch_store.get(proposal_id) if isinstance(proposal_id, str) else None
    if stored_proposal is None or record is None or record.status.value != "proposed":
        raise DomainError(
            ErrorCode.PATCH_PROPOSAL_NOT_FOUND,
            "The approved patch proposal is no longer available; start a new scan",
            status_code=status.HTTP_409_CONFLICT,
        )

    store.update(run_id, status="validating", error=None)
    try:
        report = run_shoplite_sandbox(
            stored_proposal,
            proposal_id=proposal_id,
            settings=settings,
        )
    except SandboxFailure as exc:
        store.update(
            run_id,
            status="validation_failed",
            error=str(exc),
            result={**run.result, "sandbox_error": exc.code},
        )
        raise DomainError(
            ErrorCode.SHOPLITE_SANDBOX_REJECTED,
            str(exc),
            status_code=status.HTTP_409_CONFLICT,
            details=[{"field": "sandbox", "code": exc.code}],
        ) from None

    result = {**run.result, "sandbox": report.model_dump(mode="json")}
    state = "verified" if report.status == "verified" else "validation_failed"
    updated = store.update(run_id, status=state, result=result)
    return updated.response() if updated else {"run_id": run_id, "status": state, "result": result}
