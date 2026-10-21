from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_demo_pipeline


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
