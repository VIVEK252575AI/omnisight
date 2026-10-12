"""Shared pytest fixtures: a fresh app/store per test module session."""

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import Settings


@pytest.fixture()
def client():
    """Fresh TestClient with an isolated store and per-test settings."""
    main.store = main.ScanStore()  # isolate in-memory state per test
    settings = Settings(
        enable_playwright_worker=False,
        enable_vlm_detection=False,
        enable_patch_engine=False,
        enable_github_delivery=False,
    )
    main.app.dependency_overrides[main.get_settings] = lambda: settings
    with TestClient(main.app, raise_server_exceptions=False) as c:
        yield c
    main.app.dependency_overrides.clear()
