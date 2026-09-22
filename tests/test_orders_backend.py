"""
FastAPI tests for the Order Summary tab (/orders/summary) — same
TestClient + StaticPool SQLite pattern as test_backend.py.
"""
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
from db.models import Base, OrderSummary, OrderSummaryRun, OrderSummaryRunStatus, ReportJob, ReportJobStatus, User, VenueMapping


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


def _login(client, email="admin@example.com", password="correct-horse"):
    with_user = client
    return with_user.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def _seed_user(SessionLocal):
    with SessionLocal() as session:
        session.add(User(name="Admin", email="admin@example.com", role="admin", active=True, password_hash=hash_password("correct-horse")))
        session.commit()


def _add_venue_partner(SessionLocal, email, venue_provider, password="pw123456"):
    with SessionLocal() as session:
        session.add(User(
            name=email.split("@")[0], email=email, role="venue_partner", venue_provider=venue_provider,
            active=True, password_hash=hash_password(password),
        ))
        session.commit()


def _seed_order_summary(SessionLocal, date, device_app, price, pay_type, n=5, oranges=15, juice_weight="200.00"):
    with SessionLocal() as session:
        session.add(OrderSummary(
            date=date, device_app=device_app, price=Decimal(price), pay_type=pay_type,
            number_of_orders=n, total_number_of_oranges=oranges, average_juice_weight=Decimal(juice_weight),
        ))
        existing = session.query(OrderSummaryRun).filter_by(date=date).one_or_none()
        if not existing:
            session.add(OrderSummaryRun(date=date, status=OrderSummaryRunStatus.SUCCESS, raw_orders_fetched=n, qualifying_orders=n))
        session.commit()


def test_orders_summary_requires_login(client):
    test_client, _ = client
    resp = test_client.get("/orders/summary", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_orders_summary_page_loads_with_no_data(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _login(test_client)
    resp = test_client.get("/orders/summary")
    assert resp.status_code == 200
    assert "Order Summary" in resp.text
    assert "No order data cached" in resp.text


# --- "Last updated" (backend/api/orders.py's _last_updated_at) ---
# Surfaces OrderSummaryRun.completed_at so it's obvious the numbers are
# live, not a stale one-time snapshot -- always paired with a fixed note
# naming the actual refresh cadence (orders/realtime_worker.py's
# DEFAULT_INTERVAL_SECONDS = 300).

def test_orders_summary_shows_last_updated_time_and_refresh_note(client):
    from datetime import datetime, timezone

    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=10)
    with SessionLocal() as session:
        run = session.query(OrderSummaryRun).filter_by(date="2026-08-23").one()
        run.completed_at = datetime(2026, 8, 23, 10, 15, 0, tzinfo=timezone.utc)
        session.commit()
    _login(test_client)

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23")
    assert resp.status_code == 200
    assert "Last updated" in resp.text
    assert "auto-refreshes every 5 minutes" in resp.text
    assert "23 Aug 2026" in resp.text  # the seeded completed_at, IST-formatted


def test_orders_summary_last_updated_falls_back_gracefully_with_no_data(client):
    """No OrderSummaryRun exists at all yet -- must render "—", not crash."""
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _login(test_client)

    resp = test_client.get("/orders/summary")
    assert resp.status_code == 200
    assert "Last updated" in resp.text
    assert "auto-refreshes every 5 minutes" in resp.text


def test_orders_summary_daily_shows_machine_breakdown(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=120, oranges=261, juice_weight="202.58")
    _seed_order_summary(SessionLocal, "2026-08-23", "Gravity", "120.00", "UPI", n=47, oranges=114, juice_weight="203.77")
    _login(test_client)

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23")
    assert resp.status_code == 200
    assert "NEXUS" in resp.text
    assert "Gravity" in resp.text
    assert "120" in resp.text  # total orders (120) somewhere in the tiles/table


def test_orders_summary_aggregate_hides_machine_column(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=10)
    _seed_order_summary(SessionLocal, "2026-08-23", "Gravity", "120.00", "UPI", n=5)
    _login(test_client)

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23&by_machine=0")
    assert resp.status_code == 200
    # aggregated total should combine both machines: 15 orders
    assert "aggregated" in resp.text.lower() or "(no breakdown" in resp.text.lower()


def test_orders_summary_mid_day_price_change_shows_two_rows(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=3)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "150.00", "UPI", n=2)
    _login(test_client)

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23&by_machine=1&by_price=1")
    assert resp.status_code == 200
    assert "120.00" in resp.text
    assert "150.00" in resp.text


