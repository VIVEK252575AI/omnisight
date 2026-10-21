"""OmniSight backend entry point (FastAPI).

Foundation slice: explicit contracts + deterministic state transitions.
Background execution (Playwright worker, VLM, patch engine) is deliberately
NOT wired here yet — those land in their own task slices and are gated by
feature flags that stay off until the modules actually exist.
"""

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.gemini import router as ai_router
from app.schemas import (
    LEGAL_TRANSITIONS,
    ScanCreateRequest,
    ScanResponse,
    ScanStatus,
)
from app.storage import ScanStore

logger = logging.getLogger("omnisight")
logging.basicConfig(level=logging.INFO)

store = ScanStore()

app = FastAPI(
    title="OmniSight Backend",
    version="0.1.0",
    description="Central coordinator for the Detect -> Fix -> Verify -> Deliver workflow.",
)


def _cors_origins(settings: Settings) -> list[str]:
    return settings.allowed_origins


app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ai_router)


@app.get("/health")
def health(settings: Settings = Depends(get_settings)) -> dict:
    """Liveness probe for the API process."""
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.app_version,
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.post(
    "/api/scans",
    response_model=ScanResponse,
    status_code=status.HTTP_201_CREATED,
    responses={422: {"description": "Validation error (malformed URL, routes, or viewports)"}},
)
def create_scan(
    request: ScanCreateRequest,
    settings: Settings = Depends(get_settings),
) -> ScanResponse:
    """Validate a scan request and register it in `created` state."""
    if len(str(request.target_url)) > settings.max_scan_url_length:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"target_url exceeds maximum length "
                f"({len(str(request.target_url))} > {settings.max_scan_url_length})"
            ),
        )
    record = store.create(request)
    logger.info(
        "scan created",
        extra={"scan_id": str(record.scan_id), "target_url": str(record.target_url)},
    )
    return record


@app.get(
    "/api/scans",
    response_model=list[ScanResponse],
)
def list_scans() -> list[ScanResponse]:
    """List all scans (oldest first)."""
    return store.list()


@app.get(
    "/api/scans/{scan_id}",
    response_model=ScanResponse,
    responses={404: {"description": "Unknown scan_id"}},
)
def get_scan(scan_id: UUID) -> ScanResponse:
    """Fetch a single scan record by id."""
    record = store.get(scan_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"scan {scan_id} not found",
        )
    return record


@app.post(
    "/api/scans/{scan_id}/transition",
    response_model=ScanResponse,
    responses={
        404: {"description": "Unknown scan_id"},
        409: {"description": "Illegal state transition"},
    },
)
def transition_scan(
    scan_id: UUID,
    to_status: ScanStatus,
    error: str | None = None,
) -> ScanResponse:
    """Advance a scan through its lifecycle.

    Transitions not listed in LEGAL_TRANSITIONS are rejected with 409 —
    a failed build or failed visual verification can never surface as a
    successful verified repair.
    """
    record = store.get(scan_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"scan {scan_id} not found",
        )
    current = record.status
    if current in [ScanStatus.ready_for_review, ScanStatus.failed, ScanStatus.cancelled]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"scan is in terminal state {current.value} and cannot transition",
        )
    if to_status not in LEGAL_TRANSITIONS[current]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"illegal transition from {current.value} to {to_status.value}"
                f" — allowed next states: "
                + ", ".join(sorted(s.value for s in LEGAL_TRANSITIONS[current]))
            ),
        )
    updated = record.model_copy(
        update={"status": to_status, "error": error, "updated_at": datetime.now(timezone.utc)}
    )
    store.replace(updated)
    logger.info(
        "scan transitioned",
        extra={"scan_id": str(scan_id), "from": current.value, "to": to_status.value},
    )
    return updated


# The root cause for unmanaged exceptions: return a consistent JSON error
# envelope without leaking stack traces to clients.
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error — check server logs for details."},
    )
