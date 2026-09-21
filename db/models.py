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
    LargeBinary,
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
    fault_log_entries: Mapped[list["FaultLogEntry"]] = relationship(
        back_populates="incident",
        cascade="all, delete-orphan",
        order_by="FaultLogEntry.occurred_at.desc()",
    )


class FaultLogEntry(Base):
    """One row from the target application's own Equipment Management >
    Fault Information tab (device/device_fault_log -- see
    monitoring/selectors.py's DEVICE_FAULT_LOG_API_PATH and
    monitoring/fault_codes.py for the code -> description translation),
    captured at the moment a FaultIncident opens or escalates. This is the
    richer, per-component detail a bare fault_type string doesn't carry
    (e.g. "Electronic scale Malfunction" instead of just "Malfunction").

    Best-effort by design: monitoring/worker.py fetches these AFTER the
    incident itself is already persisted, so a failure fetching or
    translating them never blocks incident detection or the alert email --
    it only means that email/page goes out with less detail this time.
    """
    __tablename__ = "fault_log_entry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("fault_incident.id"), index=True)
    # The target's own device_fault_log.id -- preserved for audit/dedup,
    # never used as our own primary key (this app's ids are independent).
    target_log_id: Mapped[int] = mapped_column(Integer)
    # Raw pinyin slug (e.g. "dianzicheng") preserved exactly as the target
    # sent it (requirement #10), alongside our translated, ready-to-display
    # text (e.g. "Electronic scale Malfunction").
    component_code: Mapped[str] = mapped_column(String(128), default="")
    component_description: Mapped[str] = mapped_column(String(255), default="")
    is_stop: Mapped[bool] = mapped_column(Boolean, default=False)
    is_clean: Mapped[bool] = mapped_column(Boolean, default=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    incident: Mapped["FaultIncident"] = relationship(back_populates="fault_log_entries")


class FaultLogHistory(Base):
    """The COMPLETE historical per-component fault log for one piece of
    equipment, backfilled from the target application's own Equipment
    Management > Fault Information tab (monitoring/fault_log_backfill.py,
    monitoring/lightweight_client.py's get_fault_log_history) -- not tied
    to any FaultIncident, unlike FaultLogEntry above.

    This is the SOLE source for the Senior Management Report's "Downtime
    per Machine" section (services/management_report.py) -- chosen over
    FaultIncident specifically because the target's own log has real
    historical depth (matching orders/backfill.py's own backfilled order
    history), while FaultIncident only ever has data from whenever this
    app's own polling started running. Re-running the backfill is always
    safe: the unique constraint below makes it idempotent.
    """
    __tablename__ = "fault_log_history"
    __table_args__ = (
        UniqueConstraint("equipment_id", "target_log_id", name="uq_fault_log_history_target_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    equipment_id: Mapped[int] = mapped_column(ForeignKey("equipment.id"), index=True)
    target_log_id: Mapped[int] = mapped_column(Integer)  # the target's own device_fault_log.id
    component_code: Mapped[str] = mapped_column(String(128), default="")
    component_description: Mapped[str] = mapped_column(String(255), default="")
    # is_stop ("Record" on the target's own UI) is what actually gates
    # whether a row counts as downtime (services/management_report.py) --
    # a component fault that never stopped the machine isn't downtime.
    is_stop: Mapped[bool] = mapped_column(Boolean, default=False)
    is_clean: Mapped[bool] = mapped_column(Boolean, default=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


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
    # Gates changing ANY user's role to/from "admin" (backend/api/users.py) --
    # a regular admin can still edit everything else (name/email/venue/
    # active/password) on any user, and can freely switch a non-admin
    # between operations/venue_partner. Only a super admin can touch the
    # admin role itself. Bootstrapped by db/seed_admin.py for exactly one
    # account; promote/demote anyone else only via /users/{id}/edit once
    # a super admin exists. NOTE: this is a new column on an existing
    # live table -- see README.md's RBAC section for the manual
    # `ALTER TABLE users ADD COLUMN is_super_admin ...` needed on any
    # already-existing database (this project has no Alembic; create_all()
    # only creates missing tables, never alters existing ones).
    is_super_admin: Mapped[bool] = mapped_column(Boolean, default=False)
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


class AlertRecipient(Base):
    """A plain email address that gets every Critical-severity alert
    (malfunction/offline — same trigger as the admin-always rule in
    services/alert_engine.py), independent of any dashboard User account.
    For people who need malfunction alerts but should never need to log
    into this app — the admin-editable counterpart to AlertSubscription,
    which requires a real User row. Admin-managed at /alert-recipients."""

    __tablename__ = "alert_recipient"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)  # admin's own reference label, optional
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


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


class OrderPaymentRecord(Base):
    """One row per individual order (not aggregated, unlike OrderSummary)
    -- exists specifically to support PayU reconciliation
    (services/reconciliation.py, backend/api/reconciliation.py), which
    needs to match one machine-recorded sale to one gateway transaction
    and OrderSummary's grouped-by-(date, device_app, price, pay_type)
    shape can't do that. Populated going forward by
    orders/realtime_worker.py and orders/backfill.py, right alongside
    (not instead of) the existing OrderSummary aggregation -- see
    orders/store.py's save_order_payment_records().

    out_trade_no is the reconciliation key: the merchant-supplied
    reference sent to the target's PayU-shaped payment gateway
    (confirmed live 2026-08-30 -- the target's own per-order "clients"
    object exposes upi_key/upi_url* fields whose endpoint path matches
    PayU's own /merchant/postservice API), and per explicit confirmation
    from the account owner, the same "external order id" PayU's own
    transaction records reference alongside PayU's own internal id.
    Unique on order_id (the target's own numeric order id) so re-running
    a backfill for an already-recorded date never duplicates rows.
    """
    __tablename__ = "order_payment_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # target's own numeric order id
    order_code: Mapped[str] = mapped_column(String(64), index=True)
    device_app: Mapped[str] = mapped_column(String(128), index=True)  # equipment name, e.g. "NEXUS"
    order_date: Mapped[str] = mapped_column(String(10), index=True)  # 'YYYY-MM-DD', IST calendar day
    order_money: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    pay_type: Mapped[str] = mapped_column(String(32))  # e.g. "UPI" -- only UPI orders are PayU-eligible
    payment_status: Mapped[str] = mapped_column(String(64), default="")  # pay_state_text, e.g. "Have paid"
    out_trade_no: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at_target: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # order's own createtime
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # order's own paytime
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


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


class Venue(Base):
    """Master venue registry (admin-managed at /venues) for recurring
    rent cost-generation (services/recurring_costs.py) -- distinct from
    VenueMapping above, which maps machine_name -> a free-text
    venue_provider string and has no "one row per venue", rent, or
    active concept (a venue with 3 machines has 3 VenueMapping rows).
    `name` is expected to match the same venue_provider string used
    elsewhere (VenueMapping.venue_provider, User.venue_provider) so
    admins recognize it as the same venue app-wide, but this is a plain
    string match, not a hard FK -- adding a Venue here never risks
    either existing system.
    """
    __tablename__ = "venue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    # NULL = registered but not (yet) charged rent through this
    # feature -- e.g. a company-owned location. See recurring_costs.py.
    monthly_rent: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, onupdate=_utcnow)


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
    __table_args__ = (
        # Guarantees "one entry per venue/staff per period" (explicit
        # requirement) at the DB level, even if Generate is clicked twice
        # for the same month -- see services/recurring_costs.py. Every
        # ordinary manually-logged entry has all three columns NULL;
        # Postgres treats each NULL as distinct in a unique constraint,
        # so manual entries never collide with each other or with this.
        UniqueConstraint(
            "recurring_source_type", "recurring_source_id", "recurring_period",
            name="uq_cost_entry_recurring",
        ),
    )

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

    # Set only for an entry auto-generated by
    # services/recurring_costs.py (from a Venue's rent or a Staff
    # member's salary) -- NULL for every ordinary manually-logged entry.
    # recurring_source_id is a loose pointer (Venue.id or Staff.id,
    # distinguished by recurring_source_type) rather than a real FK --
    # a single column can't FK to two different tables, and at this
    # app's scale a plain integer plus the type discriminator is simpler
    # than a polymorphic-association table. recurring_period is
    # 'YYYY-MM'.
    recurring_source_type: Mapped[str | None] = mapped_column(String(32), nullable=True)  # "venue_rent" | "staff_salary"
    recurring_source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recurring_period: Mapped[str | None] = mapped_column(String(7), nullable=True)


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
    # Optional (requirement: "ask admin to add email information for new
    # staff (optional)") -- lets a staff member be quick-added to the
    # Alert Recipients list (backend/api/alert_recipients.py) without
    # retyping their email, and lets offboarding auto-deactivate that
    # recipient row (see staff.py's _deactivate_alert_recipient).
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Optional -- when set, this staff member is a candidate for the
    # monthly "Staff Salaries" recurring-cost entry (see
    # services/recurring_costs.py). NULL means this staff member is
    # tracked for leave/advances only, not payroll.
    monthly_salary: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    employment_start_date: Mapped[str] = mapped_column(String(10))  # 'YYYY-MM-DD'
    # NULL = still employed. Set on offboarding, never deleted, so past
    # leave records stay attributable to a real employment period.
    employment_end_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    leaves: Mapped[list["StaffLeave"]] = relationship(back_populates="staff", cascade="all, delete-orphan")
    advances: Mapped[list["StaffAdvance"]] = relationship(back_populates="staff", cascade="all, delete-orphan")


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
    # A half-day leave is always a single day (start_date == end_date,
    # enforced in backend/api/staff.py, not here) counted as 0.5 days
    # instead of 1 in staff/leave_summary.py — there's no concept of a
    # half-day *range*, only a half-day on one specific date.
    is_half_day: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    staff: Mapped["Staff"] = relationship(back_populates="leaves")


class StaffAdvance(Base):
    """One immutable ledger row per advance payment given to a staff
    member -- who, how much, when, optional note, who logged it. No
    repayment/deduction tracking (not requested) -- same
    log-and-never-edit precedent as StaffLeave/InventoryLogEntry. New
    table, so create_all() picks it up on any existing database with no
    migration needed (unlike User.is_super_admin above)."""

    __tablename__ = "staff_advance"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    date: Mapped[str] = mapped_column(String(10))  # 'YYYY-MM-DD', same convention as CostEntry.date/StaffLeave dates
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    staff: Mapped["Staff"] = relationship(back_populates="advances")


# --- Inventory management (admin/operations — see backend/api/inventory.py) ---
#
# A FIXED set of 10 known consumables, each holding its CURRENT stock —
# architecturally closer to EquipmentCurrentState (a live "now" row) than
# to CostEntry's open-ended, period-summarized ledger. Item creation is
# seed-script-only (db/seed_inventory_items.py); there's no in-app "add a
# new item" flow, matching the fixed list in the spec.

INVENTORY_ITEM_ORDER = (
    "oranges", "sealing_films", "glasses", "straws", "kitchen_cleaner",
    "dustbin_bags", "floor_cleaner", "orange_refill_bags", "shower_caps", "gloves",
)

INVENTORY_ENTRY_TYPES = ("usage", "correction")


class InventoryItem(Base):
    __tablename__ = "inventory_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # stable slug, e.g. "oranges" — matches config/inventory_rules.yaml
    name: Mapped[str] = mapped_column(String(255))  # display label, e.g. "Oranges"
    unit: Mapped[str] = mapped_column(String(32))  # display unit for current_stock, e.g. "boxes", "pieces"
    # ONLY set for glasses/straws -- a pure conversion aid for the
    # form/display. current_stock is ALWAYS in the item's base unit
    # (pieces for glasses/straws, otherwise its own unit) so the
    # usage-decrement/set-stock code never needs item-type branching.
    pieces_per_carton: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_stock: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class InventoryLogEntry(Base):
    """One audit-trail table for BOTH usage and stock corrections/restocks
    (entry_type discriminator) -- they share identical columns and both
    feed the same low-stock crossing-check and the same "recent activity"
    view, so two tables would just force that view to union two queries
    for no modeling benefit. Immutable once created (like StaffLeave) —
    a mistaken entry is corrected via a new "correction" row, never
    edited/deleted, so the audit trail is always complete."""

    __tablename__ = "inventory_log_entry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("inventory_item.id"), index=True)
    entry_type: Mapped[str] = mapped_column(String(16), index=True)  # one of INVENTORY_ENTRY_TYPES
    date: Mapped[str] = mapped_column(String(10))  # 'YYYY-MM-DD', same convention as CostEntry.date -- may be backdated
    quantity_change: Mapped[int] = mapped_column(Integer)  # signed delta actually applied to current_stock
    resulting_stock: Mapped[int] = mapped_column(Integer)  # current_stock immediately after this entry
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# --- Senior Management Report generation jobs (admin-only, see
# backend/api/reports.py + services/report_job_worker.py) ---
#
# Per explicit request: the report's computation and PDF rendering run
# in the background worker process (services/report_job_worker.py,
# wired into monitoring/combined_worker.py as a third loop), never
# inline in a web request -- "an isolated report generation process."
# The web process only ever creates a PENDING row here and later reads
# it back; it never calls services/management_report.py or
# services/report_pdf.py itself.


class ReportJobStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


_report_job_status_type = Enum(ReportJobStatus, name="report_job_status", native_enum=False, length=16)


class ReportJob(Base):
    __tablename__ = "report_job"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    requested_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # The exact request, re-resolved into a concrete date range at
    # creation time (backend/period_utils.py's period_range()) so the
    # worker never needs to re-interpret "as of today" after the fact.
    period: Mapped[str] = mapped_column(String(16))
    as_of: Mapped[str] = mapped_column(String(10))
    start: Mapped[str] = mapped_column(String(10))
    end: Mapped[str] = mapped_column(String(10))
    equipment_id: Mapped[int | None] = mapped_column(ForeignKey("equipment.id"), nullable=True)  # NULL = all machines
    # Per explicit request: the day-by-day sales table (report.daily,
    # already computed unconditionally by services/management_report.py
    # for its own "sales over time" chart) only renders in the PDF when
    # this is explicitly turned on -- default off, so the common case
    # (a monthly/aggregated report) stays concise.
    include_datewise_sales: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[ReportJobStatus] = mapped_column(_report_job_status_type, default=ReportJobStatus.PENDING, index=True)
    # Stored directly in Postgres rather than on disk -- this app has no
    # other on-disk file-storage convention, a report PDF is on the
    # order of tens of KB, and keeping it in the same database as
    # everything else means one backup covers it too, with no separate
    # file-lifecycle/cleanup concern.
    pdf_data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
