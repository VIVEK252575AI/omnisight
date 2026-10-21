"""Thin wrapper around a teammate's VLM module.

This adapter exists so the backend never re-implements the model call: it
delegates to an injected callable that implements the agreed teammate
contract and only translates its result into :class:`RawVisionResult`.

Expected teammate contract (the only assumption made here): a callable

    run(request: VisionRequest, image_paths: list[Path]) -> str

that returns the model's raw text (JSON preferred). Everything else — prompt
building, provider SDK, credentials — stays inside the teammate's module.

No teammate module exists yet, so this wrapper is provided and unit-tested with
an injected fake callable; a real integration is pending.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from app.ai.adapters.base import (
    ImageResolver,
    adapter_logger,
    call_with_timeout,
    enforce_image_budget,
    log_vision_call,
    resolve_image_paths,
)
from app.ai.contracts import RawVisionResult, VisionRequest
from app.ai.errors import (
    AiError,
    VisionInvalidResponseError,
    VisionUnavailableError,
)

TeammateCall = Callable[[VisionRequest, list[Path]], str]


class TeammateVisionAnalyzer:
    """Wrap an injected teammate callable behind the :class:`VisionAnalyzer` API."""

    is_mock = False

    def __init__(
        self,
        call: TeammateCall,
        *,
        resolver: ImageResolver | None = None,
        provider_label: str = "TEAMMATE",
        model_label: str | None = None,
        max_image_bytes: int = 5_242_880,
        max_images: int | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not callable(call):
            raise TypeError("TeammateVisionAnalyzer requires a callable")
        self._call = call
        self._resolver = resolver
        self.provider_label = provider_label
        self.model_label = model_label
        self._max_image_bytes = max_image_bytes
        self._max_images = max_images
        self._timeout = timeout_seconds
        self._logger = adapter_logger("teammate")

    def analyze(
        self, request: VisionRequest, *, repair: bool = False
    ) -> RawVisionResult:
        paths = resolve_image_paths(request, self._resolver)
        enforce_image_budget(
            paths, max_total_bytes=self._max_image_bytes, max_count=self._max_images
        )
        log_vision_call(
            self._logger,
            provider_label=self.provider_label,
            request=request,
            image_count=len(paths),
            repair=repair,
        )

        try:
            text = call_with_timeout(
                lambda: self._call(request, paths), self._timeout
            )
        except AiError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalise unknown provider errors
            raise VisionUnavailableError(
                f"teammate analyzer failed: {type(exc).__name__}"
            ) from None

        if not isinstance(text, str) or not text.strip():
            raise VisionInvalidResponseError(
                "teammate analyzer returned no usable text"
            )

        return RawVisionResult(
            text=text,
            provider_label=self.provider_label,
            model_label=self.model_label,
            duration_ms=0.0,
            finish_reason=None,
        )
