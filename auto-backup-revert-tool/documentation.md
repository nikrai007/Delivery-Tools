# AutoBackupRevert — Tool Overview

> **Self-service, FK-safe Oracle rollback-script generator with full audit trail and admin-driven auto-ingestion from external storage.**

| Author | Owner | Status | Last reviewed |
|---|---|---|---|
| Nikhil Kumar (EC2845) | Personal Dev Corporation Ltd | Production (Oracle Cloud, Singapore) | 2026-06-24 |

---

## 1. Executive summary

AutoBackupRevert takes an Oracle migration bundle (`.sql`, `.zip`, or `.7z`) — uploaded manually or pulled automatically from a watched folder / Git repo — and emits a **single tagged bundle ZIP** organised in numbered folders:

| Folder / File | Artefact | Role |
|---|---|---|
| `01_Backup/01_Backup.sql` | **Backup** | Snapshots the affected rows into `BKP_<table>_<YYMMDDHH>` tables *before* the migration runs. |
| `02_Migration/<scripts>` | **Migration source** | The original uploaded migration scripts, preserved for traceability. |
| `03_Revert/01_Revert.sql` | **Revert** | Replays each DELETE block in source order, then INSERTs from the BKP snapshots in reverse order (parent → child) — FK-safe by construction. |
| `04_Drop_Backup/01_Cleanup.sql` | **Cleanup** | Drops the `BKP_*` snapshots once the rollback window has passed. |
| `ALTERS.sql` *(root)* | **Alters** | Verbatim `ALTER TABLE` statements from the bundle, grouped by file. |
| `PROCEDURES.txt` *(root)* | **Procedures** | Index of stored-code definitions (PROCEDURE / FUNCTION / PACKAGE / TRIGGER). |

Every bundle is tagged with a mandatory **enhancement name** and **production loading date** so it's traceable in the history page and downloadable as one ZIP for hand-off to a DBA.

It replaces a Colab notebook one engineer was hand-maintaining; deployed on Oracle Cloud Always-Free in Singapore.

---

## 2. The business problem

Oracle migrations in BFSI / CRM are **high-stakes and routinely rolled back**. Historical pain points:

- **DBAs hand-write rollback scripts** under time pressure → FK ordering errors → `ORA-02292` → escalation.
- **No standard backup discipline**: some engineers snapshot, others don't; the format differs per author.
- **No traceability**: which release? which ticket? which prod date? — scattered across email, chat, file shares.
- **No central pipeline**: scripts live in WhatsApp, attachments, shared drives, USBs.
- **No safety guarantees**: audit / log tables get accidentally rolled back, polluting compliance data.
- **No way to plug into release automation**: every team copies the file to the DBA by hand.

The tool solves all six.

---

## 3. Solution overview

A Flask + SQLite web app with two equally first-class job ingest modes:

### Manual web flow
1. Engineer logs in.
2. Fills two mandatory fields — **enhancement name** + **production loading date**.
3. Uploads `.sql` / `.zip` / `.7z` (≤ 500 MB) or points at a server-side path.
4. Reviews the detected `delete.sql`.
5. Clicks **Generate** → downloads the **single BUNDLE_*.zip**.

### Scheduler flow (admin-configured "watched sources")
1. Admin registers a watched source — either a **local / network folder** or a **Git repository** — with a destination folder and a cadence.
2. APScheduler polls the source on the configured cadence.
3. New `.sql` / `.zip` / `.7z` files are auto-processed; the bundle ZIP is dropped at the destination folder; the job appears in history under that admin's account.
4. **Enhancement name** is taken from the source-file's **parent folder name**; **production date** is today.
5. SHA-256 idempotency manifest prevents the same file being processed twice.

Both flows produce the same five-artefact bundle and the same history-page entry.

---

## 4. Architecture (high level)

