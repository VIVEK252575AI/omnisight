from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app.config import Settings
from app.db_models import Base, Project, TargetEnvironment
from app.project_store import ProjectStore, get_project_store


@pytest.fixture()
def projects_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[Project.__table__, TargetEnvironment.__table__])
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
