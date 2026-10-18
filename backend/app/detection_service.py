"""VLM detection service: evidence -> adapter -> validated findings.

One :class:`DetectionService` performs exactly one detection run for a scan:

1. load the stored evidence (W1.4) for the scan,
2. select a bounded set of DOM candidates (relevant elements first),
3. build a provider-neutral :class:`~app.ai.contracts.VisionRequest`,
4. call the :class:`~app.ai.gateway.AiGateway` under the configured timeout,
5. persist validated findings, the run's provenance, and (capped) raw text,
6. transition the scan out of ``detecting``.

Every path leaves the scan in a terminal detection state — ``detected``,
``detection_failed`` or ``needs_inspection`` — never stuck in ``detecting``.
Raw model text is treated as untrusted and is never executed, imported or
written to disk; it is only retained in memory for audit.
"""

from __future__ import annotations

import logging
from uuid import UUID

from app.ai.adapters.base import VisionAnalyzer
from app.ai.contracts import (
    DeterministicMeasurement,
    FindingCategory,
    PageContext,
    ScreenEvidenceRef,
    VisionRequest,
)
from app.ai.errors import AiErrorCode
from app.ai.gateway import AiGateway, GatewayResult
from app.ai.prompts import VISUAL_ANALYSIS_PROMPT_VERSION
from app.config import Settings
from app.detection_schemas import DetectionRun, FindingRecord
from app.detection_storage import DetectionStore
from app.evidence_storage import EvidenceStore
from app.ids import utcnow
from app.schemas import EvidenceRecord, Observation, ScanResponse, ScanStatus
from app.storage import ScanStore

logger = logging.getLogger("omnisight")

# Tags whose geometry is most useful when explaining a visual defect.
_INTERACTIVE_TAGS = frozenset({"button", "a", "input", "select", "textarea", "label"})

# A short, stable reason for transport/config failures (parse rejections report
# the ParseReason instead, e.g. "schema_invalid").
_TRANSPORT_REASON: dict[AiErrorCode, str] = {
    AiErrorCode.AI_TIMEOUT: "timeout",
    AiErrorCode.AI_PROVIDER_UNAVAILABLE: "provider_unavailable",
    AiErrorCode.AI_PROVIDER_AUTH_ERROR: "auth_error",
    AiErrorCode.AI_PROVIDER_RATE_LIMITED: "rate_limited",
    AiErrorCode.AI_CONFIGURATION_ERROR: "configuration_error",
    AiErrorCode.AI_BUDGET_EXCEEDED: "budget_exceeded",
    AiErrorCode.AI_CAPABILITY_UNSUPPORTED: "capability_unsupported",
    AiErrorCode.AI_CANCELLED: "cancelled",
}


def _relevance(element) -> int:
    """Rank an element by how likely it is to explain a visual defect."""
    score = 0
    if element.tag.lower() in _INTERACTIVE_TAGS:
        score += 2
    if not element.in_viewport:
        score += 2
    if not element.visible:
        score += 1
    return score


def select_candidate_elements(elements: list, limit: int) -> list:
    """Return the most relevant elements, bounded to ``limit``.

    Interactive and off-viewport elements are considered first (they are what
    a clipped/overlapping/hidden defect is usually about), then the rest, in
    capture order. Duplicate selectors are dropped.
    """
    ranked = sorted(
        enumerate(elements),
        key=lambda pair: (-_relevance(pair[1]), pair[0]),
    )
    chosen: list = []
    seen: set[str] = set()
    for _, element in ranked:
        if element.selector in seen:
            continue
        seen.add(element.selector)
        chosen.append(element)
        if len(chosen) >= max(1, limit):
            break
    return chosen


