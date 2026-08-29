"""
Cost Management tab (admin-only): log raw-material/operating costs
against standard categories (or a custom one via "Others"), summarize
them over a period — same Daily/Weekly/Monthly/YTD/custom period shape
as Order Summary (backend/period_utils.py), breakdown by
category/vendor/item name in any combination (costs/rollup.py) — and
view/filter/edit/delete the underlying raw entries, since a rollup total
alone doesn't let an admin fix or remove a single bad entry.

Also hosts "Recurring Costs" (services/recurring_costs.py): one Rent
entry per active Venue, one Staff Salaries entry per active Staff
member, generated on admin request for a chosen month rather than
retyped every time.
"""
from __future__ import annotations

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
from services import recurring_costs

router = APIRouter()

RECURRING_PERIOD_LENGTH = 7  # 'YYYY-MM'


def _resolve_recurring_period(raw: str | None) -> str:
    """Falls back to the current calendar month on anything malformed —
    this only ever comes from our own <input type="month"> field, but
    validated defensively like every other date/period input in this
    app (e.g. staff.py's year/month parsing)."""
    candidate = (raw or "").strip()
    if len(candidate) == RECURRING_PERIOD_LENGTH:
        try:
            date_cls.fromisoformat(f"{candidate}-01")
            return candidate
        except ValueError:
            pass
    return date_cls.today().strftime("%Y-%m")

STAFF_SALARIES_CATEGORY = "Staff Salaries"
RAW_ROWS_LIMIT = 300  # a hard cap so one huge period doesn't render an unbounded table


def _resolve_category_and_vendor(category: str, custom_category: str, vendor_name: str) -> tuple[str, str | None]:
    """Shared by create and edit — same rules both times: "Others" ->
    the admin's typed text becomes the real category; vendor is skipped
    for Staff Salaries and defaults to a filterable placeholder if left
    blank for every other category."""
    resolved_category = custom_category.strip() if category == "Others" else category
    if not resolved_category:
        resolved_category = "Others"

    if resolved_category == STAFF_SALARIES_CATEGORY:
        resolved_vendor = None
    else:
        resolved_vendor = vendor_name.strip() or UNSPECIFIED_VENDOR
    return resolved_category, resolved_vendor


def _distinct(db: Session, column) -> list[str]:
    return sorted({row[0] for row in db.execute(select(column).distinct()) if row[0]})


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

    # "Admin should be able to view raw data for selected item, category
    # or vendor" -- these narrow BOTH the rollup and the raw-entries list
    # below it, so the two stay consistent with each other.
    filter_category = q.get("filter_category", "").strip()
    filter_vendor = q.get("filter_vendor", "").strip()
    filter_item = q.get("filter_item", "").strip()

    query = select(CostEntry).where(CostEntry.date >= start, CostEntry.date <= end)
    if filter_category:
        query = query.where(CostEntry.category == filter_category)
    if filter_vendor:
        query = query.where(CostEntry.vendor_name == filter_vendor)
    if filter_item:
        query = query.where(CostEntry.item_name == filter_item)

    rows = db.execute(query).scalars().all()

    group_by = tuple(
        dim for dim, flag in
        [("category", by_category), ("vendor_name", by_vendor), ("item_name", by_item)]
        if flag
    )
    table_rows = rollup(rows, group_by=group_by)
    overall = rollup(rows, group_by=())
    overall_row = overall[0] if overall else None

    # "Raw data ... should only be displayed if explicitly asked" --
    # defaults to hidden; the radio buttons on the page are what turn it
    # on, not just having a category/vendor/item filter selected.
    show_raw_data = q.get("show_raw_data", "no") == "yes"
    raw_total_count = len(rows)
    if show_raw_data:
        raw_rows_all = sorted(rows, key=lambda r: (r.date, r.id), reverse=True)
        raw_truncated = raw_total_count > RAW_ROWS_LIMIT
        raw_rows = raw_rows_all[:RAW_ROWS_LIMIT]
    else:
        raw_rows, raw_truncated = [], False

    recurring_period = _resolve_recurring_period(q.get("recurring_period"))
    recurring_candidates = recurring_costs.list_candidates(db, recurring_period)

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
            "filter_category": filter_category,
            "filter_vendor": filter_vendor,
            "filter_item": filter_item,
            "all_categories": _distinct(db, CostEntry.category),
            "all_vendors": _distinct(db, CostEntry.vendor_name),
            "all_items": _distinct(db, CostEntry.item_name),
            "group_by_labels": [d.replace("category", "Category").replace("vendor_name", "Vendor").replace("item_name", "Item") for d in group_by] or ["(no breakdown — total)"],
            "table_rows": table_rows,
            "overall_row": overall_row,
            "has_data": bool(rows),
            "show_raw_data": show_raw_data,
            "raw_rows": raw_rows,
            "raw_total_count": raw_total_count,
            "raw_truncated": raw_truncated,
            "standard_categories": STANDARD_COST_CATEGORIES,
            "staff_salaries_category": STAFF_SALARIES_CATEGORY,
            "today": date_cls.today().isoformat(),
            "recurring_period": recurring_period,
            "recurring_candidates": recurring_candidates,
            "recurring_pending_count": sum(1 for c in recurring_candidates if not c.already_generated),
        },
    )


