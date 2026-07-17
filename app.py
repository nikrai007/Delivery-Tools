"""
Delivery Toolbox — application entry point (Flask application factory).

A single Flask process that hosts every tool as an isolated blueprint:

    landing  -> landing-page/source-code/landing_routes.py   ("/", "/about")
    auth     -> login/source-code/auth.py                     (login/register/reset)
    abr      -> auto-backup-revert-tool/source-code/abr_routes.py  (the tool)
    tool3    -> tool-3/source-code/tool3_routes.py            (scaffold)

Each tool's code lives in its own top-level folder. Because those folders use
hyphenated names (not importable as Python packages), the factory puts each
tool's code directory on sys.path so its modules import by plain name
(`import core`, `from auth import ...`, etc.).

Run (dev):   python app.py
Run (prod):  python -m waitress --listen=0.0.0.0:5000 app:app
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# --- Make the hyphenated tool folders importable ------------------------
# Order matters only for readability; module names are unique across folders.
_CODE_DIRS = [
    "shared/constants",
    "shared/utilities",
    "shared/common-libraries",
    "database/database-config",
    "login/authentication-config",
    "login/source-code",
    "landing-page/source-code",
    "auto-backup-revert-tool/dependencies",
    "auto-backup-revert-tool/source-code",
    "encrypt-decrypt-tool/source-code",
    "query-generator-tool/source-code",
    "tool-3/source-code",
    "xpm-automator-tool/source-code",
    "release-tracker-tool/source-code",
    "team-management/source-code",
    "portal-admin/source-code",
    "admin-console/source-code",
]
for _rel in _CODE_DIRS:
    p = str((ROOT / _rel).resolve())
    if p not in sys.path:
        sys.path.insert(0, p)

import json  # noqa: E402

from flask import Flask, current_app, flash, redirect, request, url_for  # noqa: E402
from flask_login import current_user  # noqa: E402

import constants  # noqa: E402
import launcher  # noqa: E402
import models  # noqa: E402
import screen_content  # noqa: E402  (admin-editable on-screen copy)
from login_manager import login_manager  # noqa: E402
from auth import auth_bp  # noqa: E402
from landing_routes import landing_bp, LANDING_TOOLS  # noqa: E402
from abr_routes import abr, start_workers  # noqa: E402
from edu_routes import edu_bp  # noqa: E402
from qgen_routes import qgen_bp  # noqa: E402
from tool3_routes import tool3_bp  # noqa: E402
from xpm_routes import xpm_bp  # noqa: E402
import xpm_store  # noqa: E402
from rt_routes import rt_bp  # noqa: E402
import rt_store  # noqa: E402
from team_routes import teams_bp  # noqa: E402
from portal_admin_routes import portal_admin_bp  # noqa: E402
from admin_console_routes import admin_bp  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("delivery-toolbox")


def _tool_nav_entry(row) -> dict | None:
    """Build a sidebar nav entry {name, icon, icon_type, href, target, slug} for a
    portal_tools row, resolving its launch target to a URL. Returns None if the
    tool cannot produce a working link (e.g. an internal endpoint that no longer
    exists), so a broken tool never renders a dead sidebar item."""
    try:
        cfg = json.loads(row["launch_config"] or "{}")
    except (TypeError, ValueError):
        cfg = {}
    lt = row["launch_type"]
    href, target = None, None
    if lt == "internal":
        endpoint = cfg.get("endpoint")
        if endpoint:
            try:
                href = url_for(endpoint)
            except Exception:
                return None
    elif lt == "external_url":
        href = cfg.get("url")
        target = "_blank"
    else:  # folder_path | executable — routed through the launcher stub
        try:
            href = url_for("landing.launch", slug=row["slug"])
        except Exception:
            return None
    if not href:
        return None
    return {
        "slug": row["slug"], "name": row["name"], "icon": row["icon"],
        "icon_type": row["icon_type"], "href": href, "target": target,
    }


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(ROOT / "templates"),   # shared chrome: base.html, _flash, _status_badge
        static_folder=str(ROOT / "static"),         # shared design-system assets
        static_url_path="/static",
    )
    app.config["SECRET_KEY"]         = constants.SECRET_KEY
    app.config["MAX_CONTENT_LENGTH"] = constants.MAX_UPLOAD_MB * 1024 * 1024
    app.config["UPLOAD_ROOT"]        = str(constants.UPLOAD_ROOT)
    # Always re-read templates from disk when they change, independent of debug.
    # (Debug is off by default for security; without this a template edit would
    # not appear until a full process restart.)
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    # Persist all logs (incl. unhandled-exception tracebacks behind the generic
    # 500 page) to Delivery-Tools/logs/app.log so production errors are
    # diagnosable without debug mode. `type logs\app.log` on the server.
    from logging.handlers import RotatingFileHandler
    logs_dir = ROOT / "logs"
    logs_dir.mkdir(exist_ok=True)
    fh = RotatingFileHandler(logs_dir / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    app.logger.addHandler(fh)
    logging.getLogger().addHandler(fh)  # capture werkzeug + library tracebacks too

    constants.UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    constants.DATA_DIR.mkdir(parents=True, exist_ok=True)
    constants.BRAND_DIR.mkdir(parents=True, exist_ok=True)
    launcher.init(ROOT / "logs" / "tools")   # managed-tool process logs
    models.init_db(constants.DB_PATH)
    models.ensure_admin(constants.ADMIN_USERNAME, constants.ADMIN_EMAIL, constants.ADMIN_PASSWORD)
    # First-run seed of the dynamic tool registry from the legacy hardcoded list.
    # Idempotent — a no-op once the admin has managed tools in the DB.
    models.seed_portal_tools(LANDING_TOOLS)
    # XPM Automator owns its own tables + registers its portal card (idempotent,
    # additive — no change to the platform schema or existing tools).
    xpm_store.init_store()
    # Release Tracker owns its own registry table (rt_projects) + registers its
    # portal card (idempotent, additive — no change to the platform schema).
    rt_store.init_store()
    # Uploaded tool icons + user avatars live alongside the brand assets under static/.
    (constants.ROOT / "static" / "tool-icons").mkdir(parents=True, exist_ok=True)
    constants.AVATAR_DIR.mkdir(parents=True, exist_ok=True)

    @app.template_filter("fromjson")
    def _fromjson(s):
        """Parse a JSON string in templates (used for tags_json / launch_config)."""
        try:
            return json.loads(s or "[]")
        except (TypeError, ValueError):
            return []

    # Admin-editable on-screen copy: {{ content('screen', 'field', 'default') }}
    # resolves an admin override (settings) or falls back to the default.
    app.jinja_env.globals["content"] = screen_content.get

    login_manager.init_app(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(landing_bp)
    app.register_blueprint(abr)
    app.register_blueprint(edu_bp)
    app.register_blueprint(qgen_bp)
    app.register_blueprint(tool3_bp)
    app.register_blueprint(xpm_bp)
    app.register_blueprint(rt_bp)
    app.register_blueprint(teams_bp)
    app.register_blueprint(portal_admin_bp)
    app.register_blueprint(admin_bp)

    # Pending-approval guard: redirect pending users away from all pages
    # except logout, the pending page itself, auth pages, and static assets.
    _PENDING_EXEMPT = {
        "auth.logout", "auth.login", "auth.register", "auth.forgot", "auth.reset",
        "teams.pending_approval", "static",
    }

    @app.before_request
    def check_pending_approval():
        if not current_user.is_authenticated:
            return None
        if current_user.is_admin:
            return None
        if getattr(current_user, "is_pending_approval", False):
            if request.endpoint not in _PENDING_EXEMPT:
                return redirect(url_for("teams.pending_approval"))
        return None

    # Force a password change (admin-created accounts / admin resets) before the
    # user can reach any other page. Exempts the change page itself + auth/static.
    _CHANGE_PW_EXEMPT = {
        "auth.change_password", "auth.logout", "auth.two_factor", "static",
    }

    @app.before_request
    def force_password_change():
        if not current_user.is_authenticated:
            return None
        if getattr(current_user, "must_change_password", False):
            if request.endpoint not in _CHANGE_PW_EXEMPT:
                return redirect(url_for("auth.change_password"))
        return None

    # Optional org policy: admins must enrol in 2FA before using the platform.
    _2FA_EXEMPT = {
        "auth.security_settings", "auth.enable_2fa", "auth.disable_2fa",
        "auth.change_password", "auth.logout", "auth.two_factor", "static",
    }

    @app.before_request
    def enforce_admin_2fa():
        if not current_user.is_authenticated or not getattr(current_user, "is_admin", False):
            return None
        if getattr(current_user, "totp_enabled", False):
            return None
        if models.setting_get("security.require_admin_2fa") == "1":
            if request.endpoint not in _2FA_EXEMPT:
                flash("Your organization requires administrators to enable two-factor authentication.", "warning")
                return redirect(url_for("auth.security_settings"))
        return None

    @app.context_processor
    def inject_globals():
        year = datetime.now().year
        notif_count = 0
        notifications = []
        pending_team_requests = 0
        is_pending = False
        accessible_tool_slugs: set = set()
        nav_tools: list = []
        if current_user.is_authenticated:
            notif_count    = models.count_unread_notifications(current_user.id)
            notifications  = models.list_notifications(current_user.id, limit=15)
            is_pending     = getattr(current_user, "is_pending_approval", False)
            if getattr(current_user, "is_admin", False):
                pending_team_requests = models.count_pending_join_requests()
            elif getattr(current_user, "is_team_leader", False) and current_user.team_id:
                pending_team_requests = len(models.list_join_requests_for_team(current_user.team_id))
            # Data-driven navigation: only the tools this user may open.
            if not is_pending:
                try:
                    for t in models.list_accessible_tools(current_user):
                        accessible_tool_slugs.add(t["slug"])
                        nav = _tool_nav_entry(t)
                        if nav is not None and t["status"] == "live":
                            nav_tools.append(nav)
                except Exception:                       # never let nav break a page
                    log.exception("Failed to build dynamic tool navigation")
        custom_logo = models.setting_get("brand.logo_filename") or ""
        if custom_logo and (constants.BRAND_DIR / custom_logo).exists():
            logo_url = url_for("static", filename=f"brand/{custom_logo}")
            # The uploaded brand logo doubles as the browser-tab favicon. A
            # cache-busting ?v=<mtime> forces browsers to fetch the new icon
            # instead of the aggressively-cached old one after a logo change.
            try:
                _v = int((constants.BRAND_DIR / custom_logo).stat().st_mtime)
            except OSError:
                _v = 0
            favicon_url = f"{logo_url}?v={_v}"
        else:
            logo_url = url_for("static", filename="logo.svg")
            favicon_url = url_for("static", filename="favicon.svg")
        # Current user's uploaded avatar (falls back to an initial in templates).
        avatar_url = None
        if current_user.is_authenticated:
            af = getattr(current_user, "avatar_filename", None)
            if af and (constants.AVATAR_DIR / af).exists():
                avatar_url = url_for("static", filename=f"avatars/{af}")
        try:
            session_timeout_minutes = int(models.setting_get("security.session_timeout_minutes") or 5)
        except (TypeError, ValueError):
            session_timeout_minutes = 5
        return {
            "platform_name":         constants.PLATFORM_NAME,
            "platform_tagline":      constants.PLATFORM_TAGLINE,
            "app_name":              constants.APP_NAME,
            "current_year":          year,
            "session_timeout_minutes": session_timeout_minutes,
            "attribution":           f"{constants.APP_OWNER} · {constants.APP_COMPANY} · © {year}",
            "is_admin":              getattr(current_user, "is_admin", False) if current_user.is_authenticated else False,
            "is_team_leader":        getattr(current_user, "is_team_leader", False) if current_user.is_authenticated else False,
            "is_pending":            is_pending,
            "notif_count":           notif_count,
            "notifications":         notifications,
            "pending_team_requests": pending_team_requests,
            "logo_url":              logo_url,
            "favicon_url":           favicon_url,
            "avatar_url":            avatar_url,
            "accessible_tool_slugs": accessible_tool_slugs,
            "nav_tools":             nav_tools,
        }

    @app.errorhandler(413)
    def too_large(_e):
        flash(f"Upload exceeded {constants.MAX_UPLOAD_MB} MB limit.", "error")
        return redirect(url_for("abr.new_job")), 413

    start_workers(app)

    log.info("Delivery Toolbox ready — %d blueprint(s) registered.", len(app.blueprints))
    return app


app = create_app()


if __name__ == "__main__":
    # Dev server only — production runs under waitress, which never executes this
    # block, so the Werkzeug debugger is never exposed in production. Debug (auto
    # code-reload + debugger) is ON by default for local development on localhost;
    # set FLASK_DEBUG=0 to run the dev server hardened. Never bind HOST=0.0.0.0
    # with debug on. Templates hot-reload regardless (TEMPLATES_AUTO_RELOAD).
    app.run(host=os.getenv("HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", "5000")),
            debug=os.getenv("FLASK_DEBUG", "1") == "1")
