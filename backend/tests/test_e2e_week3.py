"""Week 3 E2E smoke (skipped unless OMNISIGHT_E2E=1)."""

from __future__ import annotations

import os
import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("OMNISIGHT_E2E") != "1",
    reason="Set OMNISIGHT_E2E=1 to run the Week 3 end-to-end smoke.",
)


def test_week3_e2e_placeholder():
    # Real E2E requires Playwright, Docker, Gemini and an authorized test repo.
    assert True