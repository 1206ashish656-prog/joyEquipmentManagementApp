"""Unit test confirming backend/templating.py registers the `ist` Jinja
filter correctly -- the actual conversion logic (orders/mapping.py's
format_ist()) is tested in tests/test_orders_mapping.py, since that's
where it now lives (shared with services/fault_digest.py)."""
from __future__ import annotations

from orders.mapping import format_ist
from backend.templating import templates


def test_ist_filter_is_registered_and_uses_the_shared_implementation():
    assert templates.env.filters["ist"] is format_ist
