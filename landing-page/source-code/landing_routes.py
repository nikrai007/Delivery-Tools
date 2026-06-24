"""
Landing page / platform hub for the Delivery Toolbox.

Blueprint ``landing`` serves the public launchpad ("/") and the About page.
The tool grid is data-driven by ``LANDING_TOOLS`` — add a dict to surface a
new tool card. Set ``status="live"`` + an ``endpoint`` to make a card
clickable, or ``status="soon"`` for a dimmed "coming soon" placeholder.
"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, render_template

import constants

_HERE = Path(__file__).resolve().parents[1]  # landing-page/

landing_bp = Blueprint(
    "landing", __name__,
    template_folder=str(_HERE / "templates"),
)


LANDING_TOOLS = [
    {
        "name": "AutoBackupRevert",
        "icon": "settings_backup_restore",
        "desc": ("FK-safe Oracle rollback-script generator. Upload a migration bundle and get "
                 "matched BACKUP / REVERT / CLEANUP scripts with a full audit trail."),
        "tags": ["Oracle", "SQL", "Scheduler"],
        "status": "live",
        "endpoint": "abr.dashboard",
    },
    {
        "name": "Encrypt Decrypt Utility",
        "icon": "lock",
        "desc": ("AES-256-CBC encrypt / decrypt of strings and encrypted nonces, with URL-safe "
                 "tokens — byte-for-byte interoperable with the existing C# tool."),
        "tags": ["AES-256", "CBC", "Nonce"],
        "status": "live",
        "endpoint": "edu.index",
    },
    {
        "name": "Query Generator",
        "icon": "database",
        "desc": ("Turn a large raw payload into a Standard SQL UPDATE and a chunked Oracle PL/SQL "
                 "NCLOB rebuild — byte-for-byte to spec."),
        "tags": ["Oracle", "PL/SQL", "NCLOB"],
        "status": "live",
        "endpoint": "qgen.index",
    },
    {
        "name": "FlowEngine Linter",
        "icon": "rule",
        "desc": "Static analysis for migration ordering and FK-chain safety before a release ships.",
        "tags": ["Linter", "FK-safe"],
        "status": "soon",
    },
    {
        "name": "Release Notifier",
        "icon": "notifications_active",
        "desc": "Slack & Teams webhooks for pipeline and scheduler events, with per-channel routing.",
        "tags": ["Slack", "Teams", "Webhook"],
        "status": "soon",
    },
    {
        "name": "Audit Exporter",
        "icon": "table_view",
        "desc": "One-click SOX / SOC2 compliance CSV exports across all jobs and download logs.",
        "tags": ["SOX", "SOC2", "CSV"],
        "status": "soon",
    },
    {
        "name": "Schema Diff",
        "icon": "difference",
        "desc": "Visual diff between two Oracle schemas — detect drift across DEV, UAT and PROD.",
        "tags": ["DEV", "UAT", "PROD"],
        "status": "soon",
    },
]


@landing_bp.route("/")
def index():
    live_count = sum(1 for t in LANDING_TOOLS if t["status"] == "live")
    return render_template(
        "landing.html",
        platform_name=constants.PLATFORM_NAME,
        tools=LANDING_TOOLS,
        live_count=live_count,
        soon_count=len(LANDING_TOOLS) - live_count,
    )


@landing_bp.route("/about")
def about():
    return render_template(
        "about.html",
        app_name=constants.APP_NAME,
        app_owner=constants.APP_OWNER,
        app_company=constants.APP_COMPANY,
        version=constants.VERSION,
        db_path=str(constants.DB_PATH),
        upload_root=str(constants.UPLOAD_ROOT),
    )
