"""
Release Tracker — Flask blueprint (pages + JSON APIs).

Blueprint ``rt`` mounted at ``/tools/release-tracker``. Thin controller layer:
request parsing, role enforcement, delegation to the service/data/IO modules, and
audit logging. All heavy lifting lives in ``rt_service`` / ``rt_db`` / ``rt_io``.

Role model (enforced server-side, never just in the UI):
  * View / manual entry / import / export / bulk update / inline edit — any
    authenticated user with access to the tool.
  * Database Configuration (create/modify project databases) — Admin / Team Lead.
  * Delete records — Admin / Team Lead.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from flask import (Blueprint, Response, abort, flash, jsonify, redirect,
                   render_template, request, url_for)
from flask_login import current_user, login_required

import audit
import models
import rt_db
import rt_io
import rt_service
import rt_store
from decorators import team_leader_required

log = logging.getLogger("release-tracker")

_HERE = Path(__file__).resolve().parents[1]  # release-tracker-tool/

rt_bp = Blueprint("rt", __name__, url_prefix="/tools/release-tracker",
                  template_folder=str(_HERE / "templates"))

_PAGE_SIZES = (50, 100, 200, 500)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _can_delete() -> bool:
    return bool(getattr(current_user, "is_admin", False)
                or getattr(current_user, "is_team_leader", False))


def _can_configure() -> bool:
    return _can_delete()


def _default_sent_by() -> str:
    return (getattr(current_user, "employee_code", None)
            or getattr(current_user, "username", "") or "")


def _sent_by_options() -> list[dict]:
    """Selectable 'Sent By' values: the current user plus same-team members."""
    opts: dict[str, str] = {}
    me = _default_sent_by()
    if me:
        opts[me] = f"{getattr(current_user, 'full_name', None) or current_user.username} (you)"
    team_id = getattr(current_user, "team_id", None)
    if team_id:
        try:
            for m in models.get_team_members(team_id):
                val = m["employee_code"] or m["username"]
                if val and val not in opts:
                    opts[val] = f"{m['full_name'] or m['username']}"
        except Exception:  # noqa: BLE001
            log.exception("Failed to load team members for sent-by options")
    return [{"value": v, "label": lbl} for v, lbl in opts.items()]


def _allowed_sent_by() -> set[str]:
    return {o["value"] for o in _sent_by_options()}


def _selected_project():
    """Resolve the active project from ?project= or default to the first one."""
    projects = rt_store.list_projects(active_only=True)
    if not projects:
        return None, []
    pid = request.args.get("project", type=int)
    chosen = next((p for p in projects if p["id"] == pid), projects[0])
    return chosen, projects


def _conn(project_row):
    """Return (provider, cfg, table_name) for a project row, or abort 404."""
    info = rt_store.project_connection(project_row["id"])
    if info is None:
        abort(404)
    provider, cfg = info
    return provider, cfg, project_row["table_name"]


def _parse_filters(args) -> dict:
    f: dict = {}
    for key in ("q", "category", "sent_by", "enhancement_id"):
        v = (args.get(key) or "").strip()
        if v:
            f[key] = v
    for key in ("batch_number", "batch_from", "batch_to"):
        v = args.get(key, type=int)
        if v is not None:
            f[key] = v
    for name in rt_service.DATE_FIELDS:
        for suffix in ("", "_from", "_to"):
            raw = (args.get(name + suffix) or "").strip()
            if not raw:
                continue
            try:
                d = rt_service.parse_date(raw)
            except rt_service.ValidationError:
                continue
            if d is not None:
                f[name + suffix] = d
    return f


def _json_ok(**kw):
    return jsonify({"ok": True, **kw})


def _json_err(message, status=400, **kw):
    return jsonify({"ok": False, "error": message, **kw}), status


# ----------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------
@rt_bp.route("/")
@login_required
def dashboard():
    project, projects = _selected_project()
    return render_template(
        "rt_dashboard.html",
        project=project,
        projects=projects,
        categories=rt_service.CATEGORIES,
        grid_columns=rt_service.GRID_COLUMNS,
        page_sizes=_PAGE_SIZES,
        sent_by_options=_sent_by_options(),
        default_sent_by=_default_sent_by(),
        today=date.today().isoformat(),
        can_delete=_can_delete(),
        can_configure=_can_configure(),
    )


@rt_bp.route("/config")
@team_leader_required
def config():
    import db_providers
    providers = [{
        "id": p.id, "label": p.label, "kind": p.kind,
        "default_port": p.default_port, "fields": list(p.fields),
        "available": db_providers.driver_available(p.id),
    } for p in db_providers.list_providers()]
    projects = rt_store.list_projects(active_only=False)
    rows = []
    for pr in projects:
        rows.append({"row": pr, "cfg": rt_store.project_public_config(pr)})
    return render_template("rt_config.html", providers=providers, projects=rows,
                          can_configure=True)


# ----------------------------------------------------------------------
# Database configuration APIs (Admin / Team Lead only)
# ----------------------------------------------------------------------
def _cfg_from_form(form) -> tuple[str, dict]:
    provider = (form.get("provider") or "").strip()
    cfg = {}
    for key in ("host", "port", "database", "service_name", "username", "password", "extra", "path"):
        v = (form.get(key) or "").strip()
        if v:
            cfg[key] = v
    return provider, cfg


@rt_bp.route("/config/test", methods=["POST"])
@team_leader_required
def config_test():
    provider, cfg = _cfg_from_form(request.form)
    if not provider:
        return _json_err("Select a database provider.")
    result = rt_db.test_connection(provider, cfg)
    return jsonify(result)


@rt_bp.route("/config/create", methods=["POST"])
@team_leader_required
def config_create():
    name = (request.form.get("name") or "").strip()
    provider, cfg = _cfg_from_form(request.form)
    if not name:
        flash("Project name is required.", "error")
        return redirect(url_for("rt.config"))
    if not provider:
        flash("Select a database provider.", "error")
        return redirect(url_for("rt.config"))

    # 1) validate the connection before doing anything persistent
    result = rt_db.test_connection(provider, cfg)
    if not result.get("ok"):
        flash(f"Connection failed: {result.get('message')}", "error")
        return redirect(url_for("rt.config"))

    # 2) create the project registration, then 3) create its external table
    try:
        pid = rt_store.create_project(
            name=name, provider=provider, cfg=cfg,
            created_by=getattr(current_user, "id", None),
            created_by_name=getattr(current_user, "username", None))
        project = rt_store.get_project(pid)
        rt_db.create_table(provider, cfg, project["table_name"])
    except Exception as exc:  # noqa: BLE001
        log.exception("Failed to create Release Tracker project")
        # roll back the registration if table creation failed
        try:
            if "pid" in locals():
                rt_store.delete_project(pid)
        except Exception:  # noqa: BLE001
            pass
        flash(f"Could not create the project tables: {exc}", "error")
        return redirect(url_for("rt.config"))

    audit.record("release_tracker.project_created", category=audit.CAT_CONFIG,
                 target_type="rt_project", target_id=pid, target_label=name,
                 new_value={"provider": provider, "table": project["table_name"]})
    flash(f"Project '{name}' created and Release Tracker tables initialised.", "success")
    return redirect(url_for("rt.dashboard", project=pid))


@rt_bp.route("/config/<int:project_id>/delete", methods=["POST"])
@team_leader_required
def config_delete(project_id):
    project = rt_store.get_project(project_id)
    if project is None:
        abort(404)
    rt_store.delete_project(project_id)
    audit.record("release_tracker.project_deleted", category=audit.CAT_CONFIG,
                 target_type="rt_project", target_id=project_id,
                 target_label=project["name"])
    flash(f"Project '{project['name']}' configuration removed (external table left intact).",
          "success")
    return redirect(url_for("rt.config"))


# ----------------------------------------------------------------------
# Record APIs
# ----------------------------------------------------------------------
@rt_bp.route("/api/records")
@login_required
def api_records():
    project, _ = _selected_project()
    if project is None:
        return _json_err("No project configured yet.", status=404)
    provider, cfg, table = _conn(project)

    page = max(1, request.args.get("page", 1, type=int))
    page_size = request.args.get("page_size", 50, type=int)
    if page_size not in _PAGE_SIZES:
        page_size = 50
    sort = request.args.get("sort", "s_no")
    direction = request.args.get("dir", "desc")
    grouped = request.args.get("group", "1") == "1"
    filters = _parse_filters(request.args)

    try:
        total = rt_db.count_records(provider, cfg, table, filters=filters)
        rows = rt_db.search_records(provider, cfg, table, filters=filters,
                                    sort=sort, direction=direction,
                                    limit=page_size, offset=(page - 1) * page_size)
    except Exception as exc:  # noqa: BLE001
        log.exception("Release Tracker record query failed")
        return _json_err(f"Database error: {exc}", status=502)

    total_pages = max(1, (total + page_size - 1) // page_size)
    payload = {
        "rows": rows,
        "total": total, "page": page, "page_size": page_size,
        "total_pages": total_pages,
    }
    if grouped:
        payload["groups"] = rt_service.group_records(rows)
    return _json_ok(**payload)


@rt_bp.route("/api/records", methods=["POST"])
@login_required
def api_create():
    project, _ = _selected_project()
    if project is None:
        return _json_err("No project configured yet.", status=404)
    provider, cfg, table = _conn(project)
    data = request.get_json(silent=True) or {}

    sent_by = (data.get("sent_by") or _default_sent_by()).strip()
    if sent_by not in _allowed_sent_by():
        return _json_err("You can only set 'Sent By' to yourself or a member of your team.")

    try:
        records = rt_service.build_manual_records(data, sent_by=sent_by)
    except rt_service.ValidationError as exc:
        return _json_err(str(exc))

    try:
        existing = rt_db.existing_batch_numbers(provider, cfg, table)
        to_insert, skipped = [], []
        for rec in records:
            if rec["batch_number"] in existing:
                skipped.append(rec["batch_number"])
            else:
                to_insert.append(rec)
                existing.add(rec["batch_number"])
        inserted = rt_db.insert_records(provider, cfg, table, to_insert,
                                        created_by=_default_sent_by())
    except Exception as exc:  # noqa: BLE001
        log.exception("Release Tracker manual insert failed")
        return _json_err(f"Database error: {exc}", status=502)

    audit.record("release_tracker.records_created", category=audit.CAT_TOOL,
                 target_type="rt_project", target_id=project["id"],
                 target_label=project["name"],
                 details={"inserted": inserted, "skipped": skipped})
    msg = f"Added {inserted} record(s)."
    if skipped:
        msg += f" Skipped {len(skipped)} duplicate batch number(s): {', '.join(map(str, skipped))}."
    return _json_ok(inserted=inserted, skipped=skipped, message=msg)


@rt_bp.route("/api/records/<int:s_no>/update", methods=["POST"])
@login_required
def api_update(s_no):
    project, _ = _selected_project()
    if project is None:
        return _json_err("No project configured yet.", status=404)
    provider, cfg, table = _conn(project)
    data = request.get_json(silent=True) or {}

    changes = {}
    for col in rt_service.EDITABLE_FIELDS:
        if col not in data:
            continue
        val = data[col]
        if col in rt_service.DATE_FIELDS:
            try:
                changes[col] = rt_service.parse_date(val, field=col)
            except rt_service.ValidationError as exc:
                return _json_err(str(exc))
        elif col == "enhancement_id":
            try:
                changes[col] = rt_service.validate_enhancement_id(val)
            except rt_service.ValidationError as exc:
                return _json_err(str(exc))
        else:
            s = str(val or "").strip()
            if col == "release_subject" and not s:
                return _json_err("Release Mail Subject cannot be empty.")
            changes[col] = s
    if not changes:
        return _json_err("No editable fields supplied.")

    try:
        affected = rt_db.update_record(provider, cfg, table, s_no, changes,
                                       updated_by=_default_sent_by())
    except Exception as exc:  # noqa: BLE001
        log.exception("Release Tracker update failed")
        return _json_err(f"Database error: {exc}", status=502)
    if not affected:
        return _json_err("Record not found or already deleted.", status=404)

    audit.record("release_tracker.record_updated", category=audit.CAT_TOOL,
                 target_type="rt_record", target_id=s_no,
                 target_label=project["name"], new_value=_isoify(changes))
    return _json_ok(message="Record updated.", updated=affected)


@rt_bp.route("/api/records/delete", methods=["POST"])
@login_required
def api_delete():
    if not _can_delete():
        return _json_err("Only Team Leads and Admins may delete records.", status=403)
    project, _ = _selected_project()
    if project is None:
        return _json_err("No project configured yet.", status=404)
    provider, cfg, table = _conn(project)
    data = request.get_json(silent=True) or {}
    s_nos = [int(x) for x in (data.get("s_nos") or []) if str(x).isdigit()]
    if not s_nos:
        return _json_err("Select at least one record to delete.")

    try:
        deleted = rt_db.soft_delete(provider, cfg, table, s_nos,
                                    deleted_by=_default_sent_by())
    except Exception as exc:  # noqa: BLE001
        log.exception("Release Tracker delete failed")
        return _json_err(f"Database error: {exc}", status=502)

    audit.record("release_tracker.records_deleted", category=audit.CAT_TOOL,
                 target_type="rt_project", target_id=project["id"],
                 target_label=project["name"], details={"s_nos": s_nos, "count": deleted})
    return _json_ok(message=f"Deleted {deleted} record(s).", deleted=deleted)


def _isoify(changes: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in changes.items()}


# ----------------------------------------------------------------------
# Import
# ----------------------------------------------------------------------
@rt_bp.route("/api/import", methods=["POST"])
@login_required
def api_import():
    project, _ = _selected_project()
    if project is None:
        return _json_err("No project configured yet.", status=404)
    provider, cfg, table = _conn(project)

    file = request.files.get("file")
    if not file or not file.filename:
        return _json_err("Choose a CSV or Excel file to import.")
    try:
        raw_rows, header_map = rt_io.read_records(file.filename, file.read())
    except rt_io.ImportFormatError as exc:
        return _json_err(str(exc))

    required = {"enhancement_id", "release_subject", "category", "sent_by",
                "batch_number", "crm_delivery_date"}
    missing = required - set(header_map)
    if missing:
        labels = {"enhancement_id": "Enhancement ID", "release_subject": "Mail Subject",
                  "category": "Category", "sent_by": "Sent By",
                  "batch_number": "Batch Number", "crm_delivery_date": "CRM Delivery Date"}
        return _json_err("Missing required column(s): "
                         + ", ".join(sorted(labels[m] for m in missing)))

    try:
        existing = rt_db.existing_batch_numbers(provider, cfg, table)
    except Exception as exc:  # noqa: BLE001
        log.exception("Release Tracker import pre-check failed")
        return _json_err(f"Database error: {exc}", status=502)

    seen: set[int] = set()
    valid, errors = [], []
    for i, raw in enumerate(raw_rows, start=2):  # row 1 = header
        rec, err = rt_service.validate_import_record(
            raw, seen_batches=seen, existing_batches=existing)
        if err:
            errors.append({"row": i, "batch": raw.get("batch_number"), "error": err})
            continue
        seen.add(rec["batch_number"])
        valid.append(rec)

    inserted = 0
    if valid:
        try:
            inserted = rt_db.insert_records(provider, cfg, table, valid,
                                            created_by=_default_sent_by())
        except Exception as exc:  # noqa: BLE001
            log.exception("Release Tracker import insert failed")
            return _json_err(f"Database error during insert: {exc}", status=502)

    skipped = sum(1 for e in errors if "duplicate" in e["error"].lower() or "already exists" in e["error"].lower())
    failed = len(errors) - skipped
    audit.record("release_tracker.import", category=audit.CAT_TOOL,
                 target_type="rt_project", target_id=project["id"],
                 target_label=project["name"],
                 details={"inserted": inserted, "skipped": skipped, "failed": failed})
    return _json_ok(inserted=inserted, skipped=skipped, failed=failed, errors=errors,
                    message=f"Inserted {inserted} · Skipped {skipped} · Failed {failed}")


# ----------------------------------------------------------------------
# Bulk update
# ----------------------------------------------------------------------
@rt_bp.route("/api/bulk-update", methods=["POST"])
@login_required
def api_bulk_update():
    project, _ = _selected_project()
    if project is None:
        return _json_err("No project configured yet.", status=404)
    provider, cfg, table = _conn(project)

    file = request.files.get("file")
    if not file or not file.filename:
        return _json_err("Choose a CSV or Excel file for the bulk update.")
    try:
        raw_rows, header_map = rt_io.read_records(file.filename, file.read())
    except rt_io.ImportFormatError as exc:
        return _json_err(str(exc))
    if "batch_number" not in header_map:
        return _json_err("The file must contain a 'Batch Number' column to match records.")

    updates, errors = [], []
    for i, raw in enumerate(raw_rows, start=2):
        item, err = rt_service.normalize_bulk_row(raw)
        if err:
            errors.append({"row": i, "batch": raw.get("batch_number"), "error": err})
            continue
        updates.append(item)

    result = {"updated": 0, "skipped": 0, "skipped_batches": []}
    if updates:
        try:
            result = rt_db.bulk_update_by_batch(provider, cfg, table, updates,
                                                updated_by=_default_sent_by())
        except Exception as exc:  # noqa: BLE001
            log.exception("Release Tracker bulk update failed")
            return _json_err(f"Database error: {exc}", status=502)

    audit.record("release_tracker.bulk_update", category=audit.CAT_TOOL,
                 target_type="rt_project", target_id=project["id"],
                 target_label=project["name"], details=result)
    msg = f"Updated {result['updated']} record(s)."
    if result["skipped"]:
        msg += f" Skipped {result['skipped']} row(s)."
    return _json_ok(errors=errors, message=msg, **result)


# ----------------------------------------------------------------------
# Export
# ----------------------------------------------------------------------
@rt_bp.route("/export")
@login_required
def export():
    project, _ = _selected_project()
    if project is None:
        flash("No project configured yet.", "error")
        return redirect(url_for("rt.dashboard"))
    provider, cfg, table = _conn(project)
    fmt = "csv" if request.args.get("fmt") == "csv" else "xlsx"
    filters = _parse_filters(request.args)

    try:
        rows = rt_db.all_records(provider, cfg, table, filters=filters,
                                 sort="batch_number", direction="asc")
    except Exception as exc:  # noqa: BLE001
        log.exception("Release Tracker export failed")
        flash(f"Export failed: {exc}", "error")
        return redirect(url_for("rt.dashboard", project=project["id"]))

    payload, mimetype, ext = rt_io.export_bytes(rows, fmt, sheet_title=project["name"])
    audit.record("release_tracker.export", category=audit.CAT_TOOL,
                 target_type="rt_project", target_id=project["id"],
                 target_label=project["name"], details={"rows": len(rows), "format": ext})
    fname = f"release_tracker_{project['slug']}_{date.today().isoformat()}.{ext}"
    return Response(payload, mimetype=mimetype,
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ----------------------------------------------------------------------
# Distinct sent-by values (filter dropdown, current project scope)
# ----------------------------------------------------------------------
@rt_bp.route("/api/stats")
@login_required
def api_stats():
    project, _ = _selected_project()
    if project is None:
        return _json_ok(stats={"total": 0, "delivered_this_month": 0,
                               "awaiting_prod": 0, "added_this_week": 0})
    provider, cfg, table = _conn(project)
    try:
        s = rt_db.stats(provider, cfg, table)
    except Exception:  # noqa: BLE001
        log.exception("Release Tracker stats failed")
        s = {"total": 0, "delivered_this_month": 0, "awaiting_prod": 0, "added_this_week": 0}
    return _json_ok(stats=s)


@rt_bp.route("/api/batch-gaps")
@login_required
def api_batch_gaps():
    """Missing batch numbers between the lowest and highest uploaded batch."""
    project, _ = _selected_project()
    if project is None:
        return _json_err("No project configured yet.", status=404)
    provider, cfg, table = _conn(project)
    try:
        g = rt_db.batch_gaps(provider, cfg, table)
    except Exception as exc:  # noqa: BLE001
        log.exception("Release Tracker batch-gaps failed")
        return _json_err(f"Database error: {exc}", status=502)
    # Cap the list returned to the browser; the full set is available via export.
    cap = 5000
    missing = g["missing"]
    return _json_ok(min=g["min"], max=g["max"], present=g["present"], count=g["count"],
                    missing=missing[:cap], truncated=len(missing) > cap)


@rt_bp.route("/export/batch-gaps")
@login_required
def export_batch_gaps():
    """Download the full list of missing batch numbers as CSV."""
    project, _ = _selected_project()
    if project is None:
        flash("No project configured yet.", "error")
        return redirect(url_for("rt.dashboard"))
    provider, cfg, table = _conn(project)
    try:
        g = rt_db.batch_gaps(provider, cfg, table)
    except Exception as exc:  # noqa: BLE001
        log.exception("Release Tracker batch-gaps export failed")
        flash(f"Export failed: {exc}", "error")
        return redirect(url_for("rt.dashboard", project=project["id"]))

    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Missing Batch Number"])
    for n in g["missing"]:
        w.writerow([n])
    audit.record("release_tracker.export_batch_gaps", category=audit.CAT_TOOL,
                 target_type="rt_project", target_id=project["id"],
                 target_label=project["name"],
                 details={"missing": g["count"], "min": g["min"], "max": g["max"]})
    fname = f"missing_batches_{project['slug']}_{date.today().isoformat()}.csv"
    return Response(buf.getvalue().encode("utf-8-sig"), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@rt_bp.route("/template/<kind>")
@login_required
def template(kind):
    """Downloadable CSV templates for Import / Bulk Update so users start correct."""
    if kind == "bulk":
        headers = ["Batch Number", "SIT Execution Date", "UAT Execution Date",
                   "PreProd Execution Date", "Prod Live Date"]
        example = ["84", "2026-07-12", "2026-07-15", "2026-07-18", "2026-07-20"]
        fname = "release_tracker_bulk_update_template.csv"
    else:
        headers = ["Enhancement ID", "Mail Subject", "Category", "Sent By", "Batch Number",
                   "CRM Delivery Date", "SIT Date", "UAT Date", "PreProd Date", "Prod Live Date"]
        example = ["ENH-1024", "Payments release notes", "Release",
                   _default_sent_by() or "EC0000", "84", "2026-07-10", "", "", "", ""]
        fname = "release_tracker_import_template.csv"
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerow(example)
    return Response(buf.getvalue().encode("utf-8-sig"), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@rt_bp.route("/api/sent-by-values")
@login_required
def api_sent_by_values():
    project, _ = _selected_project()
    if project is None:
        return _json_ok(values=[])
    provider, cfg, table = _conn(project)
    try:
        vals = rt_db.distinct_values(provider, cfg, table, "sent_by")
    except Exception:  # noqa: BLE001
        vals = []
    return _json_ok(values=vals)