def build_measurements(
    observation: Observation, candidates: list
) -> list[DeterministicMeasurement]:
    """Derive deterministic geometry measurements from captured evidence.

    These come from the collector, not the model, so they are safe context to
    hand to the adapter.
    """
    viewport = observation.viewport
    measurements = [
        DeterministicMeasurement(
            name="viewport",
            value=f"{viewport.width}x{viewport.height}",
            unit="px",
            source="observation",
        )
    ]
    for element in candidates:
        box = element.box
        measurements.append(
            DeterministicMeasurement(
                name=f"box.{element.selector}",
                value=f"x={box.x},y={box.y},w={box.w},h={box.h}",
                unit="px",
                source="observation",
            )
        )
        measurements.append(
            DeterministicMeasurement(
                name=f"in_viewport.{element.selector}",
                value="true" if element.in_viewport else "false",
                source="observation",
            )
        )
    return measurements


def choose_evidence(scan: ScanResponse, records: list[EvidenceRecord]) -> EvidenceRecord:
    """Pick the evidence record whose viewport best matches the scan request."""
    target_width = scan.viewports[0].width if scan.viewports else None
    if target_width is not None:
        for record in records:
            if record.observation.viewport.width == target_width:
                return record
    return records[0]


def _cap_text(text: str, max_bytes: int) -> str:
    """Truncate ``text`` to at most ``max_bytes`` UTF-8 bytes."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


class DetectionService:
    """Executes one detection run and records its provenance."""

    def __init__(
        self,
        settings: Settings,
        *,
        scan_store: ScanStore,
        evidence_store: EvidenceStore,
        detection_store: DetectionStore,
        analyzer: VisionAnalyzer,
    ) -> None:
        self._settings = settings
        self.scan_store = scan_store
        self.evidence_store = evidence_store
        self.detection_store = detection_store
        self.analyzer = analyzer

    # -- public -------------------------------------------------------------

    def run_guarded(self, scan_id: UUID) -> None:
        """Run a detection, guaranteeing the scan never stays ``detecting``.

        Any unexpected exception is logged (not re-raised) and the scan is
        settled into ``detection_failed`` so a background crash cannot wedge the
        workflow.
        """
        try:
            self.run(scan_id)
        except Exception as exc:  # noqa: BLE001 - a task must never die hard
            logger.exception("detection crashed for scan %s", scan_id)
            self._force_failed(scan_id, f"detection crashed: {type(exc).__name__}")
        finally:
            self._settle(scan_id)

    def run(self, scan_id: UUID) -> DetectionRun | None:
        """Perform one detection run. Returns the recorded run, or None."""
        scan = self.scan_store.get(scan_id)
        if scan is None:
            logger.warning("detection skipped: scan %s not found", scan_id)
            return None
        if scan.status is not ScanStatus.detecting:
            logger.info(
                "detection skipped: scan %s is %s (not detecting)",
                scan_id,
                scan.status.value,
            )
            return self.detection_store.get_run(scan_id)

        started_at = utcnow()
        # A new run starts with no retained output; only this run's text is
        # kept for audit (and only in memory).
        self.detection_store.clear_raw(scan_id)
        records = self.evidence_store.list_for_scan(scan_id)
        if not records:
            return self._fail(
                scan,
                reason_code="no_evidence",
                reason_message="no stored evidence for this scan",
                attempts=1,
                duration_ms=0.0,
                started_at=started_at,
            )

        record = choose_evidence(scan, records)
        observation = record.observation
        candidates = select_candidate_elements(
            observation.elements, self._settings.detection_max_candidates
        )
        request = self._build_request(scan, record, observation, candidates)

        gateway = AiGateway(self._settings, self.analyzer)
        result = gateway.run(request, candidates=[c.selector for c in candidates])

        if not result.ok:
            reason_code, reason_message = self._failure_reason(result)
            self._store_raw(scan_id, result.raw_text)
            return self._fail(
                scan,
                reason_code=reason_code,
                reason_message=reason_message,
                attempts=result.attempts,
                duration_ms=result.duration_ms,
                started_at=started_at,
                adapter_label=result.provider_label,
                is_mock=result.is_mock,
                warnings=result.warnings,
            )

        return self._succeed(scan, record, observation, result, records, started_at)

    # -- request construction ------------------------------------------------

    def _build_request(
        self,
        scan: ScanResponse,
        record: EvidenceRecord,
        observation: Observation,
        candidates: list,
    ) -> VisionRequest:
        screenshots: list[ScreenEvidenceRef] = []
        if record.screenshot_artifact_id is not None:
            screenshots.append(
                ScreenEvidenceRef(
                    artifact_id=record.screenshot_artifact_id,
                    label="detection",
                    viewport=observation.viewport,
                )
            )
        return VisionRequest(
            run_id=str(scan.scan_id),
            prompt_version=VISUAL_ANALYSIS_PROMPT_VERSION,
            page_context=PageContext(route=observation.route, viewport=observation.viewport),
            screenshots=screenshots,
            measurements=build_measurements(observation, candidates),
            expected_behavior=None,
            allowed_categories=list(FindingCategory),
            max_findings=self._settings.ai_max_findings,
        )

    # -- persistence ---------------------------------------------------------

    def _base_run(
        self,
        scan: ScanResponse,
        *,
        status: ScanStatus,
        adapter_label: str,
        is_mock: bool,
        attempts: int,
        duration_ms: float,
        started_at,
    ) -> DetectionRun:
        return DetectionRun(
            scan_id=scan.scan_id,
            status=status,
            adapter_label=adapter_label,
            is_mock=is_mock,
            model_label=getattr(self.analyzer, "model_label", None),
            prompt_version=VISUAL_ANALYSIS_PROMPT_VERSION,
            attempts=max(1, attempts),
            duration_ms=duration_ms,
            started_at=started_at,
        )

    def _succeed(
        self,
        scan: ScanResponse,
        record: EvidenceRecord,
        observation: Observation,
        result: GatewayResult,
        records: list[EvidenceRecord],
        started_at,
    ) -> DetectionRun:
        response = result.response
        adapter_label = result.metadata.provider_label
        is_mock = result.is_mock

        records_out: list[FindingRecord] = []
        for finding in response.findings:
            records_out.append(
                FindingRecord(
                    finding=finding,
                    scan_id=scan.scan_id,
                    adapter_label=adapter_label,
                    is_mock=is_mock,
                    route=observation.route,
                    viewport=observation.viewport,
                    evidence_ids=[record.evidence_id],
                    screenshot_artifact_ids=[
                        r.screenshot_artifact_id
                        for r in records
                        if r.screenshot_artifact_id is not None
                    ],
                )
            )
        outcome = response.outcome
        final = (
            ScanStatus.needs_inspection
            if outcome.value == "needs_inspection"
            else ScanStatus.detected
        )
        run = self._base_run(
            scan,
            status=final,
            adapter_label=adapter_label,
            is_mock=is_mock,
            attempts=result.attempts,
            duration_ms=result.duration_ms,
            started_at=started_at,
        )
        run = run.model_copy(
            update={
                "model_label": (
                    result.metadata.model_label
                    if result.metadata is not None
                    else None
                ),
                "outcome": outcome,
                "findings_count": len(records_out),
                "warnings": result.warnings,
                "finished_at": utcnow(),
            }
        )

        # The state machine is authoritative: only persist findings/run once
        # the scan has actually taken the transition. Otherwise a concurrent
        # writer moved the scan while this run was in flight and the result is
        # void — `_settle` will drive the scan out of `detecting`.
        if not self._transition(scan, final, error=None):
            return run

        self._store_raw(scan.scan_id, result.raw_text)
        self.detection_store.replace_findings(scan.scan_id, records_out)
        self.detection_store.put_run(run)
        logger.info(
            "detection completed",
            extra={
                "scan_id": str(scan.scan_id),
                "status": final.value,
                "outcome": outcome.value,
                "findings": len(records_out),
                "adapter": adapter_label,
                "is_mock": is_mock,
                "duration_ms": result.duration_ms,
            },
        )
        return run

    def _fail(
        self,
        scan: ScanResponse,
        *,
        reason_code: str,
        reason_message: str,
        attempts: int,
        duration_ms: float,
        started_at,
        adapter_label: str | None = None,
        is_mock: bool | None = None,
        warnings: list[str] | None = None,
    ) -> DetectionRun:
        if adapter_label is None:
            adapter_label = self.analyzer.provider_label
        if is_mock is None:
            is_mock = bool(getattr(self.analyzer, "is_mock", False))
        run = self._base_run(
            scan,
            status=ScanStatus.detection_failed,
            adapter_label=adapter_label,
            is_mock=is_mock,
            attempts=attempts,
            duration_ms=duration_ms,
            started_at=started_at,
        )
        run = run.model_copy(
            update={
                "reason_code": reason_code,
                "reason_message": reason_message[:500],
                "warnings": warnings or [],
                # `findings_count` counts what THIS run validated (none).
                # Previously validated findings are deliberately left in place:
                # a failed re-run must not destroy audit data it could not
                # reproduce.
                "findings_count": 0,
                "finished_at": utcnow(),
            }
        )
        if not self._transition(scan, ScanStatus.detection_failed, error=reason_message):
            return run
        self.detection_store.put_run(run)
        logger.warning(
            "detection failed",
            extra={
                "scan_id": str(scan.scan_id),
                "reason_code": reason_code,
                "adapter": adapter_label,
                "is_mock": is_mock,
                "duration_ms": duration_ms,
            },
        )
        return run

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _failure_reason(result: GatewayResult) -> tuple[str, str]:
        if result.reason is not None:
            return result.reason.value, (result.error.message if result.error else "")
        if result.error is not None:
            return (
                _TRANSPORT_REASON.get(result.error.code, result.error.code.value),
                result.error.message,
            )
        return "unknown", "detection failed without a reason"

    def _store_raw(self, scan_id: UUID, text: str | None) -> None:
        if text is None:
            return
        self.detection_store.set_raw(
            scan_id,
            _cap_text(text, self._settings.detection_max_raw_output_bytes),
        )

    def _transition(self, scan: ScanResponse, to: ScanStatus, *, error: str | None) -> bool:
        """Atomically leave ``detecting``. Returns False if the scan already moved."""
        updated = self.scan_store.compare_and_transition(
            scan.scan_id,
            expected=frozenset({ScanStatus.detecting}),
            to_status=to,
            error=error,
        )
        if updated is None:
            logger.warning(
                "detection could not leave 'detecting' (scan already moved)",
                extra={"scan_id": str(scan.scan_id), "to": to.value},
            )
            return False
        return True

    def _force_failed(self, scan_id: UUID, reason: str) -> None:
        self.scan_store.compare_and_transition(
            scan_id,
            expected=frozenset({ScanStatus.detecting}),
            to_status=ScanStatus.detection_failed,
            error=reason,
        )
        scan = self.scan_store.get(scan_id)
        if scan is None or scan.status is not ScanStatus.detection_failed:
            return
        run = self.detection_store.get_run(scan_id)
        if run is not None and run.status is not ScanStatus.detecting:
            # A finished run already exists (e.g. a legitimate failure); leave it.
            return
        failed = DetectionRun(
            scan_id=scan_id,
            status=ScanStatus.detection_failed,
            adapter_label=self.analyzer.provider_label,
            is_mock=bool(getattr(self.analyzer, "is_mock", False)),
            prompt_version=VISUAL_ANALYSIS_PROMPT_VERSION,
            reason_code="interrupted",
            reason_message=reason[:500],
            findings_count=0,
            finished_at=utcnow(),
        )
        self.detection_store.put_run(failed)

    def _settle(self, scan_id: UUID) -> None:
        """Final safety net: a scan must never remain in ``detecting``."""
        scan = self.scan_store.get(scan_id)
        if scan is None or scan.status is not ScanStatus.detecting:
            return
        self._force_failed(scan_id, "detection did not complete")
