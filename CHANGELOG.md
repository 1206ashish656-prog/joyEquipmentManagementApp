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

**Follow-up (2026-08-24): `orders/realtime_worker.py`.** Answers "why
does today's order data not show up?" — `orders/backfill.py` only ever
processes a fully-elapsed date on purpose (a day still accumulating
orders would get permanently cached on a partial snapshot otherwise), so
today never appears until tomorrow's backfill run. This new script is
the deliberate exception: always targets "today" in the target's own
UTC+8 calendar (recomputed every cycle) and unconditionally overwrites,
whether or not that date already has a `SUCCESS` row. `python -m
orders.realtime_worker --loop` (default every 5 minutes) runs as a
fourth long-lived process alongside the equipment worker and dashboard.
Handles the UTC+8 day-rollover case explicitly — the day that just ended
gets one final refresh before the new day starts being tracked, so
orders placed in the last few minutes before midnight aren't lost.
9 new tests; 205/205 passing overall. Live-verified: today's data was
confirmed missing, then appeared (215 raw → 172 qualifying orders, 3
groups) within seconds of running `--once`; `--loop` left running.

**Follow-up (same day): reporting timezone switched from the target's
UTC+8 to IST.** Per explicit request — "I want to see glasses sold today
(as per Indian timezone)". `order_date` (the single source of truth for
every day boundary in the whole `orders/` pipeline) is now computed in
IST (`orders/mapping.py`'s `IST_TZ`), not the target's own UTC+8
(`TARGET_TZ`, which is unchanged and still needed internally). Since IST
is 2.5 hours behind UTC+8, one IST day always straddles two of the
target's own UTC+8 days — `orders/client.py`'s `fetch_day()` now queries
both and filters to the IST match, rather than trusting the target's own
bucketing directly. `orders/realtime_worker.py`'s `today_utc8()` became
`today_ist()`.

**Full history re-backfilled** under IST boundaries the same day
(`--force`, 2026-05-02 through 2026-08-23) so there's no seam between
old UTC+8-bucketed and new IST-bucketed data — every stored day uses the
same convention. Caught and fixed a bug of my own along the way: the
"unexpected order_date" sanity-check warning in `fetch_day()` had the
wrong expected-date window (only looked forward a day, not also
backward) and fired on every single legitimate cross-day spillover —
cosmetic only, the actual returned/stored data was correct throughout,
but fixed and covered by a regression test
(`test_fetch_day_legitimate_spillover_does_not_warn`) before trusting
the logs again. Also cleaned up two stray `OrderSummary`/
`OrderSummaryRun` rows for "today" and a not-yet-existing-in-IST future
date, left over from the old UTC+8-based realtime worker's last cycle
before being replaced.

18 new/updated tests (IST day-boundary computation with an injectable
`now`, `orders/client.py`'s two-target-day fetch-and-filter logic via
`httpx.MockTransport`, the spillover-warning regression). 213/213
passing overall. Live-verified: today's IST total is 152 orders (3
machines) after the fix and re-backfill.

**Follow-up (same day): edit staff roster entries.** Per explicit
request — "Suppose I mistakenly marked an employee as left; now I want
to reset the employee status to previous state using edit functionality.
Also, show mark as left option once edited and employment remains
active." Every roster row now has an Edit link (`/staff/{id}/edit`,
`backend/templates/staff_edit.html`) that can change any field —
clearing the employment end date is exactly how a mistaken "Mark as
left" gets undone, and the roster's existing `not
employment_end_date` check is what naturally brings "Mark as left" back
into view once they're active again (no new conditional needed). 7 new
tests. 220/220 passing overall. Live-verified against the demo DB's own
Priya Sharma, who happened to already be marked left (2026-08-24) —
edited her end date blank, confirmed she shows active with "Mark as
left" restored on the roster.

**Follow-up (same day): "build the email alerting system."** Turned out
this already existed and was already running — `services/alert_engine.py`
+ `services/notification_service.py`, wired into `monitoring/worker.py`
since Phase 4, detecting a MALFUNCTION/OFFLINE transition exactly once
and composing a message with the machine's name/ID/code/health/fault
text/severity/detected time. Verified this with real evidence rather
than taking it on faith: found 8 real `FaultIncident` rows already in
the demo DB, 3 with `notification_sent=True` from real incidents.

What was actually missing, clarified by asking rather than guessing: a
**flat, admin-managed recipient list** not tied to a dashboard account
(the existing model requires either the `admin` role or a per-user
`/subscriptions` row). Added `AlertRecipient` (`db/models.py`) and
`/alert-recipients` (admin-only CRUD — add/deactivate/reactivate an
email, optional reference name). `AlertEngine.get_recipients()` now
unions in every active `AlertRecipient` whenever severity is Critical —
same trigger as the admin-always rule, additive to it, not a
replacement.

Found and fixed a **pre-existing UI bug** while building the recipients
page: `form.stacked-form`'s CSS centered every such form, not just the
login page it was meant for — every "Add X" form on Cost Management,
Staff & Leave, and now Alert Recipients rendered indented/centered
instead of left-aligned. Fixed at the CSS root (`.login-box` already
handles login's own centering independently) instead of patching yet
another template with an inline override.

18 new tests (recipient resolution incl. Critical-only/inactive/dedup-
with-admins, `/alert-recipients` CRUD + RBAC). 233/233 passing overall.
Live-verified end-to-end against the real demo DB: simulated a fresh
MALFUNCTION through the exact code path `monitoring/worker.py` uses,
watched the composed message list all three real recipients (the admin
+ two `AlertRecipient` emails, one of which — `support@refresha.in` —
was added by the user directly while this was being built), then
cleaned up the synthetic equipment/incident afterward. **The one
remaining real gap, unchanged from Phase 4**: no SMTP credentials are
configured, so every alert still only reaches the console log, not a
real inbox.

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
