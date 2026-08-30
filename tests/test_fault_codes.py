"""Unit tests for monitoring/fault_codes.py's code -> description
translation table, harvested from the target's own backend/ajax/lang
endpoint (see the module docstring for the confirmation details)."""
from __future__ import annotations

from monitoring.fault_codes import describe_fault_code


def test_known_code_matches_targets_own_rendering():
    """Reproduces the target's own device_fault_log.js formatter output
    exactly, confirmed live against a real historical row (target id
    21529, device_id 109)."""
    assert describe_fault_code("luozhentanzhenkaiguan") == "Drop cup probe switch Malfunction"


def test_second_known_code_matches_targets_own_rendering():
    """Confirmed live via the target's own UI screenshot showing this
    exact rendered text."""
    assert describe_fault_code("dianzicheng") == "Electronic scale Malfunction"


def test_unknown_code_falls_back_to_readable_slug(caplog):
    """The translation table was captured from the target's lang pack, not
    guaranteed exhaustive — an unrecognized code must never raise or
    silently drop information, only degrade to a best-effort label."""
    with caplog.at_level("WARNING"):
        result = describe_fault_code("some_new_component")
    assert result == "some new component Malfunction"
    assert "Unrecognized fault code" in caplog.text


def test_empty_code_does_not_crash():
    result = describe_fault_code("")
    assert result == "Unknown component Malfunction"


def test_lookup_is_case_insensitive():
    """Regression: confirmed live via monitoring.fault_log_backfill that
    the target sends the same component code with inconsistent casing
    across devices/history (e.g. "Uxingguangan" vs the table's
    "uxingguangan") -- both must resolve to the same translation."""
    assert describe_fault_code("Uxingguangan") == describe_fault_code("uxingguangan")
    assert describe_fault_code("DIANZICHENG") == "Electronic scale Malfunction"
