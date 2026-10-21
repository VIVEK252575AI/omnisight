"""Scan lifecycle endpoints.

Routes are unchanged from the original single-module app (paths and methods
are preserved); they now live on a router so later modules (CI webhooks,
orchestration) can be added without growing ``main.py``.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.config import Settings, get_settings
from app.errors import DomainError, ErrorCode
from app.ids import utcnow
from app.schemas import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    ScanCreateRequest,
    ScanResponse,
    ScanStatus,
)
from app.storage import ScanStore, get_store

logger = logging.getLogger("omnisight")

router = APIRouter(prefix="/api", tags=["scans"])


@router.get("/scans", response_model=list[ScanResponse])
def list_scans(store: ScanStore = Depends(get_store)) -> list[ScanResponse]:
    """List all scans (oldest first)."""
    return store.list()


@router.post(
    "/scans",
    response_model=ScanResponse,
    status_code=status.HTTP_201_CREATED,
    responses={422: {"description": "Validation error (malformed URL, routes, or viewports)"}},
)
def create_scan(
    request: ScanCreateRequest,
    settings: Settings = Depends(get_settings),
    store: ScanStore = Depends(get_store),
) -> ScanResponse:
    """Validate a scan request and register it in `created` state."""
    if len(str(request.target_url)) > settings.max_scan_url_length:
        raise DomainError(
            ErrorCode.URL_TOO_LONG,
            (
                f"target_url exceeds maximum length "
                f"({len(str(request.target_url))} > {settings.max_scan_url_length})"
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    record = store.create(request)
    logger.info(
        "scan created",
        extra={"scan_id": str(record.scan_id), "target_url": str(record.target_url)},
    )
    return record


@router.get(
    "/scans/{scan_id}",
    response_model=ScanResponse,
    responses={404: {"description": "Unknown scan_id"}},
)
def get_scan(scan_id: UUID, store: ScanStore = Depends(get_store)) -> ScanResponse:
    """Fetch a single scan record by id."""
    record = store.get(scan_id)
    if record is None:
        raise DomainError(
            ErrorCode.SCAN_NOT_FOUND,
            f"scan {scan_id} not found",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return record


@router.post(
    "/scans/{scan_id}/transition",
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
    store: ScanStore = Depends(get_store),
) -> ScanResponse:
    """Advance a scan through its lifecycle.

    Transitions not listed in LEGAL_TRANSITIONS are rejected with 409 —
    a failed build or failed visual verification can never surface as a
    successful verified repair.
    """
    record = store.get(scan_id)
    if record is None:
        raise DomainError(
            ErrorCode.SCAN_NOT_FOUND,
            f"scan {scan_id} not found",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    current = record.status
    if current in TERMINAL_STATES:
        raise DomainError(
            ErrorCode.TERMINAL_STATE,
            f"scan is in terminal state {current.value} and cannot transition",
            status_code=status.HTTP_409_CONFLICT,
        )
    if to_status not in LEGAL_TRANSITIONS[current]:
        raise DomainError(
            ErrorCode.ILLEGAL_TRANSITION,
            (
                f"illegal transition from {current.value} to {to_status.value}"
                f" — allowed next states: "
                + ", ".join(sorted(s.value for s in LEGAL_TRANSITIONS[current]))
            ),
            status_code=status.HTTP_409_CONFLICT,
        )
    updated = record.model_copy(
        update={"status": to_status, "error": error, "updated_at": utcnow()}
    )
    store.replace(updated)
    logger.info(
        "scan transitioned",
        extra={"scan_id": str(scan_id), "from": current.value, "to": to_status.value},
    )
    return updated
