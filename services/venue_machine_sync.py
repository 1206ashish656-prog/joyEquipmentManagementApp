"""
Auto-maps a Venue (the master list, /venues, db/models.py's Venue) to a
machine of the exact same name -- per explicit request: "sync the
venues available on venues database automatically maps to same
machine." Closes the gap that caused a real reported bug: a venue
partner user assigned to a venue that existed at /venues, with no
VenueMapping row ever created for it, saw "no machine mapped to your
venue" on Order Summary.

Deliberately narrow: only fires when there is EXACTLY ONE Equipment row
whose name matches the venue name case-insensitively, and only when
that machine isn't already mapped to some venue_provider (never
silently steals a mapping away from whatever it currently points at,
and never guesses when zero or multiple machines could match) -- same
"never turn uncertainty into a wrong action" principle already used by
monitoring/health_engine.py's UNKNOWN-on-uncertainty rule. A venue
whose real machine has a DIFFERENT name (e.g. "PNR Felicity" -> "PNR",
"Forum Kormangala" -> "NEXUS") will never auto-match here and still
needs the existing manual `python -m db.seed_venue_mapping --set
"PNR=PNR Felicity"` path -- this doesn't replace that, it only handles
the common case of a venue named after its one machine (e.g. newer
machines renamed to their location on the target app itself, like
"Navi").
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Equipment, VenueMapping


def sync_venue_to_matching_machine(db: Session, venue_name: str) -> str | None:
    """Returns the machine name it just mapped, or None if nothing
    happened (no exact single match, or that machine already has a
    mapping of its own)."""
    venue_name = venue_name.strip()
    if not venue_name:
        return None

    matches = db.execute(
        select(Equipment).where(func.lower(Equipment.name) == venue_name.lower())
    ).scalars().all()
    if len(matches) != 1:
        return None
    machine_name = matches[0].name

    already_mapped = db.execute(
        select(VenueMapping).where(VenueMapping.machine_name == machine_name)
    ).scalar_one_or_none()
    if already_mapped is not None:
        return None

    db.add(VenueMapping(machine_name=machine_name, venue_provider=venue_name))
    return machine_name


def sync_all_venues(db: Session) -> list[tuple[str, str]]:
    """Runs sync_venue_to_matching_machine for every Venue currently on
    record -- called from GET /venues so the page is self-healing over
    time as venues/machines are added, with no separate backfill step
    ever needed. Returns [(venue_name, machine_name), ...] for whatever
    got newly mapped, so the caller can surface it if useful."""
    from db.models import Venue  # local import: avoids a hard dependency for callers that only need the single-venue function

    venue_names = [v for (v,) in db.execute(select(Venue.name))]
    synced = []
    for name in venue_names:
        machine_name = sync_venue_to_matching_machine(db, name)
        if machine_name:
            synced.append((name, machine_name))
    return synced
