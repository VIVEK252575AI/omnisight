"""The AI Gateway: the single, validated path to a VLM.

The gateway sits on top of a :class:`VisionAnalyzer` adapter and:

1. calls the adapter (which resolves images, enforces the payload budget, and
   applies the provider timeout),
2. parses the raw text into a :class:`VlmResponse` via
   :func:`~app.ai.parsing.parse_vlm_output` (schema + semantic checks), and
3. optionally performs ONE bounded repair re-ask on a repairable failure.

Two entry points:

- :meth:`AiGateway.analyze_visual` — the strict form: returns validated output
  or **raises** :class:`AiError` (used by callers that only care about success).
- :meth:`AiGateway.run` — the auditable form: **never raises** for AI failures
  and returns a :class:`GatewayResult` that keeps the raw text alongside the
  parsed result or the failure reason. The detection service uses it so a
  rejected response can still be recorded for audit.

Raw model output never leaves this module as an unvalidated value: it is only
ever surfaced inside a :class:`GatewayResult` (which callers must treat as
untrusted), and validation always happens before any parsed record exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.ai.adapters.base import VisionAnalyzer
from app.ai.contracts import (
    ModelCallMetadata,
    VisionRequest,
    VlmResponse,
)
from app.ai.errors import AiError, AiErrorCode
from app.ai.parsing import ParseReason, parse_vlm_output
from app.config import Settings

_REASON_TO_CODE = {
    ParseReason.empty: AiErrorCode.AI_RESPONSE_EMPTY,
    ParseReason.not_json: AiErrorCode.AI_RESPONSE_NOT_JSON,
    ParseReason.schema_invalid: AiErrorCode.AI_SCHEMA_VALIDATION_FAILED,
    ParseReason.unknown_category: AiErrorCode.AI_SCHEMA_VALIDATION_FAILED,
    ParseReason.contradictory: AiErrorCode.AI_EVIDENCE_VALIDATION_FAILED,
}


def _parse_error(reason: ParseReason, message: str | None) -> AiError:
    return AiError(
        _REASON_TO_CODE[reason],
        message or f"model output rejected: {reason.value}",
        details=[{"reason": reason.value}],
    )


@dataclass
class GatewayResult:
    """Auditable outcome of one gateway call (never fabricated).

    ``ok`` means ``response`` is set. On failure, ``error`` holds the AI error
    and ``reason`` holds the parse reason when the text was received but
    rejected. ``raw_text`` is the raw model text (already returned by the
    adapter) and is UNTRUSTED — callers must not execute, import, or render it.
    """

    provider_label: str
    is_mock: bool
    attempts: int = 0
    duration_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)
    response: VlmResponse | None = None
    metadata: ModelCallMetadata | None = None
    raw_text: str | None = None
    error: AiError | None = None
    reason: ParseReason | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.response is not None


class AiGateway:
    """Validated orchestration over a single :class:`VisionAnalyzer`."""

    def __init__(self, settings: Settings, analyzer: VisionAnalyzer) -> None:
        self._settings = settings
        self._analyzer = analyzer

    def analyze_visual(
        self,
        request: VisionRequest,
        *,
        candidates: list[str] | None = None,
    ) -> tuple[VlmResponse, ModelCallMetadata]:
        """Run a validated visual-analysis call, raising on any AI failure.

        ``candidates`` is the list of DOM candidate selectors/ids supplied to
        the model; a finding targeting anything else is flagged unverified.
        """
        result = self.run(request, candidates=candidates)
        if not result.ok:
            raise result.error or AiError(
                AiErrorCode.AI_PROVIDER_UNAVAILABLE, "no attempts were made"
            )
        return result.response, result.metadata

    def run(
        self,
        request: VisionRequest,
        *,
        candidates: list[str] | None = None,
    ) -> GatewayResult:
        """Run a visual-analysis call and describe the outcome.

        Never raises for AI-side failures (transport or parse); callers get a
        :class:`GatewayResult` they can audit and act on.
        """
        max_attempts = 1 + max(0, self._settings.ai_max_repair_attempts)
        provider_label = self._analyzer.provider_label
        is_mock = bool(getattr(self._analyzer, "is_mock", False))
        total_ms = 0.0
        warnings: list[str] = []
        raw_text: str | None = None
        attempts = 0
        last_error: AiError | None = None
        last_reason: ParseReason | None = None

        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            is_repair = attempt > 1
            try:
                raw = self._analyzer.analyze(request, repair=is_repair)
            except AiError as exc:
                # Transport/config failure: no usable text, no repair possible.
                last_error = exc
                break

            provider_label = raw.provider_label
            is_mock = bool(getattr(self._analyzer, "is_mock", False))
            total_ms += raw.duration_ms
            raw_text = raw.text

            parsed = parse_vlm_output(raw.text, candidates)
            if parsed.ok:
                if is_repair:
                    warnings.append(f"repaired after {attempt - 1} failed attempt(s)")
                return GatewayResult(
                    provider_label=provider_label,
                    is_mock=is_mock,
                    attempts=attempt,
                    duration_ms=total_ms,
                    warnings=warnings,
                    response=parsed.response,
                    metadata=ModelCallMetadata(
                        request_id=request.request_id,
                        provider_label=raw.provider_label,
                        model_label=raw.model_label,
                        prompt_version=request.prompt_version,
                        schema_version=request.schema_version,
                        attempts=attempt,
                        duration_ms=total_ms,
                        finish_reason=raw.finish_reason,
                        is_mock=is_mock,
                        warnings=warnings,
                    ),
                    raw_text=raw.text,
                )

            last_error = _parse_error(parsed.reason, parsed.message)
            last_reason = parsed.reason
            if last_error.repairable and attempt < max_attempts:
                warnings.append(
                    f"repair attempt {attempt} after {last_error.code.value}"
                )
                continue
            break

        error = last_error or AiError(
            AiErrorCode.AI_PROVIDER_UNAVAILABLE, "no attempts were made"
        )
        return GatewayResult(
            provider_label=provider_label,
            is_mock=is_mock,
            attempts=attempts,
            duration_ms=total_ms,
            warnings=warnings,
            raw_text=raw_text,
            error=error,
            reason=last_reason,
        )
