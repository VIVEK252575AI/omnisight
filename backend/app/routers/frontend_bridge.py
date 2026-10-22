"""Small browser-to-VLM bridge for the static OmniSight frontend.

This endpoint accepts a screenshot captured/uploaded by the browser, stores it
inside the configured artifact root, creates a valid observation/v1 evidence
record, and advances the scan to evidence_collection. The existing detection
endpoint then runs the configured VLM adapter over that evidence.
"""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from app.artifacts import artifact_root
from app.config import Settings, get_settings
from app.errors import DomainError, ErrorCode
from app.evidence_storage import EvidenceStore, get_evidence_store
from app.ids import new_uuid, utcnow
from app.schemas import EvidenceRecord, Observation, ScanStatus, Viewport
from app.storage import ScanStore, get_store

router = APIRouter(prefix="/api/v1", tags=["frontend"])

class BrowserEvidenceUpload(BaseModel):
    image_base64: str = Field(min_length=16)
    media_type: str = "image/png"
    viewport: Viewport = Field(default_factory=lambda: Viewport(width=390, height=844))
    route: str | None = None

@router.post("/scans/{scan_id}/browser-evidence", response_model=EvidenceRecord, status_code=201)
def browser_evidence(
    scan_id: UUID,
    payload: BrowserEvidenceUpload,
    settings: Settings = Depends(get_settings),
    scan_store: ScanStore = Depends(get_store),
    evidence_store: EvidenceStore = Depends(get_evidence_store),
) -> EvidenceRecord:
    scan = scan_store.get(scan_id)
    if scan is None:
        raise DomainError(ErrorCode.SCAN_NOT_FOUND, f"scan {scan_id} not found", status_code=404)
    if scan.status is not ScanStatus.created:
        raise DomainError(ErrorCode.EVIDENCE_STATE_CONFLICT, "browser evidence is only accepted while scan is created", status_code=409)

    media = payload.media_type.lower()
    ext = ".png" if media == "image/png" else ".jpg" if media in {"image/jpeg", "image/jpg"} else None
    if ext is None:
        raise DomainError(ErrorCode.ARTIFACT_INVALID, "only PNG or JPEG screenshots are accepted", status_code=400)
    encoded = payload.image_base64.split(",", 1)[-1]
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DomainError(ErrorCode.ARTIFACT_INVALID, "invalid base64 screenshot", status_code=400) from exc
    if len(raw) > settings.artifact_max_image_bytes:
        raise DomainError(ErrorCode.PAYLOAD_TOO_LARGE, "screenshot exceeds configured image limit", status_code=413)
    if ext == ".png" and not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise DomainError(ErrorCode.ARTIFACT_INVALID, "invalid PNG data", status_code=400)
    if ext == ".jpg" and not raw.startswith(b"\xff\xd8\xff"):
        raise DomainError(ErrorCode.ARTIFACT_INVALID, "invalid JPEG data", status_code=400)

    rel = Path("browser_uploads") / f"{new_uuid()}{ext}"
    dest = artifact_root(settings) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)

    artifact_id = new_uuid()
    observation = Observation(
        schema_version="observation/v1",
        url=scan.target_url,
        route=payload.route,
        state="frontend-upload",
        viewport=payload.viewport,
        screenshot=rel.as_posix(),
        elements=[],
        console_errors=[],
        network_errors=[],
        captured_at=datetime.now(timezone.utc),
    )
    record = EvidenceRecord(
        evidence_id=new_uuid(), scan_id=scan_id, observation=observation,
        screenshot_artifact_id=artifact_id, created_at=utcnow(),
    )
    evidence_store.add(record, artifact_id=artifact_id, artifact_path=dest)
    scan_store.replace(scan.model_copy(update={"status": ScanStatus.evidence_collection, "updated_at": utcnow()}))
    return record
