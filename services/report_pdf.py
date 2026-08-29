"""
Generic HTML -> PDF renderer for admin-downloadable reports (currently
just the management report, backend/api/reports.py). Uses Playwright's
Chromium (already a hard dependency of this project — monitoring/
browser_manager.py — and already downloaded/verified working in this
environment), NOT a new PDF library: this avoids adding a dependency
with painful native-library install requirements on Windows (e.g.
WeasyPrint needs GTK/Pango) for something this project already has a
working, battle-tested browser engine for.

Deliberately independent of monitoring/browser_manager.py's
target-application session browser — this renders a LOCAL HTML string
we already built ourselves; it has nothing to do with jwintell.com and
must never touch that saved session. A fresh throwaway headless
Chromium instance is launched per call and closed immediately after —
report generation is rare/on-demand (an admin clicking "Download PDF"),
not a hot path worth keeping a browser warm for.
"""
from __future__ import annotations

from playwright.async_api import async_playwright


async def render_html_to_pdf(html: str) -> bytes:
    """page.pdf() only works against a Chromium instance actually
    launched headless (not merely headless=True on a machine that then
    renders headed for other reasons) -- confirmed working in this
    environment. Standard A4 with modest margins; the report's own CSS
    controls everything else (page-break hints, table styling)."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="networkidle")
            return await page.pdf(
                format="A4",
                print_background=True,
                margin={"top": "16mm", "bottom": "16mm", "left": "12mm", "right": "12mm"},
            )
        finally:
            await browser.close()
