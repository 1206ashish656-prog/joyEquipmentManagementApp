# Changelog

All notable milestones for this project, newest first. Not every commit
gets an entry — this tracks meaningful, user-facing checkpoints, not a
mirror of `git log`. See [`docs/prompt_logs.md`](docs/prompt_logs.md) for
the verbatim request behind each of these, and
[`docs/NEXT_SESSION_PROMPT.md`](docs/NEXT_SESSION_PROMPT.md) for the
living, more granular version of "pending work."

## [Unreleased] — since 2.0.0

Two more admin-only tools, both explicitly restricted to the `admin`
role (`operations`/`venue_partner` get `403`, same as `/users`):

- **Cost Management** (`/costs`, `costs/` package) — log operating costs
  against standard categories (Oranges, Glass, Straws, Sealing Films,
  Staff Salaries, Rent, Cleaning Items) or a custom one via "Others";
  vendor name is skipped for Staff Salaries and defaults to a filterable
  `"UNSPECIFIED"` placeholder (not a bare NULL) if left blank elsewhere.
  Summarized over Daily/Weekly/Monthly/YTD or a **user-provided custom
  date range**, with independent breakdown by category/vendor/item name.
- **Staff & Leave Management** (`/staff`, `staff/` package) — a staff
  roster with employment start/end dates, leaves logged by the admin on
  a staff member's behalf, and a monthly summary that explicitly
  highlights any staff with more than 2 leave days that month (correctly
  clipping leaves that span a month boundary).
- `backend/period_utils.py` extracted from Order Summary's period-range
  logic (now shared by both features) and gained a `custom` period —
  Order Summary can use it too, though its UI doesn't expose it yet.
- Cost Management gained, per follow-up request: filter dropdowns to
  view raw data for a selected category/vendor/item (narrows both the
  rollup and a new Raw Entries table beneath it); edit and delete on any
  logged entry, with the same category/vendor resolution rules as
  creation reused for edits (`_resolve_category_and_vendor`). A
  never-edited entry's `updated_at` stays `NULL`.

Also resolved the RBAC open question from 2.0.0: **operations staff do
not see Order Summary at all**, confirmed — no code change needed, that
was already how `require_venue_partner` was built.

**Follow-up (same day):**
- Staff gained `department`/`sub_department` (free text, e.g. "Operations"
  / "Logistics") — shown in the roster, set on creation, suggested via a
  `<datalist>` of previously-used values so spelling stays consistent
  without a hard-coded list.
- Cost Management's Raw Entries table is now **hidden by default** —
  a "Raw data: Show / Hide" radio pair controls it explicitly, per
  follow-up request ("should only be displayed if explicitly asked").
  The category/vendor/item filters and rollup breakdown are unaffected
  either way.

See README's "Cost Management and Staff & Leave Management" section for
the full picture. 196/196 tests passing.

## [2.0.0] — 2026-08-24

Everything built on top of the original equipment-monitoring app (1.0.0):
an independent order-analytics feature, full role-based access control,
and a rebrand to the company's own visual identity. All 142 automated
tests passing; every piece below has also been verified against the real
target site or a live local server, not just in tests.

### Added

- **Order Summary feature** (`orders/` package, two new tables). Daily
  order data from Order Management → Order Information, filtered to
  `order_status=Completed AND delivery_status=Success`, stored at the
  finest useful grain — one row per `(date, device_app, price,
  pay_type)`, so a mid-day price change produces separate rows instead of
  a blended average. Every other view (aggregate, machine-wise,
  price-wise, pay-type-wise, or any combination) is a rollup computed at
  query time (`orders/rollup.py`), not a separately stored table.
  - Dashboard tab at `/orders/summary`: Daily/Weekly/Monthly/YTD period
    selector, independent breakdown checkboxes (machine/price/pay type,
    any combination), an always-on aggregated trend chart plus an
    optional by-machine trend chart (Chart.js via CDN).
  - Full historical backfill: 2026-05-02 through 2026-08-23 (114 days,
    zero failures) — `python -m orders.backfill --start … --end …`.
  - A real, previously-undocumented target-app detail surfaced building
    this: the `createtime` date-range filter uses **UTC+8 day
    boundaries**, not UTC/IST/local time (see
    `docs/target_application_integration_spec.md`).
  - **Admin-only Revenue and Oranges/Glass** metrics — `orders/rollup.py`
    computes `revenue = orders × avg price` and `oranges_per_glass =
    total oranges / orders` as derived properties, rendered only for the
    `admin` role.
- **Role-based access control** (`db/models.py`'s `UserRole`, enforced via
  `backend/deps.py`). Three roles: `admin` (unrestricted), `operations`
  (equipment monitoring only — Dashboard/Active Faults/My Alerts, no
  order data), `venue_partner` (Order Summary only, scoped to their own
  venue). Enforced at the route level, not just hidden nav links; nav and
  post-login redirect are role-aware too.
- **Venue mapping** — new `venue_mapping` table (`machine_name` →
  `venue_provider`, matched case-insensitively), seeded via `python -m
  db.seed_venue_mapping`:
  `PNR → PNR Felicity`, `NEXUS → Forum Kormangala`, `Gravity → Prestige Tech Park`.
  A venue partner with no venue assigned (or one mapped to zero machines)
  gets an explicit message rather than a silently-empty page.
