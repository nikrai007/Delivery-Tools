"""
Platform Admin Console — Flask blueprint ``admin``.

A single, tool-independent home for every administration surface. Historically
the admin screens were reached "through" the AutoBackupRevert tool; they now live
here as a first-class platform concern, surfaced from the home page.

The console (``/admin/``) is a data-driven hub; every /admin/* screen (users,
storage, branding, watched sources, user activity) is served by this blueprint.
URLs are unchanged from when these lived on the abr blueprint — only the endpoint
names moved (abr.admin_* -> admin.*).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
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
import connectors
import db_migrate
import db_providers
import email_utils
import health
import models
import screen_content
import scheduler as scheduler_mod
import security
from constants import BRAND_DIR, DATA_DIR, DB_PATH, JOB_RETENTION_DAYS, MAX_UPLOAD_MB, UPLOAD_ROOT
from decorators import admin_required

log = logging.getLogger("admin-console")

admin_bp = Blueprint(
    "admin", __name__,
    url_prefix="/admin",
    template_folder=str(Path(__file__).resolve().parents[1] / "templates"),
)

# Username rule for admin-created accounts (mirrors the auth blueprint).
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")


ADMIN_SECTIONS = [
    {
        "group": "Platform & UI configuration",
        "cards": [
            {"endpoint": "portal_admin.tools", "icon": "apps", "title": "Tool Portal",
             "desc": "Add, edit, reorder, enable/disable and gate the tool tiles shown on the dashboard.",
             "primary": True},
            {"endpoint": "admin.logo", "icon": "image", "title": "Branding & logo",
             "desc": "Upload the platform logo used across every page and tool."},
            {"endpoint": "admin.storage", "icon": "folder_managed", "title": "Storage settings",
             "desc": "Configure upload/data locations and retention/cleanup behaviour."},
            {"endpoint": "admin.email", "icon": "mail", "title": "Email & notifications",
             "desc": "Configure the SMTP mailer, edit notification templates and send a test email."},
            {"endpoint": "admin.screens", "icon": "edit_note", "title": "Screen content",
             "desc": "Edit on-screen titles, help text and notes without a deployment — changes go live instantly."},
            {"endpoint": "admin.database", "icon": "database", "title": "Database",
             "desc": "SQLite by default. Configure and test an enterprise database (PostgreSQL, "
                     "MySQL, SQL Server, Oracle, MongoDB) and migrate existing data to it."},
        ],
    },
    {
        "group": "People & access",
        "cards": [
            {"endpoint": "admin.users", "icon": "manage_accounts", "title": "Users",
             "desc": "Create users, reset passwords, change roles and activate/deactivate accounts.",
             "badge": "users_count"},
            {"endpoint": "teams.admin_teams", "icon": "group_work", "title": "Teams",
             "desc": "Manage teams, assign leaders and approve join requests.",
             "badge": "pending_team_requests"},
            {"endpoint": "admin.users_bulk", "icon": "upload_file", "title": "Bulk import",
             "desc": "Provision many users at once from CSV, with team and role assignment — "
                     "no manual account creation."},
        ],
    },
    {
        "group": "Operations",
        "cards": [
            {"endpoint": "admin.activity", "icon": "groups", "title": "User activity",
             "desc": "Review platform-wide job activity and per-user usage."},
            {"endpoint": "admin.analytics", "icon": "monitoring", "title": "Tool analytics",
             "desc": "Tool launches over time, adoption by tool / user / team, and most-used tools for capacity planning."},
            {"endpoint": "admin.sources", "icon": "cloud_sync", "title": "Watched sources",
             "desc": "Configure folders/Git repos that are polled and auto-processed on a schedule."},
        ],
    },
    {
        "group": "Monitoring",
        "cards": [
            {"endpoint": "admin.status", "icon": "monitor_heart", "title": "System status",
             "desc": "Live health of the database, scheduler and every tool — response times, "
                     "last check and error details, auto-refreshing."},
            {"endpoint": "portal_admin.runtime", "icon": "terminal", "title": "Tool runtime",
             "desc": "Start, stop, restart and view logs for Python-app and executable tools "
                     "managed as processes on the host."},
        ],
    },
    {
        "group": "Security & compliance",
        "cards": [
            {"endpoint": "admin.security_policy", "icon": "shield_lock", "title": "Security policy",
             "desc": "Password policy, login rate limiting, account lockout, session timeout and "
                     "mandatory admin 2FA."},
            {"endpoint": "admin.audit_log", "icon": "policy", "title": "Audit log",
             "desc": "Search the enterprise audit trail: logins, user/team/tool/role changes, "
                     "approvals and configuration events — with who, when, IP and before/after values."},
        ],
    },
]


@admin_bp.route("/")
@admin_required
def console():
    stats = {
        "tools_count": len(models.list_portal_tools()),
        "users_count": len(models.list_users()),
        "teams_count": len(models.list_teams()),
        "pending_team_requests": models.count_pending_join_requests(),
    }
    return render_template("admin_console.html", sections=ADMIN_SECTIONS, stats=stats)


# ----------------------------------------------------------------------
# Admin — jobs / users / API tokens / API toggle
# ----------------------------------------------------------------------
@admin_bp.route("/activity")
@admin_required
def activity():
    jobs  = models.list_all_jobs(limit=500)
    users = models.list_users()
    return render_template("admin.html", jobs=jobs, users=users)


@admin_bp.route("/job/<int:job_id>")
@admin_required
def job(job_id):
    job = models.get_job(job_id)
    if job is None:
        abort(404)
    return render_template("admin_job.html",
                           job=job,
                           downloads=models.downloads_for_job(job_id),
                           files=models.job_files(job),
                           user=models.get_user(job["user_id"]))


@admin_bp.route("/users", methods=["GET"])
@admin_required
def users():
    return render_template("admin_users.html", users=models.list_users(),
                           admin_count=models.admin_count())


@admin_bp.route("/users/create", methods=["POST"])
@admin_required
def users_create():
    username = request.form.get("username", "").strip()
    email    = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    role     = request.form.get("role", "user")
    if not USERNAME_RE.match(username):
        flash("Username must be 3–32 chars, letters/digits/._- only.", "error")
        return redirect(url_for("admin.users"))
    pw_errors = security.validate_password(password)
    if pw_errors:
        for e in pw_errors:
            flash(e, "error")
        return redirect(url_for("admin.users"))
    if role not in ("user", "admin"):
        flash("Invalid role.", "error")
        return redirect(url_for("admin.users"))
    if models.username_exists(username):
        flash("Username is already taken.", "error")
        return redirect(url_for("admin.users"))
    # Admin-created accounts must set their own password on first login.
    new_id = models.create_user(username, email, password, role=role,
                                created_by=current_user.id, must_change_password=True)
    audit.record("user.created", category=audit.CAT_USER,
                 target_type="user", target_id=new_id, target_label=username,
                 new_value={"username": username, "email": email, "role": role},
                 details={"created_by_admin": True})
    if email:
        email_utils.notify(
            "account_created", email,
            full_name=username, username=username,
            note="Your administrator will share your initial password separately.",
        )
    flash(f"User '{username}' created.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:uid>/edit", methods=["POST"])
@admin_required
def users_edit(uid):
    target = models.get_user(uid)
    if target is None:
        abort(404)
    email     = request.form.get("email", "").strip()
    role      = request.form.get("role", target["role"])
    is_active = bool(request.form.get("is_active"))

    # Self-protection: can't lock yourself out / strip your last admin.
    if target["id"] == current_user.id:
        if not is_active:
            flash("You can't deactivate your own account.", "error")
            return redirect(url_for("admin.users"))
        if role != "admin":
            flash("You can't demote your own account.", "error")
            return redirect(url_for("admin.users"))
    else:
        if (target["role"] == "admin" and role != "admin"
                and models.admin_count() <= 1):
            flash("Cannot demote the last active admin.", "error")
            return redirect(url_for("admin.users"))
        if (target["role"] == "admin" and target["is_active"]
                and not is_active and models.admin_count() <= 1):
            flash("Cannot deactivate the last active admin.", "error")
            return redirect(url_for("admin.users"))

    if role not in ("user", "admin"):
        flash("Invalid role.", "error")
        return redirect(url_for("admin.users"))
    models.update_user(uid, email=email, role=role, is_active=is_active)
    audit.record("user.updated", category=audit.CAT_USER,
                 target_type="user", target_id=uid, target_label=target["username"],
                 old_value={"email": target["email"], "role": target["role"],
                            "is_active": bool(target["is_active"])},
                 new_value={"email": email, "role": role, "is_active": is_active})
    if role != target["role"]:
        audit.record("user.role_changed", category=audit.CAT_SECURITY,
                     target_type="user", target_id=uid, target_label=target["username"],
                     old_value={"role": target["role"]}, new_value={"role": role})
        if email:
            email_utils.notify("role_changed", email, username=target["username"],
                               old_role=target["role"], new_role=role)
        if role == "admin":  # privilege escalation — alert all admins
            email_utils.notify_admins(
                "admin_event", title="New administrator",
                message=f"'{target['username']}' was granted the admin role by "
                        f"{current_user.username}.")
    flash(f"User '{target['username']}' updated.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:uid>/reset_password", methods=["POST"])
@admin_required
def users_reset(uid):
    target = models.get_user(uid)
    if target is None:
        abort(404)
    new_password = request.form.get("new_password", "")
    pw_errors = security.validate_password(new_password)
    if pw_errors:
        for e in pw_errors:
            flash(e, "error")
        return redirect(url_for("admin.users"))
    models.set_password(uid, new_password)
    models.set_must_change_password(uid, True)  # force change on next login
    audit.record("user.password_reset", category=audit.CAT_SECURITY,
                 target_type="user", target_id=uid, target_label=target["username"],
                 details={"reset_by_admin": True, "force_change": True})
    if target["email"]:
        email_utils.notify("password_reset_by_admin", target["email"],
                           username=target["username"])
    flash(f"Password reset for '{target['username']}'.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:uid>/delete", methods=["POST"])
@admin_required
def users_delete(uid):
    target = models.get_user(uid)
    if target is None:
        abort(404)
    if target["id"] == current_user.id:
        flash("You can't delete your own account.", "error")
        return redirect(url_for("admin.users"))
    if target["role"] == "admin" and models.admin_count() <= 1:
        flash("Cannot delete the last active admin.", "error")
        return redirect(url_for("admin.users"))
    models.delete_user(uid)
    audit.record("user.deleted", category=audit.CAT_USER,
                 target_type="user", target_id=uid, target_label=target["username"],
                 old_value={"username": target["username"], "email": target["email"],
                            "role": target["role"]})
    flash(f"User '{target['username']}' deleted.", "success")
    return redirect(url_for("admin.users"))


# ----------------------------------------------------------------------
# Admin — storage & path settings
# ----------------------------------------------------------------------
def _retention_days() -> int:
    """Read retention days from DB settings, falling back to the env constant."""
    raw = models.setting_get("retention.days")
    try:
        return max(1, int(raw)) if raw else JOB_RETENTION_DAYS
    except ValueError:
        return JOB_RETENTION_DAYS


@admin_bp.route("/storage", methods=["GET", "POST"])
@admin_required
def storage():
    import shutil as _shutil
    from flask import current_app

    if request.method == "POST":
        action = request.form.get("action", "save")

        # ── Storage paths ────────────────────────────────────────────────
        if action == "save":
            log_path    = request.form.get("log_path", "").strip()
            backup_dest = request.form.get("backup_dest", "").strip()
            upload_root = request.form.get("upload_root", "").strip()
            errors = []
            if log_path:
                try:
                    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
                except Exception as exc:
                    errors.append(f"Log path invalid: {exc}")
            if backup_dest:
                try:
                    Path(backup_dest).mkdir(parents=True, exist_ok=True)
                except Exception as exc:
                    errors.append(f"Backup destination invalid: {exc}")
            if upload_root:
                try:
                    Path(upload_root).mkdir(parents=True, exist_ok=True)
                except Exception as exc:
                    errors.append(f"Upload root invalid: {exc}")
            if errors:
                for e in errors:
                    flash(e, "error")
            else:
                if log_path:    models.setting_set("storage.log_path",    log_path)
                if backup_dest: models.setting_set("storage.backup_dest", backup_dest)
                if upload_root: models.setting_set("storage.upload_root", upload_root)
                audit.record("config.storage_paths_updated", category=audit.CAT_CONFIG,
                             target_type="settings", target_label="Storage paths",
                             new_value={"log_path": log_path, "backup_dest": backup_dest,
                                        "upload_root": upload_root})
                flash("Storage paths saved.", "success")

        # ── Retention settings ───────────────────────────────────────────
        elif action == "save_retention":
            days_raw      = request.form.get("retention_days", "").strip()
            auto_cleanup  = "1" if request.form.get("auto_cleanup") == "1" else "0"
            if days_raw:
                try:
                    days = int(days_raw)
                    if days < 1:
                        raise ValueError
                    models.setting_set("retention.days", str(days))
                except ValueError:
                    flash("Retention days must be a positive integer.", "error")
                    return redirect(url_for("admin.storage"))
            models.setting_set("retention.auto_cleanup", auto_cleanup)
            audit.record("config.retention_updated", category=audit.CAT_CONFIG,
                         target_type="settings", target_label="Retention settings",
                         new_value={"retention_days": days_raw or None,
                                    "auto_cleanup": auto_cleanup == "1"})
            flash("Retention settings saved.", "success")

        # ── System limits ────────────────────────────────────────────────
        elif action == "save_system":
            max_mb_raw = request.form.get("max_upload_mb", "").strip()
            ttl_raw    = request.form.get("reset_token_ttl", "").strip()
            if max_mb_raw:
                try:
                    mb = int(max_mb_raw)
                    if mb < 1:
                        raise ValueError
                    models.setting_set("upload.max_mb", str(mb))
                    current_app.config["MAX_CONTENT_LENGTH"] = mb * 1024 * 1024
                except ValueError:
                    flash("Max upload MB must be a positive integer.", "error")
                    return redirect(url_for("admin.storage"))
            if ttl_raw:
                try:
                    ttl = int(ttl_raw)
                    if ttl < 1:
                        raise ValueError
                    models.setting_set("auth.reset_token_ttl", str(ttl))
                except ValueError:
                    flash("Token TTL must be a positive integer (minutes).", "error")
                    return redirect(url_for("admin.storage"))
            audit.record("config.system_limits_updated", category=audit.CAT_CONFIG,
                         target_type="settings", target_label="System limits",
                         new_value={"max_upload_mb": max_mb_raw or None,
                                    "reset_token_ttl": ttl_raw or None})
            flash("System settings saved.", "success")

        # ── Manual cleanup (older than retention window) ─────────────────
        elif action == "cleanup":
            days = _retention_days()
            removed = 0
            for job_id, work_dir in models.expired_job_work_dirs(days):
                p = Path(work_dir)
                if p.exists():
                    try:
                        _shutil.rmtree(p, ignore_errors=True)
                        removed += 1
                    except Exception:
                        pass
                models.clear_job_workdir(job_id)
            flash(
                f"Cleaned {removed} director{'y' if removed == 1 else 'ies'} "
                f"older than {days} day(s).",
                "success",
            )

        # ── Date-range cleanup ───────────────────────────────────────────
        elif action == "cleanup_range":
            date_from = request.form.get("date_from", "").strip()
            date_to   = request.form.get("date_to",   "").strip()
            if not date_from or not date_to:
                flash("Both from and to dates are required.", "error")
            elif date_from > date_to:
                flash("'From' date must be on or before 'To' date.", "error")
            else:
                removed = 0
                for job_id, work_dir in models.expired_job_work_dirs_range(date_from, date_to):
                    p = Path(work_dir)
                    if p.exists():
                        try:
                            _shutil.rmtree(p, ignore_errors=True)
                            removed += 1
                        except Exception:
                            pass
                    models.clear_job_workdir(job_id)
                flash(
                    f"Removed working directories for {removed} job(s) "
                    f"created {date_from} → {date_to}. "
                    f"Job records and history are preserved.",
                    "success" if removed else "info",
                )

        return redirect(url_for("admin.storage"))

    # ── GET — gather current effective values ────────────────────────────
    import shutil as _shutil
    ROOT_DIR     = Path(__file__).resolve().parents[2]
    _UPLOAD_ROOT = UPLOAD_ROOT
    log_file     = ROOT_DIR / "logs" / "app.log"
    disk         = _shutil.disk_usage(ROOT_DIR)
    upload_size  = sum(
        f.stat().st_size for f in _UPLOAD_ROOT.rglob("*") if f.is_file()
    ) if _UPLOAD_ROOT.exists() else 0

    ret_days     = _retention_days()
    auto_cleanup = models.setting_get("retention.auto_cleanup", "1") == "1"
    max_mb       = int(models.setting_get("upload.max_mb") or MAX_UPLOAD_MB)
    reset_ttl    = int(models.setting_get("auth.reset_token_ttl") or
                       os.getenv("RESET_TOKEN_TTL_MINUTES", "60"))

    ctx = {
        "current_log_path":    str(models.setting_get("storage.log_path") or log_file),
        "current_backup_dest": str(models.setting_get("storage.backup_dest") or ""),
        "current_upload_root": str(models.setting_get("storage.upload_root") or _UPLOAD_ROOT),
        "current_data_dir":    str(DATA_DIR),
        "disk_total_gb":       disk.total / 1e9,
        "disk_used_gb":        disk.used  / 1e9,
        "disk_free_gb":        disk.free  / 1e9,
        "upload_size_mb":      upload_size / 1e6,
        "retention_days":      ret_days,
        "auto_cleanup":        auto_cleanup,
        "max_upload_mb":       max_mb,
        "reset_token_ttl":     reset_ttl,
    }
    return render_template("admin_storage.html", **ctx)


# ----------------------------------------------------------------------
# Admin — logo management
# ----------------------------------------------------------------------
_ALLOWED_LOGO_EXT = {".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif"}
_MAX_LOGO_BYTES   = 5 * 1024 * 1024   # 5 MB


@admin_bp.route("/logo", methods=["GET"])
@admin_required
def logo():
    current_logo = models.setting_get("brand.logo_filename") or ""
    preview_url  = None
    if current_logo and (BRAND_DIR / current_logo).exists():
        preview_url = url_for("static", filename=f"brand/{current_logo}")
    return render_template("admin_logo.html",
                           current_logo=current_logo,
                           preview_url=preview_url)


@admin_bp.route("/logo/upload", methods=["POST"])
@admin_required
def logo_upload():
    f = request.files.get("logo")
    if not f or not f.filename:
        flash("No file selected.", "error")
        return redirect(url_for("admin.logo"))
    ext = Path(secure_filename(f.filename)).suffix.lower()
    if ext not in _ALLOWED_LOGO_EXT:
        flash(
            f"Unsupported format '{ext}'. Allowed: "
            + ", ".join(sorted(_ALLOWED_LOGO_EXT)),
            "error",
        )
        return redirect(url_for("admin.logo"))
    data = f.read()
    if len(data) > _MAX_LOGO_BYTES:
        flash("Logo file exceeds the 5 MB size limit.", "error")
        return redirect(url_for("admin.logo"))
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"logo{ext}"
    (BRAND_DIR / filename).write_bytes(data)
    models.setting_set("brand.logo_filename", filename)
    audit.record("config.logo_updated", category=audit.CAT_CONFIG,
                 target_type="settings", target_label="Platform logo",
                 new_value={"logo_filename": filename})
    flash("Logo updated successfully. Changes are live across the platform.", "success")
    return redirect(url_for("admin.logo"))


@admin_bp.route("/logo/reset", methods=["POST"])
@admin_required
def logo_reset():
    current = models.setting_get("brand.logo_filename") or ""
    if current:
        try:
            (BRAND_DIR / current).unlink(missing_ok=True)
        except OSError:
            pass
        models.setting_set("brand.logo_filename", "")
    audit.record("config.logo_reset", category=audit.CAT_CONFIG,
                 target_type="settings", target_label="Platform logo",
                 old_value={"logo_filename": current})
    flash("Logo reset to the built-in default.", "success")
    return redirect(url_for("admin.logo"))


# ----------------------------------------------------------------------
# Admin — watched sources (Phase 2 + 3)
# ----------------------------------------------------------------------
_CONNECTOR_KINDS  = ("local", "git")
_SCHEDULE_MODES   = ("weekly_at", "every_minutes", "cron")
_DEFAULT_TIMEZONES = (
    "Asia/Singapore", "Asia/Kolkata", "Asia/Dubai", "Asia/Tokyo",
    "Europe/London", "Europe/Berlin",
    "America/New_York", "America/Los_Angeles",
    "UTC",
)


def _parse_schedule_form(form) -> tuple[dict, list[str]]:
    """
    Pull the rich schedule fields into a normalized ``schedule_json`` dict.

    Accepted fields:
      mode             ∈ weekly_at | every_minutes | cron
      timezone         IANA TZ string
      days             list[str] subset of DAY_KEYS — optional (empty = all days)
      times            list of HH:MM (mode='weekly_at')
      interval_minutes 1..1440 (mode='every_minutes')
      cron_expr        5-field cron (mode='cron')
      start_date       YYYY-MM-DD — optional
      end_date         YYYY-MM-DD — optional
      pause_until      ISO-8601 — optional
    """
    errors: list[str] = []
    mode = (form.get("schedule_mode") or "").strip()
    if mode not in _SCHEDULE_MODES:
        errors.append(f"Schedule mode must be one of: {', '.join(_SCHEDULE_MODES)}.")
        return {}, errors

    timezone   = (form.get("schedule_timezone") or "UTC").strip() or "UTC"
    days       = [d for d in form.getlist("schedule_days") if d in scheduler_mod.DAY_KEYS]
    start_date = (form.get("schedule_start_date") or "").strip() or None
    end_date   = (form.get("schedule_end_date") or "").strip()   or None
    pause_until = (form.get("schedule_pause_until") or "").strip() or None

    schedule: dict = {
        "mode": mode,
        "timezone": timezone,
        "days": days,
        "start_date": start_date,
        "end_date": end_date,
        "pause_until": pause_until,
    }

    if mode == "weekly_at":
        # Multiple time-slot inputs all named "schedule_times".
        slots = [s.strip() for s in form.getlist("schedule_times") if s and s.strip()]
        if not slots:
            errors.append("Add at least one time slot (HH:MM).")
        schedule["times"] = slots
    elif mode == "every_minutes":
        raw = (form.get("schedule_interval_minutes") or "").strip()
        try:
            n = int(raw)
            if n < 1 or n > 720:
                raise ValueError
            schedule["interval_minutes"] = n
        except ValueError:
            errors.append(
                "Interval (minutes) must be an integer 1..720 (i.e. up to every "
                "12 hours). For once-daily or longer cadence, switch to "
                "'specific times on chosen days' mode with a fixed HH:MM."
            )
    elif mode == "cron":
        expr = (form.get("schedule_cron_expr") or "").strip()
        if not expr:
            errors.append("Cron expression is required.")
        schedule["cron_expr"] = expr

    # Validate by actually building the trigger
    if not errors:
        try:
            scheduler_mod.build_trigger_from_json(schedule)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Schedule is invalid: {exc}")

    return schedule, errors


def _parse_source_form(form, *, for_kind: str) -> tuple[dict, dict, dict, list[str]]:
    """
    Pull the create/edit form into (fields, connector_config, schedule_dict, errors).
    """
    errors: list[str] = []
    name        = (form.get("name") or "").strip()
    source_path = (form.get("source_path") or "").strip()
    dest_path   = (form.get("dest_path") or "").strip()
    enabled     = form.get("enabled") == "on"

    sub_path = (form.get("sub_path") or "").strip()
    branch   = (form.get("branch") or "").strip()
    pat      = (form.get("pat") or "").strip()

    if not name:
        errors.append("Source name is required.")
    elif not re.match(r"^[A-Za-z0-9._\- ]{2,64}$", name):
        errors.append("Source name must be 2–64 chars (letters, digits, dots, dashes, underscores, spaces).")

    if for_kind not in _CONNECTOR_KINDS:
        errors.append(f"Connector kind must be one of: {', '.join(_CONNECTOR_KINDS)}.")
    if not source_path:
        errors.append("Source path is required.")
    if not dest_path:
        errors.append("Destination path is required.")

    schedule, sched_errors = _parse_schedule_form(form)
    errors.extend(sched_errors)

    # Validate against the connector
    if not [e for e in errors if "connector" not in e.lower()]:
        try:
            connector = connectors.get_connector(for_kind)
            cfg = {"sub_path": sub_path}
            if for_kind == "git":
                cfg["branch"] = branch or "main"
                if pat:
                    cfg["pat"] = pat
            for e in connector.validate(source_path, dest_path, cfg):
                errors.append(e)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Connector check failed: {exc}")

    config = {"sub_path": sub_path}
    if for_kind == "git":
        config["branch"] = branch or "main"
        if pat:
            config["pat"] = pat

    fields = {
        "name": name, "source_path": source_path, "dest_path": dest_path,
        "enabled": enabled,
        "sub_path": sub_path, "branch": branch,
        "pat_set": bool(pat),
    }
    return fields, config, schedule, errors


@admin_bp.route("/sources")
@admin_required
def sources():
    rows = models.list_watched_sources()
    rows_view = []
    for r in rows:
        # Prefer rich schedule_json describe; fall back to legacy describe_interval.
        label = ""
        paused_until = None
        next_fires_list: list[str] = []
        sj = r["schedule_json"] if "schedule_json" in r.keys() else None
        if sj:
            try:
                sched = json.loads(sj)
                label = scheduler_mod.describe_schedule(sched)
                paused_until = sched.get("pause_until") or None
                next_fires_list = scheduler_mod.next_fires(sched, n=3)
            except Exception:  # noqa: BLE001
                label = "(invalid schedule)"
        if not label:
            label = scheduler_mod.describe_interval(r["interval_kind"], r["interval_value"])
        rows_view.append({
            **{k: r[k] for k in r.keys()},
            "interval_label": label,
            "paused_until": paused_until,
            "next_fires": next_fires_list,
        })
    return render_template("admin_sources.html", sources=rows_view)


def _default_schedule() -> dict:
    """Sensible default for the New-source form: weekdays at 09:00 SGT."""
    return {
        "mode": "weekly_at",
        "timezone": "Asia/Singapore",
        "days": list(scheduler_mod.PRESETS["weekdays"]),
        "times": ["09:00"],
        "interval_minutes": 15,
        "cron_expr": "",
        "start_date": datetime.now().strftime("%Y-%m-%d"),
        "end_date": None,
        "pause_until": None,
    }


def _render_source_form(*, form_mode: str, kind: str, source_view: dict | None,
                        schedule_view: dict, errors: list[str], existing_pat: bool):
    return render_template(
        "admin_source_edit.html",
        mode=form_mode, kind=kind, users=models.list_users(),
        source=source_view, schedule=schedule_view, errors=errors,
        existing_pat=existing_pat,
        connector_kinds=_CONNECTOR_KINDS,
        schedule_modes=_SCHEDULE_MODES,
        day_keys=scheduler_mod.DAY_KEYS,
        day_labels=scheduler_mod.DAY_LABELS,
        presets=scheduler_mod.PRESETS,
        timezones=_DEFAULT_TIMEZONES,
    )


@admin_bp.route("/sources/new", methods=["GET", "POST"])
@admin_required
def sources_new():
    if request.method == "GET":
        kind = (request.args.get("kind") or "local").strip()
        if kind not in _CONNECTOR_KINDS:
            kind = "local"
        return _render_source_form(
            form_mode="new", kind=kind, source_view=None,
            schedule_view=_default_schedule(), errors=[], existing_pat=False,
        )

    kind = (request.form.get("kind") or "").strip()
    if kind not in _CONNECTOR_KINDS:
        flash("Pick a connector kind.", "error")
        return redirect(url_for("admin.sources_new"))

    fields, config, schedule, errors = _parse_source_form(request.form, for_kind=kind)
    owner_user_id = int(request.form.get("owner_user_id") or current_user.id)
    if not models.get_user(owner_user_id):
        errors.append("Owner user does not exist.")

    if errors:
        for e in errors:
            flash(e, "error")
        return _render_source_form(
            form_mode="new", kind=kind,
            source_view={"id": None, **fields, "owner_user_id": owner_user_id},
            schedule_view=schedule or _default_schedule(),
            errors=errors, existing_pat=False,
        )

    # Mirror the new model into the legacy interval_* columns so older code
    # paths and pre-v2 readers still see something sensible.
    legacy_kind, legacy_value = _legacy_interval_from_schedule(schedule)

    source_id = models.create_watched_source(
        name=fields["name"], kind=kind,
        source_path=fields["source_path"], dest_path=fields["dest_path"],
        config_json=json.dumps(config),
        interval_kind=legacy_kind, interval_value=legacy_value,
        schedule_json=json.dumps(schedule),
        owner_user_id=owner_user_id, created_by_user_id=current_user.id,
    )
    scheduler_mod.reload_source(source_id)
    audit.record("config.source_created", category=audit.CAT_CONFIG,
                 target_type="source", target_id=source_id, target_label=fields["name"],
                 new_value={"kind": kind, "source_path": fields["source_path"],
                            "dest_path": fields["dest_path"], "owner_user_id": owner_user_id})
    flash(f"Watched source '{fields['name']}' created.", "success")
    return redirect(url_for("admin.sources"))


@admin_bp.route("/sources/<int:source_id>/edit", methods=["GET", "POST"])
@admin_required
def sources_edit(source_id):
    source = models.get_watched_source(source_id)
    if source is None:
        abort(404)
    kind = source["kind"]
    existing_cfg = connectors.parse_config(source["config_json"])
    existing_pat = bool(existing_cfg.get("pat"))
    existing_schedule: dict
    if "schedule_json" in source.keys() and source["schedule_json"]:
        try:
            existing_schedule = json.loads(source["schedule_json"])
        except Exception:  # noqa: BLE001
            existing_schedule = _default_schedule()
    else:
        existing_schedule = _default_schedule()

    if request.method == "GET":
        view = {**{k: source[k] for k in source.keys()},
                "sub_path": existing_cfg.get("sub_path") or "",
                "branch":   existing_cfg.get("branch")   or "",
                "enabled":  bool(source["enabled"])}
        return _render_source_form(
            form_mode="edit", kind=kind, source_view=view,
            schedule_view=existing_schedule, errors=[], existing_pat=existing_pat,
        )

    fields, config, schedule, errors = _parse_source_form(request.form, for_kind=kind)
    # Preserve existing PAT if the form left it blank.
    if kind == "git" and not config.get("pat") and existing_pat:
        config["pat"] = existing_cfg["pat"]
    # Preserve existing pause_until if the edit form didn't include one
    # (so the existing snooze isn't lost on an unrelated edit).
    if not schedule.get("pause_until") and existing_schedule.get("pause_until"):
        schedule["pause_until"] = existing_schedule["pause_until"]

    if errors:
        for e in errors:
            flash(e, "error")
        view = {**{k: source[k] for k in source.keys()},
                **fields, "id": source_id,
                "enabled": fields["enabled"]}
        return _render_source_form(
            form_mode="edit", kind=kind, source_view=view,
            schedule_view=schedule or existing_schedule,
            errors=errors, existing_pat=existing_pat,
        )

    legacy_kind, legacy_value = _legacy_interval_from_schedule(schedule)
    models.update_watched_source(
        source_id,
        name=fields["name"],
        source_path=fields["source_path"], dest_path=fields["dest_path"],
        config_json=json.dumps(config),
        interval_kind=legacy_kind, interval_value=legacy_value,
        schedule_json=json.dumps(schedule),
        enabled=fields["enabled"],
    )
    if fields["enabled"]:
        scheduler_mod.reload_source(source_id)
    else:
        scheduler_mod.unschedule_source(source_id)
    audit.record("config.source_updated", category=audit.CAT_CONFIG,
                 target_type="source", target_id=source_id, target_label=fields["name"],
                 old_value={"name": source["name"], "source_path": source["source_path"],
                            "dest_path": source["dest_path"], "enabled": bool(source["enabled"])},
                 new_value={"name": fields["name"], "source_path": fields["source_path"],
                            "dest_path": fields["dest_path"], "enabled": fields["enabled"]})
    flash(f"Watched source '{fields['name']}' updated.", "success")
    return redirect(url_for("admin.sources"))


def _legacy_interval_from_schedule(schedule: dict) -> tuple[str, str]:
    """
    Synthesize legacy ``(interval_kind, interval_value)`` strings from the
    rich schedule so the older NOT NULL columns get a sensible value.
    These columns are not used by the scheduler when schedule_json is set;
    they only exist for backward-compat with pre-v2 rows.
    """
    mode = schedule.get("mode")
    if mode == "weekly_at":
        return "daily_at", ",".join(schedule.get("times") or [])
    if mode == "every_minutes":
        return "every_minutes", str(schedule.get("interval_minutes") or 15)
    if mode == "cron":
        return "cron", schedule.get("cron_expr") or "*/15 * * * *"
    return "every_minutes", "60"


@admin_bp.route("/sources/preview", methods=["POST"])
@admin_required
def sources_preview():
    """
    Live "next 5 fire times" preview used by the source-edit form.

    Body: same field names as the create/edit form. We only parse the schedule
    bits — connector validation is irrelevant for the preview.
    """
    schedule, errors = _parse_schedule_form(request.form)
    if errors:
        return jsonify(ok=False, errors=errors, fires=[])
    fires = scheduler_mod.next_fires(schedule, n=5)
    return jsonify(
        ok=True, errors=[],
        label=scheduler_mod.describe_schedule(schedule),
        fires=fires,
    )


@admin_bp.route("/sources/<int:source_id>/pause", methods=["POST"])
@admin_required
def sources_pause(source_id):
    """Snooze a source until a chosen instant (ISO-8601 in UTC)."""
    until = (request.form.get("until") or "").strip()
    if not until:
        # Default: pause for 24 hours from now.
        until = (datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None) + timedelta(hours=24)).isoformat() + "Z"
    models.set_watched_source_pause(source_id, until)
    flash(f"Source paused until {until} (UTC).", "info")
    return redirect(url_for("admin.sources"))


@admin_bp.route("/sources/<int:source_id>/resume", methods=["POST"])
@admin_required
def sources_resume(source_id):
    """Clear any active pause."""
    models.set_watched_source_pause(source_id, None)
    flash("Source resumed.", "success")
    return redirect(url_for("admin.sources"))


@admin_bp.route("/sources/<int:source_id>/delete", methods=["POST"])
@admin_required
def sources_delete(source_id):
    source = models.get_watched_source(source_id)
    if source is None:
        abort(404)
    scheduler_mod.unschedule_source(source_id)
    models.delete_watched_source(source_id)
    audit.record("config.source_deleted", category=audit.CAT_CONFIG,
                 target_type="source", target_id=source_id, target_label=source["name"],
                 old_value={"kind": source["kind"], "source_path": source["source_path"]})
    flash(f"Watched source '{source['name']}' deleted.", "info")
    return redirect(url_for("admin.sources"))


@admin_bp.route("/sources/<int:source_id>/run", methods=["POST"])
@admin_required
def sources_run(source_id):
    source = models.get_watched_source(source_id)
    if source is None:
        abort(404)
    scheduler_mod.run_source_now(source_id)
    flash(f"Manual run triggered for '{source['name']}'. Refresh in a few seconds.", "info")
    return redirect(url_for("admin.sources"))


# ----------------------------------------------------------------------
# Admin — enterprise audit log (searchable trail; admin-only)
# ----------------------------------------------------------------------
_AUDIT_PAGE_SIZE = 100


def _audit_filters_from_request():
    """Extract & normalize the audit search filters shared by the viewer and CSV export."""
    q         = (request.args.get("q") or "").strip()
    category  = (request.args.get("category") or "").strip()
    action    = (request.args.get("action") or "").strip()
    status    = (request.args.get("status") or "").strip()
    date_from = (request.args.get("from") or "").strip()
    date_to   = (request.args.get("to") or "").strip()
    uid_raw   = (request.args.get("user_id") or "").strip()
    user_id   = int(uid_raw) if uid_raw.isdigit() else None
    return {
        "q": q or None, "category": category or None, "action": action or None,
        "status": status or None, "date_from": date_from or None,
        "date_to": date_to or None, "user_id": user_id,
    }


@admin_bp.route("/audit")
@admin_required
def audit_log():
    filters = _audit_filters_from_request()
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    offset = (page - 1) * _AUDIT_PAGE_SIZE

    total = models.count_audit_log(**filters)
    rows  = models.search_audit_log(**filters, limit=_AUDIT_PAGE_SIZE, offset=offset)
    total_pages = max(1, (total + _AUDIT_PAGE_SIZE - 1) // _AUDIT_PAGE_SIZE)

    # Raw args (as strings) for re-rendering the form / building pager links.
    raw = {
        "q": request.args.get("q", ""), "category": request.args.get("category", ""),
        "action": request.args.get("action", ""), "status": request.args.get("status", ""),
        "from": request.args.get("from", ""), "to": request.args.get("to", ""),
        "user_id": request.args.get("user_id", ""),
    }
    return render_template(
        "admin_audit.html",
        rows=rows, total=total, page=page, total_pages=total_pages,
        page_size=_AUDIT_PAGE_SIZE,
        categories=models.distinct_audit_categories(),
        actions=models.distinct_audit_actions(),
        users=models.list_users(),
        raw=raw,
    )


@admin_bp.route("/audit/export.csv")
@admin_required
def audit_export():
    """Download the current filtered audit view as CSV (compliance export)."""
    import csv
    import io

    filters = _audit_filters_from_request()
    # Export is capped high enough for compliance pulls without unbounded memory use.
    rows = models.search_audit_log(**filters, limit=50000, offset=0)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "timestamp_utc", "user_id", "username", "actor_role", "ip_address",
        "category", "action", "target_type", "target_id", "target_label",
        "old_value", "new_value", "details", "status",
    ])
    for r in rows:
        writer.writerow([
            r["id"], r["created_at"], r["user_id"], r["username"], r["actor_role"],
            r["ip_address"], r["category"], r["action"], r["target_type"],
            r["target_id"], r["target_label"], r["old_value"], r["new_value"],
            r["details"], r["status"],
        ])

    from flask import Response
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    audit.record("config.audit_exported", category=audit.CAT_CONFIG,
                 target_type="audit_log", target_label="CSV export",
                 details={"rows": len(rows), "filters": {k: v for k, v in filters.items() if v}})
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=audit_log_{stamp}.csv"},
    )


# ----------------------------------------------------------------------
# Admin — email / SMTP configuration + notification templates
# ----------------------------------------------------------------------
@admin_bp.route("/email", methods=["GET", "POST"])
@admin_required
def email():
    if request.method == "POST":
        action = request.form.get("action", "save_smtp")

        # ── SMTP connection settings ─────────────────────────────────────
        if action == "save_smtp":
            host     = (request.form.get("smtp_host") or "").strip()
            port     = (request.form.get("smtp_port") or "").strip()
            user     = (request.form.get("smtp_user") or "").strip()
            password = (request.form.get("smtp_password") or "")
            mail_from = (request.form.get("smtp_from") or "").strip()
            starttls = "1" if request.form.get("smtp_starttls") == "1" else "0"
            enabled  = "1" if request.form.get("smtp_enabled") == "1" else "0"

            if port and not port.isdigit():
                flash("SMTP port must be a number.", "error")
                return redirect(url_for("admin.email"))

            models.setting_set("smtp.host", host)
            models.setting_set("smtp.port", port or "587")
            models.setting_set("smtp.user", user)
            models.setting_set("smtp.from", mail_from)
            models.setting_set("smtp.starttls", starttls)
            models.setting_set("smtp.enabled", enabled)
            # Only overwrite the stored password when a new one is supplied,
            # so re-saving the form doesn't wipe it (mirrors the PAT pattern).
            if password:
                models.setting_set("smtp.password", password)

            audit.record("config.smtp_updated", category=audit.CAT_CONFIG,
                         target_type="settings", target_label="SMTP configuration",
                         new_value={"host": host, "port": port or "587", "user": user,
                                    "from": mail_from, "starttls": starttls == "1",
                                    "enabled": enabled == "1",
                                    "password_changed": bool(password)})
            flash("SMTP settings saved.", "success")
            return redirect(url_for("admin.email"))

        # ── Send a test email ────────────────────────────────────────────
        if action == "send_test":
            to = (request.form.get("test_to") or "").strip()
            if not to:
                flash("Enter a recipient address for the test.", "error")
                return redirect(url_for("admin.email"))
            ok, msg = email_utils.send_test(to)
            flash(msg, "success" if ok else "error")
            return redirect(url_for("admin.email"))

        # ── Save notification templates (built-in overrides + custom) ─────
        if action == "save_templates":
            changed = []
            for key in email_utils.TEMPLATE_LABELS:
                subj = (request.form.get(f"tpl_{key}_subject") or "").strip()
                body = (request.form.get(f"tpl_{key}_body") or "").strip()
                default = email_utils.DEFAULT_TEMPLATES.get(key, {})
                # Store only when it differs from the built-in default; a blank
                # field (or one equal to default) clears the override.
                if subj and subj != default.get("subject"):
                    models.setting_set(f"email.tpl.{key}.subject", subj); changed.append(key)
                else:
                    models.setting_set(f"email.tpl.{key}.subject", "")
                if body and body != default.get("body"):
                    models.setting_set(f"email.tpl.{key}.body", body); changed.append(key)
                else:
                    models.setting_set(f"email.tpl.{key}.body", "")
            # Custom templates have no built-in default — persist directly.
            for t in email_utils.list_custom_templates():
                ck = t["key"]
                subj = (request.form.get(f"tpl_{ck}_subject") or "").strip()
                body = (request.form.get(f"tpl_{ck}_body") or "").strip()
                if subj and body:
                    email_utils.update_custom_template(ck, "", subj, body); changed.append(ck)
            audit.record("config.email_templates_updated", category=audit.CAT_CONFIG,
                         target_type="settings", target_label="Email templates",
                         new_value={"overridden": sorted(set(changed))})
            flash("Email templates saved.", "success")
            return redirect(url_for("admin.email"))

        # ── Add a custom template ─────────────────────────────────────────
        if action == "add_template":
            ok, msg = email_utils.add_custom_template(
                request.form.get("new_key", ""), request.form.get("new_label", ""),
                request.form.get("new_subject", ""), request.form.get("new_body", ""))
            if ok:
                audit.record("config.email_template_added", category=audit.CAT_CONFIG,
                             target_type="settings",
                             target_label=(request.form.get("new_key") or "").strip().lower())
            flash(msg, "success" if ok else "error")
            return redirect(url_for("admin.email"))

        # ── Delete a custom template ──────────────────────────────────────
        if action == "delete_template":
            key = (request.form.get("del_key") or "").strip()
            ok, msg = email_utils.delete_custom_template(key)
            if ok:
                audit.record("config.email_template_deleted", category=audit.CAT_CONFIG,
                             target_type="settings", target_label=key)
            flash(msg, "success" if ok else "error")
            return redirect(url_for("admin.email"))

        return redirect(url_for("admin.email"))

    # ── GET ──────────────────────────────────────────────────────────────
    cfg = email_utils.get_config()
    templates = []
    for key, label in email_utils.TEMPLATE_LABELS.items():
        eff = email_utils.get_template(key)
        default = email_utils.DEFAULT_TEMPLATES.get(key, {})
        templates.append({
            "key": key, "label": label, "custom": False,
            "subject": eff["subject"], "body": eff["body"],
            "is_overridden": (eff["subject"] != default.get("subject")
                              or eff["body"] != default.get("body")),
        })
    custom_templates = []
    for t in email_utils.list_custom_templates():
        eff = email_utils.get_template(t["key"])
        custom_templates.append({
            "key": t["key"], "label": t["label"], "custom": True,
            "subject": eff["subject"], "body": eff["body"], "is_overridden": True,
        })
    return render_template("admin_email.html", cfg=cfg, templates=templates,
                           custom_templates=custom_templates,
                           placeholders=sorted(email_utils.sample_context().keys()),
                           configured=email_utils.smtp_configured())


@admin_bp.route("/email/preview/<key>", methods=["POST"])
@admin_required
def email_preview(key):
    """Live-render a template with sample data for the admin editor (JSON).
    Renders the POSTed (unsaved) subject/body if provided, else the saved one."""
    subject = request.form.get("subject")
    body = request.form.get("body")
    if subject or body:
        rs, rb = email_utils.render_strings(subject or "", body or "")
    else:
        rs, rb = email_utils.preview(key)
    return jsonify(ok=True, subject=rs, body=rb)


# ----------------------------------------------------------------------
# Admin — editable on-screen content (titles / help text / notes)
# ----------------------------------------------------------------------
@admin_bp.route("/screens")
@admin_required
def screens():
    return render_template("admin_screens.html", screens=screen_content.list_screens())


@admin_bp.route("/screens/<key>", methods=["GET", "POST"])
@admin_required
def edit_screen(key):
    meta = screen_content.SCREENS.get(key)
    if meta is None:
        abort(404)
    if request.method == "POST":
        errors = []
        for f in meta["fields"]:
            val = (request.form.get("f_" + f["key"]) or "").strip()
            if len(val) > screen_content.MAX_LEN:
                errors.append(f"'{f['label']}' exceeds {screen_content.MAX_LEN} characters.")
                continue
            # Blank restores the built-in default (store empty → get() falls back).
            screen_content.set(key, f["key"], val)
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            audit.record("config.screen_content_updated", category=audit.CAT_CONFIG,
                         target_type="screen", target_label=meta["label"],
                         details={"screen": key})
            flash("Screen text saved — changes are live immediately.", "success")
        return redirect(url_for("admin.edit_screen", key=key))

    return render_template("admin_screen_edit.html", key=key, meta=meta,
                           values=screen_content.get_values(key))


# ----------------------------------------------------------------------
# Admin — tool usage analytics
# ----------------------------------------------------------------------
@admin_bp.route("/analytics")
@admin_required
def analytics():
    try:
        days = int(request.args.get("days", "30"))
    except ValueError:
        days = 30
    days = max(7, min(days, 365))
    per_day = models.analytics_launches_per_day(days)
    peak = max((d["count"] for d in per_day), default=0)
    return render_template(
        "admin_analytics.html",
        overview=models.analytics_overview(days),
        by_tool=models.analytics_by_tool(),
        by_user=models.analytics_by_user(20),
        by_team=models.analytics_by_team(20),
        per_day=per_day,
        peak=peak,
        days=days,
    )


# ----------------------------------------------------------------------
# Admin — live system status dashboard
# ----------------------------------------------------------------------
@admin_bp.route("/status")
@admin_required
def status():
    checks = health.run_all_checks()
    return render_template("admin_status.html",
                           checks=checks, summary=health.summarize(checks),
                           refresh=health.refresh_seconds())


@admin_bp.route("/status.json")
@admin_required
def status_json():
    """Polled by the status page for live auto-refresh."""
    checks = health.run_all_checks()
    return jsonify(summary=health.summarize(checks), checks=checks,
                   refresh=health.refresh_seconds())


@admin_bp.route("/security", methods=["GET", "POST"])
@admin_required
def security_policy():
    # Boolean + integer setting keys managed on this page.
    _bools = ["security.pw_require_upper", "security.pw_require_lower",
              "security.pw_require_digit", "security.pw_require_symbol",
              "security.require_admin_2fa"]
    _ints = {
        "security.pw_min_length":     (4, 128),
        "security.lockout_threshold": (0, 100),
        "security.lockout_minutes":   (1, 1440),
        "security.ratelimit_max":     (0, 1000),
        "security.ratelimit_window":  (10, 3600),
        "security.session_timeout_minutes": (1, 480),
    }
    if request.method == "POST":
        for key, (lo, hi) in _ints.items():
            raw = (request.form.get(key.replace("security.", "")) or "").strip()
            if raw:
                try:
                    models.setting_set(key, str(max(lo, min(int(raw), hi))))
                except ValueError:
                    flash(f"{key} must be a number.", "error")
        for key in _bools:
            on = request.form.get(key.replace("security.", "")) == "1"
            models.setting_set(key, "1" if on else "0")
        audit.record("config.security_policy_updated", category=audit.CAT_SECURITY,
                     target_type="settings", target_label="Security policy")
        flash("Security policy saved.", "success")
        return redirect(url_for("admin.security"))

    ctx = {k.replace("security.", ""): security.get_int(k) for k in _ints}
    for k in _bools:
        ctx[k.replace("security.", "")] = security.get_bool(k)
    return render_template("admin_security.html", **ctx)


# ----------------------------------------------------------------------
# Admin — configurable database provider + migration (#9)
# ----------------------------------------------------------------------
def _db_saved_config(pid: str) -> dict:
    raw = models.setting_get(f"db.config.{pid}")
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def _db_form_config(pid: str, form) -> dict:
    prov = db_providers.get_provider(pid)
    cfg = {}
    for fld in (prov.fields if prov else ()):
        cfg[fld] = (form.get(f"cfg_{fld}") or "").strip()
    # Preserve stored password when the field is left blank.
    if not cfg.get("password"):
        prev = _db_saved_config(pid)
        if prev.get("password"):
            cfg["password"] = prev["password"]
    return cfg


@admin_bp.route("/database", methods=["GET", "POST"])
@admin_required
def database():
    active = models.setting_get("db.target_provider") or "sqlite"

    if request.method == "POST":
        action = request.form.get("action", "save")
        pid = (request.form.get("provider") or "sqlite").strip()
        prov = db_providers.get_provider(pid)
        if prov is None:
            flash("Unknown database provider.", "error")
            return redirect(url_for("admin.database"))
        cfg = _db_form_config(pid, request.form)

        if action == "test":
            result = db_providers.test_connection(pid, cfg)
            flash(("✓ " if result["ok"] else "✗ ") + result["message"]
                  + (f" ({result['latency_ms']} ms)" if result.get("latency_ms") else ""),
                  "success" if result["ok"] else "error")

        elif action == "save":
            models.setting_set(f"db.config.{pid}", json.dumps(cfg))
            models.setting_set("db.target_provider", pid)
            audit.record("config.database_configured", category=audit.CAT_CONFIG,
                         target_type="settings", target_label=f"Database provider: {pid}",
                         new_value={k: ("***" if k == "password" else v) for k, v in cfg.items()})
            flash(f"Database configuration saved for {prov.label}.", "success")

        elif action == "migrate":
            if pid == "sqlite":
                flash("Select a target provider other than SQLite to migrate.", "error")
                return redirect(url_for("admin.database"))
            test = db_providers.test_connection(pid, cfg)
            if not test["ok"]:
                flash(f"Cannot migrate — connection failed: {test['message']}", "error")
                return redirect(url_for("admin.database"))
            run_id = db_migrate.start_migration(str(DB_PATH), pid, cfg)
            audit.record("config.database_migration_started", category=audit.CAT_CONFIG,
                         target_type="database", target_label=prov.label,
                         details={"run_id": run_id})
            flash(f"Migration to {prov.label} started.", "info")
            return redirect(url_for("admin.database", run=run_id))

        return redirect(url_for("admin.database"))

    # GET
    providers = []
    for p in db_providers.list_providers():
        providers.append({
            "id": p.id, "label": p.label, "kind": p.kind, "fields": list(p.fields),
            "available": db_providers.driver_available(p.id),
            "config": {k: ("" if k == "password" else v)
                       for k, v in _db_saved_config(p.id).items()},
            "password_set": bool(_db_saved_config(p.id).get("password")),
        })
    return render_template("admin_database.html",
                           providers=providers, active=active,
                           run_id=request.args.get("run"))


@admin_bp.route("/database/progress/<run_id>")
@admin_required
def database_progress(run_id):
    prog = db_migrate.get_progress(run_id)
    if prog is None:
        return jsonify(ok=False, error="Unknown migration run."), 404
    return jsonify(ok=True, **prog)


# ----------------------------------------------------------------------
# Admin — bulk user provisioning from CSV
# ----------------------------------------------------------------------
_BULK_HEADERS = "username,email,full_name,employee_code,role,team,password"


@admin_bp.route("/users/bulk", methods=["GET", "POST"])
@admin_required
def users_bulk():
    if request.method == "POST":
        import csv
        import io
        import secrets as _secrets

        f = request.files.get("csv")
        if not f or not f.filename:
            flash("Choose a CSV file to import.", "error")
            return redirect(url_for("admin.users_bulk"))
        try:
            text = f.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            flash("File must be UTF-8 encoded CSV.", "error")
            return redirect(url_for("admin.users_bulk"))

        reader = csv.DictReader(io.StringIO(text))
        results = {"created": 0, "updated": 0, "skipped": 0, "errors": []}
        teams_by_name = {t["name"].lower(): t["id"] for t in models.list_teams()}

        for i, raw in enumerate(reader, start=2):  # row 1 is the header
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            username = row.get("username", "")
            if not username:
                results["errors"].append(f"Row {i}: missing username — skipped.")
                results["skipped"] += 1
                continue
            if not USERNAME_RE.match(username):
                results["errors"].append(f"Row {i}: invalid username '{username}'.")
                results["skipped"] += 1
                continue
            role = row.get("role", "user").lower()
            if role not in ("user", "admin"):
                role = "user"
            team_id = None
            team_name = row.get("team", "")
            if team_name:
                team_id = teams_by_name.get(team_name.lower())
                if team_id is None:
                    results["errors"].append(f"Row {i}: unknown team '{team_name}' — user imported without a team.")
            existing = models.get_user_by_username(username)
            try:
                if existing is None:
                    pw = row.get("password") or _secrets.token_urlsafe(12)
                    force = not row.get("password")  # force change unless an explicit password was supplied
                    uid = models.create_user(
                        username, row.get("email", ""), pw, role=role,
                        created_by=current_user.id, full_name=row.get("full_name") or None,
                        employee_code=row.get("employee_code") or None,
                        team_id=team_id, approval_status="approved",
                        must_change_password=force,
                    )
                    results["created"] += 1
                else:
                    models.update_user(existing["id"], role=role, team_id=team_id,
                                       full_name=row.get("full_name") or None,
                                       employee_code=row.get("employee_code") or None,
                                       approval_status="approved")
                    results["updated"] += 1
            except Exception as exc:  # noqa: BLE001
                results["errors"].append(f"Row {i} ({username}): {exc}")
                results["skipped"] += 1

        audit.record("user.bulk_imported", category=audit.CAT_USER,
                     target_type="users", target_label="CSV bulk import",
                     new_value={k: v for k, v in results.items() if k != "errors"},
                     details={"error_count": len(results["errors"])})
        flash(f"Import complete: {results['created']} created, {results['updated']} updated, "
              f"{results['skipped']} skipped.", "success" if not results["errors"] else "info")
        return render_template("admin_users_bulk.html", results=results, headers=_BULK_HEADERS)

    return render_template("admin_users_bulk.html", results=None, headers=_BULK_HEADERS)


@admin_bp.route("/status/settings", methods=["POST"])
@admin_required
def status_settings():
    raw = (request.form.get("refresh_seconds") or "").strip()
    try:
        secs = max(5, min(int(raw), 3600))
    except ValueError:
        flash("Refresh interval must be a number of seconds.", "error")
        return redirect(url_for("admin.status"))
    models.setting_set("status.refresh_seconds", str(secs))
    audit.record("config.status_refresh_updated", category=audit.CAT_CONFIG,
                 target_type="settings", target_label="Status refresh interval",
                 new_value={"refresh_seconds": secs})
    flash(f"Auto-refresh set to every {secs} seconds.", "success")
    return redirect(url_for("admin.status"))
