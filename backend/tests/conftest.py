"""Shared pytest fixtures: fresh stores and settings per test."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.action_engine import StubPatchExtractor
from app.ai.adapters import MockVisionAnalyzer
from app.ai.adapters.base import VisionAnalyzer
from app.ai.adapters.patch import MockPatchGenerator
from app.config import Settings
from app.detection_storage import DetectionStore, get_detection_store
from app.evidence_storage import EvidenceStore, get_evidence_store
from app.event_storage import InMemoryCIEventStore, get_event_store
from app.patch_storage import PatchStore, get_patch_store
from app.routers import detection, patches
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


@pytest.fixture()
def detection_store() -> DetectionStore:
    """Fresh detection store — the module singleton is process-wide, so every
    test must get its own or findings would leak between tests."""
    return DetectionStore()


@pytest.fixture()
def patch_store() -> PatchStore:
    """Fresh patch store — same process-wide-singleton reasoning as above."""
    return PatchStore()


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


@pytest.fixture()
def detect_client(scan_store, event_store, evidence_store, detection_store):
    """Factory for a TestClient with a caller-supplied VLM adapter.

    Always wires fixture artifacts and a fresh detection store, and overrides
    the detection adapter dependency so a test can drive any mock scenario (or
    a teammate-supplied callable) without touching global state.
    """
    def _build(
        analyzer: VisionAnalyzer | None = None,
        *,
        scenario: str = "clipped_button",
        settings: Settings | None = None,
    ) -> TestClient:
        if settings is None:
            settings = _base_settings(artifacts_dir=FIXTURES_DIR)
        if analyzer is None:
            analyzer = MockVisionAnalyzer(
                scenario, resolver=evidence_store.get_artifact
            )
        main.app.dependency_overrides[get_store] = lambda: scan_store
        main.app.dependency_overrides[get_event_store] = lambda: event_store
        main.app.dependency_overrides[get_evidence_store] = lambda: evidence_store
        main.app.dependency_overrides[get_detection_store] = lambda: detection_store
        main.app.dependency_overrides[main.get_settings] = lambda: settings
        main.app.dependency_overrides[detection.get_detection_analyzer] = (
            lambda: analyzer
        )
        return TestClient(main.app, raise_server_exceptions=False)

    yield _build
    main.app.dependency_overrides.clear()


@pytest.fixture()
def patch_client(scan_store, event_store, evidence_store, detection_store, patch_store):
    """Factory for a TestClient wired to the W2.5 action engine.

    Every dependency the action engine uses is overridden with a fresh instance
    so proposal records and finding status can never leak between tests. The
    extractor defaults to the production stub (which rejects with
    ``extractor_not_implemented``); tests that need the ``proposed`` path inject
    a labelled test-double extractor.
    """
    def _build(
        *,
        generator=None,
        extractor=None,
        settings: Settings | None = None,
    ) -> TestClient:
        if settings is None:
            settings = _base_settings()
        if generator is None:
            generator = MockPatchGenerator(settings.patch_mock_scenario)
        if extractor is None:
            extractor = StubPatchExtractor()
        main.app.dependency_overrides[get_store] = lambda: scan_store
        main.app.dependency_overrides[get_event_store] = lambda: event_store
        main.app.dependency_overrides[get_evidence_store] = lambda: evidence_store
        main.app.dependency_overrides[get_detection_store] = lambda: detection_store
        main.app.dependency_overrides[get_patch_store] = lambda: patch_store
        main.app.dependency_overrides[main.get_settings] = lambda: settings
        main.app.dependency_overrides[patches.get_patch_generator] = lambda: generator
        main.app.dependency_overrides[patches.get_patch_extractor] = lambda: extractor
        return TestClient(main.app, raise_server_exceptions=False)

    yield _build
    main.app.dependency_overrides.clear()
