"""Playwright capture endpoint that persists evidence for the VLM pipeline.

The endpoint is intentionally part of the normal scan lifecycle:

    created -> Playwright capture -> evidence_collection -> VLM detection

Screenshots are written beneath the configured private artifact root and
registered in EvidenceStore. The VLM adapter therefore receives opaque
artifact IDs through the existing evidence contract; it never reads a
frontend-local file path.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from pydantic import BaseModel, HttpUrl, Field

from app.artifacts import artifact_root
from app.config import Settings, get_settings
from app.evidence_storage import EvidenceStore, get_evidence_store
from app.ids import new_uuid, utcnow
from app.schemas import EvidenceRecord, Observation, ScanStatus, Viewport
from app.storage import ScanStore, get_store

logger = logging.getLogger("omnisight.playwright")
router = APIRouter(prefix="/api/v1/scans", tags=["playwright"])


class CaptureRequest(BaseModel):
    url: HttpUrl
    viewports: list[Viewport] | None = Field(default=None, max_length=4)


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or default_port


async def _collect_dom(page) -> list[dict]:
    """Collect bounded, inert layout metadata; page content is never executed by OmniSight."""
    return await page.evaluate("""() => {
      const nodes = [...document.querySelectorAll(
        'main,header,footer,nav,section,article,h1,h2,h3,p,button,a,input,textarea,select,img,[role]'
      )].slice(0, 1200);
      const selectorFor = (el) => {
        if (el.id) return `#${CSS.escape(el.id)}`;
        const testId = el.getAttribute('data-testid');
        if (testId) return `[data-testid="${CSS.escape(testId)}"]`;
        const parts = [];
        let node = el;
        while (node && node.nodeType === 1 && parts.length < 5) {
          let part = node.tagName.toLowerCase();
          if (node.classList && node.classList.length) {
            part += '.' + [...node.classList].slice(0, 2).map(c => CSS.escape(c)).join('.');
          }
          const parent = node.parentElement;
          if (parent) {
            const same = [...parent.children].filter(x => x.tagName === node.tagName);
            if (same.length > 1) part += `:nth-of-type(${same.indexOf(node) + 1})`;
          }
          parts.unshift(part);
          node = parent;
        }
        return parts.join(' > ').slice(0, 500);
      };
      return nodes.map(el => {
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        const visible = style.display !== 'none' && style.visibility !== 'hidden' &&
          Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
        const text = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('alt') || '').trim();
        return {
          selector: selectorFor(el), tag: el.tagName.toLowerCase(),
          text: text.slice(0, 500) || null, visible,
          in_viewport: rect.bottom > 0 && rect.right > 0 &&
            rect.top < window.innerHeight && rect.left < window.innerWidth,
          box: {x: rect.x + window.scrollX, y: rect.y + window.scrollY,
            w: Math.max(0, rect.width), h: Math.max(0, rect.height)},
          computed_style: {
            color: style.color, backgroundColor: style.backgroundColor,
            fontSize: style.fontSize, fontWeight: style.fontWeight,
            lineHeight: style.lineHeight, display: style.display,
            position: style.position, overflow: style.overflow
          }
        };
      });
    }""")


@router.post("/{scan_id}/playwright-capture")
async def playwright_capture(
    scan_id: UUID,
    payload: CaptureRequest,
    settings: Settings = Depends(get_settings),
    scan_store: ScanStore = Depends(get_store),
    evidence_store: EvidenceStore = Depends(get_evidence_store),
):
    """Capture requested viewports and persist them as VLM-readable evidence."""
    scan = scan_store.get(scan_id)
    if scan is None:
        raise HTTPException(status_code=404, detail="Scan not found.")
    if scan.status is not ScanStatus.created:
        raise HTTPException(
            status_code=409,
            detail=f"Playwright capture requires a new scan; current status is {scan.status.value}.",
        )

    target_url = str(payload.url)
    if _origin(target_url) != _origin(str(scan.target_url)):
        raise HTTPException(
            status_code=400,
            detail="Capture URL origin must match the target URL registered for this scan.",
        )

    viewports = payload.viewports or scan.viewports
    if not viewports:
        viewports = [Viewport(width=1440, height=900)]

    root = artifact_root(settings)
    capture_dir = root / "playwright"
    capture_dir.mkdir(parents=True, exist_ok=True)
    captured: list[dict] = []
    page_title = ""
    final_url = target_url
    http_status = None

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                for viewport in viewports:
                    context = await browser.new_context(
                        viewport={"width": viewport.width, "height": viewport.height},
                        device_scale_factor=1,
                    )
                    page = await context.new_page()
                    console_errors: list[dict] = []
                    page_errors: list[dict] = []
                    network_errors: list[dict] = []

                    page.on("console", lambda msg, out=console_errors: out.append({
                        "message": msg.text[:4000], "type": msg.type
                    }) if msg.type == "error" and len(out) < 100 else None)
                    page.on("pageerror", lambda err, out=page_errors: out.append({
                        "message": str(err)[:4000], "type": "pageerror"
                    }) if len(out) < 100 else None)
                    page.on("requestfailed", lambda req, out=network_errors: out.append({
                        "url": req.url[:2000], "method": req.method,
                        "error": (req.failure or "request failed")[:2000],
                        "resource_type": req.resource_type,
                    }) if len(out) < 100 else None)
                    page.on("response", lambda response, out=network_errors: out.append({
                        "url": response.url[:2000], "method": response.request.method,
                        "status": response.status,
                        "resource_type": response.request.resource_type,
                    }) if response.status >= 400 and len(out) < 100 else None)

                    response = await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(700)
                    try:
                        await page.evaluate("() => document.fonts && document.fonts.ready")
                    except PlaywrightError:
                        pass

                    page_title = await page.title()
                    final_url = page.url
                    http_status = response.status if response else None
                    elements = await _collect_dom(page)

                    file_name = f"{scan_id}_{viewport.width}x{viewport.height}.jpg"
                    screenshot_path = capture_dir / file_name
                    await page.screenshot(
                        path=str(screenshot_path), full_page=True,
                        type="jpeg", quality=82, animations="disabled",
                    )
                    if screenshot_path.stat().st_size > settings.artifact_max_image_bytes:
                        screenshot_path.unlink(missing_ok=True)
                        raise HTTPException(
                            status_code=413,
                            detail=("Playwright screenshot exceeds the configured image limit; "
                                    "reduce the page size or increase OMNISIGHT_ARTIFACT_MAX_IMAGE_BYTES."),
                        )

                    relative_path = screenshot_path.relative_to(root).as_posix()
                    observation = Observation(
                        url=final_url,
                        route=urlsplit(final_url).path or "/",
                        state="initial-page-load",
                        viewport=viewport,
                        screenshot=relative_path,
                        elements=elements,
                        console_errors=console_errors[:100],
                        page_errors=page_errors[:100],
                        network_errors=network_errors[:100],
                        captured_at=utcnow(),
                    )
                    artifact_id = new_uuid()
                    evidence_record = EvidenceRecord(
                        evidence_id=new_uuid(),
                        scan_id=scan_id,
                        observation=observation,
                        screenshot_artifact_id=artifact_id,
                        created_at=utcnow(),
                    )
                    evidence_store.add(
                        evidence_record,
                        artifact_id=artifact_id,
                        artifact_path=screenshot_path,
                    )
                    captured.append({
                        "viewport": viewport.model_dump(),
                        "evidence_id": str(evidence_record.evidence_id),
                        "artifact_id": str(artifact_id),
                        "screenshot_url": f"/api/v1/artifacts/{artifact_id}",
                        "screenshot_path": relative_path,
                        "element_count": len(elements),
                    })
                    await context.close()
                await browser.close()
            except Exception:
                await browser.close()
                raise
    except HTTPException:
        raise
    except PlaywrightError as error:
        logger.exception("Playwright capture failed for scan %s", scan_id)
        failed = scan.model_copy(update={
            "status": ScanStatus.failed,
            "error": f"Playwright could not capture the target: {type(error).__name__}",
            "updated_at": utcnow(),
        })
        scan_store.replace(failed)
        raise HTTPException(
            status_code=502,
            detail="Playwright could not open the target. Check that the URL is reachable and Chromium is installed.",
        ) from error
    except Exception as error:
        logger.exception("Evidence persistence failed for scan %s", scan_id)
        failed = scan.model_copy(update={
            "status": ScanStatus.failed,
            "error": "Playwright evidence could not be stored.",
            "updated_at": utcnow(),
        })
        scan_store.replace(failed)
        raise HTTPException(status_code=500, detail="Playwright evidence could not be stored.") from error

    if not captured:
        raise HTTPException(status_code=502, detail="Playwright did not capture any screenshots.")

    scan_store.replace(scan.model_copy(update={
        "status": ScanStatus.evidence_collection,
        "updated_at": utcnow(),
    }))

    return {
        "scan_id": str(scan_id),
        "stage": "playwright_complete",
        "target_url": target_url,
        "final_url": final_url,
        "page_title": page_title,
        "http_status": http_status,
        "screens": captured,
        "screenshot_url": captured[0]["screenshot_url"],
        "screenshot_path": captured[0]["screenshot_path"],
        "evidence_count": len(captured),
        "next_stage": f"/api/v1/scans/{scan_id}/detect",
    }
