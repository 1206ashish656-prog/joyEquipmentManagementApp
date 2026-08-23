"""
Core data types shared across the monitoring engine.

Deliberately does NOT include a health-state enum (HEALTHY/WARNING/
MALFUNCTION/OFFLINE/UNKNOWN) yet — that belongs to the Health Evaluation
Engine, which is Phase 3 work once there is a database to persist
transitions against. This module only models:

  1. Technical monitoring states (never to be confused with equipment
     health — see project requirement #6/#28).
  2. The raw-but-normalized equipment record shape extracted from the
     target application.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class MonitoringState(str, Enum):
    """Technical state of the monitoring system itself.

    These must NEVER be interpreted as equipment health. A machine's
    health is UNKNOWN only when the target explicitly reported something
    unrecognized for that machine — not when the monitoring system as a
    whole failed to reach the target.
    """

    OK = "OK"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    TARGET_UNAVAILABLE = "TARGET_UNAVAILABLE"
    EXTRACTION_ERROR = "EXTRACTION_ERROR"
    MONITORING_ERROR = "MONITORING_ERROR"


class MonitoringError(Exception):
    """Base class for classified monitoring failures."""

    state: MonitoringState = MonitoringState.MONITORING_ERROR


class AuthenticationRequiredError(MonitoringError):
    state = MonitoringState.AUTHENTICATION_REQUIRED


class TargetUnavailableError(MonitoringError):
    state = MonitoringState.TARGET_UNAVAILABLE


class ExtractionError(MonitoringError):
    state = MonitoringState.EXTRACTION_ERROR


class DataValidationError(ExtractionError):
    """Subset of extraction errors: data was retrieved but failed
    validation (e.g. unexpected zero records, malformed IDs)."""


@dataclass
class EquipmentRecord:
    """Normalized equipment/device record as read from the target
    application's Device Information page. This is raw target data —
    NOT a health verdict. See services/health_engine (Phase 3) for that.
    """

    # --- Mandatory fields (project requirement #5) ---
    equipment_id: str
    equipment_code: str
    name: str
    device_type: str
    status: str
    network_status: str
    fault_type: str
    material_shortage_status: str
    observed_at: datetime

    # --- Optional fields ---
    advertising_group: Optional[str] = None
    device_address: Optional[str] = None
    selling_price: Optional[str] = None
    remaining_oranges: Optional[str] = None

    # Full raw row as scraped, for audit/troubleshooting (project
    # requirement #10: preserve exact fault information from the source).
    raw: dict[str, Any] = field(default_factory=dict)
