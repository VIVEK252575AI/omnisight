"""Pydantic request/response schemas — the backend's explicit API contract.

Field names for findings intentionally match the JSON contract examples in
the project README (`type`, `target`, `evidence`, `severity`, `confidence`)
so the frontend and AI modules can align against one definition.
"""

import json
from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from app.ids import new_uuid, utcnow


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

    The workflow advances forward through evidence -> detection -> analysis ->
    patching -> validation -> verification. States with FAILED_*/CANCELLED
    semantics are terminal. `patch_verified` can only be reached after
    `validation_passed` AND a successful visual re-check — never from a failed
    build or test.

    Detection sub-lifecycle (Week 2): `detecting` -> `detected` (findings, or a
    clean page), `detection_failed` (adapter or parse failure, with a recorded
    reason), or `needs_inspection` (the model could not decide).
    """

    created = "created"
    evidence_collection = "evidence_collection"
    detecting = "detecting"
    detected = "detected"
    detection_failed = "detection_failed"
    needs_inspection = "needs_inspection"
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
        # `detecting` is how the VLM detection service starts; `analysis`
        # remains legal as the manual/legacy entry point.
        {ScanStatus.detecting, ScanStatus.analysis, ScanStatus.failed, ScanStatus.cancelled}
    ),
    ScanStatus.detecting: frozenset(
        {
            ScanStatus.detected,
            ScanStatus.detection_failed,
            ScanStatus.needs_inspection,
        }
    ),
    ScanStatus.detected: frozenset({ScanStatus.analysis, ScanStatus.failed}),
    ScanStatus.detection_failed: frozenset(
        # Bounded retry of the same detection, or escalate for human review.
        {ScanStatus.detecting, ScanStatus.analysis, ScanStatus.failed}
    ),
    ScanStatus.needs_inspection: frozenset(
        {ScanStatus.detecting, ScanStatus.analysis, ScanStatus.failed}
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

    finding_id: str = Field(default_factory=lambda: str(new_uuid()))
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
    created_at: datetime = Field(default_factory=utcnow)


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
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    model_config = {"from_attributes": True}


class ErrorDetail(BaseModel):
    """Field-level detail attached to a validation error."""

    field: str | None = None
    message: str
    type: str | None = None


class ErrorBody(BaseModel):
    """The single error body used by every non-2xx response."""

    code: str
    message: str
    details: list[ErrorDetail] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    """Consistent error envelope for all non-2xx responses."""

    error: ErrorBody


class BuildEventType(str, Enum):
    """Closed set of SIMULATED CI/CD build event types."""

    started = "build.started"
    succeeded = "build.succeeded"
    failed = "build.failed"


_METADATA_MAX_KEYS = 50
_METADATA_MAX_BYTES = 4096


class BuildEvent(BaseModel):
    """Inbound SIMULATED CI/CD build event (webhook request body).

    This models a synthetic event posted by the local simulator or by a
    teammate's tooling. It is NOT a GitHub Actions/Jenkins payload: `source`
    is pinned to ``"simulated"`` so nothing can masquerade as a real
    integration.
    """

    model_config = {"extra": "forbid"}

    event_id: str = Field(min_length=1, max_length=200)
    event_type: BuildEventType
    source: Literal["simulated"] = "simulated"
    repository: str = Field(min_length=1, max_length=300)
    branch: str = Field(min_length=1, max_length=300)
    commit_sha: str = Field(min_length=1, max_length=100)
    build_id: str = Field(min_length=1, max_length=200)
    target_url: HttpUrl | None = Field(
        default=None,
        description="Required for build.succeeded; used to create the scan stub.",
    )
    timestamp: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def metadata_bounded(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(v) > _METADATA_MAX_KEYS:
            raise ValueError(f"metadata has too many keys (> {_METADATA_MAX_KEYS})")
        if len(json.dumps(v, default=str)) > _METADATA_MAX_BYTES:
            raise ValueError(f"metadata too large (> {_METADATA_MAX_BYTES} bytes)")
        return v

    @model_validator(mode="after")
    def target_url_required_on_success(self) -> "BuildEvent":
        if self.event_type is BuildEventType.succeeded and self.target_url is None:
            raise ValueError(
                "target_url is required when event_type is 'build.succeeded'"
            )
        return self


class CIEventRecord(BaseModel):
    """Persisted SIMULATED CI/CD event, optionally linked to a scan stub."""

    model_config = {"from_attributes": True}

    event_id: str
    event_type: BuildEventType
    source: Literal["simulated"] = "simulated"
    repository: str
    branch: str
    commit_sha: str
    build_id: str
    target_url: HttpUrl | None = None
    timestamp: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: Literal["accepted"] = "accepted"
    scan_id: UUID | None = None
    received_at: datetime = Field(default_factory=utcnow)


class WebhookAcceptedResponse(BaseModel):
    """POST /api/v1/webhooks/ci response (202 accepted / 200 duplicate)."""

    event_id: str
    status: Literal["accepted", "duplicate"]
    scan_id: UUID | None = None


# ---------------------------------------------------------------------------
# Evidence contract (observation.json v1)
# ---------------------------------------------------------------------------
#
# `Observation` is the JSON contract through which the Playwright collector
# delivers browser evidence to the backend. Field names mirror the project
# README's "Observation JSON" example; `schema_version`, `route`, `state` and
# `captured_at` are added by this contract. The collector emits ONE file per
# (route, state, viewport) as `observation.json` (see README "Browser
# Artifacts"); the backend stores each as an inert record.
#
# Contract summary (observation/v1):
#   schema_version  "observation/v1"          (bump on breaking change)
#   url             absolute http(s) URL of the captured page
#   route           relative route, e.g. "/checkout"
#   state           free-form UI state label, e.g. "cart-filled"
#   viewport        {width, height}
#   screenshot      path RELATIVE to the artifact root (png/jpg only)
#   elements[]      selector, tag, text, visible, in_viewport, box{x,y,w,h},
#                   optional computed_style (bounded subset)
#   console_errors[]  {message, type?}
#   network_errors[]  {url, method?, status?, error?, resource_type?}
#   captured_at     ISO-8601 timestamp
#
# The `_fixture` key seen in test fixtures is ignored (extra fields are
# tolerated so the collector can evolve; `schema_version` gates breakage).

OBSERVATION_SCHEMA_VERSION = "observation/v1"

_STYLE_MAX_PROPS = 40
_STYLE_KEY_MAX = 100
_STYLE_VALUE_MAX = 500


class ElementBox(BaseModel):
    """Bounding box in CSS pixels; width/height must be non-negative."""

    x: float
    y: float
    w: float = Field(ge=0)
    h: float = Field(ge=0)


class Element(BaseModel):
    """A single DOM element captured by the collector."""

    model_config = {"extra": "ignore"}

    selector: str = Field(min_length=1, max_length=500)
    tag: str = Field(min_length=1, max_length=100)
    text: str | None = Field(default=None, max_length=2000)
    visible: bool
    in_viewport: bool
    box: ElementBox
    computed_style: dict[str, str] | None = Field(
        default=None,
        description="Optional bounded subset of computed styles.",
    )

    @field_validator("computed_style")
    @classmethod
    def style_bounded(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return v
        if len(v) > _STYLE_MAX_PROPS:
            raise ValueError(f"computed_style has too many properties (> {_STYLE_MAX_PROPS})")
        for key, value in v.items():
            if len(key) > _STYLE_KEY_MAX or len(value) > _STYLE_VALUE_MAX:
                raise ValueError("computed_style entry too long")
        return v


class ConsoleError(BaseModel):
    """A console error captured from the page (untrusted, inert text)."""

    model_config = {"extra": "ignore"}

    message: str = Field(min_length=1, max_length=4000)
    type: str | None = Field(default=None, max_length=200)


class NetworkError(BaseModel):
    """A failed/erroring network request captured from the page."""

    model_config = {"extra": "ignore"}

    url: str = Field(min_length=1, max_length=2000)
    method: str | None = Field(default=None, max_length=20)
    status: int | None = None
    error: str | None = Field(default=None, max_length=2000)
    resource_type: str | None = Field(default=None, max_length=100)


class Observation(BaseModel):
    """Structured browser evidence for one (route, state, viewport) capture."""

    model_config = {"extra": "ignore"}

    schema_version: Literal["observation/v1"] = OBSERVATION_SCHEMA_VERSION
    url: HttpUrl
    route: str | None = Field(default=None, max_length=500)
    state: str | None = Field(default=None, max_length=200)
    viewport: Viewport
    screenshot: str = Field(
        min_length=1,
        max_length=1000,
        description="Artifact path relative to the configured artifact root.",
    )
    elements: list[Element] = Field(default_factory=list, max_length=2000)
    console_errors: list[ConsoleError] = Field(default_factory=list, max_length=500)
    network_errors: list[NetworkError] = Field(default_factory=list, max_length=500)
    captured_at: datetime


class EvidenceRecord(BaseModel):
    """A stored observation tied to a scan, with its referenced artifact."""

    model_config = {"from_attributes": True}

    evidence_id: UUID
    scan_id: UUID
    observation: Observation
    screenshot_artifact_id: UUID | None = None
    created_at: datetime = Field(default_factory=utcnow)
