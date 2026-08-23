"""
Centralized selectors/URLs/field-mappings for the target application.

This is intentionally the ONLY place (together with the mapping helpers in
target_client.py) that should need to change if the target site's
structure changes — see requirement #34.

STATUS: confirmed against the live site via discovery on 2026-08-23 — see
docs/target_application_integration_spec.md for the full write-up. It is
a FastAdmin-style (ThinkPHP) back office with an iframe/tab shell:

  outer page  https://www.jwintell.com/NgsEmfuaOv.php/device/device?ref=addtabs
  contains an <iframe src="/NgsEmfuaOv.php/device/device?addtabs=1">
  whose Bootstrap Table (id="table") is populated by a JSON XHR:
  GET /NgsEmfuaOv.php/device/device/index?addtabs=1&sort=id&order=desc
      &offset=<N>&limit=<N>&filter={}&op={}
  (header X-Requested-With: XMLHttpRequest)

We use that JSON endpoint directly as the primary data source (requirement
#15: prefer a structured request over DOM scraping where one exists). DOM
scraping of the same table is kept as a fallback in case the endpoint
changes/breaks — those selectors are still best-effort/unconfirmed for
row-reading purposes (the page was never actually scraped via DOM in
practice) and should be re-verified against data/discovery_output/ if the
fallback path is ever exercised for real.
"""
from __future__ import annotations

# --- Login page ---
# CONFIRMED 2026-08-23 via direct DOM inspection of the live login form
# (data/discovery_output/login_page_fresh.html). ThinkPHP/FastAdmin-style
# form: username/password/captcha inputs, a "keeplogin" checkbox, and a
# captcha image served from /index.php?s=/captcha (regenerable via its
# onclick handler — irrelevant to us, see AUTO_SOLVE_CAPTCHA below).
LOGIN_USERNAME_INPUT = "input[name='username']"
LOGIN_PASSWORD_INPUT = "input[name='password']"
LOGIN_CAPTCHA_INPUT = "input[name='captcha']"
LOGIN_KEEPLOGIN_CHECKBOX = "input[name='keeplogin']"
LOGIN_SUBMIT_BUTTON = "button[type='submit']"

# --- Post-login indicators (session validity) ---
# CONFIRMED 2026-08-23: TC-001 correctly detected both the invalid
# pre-login state and the valid post-login state using these.
AUTHENTICATED_MARKER_TEXT = "Console"
LOGIN_PAGE_MARKER_TEXT = "Login"

# --- Device Information page ---
# CONFIRMED 2026-08-23. The outer "?ref=addtabs" URL just wraps this in an
# iframe for the tabbed admin shell; navigating straight to the iframe's
# own src (addtabs=1) avoids the sidebar's slide-toggle submenu, which
# does not reliably respond to a plain Playwright click (see
# docs/target_application_integration_spec.md).
DEVICE_INFORMATION_PATH = "/NgsEmfuaOv.php/device/device"
DEVICE_LIST_API_PATH = "/NgsEmfuaOv.php/device/device/index"
DEVICE_LIST_API_PAGE_SIZE = 100

# --- Device Information table (DOM fallback only — see module docstring) ---
DEVICE_TABLE_CONTAINER = "table#table"
DEVICE_TABLE_HEADER_ROW = "table#table thead tr"
DEVICE_TABLE_HEADER_CELL = "th"
DEVICE_TABLE_BODY_ROW = "table#table tbody tr"
DEVICE_TABLE_BODY_CELL = "td"
PAGINATION_NEXT_BUTTON = (
    "button[aria-label*='next' i], li.btn-next, a:has-text('Next'), button:has-text('Next')"
)

# Raw JSON API field -> our canonical EquipmentRecord field. Supports
# dotted paths for nested objects (e.g. device_type.name).
# CONFIRMED against a live response on 2026-08-23 (6 devices, incl. the
# ID 205 / NEXUS example from the spec).
API_FIELD_MAP: dict[str, str] = {
    "id": "equipment_id",
    "sn": "equipment_code",
    "name": "name",
    "device_type.name": "device_type",
    "status_text": "status",
    "online_status_text": "network_status",
    "fault_status_text": "fault_type",
    "lack_status_text": "material_shortage_status",
    "ad_group.name": "advertising_group",
    "address": "device_address",
    "shop_price": "selling_price",
    # UNCONFIRMED: no field literally named "remaining oranges" appears in
    # the API row despite the DOM column header claiming data-field
    # "orange_num" (likely a client-side bootstrap-table formatter, not a
    # raw field). Best guess pending confirmation against a device with
    # known remaining stock.
    "orange_weight": "remaining_oranges",
}

# Raw DOM header text (as rendered) -> canonical field, for the DOM
# fallback path only.
COLUMN_HEADER_MAP: dict[str, str] = {
    "id": "equipment_id",
    "status": "status",
    "network connection status": "network_status",
    "fault type": "fault_type",
    "material shortage status": "material_shortage_status",
    "device type": "device_type",
    "advertising group": "advertising_group",
    "equipment code": "equipment_code",
    "name": "name",
    "device address": "device_address",
    "selling price": "selling_price",
    "remaining number of oranges": "remaining_oranges",
}
