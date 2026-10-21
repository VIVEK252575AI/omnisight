"""Loopback-only API for the fixed ShopLite end-to-end demo."""

from __future__ import annotations

import ipaddress
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from difflib import unified_diff
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.demo_run_store import DemoRunStore, get_demo_run_store
from app.evidence_storage import EvidenceStore, get_evidence_store
from app.errors import DomainError, ErrorCode
from app.ids import utcnow
from app.artifacts import artifact_root
from app.patch_storage import PatchStore, get_patch_store
from app.patch_validation import validate_patch_proposal
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
    base_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ProposalRejection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


def _persist_sandbox_screenshot(
    image_bytes: bytes,
    label: str,
    *,
    run_id: str,
    settings: Settings,
    evidence_store: EvidenceStore,
) -> str:
    """Store a before/after screenshot as private, run-linked evidence."""
    if label not in {"baseline_screenshot_artifact_id", "candidate_screenshot_artifact_id"}:
        raise ValueError("unknown ShopLite screenshot label")
    if not re.fullmatch(r"demo_[0-9a-f]{32}", run_id):
        raise ValueError("invalid ShopLite run identifier")
    root = artifact_root(settings)
    run_directory = (root / "shoplite-sandbox" / run_id).resolve()
    try:
        run_directory.relative_to(root)
    except ValueError:
        raise ValueError("ShopLite screenshot path escaped the private artifact root") from None
    run_directory.mkdir(parents=True, exist_ok=True)

    artifact_id = uuid4()
    relative_path = (Path("shoplite-sandbox") / run_id / f"{artifact_id}.png").as_posix()
    artifact_path = root / relative_path
    try:
        with artifact_path.open("xb") as screenshot_file:
            screenshot_file.write(image_bytes)
        evidence_store.add_artifact(run_id, artifact_id=artifact_id, artifact_path=artifact_path)
    except Exception as exc:
        artifact_path.unlink(missing_ok=True)
        raise SandboxFailure(
            "SANDBOX_EVIDENCE_STORAGE_FAILED",
            "Could not persist the sandbox screenshot evidence for this run",
        ) from exc
    return str(artifact_id)


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


@router.get("/workspace")
def get_shoplite_workspace(
    request: Request,
    settings: Settings = Depends(get_settings),
    store: DemoRunStore = Depends(get_demo_run_store),
) -> dict:
    """Show the persisted fixed ShopLite project and published test-flow version."""
    _require_local_ui(request, settings, require_origin=False)
    try:
        return store.workspace()
    except Exception as exc:
        # Keep database internals out of the response while retaining server diagnostics.
        raise DomainError(
            ErrorCode.INTERNAL_ERROR,
            "The ShopLite workspace could not be loaded. Check the local database connection.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc


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


@router.get("/runs/{run_id}/patch-preview")
def get_shoplite_patch_preview(
    run_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    store: DemoRunStore = Depends(get_demo_run_store),
    patch_store: PatchStore = Depends(get_patch_store),
) -> dict:
    """Return a bounded unified diff for the exact validated ShopLite proposal."""
    _require_local_ui(request, settings, require_origin=False)
    run = store.get(run_id)
    if run is None:
        raise DomainError(ErrorCode.NOT_FOUND, "ShopLite run not found", status_code=status.HTTP_404_NOT_FOUND)
    if run.status != "awaiting_sandbox_approval":
        raise DomainError(
            ErrorCode.CONFLICT,
            "Patch preview is available only while the run is waiting for review",
            status_code=status.HTTP_409_CONFLICT,
        )

    proposal_record = run.result.get("proposal")
    proposal_id = proposal_record.get("proposal_id") if isinstance(proposal_record, dict) else None
    proposal = patch_store.get_proposal(proposal_id) if isinstance(proposal_id, str) else None
    record = patch_store.get(proposal_id) if isinstance(proposal_id, str) else None
    finding_ids = {
        item.get("finding_id") for item in run.result.get("findings", [])
        if isinstance(item, dict) and isinstance(item.get("finding_id"), str)
    }
    if (
        proposal is None
        or record is None
        or record.status.value != "proposed"
        or record.finding_id not in finding_ids
        or proposal.finding_id != record.finding_id
    ):
        raise DomainError(
            ErrorCode.PATCH_PROPOSAL_NOT_FOUND,
            "The validated proposal is no longer available for review",
            status_code=status.HTTP_409_CONFLICT,
        )
    if (
        len(proposal.files) != 1
        or proposal.files[0].path != "styles/checkout.css"
        or len(proposal.files[0].edits) != 1
    ):
        raise DomainError(
            ErrorCode.SHOPLITE_SANDBOX_REJECTED,
            "Only the fixed ShopLite checkout CSS edit can be previewed",
            status_code=status.HTTP_409_CONFLICT,
        )
    if validate_patch_proposal(proposal, settings=settings, expected_finding_id=record.finding_id):
        raise DomainError(
            ErrorCode.SHOPLITE_SANDBOX_REJECTED,
            "The proposal no longer passes the configured patch safety checks",
            status_code=status.HTTP_409_CONFLICT,
        )

    configured_root = settings.patch_source_root.strip()
    root = Path(configured_root).resolve() if configured_root else None
    if root is None or not root.is_dir() or root != DEMO_APP_ROOT.resolve():
        raise DomainError(
            ErrorCode.SHOPLITE_SANDBOX_REJECTED,
            "The approved ShopLite source directory is unavailable",
            status_code=status.HTTP_409_CONFLICT,
        )
    source_path = root / "styles" / "checkout.css"
    try:
        resolved_source = source_path.resolve(strict=True)
        resolved_source.relative_to(root)
        if source_path.is_symlink() or not resolved_source.is_file() or resolved_source.stat().st_size > 1_048_576:
            raise ValueError
        source_bytes = resolved_source.read_bytes()
        source_text = resolved_source.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError):
        raise DomainError(
            ErrorCode.SHOPLITE_SANDBOX_REJECTED,
            "The approved ShopLite CSS file is missing or cannot be reviewed safely",
            status_code=status.HTTP_409_CONFLICT,
        ) from None

    edit = proposal.files[0].edits[0]
    if source_text.count(edit.old_text) != 1 or edit.old_text == edit.new_text:
        raise DomainError(
            ErrorCode.CONFLICT,
            "The proposal no longer matches the current ShopLite source; start a new scan",
            status_code=status.HTTP_409_CONFLICT,
        )
    candidate_text = source_text.replace(edit.old_text, edit.new_text, 1)
    diff = "".join(unified_diff(
        source_text.splitlines(keepends=True),
        candidate_text.splitlines(keepends=True),
        fromfile="a/demo-app/styles/checkout.css",
        tofile="b/demo-app/styles/checkout.css",
        n=3,
    ))
    if not diff or len(diff.encode("utf-8")) > 32_768:
        raise DomainError(
            ErrorCode.SHOPLITE_SANDBOX_REJECTED,
            "The proposed change could not be represented as a bounded review diff",
            status_code=status.HTTP_409_CONFLICT,
        )
    candidate_bytes = candidate_text.encode("utf-8")
    return {
        "proposal_id": proposal_id,
        "file_path": "demo-app/styles/checkout.css",
        "base_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "candidate_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
        "diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "diff": diff,
    }


