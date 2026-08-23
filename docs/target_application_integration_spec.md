# Target Application Integration Specification

Phase 1 deliverable. Captured via manual login + live inspection on
2026-08-23. Source artifacts: `data/discovery_output/` (git-ignored —
contains real account data; regenerate with `discovery/inspect.py` or the
ad hoc diagnostics in `tests/tc_001_target_connection/`).

## Application shell

FastAdmin-style (ThinkPHP) back office, tabbed/iframe admin shell:

- Login: `GET /NgsEmfuaOv.php/index/login?url=<redirect>` — username +
  password + a 4-character CAPTCHA image. Never automated (requirement #3).
- On success, redirects to the URL embedded in the login page's own `url`
  query param — for us, `/NgsEmfuaOv.php/dashboard?lang=en-us&ref=addtabs`.
  We derive this dynamically from `TARGET_LOGIN_URL` rather than
  hardcoding it (`target_client._derive_dashboard_url`).
- Every sidebar link (e.g. "Device Information") opens a `?ref=addtabs`
  wrapper page containing a single `<iframe src="...&addtabs=1">` — the
  wrapper is just tab-shell chrome; the iframe's own URL is where the
  real content (and its own page title, e.g. "Device Information") lives.
- The sidebar's "Equipment Management" parent uses a slide-toggle submenu
  that does **not** reliably expand from a plain Playwright `.click()` —
  the "Device Information" link resolves in the DOM but stays
  `visible: false`. We sidestep this entirely by navigating directly to
  the iframe's confirmed URL instead of driving the sidebar UI.

## Login form fields (confirmed 2026-08-23)

Direct DOM inspection of the live login form (ThinkPHP/FastAdmin style):

| Field | Selector | Notes |
|---|---|---|
| Username | `input[name='username']` | |
| Password | `input[name='password']` | |
| CAPTCHA | `input[name='captcha']` | text input, placeholder "验证码" (verification code) |
| CAPTCHA image | `img[src="/index.php?s=/captcha"]` | regenerates via its own `onclick` handler |
| Remember me | `input[name='keeplogin']` | checkbox — checking it is a small enhancement to reduce reauth frequency |
| Submit | `button[type='submit']` | text "登 录" (Login) |

## CAPTCHA behavior — confirmed glitch, account-owner-authorized automation

**Update 2026-08-23:** the account owner confirmed and explicitly authorized
exploiting a target-side bug: the CAPTCHA field's submitted value is never
actually validated against the code shown in the image — any 4
alphanumeric characters are accepted. Verified live: `authenticate()` now
fills the CAPTCHA field with a random 4-character string
(`monitoring/target_client.py::_submit_login_auto`) and the login
succeeds regardless of what the displayed image actually shows.

This directly contradicts the original project requirement ("must NOT be
designed around bypassing or defeating CAPTCHA" / "never attempt to
bypass CAPTCHA" — sections 3 and 33 of the original spec), which is why
it is implemented as an explicit, default-OFF opt-in
(`AUTO_SOLVE_CAPTCHA` — see `.env.example`) rather than a silent
behavior change, and documented here rather than presented as "the
system just doesn't need a human anymore." The human-in-the-loop path
(`_submit_login_manual`) is fully preserved and is what runs when this
flag is left at its default.

## Session validity

- Confirmed marker for "authenticated" (Playwright-based client,
  `target_client.py`): the text `Console` is present and visible on the
  post-login dashboard.
- Confirmed marker for "session invalid/expired" (Playwright-based
  client): current URL contains `login` after navigating to the
  dashboard URL.
- **Important correction (2026-08-23):** the lightweight HTTP client
  (`lightweight_client.py`) does NOT use the dashboard-URL check above.
  Testing with a completely empty cookie jar (no session at all) showed
  the dashboard shell page still returns HTTP 200 with no redirect —
  it's tab-shell chrome that renders regardless of auth state. That made
  the URL-based check a false-positive trap for "no session yet"
  specifically (as opposed to "invalid/expired session cookie", which it
  does still detect correctly). The lightweight client now probes the
  actual list API instead (`limit=1`) and checks the JSON shape:
  `{"total":N,"rows":[...]}` = valid, `{"code":0,"url":"...login...",
  "wait":N}` (FastAdmin's standard "please log in" AJAX response) =
  invalid. This is both more accurate and simpler, since it checks the
  literal endpoint the client depends on rather than a separate proxy
  signal.
- All of the above were exercised for real, including from a fully
  cold/deleted session: `is_session_valid()` correctly returned `False`,
  `authenticate()` completed with zero human input (headless, CAPTCHA
  field auto-filled), and the very next poll cycle succeeded (6/6 records).

## Device Information data source

**Primary (used): structured JSON API**, per requirement #15's preference
for a structured request over DOM scraping:

```
GET /NgsEmfuaOv.php/device/device/index
    ?addtabs=1&sort=id&order=desc&offset=<N>&limit=<N>&filter={}&op={}
Header: X-Requested-With: XMLHttpRequest
```

Response: `{"total": <int>, "rows": [ {...device...}, ... ]}`. Paginate by
incrementing `offset` by `limit` until `offset >= total` (see
`TargetApplicationClient._get_equipment_data_api`).

This account currently has **6** devices (not the spec's illustrative 62 —
that number was an example, not a guaranteed count).

**Fallback (implemented, not yet exercised against a real failure): DOM
scraping** of the same data via the underlying Bootstrap Table
(`<table id="table">`) rendered inside that iframe, used only if the API
call fails structurally (wrong content-type, missing `rows` key, etc.) —
see `TargetApplicationClient._get_equipment_data_dom`.

### Confirmed JSON field → canonical field mapping

| API field | Canonical field | Notes |
|---|---|---|
| `id` | `equipment_id` | |
| `sn` | `equipment_code` | |
| `name` | `name` | |
| `device_type.name` | `device_type` | nested object |
| `status_text` | `status` | e.g. `"Normal"` |
| `online_status_text` | `network_status` | e.g. `" Online"` / `"Offline"` (leading space seen on "Online" — stripped) |
| `fault_status_text` | `fault_type` | e.g. `"Normal"`; no faulty example observed yet — **fault vocabulary beyond "Normal" is still unconfirmed** |
| `lack_status_text` | `material_shortage_status` | e.g. `"Normal"`; no shortage example observed yet |
| `ad_group.name` | `advertising_group` | nested object |
| `address` | `device_address` | empty string on all 6 current devices |
| `shop_price` | `selling_price` | e.g. `"120.00"` |
| `orange_weight` | `remaining_oranges` | **best guess, unconfirmed** — no field literally named `orange_num` despite the DOM column header claiming `data-field="orange_num"`; likely a client-side bootstrap-table formatter. Revisit once a device with known stock differences is available. |

Full raw API row is preserved as `EquipmentRecord.raw` regardless (audit
requirement #10) — so even unmapped fields are never lost.

### Additional fields available but not yet wired in (optional, future)

The API row also includes (not currently mapped, since they're beyond the
spec's mandatory/optional field list, but confirmed present for later
use): `cup_num`/`film_num`-equivalents were not present under those exact
names — the real extras seen are `juiced_num`, `this_heat` (temperature),
`shop_num` (24h sales volume), `app_version`, `plc_version`,
`description`, `admin.nickname`, `createtime`/`updatetime`, and — most
notably — **`this_fault`**: a JSON-encoded array of per-component fault
flags (motor/sealing-machine/etc., each with `isGuZhang`
["is broken"]/`isTingji` ["is stopped"] booleans). This is a much richer
fault detail source than `fault_status_text` alone and is a strong
candidate for the "exact fault information" requirement (#10) once the
health engine (Phase 3) is built — currently preserved only inside `raw`.

There is also a **per-device fault history endpoint**, useful for Phase 3+
audit trails:

```
GET /NgsEmfuaOv.php/device/device_fault_log/index?sort=id&order=desc&dialog=1&device_id=<id>
```

## What's still unconfirmed

- No device in this account is currently reporting a non-"Normal"
  `fault_status_text` or `lack_status_text` — so the actual vocabulary of
  fault/shortage values (needed for the Phase 3 health-engine mapping
  table) is **not yet known**. TC-001 should be re-run once a real fault
  occurs (or a test device can be put into a fault state) to capture real
  values.
- `remaining_oranges` field mapping (see above).
- Whether `total` in the API response is a true server-side count across
  all pages or capped — only tested with `limit=200` against 6 total
  rows, i.e. always fit on one page so far.
