"""Shared pytest fixtures: fresh stores and settings per test."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import Settings
from app.evidence_storage import EvidenceStore, get_evidence_store
from app.event_storage import InMemoryCIEventStore, get_event_store
from app.storage import ScanStore, get_store

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _base_settings(**overrides) -> Settings:
    return Settings(
        enable_playwright_worker=False,
        enable_vlm_detection=False,
        enable_patch_engine=False,
        enable_github_delivery=False,
        **overrides,
    )


@pytest.fixture()
def scan_store() -> ScanStore:
    return ScanStore()


@pytest.fixture()
def event_store() -> InMemoryCIEventStore:
    return InMemoryCIEventStore()


@pytest.fixture()
def evidence_store() -> EvidenceStore:
    return EvidenceStore()


def _client(
    scan_store: ScanStore,
    event_store: InMemoryCIEventStore,
    evidence_store: EvidenceStore,
    settings: Settings,
) -> TestClient:
    main.app.dependency_overrides[get_store] = lambda: scan_store
    main.app.dependency_overrides[get_event_store] = lambda: event_store
    main.app.dependency_overrides[get_evidence_store] = lambda: evidence_store
    main.app.dependency_overrides[main.get_settings] = lambda: settings
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture()
def client(scan_store, event_store, evidence_store):
    """Fresh TestClient with isolated stores and default (secret-less) settings."""
    with _client(scan_store, event_store, evidence_store, _base_settings()) as c:
        yield c
    main.app.dependency_overrides.clear()


@pytest.fixture()
def client_with_secret(scan_store, event_store, evidence_store):
    """TestClient whose webhook receiver requires an HMAC signature."""
    settings = _base_settings(ci_webhook_secret="test-secret-123")
    with _client(scan_store, event_store, evidence_store, settings) as c:
        yield c
    main.app.dependency_overrides.clear()


@pytest.fixture()
def client_with_allowlist(scan_store, event_store, evidence_store):
    """TestClient that only allows a specific target host."""
    settings = _base_settings(target_host_allowlist=["allowed.test"])
    with _client(scan_store, event_store, evidence_store, settings) as c:
        yield c
    main.app.dependency_overrides.clear()


@pytest.fixture()
def client_with_artifacts(scan_store, event_store, evidence_store):
    """TestClient whose artifact root is the test fixtures directory."""
    settings = _base_settings(artifacts_dir=FIXTURES_DIR)
    with _client(scan_store, event_store, evidence_store, settings) as c:
        yield c
    main.app.dependency_overrides.clear()