def test_orders_summary_weekly_period_includes_multiple_days(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    # 2026-08-23 is a Sunday; the ISO week containing it is Aug 17-23.
    _seed_order_summary(SessionLocal, "2026-08-18", "NEXUS", "120.00", "UPI", n=10)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=20)
    _login(test_client)

    resp = test_client.get("/orders/summary?period=weekly&as_of=2026-08-23")
    assert resp.status_code == 200
    # weighted-average price is still 120.00, but total orders should be 30
    assert "30" in resp.text


def test_orders_summary_custom_range_uses_exact_start_and_end(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _seed_order_summary(SessionLocal, "2026-08-05", "NEXUS", "120.00", "UPI", n=7)   # just inside the range
    _seed_order_summary(SessionLocal, "2026-08-01", "NEXUS", "120.00", "UPI", n=99)  # before the range -- excluded
    _seed_order_summary(SessionLocal, "2026-08-20", "NEXUS", "120.00", "UPI", n=99)  # after the range -- excluded
    _login(test_client)

    resp = test_client.get("/orders/summary?period=custom&start=2026-08-04&end=2026-08-10")
    assert resp.status_code == 200
    assert "2026-08-04" in resp.text
    assert "2026-08-10" in resp.text
    assert "7" in resp.text
    assert "99" not in resp.text


def test_orders_summary_custom_range_option_present_in_dropdown(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _login(test_client)

    resp = test_client.get("/orders/summary")
    assert resp.status_code == 200
    assert "Custom range" in resp.text
    assert 'id="order-filter-start-field"' in resp.text
    assert 'id="order-filter-end-field"' in resp.text


def test_orders_summary_custom_range_malformed_falls_back_to_as_of(client):
    """Same "uncertainty never becomes a crash" rule period_range()
    already documents -- a missing/invalid start or end must not 500,
    it falls back to a single day."""
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _login(test_client)

    resp = test_client.get("/orders/summary?period=custom&as_of=2026-08-15")
    assert resp.status_code == 200
    assert "2026-08-15" in resp.text


def test_venue_partner_can_use_custom_range_scoped_to_their_venue(client):
    test_client, SessionLocal = client
    with SessionLocal() as session:
        from db.models import VenueMapping
        session.add(VenueMapping(machine_name="NEXUS", venue_provider="Forum Kormangala"))
        session.add(User(
            name="Venue", email="venue@example.com", role="venue_partner", venue_provider="Forum Kormangala",
            active=True, password_hash=hash_password("pw123456"),
        ))
        session.commit()
    _seed_order_summary(SessionLocal, "2026-08-05", "NEXUS", "120.00", "UPI", n=7)
    _login(test_client, "venue@example.com", "pw123456")

    resp = test_client.get("/orders/summary?period=custom&start=2026-08-04&end=2026-08-10")
    assert resp.status_code == 200
    assert "7" in resp.text


def test_orders_summary_chart_data_embedded_as_json(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=10)
    _login(test_client)

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23")
    assert "aggregateChartData" in resp.text
    assert "2026-08-23" in resp.text


def test_orders_summary_aggregate_chart_present_without_machine_breakdown(client):
    """The aggregated trend chart is always rendered, even when the table
    itself isn't broken down by machine — it's independent of the
    breakdown checkboxes, per the 'trend for aggregated orders over time
    as well' requirement."""
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=10)
    _login(test_client)

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23&by_machine=0")
    assert resp.status_code == 200
    assert "aggregateChartData" in resp.text
    assert "machineChartData" not in resp.text


def test_orders_summary_machine_chart_present_with_machine_breakdown(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=10)
    _seed_order_summary(SessionLocal, "2026-08-23", "Gravity", "120.00", "UPI", n=5)
    _login(test_client)

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23&by_machine=1")
    assert resp.status_code == 200
    assert "aggregateChartData" in resp.text
    assert "machineChartData" in resp.text
    assert "NEXUS" in resp.text


def test_orders_summary_breakdown_by_price_only(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=10)
    _seed_order_summary(SessionLocal, "2026-08-23", "Gravity", "150.00", "UPI", n=5)
    _login(test_client)

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23&by_machine=0&by_price=1")
    assert resp.status_code == 200
    # two distinct prices should each get their own row; machine column absent
    assert "120.00" in resp.text
    assert "150.00" in resp.text
    assert "NEXUS" not in resp.text


def test_orders_summary_breakdown_by_pay_type_only(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=10)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "Cash", n=4)
    _login(test_client)

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23&by_machine=0&by_pay_type=1")
    assert resp.status_code == 200
    assert "UPI" in resp.text
    assert "Cash" in resp.text
    assert "NEXUS" not in resp.text


