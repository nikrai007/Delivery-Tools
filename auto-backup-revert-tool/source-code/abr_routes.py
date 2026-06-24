"""
AutoBackupRevert — Flask blueprint ``abr``.

Routes for the rollback-script generator: dashboard, job lifecycle
(new → review → result → download), history, and admin surfaces
(users, watched sources, logo management).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

import connectors
import core
import models
import scheduler as scheduler_mod
from constants import (
    ALLOWED_UPLOAD_EXT,
    BRAND_DIR,
    CLEANUP_MINUTES,
    DATA_DIR,
    JOB_RETENTION_DAYS,
    MAX_UPLOAD_MB,
    UPLOAD_ROOT,
)
from decorators import admin_required

log = logging.getLogger("autobackuprevert")

# AutoBackupRevert templates live one level up at auto-backup-revert-tool/templates/.
abr = Blueprint(
    "abr", __name__,
    template_folder=str(Path(__file__).resolve().parents[1] / "templates"),
)

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")


def _client_ip() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr or "")


def _new_work_dir() -> Path:
    wd = UPLOAD_ROOT / uuid.uuid4().hex
    wd.mkdir(parents=True, exist_ok=True)
    return wd


def _job_or_404(job_id: int):
    job = models.get_job(job_id)
    if job is None:
        abort(404)
    if job["user_id"] != current_user.id and not current_user.is_admin:
        abort(403)
    return job


def _preview(text: str, max_lines: int = 500) -> tuple[str, bool]:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text, False
    return ("\n".join(lines[:max_lines])
            + f"\n\n-- ... ({len(lines) - max_lines} more lines truncated in preview) ...\n", True)


def _derive_unique_procs_text(procs_file_path: Path) -> str:
    """
    Parse a PROCEDURES.txt file and produce a *distinct-names* view.

    Dedup key is ``(KIND, UPPER(NAME))`` — same key as
    ``core.collect_procedures``'s ``unique_names`` count, so the totals match.
    Output sorts by (kind, name) so the file is deterministic across re-runs.

    Pure-text derivation (no work_dir dependency) so the unique view stays
    available even after the per-job working directory has been cleaned up.
    """
    if not procs_file_path.exists():
        return ""
    content = procs_file_path.read_text(encoding="utf-8")
    seen: set[tuple[str, str]] = set()
    rows: list[tuple[str, str]] = []
    # Two-column layout: cols 0..13 are KIND (left-padded), then NAME starts at col 15.
    # KIND can be "PACKAGE BODY" (two words) — taking columns 0..13 trimmed handles that.
    name_re = re.compile(r"^\s*(\S+)")
    for line in content.splitlines():
        if not line or line.startswith("#"):
            continue
        if len(line) < 16:
            continue
        kind = line[:14].rstrip()
        rest = line[15:]
        m = name_re.match(rest)
        if not m:
            continue
        name = m.group(1)
        key = (kind, name.upper())
        if key in seen:
            continue
        seen.add(key)
        rows.append((kind, name))
    rows.sort(key=lambda r: (r[0], r[1].upper()))

    header = (
        "# Stored-code UNIQUE names (filtered from PROCEDURES.txt)\n"
        f"# {len(rows)} unique name(s)\n"
        f"# {'TYPE':<14} NAME\n"
        f"# {'-'*14} {'-'*48}\n"
    )
    if not rows:
        return header + "# (no stored-code definitions found)\n"
    return header + "\n".join(f"{k:<14} {n}" for k, n in rows) + "\n"


def _bundle_basename(enhancement: str, prod_date: str, job_id: int) -> str:
    """Slugify the enhancement name to make a safe ZIP filename."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", enhancement).strip("._-") or f"job{job_id}"
    return f"BUNDLE_{slug}_{prod_date}_job{job_id}.zip"


