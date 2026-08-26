"""
Inventory Management tab (admin OR operations — see backend/deps.py's
require_operations, which already means exactly this role pair): current
stock for a FIXED set of consumable items (db/seed_inventory_items.py),
a "log usage today" form that decrements stock, and a "set stock" form
for corrections/restocks. Low-stock thresholds come from
config/inventory_rules.yaml (services/inventory_rules.py); crossing below
threshold fires a real email exactly once (services/inventory_alert.py),
while the dashboard badge is always recomputed live from current_stock —
no persisted "is low" flag to keep in sync.
"""
from __future__ import annotations

from datetime import date as date_cls

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.deps import get_db, get_settings, require_operations
from backend.templating import templates
from db.models import INVENTORY_ITEM_ORDER, InventoryItem, InventoryLogEntry, User
from monitoring.config import Settings
from services.inventory_alert import check_and_notify
from services.inventory_rules import InventoryRules
from services.notification_service import NotificationService

router = APIRouter()

RECENT_ACTIVITY_LIMIT = 50


def _ordered_items(db: Session) -> list[InventoryItem]:
    items = db.execute(select(InventoryItem)).scalars().all()
    # Display in the same order the requirements listed them, not
    # DB-insertion/alphabetical order -- see INVENTORY_ITEM_ORDER.
    return sorted(
        items,
        key=lambda i: INVENTORY_ITEM_ORDER.index(i.key) if i.key in INVENTORY_ITEM_ORDER else len(INVENTORY_ITEM_ORDER),
    )


@router.get("/inventory", response_class=HTMLResponse)
def inventory_dashboard(
    request: Request,
    user: User = Depends(require_operations),
    db: Session = Depends(get_db),
):
    rules = InventoryRules()
    items = _ordered_items(db)
    rows = [
        {"item": item, "threshold": rules.threshold_for(item.key), "is_low": item.current_stock < rules.threshold_for(item.key)}
        for item in items
    ]
    low_count = sum(1 for r in rows if r["is_low"])

    recent_entries = db.execute(
        select(InventoryLogEntry, InventoryItem)
        .join(InventoryItem, InventoryLogEntry.item_id == InventoryItem.id)
        .order_by(InventoryLogEntry.created_at.desc())
        .limit(RECENT_ACTIVITY_LIMIT)
    ).all()

    return templates.TemplateResponse(
        request,
        "inventory.html",
        {
            "user": user,
            "rows": rows,
            "items": items,
            "low_count": low_count,
            "recent_entries": recent_entries,
            "today": date_cls.today().isoformat(),
        },
    )


@router.post("/inventory/log-usage")
def log_usage(
    item_id: int = Form(...),
    date: str = Form(...),
    quantity_used: int = Form(...),
    note: str = Form(""),
    user: User = Depends(require_operations),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    item = db.get(InventoryItem, item_id)
    if item is None:
        return RedirectResponse(url="/inventory", status_code=303)

    quantity_used = max(0, quantity_used)  # bad/negative input never increases stock via this path
    old_stock = item.current_stock
    new_stock = max(0, old_stock - quantity_used)  # never goes negative

    item.current_stock = new_stock
    db.add(
        InventoryLogEntry(
            item_id=item.id,
            entry_type="usage",
            date=date,
            quantity_change=new_stock - old_stock,
            resulting_stock=new_stock,
            note=note.strip() or None,
            created_by_user_id=user.id,
        )
    )
    db.flush()  # row visible in this session before the alert check runs

    check_and_notify(db, NotificationService(settings), InventoryRules(), item, old_stock, new_stock)
    return RedirectResponse(url="/inventory", status_code=303)


@router.post("/inventory/{item_id}/set-stock")
def set_stock(
    item_id: int,
    new_stock: int = Form(...),
    note: str = Form(""),
    user: User = Depends(require_operations),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    item = db.get(InventoryItem, item_id)
    if item is None:
        return RedirectResponse(url="/inventory", status_code=303)

    new_stock = max(0, new_stock)
    old_stock = item.current_stock

    item.current_stock = new_stock
    db.add(
        InventoryLogEntry(
            item_id=item.id,
            entry_type="correction",
            date=date_cls.today().isoformat(),
            quantity_change=new_stock - old_stock,
            resulting_stock=new_stock,
            note=note.strip() or None,
            created_by_user_id=user.id,
        )
    )
    db.flush()

    # A downward correction can also cross the threshold -- same check
    # either direction.
    check_and_notify(db, NotificationService(settings), InventoryRules(), item, old_stock, new_stock)
    return RedirectResponse(url="/inventory", status_code=303)
