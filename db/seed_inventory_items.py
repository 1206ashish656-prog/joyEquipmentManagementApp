"""
Seeds/updates the fixed 10-item inventory catalog (InventoryItem) used by
Inventory Management — see backend/api/inventory.py.

Idempotent — safe to re-run after correcting a unit label or
pieces_per_carton value. NEVER touches current_stock on an existing row
(only sets it, to 0, on first creation) — current_stock is live
operational data an admin/operations user may have already logged real
usage/corrections against, so re-running this script to fix metadata
must never reset it.

Run:
    python -m db.seed_inventory_items

Item creation is seed-script-only — there's no in-app "add a new item"
flow, matching the fixed list from the original request. To add an
11th item later, add it to DEFAULT_ITEMS below and re-run.

To track any OTHER item by a primary/secondary unit (e.g. "50 cartons,
each carton has 10 pieces"), just set that item's pieces_per_carton
here and re-run — the Current Stock display, the Log Usage form, and
the Set Stock form (as of 2026-08-28) all already key off this one
field generically. No code changes needed for a new item; only
glasses/straws happen to have it set today, that's just seed data.
"""
from __future__ import annotations

from sqlalchemy import select

from db import base as db_base
from db.models import InventoryItem

# Kitchen Cleaner, Floor Cleaner, Shower Caps, and Gloves had no unit
# specified in the original request — defaulted to "units", flagged here
# for visibility. Glasses/Straws' pieces_per_carton are starting guesses
# (24 / 100) — correct by editing here and re-running.
DEFAULT_ITEMS = [
    {"key": "oranges", "name": "Oranges", "unit": "boxes", "pieces_per_carton": None},
    {"key": "sealing_films", "name": "Sealing Films", "unit": "units", "pieces_per_carton": None},
    {"key": "glasses", "name": "Glasses", "unit": "pieces", "pieces_per_carton": 24},
    {"key": "straws", "name": "Straws", "unit": "pieces", "pieces_per_carton": 100},
    {"key": "kitchen_cleaner", "name": "Kitchen Cleaner", "unit": "units", "pieces_per_carton": None},
    {"key": "dustbin_bags", "name": "Dustbin Bags", "unit": "packs", "pieces_per_carton": None},
    {"key": "floor_cleaner", "name": "Floor Cleaner", "unit": "units", "pieces_per_carton": None},
    {"key": "orange_refill_bags", "name": "Orange Refill Bags", "unit": "packets", "pieces_per_carton": None},
    {"key": "shower_caps", "name": "Shower Caps", "unit": "units", "pieces_per_carton": None},
    {"key": "gloves", "name": "Gloves", "unit": "units", "pieces_per_carton": None},
]


def upsert(session, item: dict) -> str:
    row = session.execute(select(InventoryItem).where(InventoryItem.key == item["key"])).scalar_one_or_none()
    if row is None:
        session.add(InventoryItem(current_stock=0, **item))
        return "created"

    changed = row.name != item["name"] or row.unit != item["unit"] or row.pieces_per_carton != item["pieces_per_carton"]
    row.name = item["name"]
    row.unit = item["unit"]
    row.pieces_per_carton = item["pieces_per_carton"]
    return "updated" if changed else "unchanged"


def main() -> None:
    db_base.init_engine()
    db_base.create_all()

    with db_base.get_session() as session:
        for item in DEFAULT_ITEMS:
            action = upsert(session, item)
            print(f"{action}: {item['key']} ({item['name']})")


if __name__ == "__main__":
    main()
