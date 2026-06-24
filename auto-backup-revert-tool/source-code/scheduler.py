"""
Watched-source scheduler — Phase 2 & 3 orchestrator.

Public entry points:

  init_scheduler(app)              -> wire APScheduler into the Flask app's
                                       lifecycle. Idempotent.
  reload_source(source_id)         -> (re)register a single watched source
                                       so config changes take effect without
                                       restarting the app.
  unschedule_source(source_id)     -> remove a source from the scheduler
                                       (e.g., on delete or disable).
  run_source_now(source_id)        -> manual fire from the admin UI.

A scheduled fire calls ``_run_source`` which:
  1. Asks the connector for candidate files in the watched source.
  2. SHA-256-hashes each candidate, dedups against ``processed_files``.
  3. For each new file, creates a job (source='scheduler', enhancement_name=
     parent-folder-name, prod_date=today), runs collect + generate, packs the
     bundle ZIP, and asks the connector to deliver the bundle to the
     destination path.
  4. Records the run outcome on watched_sources.last_run_*.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytz   # bundled with APScheduler — used for IANA-name lookup via pytz.timezone(name)

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import connectors
import core
import models


log = logging.getLogger("autobackuprevert.scheduler")
_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()
_app_ref = None  # set by init_scheduler so background jobs can access app state


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------
def init_scheduler(app) -> None:
    """Boot APScheduler and register every enabled watched source."""
    global _scheduler, _app_ref
    if _scheduler is not None:
        return
    _app_ref = app
    _scheduler = BackgroundScheduler(timezone="UTC", daemon=True)
    _scheduler.start()
    for row in models.list_watched_sources():
        if row["enabled"]:
            _register(row)
    log.info("Scheduler started with %d active source(s)", len(_scheduler.get_jobs()))


def reload_source(source_id: int) -> None:
    """(Re)schedule a single source. Safe to call after create / edit."""
    with _lock:
        if _scheduler is None:
            return
        try:
            _scheduler.remove_job(_job_id(source_id))
        except Exception:  # noqa: BLE001
            pass
        row = models.get_watched_source(source_id)
        if row and row["enabled"]:
            _register(row)


def unschedule_source(source_id: int) -> None:
    with _lock:
        if _scheduler is None:
            return
        try:
            _scheduler.remove_job(_job_id(source_id))
        except Exception:  # noqa: BLE001
            pass


def run_source_now(source_id: int) -> None:
    """Fire a source's processing immediately (admin 'Run now' button)."""
    threading.Thread(
        target=_run_source, args=(source_id,),
        name=f"manual-run-{source_id}", daemon=True,
    ).start()


# ----------------------------------------------------------------------
# Trigger construction (rich schedule_json — preferred)
# ----------------------------------------------------------------------
DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_LABELS = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu",
              "fri": "Fri", "sat": "Sat", "sun": "Sun"}
PRESETS = {
    "every_day":     list(DAY_KEYS),
    "weekdays":      ["mon", "tue", "wed", "thu", "fri"],
    "weekends":      ["sat", "sun"],
    "mideast_week":  ["sun", "mon", "tue", "wed", "thu"],
}


def _tz(name: str | None):
    """Resolve a TZ name to a pytz timezone (APScheduler accepts pytz)."""
    name = (name or "UTC").strip() or "UTC"
    try:
        return pytz.timezone(name)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Unknown timezone: {name!r}") from exc


def _validate_dates(start_date: str | None, end_date: str | None) -> tuple[datetime | None, datetime | None]:
    """Parse optional YYYY-MM-DD bounds; ensure end >= start."""
    def parse(s: str | None):
        if not s:
            return None
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"Invalid date: {s!r} (want YYYY-MM-DD)") from exc
    sd = parse(start_date)
    ed = parse(end_date)
    if sd and ed and ed < sd:
        raise ValueError("end_date must be on or after start_date")
    return sd, ed


def _parse_times(times: list[str]) -> tuple[list[str], list[str]]:
    """Parse ['09:00','14:00'] → (['9','14'], ['0','0'])."""
    if not times:
        raise ValueError("at least one time slot (HH:MM) is required")
    hours, mins = [], []
    for s in times:
        s = (s or "").strip()
        m = re.match(r"^(\d{1,2}):(\d{2})$", s)
        if not m:
            raise ValueError(f"Invalid time slot: {s!r} (want HH:MM)")
        h, mi = int(m.group(1)), int(m.group(2))
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            raise ValueError(f"Out-of-range time: {s!r}")
        hours.append(str(h)); mins.append(str(mi))
    return hours, mins