def _build_bundle(*, job_id: int, work_dir: Path,
                  enhancement_name: str, prod_date: str,
                  source_input_path: Path | None,
                  backup_path: Path, revert_path: Path,
                  cleanup_path: Path | None, alters_path: Path, procs_path: Path) -> Path:
    """
    Pack every artefact into a structured BUNDLE_*.zip. Returns the ZIP path.

    Layout:
        01_Backup/      — backup script
        02_Migration/   — original migration scripts (extracted dir or source file)
        03_Revert/      — revert script
        04_Drop_Backup/ — cleanup/drop-backup script (when present)
        <root>          — delete SQL, ALTER scripts, procedure definitions
    """
    bundle_path = work_dir / _bundle_basename(enhancement_name, prod_date, job_id)

    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:

        # 01_Backup
        if backup_path.exists():
            zf.write(backup_path, arcname="01_Backup/01_Backup.sql")

        # 02_Migration — prefer the extracted directory (archives); fall back to the raw source file
        extracted_dir = work_dir / "extracted"
        if extracted_dir.is_dir():
            for p in sorted(extracted_dir.rglob("*")):
                if p.is_file():
                    rel = p.relative_to(extracted_dir)
                    zf.write(p, arcname=f"02_Migration/{rel.as_posix()}")
        elif source_input_path is not None and source_input_path.exists() and source_input_path.is_file():
            zf.write(source_input_path, arcname=f"02_Migration/{source_input_path.name}")

        # 03_Revert
        if revert_path.exists():
            zf.write(revert_path, arcname="03_Revert/01_Revert.sql")

        # 04_Drop_Backup
        if cleanup_path is not None and cleanup_path.exists():
            zf.write(cleanup_path, arcname="04_Drop_Backup/01_Cleanup.sql")

        # Root level — ALTER scripts, procedure definitions
        if alters_path.exists():
            zf.write(alters_path, arcname="ALTERS.sql")
        if procs_path.exists():
            zf.write(procs_path, arcname="PROCEDURES.txt")

    return bundle_path

@abr.route("/dashboard")
@login_required
def dashboard():
    if current_user.is_admin:
        stats   = models.stats_overall()
        per_day = models.jobs_per_day(days=30)
        tables  = models.top_tables(limit=10)
        recent  = models.list_all_jobs(limit=10)
    else:
        # Both regular users and team leaders see their OWN work here.
        # Team leaders access team-wide data through the Team Dashboard.
        stats   = models.stats_for_user(current_user.id)
        per_day = models.jobs_per_day(days=30, user_id=current_user.id)
        tables  = models.top_tables(limit=10, user_id=current_user.id)
        recent  = models.list_jobs_for_user(current_user.id, limit=10)
    return render_template("dashboard.html",
                           stats=stats, per_day=per_day, top_tables=tables, recent_jobs=recent)


_PROD_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ENHANCEMENT_RE = re.compile(r"^[A-Za-z0-9._\- ]{2,80}$")


def _validate_metadata(enhancement_name: str, prod_date: str) -> str | None:
    """Return an error message if the metadata is invalid, else None."""
    if not enhancement_name:
        return "Enhancement name is required."
    if not _ENHANCEMENT_RE.match(enhancement_name):
        return ("Enhancement name must be 2–80 chars (letters, digits, dots, dashes, "
                "underscores, spaces).")
    if not prod_date:
        return "Production loading date is required."
    if not _PROD_DATE_RE.match(prod_date):
        return "Production loading date must be in YYYY-MM-DD format."
    try:
        datetime.strptime(prod_date, "%Y-%m-%d")
    except ValueError:
        return "Production loading date is not a valid calendar date."
    return None


