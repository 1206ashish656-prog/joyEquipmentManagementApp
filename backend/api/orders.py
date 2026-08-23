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
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.deps import get_db, require_user
from backend.templating import templates
from db.models import OrderSummary, OrderSummaryRun, OrderSummaryRunStatus, User
from orders.rollup import rollup

router = APIRouter()

PERIODS = ("daily", "weekly", "monthly", "ytd")


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


def _latest_available_date(db: Session) -> str | None:
    return db.execute(
        select(func.max(OrderSummaryRun.date)).where(OrderSummaryRun.status == OrderSummaryRunStatus.SUCCESS)
    ).scalar_one_or_none()


def _period_range(period: str, as_of: str) -> tuple[str, str]:
    d = datetime.strptime(as_of, "%Y-%m-%d").date()
    if period == "daily":
        start = end = d
    elif period == "weekly":
        start = d - timedelta(days=d.weekday())  # Monday
        end = start + timedelta(days=6)
    elif period == "monthly":
        start = d.replace(day=1)
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = next_month - timedelta(days=1)
    elif period == "ytd":
        start = d.replace(month=1, day=1)
        end = d
    else:
        start = end = d
    return start.isoformat(), end.isoformat()


def _chart_payload(rows: list[OrderSummary], by_machine: bool) -> dict:
    group_by = ("date", "device_app") if by_machine else ("date",)
    chart_rows = rollup(rows, group_by=group_by)

    dates = sorted({r.key["date"] for r in chart_rows})
    if by_machine:
        machines = sorted({r.key["device_app"] for r in chart_rows})
        by_key = {(r.key["date"], r.key["device_app"]): r for r in chart_rows}
        datasets = [
            {
                "label": machine,
                "orders": [by_key[(d, machine)].number_of_orders if (d, machine) in by_key else 0 for d in dates],
                "revenue": [
                    float(by_key[(d, machine)].number_of_orders * by_key[(d, machine)].average_price)
                    if (d, machine) in by_key else 0
                    for d in dates
                ],
            }
            for machine in machines
        ]
    else:
        by_key = {r.key["date"]: r for r in chart_rows}
        datasets = [{
            "label": "All machines",
            "orders": [by_key[d].number_of_orders if d in by_key else 0 for d in dates],
            "revenue": [
                float(by_key[d].number_of_orders * by_key[d].average_price) if d in by_key else 0 for d in dates
            ],
        }]

    return {"dates": dates, "datasets": datasets}


@router.get("/orders/summary", response_class=HTMLResponse)
def orders_summary(
    request: Request,
    user: User = Depends(require_user),
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
    as_of = q.get("as_of") or latest or date_cls.today().isoformat()

    start, end = _period_range(period, as_of)
    rows = db.execute(
        select(OrderSummary).where(OrderSummary.date >= start, OrderSummary.date <= end)
    ).scalars().all()

    group_by = tuple(
        dim for dim, flag in [("device_app", by_machine), ("price", by_price), ("pay_type", by_pay_type)] if flag
    )
    table_rows = rollup(rows, group_by=group_by)
    overall = rollup(rows, group_by=())
    overall_row = overall[0] if overall else None

    chart_data = _chart_payload(rows, by_machine=by_machine)

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
            "has_data": bool(rows),
            "chart_data_json": json.dumps(chart_data, cls=_DecimalEncoder),
        },
    )
