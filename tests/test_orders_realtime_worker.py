"""
Unit tests for orders/realtime_worker.py — the "keep today's order
summary continuously up to date" loop. Covers: IST date computation,
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
from orders.realtime_worker import reconcile_after_downtime, refresh_date, run_cycle, today_ist


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
    day). Also counts reload_cookies() calls, since run_cycle() is
    expected to call it once per cycle (see OrdersClient.reload_cookies)."""

    def __init__(self, records_by_date: dict[str, list] | None = None, fail_dates: set[str] | None = None):
        self.records_by_date = records_by_date or {}
        self.fail_dates = fail_dates or set()
        self.fetch_calls: list[str] = []
        self.reload_cookies_calls = 0

    def reload_cookies(self) -> None:
        self.reload_cookies_calls += 1

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


# --- today_ist ---

def test_today_ist_uses_ist_not_utc():
    # 2026-08-23 20:00 UTC is already 2026-08-24 01:30 in IST -- a naive
    # UTC read would still say 2026-08-23.
    now = datetime(2026, 8, 23, 20, 0, 0, tzinfo=timezone.utc)  # = 2026-08-24 01:30 IST
    assert today_ist(now) == "2026-08-24"


def test_today_ist_just_before_ist_midnight():
    now = datetime(2026, 8, 23, 18, 25, 0, tzinfo=timezone.utc)  # = 2026-08-23 23:55 IST
    assert today_ist(now) == "2026-08-23"


def test_today_ist_just_after_ist_midnight():
    now = datetime(2026, 8, 23, 18, 35, 0, tzinfo=timezone.utc)  # = 2026-08-24 00:05 IST
    assert today_ist(now) == "2026-08-24"


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
    """The requirement this exists for: when the IST date changes
    between cycles, the day that just ended gets ONE FINAL refresh
    (capturing whatever landed between the last tick and midnight)
    before the new day starts being tracked."""
    client = FakeOrdersClient()
    today_fn = _today_fn_sequence(["2026-08-25"])
    last_seen = await run_cycle(client, "2026-08-24", today_fn=today_fn)
    assert last_seen == "2026-08-25"
    # Old day refreshed first (final snapshot), then the new day.
    assert client.fetch_calls == ["2026-08-24", "2026-08-25"]


@pytest.mark.asyncio
async def test_run_cycle_reloads_cookies_exactly_once_per_cycle(db):
    """Needed so a fresh session written by monitoring.worker (running
    alongside this loop in monitoring/combined_worker.py) actually gets
    picked up -- see OrdersClient.reload_cookies()."""
    client = FakeOrdersClient()
    today_fn = _today_fn_sequence(["2026-08-24", "2026-08-24"])
    await run_cycle(client, None, today_fn=today_fn)
    assert client.reload_cookies_calls == 1
    await run_cycle(client, "2026-08-24", today_fn=today_fn)
    assert client.reload_cookies_calls == 2


# --- reconcile_after_downtime (startup gap-closing) ---

class FakeBackfill:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, start: str, end: str) -> None:
        self.calls.append((start, end))


async def _seed_run(SessionLocal_or_db, date: str, status=OrderSummaryRunStatus.SUCCESS):
    with db_base.get_session() as session:
        session.add(OrderSummaryRun(date=date, status=status, raw_orders_fetched=0))


@pytest.mark.asyncio
async def test_reconcile_does_nothing_on_first_ever_run(db):
    """No OrderSummaryRun history at all -- nothing to reconcile."""
    client = FakeOrdersClient()
    backfill = FakeBackfill()
    await reconcile_after_downtime(client, today_fn=lambda: "2026-08-27", backfill_fn=backfill)

    assert client.fetch_calls == []
    assert backfill.calls == []


@pytest.mark.asyncio
async def test_reconcile_does_nothing_when_restarted_same_day(db):
    await _seed_run(db, "2026-08-27")
    client = FakeOrdersClient()
    backfill = FakeBackfill()
    await reconcile_after_downtime(client, today_fn=lambda: "2026-08-27", backfill_fn=backfill)

    assert client.fetch_calls == []
    assert backfill.calls == []


@pytest.mark.asyncio
async def test_reconcile_force_refreshes_last_active_day_after_overnight_downtime(db):
    """The actual bug being fixed: app was down when the IST date rolled
    over, so the last day it was updating (already SUCCESS from its own
    earlier cycle) is stuck on a partial snapshot -- must get force-
    refreshed even though it's already "done" as far as backfill.py's
    caching would normally be concerned."""
    await _seed_run(db, "2026-08-26")
    client = FakeOrdersClient()
    backfill = FakeBackfill()
    await reconcile_after_downtime(client, today_fn=lambda: "2026-08-27", backfill_fn=backfill)

    assert client.fetch_calls == ["2026-08-26"]  # forced refresh via refresh_date, not the backfill path
    assert backfill.calls == []  # no gap -- yesterday IS the last known day


@pytest.mark.asyncio
async def test_reconcile_backfills_multi_day_gap(db):
    """App was down across several IST days -- the last active day gets
    force-refreshed directly, and the fully-missing days in between get
    handed to the real backfill machinery (which has its own
    skip-if-cached/fetch-if-missing logic)."""
    await _seed_run(db, "2026-08-20")
    client = FakeOrdersClient()
    backfill = FakeBackfill()
    await reconcile_after_downtime(client, today_fn=lambda: "2026-08-27", backfill_fn=backfill)

    assert client.fetch_calls == ["2026-08-20"]
    assert backfill.calls == [("2026-08-21", "2026-08-26")]


@pytest.mark.asyncio
async def test_reconcile_single_missing_day_gap(db):
    """last_known=2026-08-25, today=2026-08-27 -- exactly one fully-
    elapsed day (08-26) was never touched at all; it goes through the
    normal backfill path as a one-day range, while 08-25 (the day that
    WAS actively updating) gets the forced refresh_date() call instead."""
    await _seed_run(db, "2026-08-25")
    client = FakeOrdersClient()
    backfill = FakeBackfill()
    await reconcile_after_downtime(client, today_fn=lambda: "2026-08-27", backfill_fn=backfill)

    assert client.fetch_calls == ["2026-08-25"]
    assert backfill.calls == [("2026-08-26", "2026-08-26")]
