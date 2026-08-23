"""
Phase 1 discovery tool (project spec section "Phase 1 — Target Application
Discovery"). This is a one-off, human-driven exploration script — NOT part
of the monitoring worker.

Run:
    python -m discovery.inspect

It opens a VISIBLE Chromium window at the login page. You log in yourself
(including the CAPTCHA — never automated here) and navigate to
Equipment Management -> Device Information yourself, since the real
selectors aren't confirmed yet. Once that page is loaded, press Enter in
this terminal. The script then saves, under data/discovery_output/:

  device_information.html   full page HTML (to find real table structure)
  device_information.png    full-page screenshot
  network_log.jsonl         captured XHR/fetch responses (to check for a
                             structured data endpoint, per requirement #15)

...and saves the authenticated session to data/storage_state/session.json
so monitoring/poc_runner.py can reuse it without logging in again.

Use the output to fill in the TODO_DISCOVERY items in monitoring/selectors.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

from monitoring.config import PROJECT_ROOT, load_settings

OUTPUT_DIR = PROJECT_ROOT / "data" / "discovery_output"


def main() -> None:
    settings = load_settings()
    if not settings.target_login_url:
        raise SystemExit("TARGET_LOGIN_URL is not set. Copy .env.example to .env and fill it in.")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    captured_requests: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        def on_response(response) -> None:
            try:
                request = response.request
                if request.resource_type not in ("xhr", "fetch"):
                    return
                content_type = response.headers.get("content-type", "")
                entry = {
                    "url": response.url,
                    "method": request.method,
                    "status": response.status,
                    "content_type": content_type,
                    "post_data": request.post_data,
                }
                if "json" in content_type:
                    try:
                        entry["body"] = response.json()
                    except Exception:
                        entry["body_text"] = response.text()[:5000]
                captured_requests.append(entry)
            except Exception as e:  # never let logging break the session
                captured_requests.append({"error": str(e)})

        page.on("response", on_response)

        print(f"Opening login page: {settings.target_login_url}")
        page.goto(settings.target_login_url, wait_until="domcontentloaded")

        print("\n" + "=" * 70)
        print("MANUAL STEP — in the browser window that just opened:")
        print("  1. Log in (including the CAPTCHA). Never automated here.")
        print("  2. Navigate to Equipment Management -> Device Information.")
        print("  3. Wait until the equipment table is fully loaded.")
        print("=" * 70)
        input("Press Enter here once the Device Information table is visible... ")

        html = page.content()
        (OUTPUT_DIR / "device_information.html").write_text(html, encoding="utf-8")
        page.screenshot(path=str(OUTPUT_DIR / "device_information.png"), full_page=True)

        with (OUTPUT_DIR / "network_log.jsonl").open("w", encoding="utf-8") as f:
            for entry in captured_requests:
                f.write(json.dumps(entry, default=str) + "\n")

        settings.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(settings.storage_state_path))

        summary = "\n".join([
            "# Target Application Integration Spec — Discovery Notes",
            f"Captured: {datetime.now(timezone.utc).isoformat()}",
            "",
            f"- Final URL: {page.url}",
            f"- Page title: {page.title()}",
            f"- XHR/fetch requests captured: {len(captured_requests)}",
            "- Saved HTML: data/discovery_output/device_information.html",
            "- Saved screenshot: data/discovery_output/device_information.png",
            "- Saved network log: data/discovery_output/network_log.jsonl",
            f"- Saved session (cookies/localStorage): {settings.storage_state_path}",
            "",
            "## Next steps",
            "1. Open device_information.html and check whether the equipment",
            "   table is server-rendered or populated via JS.",
            "2. Check network_log.jsonl for a JSON response containing",
            "   equipment records (id/status/fault type/etc.) — if found,",
            "   prefer it over DOM scraping (requirement #15).",
            "3. Update monitoring/selectors.py TODO_DISCOVERY items with",
            "   confirmed selectors/structure.",
            "4. Re-run `python -m monitoring.poc_runner` — it will reuse the",
            "   session saved just now instead of logging in again.",
        ])
        (OUTPUT_DIR / "summary.md").write_text(summary, encoding="utf-8")

        print("\nSaved discovery output to:", OUTPUT_DIR)
        print("Saved session state to:", settings.storage_state_path)

        browser.close()


if __name__ == "__main__":
    main()
