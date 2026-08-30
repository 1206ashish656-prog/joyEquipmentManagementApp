"""
Unit tests for monitoring/fault_log_backfill.py's backfill_one_equipment
-- the dedup logic that makes re-running the backfill safe. Uses the
same monkeypatched-db_base + in-memory SQLite pattern as the FastAPI
backend tests, since this module (like orders/backfill.py) opens its
own sessions via db_base.get_session() rather than taking one as a
parameter. The real network-touching run_backfill()/get_fault_log_history
path is exercised via live verification, not this unit suite -- same
convention as orders/backfill.py (also never unit-tested at this
orchestration layer; its lower-level pieces are).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import db.base as db_base
from db.models import Base, Equipment, FaultLogHistory
from monitoring.fault_log_backfill import backfill_one_equipment


@pytest.fixture()
def engine(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(db_base, "_engine", engine)
    monkeypatch.setattr(db_base, "_SessionLocal", SessionLocal)
    return engine


class FakeClient:
    def __init__(self, rows):
        self._rows = rows
        self.requested_device_id: str | None = None

    async def get_fault_log_history(self, device_id: str):
        self.requested_device_id = device_id
        return self._rows


def _add_equipment(name="NEXUS", external_id="205") -> Equipment:
    with db_base.get_session() as db:
        eq = Equipment(external_id=external_id, equipment_code=f"CODE-{external_id}", name=name)
        db.add(eq)
        db.flush()
        db.refresh(eq)
        return eq


def _row(target_log_id, code="dianzicheng", is_stop=True, is_clean=True):
    return {
        "target_log_id": target_log_id,
        "component_code": code,
        "component_description": "Electronic scale Malfunction",
        "is_stop": is_stop,
        "is_clean": is_clean,
        "occurred_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
        "cleared_at": datetime(2026, 5, 1, 1, tzinfo=timezone.utc) if is_clean else None,
    }


@pytest.mark.asyncio
async def test_backfill_stores_all_new_rows(engine):
    equipment = _add_equipment()
    client = FakeClient([_row(1), _row(2), _row(3)])

    created = await backfill_one_equipment(client, equipment)

    assert created == 3
    assert client.requested_device_id == "205"
    with db_base.get_session() as db:
        rows = db.execute(select(FaultLogHistory).where(FaultLogHistory.equipment_id == equipment.id)).scalars().all()
        assert len(rows) == 3


@pytest.mark.asyncio
async def test_backfill_is_idempotent_on_rerun(engine):
    """Explicit safety property: re-running the backfill must never
    duplicate an already-stored row."""
    equipment = _add_equipment()
    client = FakeClient([_row(1), _row(2)])

    first = await backfill_one_equipment(client, equipment)
    second = await backfill_one_equipment(client, equipment)

    assert first == 2
    assert second == 0
    with db_base.get_session() as db:
        rows = db.execute(select(FaultLogHistory).where(FaultLogHistory.equipment_id == equipment.id)).scalars().all()
        assert len(rows) == 2


@pytest.mark.asyncio
async def test_backfill_only_adds_genuinely_new_rows_on_partial_rerun(engine):
    """A second run with one already-seen id plus one genuinely new one
    only stores the new one."""
    equipment = _add_equipment()
    await backfill_one_equipment(FakeClient([_row(1)]), equipment)

    created = await backfill_one_equipment(FakeClient([_row(1), _row(2)]), equipment)

    assert created == 1
    with db_base.get_session() as db:
        target_ids = {r.target_log_id for r in db.execute(select(FaultLogHistory)).scalars().all()}
        assert target_ids == {1, 2}


@pytest.mark.asyncio
async def test_backfill_scopes_dedup_per_equipment(engine):
    """Two different machines can each have their own target_log_id=1
    without colliding -- the unique constraint is (equipment_id,
    target_log_id), not target_log_id alone."""
    eq_a = _add_equipment("NEXUS", "205")
    eq_b = _add_equipment("Gravity", "116")

    created_a = await backfill_one_equipment(FakeClient([_row(1)]), eq_a)
    created_b = await backfill_one_equipment(FakeClient([_row(1)]), eq_b)

    assert created_a == 1
    assert created_b == 1
    with db_base.get_session() as db:
        assert len(db.execute(select(FaultLogHistory)).scalars().all()) == 2


@pytest.mark.asyncio
async def test_backfill_preserves_row_fields(engine):
    equipment = _add_equipment()
    await backfill_one_equipment(FakeClient([_row(1, code="luozhentanzhenkaiguan", is_stop=True, is_clean=False)]), equipment)

    with db_base.get_session() as db:
        row = db.execute(select(FaultLogHistory)).scalar_one()
        assert row.component_code == "luozhentanzhenkaiguan"
        assert row.is_stop is True
        assert row.is_clean is False
        assert row.cleared_at is None
