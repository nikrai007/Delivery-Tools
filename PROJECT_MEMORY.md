# PROJECT_MEMORY.md — Delivery Toolbox

> Architectural memory for future AI/dev sessions. Read this **plus** `CLAUDE.md`,
> `DESIGN.md`, and `PRODUCT.md` before changing anything. This file captures the
> *how and why* that isn't obvious from the code. Keep it updated when you change
> a convention or learn something non-obvious.

Last updated: 2026-07-11.

---

## 1. What this is

A single self-hosted **Flask** process that hosts several independent internal
tools behind one login ("one login, many safe tools"). Tools are isolated
blueprints; they share auth, navigation, a design system, audit logging, and a
single SQLite datastore. Deployed on Oracle Cloud Always-Free (Ubuntu 22.04 +
waitress + nginx + Let's Encrypt + DuckDNS), Singapore region.

- App root: `Delivery-Tools/` (repo root also holds `.venv` py3.14 and a `build/`).
- Entry point: `app.py` — application factory `create_app()`; run dev `python app.py`,
  prod `python -m waitress --listen=0.0.0.0:5000 app:app`.
- **Never touch `To_Ship/`** (excluded, release-staging).

## 2. Architecture

- **Blueprint-per-tool.** Each tool is a top-level hyphenated folder
  (`auto-backup-revert-tool/`, `xpm-automator-tool/`, `release-tracker-tool/`, …)
  with `source-code/`, `templates/`, `documentation.md`.
- Hyphenated folders aren't importable, so `app.py` pushes each tool's
  `source-code` dir onto `sys.path` (`_CODE_DIRS`) and imports modules by plain
  name (`import rt_routes`). Sub-packages (`qgen_core/`, `xpm_core/`) import as
  packages under `source-code/`.
- **Shared layer:** `shared/constants/constants.py` (single source of config),
  `shared/utilities/` (`decorators.py` = `admin_required`/`team_leader_required`,
  `audit.py` = audit helper, `security.py`, `launcher.py`, `screen_content.py`),
  `database/database-config/` (`models.py`, `db_providers.py`, `db_migrate.py`),
  `login/` (auth + flask-login `login_manager`).
- **Persistence:** one SQLite DB via `models.connect()` (raw SQL, `sqlite3.Row`).
  `init_db()` runs a big idempotent `SCHEMA` + `ALTERS`. `setting_get/set` is a KV
  store; `record_audit` + the `audit` helper log events.
- **Data-driven nav/landing:** the `portal_tools` table drives which cards/nav
  entries appear; `models.list_accessible_tools(user)` respects role + per-team +
  per-user access grants.

## 3. How to add a tool (the zero-impact seam)

Mirror `xpm-automator-tool` or `release-tracker-tool` exactly:
1. `app.py`: append `<tool>/source-code` to `_CODE_DIRS`.
2. `app.py`: `from x_routes import x_bp` + `import x_store`.
3. `app.py`: `x_store.init_store()` (after other stores) + `register_blueprint(x_bp)`.
4. A `x_store.py` that owns its tables via `models.connect()` with an idempotent
   `CREATE TABLE IF NOT EXISTS` guarded by a lazy `_ensure()` flag, and an
   `ensure_registered()` that upserts a `portal_tools` card. **Do not edit `models.py`.**
5. `landing-page/source-code/landing_routes.py`: add a `LANDING_TOOLS` card
   (first-run seed). `templates/base.html`: add a `<slug>.*` sub-nav block.
6. `requirements.txt`: pin any new dep.

Templates `{% extends "base.html" %}` and fill `page_title` / `content` / `scripts`.

## 4. Conventions

- **Code:** `from __future__ import annotations`; module-level `log =
  logging.getLogger("<tool>")`; UTC ISO timestamps via a local `_now()`
  (`...isoformat(timespec="seconds") + "Z"`); pure service layers must not import
  Flask (keep them unit-testable); swallow-and-log in audit/registration paths so
  they never break a request or startup (`except Exception: log.exception(...)`).
- **DB (platform):** raw parameterised SQL through `models.connect()`. Never
  f-string user input into SQL. Tool tables are prefixed by tool (`xpm_*`, `rt_*`).
- **DB (external, Release Tracker only):** SQLAlchemy Core via `db_providers`;
  bound params only; column/table names come from whitelists/slugs, never raw input.
- **API:** JSON endpoints return `{ok: true, ...}` or `{ok: false, error: "..."}`
  with an appropriate status. Role checks are enforced **server-side** in the view,
  not just hidden in the UI.
- **UI:** Tailwind (CDN) with the `brand` indigo palette + `.dark` class; **Material
  Symbols** icons (NOT Heroicons — Stitch designs must be translated); **Inter** for
  UI, **JetBrains Mono** (`.rt-mono` / `font-mono`) for IDs/dates/numbers. Theme-aware
  light+dark. Reuse `_flash.html`, `_status_badge.html`, `_edit_content.html`.
- **Naming:** tool slug is short (`abr`, `xpm`, `rt`); blueprint name == slug;
  endpoints `slug.view`; files `<slug>_routes.py` / `<slug>_store.py` / `<slug>_service.py`.

## 5. Design system (see DESIGN.md / PRODUCT.md)

North star "The Glass Instrument": glass/aurora on the *frame* (auth, topbar),
crisp utilitarian surfaces for *tasks*. Iris indigo `#6366f1`/`#4f46e5` is the only
action/selection color over zinc neutrals. Cards `rounded-2xl` + 1px border +
`shadow-sm`; inputs/buttons `rounded-xl`; primary buttons get a soft indigo glow.
WCAG 2.1 AA. A real Stitch project **"CRM Orchestration Engine"** (id
`14727986602826141515`) encodes this exact system — regenerate reference screens
there for consistency, then implement in the platform's Tailwind (do not ship raw
Stitch export).

