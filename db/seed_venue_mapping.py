"""
Seeds/updates the machine_name -> venue_provider lookup table
(VenueMapping) used to scope venue_partner users' Order Summary view to
their own venue's machine(s) — see backend/api/orders.py.

Idempotent (upsert by machine_name) — safe to re-run after adding a new
machine or correcting a venue name.

Run:
    python -m db.seed_venue_mapping
    python -m db.seed_venue_mapping --set "PNR=PNR Felicity"

With no --set flags, seeds the mapping known as of 2026-08-24:
    PNR     -> PNR Felicity
    NEXUS   -> Forum Kormangala
    Gravity -> Prestige Tech Park
"""
from __future__ import annotations

import argparse

from sqlalchemy import select

from db import base as db_base
from db.models import VenueMapping

DEFAULT_MAPPING = {
    "PNR": "PNR Felicity",
    "NEXUS": "Forum Kormangala",
    "Gravity": "Prestige Tech Park",
}


def upsert(session, machine_name: str, venue_provider: str) -> str:
    row = session.execute(
        select(VenueMapping).where(VenueMapping.machine_name == machine_name)
    ).scalar_one_or_none()
    if row is None:
        session.add(VenueMapping(machine_name=machine_name, venue_provider=venue_provider))
        return "created"
    if row.venue_provider != venue_provider:
        row.venue_provider = venue_provider
        return "updated"
    return "unchanged"


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed/update the machine -> venue_provider mapping")
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="MACHINE=VENUE",
        help="Add/override one mapping, e.g. --set 'Warehouse=Test Site'. Repeatable.",
    )
    args = parser.parse_args()

    mapping = dict(DEFAULT_MAPPING)
    for item in args.set:
        if "=" not in item:
            raise SystemExit(f"--set expects MACHINE=VENUE, got: {item!r}")
        machine, venue = item.split("=", 1)
        mapping[machine.strip()] = venue.strip()

    db_base.init_engine()
    db_base.create_all()

    with db_base.get_session() as session:
        for machine_name, venue_provider in mapping.items():
            action = upsert(session, machine_name, venue_provider)
            print(f"{action}: {machine_name} -> {venue_provider}")


if __name__ == "__main__":
    main()
