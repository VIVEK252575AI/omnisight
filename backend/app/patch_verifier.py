"""The real Validator and Verifier for the repair loop (Week 3, W3.3).

These implement the two protocols declared in :mod:`app.orchestrator`:

``WorkspaceValidator``
    Runs the configured build/test commands with the workspace's ``source/``
    directory as ``cwd`` and returns a bounded :class:`~app.validation.ValidationResult`.

``SourcePatchVerifier``
    Serves the patched workspace, captures fresh evidence at the original
    viewports, recomputes the finding's geometric fact, asks the VLM verifier to
    re-examine the ORIGINAL defect on before/after images, and applies the
    policy in :func:`app.verification.decide`.

Neither class decides that a repair succeeded on its own: the orchestrator
records the outcome, and only a ``verified`` verification moves the scan on.
Capture failures, missing workspaces and unparseable model output all end as a
*failed* or *inconclusive* outcome — never a pass.
"""

from __future__ import annotations

import logging
import time

from app.config import Settings
from app.detection_storage import DetectionStore
from app.evidence_capture import (
    CaptureError,
    ShopLiteCheckoutCollector,
    WorkspaceEvidenceCollector,
)
from app.evidence_storage import EvidenceStore
from app.orchestrator import StageOutcome
from app.validation import ValidationResult, ValidationRunner, ValidationStatus
from app.verification import (
    VerificationRequest,
    VerificationResult,
    VlmVerifier,
    decide,
    deterministic_check,
)
from app.workspace import WorkspaceError, WorkspaceManager

logger = logging.getLogger("omnisight")


class WorkspaceValidator:
    """Runs the configured build/test checks inside a workspace."""

    def __init__(
        self,
        *,
        settings: Settings,
        workspace_manager: WorkspaceManager,
        runner: ValidationRunner | None = None,
    ) -> None:
        self.settings = settings
        self.workspaces = workspace_manager
        self.runner = runner or ValidationRunner(settings=settings)

    def run(self, workspace_id: str) -> ValidationResult | None:
        """Run the checks and return the full result (None for an unknown workspace)."""
        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            return None
        return self.runner.run(workspace.source)

    def validate(self, workspace_id: str) -> StageOutcome:
        """Protocol entry point: a passing result is required, never assumed."""
        try:
            result = self.run(workspace_id)
        except WorkspaceError as exc:
            return StageOutcome(
                passed=False,
                summary=f"validation failed: {exc.code}",
                detail={"status": ValidationStatus.failed.value, "reasons": [str(exc)]},
            )
        if result is None:
            return StageOutcome(
                passed=False,
                summary="validation failed: the workspace does not exist",
                detail={"status": ValidationStatus.failed.value},
            )
        detail = result.model_dump(mode="json")
        return StageOutcome(
            passed=result.passed, summary=result.summary(), detail=detail
        )


class SourcePatchVerifier:
    """Re-checks the ORIGINAL defect on fresh evidence from the patched copy."""

    def __init__(
        self,
        *,
        settings: Settings,
        detection_store: DetectionStore,
        evidence_store: EvidenceStore,
        workspace_manager: WorkspaceManager,
        vlm: VlmVerifier,
        collector: WorkspaceEvidenceCollector | None = None,
    ) -> None:
        self.settings = settings
        self.detection_store = detection_store
        self.evidence_store = evidence_store
        self.workspaces = workspace_manager
        self.vlm = vlm
        self.collector = collector or ShopLiteCheckoutCollector(
            settings=settings, evidence_store=evidence_store
        )

    def verify(self, finding_id: str, workspace_id: str) -> StageOutcome:
        """Protocol entry point for the orchestrator's verify step."""
        record = self.detection_store.get_finding(finding_id)
        if record is None:
            return self._fail("the finding no longer exists", code="FINDING_MISSING")
        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            return self._fail(
                "the attempt workspace no longer exists", code="WORKSPACE_MISSING"
            )

        viewports = [record.viewport] if record.viewport is not None else []
        started = time.monotonic()
        try:
            capture = self.collector.capture(
                scan_id=record.scan_id,
                source_dir=workspace.source,
                viewports=viewports,
                attempt_id=workspace.attempt_id or workspace.workspace_id,
            )
        except CaptureError as exc:
            logger.warning(
                "fresh capture failed", extra={"code": exc.code, "scan_id": str(record.scan_id)}
            )
            return self._fail(f"fresh capture failed: {exc.code}", code=exc.code)

        deterministic = deterministic_check(
            record.finding,
            capture,
            viewport=record.viewport,
            min_contrast_ratio=self.settings.min_contrast_ratio,
        )
        request = VerificationRequest(
            finding=record.finding,
            route=record.route,
            viewport=record.viewport,
            before_artifact_ids=list(record.screenshot_artifact_ids),
            after_artifact_ids=capture.artifact_ids,
            deterministic_status=deterministic.status,
        )
        vlm = self.vlm.verify(request)

        # Validation already ran in the orchestrator; a verified verdict here
        # additionally requires the deterministic and model signals to agree.
        status, reasons = decide(
            deterministic=deterministic,
            vlm=vlm,
            validation_passed=True,
            max_new_issues=self.settings.verification_max_new_issues,
        )

        verification = VerificationResult(
            status=status,
            deterministic=deterministic,
            vlm=vlm,
            reasons=reasons,
            collector_label=capture.collector_label,
            is_fixture=capture.is_fixture,
            evidence_ids=list(capture.artifact_ids),
            duration_seconds=round(time.monotonic() - started, 3),
        )
        detail = verification.model_dump(mode="json")
        # Which original screenshots the model compared against.
        detail["before_evidence_ids"] = [
            str(item) for item in record.screenshot_artifact_ids
        ]

        logger.info(
            "verification decided",
            extra={
                "scan_id": str(record.scan_id),
                "status": status.value,
                "deterministic": deterministic.status.value,
                "vlm": vlm.verdict.value,
                "is_fixture": capture.is_fixture,
                "vlm_is_mock": vlm.is_mock,
            },
        )
        return StageOutcome(
            passed=verification.passed,
            summary=verification.summary(),
            # None unless BOTH signals came from real (non-fixture, non-mock) runs.
            deterministic_evidence=verification.deterministic_evidence(),
            detail=detail,
        )

    def _fail(self, message: str, *, code: str) -> StageOutcome:
        return StageOutcome(
            passed=False,
            summary=f"verification failed: {message}",
            detail={"status": "failed", "reasons": [f"{code}: {message}"]},
        )


def build_workspace_validator(
    settings: Settings, workspace_manager: WorkspaceManager
) -> WorkspaceValidator:
    """Construct the validator from configuration."""
    return WorkspaceValidator(settings=settings, workspace_manager=workspace_manager)
