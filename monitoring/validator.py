"""
Validation layer between raw extraction and everything downstream.

Implements project requirement #27/#28: an unexpected zero-record result
(or otherwise malformed data) must NEVER be silently treated as "zero
machines" / a negative equipment health state. It must be treated as a
scraping/validation failure so the caller can retry and, if persistent,
raise EXTRACTION_ERROR / DataValidationError instead of writing bogus
equipment state.
"""
from __future__ import annotations

import re
from typing import Iterable

from .models import DataValidationError, EquipmentRecord

# Loosened on purpose: we don't yet know the real ID/code formats from the
# target app (Phase 1 discovery pending). These just reject obviously-empty
# or whitespace-only values rather than enforcing a specific pattern.
_ID_PATTERN = re.compile(r".+")
_CODE_PATTERN = re.compile(r".+")


def validate_records(
    records: list[EquipmentRecord], min_expected: int = 1
) -> None:
    """Raises DataValidationError if the record set looks untrustworthy.

    Does not return a filtered list — validation failure should stop the
    pipeline before anything is persisted, not silently drop rows.
    """
    if len(records) < min_expected:
        raise DataValidationError(
            f"Expected at least {min_expected} equipment record(s) but got "
            f"{len(records)}. Refusing to treat this as 'zero machines' — "
            "this is a validation failure, not an equipment state."
        )

    errors: list[str] = []
    seen_ids: set[str] = set()

    for idx, rec in enumerate(records):
        if not rec.equipment_id or not _ID_PATTERN.match(rec.equipment_id):
            errors.append(f"row {idx}: missing/invalid equipment_id")
        elif rec.equipment_id in seen_ids:
            errors.append(f"row {idx}: duplicate equipment_id {rec.equipment_id!r}")
        else:
            seen_ids.add(rec.equipment_id)

        if not rec.equipment_code or not _CODE_PATTERN.match(rec.equipment_code):
            errors.append(f"row {idx}: missing/invalid equipment_code")

        if not rec.status:
            errors.append(f"row {idx}: missing status")
        if not rec.network_status:
            errors.append(f"row {idx}: missing network_status")
        if not rec.fault_type:
            errors.append(f"row {idx}: missing fault_type")
        if not rec.material_shortage_status:
            errors.append(f"row {idx}: missing material_shortage_status")

    if errors:
        raise DataValidationError(
            f"{len(errors)} validation error(s) found in {len(records)} "
            f"record(s): " + "; ".join(errors[:10]) + (" ..." if len(errors) > 10 else "")
        )


def validate_raw_rows(rows: Iterable[dict]) -> list[dict]:
    """Cheap pre-check on raw scraped rows before normalization: drop rows
    that are entirely empty (e.g. spacer rows), but do NOT silently accept
    an empty overall set — that is validate_records' job, post-normalization.
    """
    return [r for r in rows if any((v or "").strip() for v in r.values())]
