"""
DB-backed persistence for order summaries — supersedes orders/cache.py's
CSV approach (kept in the tree as a standalone export utility, but no
longer part of the write path; see its module docstring).

A date counts as "already processed" once an OrderSummaryRun row with
status=SUCCESS exists for it — including a quiet day with zero
qualifying orders, which is a legitimate outcome, not a gap to keep
retrying (see db/models.py's OrderSummaryRun docstring).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import OrderPaymentRecord, OrderSummary, OrderSummaryRun, OrderSummaryRunStatus

from .mapping import OrderRecord
from .summary import GroupSummary


def get_cached_dates(session: Session) -> set[str]:
    rows = session.execute(
        select(OrderSummaryRun.date).where(OrderSummaryRun.status == OrderSummaryRunStatus.SUCCESS)
    ).scalars().all()
    return set(rows)


def save_day(session: Session, date: str, groups: list[GroupSummary], raw_orders_fetched: int) -> None:
    """Idempotent: safe to re-run for a date that already has data (e.g.
    with --force) — existing rows for that date are replaced, not
    duplicated or appended to."""
    existing_run = session.execute(select(OrderSummaryRun).where(OrderSummaryRun.date == date)).scalar_one_or_none()
    started_at = existing_run.started_at if existing_run else datetime.now(timezone.utc)

    # Replace any prior groups for this date (re-run safety).
    old_rows = session.execute(select(OrderSummary).where(OrderSummary.date == date)).scalars().all()
    for row in old_rows:
        session.delete(row)
    session.flush()

    for g in groups:
        session.add(
            OrderSummary(
                date=g.date,
                device_app=g.device_app,
                price=g.price,
                pay_type=g.pay_type,
                number_of_orders=g.number_of_orders,
                total_number_of_oranges=g.total_number_of_oranges,
                average_juice_weight=g.average_juice_weight,
            )
        )

    qualifying = sum(g.number_of_orders for g in groups)
    if existing_run:
        existing_run.status = OrderSummaryRunStatus.SUCCESS
        existing_run.completed_at = datetime.now(timezone.utc)
        existing_run.raw_orders_fetched = raw_orders_fetched
        existing_run.qualifying_orders = qualifying
        existing_run.error_message = None
    else:
        session.add(
            OrderSummaryRun(
                date=date,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                status=OrderSummaryRunStatus.SUCCESS,
                raw_orders_fetched=raw_orders_fetched,
                qualifying_orders=qualifying,
            )
        )


def save_order_payment_records(session: Session, date: str, records: list[OrderRecord]) -> int:
    """Persists individual orders for PayU reconciliation
    (services/reconciliation.py) -- separate from, and in addition to,
    save_day()'s aggregation. ALL orders for the day are stored here
    (not just the "qualifying" Completed+Success ones save_day() counts
    towards the summary), since a failed/cancelled order with a payment
    attempt is exactly the kind of row reconciliation needs to see (e.g.
    "PayU shows this was captured but the machine recorded it as
    Non-payment").

    Idempotent: replaces any existing rows for this date (same
    delete-then-reinsert pattern as save_day()) via order_id, so
    re-running a backfill never duplicates. Returns how many rows were
    stored."""
    old_rows = session.execute(
        select(OrderPaymentRecord).where(OrderPaymentRecord.order_date == date)
    ).scalars().all()
    for row in old_rows:
        session.delete(row)
    session.flush()

    stored = 0
    for r in records:
        if not r.order_id:
            continue
        session.add(
            OrderPaymentRecord(
                order_id=r.order_id,
                order_code=r.order_code,
                device_app=r.device_app,
                order_date=r.order_date,
                order_money=r.order_money,
                pay_type=r.pay_type,
                payment_status=r.payment_status,
                out_trade_no=r.out_trade_no or None,
                created_at_target=datetime.fromtimestamp(r.createtime, tz=timezone.utc) if r.createtime else datetime.now(timezone.utc),
                paid_at=datetime.fromtimestamp(r.paytime, tz=timezone.utc) if r.paytime else None,
            )
        )
        stored += 1
    return stored


def mark_day_failed(session: Session, date: str, error_message: str) -> None:
    existing_run = session.execute(select(OrderSummaryRun).where(OrderSummaryRun.date == date)).scalar_one_or_none()
    if existing_run:
        existing_run.status = OrderSummaryRunStatus.FAILED
        existing_run.completed_at = datetime.now(timezone.utc)
        existing_run.error_message = error_message
    else:
        session.add(
            OrderSummaryRun(
                date=date,
                completed_at=datetime.now(timezone.utc),
                status=OrderSummaryRunStatus.FAILED,
                error_message=error_message,
            )
        )
