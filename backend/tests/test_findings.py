"""Tests for the structured visual-finding parser (Week 2, W2.3).

``parse_vlm_output`` is defensive: every case below yields a ``ParseResult``
(``ok`` or ``rejected`` with a reason code) and never raises.
"""

from __future__ import annotations

import json

from app.ai.contracts import (
    FindingCategory,
    FindingStatus,
    VlmOutcome,
)
from app.ai.parsing import ParseReason, parse_vlm_output


def finding(**overrides) -> dict:
    base = {
        "category": "clipped_element",
        "target_candidate": "#submit",
        "observation": "Button extends past the right viewport edge.",
        "evidence": "geometry: button.x=380 width=220 viewport.width=390",
        "potential_causes": ["fixed pixel width"],
        "severity": "high",
        "confidence": 0.9,
    }
    base.update(overrides)
    return base


def response(outcome="defects_found", findings=None, summary="summary") -> str:
    payload = {
        "schema_version": 1,
        "outcome": outcome,
        "summary": summary,
        "findings": findings if findings is not None else [finding()],
    }
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Valid outputs
# ---------------------------------------------------------------------------


def test_valid_defects_found_parses_into_findings():
    result = parse_vlm_output(response(), candidates=["#submit"])
    assert result.ok
    assert result.response.outcome is VlmOutcome.defects_found
    assert len(result.response.findings) == 1
    parsed = result.response.findings[0]
    assert parsed.category is FindingCategory.clipped_element
    assert parsed.observation.startswith("Button extends")
    assert parsed.evidence.startswith("geometry")
    assert parsed.potential_causes == ["fixed pixel width"]
    assert parsed.target_unverified is False
    assert parsed.status is FindingStatus.detected


def test_valid_no_defects_parses_empty_findings():
    result = parse_vlm_output(response(outcome="no_defects", findings=[]))
    assert result.ok
    assert result.response.outcome is VlmOutcome.no_defects
    assert result.response.findings == []


def test_needs_inspection_outcome_with_findings_is_ok():
    result = parse_vlm_output(response(outcome="needs_inspection"))
    assert result.ok
    assert result.response.outcome is VlmOutcome.needs_inspection
    assert len(result.response.findings) == 1


def test_region_is_parsed():
    payload = response(findings=[finding(region={"x": 1, "y": 2, "width": 3, "height": 4})])
    result = parse_vlm_output(payload)
    assert result.ok
    assert result.response.findings[0].region.width == 3


# ---------------------------------------------------------------------------
# Backend-assigned fields are never trusted from the model
# ---------------------------------------------------------------------------


def test_backend_assigns_finding_id_and_ignores_model_value():
    payload = response(findings=[finding(finding_id="model-supplied", status="fixed")])
    result = parse_vlm_output(payload)
    assert result.ok
    parsed = result.response.findings[0]
    assert parsed.finding_id != "model-supplied"
    assert parsed.status is FindingStatus.detected


def test_target_flagged_unverified_when_not_a_candidate():
    result = parse_vlm_output(response(), candidates=["#other"])
    assert result.ok
    assert result.response.findings[0].target_unverified is True


def test_null_target_is_not_flagged_unverified():
    payload = response(findings=[finding(target_candidate=None)])
    result = parse_vlm_output(payload, candidates=["#submit"])
    assert result.ok
    assert result.response.findings[0].target_unverified is False


# ---------------------------------------------------------------------------
# Rejections (each yields the specified reason code)
# ---------------------------------------------------------------------------


def test_empty_and_whitespace_are_rejected_as_empty():
    for raw in ("", "   ", "\n\t"):
        result = parse_vlm_output(raw)
        assert result.status == "rejected"
        assert result.reason is ParseReason.empty


def test_non_json_prose_is_rejected():
    result = parse_vlm_output("The button is probably clipped, I think.")
    assert result.reason is ParseReason.not_json


def test_truncated_json_is_rejected():
    result = parse_vlm_output('{"outcome": "defects_found", "findings": [{')
    assert result.reason is ParseReason.not_json


def test_json_array_is_rejected_as_schema_invalid():
    result = parse_vlm_output("[]")
    assert result.reason is ParseReason.schema_invalid


def test_unknown_category_is_rejected():
    payload = response(findings=[finding(category="layout_overflow")])
    result = parse_vlm_output(payload)
    assert result.reason is ParseReason.unknown_category


def test_missing_required_field_is_rejected():
    payload = response(findings=[{"category": "clipped_element"}])  # no observation
    result = parse_vlm_output(payload)
    assert result.reason is ParseReason.schema_invalid


def test_wrong_type_findings_is_rejected():
    payload = json.dumps(
        {"schema_version": 1, "outcome": "defects_found", "findings": "nope"}
    )
    result = parse_vlm_output(payload)
    assert result.reason is ParseReason.schema_invalid


def test_confidence_out_of_range_is_rejected():
    payload = response(findings=[finding(confidence=1.5)])
    result = parse_vlm_output(payload)
    assert result.reason is ParseReason.schema_invalid


def test_invalid_severity_is_rejected():
    payload = response(findings=[finding(severity="blocker")])
    result = parse_vlm_output(payload)
    assert result.reason is ParseReason.schema_invalid


def test_unknown_outcome_is_rejected():
    payload = json.dumps({"outcome": "maybe_broken", "findings": []})
    result = parse_vlm_output(payload)
    assert result.reason is ParseReason.schema_invalid


def test_contradictory_no_defects_with_findings_is_rejected():
    payload = response(outcome="no_defects")  # default has one finding
    result = parse_vlm_output(payload)
    assert result.reason is ParseReason.contradictory


def test_contradictory_defects_found_without_findings_is_rejected():
    payload = response(outcome="defects_found", findings=[])
    result = parse_vlm_output(payload)
    assert result.reason is ParseReason.contradictory


# ---------------------------------------------------------------------------
# Duplicates + tolerance
# ---------------------------------------------------------------------------


def test_duplicate_findings_are_merged():
    payload = response(
        findings=[
            finding(potential_causes=["fixed width"]),
            finding(potential_causes=["missing flex shrink"]),
        ]
    )
    result = parse_vlm_output(payload)
    assert result.ok
    assert len(result.response.findings) == 1
    assert set(result.response.findings[0].potential_causes) == {
        "fixed width",
        "missing flex shrink",
    }


def test_json_in_prose_single_tagged_block_is_accepted():
    text = "Here is the result:\n```json\n" + response() + "\n```\nHope that helps."
    result = parse_vlm_output(text)
    assert result.ok
    assert result.response.outcome is VlmOutcome.defects_found


def test_control_characters_stripped_and_text_capped():
    payload = response(
        findings=[finding(observation="bad\x00\x07 text" + "x" * 5000)]
    )
    result = parse_vlm_output(payload)
    assert result.ok
    observation = result.response.findings[0].observation
    assert "\x00" not in observation and "\x07" not in observation
    assert len(observation) <= 2000


def test_never_raises_on_non_string_input():
    for raw in (None, 123, {"outcome": "no_defects"}, [1, 2, 3]):
        result = parse_vlm_output(raw)
        assert result.status == "rejected"
