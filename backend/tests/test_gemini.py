"""Offline contract tests for Gemini patch suggestions (provider calls mocked)."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from app.ai.adapters.gemini import GeminiProviderError, GeminiTextAdapter
from app.config import Settings, get_settings


def _request_body() -> dict:
    return {
        "finding": {
            "finding_id": "finding-demo",
            "type": "clipped_element",
            "target": "#submit",
            "route": "/checkout",
            "viewport": {"width": 390, "height": 844},
            "evidence": "The button extends past the viewport.",
            "explanation": "A fixed width is wider than the viewport.",
            "severity": "high",
            "confidence": 0.9,
        },
        "source_files": [
            {
                "path": "styles/checkout.css",
                "text": "#submit {\n  width: 520px;\n}\n",
            }
        ],
    }


def _provider_response(*, old_text: str = "width: 520px;") -> dict:
    return {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "parts": [
                        {
                            "text": (
                                '{"schema_version":1,'
                                '"finding_id":"finding-demo",'
                                '"rationale":"Allow the button to fit the viewport.",'
                                '"generator_label":"GEMINI",'
                                '"files":[{"path":"styles/checkout.css",'
                                '"edits":[{"old_text":'
                                + json.dumps(old_text)
                                + ',"new_text":"max-width: 100%;"}]}]}'
                            )
                        }
                    ]
                },
            }
        ]
    }


def test_gemini_patch_suggestion_is_validated_and_review_only(
    client, monkeypatch
):
    settings = Settings(gemini_api_key=SecretStr("test-secret"))
    client.app.dependency_overrides[get_settings] = lambda: settings
    observed: dict = {}

    def fake_post(url, *, headers, json, timeout):
        observed.update(url=url, headers=headers, payload=json, timeout=timeout)
        return httpx.Response(200, json=_provider_response())

    monkeypatch.setattr("app.ai.adapters.gemini.httpx.post", fake_post)
    response = client.post("/api/ai/patch-suggestions", json=_request_body())

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "proposed"
    assert result["provider"] == "gemini"
    assert result["finding_id"] == "finding-demo"
    assert result["files"][0]["path"] == "styles/checkout.css"
    assert result["files"][0]["edits"][0]["new_text"] == "max-width: 100%;"
    assert observed["headers"]["x-goog-api-key"] == "test-secret"
    assert "test-secret" not in response.text


def test_gemini_patch_suggestion_rejects_non_matching_old_text(client, monkeypatch):
    settings = Settings(gemini_api_key=SecretStr("test-secret"))
    client.app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr(
        "app.ai.adapters.gemini.httpx.post",
        lambda *args, **kwargs: httpx.Response(
            200, json=_provider_response(old_text="color: purple;")
        ),
    )

    response = client.post("/api/ai/patch-suggestions", json=_request_body())

    assert response.status_code == 422
    assert "exact, unique source edit" in response.json()["error"]["message"]


def test_gemini_patch_suggestion_requires_server_key(client):
    response = client.post("/api/ai/patch-suggestions", json=_request_body())

    assert response.status_code == 503
    assert "OMNISIGHT_GEMINI_API_KEY" in response.json()["error"]["message"]


def test_gemini_patch_suggestion_rejects_traversal_path(client):
    body = _request_body()
    body["source_files"][0]["path"] = "../outside.css"

    response = client.post("/api/ai/patch-suggestions", json=body)

    assert response.status_code == 422


def test_gemini_retries_one_transient_service_error(monkeypatch):
    adapter = GeminiTextAdapter(Settings(gemini_api_key=SecretStr("test-secret")))
    responses = iter(
        [
            httpx.Response(503, json={"error": {"message": "temporarily overloaded"}}),
            httpx.Response(200, json=_provider_response()),
        ]
    )
    calls = []
    delays = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return next(responses)

    monkeypatch.setattr("app.ai.adapters.gemini.httpx.post", fake_post)
    monkeypatch.setattr("app.ai.adapters.gemini.time.sleep", delays.append)

    result = adapter.generate_json(
        system_instruction="Return JSON.", user_content="Test request."
    )

    assert json.loads(result)["finding_id"] == "finding-demo"
    assert len(calls) == 2
    assert delays == [1.0]
    assert all(call[1]["headers"]["x-goog-api-key"] == "test-secret" for call in calls)


def test_gemini_stops_after_bounded_transient_retries(monkeypatch):
    adapter = GeminiTextAdapter(Settings(gemini_api_key=SecretStr("test-secret")))
    calls = []
    delays = []

    def fake_post(*args, **kwargs):
        calls.append(kwargs)
        return httpx.Response(503, json={"error": {"message": "busy"}})

    monkeypatch.setattr("app.ai.adapters.gemini.httpx.post", fake_post)
    monkeypatch.setattr("app.ai.adapters.gemini.time.sleep", delays.append)

    with pytest.raises(GeminiProviderError) as error:
        adapter.generate_json(
            system_instruction="Return JSON.", user_content="Test request."
        )

    assert error.value.kind == "unavailable"
    assert len(calls) == 3
    assert delays == [1.0, 2.0]


def test_plain_text_generation_honors_output_token_limit(monkeypatch):
    adapter = GeminiTextAdapter(Settings(gemini_api_key=SecretStr("test-secret")))
    observed = {}

    def fake_post(url, *, headers, json, timeout):
        observed.update(url=url, headers=headers, payload=json, timeout=timeout)
        return httpx.Response(200, json=_provider_response())

    monkeypatch.setattr("app.ai.adapters.gemini.httpx.post", fake_post)

    adapter.generate_json(
        system_instruction="Return JSON.",
        user_content="Check connectivity.",
        max_output_tokens=32,
    )

    assert observed["payload"]["generationConfig"]["maxOutputTokens"] == 32
