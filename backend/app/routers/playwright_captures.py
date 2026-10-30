from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, HttpUrl
from playwright.async_api import async_playwright


# ---------------------------------------------------------
# Router configuration
# ---------------------------------------------------------

router = APIRouter(
    prefix="/api/v1/scans",
    tags=["playwright"]
)


# ---------------------------------------------------------
# Screenshot storage
# ---------------------------------------------------------

SCREENSHOT_DIR = Path("artifacts/playwright")
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------
# Request model
# ---------------------------------------------------------

class CaptureRequest(BaseModel):
    url: HttpUrl


# ---------------------------------------------------------
# Playwright screenshot endpoint
# ---------------------------------------------------------

@router.post("/{scan_id}/playwright-capture")
async def playwright_capture(
    scan_id: str,
    payload: CaptureRequest
):
    """
    Opens the target website using Playwright and captures
    a full-page screenshot before VLM detection.

    Workflow:
    React Frontend
        -> FastAPI
        -> Playwright
        -> Screenshot
        -> VLM
    """

    # Sanitize scan ID before using it as a filename
    safe_scan_id = "".join(
        char for char in scan_id
        if char.isalnum() or char in "-_"
    )

    if not safe_scan_id:
        raise HTTPException(
            status_code=400,
            detail="Invalid scan ID."
        )

    screenshot_path = (
        SCREENSHOT_DIR / f"{safe_scan_id}.png"
    )

    try:

        # Start Playwright
        async with async_playwright() as playwright:

            # Launch Chromium browser
            browser = await playwright.chromium.launch(
                headless=True
            )

            # Create browser page
            page = await browser.new_page(
                viewport={
                    "width": 1440,
                    "height": 900
                }
            )

            # Open target website
            response = await page.goto(
                str(payload.url),
                wait_until="networkidle",
                timeout=30000
            )

            # Get page information
            page_title = await page.title()
            final_url = page.url

            # Capture full-page screenshot
            await page.screenshot(
                path=str(screenshot_path),
                full_page=True
            )

            # Close browser
            await browser.close()

    except Exception as error:

        raise HTTPException(
            status_code=502,
            detail=f"Playwright capture failed: {error}"
        ) from error


    # -----------------------------------------------------
    # Return screenshot information
    # -----------------------------------------------------

    return {

        "scan_id": scan_id,

        "stage": "playwright_complete",

        "target_url": str(payload.url),

        "final_url": final_url,

        "page_title": page_title,

        "http_status": (
            response.status
            if response
            else None
        ),

        "screenshot_path": str(screenshot_path),

        "next_stage":
            f"/api/v1/scans/{scan_id}/detect"
    }
