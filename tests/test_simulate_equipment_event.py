"""
Unit tests for services/simulate_equipment_event.py -- the CLI operators
use (via docs/OPERATIONS_RUNBOOK.md) to add/remove TEST-* equipment rows.
Uses a real file-backed SQLite DB per test (not :memory:) since add() and
remove() each call db_base.init_engine() independently, same as the real
CLI invocations would across two separate process runs.
"""
from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

import db.base as db_base
from db.models import Equipment, FaultIncident, IncidentStatus, User
from services.simulate_equipment_event import add, remove


@pytest.fixture(autouse=True)
def _sqlite_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    yield


def _add_args(**overrides):
    defaults = dict(id="TEST-001", state="healthy", name=None, code=None, device_type="REFRESHA", fault_type=None, notify=False)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_add_rejects_id_not_starting_with_test():
    with pytest.raises(SystemExit):
        add(_add_args(id="REAL-MACHINE-001"))


def test_add_healthy_creates_equipment_with_no_incident():
    add(_add_args(id="TEST-001", state="healthy"))

    with db_base.get_session() as session:
        equipment = session.execute(select(Equipment).where(Equipment.external_id == "TEST-001")).scalar_one()
        assert equipment.name == "[TEST] TEST-001"
        incidents = session.execute(select(FaultIncident).where(FaultIncident.equipment_id == equipment.id)).scalars().all()
        assert incidents == []


def test_add_malfunction_opens_incident():
    add(_add_args(id="TEST-002", state="malfunction"))

    with db_base.get_session() as session:
        equipment = session.execute(select(Equipment).where(Equipment.external_id == "TEST-002")).scalar_one()
        incidents = session.execute(select(FaultIncident).where(FaultIncident.equipment_id == equipment.id)).scalars().all()
        assert len(incidents) == 1
        assert incidents[0].status == IncidentStatus.ACTIVE
        assert incidents[0].fault_type == "Simulated Fault"


def test_add_malfunction_custom_fault_type_is_preserved():
    add(_add_args(id="TEST-003", state="malfunction", fault_type="Custom Motor Fault"))

    with db_base.get_session() as session:
        equipment = session.execute(select(Equipment).where(Equipment.external_id == "TEST-003")).scalar_one()
        incident = session.execute(select(FaultIncident).where(FaultIncident.equipment_id == equipment.id)).scalar_one()
        assert incident.fault_type == "Custom Motor Fault"


def test_add_twice_same_fault_does_not_open_a_second_incident():
    add(_add_args(id="TEST-004", state="malfunction"))
    add(_add_args(id="TEST-004", state="malfunction"))

    with db_base.get_session() as session:
        equipment = session.execute(select(Equipment).where(Equipment.external_id == "TEST-004")).scalar_one()
        incidents = session.execute(select(FaultIncident).where(FaultIncident.equipment_id == equipment.id)).scalars().all()
        assert len(incidents) == 1


def test_add_with_notify_sends_real_email_on_new_incident(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USERNAME", "user@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "alerts@example.com")

    db_base.init_engine()
    db_base.create_all()
    with db_base.get_session() as session:
        session.add(User(name="Admin", email="admin@example.com", role="admin", active=True, password_hash="x"))

    mock_smtp_ctx = MagicMock()
    with patch("services.notification_service.smtplib.SMTP", return_value=mock_smtp_ctx):
        add(_add_args(id="TEST-005", state="malfunction", notify=True))

    assert mock_smtp_ctx.__enter__.return_value.send_message.called
    with db_base.get_session() as session:
        equipment = session.execute(select(Equipment).where(Equipment.external_id == "TEST-005")).scalar_one()
        incident = session.execute(select(FaultIncident).where(FaultIncident.equipment_id == equipment.id)).scalar_one()
        assert incident.notification_sent is True


def test_remove_deletes_equipment_and_cascades():
    add(_add_args(id="TEST-006", state="malfunction"))
    remove(argparse.Namespace(id="TEST-006"))

    with db_base.get_session() as session:
        assert session.execute(select(Equipment).where(Equipment.external_id == "TEST-006")).scalar_one_or_none() is None
        assert session.execute(select(FaultIncident)).scalars().all() == []


def test_remove_nonexistent_is_a_no_op_not_an_error():
    remove(argparse.Namespace(id="TEST-DOES-NOT-EXIST"))  # must not raise


def test_remove_rejects_id_not_starting_with_test():
    with pytest.raises(SystemExit):
        remove(argparse.Namespace(id="REAL-MACHINE-001"))