def _days_to_dow(days: list[str]) -> str:
    """Convert ['mon','wed','fri'] → cron 'mon,wed,fri'; empty list → '*'."""
    if not days:
        return "*"
    bad = [d for d in days if d not in DAY_KEYS]
    if bad:
        raise ValueError(f"Invalid day keys: {bad}")
    # Preserve canonical order
    return ",".join(d for d in DAY_KEYS if d in days)


def build_trigger_from_json(schedule: dict):
    """
    Build an APScheduler trigger from the rich schedule v2 dict.

      mode='weekly_at':   CronTrigger(day_of_week=…, hour=…, minute=…, …)
      mode='every_minutes': CronTrigger(minute='*/N', day_of_week=…, …)
                            (uses cron instead of IntervalTrigger so day-of-week
                            and date-range filters stay native to APScheduler)
      mode='cron':        CronTrigger.from_crontab(expr) + TZ + date bounds
    """
    mode = (schedule.get("mode") or "").strip()
    tz   = _tz(schedule.get("timezone"))
    days = schedule.get("days") or []
    sd, ed = _validate_dates(schedule.get("start_date"), schedule.get("end_date"))

    if mode == "weekly_at":
        hours, mins = _parse_times(schedule.get("times") or [])
        dow = _days_to_dow(days)
        return CronTrigger(
            day_of_week=dow,
            hour=",".join(hours),
            minute=",".join(mins),
            timezone=tz, start_date=sd, end_date=ed,
        )

    if mode == "every_minutes":
        n = int(schedule.get("interval_minutes") or 0)
        # Cap at 12 hours (720 min). For any cadence longer than that — daily,
        # every-other-day, etc. — use mode='weekly_at' with specific times so
        # the fire instant is deterministic instead of "the next divisor of
        # 24 hours". The cap also prevents the cron-generation pitfall where
        # interval_minutes >= 24h would emit hour='*/24' (out of cron range).
        if n < 1 or n > 720:
            raise ValueError(
                "interval_minutes must be between 1 and 720 (i.e. up to every 12 hours). "
                "For once-daily or longer cadence, switch to 'weekly_at' mode with a fixed time."
            )
        dow = _days_to_dow(days)
        # CronTrigger with minute='*/N'. For N that doesn't divide 60 (e.g., 7,
        # 13) cron will re-anchor at the top of each hour — acceptable for
        # business polling cadence.
        return CronTrigger(
            day_of_week=dow,
            minute=f"*/{n}" if n <= 59 else "0",
            hour=("*" if n <= 59 else f"*/{max(1, n // 60)}"),
            timezone=tz, start_date=sd, end_date=ed,
        )

    if mode == "cron":
        expr = (schedule.get("cron_expr") or "").strip()
        if not expr:
            raise ValueError("cron_expr is required for mode='cron'")
        base = CronTrigger.from_crontab(expr, timezone=tz)
        # APScheduler doesn't accept start/end_date on from_crontab; re-wrap.
        return CronTrigger(
            year=base.fields[0], month=base.fields[1], day=base.fields[2],
            week=base.fields[3], day_of_week=base.fields[4],
            hour=base.fields[5], minute=base.fields[6], second=base.fields[7],
            timezone=tz, start_date=sd, end_date=ed,
        )

    raise ValueError(f"Unknown schedule mode: {mode!r}")


def describe_schedule(schedule: dict) -> str:
    """Human-readable cadence label, e.g. 'Mon–Fri at 09:00, 14:00 · Asia/Singapore'."""
    if not schedule:
        return "(unset)"
    mode = schedule.get("mode")
    tz   = schedule.get("timezone") or "UTC"
    days = schedule.get("days") or []

    # Compact day label
    if not days or set(days) == set(DAY_KEYS):
        day_label = "Every day"
    elif set(days) == set(PRESETS["weekdays"]):
        day_label = "Mon–Fri"
    elif set(days) == set(PRESETS["weekends"]):
        day_label = "Sat–Sun"
    elif set(days) == set(PRESETS["mideast_week"]):
        day_label = "Sun–Thu"
    else:
        day_label = ", ".join(DAY_LABELS[d] for d in DAY_KEYS if d in days)

    if mode == "weekly_at":
        times = ", ".join(schedule.get("times") or [])
        return f"{day_label} at {times} · {tz}"
    if mode == "every_minutes":
        n = int(schedule.get("interval_minutes") or 0)
        if n % 60 == 0 and n:
            cad = f"every {n // 60} hour(s)"
        else:
            cad = f"every {n} min"
        return f"{day_label} · {cad} · {tz}"
    if mode == "cron":
        return f"cron `{schedule.get('cron_expr')}` · {tz}"
    return "(invalid schedule)"


