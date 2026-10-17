"""Deterministic, offline MOCK VLM adapter.

Never touches the network or a model. Canned responses are keyed by *scenario*
so tests and offline development can drive every outcome deterministically:

- ``clipped_button``  -> a ``findings_detected`` result (viewport_clipping),
- ``healthy_page``    -> ``no_findings_detected``,
- ``malformed_text``  -> non-JSON text (shape failure),
- ``empty``           -> empty text (empty-output failure),
- ``timeout``         -> raises :class:`VisionTimeoutError`,
- ``provider_error``  -> raises :class:`VisionUnavailableError`.

Every result is labelled ``MOCK``. Generated evidence references point at the
screenshots actually supplied in the request, so validation stays meaningful.
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
from app.ai.validation import screenshot_ref

MOCK_LABEL = "MOCK"

SCENARIOS = (
    "clipped_button",
    "healthy_page",
    "malformed_text",
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
        if scenario == "empty":
            return ""
        if scenario == "healthy_page":
            return json.dumps(self._healthy_payload())
        return json.dumps(self._clipped_button_payload(request))

    @staticmethod
    def _ref(request: VisionRequest) -> str:
        if request.screenshots:
            return screenshot_ref(request.screenshots[0].artifact_id)
        return "screenshot:unknown"

    def _clipped_button_payload(self, request: VisionRequest) -> dict:
        ref = self._ref(request)
        return {
            "schema_version": "visual-analysis-result/v1",
            "analysis_outcome": "findings_detected",
            "summary": "MOCK: primary button is clipped at this viewport.",
            "findings": [
                {
                    "temporary_key": "mock-f1",
                    "category": "viewport_clipping",
                    "title": "Submit button clipped",
                    "description": "The primary button extends past the viewport.",
                    "severity": "high",
                    "confidence": 0.9,
                    "confidence_label": "high",
                    "observed_facts": [
                        {
                            "statement": "button box exceeds viewport width",
                            "evidence_refs": [ref],
                        }
                    ],
                    "interpretation": "Likely a fixed pixel width.",
                    "evidence_refs": [ref],
                    "affected_element_refs": ["#submit"],
                    "expected_behavior_source": None,
                    "limitations": ["MOCK output — not real model inference"],
                }
            ],
            "limitations": ["MOCK output — not real model inference"],
            "additional_evidence_needed": [],
        }

    @staticmethod
    def _healthy_payload() -> dict:
        return {
            "schema_version": "visual-analysis-result/v1",
            "analysis_outcome": "no_findings_detected",
            "summary": "MOCK: no visual defects detected.",
            "findings": [],
            "limitations": ["MOCK output — not real model inference"],
            "additional_evidence_needed": [],
        }
