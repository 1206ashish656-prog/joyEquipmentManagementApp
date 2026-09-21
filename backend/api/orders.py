"""
Order Summary tab: period selection (daily/weekly/monthly/YTD), optional
breakdown by machine/price/pay_type, and a time-series chart — all
computed at request time from the stored (date, device_app, price,
pay_type) rows via orders/rollup.py. No new aggregation logic lives here;
this module is presentation only.
"""
from __future__ import annotations

import json
from datetime import date as date_cls
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.deps import get_db, require_venue_partner
from backend.period_utils import PERIODS, period_range
from backend.templating import templates
from db.models import OrderSummary, OrderSummaryRun, OrderSummaryRunStatus, ReportJob, ReportJobStatus, User, VenueMapping
from orders.rollup import rollup

router = APIRouter()


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


def _latest_available_date(db: Session) -> str | None:
    return db.execute(
        select(func.max(OrderSummaryRun.date)).where(OrderSummaryRun.status == OrderSummaryRunStatus.SUCCESS)
    ).scalar_one_or_none()


def _last_updated_at(db: Session):
    """Most recent successful OrderSummaryRun.completed_at across every
    date -- in practice this is always today's row, since
    orders/realtime_worker.py re-runs it on every refresh cycle
    (DEFAULT_INTERVAL_SECONDS = 300). Shown on the page so it's obvious
    the numbers are live, not a stale one-time snapshot."""
    return db.execute(
        select(func.max(OrderSummaryRun.completed_at)).where(OrderSummaryRun.status == OrderSummaryRunStatus.SUCCESS)
    ).scalar_one_or_none()


def _venue_machines(db: Session, venue_provider: str) -> list[str]:
    """Machine names (OrderSummary.device_app values) mapped to a given
    venue_provider — case-insensitively, since the target app isn't
    consistent about casing (e.g. "NEXUS" vs "Nexus")."""
    rows = db.execute(
        select(VenueMapping.machine_name).where(func.lower(VenueMapping.venue_provider) == venue_provider.lower())
    ).scalars().all()
    return list(rows)


def _time_series_payload(rows: list[OrderSummary], split_by_machine: bool, include_revenue: bool) -> dict:
    """A per-date time series, either as a single 'All machines' line or
    one line per machine — used for BOTH the always-on aggregate chart
    and the optional by-machine chart, so there's one implementation of
    'turn rows into a Chart.js-shaped series', not two.

    include_revenue: the chart JS (order_summary.html's renderOrdersChart)
    only ever plots "orders", never "revenue" — but the raw JSON blob is
    embedded in the page for every role, so a venue_partner could
    previously read real revenue numbers out of page-source/network tab
    even though nothing visibly rendered them (Revenue is otherwise
    correctly admin-only gated everywhere else on this page). Omitting
    the key entirely for a non-admin closes that leak with zero visible
    change for anyone, since nothing ever read it."""
    group_by = ("date", "device_app") if split_by_machine else ("date",)
    chart_rows = rollup(rows, group_by=group_by)

    dates = sorted({r.key["date"] for r in chart_rows})
    if split_by_machine:
        machines = sorted({r.key["device_app"] for r in chart_rows})
        by_key = {(r.key["date"], r.key["device_app"]): r for r in chart_rows}
        datasets = [
            {
                "label": machine,
                "orders": [by_key[(d, machine)].number_of_orders if (d, machine) in by_key else 0 for d in dates],
                **({
                    "revenue": [
                        float(by_key[(d, machine)].number_of_orders * by_key[(d, machine)].average_price)
                        if (d, machine) in by_key else 0
                        for d in dates
                    ],
                } if include_revenue else {}),
            }
            for machine in machines
        ]
    else:
        by_key = {r.key["date"]: r for r in chart_rows}
        datasets = [{
            "label": "All machines",
            "orders": [by_key[d].number_of_orders if d in by_key else 0 for d in dates],
            **({
                "revenue": [
                    float(by_key[d].number_of_orders * by_key[d].average_price) if d in by_key else 0 for d in dates
                ],
            } if include_revenue else {}),
        }]

    return {"dates": dates, "datasets": datasets}