def next_fires(schedule: dict, n: int = 5) -> list[str]:
    """Compute the next ``n`` UTC fire times for a schedule. Used by the form preview."""
    try:
        trig = build_trigger_from_json(schedule)
    except Exception as exc:  # noqa: BLE001
        return [f"⚠️ invalid schedule: {exc}"]
    out = []
    # Respect pause_until — skip any computed fire that would be before pause expiry.
    pause_iso = (schedule.get("pause_until") or "").strip()
    pause_until = None
    if pause_iso:
        try:
            pause_until = datetime.fromisoformat(pause_iso.replace("Z", "+00:00"))
        except ValueError:
            pause_until = None
    cursor = datetime.now(timezone.utc)
    fires_collected = 0
    safety = 0
    while fires_collected < n and safety < 100:
        safety += 1
        nxt = trig.get_next_fire_time(None, cursor)
        if nxt is None:
            break
        if pause_until and nxt < (pause_until if pause_until.tzinfo else pytz.UTC.localize(pause_until)):
            cursor = nxt + timedelta(seconds=1)
            continue
        out.append(nxt.astimezone(pytz.UTC).strftime("%Y-%m-%d %H:%M %Z"))
        cursor = nxt + timedelta(seconds=1)
        fires_collected += 1
    return out


# ----------------------------------------------------------------------
# Legacy v1 trigger (kept so older rows without schedule_json still work)
# ----------------------------------------------------------------------
def build_trigger(interval_kind: str, interval_value: str):
    """Legacy v1 trigger (pre-schedule_json). Retained for backward-compat only."""
    if interval_kind == "every_minutes":
        n = max(1, int(interval_value))
        return IntervalTrigger(minutes=n)
    if interval_kind == "daily_at":
        slots = [s.strip() for s in (interval_value or "").split(",") if s.strip()]
        if not slots:
            raise ValueError("daily_at needs at least one HH:MM slot")
        hours, mins = [], []
        for s in slots:
            m = re.match(r"^(\d{1,2}):(\d{2})$", s)
            if not m:
                raise ValueError(f"Invalid time slot: {s!r}")
            h, mi = int(m.group(1)), int(m.group(2))
            if not (0 <= h <= 23 and 0 <= mi <= 59):
                raise ValueError(f"Out-of-range time: {s!r}")
            hours.append(str(h)); mins.append(str(mi))
        return CronTrigger(hour=",".join(hours), minute=",".join(mins))
    if interval_kind == "cron":
        return CronTrigger.from_crontab(interval_value)
    raise ValueError(f"Unknown interval_kind: {interval_kind!r}")


def describe_interval(interval_kind: str, interval_value: str) -> str:
    """Legacy v1 description (only used for rows without schedule_json)."""
    if interval_kind == "every_minutes":
        try:
            n = int(interval_value)
        except ValueError:
            return f"every {interval_value} minutes"
        if n % 1440 == 0:
            return f"every {n // 1440} day(s)"
        if n % 60 == 0:
            return f"every {n // 60} hour(s)"
        return f"every {n} minute(s)"
    if interval_kind == "daily_at":
        return f"daily at {interval_value}"
    if interval_kind == "cron":
        return f"cron `{interval_value}`"
    return f"{interval_kind}={interval_value}"


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------
def _job_id(source_id: int) -> str:
    return f"watched-source-{source_id}"