```
┌──────────────────────────────────────────────────────────────────────┐
│                        EXTERNAL STORAGE                              │
│                                                                      │
│  ┌──────────────────────┐    ┌──────────────────────┐               │
│  │  Local folder / SMB  │    │     Git repository   │               │
│  │  (releases inbox)    │    │  (branch + sub-path) │               │
│  └──────────┬───────────┘    └──────────┬───────────┘               │
│             │  poll                       │  fetch                   │
└─────────────┼───────────────────────────┼──────────────────────────┘
              │                           │
        ┌─────┴─────┐               ┌─────┴─────┐
        │  connect- │               │  connect- │
        │  ors/     │               │  ors/     │
        │  local.py │               │  git_repo │
        └─────┬─────┘               └─────┬─────┘
              │                           │
              └─────────────┬─────────────┘
                            │
                  ┌─────────┴─────────┐
                  │   scheduler.py    │      (APScheduler — cron / N-min)
                  │   orchestrator    │
                  └─────────┬─────────┘
                            │
                  ┌─────────┴─────────┐
                  │      core.py      │      (scan + BACKUP / REVERT / CLEANUP)
                  └─────────┬─────────┘
                            │
                  ┌─────────┴─────────┐
                  │  bundle ZIP   ←───┼── delivered to dest_path
                  │  BUNDLE_*.zip │   │
                  └───────────────────┘

┌──────────────────────────────────────────────────────────────────────┐
│   Browser  (Tailwind + Material Symbols + dark/light toggle)         │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ HTTPS (Let's Encrypt)
┌──────────────────────────────┴───────────────────────────────────────┐
│   nginx  (reverse proxy, gzip, static caching)                       │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
┌──────────────────────────────┴───────────────────────────────────────┐
│   waitress  (production WSGI, systemd-managed)                       │
│   ┌──────────────────────────────────────────────────────────────┐   │
│   │  Flask app                                                   │   │
│   │   ├── auth.py        — login / register / password reset     │   │
│   │   ├── core.py        — scan + BACKUP / REVERT / CLEANUP      │   │
│   │   ├── connectors/    — local + git pluggable connectors      │   │
│   │   ├── scheduler.py   — APScheduler + orchestrator            │   │
│   │   ├── models.py      — SQLite persistence layer              │   │
│   │   ├── app.py         — routes, jobs, downloads, admin        │   │
│   │   └── email_utils.py — SMTP for password-reset mail          │   │
│   └──────────────────────────────────────────────────────────────┘   │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
       ┌───────────────────────┼────────────────────────────┐
       │                       │                            │
┌──────┴──────┐         ┌──────┴───────┐            ┌───────┴────────┐
│  data/app.db│         │  uploads/    │            │  .env (secrets)│
│  (SQLite)   │         │  (per-job +  │            │                │
│             │         │  scheduler   │            │                │
│             │         │  cache)      │            │                │
└─────────────┘         └──────────────┘            └────────────────┘
```

**Deployment**: Oracle Cloud Always-Free VM, Ubuntu 22.04, Singapore region (`ap-singapore-1`). DuckDNS subdomain + Let's Encrypt cert. Runs as a `systemd` unit. Also runs on **Windows Server** — see §14 for the deployment recipe.

---

## 4a. Persistence layer

A single **SQLite** database file at `data/app.db` (relative to the project root). SQLite is bundled with Python — no separate DB server, no install, no port to open. Perfect fit for a single-process waitress deployment.

### Tables

| Table | Rows | Purpose |
|---|---|---|
| `users` | one per account | username, email, hashed password (PBKDF2), role (user/admin), `last_login_at`, `created_by_user_id`, `is_active` |
| `jobs` | one per run | input metadata, **enhancement_name + prod_date**, generated-file paths, **bundle_file**, status (`collecting`/`reviewed`/`generated`/`failed`), source (`manual`/`scheduler`), watched-source FK |
| `downloads` | one per download | audit log: `(job_id, user_id, filename, downloaded_at, ip_address)` |
| `password_resets` | short-lived | one-time hashed reset tokens with `expires_at` |
| `watched_sources` | one per source | name, kind (`local`/`git`), source_path, dest_path, **schedule_json** (the rich v2 cadence), legacy interval columns, enabled, last_run_status/at/message |
| `processed_files` | growing | idempotency manifest: `(watched_source_id, file_hash)` unique — prevents the scheduler reprocessing the same file twice |
| `settings` | key/value | runtime feature flags |
| `api_tokens` | dead | empty table retained on existing installs from the removed REST API; safe to ignore |

