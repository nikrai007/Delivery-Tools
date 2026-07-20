# Release Tracker

Track enhancement/case releases across CRM, SIT, UAT, PreProd and Production.
Each **project** points at its own external database; release records are stored
there, not in the platform SQLite DB. Manual entry supports batch-range
expansion, and the module ships Import, Export, Bulk Update, inline editing,
dynamic grouping, filtering, search, pagination, role-based permissions and a
full audit trail.

Blueprint `rt`, mounted at `/tools/release-tracker`.

---

## Architecture (self-contained, zero platform impact)

Follows the platform's tool convention exactly (see the XPM Automator). Nothing
in `To_Ship/` is touched; `models.py` is **not** modified.

```
release-tracker-tool/
  source-code/
    rt_secrets.py   AES-256-GCM encryption for saved DB passwords (key = SECRET_KEY)
    rt_store.py     project registry (rt_projects) in platform SQLite + portal card
    rt_db.py        external-DB engine layer — dynamic table, CRUD, filters, bulk
    rt_service.py   pure domain logic — field spec, validation, batch expand, grouping
    rt_io.py        CSV + Excel (openpyxl) import / export / error report
    rt_routes.py    Flask blueprint — pages + JSON APIs + role guards
  templates/
    rt_macros.html          shared header/toolbar macros
    rt_config.html          Database Configuration (Admin / Team Lead only)
    rt_dashboard.html       main page — manual entry card + data grid + modals
    rt_dashboard_js.html    grid controller (vanilla JS, included into the page)
  documentation.md
```

**Integration seam (5 additive edits, mirrors XPM):**
1. `app.py` — added `release-tracker-tool/source-code` to `_CODE_DIRS`.
2. `app.py` — `from rt_routes import rt_bp`, `import rt_store`.
3. `app.py` — `rt_store.init_store()` after `xpm_store.init_store()`.
4. `app.py` — `app.register_blueprint(rt_bp)`.
5. `landing_routes.py` — `LANDING_TOOLS` card; `base.html` — `rt.*` sub-nav.
   `requirements.txt` — pinned `openpyxl>=3.1`.

The portal card is registered idempotently at startup (`rt_store.ensure_registered`),
so the tool appears in the data-driven nav/landing on existing installs too.

---

## Database Configuration

Restricted to **Admin** and **Team Lead** (`@team_leader_required`, which also
admits admins). Normal users cannot view or reach `/config`, create tables, or
change settings — enforced server-side, not just hidden in the UI.

Workflow (`/tools/release-tracker/config`):
1. Enter a project name and pick a provider (PostgreSQL / MySQL / SQL Server /
   Oracle / MongoDB* / SQLite).
2. **Test connection** — a live `SELECT 1` via the platform `db_providers`.
3. **Create & provision** — validates the connection, saves the project
   (password encrypted at rest), then creates the `release_tracker_<slug>` table
   in that database.

Supported engines and drivers come from the platform provider registry
(`database/database-config/db_providers.py`). Relational engines use SQLAlchemy;
the schema is built from portable generic column types so it materialises
correctly on every engine. If a driver is not installed on the server, the
provider is flagged in the form and connection tests report it clearly.

**Oracle:** connect by **Service Name** (Oracle Cloud / Autonomous DB and modern
listeners) — the form has a dedicated Service Name field that builds a
`?service_name=…` URL. The generic "Database" field is treated as a legacy **SID**
fallback. For Autonomous DB with a wallet, set `TNS_ADMIN` on the server and use
the TNS alias as the Service Name. (`pyodbc` for SQL Server is not installed by
default — install it on the server to use MSSQL.)

> *MongoDB is listed by the shared provider registry; the Release Tracker record
> store targets relational engines. Use a relational provider for a project DB.

**Security:** connection passwords are encrypted with AES-256-GCM
(`rt_secrets`, key derived from the platform `SECRET_KEY`) and never returned to
the browser after saving. Removing a project deletes only its saved
configuration — the external table is deliberately left intact.

---

## Record schema (`release_tracker_<slug>`)

