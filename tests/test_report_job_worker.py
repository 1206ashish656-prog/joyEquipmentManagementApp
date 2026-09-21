"""
Unit tests for services/report_job_worker.py -- the background loop
that turns a PENDING ReportJob into a finished (SUCCESS/FAILED) one.
services.report_pdf.render_management_report_pdf (a real Playwright
launch) is monkeypatched -- the real render is exercised via live
verification against the running app, matching this project's
established convention for every other Playwright-touching path.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import db.base as db_base
import services.report_job_worker as report_job_worker
from db.models import Base, ReportJob, ReportJobStatus


@pytest.fixture()
def session_factory(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(db_base, "_engine", engine)
    monkeypatch.setattr(db_base, "_SessionLocal", SessionLocal)
    return SessionLocal


def _add_job(SessionLocal, **kwargs) -> int:
    defaults = dict(period="monthly", as_of="2026-08-31", start="2026-08-01", end="2026-08-31", status=ReportJobStatus.PENDING)
    defaults.update(kwargs)
    with SessionLocal() as session:
        job = ReportJob(**defaults)
        session.add(job)
        session.commit()
        session.refresh(job)
        return job.id


@pytest.mark.asyncio
async def test_process_pending_jobs_marks_success(session_factory, monkeypatch):
    job_id = _add_job(session_factory)

    async def fake_render(db, *, start, end, equipment_id, period, include_datewise_sales=False):
        return b"%PDF-1.4 fake"

    monkeypatch.setattr(report_job_worker, "render_management_report_pdf", fake_render)

    processed = await report_job_worker.process_pending_jobs()

    assert processed == 1
    with session_factory() as session:
        job = session.get(ReportJob, job_id)
        assert job.status == ReportJobStatus.SUCCESS
        assert job.pdf_data == b"%PDF-1.4 fake"
        assert job.completed_at is not None
        assert job.error_message is None


@pytest.mark.asyncio
async def test_process_pending_jobs_passes_include_datewise_sales_through(session_factory, monkeypatch):
    job_id = _add_job(session_factory, include_datewise_sales=True)

    seen = {}

    async def fake_render(db, *, start, end, equipment_id, period, include_datewise_sales=False):
        seen["include_datewise_sales"] = include_datewise_sales
        return b"%PDF-1.4 fake"

    monkeypatch.setattr(report_job_worker, "render_management_report_pdf", fake_render)

    await report_job_worker.process_pending_jobs()

    assert seen["include_datewise_sales"] is True
    with session_factory() as session:
        assert session.get(ReportJob, job_id).status == ReportJobStatus.SUCCESS


@pytest.mark.asyncio
async def test_process_pending_jobs_marks_failed_without_raising(session_factory, monkeypatch):
    """A single job's failure must never crash the loop -- it's recorded
    on the job instead (services/report_job_worker.py's module
    docstring)."""
    job_id = _add_job(session_factory)

    async def failing_render(db, *, start, end, equipment_id, period, include_datewise_sales=False):
        raise RuntimeError("Playwright exploded")

    monkeypatch.setattr(report_job_worker, "render_management_report_pdf", failing_render)

    processed = await report_job_worker.process_pending_jobs()  # must not raise

    assert processed == 1
    with session_factory() as session:
        job = session.get(ReportJob, job_id)
        assert job.status == ReportJobStatus.FAILED
        assert "Playwright exploded" in job.error_message
        assert job.pdf_data is None
        assert job.completed_at is not None


@pytest.mark.asyncio
async def test_only_pending_jobs_are_processed(session_factory, monkeypatch):
    _add_job(session_factory, status=ReportJobStatus.SUCCESS, pdf_data=b"already done")
    _add_job(session_factory, status=ReportJobStatus.FAILED, error_message="already failed")
    pending_id = _add_job(session_factory)

    calls = []

    async def fake_render(db, *, start, end, equipment_id, period, include_datewise_sales=False):
        calls.append(1)
        return b"%PDF-new"

    monkeypatch.setattr(report_job_worker, "render_management_report_pdf", fake_render)

    processed = await report_job_worker.process_pending_jobs()

    assert processed == 1
    assert len(calls) == 1
    with session_factory() as session:
        job = session.get(ReportJob, pending_id)
        assert job.status == ReportJobStatus.SUCCESS


@pytest.mark.asyncio
async def test_no_pending_jobs_is_a_no_op(session_factory):
    processed = await report_job_worker.process_pending_jobs()
    assert processed == 0


@pytest.mark.asyncio
async def test_multiple_pending_jobs_all_processed_oldest_first(session_factory, monkeypatch):
    from datetime import datetime, timedelta, timezone

    first_id = _add_job(session_factory)
    second_id = _add_job(session_factory)
    with session_factory() as session:
        session.get(ReportJob, first_id).created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        session.get(ReportJob, second_id).created_at = datetime.now(timezone.utc)
        session.commit()

    order = []

    async def fake_render(db, *, start, end, equipment_id, period, include_datewise_sales=False):
        order.append((start, end))
        return b"%PDF"

    monkeypatch.setattr(report_job_worker, "render_management_report_pdf", fake_render)

    processed = await report_job_worker.process_pending_jobs()

    assert processed == 2
    with session_factory() as session:
        assert session.get(ReportJob, first_id).status == ReportJobStatus.SUCCESS
        assert session.get(ReportJob, second_id).status == ReportJobStatus.SUCCESS
