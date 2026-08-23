"""
HealthEngine: converts a target-reported EquipmentRecord into our own
canonical HealthState (project requirement #6) — never a passthrough of
the target's own `status` field.

Rules are loaded from config/health_rules.yaml (requirement #7:
"Implement the health rules as configurable business logic") rather than
hardcoded here, so operators can add e.g. a new fault_type value without a
code change.

Evaluation order (matches requirement #7's examples, with an UNKNOWN
fallback per requirement #28 — never guess a negative state from a
missing/unrecognized value):

    1. network_status offline               -> OFFLINE
    2. network_status unrecognized/missing  -> UNKNOWN
    3. fault_type not a known "no fault"    -> MALFUNCTION
    4. fault_type missing                   -> UNKNOWN
    5. material_shortage critical/warning   -> WARNING (or MALFUNCTION if
                                                escalate_critical_to_malfunction)
    6. material_shortage unrecognized/missing -> UNKNOWN
    7. otherwise                            -> HEALTHY
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from db.models import HealthState
from monitoring.config import PROJECT_ROOT
from monitoring.models import EquipmentRecord

DEFAULT_RULES_PATH = PROJECT_ROOT / "config" / "health_rules.yaml"


@dataclass(frozen=True)
class HealthEvaluation:
    state: HealthState
    reason: str


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


class HealthEngine:
    def __init__(self, rules_path: Path = DEFAULT_RULES_PATH):
        with open(rules_path, "r", encoding="utf-8") as f:
            rules = yaml.safe_load(f)

        self._no_fault_values = {v.lower() for v in rules["fault_type"]["no_fault_values"]}
        self._online_values = {v.lower() for v in rules["network_status"]["online_values"]}
        self._offline_values = {v.lower() for v in rules["network_status"]["offline_values"]}
        self._material_ok_values = {v.lower() for v in rules["material_shortage_status"]["ok_values"]}
        self._material_warning_values = {v.lower() for v in rules["material_shortage_status"]["warning_values"]}
        self._material_critical_values = {v.lower() for v in rules["material_shortage_status"]["critical_values"]}
        self._escalate_critical = bool(
            rules["material_shortage_status"].get("escalate_critical_to_malfunction", False)
        )
        self._severity_map: dict[str, str] = rules.get("severity", {})

    def evaluate(self, record: EquipmentRecord) -> HealthEvaluation:
        network = _norm(record.network_status)
        fault = _norm(record.fault_type)
        material = _norm(record.material_shortage_status)

        if not network:
            return HealthEvaluation(HealthState.UNKNOWN, "network_status missing")
        if network in self._offline_values:
            return HealthEvaluation(HealthState.OFFLINE, f"network_status={record.network_status!r} is offline")
        if network not in self._online_values:
            return HealthEvaluation(HealthState.UNKNOWN, f"unrecognized network_status={record.network_status!r}")

        if not fault:
            return HealthEvaluation(HealthState.UNKNOWN, "fault_type missing")
        if fault not in self._no_fault_values:
            return HealthEvaluation(HealthState.MALFUNCTION, f"fault_type={record.fault_type!r}")

        if not material:
            return HealthEvaluation(HealthState.UNKNOWN, "material_shortage_status missing")
        if material in self._material_critical_values:
            state = HealthState.MALFUNCTION if self._escalate_critical else HealthState.WARNING
            return HealthEvaluation(
                state, f"material_shortage_status={record.material_shortage_status!r} (critical)"
            )
        if material in self._material_warning_values:
            return HealthEvaluation(
                HealthState.WARNING, f"material_shortage_status={record.material_shortage_status!r}"
            )
        if material not in self._material_ok_values:
            return HealthEvaluation(
                HealthState.UNKNOWN, f"unrecognized material_shortage_status={record.material_shortage_status!r}"
            )

        return HealthEvaluation(HealthState.HEALTHY, "status/network/fault/material all nominal")

    def severity_for(self, state: HealthState) -> str:
        return self._severity_map.get(state.value, "Warning")
