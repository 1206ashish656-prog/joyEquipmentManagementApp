"""
One-off diagnostic (not a formal test case): reuses the session already
saved by run.py to inspect why "Device Information" isn't clickable after
clicking "Equipment Management", without needing a human for CAPTCHA again.

Saves HTML/screenshots at each step to data/discovery_output/ for
inspection, and dumps info about every element matching "Device
Information" text (visibility, bounding box, outer HTML).

Run:
    python -m tests.tc_001_target_connection.diagnose_nav
"""
from __future__ import annotations

import asyncio
import dataclasses
import json

from monitoring.browser_manager import BrowserManager
from monitoring.config import PROJECT_ROOT, load_settings
from monitoring.target_client import TargetApplicationClient

OUT = PROJECT_ROOT / "data" / "discovery_output"


async def main() -> None:
    settings = load_settings()
    # Force headless — session is already authenticated, no human needed.
    settings = dataclasses.replace(settings, headless=True)
    OUT.mkdir(parents=True, exist_ok=True)

    bm = BrowserManager(settings.storage_state_path, headless=True)
    page = await bm.start()
    client = TargetApplicationClient(page, settings)

    try:
        if not await client.is_session_valid():
            print("FAIL: saved session is not valid anymore. Re-run tests.tc_001_target_connection.run first.")
            return

        (OUT / "dashboard.html").write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(OUT / "dashboard.png"), full_page=True)
        print("Saved dashboard.html / dashboard.png")

        try:
            await page.click("text=Equipment Management", timeout=8000)
            print("Clicked 'Equipment Management' OK")
        except Exception as e:
            print(f"Click 'Equipment Management' FAILED: {e}")

        await page.wait_for_timeout(1000)  # let any expand animation finish

        (OUT / "after_equipment_click.html").write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(OUT / "after_equipment_click.png"), full_page=True)
        print("Saved after_equipment_click.html / .png")

        locator = page.get_by_text("Device Information", exact=False)
        count = await locator.count()
        print(f"Elements matching text 'Device Information': {count}")

        details = []
        for i in range(count):
            el = locator.nth(i)
            try:
                visible = await el.is_visible()
                box = await el.bounding_box()
                outer_html = await el.evaluate("e => e.outerHTML")
                parent_html = await el.evaluate("e => e.parentElement ? e.parentElement.outerHTML : null")
                details.append({
                    "index": i,
                    "visible": visible,
                    "bounding_box": box,
                    "outer_html": outer_html[:500],
                    "parent_outer_html": (parent_html or "")[:800],
                })
            except Exception as e:
                details.append({"index": i, "error": str(e)})

        (OUT / "device_information_matches.json").write_text(json.dumps(details, indent=2), encoding="utf-8")
        print(f"Saved match details to {OUT / 'device_information_matches.json'}")
        for d in details:
            print(json.dumps(d, indent=2)[:1000])

        # We found a direct href for Device Information in the sidebar markup
        # above (/NgsEmfuaOv.php/device/device?ref=addtabs) — navigate there
        # directly rather than fighting the slide-toggle submenu animation.
        device_url = f"{settings.target_base_url}/NgsEmfuaOv.php/device/device?ref=addtabs"
        await page.goto(device_url, wait_until="networkidle", timeout=20000)
        await page.wait_for_timeout(1000)
        (OUT / "device_information.html").write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(OUT / "device_information.png"), full_page=True)
        print(f"Saved device_information.html / .png (navigated directly to {device_url})")

        table_count = await page.locator("table").count()
        print(f"<table> elements on page: {table_count}")

    finally:
        await bm.close()


if __name__ == "__main__":
    asyncio.run(main())
