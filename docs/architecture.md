# Architecture — Delivery Toolbox

## Overview

Delivery Toolbox is a **modular monolith**: a single Flask process that hosts
multiple independent tools as blueprints. This gives the operational simplicity
of one deployment (one port, one login, one DB, one design system) with the
code-organisation benefits of clear per-tool boundaries.

```
                         ┌──────────────────────────────┐
   Browser  ── HTTPS ──▶ │  nginx / IIS (reverse proxy) │
                         └───────────────┬──────────────┘
                                         │
                            ┌────────────┴───────────┐
                            │   waitress (WSGI)      │
                            │   app:app (factory)    │
                            └────────────┬───────────┘
                                         │ registers blueprints
       ┌──────────────┬─────────────────┼───────────────────┬─────────────┐
       ▼              ▼                 ▼                   ▼             ▼
   landing         auth               abr                tool3        (future)
 landing-page/   login/    auto-backup-revert-tool/    tool-3/
       │              │                 │
       └──────────────┴──────┬──────────┘
                             ▼
                 shared/  (constants, utilities, libs)
                             ▼
                 database/ (models.py → SQLite app.db)
```

## The application factory

[`app.py`](../app.py) is the single entry point. On import it:

1. **Resolves the project root** and inserts every tool's code directory into
   `sys.path`. The folders use hyphenated names (`auto-backup-revert-tool`,
   `landing-page`) which are **not** valid Python package names, so direct
   package imports are impossible. Putting each code dir on `sys.path` lets the
   modules import by plain name (`import core`, `from auth import auth_bp`).
   Module filenames are unique across folders to avoid collisions
   (`landing_routes.py`, `abr_routes.py`, `tool3_routes.py`).
2. Builds the `Flask` app with a **shared** `template_folder` (chrome:
   `base.html`, partials) and **shared** `static_folder` (the design system).
3. Initialises the DB, bootstraps the admin, wires Flask-Login.
4. Registers each blueprint (`auth`, `landing`, `abr`, `tool3`).
5. Installs a global context processor (branding/attribution) + 413 handler.
6. Starts background workers (upload cleanup thread + APScheduler).

## Blueprints & endpoints

Endpoints are blueprint-qualified (`abr.dashboard`, `landing.about`,
`auth.login`). Templates and redirects use these names via `url_for`.

| Blueprint | Code | Templates | Static |
|---|---|---|---|
| `landing` | landing-page/source-code/landing_routes.py | landing-page/templates | /static (shared) |
| `auth` | login/source-code/auth.py | login/templates | /static (shared) |
| `abr` | auto-backup-revert-tool/source-code/abr_routes.py | auto-backup-revert-tool/templates | /static (shared) |
| `tool3` | tool-3/source-code/tool3_routes.py | — | — |

## Template & static resolution

- **Shared chrome** (`base.html`, `_flash.html`, `_status_badge.html`) lives in
  `templates/` (the app's `template_folder`). Every tool template
  `{% extends "base.html" %}`; Jinja's loader searches the app folder plus each
  blueprint's `template_folder`. All template filenames are unique, so there are
  no collisions.
- **Shared assets** (`style.css`, `app.js`, `charts.js`, `logo.svg`,
  `favicon.svg`) live in `static/`, served at `/static`. This is the single
  design-system source referenced by every page (`url_for('static', ...)`).

## Data layer

A single **SQLite** database at `database/data/app.db`, accessed through
`database/database-config/models.py`. Schema is created from `models.SCHEMA`
(exported to `database/schema/schema.sql`); forward migrations live in
`models.ALTERS` (exported to `database/migrations/migrations.sql`) and run before
the schema on every boot. See [database/documentation.md](../database/documentation.md).

## Background work

`abr` owns two workers (started by the factory via `start_workers(app)`):
- **Upload cleanup thread** — prunes orphaned/expired per-job work dirs.
- **APScheduler** — polls admin-configured *watched sources* (local folder / Git
  repo) and runs the AutoBackupRevert pipeline on new files.

## Why a modular monolith (not microservices)?

- One process, one SQLite file, one TLS endpoint — trivial to operate on a small
  VM (the current Oracle Cloud Always-Free deployment).
- Tools share auth, design system, and DB without network hops.
- Each tool is still cleanly separable: if one outgrows the monolith, its
  blueprint folder lifts out into its own service with minimal coupling
  (it only depends on `shared/` and `database/`).
