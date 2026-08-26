"""Shared Jinja2Templates instance + template helpers, used by every
router in backend/api/ so pages render consistently (spec section 21:
clear visual indicators for health state)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates

from backend.deps import home_url_for
from db.models import HealthState
from orders.mapping import IST_TZ

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["home_url"] = home_url_for
# Not registered by core Jinja2 (only by Flask) -- needed for safely
# embedding a Python string/value as a JS literal in an inline <script>.
templates.env.filters["tojson"] = json.dumps


def ist(value: datetime | None, fmt: str = "%d %b %Y, %H:%M:%S IST") -> str:
    """Every timestamp on the equipment monitoring pages (dashboard,
    equipment detail, faults) is stored as UTC (db/models.py's
    _utcnow()) but was being rendered raw, i.e. still in UTC -- per
    explicit request, display these in IST instead, reusing the same
    IST_TZ already established for Order Summary (orders/mapping.py)
    rather than defining a second timezone constant. SQLite can lose
    tzinfo on round-trip, so a naive value is assumed UTC (the only
    thing _utcnow() ever produces) rather than left ambiguous --
    same defensive pattern already used in backend/api/alerts.py."""
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST_TZ).strftime(fmt)


templates.env.filters["ist"] = ist

_HEALTH_EMOJI = {
    HealthState.HEALTHY: "🟢",
    HealthState.WARNING: "🟡",
    HealthState.MALFUNCTION: "🔴",
    HealthState.OFFLINE: "⚫",
    HealthState.UNKNOWN: "⚪",
}

_HEALTH_CSS_CLASS = {
    HealthState.HEALTHY: "badge-healthy",
    HealthState.WARNING: "badge-warning",
    HealthState.MALFUNCTION: "badge-malfunction",
    HealthState.OFFLINE: "badge-offline",
    HealthState.UNKNOWN: "badge-unknown",
}


def health_emoji(state: HealthState | str | None) -> str:
    if state is None:
        return _HEALTH_EMOJI[HealthState.UNKNOWN]
    if isinstance(state, str):
        state = HealthState(state)
    return _HEALTH_EMOJI.get(state, "⚪")


def health_css_class(state: HealthState | str | None) -> str:
    if state is None:
        return _HEALTH_CSS_CLASS[HealthState.UNKNOWN]
    if isinstance(state, str):
        state = HealthState(state)
    return _HEALTH_CSS_CLASS.get(state, "badge-unknown")


templates.env.filters["health_emoji"] = health_emoji
templates.env.filters["health_css_class"] = health_css_class
