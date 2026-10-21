"""Trusted ShopLite-only Playwright harness; candidate files remain read-only."""

from __future__ import annotations

import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

WORKSPACE = Path("/workspace")
ORIGIN = "http://127.0.0.1:4173"
VIEWPORTS = {"mobile": {"width": 390, "height": 844}, "desktop": {"width": 1440, "height": 900}}


class QuietHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WORKSPACE), **kwargs)

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> int:
    stage = "start_server"
    server = ThreadingHTTPServer(("127.0.0.1", 4173), QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    report: dict = {"passed": False, "checks": {}}
    errors: list[str] = []
    blocked_requests: list[str] = []
    try:
        with sync_playwright() as playwright:
            stage = "launch_browser"
            browser = playwright.chromium.launch(
                headless=True,
                args=["--disable-background-networking", "--disable-component-update"],
            )
            try:
                results: dict[str, float] = {}
                for tree in ("baseline", "candidate"):
                    for device, viewport in VIEWPORTS.items():
                        stage = f"load_{tree}_{device}"
                        context = browser.new_context(viewport=viewport)

                        def allow_local_only(route) -> None:
                            if route.request.url.startswith(ORIGIN + "/"):
                                route.continue_()
                            else:
                                blocked_requests.append(route.request.url[:300])
                                route.abort()

                        context.route("**/*", allow_local_only)
                        page = context.new_page()
                        page.on("pageerror", lambda error: errors.append(str(error)[:300]))
                        page.on(
                            "console",
                            lambda message: errors.append(message.text[:300])
                            if message.type == "error"
                            else None,
                        )
                        response = page.goto(
                            f"{ORIGIN}/{tree}/checkout/",
                            wait_until="networkidle",
                            timeout=8_000,
                        )
                        if response is None or response.status >= 400:
                            raise RuntimeError("checkout page did not load")
                        page.get_by_role("heading", name="Checkout", exact=True).wait_for()
                        button = page.locator("#place-order")
                        box = button.bounding_box()
                        if box is None:
                            raise RuntimeError("checkout action button is missing")
                        results[f"{tree}_{device}_overflow_px"] = round(
                            max(0.0, box["x"] + box["width"] - viewport["width"]), 2
                        )
                        if tree == "candidate" and device == "mobile":
                            stage = "verify_order_confirmation"
                            button.click(timeout=2_000)
                            page.get_by_role("status").get_by_text(
                                "No payment was collected", exact=False
                            ).wait_for(state="visible", timeout=2_000)
                        context.close()
                report["checks"] = {
                    "baseline_mobile_overflow_px": results["baseline_mobile_overflow_px"],
                    "patched_mobile_overflow_px": results["candidate_mobile_overflow_px"],
                    "patched_desktop_overflow_px": results["candidate_desktop_overflow_px"],
                    "order_confirmation": True,
                    "browser_error_count": len(errors),
                    "blocked_external_request_count": len(blocked_requests),
                }
                report["passed"] = (
                    report["checks"]["baseline_mobile_overflow_px"] > 0
                    and report["checks"]["patched_mobile_overflow_px"] <= 1
                    and report["checks"]["patched_desktop_overflow_px"] <= 1
                    and report["checks"]["order_confirmation"]
                    and not errors
                    and not blocked_requests
                )
                report["blocked_external_requests"] = len(blocked_requests)
            finally:
                browser.close()
    except Exception as exc:
        # Do not echo candidate source or stack traces into the API response.
        report["error"] = type(exc).__name__
        report["stage"] = stage
        report["passed"] = False
    finally:
        server.shutdown()
        server.server_close()
    print(json.dumps(report, separators=(",", ":")))
    # A completed failed check is still a valid, structured sandbox result.
    return 0 if "checks" in report else 1


if __name__ == "__main__":
    raise SystemExit(main())
