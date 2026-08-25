# Deploying to the cloud (Railway)

Written 2026-08-25 for a first-time cloud deployer — this walks through
every step, not just a checklist. See README's "Deploying to the cloud"
section for the *why* behind the architecture (2 services, not 3, and
why); this document is the *how*.

## Why Railway

Compared against Render and Fly.io on what this app actually needs (one
web process + one background worker sharing a filesystem, a managed
Postgres, no VPC/IAM knowledge required):

- **Railway** (chosen): native multi-service-from-one-repo with
  per-service start-command overrides, one-click Postgres with an
  auto-wired `DATABASE_URL` reference variable, per-service persistent
  volumes, a CLI (`railway run`) for one-off commands. Usage-based
  pricing — realistically **~$10–20/mo** for this workload (2 small,
  mostly-idle processes + a small Postgres instance).
- **Render**: similar simplicity, but a hard $7/mo-per-service floor
  makes 2 services + Postgres addon ~$21+/mo minimum, with no offsetting
  advantage.
- **Fly.io**: most powerful, but its Machine/region/volume model (a
  volume is physically pinned to one Machine in one region) surfaces
  exactly the infrastructure-topology knowledge a first-timer doesn't
  have yet, for no real benefit at this scale.

Pricing/features shift over time — verify current numbers at signup —
but this relative ordering is unlikely to flip for a workload this size.

## Architecture recap

Two deployed services, not three, even though there are three local
`--loop` scripts:

- **`web`** — `uvicorn backend.main:app`, the only one with a public
  URL. `Dockerfile`'s default `CMD`.
- **`combined-worker`** — `python -m monitoring.combined_worker --loop`,
  runs the equipment-polling loop and the order-refresh loop together in
  one process (see README for why this consolidation exists — it's a
  real correctness fix, not just cost-saving). No public port. One
  persistent volume at `/app/data`.
- **Postgres** — Railway's managed plugin.

## Step-by-step

**Step 0 — push this repo to GitHub.** No remote is configured yet.
Create an empty GitHub repository (no README/gitignore/license — this
repo already has all of those), then from the project directory:
```bash
git remote add origin https://github.com/<your-username>/<repo-name>.git
git push -u origin master
```
*If this fails:* "remote origin already exists" means a previous attempt
already set it — use `git remote set-url origin ...` instead. An auth
prompt means GitHub credentials aren't configured — `gh auth login`
first, or use a personal access token when prompted for a password.

**Step 1 — create a Railway account.** Go to railway.com, sign in with
GitHub (this also grants Railway read access to your repos for step 2).

**Step 2 — new project, connect the repo.** "New Project" → "Deploy
from GitHub repo" → pick this repo. Railway detects the `Dockerfile` at
the repo root and creates one service from it automatically — this
becomes the `web` service.
*If it fails to detect the Dockerfile:* confirm it's committed and
pushed at the repo root (not inside a subfolder), then retry from the
project's service settings.

**Step 3 — add the Postgres plugin.** In the same project: "New" →
"Database" → "Add PostgreSQL." Railway provisions it and exposes a
`DATABASE_URL` reference variable other services in the project can
consume as `${{Postgres.DATABASE_URL}}`.

