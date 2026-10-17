"""Semantic validation of model output against its request.

Pydantic confirms the *shape*; these checks confirm the output is *coherent*
with the evidence it was given — e.g. a finding may only cite evidence that was
actually supplied, and may only use categories the caller allowed. Failures
raise :class:`AiError` (code ``AI_EVIDENCE_VALIDATION_FAILED``).
"""

from __future__ import annotations

from app.ai.contracts import (
    AnalysisOutcome,
    VisionRequest,
    VisualAnalysisResult,
)
from app.ai.errors import AiError, AiErrorCode


def screenshot_ref(artifact_id: object) -> str:
    """Canonical evidence-ref string for a screenshot artifact id."""
    return f"screenshot:{artifact_id}"


def measurement_ref(name: str) -> str:
    """Canonical evidence-ref string for a measurement."""
    return f"measurement:{name}"


def allowed_evidence_refs(request: VisionRequest) -> set[str]:
    """The set of evidence refs a result is permitted to cite for this request."""
    refs = {screenshot_ref(shot.artifact_id) for shot in request.screenshots}
    refs.update(measurement_ref(m.name) for m in request.measurements)
    return refs


def _evidence_error(message: str, *, details: list[dict] | None = None) -> AiError:
    return AiError(
        AiErrorCode.AI_EVIDENCE_VALIDATION_FAILED,
        message,
        details=details or [],
    )


def validate_visual_analysis(
    result: VisualAnalysisResult,
    request: VisionRequest,
) -> None:
    """Raise :class:`AiError` if ``result`` is inconsistent with ``request``."""
    allowed = allowed_evidence_refs(request)

    if result.analysis_outcome is AnalysisOutcome.findings_detected:
        if not result.findings:
            raise _evidence_error(
                "analysis_outcome is 'findings_detected' but no findings were returned"
            )
    elif result.findings:
        raise _evidence_error(
            f"analysis_outcome is '{result.analysis_outcome.value}' "
            "but findings were returned"
        )

    if len(result.findings) > request.max_findings:
        raise _evidence_error(
            f"returned {len(result.findings)} findings, over the "
            f"{request.max_findings} allowed"
        )

    allowed_categories = set(request.allowed_categories)
    seen_keys: set[str] = set()
    for finding in result.findings:
        if finding.category not in allowed_categories:
            raise _evidence_error(
                f"category '{finding.category.value}' is not in the allowed set"
            )
        if finding.temporary_key in seen_keys:
            raise _evidence_error(
                f"duplicate finding temporary_key '{finding.temporary_key}'"
            )
        seen_keys.add(finding.temporary_key)

        if finding.confidence is None and finding.confidence_label is None:
            raise _evidence_error(
                f"finding '{finding.temporary_key}' has neither confidence "
                "nor confidence_label"
            )

        cited = set(finding.evidence_refs)
        for fact in finding.observed_facts:
            cited.update(fact.evidence_refs)
        unknown = cited - allowed
        if unknown:
            raise _evidence_error(
                f"finding '{finding.temporary_key}' cites unknown evidence refs",
                details=[{"unknown_refs": sorted(unknown)}],
            )
