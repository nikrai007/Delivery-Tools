"""
Tool Portal administration — Flask blueprint ``portal_admin``.

Admin-only CRUD for the dynamic tool registry that powers the dashboard's
tool cards and the sidebar navigation. Nothing here is hardcoded: adding a
row makes a tool appear; toggling ``is_enabled`` hides it; ``tool_access``
rows gate a tool to specific teams.

Launch types supported:
  internal      — an existing Flask blueprint endpoint (url_for)
  external_url  — any URL, opened in a new tab
  folder_path   — a Python app folder (Phase 2 runner; registered now)
  executable    — an executable path (Phase 2 runner; registered now)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user
from werkzeug.utils import secure_filename

import audit
import launcher
import models
from decorators import admin_required

log = logging.getLogger("portal-admin")

portal_admin_bp = Blueprint(
    "portal_admin", __name__,
    url_prefix="/portal-admin",
    template_folder=str(Path(__file__).resolve().parents[1] / "templates"),
)

LAUNCH_TYPES = ("internal", "external_url", "folder_path", "executable")
STATUSES = ("live", "soon")
ALLOWED_ICON_EXT = {".png", ".svg", ".jpg", ".jpeg", ".webp", ".gif"}


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _internal_endpoints() -> list[str]:
    """GET endpoints that take no URL args — the valid targets for an
    'internal' tool. Offered to the admin as an autocomplete datalist."""
    out = set()
    for rule in current_app.url_map.iter_rules():
        if rule.arguments:
            continue
        if rule.endpoint == "static" or rule.endpoint.startswith("portal_admin."):
            continue
        methods = rule.methods or set()
        if "GET" not in methods:
            continue
        out.add(rule.endpoint)
    return sorted(out)


def _parse_tags(raw: str) -> list[str]:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def _build_launch_config(launch_type: str, form) -> dict:
    if launch_type == "internal":
        return {"endpoint": (form.get("config_endpoint") or "").strip()}
    if launch_type == "external_url":
        return {"url": (form.get("config_url") or "").strip()}
    if launch_type == "folder_path":
        cfg: dict = {"path": (form.get("config_path") or "").strip()}
        port = (form.get("config_port") or "").strip()
        if port.isdigit():
            cfg["port"] = int(port)
        entry = (form.get("config_entry") or "").strip()
        if entry:
            cfg["entry"] = entry
        return cfg
    if launch_type == "executable":
        return {"cmd": (form.get("config_cmd") or "").strip()}
    return {}


def _save_icon(file_storage) -> str | None:
    """Persist an uploaded icon under static/tool-icons and return its filename."""
    if not file_storage or not file_storage.filename:
        return None
    ext = Path(file_storage.filename).suffix.lower()
    if ext not in ALLOWED_ICON_EXT:
        raise ValueError(f"Unsupported icon type '{ext}'. Allowed: {', '.join(sorted(ALLOWED_ICON_EXT))}")
    icon_dir = Path(current_app.static_folder) / "tool-icons"
    icon_dir.mkdir(parents=True, exist_ok=True)
    safe = secure_filename(file_storage.filename) or ("icon" + ext)
    # Prefix to avoid collisions between tools that upload the same filename.
    fname = f"{models._now().replace(':', '').replace('-', '').replace('.', '')[:15]}_{safe}"
    file_storage.save(str(icon_dir / fname))
    return fname


def _validate(*, name: str, launch_type: str, status: str, cfg: dict) -> list[str]:
    errors = []
    if not name:
        errors.append("Tool name is required.")
    if launch_type not in LAUNCH_TYPES:
        errors.append("Invalid launch type.")
    if status not in STATUSES:
        errors.append("Invalid status.")
    if status == "live":
        if launch_type == "internal" and not cfg.get("endpoint"):
            errors.append("An internal tool needs a target endpoint.")
        if launch_type == "external_url" and not cfg.get("url"):
            errors.append("An external-URL tool needs a URL.")
        if launch_type == "folder_path" and not cfg.get("path"):
            errors.append("A Python-app tool needs a folder path.")
        if launch_type == "executable" and not cfg.get("cmd"):
            errors.append("An executable tool needs a command/path.")
    return errors


def _form_ctx(tool=None):
    """Shared template context for the create/edit form."""
    cfg = {}
    if tool is not None:
        try:
            cfg = json.loads(tool["launch_config"] or "{}")
        except (TypeError, ValueError):
            cfg = {}
    return {
        "tool": tool,
        "cfg": cfg,
        "launch_types": LAUNCH_TYPES,
        "statuses": STATUSES,
        "internal_endpoints": _internal_endpoints(),
    }


# ----------------------------------------------------------------------
# List
# ----------------------------------------------------------------------
@portal_admin_bp.route("/tools")
@admin_required
def tools():
    all_tools = models.list_portal_tools()
    teams = models.list_teams()
    team_names = {t["id"]: t["name"] for t in teams}
    # Precompute a readable access summary per tool for the table.
    access_summary = {}
    for t in all_tools:
        if not t["requires_team"]:
            access_summary[t["id"]] = "Everyone"
            continue
        grants = models.list_tool_access_team_ids(t["id"])
        if not grants:
            access_summary[t["id"]] = "No teams"
        elif "all" in grants:
            access_summary[t["id"]] = "All teams"
        else:
            names = [team_names.get(g, f"#{g}") for g in grants]
            access_summary[t["id"]] = ", ".join(names)
    return render_template("admin_tools.html", tools=all_tools,
                           access_summary=access_summary)


# ----------------------------------------------------------------------
# Create
# ----------------------------------------------------------------------
@portal_admin_bp.route("/tools/new", methods=["GET", "POST"])
@admin_required
def tool_new():
    if request.method == "POST":
        name        = (request.form.get("name") or "").strip()
        slug_in     = (request.form.get("slug") or "").strip()
        description = (request.form.get("description") or "").strip() or None
        status      = (request.form.get("status") or "live").strip()
        launch_type = (request.form.get("launch_type") or "internal").strip()
        tags        = _parse_tags(request.form.get("tags", ""))
        requires_team = bool(request.form.get("requires_team"))
        order_raw   = (request.form.get("display_order") or "").strip()
        cfg         = _build_launch_config(launch_type, request.form)

        icon_type = (request.form.get("icon_type") or "symbol").strip()
        icon = (request.form.get("icon") or "apps").strip() or "apps"

        errors = _validate(name=name, launch_type=launch_type, status=status, cfg=cfg)
        slug = models.slugify(slug_in) if slug_in else models.slugify(name)
        if models.get_portal_tool_by_slug(slug):
            errors.append(f"Slug '{slug}' is already in use — choose another.")

        if icon_type == "image":
            try:
                saved = _save_icon(request.files.get("icon_file"))
                if saved:
                    icon = saved
                elif not (request.form.get("icon") or "").strip():
                    errors.append("Choose an image file, or switch to a Material icon name.")
            except ValueError as e:
                errors.append(str(e))

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("admin_tool_form.html", **_form_ctx(None))

        display_order = int(order_raw) if order_raw.lstrip("-").isdigit() else models.next_portal_tool_order()
        new_tool_id = models.create_portal_tool(
            slug=slug, name=name, description=description,
            icon=icon, icon_type=icon_type, tags=tags, status=status,
            launch_type=launch_type, launch_config=cfg,
            display_order=display_order, requires_team=requires_team,
            created_by=current_user.id,
        )
        audit.record("tool.created", category=audit.CAT_TOOL,
                     target_type="tool", target_id=new_tool_id, target_label=name,
                     new_value={"slug": slug, "launch_type": launch_type, "status": status,
                                "requires_team": requires_team})
        flash(f"Tool '{name}' created.", "success")
        return redirect(url_for("portal_admin.tools"))

    return render_template("admin_tool_form.html", **_form_ctx(None))


# ----------------------------------------------------------------------
# Edit
# ----------------------------------------------------------------------
@portal_admin_bp.route("/tools/<int:tool_id>/edit", methods=["GET", "POST"])
@admin_required
def tool_edit(tool_id: int):
    tool = models.get_portal_tool(tool_id)
    if tool is None:
        abort(404)

    if request.method == "POST":
        name        = (request.form.get("name") or "").strip()
        description = (request.form.get("description") or "").strip() or None
        status      = (request.form.get("status") or "live").strip()
        launch_type = (request.form.get("launch_type") or "internal").strip()
        tags        = _parse_tags(request.form.get("tags", ""))
        requires_team = bool(request.form.get("requires_team"))
        order_raw   = (request.form.get("display_order") or "").strip()
        cfg         = _build_launch_config(launch_type, request.form)

        icon_type = (request.form.get("icon_type") or "symbol").strip()
        icon = (request.form.get("icon") or tool["icon"]).strip() or "apps"

        errors = _validate(name=name, launch_type=launch_type, status=status, cfg=cfg)

        if icon_type == "image":
            try:
                saved = _save_icon(request.files.get("icon_file"))
                if saved:
                    icon = saved
                elif tool["icon_type"] != "image":
                    errors.append("Choose an image file, or switch to a Material icon name.")
                else:
                    icon = tool["icon"]  # keep the existing uploaded image
            except ValueError as e:
                errors.append(str(e))

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("admin_tool_form.html", **_form_ctx(tool))

        display_order = int(order_raw) if order_raw.lstrip("-").isdigit() else tool["display_order"]
        models.update_portal_tool(
            tool_id, name=name, description=description,
            icon=icon, icon_type=icon_type, tags=tags, status=status,
            launch_type=launch_type, launch_config=cfg,
            display_order=display_order, requires_team=requires_team,
        )
        audit.record("tool.updated", category=audit.CAT_TOOL,
                     target_type="tool", target_id=tool_id, target_label=name,
                     old_value={"name": tool["name"], "status": tool["status"],
                                "launch_type": tool["launch_type"],
                                "requires_team": bool(tool["requires_team"])},
                     new_value={"name": name, "status": status, "launch_type": launch_type,
                                "requires_team": requires_team})
        flash(f"Tool '{name}' updated.", "success")
        return redirect(url_for("portal_admin.tools"))

    return render_template("admin_tool_form.html", **_form_ctx(tool))


# ----------------------------------------------------------------------
# Reorder (drag-and-drop) — persists display_order from a dropped id list
# ----------------------------------------------------------------------
@portal_admin_bp.route("/tools/reorder", methods=["POST"])
@admin_required
def tools_reorder():
    data = request.get_json(silent=True) or {}
    ids = data.get("order") or request.form.getlist("order[]")
    try:
        ordered = [int(x) for x in ids]
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Invalid order payload."), 400
    if not ordered:
        return jsonify(ok=False, error="Empty order."), 400
    models.set_tool_order(ordered)
    audit.record("tool.reordered", category=audit.CAT_TOOL,
                 target_type="tool", target_label="Tool display order",
                 new_value={"order": ordered})
    return jsonify(ok=True)


# ----------------------------------------------------------------------
# Runtime — managed processes for folder_path / executable tools (#4)
# ----------------------------------------------------------------------
_PROCESS_TYPES = ("folder_path", "executable")


@portal_admin_bp.route("/runtime")
@admin_required
def runtime():
    tools = [t for t in models.list_portal_tools() if t["launch_type"] in _PROCESS_TYPES]
    rows = [{"tool": t, "status": launcher.status(t["slug"])} for t in tools]
    return render_template("admin_runtime.html", rows=rows)


@portal_admin_bp.route("/runtime/status.json")
@admin_required
def runtime_status():
    tools = [t for t in models.list_portal_tools() if t["launch_type"] in _PROCESS_TYPES]
    return jsonify(statuses={t["slug"]: launcher.status(t["slug"]) for t in tools})


@portal_admin_bp.route("/tools/<int:tool_id>/process/<action>", methods=["POST"])
@admin_required
def tool_process(tool_id: int, action: str):
    tool = models.get_portal_tool(tool_id)
    if tool is None:
        abort(404)
    if tool["launch_type"] not in _PROCESS_TYPES:
        flash("This tool type does not run as a managed process.", "error")
        return redirect(url_for("portal_admin.runtime"))
    try:
        if action == "start":
            st = launcher.start(tool)
            audit.record("tool.process_started", category=audit.CAT_TOOL,
                         target_type="tool", target_id=tool_id, target_label=tool["name"],
                         details={"pid": st.get("pid"), "url": st.get("url")})
            flash(f"Started '{tool['name']}'.", "success")
        elif action == "stop":
            launcher.stop(tool["slug"])
            audit.record("tool.process_stopped", category=audit.CAT_TOOL,
                         target_type="tool", target_id=tool_id, target_label=tool["name"])
            flash(f"Stopped '{tool['name']}'.", "info")
        elif action == "restart":
            launcher.restart(tool)
            audit.record("tool.process_restarted", category=audit.CAT_TOOL,
                         target_type="tool", target_id=tool_id, target_label=tool["name"])
            flash(f"Restarted '{tool['name']}'.", "success")
        else:
            abort(404)
    except launcher.LaunchError as exc:
        flash(f"Could not {action} '{tool['name']}': {exc}", "error")
    return redirect(url_for("portal_admin.runtime"))


@portal_admin_bp.route("/tools/<int:tool_id>/process/log")
@admin_required
def tool_process_log(tool_id: int):
    tool = models.get_portal_tool(tool_id)
    if tool is None:
        abort(404)
    from flask import Response
    return Response(launcher.tail_log(tool["slug"], lines=300), mimetype="text/plain")


# ----------------------------------------------------------------------
# Toggle enable / disable
# ----------------------------------------------------------------------
@portal_admin_bp.route("/tools/<int:tool_id>/toggle", methods=["POST"])
@admin_required
def tool_toggle(tool_id: int):
    tool = models.get_portal_tool(tool_id)
    if tool is None:
        abort(404)
    new_state = not bool(tool["is_enabled"])
    models.set_portal_tool_enabled(tool_id, new_state)
    audit.record("tool.enabled" if new_state else "tool.disabled", category=audit.CAT_TOOL,
                 target_type="tool", target_id=tool_id, target_label=tool["name"],
                 old_value={"is_enabled": bool(tool["is_enabled"])},
                 new_value={"is_enabled": new_state})
    flash(f"Tool '{tool['name']}' {'enabled' if new_state else 'disabled'}.", "info")
    return redirect(url_for("portal_admin.tools"))


# ----------------------------------------------------------------------
# Delete
# ----------------------------------------------------------------------
@portal_admin_bp.route("/tools/<int:tool_id>/delete", methods=["POST"])
@admin_required
def tool_delete(tool_id: int):
    tool = models.get_portal_tool(tool_id)
    if tool is None:
        abort(404)
    models.delete_portal_tool(tool_id)
    audit.record("tool.deleted", category=audit.CAT_TOOL,
                 target_type="tool", target_id=tool_id, target_label=tool["name"],
                 old_value={"slug": tool["slug"], "launch_type": tool["launch_type"]})
    flash(f"Tool '{tool['name']}' deleted.", "info")
    return redirect(url_for("portal_admin.tools"))


# ----------------------------------------------------------------------
# Team access assignment
# ----------------------------------------------------------------------
@portal_admin_bp.route("/tools/<int:tool_id>/access", methods=["GET", "POST"])
@admin_required
def tool_access(tool_id: int):
    tool = models.get_portal_tool(tool_id)
    if tool is None:
        abort(404)
    teams = models.list_teams()

    if request.method == "POST":
        requires_team = bool(request.form.get("requires_team"))
        # Persist the gating flag on the tool itself.
        cfg = {}
        try:
            cfg = json.loads(tool["launch_config"] or "{}")
        except (TypeError, ValueError):
            cfg = {}
        models.update_portal_tool(
            tool_id, name=tool["name"], description=tool["description"],
            icon=tool["icon"], icon_type=tool["icon_type"],
            tags=json.loads(tool["tags_json"] or "[]"), status=tool["status"],
            launch_type=tool["launch_type"], launch_config=cfg,
            display_order=tool["display_order"], requires_team=requires_team,
        )
        if requires_team:
            scope = request.form.get("scope", "all")
            if scope == "all":
                models.set_tool_access(tool_id, ["all"], granted_by=current_user.id)
            else:
                chosen = request.form.getlist("team_ids")
                models.set_tool_access(tool_id, chosen, granted_by=current_user.id)
        else:
            models.set_tool_access(tool_id, [], granted_by=current_user.id)
        audit.record("tool.access_changed", category=audit.CAT_TOOL,
                     target_type="tool", target_id=tool_id, target_label=tool["name"],
                     new_value={"requires_team": requires_team,
                                "grants": models.list_tool_access_team_ids(tool_id)})
        flash("Tool access updated.", "success")
        return redirect(url_for("portal_admin.tools"))

    grants = models.list_tool_access_team_ids(tool_id)
    return render_template("admin_tool_access.html", tool=tool, teams=teams,
                           grants=grants, grant_all=("all" in grants))