### Schema management

- **`SCHEMA`** constant in `models.py` is the up-to-date CREATE TABLE / CREATE INDEX script. Fresh installs run this verbatim.
- **`ALTERS`** list in `models.py` is the migration ledger — one `ALTER TABLE … ADD COLUMN …` per upgrade. Runs **before** SCHEMA on every boot so existing DBs are brought forward; errors from "column already exists" are swallowed.
- `init_db()` runs both, idempotent on every restart.

### Where each model field is used

- **`watched_sources.schedule_json`** is the canonical schedule.
  - Written by the admin source form (`_parse_schedule_form` in `app.py`).
  - Read by `scheduler._register()` on boot and after every CRUD edit → builds an APScheduler `CronTrigger`.
  - Read on every fire by `scheduler._is_paused()` to honour temporary snoozes.
  - Read by the admin source list (`app.admin_sources()`) and rendered as the human cadence label + next-fires preview.
  - Mirrored into the legacy `interval_kind` / `interval_value` columns so any reader written before the v2 upgrade still sees something sensible.
- **`jobs.enhancement_name` / `prod_date`** are persisted on creation, surfaced everywhere (history filters, result page banner, bundle filename).
- **`processed_files`** is the SHA-256 idempotency layer between the scheduler and the connectors.

### When you would outgrow SQLite

| Trigger | What to do |
|---|---|
| Running multiple Flask processes (gunicorn workers, multi-instance load balancer) | Migrate to PostgreSQL — SQL is standard, only the connection layer in `models.py` changes |
| Sustained > 50 writes/sec | Same |
| Need replication / HA / point-in-time recovery | Same |
| Single process, < 50 writes/sec | **Stay on SQLite — that's the entire current deployment.** |

---

## 5. The five-stage process flow

### Stage 1 — Ingest
* **Manual**: user fills enhancement + prod-date, uploads `.sql` / `.zip` / `.7z` (≤ 500 MB).
* **Scheduler**: connector pulls candidate files from the watched source on the configured cadence. SHA-256-hashed against `processed_files`; duplicates skipped.

### Stage 2 — Scan
`core.collect_deletes` walks every `.sql` file (natural-sorted), strips comments, splits by `;`, and groups consecutive DELETE statements into **blocks**. Anything that isn't a DELETE (`COMMIT`, `UPDATE`, etc.) closes a block; file boundaries also close blocks.
In parallel: ALTERs are catalogued, stored-code definitions (PROCEDURE / FUNCTION / PACKAGE / TRIGGER) are listed, and trigger names referenced by either `CREATE TRIGGER` or `ALTER TRIGGER` are collected.
Job moves to `reviewed`.

### Stage 3 — Generate
`core.generate_backup_revert` walks the collected `delete.sql` text and, **per block**, builds:
- A list of original DELETE statements (in source order).
- A matching list of INSERT-from-BKP statements (in reversed order).

Tables matching `_LT$` / `_LOG$` / `^SBC_` are **omitted entirely** from BACKUP/REVERT (they're audit / lookup / log tables that shouldn't be rolled back).

### Stage 4 — Bundle
Every artefact is packed into a structured **`BUNDLE_<enhancement>_<prod_date>_job<N>.zip`** with numbered folders (`01_Backup/`, `02_Migration/`, `03_Revert/`, `04_Drop_Backup/`); ALTER scripts and procedure definitions sit at the ZIP root. Scheduler-driven runs deliver this ZIP to the destination path; manual runs keep it inside the job's work-dir and expose a Download-bundle button.

