"""SIMULATED CI/CD webhook receiver.

Accepts synthetic build events (``build.started`` / ``build.succeeded`` /
``build.failed``), validates them, persists them idempotently and makes them
queryable. This is a Week 1 simulation milestone: **there is no real GitHub
Actions or Jenkins integration**. A ``build.succeeded`` event creates a scan
stub in ``created`` state only — no browser, model or GitHub call is triggered
here.

Endpoints (all under ``/api/v1/webhooks``)::

    POST /ci                 -> 202 accepted (200 duplicate)
    GET  /events             -> paginated list, optional ?event_type=
    GET  /events/{event_id}  -> single event (404 envelope if unknown)
"""

import hashlib
import hmac
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import HttpUrl, ValidationError

from app.config import Settings, get_settings
from app.errors import DomainError, ErrorCode, validation_details
from app.event_storage import CIEventStore, get_event_store
from app.schemas import (
    BuildEvent,
    BuildEventType,
    CIEventRecord,
    ScanCreateRequest,
    WebhookAcceptedResponse,
)
from app.storage import ScanStore, get_store

logger = logging.getLogger("omnisight")

router = APIRouter(prefix="/api/v1/webhooks", tags=["ci-webhooks"])

SIGNATURE_HEADER = "X-OmniSight-Signature"
_SIGNATURE_PREFIX = "sha256="


async def _read_bounded_body(request: Request, settings: Settings) -> bytes:
    """Read the raw body, rejecting anything over the configured limit."""
    limit = settings.webhook_max_body_bytes
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise DomainError(
                    ErrorCode.PAYLOAD_TOO_LARGE,
                    f"request body exceeds {limit} bytes",
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                )
        except ValueError:
            pass
    body = await request.body()
    if len(body) > limit:
        raise DomainError(
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"request body exceeds {limit} bytes",
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )
    return body


def _verify_signature(raw: bytes, signature: str | None, settings: Settings) -> None:
    """Verify the HMAC-SHA256 signature when a shared secret is configured.

    When no secret is set the receiver is unauthenticated (dev/simulation
    only; a startup warning is logged). The secret is never echoed back.
    """
    secret = settings.ci_webhook_secret
    if not secret:
        return
    if not signature:
        raise DomainError(
            ErrorCode.UNAUTHORIZED,
            f"missing {SIGNATURE_HEADER} header",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    provided = signature[len(_SIGNATURE_PREFIX):] if signature.startswith(
        _SIGNATURE_PREFIX
    ) else signature
    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided):
        raise DomainError(
            ErrorCode.UNAUTHORIZED,
            "invalid signature",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )


def _check_host_allowed(url: HttpUrl, settings: Settings) -> None:
    """Reject target hosts outside the optional allow-list (SSRF guard).

    Empty allow-list (the default) is permissive — intended for local
    simulation. Set ``target_host_allowlist`` to restrict in other contexts.
    """
    allow = settings.target_host_allowlist
    if not allow:
        return
    host = (url.host or "").lower()
    if host not in {allowed.lower() for allowed in allow}:
        raise DomainError(
            ErrorCode.FORBIDDEN,
            f"target host not allowed: {host}",
            status_code=status.HTTP_403_FORBIDDEN,
        )


@router.post(
    "/ci",
    response_model=WebhookAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        200: {"description": "Duplicate event_id — original result returned"},
        401: {"description": "Missing or invalid signature"},
        413: {"description": "Request body too large"},
        422: {"description": "Invalid event payload"},
    },
)
async def receive_ci_event(
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
    scan_store: ScanStore = Depends(get_store),
    event_store: CIEventStore = Depends(get_event_store),
) -> WebhookAcceptedResponse:
    """Accept a SIMULATED build event, persist it, and stub a scan on success."""
    raw = await _read_bounded_body(request, settings)
    _verify_signature(raw, request.headers.get(SIGNATURE_HEADER), settings)

    try:
        event = BuildEvent.model_validate_json(raw)
    except ValidationError as exc:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            "Request validation failed",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            details=validation_details(exc.errors()),
        ) from exc

    def make_scan() -> UUID | None:
        assert event.target_url is not None  # guaranteed by BuildEvent validator
        _check_host_allowed(event.target_url, settings)
        stub = scan_store.create(ScanCreateRequest(target_url=event.target_url))
        logger.info(
            "scan stub created from CI event",
            extra={"scan_id": str(stub.scan_id), "event_id": event.event_id},
        )
        return stub.scan_id

    record, created = event_store.record(event, make_scan)

    if not created:
        response.status_code = status.HTTP_200_OK
        logger.info("duplicate CI event ignored", extra={"event_id": event.event_id})
        return WebhookAcceptedResponse(
            event_id=record.event_id, status="duplicate", scan_id=record.scan_id
        )

    logger.info(
        "CI event accepted",
        extra={
            "event_id": record.event_id,
            "event_type": record.event_type.value,
            "scan_id": str(record.scan_id) if record.scan_id else None,
        },
    )
    return WebhookAcceptedResponse(
        event_id=record.event_id, status="accepted", scan_id=record.scan_id
    )


@router.get("/events", response_model=list[CIEventRecord])
def list_events(
    event_type: BuildEventType | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    event_store: CIEventStore = Depends(get_event_store),
) -> list[CIEventRecord]:
    """List stored CI events (oldest first), optionally filtered by type."""
    return event_store.list(event_type=event_type, limit=limit, offset=offset)


@router.get(
    "/events/{event_id}",
    response_model=CIEventRecord,
    responses={404: {"description": "Unknown event_id"}},
)
def get_event(
    event_id: str,
    event_store: CIEventStore = Depends(get_event_store),
) -> CIEventRecord:
    """Fetch a single stored CI event by its ``event_id``."""
    record = event_store.get(event_id)
    if record is None:
        raise DomainError(
            ErrorCode.CI_EVENT_NOT_FOUND,
            f"ci event {event_id} not found",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return record