@abr.route("/new", methods=["GET", "POST"])
@login_required
def new_job():
    if request.method == "GET":
        today = datetime.now().strftime("%Y-%m-%d")
        return render_template("upload.html", max_mb=MAX_UPLOAD_MB, today=today)

    enhancement_name = (request.form.get("enhancement_name") or "").strip()
    prod_date        = (request.form.get("prod_date") or "").strip()
    err = _validate_metadata(enhancement_name, prod_date)
    if err:
        flash(err, "error")
        return redirect(url_for("abr.new_job"))

    uploaded = request.files.get("archive")
    server_path = (request.form.get("server_path") or "").strip()

    if not uploaded and not server_path:
        flash("Provide a file to upload or a server-side path.", "error")
        return redirect(url_for("abr.new_job"))

    work_dir = _new_work_dir()
    try:
        if uploaded and uploaded.filename:
            filename = secure_filename(uploaded.filename)
            if not filename:
                raise ValueError("Empty filename after sanitisation.")
            ext = Path(filename).suffix.lower()
            if ext not in ALLOWED_UPLOAD_EXT:
                raise ValueError(f"Unsupported extension {ext or '(none)'}. Allowed: .7z .zip .sql")
            saved = work_dir / filename
            uploaded.save(saved)
            input_name = filename
            input_type = "upload_archive" if ext in {".7z", ".zip"} else "upload_sql"
            input_size = saved.stat().st_size
            src_path = saved
        else:
            # Server-side path. Mirror the source INTO work_dir so the rest of
            # the pipeline (collect_deletes / collect_alters / collect_procedures
            # / collect_triggers / _build_bundle) finds everything under one
            # known root — same shape as an uploaded archive. Without this
            # step, only collect_deletes (called once here against the external
            # path) sees the data; the review step re-scans work_dir and
            # everything else returns empty.
            src = Path(server_path).expanduser()
            if not src.exists():
                raise FileNotFoundError(f"Path not found: {server_path}")
            input_name = src.name
            if src.is_file():
                ext = src.suffix.lower()
                if ext not in ALLOWED_UPLOAD_EXT:
                    raise ValueError(
                        f"Unsupported file at server path: {ext or '(no extension)'}. "
                        f"Allowed: .7z .zip .sql"
                    )
                target = work_dir / src.name
                shutil.copy2(src, target)
                input_size = target.stat().st_size
                input_type = "server_path_archive" if ext in {".7z", ".zip"} else "server_path_sql"
                src_path = target
            elif src.is_dir():
                # Treat the directory as pre-extracted bundle contents so the
                # review step finds it via the standard work_dir/extracted/
                # lookup that already handles archives.
                target = work_dir / "extracted"
                shutil.copytree(src, target)
                # Best-effort size: sum file sizes inside the copied tree.
                input_size = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
                input_type = "server_path_dir"
                src_path = target
            else:
                raise ValueError(
                    f"Server path is neither a file nor a directory: {server_path}"
                )

        job_id = models.create_job(
            user_id=current_user.id,
            input_name=input_name, input_type=input_type, input_size_bytes=input_size,
            work_dir=str(work_dir), ip=_client_ip(),
            enhancement_name=enhancement_name, prod_date=prod_date,
            source="manual",
        )
        try:
            scan_root = core.prepare_input(src_path, work_dir)
            result = core.collect_deletes(scan_root, add_file_headers=True)
            base = Path(input_name).stem or "job"
            delete_filename = core.timestamped("delete", base)
            delete_path = work_dir / delete_filename
            delete_path.write_text(result.delete_sql, encoding="utf-8")
            models.update_job_collection(
                job_id=job_id, files=result.files_scanned,
                delete_count=result.total_deletes, warnings=result.warnings,
                delete_sql_file=str(delete_path),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Collection failed for job %s", job_id)
            models.fail_job(job_id, str(exc))
            flash(f"Collection failed: {exc}", "error")
            return redirect(url_for("abr.new_job"))
        return redirect(url_for("abr.review", job_id=job_id))
    except Exception as exc:  # noqa: BLE001
        core.safe_rmtree(work_dir)
        flash(f"Could not start job: {exc}", "error")
        return redirect(url_for("abr.new_job"))


@abr.route("/review/<int:job_id>", methods=["GET", "POST"])
@login_required
def review(job_id):
    job = _job_or_404(job_id)
    if request.method == "POST":
        if not job["delete_sql_file"] or not Path(job["delete_sql_file"]).exists():
            flash("delete.sql is missing or has been cleaned up.", "error")
            return redirect(url_for("abr.new_job"))
        delete_text = Path(job["delete_sql_file"]).read_text(encoding="utf-8")
        wd = Path(job["work_dir"])
        try:
            # Re-scan the original source so we can harvest ALTER TABLE +
            # stored-code definitions + trigger names at the same time the
            # user asks for BACKUP/REVERT.
            scan_root = wd / "extracted" if (wd / "extracted").is_dir() else wd
            alters    = core.collect_alters(scan_root, add_file_headers=True)
            procs     = core.collect_procedures(scan_root)
            triggers  = core.collect_triggers(scan_root)
            gen       = core.generate_backup_revert(delete_text, triggers=triggers)
        except Exception as exc:  # noqa: BLE001
            log.exception("Generation failed for job %s", job_id)
            models.fail_job(job_id, str(exc))
            flash(f"Generation failed: {exc}", "error")
            return redirect(url_for("abr.review", job_id=job_id))

        base = Path(job["input_name"] or "job").stem or "job"
        backup_path  = wd / core.timestamped("BACKUP",  base)
        revert_path  = wd / core.timestamped("REVERT",  base)
        cleanup_path = wd / core.timestamped("CLEANUP", base) if gen.cleanup_sql else None
        alters_path  = wd / core.timestamped("ALTERS",  base)
        procs_path   = wd / core.timestamped("PROCEDURES", base, ext=".txt")
        backup_path.write_text(gen.backup_sql, encoding="utf-8")
        revert_path.write_text(gen.revert_sql, encoding="utf-8")
        if cleanup_path is not None:
            cleanup_path.write_text(gen.cleanup_sql, encoding="utf-8")
        alters_path.write_text(alters.alter_sql or "-- No ALTER TABLE statements were found in the bundle.\n",
                               encoding="utf-8")
        procs_path.write_text(procs.procedures_text, encoding="utf-8")

        # Locate the original source the user uploaded so we can include it
        # in the bundle ZIP for archival.
        source_input_path: Path | None = None
        if job["input_name"]:
            candidate = wd / job["input_name"]
            if candidate.is_file():
                source_input_path = candidate

        bundle_path = _build_bundle(
            job_id=job_id, work_dir=wd,
            enhancement_name=job["enhancement_name"] or "job",
            prod_date=job["prod_date"] or datetime.now().strftime("%Y-%m-%d"),
            source_input_path=source_input_path,
            backup_path=backup_path, revert_path=revert_path,
            cleanup_path=cleanup_path,
            alters_path=alters_path, procs_path=procs_path,
        )

        models.update_job_generation(
            job_id=job_id,
            unique_tables=gen.unique_tables,
            revert_count=gen.revert_statements,
            extra_warnings=len(gen.warnings) + len(alters.warnings) + len(procs.warnings),
            backup_file=str(backup_path), revert_file=str(revert_path),
            cleanup_file=str(cleanup_path) if cleanup_path else None,
            alters_count=alters.total_alters, alters_file=str(alters_path),
            procedures_count=procs.total,    procedures_file=str(procs_path),
            bundle_file=str(bundle_path),
        )
        return redirect(url_for("abr.result", job_id=job_id))

    delete_text = ""
    if job["delete_sql_file"] and Path(job["delete_sql_file"]).exists():
        delete_text = Path(job["delete_sql_file"]).read_text(encoding="utf-8")
    preview, truncated = _preview(delete_text)
    return render_template("review.html", job=job, files=models.job_files(job),
                           preview=preview, truncated=truncated)


@abr.route("/result/<int:job_id>")
@login_required
def result(job_id):
    job = _job_or_404(job_id)
    if job["status"] != "generated":
        return redirect(url_for("abr.review", job_id=job_id))

    def read(col):
        p = job[col] if col in job.keys() else None
        if not p or not Path(p).exists():
            return "", False
        return _preview(Path(p).read_text(encoding="utf-8"))
    backup_preview,  backup_trunc  = read("backup_sql_file")
    revert_preview,  revert_trunc  = read("revert_sql_file")
    cleanup_preview, cleanup_trunc = read("cleanup_sql_file")
    alters_preview,  alters_trunc  = read("alters_sql_file")
    procs_preview,   procs_trunc   = read("procedures_file")

    # Distinct (UNIQUE) view of PROCEDURES.txt — derived on the fly so we
    # don't persist a second file or migrate the schema. Used by the
    # filter-toggle on the result page and by the download ?unique=1 branch.
    procs_unique_preview, procs_unique_trunc = "", False
    procs_unique_count = 0
    if job["procedures_file"]:
        procs_file_path = Path(job["procedures_file"])
        if procs_file_path.exists():
            unique_text = _derive_unique_procs_text(procs_file_path)
            if unique_text:
                procs_unique_preview, procs_unique_trunc = _preview(unique_text)
                # Count non-comment lines that have content
                procs_unique_count = sum(
                    1 for ln in unique_text.splitlines()
                    if ln and not ln.startswith("#")
                )

    # Count triggers wrapped into REVERT (cheap — just re-scans the bundle).
    trigger_count = 0
    if job["work_dir"]:
        wd = Path(job["work_dir"])
        if wd.exists():
            scan_root = wd / "extracted" if (wd / "extracted").is_dir() else wd
            trigger_count = len(core.collect_triggers(scan_root))

    return render_template("result.html", job=job,
                           backup_preview=backup_preview,   backup_trunc=backup_trunc,
                           revert_preview=revert_preview,   revert_trunc=revert_trunc,
                           cleanup_preview=cleanup_preview, cleanup_trunc=cleanup_trunc,
                           alters_preview=alters_preview,   alters_trunc=alters_trunc,
                           procs_preview=procs_preview,     procs_trunc=procs_trunc,
                           procs_unique_preview=procs_unique_preview,
                           procs_unique_trunc=procs_unique_trunc,
                           procs_unique_count=procs_unique_count,
                           trigger_count=trigger_count)


@abr.route("/download/<int:job_id>/<kind>")
@login_required
def download(job_id, kind):
    job = _job_or_404(job_id)
    col = {
        "delete":     "delete_sql_file",
        "backup":     "backup_sql_file",
        "revert":     "revert_sql_file",
        "cleanup":    "cleanup_sql_file",
        "alters":     "alters_sql_file",
        "procedures": "procedures_file",
        "bundle":     "bundle_file",
    }.get(kind)
    if col is None:
        abort(404)
    path = job[col]
    if not path or not Path(path).exists():
        flash("File no longer available (maybe cleaned up).", "error")
        return redirect(url_for("abr.history"))

    # On ?unique=1 + kind=procedures we serve the deduped view on the fly,
    # generated from the persisted PROCEDURES.txt — no separate file persisted.
    if kind == "procedures" and request.args.get("unique") in ("1", "true", "on"):
        unique_text = _derive_unique_procs_text(Path(path))
        if not unique_text:
            flash("No procedures to filter.", "warning")
            return redirect(url_for("abr.result", job_id=job_id))
        src_name = Path(path).name
        out_name = src_name.replace("PROCEDURES_", "PROCEDURES_UNIQUE_", 1) \
                   if src_name.startswith("PROCEDURES_") else "PROCEDURES_UNIQUE_" + src_name
        models.record_download(job_id, current_user.id, out_name, _client_ip())
        return Response(
            unique_text,
            mimetype="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
        )

    filename = Path(path).name
    models.record_download(job_id, current_user.id, filename, _client_ip())
    return send_file(path, as_attachment=True, download_name=filename)


@abr.route("/history")
@login_required
def history():
    q          = (request.args.get("q") or "").strip()
    prod_from  = (request.args.get("prod_from") or "").strip()
    prod_to    = (request.args.get("prod_to") or "").strip()
    status     = (request.args.get("status") or "").strip()
    # Non-admins see only their own jobs; admins can pass &all=1 to widen.
    show_all   = current_user.is_admin and request.args.get("all") == "1"
    jobs = models.search_jobs(
        user_id=None if show_all else current_user.id,
        q=q or None, prod_from=prod_from or None, prod_to=prod_to or None,
        status=status or None, limit=500,
    )
    return render_template("history.html", jobs=jobs,
                           show_user=show_all,
                           q=q, prod_from=prod_from, prod_to=prod_to,
                           status=status, show_all=show_all)


# ----------------------------------------------------------------------
# Admin — jobs / users / API tokens / API toggle
# ----------------------------------------------------------------------
@abr.route("/admin")
@admin_required
def admin_home():
    jobs  = models.list_all_jobs(limit=500)
    users = models.list_users()
    return render_template("admin.html", jobs=jobs, users=users)


@abr.route("/admin/job/<int:job_id>")
@admin_required
def admin_job(job_id):
    job = models.get_job(job_id)
    if job is None:
        abort(404)
    return render_template("admin_job.html",
                           job=job,
                           downloads=models.downloads_for_job(job_id),
                           files=models.job_files(job),
                           user=models.get_user(job["user_id"]))


@abr.route("/admin/users", methods=["GET"])
@admin_required
def admin_users():
    return render_template("admin_users.html", users=models.list_users(),
                           admin_count=models.admin_count())


@abr.route("/admin/users/create", methods=["POST"])
@admin_required
def admin_users_create():
    username = request.form.get("username", "").strip()
    email    = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    role     = request.form.get("role", "user")
    if not USERNAME_RE.match(username):
        flash("Username must be 3–32 chars, letters/digits/._- only.", "error")
        return redirect(url_for("abr.admin_users"))
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("abr.admin_users"))
    if role not in ("user", "admin"):
        flash("Invalid role.", "error")
        return redirect(url_for("abr.admin_users"))
    if models.username_exists(username):
        flash("Username is already taken.", "error")
        return redirect(url_for("abr.admin_users"))
    models.create_user(username, email, password, role=role, created_by=current_user.id)
    flash(f"User '{username}' created.", "success")
    return redirect(url_for("abr.admin_users"))


