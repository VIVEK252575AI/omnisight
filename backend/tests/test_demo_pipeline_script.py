from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_demo_pipeline
import capture_demo_checkout


def test_pipeline_preserves_safe_gemini_quota_reason(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        run_demo_pipeline,
        "run_detection",
        lambda **_kwargs: {
            "scan_id": "scan-demo",
            "status": "detection_failed",
            "adapter": "GEMINI",
            "is_mock": False,
            "findings": [],
            "reason_code": "rate_limited",
            "reason_message": "Gemini quota or rate limit was reached",
            "capture": {"screenshot_artifact_id": "artifact-demo"},
        },
    )

    result = run_demo_pipeline.run_pipeline(
        checkout_url="http://127.0.0.1:4173",
        api_url="http://127.0.0.1:8000",
        artifacts=tmp_path,
        expected="overflow",
        timeout_seconds=5,
        approve_sandbox=True,
    )

    assert result["status"] == "needs_inspection"
    assert result["detection_reason_code"] == "rate_limited"
    assert result["detection_reason_message"] == "Gemini quota or rate limit was reached"
    assert "proposal_attempts" not in result


def test_flow_viewport_parser_accepts_bounded_mobile_desktop_pair():
    viewports = run_demo_pipeline.parse_viewports(
        '[{"width":400,"height":844},{"width":1440,"height":900}]'
    )
    assert viewports == [
        {"width": 400, "height": 844}, {"width": 1440, "height": 900}
    ]


def test_flow_viewport_parser_rejects_extra_fields_and_unbounded_values():
    import pytest

    with pytest.raises(ValueError):
        run_demo_pipeline.parse_viewports(
            '[{"width":400,"height":844,"url":"https://attacker.example"},'
            '{"width":1440,"height":900}]'
        )
    with pytest.raises(ValueError):
        run_demo_pipeline.parse_viewports(
            '[{"width":2000,"height":844},{"width":1440,"height":900}]'
        )


def test_capture_cli_returns_failure_when_any_flow_step_failed(monkeypatch, capsys):
    monkeypatch.setattr(
        capture_demo_checkout,
        "capture",
        lambda *_args, **_kwargs: {
            "matches_expected": True,
            "flow_status": "failed",
            "flow_steps": [{"step_key": "required-check", "status": "failed"}],
        },
    )
    monkeypatch.setattr(sys, "argv", ["capture_demo_checkout"])

    assert capture_demo_checkout.main() == 2
    assert '"flow_status": "failed"' in capsys.readouterr().out
