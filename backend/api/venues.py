"""
Venue master list (admin-only): onboard/edit venues and their monthly
rent for the Cost Management dashboard's recurring-cost generation
(services/recurring_costs.py). Deliberately no delete route -- a Venue
may already be referenced by past CostEntry rows (via
recurring_source_id, a loose pointer, not a hard FK -- see
db/models.py's CostEntry comment); deactivating instead (active=False)
removes it from future recurring-cost candidate lists without touching
that history, same rationale as AlertRecipient/User active toggles.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.deps import get_db, require_admin
from backend.templating import templates
from db.models import User, Venue, VenueMapping
from services.venue_machine_sync import sync_all_venues, sync_venue_to_matching_machine

router = APIRouter()


def _known_venue_names(db: Session) -> list[str]:
    """Existing venue_provider strings already in use for order-summary
    scoping (VenueMapping) -- offered as a <datalist> suggestion when
    onboarding a Venue here, so the same venue uses a matching name
    across the app rather than two subtly different spellings."""
    return sorted({v for (v,) in db.execute(select(VenueMapping.venue_provider).distinct())})


def _mapped_machines_by_venue(db: Session) -> dict[str, list[str]]:
    """venue_provider -> every machine_name currently mapped to it, for
    the "Mapped Machine(s)" column below -- makes the otherwise-invisible
    VenueMapping relationship visible right on this page instead of only
    discoverable by querying the database directly (the actual confusion
    behind the reported bug this module's sync function fixes)."""
    rows = db.execute(select(VenueMapping.venue_provider, VenueMapping.machine_name)).all()
    result: dict[str, list[str]] = {}
    for venue_provider, machine_name in rows:
        result.setdefault(venue_provider, []).append(machine_name)
    return result


def _parse_rent(raw: str) -> Decimal | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


@router.get("/venues", response_class=HTMLResponse)
def list_venues(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    # Self-healing: catches up any venue that doesn't yet have a
    # same-named machine mapped, every time this page loads -- no
    # separate backfill step ever needed for the common case (see
    # services/venue_machine_sync.py).
    sync_all_venues(db)
    venues = db.execute(select(Venue).order_by(Venue.name)).scalars().all()
    return templates.TemplateResponse(
        request,
        "venues.html",
        {
            "user": admin, "venues": venues, "known_venue_names": _known_venue_names(db),
            "mapped_machines_by_venue": _mapped_machines_by_venue(db), "error": None,
        },
    )


@router.post("/venues")
def create_venue(
    request: Request,
    name: str = Form(...),
    monthly_rent: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    name = name.strip()

    def _rerender(error: str):
        venues = db.execute(select(Venue).order_by(Venue.name)).scalars().all()
        return templates.TemplateResponse(
            request,
            "venues.html",
            {
                "user": admin, "venues": venues, "known_venue_names": _known_venue_names(db),
                "mapped_machines_by_venue": _mapped_machines_by_venue(db), "error": error,
            },
            status_code=400,
        )

    if not name:
        return _rerender("Venue name is required.")

    existing = db.execute(select(Venue).where(Venue.name == name)).scalar_one_or_none()
    if existing is not None:
        return _rerender(f"A venue named '{name}' already exists.")

    db.add(Venue(name=name, monthly_rent=_parse_rent(monthly_rent), active=True))
    db.flush()  # the new Venue row must be committed-visible before the sync query below can see it
    sync_venue_to_matching_machine(db, name)
    return RedirectResponse(url="/venues", status_code=303)


@router.get("/venues/{venue_id}/edit", response_class=HTMLResponse)
def edit_venue_form(
    venue_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    venue = db.get(Venue, venue_id)
    if venue is None:
        return templates.TemplateResponse(request, "not_found.html", {"user": admin}, status_code=404)
    return templates.TemplateResponse(request, "venue_edit.html", {"user": admin, "venue": venue, "error": None})


@router.post("/venues/{venue_id}/edit")
def update_venue(
    venue_id: int,
    request: Request,
    name: str = Form(...),
    monthly_rent: str = Form(""),
    active: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    venue = db.get(Venue, venue_id)
    if venue is None:
        return RedirectResponse(url="/venues", status_code=303)

    name = name.strip()
    if not name:
        return templates.TemplateResponse(
            request, "venue_edit.html", {"user": admin, "venue": venue, "error": "Venue name is required."},
            status_code=400,
        )

    existing = db.execute(select(Venue).where(Venue.name == name, Venue.id != venue_id)).scalar_one_or_none()
    if existing is not None:
        return templates.TemplateResponse(
            request,
            "venue_edit.html",
            {"user": admin, "venue": venue, "error": f"A venue named '{name}' already exists."},
            status_code=400,
        )

    venue.name = name
    venue.monthly_rent = _parse_rent(monthly_rent)
    venue.active = active == "on"
    sync_venue_to_matching_machine(db, name)
    return RedirectResponse(url="/venues", status_code=303)
