"""Unit tests for the provider-neutral AI Gateway boundary (Week 2).

All tests use the deterministic offline ``FakeModelProvider`` — no network, no
model SDK, no paid API.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from app.ai.contracts import (
    AiCapability,
    AnalysisOutcome,
    FindingCategory,
    FindingCandidate,
    PageContext,
    PatchConstraints,
    PatchGenerationRequest,
    RelevantSourceFile,
    ScreenEvidenceRef,
    Severity,
    VisualAnalysisRequest,
    VisualAnalysisResult,
)
from app.ai.errors import AiError, AiErrorCode
from app.ai.gateway import AiGateway
from app.ai.prompts import (
    PATCH_GENERATION_PROMPT_VERSION,
    VISUAL_ANALYSIS_PROMPT_VERSION,
)
from app.ai.providers.fake import FakeModelProvider
from app.ai.validation import screenshot_ref
from app.schemas import Viewport

ARTIFACT_ID = UUID("11111111-1111-1111-1111-111111111111")


def make_visual_request(
    artifact_id: UUID = ARTIFACT_ID, **overrides
) -> VisualAnalysisRequest:
    data = {
        "prompt_version": VISUAL_ANALYSIS_PROMPT_VERSION,
        "page_context": PageContext(route="/checkout", viewport=Viewport(width=390, height=844)),
        "screenshots": [
            ScreenEvidenceRef(artifact_id=artifact_id, label="dominant"),
        ],
        "allowed_categories": [
            FindingCategory.viewport_clipping,
            FindingCategory.other_visual_issue,
        ],
        "max_findings": 5,
    }
    data.update(overrides)
    return VisualAnalysisRequest(**data)


def visual_payload(artifact_id: UUID = ARTIFACT_ID, **overrides) -> dict:
    ref = screenshot_ref(artifact_id)
    payload = {
        "schema_version": "visual-analysis-result/v1",
        "analysis_outcome": "findings_detected",
        "summary": "One clipped control.",
        "findings": [
            {
                "temporary_key": "f1",
                "category": "viewport_clipping",
                "title": "CTA clipped at 390px",
                "description": "Primary button extends past the right edge.",
                "severity": "high",
                "confidence": 0.8,
                "confidence_label": "high",
                "observed_facts": [
                    {"statement": "button box exceeds viewport width", "evidence_refs": [ref]}
                ],
                "interpretation": "Likely a fixed pixel width.",
                "evidence_refs": [ref],
                "affected_element_refs": ["#checkout-cta"],
                "expected_behavior_source": None,
                "limitations": [],
            }
        ],
        "limitations": [],
        "additional_evidence_needed": [],
    }
    payload.update(overrides)
    return payload


def fake_gateway(
    responses,
    *,
    ai_mode: str = "fake",
    max_repair_attempts: int = 1,
    timeout_seconds: float = 45.0,
    delay_seconds: float = 0.0,
    resolve_artifact=None,
) -> tuple[AiGateway, FakeModelProvider]:
    from app.config import Settings

    provider = FakeModelProvider(responses=responses, delay_seconds=delay_seconds)
    settings = Settings(
        ai_mode=ai_mode,
        ai_provider_timeout_seconds=timeout_seconds,
        ai_max_repair_attempts=max_repair_attempts,
    )
    return AiGateway(settings, provider, resolve_artifact=resolve_artifact), provider


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


def test_valid_result_constructs():
    result = VisualAnalysisResult.model_validate(visual_payload())
    assert result.analysis_outcome is AnalysisOutcome.findings_detected
    assert len(result.findings) == 1
    assert isinstance(result.findings[0], FindingCandidate)


def test_unknown_category_is_rejected():
    payload = visual_payload()
    payload["findings"][0]["category"] = "layout_overflow"  # 06-AI-PIPELINE name
    with pytest.raises(Exception):
        VisualAnalysisResult.model_validate(payload)


def test_confidence_out_of_range_is_rejected():
    payload = visual_payload()
    payload["findings"][0]["confidence"] = 1.5
    with pytest.raises(Exception):
        VisualAnalysisResult.model_validate(payload)


def test_finding_requires_observed_facts():
    payload = visual_payload()
    payload["findings"][0]["observed_facts"] = []
    with pytest.raises(Exception):
        VisualAnalysisResult.model_validate(payload)


# ---------------------------------------------------------------------------
# Gateway — happy path
# ---------------------------------------------------------------------------


def test_gateway_parses_valid_response():
    gateway, provider = fake_gateway(
        {AiCapability.visual_analysis: [FakeModelProvider.json_text(visual_payload())]}
    )
    result, meta = gateway.analyze_visual(make_visual_request())
    assert result.findings[0].category is FindingCategory.viewport_clipping
    assert meta.is_mock is True
    assert meta.attempts == 1
    assert meta.provider_id == "fake"
    assert len(provider.calls) == 1


def test_gateway_tolerates_code_fenced_json():
    fenced = "```json\n" + json.dumps(visual_payload()) + "\n```"
    gateway, _ = fake_gateway({AiCapability.visual_analysis: [fenced]})
    result, _ = gateway.analyze_visual(make_visual_request())
    assert result.findings[0].temporary_key == "f1"


def test_gateway_resolves_artifact_ids_to_paths(tmp_path):
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    gateway, provider = fake_gateway(
        {AiCapability.visual_analysis: [FakeModelProvider.json_text(visual_payload())]},
        resolve_artifact=lambda _aid: png,
    )
    gateway.analyze_visual(make_visual_request())
    assert provider.calls[0]["image_count"] == 1


# ---------------------------------------------------------------------------
# Gateway — failure + repair paths
# ---------------------------------------------------------------------------


def test_gateway_repairs_bad_json_once():
    gateway, provider = fake_gateway(
        {
            AiCapability.visual_analysis: [
                "this is not json",
                FakeModelProvider.json_text(visual_payload()),
            ]
        },
        max_repair_attempts=1,
    )
    result, meta = gateway.analyze_visual(make_visual_request())
    assert meta.attempts == 2
    assert any("repair" in w for w in meta.warnings)
    assert len(provider.calls) == 2


def test_gateway_no_repair_when_disabled():
    gateway, _ = fake_gateway(
        {AiCapability.visual_analysis: ["not json"]}, max_repair_attempts=0
    )
    with pytest.raises(AiError) as exc:
        gateway.analyze_visual(make_visual_request())
    assert exc.value.code is AiErrorCode.AI_RESPONSE_NOT_JSON


def test_gateway_empty_output_raises():
    gateway, _ = fake_gateway(
        {AiCapability.visual_analysis: [""]}, max_repair_attempts=0
    )
    with pytest.raises(AiError) as exc:
        gateway.analyze_visual(make_visual_request())
    assert exc.value.code is AiErrorCode.AI_RESPONSE_EMPTY


def test_gateway_schema_failure_raises():
    bad = visual_payload()
    del bad["findings"][0]["severity"]
    gateway, _ = fake_gateway(
        {AiCapability.visual_analysis: [json.dumps(bad)]}, max_repair_attempts=0
    )
    with pytest.raises(AiError) as exc:
        gateway.analyze_visual(make_visual_request())
    assert exc.value.code is AiErrorCode.AI_SCHEMA_VALIDATION_FAILED


def test_gateway_rejects_unknown_evidence_ref():
    payload = visual_payload()
    payload["findings"][0]["evidence_refs"] = ["screenshot:does-not-exist"]
    payload["findings"][0]["observed_facts"][0]["evidence_refs"] = [
        "screenshot:does-not-exist"
    ]
    gateway, _ = fake_gateway(
        {AiCapability.visual_analysis: [json.dumps(payload)]}, max_repair_attempts=0
    )
    with pytest.raises(AiError) as exc:
        gateway.analyze_visual(make_visual_request())
    assert exc.value.code is AiErrorCode.AI_EVIDENCE_VALIDATION_FAILED


def test_gateway_rejects_disallowed_category():
    payload = visual_payload()
    payload["findings"][0]["category"] = "element_overlap"  # valid, but not allowed here
    gateway, _ = fake_gateway(
        {AiCapability.visual_analysis: [json.dumps(payload)]}, max_repair_attempts=0
    )
    with pytest.raises(AiError) as exc:
        gateway.analyze_visual(make_visual_request())
    assert exc.value.code is AiErrorCode.AI_EVIDENCE_VALIDATION_FAILED


def test_gateway_disabled_mode_raises():
    gateway, _ = fake_gateway(
        {AiCapability.visual_analysis: [FakeModelProvider.json_text(visual_payload())]},
        ai_mode="disabled",
    )
    with pytest.raises(AiError) as exc:
        gateway.analyze_visual(make_visual_request())
    assert exc.value.code is AiErrorCode.AI_CONFIGURATION_ERROR


def test_gateway_unsupported_capability_raises():
    gateway, _ = fake_gateway(
        {AiCapability.visual_analysis: [FakeModelProvider.json_text(visual_payload())]}
    )
    request = PatchGenerationRequest(
        prompt_version=PATCH_GENERATION_PROMPT_VERSION,
        base_revision="abc123",
        finding_summary="CTA clipped",
    )
    with pytest.raises(AiError) as exc:
        gateway.generate_patch(request)
    assert exc.value.code is AiErrorCode.AI_CAPABILITY_UNSUPPORTED


def test_gateway_provider_unavailable_raises():
    gateway, _ = fake_gateway({AiCapability.visual_analysis: []})
    with pytest.raises(AiError) as exc:
        gateway.analyze_visual(make_visual_request())
    assert exc.value.code is AiErrorCode.AI_PROVIDER_UNAVAILABLE


def test_gateway_timeout_raises():
    gateway, _ = fake_gateway(
        {AiCapability.visual_analysis: [FakeModelProvider.json_text(visual_payload())]},
        timeout_seconds=0.05,
        delay_seconds=0.5,
    )
    with pytest.raises(AiError) as exc:
        gateway.analyze_visual(make_visual_request())
    assert exc.value.code is AiErrorCode.AI_TIMEOUT


# ---------------------------------------------------------------------------
# Patch generation
# ---------------------------------------------------------------------------


def make_patch_request(**overrides) -> PatchGenerationRequest:
    data = {
        "prompt_version": PATCH_GENERATION_PROMPT_VERSION,
        "base_revision": "abc123",
        "finding_summary": "CTA clipped at 390px",
        "relevant_files": [
            RelevantSourceFile(path="src/Checkout.css", content=".cta { width: 540px; }")
        ],
        "constraints": PatchConstraints(),
    }
    data.update(overrides)
    return PatchGenerationRequest(**data)


def patch_payload(**overrides) -> dict:
    payload = {
        "schema_version": "patch-proposal/v1",
        "proposal_outcome": "patch_proposed",
        "summary": "Make the CTA fluid.",
        "rationale": [".cta had a fixed 540px width."],
        "base_revision": "abc123",
        "unified_diff": "--- a/src/Checkout.css\n+++ b/src/Checkout.css\n@@\n-.cta { width: 540px; }\n+.cta { width: 100%; max-width: 540px; }\n",
        "changed_paths": ["src/Checkout.css"],
        "risk_notes": [],
        "validation_suggestions": ["re-run visual check at 390px"],
        "assumptions": [],
        "limitations": [],
    }
    payload.update(overrides)
    return payload


def test_patch_generation_happy_path():
    gateway, _ = fake_gateway(
        {AiCapability.patch_generation: [json.dumps(patch_payload())]}
    )
    proposal, meta = gateway.generate_patch(make_patch_request())
    assert proposal.proposal_outcome.value == "patch_proposed"
    assert proposal.changed_paths == ["src/Checkout.css"]
    assert meta.capability is AiCapability.patch_generation


def test_patch_rejects_unprovided_file():
    payload = patch_payload(changed_paths=["src/Secret.tsx"])
    gateway, _ = fake_gateway(
        {AiCapability.patch_generation: [json.dumps(payload)]}, max_repair_attempts=0
    )
    with pytest.raises(AiError) as exc:
        gateway.generate_patch(make_patch_request())
    assert exc.value.code is AiErrorCode.AI_EVIDENCE_VALIDATION_FAILED


def test_patch_requires_matching_base_revision():
    payload = patch_payload(base_revision="deadbeef")
    gateway, _ = fake_gateway(
        {AiCapability.patch_generation: [json.dumps(payload)]}, max_repair_attempts=0
    )
    with pytest.raises(AiError) as exc:
        gateway.generate_patch(make_patch_request())
    assert exc.value.code is AiErrorCode.AI_EVIDENCE_VALIDATION_FAILED


def test_patch_rejects_disallowed_extension():
    request = make_patch_request(
        relevant_files=[RelevantSourceFile(path="src/App.exe", content="x")]
    )
    payload = patch_payload(changed_paths=["src/App.exe"])
    gateway, _ = fake_gateway(
        {AiCapability.patch_generation: [json.dumps(payload)]}, max_repair_attempts=0
    )
    with pytest.raises(AiError) as exc:
        gateway.generate_patch(request)
    assert exc.value.code is AiErrorCode.AI_EVIDENCE_VALIDATION_FAILED
