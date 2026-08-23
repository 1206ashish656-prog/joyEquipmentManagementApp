"""
Shared "period selector" logic for any summary tab that offers
Daily/Weekly/Monthly/YTD presets plus a user-provided custom date range
(spec: "Summarize cost incurred at user provided time period or standard
option like ytd, monthly, weekly or daily" — the same shape Order
Summary already used, extracted here so Cost Management reuses it
instead of re-deriving its own).
"""
from __future__ import annotations

from datetime import datetime, timedelta

PERIODS = ("daily", "weekly", "monthly", "ytd", "custom")


def period_range(period: str, as_of: str, start: str | None = None, end: str | None = None) -> tuple[str, str]:
    """Returns (start_date, end_date) as 'YYYY-MM-DD' strings, inclusive.

    period="custom" uses the caller-provided `start`/`end` directly
    (falling back to a single day at `as_of` if either is missing/invalid
    — never raises on bad input, matching this app's "uncertainty never
    becomes a crash" convention). Every other period is computed from
    `as_of`.
    """
    if period == "custom":
        try:
            s = datetime.strptime(start, "%Y-%m-%d").date() if start else None
            e = datetime.strptime(end, "%Y-%m-%d").date() if end else None
        except ValueError:
            s = e = None
        if s and e and s <= e:
            return s.isoformat(), e.isoformat()
        # Malformed/missing custom range: fall back to a single day
        # rather than silently guessing a "reasonable" range.
        d = datetime.strptime(as_of, "%Y-%m-%d").date()
        return d.isoformat(), d.isoformat()

    d = datetime.strptime(as_of, "%Y-%m-%d").date()
    if period == "daily":
        s = e = d
    elif period == "weekly":
        s = d - timedelta(days=d.weekday())  # Monday
        e = s + timedelta(days=6)
    elif period == "monthly":
        s = d.replace(day=1)
        next_month = (s.replace(day=28) + timedelta(days=4)).replace(day=1)
        e = next_month - timedelta(days=1)
    elif period == "ytd":
        s = d.replace(month=1, day=1)
        e = d
    else:
        s = e = d
    return s.isoformat(), e.isoformat()
