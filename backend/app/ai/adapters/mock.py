"""Deterministic, offline MOCK VLM adapter.

Never touches the network or a model. Canned responses are keyed by *scenario*
so tests and offline development can drive every outcome deterministically:

- ``clipped_button``  -> a ``defects_found`` result (clipped_element),
- ``healthy_page``    -> ``no_defects``,
- ``needs_inspection``-> ``needs_inspection`` (model could not decide),
- ``malformed_text``  -> non-JSON text (not_json failure),
- ``malformed_schema``-> valid JSON with an invalid finding (schema_invalid),
- ``empty``           -> empty text (empty-output failure),
- ``timeout``         -> raises :class:`VisionTimeoutError`,
- ``provider_error``  -> raises :class:`VisionUnavailableError`.

Every result is labelled ``MOCK``.
"""

from __future__ import annotations

import json
from typing import Sequence

from app.ai.adapters.base import (
    ImageResolver,
    adapter_logger,
    enforce_image_budget,
    log_vision_call,
    resolve_image_paths,
)
from app.ai.contracts import RawVisionResult, VisionRequest
from app.ai.errors import (
    VisionConfigurationError,
    VisionTimeoutError,
    VisionUnavailableError,
)

MOCK_LABEL = "MOCK"

SCENARIOS = (
    "clipped_button",
    "healthy_page",
    "needs_inspection",
    "malformed_text",
    "malformed_schema",
    "empty",
    "timeout",
    "provider_error",
)


class MockVisionAnalyzer:
    """A scripted, network-free :class:`VisionAnalyzer` (labelled ``MOCK``)."""

    provider_label = MOCK_LABEL
    model_label = "mock-vlm-deterministic"
    is_mock = True

    def __init__(
        self,
        scenario: str | Sequence[str] = "clipped_button",
        *,
        resolver: ImageResolver | None = None,
        max_image_bytes: int = 5_242_880,
        max_images: int | None = None,
    ) -> None:
        scenarios = [scenario] if isinstance(scenario, str) else list(scenario)
        if not scenarios:
            raise VisionConfigurationError("mock scenario list must not be empty")
        for name in scenarios:
            if name not in SCENARIOS:
                raise VisionConfigurationError(
                    f"unknown mock scenario '{name}'; known: {', '.join(SCENARIOS)}"
                )
        self._scenarios = scenarios
        self._resolver = resolver
        self._max_image_bytes = max_image_bytes
        self._max_images = max_images
        self._logger = adapter_logger("mock")
        self.calls: list[dict] = []

    # -- VisionAnalyzer -----------------------------------------------------

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
        self.calls.append({"request_id": request.request_id, "repair": repair})

        scenario = (
            self._scenarios.pop(0)
            if len(self._scenarios) > 1
            else self._scenarios[0]
        )

        if scenario == "timeout":
            raise VisionTimeoutError("MOCK: simulated provider timeout")
        if scenario == "provider_error":
            raise VisionUnavailableError("MOCK: simulated provider unavailable")

        return RawVisionResult(
            text=self._text_for(scenario, request),
            provider_label=self.provider_label,
            model_label=self.model_label,
            duration_ms=0.0,
            finish_reason="mock",
        )

    # -- canned payloads ----------------------------------------------------

    def _text_for(self, scenario: str, request: VisionRequest) -> str:
        if scenario == "malformed_text":
            return "I think the button is probably clipped. (not JSON)"
        if scenario == "malformed_schema":
            return json.dumps(
                {
                    "schema_version": 1,
                    "outcome": "defects_found",
                    "summary": "MOCK: malformed finding payload.",
                    "findings": [{"category": "clipped_element"}],
                }
            )
        if scenario == "empty":
            return ""
        if scenario == "healthy_page":
            return json.dumps(self._healthy_payload())
        if scenario == "needs_inspection":
            return json.dumps(
                {
                    "schema_version": 1,
                    "outcome": "needs_inspection",
                    "summary": "MOCK: possible overlap; needs a human look.",
                    "findings": [],
                }
            )
        return json.dumps(self._clipped_button_payload(request))

    @staticmethod
    def _clipped_button_payload(request: VisionRequest) -> dict:
        return {
            "schema_version": 1,
            "outcome": "defects_found",
            "summary": "MOCK: primary button is clipped at this viewport.",
            "findings": [
                {
                    "category": "clipped_element",
                    "target_candidate": "#submit",
                    "observation": "The primary button extends past the viewport.",
                    "evidence": "button box exceeds viewport width (MOCK geometry).",
                    "potential_causes": ["fixed pixel width"],
                    "severity": "high",
                    "confidence": 0.9,
                    "region": {"x": 10.0, "y": 20.0, "width": 220.0, "height": 48.0},
                }
            ],
        }

    @staticmethod
    def _healthy_payload() -> dict:
        return {
            "schema_version": 1,
            "outcome": "no_defects",
            "summary": "MOCK: no visual defects detected.",
            "findings": [],
        }
