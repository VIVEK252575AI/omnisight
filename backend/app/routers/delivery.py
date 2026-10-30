"""Delivery endpoints (W3.6)."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.config import Settings, get_settings
from app.delivery import PRRecord, DeliveryService, get_pr_store
from app.errors import DomainError, ErrorCode
from app.orchestrator import AttemptStore, get_attempt_store
from app.patch_storage import PatchStore, get_patch_store
from app.schemas import ScanStatus
from app.storage import ScanStore, get_store
from app.detection_storage import DetectionStore, get_detection_store

logger = logging.getLogger("omnisight")

router = APIRouter(prefix="/api/v1", tags=["delivery"])


def get_delivery_service(
    settings: Settings = Depends(get_settings),
    scan_store: ScanStore = Depends(get_store),
    detection_store: DetectionStore = Depends(get_detection_store),
    patch_store: PatchStore = Depends(get_patch_store),
    attempt_store: AttemptStore = Depends(get_attempt_store),
) -> DeliveryService:
    from app.github_delivery import build_github_service

    from app.delivery import DeliveryService

    return DeliveryService(
        settings=settings,
        github_service=build_github_service(settings),
        pr_store=get_pr_store(),
        scan_store=scan_store,
        detection_store=detection_store,
        patch_store=patch_store,
        attempt_store=attempt_store,
    )


from pydantic import BaseModel


class DeliverRequest(BaseModel):
    finding_id: str | None = None


@router.post(
    "/scans/{scan_id}/deliver",
    response_model=PRRecord,
    status_code=status.HTTP_200_OK,
    responses={404: {}, 409: {}},
)
def deliver_scan(
    scan_id: UUID,
    body: DeliverRequest | None = None,
    finding_id: str | None = None,
    service: DeliveryService = Depends(get_delivery_service),
) -> PRRecord:
    fid = finding_id
    if fid is None and body is not None:
        fid = body.finding_id
    if not fid:
        attempts = service.attempt_store.list_for_scan(scan_id)
        verified = [a for a in attempts if a.status.value == "verified"]
        if verified:
            fid = verified[-1].finding_id
        elif attempts:
            fid = attempts[-1].finding_id
        else:
            fid = ""
    return service.deliver(scan_id, fid or "")


@router.get(
    "/scans/{scan_id}/pull-request",
    response_model=PRRecord,
    responses={404: {}},
)
def get_pull_request(
    scan_id: UUID,
    service: DeliveryService = Depends(get_delivery_service),
) -> PRRecord:
    rec = service.get_pr(scan_id)
    if rec is None:
        raise DomainError(ErrorCode.NOT_FOUND, "no pull request for scan", status_code=404)
    return rec