"""Visual verification: deterministic re-check + VLM re-check + policy (W3.3).

A repair is only *verified* when two independent kinds of evidence agree:

1. **Deterministic** — the original finding's geometric fact recomputed on fresh
   evidence (target now inside the viewport, overlap now zero, contrast now
   above threshold). This is measured, not judged.
2. **VLM** — the model re-examines before/after images of the ORIGINAL defect
   and answers ``resolved | not_resolved | inconclusive``.

The policy in :func:`decide` is the only place that turns those two into a
verdict, so the rules are auditable in one table:

| deterministic | VLM          | new high issue | verdict        |
|---------------|--------------|----------------|----------------|
| fail          | anything     | —              | ``failed``     |
| anything      | not_resolved | —              | ``failed``     |
| pass          | resolved     | yes            | ``failed``     |
| pass          | resolved     | no             | ``verified``   |
| pass          | inconclusive | —              | ``inconclusive``|
| not_checkable | resolved     | no             | ``verified``   |
| not_checkable | not_resolved | —              | ``failed``     |
| not_checkable | inconclusive | —              | ``inconclusive``|

A deterministic ``fail`` always overrides a VLM ``resolved``: the model saying
"fixed" is not proof. ``inconclusive`` never becomes ``verified``. Build
success is handled earlier and is never sufficient on its own.

Model output is untrusted: :class:`VlmVerificationResponse` is a strict,
closed schema (``extra="forbid"``), findings use the closed
:class:`~app.ai.contracts.FindingCategory` set, and anything unparseable
becomes ``inconclusive`` with a recorded reason — never a silent pass.
"""

from __future__ import annotations

import logging
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.ai.contracts import (
    Finding,
    FindingCategory,
    Severity,
    VisionRequest,
)
from app.ai.adapters.base import VisionAnalyzer
from app.ai.errors import VisionAdapterError, VisionConfigurationError
from app.config import Settings
from app.evidence_capture import CaptureResult, CapturedScreen
from app.ids import utcnow
from app.schemas import Viewport

logger = logging.getLogger("omnisight")

#: WCAG AA for normal text. Only used when the collector captured colours.
DEFAULT_MIN_CONTRAST_RATIO = 4.5

VERIFICATION_PROMPT_VERSION = "verify/v1"


class CheckStatus(str, Enum):
    """Outcome of one deterministic check."""

    passed = "pass"
    failed = "fail"
    not_checkable = "not_checkable"


class VerificationStatus(str, Enum):
    """Final verdict for one attempt's visual verification."""

    verified = "verified"
    failed = "failed"
    inconclusive = "inconclusive"


class VlmVerdict(str, Enum):
    """What the model concluded about the ORIGINAL defect."""

    resolved = "resolved"
    not_resolved = "not_resolved"
    inconclusive = "inconclusive"


class DeterministicCheck(BaseModel):
    """One measured check on fresh evidence."""

    name: str = Field(min_length=1, max_length=100)
    status: CheckStatus
    detail: str = Field(min_length=1, max_length=500)
    measured: float | None = None
    threshold: float | None = None


class DeterministicVerification(BaseModel):
    """All deterministic checks for one finding, plus the matched screen."""

    status: CheckStatus
    category: FindingCategory
    selector: str | None = Field(default=None, max_length=500)
    viewport: Viewport | None = None
    checks: list[DeterministicCheck] = Field(default_factory=list, max_length=20)
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=20)

    def summary(self) -> str:
        if self.status is CheckStatus.not_checkable:
            detail = self.checks[0].detail if self.checks else "no applicable check"
            return f"not checkable: {detail}"
        failing = [c for c in self.checks if c.status is CheckStatus.failed]
        if failing:
            return "; ".join(check.detail for check in failing[:3])
        return "; ".join(check.detail for check in self.checks[:3]) or "checks passed"


class VlmIssue(BaseModel):
    """A NEW issue the model reports after the patch (untrusted, bounded)."""

    model_config = ConfigDict(extra="forbid")

    category: FindingCategory
    severity: Severity | None = None
    observation: str = Field(min_length=1, max_length=500)


class VlmVerificationResponse(BaseModel):
    """Strict schema the verification model MUST return."""

    model_config = ConfigDict(extra="forbid")

    verdict: VlmVerdict
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    observation: str | None = Field(default=None, max_length=1000)
    new_issues: list[VlmIssue] = Field(default_factory=list, max_length=20)