**Step 4 — configure the `web` service.**
- Leave its Start Command blank (it uses the Dockerfile's default `CMD`).
- Settings → Networking → "Generate Domain" — gives you a public HTTPS
  URL. This is the only one of the two services that needs one.
- Variables tab: add every variable from the table below. Easiest: add
  them once as project-level "Shared Variables" so the `combined-worker`
  service (step 5) inherits them automatically instead of re-entering
  everything twice.

| Variable | Production value | Differs from local `.env`? |
|---|---|---|
| `TARGET_BASE_URL`, `TARGET_LOGIN_URL`, `TARGET_USERNAME`, `TARGET_PASSWORD` | same as local `.env` | no |
| `STORAGE_STATE_PATH` | `data/storage_state/session.json` | no — resolves to `/app/data/storage_state/session.json` inside the container |
| `HEADLESS` | `true` | **yes** — no display exists on a cloud server |
| `AUTO_SOLVE_CAPTCHA` | `true` | **yes** — the pre-existing, explicitly authorized (2026-08-23) exception for this one target; required since no human is present to solve a CAPTCHA on a headless server |
| `AUTH_MANUAL_TIMEOUT_SECONDS`, `POLL_INTERVAL_SECONDS`, `POC_ITERATIONS` | same as local `.env` | no |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` (Railway's own reference syntax) | **yes** — not manually typed; leave `DB_HOST`/`DB_PORT`/`DB_NAME`/`DB_USER`/`DB_PASSWORD` unset |
| `SMTP_HOST`/`SMTP_PORT`/`SMTP_USERNAME`/`SMTP_PASSWORD`/`SMTP_FROM_EMAIL`/`SMTP_USE_TLS`, `ALERT_ON_ESCALATION`, `SEND_RECOVERY_NOTIFICATIONS` | same as local (leave SMTP_* blank until real credentials exist; set them the same way here whenever ready) | no |
| `WEB_SECRET_KEY` | fresh output of `python -c "import secrets; print(secrets.token_hex(32))"` | **yes** — must never match the local dev value |

*If the build fails:* check the build logs for a `requirements.txt` or
base-image pull error. *If it builds but crashes on startup:* check the
deploy logs — usually a blank required env var.

**Step 5 — add the `combined-worker` service.** "New" → "GitHub Repo" →
the same repo again (Railway supports deploying one repo as multiple
services in one project). Settings → Deploy → Custom Start Command:
```
python -m monitoring.combined_worker --loop
```
No public domain needed — leave networking off. Use the same shared
variables from step 4.
Then attach the volume: Settings → Volumes → "New Volume," mount path
`/app/data`.

**Step 6 — trigger the first deploy.** Both services deploy
automatically once their settings are saved. Watch each "Deployments"
tab go green.
*If `combined-worker` fails on first boot:* check its logs for the
first re-auth cycle (Chromium launching → navigating to
`TARGET_LOGIN_URL` → session written). A failure here is almost always
a wrong `TARGET_USERNAME`/`TARGET_PASSWORD`, or the Dockerfile's base
image tag not actually matching `playwright==1.47.0` in
`requirements.txt`.

**Step 7 — seed the real admin account.** Install the Railway CLI
locally, then:
```bash
railway login
railway link          # pick this project
railway run --service web python -m db.seed_admin --name "Ashish" --email 1206ashish656@gmail.com --password "<choose a genuinely strong password now>"
railway run --service web python -m db.seed_venue_mapping   # only if venue-partner accounts are needed
```
Both scripts call `db_base.create_all()` themselves — this is also what
creates the schema on the fresh Postgres database (no separate migration
step; Alembic is deferred, see below). **Do not** seed the local demo's
weak-password accounts (`demo@example.com`/`admin123` etc.) into
production — this creates a real admin with a password you choose.
*If it fails:* a connection error means `DATABASE_URL` isn't reaching
the `web` service correctly — recheck step 4.

**Step 8 — verify.**
- Visit the `web` service's public URL at `/healthz` → expect `{"status": "ok"}`.
- Go to `/login`, sign in with the admin account from step 7.
- Confirm the dashboard shows real equipment data (proves
  `combined-worker` → Postgres → `web` are all correctly connected).
- Confirm the Order Summary tab shows today's data (proves the
  `reload_cookies()` fix is working — no manual intervention needed).
- In `combined-worker`'s logs, confirm both `"Poll cycle complete:
  status=SUCCESS..."` (roughly every 60s) and `"... refreshed — N raw
  orders..."` (roughly every 300s) lines appear, interleaved in the same
  log stream — proof both loops are genuinely running concurrently in
  one process.

## Cost reality

Not free — two always-on processes plus a managed Postgres instance is
real, continuous compute. Budget for a genuine recurring bill, realistic
range **~$10–20/mo** on Railway for this exact workload, moving with
actual usage rather than a fixed number.

## Explicitly deferred for this first deployment

- Alembic migrations (schema stays on `Base.metadata.create_all()`)
- Rate limiting on login/API routes
- CSRF protection on mutating forms
- Structured logging / observability stack (platform's own log tail only)
- Automated DB backups
- Custom domain / DNS (the platform's auto-generated URL only)