### Stage 5 — Review / Cleanup (DBA workflow, off-platform)
The DBA runs BACKUP.sql first (snapshots), then runs the migration, then — if rollback is needed — runs REVERT.sql. After the rollback window expires, CLEANUP.sql drops the snapshots.

---

## 6. User roles & permissions

| Capability | Anonymous | User | Team Leader | Admin |
|---|---|---|---|---|
| Register / log in | ✓ | — | — | — |
| Upload bundle / generate scripts | — | ✓ | ✓ | ✓ |
| Mandatory enhancement-name + prod-date on upload | — | ✓ | ✓ | ✓ |
| See **own** job history (Personal Dashboard) | — | ✓ | ✓ | ✓ |
| Download own generated files + bundle ZIP | — | ✓ | ✓ | ✓ |
| View **team dashboard** (team stats + activity) | — | — | ✓ (own team only) | ✓ |
| View + download **team members' jobs** | — | — | ✓ (own team only) | ✓ |
| Approve / reject team join requests | — | — | ✓ (own team only) | ✓ |
| See **all users' jobs** (history `&all=1`) | — | — | — | ✓ |
| Manage users (create / activate / reset password) | — | — | — | ✓ |
| **CRUD teams** + assign team leaders | — | — | — | ✓ |
| **CRUD watched sources** (local + Git) | — | — | — | ✓ |
| Configure scheduler cadence + Run now | — | — | — | ✓ |
| Upload / reset platform logo | — | — | — | ✓ |
| Read all download audit rows | — | — | — | ✓ |

Roles are bootstrapped on first run via the `ADMIN_USERNAME` / `ADMIN_PASSWORD` env vars in `.env`.

---

## 7. Watched sources — scheduler design

### Two connector kinds
| Kind | What it watches | Auth | Destination |
|---|---|---|---|
| **Local** | A folder on the server, a mounted SMB / NFS share, or a sub-tree thereof | None — relies on filesystem ACLs | Local folder (configurable) |
| **Git** | A branch in a Git repository, optionally restricted to a sub-path | HTTPS + PAT (Personal Access Token), stored server-side | Local folder (configurable) |

Both deliver the generated bundle ZIP to the configured `dest_path` after each run.

### Configurable cadence (admin-driven, production-grade)

Each watched source carries its own rich `schedule_json` blob. The form lets admins compose any of these without touching cron syntax:

| Knob | Options | Example |
|---|---|---|
| **Mode** | `weekly_at` · `every_minutes` · `cron` | "Specific times on chosen days", "Every N minutes", or "Custom cron" |
| **Day-of-week mask** | any subset of Mon–Sun | Mon–Fri only · Sat+Sun only · single day · arbitrary mix |
| **Day presets** | one-click | **Every day · Mon–Fri · Sat–Sun · Sun–Thu** (Mid-East workweek) |
| **Time slot(s)** | one or more `HH:MM`, per day | `09:00` · `09:00,14:00,21:00` |
| **Every-N-minutes** | integer 1–720 (≤ 12h) | `5`, `15`, `30`, `60`, `360` |
| **Cron** | standard 5-field | `0 9-17/2 * * 1-5` (every 2h, 9–5, weekdays) |
| **Time zone** | any IANA zone | `Asia/Singapore`, `Asia/Kolkata`, `Asia/Dubai`, `Europe/London`, … |
| **Valid from / Valid until** | YYYY-MM-DD bounds | "Active only during July 2026" |
| **Pause until** | ISO timestamp | release-freeze snooze — fire instants before this point are skipped |

**Defenses built in:**
- All inputs are validated on save by actually building the APScheduler trigger; invalid combos are rejected with a clear message.
- `interval_minutes` is capped at 720 (12 h). For longer cadence the user is routed to `weekly_at` mode with a fixed time — avoids cron-step overflow and ambiguous fire instants.
- `_is_paused()` is also checked at fire time so a stale snooze in the DB still blocks processing.
- Hot-reload: `scheduler_mod.reload_source(id)` is called on every save/edit, so cadence changes take effect without restarting the app.

