"""VLM detection endpoints (Week 2, W2.4).

Endpoints
---------
- ``POST /api/v1/scans/{scan_id}/detect``  — 202, schedules one detection run.
- ``GET  /api/v1/scans/{scan_id}/findings`` — validated findings + run history.
- ``GET  /api/v1/findings/{finding_id}``   — one finding with its evidence refs.

Rules
-----
- **202 idempotent**: while a scan is ``detecting`` or already ``detected``,
  re-posting does not start a second run (``accepted`` is ``false``).
- **409**: any other status cannot legally enter ``detecting`` (terminal,
  early, or already past detection).
- The state change into ``detecting`` is atomic (single-writer compare-and-set),
  so concurrent triggers cannot both win — a duplicate detector can never run.
- The model call runs as a background task; because the scan is already in
  ``detecting`` before it is scheduled, a crash cannot leave it stuck (the
  service settles it into ``detection_failed``).
- Raw model text is untrusted and never returned unless the debug flag
  ``OMNISIGHT_DETECTION_EXPOSE_RAW_OUTPUT`` is enabled AND explicitly requested
  per call via ``?include_raw=true``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from uuid import UUID

from app.ai.adapters.base import VisionAnalyzer
from app.ai.adapters.factory import create_vision_analyzer
from app.ai.prompts import VISUAL_ANALYSIS_PROMPT_VERSION
from app.config import Settings, get_settings
from app.detection_schemas import (
    DetectionRun,
    DetectionTriggerResponse,
    ScanFindingsResponse,
)
from app.detection_service import DetectionService
from app.detection_storage import DetectionStore, get_detection_store
from app.errors import DomainError, ErrorCode
from app.evidence_storage import EvidenceStore, get_evidence_store
from app.ids import utcnow
from app.schemas import ScanStatus
from app.storage import ScanStore, get_store

logger = logging.getLogger("omnisight")

router = APIRouter(prefix="/api/v1", tags=["detection"])

# Statuses that may legally enter `detecting` (mirrors LEGAL_TRANSITIONS).
_STARTABLE: frozenset[ScanStatus] = frozenset(
    {
        ScanStatus.evidence_collection,
        ScanStatus.detection_failed,
        ScanStatus.needs_inspection,
    }
)

# Statuses where a trigger is a no-op rather than an error.
_IN_FLIGHT: frozenset[ScanStatus] = frozenset(
    {ScanStatus.detecting, ScanStatus.detected}
)


def get_detection_analyzer(
    settings: Settings = Depends(get_settings),
    evidence_store: EvidenceStore = Depends(get_evidence_store),
) -> VisionAnalyzer:
    """Build the configured adapter, resolving images against *this* store.

    Deliberately per-service rather than ``app.state.vision_analyzer``: the
    startup analyzer resolves against the process-wide evidence store, which is
    the wrong store under test dependency overrides.
    """
    return create_vision_analyzer(settings, resolver=evidence_store.get_artifact)


def get_detection_service(
    settings: Settings = Depends(get_settings),
    scan_store: ScanStore = Depends(get_store),
    evidence_store: EvidenceStore = Depends(get_evidence_store),
    detection_store: DetectionStore = Depends(get_detection_store),
    analyzer: VisionAnalyzer = Depends(get_detection_analyzer),
) -> DetectionService:
    return DetectionService(
        settings,
        scan_store=scan_store,
        evidence_store=evidence_store,
        detection_store=detection_store,
        analyzer=analyzer,
    )


def _attach_raw(
    run: DetectionRun | None,
    *,
    include_raw: bool,
    settings: Settings,
) -> DetectionRun | None:
    """Return ``run`` with raw output attached only if debug exposure is on."""
    if run is None:
        return None
    if not (include_raw and settings.detection_expose_raw_output):
        return run.model_copy(update={"raw_output": None})
    if run.raw_output is None:
        return run
    return run.model_copy(update={"raw_output": run.raw_output})


def _placeholder_run(scan_id: UUID, analyzer: VisionAnalyzer) -> DetectionRun:
    """Run record returned by a 202 before the background task finishes.

    It records which adapter was invoked (and whether it is MOCK) so a caller
    sees the provenance immediately; the background task replaces it with the
    finished run.
    """
    return DetectionRun(
        scan_id=scan_id,
        status=ScanStatus.detecting,
        adapter_label=analyzer.provider_label,
        is_mock=bool(getattr(analyzer, "is_mock", False)),
        model_label=getattr(analyzer, "model_label", None),
        prompt_version=VISUAL_ANALYSIS_PROMPT_VERSION,
        attempts=1,
        started_at=utcnow(),
    )


@router.post(
    "/scans/{scan_id}/detect",
    response_model=DetectionTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        404: {"description": "Unknown scan_id"},
        409: {"description": "Scan is not in a state that can start detection"},
    },
)
def trigger_detection(
    scan_id: UUID,
    background_tasks: BackgroundTasks,
    scan_store: ScanStore = Depends(get_store),
    service: DetectionService = Depends(get_detection_service),
    analyzer: VisionAnalyzer = Depends(get_detection_analyzer),
) -> DetectionTriggerResponse:
    """Schedule one VLM detection run for the scan's stored evidence."""
    scan = scan_store.get(scan_id)
    if scan is None:
        raise DomainError(
            ErrorCode.SCAN_NOT_FOUND,
            f"scan {scan_id} not found",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    if scan.status in _IN_FLIGHT:
        logger.info(
            "detection trigger ignored (already %s)",
            scan.status.value,
            extra={"scan_id": str(scan_id)},
        )
        return DetectionTriggerResponse(
            scan_id=scan_id,
            status=scan.status,
            accepted=False,
            detection=service.detection_store.get_run(scan_id),
        )

    # Atomic single-writer transition. If another request won the race the scan
    # has already moved, so we re-read and report the in-flight run instead.
    updated = scan_store.compare_and_transition(
        scan_id,
        expected=_STARTABLE,
        to_status=ScanStatus.detecting,
        error=None,
    )
    if updated is None:
        scan = scan_store.get(scan_id)
        if scan is None:
            raise DomainError(
                ErrorCode.SCAN_NOT_FOUND,
                f"scan {scan_id} not found",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if scan.status in _IN_FLIGHT:
            return DetectionTriggerResponse(
                scan_id=scan_id,
                status=scan.status,
                accepted=False,
                detection=service.detection_store.get_run(scan_id),
            )
        raise DomainError(
            ErrorCode.DETECTION_STATE_CONFLICT,
            (
                f"scan status {scan.status.value} cannot start detection"
                f" — allowed from: "
                + ", ".join(sorted(s.value for s in _STARTABLE))
            ),
            status_code=status.HTTP_409_CONFLICT,
        )

    placeholder = _placeholder_run(scan_id, analyzer)
    service.detection_store.put_run(placeholder)

    # The scan is already `detecting` before the task is scheduled, so a crash
    # inside the task is settled into `detection_failed` rather than wedging.
    background_tasks.add_task(service.run_guarded, scan_id)

    logger.info(
        "detection scheduled",
        extra={
            "scan_id": str(scan_id),
            "adapter": analyzer.provider_label,
            "is_mock": bool(getattr(analyzer, "is_mock", False)),
        },
    )
    return DetectionTriggerResponse(
        scan_id=scan_id,
        status=ScanStatus.detecting,
        accepted=True,
        detection=placeholder,
    )


@router.get(
    "/scans/{scan_id}/findings",
    response_model=ScanFindingsResponse,
    responses={404: {"description": "Unknown scan_id"}},
)
def list_findings(
    scan_id: UUID,
    include_raw: bool = Query(
        default=False,
        description="Include raw model text; ignored unless debug exposure is enabled.",
    ),
    scan_store: ScanStore = Depends(get_store),
    detection_store: DetectionStore = Depends(get_detection_store),
    settings: Settings = Depends(get_settings),
) -> ScanFindingsResponse:
    """Validated findings for a scan, plus the latest detection run's provenance."""
    scan = scan_store.get(scan_id)
    if scan is None:
        raise DomainError(
            ErrorCode.SCAN_NOT_FOUND,
            f"scan {scan_id} not found",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    run = _attach_raw(
        detection_store.get_run(scan_id), include_raw=include_raw, settings=settings
    )
    records = detection_store.list_for_scan(scan_id)
    return ScanFindingsResponse(
        scan_id=scan_id,
        status=scan.status,
        adapter_label=run.adapter_label if run else None,
        detection=run,
        findings=records,
    )


@router.get(
    "/findings/{finding_id}",
    responses={404: {"description": "Unknown finding_id"}},
)
def get_finding(
    finding_id: str,
    include_raw: bool = Query(default=False),
    detection_store: DetectionStore = Depends(get_detection_store),
    settings: Settings = Depends(get_settings),
):
    """One finding, with the evidence references and adapter labelling it came from."""
    record = detection_store.get_finding(finding_id)
    if record is None:
        raise DomainError(
            ErrorCode.FINDING_NOT_FOUND,
            f"finding {finding_id} not found",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    if not (include_raw and settings.detection_expose_raw_output):
        return record
    run = detection_store.get_run(record.scan_id)
    if run is None or run.raw_output is None:
        return record
    return record.model_copy(update={"raw_output": run.raw_output})
