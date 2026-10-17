"""The AI Gateway: the single, validated path to a VLM.

The gateway sits on top of a :class:`VisionAnalyzer` adapter and:

1. calls the adapter (which resolves images, enforces the payload budget, and
   applies the provider timeout),
2. parses the raw text into a :class:`VlmResponse` via
   :func:`~app.ai.parsing.parse_vlm_output` (schema + semantic checks), and
3. optionally performs ONE bounded repair re-ask on a repairable failure.

Raw model output never leaves this module: callers receive validated contract
objects plus non-sensitive :class:`ModelCallMetadata` (including the
``provider_label``, so ``MOCK`` output is never presented as real inference).
"""

from __future__ import annotations

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
        """Run a validated visual-analysis call.

        ``candidates`` is the list of DOM candidate selectors/ids supplied to
        the model; a finding targeting anything else is flagged unverified.
        """
        max_attempts = 1 + max(0, self._settings.ai_max_repair_attempts)
        total_ms = 0.0
        warnings: list[str] = []
        last_error: AiError | None = None

        for attempt in range(1, max_attempts + 1):
            is_repair = attempt > 1
            raw = self._analyzer.analyze(request, repair=is_repair)
            total_ms += raw.duration_ms

            parsed = parse_vlm_output(raw.text, candidates)
            if parsed.ok:
                if is_repair:
                    warnings.append(f"repaired after {attempt - 1} failed attempt(s)")
                return parsed.response, ModelCallMetadata(
                    request_id=request.request_id,
                    provider_label=raw.provider_label,
                    model_label=raw.model_label,
                    prompt_version=request.prompt_version,
                    schema_version=request.schema_version,
                    attempts=attempt,
                    duration_ms=total_ms,
                    finish_reason=raw.finish_reason,
                    is_mock=self._analyzer.is_mock,
                    warnings=warnings,
                )

            last_error = _parse_error(parsed.reason, parsed.message)
            if last_error.repairable and attempt < max_attempts:
                warnings.append(
                    f"repair attempt {attempt} after {last_error.code.value}"
                )
                continue
            raise last_error

        raise last_error or AiError(
            AiErrorCode.AI_PROVIDER_UNAVAILABLE, "no attempts were made"
        )
