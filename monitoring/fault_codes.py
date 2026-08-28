"""
Translation table for the target application's per-component fault codes
(the `code` field on its Equipment Management > Fault Information tab,
served by GET device/device_fault_log/index -- see selectors.py's
DEVICE_FAULT_LOG_API_PATH).

Harvested verbatim from the target's own backend language pack --
CONFIRMED live 2026-08-29 via
GET /NgsEmfuaOv.php/ajax/lang?controllername=device.device_fault_log&lang=en-us,
the exact endpoint the target's own require-backend.js loads client-side to
resolve every __('...') call its fault-log table's column formatter makes
(device/device_fault_log.js): `return __(value) + ' ' + __("故障")` --
i.e. the target's own UI renders precisely "<this description> Malfunction"
for a row, which is what describe_fault_code() below reproduces. Verified
against one real historical row (target's own id=21529, device_id=109,
code="luozhentanzhenkaiguan") which the target's own formatter would
render as "Drop cup probe switch Malfunction" -- matches this table.

Kept as a static table (rather than calling that lang endpoint live) so
the alert pipeline never depends on one more network round-trip succeeding
at the worst possible moment -- mid-incident, see monitoring/worker.py.

An unmapped code is NOT an error (requirement #28: a technical/knowledge
gap must never block or corrupt an alert) -- describe_fault_code() falls
back to a best-effort readable label and logs a warning so this table can
be extended once a genuinely new component code is observed.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# code -> English component name, exactly as translated by the target's
# own backend/ajax/lang endpoint (device.device_fault_log controller,
# en-us). Two of these (dianzicheng, luozhentanzhenkaiguan) are also the
# two fault types independently confirmed via the target's own UI
# screenshot ("Electronic scale Malfunction" / "Drop cup probe switch
# Malfunction") -- the rest were captured from the same lang dump but not
# yet independently seen triggering on a real machine.
FAULT_CODE_DESCRIPTIONS: dict[str, str] = {
    "l3pidaidaiji": "Three-layer belt motor",
    "zhazhidianji": "Drop cupper motor",
    "daoliucaodianji": "Diversion channel motor",
    "fengmoshebei": "Sealing equipment",
    "cemenmensuo": "Side door lock",
    "quhuomensuodianji": "Pick-up port door lock motor",
    "yicengguanggan": "First layer light sensor",
    "ercengguanggan": "Second layer light sensor",
    "sancengguanggan": "Third layer light sensor",
    "luozhentanzhenkaiguan": "Drop cup probe switch",
    "daoliucansang": "Upper limit switch of diversion channel",
    "daoliucanxia": "Lower limit switch of diversion channel",
    "fengmoji": "Sealing machine limit switch",
    "uxingguangan": "U-shaped cup-moving light sensor",
    "quwumenkai": "Open/close the access door",
    "quwumenguan": "Open/close the access door",
    "maindoor": "External door",
    "zhibeijiance": "Paper cup detection sensor",
    "luochengchuanganqi": "Orange drop sensor",
    "kuwenchuanganqi": "Storage temperature sensor",
    "huashuangchuanganqi": "Defrosting sensor",
    "dianzicheng": "Electronic scale",
}

# The target's own translation of "故障" (the word its formatter always
# appends after the component name).
_MALFUNCTION_SUFFIX = "Malfunction"


def describe_fault_code(code: str) -> str:
    """Returns "<Component> Malfunction" for a raw fault-log `code`,
    matching the target application's own rendering exactly for every code
    confirmed in FAULT_CODE_DESCRIPTIONS. Never raises -- an unrecognized
    code (this table was captured from the target's lang pack, not
    guaranteed exhaustive) falls back to a readable guess built from the
    raw slug and is logged once so the table can be extended."""
    description = FAULT_CODE_DESCRIPTIONS.get(code)
    if description is None:
        logger.warning("Unrecognized fault code %r -- add it to monitoring.fault_codes once identified", code)
        description = code.replace("_", " ").strip() or "Unknown component"
    return f"{description} {_MALFUNCTION_SUFFIX}"
