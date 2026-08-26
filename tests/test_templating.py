"""Unit tests for backend/templating.py's `ist` Jinja filter -- every
timestamp on the equipment monitoring pages (dashboard, equipment
detail, faults) is stored as UTC but displayed in IST, per explicit
request. Reuses orders/mapping.py's IST_TZ rather than a second
timezone constant."""
from __future__ import annotations

from datetime import datetime, timezone

from backend.templating import ist


def test_ist_converts_utc_to_ist():
    # 2026-08-25 18:30:00 UTC = 2026-08-26 00:00:00 IST (UTC+5:30).
    value = datetime(2026, 8, 25, 18, 30, 0, tzinfo=timezone.utc)
    assert ist(value) == "26 Aug 2026, 00:00:00 IST"


def test_ist_handles_none():
    assert ist(None) == "—"


def test_ist_assumes_naive_datetime_is_utc():
    # SQLite can round-trip a tz-aware column back as naive -- must be
    # treated as UTC (the only thing db/models.py's _utcnow() ever
    # produces), not left ambiguous or misinterpreted as local time.
    naive = datetime(2026, 8, 25, 18, 30, 0)
    aware = datetime(2026, 8, 25, 18, 30, 0, tzinfo=timezone.utc)
    assert ist(naive) == ist(aware)


def test_ist_custom_format():
    value = datetime(2026, 8, 25, 18, 30, 0, tzinfo=timezone.utc)
    assert ist(value, fmt="%Y-%m-%d") == "2026-08-26"
