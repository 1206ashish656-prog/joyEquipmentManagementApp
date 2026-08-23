"""
Unit tests for services/state_manager.py — the core alerting-correctness
requirements (spec section 8/9). Reproduces the spec's own worked example:

    10:00  Normal
    10:01  Normal
    10:02  Motor Fault   <- exactly ONE incident opens here
    10:03  Motor Fault   <- no new incident
    10:04  Motor Fault   <- no new incident
    10:05  Motor Fault   <- no new incident
    10:06  Normal        <- incident resolves

Uses the in-memory SQLite db_session fixture from conftest.py and the
real HealthEngine (not mocked) so this exercises the actual production
rule set.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from db.models import FaultIncident, HealthState, IncidentStatus
from services.health_engine import HealthEngine
from services.state_manager import StateManager
from tests.conftest import make_record

T0 = datetime(2026, 8, 23, 10, 0, 0, tzinfo=timezone.utc)


def _at(minute: int) -> datetime:
    return T0 + timedelta(minutes=minute)


def _manager() -> StateManager:
    return StateManager(HealthEngine())


def test_dedup_matches_spec_worked_example(db_session):
    sm = _manager()

    r0 = sm.process_observation(db_session, make_record(observed_at=_at(0)))
    r1 = sm.process_observation(db_session, make_record(observed_at=_at(1)))
    r2 = sm.process_observation(db_session, make_record(fault_type="Motor Fault", observed_at=_at(2)))
    r3 = sm.process_observation(db_session, make_record(fault_type="Motor Fault", observed_at=_at(3)))
    r4 = sm.process_observation(db_session, make_record(fault_type="Motor Fault", observed_at=_at(4)))
    r5 = sm.process_observation(db_session, make_record(fault_type="Motor Fault", observed_at=_at(5)))

    assert r0.incident_opened is None and r1.incident_opened is None
    assert r2.incident_opened is not None, "exactly one incident must open on first fault"
    assert r3.incident_opened is None, "no duplicate incident on repeated fault"
    assert r4.incident_opened is None
    assert r5.incident_opened is None

    all_incidents = db_session.query(FaultIncident).all()
    assert len(all_incidents) == 1, "spec requirement: ONE alert, not four"
    assert all_incidents[0].fault_type == "Motor Fault"
    assert all_incidents[0].status == IncidentStatus.ACTIVE

    r6 = sm.process_observation(db_session, make_record(observed_at=_at(6)))
    assert r6.incident_resolved is not None
    assert r6.incident_resolved.status == IncidentStatus.RESOLVED
    assert r6.incident_resolved.resolved_at == _at(6)

    # Still exactly one incident row overall — resolution updates it in
    # place, it doesn't create a second row.
    assert db_session.query(FaultIncident).count() == 1


def test_incident_preserves_exact_fault_text(db_session):
    sm = _manager()
    result = sm.process_observation(db_session, make_record(fault_type="Temperature Sensor Fault"))
    assert result.incident_opened.fault_type == "Temperature Sensor Fault"


def test_downtime_is_calculable_from_started_and_resolved(db_session):
    sm = _manager()
    sm.process_observation(db_session, make_record(fault_type="Motor Fault", observed_at=_at(0)))
    result = sm.process_observation(db_session, make_record(observed_at=_at(25)))
    incident = result.incident_resolved
    # .replace(tzinfo=None) on both sides: SQLite (unlike the real Postgres
    # backend) doesn't round-trip tz-awareness on DateTime(timezone=True) —
    # this is a test-storage quirk, not something being asserted about.
    started = incident.started_at.replace(tzinfo=None)
    resolved = incident.resolved_at.replace(tzinfo=None)
    assert resolved - started == timedelta(minutes=25)


def test_escalation_from_warning_to_malfunction_updates_same_incident(db_session):
    sm = _manager()
    r1 = sm.process_observation(db_session, make_record(material_shortage_status="Low", observed_at=_at(0)))
    assert r1.incident_opened is not None
    incident_id = r1.incident_opened.id

    r2 = sm.process_observation(db_session, make_record(fault_type="Motor Fault", observed_at=_at(1)))
    assert r2.incident_opened is None, "escalation must not open a second incident"
    assert r2.incident_escalated is not None
    assert r2.incident_escalated.id == incident_id
    assert r2.incident_escalated.current_health == HealthState.MALFUNCTION

    assert db_session.query(FaultIncident).count() == 1


def test_offline_opens_and_resolves_like_any_other_fault_state(db_session):
    sm = _manager()
    r1 = sm.process_observation(db_session, make_record(network_status="Offline", observed_at=_at(0)))
    assert r1.incident_opened is not None
    assert r1.evaluation.state == HealthState.OFFLINE

    r2 = sm.process_observation(db_session, make_record(observed_at=_at(1)))
    assert r2.incident_resolved is not None


def test_unknown_observation_does_not_resolve_an_active_incident(db_session):
    """Reliability requirement #28: uncertainty must never be read as
    recovery. A machine mid-malfunction whose network_status is suddenly
    unparseable is NOT the same as it recovering."""
    sm = _manager()
    r1 = sm.process_observation(db_session, make_record(fault_type="Motor Fault", observed_at=_at(0)))
    assert r1.incident_opened is not None
    incident_id = r1.incident_opened.id

    r2 = sm.process_observation(db_session, make_record(network_status="", observed_at=_at(1)))
    assert r2.evaluation.state == HealthState.UNKNOWN
    assert r2.incident_resolved is None
    assert r2.incident_opened is None
    assert r2.incident_escalated is None

    incident = db_session.get(FaultIncident, incident_id)
    assert incident.status == IncidentStatus.ACTIVE, "incident must remain open through an UNKNOWN observation"


def test_unknown_observation_does_not_open_an_incident(db_session):
    sm = _manager()
    result = sm.process_observation(db_session, make_record(network_status="", observed_at=_at(0)))
    assert result.evaluation.state == HealthState.UNKNOWN
    assert result.incident_opened is None
    assert db_session.query(FaultIncident).count() == 0


def test_equipment_master_row_created_and_reused(db_session):
    sm = _manager()
    sm.process_observation(db_session, make_record(observed_at=_at(0)))
    sm.process_observation(db_session, make_record(observed_at=_at(1)))
    from db.models import Equipment

    assert db_session.query(Equipment).count() == 1


def test_snapshot_recorded_every_poll_even_without_state_change(db_session):
    sm = _manager()
    from db.models import EquipmentSnapshot

    for i in range(5):
        sm.process_observation(db_session, make_record(observed_at=_at(i)))
    assert db_session.query(EquipmentSnapshot).count() == 5


def test_current_state_reflects_last_observation(db_session):
    sm = _manager()
    from db.models import EquipmentCurrentState

    sm.process_observation(db_session, make_record(observed_at=_at(0)))
    sm.process_observation(db_session, make_record(fault_type="Motor Fault", observed_at=_at(1)))

    current = db_session.query(EquipmentCurrentState).one()
    assert current.health_state == HealthState.MALFUNCTION
    assert current.fault_type == "Motor Fault"
    # .replace(tzinfo=None): see note in test_downtime_is_calculable_... —
    # SQLite-only quirk, not a production behavior being asserted.
    assert current.last_changed_at.replace(tzinfo=None) == _at(1).replace(tzinfo=None)