def _register(row) -> None:
    """
    Register a watched source with APScheduler.

    Prefers the rich v2 ``schedule_json`` column when present, falls back to
    the legacy ``interval_kind`` / ``interval_value`` text columns for rows
    created before the v2 migration.
    """
    try:
        sj = row["schedule_json"] if "schedule_json" in row.keys() else None
        if sj:
            trigger = build_trigger_from_json(json.loads(sj))
        else:
            trigger = build_trigger(row["interval_kind"], row["interval_value"])
    except Exception as exc:  # noqa: BLE001
        log.warning("Skipping source %s (%s): bad trigger — %s",
                    row["id"], row["name"], exc)
        return
    _scheduler.add_job(
        _run_source, trigger=trigger,
        args=[row["id"]], id=_job_id(row["id"]),
        replace_existing=True, max_instances=1, coalesce=True,
        misfire_grace_time=300,
    )


def _is_paused(row) -> tuple[bool, str | None]:
    """
    Inspect schedule_json.pause_until. Return (paused, reason).
    Pause expires when its timestamp falls into the past.
    """
    sj = row["schedule_json"] if "schedule_json" in row.keys() else None
    if not sj:
        return False, None
    try:
        sched = json.loads(sj)
    except Exception:  # noqa: BLE001
        return False, None
    raw = (sched.get("pause_until") or "").strip()
    if not raw:
        return False, None
    try:
        # Accept ISO with or without explicit timezone — treat naive as UTC.
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return False, None
    now = datetime.now(timezone.utc)
    if ts > now:
        return True, f"paused until {raw}"
    return False, None


def _run_source(source_id: int) -> None:
    """The work that fires on each scheduler tick (and from 'Run now')."""
    row = models.get_watched_source(source_id)
    if row is None or not row["enabled"]:
        return

    paused, reason = _is_paused(row)
    if paused:
        log.info("Source %s (%s) is %s — skipping fire", source_id, row["name"], reason)
        models.record_watched_source_run(source_id, "no_new_files", reason)
        return

    log.info("Scheduler firing source %s (%s)", source_id, row["name"])
    cfg = connectors.parse_config(row["config_json"])
    try:
        connector = connectors.get_connector(row["kind"])
    except Exception as exc:  # noqa: BLE001
        models.record_watched_source_run(source_id, "error", f"connector load failed: {exc}")
        return

    upload_root = _app_ref.config["UPLOAD_ROOT"] if _app_ref else "uploads"
    cache_root = Path(upload_root) / "_scheduler_cache" / f"src{source_id}"
    cache_root.mkdir(parents=True, exist_ok=True)

    # 1. Discover candidates
    try:
        if row["kind"] == "git":
            candidates = list(connector.discover(
                source_path=row["source_path"], config=cfg, cache_root=cache_root,
            ))
        else:
            candidates = list(connector.discover(
                source_path=row["source_path"], config=cfg,
            ))
    except Exception as exc:  # noqa: BLE001
        log.exception("Discover failed for source %s", source_id)
        models.record_watched_source_run(source_id, "error", f"discover failed: {exc}")
        return

    # 2. Filter via idempotency table
    new_files: list[tuple[Path, str]] = []   # (local_path, file_hash)
    for path in candidates:
        try:
            h = connectors.sha256_of(path)
        except Exception:  # noqa: BLE001
            continue
        if models.file_already_processed(source_id, h):
            continue
        new_files.append((path, h))

    if not new_files:
        models.record_watched_source_run(source_id, "no_new_files",
                                         f"scanned {len(candidates)} file(s)")
        return

    log.info("Source %s has %d new file(s) to process", source_id, len(new_files))

    processed_ok = 0
    failures: list[str] = []
    for path, file_hash in new_files:
        try:
            _process_one(row, cfg, path, file_hash)
            processed_ok += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("Processing failed for %s (%s)", path, source_id)
            failures.append(f"{path.name}: {exc}")
            # Mark as processed without a job so we don't retry a broken file
            # in a tight loop. Operator can delete the processed_files row to
            # force a re-run after fixing the input.
            models.mark_file_processed(source_id, file_hash, str(path), None)

    msg_parts = [f"processed {processed_ok}/{len(new_files)} file(s)"]
    if failures:
        msg_parts.append(f"failures: {'; '.join(failures[:5])}")
    status = "ok" if not failures else "error"
    models.record_watched_source_run(source_id, status, " — ".join(msg_parts))


