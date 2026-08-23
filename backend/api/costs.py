"""
Cost Management tab (admin-only): log raw-material/operating costs
against standard categories (or a custom one via "Others"), and
summarize them over a period — same Daily/Weekly/Monthly/YTD/custom
period shape as Order Summary (backend/period_utils.py), breakdown by
category/vendor/item name in any combination (costs/rollup.py).
"""
from __future__ import annotations

import json
from datetime import date as date_cls
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.deps import get_db, require_admin
from backend.period_utils import PERIODS, period_range
from backend.templating import templates
from db.models import STANDARD_COST_CATEGORIES, UNSPECIFIED_VENDOR, CostEntry, User

router = APIRouter()

STAFF_SALARIES_CATEGORY = "Staff Salaries"


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


@router.get("/costs", response_class=HTMLResponse)
def costs_summary(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    from costs.rollup import rollup  # local import: keep the route module light to read top-to-bottom

    q = request.query_params
    period = q.get("period", "monthly")
    if period not in PERIODS:
        period = "monthly"
    as_of = q.get("as_of") or date_cls.today().isoformat()
    start, end = period_range(period, as_of, start=q.get("start"), end=q.get("end"))

    by_category = q.get("by_category", "1") != "0"
    by_vendor = q.get("by_vendor", "0") == "1"
    by_item = q.get("by_item", "0") == "1"

    rows = db.execute(
        select(CostEntry).where(CostEntry.date >= start, CostEntry.date <= end)
    ).scalars().all()

    group_by = tuple(
        dim for dim, flag in
        [("category", by_category), ("vendor_name", by_vendor), ("item_name", by_item)]
        if flag
    )
    table_rows = rollup(rows, group_by=group_by)
    overall = rollup(rows, group_by=())
    overall_row = overall[0] if overall else None

    return templates.TemplateResponse(
        request,
        "costs.html",
        {
            "user": admin,
            "period": period,
            "as_of": as_of,
            "start": start,
            "end": end,
            "by_category": by_category,
            "by_vendor": by_vendor,
            "by_item": by_item,
            "group_by_labels": [d.replace("category", "Category").replace("vendor_name", "Vendor").replace("item_name", "Item") for d in group_by] or ["(no breakdown — total)"],
            "table_rows": table_rows,
            "overall_row": overall_row,
            "has_data": bool(rows),
            "standard_categories": STANDARD_COST_CATEGORIES,
            "staff_salaries_category": STAFF_SALARIES_CATEGORY,
            "today": date_cls.today().isoformat(),
        },
    )


@router.post("/costs")
def create_cost_entry(
    date: str = Form(...),
    category: str = Form(...),
    custom_category: str = Form(""),
    vendor_name: str = Form(""),
    item_name: str = Form(...),
    amount: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    # "Others" is a UI sentinel only -- the admin's typed-in text becomes
    # the actual stored category, per "If admin select other, ask admin
    # to specify the category explicitly."
    resolved_category = custom_category.strip() if category == "Others" else category
    if not resolved_category:
        resolved_category = "Others"

    # Vendor isn't applicable to Staff Salaries at all (not asked, not
    # stored); every other category gets a filterable placeholder if left
    # blank rather than a bare NULL.
    if resolved_category == STAFF_SALARIES_CATEGORY:
        resolved_vendor = None
    else:
        resolved_vendor = vendor_name.strip() or UNSPECIFIED_VENDOR

    try:
        resolved_amount = Decimal(amount)
    except InvalidOperation:
        resolved_amount = Decimal("0")

    db.add(
        CostEntry(
            date=date,
            category=resolved_category,
            vendor_name=resolved_vendor,
            item_name=item_name.strip(),
            amount=resolved_amount,
            created_by_user_id=admin.id,
        )
    )
    return RedirectResponse(url="/costs", status_code=303)
