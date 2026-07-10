"""
Landing page / platform hub for the Delivery Toolbox.

Blueprint ``landing`` serves the public launchpad ("/") and the About page.
The tool grid is data-driven by ``LANDING_TOOLS`` — add a dict to surface a
new tool card. Set ``status="live"`` + an ``endpoint`` to make a card
clickable, or ``status="soon"`` for a dimmed "coming soon" placeholder.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from flask import Blueprint, abort, redirect, render_template, request, url_for
from flask_login import current_user

import constants
import models

log = logging.getLogger("landing")

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
        "name": "XPM Automator",
        "icon": "cloud_sync",
        "desc": ("Bulk-upload SQL/TXT migration scripts to the XPM CRM in order, auto-generate an "
                 "editable Batch Number, download the consolidated script, and keep a full "
                 "processing-history audit trail."),
        "tags": ["XPM", "Migration", "Upload"],
        "status": "live",
        "endpoint": "xpm.dashboard",
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


def _tool_view(row) -> dict:
    """Map a portal_tools DB row to the dict shape the landing template expects,
    resolving each launch type to a concrete href/target and clickability."""
    try:
        tags = json.loads(row["tags_json"] or "[]")
    except (TypeError, ValueError):
        tags = []
    try:
        cfg = json.loads(row["launch_config"] or "{}")
    except (TypeError, ValueError):
        cfg = {}

    lt = row["launch_type"]
    href, target = "#", None
    clickable = row["status"] == "live"

    # All live tools open through the central launcher (landing.launch) so every
    # open is recorded for usage analytics; the launcher then redirects to the
    # real target. We still validate the target here to decide *clickability*
    # (a broken internal endpoint stays non-clickable, never a dead link).
    if lt == "internal":
        endpoint = cfg.get("endpoint")
        if endpoint:
            try:
                url_for(endpoint)  # validate only
                href = url_for("landing.launch", slug=row["slug"])
            except Exception:
                href, clickable = "#", False
        else:
            clickable = False
    elif lt == "external_url":
        if cfg.get("url"):
            href = url_for("landing.launch", slug=row["slug"])
            target = "_blank"
        else:
            clickable = False
    else:  # folder_path | executable -> launcher
        try:
            href = url_for("landing.launch", slug=row["slug"])
        except Exception:
            href, clickable = "#", False

    return {
        "name": row["name"],
        "icon": row["icon"],
        "icon_type": row["icon_type"],
        "desc": row["description"] or "",
        "tags": tags,
        "status": row["status"],
        "href": href,
        "target": target,
        "clickable": clickable,
    }


@landing_bp.route("/")
def index():
    rows = models.list_accessible_tools(current_user)
    tools = [_tool_view(r) for r in rows]
    live_count = sum(1 for t in tools if t["status"] == "live")
    return render_template(
        "landing.html",
        platform_name=constants.PLATFORM_NAME,
        tools=tools,
        live_count=live_count,
        soon_count=len(tools) - live_count,
    )


@landing_bp.route("/launch/<slug>")
def launch(slug: str):
    """Central launcher. Internal/external tools resolve inline on their cards;
    this route handles folder_path / executable tools whose runtime process
    manager ships in a later phase, plus any internal tool linked by slug."""
    tool = models.get_portal_tool_by_slug(slug)
    if tool is None or not tool["is_enabled"]:
        abort(404)
    # Enforce the same visibility rules as the dashboard.
    if slug not in models.get_accessible_tool_slugs(current_user):
        abort(403)
    # Record the launch for usage analytics (best-effort — never blocks opening).
    try:
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        models.record_tool_launch(
            tool_id=tool["id"], tool_slug=slug,
            user_id=getattr(current_user, "id", None) if current_user.is_authenticated else None,
            team_id=getattr(current_user, "team_id", None) if current_user.is_authenticated else None,
            ip=ip,
        )
    except Exception:
        log.exception("Failed to record tool launch for %s", slug)
    try:
        cfg = json.loads(tool["launch_config"] or "{}")
    except (TypeError, ValueError):
        cfg = {}
    lt = tool["launch_type"]
    if lt == "internal" and cfg.get("endpoint"):
        return redirect(url_for(cfg["endpoint"]))
    if lt == "external_url" and cfg.get("url"):
        return redirect(cfg["url"])
    # folder_path / executable — managed process. If it's up and serves a URL,
    # send the user there; otherwise show a holding page with live status.
    import launcher
    st = launcher.status(tool["slug"])
    if st.get("running") and st.get("url"):
        return redirect(st["url"])
    return render_template("tool_pending.html", tool=tool, cfg=cfg, status=st)


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
