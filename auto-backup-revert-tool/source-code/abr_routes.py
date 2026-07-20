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
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

import core
import models
import scheduler as scheduler_mod
from constants import (
    ALLOWED_UPLOAD_EXT,
    CLEANUP_MINUTES,
    JOB_RETENTION_DAYS,
    MAX_UPLOAD_MB,
    UPLOAD_ROOT,
)

# NOTE: the platform admin screens (users/storage/logo/watched-sources/activity)
# moved to admin-console/source-code/admin_console_routes.py (endpoints admin.*).

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
# NOTE: all /admin/* screens (user activity, users, storage, branding,
# watched sources) now live in the platform Admin Console blueprint:
#   admin-console/source-code/admin_console_routes.py  (endpoints admin.*)
# Only the retention helper stays here — the background cleanup loop needs it.
# ----------------------------------------------------------------------
def _retention_days() -> int:
    """Read retention days from DB settings, falling back to the env constant."""
    raw = models.setting_get("retention.days")
    try:
        return max(1, int(raw)) if raw else JOB_RETENTION_DAYS
    except ValueError:
        return JOB_RETENTION_DAYS


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
