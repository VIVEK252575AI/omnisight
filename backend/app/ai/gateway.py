"""The AI Gateway: the single, validated path to a VLM.

The gateway sits on top of a :class:`VisionAnalyzer` adapter and:

1. calls the adapter (which resolves images, enforces the payload budget, and
   applies the provider timeout),
2. parses the raw text into :class:`VisualAnalysisResult`,
3. applies semantic validation, and
4. optionally performs ONE bounded repair re-ask on a shape failure.

Raw model output never leaves this module: callers receive validated contract
objects plus non-sensitive :class:`ModelCallMetadata` (including the
``provider_label``, so ``MOCK`` output is never presented as real inference).
"""

from __future__ import annotations

import json
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

from app.ai.adapters.base import VisionAnalyzer
from app.ai.contracts import (
    ModelCallMetadata,
    VisionRequest,
    VisualAnalysisResult,
)
from app.ai.errors import AiError, AiErrorCode
from app.ai.validation import validate_visual_analysis
from app.config import Settings

_ResultT = TypeVar("_ResultT", bound=BaseModel)


def _extract_json(text: str) -> dict:
    """Parse model text into a JSON object, tolerating ``` fences."""
    if text is None or not text.strip():
        raise AiError(AiErrorCode.AI_RESPONSE_EMPTY, "provider returned empty output")

    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate[3:]
        newline = candidate.find("\n")
        if newline != -1:
            candidate = candidate[newline + 1 :]
        candidate = candidate.rstrip("`").strip()

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise AiError(
                AiErrorCode.AI_RESPONSE_NOT_JSON,
                "provider output is not valid JSON",
            ) from None
        try:
            data = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            raise AiError(
                AiErrorCode.AI_RESPONSE_NOT_JSON,
                "provider output is not valid JSON",
            ) from None

    if not isinstance(data, dict):
        raise AiError(
            AiErrorCode.AI_RESPONSE_NOT_JSON,
            "provider JSON output must be an object",
        )
    return data


def _schema_error(exc: ValidationError) -> AiError:
    details = [
        {
            "field": ".".join(str(part) for part in err.get("loc", [])),
            "message": err.get("msg", ""),
            "type": err.get("type", ""),
        }
        for err in exc.errors()
    ]
    return AiError(
        AiErrorCode.AI_SCHEMA_VALIDATION_FAILED,
        "provider output failed schema validation",
        details=details,
    )


class AiGateway:
    """Validated orchestration over a single :class:`VisionAnalyzer`."""

    def __init__(self, settings: Settings, analyzer: VisionAnalyzer) -> None:
        self._settings = settings
        self._analyzer = analyzer

    def analyze_visual(
        self, request: VisionRequest
    ) -> tuple[VisualAnalysisResult, ModelCallMetadata]:
        """Run a validated visual-analysis call."""
        return self._run(
            request=request,
            result_model=VisualAnalysisResult,
            validate=lambda result: validate_visual_analysis(result, request),
        )

    def _run(
        self,
        *,
        request: VisionRequest,
        result_model: type[_ResultT],
        validate: Callable[[_ResultT], None],
    ) -> tuple[_ResultT, ModelCallMetadata]:
        max_attempts = 1 + max(0, self._settings.ai_max_repair_attempts)
        total_ms = 0.0
        warnings: list[str] = []
        last_error: AiError | None = None

        for attempt in range(1, max_attempts + 1):
            is_repair = attempt > 1
            raw = self._analyzer.analyze(request, repair=is_repair)
            total_ms += raw.duration_ms

            try:
                parsed = result_model.model_validate(_extract_json(raw.text))
                validate(parsed)
            except (AiError, ValidationError) as exc:
                last_error = (
                    _schema_error(exc) if isinstance(exc, ValidationError) else exc
                )
                if last_error.repairable and attempt < max_attempts:
                    warnings.append(
                        f"repair attempt {attempt} after {last_error.code.value}"
                    )
                    continue
                raise last_error

            if is_repair:
                warnings.append(f"repaired after {attempt - 1} failed attempt(s)")
            return parsed, ModelCallMetadata(
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

        raise last_error or AiError(
            AiErrorCode.AI_PROVIDER_UNAVAILABLE, "no attempts were made"
        )
