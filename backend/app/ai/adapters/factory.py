"""Factory selecting a :class:`VisionAnalyzer` from configuration.

Selection is by ``OMNISIGHT_VLM_ADAPTER`` (``settings.vlm_adapter``). The
default is ``mock`` (deterministic, offline). An unknown value fails fast with a
clear :class:`VisionConfigurationError`, so a misconfigured deployment never
starts and silently fall back.

Callers depend only on the ``VisionAnalyzer`` interface, so switching the
adapter name changes the implementation without touching any caller.
"""

from __future__ import annotations

from app.ai.adapters.base import ImageResolver, VisionAnalyzer
from app.ai.adapters.gemini_vision import GeminiVisionAnalyzer
from app.ai.adapters.mock import MockVisionAnalyzer
from app.ai.adapters.teammate import TeammateCall, TeammateVisionAnalyzer
from app.ai.errors import VisionConfigurationError
from app.config import Settings

MOCK_ADAPTER = "mock"
TEAMMATE_ADAPTER = "teammate"
GEMINI_ADAPTER = "gemini"

KNOWN_ADAPTERS = (MOCK_ADAPTER, GEMINI_ADAPTER, TEAMMATE_ADAPTER)


def create_vision_analyzer(
    settings: Settings,
    *,
    resolver: ImageResolver | None = None,
    teammate_call: TeammateCall | None = None,
) -> VisionAnalyzer:
    """Build the configured :class:`VisionAnalyzer` or fail fast."""
    name = (settings.vlm_adapter or MOCK_ADAPTER).strip().lower()

    if name == MOCK_ADAPTER:
        return MockVisionAnalyzer(
            settings.vlm_mock_scenario,
            resolver=resolver,
            max_image_bytes=settings.vlm_max_image_bytes,
            max_images=settings.vlm_max_images_per_analysis,
        )

    if name == GEMINI_ADAPTER:
        return GeminiVisionAnalyzer(settings, resolver=resolver)

    if name == TEAMMATE_ADAPTER:
        if teammate_call is None:
            raise VisionConfigurationError(
                "VLM_ADAPTER='teammate' requires an injected teammate callable; "
                "the teammate VLM module is not integrated yet"
            )
        return TeammateVisionAnalyzer(
            teammate_call,
            resolver=resolver,
            provider_label="TEAMMATE",
            max_image_bytes=settings.vlm_max_image_bytes,
            max_images=settings.vlm_max_images_per_analysis,
            timeout_seconds=settings.vlm_timeout_seconds,
        )

    raise VisionConfigurationError(
        f"unknown VLM_ADAPTER '{name}'; known adapters: "
        f"{', '.join(KNOWN_ADAPTERS)}"
    )
