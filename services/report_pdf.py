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
Chromium instance is launched per call and closed immediately after.

render_management_report_pdf() below is the actual report-specific
orchestration (compute -> pick chart granularity -> render -> PDF) --
per explicit request, this now runs ONLY from
services/report_job_worker.py's background loop ("an isolated report
generation process"), never inline in a web request. backend/api/
reports.py only ever creates/reads ReportJob rows; it does not import
this function directly.
"""
from __future__ import annotations

from datetime import datetime, timezone

from playwright.async_api import async_playwright
from sqlalchemy.orm import Session

_DATE_GRANULARITY_PERIODS = {"weekly", "monthly"}


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


async def render_management_report_pdf(
    db: Session, *, start: str, end: str, equipment_id: int | None, period: str,
    include_datewise_sales: bool = False,
) -> bytes:
    """Builds the Senior Management Report (services/management_report.py)
    for a resolved date range + optional machine scope, picks the "sales
    over time" chart's granularity from `period` (explicit rule: weekly/
    monthly periods chart by exact date, everything else by month --
    services/chart_svg.py does the actual rendering), and returns the
    finished PDF bytes. Local imports to avoid a needless import-time
    dependency from services/report_pdf's module load: backend.templating
    pulls in FastAPI's Jinja2Templates, which every OTHER function in
    this module (and every test importing it) has no reason to need."""
    from backend.templating import templates
    from services import chart_svg
    from services.management_report import build_report

    report = build_report(db, start, end, equipment_id)

    if period in _DATE_GRANULARITY_PERIODS:
        time_series_labels = [d.date for d in report.daily]
        time_series_values = [d.orders for d in report.daily]
    else:
        time_series_labels = [m.month for m in report.monthly]
        time_series_values = [m.orders for m in report.monthly]

    context = {
        "report": report,
        "generated_at": datetime.now(timezone.utc),
        "include_datewise_sales": include_datewise_sales,
        "sales_over_time_chart": chart_svg.render_line_chart(
            time_series_labels, time_series_values, title="Sales Over Time (Orders)",
        ),
        "sales_by_venue_chart": chart_svg.render_bar_chart(
            [v.venue for v in report.venue_performance],
            [v.orders for v in report.venue_performance],
            title="Sales by Venue (Orders)",
        ),
    }
    html = templates.get_template("management_report_pdf.html").render(context)
    return await render_html_to_pdf(html)


async def render_vendor_report_pdf(
    db: Session, *, start: str, end: str, venue_provider: str, include_datewise_sales: bool = False,
) -> bytes:
    """The vendor (venue_partner) equivalent of render_management_report_pdf
    -- per explicit request, a much simpler sales-only report (no
    revenue/cost/profit/venue-comparison/downtime, no chart), scoped to
    one venue's own machine(s). Always includes the Monthly Breakdown
    table (naturally the right shape for a YTD request, which spans
    many months); the day-by-day table is additive, shown only when
    include_datewise_sales is set -- same toggle semantics as the admin
    report. Local imports for the same reason render_management_report_pdf
    uses them: backend.templating pulls in FastAPI's Jinja2Templates,
    which no other function in this module needs at import time."""
    from backend.templating import templates
    from services.management_report import build_vendor_report

    report = build_vendor_report(db, start, end, venue_provider)

    context = {
        "report": report,
        "generated_at": datetime.now(timezone.utc),
        "include_datewise_sales": include_datewise_sales,
    }
    html = templates.get_template("vendor_report_pdf.html").render(context)
    return await render_html_to_pdf(html)
