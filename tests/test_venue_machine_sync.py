"""Unit tests for services/venue_machine_sync.py -- auto-mapping a Venue
to a same-named machine. Uses the shared db_session fixture
(tests/conftest.py)."""
from __future__ import annotations

from sqlalchemy import select

from db.models import Equipment, VenueMapping
from services.venue_machine_sync import sync_all_venues, sync_venue_to_matching_machine


def _add_equipment(db, name, external_id) -> Equipment:
    eq = Equipment(external_id=external_id, equipment_code=f"CODE-{external_id}", name=name)
    db.add(eq)
    db.flush()
    return eq


def test_syncs_exact_case_insensitive_name_match(db_session):
    _add_equipment(db_session, "Navi", "105")

    result = sync_venue_to_matching_machine(db_session, "navi")  # different case than the machine's own

    assert result == "Navi"
    mapping = db_session.execute(select(VenueMapping)).scalar_one()
    assert mapping.machine_name == "Navi"
    assert mapping.venue_provider == "navi"  # stores the venue's own spelling, not the machine's


def test_no_match_when_no_machine_has_that_name(db_session):
    _add_equipment(db_session, "NEXUS", "205")

    result = sync_venue_to_matching_machine(db_session, "PNR Felicity")

    assert result is None
    assert db_session.execute(select(VenueMapping)).scalars().all() == []


def test_no_match_when_multiple_machines_share_the_name(db_session):
    """Ambiguous -- never guess which one the venue actually means."""
    _add_equipment(db_session, "Navi", "105")
    _add_equipment(db_session, "Navi", "106")

    result = sync_venue_to_matching_machine(db_session, "Navi")

    assert result is None
    assert db_session.execute(select(VenueMapping)).scalars().all() == []


def test_does_not_steal_a_machine_already_mapped_to_another_venue(db_session):
    _add_equipment(db_session, "Navi", "105")
    db_session.add(VenueMapping(machine_name="Navi", venue_provider="Some Other Venue"))
    db_session.flush()

    result = sync_venue_to_matching_machine(db_session, "Navi")

    assert result is None
    mapping = db_session.execute(select(VenueMapping)).scalar_one()
    assert mapping.venue_provider == "Some Other Venue"  # untouched


def test_blank_venue_name_is_a_no_op(db_session):
    _add_equipment(db_session, "Navi", "105")

    assert sync_venue_to_matching_machine(db_session, "   ") is None
    assert db_session.execute(select(VenueMapping)).scalars().all() == []


def test_sync_all_venues_maps_every_matching_one(db_session):
    from db.models import Venue

    _add_equipment(db_session, "Navi", "105")
    _add_equipment(db_session, "Pacific", "74")
    db_session.add(Venue(name="Navi", active=True))
    db_session.add(Venue(name="Pacific", active=True))
    db_session.add(Venue(name="PNR Felicity", active=True))  # no machine literally named this -- stays unmapped
    db_session.flush()

    synced = sync_all_venues(db_session)

    assert set(synced) == {("Navi", "Navi"), ("Pacific", "Pacific")}
    mappings = {m.venue_provider: m.machine_name for m in db_session.execute(select(VenueMapping)).scalars().all()}
    assert mappings == {"Navi": "Navi", "Pacific": "Pacific"}


def test_sync_all_venues_is_idempotent(db_session):
    from db.models import Venue

    _add_equipment(db_session, "Navi", "105")
    db_session.add(Venue(name="Navi", active=True))
    db_session.flush()

    sync_all_venues(db_session)
    second_run = sync_all_venues(db_session)  # nothing new to do

    assert second_run == []
    assert len(db_session.execute(select(VenueMapping)).scalars().all()) == 1
