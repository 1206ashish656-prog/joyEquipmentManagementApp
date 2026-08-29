"""
Recurring cost generation (Cost Management dashboard's "Recurring
Costs" section): derives exactly what's due for a given month directly
from the live Venue and Staff databases -- one Rent entry per active
Venue with a monthly_rent set, one Staff Salaries entry per active
(not-yet-offboarded) Staff member with a monthly_salary set -- rather
than an admin retyping the same rent/salary figures every month. The
candidate list updates itself automatically as venues/staff are
onboarded, edited, or offboarded (requirement: "backend active venues
and staff list so that entry automatically updates").

Admin-triggered only (backend/api/costs.py's generate_recurring_costs
route), per explicit decision -- nothing in this app silently writes a
financial record without an admin action; this module only computes
what WOULD be created and creates it on request, it never runs on a
timer.

Idempotent by construction, guaranteeing "one entry per venue/staff"
per period: CostEntry's (recurring_source_type, recurring_source_id,
recurring_period) has a DB-level unique constraint (db/models.py), so
calling generate() twice for the same month never double-creates --
already-generated candidates are reported back as such, not re-inserted.

Editing a Venue's rent or a Staff member's salary only affects FUTURE
periods -- an already-generated CostEntry for a past period is a real,
historical record and is never rewritten by this module (it can still
be corrected individually via the existing /costs/{id}/edit route,
same as any manually-logged entry).

Rent carries 18% GST (RENT_GST_RATE, per explicit request), added on
top of Venue.monthly_rent at generation time -- the venue's configured
rent stays the pre-GST base figure an admin negotiates/edits, and the
generated CostEntry.amount is the actual GST-inclusive total payable.
Staff salaries are never subject to GST. list_candidates() computes the
GST-inclusive `amount` (what will actually be generated/charged) so the
Cost Management preview table and the real generated entry can never
show two different numbers for the same candidate.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import CostEntry, Staff, Venue

RENT_CATEGORY = "Rent"
SALARY_CATEGORY = "Staff Salaries"
VENUE_SOURCE = "venue_rent"
STAFF_SOURCE = "staff_salary"
RENT_GST_RATE = Decimal("0.18")


def _round2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class RecurringCandidate:
    source_type: str  # VENUE_SOURCE | STAFF_SOURCE
    source_id: int
    label: str  # venue or staff name, for display
    base_amount: Decimal  # pre-GST rent, or the salary as-is (no GST)
    amount: Decimal  # what actually gets generated/charged -- GST-inclusive for rent
    already_generated: bool
    existing_entry_id: int | None = None

    @property
    def category(self) -> str:
        return RENT_CATEGORY if self.source_type == VENUE_SOURCE else SALARY_CATEGORY

    @property
    def gst_amount(self) -> Decimal:
        return self.amount - self.base_amount


def _period_start_date(period: str) -> str:
    """First of the period's month -- a sensible default `date` for the
    generated CostEntry; the admin can still edit the date afterward
    like any other entry."""
    return f"{period}-01"


def list_candidates(db: Session, period: str) -> list[RecurringCandidate]:
    """period: 'YYYY-MM'. One candidate per active Venue with a
    monthly_rent set, one per active Staff member with a monthly_salary
    set -- each flagged with whether that period's entry already
    exists, so the UI can show "pending" vs "already generated" without
    a second round-trip."""
    existing = {
        (e.recurring_source_type, e.recurring_source_id): e.id
        for e in db.execute(
            select(CostEntry).where(
                CostEntry.recurring_period == period,
                CostEntry.recurring_source_type.is_not(None),
            )
        ).scalars()
    }

    candidates: list[RecurringCandidate] = []

    venues = db.execute(
        select(Venue).where(Venue.active.is_(True), Venue.monthly_rent.is_not(None)).order_by(Venue.name)
    ).scalars().all()
    for v in venues:
        key = (VENUE_SOURCE, v.id)
        candidates.append(RecurringCandidate(
            source_type=VENUE_SOURCE, source_id=v.id, label=v.name,
            base_amount=v.monthly_rent, amount=_round2(v.monthly_rent * (1 + RENT_GST_RATE)),
            already_generated=key in existing, existing_entry_id=existing.get(key),
        ))

    staff_list = db.execute(
        select(Staff).where(Staff.employment_end_date.is_(None), Staff.monthly_salary.is_not(None)).order_by(Staff.name)
    ).scalars().all()
    for s in staff_list:
        key = (STAFF_SOURCE, s.id)
        candidates.append(RecurringCandidate(
            source_type=STAFF_SOURCE, source_id=s.id, label=s.name,
            base_amount=s.monthly_salary, amount=s.monthly_salary,  # no GST on salaries
            already_generated=key in existing, existing_entry_id=existing.get(key),
        ))

    return candidates


def generate(db: Session, period: str, admin_id: int | None) -> int:
    """Creates a CostEntry for every not-yet-generated candidate for
    this period; already-generated ones are left untouched. Returns how
    many were actually created."""
    created = 0
    for c in list_candidates(db, period):
        if c.already_generated:
            continue
        db.add(CostEntry(
            date=_period_start_date(period),
            category=c.category,
            # Venue rent: the venue IS the "vendor" for that row, same
            # role UNSPECIFIED_VENDOR plays for a manual entry -- lets a
            # by-vendor breakdown show rent per venue distinctly.
            # Staff salary: vendor is never applicable (matches
            # backend/api/costs.py's _resolve_category_and_vendor rule
            # for the Staff Salaries category on a manual entry too).
            vendor_name=None if c.source_type == STAFF_SOURCE else c.label,
            # The GST-inclusive amount is what's actually stored (see
            # module docstring) -- noted directly in item_name so it's
            # visible in the Raw Entries ledger without a schema change.
            item_name=f"{c.label} (Rent incl. 18% GST)" if c.source_type == VENUE_SOURCE else c.label,
            amount=c.amount,
            created_by_user_id=admin_id,
            recurring_source_type=c.source_type,
            recurring_source_id=c.source_id,
            recurring_period=period,
        ))
        created += 1
    return created
