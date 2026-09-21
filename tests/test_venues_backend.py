"""FastAPI tests for the Venues tab (/venues) -- admin-only, same
TestClient + StaticPool SQLite pattern as test_staff_backend.py."""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import db.base as db_base
from backend.main import app
from backend.security import hash_password
from db.models import Base, Equipment, User, Venue, VenueMapping


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(db_base, "_engine", engine)
    monkeypatch.setattr(db_base, "_SessionLocal", SessionLocal)
    test_client = TestClient(app)
    yield test_client, SessionLocal


def _add_user(SessionLocal, email, role, password="pw123456"):
    with SessionLocal() as session:
        session.add(User(name=email.split("@")[0], email=email, role=role, active=True, password_hash=hash_password(password)))
        session.commit()


def _login(client, email, password="pw123456"):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def _add_venue(SessionLocal, name, rent="10000.00", active=True) -> int:
    with SessionLocal() as session:
        v = Venue(name=name, monthly_rent=Decimal(rent) if rent is not None else None, active=active)
        session.add(v)
        session.commit()
        session.refresh(v)
        return v.id


def _add_equipment(SessionLocal, name, external_id):
    with SessionLocal() as session:
        session.add(Equipment(external_id=external_id, equipment_code=f"CODE-{external_id}", name=name))
        session.commit()


