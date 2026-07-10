"""
Persistence for the XPM Automator — self-contained, zero-impact on models.py.

The tool owns two tables in the shared SQLite database and manages them itself,
reusing only the platform's ``models.connect()`` context manager (no schema
changes to the platform's own tables). Table creation is idempotent and lazily
guarded, so the tool can be dropped in without touching ``init_db``.

Tables:
  xpm_runs        one row per upload/download run — the "Processing History"
  xpm_run_files   one row per file in an upload run (per-file status/audit)

Secrets: the XPM password is **never** stored here. Only a redacted config
snapshot (URL / user / project / process) is persisted.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import models  # shared platform DB helpers (connect / portal registry) — reused, not modified

TOOL_SLUG = "xpm"
TOOL_ENDPOINT = "xpm.dashboard"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS xpm_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_number        TEXT NOT NULL,                    -- generated on upload
    final_batch_number  TEXT,                             -- user-editable; defaults to batch_number
    mode                TEXT NOT NULL DEFAULT 'upload',   -- 'upload' | 'batch_download'
    status              TEXT NOT NULL DEFAULT 'uploaded', -- uploaded|processing|completed|failed|cancelled
    user_id             INTEGER,
    username            TEXT,                             -- denormalized (survives user deletion)
    user_email          TEXT,                             -- denormalized
    created_at          TEXT NOT NULL,                    -- upload date & time
    started_at          TEXT,
    finished_at         TEXT,
    duration_ms         INTEGER,
    -- redacted config snapshot (no password)
    base_url            TEXT,
    project_id          TEXT,
    project_name        TEXT,
    process_name        TEXT,
    -- counts
    file_count          INTEGER DEFAULT 0,
    files_uploaded      INTEGER DEFAULT 0,
    files_failed        INTEGER DEFAULT 0,
    -- batch_download mode
    batch_from          INTEGER,
    batch_to            INTEGER,
    -- artefacts
    work_dir            TEXT,
    output_file         TEXT,
    output_name         TEXT,
    output_size_bytes   INTEGER,
    -- audit
    download_count      INTEGER NOT NULL DEFAULT 0,
    last_download_at    TEXT,
    error_message       TEXT,
    remarks             TEXT,
    log_json            TEXT,                             -- persisted processing timeline
    ip_address          TEXT
);

CREATE TABLE IF NOT EXISTS xpm_run_files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL,
    ordinal      INTEGER NOT NULL DEFAULT 0,
    filename     TEXT NOT NULL,
    size_bytes   INTEGER,
    status       TEXT NOT NULL DEFAULT 'pending',        -- pending|uploaded|failed
    error        TEXT,
    FOREIGN KEY (run_id) REFERENCES xpm_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_xpm_runs_created   ON xpm_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_xpm_runs_user      ON xpm_runs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_xpm_runs_status    ON xpm_runs(status);
CREATE INDEX IF NOT EXISTS idx_xpm_runs_batch     ON xpm_runs(batch_number);
CREATE INDEX IF NOT EXISTS idx_xpm_run_files_run  ON xpm_run_files(run_id);
"""

_initialised = False


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


def _ensure() -> None:
    """Create the tables once per process (idempotent, cheap after first call)."""
    global _initialised
    if _initialised:
        return
    with models.connect() as con:
        con.executescript(_SCHEMA)
        con.commit()
    _initialised = True


def init_store() -> None:
    """Eager init used by the app factory: create tables + register the tool card."""
    _ensure()
    ensure_registered()


