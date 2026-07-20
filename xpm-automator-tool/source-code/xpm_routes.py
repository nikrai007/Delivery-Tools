"""
XPM Automator — Flask blueprint ``xpm``.

An enterprise-grade web front end for the standalone XPM migration-script
uploader. Routes:

    /dashboard                    stats + recent activity
    /new           GET/POST       upload files (or batch-range download) → start a run
    /run/<id>                     live status page (polls status.json)
    /run/<id>/status.json         live progress JSON
    /run/<id>/batch-number POST   edit the run's Batch Number
    /run/<id>/cancel       POST   cooperative cancel
    /download/<id>                download the produced script (named by Batch Number)
    /history                      searchable / filterable / sortable / paginated log
    /history/export.csv           export the current filter as CSV
    /detail/<id>                  full run detail (files, timeline, config)

Zero platform impact: owns its own DB tables (``xpm_store``) and templates; the
only shared touch-points are the ``models``/``constants``/``audit`` helpers it
reuses read-only.
"""

from __future__ import annotations

import csv
import io
import logging
import uuid
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

import audit
import models
import xpm_store as store
import xpm_service
from constants import MAX_UPLOAD_MB, UPLOAD_ROOT
from xpm_core import batch as batch_mod
from xpm_core.client import XPMError
from xpm_core.config import DEFAULTS, SETTING_KEYS, XPMConfig
from xpm_core.pipeline import TERMINAL, registry

log = logging.getLogger("xpm")

_HERE = Path(__file__).resolve().parents[1]  # xpm-automator-tool/

xpm_bp = Blueprint(
    "xpm", __name__,
    url_prefix="/tools/xpm-automator",
    template_folder=str(_HERE / "templates"),
)

ALLOWED_EXT = {".sql", ".txt"}
PER_PAGE = 25
STATUS_CHOICES = ["uploaded", "processing", "completed", "failed", "cancelled"]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _client_ip() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr or "")


def _new_work_dir() -> Path:
    wd = UPLOAD_ROOT / "xpm" / uuid.uuid4().hex
    wd.mkdir(parents=True, exist_ok=True)
    return wd


def _run_or_404(run_id: int):
    run = store.get_run(run_id)
    if run is None:
        abort(404)
    if run["user_id"] != current_user.id and not current_user.is_admin:
        abort(403)
    return run


def _saved_defaults() -> dict:
    """Admin-saved non-secret defaults, falling back to package defaults."""
    d = dict(DEFAULTS)
    for key, skey in SETTING_KEYS.items():
        val = models.setting_get(skey)
        if val:
            d[key] = val
    return d


def _persist_defaults(cfg: XPMConfig) -> None:
    """Remember the non-secret config so the next run pre-fills it."""
    snap = cfg.redacted()
    for key, skey in SETTING_KEYS.items():
        try:
            models.setting_set(skey, str(snap.get(key, "")))
        except Exception:  # noqa: BLE001
            log.exception("Failed to persist xpm default %s", skey)


def _download_name(run) -> str:
    """Filename for a served artefact — derived from the current Batch Number."""
    fbn = run["final_batch_number"] or run["batch_number"]
    ext = ".sql"
    if run["output_name"]:
        ext = Path(run["output_name"]).suffix or ".sql"
    return f"{batch_mod.slug_for_filename(fbn)}{ext}"


# ----------------------------------------------------------------------
# Dashboard
# ----------------------------------------------------------------------
@xpm_bp.route("/")
@login_required
def index():
    return redirect(url_for("xpm.dashboard"))


@xpm_bp.route("/dashboard")
@login_required
def dashboard():
    scope = None if current_user.is_admin else current_user.id
    stats = store.dashboard_stats(scope)
    per_day = store.runs_per_day(days=14, user_id=scope)
    recent = store.list_recent(scope, limit=8)
    return render_template("xpm_dashboard.html", stats=stats, per_day=per_day,
                           recent=recent, is_admin_scope=(scope is None))


# ----------------------------------------------------------------------
# New run (upload or batch-download)
# ----------------------------------------------------------------------
@xpm_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_run():
    if request.method == "GET":
        return render_template("xpm_upload.html", cfg=_saved_defaults(),
                               max_mb=MAX_UPLOAD_MB, allowed=", ".join(sorted(ALLOWED_EXT)))

    mode = (request.form.get("mode") or "upload").strip()
    defaults = _saved_defaults()
    cfg = XPMConfig.from_form(request.form, defaults=defaults)

    errs = cfg.validate(require_password=True)
    if errs:
        for e in errs:
            flash(e, "error")
        return redirect(url_for("xpm.new_run"))

    if mode == "batch_download":
        return _start_batch_download(cfg)
    return _start_upload(cfg)


