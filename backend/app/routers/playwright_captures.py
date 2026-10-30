"""Playwright screenshot capture endpoint (async subprocess for Windows)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, HttpUrl

from app.storage import ScanStore, get_store
from app.evidence_storage import EvidenceStore, get_evidence_store
from app.schemas import Observation, Viewport, EvidenceRecord, ScanStatus
from app.ids import new_uuid, utcnow

router = APIRouter(prefix="/api/v1/scans", tags=["playwright"])

SCREENSHOT_DIR = Path("artifacts/playwright")
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


class CaptureRequest(BaseModel):
    url: HttpUrl


@router.post("/{scan_id}/playwright-capture")
async def playwright_capture(scan_id: str, payload: CaptureRequest, scan_store: ScanStore = Depends(get_store), evidence_store: EvidenceStore = Depends(get_evidence_store)):
    safe_scan_id = "".join(c for c in scan_id if c.isalnum() or c in "-_")
    if not safe_scan_id:
        raise HTTPException(status_code=400, detail="Invalid scan ID.")
    screenshot_path = SCREENSHOT_DIR / f"{safe_scan_id}.png"
    script = Path(__file__).resolve().parents[2] / "scripts" / "capture_screenshot.py"
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script), str(payload.url), str(screenshot_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Playwright capture failed: {type(error).__name__}: {error}") from error
    if proc.returncode != 0:
        raise HTTPException(status_code=502, detail=f"Playwright capture failed: {stderr.decode().strip() or stdout.decode().strip()}")
    try:
        info = json.loads(stdout.decode().strip().splitlines()[-1])
    except Exception:
        info = {}
    # store an in-memory evidence record so detection can run
    try:
        from uuid import UUID
        sid = UUID(scan_id)
        obs = Observation(url=payload.url, viewport=Viewport(width=1440, height=900), screenshot=f"playwright/{safe_scan_id}.png", elements=[], captured_at=utcnow())
        ev = EvidenceRecord(evidence_id=new_uuid(), scan_id=sid, observation=obs, screenshot_artifact_id=new_uuid(), created_at=utcnow())
        evidence_store.add(ev, artifact_id=ev.screenshot_artifact_id, artifact_path=screenshot_path)
    except Exception:
        pass
    try:
        from uuid import UUID
        sid = UUID(scan_id)
        scan = scan_store.get(sid)
        if scan is not None and scan.status.value == "created":
            scan_store.compare_and_transition(
                sid,
                expected=frozenset({ScanStatus.created}),
                to_status=ScanStatus.evidence_collection,
                error=None,
            )
    except Exception:
        pass
    return {
        "scan_id": scan_id,
        "stage": "playwright_complete",
        "target_url": str(payload.url),
        "final_url": info.get("final_url", str(payload.url)),
        "page_title": info.get("title"),
        "http_status": info.get("http_status"),
        "screenshot_path": str(screenshot_path),
        "next_stage": f"/api/v1/scans/{scan_id}/detect",
    }