@abr.route("/admin/users/<int:uid>/edit", methods=["POST"])
@admin_required
def admin_users_edit(uid):
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
            return redirect(url_for("abr.admin_users"))
        if role != "admin":
            flash("You can't demote your own account.", "error")
            return redirect(url_for("abr.admin_users"))
    else:
        if (target["role"] == "admin" and role != "admin"
                and models.admin_count() <= 1):
            flash("Cannot demote the last active admin.", "error")
            return redirect(url_for("abr.admin_users"))
        if (target["role"] == "admin" and target["is_active"]
                and not is_active and models.admin_count() <= 1):
            flash("Cannot deactivate the last active admin.", "error")
            return redirect(url_for("abr.admin_users"))

    if role not in ("user", "admin"):
        flash("Invalid role.", "error")
        return redirect(url_for("abr.admin_users"))
    models.update_user(uid, email=email, role=role, is_active=is_active)
    flash(f"User '{target['username']}' updated.", "success")
    return redirect(url_for("abr.admin_users"))


@abr.route("/admin/users/<int:uid>/reset_password", methods=["POST"])
@admin_required
def admin_users_reset(uid):
    target = models.get_user(uid)
    if target is None:
        abort(404)
    new_password = request.form.get("new_password", "")
    if len(new_password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("abr.admin_users"))
    models.set_password(uid, new_password)
    flash(f"Password reset for '{target['username']}'.", "success")
    return redirect(url_for("abr.admin_users"))


