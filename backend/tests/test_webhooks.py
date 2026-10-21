"""Tests for the SIMULATED CI/CD webhook receiver.

These exercise the real FastAPI app (TestClient) against in-memory stores via
dependency overrides. Nothing here talks to a real CI system.
"""

import hashlib
import hmac
import json
import uuid

import pytest

SECRET = "test-secret-123"
WEBHOOK = "/api/v1/webhooks"
CI = f"{WEBHOOK}/ci"
EVENTS = f"{WEBHOOK}/events"
SCANS = "/api/scans"


def _event(**overrides) -> dict:
    event = {
        "event_id": str(uuid.uuid4()),
        "event_type": "build.succeeded",
        "source": "simulated",
        "repository": "VIVEK252575AI/omnisight",
        "branch": "lakshy",
        "commit_sha": "a" * 40,
        "build_id": "build-1",
        "target_url": "https://example.com/shop",
        "timestamp": "2026-01-01T00:00:00Z",
        "metadata": {"run": 1},
    }
    event.update(overrides)
    return event


def _raw(**overrides) -> bytes:
    return json.dumps(_event(**overrides)).encode("utf-8")


def _sign(raw: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


class TestAccept:
    def test_succeeded_creates_scan(self, client):
        body = _event(event_id="evt-1")
        resp = client.post(CI, json=body)
        assert resp.status_code == 202, resp.text
        payload = resp.json()
        assert payload["status"] == "accepted"
        assert payload["event_id"] == "evt-1"
        assert payload["scan_id"]

        scan = client.get(f"{SCANS}/{payload['scan_id']}")
        assert scan.status_code == 200
        assert scan.json()["status"] == "created"

    def test_started_stored_without_scan(self, client):
        resp = client.post(CI, json=_event(event_id="evt-2", event_type="build.started"))
        assert resp.status_code == 202
        assert resp.json()["scan_id"] is None
        assert client.get(f"{EVENTS}/evt-2").status_code == 200

    def test_failed_stored_without_scan(self, client):
        resp = client.post(
            CI,
            json=_event(event_id="evt-3", event_type="build.failed", target_url=None),
        )
        assert resp.status_code == 202
        assert resp.json()["scan_id"] is None


class TestIdempotency:
    def test_duplicate_returns_200_single_scan(self, client, event_store, scan_store):
        body = _event(event_id="dup-1")
        first = client.post(CI, json=body)
        second = client.post(CI, json=body)
        assert first.status_code == 202
        assert second.status_code == 200
        assert second.json()["status"] == "duplicate"
        assert second.json()["scan_id"] == first.json()["scan_id"]
        assert len(scan_store.list()) == 1
        assert len(event_store.list()) == 1


class TestValidation:
    def test_succeeded_missing_target_url_422(self, client):
        body = _event(target_url=None)
        resp = client.post(CI, json=body)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_unknown_event_type_422(self, client):
        resp = client.post(CI, json=_event(event_type="build.exploded"))
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_bad_url_scheme_422(self, client):
        resp = client.post(CI, json=_event(target_url="ftp://example.com/x"))
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_non_simulated_source_422(self, client):
        resp = client.post(CI, json=_event(source="github-actions"))
        assert resp.status_code == 422

    def test_extra_field_422(self, client):
        resp = client.post(CI, json=_event(unexpected="nope"))
        assert resp.status_code == 422

    def test_malformed_json_422(self, client):
        resp = client.post(
            CI, content=b"{not json", headers={"content-type": "application/json"}
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_oversized_body_413(self, client):
        big = _event(metadata={"blob": "x" * 70000})
        resp = client.post(CI, content=json.dumps(big).encode())
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


class TestSignature:
    def test_valid_signature_accepted(self, client_with_secret):
        raw = _raw(event_id="sig-1")
        resp = client_with_secret.post(
            CI,
            content=raw,
            headers={"content-type": "application/json", "X-OmniSight-Signature": _sign(raw)},
        )
        assert resp.status_code == 202, resp.text

    def test_sha256_prefixed_signature_accepted(self, client_with_secret):
        raw = _raw(event_id="sig-2")
        resp = client_with_secret.post(
            CI,
            content=raw,
            headers={
                "content-type": "application/json",
                "X-OmniSight-Signature": "sha256=" + _sign(raw),
            },
        )
        assert resp.status_code == 202

    def test_invalid_signature_401(self, client_with_secret):
        raw = _raw()
        resp = client_with_secret.post(
            CI,
            content=raw,
            headers={"content-type": "application/json", "X-OmniSight-Signature": "deadbeef"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "UNAUTHORIZED"

    def test_missing_signature_401(self, client_with_secret):
        resp = client_with_secret.post(CI, content=_raw())
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "UNAUTHORIZED"

    def test_tampered_body_rejected(self, client_with_secret):
        signature = _sign(_raw(event_id="sig-3"))
        tampered = _raw(event_id="sig-3", build_id="evil")
        resp = client_with_secret.post(
            CI,
            content=tampered,
            headers={"content-type": "application/json", "X-OmniSight-Signature": signature},
        )
        assert resp.status_code == 401

    def test_signature_ignored_when_no_secret(self, client):
        resp = client.post(
            CI, content=_raw(), headers={"X-OmniSight-Signature": "whatever"}
        )
        assert resp.status_code == 202


class TestQuery:
    def test_list_and_filter(self, client):
        client.post(CI, json=_event(event_id="q-1", event_type="build.succeeded"))
        client.post(CI, json=_event(event_id="q-2", event_type="build.started"))
        client.post(CI, json=_event(event_id="q-3", event_type="build.failed", target_url=None))

        all_events = client.get(EVENTS)
        assert all_events.status_code == 200
        assert len(all_events.json()) == 3

        started = client.get(EVENTS, params={"event_type": "build.started"})
        assert started.status_code == 200
        assert [e["event_id"] for e in started.json()] == ["q-2"]

    def test_pagination(self, client):
        for i in range(5):
            client.post(CI, json=_event(event_id=f"p-{i}", event_type="build.started"))
        page = client.get(EVENTS, params={"limit": 2, "offset": 2})
        assert page.status_code == 200
        assert [e["event_id"] for e in page.json()] == ["p-2", "p-3"]

    def test_get_unknown_event_404(self, client):
        resp = client.get(f"{EVENTS}/does-not-exist")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "CI_EVENT_NOT_FOUND"


class TestHostAllowlist:
    def test_disallowed_host_403(self, client_with_allowlist):
        resp = client_with_allowlist.post(
            CI, json=_event(target_url="https://blocked.test/shop")
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"

    def test_allowed_host_accepted(self, client_with_allowlist):
        resp = client_with_allowlist.post(
            CI, json=_event(target_url="https://allowed.test/shop")
        )
        assert resp.status_code == 202


class TestStoreFailure:
    def test_scan_failure_500_no_partial(self, client, scan_store, event_store, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("injected store failure")

        monkeypatch.setattr(scan_store, "create", boom)
        resp = client.post(CI, json=_event(event_id="fail-1"))
        assert resp.status_code == 500
        assert resp.json()["error"]["code"] == "INTERNAL_ERROR"
        assert event_store.get("fail-1") is None
        assert scan_store.list() == []
