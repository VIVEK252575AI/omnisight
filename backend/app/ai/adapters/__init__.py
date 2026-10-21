"""VLM adapter implementations.

Import the interface and the factory from here::

    from app.ai.adapters import VisionAnalyzer, create_vision_analyzer
"""

from app.ai.adapters.base import ImageResolver, VisionAnalyzer
from app.ai.adapters.factory import (
    KNOWN_ADAPTERS,
    MOCK_ADAPTER,
    TEAMMATE_ADAPTER,
    create_vision_analyzer,
)
from app.ai.adapters.mock import MOCK_LABEL, MockVisionAnalyzer
from app.ai.adapters.patch import (
    GEMINI_PATCH_GENERATOR,
    KNOWN_PATCH_GENERATORS,
    MOCK_PATCH_GENERATOR,
    TEAMMATE_PATCH_GENERATOR,
    MockPatchGenerator,
    PatchGenerator,
    PatchGeneratorError,
    PatchGeneratorTimeoutError,
    create_patch_generator,
)
from app.ai.adapters.gemini_patch import GeminiPatchGenerator
from app.ai.adapters.teammate import TeammateVisionAnalyzer

__all__ = [
    "ImageResolver",
    "VisionAnalyzer",
    "MockVisionAnalyzer",
    "TeammateVisionAnalyzer",
    "create_vision_analyzer",
    "MOCK_LABEL",
    "MOCK_ADAPTER",
    "TEAMMATE_ADAPTER",
    "KNOWN_ADAPTERS",
    "PatchGenerator",
    "MockPatchGenerator",
    "GeminiPatchGenerator",
    "PatchGeneratorError",
    "PatchGeneratorTimeoutError",
    "create_patch_generator",
    "MOCK_PATCH_GENERATOR",
    "GEMINI_PATCH_GENERATOR",
    "TEAMMATE_PATCH_GENERATOR",
    "KNOWN_PATCH_GENERATORS",
]
