"""Gemini GenerateContent adapter. Provider credentials remain server-side."""

from __future__ import annotations

import base64
import re
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from app.config import Settings

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
MAX_RESPONSE_BYTES = 32_768
MAX_TRANSIENT_RETRIES = 2
RETRYABLE_STATUS_CODES = frozenset({500, 502, 503, 504})
RETRY_BACKOFF_SECONDS = 1.0
_MODEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class GeminiProviderError(Exception):
    """Sanitized provider failure; never contains a response body or API key."""

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        super().__init__(message)


class GeminiTextAdapter:
    """Bounded Gemini JSON generation for text and screenshot inputs."""

    provider_label = "GEMINI"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def generate_json(
        self,
        *,
        system_instruction: str,
        user_content: str,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        return self._generate_json(
            system_instruction=system_instruction,
            parts=[{"text": user_content}],
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )

    def generate_json_with_images(
        self,
        *,
        system_instruction: str,
        user_content: str,
        image_paths: list[Path],
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        """Send local screenshots as inline image parts with bounded JSON text."""
        parts: list[dict] = [{"text": user_content}]
        for path in image_paths:
            mime_type = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
            }.get(path.suffix.lower())
            if mime_type is None:
                raise GeminiProviderError(
                    "configuration", "Gemini vision accepts PNG or JPEG screenshots"
                )
            try:
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            except OSError:
                raise GeminiProviderError(
                    "configuration", "Gemini could not read a stored screenshot"
                ) from None
            parts.append(
                {"inlineData": {"mimeType": mime_type, "data": encoded}}
            )
        return self._generate_json(
            system_instruction=system_instruction,
            parts=parts,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )

    def _generate_json(
        self,
        *,
        system_instruction: str,
        parts: list[dict],
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
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

        request_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else self._settings.gemini_timeout_seconds
        )
        deadline = time.monotonic() + request_timeout
        request_url = f"{GEMINI_API_BASE}/{quote(model, safe='')}:generateContent"
        request_payload = {
            "systemInstruction": {"parts": [{"text": system_instruction}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "maxOutputTokens": (
                    max_output_tokens or self._settings.gemini_max_output_tokens
                ),
                "temperature": 0.1,
            },
        }
        response = None
        for attempt in range(MAX_TRANSIENT_RETRIES + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GeminiProviderError("timeout", "Gemini request timed out")
            try:
                response = httpx.post(
                    request_url,
                    headers={"x-goog-api-key": key},
                    json=request_payload,
                    timeout=min(request_timeout, remaining),
                )
            except httpx.TimeoutException:
                # A timed-out request may have been processed remotely. Do not
                # repeat it and risk an unbounded duplicate provider charge.
                raise GeminiProviderError("timeout", "Gemini request timed out") from None
            except httpx.RequestError as exc:
                raise GeminiProviderError(
                    "unavailable", f"Gemini request failed ({type(exc).__name__})"
                ) from None

            if (
                response.status_code not in RETRYABLE_STATUS_CODES
                or attempt >= MAX_TRANSIENT_RETRIES
            ):
                break
            remaining = deadline - time.monotonic()
            backoff = RETRY_BACKOFF_SECONDS * (2**attempt)
            if remaining <= backoff:
                break
            time.sleep(backoff)

        assert response is not None

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
