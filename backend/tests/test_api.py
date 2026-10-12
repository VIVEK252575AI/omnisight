"""Tests for the scan API endpoints and lifecycle state guard."""

import uuid

from app.schemas import LEGAL_TRANSITIONS, ScanStatus, TERMINAL_STATES

BASE = {"target_url": "https://example.com/shop"}


def _create(client, **overrides):
    body = {**BASE, **overrides}
    resp = client.post("/api/scans", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------- /health ----------

def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"


# ---------- POST /api/scans ----------

def test_create_scan_returns_created_state(client):
    data = _create(client)
    assert data["status"] == "created"
    assert data["findings"] == []
    assert data["target_url"] == "https://example.com/shop"
    assert uuid.UUID(data["scan_id"])  # valid UUID
    assert data["viewports"] == [{"width": 390, "height": 844}]


def test_create_scan_accepts_explicit_viewports_and_routes(client):
    data = _create(
        client,
        viewports=[{"width": 1440, "height": 900}],
        routes=["/checkout"],
    )
    assert data["viewports"] == [{"width": 1440, "height": 900}]
    assert data["routes"] == ["/checkout"]


def test_create_scan_rejects_absolute_route(client):
    resp = client.post("/api/scans", json={**BASE, "routes": ["https://evil.com"]})
    assert resp.status_code == 422
    details = resp.json()["error"]["details"]
    assert any("relative" in d["message"] for d in details)


def test_create_scan_rejects_malformed_url_and_too_many_viewports(client):
    resp = client.post("/api/scans", json={"target_url": "not a url"})
    assert resp.status_code == 422

    resp = client.post(
        "/api/scans",
        json={
            **BASE,
            "viewports": [
                {"width": 390, "height": 844},
                {"width": 768, "height": 1024},
                {"width": 1280, "height": 800},
                {"width": 1440, "height": 900},
                {"width": 1920, "height": 1080},
            ],
        },
    )
    assert resp.status_code == 422

    resp = client.post("/api/scans", json={"target_url": BASE["target_url"], "viewports": []})
    assert resp.status_code == 422


# ---------- GET /api/scans/{scan_id} ----------

def test_get_scan_roundtrip(client):
    data = _create(client)
    resp = client.get(f"/api/scans/{data['scan_id']}")
    assert resp.status_code == 200
    assert resp.json()["scan_id"] == data["scan_id"]


def test_get_unknown_scan_returns_404_with_actionable_detail(client):
    resp = client.get(f"/api/scans/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert "not found" in resp.json()["error"]["message"]


def test_list_scans_returns_created_scans(client):
    first = _create(client)
    second = _create(client, target_url="https://example.com/admin")
    resp = client.get("/api/scans")
    assert resp.status_code == 200
    ids = [s["scan_id"] for s in resp.json()]
    assert first["scan_id"] in ids and second["scan_id"] in ids


# ---------- POST /api/scans/{scan_id}/transition (state guard) ----------

def test_legal_forward_transition(client):
    data = _create(client)
    resp = client.post(
        f"/api/scans/{data['scan_id']}/transition?to_status=evidence_collection"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "evidence_collection"


def test_scan_never_reports_verified_before_early_stages(client):
    """A created scan cannot jump to validated/verified states."""
    data = _create(client)
    for forbidden in (
        ScanStatus.validation_passed,
        ScanStatus.patch_verified,
        ScanStatus.ready_for_review,
    ):
        resp = client.post(
            f"/api/scans/{data['scan_id']}/transition?to_status={forbidden.value}"
        )
        assert resp.status_code == 409, f"{forbidden} reachable from created!"
        assert "illegal transition" in resp.json()["error"]["message"]


def test_full_happy_path_lifecycle(client):
    data = _create(client)
    scan_id = data["scan_id"]
    path = (
        ScanStatus.evidence_collection,
        ScanStatus.analysis,
        ScanStatus.patch_proposed,
        ScanStatus.sandbox_validation,
        ScanStatus.validation_passed,
        ScanStatus.visual_verification,
        ScanStatus.patch_verified,
        ScanStatus.review_preparation,
        ScanStatus.ready_for_review,
    )
    for state in path:
        resp = client.post(f"/api/scans/{scan_id}/transition?to_status={state.value}")
        assert resp.status_code == 200, (state, resp.text)
        assert resp.json()["status"] == state.value


def test_failure_states_are_traceable_and_cannot_mask_success(client):
    data = _create(client)
    scan_id = data["scan_id"]
    client.post(f"/api/scans/{scan_id}/transition?to_status=evidence_collection")
    client.post(f"/api/scans/{scan_id}/transition?to_status=analysis")
    client.post(f"/api/scans/{scan_id}/transition?to_status=patch_proposed")
    resp = client.post(f"/api/scans/{scan_id}/transition?to_status=sandbox_validation")
    assert resp.status_code == 200
    # validation fails with error context
    resp = client.post(
        f"/api/scans/{scan_id}/transition?to_status=validation_failed&error=npm+build+failed"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "validation_failed"
    assert body["error"] == "npm build failed"
    # from validation_failed a direct jump to verified is forbidden
    resp = client.post(f"/api/scans/{scan_id}/transition?to_status=patch_verified")
    assert resp.status_code == 409


def test_transition_from_terminal_state_rejected(client):
    data = _create(client)
    scan_id = data["scan_id"]
    client.post(f"/api/scans/{scan_id}/transition?to_status=cancelled")
    resp = client.post(f"/api/scans/{scan_id}/transition?to_status=created")
    assert resp.status_code == 409
    assert "terminal" in resp.json()["error"]["message"]


def test_transition_unknown_scan_404(client):
    resp = client.post(f"/api/scans/{uuid.uuid4()}/transition?to_status=analysis")
    assert resp.status_code == 404


# ---------- Pure state-machine consistency ----------

def test_state_machine_edges_are_well_formed():
    for state in ScanStatus:
        assert state in LEGAL_TRANSITIONS, f"missing transitions for {state}"
        for target in LEGAL_TRANSITIONS[state]:
            assert target in LEGAL_TRANSITIONS, f"unknown target {target}"
        # no transitions out of terminal states
        if state in TERMINAL_STATES:
            assert LEGAL_TRANSITIONS[state] == frozenset()
        # failed build/test can never lead directly to verified success
        for source in (ScanStatus.validation_failed, ScanStatus.verification_failed):
            assert ScanStatus.patch_verified not in LEGAL_TRANSITIONS[source]