## 6. Reusable pieces worth knowing

- `decorators.admin_required` / `team_leader_required` (team-leader also admits admins).
- `audit.record(action, category=CAT_*, target_*, old/new_value, details)` — never raises.
- `db_providers` — provider registry (SQLite/Postgres/MySQL/MSSQL/Oracle/Mongo) with
  `build_sqlalchemy_url`, `driver_available`, `test_connection`. Lazy driver imports.
- `screen_content` — admin-editable on-screen copy (`content(screen, field, default)`).
- `models.setting_get/set` — KV store for feature settings.

## 7. Design decisions & lessons

- **Self-contained tool stores** (own tables via `models.connect()`, no `models.py`
  edits) keep tools drop-in and zero-impact. Proven by XPM and Release Tracker.
- **Release Tracker targets real external DBs per project** (user decision): project
  metadata (encrypted connection config) lives in platform SQLite (`rt_projects`);
  release rows live in `release_tracker_<slug>` tables created dynamically in the
  configured engine. Reuses `db_providers`. Secrets encrypted with AES-256-GCM
  (`rt_secrets`, key = SHA256(SECRET_KEY)); never returned to the browser.
- **Oracle connects by service name**, not a database/SID. `db_providers`
  emits `?service_name=…` (SID fallback via the `database` field). This was a real
  bug: the old `/dbname` URL is treated as a SID and fails on Oracle Cloud listeners.
- **Excel** via `openpyxl` (added dep); CSV via stdlib. Real date typing on xlsx export.
- **Grid is client-rendered** from JSON (`/api/records`) for responsiveness; grouping
  (same Enhancement ID + Upload Date + Category → collapsed batch range like `84-90`)
  is computed server-side in `rt_service.group_records` + `compress_batches`.

## 8. Common pitfalls (bit us before)

- `dict.setdefault(k, v)` does **not** replace an existing `None` value — caused a
  `NOT NULL` insert failure on `upload_date`. Use `if not rec.get(k): rec[k] = v`.
- Sidebar subtitle historically showed `app_name` ("AutoBackupRevert", a single tool)
  on every page. Use `platform_tagline` for platform-level labels; `APP_NAME` is the
  deployment identity (About page, email subjects) — don't conflate them.
- `.rt-in` uses a `padding` shorthand that **overrides** Tailwind `pl-*` utilities
  (source order). Don't rely on `pl-9` on `.rt-in`; use `!pl-9` or avoid leading icons.
- IDE diagnostics constantly report "Cannot find module flask/models/…" because the
  IDE resolves against the system interpreter, not `.venv`, and the tool folders are
  added to `sys.path` at runtime. **These are false positives — ignore them.**
- Headless-Chrome screenshots need a **Windows** `file:///C:/…` URL (not the MSYS
  `/c/…` path) and `--virtual-time-budget` so the Tailwind CDN + fonts + JS load.

## 9. Frequently modified modules

`app.py` (wiring), `templates/base.html` (shared shell/nav), `landing_routes.py`
(tool cards), `database/database-config/models.py` (platform schema — edit
carefully; shared by everything), `shared/constants/constants.py`.

## 10. Dependencies (see requirements.txt)

Flask 3.0.3, Flask-Login, Werkzeug 3.0.4, waitress 3.0, python-dotenv, APScheduler,
cryptography, pycryptodomex (Cryptodome), requests, **openpyxl** (Excel),
**SQLAlchemy 2.0** + `psycopg[binary]` / `PyMySQL` / `oracledb` / `pymongo`. `pyodbc`
(MSSQL) is intentionally commented out — install per-server. No beautifulsoup4 (use
stdlib `html.parser`). No pandas.

## 11. Known limitations / technical debt

- External-DB engines are cached in-process without an LRU cap or disposal (fine for
  a handful of projects; revisit if projects proliferate).
- No "edit project connection" UI in Release Tracker (`rt_store.update_project_config`
  exists but is unwired) — recreate the project to change a connection.
- Import/bulk read the whole file into memory (bounded by `MAX_UPLOAD_MB`=500).
- Tests are exercised via the Flask test client + service-layer scripts; there is no
  committed automated test suite/CI yet.
- `tool-3` is an intentional scaffold; several `LANDING_TOOLS` are "coming soon"
  placeholders — not dead code, not to be "completed" without a real spec.

## 12. Future considerations

Commit a `pytest` suite + CI; add an LRU/disposal policy for external engines; add a
CSRF layer if any tool starts accepting cross-origin writes; consider promoting the
platform datastore off SQLite via the existing `db_migrate` cut-over path if
concurrency grows.