def test_requires_login(client):
    test_client, _ = client
    resp = test_client.get("/venues", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_operations_cannot_view(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "ops@example.com")
    resp = test_client.get("/venues")
    assert resp.status_code == 403


def test_create_venue(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.post(
        "/venues", data={"name": "PNR Felicity", "monthly_rent": "25000.00"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        v = session.execute(select(Venue)).scalar_one()
        assert v.name == "PNR Felicity"
        assert v.monthly_rent == Decimal("25000.00")
        assert v.active is True


def test_create_venue_without_rent_leaves_it_null(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    test_client.post("/venues", data={"name": "No Rent Venue"})
    with SessionLocal() as session:
        v = session.execute(select(Venue)).scalar_one()
        assert v.monthly_rent is None


def test_create_venue_rejects_duplicate_name(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_venue(SessionLocal, "PNR Felicity")
    _login(test_client, "admin@example.com")

    resp = test_client.post("/venues", data={"name": "PNR Felicity", "monthly_rent": "1"})
    assert resp.status_code == 400
    assert "already exists" in resp.text
    with SessionLocal() as session:
        assert len(session.execute(select(Venue)).scalars().all()) == 1


def test_create_venue_rejects_blank_name(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.post("/venues", data={"name": "  ", "monthly_rent": "1"})
    assert resp.status_code == 400
    with SessionLocal() as session:
        assert session.execute(select(Venue)).scalars().all() == []


def test_edit_venue_updates_rent_and_active(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    venue_id = _add_venue(SessionLocal, "PNR Felicity", rent="25000.00")
    _login(test_client, "admin@example.com")

    resp = test_client.post(
        f"/venues/{venue_id}/edit",
        data={"name": "PNR Felicity", "monthly_rent": "30000.00", "active": "on"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        v = session.get(Venue, venue_id)
        assert v.monthly_rent == Decimal("30000.00")
        assert v.active is True


def test_edit_venue_can_deactivate(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    venue_id = _add_venue(SessionLocal, "PNR Felicity")
    _login(test_client, "admin@example.com")

    test_client.post(
        f"/venues/{venue_id}/edit", data={"name": "PNR Felicity", "monthly_rent": "10000.00"},  # no "active" field = unchecked
    )
    with SessionLocal() as session:
        assert session.get(Venue, venue_id).active is False


def test_edit_nonexistent_venue_redirects_without_error(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.post("/venues/999/edit", data={"name": "Ghost", "monthly_rent": "1"}, follow_redirects=False)
    assert resp.status_code == 303


def test_known_venue_names_datalist_sourced_from_venue_mapping(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    with SessionLocal() as session:
        session.add(VenueMapping(machine_name="NEXUS", venue_provider="PNR Felicity"))
        session.commit()
    _login(test_client, "admin@example.com")

    resp = test_client.get("/venues")
    assert resp.status_code == 200
    assert "PNR Felicity" in resp.text


def test_venues_page_shows_gst_inclusive_total(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_venue(SessionLocal, "PNR Felicity", rent="25000.00")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/venues")
    assert resp.status_code == 200
    assert "25000.00" in resp.text  # base rent
    assert "29500.00" in resp.text  # incl. 18% GST


# --- Auto-sync to a same-named machine (services/venue_machine_sync.py) ---

def test_creating_a_venue_auto_maps_a_same_named_machine(client):
    """The actual reported bug this fixes: a venue existed with no
    machine mapped to it, and stayed that way forever with no manual
    step. Creating a venue named exactly like a real machine should
    just work immediately."""
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_equipment(SessionLocal, "Navi", "105")
    _login(test_client, "admin@example.com")

    test_client.post("/venues", data={"name": "Navi", "monthly_rent": ""})

    with SessionLocal() as session:
        mapping = session.execute(select(VenueMapping)).scalar_one()
        assert mapping.machine_name == "Navi"
        assert mapping.venue_provider == "Navi"


def test_creating_a_venue_with_no_matching_machine_stays_unmapped(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    test_client.post("/venues", data={"name": "PNR Felicity", "monthly_rent": ""})

    with SessionLocal() as session:
        assert session.execute(select(VenueMapping)).scalars().all() == []


def test_visiting_venues_page_self_heals_an_already_existing_gap(client):
    """A venue created BEFORE its matching machine existed (or before
    this fix shipped) gets synced the next time an admin just loads the
    page -- no manual backfill command required."""
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_venue(SessionLocal, "Navi")  # created with no matching Equipment row yet
    _add_equipment(SessionLocal, "Navi", "105")  # machine shows up later
    _login(test_client, "admin@example.com")

    resp = test_client.get("/venues")

    assert resp.status_code == 200
    with SessionLocal() as session:
        mapping = session.execute(select(VenueMapping)).scalar_one()
        assert mapping.machine_name == "Navi"


def test_venues_page_shows_mapped_machine_name(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_venue(SessionLocal, "Navi")
    _add_equipment(SessionLocal, "Navi", "105")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/venues")
    assert "Navi" in resp.text
    assert "No machine mapped" not in resp.text


def test_venues_page_warns_when_no_machine_mapped(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_venue(SessionLocal, "PNR Felicity")  # no matching machine, no manual mapping either
    _login(test_client, "admin@example.com")

    resp = test_client.get("/venues")
    assert "No machine mapped" in resp.text


def test_renaming_a_venue_to_match_a_machine_triggers_sync(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    venue_id = _add_venue(SessionLocal, "Old Name")
    _add_equipment(SessionLocal, "Navi", "105")
    _login(test_client, "admin@example.com")

    test_client.post(f"/venues/{venue_id}/edit", data={"name": "Navi", "monthly_rent": "1", "active": "on"})

    with SessionLocal() as session:
        mapping = session.execute(select(VenueMapping)).scalar_one()
        assert mapping.machine_name == "Navi"
        assert mapping.venue_provider == "Navi"


def test_auto_sync_never_steals_an_already_mapped_machine(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_equipment(SessionLocal, "Navi", "105")
    with SessionLocal() as session:
        session.add(VenueMapping(machine_name="Navi", venue_provider="A Different Venue Entirely"))
        session.commit()
    _login(test_client, "admin@example.com")

    test_client.post("/venues", data={"name": "Navi", "monthly_rent": ""})

    with SessionLocal() as session:
        mapping = session.execute(select(VenueMapping)).scalar_one()
        assert mapping.venue_provider == "A Different Venue Entirely"  # untouched
