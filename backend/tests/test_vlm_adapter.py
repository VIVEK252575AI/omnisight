"""Tests for the replaceable VLM adapter boundary (Week 2, W2.2).

Everything here runs offline against the deterministic MOCK adapter or injected
callables — no network, no model SDK, no paid API.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from app.ai.adapters import (
    MockVisionAnalyzer,
    TeammateVisionAnalyzer,
    VisionAnalyzer,
    create_vision_analyzer,
)
from app.ai.contracts import (
    DeterministicMeasurement,
    FindingCategory,
    PageContext,
    RawVisionResult,
    ScreenEvidenceRef,
    VisionRequest,
)
from app.ai.errors import (
    AiErrorCode,
    VisionConfigurationError,
    VisionInvalidResponseError,
    VisionPayloadTooLargeError,
    VisionTimeoutError,
    VisionUnavailableError,
)
from app.ai.prompts import VISUAL_ANALYSIS_PROMPT_VERSION
from app.config import Settings
from app.schemas import Viewport

ARTIFACT_ID = UUID("11111111-1111-1111-1111-111111111111")


def make_request(artifact_id: UUID = ARTIFACT_ID) -> VisionRequest:
    return VisionRequest(
        prompt_version=VISUAL_ANALYSIS_PROMPT_VERSION,
        page_context=PageContext(route="/checkout", viewport=Viewport(width=390, height=844)),
        screenshots=[ScreenEvidenceRef(artifact_id=artifact_id, label="dominant")],
        allowed_categories=[
            FindingCategory.clipped_element,
            FindingCategory.needs_inspection,
        ],
        max_findings=5,
    )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_factory_defaults_to_mock():
    analyzer = create_vision_analyzer(Settings())
    assert isinstance(analyzer, MockVisionAnalyzer)
    assert analyzer.is_mock is True
    assert analyzer.provider_label == "MOCK"


def test_factory_unknown_adapter_fails_fast():
    with pytest.raises(VisionConfigurationError) as exc:
        create_vision_analyzer(Settings(vlm_adapter="does-not-exist"))
    assert exc.value.code is AiErrorCode.AI_CONFIGURATION_ERROR
    assert "unknown VLM_ADAPTER" in exc.value.message


def test_factory_teammate_requires_callable():
    with pytest.raises(VisionConfigurationError):
        create_vision_analyzer(Settings(vlm_adapter="teammate"))


def test_factory_teammate_with_callable_selected():
    analyzer = create_vision_analyzer(
        Settings(vlm_adapter="teammate"), teammate_call=lambda req, imgs: "{}"
    )
    assert isinstance(analyzer, TeammateVisionAnalyzer)


def test_switching_adapter_name_changes_implementation():
    mock = create_vision_analyzer(Settings(vlm_adapter="mock"))
    teammate = create_vision_analyzer(
        Settings(vlm_adapter="teammate"), teammate_call=lambda req, imgs: "{}"
    )
    assert type(mock) is not type(teammate)
    # both satisfy the same interface
    assert isinstance(mock, VisionAnalyzer)
    assert isinstance(teammate, VisionAnalyzer)


# ---------------------------------------------------------------------------
# Mock scenarios + MOCK labelling
# ---------------------------------------------------------------------------


def test_mock_clipped_button_scenario():
    raw = MockVisionAnalyzer("clipped_button").analyze(make_request())
    assert isinstance(raw, RawVisionResult)
    assert raw.provider_label == "MOCK"
    payload = json.loads(raw.text)
    assert payload["outcome"] == "defects_found"
    assert payload["findings"][0]["category"] == "clipped_element"


def test_mock_clipped_button_uses_candidate_geometry_and_respects_a_fix():
    analyzer = MockVisionAnalyzer("clipped_button")
    request = make_request().model_copy(
        update={
            "measurements": [
                DeterministicMeasurement(
                    name="box.#place-order",
                    value="x=37,y=483,w=520,h=48",
                    unit="px",
                ),
                DeterministicMeasurement(
                    name="box.#checkout-summary",
                    value="x=16,y=568,w=358,h=278",
                    unit="px",
                ),
            ]
        }
    )

    broken = json.loads(analyzer.analyze(request).text)
    assert broken["outcome"] == "defects_found"
    assert broken["findings"][0]["target_candidate"] == "#place-order"
    assert "right edge=557px" in broken["findings"][0]["evidence"]

    fixed_request = request.model_copy(
        update={
            "measurements": [
                DeterministicMeasurement(
                    name="box.#place-order",
                    value="x=37,y=483,w=318,h=48",
                    unit="px",
                ),
                request.measurements[1],
            ]
        }
    )
    fixed = json.loads(analyzer.analyze(fixed_request).text)
    assert fixed["outcome"] == "no_defects"
    assert fixed["findings"] == []


def test_mock_healthy_page_scenario():
    raw = MockVisionAnalyzer("healthy_page").analyze(make_request())
    payload = json.loads(raw.text)
    assert payload["outcome"] == "no_defects"
    assert payload["findings"] == []


def test_mock_malformed_text_scenario():
    raw = MockVisionAnalyzer("malformed_text").analyze(make_request())
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw.text)


def test_mock_empty_scenario():
    raw = MockVisionAnalyzer("empty").analyze(make_request())
    assert raw.text == ""


def test_mock_timeout_scenario_raises_typed_error():
    with pytest.raises(VisionTimeoutError) as exc:
        MockVisionAnalyzer("timeout").analyze(make_request())
    assert exc.value.code is AiErrorCode.AI_TIMEOUT


def test_mock_provider_error_scenario_raises_typed_error():
    with pytest.raises(VisionUnavailableError) as exc:
        MockVisionAnalyzer("provider_error").analyze(make_request())
    assert exc.value.code is AiErrorCode.AI_PROVIDER_UNAVAILABLE


def test_mock_unknown_scenario_fails_fast():
    with pytest.raises(VisionConfigurationError):
        MockVisionAnalyzer("not-a-scenario")


def test_mock_labels_every_result_mock():
    for scenario in ("clipped_button", "healthy_page", "malformed_text", "empty"):
        raw = MockVisionAnalyzer(scenario).analyze(make_request())
        assert raw.provider_label == "MOCK"


def test_mock_scenario_sequence_advances():
    analyzer = MockVisionAnalyzer(["malformed_text", "clipped_button"])
    first = analyzer.analyze(make_request())
    second = analyzer.analyze(make_request())
    with pytest.raises(json.JSONDecodeError):
        json.loads(first.text)
    assert json.loads(second.text)["outcome"] == "defects_found"


# ---------------------------------------------------------------------------
# Image budget (rejected before the call)
# ---------------------------------------------------------------------------


def test_oversized_image_rejected_before_call(tmp_path: Path):
    big = tmp_path / "big.png"
    big.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096)
    analyzer = MockVisionAnalyzer(
        "clipped_button", resolver=lambda _aid: big, max_image_bytes=1024
    )
    with pytest.raises(VisionPayloadTooLargeError) as exc:
        analyzer.analyze(make_request())
    assert exc.value.code is AiErrorCode.AI_BUDGET_EXCEEDED
    assert analyzer.calls == []  # rejected before the model was invoked


def test_too_many_images_rejected(tmp_path: Path):
    small = tmp_path / "small.png"
    small.write_bytes(b"\x89PNG\r\n\x1a\n")
    analyzer = MockVisionAnalyzer(
        "clipped_button", resolver=lambda _aid: small, max_images=0
    )
    with pytest.raises(VisionPayloadTooLargeError):
        analyzer.analyze(make_request())


# ---------------------------------------------------------------------------
# Teammate wrapper (tested with an injected fake callable)
# ---------------------------------------------------------------------------


def test_teammate_wrapper_delegates_and_labels():
    calls: list[tuple] = []

    def fake_call(request: VisionRequest, image_paths: list[Path]) -> str:
        calls.append((request.request_id, len(image_paths)))
        return json.dumps({"outcome": "no_defects", "findings": [], "summary": ""})

    analyzer = TeammateVisionAnalyzer(fake_call, provider_label="TEAMMATE")
    raw = analyzer.analyze(make_request())
    assert raw.provider_label == "TEAMMATE"
    assert json.loads(raw.text)["outcome"] == "no_defects"
    assert len(calls) == 1


def test_teammate_wrapper_maps_exceptions():
    def boom(request, image_paths):
        raise RuntimeError("teammate crashed")

    with pytest.raises(VisionUnavailableError):
        TeammateVisionAnalyzer(boom).analyze(make_request())


def test_teammate_wrapper_empty_output_is_invalid():
    with pytest.raises(VisionInvalidResponseError):
        TeammateVisionAnalyzer(lambda req, imgs: "   ").analyze(make_request())


def test_teammate_wrapper_timeout():
    import time

    def slow(request, image_paths):
        time.sleep(0.5)
        return "{}"

    with pytest.raises(VisionTimeoutError):
        TeammateVisionAnalyzer(slow, timeout_seconds=0.05).analyze(make_request())
