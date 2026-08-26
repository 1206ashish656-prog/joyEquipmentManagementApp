"""Unit tests for services/inventory_rules.py against the REAL
config/inventory_rules.yaml (mirrors tests/test_health_engine.py's
approach of testing against the real rules file, not a fixture copy)."""
from __future__ import annotations

from db.models import INVENTORY_ITEM_ORDER
from services.inventory_rules import InventoryRules


def test_all_ten_items_have_a_positive_threshold():
    rules = InventoryRules()
    for key in INVENTORY_ITEM_ORDER:
        assert rules.threshold_for(key) > 0, f"{key} should have a configured threshold"


def test_unknown_key_returns_zero_not_raise():
    rules = InventoryRules()
    assert rules.threshold_for("does-not-exist") == 0
