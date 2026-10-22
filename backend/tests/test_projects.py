from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app.config import Settings
from app.db_models import Base, Project, TargetAuthorizationEvent, TargetEnvironment
from app.project_store import ProjectStore, get_project_store
from app import target_policy


@pytest.fixture()
def projects_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[Project.__table__, TargetEnvironment.__table__, TargetAuthorizationEvent.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(allowed_origins=["http://127.0.0.1:8000"])
    main.app.dependency_overrides[main.get_settings] = lambda: settings
    main.app.dependency_overrides[get_project_store] = lambda: ProjectStore(factory)

    async def loopback_app(scope, receive, send):
        if scope["type"] == "http":
            scope["client"] = ("127.0.0.1", 50123)
        await main.app(scope, receive, send)

    try:
        with TestClient(loopback_app, base_url="http://127.0.0.1:8000", raise_server_exceptions=False) as client:
            yield client
    finally:
        main.app.dependency_overrides.clear()
        engine.dispose()


def test_project_and_pending_target_persist_across_requests(projects_client):
    client = projects_client
    headers = {"Origin": "http://127.0.0.1:8000"}

    created = client.post(
        "/api/v1/projects",
        json={"name": " Checkout QA ", "target_url": "https://staging.example.test/shop/", "environment_kind": "staging"},
        headers=headers,
    )
    assert created.status_code == 201
    project = created.json()
    assert project["name"] == "Checkout QA"
    assert project["slug"] == "checkout-qa"
    assert project["repository_status"] == "not_connected"
    assert len(project["id"]) == 36
    assert project["targets"] == [{
        "id": project["targets"][0]["id"],
        "name": "Primary target",
        "base_url": "https://staging.example.test/shop",
        "environment_kind": "staging",
        "authorization_status": "pending",
        "allowed_hosts": [],
        "version": 1,
        "created_at": project["targets"][0]["created_at"],
    }]

    listed = client.get("/api/v1/projects")
    detail = client.get(f"/api/v1/projects/{project['id']}")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [project["id"]]
    assert detail.status_code == 200
    assert detail.json()["id"] == project["id"]
    assert detail.json()["targets"][0]["id"] == project["targets"][0]["id"]
    assert detail.json()["targets"][0]["base_url"] == project["targets"][0]["base_url"]

    duplicate = client.post(
        "/api/v1/projects",
        json={"name": "checkout qa", "target_url": "https://other.example.test"},
        headers=headers,
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["message"] == "A project with this name already exists"


@pytest.mark.parametrize("target_url", [
    "ftp://staging.example.test",
    "https://user:password@staging.example.test",
    "https://staging.example.test/?token=secret",
    "https://staging.example.test/#private",
    "https://staging.example.test:invalid",
    "not-a-url",
])
def test_project_target_rejects_unsupported_or_credential_bearing_url(projects_client, target_url):
    response = projects_client.post(
        "/api/v1/projects",
        json={"name": "Invalid target", "target_url": target_url},
        headers={"Origin": "http://127.0.0.1:8000"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_project_create_requires_allowed_local_origin(projects_client):
    body = {"name": "Origin check", "target_url": "https://staging.example.test"}
    missing_origin = projects_client.post("/api/v1/projects", json=body)
    foreign_origin = projects_client.post(
        "/api/v1/projects", json=body, headers={"Origin": "https://attacker.example"}
    )
    assert missing_origin.status_code == 403
    assert foreign_origin.status_code == 403


def test_target_authorization_is_explicit_persisted_and_revocable(projects_client, monkeypatch):
    monkeypatch.setattr(target_policy, "resolve_target_addresses", lambda _host: {"93.184.216.34"})
    client = projects_client
    headers = {"Origin": "http://127.0.0.1:8000"}
    project = client.post(
        "/api/v1/projects",
        json={"name": "Authorized target", "target_url": "https://staging.example.com/shop"},
        headers=headers,
    ).json()
    target = project["targets"][0]
    authorize_url = f"/api/v1/projects/{project['id']}/targets/{target['id']}/authorize"

    missing_confirmation = client.post(authorize_url, json={"confirm_authorization": False}, headers=headers)
    assert missing_confirmation.status_code == 403
    non_boolean_confirmation = client.post(
        authorize_url, json={"confirm_authorization": "true"}, headers=headers
    )
    assert non_boolean_confirmation.status_code == 422

    approved = client.post(authorize_url, json={"confirm_authorization": True}, headers=headers)
    assert approved.status_code == 200
    assert approved.json()["targets"][0]["authorization_status"] == "approved"
    assert approved.json()["targets"][0]["allowed_hosts"] == ["staging.example.com"]

    # Repeating the same command is idempotent and does not create duplicate audit events.
    repeated = client.post(authorize_url, json={"confirm_authorization": True}, headers=headers)
    assert repeated.status_code == 200
    events_url = f"/api/v1/projects/{project['id']}/targets/{target['id']}/authorization-events"
    events = client.get(events_url).json()
    assert [item["action"] for item in events] == ["authorized"]
    assert events[0]["actor_id"] == "local-owner"
    assert events[0]["target_url_snapshot"] == "https://staging.example.com/shop"
    assert events[0]["allowed_hosts_snapshot"] == ["staging.example.com"]

    revoked = client.post(
        f"/api/v1/projects/{project['id']}/targets/{target['id']}/revoke",
        headers=headers,
    )
    assert revoked.status_code == 200
    assert revoked.json()["targets"][0]["authorization_status"] == "revoked"
    assert revoked.json()["targets"][0]["allowed_hosts"] == []
    assert [item["action"] for item in client.get(events_url).json()] == ["authorized", "revoked"]
    client.post(f"/api/v1/projects/{project['id']}/targets/{target['id']}/revoke", headers=headers)
    assert [item["action"] for item in client.get(events_url).json()] == ["authorized", "revoked"]


@pytest.mark.parametrize(("target_url", "environment_kind", "addresses"), [
    ("http://staging.example.com", "staging", {"93.184.216.34"}),
    ("https://staging.example.com:8443", "staging", {"93.184.216.34"}),
    ("https://127.0.0.1", "development", {"127.0.0.1"}),
    ("https://[::1]", "development", {"::1"}),
    ("https://staging.example.com", "staging", {"10.10.0.8"}),
    ("https://staging.example.com", "staging", {"169.254.169.254"}),
    ("https://staging.example.com", "staging", {"93.184.216.34", "fc00::1"}),
    ("https://staging.example.com", "other", {"93.184.216.34"}),
])
def test_target_authorization_rejects_unsafe_targets(projects_client, monkeypatch, target_url, environment_kind, addresses):
    monkeypatch.setattr(target_policy, "resolve_target_addresses", lambda _host: addresses)
    client = projects_client
    headers = {"Origin": "http://127.0.0.1:8000"}
    project = client.post(
        "/api/v1/projects",
        json={"name": "Unsafe target", "target_url": target_url, "environment_kind": environment_kind},
        headers=headers,
    ).json()
    target = project["targets"][0]
    response = client.post(
        f"/api/v1/projects/{project['id']}/targets/{target['id']}/authorize",
        json={"confirm_authorization": True},
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TARGET_NOT_AUTHORIZABLE"
    assert client.get(f"/api/v1/projects/{project['id']}").json()["targets"][0]["authorization_status"] == "pending"


def test_target_authorization_requires_loopback_and_allowed_origin(projects_client, monkeypatch):
    monkeypatch.setattr(target_policy, "resolve_target_addresses", lambda _host: {"93.184.216.34"})
    client = projects_client
    project = client.post(
        "/api/v1/projects",
        json={"name": "Origin target", "target_url": "https://staging.example.com"},
        headers={"Origin": "http://127.0.0.1:8000"},
    ).json()
    target = project["targets"][0]
    path = f"/api/v1/projects/{project['id']}/targets/{target['id']}/authorize"
    response = client.post(path, json={"confirm_authorization": True})
    assert response.status_code == 403
