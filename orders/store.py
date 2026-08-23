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

from db.models import OrderSummary, OrderSummaryRun, OrderSummaryRunStatus

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
