"""
Shared pytest fixtures for the unit test suite (test_health_engine.py,
test_state_manager.py). Uses an in-memory SQLite database rather than the
real Postgres from docker-compose.yml — the ORM models use only portable
column types specifically so this works, keeping these tests fast and
independent of any running infrastructure.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base
from monitoring.models import EquipmentRecord


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def make_record(
    equipment_id: str = "205",
    equipment_code: str = "153DF041EE7E279C",
    name: str = "NEXUS",
    device_type: str = "REFRESHA",
    status: str = "Normal",
    network_status: str = "Online",
    fault_type: str = "Normal",
    material_shortage_status: str = "Normal",
    observed_at: datetime | None = None,
    **extra,
) -> EquipmentRecord:
    """Test-only factory for EquipmentRecord, defaulting to the fully
    healthy example device from the spec."""
    return EquipmentRecord(
        equipment_id=equipment_id,
        equipment_code=equipment_code,
        name=name,
        device_type=device_type,
        status=status,
        network_status=network_status,
        fault_type=fault_type,
        material_shortage_status=material_shortage_status,
        observed_at=observed_at or datetime.now(timezone.utc),
        **extra,
    )