@router.post("/runs/{run_id}/validate")
def validate_shoplite_run(
    run_id: str,
    confirmation: SandboxConfirmation,
    request: Request,
    settings: Settings = Depends(get_settings),
    store: DemoRunStore = Depends(get_demo_run_store),
    patch_store: PatchStore = Depends(get_patch_store),
    evidence_store: EvidenceStore = Depends(get_evidence_store),
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
    preview = get_shoplite_patch_preview(run_id, request, settings, store, patch_store)
    if confirmation.base_sha256 != preview["base_sha256"]:
        raise DomainError(
            ErrorCode.CONFLICT,
            "The ShopLite source changed after review; reload the preview before approving",
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
            screenshot_store=lambda image_bytes, label: _persist_sandbox_screenshot(
                image_bytes,
                label,
                run_id=run_id,
                settings=settings,
                evidence_store=evidence_store,
            ),
            expected_base_sha256=confirmation.base_sha256,
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


@router.post("/runs/{run_id}/reject-proposal")
def reject_shoplite_proposal(
    run_id: str,
    decision: ProposalRejection,
    request: Request,
    settings: Settings = Depends(get_settings),
    store: DemoRunStore = Depends(get_demo_run_store),
    patch_store: PatchStore = Depends(get_patch_store),
) -> dict:
    """Persist a local owner's rejection and prevent later validation or publishing."""
    _require_local_ui(request, settings)
    run = store.get(run_id)
    if run is None:
        raise DomainError(ErrorCode.NOT_FOUND, "ShopLite run not found", status_code=status.HTTP_404_NOT_FOUND)
    if run.status != "awaiting_sandbox_approval":
        raise DomainError(
            ErrorCode.CONFLICT,
            "This ShopLite run is no longer waiting for a proposal decision",
            status_code=status.HTTP_409_CONFLICT,
        )
    proposal_info = run.result.get("proposal")
    proposal_id = proposal_info.get("proposal_id") if isinstance(proposal_info, dict) else None
    record = patch_store.get(proposal_id) if isinstance(proposal_id, str) else None
    finding_ids = {
        item.get("finding_id") for item in run.result.get("findings", [])
        if isinstance(item, dict) and isinstance(item.get("finding_id"), str)
    }
    if record is None or record.status.value != "proposed" or record.finding_id not in finding_ids:
        raise DomainError(
            ErrorCode.PATCH_PROPOSAL_NOT_FOUND,
            "The proposed patch is no longer available for review",
            status_code=status.HTTP_409_CONFLICT,
        )

    reason = decision.reason.strip()
    if not reason:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            "Enter a reason so the review decision is recorded",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    rejected = patch_store.reject_by_human(proposal_id, reason=reason)
    if rejected is None:
        raise DomainError(
            ErrorCode.CONFLICT,
            "The proposal changed while the review decision was being saved",
            status_code=status.HTTP_409_CONFLICT,
        )
    reviewed_at = utcnow().isoformat()
    result = {
        **run.result,
        "proposal": {**proposal_info, "status": "rejected", "rejection_reasons": ["human_rejected"]},
        "proposal_review": {"decision": "rejected", "reason": reason, "reviewed_at": reviewed_at},
    }
    updated = store.update(run_id, status="proposal_rejected", result=result)
    return updated.response() if updated else {"run_id": run_id, "status": "proposal_rejected", "result": result}
