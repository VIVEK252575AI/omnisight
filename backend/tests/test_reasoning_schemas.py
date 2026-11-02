from app.reasoning_schemas import ReasoningResult


def test_reasoning_result_accepts_structured_review_only_output():
    result = ReasoningResult.model_validate({
        "scan_id": "scan-123",
        "summary": "Two visual findings need responsive layout review.",
        "priorities": [{
            "finding_ids": ["F-1"],
            "priority": "high",
            "score": 82,
            "rationale": "The issue blocks a primary action."
        }],
        "related_findings": [],
        "observed_facts": ["The supplied VLM finding reports horizontal overflow."],
        "root_cause_hypotheses": [{
            "finding_ids": ["F-1"],
            "hypothesis": "A fixed-width child may exceed its parent.",
            "confidence": 0.7,
            "evidence_refs": [],
            "source_inspection_required": True
        }],
        "unknowns": ["The exact CSS declaration is not available."],
        "repair_plan": ["Inspect width constraints at the affected viewport."],
        "verification_checks": ["Repeat the viewport test after a reviewed change."],
        "model": "gemini-3.6-flash"
    })
    assert result.priorities[0].priority == "high"
    assert result.root_cause_hypotheses[0].source_inspection_required is True


def test_reasoning_result_rejects_unknown_fields():
    payload = {
        "scan_id": "scan-123",
        "summary": "Summary",
        "model": "gemini-3.6-flash",
        "unexpected_instruction": "should not pass"
    }
    try:
        ReasoningResult.model_validate(payload)
    except Exception:
        pass
    else:
        raise AssertionError("Unknown model fields must be rejected")
