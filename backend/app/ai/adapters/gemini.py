"""Gemini GenerateContent adapter. Provider credentials remain server-side."""

from __future__ import annotations

import re
from urllib.parse import quote

import httpx

from app.config import Settings

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
MAX_RESPONSE_BYTES = 32_768
_MODEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class GeminiProviderError(Exception):
    """Sanitized provider failure; never contains a response body or API key."""

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        super().__init__(message)


class GeminiTextAdapter:
    """One bounded Gemini text-generation call for the patch-proposal gateway."""

    provider_label = "GEMINI"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def generate_json(self, *, system_instruction: str, user_content: str) -> str:
        key = (
            self._settings.gemini_api_key.get_secret_value()
            if self._settings.gemini_api_key
            else ""
        )
        if not key:
            raise GeminiProviderError("configuration", "Gemini API key is not configured")
        model = self._settings.gemini_model.strip()
        if not _MODEL_RE.fullmatch(model):
            raise GeminiProviderError("configuration", "Gemini model name is invalid")

        try:
            response = httpx.post(
                f"{GEMINI_API_BASE}/{quote(model, safe='')}:generateContent",
                headers={"x-goog-api-key": key},
                json={
                    "systemInstruction": {"parts": [{"text": system_instruction}]},
                    "contents": [{"role": "user", "parts": [{"text": user_content}]}],
                    "generationConfig": {
                        "responseMimeType": "application/json",
                        "maxOutputTokens": self._settings.gemini_max_output_tokens,
                        "temperature": 0.1,
                    },
                },
                timeout=self._settings.gemini_timeout_seconds,
            )
        except httpx.TimeoutException:
            raise GeminiProviderError("timeout", "Gemini request timed out") from None
        except httpx.RequestError as exc:
            raise GeminiProviderError(
                "unavailable", f"Gemini request failed ({type(exc).__name__})"
            ) from None

        if response.status_code == 429:
            raise GeminiProviderError("rate_limited", "Gemini quota or rate limit was reached")
        if response.status_code in {401, 403}:
            raise GeminiProviderError("authentication", "Gemini rejected the API key or model access")
        if response.status_code >= 400:
            raise GeminiProviderError(
                "unavailable", f"Gemini request failed with HTTP {response.status_code}"
            )
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise GeminiProviderError("invalid_response", "Gemini response exceeded the size limit")

        try:
            body = response.json()
            candidates = body["candidates"]
            parts = candidates[0]["content"]["parts"]
            text = "".join(
                part["text"]
                for part in parts
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise GeminiProviderError(
                "invalid_response", "Gemini returned an empty or invalid response"
            ) from None
        if not text.strip():
            raise GeminiProviderError("invalid_response", "Gemini returned no text content")
        return text