def _start_upload(cfg: XPMConfig):
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        flash("Add at least one .sql or .txt migration script to upload.", "error")
        return redirect(url_for("xpm.new_run"))

    work_dir = _new_work_dir()
    saved_paths: list[str] = []
    try:
        for f in files:
            name = secure_filename(f.filename)
            if not name:
                raise ValueError(f"Invalid filename: {f.filename!r}")
            ext = Path(name).suffix.lower()
            if ext not in ALLOWED_EXT:
                raise ValueError(f"Unsupported file '{name}'. Allowed: {', '.join(sorted(ALLOWED_EXT))}")
            dest = work_dir / name
            # Avoid collisions when two selected files share a name.
            n = 2
            while dest.exists():
                dest = work_dir / f"{Path(name).stem}_{n}{ext}"
                n += 1
            f.save(dest)
            if dest.stat().st_size == 0:
                raise ValueError(f"'{name}' is empty.")
            saved_paths.append(str(dest))
    except Exception as exc:  # noqa: BLE001
        _safe_rmtree(work_dir)
        flash(f"Upload rejected: {exc}", "error")
        return redirect(url_for("xpm.new_run"))

    # Deterministic XPM batch order: numeric-prefix natural sort (01_ before 10_).
    saved_paths.sort(key=lambda p: xpm_service.natural_key(Path(p).name))

    batch_number = batch_mod.generate()
    run_id = store.create_run(
        batch_number=batch_number, mode="upload",
        user_id=current_user.id, username=current_user.username,
        user_email=getattr(current_user, "email", None),
        cfg_snapshot=cfg.redacted(), ip=_client_ip(),
        work_dir=str(work_dir), file_count=len(saved_paths),
    )
    for i, p in enumerate(saved_paths, start=1):
        store.add_run_file(run_id, Path(p).name, Path(p).stat().st_size, i)

    _persist_defaults(cfg)
    audit.record("xpm.upload_started", category=audit.CAT_TOOL,
                 target_type="xpm_run", target_id=run_id, target_label=batch_number,
                 details={"files": len(saved_paths), "project": cfg.project_name})

    xpm_service.start_upload_run(run_id, cfg, saved_paths, str(work_dir))
    flash(f"Upload started — Batch Number {batch_number}.", "success")
    return redirect(url_for("xpm.run_status", run_id=run_id))


def _start_batch_download(cfg: XPMConfig):
    def _int(name):
        raw = (request.form.get(name) or "").strip()
        return int(raw) if raw.isdigit() else None

    bf, bt = _int("batch_from"), _int("batch_to")
    if bf is None or bt is None:
        flash("Enter both a numeric From and To batch number.", "error")
        return redirect(url_for("xpm.new_run"))
    if bf > bt:
        flash(f"From batch (#{bf}) must be ≤ To batch (#{bt}).", "error")
        return redirect(url_for("xpm.new_run"))

    work_dir = _new_work_dir()
    batch_number = batch_mod.generate()
    run_id = store.create_run(
        batch_number=batch_number, mode="batch_download",
        user_id=current_user.id, username=current_user.username,
        user_email=getattr(current_user, "email", None),
        cfg_snapshot=cfg.redacted(), ip=_client_ip(),
        work_dir=str(work_dir), file_count=0, batch_from=bf, batch_to=bt,
    )
    _persist_defaults(cfg)
    audit.record("xpm.batch_download_started", category=audit.CAT_TOOL,
                 target_type="xpm_run", target_id=run_id, target_label=batch_number,
                 details={"from": bf, "to": bt, "project": cfg.project_name})

    xpm_service.start_batch_download_run(run_id, cfg, bf, bt, str(work_dir))
    flash(f"Batch download started — Batch Number {batch_number}.", "success")
    return redirect(url_for("xpm.run_status", run_id=run_id))


# ----------------------------------------------------------------------
# Run status (live)
# ----------------------------------------------------------------------
@xpm_bp.route("/run/<int:run_id>")
@login_required
def run_status(run_id):
    run = _run_or_404(run_id)
    files = store.get_run_files(run_id)
    return render_template("xpm_run.html", run=run, files=files,
                           terminal=(run["status"] in TERMINAL))


@xpm_bp.route("/run/<int:run_id>/status.json")
@login_required
def run_status_json(run_id):
    run = _run_or_404(run_id)
    rp = registry.get(run_id)
    if rp is not None:
        payload = rp.public()
    else:
        # Terminal run whose live entry was reaped — reconstruct from the DB.
        import json
        try:
            steps = json.loads(run["log_json"] or "[]")
        except (TypeError, ValueError):
            steps = []
        payload = {
            "run_id": run_id, "status": run["status"], "phase": "done",
            "percent": 100 if run["status"] == "completed" else 0,
            "message": run["remarks"] or run["error_message"] or run["status"].title(),
            "steps": steps,
            "files": [{"name": f["filename"], "status": f["status"], "error": f["error"]}
                      for f in store.get_run_files(run_id)],
            "cancel_requested": False, "done": run["status"] in TERMINAL,
        }
    payload["can_download"] = bool(run["output_file"] and run["status"] == "completed")
    payload["download_url"] = url_for("xpm.download", run_id=run_id)
    return jsonify(payload)