def test_orders_summary_breakdown_by_price_and_pay_type_without_machine(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=10)
    _seed_order_summary(SessionLocal, "2026-08-23", "Gravity", "120.00", "Cash", n=3)
    _login(test_client)

    resp = test_client.get(
        "/orders/summary?period=daily&as_of=2026-08-23&by_machine=0&by_price=1&by_pay_type=1"
    )
    assert resp.status_code == 200
    assert "120.00" in resp.text
    assert "UPI" in resp.text
    assert "Cash" in resp.text
    # both machines' orders (10 + 3) should be merged into price/pay_type rows, not shown by name
    assert "NEXUS" not in resp.text


# --- Admin-only Revenue / Oranges-per-Glass columns ---

def test_admin_sees_revenue_and_oranges_per_glass(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    # 10 orders @ 120.00 -> revenue 1200.00; 25 oranges / 10 orders = 2.50/glass
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=10, oranges=25)
    _login(test_client)

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23")
    assert resp.status_code == 200
    assert "Revenue" in resp.text
    assert "Oranges/Glass" in resp.text
    assert "1200.00" in resp.text
    assert "2.50" in resp.text


def test_venue_partner_does_not_see_revenue_or_oranges_per_glass(client):
    test_client, SessionLocal = client
    with SessionLocal() as session:
        from db.models import VenueMapping
        session.add(VenueMapping(machine_name="NEXUS", venue_provider="Forum Kormangala"))
        session.add(User(
            name="Venue", email="venue@example.com", role="venue_partner", venue_provider="Forum Kormangala",
            active=True, password_hash=hash_password("pw123456"),
        ))
        session.commit()
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=10, oranges=25)
    _login(test_client, "venue@example.com", "pw123456")

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23")
    assert resp.status_code == 200
    assert "Revenue" not in resp.text
    assert "Oranges/Glass" not in resp.text
    # the venue partner still sees their own underlying numbers, just not
    # the two derived admin-only metrics.
    assert "NEXUS" in resp.text or "10" in resp.text


def test_venue_partner_chart_json_omits_revenue(client):
    """Hardening the above: the chart's embedded JSON payload must not
    leak raw revenue numbers either, even though nothing visibly renders
    them -- a page-source/network-tab read shouldn't reveal it."""
    test_client, SessionLocal = client
    with SessionLocal() as session:
        from db.models import VenueMapping
        session.add(VenueMapping(machine_name="NEXUS", venue_provider="Forum Kormangala"))
        session.add(User(
            name="Venue", email="venue@example.com", role="venue_partner", venue_provider="Forum Kormangala",
            active=True, password_hash=hash_password("pw123456"),
        ))
        session.commit()
    # 2 orders @ 500.00 -> revenue 1000.00, a figure distinct from every
    # OTHER visible field (avg price 500.00, oranges, juice weight) so a
    # match can only come from the revenue computation itself.
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "500.00", "UPI", n=2, oranges=6)
    _login(test_client, "venue@example.com", "pw123456")

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23")
    assert resp.status_code == 200
    assert "1000.00" not in resp.text
    assert '"revenue"' not in resp.text


def test_admin_chart_json_includes_revenue(client):
    """Regression guard: the fix above must not accidentally strip
    revenue from the admin-facing chart too."""
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "500.00", "UPI", n=2, oranges=6)
    _login(test_client)

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23")
    assert resp.status_code == 200
    assert '"revenue"' in resp.text