@router.post("/costs")
def create_cost_entry(
    date: str = Form(...),
    category: str = Form(...),
    custom_category: str = Form(""),
    vendor_name: str = Form(""),
    item_name: str = Form(""),
    amount: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    resolved_category, resolved_vendor = _resolve_category_and_vendor(category, custom_category, vendor_name)
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


@router.get("/costs/{entry_id}/edit", response_class=HTMLResponse)
def edit_cost_entry_form(
    entry_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    entry = db.get(CostEntry, entry_id)
    if entry is None:
        return templates.TemplateResponse(request, "not_found.html", {"user": admin}, status_code=404)

    is_standard = entry.category in STANDARD_COST_CATEGORIES
    return templates.TemplateResponse(
        request,
        "cost_entry_edit.html",
        {
            "user": admin,
            "entry": entry,
            "standard_categories": STANDARD_COST_CATEGORIES,
            "staff_salaries_category": STAFF_SALARIES_CATEGORY,
            "is_standard_category": is_standard,
        },
    )


@router.post("/costs/{entry_id}/edit")
def update_cost_entry(
    entry_id: int,
    date: str = Form(...),
    category: str = Form(...),
    custom_category: str = Form(""),
    vendor_name: str = Form(""),
    item_name: str = Form(""),
    amount: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    entry = db.get(CostEntry, entry_id)
    if entry is None:
        return RedirectResponse(url="/costs", status_code=303)

    resolved_category, resolved_vendor = _resolve_category_and_vendor(category, custom_category, vendor_name)
    try:
        resolved_amount = Decimal(amount)
    except InvalidOperation:
        # A bad amount on edit shouldn't silently zero out a real,
        # previously-valid entry -- keep what was there.
        resolved_amount = entry.amount

    entry.date = date
    entry.category = resolved_category
    entry.vendor_name = resolved_vendor
    entry.item_name = item_name.strip()
    entry.amount = resolved_amount
    return RedirectResponse(url="/costs", status_code=303)


@router.post("/costs/{entry_id}/delete")
def delete_cost_entry(
    entry_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    entry = db.get(CostEntry, entry_id)
    if entry is not None:
        db.delete(entry)
    return RedirectResponse(url="/costs", status_code=303)


@router.post("/costs/recurring/generate")
def generate_recurring_costs(
    period: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    resolved_period = _resolve_recurring_period(period)
    recurring_costs.generate(db, resolved_period, admin.id)
    return RedirectResponse(url=f"/costs?recurring_period={resolved_period}", status_code=303)
