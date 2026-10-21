"""Gemini integration through the regular approved-source patch workflow."""

from __future__ import annotations

import json as jsonlib
from uuid import uuid4

import httpx

from app.ai.adapters.patch import create_patch_generator
from app.ai.contracts import Finding, FindingCategory, FindingStatus, Severity
from app.config import Settings
from app.detection_schemas import FindingRecord


def _make_finding(detection_store) -> str:
    finding = Finding(
        category=FindingCategory.clipped_element,
        target_candidate="#submit",
        observation="The submit control exceeds the mobile viewport.",
        severity=Severity.high,
        confidence=0.9,
        status=FindingStatus.detected,
    )
    record = FindingRecord(
        finding=finding,
        scan_id=uuid4(),
        adapter_label="MOCK",
        is_mock=True,
        route="/checkout",
    )
    detection_store.replace_findings(record.scan_id, [record])
    return finding.finding_id


def test_gemini_runs_through_the_standard_approved_patch_pipeline(
    tmp_path, monkeypatch, patch_client, detection_store
):
    source = tmp_path / "styles" / "checkout.css"
    source.parent.mkdir(parents=True)
    source.write_text("#submit {\n  width: 520px;\n}\n", encoding="utf-8")
    settings = Settings(
        patch_generator="gemini",
        patch_source_root=str(tmp_path),
        approved_patch_paths=["styles/**"],
        gemini_api_key="test-key",
    )
    generator = create_patch_generator(settings)
    observed: dict = {}

    def fake_post(url, *, headers, json, timeout):
        observed.update(url=url, headers=headers, payload=json, timeout=timeout)
        request_context = jsonlib.loads(json["contents"][0]["parts"][0]["text"])
        proposal = {
            "schema_version": 1,
            "finding_id": request_context["finding"]["finding_id"],
            "rationale": "Allow the button to fit the viewport.",
            "generator_label": "GEMINI",
            "files": [
                {
                    "path": "styles/checkout.css",
                    "edits": [
                        {
                            "old_text": "width: 520px;",
                            "new_text": "max-width: 100%;",
                        }
                    ],
                }
            ],
        }
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": jsonlib.dumps(proposal)}
                            ]
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr("app.ai.adapters.gemini.httpx.post", fake_post)
    client = patch_client(generator=generator, settings=settings)
    finding_id = _make_finding(detection_store)

    response = client.post(f"/api/v1/findings/{finding_id}/patch-proposals")

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "proposed"
    assert body["generator_label"] == "GEMINI"
    assert body["is_mock"] is False
    assert body["files"][0]["path"] == "styles/checkout.css"
    assert observed["headers"]["x-goog-api-key"] == "test-key"
    assert str(tmp_path) not in observed["payload"]["contents"][0]["parts"][0]["text"]


def test_gemini_patch_generator_without_key_is_recorded_as_rejected(
    tmp_path, patch_client, detection_store
):
    source = tmp_path / "styles" / "checkout.css"
    source.parent.mkdir(parents=True)
    source.write_text("#submit { width: 520px; }\n", encoding="utf-8")
    settings = Settings(
        patch_generator="gemini",
        patch_source_root=str(tmp_path),
        approved_patch_paths=["styles/**"],
    )
    client = patch_client(
        generator=create_patch_generator(settings), settings=settings
    )
    finding_id = _make_finding(detection_store)

    response = client.post(f"/api/v1/findings/{finding_id}/patch-proposals")

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "rejected"
    assert body["rejection_reasons"] == ["generator_error"]
    assert body["generator_label"] == "GEMINI"
    assert body["is_mock"] is False


def test_gemini_timeout_is_recorded_with_timeout_reason(
    tmp_path, monkeypatch, patch_client, detection_store
):
    source = tmp_path / "styles" / "checkout.css"
    source.parent.mkdir(parents=True)
    source.write_text("#submit { width: 520px; }\n", encoding="utf-8")
    settings = Settings(
        patch_generator="gemini",
        patch_source_root=str(tmp_path),
        approved_patch_paths=["styles/**"],
        gemini_api_key="test-key",
    )

    def timeout(*args, **kwargs):
        raise httpx.TimeoutException("simulated provider timeout")

    monkeypatch.setattr("app.ai.adapters.gemini.httpx.post", timeout)
    client = patch_client(
        generator=create_patch_generator(settings), settings=settings
    )
    finding_id = _make_finding(detection_store)

    response = client.post(f"/api/v1/findings/{finding_id}/patch-proposals")

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "rejected"
    assert body["rejection_reasons"] == ["generator_timeout"]
    assert body["generator_label"] == "GEMINI"
