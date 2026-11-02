from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from app import main
from app.config import Settings
from app.routers import playwright_captures
from app.schemas import ScanStatus


class FakePage:
    url = "https://example.com/"

    def on(self, *_args, **_kwargs):
        pass

    async def goto(self, *_args, **_kwargs):
        return SimpleNamespace(status=200)

    async def wait_for_timeout(self, *_args):
        pass

    async def evaluate(self, *_args):
        return []

    async def title(self):
        return "Example page"

    async def screenshot(self, *, path, **_kwargs):
        Path(path).write_bytes(b"\xff\xd8\xff" + b"fake-jpeg-content")


class FakeContext:
    async def new_page(self):
        return FakePage()

    async def close(self):
        pass


class FakeBrowser:
    async def new_context(self, **_kwargs):
        return FakeContext()

    async def close(self):
        pass


class FakePlaywrightManager:
    async def __aenter__(self):
        chromium = SimpleNamespace(launch=lambda **_kwargs: _launch_browser())
        self.playwright = SimpleNamespace(chromium=chromium)
        return self.playwright

    async def __aexit__(self, *_args):
        return False


async def _launch_browser():
    return FakeBrowser()


def test_playwright_capture_persists_evidence_and_advances_scan(
    client, scan_store, evidence_store, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        playwright_captures, "async_playwright", FakePlaywrightManager
    )
    main.app.dependency_overrides[main.get_settings] = lambda: Settings(
        artifacts_dir=tmp_path,
        artifact_max_image_bytes=1024 * 1024,
    )

    created = client.post(
        "/api/scans",
        json={"target_url": "https://example.com/", "viewports": [{"width": 390, "height": 844}]},
    )
    assert created.status_code == 201
    scan_id = created.json()["scan_id"]

    response = client.post(
        f"/api/v1/scans/{scan_id}/playwright-capture",
        json={"url": "https://example.com/"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["evidence_count"] == 1
    assert body["page_title"] == "Example page"
    assert len(evidence_store.list_for_scan(UUID(scan_id))) == 1
    assert scan_store.get(UUID(scan_id)).status is ScanStatus.evidence_collection
    assert (tmp_path / body["screens"][0]["screenshot_path"]).is_file()


def test_playwright_capture_rejects_url_origin_mismatch(client):
    created = client.post(
        "/api/scans", json={"target_url": "https://example.com/"}
    )
    scan_id = created.json()["scan_id"]

    response = client.post(
        f"/api/v1/scans/{scan_id}/playwright-capture",
        json={"url": "https://different.example/"},
    )

    assert response.status_code == 400
    assert "origin must match" in response.json()["error"]["message"]