@xpm_bp.route("/run/<int:run_id>/batch-number", methods=["POST"])
@login_required
def edit_batch_number(run_id):
    run = _run_or_404(run_id)
    value = batch_mod.normalise(request.form.get("final_batch_number") or "")
    wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not batch_mod.is_valid(value):
        msg = "Batch Number must be 3–64 chars (letters, digits, dot, dash, underscore)."
        if wants_json:
            return jsonify(ok=False, error=msg), 400
        flash(msg, "error")
        return redirect(url_for("xpm.run_status", run_id=run_id))
    old = run["final_batch_number"]
    store.set_final_batch_number(run_id, value)
    audit.record("xpm.batch_number_edited", category=audit.CAT_TOOL,
                 target_type="xpm_run", target_id=run_id, target_label=value,
                 old_value={"batch": old}, new_value={"batch": value})
    if wants_json:
        return jsonify(ok=True, final_batch_number=value)
    flash("Batch Number updated.", "success")
    return redirect(url_for("xpm.run_status", run_id=run_id))


@xpm_bp.route("/run/<int:run_id>/cancel", methods=["POST"])
@login_required
def cancel_run(run_id):
    _run_or_404(run_id)
    ok = registry.request_cancel(run_id)
    if ok:
        flash("Cancellation requested — finishing the current step…", "warning")
    else:
        flash("Run already finished; nothing to cancel.", "error")
    return redirect(url_for("xpm.run_status", run_id=run_id))


# ----------------------------------------------------------------------
# Download
# ----------------------------------------------------------------------
@xpm_bp.route("/download/<int:run_id>")
@login_required
def download(run_id):
    run = _run_or_404(run_id)
    path = run["output_file"]
    if not path or not Path(path).exists():
        flash("The produced file is no longer available (it may have been cleaned up).", "error")
        return redirect(url_for("xpm.run_status", run_id=run_id))
    store.record_download(run_id)
    audit.record("xpm.download", category=audit.CAT_TOOL,
                 target_type="xpm_run", target_id=run_id,
                 target_label=run["final_batch_number"] or run["batch_number"])
    return send_file(path, as_attachment=True, download_name=_download_name(run))


# ----------------------------------------------------------------------
# History (Processing History / Upload Logs)
# ----------------------------------------------------------------------
def _history_filters():
    return {
        "q": (request.args.get("q") or "").strip(),
        "status": (request.args.get("status") or "").strip(),
        "batch": (request.args.get("batch") or "").strip(),
        "username": (request.args.get("user") or "").strip(),
        "date_from": (request.args.get("date_from") or "").strip(),
        "date_to": (request.args.get("date_to") or "").strip(),
    }


@xpm_bp.route("/history")
@login_required
def history():
    show_all = current_user.is_admin and request.args.get("all") == "1"
    scope = None if show_all else current_user.id
    f = _history_filters()
    sort = request.args.get("sort") or "created_at"
    direction = request.args.get("dir") or "desc"
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1

    total = store.count_runs(user_id=scope, q=f["q"] or None, status=f["status"] or None,
                             date_from=f["date_from"] or None, date_to=f["date_to"] or None,
                             batch=f["batch"] or None, username=f["username"] or None)
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = min(page, pages)
    runs = store.search_runs(user_id=scope, q=f["q"] or None, status=f["status"] or None,
                             date_from=f["date_from"] or None, date_to=f["date_to"] or None,
                             batch=f["batch"] or None, username=f["username"] or None,
                             sort=sort, direction=direction,
                             limit=PER_PAGE, offset=(page - 1) * PER_PAGE)
    return render_template(
        "xpm_history.html", runs=runs, filters=f, sort=sort, direction=direction,
        page=page, pages=pages, total=total, show_all=show_all,
        status_choices=STATUS_CHOICES, users=store.distinct_usernames(scope),
    )


