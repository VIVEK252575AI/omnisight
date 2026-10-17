"""Structured visual-finding parser (Week 2, W2.3).

:func:`parse_vlm_output` is the single, defensive entry point that turns raw,
untrusted model text into a validated :class:`~app.ai.contracts.VlmResponse`.

Guarantees:

- **Never raises.** Every failure is reported as a :class:`ParseResult` with a
  machine-readable :class:`ParseReason`.
- **Conservative JSON extraction.** Whole-text JSON, or exactly one tagged
  (```json) block. No ``eval``/``exec``. Ambiguous or prose output is rejected.
- **Closed vocabulary.** An unknown finding category is rejected, not coerced.
- **Backend-assigned fields.** ``finding_id``, ``status`` and
  ``target_unverified`` are computed here; a model can never set them.
- **Sanitised text.** Control characters are stripped and strings are capped
  before the record is returned.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Iterable, Literal

from pydantic import BaseModel, Field, ValidationError

from app.ai.contracts import (
    FINDING_SCHEMA_VERSION,
    Finding,
    FindingCategory,
    FindingStatus,
    Severity,
    SourceRegion,
    VlmOutcome,
    VlmResponse,
)

# --- free-text caps (defensive; applied before the record is returned) -------
MAX_SUMMARY = 4000
MAX_OBSERVATION = 2000
MAX_EVIDENCE = 2000
MAX_TARGET = 500
MAX_CAUSE = 1000
MAX_CAUSES = 50

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FENCED_JSON = re.compile(r"```[ \t]*json[ \t]*\r?\n(.*?)```", re.IGNORECASE | re.DOTALL)


class ParseReason(str, Enum):
    """Why a parse attempt was rejected."""

    not_json = "not_json"
    schema_invalid = "schema_invalid"
    unknown_category = "unknown_category"
    contradictory = "contradictory"
    empty = "empty"


class ParseResult(BaseModel):
    """Outcome of :func:`parse_vlm_output` (ok carries a response)."""

    model_config = {"extra": "forbid"}

    status: Literal["ok", "rejected"]
    response: VlmResponse | None = None
    reason: ParseReason | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class _RawFinding(BaseModel):
    """Strict model of one model-provided finding.

    Backend-assigned fields (``finding_id``, ``status``, ``target_unverified``)
    are declared only so a model that sends them does not fail on an unexpected
    field; their values are **ignored** and recomputed by the backend.
    """

    model_config = {"extra": "forbid"}

    category: str
    target_candidate: str | None = None
    observation: str = Field(min_length=1)
    evidence: str = ""
    potential_causes: list[str] = Field(default_factory=list)
    severity: Severity | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    region: SourceRegion | None = None
    finding_id: str | None = None  # ignored
    status: str | None = None  # ignored
    target_unverified: bool | None = None  # ignored


def _clean(value: object, max_len: int) -> object:
    """Strip control characters and cap length on a string value."""
    if not isinstance(value, str):
        return value
    return _CTRL.sub("", value)[:max_len]


def _reject(reason: ParseReason, message: str) -> ParseResult:
    return ParseResult(status="rejected", reason=reason, message=message)


def _extract_json(text: str) -> tuple[object | None, ParseResult | None]:
    """Conservative JSON extraction: whole text, or one tagged ```json block."""
    stripped = text.strip()
    try:
        return json.loads(stripped), None
    except json.JSONDecodeError:
        pass

    blocks = _FENCED_JSON.findall(stripped)
    if len(blocks) != 1:
        return None, _reject(
            ParseReason.not_json,
            "output is not JSON and does not contain exactly one ```json block",
        )
    try:
        return json.loads(blocks[0]), None
    except json.JSONDecodeError:
        return None, _reject(ParseReason.not_json, "tagged JSON block is not valid JSON")


def _coerce_category(value: object) -> FindingCategory | None:
    if not isinstance(value, str):
        return None
    try:
        return FindingCategory(value)
    except ValueError:
        return None


def parse_vlm_output(
    raw_text: object,
    candidates: Iterable[str] | None = None,
) -> ParseResult:
    """Parse raw model text into a validated :class:`VlmResponse`.

    ``candidates`` is the set of DOM candidate selectors/ids supplied to the
    model. A finding whose ``target_candidate`` is not in that set is kept but
    flagged ``target_unverified=True``; a ``None`` target is not flagged.
    """
    try:
        return _parse(raw_text, candidates)
    except Exception as exc:  # defensive: parsing must never raise
        return _reject(ParseReason.schema_invalid, f"unexpected parse error: {exc}")


