"""
Senior Management Report (admin-only, /reports/management): aggregated
+ monthly sales/revenue/cost/profit, venue performance ranked by sales
volume, and per-machine downtime by time of day — see
services/management_report.py for the actual computation (this module
is presentation + PDF export only) and its module docstring for why
cost/profit is company-wide only. Downloadable as a PDF
(services/report_pdf.py, Playwright-rendered) in addition to the
on-screen HTML preview — same underlying report, same template content
partial, so the two never drift apart.

Charts (services/chart_svg.py, plain inline SVG — see that module's
docstring for why not Chart.js) render here, not in
management_report.py: which granularity the "sales over time" chart
uses is a presentation choice (explicit request — "if monthly or
weekly selected, generate charts by date else generate monthly sales
chart"), not something the UI-agnostic report data layer should know
about.
"""
from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.deps import get_db, require_admin
from backend.period_utils import PERIODS, period_range
from backend.templating import templates
from db.models import Equipment, User
from services import chart_svg, report_pdf
from services.management_report import build_report

router = APIRouter()

_DATE_GRANULARITY_PERIODS = {"weekly", "monthly"}


def _resolve_equipment(db: Session, raw_equipment_id: str) -> Equipment | None:
    raw_equipment_id = (raw_equipment_id or "").strip()
    if not raw_equipment_id:
        return None
    try:
        equipment_id = int(raw_equipment_id)
    except ValueError:
        return None
    return db.get(Equipment, equipment_id)


def _build_context(request: Request, admin: User, db: Session) -> dict:
    q = request.query_params
    period = q.get("period", "monthly")
    if period not in PERIODS:
        period = "monthly"
    as_of = q.get("as_of") or date_cls.today().isoformat()
    start, end = period_range(period, as_of, start=q.get("start"), end=q.get("end"))

    equipment = _resolve_equipment(db, q.get("equipment_id", ""))
    report = build_report(db, start, end, equipment.id if equipment else None)
    equipment_list = db.execute(select(Equipment).order_by(Equipment.name)).scalars().all()

    # Explicit requirement: weekly/monthly periods chart by exact date
    # (short enough ranges that daily granularity stays readable);
    # every other period (daily, ytd, custom) charts by month instead.
    if period in _DATE_GRANULARITY_PERIODS:
        time_series_labels = [d.date for d in report.daily]
        time_series_values = [d.orders for d in report.daily]
    else:
        time_series_labels = [m.month for m in report.monthly]
        time_series_values = [m.orders for m in report.monthly]

    sales_over_time_chart = chart_svg.render_line_chart(
        time_series_labels, time_series_values, title="Sales Over Time (Orders)",
    )
    sales_by_venue_chart = chart_svg.render_bar_chart(
        [v.venue for v in report.venue_performance],
        [v.orders for v in report.venue_performance],
        title="Sales by Venue (Orders)",
    )

    return {
        "user": admin,
        "period": period,
        "as_of": as_of,
        "start": start,
        "end": end,
        "equipment_id": equipment.id if equipment else "",
        "equipment_list": equipment_list,
        "report": report,
        "generated_at": datetime.now(timezone.utc),
        "sales_over_time_chart": sales_over_time_chart,
        "sales_by_venue_chart": sales_by_venue_chart,
    }


@router.get("/reports/management", response_class=HTMLResponse)
def management_report_page(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "management_report.html", _build_context(request, admin, db))


@router.get("/reports/management/pdf")
async def management_report_pdf(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    context = _build_context(request, admin, db)
    html = templates.get_template("management_report_pdf.html").render(context)
    pdf_bytes = await report_pdf.render_html_to_pdf(html)

    scope_slug = context["report"].equipment_scope.replace(" ", "_")
    filename = f"management-report_{context['start']}_to_{context['end']}_{scope_slug}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
