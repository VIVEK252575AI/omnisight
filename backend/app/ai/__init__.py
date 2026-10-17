"""Provider-neutral VLM adapter boundary (Week 2).

Public surface:

- :class:`~app.ai.adapters.base.VisionAnalyzer` — the replaceable interface.
- :func:`~app.ai.adapters.factory.create_vision_analyzer` — config-selected impl.
- :class:`~app.ai.adapters.mock.MockVisionAnalyzer` — labelled ``MOCK`` impl.
- :class:`~app.ai.gateway.AiGateway` — parse + validate raw output.
- :class:`~app.ai.contracts.VisionRequest` / ``RawVisionResult`` — boundary models.
- :class:`~app.ai.errors.AiError` and typed ``Vision*`` errors.

Model output is always treated as untrusted text and parsed into the validated
``contracts`` models before any other module may consume it.
"""

from app.ai.adapters import (
    MockVisionAnalyzer,
    TeammateVisionAnalyzer,
    VisionAnalyzer,
    create_vision_analyzer,
)
from app.ai.errors import AiError, AiErrorCode

__all__ = [
    "AiError",
    "AiErrorCode",
    "VisionAnalyzer",
    "MockVisionAnalyzer",
    "TeammateVisionAnalyzer",
    "create_vision_analyzer",
]
