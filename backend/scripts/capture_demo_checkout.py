"""Run the fixed local checkout flow and write observation/v1 evidence."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

DEFAULT_BASE_URL = "http://127.0.0.1:4173"
VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "mobile": {"width": 390, "height": 844},
}
DEFAULT_FLOW_VIEWPORTS = [VIEWPORTS["mobile"], VIEWPORTS["desktop"]]
ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_ROOT = ROOT / "backend" / "artifacts"


def _base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise argparse.ArgumentTypeError(
            "this demo runner only permits an HTTP loopback URL"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError("URL credentials, query, and fragment are not allowed")
    return value.rstrip("/")


def _visible_element(page, selector: str, viewport: dict[str, int]) -> dict | None:
    locator = page.locator(selector).first
    if locator.count() == 0 or not locator.is_visible():
        return None
    box = locator.bounding_box()
    if box is None:
        return None
    return {
        "selector": selector,
        "tag": locator.evaluate("element => element.tagName.toLowerCase()"),
        "text": locator.inner_text(timeout=1000)[:500],
        "visible": True,
        "in_viewport": (
            box["x"] >= 0
            and box["y"] >= 0
            and box["x"] + box["width"] <= viewport["width"]
            and box["y"] + box["height"] <= viewport["height"]
        ),
        "box": {
            "x": box["x"],
            "y": box["y"],
            "w": box["width"],
            "h": box["height"],
        },
    }


def parse_viewports(raw: str) -> list[dict[str, int]]:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise ValueError("flow viewports must be valid JSON") from None
    if not isinstance(value, list) or len(value) != 2 or not all(isinstance(item, dict) for item in value):
        raise ValueError("flow must define exactly two viewports")
    mobile, desktop = value
    for viewport in value:
        if set(viewport) != {"width", "height"}:
            raise ValueError("viewport definitions contain unsupported fields")
        if any(type(viewport[key]) is not int for key in ("width", "height")):
            raise ValueError("viewport dimensions must be integers")
        if not 320 <= viewport["width"] <= 1920 or not 480 <= viewport["height"] <= 2160:
            raise ValueError("viewport dimensions exceed the supported range")
    if mobile["width"] > 600 or desktop["width"] < 800:
        raise ValueError("flow viewports must be ordered mobile first, then desktop")
    return [mobile, desktop]


def capture(
    base_url: str, artifact_root: Path, expected: str,
    viewports: list[dict[str, int]] | None = None,
) -> dict:
    configured = parse_viewports(json.dumps(DEFAULT_FLOW_VIEWPORTS if viewports is None else viewports))
    active_viewports = {"desktop": configured[1], "mobile": configured[0]}
    run_dir = artifact_root / "demo-checkout"
    run_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}
    captured_at = datetime.now(timezone.utc).isoformat()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for viewport_name, viewport in active_viewports.items():
                context = browser.new_context(viewport=viewport, device_scale_factor=1)
                page = context.new_page()
                console_errors: list[dict] = []
                page_errors: list[dict] = []
                page.on(
                    "console",
                    lambda message: console_errors.append(
                        {"message": message.text[:1000], "type": message.type}
                    )
                    if message.type == "error" and len(console_errors) < 20
                    else None,
                )
                page.on(
                    "pageerror",
                    lambda error: page_errors.append(
                        {"message": str(error)[:1000], "type": "pageerror"}
                    )
                    if len(page_errors) < 20
                    else None,
                )

                response = page.goto(
                    f"{base_url}/checkout",
                    wait_until="networkidle",
                    timeout=10_000,
                )
                if response is None or response.status >= 400:
                    raise RuntimeError("demo checkout did not return a successful response")
                page.get_by_role("heading", name="Checkout", exact=True).wait_for(
                    state="visible", timeout=5_000
                )

                button = page.locator("#place-order")
                box = button.bounding_box()
                if box is None:
                    raise RuntimeError("place-order control has no visible bounding box")
                overflow_px = max(
                    0.0, box["x"] + box["width"] - viewport["width"]
                )

                evidence_dir = run_dir / viewport_name
                evidence_dir.mkdir(parents=True, exist_ok=True)
                screenshot = evidence_dir / "checkout.png"
                page.screenshot(path=str(screenshot), full_page=False)

                elements = [
                    item
                    for selector in (
                        "h1",
                        "#place-order",
                        "[data-testid='checkout-summary']",
                    )
                    if (item := _visible_element(page, selector, viewport)) is not None
                ]
                observation = {
                    "schema_version": "observation/v1",
                    "url": page.url,
                    "route": "/checkout",
                    "state": "checkout-before-submit",
                    "viewport": viewport,
                    "screenshot": screenshot.relative_to(artifact_root).as_posix(),
                    "elements": elements,
                    "console_errors": console_errors,
                    "network_errors": [],
                    "captured_at": captured_at,
                }
                (evidence_dir / "observation.json").write_text(
                    json.dumps(observation, indent=2), encoding="utf-8"
                )

                # The action is part of the fixed demo flow; no flow data can
                # inject arbitrary JavaScript or browser commands.
                page.get_by_role("button", name="Place demo order · $48.00").click(
                    timeout=5_000
                )
                page.get_by_role("status").get_by_text(
                    "No payment was collected", exact=False
                ).wait_for(state="visible", timeout=5_000)

                results[viewport_name] = {
                    "viewport": viewport,
                    "button_overflow_px": round(overflow_px, 2),
                    "order_confirmation": page.get_by_role("status").inner_text(),
                    "screenshot": observation["screenshot"],
                    "observation": (
                        evidence_dir / "observation.json"
                    ).relative_to(artifact_root).as_posix(),
                    "console_error_count": len(console_errors) + len(page_errors),
                }
                context.close()
        finally:
            browser.close()

    mobile_overflow = results["mobile"]["button_overflow_px"] > 0
    results_summary = {
        "flow": "demo-checkout-v1",
        "target": f"{base_url}/checkout",
        "expected": expected,
        "matches_expected": mobile_overflow == (expected == "overflow"),
        "viewports": results,
        "artifact_root": str(artifact_root),
    }
    (run_dir / "run-summary.json").write_text(
        json.dumps(results_summary, indent=2), encoding="utf-8"
    )
    return results_summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", type=_base_url, default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--artifacts",
        type=Path,
        default=ARTIFACT_ROOT,
        help="artifact root for screenshots and observation/v1 JSON",
    )
    parser.add_argument(
        "--expected",
        choices=("overflow", "no-overflow"),
        default="overflow",
        help="expected mobile button state; use no-overflow after the fix",
    )
    parser.add_argument("--viewports-json", default=json.dumps(DEFAULT_FLOW_VIEWPORTS))
    args = parser.parse_args()
    try:
        result = capture(
            args.base_url, args.artifacts.resolve(), args.expected,
            parse_viewports(args.viewports_json),
        )
    except Exception as exc:
        print(f"Demo flow failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0 if result["matches_expected"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