class VlmVerification(BaseModel):
    """The parsed, validated outcome of the VLM re-check."""

    verdict: VlmVerdict
    confidence: float | None = None
    observation: str | None = Field(default=None, max_length=1000)
    new_issues: list[VlmIssue] = Field(default_factory=list, max_length=20)
    provider_label: str = Field(min_length=1, max_length=100)
    is_mock: bool = False
    reason: str | None = Field(default=None, max_length=500)

    @property
    def resolved(self) -> bool:
        return self.verdict is VlmVerdict.resolved


class VerificationResult(BaseModel):
    """Everything the attempt records about visual verification."""

    status: VerificationStatus
    deterministic: DeterministicVerification
    vlm: VlmVerification | None = None
    reasons: list[str] = Field(default_factory=list, max_length=20)
    collector_label: str = Field(min_length=1, max_length=100)
    is_fixture: bool = False
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=40)
    duration_seconds: float = Field(default=0.0, ge=0.0)
    finished_at: datetime = Field(default_factory=utcnow)

    @property
    def passed(self) -> bool:
        return self.status is VerificationStatus.verified

    def deterministic_evidence(self) -> str | None:
        """The measured facts behind the verdict, for the attempt record.

        Deliberately empty unless BOTH signals are usable: a fixture capture or
        a mock verdict must not be presented as proof of a real fix.
        """
        if self.is_fixture:
            return None
        if self.vlm is not None and self.vlm.is_mock:
            return None
        if self.deterministic.status is CheckStatus.not_checkable:
            return None
        return self.deterministic.summary()

    def summary(self) -> str:
        """One-line, non-sensitive summary for the attempt record."""
        label = "FIXTURE" if self.is_fixture else self.collector_label
        mock = " MOCK" if self.vlm is not None and self.vlm.is_mock else ""
        verdict = self.vlm.verdict.value if self.vlm is not None else "no-vlm"
        detail = "; ".join(self.reasons[:2])
        return (
            f"{self.status.value}: deterministic={self.deterministic.status.value}, "
            f"vlm={verdict} [{label}{mock}]{(' — ' + detail) if detail else ''}"
        )


# ---------------------------------------------------------------------------
# VLM verifier boundary
# ---------------------------------------------------------------------------


@runtime_checkable
class VlmVerifier(Protocol):
    """Re-checks the ORIGINAL defect on before/after images."""

    provider_label: str
    is_mock: bool

    def verify(self, request: VerificationRequest) -> VlmVerification:
        ...


class VerificationRequest(BaseModel):
    """Everything the verifier may send to a model (artifact ids, not paths)."""

    finding: Finding
    route: str | None = Field(default=None, max_length=500)
    viewport: Viewport | None = None
    before_artifact_ids: list[UUID] = Field(default_factory=list, max_length=10)
    after_artifact_ids: list[UUID] = Field(default_factory=list, max_length=10)
    deterministic_status: CheckStatus = CheckStatus.not_checkable


class MockVlmVerifier:
    """Deterministic, offline verification verdict (labelled ``MOCK``).

    Scenarios: ``resolved``, ``not_resolved``, ``inconclusive``, ``malformed``
    (returns unparseable text through the analyzer path) and ``provider_error``
    (raised as an adapter error). It never touches a network or a model.
    """

    is_mock = True
    provider_label = "MOCK"

    SCENARIOS = ("resolved", "not_resolved", "inconclusive")

    def __init__(self, scenario: str = "resolved", *, label: str = "MOCK") -> None:
        if scenario not in self.SCENARIOS:
            raise ValueError(
                f"unknown mock verification scenario '{scenario}'; "
                f"known: {', '.join(self.SCENARIOS)}"
            )
        self.scenario = scenario
        self.provider_label = label

    def verify(self, request: VerificationRequest) -> VlmVerification:
        verdict = VlmVerdict(self.scenario)
        return VlmVerification(
            verdict=verdict,
            confidence=0.0 if verdict is VlmVerdict.inconclusive else 0.9,
            observation="MOCK verification verdict; no model was called.",
            provider_label=self.provider_label,
            is_mock=True,
            reason=None,
        )


