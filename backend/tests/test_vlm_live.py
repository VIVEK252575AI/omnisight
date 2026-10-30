"""Opt-in live Gemini test (skipped unless OMNISIGHT_LIVE_GEMINI=1 and key set)."""

from __future__ import annotations

import os
import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("OMNISIGHT_LIVE_GEMINI") != "1" or not os.getenv("OMNISIGHT_GEMINI_API_KEY"),
    reason="Set OMNISIGHT_LIVE_GEMINI=1 and OMNISIGHT_GEMINI_API_KEY to run.",
)


def test_live_gemini_one_screenshot():
    from pathlib import Path
    from uuid import uuid4
    from app.config import get_settings
    from app.ai.adapters import create_vision_analyzer
    from app.ai.contracts import (
        DeterministicMeasurement, FindingCategory, PageContext,
        ScreenEvidenceRef, VisionRequest,
    )
    from app.ai.gateway import AiGateway
    from app.schemas import Viewport

    img = Path("tests/fixtures/vision_audit/images/clipped-checkout.png")
    aid = uuid4()
    settings = get_settings()
    analyzer = create_vision_analyzer(settings, resolver=lambda rid: img if rid == aid else None)
    gw = AiGateway(settings, analyzer)
    req = VisionRequest(
        prompt_version="visual-analysis-v3",
        page_context=PageContext(route="/checkout", viewport=Viewport(width=390, height=844)),
        screenshots=[ScreenEvidenceRef(artifact_id=aid)],
        measurements=[DeterministicMeasurement(name="box.#place-order", value="x=37,y=483,w=520,h=48", unit="px")],
        allowed_categories=[FindingCategory.clipped_element],
        max_findings=5,
    )
    res = gw.analyze_visual(req)
    assert res.provider_label == "GEMINI"
    assert res.is_mock is False
