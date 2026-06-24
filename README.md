# Delivery Toolbox

A self-hosted **multi-tool developer platform**. One Flask process, one login,
one design system — and a growing set of independent tools, each living in its
own top-level folder. The landing page (`/`) is the launchpad; **AutoBackupRevert**
(an FK-safe Oracle rollback-script generator) is the first live tool.

```
Delivery-Tools/
│
├── app.py                      # Application factory — wires every tool together
├── run.bat                     # Windows launcher (waitress)
├── requirements.txt
├── .env / .env.example         # Configuration (secrets, paths, branding)
│
├── login/                      # Authentication tool (blueprint: auth)
│   ├── source-code/            # auth.py — login / register / forgot / reset
│   ├── authentication-config/  # login_manager.py — Flask-Login + User + loader
│   ├── API/                    # (reserved for a future auth API)
│   ├── templates/              # login / register / forgot / reset pages
│   └── documentation.md
│
├── database/                   # Persistence layer
│   ├── schema/schema.sql       # Canonical CREATE TABLE script
│   ├── migrations/             # ALTER ledger (forward-only)
│   ├── seed-data/              # Admin bootstrap notes
│   ├── database-config/        # models.py — the SQLite access layer
│   ├── data/app.db             # The live SQLite database
│   └── documentation.md
│
├── landing-page/               # Platform hub (blueprint: landing)
│   ├── source-code/            # landing_routes.py — "/" + "/about" + tools registry
│   ├── assets/                 # Brand assets (logo, favicon)
│   ├── styles/                 # Landing-specific CSS notes
│   ├── components/             # Section breakdown notes
│   ├── templates/              # landing.html, about.html
│   └── documentation.md
│
├── auto-backup-revert-tool/    # Tool #1 (blueprint: abr)
│   ├── source-code/            # abr_routes.py (routes), core.py, scheduler.py
│   ├── dependencies/           # connectors/ (local + git), email_utils.py
│   ├── configuration/          # Tool config notes
│   ├── scripts/                # Operational scripts
│   ├── templates/              # dashboard / upload / review / result / history / admin*
│   ├── samples/                # Sample migration SQL
│   └── documentation.md        # Full tool overview
│
├── tool-3/                     # Scaffold for the next tool (blueprint: tool3)
│   ├── source-code/            # tool3_routes.py (placeholder)
│   ├── dependencies/
│   ├── configuration/
│   └── documentation.md
│
├── shared/                     # Cross-tool code
│   ├── common-libraries/       # (shared libs — currently the DB + mail layers)
│   ├── utilities/              # decorators.py — admin_required, ...
│   └── constants/              # constants.py — config single source of truth
│
├── docs/                       # Platform docs
│   ├── architecture.md
│   ├── deployment-guide.md
│   └── user-guide.md
│
├── static/                     # Shared design-system assets (served at /static)
└── templates/                  # Shared chrome (base.html, partials)
```

## Run it

```bat
:: from Delivery-Tools/
run.bat
```

or manually:

```bat
python -m pip install -r requirements.txt
python app.py                 :: dev server (http://127.0.0.1:5000)
:: or, production:
python -m waitress --listen=0.0.0.0:5000 app:app
```

The bootstrap admin is created from `ADMIN_USERNAME` / `ADMIN_PASSWORD` in `.env`
on first run. **Rotate these before exposing the app.**

## How it fits together

Each tool is a **Flask blueprint**. The factory in [app.py](app.py) puts every
tool's code folder on `sys.path` (the folders use hyphens, so they can't be
Python packages) and registers the blueprints:

| Blueprint | Folder | URLs |
|---|---|---|
| `landing` | landing-page/ | `/`, `/about` |
| `auth` | login/ | `/login`, `/register`, `/forgot`, `/reset/<token>` |
| `abr` | auto-backup-revert-tool/ | `/dashboard`, `/new`, `/review`, `/result`, `/history`, `/admin*` |
| `tool3` | tool-3/ | `/tools/tool-3/` (scaffold) |

## Adding a new tool

1. Create `your-tool/source-code/yourtool_routes.py` exposing a `Blueprint`.
2. Give it a `templates/` folder and point `template_folder` at it.
3. Add its code dir(s) to `_CODE_DIRS` and register the blueprint in [app.py](app.py).
4. Add a card to `LANDING_TOOLS` in
   [landing-page/source-code/landing_routes.py](landing-page/source-code/landing_routes.py)
   (`status="live"` + `endpoint="yourtool.home"`).

That's it — the landing hub, shared chrome, login, and design system come for free.

---
*Personal Dev Corporation Ltd · © 2026*