class AnalyzerVlmVerifier:
    """Runs the verification task through the existing :class:`VisionAnalyzer`.

    The same replaceable adapter boundary used for detection is reused: the
    request carries the before/after screenshots by artifact id and the
    ``verify/v1`` prompt version. The raw text is parsed into
    :class:`VlmVerificationResponse`; malformed output becomes ``inconclusive``
    with the reason recorded, never a pass.
    """

    def __init__(self, analyzer: VisionAnalyzer, *, settings: Settings) -> None:
        self.analyzer = analyzer
        self.settings = settings
        self.provider_label = analyzer.provider_label
        self.is_mock = analyzer.is_mock

    def verify(self, request: VerificationRequest) -> VlmVerification:
        vision_request = VisionRequest(
            prompt_version=VERIFICATION_PROMPT_VERSION,
            page_context=self._page_context(request),
            screenshots=self._screenshots(request),
            measurements=[],
            expected_behavior=(
                "The original defect described below must be gone in the AFTER "
                "image. Report the ORIGINAL defect as resolved, not_resolved or "
                "inconclusive, and list only NEW issues."
            ),
            allowed_categories=list(FindingCategory),
            max_findings=self.settings.verification_max_new_issues,
        )
        try:
            raw = self.analyzer.analyze(vision_request)
        except VisionAdapterError as exc:
            return VlmVerification(
                verdict=VlmVerdict.inconclusive,
                provider_label=self.provider_label,
                is_mock=self.is_mock,
                reason=f"provider error: {type(exc).__name__}",
            )
        return self._parse(raw.text)

    def _parse(self, text: str) -> VlmVerification:
        import json

        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return VlmVerification(
                verdict=VlmVerdict.inconclusive,
                provider_label=self.provider_label,
                is_mock=self.is_mock,
                reason="malformed: the verification response was not JSON",
            )
        try:
            parsed = VlmVerificationResponse.model_validate(payload)
        except ValidationError as exc:
            return VlmVerification(
                verdict=VlmVerdict.inconclusive,
                provider_label=self.provider_label,
                is_mock=self.is_mock,
                reason=f"malformed: {exc.error_count()} schema error(s)",
            )
        return VlmVerification(
            verdict=parsed.verdict,
            confidence=parsed.confidence,
            observation=parsed.observation,
            new_issues=parsed.new_issues,
            provider_label=self.provider_label,
            is_mock=self.is_mock,
        )

    def _screenshots(self, request: VerificationRequest) -> list:
        from app.ai.contracts import ScreenEvidenceRef

        refs: list[ScreenEvidenceRef] = []
        for artifact_id in request.before_artifact_ids[:5]:
            refs.append(
                ScreenEvidenceRef(
                    artifact_id=artifact_id,
                    label="BEFORE (original evidence)",
                    viewport=request.viewport,
                )
            )
        for artifact_id in request.after_artifact_ids[:5]:
            refs.append(
                ScreenEvidenceRef(
                    artifact_id=artifact_id,
                    label="AFTER (patched workspace)",
                    viewport=request.viewport,
                )
            )
        return refs

    def _page_context(self, request: VerificationRequest):
        from app.ai.contracts import PageContext
        from app.ai.contracts import Viewport as ContractViewport

        viewport = request.viewport or ContractViewport(width=390, height=844)
        return PageContext(route=request.route, viewport=viewport)


def build_vlm_verifier(settings: Settings, analyzer: VisionAnalyzer | None = None):
    """Build the configured :class:`VlmVerifier` or fail fast.

    ``mock`` (default) is deterministic and labelled ``MOCK``. When a real
    analyzer is supplied, the verification task is routed through the same
    adapter boundary so a configured provider needs no new plumbing.
    """
    name = (settings.vlm_verifier or "mock").strip().lower()
    if name == "mock":
        return MockVlmVerifier("resolved", label="MOCK")
    if analyzer is not None:
        return AnalyzerVlmVerifier(analyzer, settings=settings)
    raise VisionConfigurationError(
        f"unknown VLM verifier '{name}': no analyzer was supplied to run it"
    )


# ---------------------------------------------------------------------------
# Deterministic re-check
# ---------------------------------------------------------------------------


