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
    "team-management/source-code",
]
for _rel in _CODE_DIRS:
    p = str((ROOT / _rel).resolve())
    if p not in sys.path:
        sys.path.insert(0, p)

from flask import Flask, current_app, flash, redirect, request, url_for  # noqa: E402
from flask_login import current_user  # noqa: E402

import constants  # noqa: E402
import models  # noqa: E402
from login_manager import login_manager  # noqa: E402
from auth import auth_bp  # noqa: E402
from landing_routes import landing_bp  # noqa: E402
from abr_routes import abr, start_workers  # noqa: E402
from edu_routes import edu_bp  # noqa: E402
from qgen_routes import qgen_bp  # noqa: E402
from tool3_routes import tool3_bp  # noqa: E402
from team_routes import teams_bp  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("delivery-toolbox")


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

    # Runtime dirs + DB bootstrap.
    constants.UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    constants.DATA_DIR.mkdir(parents=True, exist_ok=True)
    constants.BRAND_DIR.mkdir(parents=True, exist_ok=True)
    models.init_db(constants.DB_PATH)
    models.ensure_admin(constants.ADMIN_USERNAME, constants.ADMIN_EMAIL, constants.ADMIN_PASSWORD)

    # Auth + blueprints.
    login_manager.init_app(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(landing_bp)
    app.register_blueprint(abr)
    app.register_blueprint(edu_bp)
    app.register_blueprint(qgen_bp)
    app.register_blueprint(tool3_bp)
    app.register_blueprint(teams_bp)

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

    # Globals available in every template.
    @app.context_processor
    def inject_globals():
        year = datetime.now().year
        notif_count = 0
        notifications = []
        pending_team_requests = 0
        is_pending = False
        if current_user.is_authenticated:
            notif_count    = models.count_unread_notifications(current_user.id)
            notifications  = models.list_notifications(current_user.id, limit=15)
            is_pending     = getattr(current_user, "is_pending_approval", False)
            if getattr(current_user, "is_admin", False):
                pending_team_requests = models.count_pending_join_requests()
            elif getattr(current_user, "is_team_leader", False) and current_user.team_id:
                pending_team_requests = len(models.list_join_requests_for_team(current_user.team_id))
        # Centralized logo: use uploaded brand logo if set and file exists, else default.
        custom_logo = models.setting_get("brand.logo_filename") or ""
        if custom_logo and (constants.BRAND_DIR / custom_logo).exists():
            logo_url = url_for("static", filename=f"brand/{custom_logo}")
        else:
            logo_url = url_for("static", filename="logo.svg")
        return {
            "platform_name":         constants.PLATFORM_NAME,
            "app_name":              constants.APP_NAME,
            "current_year":          year,
            "attribution":           f"{constants.APP_OWNER} · {constants.APP_COMPANY} · © {year}",
            "is_admin":              getattr(current_user, "is_admin", False) if current_user.is_authenticated else False,
            "is_team_leader":        getattr(current_user, "is_team_leader", False) if current_user.is_authenticated else False,
            "is_pending":            is_pending,
            "notif_count":           notif_count,
            "notifications":         notifications,
            "pending_team_requests": pending_team_requests,
            "logo_url":              logo_url,
        }

    @app.errorhandler(413)
    def too_large(_e):
        flash(f"Upload exceeded {constants.MAX_UPLOAD_MB} MB limit.", "error")
        return redirect(url_for("abr.new_job")), 413

    # Background workers (upload cleanup + APScheduler).
    start_workers(app)

    log.info("Delivery Toolbox ready — %d blueprint(s) registered.", len(app.blueprints))
    return app


app = create_app()


if __name__ == "__main__":
    app.run(host=os.getenv("HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", "5000")), debug=True)
