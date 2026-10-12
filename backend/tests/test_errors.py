"""Scaffold tests: health, single error envelope, CORS and config parsing."""

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _err(resp):
    """Assert the shared envelope shape and return the error body."""
    body = resp.json()
    assert "error" in body, body
    error = body["error"]
    assert set(error) >= {"code", "message", "details"}
    return error


class TestHealth:
    def test_health_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["service"] == "OmniSight Backend"
        assert body["version"] == "0.1.0"


class TestErrorEnvelope:
    def test_unknown_route_envelope(self, client):
        resp = client.get("/definitely-not-a-route")
        assert resp.status_code == 404
        assert _err(resp)["code"] == "NOT_FOUND"

    def test_missing_required_field_envelope(self, client):
        resp = client.post("/api/scans", json={})
        assert resp.status_code == 422
        error = _err(resp)
        assert error["code"] == "VALIDATION_ERROR"
        assert any(d["field"] == "body.target_url" for d in error["details"])

    def test_invalid_url_envelope(self, client):
        resp = client.post("/api/scans", json={"target_url": "not-a-url"})
        assert resp.status_code == 422
        assert _err(resp)["code"] == "VALIDATION_ERROR"

    def test_malformed_json_envelope(self, client):
        resp = client.post(
            "/api/scans",
            content=b"{not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 422
        assert _err(resp)["code"] == "VALIDATION_ERROR"

    def test_empty_body_envelope(self, client):
        resp = client.post(
            "/api/scans",
            content=b"",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 422
        assert _err(resp)["code"] == "VALIDATION_ERROR"

    def test_url_length_boundary_envelope(self, client):
        # "https://example.com/" is 20 chars; +2035 a's => 2055, above the
        # 2048 default limit but below pydantic's HttpUrl 2083 cap.
        url = "https://example.com/" + "a" * 2035
        resp = client.post("/api/scans", json={"target_url": url})
        assert resp.status_code == 422
        assert _err(resp)["code"] == "URL_TOO_LONG"

    def test_oversized_body_envelope(self, client):
        resp = client.post(
            "/api/scans", json={"target_url": "https://example.com/" + "a" * 10000}
        )
        assert resp.status_code == 422
        assert _err(resp)["code"] in {"VALIDATION_ERROR", "URL_TOO_LONG"}

    def test_domain_error_scan_not_found(self, client):
        resp = client.get(f"/api/scans/{uuid.uuid4()}")
        assert resp.status_code == 404
        assert _err(resp)["code"] == "SCAN_NOT_FOUND"

    def test_domain_error_illegal_transition(self, client):
        scan = client.post(
            "/api/scans", json={"target_url": "https://example.com"}
        ).json()
        resp = client.post(
            f"/api/scans/{scan['scan_id']}/transition?to_status=patch_verified"
        )
        assert resp.status_code == 409
        assert _err(resp)["code"] == "ILLEGAL_TRANSITION"

    def test_internal_error_envelope_hides_traceback(self):
        app = create_app()

        @app.get("/__boom")
        def _boom():
            raise RuntimeError("kaboom-secret")

        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.get("/__boom")
        assert resp.status_code == 500
        assert _err(resp)["code"] == "INTERNAL_ERROR"
        assert "Traceback" not in resp.text
        assert "kaboom-secret" not in resp.text


class TestCors:
    def test_preflight_allowed_origin(self, client):
        resp = client.options(
            "/api/scans",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert resp.status_code in (200, 204)
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"

    def test_preflight_disallowed_origin_not_reflected(self, client):
        resp = client.options(
            "/api/scans",
            headers={
                "Origin": "http://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert resp.headers.get("access-control-allow-origin") != "http://evil.example"


class TestConfig:
    def test_defaults_when_unset(self):
        settings = Settings(_env_file=None)
        assert settings.log_level == "INFO"
        assert settings.ci_webhook_secret is None
        assert settings.data_dir == Path("data")
        assert settings.artifacts_dir == Path("artifacts")
        assert "http://localhost:5173" in settings.allowed_origins

    def test_allowed_origins_comma_separated(self):
        settings = Settings(
            _env_file=None, allowed_origins="http://a.test, http://b.test"
        )
        assert settings.allowed_origins == ["http://a.test", "http://b.test"]

    def test_allowed_origins_list_passthrough(self):
        settings = Settings(_env_file=None, allowed_origins=["http://a.test"])
        assert settings.allowed_origins == ["http://a.test"]
