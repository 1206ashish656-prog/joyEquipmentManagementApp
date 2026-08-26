"""
InventoryRules: loads low-stock thresholds from config/inventory_rules.yaml
(requirement: "warning trigger... threshold (maintained in inventory
config)") rather than hardcoding them — mirrors services/health_engine.py's
HealthEngine loading pattern exactly, so operators can add/change a
threshold without a code change.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from monitoring.config import PROJECT_ROOT

DEFAULT_RULES_PATH = PROJECT_ROOT / "config" / "inventory_rules.yaml"


class InventoryRules:
    def __init__(self, rules_path: Path = DEFAULT_RULES_PATH):
        with open(rules_path, "r", encoding="utf-8") as f:
            rules = yaml.safe_load(f)

        self._thresholds: dict[str, int] = {k: v["threshold"] for k, v in rules["items"].items()}

    def threshold_for(self, item_key: str) -> int:
        # Unknown key -> 0 -- fails open (never reported "low") rather
        # than crashing a route on a config/DB drift, same "uncertainty
        # never becomes a crash" convention as HealthEngine's UNKNOWN state.
        return self._thresholds.get(item_key, 0)
