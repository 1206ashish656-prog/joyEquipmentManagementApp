"""
One-off script: captures screenshots of the live demo dashboard for the
project report (docs/report/). Not part of the application itself.

Run (with the demo server already up at http://127.0.0.1:8123):
    python -m docs.report.capture_screenshots
"""
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8123"
OUT = Path(__file__).resolve().parent / "assets"
OUT.mkdir(parents=True, exist_ok=True)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})

        await page.goto(f"{BASE}/login", wait_until="networkidle")
        await page.screenshot(path=str(OUT / "01_login.png"))

        await page.fill("input[name='email']", "demo@example.com")
        await page.fill("input[name='password']", "demo-password-123")
        await page.click("button[type='submit']")
        await page.wait_for_load_state("networkidle")
        await page.screenshot(path=str(OUT / "02_dashboard.png"), full_page=True)

        # Find the malfunctioning demo equipment's detail link
        await page.goto(f"{BASE}/", wait_until="networkidle")
        link = page.locator("a", has_text="999").first
        href = await link.get_attribute("href")
        await page.goto(f"{BASE}{href}", wait_until="networkidle")
        await page.screenshot(path=str(OUT / "03_equipment_detail.png"), full_page=True)

        await page.goto(f"{BASE}/faults", wait_until="networkidle")
        await page.screenshot(path=str(OUT / "04_active_faults.png"), full_page=True)

        # First active fault's detail page
        detail_link = page.locator("a[href^='/faults/']").first
        href = await detail_link.get_attribute("href")
        await page.goto(f"{BASE}{href}", wait_until="networkidle")
        await page.screenshot(path=str(OUT / "05_fault_detail.png"), full_page=True)

        await page.goto(f"{BASE}/subscriptions", wait_until="networkidle")
        await page.screenshot(path=str(OUT / "06_subscriptions.png"), full_page=True)

        await page.goto(f"{BASE}/users", wait_until="networkidle")
        await page.screenshot(path=str(OUT / "07_users.png"), full_page=True)

        await browser.close()
        print("Screenshots saved to", OUT)


if __name__ == "__main__":
    asyncio.run(main())