- **Rebrand to JOY (joyjuice.in)** — warm orange/cream palette (`#fea419`
  brand, extracted from the live site), Poppins typeface, rounded
  cards/pill buttons, plus a working **dark/light theme toggle**
  (`backend/static/theme.js`, no-flash pre-paint script, persisted via
  `localStorage`, reachable from every page including login). Same
  FastAPI + Jinja2 backend throughout — restyle only, no JS framework or
  build step added (explicit choice over a full React rewrite).
- **`docs/prompt_logs.md`** — a chronological, mostly-verbatim record of
  every user prompt that shaped this build, maintained going forward
  rather than left to erode across context compactions.

### Fixed

- `LightweightTargetClient.is_session_valid()` was probing the dashboard
  shell page, which returns HTTP 200 even with zero cookies (a
  false-positive trap) — switched to probing the actual list API.
- Orders with a null `device.name` in the raw feed were bucketed as
  `"UNKNOWN"`; confirmed with the account owner that these are all Nexus
  machine data, so the fallback now maps to `"NEXUS"` (48 already-cached
  days were re-fetched and recomputed).

### Changed

- CAPTCHA login can now be fully automated (`AUTO_SOLVE_CAPTCHA=true`) —
  an explicit, disclosed, account-owner-authorized deviation from the
  original 1.0.0 spec, specific to a confirmed bug in this one target
  (any 4 characters pass CAPTCHA validation). Defaults to `false` and the
  human-in-the-loop path is unchanged for any other target.
- Steady-state polling moved off a persistent Playwright browser onto a
  plain `httpx` client (`monitoring/lightweight_client.py`); a real
  browser now launches only transiently, for the CAPTCHA login step
  itself.

## [1.0.0] — 2026-08-23

Initial build, from the original specification: session-aware monitoring
of a third-party equipment-management web app, a canonical health-state
engine, incident detection/escalation/resolution, email alerting, and a
server-rendered FastAPI + Jinja2 dashboard.

- Phase 1 — Target application discovery (confirmed live).
- Phase 2 — Monitoring proof of concept: session reuse, real equipment
  extraction via the target's own JSON API.
- Phase 3 — Database + state engine (`services/health_engine.py`,
  `services/state_manager.py`), live-verified against SQLite.
- Phase 4 — Alerting (`services/alert_engine.py` +
  `services/notification_service.py`), confirmed firing on real
  incidents; no real email actually sent (no SMTP credentials available).
- Phase 5 — Web dashboard, live-verified with real HTTP traffic.
- Phase 6 (production hardening) was explicitly out of scope for 1.0.0 —
  see Pending below.

---

## Pending / known gaps (as of 2.0.0)

Not silently dropped — these are the open items, roughly in the order
they'd matter for taking this beyond local/demo use. See
`docs/NEXT_SESSION_PROMPT.md` for more detail on each.

**Resolved:**
- Whether `operations` staff should see Order Summary — **no, confirmed
  2026-08-24.** Matches how it was already built: `require_venue_partner`
  only admits `admin`/`venue_partner`, `operations` gets `403`. No code
  change needed, just closing out the open question.

**Needs a decision from the user:**
- The `Warehouse` device (`avg_price = ₹0.01`, 10 orders total) is still
  unexplained — real data, not a bug, but not investigated.
- Chart.js trend-line colors are still generic (blue/green/orange/red),
  not derived from the JOY brand palette — flagged, not changed, since it
  wasn't asked for.

**Verification gaps (blocked on infrastructure/credentials, not code):**
- Never run against real PostgreSQL — only SQLite (dev/test/demo). No
  Docker/Postgres available in this environment so far.
- Never sent a real email — `NotificationService`'s SMTP path is
  unit-tested with `smtplib` mocked only; no SMTP credentials available.
- Order Summary totals have not been independently cross-checked against
  the target app's own UI (only against this app's own CLI/DB/dashboard
  agreeing with each other).

**Product/process gaps introduced by this session's features:**
- No in-app way to edit an existing user's role or venue — `/users` only
  sets these at creation time; changing them today means a direct DB
  edit.
- No self-service invite/password-set flow for a new venue partner — an
  admin sets their initial password directly.
- The demo accounts created for RBAC verification (`demo@example.com`,
  `ops_demo@example.com`, `venue_demo@example.com`) use weak, throwaway
  passwords — fine for local demo, should be rotated or removed before
  this ever points at anything beyond this machine.

**Phase 6 — production hardening (not started, pick based on actual
need, not all of this unprompted):**
- Alembic migrations (schema is currently hand-patched — see README's
  RBAC section for the manual `ALTER TABLE` this release already needed).
- Retry-with-backoff within a single poll cycle.
- Browser crash/restart recovery for the re-authentication path.
- Structured logging, a health-check endpoint, Prometheus/Grafana.
- Docker images for the worker + backend, Nginx reverse proxy.
- A secrets manager in place of plain `.env`.
- Documented DB backup/restore procedure.
- Rate limiting on login/API routes; a CSRF token on mutating forms
  (currently relies only on `SameSite=Lax`).
- 🔖 Always-on cloud hosting, so the app doesn't fully stop whenever the
  host machine sleeps (raised earlier, not yet acted on).

**Lower priority, needs user direction before building:**
- Multiple monitoring profiles (more than one target account).
- Wire the target API's rich per-component `this_fault` detail into
  incidents (currently only a flat `fault_type` string is kept).
- Confirm real-world fault/shortage vocabulary once this account
  produces an actual non-"Normal" reading outside the spec's examples.
- Configurable dashboard session lifetime (fixed at 7 days).