def _find_element(screen: CapturedScreen, selector: str | None):
    if not selector:
        return None
    wanted = selector.strip().lower()
    for element in screen.observation.elements:
        if element.selector.strip().lower() == wanted:
            return element
    return None


def _intersection_area(a, b) -> float:
    left = max(a.x, b.x)
    top = max(a.y, b.y)
    right = min(a.x + a.w, b.x + b.w)
    bottom = min(a.y + a.h, b.y + b.h)
    return max(0.0, right - left) * max(0.0, bottom - top)


def contrast_ratio(foreground: str, background: str) -> float | None:
    """WCAG contrast ratio from two ``#rrggbb`` / ``rgb(r, g, b)`` values."""
    fg = _parse_colour(foreground)
    bg = _parse_colour(background)
    if fg is None or bg is None:
        return None
    light, dark = sorted((_relative_luminance(fg), _relative_luminance(bg)), reverse=True)
    return round((light + 0.05) / (dark + 0.05), 3)


def _parse_colour(value: str) -> tuple[int, int, int] | None:
    text = (value or "").strip().lower()
    if text.startswith("#") and len(text) == 7:
        try:
            return (int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16))
        except ValueError:
            return None
    if text.startswith("rgb(") and text.endswith(")"):
        parts = [part.strip() for part in text[4:-1].split(",")]
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            values = tuple(int(part) for part in parts)
            if all(0 <= value <= 255 for value in values):
                return values  # type: ignore[return-value]
    return None


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    channels = []
    for value in rgb:
        channel = value / 255
        channels.append(
            channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4
        )
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def deterministic_check(
    finding: Finding,
    capture: CaptureResult,
    *,
    viewport: Viewport | None = None,
    min_contrast_ratio: float = DEFAULT_MIN_CONTRAST_RATIO,
) -> DeterministicVerification:
    """Recompute the original finding's geometric fact on fresh evidence.

    ``viewport`` is the viewport the ORIGINAL evidence was captured at (from the
    finding record). When it is unknown or absent from the fresh capture, the
    narrowest fresh screen is used and the choice is visible on the result.
    """
    screen = capture.screen_for(viewport)
    if screen is None:
        return DeterministicVerification(
            status=CheckStatus.not_checkable,
            category=finding.category,
            selector=finding.target_candidate,
            checks=[
                DeterministicCheck(
                    name="fresh_evidence",
                    status=CheckStatus.not_checkable,
                    detail="no fresh evidence was captured",
                )
            ],
        )

    category = finding.category
    selector = finding.target_candidate
    checks: list[DeterministicCheck] = []

    if category is FindingCategory.needs_inspection:
        return DeterministicVerification(
            status=CheckStatus.not_checkable,
            category=category,
            selector=selector,
            viewport=screen.viewport,
            checks=[
                DeterministicCheck(
                    name="category",
                    status=CheckStatus.not_checkable,
                    detail="'needs_inspection' has no deterministic check",
                )
            ],
            evidence_ids=[screen.artifact_id],
        )

    element = _find_element(screen, selector)
    if element is None:
        return DeterministicVerification(
            status=CheckStatus.not_checkable,
            category=category,
            selector=selector,
            viewport=screen.viewport,
            checks=[
                DeterministicCheck(
                    name="target_present",
                    status=CheckStatus.not_checkable,
                    detail=(
                        "the finding's target selector was not captured in the fresh "
                        "evidence"
                    ),
                )
            ],
            evidence_ids=[screen.artifact_id],
        )

    if category is FindingCategory.clipped_element:
        checks.append(
            DeterministicCheck(
                name="target_in_viewport",
                status=CheckStatus.passed if element.in_viewport else CheckStatus.failed,
                detail=(
                    "the target is fully inside the viewport"
                    if element.in_viewport
                    else "the target is still clipped by the viewport edge"
                ),
                measured=round(
                    element.box.x + element.box.w - screen.viewport.width, 3
                ),
                threshold=0.0,
            )
        )

    elif category is FindingCategory.responsive_layout_failure:
        overflow = round(element.box.x + element.box.w - screen.viewport.width, 3)
        checks.append(
            DeterministicCheck(
                name="horizontal_overflow",
                status=CheckStatus.passed if overflow <= 0 else CheckStatus.failed,
                detail=(
                    f"no horizontal overflow ({overflow:g}px)"
                    if overflow <= 0
                    else f"the target still overflows the viewport by {overflow:g}px"
                ),
                measured=overflow,
                threshold=0.0,
            )
        )

    elif category is FindingCategory.overlapping_elements:
        overlaps = []
        for other in screen.observation.elements:
            if other.selector == element.selector:
                continue
            area = _intersection_area(element.box, other.box)
            if area > 0:
                overlaps.append((other.selector, area))
        total = round(sum(area for _selector, area in overlaps), 3)
        checks.append(
            DeterministicCheck(
                name="overlap_area",
                status=CheckStatus.passed if total == 0 else CheckStatus.failed,
                detail=(
                    "the target no longer overlaps another captured element"
                    if total == 0
                    else "the target still overlaps "
                    + ", ".join(f"{name} ({area:g}px²)" for name, area in overlaps[:3])
                ),
                measured=total,
                threshold=0.0,
            )
        )

    elif category is FindingCategory.hidden_or_unreachable_element:
        checks.append(
            DeterministicCheck(
                name="target_visible",
                status=CheckStatus.passed if element.visible else CheckStatus.failed,
                detail=(
                    "the target is visible"
                    if element.visible
                    else "the target is still not visible"
                ),
            )
        )

    elif category is FindingCategory.low_contrast_text:
        style = element.computed_style or {}
        ratio = None
        if "color" in style and "background-color" in style:
            ratio = contrast_ratio(style["color"], style["background-color"])
        if ratio is None:
            checks.append(
                DeterministicCheck(
                    name="contrast_ratio",
                    status=CheckStatus.not_checkable,
                    detail="the fresh evidence has no usable colour values",
                )
            )
        else:
            checks.append(
                DeterministicCheck(
                    name="contrast_ratio",
                    status=CheckStatus.passed
                    if ratio >= min_contrast_ratio
                    else CheckStatus.failed,
                    detail=f"contrast ratio {ratio:g}:1 (minimum {min_contrast_ratio:g}:1)",
                    measured=ratio,
                    threshold=min_contrast_ratio,
                )
            )

    status = _combine(checks)
    return DeterministicVerification(
        status=status,
        category=category,
        selector=selector,
        viewport=screen.viewport,
        checks=checks,
        evidence_ids=[screen.artifact_id],
    )


