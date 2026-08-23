"""
Unit tests for services/health_engine.py against the real
config/health_rules.yaml (not a mocked config) — these are the exact rule
examples from spec section 7.
"""
from __future__ import annotations

from db.models import HealthState
from services.health_engine import HealthEngine
from tests.conftest import make_record


def engine() -> HealthEngine:
    return HealthEngine()


def test_all_normal_is_healthy():
    result = engine().evaluate(make_record())
    assert result.state == HealthState.HEALTHY


def test_offline_network_wins_regardless_of_other_fields():
    result = engine().evaluate(make_record(network_status="Offline", fault_type="Motor Fault"))
    assert result.state == HealthState.OFFLINE


def test_non_normal_fault_type_is_malfunction():
    result = engine().evaluate(make_record(fault_type="Motor Fault"))
    assert result.state == HealthState.MALFUNCTION
    assert "Motor Fault" in result.reason


def test_material_shortage_without_fault_is_warning():
    result = engine().evaluate(make_record(material_shortage_status="Low"))
    assert result.state == HealthState.WARNING


def test_critical_shortage_defaults_to_warning_not_malfunction():
    result = engine().evaluate(make_record(material_shortage_status="Empty"))
    assert result.state == HealthState.WARNING


def test_fault_type_takes_priority_over_material_shortage():
    result = engine().evaluate(make_record(fault_type="Motor Fault", material_shortage_status="Low"))
    assert result.state == HealthState.MALFUNCTION


def test_missing_network_status_is_unknown_not_offline():
    result = engine().evaluate(make_record(network_status=""))
    assert result.state == HealthState.UNKNOWN


def test_unrecognized_network_status_is_unknown():
    result = engine().evaluate(make_record(network_status="Connecting"))
    assert result.state == HealthState.UNKNOWN


def test_missing_fault_type_is_unknown():
    result = engine().evaluate(make_record(fault_type=""))
    assert result.state == HealthState.UNKNOWN


def test_case_and_whitespace_insensitive():
    result = engine().evaluate(
        make_record(status=" normal ", network_status=" ONLINE", fault_type="normal ", material_shortage_status=" Normal")
    )
    assert result.state == HealthState.HEALTHY


def test_severity_mapping():
    e = engine()
    assert e.severity_for(HealthState.MALFUNCTION) == "Critical"
    assert e.severity_for(HealthState.OFFLINE) == "Critical"
    assert e.severity_for(HealthState.WARNING) == "Warning"