@router.get("/orders/summary", response_class=HTMLResponse)
def orders_summary(
    request: Request,
    user: User = Depends(require_venue_partner),
    db: Session = Depends(get_db),
):
    q = request.query_params
    period = q.get("period", "daily")
    if period not in PERIODS:
        period = "daily"
    by_machine = q.get("by_machine", "1") != "0"
    by_price = q.get("by_price", "0") == "1"
    by_pay_type = q.get("by_pay_type", "0") == "1"

    latest = _latest_available_date(db)
    last_updated_at = _last_updated_at(db)
    as_of = q.get("as_of") or latest or date_cls.today().isoformat()

    start, end = period_range(period, as_of, start=q.get("start"), end=q.get("end"))
    query = select(OrderSummary).where(OrderSummary.date >= start, OrderSummary.date <= end)

    # Venue partners are scoped to their own venue's machine(s) —
    # requirement: "see order summary data specific to their venue only".
    # Admins are never scoped (require_venue_partner already ensures the
    # only two roles that reach this route are admin and venue_partner).
    venue_machines: list[str] | None = None
    venue_unassigned = False
    if user.role == "venue_partner":
        if not user.venue_provider:
            venue_unassigned = True
            rows = []
        else:
            venue_machines = _venue_machines(db, user.venue_provider)
            rows = (
                db.execute(query.where(OrderSummary.device_app.in_(venue_machines))).scalars().all()
                if venue_machines
                else []
            )
    else:
        rows = db.execute(query).scalars().all()

    group_by = tuple(
        dim for dim, flag in [("device_app", by_machine), ("price", by_price), ("pay_type", by_pay_type)] if flag
    )
    table_rows = rollup(rows, group_by=group_by)
    overall = rollup(rows, group_by=())
    overall_row = overall[0] if overall else None

    # Always-on aggregate trend (requirement: "I want to see trend for
    # aggregated orders over time as well" — independent of whatever
    # breakdown is selected for the table below), plus an optional
    # by-machine trend when that breakdown is selected.
    include_revenue = user.role == "admin"
    aggregate_chart_data = _time_series_payload(rows, split_by_machine=False, include_revenue=include_revenue)
    machine_chart_data = _time_series_payload(rows, split_by_machine=True, include_revenue=include_revenue) if by_machine else None

    # Report-download section (venue_partner only) -- their own report
    # history, never another vendor's (see the download route's
    # ownership check for why this filter matters beyond just display).
    vendor_report_jobs = []
    if user.role == "venue_partner" and user.venue_provider:
        vendor_report_jobs = db.execute(
            select(ReportJob)
            .where(ReportJob.venue_provider == user.venue_provider)
            .order_by(ReportJob.created_at.desc())
            .limit(20)
        ).scalars().all()
    vendor_report_in_progress = any(
        j.status in (ReportJobStatus.PENDING, ReportJobStatus.RUNNING) for j in vendor_report_jobs
    )

    return templates.TemplateResponse(
        request,
        "order_summary.html",
        {
            "user": user,
            "period": period,
            "as_of": as_of,
            "start": start,
            "end": end,
            "by_machine": by_machine,
            "by_price": by_price,
            "by_pay_type": by_pay_type,
            "group_by_labels": [d.replace("device_app", "Machine").replace("price", "Price").replace("pay_type", "Pay Type") for d in group_by] or ["(no breakdown — aggregated)"],
            "table_rows": table_rows,
            "overall_row": overall_row,
            "latest_available_date": latest,
            "last_updated_at": last_updated_at,
            "has_data": bool(rows),
            "venue_provider": user.venue_provider,
            "venue_unassigned": venue_unassigned,
            "venue_machines": venue_machines,
            "aggregate_chart_json": json.dumps(aggregate_chart_data, cls=_DecimalEncoder),
            "machine_chart_json": json.dumps(machine_chart_data, cls=_DecimalEncoder) if machine_chart_data else None,
            "report_periods": PERIODS,
            "today": date_cls.today().isoformat(),
            "vendor_report_jobs": vendor_report_jobs,
            "vendor_report_in_progress": vendor_report_in_progress,
        },
    )


@router.post("/orders/summary/report")
def create_vendor_report_job(
    period: str = Form("monthly"),
    as_of: str = Form(""),
    start: str = Form(""),
    end: str = Form(""),
    include_datewise_sales: str = Form(""),
    user: User = Depends(require_venue_partner),
    db: Session = Depends(get_db),
):
    # require_venue_partner also admits admin (it's shared with this
    # whole page) -- but this specific report-download feature is
    # vendor-only per explicit request ("provide report downloading
    # option to vendors"); admin already has the full /reports/management.
    # An admin has no venue_provider to scope a vendor job to, so this
    # must reject rather than silently create a NULL-venue job that
    # report_job_worker.py would then misinterpret as an admin
    # "All Machines" management report.
    if user.role != "venue_partner":
        return RedirectResponse(url="/orders/summary", status_code=303)
    if not user.venue_provider:
        return RedirectResponse(url="/orders/summary", status_code=303)

    if period not in PERIODS:
        period = "monthly"
    resolved_as_of = as_of or date_cls.today().isoformat()
    resolved_start, resolved_end = period_range(period, resolved_as_of, start=start, end=end)

    db.add(ReportJob(
        requested_by_user_id=user.id,
        period=period,
        as_of=resolved_as_of,
        start=resolved_start,
        end=resolved_end,
        equipment_id=None,
        venue_provider=user.venue_provider,
        include_datewise_sales=bool(include_datewise_sales),
        status=ReportJobStatus.PENDING,
    ))
    return RedirectResponse(url="/orders/summary", status_code=303)


@router.get("/orders/summary/report/{job_id}/download")
def download_vendor_report_job(
    job_id: int, user: User = Depends(require_venue_partner), db: Session = Depends(get_db),
):
    job = db.get(ReportJob, job_id)
    # Ownership check: a venue partner must never be able to download
    # another vendor's report by guessing/incrementing a job_id, and an
    # admin-initiated job (venue_provider is None) is never reachable
    # here either -- both collapse to the same "as if it doesn't exist"
    # redirect, not a distinguishable error, so a guessed ID can't be
    # used to probe which job_ids exist.
    if (
        job is None
        or job.venue_provider is None
        or user.role != "venue_partner"
        or job.venue_provider != user.venue_provider
        or job.status != ReportJobStatus.SUCCESS
        or not job.pdf_data
    ):
        return RedirectResponse(url="/orders/summary", status_code=303)

    filename = f"sales-report_{job.start}_to_{job.end}_{job.venue_provider.replace(' ', '_')}.pdf"
    return Response(
        content=job.pdf_data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
