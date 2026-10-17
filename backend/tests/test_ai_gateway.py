"""Unit tests for the AI Gateway validation layer (Week 2).

The gateway sits on top of a ``VisionAnalyzer``; these tests use the offline
MOCK adapter or small injected callables. No network, no model SDK.
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from app.ai.adapters import MockVisionAnalyzer, TeammateVisionAnalyzer
from app.ai.contracts import (
    AnalysisOutcome,
    FindingCategory,
    Severity,
    VisualAnalysisResult,
)
from app.ai.errors import AiError, AiErrorCode
from app.ai.gateway import AiGateway
from app.config import Settings
from tests.test_vlm_adapter import ARTIFACT_ID, make_request


def gateway_with(analyzer, *, max_repair_attempts: int = 0) -> AiGateway:
    settings = Settings(ai_max_repair_attempts=max_repair_attempts, vlm_adapter="mock")
    return AiGateway(settings, analyzer)


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


def test_canonical_category_vocabulary_is_lld():
    assert {c.value for c in FindingCategory} == {
        "viewport_clipping",
        "element_overlap",
        "unexpected_hidden_control",
        "overflow_or_truncation",
        "responsive_misalignment",
        "readability_or_contrast",
        "unexpected_layout_shift",
        "content_misalignment",
        "other_visual_issue",
    }


def test_severity_vocabulary():
    assert {s.value for s in Severity} == {
        "critical",
        "high",
        "medium",
        "low",
        "informational",
    }


def test_unknown_category_is_rejected():
    payload = {
        "schema_version": "visual-analysis-result/v1",
        "analysis_outcome": "findings_detected",
        "summary": "x",
        "findings": [
            {
                "temporary_key": "f1",
                "category": "layout_overflow",  # 06-AI-PIPELINE name, not canonical
                "title": "t",
                "description": "d",
                "severity": "high",
                "confidence": 0.5,
                "observed_facts": [{"statement": "s", "evidence_refs": []}],
                "evidence_refs": ["screenshot:x"],
            }
        ],
    }
    with pytest.raises(Exception):
        VisualAnalysisResult.model_validate(payload)


# ---------------------------------------------------------------------------
# Gateway happy path
# ---------------------------------------------------------------------------


def test_gateway_parses_valid_mock_response():
    gateway = gateway_with(MockVisionAnalyzer("clipped_button"))
    result, meta = gateway.analyze_visual(make_request())
    assert result.analysis_outcome is AnalysisOutcome.findings_detected
    assert result.findings[0].category is FindingCategory.viewport_clipping
    assert meta.provider_label == "MOCK"
    assert meta.is_mock is True
    assert meta.attempts == 1


def test_gateway_propagates_mock_label_onto_metadata():
    gateway = gateway_with(MockVisionAnalyzer("healthy_page"))
    result, meta = gateway.analyze_visual(make_request())
    assert meta.is_mock is True
    assert meta.provider_label == "MOCK"
    assert result.analysis_outcome is AnalysisOutcome.no_findings_detected


def test_gateway_tolerates_code_fenced_json():
    ref = f"screenshot:{ARTIFACT_ID}"
    payload = {
        "schema_version": "visual-analysis-result/v1",
        "analysis_outcome": "findings_detected",
        "summary": "one",
        "findings": [
            {
                "temporary_key": "f1",
                "category": "viewport_clipping",
                "title": "t",
                "description": "d",
                "severity": "high",
                "confidence": 0.8,
                "observed_facts": [{"statement": "s", "evidence_refs": [ref]}],
                "evidence_refs": [ref],
            }
        ],
    }
    fenced = "```json\n" + json.dumps(payload) + "\n```"
    analyzer = TeammateVisionAnalyzer(lambda req, imgs: fenced)
    gateway = gateway_with(analyzer)
    result, _ = gateway.analyze_visual(make_request())
    assert result.findings[0].temporary_key == "f1"


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
    payload = {
        "schema_version": "visual-analysis-result/v1",
        "analysis_outcome": "no_findings_detected",
        "summary": "ok",
        # missing required key by dropping nothing valid -> use bad enum instead
        "findings": [{"temporary_key": "f1"}],
    }
    analyzer = TeammateVisionAnalyzer(lambda req, imgs: json.dumps(payload))
    gateway = gateway_with(analyzer, max_repair_attempts=0)
    with pytest.raises(AiError) as exc:
        gateway.analyze_visual(make_request())
    assert exc.value.code is AiErrorCode.AI_SCHEMA_VALIDATION_FAILED


def test_gateway_rejects_unknown_evidence_ref():
    ref = {"statement": "s", "evidence_refs": ["screenshot:does-not-exist"]}
    payload = {
        "schema_version": "visual-analysis-result/v1",
        "analysis_outcome": "findings_detected",
        "summary": "x",
        "findings": [
            {
                "temporary_key": "f1",
                "category": "viewport_clipping",
                "title": "t",
                "description": "d",
                "severity": "high",
                "confidence": 0.5,
                "observed_facts": [ref],
                "evidence_refs": ["screenshot:does-not-exist"],
            }
        ],
    }
    analyzer = TeammateVisionAnalyzer(lambda req, imgs: json.dumps(payload))
    gateway = gateway_with(analyzer, max_repair_attempts=0)
    with pytest.raises(AiError) as exc:
        gateway.analyze_visual(make_request())
    assert exc.value.code is AiErrorCode.AI_EVIDENCE_VALIDATION_FAILED


def test_gateway_rejects_disallowed_category():
    ref = f"screenshot:{ARTIFACT_ID}"
    payload = {
        "schema_version": "visual-analysis-result/v1",
        "analysis_outcome": "findings_detected",
        "summary": "x",
        "findings": [
            {
                "temporary_key": "f1",
                "category": "element_overlap",  # valid enum, but not allowed here
                "title": "t",
                "description": "d",
                "severity": "high",
                "confidence": 0.5,
                "observed_facts": [{"statement": "s", "evidence_refs": [ref]}],
                "evidence_refs": [ref],
            }
        ],
    }
    analyzer = TeammateVisionAnalyzer(lambda req, imgs: json.dumps(payload))
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
