"""Deterministic, offline MOCK VLM adapter.

Never touches the network or a model. Canned responses are keyed by *scenario*
so tests and offline development can drive every outcome deterministically:

- ``clipped_button``  -> a geometry-backed ``clipped_element`` when captured
  measurements show overflow; a clean result when measured elements fit,
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
import re
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
        viewport = request.page_context.viewport
        boxes: list[tuple[str, float, float, float, float]] = []
        for measurement in request.measurements:
            if not measurement.name.startswith("box.") or not isinstance(
                measurement.value, str
            ):
                continue
            match = re.fullmatch(
                r"x=(-?\d+(?:\.\d+)?),y=(-?\d+(?:\.\d+)?),"
                r"w=(\d+(?:\.\d+)?),h=(\d+(?:\.\d+)?)",
                measurement.value,
            )
            if match:
                x, y, width, height = (float(value) for value in match.groups())
                boxes.append((measurement.name[4:], x, y, width, height))

        clipped = [
            item
            for item in boxes
            if item[1] < 0
            or item[1] + item[3] > viewport.width
        ]
        if boxes and not clipped:
            return {
                "schema_version": 1,
                "outcome": "no_defects",
                "summary": "MOCK: measured candidate elements fit the viewport.",
                "findings": [],
            }

        selector, x, y, width, height = (
            clipped[0] if clipped else ("#submit", 10.0, 20.0, 220.0, 48.0)
        )
        right = x + width
        return {
            "schema_version": 1,
            "outcome": "defects_found",
            "summary": "MOCK: a measured candidate extends beyond the viewport.",
            "findings": [
                {
                    "category": "clipped_element",
                    "target_candidate": selector,
                    "observation": "The element extends past the captured viewport.",
                    "evidence": (
                        f"box x={x:g}, y={y:g}, w={width:g}, h={height:g}; "
                        f"viewport={viewport.width}x{viewport.height}; "
                        f"right edge={right:g}px. (MOCK measurement)"
                    ),
                    "potential_causes": ["fixed pixel width"],
                    "severity": "high",
                    "confidence": 0.9,
                    "region": {
                        "x": x,
                        "y": y,
                        "width": width,
                        "height": height,
                    },
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