def _parse(
    raw_text: object,
    candidates: Iterable[str] | None,
) -> ParseResult:
    if not isinstance(raw_text, str) or not raw_text.strip():
        return _reject(ParseReason.empty, "model output is empty")

    data, failure = _extract_json(raw_text)
    if failure is not None:
        return failure
    if not isinstance(data, dict):
        return _reject(ParseReason.schema_invalid, "top-level JSON must be an object")

    if "schema_version" in data and not isinstance(data["schema_version"], int):
        return _reject(ParseReason.schema_invalid, "schema_version must be an integer")

    outcome = data.get("outcome")
    if not isinstance(outcome, str) or outcome not in VlmOutcome._value2member_map_:
        return _reject(
            ParseReason.schema_invalid,
            "outcome must be one of: " + ", ".join(o.value for o in VlmOutcome),
        )

    summary = data.get("summary", "")
    if not isinstance(summary, str):
        return _reject(ParseReason.schema_invalid, "summary must be a string")

    raw_findings = data.get("findings")
    if not isinstance(raw_findings, list):
        return _reject(ParseReason.schema_invalid, "findings must be a list")

    allowed_targets = set(candidates) if candidates is not None else None

    findings: list[Finding] = []
    for index, item in enumerate(raw_findings):
        if not isinstance(item, dict):
            return _reject(
                ParseReason.schema_invalid, f"findings[{index}] must be an object"
            )
        category = _coerce_category(item.get("category"))
        if category is None:
            if isinstance(item.get("category"), str):
                return _reject(
                    ParseReason.unknown_category,
                    f"findings[{index}] has unknown category '{item.get('category')}'",
                )
            return _reject(
                ParseReason.schema_invalid, f"findings[{index}] is missing a category"
            )
        try:
            raw = _RawFinding.model_validate(item)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first.get("loc", []))
            return _reject(
                ParseReason.schema_invalid,
                f"findings[{index}].{where}: {first.get('msg', 'invalid')}",
            )

        target = raw.target_candidate
        target_unverified = target is not None and (
            allowed_targets is None or target not in allowed_targets
        )
        findings.append(
            Finding(
                category=category,
                target_candidate=_clean(target, MAX_TARGET) if target else None,
                target_unverified=target_unverified,
                observation=_clean(raw.observation, MAX_OBSERVATION),
                evidence=_clean(raw.evidence, MAX_EVIDENCE),
                potential_causes=[
                    _clean(cause, MAX_CAUSE)
                    for cause in raw.potential_causes[:MAX_CAUSES]
                ],
                severity=raw.severity,
                confidence=raw.confidence,
                region=raw.region,
                status=FindingStatus.detected,
            )
        )

    findings = _merge_duplicates(findings)

    contradiction = _contradiction(outcome, findings)
    if contradiction is not None:
        return _reject(ParseReason.contradictory, contradiction)

    response = VlmResponse(
        schema_version=FINDING_SCHEMA_VERSION,
        outcome=VlmOutcome(outcome),
        findings=findings,
        summary=_clean(summary, MAX_SUMMARY),
    )
    return ParseResult(status="ok", response=response)


def _merge_duplicates(findings: list[Finding]) -> list[Finding]:
    """Merge findings sharing (category, target_candidate), preserving order."""
    merged: dict[tuple[str, str | None], Finding] = {}
    order: list[tuple[str, str | None]] = []
    for finding in findings:
        key = (finding.category.value, finding.target_candidate)
        if key in merged:
            existing = merged[key]
            for cause in finding.potential_causes:
                if cause not in existing.potential_causes:
                    existing.potential_causes.append(cause)
            if finding.observation and finding.observation not in existing.observation:
                if not existing.observation:
                    existing.observation = finding.observation
        else:
            merged[key] = finding
            order.append(key)
    return [merged[key] for key in order]


def _contradiction(outcome: str, findings: list[Finding]) -> str | None:
    if outcome == VlmOutcome.no_defects.value and findings:
        return "outcome is 'no_defects' but findings were returned"
    if outcome == VlmOutcome.defects_found.value and not findings:
        return "outcome is 'defects_found' but no findings were returned"
    return None
