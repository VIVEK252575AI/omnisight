"""Make one small Gemini request using the backend's configured credentials."""

from __future__ import annotations

import json
import sys

from app.ai.adapters.gemini import GeminiProviderError, GeminiTextAdapter
from app.config import get_settings


SYSTEM_INSTRUCTION = (
    'Return only the JSON object {"service":"gemini","status":"ok"}. '
    "Do not include markdown or any other text."
)
USER_CONTENT = 'Connection check. Return {"service":"gemini","status":"ok"}.'


def verify() -> dict:
    settings = get_settings()
    if not settings.gemini_api_key or not settings.gemini_api_key.get_secret_value().strip():
        raise RuntimeError("Gemini is not configured; set OMNISIGHT_GEMINI_API_KEY in backend/.env")

    adapter = GeminiTextAdapter(settings)
    response_text = adapter.generate_json(
        system_instruction=SYSTEM_INSTRUCTION,
        user_content=USER_CONTENT,
        max_output_tokens=32,
        timeout_seconds=min(settings.gemini_timeout_seconds, 20.0),
    )
    try:
        response = json.loads(response_text)
    except (TypeError, json.JSONDecodeError):
        raise RuntimeError("Gemini responded, but its output was not valid JSON") from None
    if response != {"service": "gemini", "status": "ok"}:
        raise RuntimeError("Gemini responded, but did not match the connection-check response")
    return {"provider": "GEMINI", "model": settings.gemini_model, "status": "connected"}


def main() -> int:
    try:
        result = verify()
    except (GeminiProviderError, RuntimeError) as exc:
        # Provider errors are sanitized at the adapter boundary. Never print
        # settings, request headers, response bodies, or credential values.
        print(f"Gemini connection check failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
