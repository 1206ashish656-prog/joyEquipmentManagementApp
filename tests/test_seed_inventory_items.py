"""Tests for db/seed_inventory_items.py's upsert() -- idempotency, and
that current_stock (live operational data) is never touched by a
re-seed, only metadata."""
from __future__ import annotations

from sqlalchemy import select

from db.models import InventoryItem
from db.seed_inventory_items import DEFAULT_ITEMS, upsert


def test_seeding_twice_creates_exactly_ten_rows_not_twenty(db_session):
    for item in DEFAULT_ITEMS:
        upsert(db_session, item)
    db_session.flush()
    for item in DEFAULT_ITEMS:
        upsert(db_session, item)
    db_session.flush()

    count = len(db_session.execute(select(InventoryItem)).scalars().all())
    assert count == len(DEFAULT_ITEMS) == 10


def test_current_stock_survives_a_second_seed_run(db_session):
    for item in DEFAULT_ITEMS:
        upsert(db_session, item)
    db_session.flush()

    row = db_session.execute(select(InventoryItem).where(InventoryItem.key == "oranges")).scalar_one()
    row.current_stock = 42  # simulate real usage having been logged
    db_session.flush()

    upsert(db_session, next(i for i in DEFAULT_ITEMS if i["key"] == "oranges"))
    db_session.flush()

    row = db_session.execute(select(InventoryItem).where(InventoryItem.key == "oranges")).scalar_one()
    assert row.current_stock == 42


def test_changed_metadata_updates_existing_row_not_a_duplicate(db_session):
    upsert(db_session, {"key": "oranges", "name": "Oranges", "unit": "boxes", "pieces_per_carton": None})
    db_session.flush()

    action = upsert(db_session, {"key": "oranges", "name": "Oranges", "unit": "crates", "pieces_per_carton": None})
    db_session.flush()

    assert action == "updated"
    rows = db_session.execute(select(InventoryItem).where(InventoryItem.key == "oranges")).scalars().all()
    assert len(rows) == 1
    assert rows[0].unit == "crates"


def test_unchanged_metadata_reports_unchanged(db_session):
    item = {"key": "gloves", "name": "Gloves", "unit": "units", "pieces_per_carton": None}
    upsert(db_session, item)
    db_session.flush()

    action = upsert(db_session, item)
    assert action == "unchanged"


def test_first_seed_starts_stock_at_zero(db_session):
    upsert(db_session, {"key": "oranges", "name": "Oranges", "unit": "boxes", "pieces_per_carton": None})
    db_session.flush()

    row = db_session.execute(select(InventoryItem).where(InventoryItem.key == "oranges")).scalar_one()
    assert row.current_stock == 0
