"""FastAPI tests for the Senior Management Report control panel
(/reports/management) -- admin-only, same TestClient + StaticPool
SQLite pattern as test_costs_backend.py.

Per explicit request, report computation/PDF rendering now happens
ONLY in the background worker (services/report_job_worker.py) -- this
module's routes only ever create/read ReportJob rows, so these tests
never touch Playwright at all (no mocking needed here; the worker's own
tests, test_report_job_worker.py, cover the render step with a mock)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import db.base as db_base
from backend.main import app
from backend.security import hash_password
from db.models import Base, Equipment, ReportJob, ReportJobStatus, User


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


def _add_user(SessionLocal, email, role, password="pw123456") -> int:
    with SessionLocal() as session:
        u = User(name=email.split("@")[0], email=email, role=role, active=True, password_hash=hash_password(password))
        session.add(u)
        session.commit()
        session.refresh(u)
        return u.id


def _login(client, email, password="pw123456"):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def _add_equipment(SessionLocal, name, external_id) -> int:
    with SessionLocal() as session:
        eq = Equipment(external_id=external_id, equipment_code=f"CODE-{external_id}", name=name)
        session.add(eq)
        session.commit()
        session.refresh(eq)
        return eq.id


def _add_job(SessionLocal, *, status=ReportJobStatus.PENDING, requested_by=None, equipment_id=None,
             period="monthly", start="2026-08-01", end="2026-08-31", pdf_data=None, error_message=None) -> int:
    with SessionLocal() as session:
        job = ReportJob(
            requested_by_user_id=requested_by, period=period, as_of=end, start=start, end=end,
            equipment_id=equipment_id, status=status, pdf_data=pdf_data, error_message=error_message,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        return job.id


def test_requires_login(client):
    test_client, _ = client
    resp = test_client.get("/reports/management", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_operations_cannot_view(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "ops@example.com")
    resp = test_client.get("/reports/management")
    assert resp.status_code == 403


def test_venue_partner_cannot_view(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "vp@example.com", "venue_partner")
    _login(test_client, "vp@example.com")
    resp = test_client.get("/reports/management")
    assert resp.status_code == 403


def test_admin_can_view_empty_panel(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/reports/management")
    assert resp.status_code == 200
    assert "Senior Management Report" in resp.text
    assert "Generate Report" in resp.text
    assert "No reports generated yet" in resp.text


def test_create_report_job_creates_pending_row(client):
    test_client, SessionLocal = client
    admin_id = _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.post(
        "/reports/management/jobs",
        data={"period": "monthly", "as_of": "2026-08-15"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/reports/management"

    with SessionLocal() as session:
        job = session.execute(select(ReportJob)).scalar_one()
        assert job.status == ReportJobStatus.PENDING
        assert job.requested_by_user_id == admin_id
        assert job.start == "2026-08-01"
        assert job.end == "2026-08-31"
        assert job.equipment_id is None
        assert job.pdf_data is None


def test_create_report_job_scoped_to_a_machine(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    eq_id = _add_equipment(SessionLocal, "NEXUS", "205")
    _login(test_client, "admin@example.com")

    test_client.post("/reports/management/jobs", data={"period": "monthly", "as_of": "2026-08-15", "equipment_id": str(eq_id)})

    with SessionLocal() as session:
        job = session.execute(select(ReportJob)).scalar_one()
        assert job.equipment_id == eq_id


def test_operations_cannot_create_report_job(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "ops@example.com")

    resp = test_client.post("/reports/management/jobs", data={"period": "monthly", "as_of": "2026-08-15"})
    assert resp.status_code == 403
    with SessionLocal() as session:
        assert session.execute(select(ReportJob)).scalars().all() == []


def test_job_history_shows_status_and_requester(client):
    test_client, SessionLocal = client
    admin_id = _add_user(SessionLocal, "admin@example.com", "admin")
    _add_job(SessionLocal, status=ReportJobStatus.SUCCESS, requested_by=admin_id, pdf_data=b"%PDF-fake")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/reports/management")
    assert resp.status_code == 200
    assert "Ready" in resp.text
    assert "admin" in resp.text  # requester name (derived from email prefix)
    assert "Download PDF" in resp.text


def test_pending_job_shows_no_download_link(client):
    test_client, SessionLocal = client
    _add_job(SessionLocal, status=ReportJobStatus.PENDING)
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/reports/management")
    assert resp.status_code == 200
    assert "Pending" in resp.text
    assert "Download PDF" not in resp.text


def test_failed_job_shows_failed_status(client):
    test_client, SessionLocal = client
    _add_job(SessionLocal, status=ReportJobStatus.FAILED, error_message="Something broke")
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/reports/management")
    assert resp.status_code == 200
    assert "Failed" in resp.text
    assert "Download PDF" not in resp.text


def test_page_auto_refreshes_only_while_a_job_is_in_progress(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/reports/management")
    assert "http-equiv=\"refresh\"" not in resp.text

    _add_job(SessionLocal, status=ReportJobStatus.PENDING)
    resp = test_client.get("/reports/management")
    assert "http-equiv=\"refresh\"" in resp.text


def test_download_completed_job(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    job_id = _add_job(SessionLocal, status=ReportJobStatus.SUCCESS, pdf_data=b"%PDF-1.4 fake pdf bytes")
    _login(test_client, "admin@example.com")

    resp = test_client.get(f"/reports/management/jobs/{job_id}/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.content == b"%PDF-1.4 fake pdf bytes"


def test_download_scoped_job_filename_includes_machine_name(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    eq_id = _add_equipment(SessionLocal, "NEXUS", "205")
    job_id = _add_job(SessionLocal, status=ReportJobStatus.SUCCESS, equipment_id=eq_id, pdf_data=b"%PDF-fake")
    _login(test_client, "admin@example.com")

    resp = test_client.get(f"/reports/management/jobs/{job_id}/download")
    assert resp.status_code == 200
    assert "NEXUS" in resp.headers["content-disposition"]


def test_cannot_download_pending_job(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    job_id = _add_job(SessionLocal, status=ReportJobStatus.PENDING)
    _login(test_client, "admin@example.com")

    resp = test_client.get(f"/reports/management/jobs/{job_id}/download", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/reports/management"


def test_download_nonexistent_job_redirects(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/reports/management/jobs/999/download", follow_redirects=False)
    assert resp.status_code == 303


def test_operations_cannot_download(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    job_id = _add_job(SessionLocal, status=ReportJobStatus.SUCCESS, pdf_data=b"%PDF-fake")
    _login(test_client, "ops@example.com")

    resp = test_client.get(f"/reports/management/jobs/{job_id}/download")
    assert resp.status_code == 403