def test_venue_partner_sees_only_glasses_sold(client):
    """Per explicit request: vendors see ONLY the glasses-sold count —
    no Avg Price/Total Oranges/Avg Juice Weight either, not just the
    already-admin-only Revenue/Oranges-per-Glass."""
    test_client, SessionLocal = client
    with SessionLocal() as session:
        from db.models import VenueMapping
        session.add(VenueMapping(machine_name="NEXUS", venue_provider="Forum Kormangala"))
        session.add(User(
            name="Venue", email="venue@example.com", role="venue_partner", venue_provider="Forum Kormangala",
            active=True, password_hash=hash_password("pw123456"),
        ))
        session.commit()
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=10, oranges=25)
    _login(test_client, "venue@example.com", "pw123456")

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23")
    assert resp.status_code == 200
    assert "Glasses Sold" in resp.text
    assert "10" in resp.text  # the glasses-sold count itself still shows
    for hidden in ("Avg Price", "Total Oranges", "Avg Juice Weight", "Revenue", "Oranges/Glass"):
        assert hidden not in resp.text


def test_admin_still_sees_full_breakdown_columns(client):
    """Regression guard: the vendor-trimming above must not accidentally
    strip Avg Price/Total Oranges/Avg Juice Weight from the admin view
    too — only Revenue/Oranges-per-Glass were ever meant to stay
    admin-only before this change; now everything but the glasses-sold
    count itself is admin-only for vendors, but admin keeps all of it."""
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=10, oranges=25)
    _login(test_client)

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23")
    assert resp.status_code == 200
    for still_visible in ("Total Orders", "Avg Price", "Total Oranges", "Avg Juice Weight", "Revenue", "Oranges/Glass"):
        assert still_visible in resp.text
    assert "Glasses Sold" not in resp.text


# --- Vendor report download (POST /orders/summary/report, GET .../download) ---

def test_vendor_sees_download_report_section(client):
    test_client, SessionLocal = client
    _add_venue_partner(SessionLocal, "venue@example.com", "PNR Felicity")
    _login(test_client, "venue@example.com", "pw123456")

    resp = test_client.get("/orders/summary")
    assert resp.status_code == 200
    assert "Download a Sales Report" in resp.text


def test_admin_does_not_see_download_report_section(client):
    """This feature is vendor-only -- admin already has the full
    Senior Management Report."""
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _login(test_client)

    resp = test_client.get("/orders/summary")
    assert resp.status_code == 200
    assert "Download a Sales Report" not in resp.text


def test_create_vendor_report_job(client):
    test_client, SessionLocal = client
    _add_venue_partner(SessionLocal, "venue@example.com", "PNR Felicity")
    _login(test_client, "venue@example.com", "pw123456")

    resp = test_client.post(
        "/orders/summary/report", data={"period": "monthly", "as_of": "2026-08-15"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        job = session.execute(select(ReportJob)).scalar_one()
        assert job.venue_provider == "PNR Felicity"
        assert job.equipment_id is None
        assert job.status == ReportJobStatus.PENDING
        assert job.start == "2026-08-01"
        assert job.end == "2026-08-31"
        assert job.include_datewise_sales is False


def test_create_vendor_report_job_with_datewise_toggle(client):
    test_client, SessionLocal = client
    _add_venue_partner(SessionLocal, "venue@example.com", "PNR Felicity")
    _login(test_client, "venue@example.com", "pw123456")

    test_client.post(
        "/orders/summary/report",
        data={"period": "monthly", "as_of": "2026-08-15", "include_datewise_sales": "1"},
    )
    with SessionLocal() as session:
        assert session.execute(select(ReportJob)).scalar_one().include_datewise_sales is True


def test_admin_cannot_create_vendor_report_job(client):
    """Admin has no venue_provider -- must be rejected, not silently
    create a NULL-venue job that report_job_worker.py would then
    misread as a management report."""
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _login(test_client)

    resp = test_client.post("/orders/summary/report", data={"period": "monthly", "as_of": "2026-08-15"}, follow_redirects=False)
    assert resp.status_code == 303
    with SessionLocal() as session:
        assert session.execute(select(ReportJob)).scalars().all() == []


def test_venue_partner_with_no_venue_assigned_cannot_create_job(client):
    test_client, SessionLocal = client
    _add_venue_partner(SessionLocal, "venue@example.com", None)
    _login(test_client, "venue@example.com", "pw123456")

    test_client.post("/orders/summary/report", data={"period": "monthly", "as_of": "2026-08-15"})
    with SessionLocal() as session:
        assert session.execute(select(ReportJob)).scalars().all() == []


def test_vendor_sees_only_their_own_report_history(client):
    test_client, SessionLocal = client
    _add_venue_partner(SessionLocal, "venue1@example.com", "PNR Felicity")
    _add_venue_partner(SessionLocal, "venue2@example.com", "Forum Kormangala")
    with SessionLocal() as session:
        session.add(ReportJob(
            period="monthly", as_of="2026-08-15", start="2026-08-01", end="2026-08-31",
            venue_provider="PNR Felicity", status=ReportJobStatus.SUCCESS, pdf_data=b"%PDF-1.4 mine",
        ))
        session.add(ReportJob(
            period="monthly", as_of="2026-08-15", start="2026-08-01", end="2026-08-31",
            venue_provider="Forum Kormangala", status=ReportJobStatus.SUCCESS, pdf_data=b"%PDF-1.4 not mine",
        ))
        session.commit()
    _login(test_client, "venue1@example.com", "pw123456")

    resp = test_client.get("/orders/summary")
    assert resp.status_code == 200
    assert "PNR Felicity" in resp.text or "/orders/summary/report/1/download" in resp.text
    # Only one row's worth of download link should exist for this vendor.
    assert resp.text.count("Download PDF") == 1


def test_vendor_can_download_their_own_ready_report(client):
    test_client, SessionLocal = client
    _add_venue_partner(SessionLocal, "venue@example.com", "PNR Felicity")
    with SessionLocal() as session:
        job = ReportJob(
            period="monthly", as_of="2026-08-15", start="2026-08-01", end="2026-08-31",
            venue_provider="PNR Felicity", status=ReportJobStatus.SUCCESS, pdf_data=b"%PDF-1.4 fake",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id
    _login(test_client, "venue@example.com", "pw123456")

    resp = test_client.get(f"/orders/summary/report/{job_id}/download")
    assert resp.status_code == 200
    assert resp.content == b"%PDF-1.4 fake"
    assert resp.headers["content-type"] == "application/pdf"


def test_vendor_cannot_download_another_vendors_report(client):
    test_client, SessionLocal = client
    _add_venue_partner(SessionLocal, "venue1@example.com", "PNR Felicity")
    _add_venue_partner(SessionLocal, "venue2@example.com", "Forum Kormangala")
    with SessionLocal() as session:
        job = ReportJob(
            period="monthly", as_of="2026-08-15", start="2026-08-01", end="2026-08-31",
            venue_provider="Forum Kormangala", status=ReportJobStatus.SUCCESS, pdf_data=b"%PDF-1.4 not yours",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id
    _login(test_client, "venue1@example.com", "pw123456")  # PNR Felicity, not the job's owner

    resp = test_client.get(f"/orders/summary/report/{job_id}/download", follow_redirects=False)
    assert resp.status_code == 303  # redirected away, same as a nonexistent job -- never a distinguishable error
    with SessionLocal() as session:
        assert session.get(ReportJob, job_id).pdf_data == b"%PDF-1.4 not yours"  # untouched


def test_admin_cannot_download_via_vendor_route(client):
    """An admin-initiated job (venue_provider=None) must never be
    downloadable through this vendor-only route, even by an admin."""
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    with SessionLocal() as session:
        job = ReportJob(
            period="monthly", as_of="2026-08-15", start="2026-08-01", end="2026-08-31",
            venue_provider=None, status=ReportJobStatus.SUCCESS, pdf_data=b"%PDF-1.4 admin report",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id
    _login(test_client)

    resp = test_client.get(f"/orders/summary/report/{job_id}/download", follow_redirects=False)
    assert resp.status_code == 303


def test_download_pending_job_redirects_instead_of_serving_garbage(client):
    test_client, SessionLocal = client
    _add_venue_partner(SessionLocal, "venue@example.com", "PNR Felicity")
    with SessionLocal() as session:
        job = ReportJob(
            period="monthly", as_of="2026-08-15", start="2026-08-01", end="2026-08-31",
            venue_provider="PNR Felicity", status=ReportJobStatus.PENDING,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id
    _login(test_client, "venue@example.com", "pw123456")

    resp = test_client.get(f"/orders/summary/report/{job_id}/download", follow_redirects=False)
    assert resp.status_code == 303


def test_operations_cannot_access_vendor_report_routes(client):
    test_client, SessionLocal = client
    with SessionLocal() as session:
        session.add(User(name="Ops", email="ops@example.com", role="operations", active=True, password_hash=hash_password("pw123456")))
        session.commit()
    _login(test_client, "ops@example.com", "pw123456")

    resp = test_client.post("/orders/summary/report", data={"period": "monthly", "as_of": "2026-08-15"}, follow_redirects=False)
    assert resp.status_code == 403
