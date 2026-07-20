# Delivery Toolbox

Self-hosted, single-login platform for internal delivery tools — modern, secure, and designed for teams that want focused operational utilities without the clutter.

Built as a single Flask application that hosts each tool as an isolated blueprint and ships a shared design system (see DESIGN.md and PRODUCT.md for the design intent and product strategy).

Key design goals

- One login, many safe tools: central auth, shared chrome, isolated tool code.
- Premium, usable UI: "The Glass Instrument" design system (Inter + JetBrains Mono, Iris indigo action color).
- Accessibility-aware: aiming for WCAG 2.1 AA.

Why this repo

Delivery Toolbox provides a lightweight way to bundle multiple internal tools behind a single sign-on and a common design system. It’s intended for internal teams and trusted external clients who need self-serve operational tooling (migrations, query helpers, release imports, CRM automations, etc.).

Notable live tools

- AutoBackupRevert — FK-safe Oracle rollback & backup script generator (abr)
- Encrypt/Decrypt utility (edu)
- SQL Query Generator (qgen)
- Release Tracker (rt)
- XPM Automator (xpm)
- Team Management (teams)
- Portal Admin / Admin Console (portal-admin, admin-console)

Stack

- Language(s): Python (Flask) with HTML/CSS templates
- Runtime: Flask 3.x, deployable under waitress
- Notable libraries: flask, flask-login, sqlalchemy (optional DB backends), apscheduler
- Minimum Python: 3.12+ (managed by uv)

Repository layout (top-level)

```
app.py                      # Application factory — wires every tool together
run.sh / run.bat            # Cross-platform launchers (uses uv + waitress)
pyproject.toml / uv.lock    # Dependencies + pinned Python, managed by uv
.env.example                # Configuration example (secrets, paths, branding)

login/                      # Authentication blueprint
database/                   # DB schema, migrations, and SQLite data (default)
landing-page/               # Platform launch hub ("landing")
auto-backup-revert-tool/    # AutoBackupRevert tool (abr)
team-management/            # teams blueprint and templates
encrypt-decrypt-tool/       # encrypt/decrypt utility (edu)
query-generator-tool/       # query generator (qgen)
release-tracker-tool/       # release tracker (rt)
xpm-automator-tool/        # XPM automations (xpm)
portal-admin/               # portal admin UI and settings
admin-console/              # operational admin console
shared/                     # cross-tool libraries, utilities, constants
static/                     # shared design-system assets
templates/                  # shared chrome (base.html, partials)
```

How it fits together

- app.py adds hyphenated tool folders to sys.path, initializes the DB and stores, registers blueprints (landing, auth, abr, edu, qgen, xpm, rt, teams, portal/admin).
- models/ (database/database-config) manages persistence and the portal tool registry used to build the dynamic landing page.
- Each tool lives in its own folder and exposes a Flask Blueprint. Landing page cards link to internal endpoints (or external tools) and the platform enforces access control.

Run it (quickstart)

This repo uses uv to manage the interpreter and pinned dependencies (uv.lock). The run scripts auto-install uv if missing and start the app under waitress.

From a fresh clone:

```bash
# On macOS / Linux
./run.sh

# On Windows
run.bat
```

Manual steps (when you prefer control):

```bash
# Install uv per https://docs.astral.sh/uv/getting-started/installation/
uv sync                         # create .venv/ + install locked deps (reads uv.lock)
uv run python app.py            # dev server (http://127.0.0.1:5000)
# production (waitress)
uv run python -m waitress --listen=0.0.0.0:5000 app:app
```

Notes

- The bootstrap admin account is created from ADMIN_USERNAME / ADMIN_PASSWORD in .env on first run — rotate these before exposing the app.
- The platform defaults to SQLite (database/data/app.db). Additional DB providers (Postgres, MySQL/Maria, Oracle, MongoDB) are optional and supported via SQLAlchemy drivers listed in pyproject.toml.
- Uploaded tool icons, brand logos, and avatars are stored under static/ and BRAND_DIR.

Configuration

Copy or seed .env from .env.example and set at minimum:

- ADMIN_USERNAME, ADMIN_PASSWORD, ADMIN_EMAIL
- SECRET_KEY
- UPLOAD_ROOT (optional) — default used if unset

Security

- Change default admin credentials immediately on first run.
- Enable organization policy settings (e.g., require_admin_2fa) via the Portal Admin UI where available.

Development notes

- Add a new tool:
  1. Create your-tool/source-code/yourtool_routes.py exposing a Flask Blueprint.
  2. Add templates/ for that tool and point template_folder accordingly.
  3. Add the tool's code directory to _CODE_DIRS in app.py and register the blueprint in create_app().
  4. Add a card to LANDING_TOOLS in landing-page/source-code/landing_routes.py with status="live" and endpoint set to your tool's home endpoint.

- app.py contains several request guards and context processors that implement platform behavior (pending approvals, forced password changes, admin 2FA). Review it when adding auth-sensitive features.

Where to read next

- DESIGN.md — the visual design system and tokens ("The Glass Instrument").
- PRODUCT.md — product strategy and audience.
- landing-page/source-code/landing_routes.py — add or edit the launch cards.
- auto-backup-revert-tool/documentation.md — tool-specific docs.

Contributing

Contributions welcome. Open issues for bugs and feature requests and file PRs against main. Keep changes small and document UI/brand changes in DESIGN.md where applicable.

License

Personal Dev Corporation Ltd · © 2026 (see LICENSE or repository header for details)
