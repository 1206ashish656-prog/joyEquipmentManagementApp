"""
SQLAlchemy ORM models for the entities in project spec section 16.

Deliberately uses only portable column types (String/Text/Integer/Boolean/
DateTime/Enum — no JSONB/ARRAY/etc.) so the same models work against both
real Postgres (production, via docker-compose.yml) and SQLite (fast,
dependency-free unit tests — see tests/test_state_manager.py). At this
scale (1-100 users, section 29) that portability is worth more than
Postgres-specific features we don't need yet.

health_state uses Python's HealthState enum everywhere — this is the
canonical equipment health from services/health_engine.py, NEVER the
target application's own raw `status` field (see requirement #6).
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class HealthState(str, enum.Enum):
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    MALFUNCTION = "MALFUNCTION"
    OFFLINE = "OFFLINE"
    UNKNOWN = "UNKNOWN"


# Health states that represent an active problem worth an incident.
# HEALTHY and UNKNOWN are deliberately excluded — UNKNOWN means "we
# couldn't classify this observation", not "something is wrong"
# (requirement #28: never turn uncertainty into a negative state, and
# never auto-resolve an incident just because a later poll was uncertain).
FAULT_STATES = frozenset({HealthState.WARNING, HealthState.MALFUNCTION, HealthState.OFFLINE})

_health_state_type = Enum(HealthState, name="health_state", native_enum=False, length=32)


class IncidentStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"


_incident_status_type = Enum(IncidentStatus, name="incident_status", native_enum=False, length=16)


class Equipment(Base):
    __tablename__ = "equipment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    equipment_code: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    device_type: Mapped[str] = mapped_column(String(128), default="")
    device_address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    advertising_group: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    current_state: Mapped["EquipmentCurrentState"] = relationship(
        back_populates="equipment", uselist=False, cascade="all, delete-orphan"
    )
    snapshots: Mapped[list["EquipmentSnapshot"]] = relationship(
        back_populates="equipment", cascade="all, delete-orphan"
    )
    incidents: Mapped[list["FaultIncident"]] = relationship(
        back_populates="equipment", cascade="all, delete-orphan"
    )


class EquipmentCurrentState(Base):
    __tablename__ = "equipment_current_state"

    equipment_id: Mapped[int] = mapped_column(ForeignKey("equipment.id"), primary_key=True)
    health_state: Mapped[HealthState] = mapped_column(_health_state_type)
    status: Mapped[str] = mapped_column(String(128), default="")
    network_status: Mapped[str] = mapped_column(String(128), default="")
    fault_type: Mapped[str] = mapped_column(String(255), default="")
    material_shortage_status: Mapped[str] = mapped_column(String(128), default="")
    remaining_oranges: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    equipment: Mapped["Equipment"] = relationship(back_populates="current_state")


class EquipmentSnapshot(Base):
    __tablename__ = "equipment_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    equipment_id: Mapped[int] = mapped_column(ForeignKey("equipment.id"), index=True)
    status: Mapped[str] = mapped_column(String(128), default="")
    network_status: Mapped[str] = mapped_column(String(128), default="")
    fault_type: Mapped[str] = mapped_column(String(255), default="")
    material_shortage_status: Mapped[str] = mapped_column(String(128), default="")
    remaining_oranges: Mapped[str | None] = mapped_column(String(64), nullable=True)
    health_state: Mapped[HealthState] = mapped_column(_health_state_type)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    equipment: Mapped["Equipment"] = relationship(back_populates="snapshots")


class FaultIncident(Base):
    __tablename__ = "fault_incident"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    equipment_id: Mapped[int] = mapped_column(ForeignKey("equipment.id"), index=True)
    # Exact raw fault_type text preserved from the target app at detection
    # time (requirement #10) — never overwritten with a generic label.
    fault_type: Mapped[str] = mapped_column(String(255), default="")
    severity: Mapped[str] = mapped_column(String(32), default="")
    previous_health: Mapped[HealthState | None] = mapped_column(_health_state_type, nullable=True)
    current_health: Mapped[HealthState] = mapped_column(_health_state_type)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[IncidentStatus] = mapped_column(_incident_status_type, default=IncidentStatus.ACTIVE, index=True)
    notification_sent: Mapped[bool] = mapped_column(Boolean, default=False)

    equipment: Mapped["Equipment"] = relationship(back_populates="incidents")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    role: Mapped[str] = mapped_column(String(32), default="user")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # Salted PBKDF2 hash for THIS app's own dashboard login — entirely
    # separate from the target application's credentials (which live only
    # in .env, never in this database). One-way hash only; there is no
    # decrypt path, satisfying requirement #17's "encrypted at rest" for
    # the strongest reasonable sense of that phrase. Never rendered in any
    # template or included in any API response.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    subscriptions: Mapped[list["AlertSubscription"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AlertSubscription(Base):
    __tablename__ = "alert_subscription"
    __table_args__ = (
        UniqueConstraint("user_id", "equipment_id", "severity", "notification_type", name="uq_alert_subscription"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # NULL equipment_id = subscribed to ALL machines.
    equipment_id: Mapped[int | None] = mapped_column(ForeignKey("equipment.id"), nullable=True, index=True)
    # NULL severity = subscribed to ALL severities.
    severity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notification_type: Mapped[str] = mapped_column(String(32), default="email")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    user: Mapped["User"] = relationship(back_populates="subscriptions")


class MonitoringRunStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


_monitoring_run_status_type = Enum(MonitoringRunStatus, name="monitoring_run_status", native_enum=False, length=16)


class MonitoringRun(Base):
    __tablename__ = "monitoring_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[MonitoringRunStatus] = mapped_column(_monitoring_run_status_type, default=MonitoringRunStatus.RUNNING)
    records_found: Mapped[int | None] = mapped_column(Integer, nullable=True)
    records_processed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


# --- Order summary (orders/ package — independent feature, see README) ---
#
# Stored at the FINEST grain the feature needs: one row per
# (date, device_app, price, pay_type) group. Every UI view (aggregated,
# machine-wise, price-wise, pay-type-wise, or any combination) is a
# rollup computed from these rows at query time (orders/rollup.py) —
# nothing coarser is stored, so no view is ever "missing" because it
# wasn't pre-computed. Within one group, price is constant by
# definition (that's the grouping key), so `price` is an exact value,
# not an average; rollups across multiple price groups compute a proper
# orders-weighted average.


class OrderSummaryRunStatus(str, enum.Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


_order_summary_run_status_type = Enum(
    OrderSummaryRunStatus, name="order_summary_run_status", native_enum=False, length=16
)


class OrderSummaryRun(Base):
    """One row per processed date — the definitive "is this date done"
    marker (mirrors MonitoringRun's role for the equipment poller). A
    date with genuinely zero qualifying orders still gets a SUCCESS row
    here with qualifying_orders=0, so it's never silently re-fetched
    forever, and is distinguishable from a date that failed to fetch."""

    __tablename__ = "order_summary_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[str] = mapped_column(String(10), unique=True, index=True)  # 'YYYY-MM-DD', target's UTC+8 calendar day
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[OrderSummaryRunStatus] = mapped_column(_order_summary_run_status_type)
    raw_orders_fetched: Mapped[int | None] = mapped_column(Integer, nullable=True)
    qualifying_orders: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class OrderSummary(Base):
    __tablename__ = "order_summary"
    __table_args__ = (
        UniqueConstraint("date", "device_app", "price", "pay_type", name="uq_order_summary_group"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[str] = mapped_column(String(10), index=True)  # 'YYYY-MM-DD', target's UTC+8 calendar day
    device_app: Mapped[str] = mapped_column(String(128), index=True)  # equipment name, e.g. "NEXUS"
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))  # exact order price for this group
    pay_type: Mapped[str] = mapped_column(String(32))  # e.g. "UPI"
    number_of_orders: Mapped[int] = mapped_column(Integer)
    total_number_of_oranges: Mapped[int] = mapped_column(Integer)
    average_juice_weight: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
