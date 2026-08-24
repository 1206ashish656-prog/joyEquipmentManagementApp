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


class UserRole(str, enum.Enum):
    """The three access levels (spec: per-role view restriction).

    - ADMIN: full application access, unrestricted.
    - OPERATIONS: ground/ops staff — equipment monitoring only
      (dashboard, active faults, alert subscriptions). No order data.
    - VENUE_PARTNER: machine venue partners — order summary only,
      scoped to their own venue's machine(s) via User.venue_provider
      (see VenueMapping below). No equipment/monitoring access.
    """

    ADMIN = "admin"
    OPERATIONS = "operations"
    VENUE_PARTNER = "venue_partner"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Plain string (not a native DB enum) so it round-trips identically on
    # SQLite and Postgres, same rationale as health_state/incident_status
    # above — validated against UserRole at the application layer
    # (backend/api/users.py), not enforced by the column type.
    role: Mapped[str] = mapped_column(String(32), default=UserRole.OPERATIONS.value)
    # Only meaningful when role == VENUE_PARTNER — which venue_provider
    # (see VenueMapping.venue_provider) this user's order-summary view is
    # scoped to. NULL for admin/operations users, and for a venue_partner
    # who hasn't been assigned a venue yet (backend/api/orders.py shows an
    # explicit "no venue assigned" state rather than silently showing
    # nothing or everything).
    venue_provider: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
    date: Mapped[str] = mapped_column(String(10), unique=True, index=True)  # 'YYYY-MM-DD', IST calendar day (orders/mapping.py's IST_TZ)
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
    date: Mapped[str] = mapped_column(String(10), index=True)  # 'YYYY-MM-DD', IST calendar day (orders/mapping.py's IST_TZ)
    device_app: Mapped[str] = mapped_column(String(128), index=True)  # equipment name, e.g. "NEXUS"
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))  # exact order price for this group
    pay_type: Mapped[str] = mapped_column(String(32))  # e.g. "UPI"
    number_of_orders: Mapped[int] = mapped_column(Integer)
    total_number_of_oranges: Mapped[int] = mapped_column(Integer)
    average_juice_weight: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# --- Venue mapping (access control — see User.role/venue_provider above) ---
#
# A separate, independent table (not a column on Equipment/OrderSummary)
# because it's a pure lookup dimension maintained by an admin, decoupled
# from both the equipment poller and the order backfill — either could
# run without this table existing at all. machine_name matches
# OrderSummary.device_app / Equipment.name (e.g. "NEXUS", "Gravity");
# matching is done case-insensitively at query time (backend/api/orders.py)
# since the target app itself isn't consistent about casing.


class VenueMapping(Base):
    __tablename__ = "venue_mapping"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    machine_name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    venue_provider: Mapped[str] = mapped_column(String(255), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


# --- Cost management (admin-only — see backend/api/costs.py) ---
#
# Standard categories are UI suggestions, not a DB-enforced enum: picking
# "Others" in the form asks the admin to type the real category, and
# THAT text is what gets stored — so `category` stays a plain string,
# same rationale as health_state/role above (portable, and here also
# genuinely open-ended by design).

STANDARD_COST_CATEGORIES = (
    "Oranges", "Glass", "Straws", "Sealing Films", "Staff Salaries", "Rent", "Cleaning Items",
)

# Vendor is meaningless for Staff Salaries (no vendor to name) and is
# simply not asked/stored for that category. For every other category, a
# blank vendor still needs *something* filterable later rather than a
# NULL that's easy to lose track of — this is that placeholder.
UNSPECIFIED_VENDOR = "UNSPECIFIED"


class CostEntry(Base):
    __tablename__ = "cost_entry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[str] = mapped_column(String(10), index=True)  # 'YYYY-MM-DD', the cost's own date (admin-set, may be backdated)
    category: Mapped[str] = mapped_column(String(255), index=True)
    # NULL only for category == "Staff Salaries" (vendor isn't applicable
    # there); UNSPECIFIED_VENDOR for every other category left blank.
    vendor_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    item_name: Mapped[str] = mapped_column(String(255), index=True)  # e.g. "50kg Valencia oranges"
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # Set only on an edit (stays NULL for a never-edited entry) — lets the
    # UI show "edited" without needing a separate audit table for what's
    # still a lightweight, single-admin-editable record.
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, onupdate=_utcnow)


# --- Staff & leave management (admin-only — see backend/api/staff.py) ---


class Staff(Base):
    __tablename__ = "staff"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    # Free text, not an enum -- unlike cost categories, no standard list
    # was specified (e.g. "Operations team" / "Logistics"). Both nullable
    # for staff added before this field existed.
    department: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    sub_department: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    employment_start_date: Mapped[str] = mapped_column(String(10))  # 'YYYY-MM-DD'
    # NULL = still employed. Set on offboarding, never deleted, so past
    # leave records stay attributable to a real employment period.
    employment_end_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    leaves: Mapped[list["StaffLeave"]] = relationship(back_populates="staff", cascade="all, delete-orphan")


class StaffLeave(Base):
    """One row per leave PERIOD (not per day) — an admin logging "3 days
    off starting the 5th" is one row, not three. Day counts for the
    >2-days-in-a-month highlight (staff/leave_summary.py) are computed by
    clipping [start_date, end_date] to the month in question, so a leave
    spanning a month boundary is correctly split between both months."""

    __tablename__ = "staff_leave"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), index=True)
    start_date: Mapped[str] = mapped_column(String(10))  # 'YYYY-MM-DD'
    end_date: Mapped[str] = mapped_column(String(10))  # 'YYYY-MM-DD', inclusive, >= start_date
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    staff: Mapped["Staff"] = relationship(back_populates="leaves")
