"""
Data migration engine (#9) — copy all data from the default SQLite database to
a configured target provider, with live progress and rollback on failure.

Migrates every table (users, teams, roles/permissions via the users/teams model,
tools, categories/tags, settings, audit_log, tool_launches, jobs, …) — i.e. the
full SQLite schema, reflected dynamically so new tables are included automatically.

Relational targets use SQLAlchemy (schema reflected from SQLite, recreated on the
target, rows copied in batches). MongoDB maps each table to a collection and each
row to a document.

Safety:
  * Runs in a background thread; the admin UI polls `get_progress(run_id)`.
  * **Rollback** — on any error, tables/collections created during the run are
    dropped, leaving the target clean.
  * SQLite remains the live datastore throughout; migration is copy-only and never
    mutates the source. Promoting the target to primary is a deliberate, separate
    cut-over (see docs) — so a failed migration can never take the app down.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

import db_providers

_runs: dict[str, dict] = {}
_lock = threading.Lock()

_BATCH = 500


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


def _update(run_id: str, **kw) -> None:
    with _lock:
        if run_id in _runs:
            _runs[run_id].update(kw)


def get_progress(run_id: str) -> dict | None:
    with _lock:
        return dict(_runs[run_id]) if run_id in _runs else None


def start_migration(source_sqlite_path: str, target_pid: str, target_cfg: dict) -> str:
    """Kick off a migration in the background; returns a run id to poll."""
    run_id = uuid.uuid4().hex[:12]
    with _lock:
        _runs[run_id] = {
            "id": run_id, "state": "starting", "target": target_pid,
            "tables_total": 0, "tables_done": 0, "current": None,
            "rows_copied": 0, "error": None,
            "started_at": _now(), "finished_at": None, "log": [],
        }
    t = threading.Thread(target=_run, args=(run_id, source_sqlite_path, target_pid, target_cfg), daemon=True)
    t.start()
    return run_id


def _log(run_id: str, msg: str) -> None:
    with _lock:
        if run_id in _runs:
            _runs[run_id]["log"].append(f"{_now()}  {msg}")


def _run(run_id: str, source_path: str, target_pid: str, cfg: dict) -> None:
    try:
        if target_pid == "mongodb":
            _migrate_to_mongo(run_id, source_path, cfg)
        else:
            _migrate_relational(run_id, source_path, cfg)
        _update(run_id, state="completed", finished_at=_now(), current=None)
        _log(run_id, "Migration completed successfully.")
    except Exception as exc:  # noqa: BLE001
        _update(run_id, state="failed", error=str(exc), finished_at=_now())
        _log(run_id, f"FAILED: {exc}")


def _migrate_relational(run_id: str, source_path: str, cfg: dict) -> None:
    from sqlalchemy import MetaData, create_engine, insert, select

    src = create_engine("sqlite:///" + source_path)
    tgt = create_engine(db_providers.build_sqlalchemy_url(
        _run_target_pid(run_id), cfg), pool_pre_ping=True)

    md = MetaData()
    md.reflect(bind=src)
    tables = list(md.sorted_tables)
    _update(run_id, state="running", tables_total=len(tables))
    _log(run_id, f"Reflected {len(tables)} table(s) from SQLite.")

    created = []
    try:
        for tbl in tables:
            _update(run_id, current=tbl.name)
            tbl.create(bind=tgt, checkfirst=True)
            created.append(tbl)
            with src.connect() as sc:
                rows = [dict(r._mapping) for r in sc.execute(select(tbl))]
            if rows:
                with tgt.begin() as tc:
                    for i in range(0, len(rows), _BATCH):
                        tc.execute(insert(tbl), rows[i:i + _BATCH])
            with _lock:
                r = _runs[run_id]
                r["tables_done"] += 1
                r["rows_copied"] += len(rows)
            _log(run_id, f"{tbl.name}: {len(rows)} row(s) copied.")
    except Exception:
        _log(run_id, "Error — rolling back created tables on target…")
        try:
            md_created = MetaData()
            for t in reversed(created):
                t.tometadata(md_created)
            md_created.drop_all(bind=tgt)
            _log(run_id, "Rollback complete (created tables dropped).")
        except Exception as rb:  # noqa: BLE001
            _log(run_id, f"Rollback issue: {rb}")
        raise
    finally:
        src.dispose(); tgt.dispose()


def _migrate_to_mongo(run_id: str, source_path: str, cfg: dict) -> None:
    import sqlite3

    client = db_providers._mongo_client(cfg)
    dbname = cfg.get("database") or "delivery_toolbox"
    mdb = client[dbname]

    con = sqlite3.connect(source_path)
    con.row_factory = sqlite3.Row
    tbls = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
    _update(run_id, state="running", tables_total=len(tbls))
    _log(run_id, f"Found {len(tbls)} table(s) in SQLite.")

    created = []
    try:
        for name in tbls:
            _update(run_id, current=name)
            docs = [dict(r) for r in con.execute(f"SELECT * FROM {name}").fetchall()]
            coll = mdb[name]
            if docs:
                coll.insert_many(docs)
            created.append(name)
            with _lock:
                r = _runs[run_id]
                r["tables_done"] += 1
                r["rows_copied"] += len(docs)
            _log(run_id, f"{name}: {len(docs)} document(s) inserted.")
    except Exception:
        _log(run_id, "Error — rolling back created collections…")
        for name in created:
            try:
                mdb.drop_collection(name)
            except Exception:  # noqa: BLE001
                pass
        _log(run_id, "Rollback complete (collections dropped).")
        raise
    finally:
        con.close(); client.close()


def _run_target_pid(run_id: str) -> str:
    with _lock:
        return _runs[run_id]["target"]
