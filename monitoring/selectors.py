"""
Centralized selectors/locators for the target application.

This is intentionally the ONLY place (together with equipment_extractor.py's
column-header mapping) that should need to change if the target site's HTML
changes — see project requirement #34 (isolate the integration layer).

STATUS: PENDING PHASE 1 DISCOVERY.
Everything below is a best-effort first pass based on the field/menu labels
given in the spec (section 4 sidebar, section 5 table), written as
text/role-based locators rather than brittle CSS classes so they have a
reasonable chance of working unmodified. Each one is marked so it's easy to
find and confirm/replace once discovery/inspect.py has produced real HTML
and network samples (see data/discovery_output/ after running it).
"""
from __future__ import annotations

# --- Login page ---
# TODO_DISCOVERY: confirm actual input names/ids from data/discovery_output/login_page.html
LOGIN_USERNAME_INPUT = "input[name='username'], input[name='account'], input[type='text']"
LOGIN_PASSWORD_INPUT = "input[name='password'], input[type='password']"
LOGIN_CAPTCHA_INPUT = "input[name='captcha'], input[name='verify_code'], input[name='code']"
LOGIN_CAPTCHA_IMAGE = "img[src*='captcha'], img[alt*='captcha' i]"
LOGIN_SUBMIT_BUTTON = "button[type='submit'], button:has-text('Login'), button:has-text('Sign in')"

# --- Post-login indicators (used for session validity checks) ---
# TODO_DISCOVERY: replace with a real authenticated-only element (e.g. user
# avatar, "Console" heading, logout link) once known.
AUTHENTICATED_MARKER_TEXT = "Console"
LOGIN_PAGE_MARKER_TEXT = "Login"  # if this is visible, we are NOT authenticated

# --- Sidebar navigation ---
NAV_EQUIPMENT_MANAGEMENT = "text=Equipment Management"
NAV_DEVICE_INFORMATION = "text=Device Information"

# --- Device Information table ---
# TODO_DISCOVERY: confirm this actually renders as a <table>. Many admin
# panels (Element UI / Ant Design, which this app's URL pattern suggests)
# render "tables" as divs with role=row/role=cell instead. If so, update
# DEVICE_TABLE_CONTAINER / DEVICE_TABLE_ROW / DEVICE_TABLE_CELL and the
# extraction logic in equipment_extractor.py accordingly.
DEVICE_TABLE_CONTAINER = "table"
DEVICE_TABLE_HEADER_ROW = "thead tr"
DEVICE_TABLE_HEADER_CELL = "th"
DEVICE_TABLE_BODY_ROW = "tbody tr"
DEVICE_TABLE_BODY_CELL = "td"

# Pagination (62 machines strongly implies a paged table).
# TODO_DISCOVERY: confirm control markup; this targets common
# "next page" affordances.
PAGINATION_NEXT_BUTTON = (
    "button[aria-label*='next' i], "
    "li.btn-next, "
    "a:has-text('Next'), "
    "button:has-text('Next')"
)

# Expected raw column header labels -> our internal field names.
# Extend this mapping as more optional fields are confirmed during discovery.
# Keys are matched case-insensitively against the table's header text.
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
    "remaining oranges": "remaining_oranges",
}
