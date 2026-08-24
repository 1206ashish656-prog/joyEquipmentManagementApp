"""
Unit tests for orders/realtime_worker.py — the "keep today's order
summary continuously up to date" loop. Covers: UTC+8 date computation,
unconditional overwrite-on-refresh, fetch-failure handling, and the
day-rollover "one final refresh of the day that just ended" logic
(run_cycle), against an in-memory SQLite DB (same monkeypatch pattern as
test_orders_backend.py).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import db.base as db_base
from db.models import Base, OrderSummary, OrderSummaryRun, OrderSummaryRunStatus
from monitoring.models import MonitoringError
from orders.mapping import OrderRecord
from orders.realtime_worker import refresh_date, run_cycle, today_utc8


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(db_base, "_engine", engine)
    monkeypatch.setattr(db_base, "_SessionLocal", SessionLocal)
    yield SessionLocal


class FakeOrdersClient:
    """Spy: records every date fetch_day was called with, and returns
    whatever's configured for that date (default: empty -- a quiet
    day)."""

    def __init__(self, records_by_date: dict[str, list] | None = None, fail_dates: set[str] | None = None):
        self.records_by_date = records_by_date or {}
        self.fail_dates = fail_dates or set()
        self.fetch_calls: list[str] = []

    async def fetch_day(self, date_str: str):
        self.fetch_calls.append(date_str)
        if date_str in self.fail_dates:
            raise MonitoringError(f"simulated failure for {date_str}")
        return self.records_by_date.get(date_str, [])


def _order(device_app="NEXUS", order_money="120.00", orange_num=3, **kw) -> OrderRecord:
    return OrderRecord(
        order_id=kw.get("order_id", "1"), order_code=kw.get("order_code", "X"), device_id="205",
        device_app=device_app, order_status="Completed", payment_status="Have paid", delivery_status="Success",
        order_money=Decimal(order_money), orange_num=orange_num, orange_weight=Decimal("200"),
        pay_type="UPI", goods_name="orange juice", cup_num=1, createtime=0,
        order_date=kw.get("order_date", "2026-08-24"), raw={},
    )


# --- today_utc8 ---

def test_today_utc8_uses_target_timezone_not_utc():
    # 2026-08-24 00:30 UTC is already 2026-08-24 08:30 in UTC+8 -- same
    # day here, but proves the conversion is actually applied (a naive
    # UTC read would show the same date only by coincidence for this
    # particular instant; the boundary tests below prove the real case).
    now = datetime(2026, 8, 23, 17, 0, 0, tzinfo=timezone.utc)  # = 2026-08-24 01:00 UTC+8
    assert today_utc8(now) == "2026-08-24"


def test_today_utc8_just_before_utc8_midnight():
    now = datetime(2026, 8, 23, 15, 59, 0, tzinfo=timezone.utc)  # = 2026-08-23 23:59 UTC+8
    assert today_utc8(now) == "2026-08-23"


# --- refresh_date ---

@pytest.mark.asyncio
async def test_refresh_date_success_saves_groups(db):
    client = FakeOrdersClient(records_by_date={"2026-08-24": [_order(order_money="120.00")]})
    ok = await refresh_date(client, "2026-08-24")
    assert ok is True

    with db_base.get_session() as session:
        rows = session.execute(select(OrderSummary).where(OrderSummary.date == "2026-08-24")).scalars().all()
        assert len(rows) == 1
        assert rows[0].number_of_orders == 1
        run = session.execute(select(OrderSummaryRun).where(OrderSummaryRun.date == "2026-08-24")).scalar_one()
        assert run.status == OrderSummaryRunStatus.SUCCESS


@pytest.mark.asyncio
async def test_refresh_date_failure_returns_false_and_does_not_save(db):
    client = FakeOrdersClient(fail_dates={"2026-08-24"})
    ok = await refresh_date(client, "2026-08-24")
    assert ok is False

    with db_base.get_session() as session:
        rows = session.execute(select(OrderSummary).where(OrderSummary.date == "2026-08-24")).scalars().all()
        assert rows == []
        run = session.execute(select(OrderSummaryRun).where(OrderSummaryRun.date == "2026-08-24")).scalar_one_or_none()
        assert run is None  # a failed refresh cycle leaves no run row -- next cycle just retries cleanly


@pytest.mark.asyncio
async def test_refresh_date_overwrites_not_accumulates(db):
    """The core requirement: repeated refreshes of the same (still
    in-progress) day reflect the LATEST snapshot, not a growing pile of
    duplicate rows."""
    client = FakeOrdersClient(records_by_date={"2026-08-24": [_order()]})
    await refresh_date(client, "2026-08-24")

    client.records_by_date["2026-08-24"] = [_order(), _order(), _order()]  # 3 orders now, later in the day
    await refresh_date(client, "2026-08-24")

    with db_base.get_session() as session:
        rows = session.execute(select(OrderSummary).where(OrderSummary.date == "2026-08-24")).scalars().all()
        assert len(rows) == 1  # still one group row (same device_app/price/pay_type), not two
        assert rows[0].number_of_orders == 3  # reflects the newer snapshot
        runs = session.execute(select(OrderSummaryRun).where(OrderSummaryRun.date == "2026-08-24")).scalars().all()
        assert len(runs) == 1  # not duplicated either


@pytest.mark.asyncio
async def test_refresh_date_overwrites_even_if_already_success(db):
    """Unlike orders/backfill.py, this never checks get_cached_dates()
    first -- a SUCCESS row from an earlier cycle must not block the next
    one from overwriting it."""
    client = FakeOrdersClient(records_by_date={"2026-08-24": [_order()]})
    await refresh_date(client, "2026-08-24")
    with db_base.get_session() as session:
        assert session.execute(select(OrderSummaryRun).where(OrderSummaryRun.date == "2026-08-24")).scalar_one().status == OrderSummaryRunStatus.SUCCESS

    client.records_by_date["2026-08-24"] = [_order(), _order()]
    ok = await refresh_date(client, "2026-08-24")
    assert ok is True
    with db_base.get_session() as session:
        rows = session.execute(select(OrderSummary).where(OrderSummary.date == "2026-08-24")).scalars().all()
        assert rows[0].number_of_orders == 2


# --- run_cycle (day-rollover handling) ---

def _today_fn_sequence(dates: list[str]):
    it = iter(dates)
    return lambda: next(it)


@pytest.mark.asyncio
async def test_run_cycle_first_call_only_refreshes_current_date(db):
    client = FakeOrdersClient()
    today_fn = _today_fn_sequence(["2026-08-24"])
    last_seen = await run_cycle(client, None, today_fn=today_fn)
    assert last_seen == "2026-08-24"
    assert client.fetch_calls == ["2026-08-24"]  # no "previous day" refresh on the very first cycle


@pytest.mark.asyncio
async def test_run_cycle_same_date_twice_no_extra_refresh(db):
    client = FakeOrdersClient()
    today_fn = _today_fn_sequence(["2026-08-24"])
    last_seen = await run_cycle(client, "2026-08-24", today_fn=today_fn)
    assert last_seen == "2026-08-24"
    assert client.fetch_calls == ["2026-08-24"]  # exactly one fetch, not two


@pytest.mark.asyncio
async def test_run_cycle_date_rollover_refreshes_both_days(db):
    """The requirement this exists for: when the UTC+8 date changes
    between cycles, the day that just ended gets ONE FINAL refresh
    (capturing whatever landed between the last tick and midnight)
    before the new day starts being tracked."""
    client = FakeOrdersClient()
    today_fn = _today_fn_sequence(["2026-08-25"])
    last_seen = await run_cycle(client, "2026-08-24", today_fn=today_fn)
    assert last_seen == "2026-08-25"
    # Old day refreshed first (final snapshot), then the new day.
    assert client.fetch_calls == ["2026-08-24", "2026-08-25"]
