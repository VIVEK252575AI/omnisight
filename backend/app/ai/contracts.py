"""Provider-neutral VLM boundary contracts.

These Pydantic models are the *only* shape exchanged across the vision-model
boundary. Nothing here references a provider, SDK, endpoint, or model id.

Vocabulary is the canonical LLD (``03-LLD.md``):

- categories -> :class:`FindingCategory` (``viewport_clipping``, ...),
- severity   -> :class:`Severity` (``critical/high/medium/low/informational``),
- confidence -> numeric ``confidence`` in ``[0, 1]`` *and/or* a
  ``confidence_label`` (``low/medium/high``).

Model output crosses the boundary as :class:`RawVisionResult`: raw, untrusted
text plus a ``provider_label`` (e.g. ``"MOCK"``). It is only ever turned into a
:class:`VisualAnalysisResult` after schema + semantic validation.
"""

from __future__ import annotations

from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field

from app.ids import new_uuid
from app.schemas import Viewport

VISUAL_ANALYSIS_RESULT_SCHEMA_VERSION = "visual-analysis-result/v1"


class FindingCategory(str, Enum):
    """Closed set of allowed visual-defect categories (canonical LLD)."""

    viewport_clipping = "viewport_clipping"
    element_overlap = "element_overlap"
    unexpected_hidden_control = "unexpected_hidden_control"
    overflow_or_truncation = "overflow_or_truncation"
    responsive_misalignment = "responsive_misalignment"
    readability_or_contrast = "readability_or_contrast"
    unexpected_layout_shift = "unexpected_layout_shift"
    content_misalignment = "content_misalignment"
    other_visual_issue = "other_visual_issue"


class Severity(str, Enum):
    """Finding severity (canonical LLD vocabulary)."""

    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    informational = "informational"


class ConfidenceLabel(str, Enum):
    """Human-readable confidence bucket (canonical LLD)."""

    low = "low"
    medium = "medium"
    high = "high"


class AnalysisOutcome(str, Enum):
    """What the visual-analysis call concluded (never implies a fix)."""

    findings_detected = "findings_detected"
    no_findings_detected = "no_findings_detected"
    inconclusive = "inconclusive"
    insufficient_evidence = "insufficient_evidence"
    refused = "refused"


# ---------------------------------------------------------------------------
# Shared sub-models
# ---------------------------------------------------------------------------


class ScreenEvidenceRef(BaseModel):
    """A screenshot the analysis may reason over, referenced by artifact id.

    Images are never passed by filesystem path or inline bytes across the
    boundary; the adapter resolves ids to paths server-side.
    """

    model_config = {"extra": "forbid"}

    artifact_id: UUID
    label: str | None = Field(default=None, max_length=200)
    viewport: Viewport | None = None


class DeterministicMeasurement(BaseModel):
    """A mechanical, collector-derived measurement (not a model guess)."""

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=200)
    value: float | str
    unit: str | None = Field(default=None, max_length=50)
    source: str | None = Field(default=None, max_length=200)


class PageContext(BaseModel):
    """Non-sensitive page metadata attached to a visual-analysis request."""

    model_config = {"extra": "forbid"}

    route: str | None = Field(default=None, max_length=500)
    viewport: Viewport
    browser: str | None = Field(default=None, max_length=100)


class ObservedFact(BaseModel):
    """A factual observation, backed by evidence references."""

    model_config = {"extra": "forbid"}

    statement: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=50)


class FindingCandidate(BaseModel):
    """A single candidate defect; observation and interpretation are separated."""

    model_config = {"extra": "forbid"}

    temporary_key: str = Field(min_length=1, max_length=100)
    category: FindingCategory
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    severity: Severity
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence_label: ConfidenceLabel | None = None
    observed_facts: list[ObservedFact] = Field(min_length=1, max_length=50)
    interpretation: str | None = Field(default=None, max_length=4000)
    evidence_refs: list[str] = Field(min_length=1, max_length=50)
    affected_element_refs: list[str] = Field(default_factory=list, max_length=100)
    expected_behavior_source: str | None = Field(default=None, max_length=500)
    limitations: list[str] = Field(default_factory=list, max_length=50)


# ---------------------------------------------------------------------------
# Vision request/result across the adapter boundary
# ---------------------------------------------------------------------------


class VisionRequest(BaseModel):
    """Everything the analyzer is allowed to receive for a visual analysis."""

    model_config = {"extra": "forbid"}

    request_id: str = Field(default_factory=lambda: str(new_uuid()))
    run_id: str | None = Field(default=None, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=100)
    schema_version: str = VISUAL_ANALYSIS_RESULT_SCHEMA_VERSION
    page_context: PageContext
    screenshots: list[ScreenEvidenceRef] = Field(default_factory=list, max_length=20)
    measurements: list[DeterministicMeasurement] = Field(
        default_factory=list, max_length=500
    )
    expected_behavior: str | None = Field(default=None, max_length=4000)
    allowed_categories: list[FindingCategory] = Field(min_length=1)
    max_findings: int = Field(default=20, ge=1, le=200)


class VisualAnalysisResult(BaseModel):
    """Validated visual-analysis output (never raw model text)."""

    model_config = {"extra": "forbid"}

    schema_version: str = VISUAL_ANALYSIS_RESULT_SCHEMA_VERSION
    analysis_outcome: AnalysisOutcome
    summary: str = Field(min_length=1, max_length=4000)
    findings: list[FindingCandidate] = Field(default_factory=list, max_length=200)
    limitations: list[str] = Field(default_factory=list, max_length=50)
    additional_evidence_needed: list[str] = Field(default_factory=list, max_length=50)


class RawVisionResult(BaseModel):
    """Raw, untrusted analyzer output exactly as returned.

    ``provider_label`` identifies the origin (``"MOCK"`` for the offline
    adapter; a configured adapter name otherwise) and MUST be preserved onto
    any persisted record or API response.
    """

    model_config = {"extra": "forbid"}

    text: str
    provider_label: str = Field(min_length=1, max_length=100)
    model_label: str | None = Field(default=None, max_length=200)
    duration_ms: float = Field(default=0.0, ge=0.0)
    finish_reason: str | None = Field(default=None, max_length=100)


class ModelCallMetadata(BaseModel):
    """Non-sensitive provenance for a gateway call (safe to log)."""

    model_config = {"extra": "forbid"}

    request_id: str
    provider_label: str
    model_label: str | None = None
    prompt_version: str
    schema_version: str
    attempts: int = Field(ge=1)
    duration_ms: float = Field(ge=0.0)
    finish_reason: str | None = None
    is_mock: bool = False
    warnings: list[str] = Field(default_factory=list)
