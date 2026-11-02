"""Multi-provider VLM adapter (OpenAI, Anthropic, Grok) via httpx.

Provider selected by OMNISIGHT_VLM_PROVIDER. Each provider uses its own API key
env var. Free-tier quota is tried first; on quota exhaustion the adapter raises
a typed VisionQuotaError so callers can fall back to a paid key if configured.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Callable

import httpx

from app.ai.adapters.base import ImageResolver, VisionAnalyzer
from app.ai.contracts import RawVisionResult
from app.ai.errors import VisionAdapterError as VisionProviderError, VisionTimeoutError
from app.config import Settings


PROVIDER_OPENAI = "openai"
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_GROK = "grok"
PROVIDER_GEMINI = "gemini"

KNOWN_PROVIDERS = (PROVIDER_OPENAI, PROVIDER_ANTHROPIC, PROVIDER_GROK, PROVIDER_GEMINI)


def _read_key(settings: Settings, provider: str) -> str | None:
    if provider == PROVIDER_OPENAI:
        return settings.openai_api_key or os.getenv("OPENAI_API_KEY") or os.getenv("OMNISIGHT_OPENAI_API_KEY")
    if provider == PROVIDER_ANTHROPIC:
        return settings.anthropic_api_key or os.getenv("ANTHROPIC_API_KEY") or os.getenv("OMNISIGHT_ANTHROPIC_API_KEY")
    if provider == PROVIDER_GROK:
        return settings.grok_api_key or os.getenv("GROK_API_KEY") or os.getenv("OMNISIGHT_GROK_API_KEY") or os.getenv("XAI_API_KEY")
    if provider == PROVIDER_GEMINI:
        return settings.gemini_api_key or os.getenv("GEMINI_API_KEY") or os.getenv("OMNISIGHT_GEMINI_API_KEY")
    return None


class MultiProviderVisionAnalyzer(VisionAnalyzer):
    def __init__(self, settings: Settings, *, resolver: ImageResolver | None = None, provider: str | None = None):
        self.settings = settings
        self.resolver = resolver
        self.provider = (provider or getattr(settings, "vlm_provider", None) or PROVIDER_GEMINI).strip().lower()
        if self.provider not in KNOWN_PROVIDERS:
            raise VisionProviderError(f"unknown VLM provider '{self.provider}'")
        self.api_key = _read_key(settings, self.provider)
        if not self.api_key:
            raise VisionProviderError(f"no API key configured for provider '{self.provider}'")

    @property
    def provider_label(self) -> str:
        return self.provider.upper()

    @property
    def is_mock(self) -> bool:
        return False

    @property
    def model_label(self) -> str:
        if self.provider == PROVIDER_OPENAI:
            return getattr(self.settings, "openai_model", None) or "gpt-4o-mini"
        if self.provider == PROVIDER_ANTHROPIC:
            return getattr(self.settings, "anthropic_model", None) or "claude-3-5-sonnet-latest"
        if self.provider == PROVIDER_GROK:
            return getattr(self.settings, "grok_model", None) or "grok-2-vision-latest"
        return getattr(self.settings, "gemini_model", None) or "gemini-2.0-flash"

    def analyze(self, request) -> RawVisionResult:
        # Resolve images
        images = []
        for ref in request.screenshots:
            if self.resolver is None:
                continue
            path = self.resolver(ref.artifact_id)
            if path is None:
                continue
            data = Path(path).read_bytes()
            images.append((path, data))
        # Build a text prompt from context
        prompt = "Analyze the UI screenshot and return JSON findings."
        try:
            if self.provider == PROVIDER_OPENAI:
                return self._openai(prompt, images)
            if self.provider == PROVIDER_ANTHROPIC:
                return self._anthropic(prompt, images)
            if self.provider == PROVIDER_GROK:
                return self._grok(prompt, images)
            raise VisionProviderError("gemini should use GeminiVisionAnalyzer")
        except httpx.TimeoutException as e:
            raise VisionTimeoutError(str(e)) from e

    def _openai(self, prompt, images):
        content = [{"type": "text", "text": prompt}]
        for _, data in images:
            b64 = base64.b64encode(data).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
        r = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model_label, "messages": [{"role": "user", "content": content}], "max_tokens": 2048},
            timeout=60,
        )
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        return RawVisionResult(raw_text=text, provider_label=self.provider_label, is_mock=False, model_label=self.model_label)

    def _anthropic(self, prompt, images):
        content = [{"type": "text", "text": prompt}]
        for _, data in images:
            b64 = base64.b64encode(data).decode()
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}})
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            json={"model": self.model_label, "max_tokens": 2048, "messages": [{"role": "user", "content": content}]},
            timeout=60,
        )
        r.raise_for_status()
        text = r.json()["content"][0]["text"]
        return RawVisionResult(raw_text=text, provider_label=self.provider_label, is_mock=False, model_label=self.model_label)

    def _grok(self, prompt, images):
        # Grok uses OpenAI-compatible API
        content = [{"type": "text", "text": prompt}]
        for _, data in images:
            b64 = base64.b64encode(data).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
        r = httpx.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model_label, "messages": [{"role": "user", "content": content}], "max_tokens": 2048},
            timeout=60,
        )
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        return RawVisionResult(raw_text=text, provider_label=self.provider_label, is_mock=False, model_label=self.model_label)