### Live "Next 5 fires" preview

The admin source-edit form posts every change (300 ms debounced) to `/admin/sources/preview`, which builds a trigger from the in-flight form values and returns the next 5 UTC fire times + a human label like `Mon–Fri at 09:00, 14:00 · Asia/Singapore`. Admins see *exactly* when the schedule would fire before they save.

### Pause / Resume / Run-now

Three independent admin actions per source:
- **Run now** — fires the orchestrator immediately, regardless of cadence. Useful for ad-hoc batch ingest.
- **Snooze 24h** — sets `pause_until = now + 24h` so the next scheduled fire is skipped. The source list shows the snooze expiry.
- **Resume** — clears `pause_until` immediately.

The orchestrator records `last_run_status ∈ {ok, no_new_files, error}` and `last_run_message` on every fire, visible in the source list with colour-coded status pills (active / snoozed / disabled).

### Idempotency
Every file the connector discovers is SHA-256-hashed and checked against `processed_files (watched_source_id, file_hash)` before processing. A repeat-run on the same folder reports `no_new_files` and creates no new jobs.

### Failure handling
Each run records `last_run_status` ∈ `{ok, no_new_files, error}` and a `last_run_message` visible on the admin sources page. A file that fails mid-process is **still** marked as processed so the scheduler doesn't loop on it; the admin can manually remove the `processed_files` row to force a re-run after fixing the input.

### Manual override
The admin sources page has a **Run now** button per source — fires the orchestrator in a background thread regardless of cadence. Useful for ad-hoc batch ingest.

---

## 8. Job metadata (mandatory, indexed)

Every job carries:
* `enhancement_name` — free text, 2–80 chars, `[A-Za-z0-9._- ]` only.
  - Manual: typed by the user.
  - Scheduler: derived from the source file's **parent folder name**.
* `prod_date` — `YYYY-MM-DD`.
  - Manual: picked by the user (defaults to today).
  - Scheduler: always today.
* Both columns are indexed; the history page filters on both plus a free-text "enhancement-or-filename" search and a status filter.

The fields are **enforced** at three layers:
1. HTML5 `required` + `pattern=` on the form.
2. Server-side regex validation in `_validate_metadata`.
3. SQL NOT-NULL on the `jobs` schema (new installs only — existing rows pre-migration keep NULL).

---

## 9. UI / UX walkthrough

| Page | Who sees it | Purpose |
|---|---|---|
| `/login`, `/register`, `/forgot`, `/reset` | All | Auth flows — all registration fields are mandatory |
| `/` | All | Landing hub — tool cards, theme toggle |
| `/dashboard` | User / Team Leader / Admin | **Personal Dashboard** — own KPI tiles, 30-day activity chart, recent jobs |
| `/new` | User / Team Leader / Admin | Upload form — enhancement name + prod date required before file-picker |
| `/review/<job>` | Owner / Admin | Preview of collected DELETE statements; "Generate" button |
| `/result/<job>` | Owner / Admin | Bundle banner (enhancement + prod date + **Download bundle (.zip)**) + artefact preview cards |
| `/history` | User / Admin | Filter bar (search · prod-date range · status); admins add `?all=1` to see all users |
| `/teams/my` | Team Leader | **Team Dashboard** — team KPIs, 30-day activity chart, recent team jobs, members table |
| `/teams/my/jobs` | Team Leader | Full filterable listing of every team member's jobs with per-artefact download buttons |
| `/teams/my/requests` | Team Leader | Approve / reject join requests for own team |
| `/admin` | Admin | Platform-wide dashboard — all users' jobs, charts, KPIs |
| `/admin/users` | Admin | User management — create / activate / deactivate / reset password |
| `/admin/logo` | Admin | Upload, preview, or reset the platform logo (PNG/JPG/SVG/WebP/GIF, max 5 MB) |
| `/admin/sources` | Admin | Watched sources — name, kind, source/dest, cadence, last-run status, run-now / edit / delete |
| `/admin/sources/new?kind=local` · `?kind=git` | Admin | Create source form — name, paths, PAT (Git), cadence picker with live "next 5 fires" preview |
| `/admin/sources/<id>/edit` | Admin | Edit source — same fields + enabled toggle |
| `/admin/job/<id>` | Admin | Per-job inspection: owner, files, all downloads |
| `/teams/admin` | Admin | Team CRUD — create / edit / delete teams, assign leaders, manage members |
| `/about` | All | Platform credits |

