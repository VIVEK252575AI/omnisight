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
]
