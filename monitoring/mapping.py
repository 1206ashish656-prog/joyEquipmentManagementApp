"""
Shared raw-row -> canonical-field mapping, used by BOTH TargetApplicationClient
(target_client.py, Playwright-driven) and LightweightTargetClient
(lightweight_client.py, plain-HTTP-driven) — the two clients differ only in
HOW they fetch bytes from jwintell.com; both hand rows through the same
mapping so downstream (EquipmentExtractor, StateManager, ...) never knows
or cares which one was used.
"""
from __future__ import annotations

from .selectors import API_FIELD_MAP, COLUMN_HEADER_MAP

_MAX_PAGINATION_PAGES = 500  # sanity guard, not an expected real value


def get_nested(d: dict, dotted_path: str, default=None):
    cur = d
    for part in dotted_path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
        if cur is None:
            return default
    return cur


def blank_canonical_row() -> dict:
    return {
        "equipment_id": "",
        "equipment_code": "",
        "name": "",
        "device_type": "",
        "status": "",
        "network_status": "",
        "fault_type": "",
        "material_shortage_status": "",
        "advertising_group": None,
        "device_address": None,
        "selling_price": None,
        "remaining_oranges": None,
        "_raw": {},
    }


def map_api_row_to_canonical(row: dict) -> dict:
    """Maps one raw JSON API row to our canonical field names via
    selectors.API_FIELD_MAP, preserving the full original row as `_raw`
    for audit (requirement #10)."""
    canonical = blank_canonical_row()
    for api_field, our_field in API_FIELD_MAP.items():
        value = get_nested(row, api_field)
        if value is not None and isinstance(value, str):
            value = value.strip()
        canonical[our_field] = value if value is not None else canonical[our_field]
    canonical["equipment_id"] = str(canonical["equipment_id"] or "")
    canonical["_raw"] = row
    return canonical


def map_dom_row_to_canonical(row: dict[str, str]) -> dict:
    """Maps one raw DOM-scraped {header_text: cell_text} row to our
    canonical field names via selectors.COLUMN_HEADER_MAP."""
    canonical = blank_canonical_row()
    for raw_header, value in row.items():
        our_field = COLUMN_HEADER_MAP.get(raw_header.strip().lower())
        if our_field:
            canonical[our_field] = (value or "").strip()
    canonical["_raw"] = row
    return canonical
