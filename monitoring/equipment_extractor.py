"""
EquipmentExtractor: turns raw-but-canonically-keyed rows (as produced by
TargetApplicationClient — see target_client.py's _map_api_row_to_canonical
/ _map_dom_row_to_canonical) into validated EquipmentRecord objects.

Field-name mapping from the target's own schema (JSON API field names or
DOM header text) lives in target_client.py/selectors.py, since that's
target-specific. This module only deals with our own canonical field
names, so it doesn't care whether the data came from the API or a DOM
fallback (requirement #14/#34: page-structure changes stay isolated to
target_client.py + selectors.py).
"""
from __future__ import annotations

from datetime import datetime, timezone

from .models import EquipmentRecord
from .validator import validate_raw_rows, validate_records


class EquipmentExtractor:
    def extract(self, raw_rows: list[dict]) -> list[dict]:
        """Drops rows with no equipment_id (blank/spacer rows)."""
        rows = validate_raw_rows(raw_rows)
        return [r for r in rows if (r.get("equipment_id") or "").strip()]

    def normalize(self, raw_rows: list[dict]) -> list[EquipmentRecord]:
        now = datetime.now(timezone.utc)
        return [
            EquipmentRecord(
                equipment_id=row.get("equipment_id", ""),
                equipment_code=row.get("equipment_code", ""),
                name=row.get("name", ""),
                device_type=row.get("device_type", ""),
                status=row.get("status", ""),
                network_status=row.get("network_status", ""),
                fault_type=row.get("fault_type", ""),
                material_shortage_status=row.get("material_shortage_status", ""),
                observed_at=now,
                advertising_group=row.get("advertising_group"),
                device_address=row.get("device_address"),
                selling_price=row.get("selling_price"),
                remaining_oranges=row.get("remaining_oranges"),
                raw=row.get("_raw", row),
            )
            for row in raw_rows
        ]

    def validate(self, records: list[EquipmentRecord], min_expected: int = 1) -> None:
        validate_records(records, min_expected=min_expected)
