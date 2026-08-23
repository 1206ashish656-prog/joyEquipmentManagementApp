"""
EquipmentExtractor: turns raw scraped rows (list[dict[str, str]], keyed by
whatever header text the source table used) into normalized EquipmentRecord
objects. Kept separate from TargetApplicationClient so that a page-structure
change only requires updating selectors.py + the header map here, not the
browser-interaction code (project requirement #14/#34).
"""
from __future__ import annotations

from datetime import datetime, timezone

from .models import EquipmentRecord
from .selectors import COLUMN_HEADER_MAP
from .validator import validate_raw_rows, validate_records


class EquipmentExtractor:
    def __init__(self, header_map: dict[str, str] = COLUMN_HEADER_MAP):
        self._header_map = header_map

    def extract(self, raw_rows: list[dict[str, str]]) -> list[dict[str, str]]:
        """Cheap structural pre-filter on raw rows (drop blank rows)."""
        return validate_raw_rows(raw_rows)

    def normalize(self, raw_rows: list[dict[str, str]]) -> list[EquipmentRecord]:
        """Maps raw {header_text: cell_text} rows to EquipmentRecord using
        COLUMN_HEADER_MAP. Unrecognized headers are preserved in .raw but
        do not become named fields — this lets new columns show up in
        `raw` immediately without a code change, while still requiring an
        explicit mapping decision before they're treated as first-class
        data (project requirement #5: "allow additional fields to be added
        later without redesigning the monitoring engine").
        """
        now = datetime.now(timezone.utc)
        records: list[EquipmentRecord] = []

        for row in raw_rows:
            mapped: dict[str, str] = {}
            for raw_header, value in row.items():
                field_name = self._header_map.get(raw_header.strip().lower())
                if field_name:
                    mapped[field_name] = (value or "").strip()

            records.append(
                EquipmentRecord(
                    equipment_id=mapped.get("equipment_id", ""),
                    equipment_code=mapped.get("equipment_code", ""),
                    name=mapped.get("name", ""),
                    device_type=mapped.get("device_type", ""),
                    status=mapped.get("status", ""),
                    network_status=mapped.get("network_status", ""),
                    fault_type=mapped.get("fault_type", ""),
                    material_shortage_status=mapped.get("material_shortage_status", ""),
                    observed_at=now,
                    advertising_group=mapped.get("advertising_group"),
                    device_address=mapped.get("device_address"),
                    selling_price=mapped.get("selling_price"),
                    remaining_oranges=mapped.get("remaining_oranges"),
                    raw=row,
                )
            )

        return records

    def validate(self, records: list[EquipmentRecord], min_expected: int = 1) -> None:
        validate_records(records, min_expected=min_expected)
