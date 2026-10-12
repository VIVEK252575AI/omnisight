"""Pydantic request/response schemas — the backend's explicit API contract.

Field names for findings intentionally match the JSON contract examples in
the project README (`type`, `target`, `evidence`, `severity`, `confidence`)
so the frontend and AI modules can align against one definition.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, HttpUrl, field_validator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Viewport(BaseModel):
    """Browser viewport dimensions for evidence capture."""

    width: int = Field(ge=240, le=3840)
    height: int = Field(ge=240, le=2160)

    model_config = {
        "json_schema_extra": {"examples": [{"width": 390, "height": 844}]}
    }


class ScanCreateRequest(BaseModel):
    """POST /api/scans request body."""

    target_url: HttpUrl
    viewports: list[Viewport] = Field(
        default_factory=lambda: [Viewport(width=390, height=844)],
        min_length=1,
        max_length=4,
        description="Viewports to capture; first one drives the initial scan.",
    )
    routes: list[str] | None = Field(
        default=None,
        max_length=10,
        description="Optional relative routes to visit (e.g. ['/checkout']).",
    )

    @field_validator("routes")
    @classmethod
    def routes_must_be_relative(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        for route in v:
            if not route.startswith("/"):
                raise ValueError(f"route must be relative (start with '/'): {route!r}")
            if any(ch.isspace() for ch in route):
                raise ValueError(f"route must not contain whitespace: {route!r}")
        return v


class ScanStatus(str, Enum):
    """Scan lifecycle states.

    The workflow advances forward through evidence -> analysis -> patching ->
    validation -> verification. States with FAILED_*/CANCELLED semantics are
    terminal. `patch_verified` can only be reached after `validation_passed`
    AND a successful visual re-check — never from a failed build or test.
    """

    created = "created"
    evidence_collection = "evidence_collection"
    analysis = "analysis"
    patch_proposed = "patch_proposed"
    sandbox_validation = "sandbox_validation"
    validation_passed = "validation_passed"
    validation_failed = "validation_failed"
    visual_verification = "visual_verification"
    patch_verified = "patch_verified"
    verification_failed = "verification_failed"
    review_preparation = "review_preparation"
    ready_for_review = "ready_for_review"
    failed = "failed"
    cancelled = "cancelled"


# Explicit legal transitions — the orchestrator must consult this map.
# Any transition not listed here is rejected as a state-machine error.
LEGAL_TRANSITIONS: dict[ScanStatus, frozenset[ScanStatus]] = {
    ScanStatus.created: frozenset(
        {ScanStatus.evidence_collection, ScanStatus.cancelled, ScanStatus.failed}
    ),
    ScanStatus.evidence_collection: frozenset(
        {ScanStatus.analysis, ScanStatus.failed, ScanStatus.cancelled}
    ),
    ScanStatus.analysis: frozenset(
        {ScanStatus.patch_proposed, ScanStatus.failed, ScanStatus.cancelled}
    ),
    ScanStatus.patch_proposed: frozenset(
        {ScanStatus.sandbox_validation, ScanStatus.failed, ScanStatus.cancelled}
    ),
    ScanStatus.sandbox_validation: frozenset(
        {ScanStatus.validation_passed, ScanStatus.validation_failed}
    ),
    ScanStatus.validation_failed: frozenset(
        # Bounded retry: may re-enter validation after a repair attempt,
        # or escalate; `review_preparation` covers documenting the failure for
        # human review — the failure is never erased by re-entering review.
        {ScanStatus.sandbox_validation, ScanStatus.failed, ScanStatus.review_preparation}
    ),
    ScanStatus.validation_passed: frozenset(
        {ScanStatus.visual_verification, ScanStatus.failed, ScanStatus.cancelled}
    ),
    ScanStatus.visual_verification: frozenset(
        {ScanStatus.patch_verified, ScanStatus.verification_failed}
    ),
    ScanStatus.verification_failed: frozenset(
        {ScanStatus.sandbox_validation, ScanStatus.failed, ScanStatus.review_preparation}
    ),
    ScanStatus.patch_verified: frozenset(
        {ScanStatus.review_preparation, ScanStatus.failed, ScanStatus.cancelled}
    ),
    ScanStatus.review_preparation: frozenset(
        {ScanStatus.ready_for_review, ScanStatus.failed}
    ),
    ScanStatus.ready_for_review: frozenset(),  # terminal
    ScanStatus.failed: frozenset(),  # terminal
    ScanStatus.cancelled: frozenset(),  # terminal
}

TERMINAL_STATES: frozenset[ScanStatus] = frozenset(
    {ScanStatus.ready_for_review, ScanStatus.failed, ScanStatus.cancelled}
)


class Finding(BaseModel):
    """A structured visual defect, produced by the VLM (validated, never raw).

    Severity/confidence gate whether the finding proceeds to patch
    generation. `evidence` must cite mechanical measurements where possible.
    """

    finding_id: str = Field(default_factory=lambda: str(uuid4()))
    type: Literal[
        "clipped_element",
        "element_overlap",
        "low_contrast_text",
        "broken_responsive_layout",
        "other",
    ]
    target: str = Field(min_length=1, description="Target selector, e.g. '#checkout'.")
    route: str | None = Field(default=None, min_length=1)
    viewport: Viewport | None = None
    evidence: str = Field(
        min_length=1,
        max_length=2000,
        description="Human-readable mechanical evidence, e.g. 'box.x + w = 540 > viewport 390'.",
    )
    explanation: str | None = Field(default=None, max_length=4000)
    severity: Literal["low", "medium", "high", "critical"]
    confidence: float = Field(ge=0.0, le=1.0)
    screenshot_ref: str | None = Field(
        default=None,
        description="Reference to stored evidence artifact; never raw bytes inline.",
    )
    created_at: datetime = Field(default_factory=_utcnow)


class ScanResponse(BaseModel):
    """Public scan record returned by GET/POST endpoints."""

    scan_id: UUID
    status: ScanStatus
    target_url: HttpUrl
    viewports: list[Viewport]
    routes: list[str] | None = None
    findings: list[Finding] = Field(default_factory=list)
    error: str | None = Field(
        default=None,
        description="Actionable failure description when status is a failure state.",
    )
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    model_config = {"from_attributes": True}


class ErrorResponse(BaseModel):
    """Consistent error envelope for non-2xx responses."""

    detail: str
    scan_id: UUID | None = None
    context: dict[str, Any] | None = None
