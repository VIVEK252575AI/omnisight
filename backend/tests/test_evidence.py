"""Tests for the Playwright evidence contract (observation/v1).

Fixtures under tests/fixtures are labelled FIXTURE: SHOPLITE-LIKE — synthetic
evidence standing in for the (not yet delivered) real Playwright collector.
The artifact root is pointed at the fixtures directory via the
`client_with_artifacts` fixture, so referenced screenshots resolve to real
on-disk files.
"""

import hashlib
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SCANS = "/api/scans"
EVIDENCE = "/api/v1"


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _fixture_dict(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _create_scan(client) -> str:
    resp = client.post(SCANS, json={"target_url": "https://shoplite.local/checkout"})
    assert resp.status_code == 201, resp.text
    return resp.json()["scan_id"]


def _submit_raw(client, scan_id, raw: bytes):
    return client.post(
        f"{EVIDENCE}/scans/{scan_id}/evidence",
        content=raw,
        headers={"content-type": "application/json"},
    )


def _submit_json(client, scan_id, payload: dict):
    return client.post(f"{EVIDENCE}/scans/{scan_id}/evidence", json=payload)


class TestValidEvidence:
    def test_mobile_observation_stored_and_transitions(self, client_with_artifacts):
        scan_id = _create_scan(client_with_artifacts)
        raw = _fixture_bytes("observation_fixture.json")
        resp = _submit_raw(client_with_artifacts, scan_id, raw)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["scan_id"] == scan_id
        assert body["observation"]["viewport"] == {"width": 390, "height": 844}
        assert body["observation"]["route"] == "/checkout"
        assert body["screenshot_artifact_id"]

        scan = client_with_artifacts.get(f"{SCANS}/{scan_id}").json()
        assert scan["status"] == "evidence_collection"

        listed = client_with_artifacts.get(f"{EVIDENCE}/scans/{scan_id}/evidence").json()
        assert len(listed) == 1
        assert listed[0]["evidence_id"] == body["evidence_id"]

    def test_desktop_observation_stored(self, client_with_artifacts):
        scan_id = _create_scan(client_with_artifacts)
        resp = _submit_raw(
            client_with_artifacts, scan_id, _fixture_bytes("observation_fixture_desktop.json")
        )
        assert resp.status_code == 201, resp.text
        obs = resp.json()["observation"]
        assert obs["viewport"] == {"width": 1440, "height": 900}
        assert len(obs["console_errors"]) == 1
        assert len(obs["network_errors"]) == 1

    def test_artifact_served_with_correct_type(self, client_with_artifacts):
        scan_id = _create_scan(client_with_artifacts)
        body = _submit_raw(
            client_with_artifacts, scan_id, _fixture_bytes("observation_fixture.json")
        ).json()
        artifact_id = body["screenshot_artifact_id"]

        resp = client_with_artifacts.get(f"{EVIDENCE}/artifacts/{artifact_id}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.content.startswith(b"\x89PNG\r\n\x1a\n")

    def test_fixture_consumed_without_modification(self, client_with_artifacts):
        path = FIXTURES / "observation_fixture.json"
        before = hashlib.sha256(_fixture_bytes("observation_fixture.json")).hexdigest()
        scan_id = _create_scan(client_with_artifacts)
        resp = _submit_raw(client_with_artifacts, scan_id, path.read_bytes())
        assert resp.status_code == 201
        after = hashlib.sha256(_fixture_bytes("observation_fixture.json")).hexdigest()
        assert before == after
        # The fixture's FIXTURE label is dropped by the schema, not echoed.
        assert "_fixture" not in resp.json()["observation"]


class TestSafety:
    def test_unknown_scan_404(self, client_with_artifacts):
        resp = _submit_raw(
            client_with_artifacts,
            "00000000-0000-0000-0000-000000000000",
            _fixture_bytes("observation_fixture.json"),
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "SCAN_NOT_FOUND"

    def test_missing_screenshot_file_400(self, client_with_artifacts, scan_store):
        scan_id = _create_scan(client_with_artifacts)
        payload = _fixture_dict("observation_fixture.json")
        payload["screenshot"] = "screenshots/does-not-exist.png"
        resp = _submit_json(client_with_artifacts, scan_id, payload)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "ARTIFACT_INVALID"

    def test_traversal_path_400(self, client_with_artifacts):
        scan_id = _create_scan(client_with_artifacts)
        payload = _fixture_dict("observation_fixture.json")
        payload["screenshot"] = "screenshots/../../escape.png"
        resp = _submit_json(client_with_artifacts, scan_id, payload)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "ARTIFACT_INVALID"

    @pytest.mark.parametrize("unsafe", ["C:/Windows/secret.png", "/etc/passwd.png"])
    def test_absolute_path_400(self, client_with_artifacts, unsafe):
        scan_id = _create_scan(client_with_artifacts)
        payload = _fixture_dict("observation_fixture.json")
        payload["screenshot"] = unsafe
        resp = _submit_json(client_with_artifacts, scan_id, payload)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "ARTIFACT_INVALID"

    def test_wrong_extension_400(self, client_with_artifacts):
        scan_id = _create_scan(client_with_artifacts)
        payload = _fixture_dict("observation_fixture.json")
        payload["screenshot"] = "screenshots/checkout-390.txt"
        resp = _submit_json(client_with_artifacts, scan_id, payload)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "ARTIFACT_INVALID"

    def test_non_image_bytes_400(self, client_with_artifacts):
        scan_id = _create_scan(client_with_artifacts)
        payload = _fixture_dict("observation_fixture.json")
        payload["screenshot"] = "screenshots/not_really_a_png.png"
        resp = _submit_json(client_with_artifacts, scan_id, payload)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "ARTIFACT_INVALID"

    def test_invalid_evidence_marks_scan_failed(self, client_with_artifacts):
        scan_id = _create_scan(client_with_artifacts)
        payload = _fixture_dict("observation_fixture.json")
        payload["screenshot"] = "screenshots/missing.png"
        _submit_json(client_with_artifacts, scan_id, payload)
        scan = client_with_artifacts.get(f"{SCANS}/{scan_id}").json()
        assert scan["status"] == "failed"
        assert scan["error"]


class TestSchemaValidation:
    def test_missing_box_field_422(self, client_with_artifacts):
        scan_id = _create_scan(client_with_artifacts)
        payload = _fixture_dict("observation_fixture.json")
        del payload["elements"][0]["box"]["h"]
        resp = _submit_json(client_with_artifacts, scan_id, payload)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "INVALID_EVIDENCE"

    def test_negative_box_size_422(self, client_with_artifacts):
        scan_id = _create_scan(client_with_artifacts)
        payload = _fixture_dict("observation_fixture.json")
        payload["elements"][0]["box"]["w"] = -5
        resp = _submit_json(client_with_artifacts, scan_id, payload)
        assert resp.status_code == 422

    def test_unknown_schema_version_422(self, client_with_artifacts):
        scan_id = _create_scan(client_with_artifacts)
        payload = _fixture_dict("observation_fixture.json")
        payload["schema_version"] = "observation/v2"
        resp = _submit_json(client_with_artifacts, scan_id, payload)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "INVALID_EVIDENCE"

    def test_oversized_payload_413(self, client_with_artifacts):
        scan_id = _create_scan(client_with_artifacts)
        resp = _submit_raw(client_with_artifacts, scan_id, b"x" * 1_100_000)
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
        assert client_with_artifacts.get(f"{SCANS}/{scan_id}").json()["status"] == "failed"


class TestStateMachine:
    def test_duplicate_submission_409(self, client_with_artifacts):
        scan_id = _create_scan(client_with_artifacts)
        raw = _fixture_bytes("observation_fixture.json")
        assert _submit_raw(client_with_artifacts, scan_id, raw).status_code == 201
        second = _submit_raw(client_with_artifacts, scan_id, raw)
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "EVIDENCE_STATE_CONFLICT"
        # Only the first evidence was stored.
        assert len(client_with_artifacts.get(f"{EVIDENCE}/scans/{scan_id}/evidence").json()) == 1

    def test_evidence_on_wrong_state_409(self, client_with_artifacts):
        scan_id = _create_scan(client_with_artifacts)
        client_with_artifacts.post(
            f"{SCANS}/{scan_id}/transition?to_status=evidence_collection"
        )
        resp = _submit_raw(
            client_with_artifacts, scan_id, _fixture_bytes("observation_fixture.json")
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "EVIDENCE_STATE_CONFLICT"


class TestQueries:
    def test_list_evidence_unknown_scan_404(self, client_with_artifacts):
        resp = client_with_artifacts.get(
            f"{EVIDENCE}/scans/00000000-0000-0000-0000-000000000000/evidence"
        )
        assert resp.status_code == 404

    def test_unknown_artifact_404(self, client_with_artifacts):
        resp = client_with_artifacts.get(
            f"{EVIDENCE}/artifacts/00000000-0000-0000-0000-000000000000"
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "ARTIFACT_NOT_FOUND"