@xpm_bp.route("/history/export.csv")
@login_required
def export_csv():
    show_all = current_user.is_admin and request.args.get("all") == "1"
    scope = None if show_all else current_user.id
    f = _history_filters()
    runs = store.search_runs(user_id=scope, q=f["q"] or None, status=f["status"] or None,
                             date_from=f["date_from"] or None, date_to=f["date_to"] or None,
                             batch=f["batch"] or None, username=f["username"] or None,
                             sort=request.args.get("sort") or "created_at",
                             direction=request.args.get("dir") or "desc",
                             limit=100000, offset=0)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Upload Date/Time", "User", "User Email", "Generated Batch Number",
                "Final Batch Number", "Mode", "Files", "Uploaded", "Failed",
                "Status", "Duration (s)", "Download Count", "Last Download",
                "Output File", "Error", "Remarks"])
    for r in runs:
        dur = "" if r["duration_ms"] is None else f"{r['duration_ms'] / 1000:.1f}"
        w.writerow([
            (r["created_at"] or "").replace("T", " ").rstrip("Z"),
            r["username"] or "", r["user_email"] or "",
            r["batch_number"] or "", r["final_batch_number"] or "",
            r["mode"] or "", r["file_count"], r["files_uploaded"], r["files_failed"],
            r["status"] or "", dur, r["download_count"] or 0,
            (r["last_download_at"] or "").replace("T", " ").rstrip("Z"),
            r["output_name"] or "", r["error_message"] or "", r["remarks"] or "",
        ])
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="xpm_processing_history.csv"'},
    )


# ----------------------------------------------------------------------
# Detail
# ----------------------------------------------------------------------
@xpm_bp.route("/detail/<int:run_id>")
@login_required
def detail(run_id):
    run = _run_or_404(run_id)
    files = store.get_run_files(run_id)
    import json
    try:
        steps = json.loads(run["log_json"] or "[]")
    except (TypeError, ValueError):
        steps = []
    rp = registry.get(run_id)
    if rp is not None and rp.steps:
        steps = list(rp.steps)  # prefer live timeline while the run is in flight
    return render_template("xpm_detail.html", run=run, files=files, steps=steps)


# ----------------------------------------------------------------------
# Live config discovery — Fetch projects / processes from XPM (JSON, AJAX)
# ----------------------------------------------------------------------
def _cfg_from_json():
    data = request.get_json(silent=True) or {}
    return XPMConfig.from_form(data, defaults=_saved_defaults())


@xpm_bp.route("/api/projects", methods=["POST"])
@login_required
def api_projects():
    cfg = _cfg_from_json()
    errs = cfg.validate(require_password=True)
    if errs:
        return jsonify(ok=False, error=" ".join(errs)), 400
    try:
        return jsonify(ok=True, projects=xpm_service.fetch_projects(cfg))
    except XPMError as exc:
        return jsonify(ok=False, error=str(exc)), 502
    except Exception as exc:  # noqa: BLE001
        log.exception("Fetch projects failed")
        return jsonify(ok=False, error=f"Could not load projects: {exc}"), 500


@xpm_bp.route("/api/processes", methods=["POST"])
@login_required
def api_processes():
    cfg = _cfg_from_json()
    errs = cfg.validate(require_password=True)
    if errs:
        return jsonify(ok=False, error=" ".join(errs)), 400
    if not str(cfg.project_id or "").strip():
        return jsonify(ok=False, error="Select a project first."), 400
    try:
        return jsonify(ok=True, processes=xpm_service.fetch_processes(cfg))
    except XPMError as exc:
        return jsonify(ok=False, error=str(exc)), 502
    except Exception as exc:  # noqa: BLE001
        log.exception("Fetch processes failed")
        return jsonify(ok=False, error=f"Could not load processes: {exc}"), 500


@xpm_bp.route("/api/batches", methods=["POST"])
@login_required
def api_batches():
    """Live list of every script in the project (newest first) as JSON — powers
    the Batch Explorer grid. Reuses the same ``list_batches`` service."""
    cfg = _cfg_from_json()
    errs = cfg.validate(require_password=True)
    if errs:
        return jsonify(ok=False, error=" ".join(errs)), 400
    try:
        batches = xpm_service.list_batches(cfg)
        _persist_defaults(cfg)
        audit.record("xpm.explorer_viewed", category=audit.CAT_TOOL,
                     details={"project": cfg.project_name, "count": len(batches)})
        return jsonify(ok=True, batches=batches, project=cfg.project_name)
    except XPMError as exc:
        return jsonify(ok=False, error=str(exc)), 502
    except Exception as exc:  # noqa: BLE001
        log.exception("Fetch batches failed")
        return jsonify(ok=False, error=f"Could not load batches: {exc}"), 500


# ----------------------------------------------------------------------
# Batch Explorer — live browse of every script currently in the project
# ----------------------------------------------------------------------
@xpm_bp.route("/explorer")
@login_required
def explorer():
    """Batch Explorer shell. The grid is populated client-side via POST /api/batches
    (reusing the same project/process picker as the upload form)."""
    return render_template("xpm_explorer.html", cfg=_saved_defaults())


# ----------------------------------------------------------------------
def _safe_rmtree(path: Path) -> None:
    import shutil
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass
