"""
Unit tests for services/fault_digest.py -- the "Critical Faults Report"
tabular email listing every machine currently in a Critical-severity
ACTIVE incident. Uses the same db_session fixture (in-memory SQLite) as
test_state_manager.py/test_alert_engine.py.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from db.models import AlertRecipient, Equipment, FaultIncident, HealthState, IncidentStatus, User
from monitoring.config import load_settings
from services.alert_engine import AlertEngine
from services.fault_digest import build_digest


class FakeNotificationService:
    def send_email(self, to, subject, body, html_body=None):
        return True


def _alert_engine():
    settings = replace(load_settings(), smtp_host="")
    return AlertEngine(settings, FakeNotificationService())


def _add_equipment(db_session, external_id, name) -> Equipment:
    eq = Equipment(external_id=external_id, equipment_code=f"CODE{external_id}", name=name, device_type="REFRESHA")
    db_session.add(eq)
    db_session.flush()
    return eq


def _add_incident(
    db_session, equipment: Equipment, fault_type="Motor Fault", severity="Critical",
    health=HealthState.MALFUNCTION, status=IncidentStatus.ACTIVE, started_at=None, resolved_at=None,
) -> FaultIncident:
    inc = FaultIncident(
        equipment_id=equipment.id, fault_type=fault_type, severity=severity, current_health=health,
        started_at=started_at or (datetime.now(timezone.utc) - timedelta(hours=2)),
        resolved_at=resolved_at, status=status,
    )
    db_session.add(inc)
    db_session.flush()
    return inc


def test_no_active_critical_incidents_returns_none(db_session):
    assert build_digest(db_session, _alert_engine()) is None


def test_warning_severity_excluded(db_session):
    eq = _add_equipment(db_session, "1", "Low Stock Unit")
    _add_incident(db_session, eq, severity="Warning", health=HealthState.WARNING)
    assert build_digest(db_session, _alert_engine()) is None


def test_resolved_incident_excluded(db_session):
    eq = _add_equipment(db_session, "1", "Fixed Unit")
    _add_incident(db_session, eq, status=IncidentStatus.RESOLVED, resolved_at=datetime.now(timezone.utc))
    assert build_digest(db_session, _alert_engine()) is None


def test_single_active_critical_incident_included(db_session):
    eq = _add_equipment(db_session, "205", "NEXUS")
    _add_incident(db_session, eq, fault_type="Motor Fault")

    digest = build_digest(db_session, _alert_engine())
    assert digest is not None
    assert digest.incident_count == 1
    assert "1 machine(s) affected" in digest.subject
    assert "NEXUS" in digest.plain_body
    assert "Motor Fault" in digest.plain_body
    assert "MALFUNCTION" in digest.plain_body


def test_multiple_active_critical_incidents_all_listed(db_session):
    eq1 = _add_equipment(db_session, "1", "Warehouse")
    eq2 = _add_equipment(db_session, "2", "REFRESH-1")
    _add_incident(db_session, eq1, fault_type="Normal", health=HealthState.OFFLINE)
    _add_incident(db_session, eq2, fault_type="Normal", health=HealthState.OFFLINE)

    digest = build_digest(db_session, _alert_engine())
    assert digest.incident_count == 2
    assert "2 machine(s) affected" in digest.subject
    assert "Warehouse" in digest.plain_body
    assert "REFRESH-1" in digest.plain_body


def test_html_body_contains_table_and_data(db_session):
    eq = _add_equipment(db_session, "205", "NEXUS")
    _add_incident(db_session, eq, fault_type="Motor Fault")

    digest = build_digest(db_session, _alert_engine())
    assert "<table" in digest.html_body
    assert "<th" in digest.html_body
    assert "NEXUS" in digest.html_body
    assert "Motor Fault" in digest.html_body


def test_html_body_escapes_special_characters(db_session):
    eq = _add_equipment(db_session, "205", "NEXUS <Test>")
    _add_incident(db_session, eq, fault_type="Fault & <script>alert(1)</script>")

    digest = build_digest(db_session, _alert_engine())
    assert "<script>" not in digest.html_body
    assert "&lt;script&gt;" in digest.html_body


def test_duration_column_present(db_session):
    eq = _add_equipment(db_session, "205", "NEXUS")
    _add_incident(db_session, eq, started_at=datetime.now(timezone.utc) - timedelta(hours=3, minutes=15))

    digest = build_digest(db_session, _alert_engine())
    assert "3h" in digest.plain_body


def test_recipients_include_admin_and_alert_recipient(db_session):
    db_session.add(User(name="Admin", email="admin@example.com", role="admin", active=True))
    db_session.add(AlertRecipient(email="ops-external@example.com", active=True))
    db_session.flush()

    eq = _add_equipment(db_session, "205", "NEXUS")
    _add_incident(db_session, eq)

    digest = build_digest(db_session, _alert_engine())
    assert digest.recipients == ["admin@example.com", "ops-external@example.com"]


def test_inactive_alert_recipient_excluded(db_session):
    db_session.add(AlertRecipient(email="left-the-company@example.com", active=False))
    db_session.flush()

    eq = _add_equipment(db_session, "205", "NEXUS")
    _add_incident(db_session, eq)

    digest = build_digest(db_session, _alert_engine())
    assert digest.recipients == []