| Column | Type | Notes |
|---|---|---|
| s_no | Integer PK, auto | S.No |
| enhancement_id | String, required | alphanumeric |
| release_subject | Text, required | |
| category | String, required | Release / Hotfix / Prod Fix / Other |
| other_category | String | required when category = Other |
| sent_by | String, required | employee id; self or same-team member |
| batch_number | Integer, required | unique among live rows |
| crm_delivery_date | Date, required | defaults to today in the UI |
| sit_date / uat_date / preprod_date / prod_live_date | Date | optional |
| upload_date | Date | set on insert; drives grouping |
| created_by / created_date / updated_by / updated_date | audit columns |
| is_deleted | Integer | soft delete |

---

## Features

- **Manual entry** — full-width card. A batch value of `84` inserts one record;
  `84-90` (or `84, 86, 90-92`) expands into one record per batch, copying every
  other field. "Other" category reveals a mandatory Other Category field.
  "Sent By" defaults to the logged-in employee and may be changed only to a
  member of the same team (validated server-side).
- **Data grid** — pagination (50/100/200/500), sortable columns, global search,
  column & date-range filters, sticky header, horizontal + vertical scroll,
  multi-row selection, inline editing (Enhancement ID, Subject, SIT/UAT/PreProd/
  Prod Live dates), keyboard-friendly native inputs, responsive layout.
- **Grouping** — rows sharing Enhancement ID + Upload Date + Category collapse to
  a single row showing the batch range (e.g. `84-90`); click to expand the
  individual batch rows. Toggle grouping on/off.
- **Import** (CSV/XLSX) — columns: Enhancement ID, Mail Subject, Category,
  Sent By, Batch Number, CRM Delivery Date (SIT/UAT/PreProd/Prod optional).
  Validates mandatory fields, data types, dates, category and Enhancement ID;
  **duplicate batch numbers are never inserted** (checked against existing rows
  and within the file). Returns an Inserted / Skipped / Failed summary plus a
  downloadable error report.
- **Export** (CSV/XLSX) — honours all active filters; Excel export applies a
  styled header, real date typing and frozen header row.
- **Bulk Update** (CSV/XLSX) — matches on Batch Number and updates only the
  columns present in the file; missing columns are left untouched (existing
  values are never blanked); unknown batch numbers are skipped; returns a
  summary.
- **Delete** — soft delete, **Team Lead / Admin only** (server-enforced), with a
  confirmation dialog.
- **KPIs** — four live stat cards (Total Releases, Delivered This Month, Awaiting
  Prod, Added This Week) via `GET /api/stats`.
- **Missing batches** — the "Missing batches" button (records-card header) opens a
  modal listing the gaps in the batch sequence between the lowest and highest
  uploaded batch (`GET /api/batch-gaps`, display capped at 5,000) with a full CSV
  download (`GET /export/batch-gaps`).
- **Collapsible sections** — both the "Add release record" form and the records/
  grid view collapse to declutter the page.
- **CSV templates** — one-click starter templates for Import and Bulk Update
  (`GET /template/import|bulk`) so uploads start in the right shape.
- **Audit trail** — per-row Created/Updated By/Date columns, plus every
  create/update/delete/import/export/bulk-update/gaps-export/config action recorded
  through the platform `audit` helper (visible in the Admin console audit log).
- **Notifications** — toast messages for add/update/delete/import/export/bulk and
  validation errors.

---

## Role permissions

| Capability | User | Team Lead | Admin |
|---|:--:|:--:|:--:|
| View / manual entry / import / export / bulk update / inline edit | ✅ | ✅ | ✅ |
| Database Configuration (create/modify project DBs) | ❌ | ✅ | ✅ |
| Delete records | ❌ | ✅ | ✅ |
| Set "Sent By" to another user | same team only | same team only | same team only |

---

## Setup

```bash
pip install -r requirements.txt          # adds openpyxl; DB drivers already pinned
```

Install the driver matching your target engine if it is not already present
(`psycopg[binary]`, `PyMySQL`, `oracledb`, or `pyodbc` for SQL Server). SQLite
needs no driver and is handy for local testing.

## Testing

The `rt_service`, `rt_db`, `rt_io` and `rt_secrets` modules are pure and
importable without a running server. A SQLite target exercises the full external-DB
path end-to-end (dynamic table creation, insert/expand, filter, bulk update,
grouping, export/import round-trip, soft delete) — see the smoke test in the PR
notes.
