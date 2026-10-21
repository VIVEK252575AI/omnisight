"""Exercise the real dashboard through independent, persisted ShopLite runs."""

from __future__ import annotations

import argparse
import json
import re
import sys
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


def _loopback_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise argparse.ArgumentTypeError("dashboard verification only permits an HTTP loopback URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError("URL credentials, query, and fragment are not allowed")
    return value.rstrip("/")


def _start_and_verify_run(page, *, start_button: str | re.Pattern[str], timeout_ms: int) -> str:
    page.get_by_role("button", name=start_button).click()
    page.get_by_role("heading", name="ShopLite live demo").wait_for(timeout=timeout_ms)
    run_status = page.locator(".live-run-status .badge").first
    run_status.wait_for(timeout=timeout_ms)
    page.get_by_role("button", name="Approve and validate").wait_for(timeout=timeout_ms)
    if not page.get_by_text("Review exact source diff").is_visible():
        raise RuntimeError("The dashboard did not show the exact patch diff for review")

    run_line = page.locator(".live-run-status .panel__sub").inner_text()
    run_id_match = re.search(r"Run (demo_[0-9a-f]{32})", run_line)
    if run_id_match is None:
        raise RuntimeError("The dashboard did not show a valid persisted run identifier")
    run_id = run_id_match.group(1)

    page.locator("#live-demo-confirm").fill("approve-demo-only")
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Approve and validate").click()
    run_status.get_by_text("Verified", exact=True).wait_for(timeout=timeout_ms)
    page.get_by_text("Sandbox verification", exact=True).wait_for(timeout=timeout_ms)
    page.get_by_text(re.compile(r"\d+px\s*→\s*0px")).wait_for(timeout=timeout_ms)
    return run_id


def run(base_url: str, timeout_ms: int) -> dict:
    browser_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        response = page.goto(base_url + "/", wait_until="domcontentloaded")
        if response is None or response.status != 200:
            raise RuntimeError("The OmniSight dashboard did not load successfully")

        page.get_by_role("button", name="Settings").click()
        page.get_by_text("Project and test flow", exact=True).wait_for(timeout=timeout_ms)
        page.get_by_text("OmniSight ShopLite Demo", exact=True).wait_for(timeout=timeout_ms)
        page.get_by_text("ShopLite checkout visual QA", exact=True).wait_for(timeout=timeout_ms)
        page.get_by_text("Definition SHA-256", exact=False).wait_for(timeout=timeout_ms)
        page.get_by_role("button", name="Live ShopLite Demo").click()

        run_id = _start_and_verify_run(
            page, start_button=re.compile("Start ShopLite scan"), timeout_ms=timeout_ms
        )

        page.reload(wait_until="domcontentloaded")
        page.get_by_role("button", name=re.compile("History")).click()
        history_row = page.locator(".hist-row").filter(has_text=run_id)
        history_row.wait_for(timeout=timeout_ms)
        history_row.get_by_text("verified", exact=True).wait_for(timeout=timeout_ms)

        page.get_by_role("button", name="Live ShopLite Demo").click()
        second_run_id = _start_and_verify_run(
            page, start_button="Start real scan", timeout_ms=timeout_ms
        )
        if second_run_id == run_id:
            raise RuntimeError("The rerun reused the previous run identifier")

        page.reload(wait_until="domcontentloaded")
        page.get_by_role("button", name=re.compile("History")).click()
        for completed_run_id in (run_id, second_run_id):
            saved_row = page.locator(".hist-row").filter(has_text=completed_run_id)
            saved_row.wait_for(timeout=timeout_ms)
            saved_row.get_by_text("verified", exact=True).wait_for(timeout=timeout_ms)

        if browser_errors:
            raise RuntimeError("Dashboard JavaScript error: " + browser_errors[0][:300])
        result = {
            "status": "verified",
            "run_ids": [run_id, second_run_id],
            "ui_reviewed_patch_diff": True,
            "ui_approved_sandbox_runs": 2,
            "independent_runs_survived_refresh": 2,
            "persisted_workspace_reviewed": True,
            "browser_error_count": len(browser_errors),
        }
        browser.close()
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", type=_loopback_url, default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=int, default=180_000)
    args = parser.parse_args()
    if args.timeout < 1_000 or args.timeout > 300_000:
        parser.error("--timeout must be between 1000 and 300000 milliseconds")
    try:
        result = run(args.base_url, args.timeout)
    except Exception as exc:
        print(f"Dashboard workflow verification failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
