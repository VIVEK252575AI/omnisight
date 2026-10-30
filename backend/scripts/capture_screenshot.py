import sys
from playwright.sync_api import sync_playwright

def main():
    url = sys.argv[1]
    out = sys.argv[2]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        response = page.goto(url, wait_until="networkidle", timeout=30000)
        page.screenshot(path=out, full_page=True)
        title = page.title()
        final_url = page.url
        status = response.status if response else None
        browser.close()
    import json
    print(json.dumps({"title": title, "final_url": final_url, "http_status": status}))

if __name__ == "__main__":
    main()