def _process_one(source_row, cfg: dict, file_path: Path, file_hash: str) -> None:
    """
    Run a single discovered file through the same scan + generate + bundle
    pipeline the manual flow uses, then ask the connector to deliver the
    bundle ZIP.
    """
    # Per scheduler-run job: enhancement = file's parent folder, prod_date = today
    folder_name = file_path.parent.name or source_row["name"] or "scheduler"
    enhancement = re.sub(r"[^A-Za-z0-9._\- ]+", "_", folder_name)[:80] or "scheduler"
    prod_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    upload_root = Path(_app_ref.config["UPLOAD_ROOT"]) if _app_ref else Path("uploads")
    work_dir = upload_root / uuid.uuid4().hex
    work_dir.mkdir(parents=True, exist_ok=True)

    job_id = models.create_job(
        user_id=source_row["owner_user_id"],
        input_name=file_path.name, input_type=f"scheduler_{source_row['kind']}",
        input_size_bytes=file_path.stat().st_size,
        work_dir=str(work_dir), ip=None,
        enhancement_name=enhancement, prod_date=prod_date,
        source="scheduler", watched_source_id=source_row["id"],
    )

    # Copy the source file into work_dir so cleanup leaves it intact in cache_root.
    local_copy = work_dir / file_path.name
    shutil.copy2(file_path, local_copy)

    try:
        scan_root = core.prepare_input(local_copy, work_dir)
        result    = core.collect_deletes(scan_root, add_file_headers=True)
        alters    = core.collect_alters(scan_root, add_file_headers=True)
        procs     = core.collect_procedures(scan_root)
        triggers  = core.collect_triggers(scan_root)

        base = Path(file_path.name).stem or "job"
        delete_path = work_dir / core.timestamped("delete", base)
        delete_path.write_text(result.delete_sql, encoding="utf-8")
        models.update_job_collection(
            job_id=job_id, files=result.files_scanned,
            delete_count=result.total_deletes, warnings=result.warnings,
            delete_sql_file=str(delete_path),
        )

        gen = core.generate_backup_revert(result.delete_sql, triggers=triggers)
        backup_path  = work_dir / core.timestamped("BACKUP",  base)
        revert_path  = work_dir / core.timestamped("REVERT",  base)
        cleanup_path = work_dir / core.timestamped("CLEANUP", base) if gen.cleanup_sql else None
        alters_path  = work_dir / core.timestamped("ALTERS",  base)
        procs_path   = work_dir / core.timestamped("PROCEDURES", base, ext=".txt")
        backup_path.write_text(gen.backup_sql, encoding="utf-8")
        revert_path.write_text(gen.revert_sql, encoding="utf-8")
        if cleanup_path is not None:
            cleanup_path.write_text(gen.cleanup_sql, encoding="utf-8")
        alters_path.write_text(alters.alter_sql
                               or "-- No ALTER TABLE statements were found in the bundle.\n",
                               encoding="utf-8")
        procs_path.write_text(procs.procedures_text, encoding="utf-8")

        # Bundle ZIP
        from abr_routes import _build_bundle  # local import to break circular at module load
        bundle_path = _build_bundle(
            job_id=job_id, work_dir=work_dir,
            enhancement_name=enhancement, prod_date=prod_date,
            source_input_path=local_copy,
            backup_path=backup_path, revert_path=revert_path,
            cleanup_path=cleanup_path,
            alters_path=alters_path, procs_path=procs_path,
        )
        models.update_job_generation(
            job_id=job_id, unique_tables=gen.unique_tables,
            revert_count=gen.revert_statements,
            extra_warnings=len(gen.warnings) + len(alters.warnings) + len(procs.warnings),
            backup_file=str(backup_path), revert_file=str(revert_path),
            cleanup_file=str(cleanup_path) if cleanup_path else None,
            alters_count=alters.total_alters, alters_file=str(alters_path),
            procedures_count=procs.total,    procedures_file=str(procs_path),
            bundle_file=str(bundle_path),
        )

        # Deliver bundle to dest_path via the connector
        connector = connectors.get_connector(source_row["kind"])
        connector.deliver(dest_path=source_row["dest_path"], config=cfg,
                          bundle_path=bundle_path)

        models.mark_file_processed(source_row["id"], file_hash, str(file_path), job_id)

    except Exception as exc:
        models.fail_job(job_id, str(exc))
        models.mark_file_processed(source_row["id"], file_hash, str(file_path), job_id)
        raise
