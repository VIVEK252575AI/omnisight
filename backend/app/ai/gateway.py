"""The AI Gateway: the single, validated path to any model.

The gateway:

1. checks the gateway is enabled and the provider supports the capability,
2. resolves screenshot artifact ids to on-disk paths (server-side only),
3. calls the provider under a timeout,
4. parses the raw text into the expected contract model,
5. applies semantic validation, and
6. optionally performs ONE bounded repair re-ask on a shape failure.

Raw model output never leaves this module: callers receive validated contract
objects plus non-sensitive :class:`ModelCallMetadata`.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Callable, Sequence, TypeVar
from uuid import UUID

from pydantic import BaseModel, ValidationError

from app.ai.contracts import (
    AiCapability,
    ModelCallMetadata,
    PatchGenerationRequest,
    PatchProposal,
    RawModelResult,
    VisualAnalysisRequest,
    VisualAnalysisResult,
)
from app.ai.errors import AiError, AiErrorCode
from app.ai.prompts import (
    STRUCTURED_OUTPUT_REPAIR_PROMPT_VERSION,
    load_prompt,
)
from app.ai.providers.base import ModelProvider
from app.ai.validation import validate_patch_proposal, validate_visual_analysis
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
    """Provider-neutral orchestration over a single :class:`ModelProvider`."""

    def __init__(
        self,
        settings: Settings,
        provider: ModelProvider,
        *,
        resolve_artifact: Callable[[UUID], Path | None] | None = None,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._resolve_artifact = resolve_artifact

    # -- public API ---------------------------------------------------------

    def analyze_visual(
        self, request: VisualAnalysisRequest
    ) -> tuple[VisualAnalysisResult, ModelCallMetadata]:
        """Run a validated visual-analysis call."""
        return self._run(
            capability=AiCapability.visual_analysis,
            request=request,
            result_model=VisualAnalysisResult,
            validate=lambda result: validate_visual_analysis(result, request),
            prompt_version=request.prompt_version,
            schema_version=request.schema_version,
            images=self._resolve_images(request.screenshots),
        )

    def generate_patch(
        self, request: PatchGenerationRequest
    ) -> tuple[PatchProposal, ModelCallMetadata]:
        """Run a validated patch-generation call."""
        return self._run(
            capability=AiCapability.patch_generation,
            request=request,
            result_model=PatchProposal,
            validate=lambda proposal: validate_patch_proposal(proposal, request),
            prompt_version=request.prompt_version,
            schema_version=request.schema_version,
            images=[],
        )

    # -- internals ----------------------------------------------------------

    def _ensure_enabled(self) -> None:
        if self._settings.ai_mode == "disabled":
            raise AiError(
                AiErrorCode.AI_CONFIGURATION_ERROR,
                "AI gateway is disabled (OMNISIGHT_AI_MODE=disabled)",
            )

    def _resolve_images(self, screenshots: Sequence) -> list[Path]:
        if self._resolve_artifact is None:
            return []
        paths: list[Path] = []
        for shot in screenshots:
            resolved = self._resolve_artifact(shot.artifact_id)
            if resolved is not None:
                paths.append(resolved)
        return paths

    def _invoke_with_timeout(
        self,
        *,
        capability: AiCapability,
        system_prompt: str,
        user_prompt: str,
        image_paths: list[Path],
        request_id: str,
    ) -> RawModelResult:
        timeout = self._settings.ai_provider_timeout_seconds
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(
                self._provider.invoke,
                capability=capability,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                image_paths=image_paths,
                request_id=request_id,
            )
            try:
                return future.result(timeout=timeout)
            except FutureTimeoutError:
                future.cancel()
                raise AiError(
                    AiErrorCode.AI_TIMEOUT,
                    f"provider did not respond within {timeout}s",
                    retryable=True,
                ) from None
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _run(
        self,
        *,
        capability: AiCapability,
        request: BaseModel,
        result_model: type[_ResultT],
        validate: Callable[[_ResultT], None],
        prompt_version: str,
        schema_version: str,
        images: list[Path],
    ) -> tuple[_ResultT, ModelCallMetadata]:
        self._ensure_enabled()
        if not self._provider.supports(capability):
            raise AiError(
                AiErrorCode.AI_CAPABILITY_UNSUPPORTED,
                f"provider '{self._provider.provider_id}' does not support "
                f"'{capability.value}'",
            )

        system_prompt = load_prompt(prompt_version)
        user_prompt = json.dumps(request.model_dump(mode="json"), indent=2)
        repair_prompt = load_prompt(STRUCTURED_OUTPUT_REPAIR_PROMPT_VERSION)

        max_attempts = 1 + max(0, self._settings.ai_max_repair_attempts)
        total_ms = 0.0
        warnings: list[str] = []
        last_error: AiError | None = None

        for attempt in range(1, max_attempts + 1):
            is_repair = attempt > 1
            raw = self._invoke_with_timeout(
                capability=capability,
                system_prompt=repair_prompt if is_repair else system_prompt,
                user_prompt=user_prompt,
                image_paths=images,
                request_id=request.request_id,
            )
            total_ms += raw.duration_ms

            try:
                parsed = result_model.model_validate(_extract_json(raw.text))
                validate(parsed)
            except (AiError, ValidationError) as exc:
                last_error = _schema_error(exc) if isinstance(exc, ValidationError) else exc
                can_repair = last_error.repairable and attempt < max_attempts
                if can_repair:
                    warnings.append(
                        f"repair attempt {attempt} after {last_error.code.value}"
                    )
                    continue
                raise last_error

            if is_repair:
                warnings.append(f"repaired after {attempt - 1} failed attempt(s)")
            return parsed, ModelCallMetadata(
                request_id=request.request_id,
                capability=capability,
                provider_id=self._provider.provider_id,
                model_id=self._provider.model_id,
                prompt_version=prompt_version,
                schema_version=schema_version,
                attempts=attempt,
                duration_ms=total_ms,
                finish_reason=raw.finish_reason,
                is_mock=self._provider.is_mock,
                warnings=warnings,
            )

        # Unreachable: the loop either returns or raises.
        raise last_error or AiError(
            AiErrorCode.AI_PROVIDER_UNAVAILABLE, "no attempts were made"
        )