Theme toggles between light and dark via a top-right switch; choice persists per browser.

---

## 10. Security & audit posture

**Strengths in place:**
- Werkzeug PBKDF2 password hashing.
- Session-isolated job directories (UUID per job).
- Login required on every job/download/admin endpoint.
- Anti-enumeration on the forgot-password page (always returns the same message).
- Every download is logged with `(job_id, user_id, filename, IP, timestamp)`.
- Admin-level changes (user create, password reset, watched-source CRUD) are visible in the admin dashboard.
- Git PATs stored server-side; never shown in the UI after creation; placeholder reads `(saved — leave blank to keep current PAT)` on edit.
- Connectors validate paths and config before persisting a source (rejects non-existent source folders, missing `git` binary, etc.).
- REST API surface has been **fully removed** (no `/api/*` routes, no bearer-token decorator, no `/admin/api_tokens` UI). One less attack surface.

**Honest gaps** (listed so the audience knows we're not hiding them):
- Default `admin` password ships in `.env.example` — must be rotated on first deploy.
- No CSRF tokens on POST forms — Flask-WTF would fix in a day.
- No login rate-limiting — Flask-Limiter would fix in a day.
- Session cookies don't yet enforce `SECURE` / `HTTPONLY` / `SAMESITE` flags.
- Git PATs are stored as plaintext in `watched_sources.config_json`. For production, encrypt the column with a Fernet key sourced from `.env`.

These are listed in the roadmap (§12) as the **#1 priority hardening bundle**.

---

## 11. Known limitations

| Limitation | Workaround |
|---|---|
| Source-table names > 17 chars produce `BKP_*` names > 30 chars (fails on Oracle ≤ 12.1) | Target Oracle 12.2+ (128-char limit) **or** add a name-truncation rule |
| Two runs within the same clock hour collide on `BKP_*` names → `ORA-00955` on the second BACKUP | Drop the previous `BKP_*` tables first |
| The tool **trusts** the source DELETE order — if a migration was written parent-before-child it will faithfully echo that ordering | Future FK-chain linter (roadmap §12) will warn |
| CLEANUP.sql is not idempotent — `DROP` on an already-dropped table errors with `ORA-00942` | Comment out the affected lines or regenerate the script |
| Git connector currently delivers the bundle to a **local** dest folder (no push-back to the repo) | Mount a target folder that's already watched by your release pipeline; Git push-back is on the roadmap |
| Tool currently targets Oracle only | Future engine plugins for PostgreSQL / SQL Server (§12) |
| No SSO / LDAP integration | Local accounts only; on the roadmap |

---

## 12. Roadmap (pitch-ready)

Ranked by **pitch value to leadership** vs **engineering effort**.

### Governance trio (recommended next ship — ~4–5 days)
1. **JIRA / Change-Request ticket linkage** on every job (searchable in history alongside enhancement-name).
2. **Two-person approval workflow** — job sits in `pending_approval` until a second admin signs off before the bundle is delivered to dest_path.
3. **Audit-trail CSV export** for periodic compliance reviews (SOX / SOC2 / RBI cyber-audit).

### Production hardening (~2–3 days)
4. Default-admin-password rotation, CSRF tokens, login rate-limiting, secure cookie flags, encrypted PAT column.

### Operational quality (~3–4 days)
5. **Retention timer + auto-cleanup nudge** — show "BKP_* aged X days"; email reminder at day N.
6. **Slack / Teams notifications** — webhook on scheduler-source success / failure.
7. **Git push-back** — commit the generated bundle to a release branch instead of (or in addition to) a local dest folder.

### Engineering safety (~1–2 weeks)
8. **FK-chain visualisation** — inferred parent → child tree from WHERE subqueries.
9. **REVERT dry-run against a sandbox DB** — catch errors before prod.

### Long-term
10. PostgreSQL / SQL Server engine plugins.
11. SSO / LDAP integration.
12. Org / team accounts (multi-tenant).

---

## 13. Appendix — glossary

| Term | Meaning |
|---|---|
| **Block** | A run of consecutive `DELETE FROM …` statements with no other statement in between. The atomic unit of FK reversal. |
| **BKP_ table** | A snapshot table named `BKP_<original>_<YYMMDDHH>` created by BACKUP.sql. |
| **FK-safe order** | DELETEs flow child → parent; INSERTs flow parent → child. Both halves satisfy referential integrity. |
| **Skip filter** | The regex `^SBC_ \| _LT$ \| _LOG$` that excludes audit / lookup / log tables from BACKUP+REVERT. |
| **Trigger wrap** | The `ALTER TRIGGER … DISABLE; … ENABLE;` brackets at the top and bottom of REVERT.sql, applied when triggers are detected in the bundle. |
| **Alias threading** | When a source DELETE uses a table alias (`DELETE FROM tbl t WHERE t.id = …`), the alias is propagated into the generated INSERT's BKP subquery so the WHERE predicate binds correctly. |
| **Job** | One end-to-end run: ingest → scan → generate → bundle. Persisted in `data/app.db`. |
| **Watched source** | An admin-registered external location (local folder or Git repo) that the scheduler polls for new migration bundles. |
| **Connector** | A small Python class that knows how to discover candidate files from a watched source and deliver a generated bundle to its destination. |
| **Bundle ZIP** | `BUNDLE_<enhancement>_<prod_date>_job<N>.zip` — single download with numbered folders: `01_Backup/01_Backup.sql`, `02_Migration/<source scripts>`, `03_Revert/01_Revert.sql`, `04_Drop_Backup/01_Cleanup.sql`; `ALTERS.sql` and `PROCEDURES.txt` at root. |
| **Idempotency manifest** | The `processed_files` table — keyed on `(watched_source_id, file_hash)` — that prevents the scheduler from reprocessing files it has already seen. |

---

## 14. Windows Server deployment recipe

The tool runs unchanged on Windows Server. Key points the Ubuntu deployment doesn't have to think about:

### Will SQLite cause issues on Windows?
**No** — provided you follow two rules:
- **Keep `data\app.db` on a local NTFS volume**, not an SMB / DFS / OneDrive-synced location. SQLite's locking semantics are well-defined on NTFS but historically flaky over SMB.
- **Run a single waitress process** (no multi-worker fork). SQLite serializes writes; multiple processes will see "database is locked" errors under any real load. The whole architecture assumes single-process; if you ever need to scale out, you've outgrown SQLite anyway (see §4a).

### Required on the box
| Component | Why | How |
|---|---|---|
| Python 3.11+ | the app | install from python.org · choose "Add to PATH" |
| `git` binary | the Git connector | install Git for Windows, default settings |
| Visual C++ runtime | required by `py7zr` / `pycryptodomex` wheels | usually already present on Server 2019+ |

### Run as a Windows Service (so it survives reboots)

The Ubuntu deployment uses `systemd`. On Windows the equivalent is **NSSM** (Non-Sucking Service Manager):

```powershell
# 1. Download nssm.exe and put it on PATH
# 2. Register the service
nssm install AutoBackupRevert ^
  "C:\opt\autobackuprevert\.venv\Scripts\python.exe" ^
  "-m waitress --listen=0.0.0.0:5000 app:app"
nssm set AutoBackupRevert AppDirectory "C:\opt\autobackuprevert"
nssm set AutoBackupRevert Start SERVICE_AUTO_START
nssm set AutoBackupRevert AppStdout "C:\opt\autobackuprevert\logs\app.log"
nssm set AutoBackupRevert AppStderr "C:\opt\autobackuprevert\logs\app.log"
nssm start AutoBackupRevert
```

Service Account: create a dedicated low-privilege local user (e.g. `svc-autobackup`) and grant it **Modify** on `C:\opt\autobackuprevert\data\` and `C:\opt\autobackuprevert\uploads\`. Run the service as that account, not LocalSystem.

### Reverse proxy + TLS
- **IIS** with the URL Rewrite + ARR modules — proxy to `http://127.0.0.1:5000` and let IIS terminate TLS.
- **Caddy for Windows** — automatic Let's Encrypt cert renewal in a single `Caddyfile`.
- **nginx for Windows** — same setup as the Linux deployment, port-for-port.

Whichever you pick: bind the public address to 443, keep waitress bound to 127.0.0.1 only.

### Watched-source paths on Windows
- Use **Windows-native paths** everywhere in the admin form: `C:\releases\inbox` or `\\fs01\share\inbox\AutoBackupRevert`.
- Do **not** use forward-slash UNIX paths like `/tmp/foo` — Python on Windows resolves those to `<current-drive>:\tmp\foo` which is rarely what you want.
- For **network shares**, grant the service account `Modify` on the share AND on the underlying NTFS folder. Reading the inbox needs `Read & Execute`; writing the outbox needs `Modify`.

### IANA timezone names work fine
Windows ships a different TZ DB than Linux, but APScheduler uses **pytz** (bundled), so the scheduler accepts IANA names like `Asia/Singapore` regardless of the host OS. No `tzdata` configuration needed.

### Firewall
Open inbound TCP 443 (public) and TCP 5000 (loopback only) in Windows Defender Firewall. The Git connector needs outbound 443 to your Git host (GitHub, GitLab, Azure DevOps, Bitbucket — whichever).

### What could trip you up
| Symptom | Cause | Fix |
|---|---|---|
| "database is locked" | running multiple waitress workers | use 1 process |
| Git connector hangs on PAT prompt | Git's credential helper trying to prompt | `connectors/git_repo.py` already sets `GIT_TERMINAL_PROMPT=0` and `GIT_ASKPASS=/bin/true`; verify Git for Windows is recent enough (≥ 2.30) |
| Scheduler shows "discover failed: \[WinError 5\] Access is denied" | service account lacks Read on the watched folder | grant Read on the source folder, Modify on the destination |
| Bundle ZIP never lands in destination | path interpretation mismatch (UNIX-style path) | use `C:\` or `\\server\share\` paths |
| TLS cert error during `git fetch` | corporate proxy with MITM CA | install the corporate CA into the Windows trust store, or set `GIT_SSL_CAINFO` in the service env |

---

## 15. Quick demo script (for the live demo slide)

1. **Show admin → Watched sources page**, empty.
2. **Add a local watched source** pointing at `\\fs01\releases\inbox`, dest `\\fs01\releases\outbox`, cadence "Every 5 minutes".
3. **Copy** `samples/01_SBC-Upgrade_Gold_8_26759_26765.sql` into the inbox.
4. Click **Run now** on the source. The history page shows a new `scheduler`-source job a few seconds later, with enhancement-name = the inbox folder name and prod-date = today.
5. Open the **outbox** folder — the `BUNDLE_*.zip` is there, ready for the DBA pipeline to pick up.
6. **Re-run** the source. `last_run_status` reads `no_new_files` — the SHA-256 idempotency works.
7. Switch to a regular user and **manually upload** the same file with a tagged enhancement-name and a future prod-date. Show the **bundle banner** on the result page and the **Download bundle (.zip)** button.
8. **Filter** the history page by prod-date range and by enhancement-name keyword.

---

*Personal Dev Corporation Ltd · © 2026*