@abr.route("/admin/users/<int:uid>/delete", methods=["POST"])
@admin_required
def admin_users_delete(uid):
    target = models.get_user(uid)
    if target is None:
        abort(404)
    if target["id"] == current_user.id:
        flash("You can't delete your own account.", "error")
        return redirect(url_for("abr.admin_users"))
    if target["role"] == "admin" and models.admin_count() <= 1:
        flash("Cannot delete the last active admin.", "error")
        return redirect(url_for("abr.admin_users"))
    models.delete_user(uid)
    flash(f"User '{target['username']}' deleted.", "success")
    return redirect(url_for("abr.admin_users"))


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


@abr.route("/admin/storage", methods=["GET", "POST"])
@admin_required
def admin_storage():
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
                    return redirect(url_for("abr.admin_storage"))
            models.setting_set("retention.auto_cleanup", auto_cleanup)
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
                    return redirect(url_for("abr.admin_storage"))
            if ttl_raw:
                try:
                    ttl = int(ttl_raw)
                    if ttl < 1:
                        raise ValueError
                    models.setting_set("auth.reset_token_ttl", str(ttl))
                except ValueError:
                    flash("Token TTL must be a positive integer (minutes).", "error")
                    return redirect(url_for("abr.admin_storage"))
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

        return redirect(url_for("abr.admin_storage"))

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