def _combine(checks: list[DeterministicCheck]) -> CheckStatus:
    """Any failure wins; otherwise any pass; otherwise not checkable."""
    if any(check.status is CheckStatus.failed for check in checks):
        return CheckStatus.failed
    if any(check.status is CheckStatus.passed for check in checks):
        return CheckStatus.passed
    return CheckStatus.not_checkable


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


HIGH_SEVERITIES = frozenset({Severity.critical, Severity.high})


def decide(
    *,
    deterministic: DeterministicVerification,
    vlm: VlmVerification | None,
    validation_passed: bool = True,
    max_new_issues: int = 20,
) -> tuple[VerificationStatus, list[str]]:
    """Apply the verification policy table and return the verdict + reasons."""
    reasons: list[str] = []

    if not validation_passed:
        return VerificationStatus.failed, ["validation did not pass"]

    if deterministic.status is CheckStatus.failed:
        reasons.append(f"deterministic check failed: {deterministic.summary()}")
        # A failed measurement is final: a model cannot overrule it.
        if vlm is not None and vlm.resolved:
            reasons.append("the model reported a fix but the measurement disagrees")
        return VerificationStatus.failed, reasons

    if vlm is None:
        return VerificationStatus.inconclusive, ["no VLM verification was produced"]

    if vlm.verdict is VlmVerdict.not_resolved:
        return VerificationStatus.failed, ["the model reports the defect is still present"]

    if vlm.verdict is VlmVerdict.inconclusive:
        return VerificationStatus.inconclusive, [
            vlm.reason or "the model could not decide"
        ]

    high = [
        issue
        for issue in vlm.new_issues
        if issue.severity in HIGH_SEVERITIES
    ]
    if high:
        return VerificationStatus.failed, [
            f"the patch introduced {len(high)} new high-severity issue(s)"
        ]
    if len(vlm.new_issues) > max_new_issues:
        return VerificationStatus.failed, [
            f"the model reported {len(vlm.new_issues)} new issues"
        ]

    return VerificationStatus.verified, []
