"""The provider adapter protocol.

Implementations are constructed and injected by the gateway; nothing else in
the codebase talks to a provider directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from app.ai.contracts import AiCapability, RawModelResult


@runtime_checkable
class ModelProvider(Protocol):
    """A pluggable model backend (hosted API, local runtime, or test fake)."""

    provider_id: str
    model_id: str
    is_mock: bool

    def supports(self, capability: AiCapability) -> bool:
        """Return True if this provider can service ``capability``."""
        ...

    def invoke(
        self,
        *,
        capability: AiCapability,
        system_prompt: str,
        user_prompt: str,
        image_paths: list[Path],
        request_id: str,
    ) -> RawModelResult:
        """Run one call and return the raw, untrusted result.

        Adapters MUST NOT parse or validate the output, and MUST NOT execute
        anything from it. Transport failures should raise
        :class:`~app.ai.errors.AiError`.
        """
        ...
