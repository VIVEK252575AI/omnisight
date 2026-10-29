"""Self-healing repair endpoints (Week 3, W3.1).

Endpoints
---------
- ``POST /api/v1/scans/{scan_id}/repair``  — 202, claims the scan and runs one
  bounded attempt per patchable finding in a background task.
- ``POST /api/v1/scans/{scan_id}/cancel``  — 200, idempotent; stops the run
  between steps because cancellation is signalled by the scan's own state.
- ``GET  /api/v1/scans/{scan_id}/attempts`` — ordered attempt history.

Rules
-----
- **404** ``SCAN_NOT_FOUND`` for an unknown scan; ``GET /attempts`` uses the
  same code so an unknown scan is never mistaken for one with no attempts.
- **409** ``REPAIR_UNAVAILABLE`` when the loop cannot be driven honestly: the
  patch applier / validator / verifier implementations do not exist yet
  (W3.2–W3.4) or ``OMNISIGHT_ENABLE_REPAIR_ORCHESTRATOR`` is off. Refusing is
  the point — an unwired deployment must never report a fake repair.
- **409** ``REPAIR_STATE_CONFLICT`` when the scan is not in ``detected``, or
  when a concurrent request already claimed it. The claim is an atomic
  compare-and-transition, so only one run per scan can start.
- **409** ``REPAIR_NO_PATCHABLE_FINDINGS`` when nothing is left in ``detected``.
- **409** ``ILLEGAL_TRANSITION`` on cancel when the scan's state has no legal
  path to ``cancelled`` (e.g. it is already finished). Repeating a cancel that
  already succeeded returns 200, never an error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, status
from pydantic import BaseModel, Field

from app.action_engine import ActionEngine
from app.ai.contracts import FindingStatus
from app.config import Settings, get_settings
from app.detection_storage import DetectionStore, get_detection_store
from app.errors import DomainError, ErrorCode
from app.orchestrator import (
    AttemptListResponse,
    AttemptStore,
    DeliveryService,
    EvidenceCollector,
    Orchestrator,
    PatchApplier,
    Validator,
    Verifier,
    get_attempt_store,
)
from app.patch_storage import PatchStore, get_patch_store
from app.routers.patches import get_action_engine
from app.schemas import LEGAL_TRANSITIONS, ScanStatus
from app.storage import ScanStore, get_store

logger = logging.getLogger("omnisight")

router = APIRouter(prefix="/api/v1", tags=["repair"])


@dataclass(frozen=True)
class RepairComponents:
    """The component implementations the repair loop drives.

    ``delivery`` and ``evidence_collector`` are optional because W3.1 stops at
    ``review_preparation``: PyGithub delivery is human-gated and disabled, and
    the Playwright re-screenshot boundary is declared but not yet invoked.
    """

    applier: PatchApplier
    validator: Validator
    verifier: Verifier
    delivery: DeliveryService | None = None
    evidence_collector: EvidenceCollector | None = None


#: Registration seam for the real components. ``None`` until W3.2–W3.4 land;
#: tests override :func:`get_orchestrator` instead of mutating this.
_REPAIR_COMPONENTS: RepairComponents | None = None


def register_repair_components(components: RepairComponents | None) -> None:
    """Register (or clear) the component implementations for the API process."""
    global _REPAIR_COMPONENTS
    _REPAIR_COMPONENTS = components


def get_orchestrator(
    settings: Settings = Depends(get_settings),
    scan_store: ScanStore = Depends(get_store),
    detection_store: DetectionStore = Depends(get_detection_store),
    patch_store: PatchStore = Depends(get_patch_store),
    attempt_store: AttemptStore = Depends(get_attempt_store),
    action_engine: ActionEngine = Depends(get_action_engine),
) -> Orchestrator | None:
    """Build the repair orchestrator, or ``None`` when it cannot be wired.

    The single place the real components are attached when they exist.
    Returning ``None`` keeps the endpoint honest: no applier/validator/verifier
    means no repair, never a simulated one.
    """
    if not settings.enable_repair_orchestrator or _REPAIR_COMPONENTS is None:
        return None
    return Orchestrator(
        settings=settings,
        scan_store=scan_store,
        detection_store=detection_store,
        patch_store=patch_store,
        attempt_store=attempt_store,
        action_engine=action_engine,
        applier=_REPAIR_COMPONENTS.applier,
        validator=_REPAIR_COMPONENTS.validator,
        verifier=_REPAIR_COMPONENTS.verifier,
        delivery=_REPAIR_COMPONENTS.delivery,
        evidence_collector=_REPAIR_COMPONENTS.evidence_collector,
    )


class RepairStartResponse(BaseModel):
    """A repair run was claimed and scheduled."""

    scan_id: UUID
    scan_status: ScanStatus
    finding_ids: list[str] = Field(default_factory=list)
    message: str


class CancelResponse(BaseModel):
    """Result of a cancel request (idempotent)."""

    scan_id: UUID
    scan_status: ScanStatus
    cancelled: bool


@router.post(
    "/scans/{scan_id}/repair",
    response_model=RepairStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        404: {"description": "Unknown scan_id"},
        409: {
            "description": (
                "Repair unavailable, scan not in 'detected', no patchable "
                "findings, or a repair is already running"
            )
        },
    },
)
def start_repair(
    scan_id: UUID,
    background: BackgroundTasks,
    scan_store: ScanStore = Depends(get_store),
    detection_store: DetectionStore = Depends(get_detection_store),
    orchestrator: Orchestrator | None = Depends(get_orchestrator),
) -> RepairStartResponse:
    """Claim a ``detected`` scan and run one bounded attempt per finding."""
    scan = scan_store.get(scan_id)
    if scan is None:
        raise DomainError(
            ErrorCode.SCAN_NOT_FOUND,
            f"scan {scan_id} not found",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    if orchestrator is None:
        raise DomainError(
            ErrorCode.REPAIR_UNAVAILABLE,
            (
                "repair orchestration is not available: the patch applier, "
                "validator and verifier components are not wired yet and "
                "OMNISIGHT_ENABLE_REPAIR_ORCHESTRATOR is off by default"
            ),
            status_code=status.HTTP_409_CONFLICT,
        )
    if scan.status is not ScanStatus.detected:
        raise DomainError(
            ErrorCode.REPAIR_STATE_CONFLICT,
            (
                f"scan {scan_id} is '{scan.status.value}' — a repair run can "
                "only be started from 'detected'"
            ),
            status_code=status.HTTP_409_CONFLICT,
        )

    patchable = [
        record.finding.finding_id
        for record in detection_store.list_for_scan(scan_id)
        if record.finding.status is FindingStatus.detected
    ]
    if not patchable:
        raise DomainError(
            ErrorCode.REPAIR_NO_PATCHABLE_FINDINGS,
            (
                f"scan {scan_id} has no findings in 'detected' status — run "
                "detection first, or the findings already have proposals"
            ),
            status_code=status.HTTP_409_CONFLICT,
        )

    # Atomic claim: exactly one concurrent repair request wins.
    claimed = scan_store.compare_and_transition(
        scan_id,
        expected=frozenset({ScanStatus.detected}),
        to_status=ScanStatus.repairing,
        error=None,
    )
    if claimed is None:
        raise DomainError(
            ErrorCode.REPAIR_STATE_CONFLICT,
            (
                f"scan {scan_id} changed state while the repair was being "
                "claimed — another repair run is already in progress"
            ),
            status_code=status.HTTP_409_CONFLICT,
        )

    background.add_task(orchestrator.run_scan, scan_id)
    logger.info(
        "repair run scheduled",
        extra={"scan_id": str(scan_id), "findings": len(patchable)},
    )
    return RepairStartResponse(
        scan_id=scan_id,
        scan_status=ScanStatus.repairing,
        finding_ids=patchable,
        message=(
            f"repair started for {len(patchable)} finding(s); "
            f"poll GET /api/v1/scans/{scan_id}/attempts for progress"
        ),
    )


@router.post(
    "/scans/{scan_id}/cancel",
    response_model=CancelResponse,
    responses={
        404: {"description": "Unknown scan_id"},
        409: {"description": "Scan state has no legal path to 'cancelled'"},
    },
)
def cancel_scan(
    scan_id: UUID,
    scan_store: ScanStore = Depends(get_store),
) -> CancelResponse:
    """Cancel a scan; repeating a successful cancel is a 200 no-op.

    Cancellation is honest without extra plumbing: the orchestrator re-reads
    the scan between steps and stops when it sees ``cancelled``.
    """
    scan = scan_store.get(scan_id)
    if scan is None:
        raise DomainError(
            ErrorCode.SCAN_NOT_FOUND,
            f"scan {scan_id} not found",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    if scan.status is ScanStatus.cancelled:
        return CancelResponse(
            scan_id=scan_id, scan_status=ScanStatus.cancelled, cancelled=True
        )
    if ScanStatus.cancelled not in LEGAL_TRANSITIONS[scan.status]:
        raise DomainError(
            ErrorCode.ILLEGAL_TRANSITION,
            (
                f"scan {scan_id} is '{scan.status.value}' and has no legal "
                "transition to 'cancelled'"
            ),
            status_code=status.HTTP_409_CONFLICT,
        )

    updated = scan_store.compare_and_transition(
        scan_id,
        expected=frozenset({scan.status}),
        to_status=ScanStatus.cancelled,
        error=None,
    )
    if updated is None:
        fresh = scan_store.get(scan_id)
        if fresh is not None and fresh.status is ScanStatus.cancelled:
            # Lost the race against another cancel: same outcome, still a 200.
            return CancelResponse(
                scan_id=scan_id, scan_status=ScanStatus.cancelled, cancelled=True
            )
        raise DomainError(
            ErrorCode.REPAIR_STATE_CONFLICT,
            f"scan {scan_id} changed state while being cancelled — re-read and retry",
            status_code=status.HTTP_409_CONFLICT,
        )
    logger.info("scan cancel requested", extra={"scan_id": str(scan_id)})
    return CancelResponse(
        scan_id=scan_id, scan_status=ScanStatus.cancelled, cancelled=True
    )


@router.get(
    "/scans/{scan_id}/attempts",
    response_model=AttemptListResponse,
    responses={404: {"description": "Unknown scan_id"}},
)
def list_attempts(
    scan_id: UUID,
    scan_store: ScanStore = Depends(get_store),
    attempt_store: AttemptStore = Depends(get_attempt_store),
) -> AttemptListResponse:
    """Every repair attempt recorded for the scan, oldest first."""
    if scan_store.get(scan_id) is None:
        raise DomainError(
            ErrorCode.SCAN_NOT_FOUND,
            f"scan {scan_id} not found",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return AttemptListResponse(
        scan_id=scan_id, attempts=attempt_store.list_for_scan(scan_id)
    )
