# XPM Automator

An enterprise-grade rebuild of the standalone **XPM_Automation** desktop tool,
integrated as a first-class Delivery Toolbox tool (blueprint `xpm`, mounted at
`/tools/xpm-automator`).

It bulk-uploads SQL/TXT migration scripts to the **XPM CRM** (an ASP.NET
WebForms app), auto-generates an editable **Batch Number**, downloads the
consolidated script, and keeps a complete **Processing History** audit trail —
all inside the platform's auth, theming and navigation.

---

## What it does

| Capability | Detail |
|---|---|
| **Upload run** | Log in → switch project → upload each `.sql`/`.txt` script in natural order (`01_` before `10_`) → locate the just-uploaded scripts in the batch list and download **exactly that consolidated range**. |
| **Batch-range download** | Log in → switch project → download & merge an XPM batch range into one `.sql`. |
| **Batch explorer** | Live browse of every script currently in the project (Batch #, Script name, Scripted by/on), with client-side filtering. |
| **Live config discovery** | On the upload form, **Fetch from XPM** loads your real projects into a picker (scraped from `sdghome.aspx`); selecting one auto-fills its Project ID and auto-loads that project's processes (scraped from `cmbProcess`) — no hand-typed IDs. |
| **Batch Number** | App-generated (`XPM-YYYYMMDD-HHMMSS-XXX`) on upload, **editable**, and used as the default download filename. |
| **Live progress** | Background worker + polled status JSON drive a real-time progress bar, per-file status, and an activity timeline. |
| **Processing History** | Every run is one audited row: search, filter (date / batch / user / status), sort, paginate, export CSV, view detail. |
| **Dashboard** | Total uploads, successful/failed, today, active jobs, recent uploads, recent Batch Numbers, 14-day activity. |

---

## Architecture

```
xpm-automator-tool/
├── source-code/
│   ├── xpm_routes.py        Flask blueprint `xpm` (HTTP layer only)
│   ├── xpm_service.py       threaded run orchestration (worker driver)
│   ├── xpm_store.py         persistence: owns xpm_runs / xpm_run_files
│   └── xpm_core/            pure service layer — no Flask, no DB
│       ├── html_forms.py    stdlib ASP.NET field/VIEWSTATE parser + batch-table scraper
│       ├── config.py        XPMConfig value object + validation
│       ├── batch.py         Batch Number generate / validate / slug
│       ├── client.py        XPMClient — login/upload/scrape/consolidated/explorer
│       └── pipeline.py      in-memory progress Registry (thread-safe)
├── templates/
│   ├── xpm_macros.html      status-badge / batch-pill macros
│   ├── xpm_dashboard.html   dashboard
│   ├── xpm_upload.html      new run (upload / batch-download)
│   ├── xpm_run.html         live status (polls status.json)
│   ├── xpm_history.html     Processing History (search/filter/sort/paginate/CSV)
│   └── xpm_detail.html      full run detail (files, timeline, config)
└── documentation.md
```

**Layering (clean architecture):** `xpm_core` is a self-contained, Flask-free,
DB-free service layer (unit-testable in isolation). `xpm_store` is the only
persistence seam. `xpm_service` orchestrates a run on a daemon thread. `xpm_routes`
is a thin HTTP adapter. Separation of concerns, dependency direction always
points inward toward the pure core.

### Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/tools/xpm-automator/dashboard` | Dashboard |
| GET/POST | `/tools/xpm-automator/new` | Start an upload / batch-download run |
| GET | `/tools/xpm-automator/run/<id>` | Live status page |
| GET | `/tools/xpm-automator/run/<id>/status.json` | Live progress JSON (polled) |
| POST | `/tools/xpm-automator/run/<id>/batch-number` | Edit the Batch Number |
| POST | `/tools/xpm-automator/run/<id>/cancel` | Cooperative cancel |
| GET | `/tools/xpm-automator/download/<id>` | Download output (named by Batch Number) |
| GET | `/tools/xpm-automator/history` | Processing History |
| GET | `/tools/xpm-automator/history/export.csv` | CSV export of the current filter |
| GET | `/tools/xpm-automator/detail/<id>` | Full run detail |
| GET/POST | `/tools/xpm-automator/explorer` | Live Batch Explorer (browse project scripts) |
| POST | `/tools/xpm-automator/api/projects` | JSON: live list of XPM projects (id + name) |
| POST | `/tools/xpm-automator/api/processes` | JSON: live list of processes for a project |

---

## Data model (self-owned, zero platform-schema impact)

Two tables created idempotently via the shared `models.connect()` — **no change
to `models.py`**:

- **`xpm_runs`** — one row per run: batch numbers, mode, status, denormalised
  user, redacted config snapshot, counts, artefact paths, duration, download
  audit, error/remarks, and a persisted processing timeline (`log_json`).
- **`xpm_run_files`** — one row per uploaded file with per-file status/error.

Statuses: `uploaded → processing → {completed | failed | cancelled}`.

---

## Security

- The **XPM password is never persisted** — it lives only on the config object
  passed into the worker thread's closure; it is not written to the DB, the
  audit log, or the live progress snapshot.
- Only a redacted config (URL / user / project / process) is stored.
- Runs are owned by their creator; non-owners get 403 (admins may view all).
- Key actions (`xpm.upload_started`, `xpm.download`, `xpm.batch_number_edited`,
  …) are written to the platform enterprise audit log.

---

## Improvements over the standalone

1. Multi-user web tool inside the platform (auth, theming, nav) vs. a single-user desktop app.
2. **No BeautifulSoup dependency** — replaced with a stdlib `html.parser` extractor.
3. Value-object config + validation instead of module globals; typed `XPMError` instead of `sys.exit`.
4. Transport-level **retry with backoff** on transient network faults, plus per-file upload retry.
5. **Live** progress (bar, per-file status, streaming timeline) + cooperative **cancel**.
6. App-generated, **editable Batch Number** that drives download filenames.
7. Rich **Processing History**: search, multi-filter, sortable columns, pagination, CSV export, detail view.
8. **Dashboard** with metrics + 14-day activity chart (dependency-free).
9. Full **audit trail** in SQLite (per-run + per-file) replacing a flat CSV.
10. Clean, layered, testable architecture (pure core / store / service / routes).

---

## Operational notes

- XPM is reachable only on the **Noida office VPN**. A connection failure surfaces
  a clear message and the run is marked `failed` with no partial state committed.
- Uploaded files and produced scripts live under `uploads/xpm/<uuid>/`; they are
  swept by the platform's existing upload-cleanup loop (orphan sweep) like any
  other tool's work dir.
- Runtime dependency: `requests` (already vendored; pinned in `requirements.txt`).
