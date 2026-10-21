"""Unit tests for the AI Gateway validation layer (Week 2, W2.3 schema).

The gateway sits on top of a ``VisionAnalyzer``; these tests use the offline
MOCK adapter or small injected callables. No network, no model SDK.
"""

from __future__ import annotations

import json

import pytest

from app.ai.adapters import MockVisionAnalyzer, TeammateVisionAnalyzer
from app.ai.contracts import (
    FindingCategory,
    Severity,
    VlmOutcome,
)
from app.ai.errors import AiError, AiErrorCode
from app.ai.gateway import AiGateway
from app.config import Settings
from tests.test_vlm_adapter import make_request


def gateway_with(analyzer, *, max_repair_attempts: int = 0) -> AiGateway:
    settings = Settings(ai_max_repair_attempts=max_repair_attempts, vlm_adapter="mock")
    return AiGateway(settings, analyzer)


def finding_payload(**overrides) -> dict:
    base = {
        "category": "clipped_element",
        "target_candidate": "#submit",
        "observation": "button extends past the viewport",
        "evidence": "geometry: x=380 w=220",
        "potential_causes": ["fixed pixel width"],
        "severity": "high",
        "confidence": 0.8,
    }
    base.update(overrides)
    return base


def response_payload(outcome="defects_found", findings=None, **overrides) -> dict:
    payload = {
        "schema_version": 1,
        "outcome": outcome,
        "summary": "mock summary",
        "findings": [finding_payload()] if findings is None else findings,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


def test_canonical_category_vocabulary_is_w2_3():
    assert {c.value for c in FindingCategory} == {
        "clipped_element",
        "overlapping_elements",
        "low_contrast_text",
        "responsive_layout_failure",
        "hidden_or_unreachable_element",
        "needs_inspection",
    }


def test_severity_vocabulary():
    assert {s.value for s in Severity} == {
        "critical",
        "high",
        "medium",
        "low",
        "informational",
    }


def test_outcome_vocabulary():
    assert {o.value for o in VlmOutcome} == {
        "defects_found",
        "no_defects",
        "needs_inspection",
    }


# ---------------------------------------------------------------------------
# Gateway happy path
# ---------------------------------------------------------------------------


def test_gateway_parses_valid_mock_response():
    gateway = gateway_with(MockVisionAnalyzer("clipped_button"))
    result, meta = gateway.analyze_visual(make_request(), candidates=["#submit"])
    assert result.outcome is VlmOutcome.defects_found
    assert result.findings[0].category is FindingCategory.clipped_element
    assert result.findings[0].target_unverified is False
    assert meta.provider_label == "MOCK"
    assert meta.is_mock is True
    assert meta.attempts == 1


def test_gateway_healthy_page_is_no_defects():
    gateway = gateway_with(MockVisionAnalyzer("healthy_page"))
    result, meta = gateway.analyze_visual(make_request())
    assert meta.is_mock is True
    assert meta.provider_label == "MOCK"
    assert result.outcome is VlmOutcome.no_defects
    assert result.findings == []


def test_gateway_flags_unverified_target_when_not_a_candidate():
    gateway = gateway_with(MockVisionAnalyzer("clipped_button"))
    result, _ = gateway.analyze_visual(make_request(), candidates=["#other"])
    assert result.findings[0].target_unverified is True


def test_gateway_tolerates_code_fenced_json():
    fenced = "```json\n" + json.dumps(response_payload()) + "\n```"
    analyzer = TeammateVisionAnalyzer(lambda req, imgs: fenced)
    gateway = gateway_with(analyzer)
    result, _ = gateway.analyze_visual(make_request())
    assert result.findings[0].target_candidate == "#submit"


# ---------------------------------------------------------------------------
# Gateway failure + repair paths
# ---------------------------------------------------------------------------


def test_gateway_repairs_malformed_then_valid():
    analyzer = MockVisionAnalyzer(["malformed_text", "clipped_button"])
    gateway = gateway_with(analyzer, max_repair_attempts=1)
    result, meta = gateway.analyze_visual(make_request())
    assert meta.attempts == 2
    assert any("repair" in w for w in meta.warnings)
    assert len(analyzer.calls) == 2
    assert result.outcome is VlmOutcome.defects_found


def test_gateway_no_repair_when_disabled():
    gateway = gateway_with(MockVisionAnalyzer("malformed_text"), max_repair_attempts=0)
    with pytest.raises(AiError) as exc:
        gateway.analyze_visual(make_request())
    assert exc.value.code is AiErrorCode.AI_RESPONSE_NOT_JSON


def test_gateway_empty_output_raises():
    gateway = gateway_with(MockVisionAnalyzer("empty"), max_repair_attempts=0)
    with pytest.raises(AiError) as exc:
        gateway.analyze_visual(make_request())
    assert exc.value.code is AiErrorCode.AI_RESPONSE_EMPTY


def test_gateway_schema_failure_raises():
    analyzer = TeammateVisionAnalyzer(
        lambda req, imgs: json.dumps(response_payload(findings=[{}]))
    )
    gateway = gateway_with(analyzer, max_repair_attempts=0)
    with pytest.raises(AiError) as exc:
        gateway.analyze_visual(make_request())
    assert exc.value.code is AiErrorCode.AI_SCHEMA_VALIDATION_FAILED


def test_gateway_unknown_category_raises():
    analyzer = TeammateVisionAnalyzer(
        lambda req, imgs: json.dumps(
            response_payload(findings=[finding_payload(category="layout_overflow")])
        )
    )
    gateway = gateway_with(analyzer, max_repair_attempts=0)
    with pytest.raises(AiError) as exc:
        gateway.analyze_visual(make_request())
    assert exc.value.code is AiErrorCode.AI_SCHEMA_VALIDATION_FAILED


def test_gateway_rejects_contradictory_outcome():
    analyzer = TeammateVisionAnalyzer(
        lambda req, imgs: json.dumps(response_payload(outcome="no_defects"))
    )
    gateway = gateway_with(analyzer, max_repair_attempts=0)
    with pytest.raises(AiError) as exc:
        gateway.analyze_visual(make_request())
    assert exc.value.code is AiErrorCode.AI_EVIDENCE_VALIDATION_FAILED


def test_gateway_provider_error_propagates_typed():
    gateway = gateway_with(MockVisionAnalyzer("provider_error"))
    with pytest.raises(AiError) as exc:
        gateway.analyze_visual(make_request())
    assert exc.value.code is AiErrorCode.AI_PROVIDER_UNAVAILABLE


def test_gateway_timeout_propagates_typed():
    gateway = gateway_with(MockVisionAnalyzer("timeout"))
    with pytest.raises(AiError) as exc:
        gateway.analyze_visual(make_request())
    assert exc.value.code is AiErrorCode.AI_TIMEOUT
