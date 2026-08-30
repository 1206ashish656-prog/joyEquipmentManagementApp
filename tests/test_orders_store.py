"""
Unit tests for orders/store.py against an in-memory SQLite DB (same
db_session fixture pattern as test_state_manager.py) — the "already
processed" cache-detection logic and idempotent re-save behavior.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from db.models import OrderPaymentRecord, OrderSummary, OrderSummaryRun, OrderSummaryRunStatus
from orders.mapping import OrderRecord
from orders.store import get_cached_dates, mark_day_failed, save_day, save_order_payment_records
from orders.summary import GroupSummary


def _group(date="2026-08-23", device_app="NEXUS", price="120.00", pay_type="UPI", n=5):
    return GroupSummary(
        date=date, device_app=device_app, price=Decimal(price), pay_type=pay_type,
        number_of_orders=n, total_number_of_oranges=n * 3, average_juice_weight=Decimal("205.00"),
    )


def test_no_dates_cached_initially(db_session):
    assert get_cached_dates(db_session) == set()


def test_save_day_marks_date_cached(db_session):
    save_day(db_session, "2026-08-23", [_group()], raw_orders_fetched=10)
    assert get_cached_dates(db_session) == {"2026-08-23"}


def test_save_day_writes_group_rows(db_session):
    save_day(db_session, "2026-08-23", [_group(device_app="NEXUS"), _group(device_app="Gravity")], raw_orders_fetched=10)
    rows = db_session.execute(select(OrderSummary).where(OrderSummary.date == "2026-08-23")).scalars().all()
    assert len(rows) == 2
    assert {r.device_app for r in rows} == {"NEXUS", "Gravity"}


def test_quiet_day_zero_groups_still_marked_success(db_session):
    """A day with genuinely zero qualifying orders must still be
    recorded as processed, or it would be re-fetched forever."""
    save_day(db_session, "2026-08-23", [], raw_orders_fetched=50)
    assert get_cached_dates(db_session) == {"2026-08-23"}
    run = db_session.execute(select(OrderSummaryRun).where(OrderSummaryRun.date == "2026-08-23")).scalar_one()
    assert run.status == OrderSummaryRunStatus.SUCCESS
    assert run.qualifying_orders == 0
    assert run.raw_orders_fetched == 50


def test_resaving_a_date_replaces_rows_not_duplicates(db_session):
    save_day(db_session, "2026-08-23", [_group(device_app="NEXUS", n=5)], raw_orders_fetched=10)
    save_day(db_session, "2026-08-23", [_group(device_app="NEXUS", n=999)], raw_orders_fetched=20)

    rows = db_session.execute(select(OrderSummary).where(OrderSummary.date == "2026-08-23")).scalars().all()
    assert len(rows) == 1
    assert rows[0].number_of_orders == 999

    runs = db_session.execute(select(OrderSummaryRun).where(OrderSummaryRun.date == "2026-08-23")).scalars().all()
    assert len(runs) == 1  # not duplicated either


def test_mark_day_failed_does_not_count_as_cached(db_session):
    mark_day_failed(db_session, "2026-08-23", "target unavailable")
    assert get_cached_dates(db_session) == set()
    run = db_session.execute(select(OrderSummaryRun).where(OrderSummaryRun.date == "2026-08-23")).scalar_one()
    assert run.status == OrderSummaryRunStatus.FAILED
    assert run.error_message == "target unavailable"


def test_failed_then_successful_retry_updates_same_run_row(db_session):
    mark_day_failed(db_session, "2026-08-23", "boom")
    save_day(db_session, "2026-08-23", [_group()], raw_orders_fetched=10)

    assert get_cached_dates(db_session) == {"2026-08-23"}
    runs = db_session.execute(select(OrderSummaryRun).where(OrderSummaryRun.date == "2026-08-23")).scalars().all()
    assert len(runs) == 1
    assert runs[0].status == OrderSummaryRunStatus.SUCCESS
    assert runs[0].error_message is None


def test_multiple_dates_independent(db_session):
    save_day(db_session, "2026-08-22", [_group(date="2026-08-22")], raw_orders_fetched=5)
    save_day(db_session, "2026-08-23", [_group(date="2026-08-23")], raw_orders_fetched=5)
    assert get_cached_dates(db_session) == {"2026-08-22", "2026-08-23"}


# --- save_order_payment_records (services/reconciliation.py's data source) ---

def _order_record(
    order_id="1001", order_code="20260823001", device_app="NEXUS", order_date="2026-08-23",
    order_money="120.00", pay_type="UPI", payment_status="Have paid", out_trade_no="30388839606",
    createtime=1787940861, paytime=1787940900,
):
    return OrderRecord(
        order_id=order_id, order_code=order_code, device_id="205", device_app=device_app,
        order_status="Completed", payment_status=payment_status, delivery_status="Success",
        order_money=Decimal(order_money), orange_num=2, orange_weight=Decimal("200"),
        pay_type=pay_type, goods_name="orange juice", cup_num=1,
        createtime=createtime, order_date=order_date, out_trade_no=out_trade_no, paytime=paytime,
    )


def test_save_order_payment_records_stores_all_orders(db_session):
    records = [_order_record(order_id="1"), _order_record(order_id="2", payment_status="Non-payment", out_trade_no="")]
    stored = save_order_payment_records(db_session, "2026-08-23", records)

    assert stored == 2
    rows = db_session.execute(select(OrderPaymentRecord).where(OrderPaymentRecord.order_date == "2026-08-23")).scalars().all()
    assert len(rows) == 2  # both stored, not just the "qualifying"/paid one


def test_save_order_payment_records_preserves_out_trade_no(db_session):
    save_order_payment_records(db_session, "2026-08-23", [_order_record(order_id="1", out_trade_no="30388839606")])
    row = db_session.execute(select(OrderPaymentRecord)).scalar_one()
    assert row.out_trade_no == "30388839606"
    assert row.pay_type == "UPI"
    assert row.order_money == Decimal("120.00")
    assert row.paid_at is not None


def test_save_order_payment_records_blank_out_trade_no_stored_as_null(db_session):
    save_order_payment_records(db_session, "2026-08-23", [_order_record(order_id="1", out_trade_no="")])
    row = db_session.execute(select(OrderPaymentRecord)).scalar_one()
    assert row.out_trade_no is None


def test_save_order_payment_records_skips_rows_without_order_id(db_session):
    stored = save_order_payment_records(db_session, "2026-08-23", [_order_record(order_id="")])
    assert stored == 0
    assert db_session.execute(select(OrderPaymentRecord)).scalars().all() == []


def test_resaving_a_date_replaces_payment_records_not_duplicates(db_session):
    save_order_payment_records(db_session, "2026-08-23", [_order_record(order_id="1", order_money="120.00")])
    save_order_payment_records(db_session, "2026-08-23", [_order_record(order_id="1", order_money="150.00")])

    rows = db_session.execute(select(OrderPaymentRecord)).scalars().all()
    assert len(rows) == 1
    assert rows[0].order_money == Decimal("150.00")


def test_save_order_payment_records_scoped_per_date(db_session):
    save_order_payment_records(db_session, "2026-08-22", [_order_record(order_id="1", order_date="2026-08-22")])
    save_order_payment_records(db_session, "2026-08-23", [_order_record(order_id="2", order_date="2026-08-23")])

    assert len(db_session.execute(select(OrderPaymentRecord)).scalars().all()) == 2
    # Re-saving one date must not touch the other date's rows.
    save_order_payment_records(db_session, "2026-08-23", [_order_record(order_id="3", order_date="2026-08-23")])
    remaining_ids = {r.order_id for r in db_session.execute(select(OrderPaymentRecord)).scalars().all()}
    assert remaining_ids == {"1", "3"}
