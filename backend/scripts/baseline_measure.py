"""Baseline measurement harness (W4.1). Runs N times over a case with the mock adapter."""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from uuid import uuid4

from app.ai.adapters import create_vision_analyzer
from app.ai.contracts import (
    DeterministicMeasurement, FindingCategory, PageContext,
    ScreenEvidenceRef, VisionRequest,
)
from app.ai.gateway import AiGateway
from app.config import Settings
from app.schemas import Viewport

CASES = [
    ("clipped-checkout", "tests/fixtures/vision_audit/images/clipped-checkout.png", 390, 844),
]
N = 5


def main():
    settings = Settings(vlm_adapter="mock", vlm_mock_scenario="clipped_button")
    runs = []
    for case_id, img, w, h in CASES:
        aid = uuid4()
        img_path = Path(img)
        image_bytes = img_path.read_bytes()
        analyzer = create_vision_analyzer(settings, resolver=lambda rid: img_path if rid == aid else None)
        gw = AiGateway(settings, analyzer)
        for i in range(N):
            req = VisionRequest(
                prompt_version="visual-analysis-v3",
                page_context=PageContext(route="/checkout", viewport=Viewport(width=w, height=h)),
                screenshots=[ScreenEvidenceRef(artifact_id=aid)],
                measurements=[DeterministicMeasurement(name="box.#place-order", value="x=37,y=483,w=520,h=48", unit="px")],
                allowed_categories=[FindingCategory.clipped_element],
                max_findings=5,
            )
            t0 = time.perf_counter()
            res = gw.run(req)
            dt = (time.perf_counter() - t0) * 1000
            runs.append({
                "case_id": case_id,
                "run": i + 1,
                "image_count": 1,
                "image_bytes": len(image_bytes),
                "image_dimensions": {"width": w, "height": h},
                "latency_ms": round(dt, 2),
                "usage": None,
                "adapter_label": res.provider_label,
                "mode": "full_screenshot",
                "parse_status": "ok" if res.ok else "failed",
                "findings_count": len(res.response.findings) if (res.response and res.response.findings) else 0,
            })
    out = Path("evaluation/week4/baseline_results.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"environment": {"adapter": "mock", "N": N}, "runs": runs}, indent=2))
    # summary
    latencies = [r["latency_ms"] for r in runs]
    summary = {
        "count": len(runs),
        "latency_ms_min": min(latencies),
        "latency_ms_median": statistics.median(latencies),
        "latency_ms_max": max(latencies),
        "image_bytes": runs[0]["image_bytes"],
    }
    Path("evaluation/week4/baseline_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
