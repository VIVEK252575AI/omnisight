"""Provider-neutral VLM boundary contracts (Week 2).

These Pydantic models are the *only* shape exchanged across the vision-model
boundary. Nothing here references a provider, SDK, endpoint, or model id.

The visual-finding vocabulary is the canonical Week-2 schema (W2.3):

- categories -> :class:`FindingCategory` (``clipped_element``, ...),
- severity   -> :class:`Severity` (``critical/high/medium/low/informational``),
- confidence -> optional numeric ``confidence`` in ``[0, 1]``.

Model output crosses the boundary as :class:`RawVisionResult`: raw, untrusted
text plus a ``provider_label`` (e.g. ``"MOCK"``). It is only ever turned into a
:class:`VlmResponse` after :func:`app.ai.parsing.parse_vlm_output` has parsed,
schema-checked, and semantically checked it. Backend-assigned fields
(``finding_id``, ``status``, ``target_unverified``) are *never* trusted from the
model.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.ids import new_uuid
from app.schemas import Viewport

FINDING_SCHEMA_VERSION = 1


class FindingCategory(str, Enum):
    """Closed set of allowed visual-defect categories (canonical Week-2 schema)."""

    clipped_element = "clipped_element"
    overlapping_elements = "overlapping_elements"
    low_contrast_text = "low_contrast_text"
    responsive_layout_failure = "responsive_layout_failure"
    hidden_or_unreachable_element = "hidden_or_unreachable_element"
    needs_inspection = "needs_inspection"


class Severity(str, Enum):
    """Finding severity (canonical LLD vocabulary)."""

    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    informational = "informational"


class VlmOutcome(str, Enum):
    """What the visual-analysis call concluded (never implies a fix)."""

    defects_found = "defects_found"
    no_defects = "no_defects"
    needs_inspection = "needs_inspection"


class FindingStatus(str, Enum):
    """Lifecycle status of a finding (backend-assigned).

    ``detected``        — a validated finding, still open for a patch proposal.
    ``patch_proposed``  — a proposal reached ``proposed``; the finding is not
                          re-proposable until that proposal is reviewed. A
                          ``rejected`` proposal leaves the finding ``detected``
                          so a corrected attempt is allowed.
    """

    detected = "detected"
    patch_proposed = "patch_proposed"


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


class BrowserNetworkSignal(BaseModel):
    """A bounded, content-free summary of a browser network error."""

    model_config = {"extra": "forbid"}

    kind: Literal["request_failed", "http_error"]
    method: str | None = Field(default=None, max_length=20)
    status: int | None = Field(default=None, ge=100, le=599)
    resource_type: str | None = Field(default=None, max_length=100)


class BrowserDiagnostics(BaseModel):
    """Non-sensitive counts and request metadata; no page error text or URLs."""

    model_config = {"extra": "forbid"}

    console_error_count: int = Field(default=0, ge=0, le=20)
    page_exception_count: int = Field(default=0, ge=0, le=20)
    network_errors: list[BrowserNetworkSignal] = Field(default_factory=list, max_length=20)


class PageContext(BaseModel):
    """Non-sensitive page metadata attached to a visual-analysis request."""

    model_config = {"extra": "forbid"}

    route: str | None = Field(default=None, max_length=500)
    viewport: Viewport
    browser: str | None = Field(default=None, max_length=100)
    browser_diagnostics: BrowserDiagnostics = Field(default_factory=BrowserDiagnostics)


class SourceRegion(BaseModel):
    """A bounding box in source-image coordinates (x, y, width, height)."""

    model_config = {"extra": "forbid"}

    x: float
    y: float
    width: float = Field(ge=0.0)
    height: float = Field(ge=0.0)


# ---------------------------------------------------------------------------
# Finding record (canonical Week-2 schema)
# ---------------------------------------------------------------------------


class Finding(BaseModel):
    """A single visual finding (backend-validated record).

    ``finding_id``, ``target_unverified`` and ``status`` are assigned by the
    backend during parsing; a model can never set them.
    """

    model_config = {"extra": "forbid"}

    finding_id: str = Field(default_factory=lambda: str(new_uuid()))
    category: FindingCategory
    target_candidate: str | None = Field(default=None, max_length=500)
    target_unverified: bool = False
    observation: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(default="", max_length=2000)
    potential_causes: list[str] = Field(default_factory=list, max_length=50)
    severity: Severity | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    region: SourceRegion | None = None
    status: FindingStatus = FindingStatus.detected


class VlmResponse(BaseModel):
    """Validated visual-analysis output (never raw model text)."""

    model_config = {"extra": "forbid"}

    schema_version: int = FINDING_SCHEMA_VERSION
    outcome: VlmOutcome
    findings: list[Finding] = Field(default_factory=list, max_length=200)
    summary: str = Field(default="", max_length=4000)


# ---------------------------------------------------------------------------
# Vision request/result across the adapter boundary
# ---------------------------------------------------------------------------


class VisionRequest(BaseModel):
    """Everything the analyzer is allowed to receive for a visual analysis."""

    model_config = {"extra": "forbid"}

    request_id: str = Field(default_factory=lambda: str(new_uuid()))
    run_id: str | None = Field(default=None, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=100)
    schema_version: int = FINDING_SCHEMA_VERSION
    page_context: PageContext
    screenshots: list[ScreenEvidenceRef] = Field(default_factory=list, max_length=20)
    measurements: list[DeterministicMeasurement] = Field(
        default_factory=list, max_length=500
    )
    expected_behavior: str | None = Field(default=None, max_length=4000)
    allowed_categories: list[FindingCategory] = Field(min_length=1)
    max_findings: int = Field(default=20, ge=1, le=200)


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
    schema_version: int
    attempts: int = Field(ge=1)
    duration_ms: float = Field(ge=0.0)
    finish_reason: str | None = None
    is_mock: bool = False
    warnings: list[str] = Field(default_factory=list)
