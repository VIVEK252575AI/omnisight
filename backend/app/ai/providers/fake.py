"""Deterministic offline provider used by tests and CI.

It never touches the network. Responses are scripted per capability, so a test
can drive success, malformed output, provider errors, and timeouts
deterministically. Scripted items are consumed in order; when a capability's
script is exhausted, ``invoke`` raises ``AI_PROVIDER_UNAVAILABLE``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from app.ai.contracts import AiCapability, RawModelResult
from app.ai.errors import AiError, AiErrorCode

ScriptItem = str | RawModelResult | Callable[[], RawModelResult]


class FakeModelProvider:
    """A scripted, network-free :class:`ModelProvider` implementation."""

    def __init__(
        self,
        *,
        responses: Mapping[AiCapability, Sequence[ScriptItem]] | None = None,
        delay_seconds: float = 0.0,
        provider_id: str = "fake",
        model_id: str = "fake-deterministic-v1",
    ) -> None:
        self.provider_id = provider_id
        self.model_id = model_id
        self.is_mock = True
        self._delay = delay_seconds
        self._scripts: dict[AiCapability, list[ScriptItem]] = {
            capability: list(items) for capability, items in (responses or {}).items()
        }
        self.calls: list[dict[str, Any]] = []

    def supports(self, capability: AiCapability) -> bool:
        return capability in self._scripts

    def invoke(
        self,
        *,
        capability: AiCapability,
        system_prompt: str,
        user_prompt: str,
        image_paths: list[Path],
        request_id: str,
    ) -> RawModelResult:
        self.calls.append(
            {
                "capability": capability.value,
                "request_id": request_id,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "image_count": len(image_paths),
            }
        )
        if self._delay:
            time.sleep(self._delay)

        queue = self._scripts.get(capability)
        if not queue:
            raise AiError(
                AiErrorCode.AI_PROVIDER_UNAVAILABLE,
                f"no scripted response for capability '{capability.value}'",
                retryable=False,
            )

        item = queue.pop(0)
        return self._materialize(item)

    def _materialize(self, item: ScriptItem) -> RawModelResult:
        if callable(item):
            return item()
        if isinstance(item, RawModelResult):
            return item
        return RawModelResult(
            text=item,
            provider_id=self.provider_id,
            model_id=self.model_id,
        )

    @staticmethod
    def json_text(payload: dict[str, Any]) -> str:
        """Serialize a payload to the JSON text a provider would return."""
        return json.dumps(payload)
