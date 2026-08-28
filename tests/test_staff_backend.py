"""FastAPI tests for the Staff & Leave Management tab (/staff) --
admin-only, same TestClient + StaticPool SQLite pattern as
test_costs_backend.py."""
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
from db.models import Base, Staff, StaffAdvance, StaffLeave, User


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


def _add_staff(SessionLocal, name, start="2026-01-01", end=None) -> int:
    with SessionLocal() as session:
        s = Staff(name=name, employment_start_date=start, employment_end_date=end)
        session.add(s)
        session.commit()
        session.refresh(s)
        return s.id


def test_staff_requires_login(client):
    test_client, _ = client
    resp = test_client.get("/staff", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_operations_cannot_view_staff(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "ops@example.com")
    assert test_client.get("/staff").status_code == 403


def test_venue_partner_cannot_view_staff(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "venue@example.com", "venue_partner")
    _login(test_client, "venue@example.com")
    assert test_client.get("/staff").status_code == 403


def test_admin_can_view_staff(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")
    resp = test_client.get("/staff")
    assert resp.status_code == 200
    assert "Staff" in resp.text


def test_create_staff(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.post(
        "/staff",
        data={"name": "Priya", "employment_start_date": "2026-06-01"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        staff = session.execute(select(Staff)).scalar_one()
        assert staff.name == "Priya"
        assert staff.employment_end_date is None


def test_offboard_staff_sets_end_date(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    staff_id = _add_staff(SessionLocal, "Priya")
    _login(test_client, "admin@example.com")

    test_client.post(f"/staff/{staff_id}/offboard", data={"end_date": "2026-08-24"}, follow_redirects=False)
    with SessionLocal() as session:
        staff = session.get(Staff, staff_id)
        assert staff.employment_end_date == "2026-08-24"


def test_log_leave_for_staff(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    staff_id = _add_staff(SessionLocal, "Priya")
    _login(test_client, "admin@example.com")

    resp = test_client.post(
        "/staff/leaves",
        data={"staff_id": staff_id, "start_date": "2026-08-10", "end_date": "2026-08-13", "reason": "Family event"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        leave = session.execute(select(StaffLeave)).scalar_one()
        assert leave.staff_id == staff_id
        assert leave.reason == "Family event"


def test_leave_summary_highlights_staff_over_threshold(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    staff_id = _add_staff(SessionLocal, "Priya")
    _login(test_client, "admin@example.com")

    test_client.post("/staff/leaves", data={"staff_id": staff_id, "start_date": "2026-08-10", "end_date": "2026-08-13"})

    resp = test_client.get("/staff?year=2026&month=8")
    assert resp.status_code == 200
    assert "Exceeds 2 days" in resp.text


def test_leave_summary_does_not_highlight_short_leave(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    staff_id = _add_staff(SessionLocal, "Priya")
    _login(test_client, "admin@example.com")

    test_client.post("/staff/leaves", data={"staff_id": staff_id, "start_date": "2026-08-10", "end_date": "2026-08-11"})

    resp = test_client.get("/staff?year=2026&month=8")
    assert resp.status_code == 200
    assert "Exceeds 2 days" not in resp.text


# --- Department / sub-department mapping ---

def test_create_staff_with_department_and_sub_department(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.post(
        "/staff",
        data={
            "name": "Employee 1", "department": "Operations", "sub_department": "Logistics",
            "employment_start_date": "2026-06-01",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        staff = session.execute(select(Staff).where(Staff.name == "Employee 1")).scalar_one()
        assert staff.department == "Operations"
        assert staff.sub_department == "Logistics"


def test_create_staff_without_department_leaves_it_null(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    test_client.post("/staff", data={"name": "Priya", "employment_start_date": "2026-06-01"})
    with SessionLocal() as session:
        staff = session.execute(select(Staff).where(Staff.name == "Priya")).scalar_one()
        assert staff.department is None
        assert staff.sub_department is None


def test_department_and_sub_department_shown_in_roster(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    test_client.post(
        "/staff",
        data={"name": "Employee 1", "department": "Operations", "sub_department": "Logistics", "employment_start_date": "2026-06-01"},
    )
    resp = test_client.get("/staff")
    assert resp.status_code == 200
    assert "Operations" in resp.text
    assert "Logistics" in resp.text


# --- Edit staff (incl. undoing a mistaken "mark as left") ---

def test_edit_form_prefilled(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    staff_id = _add_staff(SessionLocal, "Priya", start="2026-01-15")
    _login(test_client, "admin@example.com")

    resp = test_client.get(f"/staff/{staff_id}/edit")
    assert resp.status_code == 200
    assert 'value="Priya"' in resp.text
    assert 'value="2026-01-15"' in resp.text


def test_edit_updates_name_and_department(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    staff_id = _add_staff(SessionLocal, "Priya")
    _login(test_client, "admin@example.com")

    resp = test_client.post(
        f"/staff/{staff_id}/edit",
        data={
            "name": "Priya Sharma", "department": "Operations", "sub_department": "Logistics",
            "employment_start_date": "2026-01-01",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        staff = session.get(Staff, staff_id)
        assert staff.name == "Priya Sharma"
        assert staff.department == "Operations"
        assert staff.sub_department == "Logistics"


def test_edit_clears_employment_end_date_reactivating_staff(client):
    """The core requirement: an employee mistakenly marked as left can
    be reset to active by clearing the end date via edit."""
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    staff_id = _add_staff(SessionLocal, "Priya", end="2026-08-20")
    _login(test_client, "admin@example.com")

    test_client.post(
        f"/staff/{staff_id}/edit",
        data={"name": "Priya", "employment_start_date": "2026-01-01", "employment_end_date": ""},
    )
    with SessionLocal() as session:
        staff = session.get(Staff, staff_id)
        assert staff.employment_end_date is None


def test_reactivated_staff_shows_mark_as_left_again(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    staff_id = _add_staff(SessionLocal, "Priya", end="2026-08-20")
    _login(test_client, "admin@example.com")

    # Before edit: marked as left, so the (only) staff member has no
    # "Mark as left" action showing -- they're already left.
    before = test_client.get("/staff").text
    assert "Mark as left" not in before

    test_client.post(
        f"/staff/{staff_id}/edit",
        data={"name": "Priya", "employment_start_date": "2026-01-01", "employment_end_date": ""},
    )

    after = test_client.get("/staff")
    assert after.status_code == 200
    assert "Mark as left" in after.text
    assert "— active —" in after.text


def test_edit_can_also_set_employment_end_date_directly(client):
    """Edit isn't only for undoing offboarding -- it can set/correct the
    end date directly too, not just clear it."""
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    staff_id = _add_staff(SessionLocal, "Priya")
    _login(test_client, "admin@example.com")

    test_client.post(
        f"/staff/{staff_id}/edit",
        data={"name": "Priya", "employment_start_date": "2026-01-01", "employment_end_date": "2026-08-15"},
    )
    with SessionLocal() as session:
        staff = session.get(Staff, staff_id)
        assert staff.employment_end_date == "2026-08-15"


def test_edit_nonexistent_staff_redirects_without_error(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.post(
        "/staff/999999/edit",
        data={"name": "Nobody", "employment_start_date": "2026-01-01"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_operations_cannot_edit_staff(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    staff_id = _add_staff(SessionLocal, "Priya")
    _login(test_client, "ops@example.com")

    assert test_client.get(f"/staff/{staff_id}/edit").status_code == 403
    resp = test_client.post(
        f"/staff/{staff_id}/edit",
        data={"name": "Hacked", "employment_start_date": "2026-01-01"},
    )
    assert resp.status_code == 403
    with SessionLocal() as session:
        assert session.get(Staff, staff_id).name == "Priya"  # unchanged


# --- Delete staff (2026-08-28) ---

def test_delete_staff_removes_staff_and_cascades_leaves(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    staff_id = _add_staff(SessionLocal, "Priya")
    with SessionLocal() as session:
        session.add(StaffLeave(staff_id=staff_id, start_date="2026-08-01", end_date="2026-08-02"))
        session.commit()
    _login(test_client, "admin@example.com")

    resp = test_client.post(f"/staff/{staff_id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    with SessionLocal() as session:
        assert session.get(Staff, staff_id) is None
        assert session.execute(select(StaffLeave).where(StaffLeave.staff_id == staff_id)).scalars().all() == []


def test_delete_staff_also_removes_their_advances(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    staff_id = _add_staff(SessionLocal, "Priya")
    with SessionLocal() as session:
        session.add(StaffAdvance(staff_id=staff_id, amount=Decimal("500.00"), date="2026-08-01"))
        session.commit()
    _login(test_client, "admin@example.com")

    test_client.post(f"/staff/{staff_id}/delete", follow_redirects=False)
    with SessionLocal() as session:
        assert session.execute(select(StaffAdvance).where(StaffAdvance.staff_id == staff_id)).scalars().all() == []


def test_operations_cannot_delete_staff(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    staff_id = _add_staff(SessionLocal, "Priya")
    _login(test_client, "ops@example.com")

    resp = test_client.post(f"/staff/{staff_id}/delete")
    assert resp.status_code == 403
    with SessionLocal() as session:
        assert session.get(Staff, staff_id) is not None  # not deleted


def test_delete_nonexistent_staff_is_a_no_op_not_an_error(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.post("/staff/999999/delete", follow_redirects=False)
    assert resp.status_code == 303


# --- Staff advance payments (2026-08-28) ---

def test_log_advance_for_staff(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    staff_id = _add_staff(SessionLocal, "Priya")
    _login(test_client, "admin@example.com")

    resp = test_client.post(
        "/staff/advances",
        data={"staff_id": staff_id, "amount": "1500.00", "date": "2026-08-28", "note": "Festival advance"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        advance = session.execute(select(StaffAdvance).where(StaffAdvance.staff_id == staff_id)).scalar_one()
        assert advance.amount == Decimal("1500.00")
        assert advance.note == "Festival advance"
        assert advance.created_by_user_id is not None


def test_advance_total_shown_in_roster(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    staff_id = _add_staff(SessionLocal, "Priya")
    _login(test_client, "admin@example.com")

    test_client.post("/staff/advances", data={"staff_id": staff_id, "amount": "1000.00", "date": "2026-08-01"})
    test_client.post("/staff/advances", data={"staff_id": staff_id, "amount": "500.00", "date": "2026-08-15"})

    resp = test_client.get("/staff")
    assert "1500.00" in resp.text


def test_operations_cannot_log_advance(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    staff_id = _add_staff(SessionLocal, "Priya")
    _login(test_client, "ops@example.com")

    resp = test_client.post("/staff/advances", data={"staff_id": staff_id, "amount": "500.00", "date": "2026-08-28"})
    assert resp.status_code == 403
    with SessionLocal() as session:
        assert session.execute(select(StaffAdvance)).scalars().all() == []
