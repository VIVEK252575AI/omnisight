"""Evidence contract endpoints for the Playwright collector.

The collector delivers one `observation.json` per (route, state, viewport) and
places the referenced screenshot in the artifact directory. The backend
validates the observation, safely resolves the artifact, stores an inert
record, and advances the scan:

    created --(valid evidence)--> evidence_collection
    created --(invalid evidence)--> failed   (reason recorded in `error`)

Routes:

    POST /api/v1/scans/{scan_id}/evidence   -> 201 evidence summary
    GET  /api/v1/scans/{scan_id}/evidence   -> list of evidence records
    GET  /api/v1/artifacts/{artifact_id}     -> the stored screenshot bytes

DOM/HTML/console/network content is stored as inert text and never rendered or
executed. See `app.schemas.Observation` for the observation/v1 contract.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import FileResponse
from pydantic import ValidationError

from app.artifacts import media_type_for, resolve_artifact
from app.config import Settings, get_settings
from app.errors import DomainError, ErrorCode, validation_details
from app.evidence_storage import EvidenceStore, get_evidence_store
from app.ids import new_uuid, utcnow
from app.schemas import EvidenceRecord, Observation, ScanResponse, ScanStatus
from app.storage import ScanStore, get_store

logger = logging.getLogger("omnisight")

router = APIRouter(prefix="/api/v1", tags=["evidence"])


async def _read_bounded_body(request: Request, settings: Settings) -> bytes:
    """Read the raw observation body, rejecting anything over the limit."""
    limit = settings.observation_max_bytes
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise DomainError(
                    ErrorCode.PAYLOAD_TOO_LARGE,
                    f"evidence payload exceeds {limit} bytes",
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                )
        except ValueError:
            pass
    body = await request.body()
    if len(body) > limit:
        raise DomainError(
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"evidence payload exceeds {limit} bytes",
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )
    return body


def _fail_scan(store: ScanStore, record: ScanResponse, reason: str) -> None:
    """Move a scan to `failed` with a recorded reason (invalid evidence)."""
    failed = record.model_copy(
        update={"status": ScanStatus.failed, "error": reason, "updated_at": utcnow()}
    )
    store.replace(failed)


@router.post(
    "/scans/{scan_id}/evidence",
    response_model=EvidenceRecord,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"description": "Unsafe, unsupported, missing or oversized artifact"},
        404: {"description": "Unknown scan_id"},
        409: {"description": "Scan is not in a state that accepts evidence"},
        413: {"description": "Observation payload too large"},
        422: {"description": "Invalid observation schema"},
    },
)
async def submit_evidence(
    scan_id: UUID,
    request: Request,
    settings: Settings = Depends(get_settings),
    scan_store: ScanStore = Depends(get_store),
    evidence_store: EvidenceStore = Depends(get_evidence_store),
) -> EvidenceRecord:
    """Validate and store one observation, advancing the scan state."""
    scan = scan_store.get(scan_id)
    if scan is None:
        raise DomainError(
            ErrorCode.SCAN_NOT_FOUND,
            f"scan {scan_id} not found",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    if scan.status is not ScanStatus.created:
        raise DomainError(
            ErrorCode.EVIDENCE_STATE_CONFLICT,
            (
                f"scan is in state {scan.status.value}; evidence is only "
                "accepted while the scan is in 'created'"
            ),
            status_code=status.HTTP_409_CONFLICT,
        )

    try:
        raw = await _read_bounded_body(request, settings)
        try:
            observation = Observation.model_validate_json(raw)
        except ValidationError as exc:
            raise DomainError(
                ErrorCode.INVALID_EVIDENCE,
                "evidence validation failed",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                details=validation_details(exc.errors()),
            ) from exc
        artifact_path = resolve_artifact(
            observation.screenshot, settings, expect_image=True
        )
    except DomainError as exc:
        _fail_scan(scan_store, scan, exc.message)
        logger.warning(
            "evidence rejected; scan failed",
            extra={"scan_id": str(scan_id), "reason": exc.message},
        )
        raise

    artifact_id = new_uuid()
    record = EvidenceRecord(
        evidence_id=new_uuid(),
        scan_id=scan_id,
        observation=observation,
        screenshot_artifact_id=artifact_id,
        created_at=utcnow(),
    )
    evidence_store.add(record, artifact_id=artifact_id, artifact_path=artifact_path)

    collected = scan.model_copy(
        update={"status": ScanStatus.evidence_collection, "updated_at": utcnow()}
    )
    scan_store.replace(collected)
    logger.info(
        "evidence collected",
        extra={
            "scan_id": str(scan_id),
            "evidence_id": str(record.evidence_id),
            "viewport": f"{observation.viewport.width}x{observation.viewport.height}",
        },
    )
    return record


@router.get(
    "/scans/{scan_id}/evidence",
    response_model=list[EvidenceRecord],
    responses={404: {"description": "Unknown scan_id"}},
)
def list_evidence(
    scan_id: UUID,
    scan_store: ScanStore = Depends(get_store),
    evidence_store: EvidenceStore = Depends(get_evidence_store),
) -> list[EvidenceRecord]:
    """List all stored evidence for a scan (oldest first)."""
    if scan_store.get(scan_id) is None:
        raise DomainError(
            ErrorCode.SCAN_NOT_FOUND,
            f"scan {scan_id} not found",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return evidence_store.list_for_scan(scan_id)


@router.get(
    "/artifacts/{artifact_id}",
    responses={
        200: {"content": {"image/png": {}}},
        404: {"description": "Unknown artifact_id"},
    },
)
def get_artifact(
    artifact_id: UUID,
    evidence_store: EvidenceStore = Depends(get_evidence_store),
) -> FileResponse:
    """Stream a stored evidence artifact by its opaque id."""
    path = evidence_store.get_artifact(artifact_id)
    if path is None:
        raise DomainError(
            ErrorCode.ARTIFACT_NOT_FOUND,
            f"artifact {artifact_id} not found",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return FileResponse(path, media_type=media_type_for(path))