@abr.route("/admin/logo", methods=["GET"])
@admin_required
def admin_logo():
    current_logo = models.setting_get("brand.logo_filename") or ""
    preview_url  = None
    if current_logo and (BRAND_DIR / current_logo).exists():
        preview_url = url_for("static", filename=f"brand/{current_logo}")
    return render_template("admin_logo.html",
                           current_logo=current_logo,
                           preview_url=preview_url)


@abr.route("/admin/logo/upload", methods=["POST"])
@admin_required
def admin_logo_upload():
    f = request.files.get("logo")
    if not f or not f.filename:
        flash("No file selected.", "error")
        return redirect(url_for("abr.admin_logo"))
    ext = Path(secure_filename(f.filename)).suffix.lower()
    if ext not in _ALLOWED_LOGO_EXT:
        flash(
            f"Unsupported format '{ext}'. Allowed: "
            + ", ".join(sorted(_ALLOWED_LOGO_EXT)),
            "error",
        )
        return redirect(url_for("abr.admin_logo"))
    data = f.read()
    if len(data) > _MAX_LOGO_BYTES:
        flash("Logo file exceeds the 5 MB size limit.", "error")
        return redirect(url_for("abr.admin_logo"))
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"logo{ext}"
    (BRAND_DIR / filename).write_bytes(data)
    models.setting_set("brand.logo_filename", filename)
    flash("Logo updated successfully. Changes are live across the platform.", "success")
    return redirect(url_for("abr.admin_logo"))