# ----------------------------------------------------------------------
# Portal registration (idempotent) — makes the tool appear in the data-driven
# nav/landing on existing installs where first-run seeding already happened.
# ----------------------------------------------------------------------
def ensure_registered() -> None:
    try:
        if models.get_portal_tool_by_slug(TOOL_SLUG) is not None:
            return
        models.create_portal_tool(
            slug=TOOL_SLUG,
            name="XPM Automator",
            description=("Bulk-upload SQL/TXT migration scripts to the XPM CRM in order, "
                         "auto-generate an editable Batch Number, download the consolidated "
                         "script, and keep a full processing-history audit trail."),
            icon="cloud_sync",
            icon_type="symbol",
            tags=["XPM", "Migration", "Upload"],
            status="live",
            launch_type="internal",
            launch_config={"endpoint": TOOL_ENDPOINT},
            display_order=models.next_portal_tool_order(),
            requires_team=False,
            created_by=None,
        )
    except Exception:  # noqa: BLE001 — registration must never break app startup
        import logging
        logging.getLogger("xpm").exception("Failed to register XPM Automator portal tool")


# ----------------------------------------------------------------------
# Run lifecycle
# ----------------------------------------------------------------------
def create_run(*, batch_number: str, mode: str, user_id: int | None, username: str | None,
               user_email: str | None, cfg_snapshot: dict, ip: str | None,
               work_dir: str | None, file_count: int = 0,
               batch_from: int | None = None, batch_to: int | None = None) -> int:
    _ensure()
    with models.connect() as con:
        cur = con.execute(
            """INSERT INTO xpm_runs
               (batch_number, final_batch_number, mode, status, user_id, username, user_email,
                created_at, base_url, project_id, project_name, process_name,
                file_count, batch_from, batch_to, work_dir, ip_address)
               VALUES (?, ?, ?, 'uploaded', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (batch_number, batch_number, mode, user_id, username, user_email,
             _now(), cfg_snapshot.get("base_url"), str(cfg_snapshot.get("project_id") or ""),
             cfg_snapshot.get("project_name"), cfg_snapshot.get("process_name"),
             file_count, batch_from, batch_to, work_dir, ip),
        )
        con.commit()
        return cur.lastrowid


def add_run_file(run_id: int, filename: str, size_bytes: int | None, ordinal: int) -> int:
    _ensure()
    with models.connect() as con:
        cur = con.execute(
            """INSERT INTO xpm_run_files (run_id, ordinal, filename, size_bytes, status)
               VALUES (?, ?, ?, ?, 'pending')""",
            (run_id, ordinal, filename, size_bytes),
        )
        con.commit()
        return cur.lastrowid


def mark_started(run_id: int) -> None:
    _ensure()
    with models.connect() as con:
        con.execute("UPDATE xpm_runs SET status='processing', started_at=? WHERE id=?",
                    (_now(), run_id))
        con.commit()


def set_file_status(run_id: int, filename: str, status: str, error: str | None = None) -> None:
    _ensure()
    with models.connect() as con:
        # Update the first still-pending row for this filename (handles duplicates).
        row = con.execute(
            "SELECT id FROM xpm_run_files WHERE run_id=? AND filename=? AND status='pending' "
            "ORDER BY ordinal LIMIT 1", (run_id, filename)).fetchone()
        if row is None:
            row = con.execute(
                "SELECT id FROM xpm_run_files WHERE run_id=? AND filename=? ORDER BY ordinal LIMIT 1",
                (run_id, filename)).fetchone()
        if row is not None:
            con.execute("UPDATE xpm_run_files SET status=?, error=? WHERE id=?",
                        (status, error, row["id"]))
            con.commit()


def finish_run(run_id: int, *, status: str, files_uploaded: int = 0, files_failed: int = 0,
               output_file: str | None = None, output_name: str | None = None,
               output_size_bytes: int | None = None, error_message: str | None = None,
               remarks: str | None = None, steps: list | None = None) -> None:
    _ensure()
    started = None
    with models.connect() as con:
        r = con.execute("SELECT started_at FROM xpm_runs WHERE id=?", (run_id,)).fetchone()
        started = r["started_at"] if r else None
    duration_ms = None
    if started:
        try:
            s = datetime.fromisoformat(started.rstrip("Z"))
            duration_ms = int((datetime.now(timezone.utc).replace(tzinfo=None) - s).total_seconds() * 1000)
        except ValueError:
            duration_ms = None
    with models.connect() as con:
        con.execute(
            """UPDATE xpm_runs
               SET status=?, finished_at=?, duration_ms=?,
                   files_uploaded=?, files_failed=?,
                   output_file=?, output_name=?, output_size_bytes=?,
                   error_message=?, remarks=?, log_json=?
               WHERE id=?""",
            (status, _now(), duration_ms, files_uploaded, files_failed,
             output_file, output_name, output_size_bytes,
             error_message, remarks, json.dumps(steps or []), run_id),
        )
        con.commit()


def set_final_batch_number(run_id: int, value: str) -> None:
    _ensure()
    with models.connect() as con:
        con.execute("UPDATE xpm_runs SET final_batch_number=? WHERE id=?", (value, run_id))
        con.commit()


def set_batch_range(run_id: int, batch_from: int, batch_to: int) -> None:
    """Record the XPM batch range the consolidated download covered (upload mode)."""
    _ensure()
    with models.connect() as con:
        con.execute("UPDATE xpm_runs SET batch_from=?, batch_to=? WHERE id=?",
                    (batch_from, batch_to, run_id))
        con.commit()


def record_download(run_id: int) -> None:
    _ensure()
    with models.connect() as con:
        con.execute(
            "UPDATE xpm_runs SET download_count = COALESCE(download_count,0)+1, last_download_at=? WHERE id=?",
            (_now(), run_id))
        con.commit()


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------
def get_run(run_id: int) -> sqlite3.Row | None:
    _ensure()
    with models.connect() as con:
        return con.execute("SELECT * FROM xpm_runs WHERE id=?", (run_id,)).fetchone()


def get_run_files(run_id: int) -> list[sqlite3.Row]:
    _ensure()
    with models.connect() as con:
        return con.execute(
            "SELECT * FROM xpm_run_files WHERE run_id=? ORDER BY ordinal, id", (run_id,)
        ).fetchall()


def _where(*, user_id, q, status, date_from, date_to, batch, username):
    where: list[str] = []
    args: list = []
    if user_id is not None:
        where.append("user_id = ?"); args.append(user_id)
    if q:
        where.append("(batch_number LIKE ? OR final_batch_number LIKE ? OR username LIKE ? "
                     "OR project_name LIKE ? OR output_name LIKE ?)")
        like = f"%{q}%"; args.extend([like, like, like, like, like])
    if status:
        where.append("status = ?"); args.append(status)
    if batch:
        where.append("(batch_number LIKE ? OR final_batch_number LIKE ?)")
        args.extend([f"%{batch}%", f"%{batch}%"])
    if username:
        where.append("username = ?"); args.append(username)
    if date_from:
        where.append("substr(created_at,1,10) >= ?"); args.append(date_from)
    if date_to:
        where.append("substr(created_at,1,10) <= ?"); args.append(date_to)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    return clause, args


_SORTABLE = {
    "created_at": "created_at", "batch": "batch_number", "status": "status",
    "files": "file_count", "duration": "duration_ms", "user": "username",
}


def search_runs(*, user_id: int | None = None, q: str | None = None,
                status: str | None = None, date_from: str | None = None,
                date_to: str | None = None, batch: str | None = None,
                username: str | None = None, sort: str = "created_at",
                direction: str = "desc", limit: int = 25, offset: int = 0) -> list[sqlite3.Row]:
    _ensure()
    clause, args = _where(user_id=user_id, q=q, status=status, date_from=date_from,
                          date_to=date_to, batch=batch, username=username)
    col = _SORTABLE.get(sort, "created_at")
    dir_sql = "ASC" if str(direction).lower() == "asc" else "DESC"
    args2 = list(args) + [limit, offset]
    with models.connect() as con:
        return con.execute(
            f"SELECT * FROM xpm_runs {clause} ORDER BY {col} {dir_sql}, id {dir_sql} LIMIT ? OFFSET ?",
            tuple(args2),
        ).fetchall()


def count_runs(*, user_id: int | None = None, q: str | None = None,
               status: str | None = None, date_from: str | None = None,
               date_to: str | None = None, batch: str | None = None,
               username: str | None = None) -> int:
    _ensure()
    clause, args = _where(user_id=user_id, q=q, status=status, date_from=date_from,
                          date_to=date_to, batch=batch, username=username)
    with models.connect() as con:
        return con.execute(f"SELECT COUNT(*) c FROM xpm_runs {clause}", tuple(args)).fetchone()["c"]


def list_recent(user_id: int | None = None, limit: int = 8) -> list[sqlite3.Row]:
    _ensure()
    with models.connect() as con:
        if user_id is None:
            return con.execute("SELECT * FROM xpm_runs ORDER BY created_at DESC LIMIT ?",
                               (limit,)).fetchall()
        return con.execute("SELECT * FROM xpm_runs WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                           (user_id, limit)).fetchall()


def distinct_usernames(user_id: int | None = None) -> list[str]:
    _ensure()
    with models.connect() as con:
        if user_id is None:
            rows = con.execute(
                "SELECT DISTINCT username FROM xpm_runs WHERE username IS NOT NULL ORDER BY username"
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT DISTINCT username FROM xpm_runs WHERE user_id=? AND username IS NOT NULL ORDER BY username",
                (user_id,)).fetchall()
    return [r["username"] for r in rows]


def dashboard_stats(user_id: int | None = None) -> dict:
    """Totals for the dashboard. user_id=None → platform-wide (admin view)."""
    _ensure()
    today = datetime.now(timezone.utc).replace(tzinfo=None).date().isoformat()
    scope = "" if user_id is None else " AND user_id = :uid"
    params = {} if user_id is None else {"uid": user_id}
    with models.connect() as con:
        def one(sql: str) -> int:
            return con.execute(sql, params).fetchone()["c"]
        total = one(f"SELECT COUNT(*) c FROM xpm_runs WHERE 1=1{scope}")
        completed = one(f"SELECT COUNT(*) c FROM xpm_runs WHERE status='completed'{scope}")
        failed = one(f"SELECT COUNT(*) c FROM xpm_runs WHERE status='failed'{scope}")
        cancelled = one(f"SELECT COUNT(*) c FROM xpm_runs WHERE status='cancelled'{scope}")
        active = one(f"SELECT COUNT(*) c FROM xpm_runs WHERE status IN ('uploaded','processing'){scope}")
        today_ct = con.execute(
            f"SELECT COUNT(*) c FROM xpm_runs WHERE substr(created_at,1,10)=:d{scope}",
            {**params, "d": today}).fetchone()["c"]
        files_up = con.execute(
            f"SELECT COALESCE(SUM(files_uploaded),0) c FROM xpm_runs WHERE 1=1{scope}", params
        ).fetchone()["c"]
    return {
        "total": total, "completed": completed, "failed": failed,
        "cancelled": cancelled, "active": active, "today": today_ct,
        "files_uploaded": files_up,
    }


def runs_per_day(days: int = 14, user_id: int | None = None) -> list[dict]:
    _ensure()
    from datetime import timedelta
    start = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days - 1)).date().isoformat()
    scope = "" if user_id is None else " AND user_id = :uid"
    params = {"start": start}
    if user_id is not None:
        params["uid"] = user_id
    with models.connect() as con:
        rows = con.execute(
            f"SELECT substr(created_at,1,10) d, COUNT(*) c FROM xpm_runs "
            f"WHERE substr(created_at,1,10) >= :start{scope} GROUP BY d", params
        ).fetchall()
    counts = {r["d"]: r["c"] for r in rows}
    out = []
    for i in range(days):
        d = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days - 1 - i)).date().isoformat()
        out.append({"date": d, "count": counts.get(d, 0)})
    return out