@abr.route("/admin/logo/reset", methods=["POST"])
@admin_required
def admin_logo_reset():
    current = models.setting_get("brand.logo_filename") or ""
    if current:
        try:
            (BRAND_DIR / current).unlink(missing_ok=True)
        except OSError:
            pass
        models.setting_set("brand.logo_filename", "")
    flash("Logo reset to the built-in default.", "success")
    return redirect(url_for("abr.admin_logo"))


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


@abr.route("/admin/sources")
@admin_required
def admin_sources():
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


@abr.route("/admin/sources/new", methods=["GET", "POST"])
@admin_required
def admin_sources_new():
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
        return redirect(url_for("abr.admin_sources_new"))

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
    flash(f"Watched source '{fields['name']}' created.", "success")
    return redirect(url_for("abr.admin_sources"))


@abr.route("/admin/sources/<int:source_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_sources_edit(source_id):
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
    flash(f"Watched source '{fields['name']}' updated.", "success")
    return redirect(url_for("abr.admin_sources"))


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


@abr.route("/admin/sources/preview", methods=["POST"])
@admin_required
def admin_sources_preview():
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


@abr.route("/admin/sources/<int:source_id>/pause", methods=["POST"])
@admin_required
def admin_sources_pause(source_id):
    """Snooze a source until a chosen instant (ISO-8601 in UTC)."""
    until = (request.form.get("until") or "").strip()
    if not until:
        # Default: pause for 24 hours from now.
        until = (datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None) + timedelta(hours=24)).isoformat() + "Z"
    models.set_watched_source_pause(source_id, until)
    flash(f"Source paused until {until} (UTC).", "info")
    return redirect(url_for("abr.admin_sources"))


@abr.route("/admin/sources/<int:source_id>/resume", methods=["POST"])
@admin_required
def admin_sources_resume(source_id):
    """Clear any active pause."""
    models.set_watched_source_pause(source_id, None)
    flash("Source resumed.", "success")
    return redirect(url_for("abr.admin_sources"))


@abr.route("/admin/sources/<int:source_id>/delete", methods=["POST"])
@admin_required
def admin_sources_delete(source_id):
    source = models.get_watched_source(source_id)
    if source is None:
        abort(404)
    scheduler_mod.unschedule_source(source_id)
    models.delete_watched_source(source_id)
    flash(f"Watched source '{source['name']}' deleted.", "info")
    return redirect(url_for("abr.admin_sources"))


@abr.route("/admin/sources/<int:source_id>/run", methods=["POST"])
@admin_required
def admin_sources_run(source_id):
    source = models.get_watched_source(source_id)
    if source is None:
        abort(404)
    scheduler_mod.run_source_now(source_id)
    flash(f"Manual run triggered for '{source['name']}'. Refresh in a few seconds.", "info")
    return redirect(url_for("abr.admin_sources"))

def cleanup_loop():
    while True:
        try:
            # Re-read from DB every cycle so changes take effect without restart
            auto_cleanup  = models.setting_get("retention.auto_cleanup", "1") == "1"
            retention_days = _retention_days()

            now = time.time()
            known = set()
            for row in models.list_all_jobs(limit=10_000):
                if row["work_dir"]:
                    known.add(str(Path(row["work_dir"]).resolve()))

            # Always sweep orphaned (no matching job) upload dirs
            for child in UPLOAD_ROOT.iterdir():
                try:
                    if not child.is_dir():
                        continue
                    if str(child.resolve()) not in known and (now - child.stat().st_mtime) / 60 > CLEANUP_MINUTES:
                        core.safe_rmtree(child)
                except OSError:
                    continue

            # Retention-based cleanup only when enabled
            if auto_cleanup:
                for job_id, wd in models.expired_job_work_dirs(retention_days):
                    p = Path(wd)
                    if p.exists():
                        core.safe_rmtree(p)
                    models.clear_job_workdir(job_id)

        except Exception:  # noqa: BLE001
            log.exception("cleanup_loop iteration failed")
        time.sleep(max(60, CLEANUP_MINUTES * 60 // 2))


def start_workers(app):
    """Start the upload-cleanup thread and boot APScheduler. Called once by
    the application factory after the DB is initialised."""
    threading.Thread(target=cleanup_loop, name="cleanup", daemon=True).start()
    scheduler_mod.init_scheduler(app)
