"""
SQLite persistence layer.

Tables:
  users            accounts (username, email, hashed password, role,
                   last_login_at, created_by_user_id, team_id, team_role,
                   approval_status, full_name, employee_code)
  teams            team registry (name, description, created_by)
  join_requests    pending team membership requests
  notifications    in-app notifications for team leaders / admins / users
  jobs             tool runs — input + metadata (enhancement_name, prod_date)
                   + scan + generate + bundle + ownership + audit
  downloads        audit: each generated-file download
  password_resets  one-time tokens for the forgot-password flow
  watched_sources  admin-configured external folders/Git repos to poll
                   on a schedule and auto-process
  processed_files  idempotency manifest (file_hash + source_id unique)
  api_tokens       (dead table from removed REST API — retained so the
                   migration is non-destructive on existing installs)
  settings         key-value store for runtime feature flags and storage paths
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    username                 TEXT NOT NULL UNIQUE,
    email                    TEXT,
    password_hash            TEXT NOT NULL,
    role                     TEXT NOT NULL DEFAULT 'user',
    is_active                INTEGER NOT NULL DEFAULT 1,
    created_at               TEXT NOT NULL,
    created_by_user_id       INTEGER,
    last_login_at            TEXT,
    last_password_change_at  TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL,
    created_at          TEXT NOT NULL,
    enhancement_name    TEXT,            -- mandatory for new jobs (manual + scheduler)
    prod_date           TEXT,            -- mandatory for new jobs (YYYY-MM-DD)
    input_name          TEXT,
    input_type          TEXT,
    input_size_bytes    INTEGER,
    status              TEXT NOT NULL,
    files_scanned       INTEGER DEFAULT 0,
    delete_count        INTEGER DEFAULT 0,
    unique_tables       INTEGER DEFAULT 0,
    revert_count        INTEGER DEFAULT 0,
    warning_count       INTEGER DEFAULT 0,
    alters_count        INTEGER DEFAULT 0,
    procedures_count    INTEGER DEFAULT 0,
    work_dir            TEXT,
    files_json          TEXT,
    delete_sql_file     TEXT,
    backup_sql_file     TEXT,
    revert_sql_file     TEXT,
    cleanup_sql_file    TEXT,
    alters_sql_file     TEXT,
    procedures_file     TEXT,
    bundle_file         TEXT,            -- ZIP of all artefacts + source + MANIFEST.json
    source              TEXT NOT NULL DEFAULT 'manual',  -- 'manual' | 'scheduler'
    watched_source_id   INTEGER,         -- non-null for scheduler-driven jobs
    api_token_id        INTEGER,         -- dead column (REST API removed)
    error_message       TEXT,
    ip_address          TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS watched_sources (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL UNIQUE,        -- human label & default enhancement_name fallback
    kind               TEXT NOT NULL,               -- 'local' | 'git'
    source_path        TEXT NOT NULL,               -- local: filesystem path; git: repo URL
    dest_path          TEXT NOT NULL,               -- where the bundle ZIP is dropped after run
    config_json        TEXT,                        -- JSON: connector-specific (branch, sub_path, pat, ...)
    interval_kind      TEXT NOT NULL,               -- legacy fallback ('every_minutes' | 'daily_at' | 'cron')
    interval_value     TEXT NOT NULL,               -- legacy fallback (free-text)
    schedule_json      TEXT,                        -- rich schedule v2 (preferred) — see scheduler.build_trigger_from_json
    enabled            INTEGER NOT NULL DEFAULT 1,
    owner_user_id      INTEGER NOT NULL,            -- which user owns scheduler-driven jobs from this source
    created_by_user_id INTEGER,
    created_at         TEXT NOT NULL,
    last_run_at        TEXT,
    last_run_status    TEXT,                        -- 'ok' | 'no_new_files' | 'error'
    last_run_message   TEXT,
    FOREIGN KEY (owner_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS processed_files (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    watched_source_id   INTEGER NOT NULL,
    file_hash           TEXT NOT NULL,               -- SHA-256
    original_path       TEXT NOT NULL,
    job_id              INTEGER,                     -- null if pre-job ingest failed
    processed_at        TEXT NOT NULL,
    FOREIGN KEY (watched_source_id) REFERENCES watched_sources(id),
    FOREIGN KEY (job_id) REFERENCES jobs(id),
    UNIQUE (watched_source_id, file_hash)
);

CREATE TABLE IF NOT EXISTS downloads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    filename      TEXT NOT NULL,
    downloaded_at TEXT NOT NULL,
    ip_address    TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS password_resets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT,
    ip_address  TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    name          TEXT NOT NULL,
    prefix        TEXT NOT NULL UNIQUE,
    token_hash    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    created_by_user_id INTEGER,
    last_used_at  TEXT,
    expires_at    TEXT,
    revoked_at    TEXT,
    FOREIGN KEY (owner_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS teams (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL UNIQUE,
    description         TEXT,
    created_at          TEXT NOT NULL,
    created_by_user_id  INTEGER,
    FOREIGN KEY (created_by_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS join_requests (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                 INTEGER NOT NULL,
    team_id                 INTEGER NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'pending',
    requested_at            TEXT NOT NULL,
    reviewed_at             TEXT,
    reviewed_by_user_id     INTEGER,
    FOREIGN KEY (user_id)             REFERENCES users(id),
    FOREIGN KEY (team_id)             REFERENCES teams(id),
    FOREIGN KEY (reviewed_by_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    message     TEXT NOT NULL,
    link        TEXT,
    is_read     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    ref_id      INTEGER,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_user_created    ON jobs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_created         ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_enhancement     ON jobs(enhancement_name);
CREATE INDEX IF NOT EXISTS idx_jobs_prod_date       ON jobs(prod_date);
CREATE INDEX IF NOT EXISTS idx_downloads_job        ON downloads(job_id);
CREATE INDEX IF NOT EXISTS idx_resets_user          ON password_resets(user_id);
CREATE INDEX IF NOT EXISTS idx_tokens_prefix        ON api_tokens(prefix);
CREATE INDEX IF NOT EXISTS idx_processed_source     ON processed_files(watched_source_id);
CREATE INDEX IF NOT EXISTS idx_join_req_team        ON join_requests(team_id, status);
CREATE INDEX IF NOT EXISTS idx_join_req_user        ON join_requests(user_id);
CREATE INDEX IF NOT EXISTS idx_notifications_user   ON notifications(user_id, is_read);
"""

# Light migration support for existing DBs created by an earlier version of this
# file (which lacked the new columns / tables).
ALTERS = [
    # original migrations
    "ALTER TABLE users ADD COLUMN created_by_user_id INTEGER",
    "ALTER TABLE users ADD COLUMN last_login_at TEXT",
    "ALTER TABLE users ADD COLUMN last_password_change_at TEXT",
    "ALTER TABLE jobs  ADD COLUMN source TEXT NOT NULL DEFAULT 'web'",
    "ALTER TABLE jobs  ADD COLUMN api_token_id INTEGER",
    "ALTER TABLE jobs  ADD COLUMN alters_count INTEGER DEFAULT 0",
    "ALTER TABLE jobs  ADD COLUMN procedures_count INTEGER DEFAULT 0",
    "ALTER TABLE jobs  ADD COLUMN alters_sql_file TEXT",
    "ALTER TABLE jobs  ADD COLUMN procedures_file TEXT",
    "ALTER TABLE jobs  ADD COLUMN cleanup_sql_file TEXT",
    "ALTER TABLE jobs  ADD COLUMN enhancement_name TEXT",
    "ALTER TABLE jobs  ADD COLUMN prod_date TEXT",
    "ALTER TABLE jobs  ADD COLUMN bundle_file TEXT",
    "ALTER TABLE jobs  ADD COLUMN watched_source_id INTEGER",
    "ALTER TABLE watched_sources ADD COLUMN schedule_json TEXT",
    # team management migrations
    "ALTER TABLE users ADD COLUMN team_id INTEGER",
    "ALTER TABLE users ADD COLUMN team_role TEXT DEFAULT 'member'",
    "ALTER TABLE users ADD COLUMN approval_status TEXT DEFAULT 'approved'",
    "ALTER TABLE users ADD COLUMN employee_code TEXT",
    "ALTER TABLE users ADD COLUMN full_name TEXT",
]


# ----------------------------------------------------------------------
# Connection helpers
# ----------------------------------------------------------------------
_db_path: Path | None = None


def init_db(db_path: Path) -> None:
    """
    Idempotent migration. Order matters:
      1. ALTERS first — adds missing columns to *existing* tables so any
         new index in SCHEMA can find them. On a fresh install this is a
         no-op (tables don't exist yet; the errors are swallowed).
      2. SCHEMA — creates anything that doesn't exist (tables + indexes).
         The CREATE TABLE IF NOT EXISTS skips existing tables; the
         indexes succeed because step 1 already added their columns.
    """
    global _db_path
    _db_path = Path(db_path)
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as con:
        for stmt in ALTERS:
            try:
                con.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column / table already present (or table not yet created)
        con.executescript(SCHEMA)
        con.commit()


@contextmanager
def connect():
    if _db_path is None:
        raise RuntimeError("Database not initialized — call init_db() first.")
    con = sqlite3.connect(_db_path)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


# Sentinel for "caller did not pass this kwarg" (distinct from None which means "clear the value").
_UNSET = object()


# ----------------------------------------------------------------------
# Users
# ----------------------------------------------------------------------
def create_user(username: str, email: str, password: str, role: str = "user",
                created_by: int | None = None, full_name: str | None = None,
                employee_code: str | None = None, team_id: int | None = None,
                approval_status: str = "approved") -> int:
    pw_hash = generate_password_hash(password)
    with connect() as con:
        cur = con.execute(
            """INSERT INTO users (username, email, password_hash, role, created_at,
                                  created_by_user_id, last_password_change_at,
                                  full_name, employee_code, team_id, approval_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (username, email, pw_hash, role, _now(), created_by, _now(),
             full_name, employee_code, team_id, approval_status),
        )
        con.commit()
        return cur.lastrowid


def ensure_admin(username: str, email: str, password: str) -> None:
    pw_hash = generate_password_hash(password)
    with connect() as con:
        row = con.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            con.execute(
                """INSERT INTO users (username, email, password_hash, role, created_at, last_password_change_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (username, email, pw_hash, "admin", _now(), _now()),
            )
        else:
            con.execute(
                "UPDATE users SET password_hash=?, email=?, role='admin' WHERE id=?",
                (pw_hash, email, row["id"]),
            )
        con.commit()


def get_user(user_id: int) -> sqlite3.Row | None:
    with connect() as con:
        return con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_username(username: str) -> sqlite3.Row | None:
    with connect() as con:
        return con.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def get_user_by_email(email: str) -> sqlite3.Row | None:
    if not email:
        return None
    with connect() as con:
        return con.execute("SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (email,)).fetchone()


def verify_user(username: str, password: str) -> sqlite3.Row | None:
    row = get_user_by_username(username)
    if row is None or not row["is_active"]:
        return None
    if check_password_hash(row["password_hash"], password):
        with connect() as con:
            con.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (_now(), row["id"]))
            con.commit()
        return row
    return None


def username_exists(username: str) -> bool:
    return get_user_by_username(username) is not None


def list_users() -> list[sqlite3.Row]:
    with connect() as con:
        return con.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()


def admin_count() -> int:
    with connect() as con:
        return con.execute("SELECT COUNT(*) c FROM users WHERE role = 'admin' AND is_active = 1").fetchone()["c"]


def update_user(user_id: int, *, email: str | None = None, role: str | None = None,
                is_active: bool | None = None, team_id: int | None = _UNSET,
                team_role: str | None = None, approval_status: str | None = None,
                full_name: str | None = None, employee_code: str | None = None) -> None:
    fields = []
    values = []
    if email is not None:
        fields.append("email = ?"); values.append(email)
    if role is not None:
        fields.append("role = ?"); values.append(role)
    if is_active is not None:
        fields.append("is_active = ?"); values.append(1 if is_active else 0)
    if team_id is not _UNSET:
        fields.append("team_id = ?"); values.append(team_id)
    if team_role is not None:
        fields.append("team_role = ?"); values.append(team_role)
    if approval_status is not None:
        fields.append("approval_status = ?"); values.append(approval_status)
    if full_name is not None:
        fields.append("full_name = ?"); values.append(full_name)
    if employee_code is not None:
        fields.append("employee_code = ?"); values.append(employee_code)
    if not fields:
        return
    values.append(user_id)
    with connect() as con:
        con.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)
        con.commit()


def set_password(user_id: int, new_password: str) -> None:
    with connect() as con:
        con.execute(
            "UPDATE users SET password_hash = ?, last_password_change_at = ? WHERE id = ?",
            (generate_password_hash(new_password), _now(), user_id),
        )
        con.commit()


def delete_user(user_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM users WHERE id = ?", (user_id,))
        con.commit()


# ----------------------------------------------------------------------
# Jobs
# ----------------------------------------------------------------------
def create_job(user_id: int, input_name: str, input_type: str,
               input_size_bytes: int, work_dir: str, ip: str | None,
               enhancement_name: str, prod_date: str,
               source: str = "manual",
               watched_source_id: int | None = None) -> int:
    with connect() as con:
        cur = con.execute(
            """INSERT INTO jobs
               (user_id, created_at, enhancement_name, prod_date,
                input_name, input_type, input_size_bytes,
                status, work_dir, ip_address, source, watched_source_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'collecting', ?, ?, ?, ?)""",
            (user_id, _now(), enhancement_name, prod_date,
             input_name, input_type, input_size_bytes, work_dir, ip,
             source, watched_source_id),
        )
        con.commit()
        return cur.lastrowid


def update_job_collection(job_id: int, files: list[dict], delete_count: int,
                          warnings: list[str], delete_sql_file: str) -> None:
    with connect() as con:
        con.execute(
            """UPDATE jobs
               SET files_scanned = ?, delete_count = ?, warning_count = ?,
                   files_json = ?, delete_sql_file = ?, status = 'reviewed'
               WHERE id = ?""",
            (len(files), delete_count, len(warnings), json.dumps(files), delete_sql_file, job_id),
        )
        con.commit()


def update_job_generation(job_id: int, unique_tables: int, revert_count: int,
                          extra_warnings: int, backup_file: str, revert_file: str,
                          alters_count: int = 0, alters_file: str | None = None,
                          procedures_count: int = 0, procedures_file: str | None = None,
                          cleanup_file: str | None = None,
                          bundle_file: str | None = None) -> None:
    with connect() as con:
        con.execute(
            """UPDATE jobs
               SET unique_tables = ?, revert_count = ?,
                   warning_count = warning_count + ?,
                   backup_sql_file = ?, revert_sql_file = ?,
                   cleanup_sql_file = ?,
                   alters_count = ?, alters_sql_file = ?,
                   procedures_count = ?, procedures_file = ?,
                   bundle_file = ?,
                   status = 'generated'
               WHERE id = ?""",
            (unique_tables, revert_count, extra_warnings,
             backup_file, revert_file,
             cleanup_file,
             alters_count, alters_file,
             procedures_count, procedures_file,
             bundle_file,
             job_id),
        )
        con.commit()


def fail_job(job_id: int, message: str) -> None:
    with connect() as con:
        con.execute("UPDATE jobs SET status='failed', error_message=? WHERE id=?", (message, job_id))
        con.commit()


def get_job(job_id: int) -> sqlite3.Row | None:
    with connect() as con:
        return con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def job_files(job: sqlite3.Row) -> list[dict]:
    if not job["files_json"]:
        return []
    try:
        return json.loads(job["files_json"])
    except json.JSONDecodeError:
        return []


def list_jobs_for_user(user_id: int, limit: int = 100) -> list[sqlite3.Row]:
    with connect() as con:
        return con.execute(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def list_all_jobs(limit: int = 500) -> list[sqlite3.Row]:
    with connect() as con:
        return con.execute(
            """SELECT j.*, u.username
               FROM jobs j LEFT JOIN users u ON u.id = j.user_id
               ORDER BY j.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()


def expired_job_work_dirs(retention_days: int) -> list[tuple[int, str]]:
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=retention_days)).isoformat(timespec="seconds") + "Z"
    with connect() as con:
        rows = con.execute(
            "SELECT id, work_dir FROM jobs WHERE work_dir IS NOT NULL AND created_at < ?",
            (cutoff,),
        ).fetchall()
    return [(r["id"], r["work_dir"]) for r in rows if r["work_dir"]]


def expired_job_work_dirs_range(date_from: str, date_to: str) -> list[tuple[int, str]]:
    """Return (job_id, work_dir) for jobs whose work_dir exists and were created
    between date_from and date_to inclusive (YYYY-MM-DD format)."""
    with connect() as con:
        rows = con.execute(
            """SELECT id, work_dir FROM jobs
               WHERE work_dir IS NOT NULL
                 AND substr(created_at, 1, 10) >= ?
                 AND substr(created_at, 1, 10) <= ?""",
            (date_from, date_to),
        ).fetchall()
    return [(r["id"], r["work_dir"]) for r in rows if r["work_dir"]]


def count_jobs_with_workdir_in_range(date_from: str, date_to: str) -> int:
    """Count jobs that have a work_dir set (i.e. disk space in use) in a date range."""
    with connect() as con:
        return con.execute(
            """SELECT COUNT(*) c FROM jobs
               WHERE work_dir IS NOT NULL
                 AND substr(created_at, 1, 10) >= ?
                 AND substr(created_at, 1, 10) <= ?""",
            (date_from, date_to),
        ).fetchone()["c"]


def clear_job_workdir(job_id: int) -> None:
    with connect() as con:
        con.execute("UPDATE jobs SET work_dir=NULL WHERE id=?", (job_id,))
        con.commit()


# ----------------------------------------------------------------------
# Downloads
# ----------------------------------------------------------------------
def record_download(job_id: int, user_id: int, filename: str, ip: str | None) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO downloads (job_id, user_id, filename, downloaded_at, ip_address) VALUES (?, ?, ?, ?, ?)",
            (job_id, user_id, filename, _now(), ip),
        )
        con.commit()


def downloads_for_job(job_id: int) -> list[sqlite3.Row]:
    with connect() as con:
        return con.execute(
            "SELECT * FROM downloads WHERE job_id = ? ORDER BY downloaded_at DESC", (job_id,)
        ).fetchall()


# ----------------------------------------------------------------------
# Stats
# ----------------------------------------------------------------------
def stats_overall() -> dict:
    with connect() as con:
        users      = con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        jobs       = con.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]
        deletes    = con.execute("SELECT COALESCE(SUM(delete_count),0) c FROM jobs").fetchone()["c"]
        downloads  = con.execute("SELECT COUNT(*) c FROM downloads").fetchone()["c"]
    return {"users": users, "jobs": jobs, "deletes": deletes, "downloads": downloads}


def stats_for_user(user_id: int) -> dict:
    with connect() as con:
        jobs      = con.execute("SELECT COUNT(*) c FROM jobs WHERE user_id=?", (user_id,)).fetchone()["c"]
        deletes   = con.execute("SELECT COALESCE(SUM(delete_count),0) c FROM jobs WHERE user_id=?", (user_id,)).fetchone()["c"]
        downloads = con.execute("SELECT COUNT(*) c FROM downloads WHERE user_id=?", (user_id,)).fetchone()["c"]
    return {"jobs": jobs, "deletes": deletes, "downloads": downloads}


def jobs_per_day(days: int = 30, user_id: int | None = None) -> list[dict]:
    start = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days - 1)).date().isoformat()
    with connect() as con:
        if user_id is None:
            rows = con.execute(
                "SELECT substr(created_at,1,10) d, COUNT(*) c FROM jobs WHERE substr(created_at,1,10) >= ? GROUP BY d",
                (start,)).fetchall()
        else:
            rows = con.execute(
                "SELECT substr(created_at,1,10) d, COUNT(*) c FROM jobs WHERE substr(created_at,1,10) >= ? AND user_id = ? GROUP BY d",
                (start, user_id)).fetchall()
    counts = {r["d"]: r["c"] for r in rows}
    out = []
    for i in range(days):
        d = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days - 1 - i)).date().isoformat()
        out.append({"date": d, "count": counts.get(d, 0)})
    return out


def top_tables(limit: int = 10, user_id: int | None = None) -> list[dict]:
    import re as _re
    # Matches legacy 8-digit ("_YYYYMMDD") and current 12-digit ("_YYMMDDHHMMSS") suffixes.
    pat = _re.compile(r"CREATE TABLE BKP_(.+?)_\d{8,14}\s", _re.IGNORECASE)
    counts: dict[str, int] = {}
    with connect() as con:
        if user_id is None:
            rows = con.execute("SELECT backup_sql_file FROM jobs WHERE backup_sql_file IS NOT NULL").fetchall()
        else:
            rows = con.execute("SELECT backup_sql_file FROM jobs WHERE backup_sql_file IS NOT NULL AND user_id=?", (user_id,)).fetchall()
    for r in rows:
        p = Path(r["backup_sql_file"])
        if not p.exists():
            continue
        try:
            with p.open(encoding="utf-8", errors="ignore") as fh:
                for i, line in enumerate(fh):
                    if i >= 2000:
                        break
                    m = pat.search(line)
                    if m:
                        counts[m.group(1)] = counts.get(m.group(1), 0) + 1
        except OSError:
            continue
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [{"table": t, "count": c} for t, c in ranked]


# ----------------------------------------------------------------------
# Password resets
# ----------------------------------------------------------------------
def hash_token(token: str) -> str:
    """Hash a token before storing — uses werkzeug PBKDF2."""
    return generate_password_hash(token)


def create_password_reset(user_id: int, ttl_minutes: int, ip: str | None) -> str:
    """Create a reset token. Returns the cleartext token to email/show — only stored hashed."""
    token = "rst_" + secrets.token_urlsafe(32)
    with connect() as con:
        con.execute(
            """INSERT INTO password_resets (user_id, token_hash, created_at, expires_at, ip_address)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id,
             hash_token(token),
             _now(),
             (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds") + "Z",
             ip),
        )
        con.commit()
    return token


def consume_password_reset(token: str) -> sqlite3.Row | None:
    """If token is valid (unused, not expired) return the user row and mark the token used."""
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM password_resets WHERE used_at IS NULL AND expires_at > ?",
            (_now(),),
        ).fetchall()
        match = next((r for r in rows if check_password_hash(r["token_hash"], token)), None)
        if match is None:
            return None
        con.execute("UPDATE password_resets SET used_at = ? WHERE id = ?", (_now(), match["id"]))
        con.commit()
        return con.execute("SELECT * FROM users WHERE id = ?", (match["user_id"],)).fetchone()


# ----------------------------------------------------------------------
# Watched sources (Phase 2 & 3 — local folder / Git repo schedulers)
# ----------------------------------------------------------------------
def list_watched_sources() -> list[sqlite3.Row]:
    with connect() as con:
        return con.execute(
            """SELECT s.*, u.username AS owner_username
               FROM watched_sources s
               LEFT JOIN users u ON u.id = s.owner_user_id
               ORDER BY s.enabled DESC, s.name"""
        ).fetchall()


def get_watched_source(source_id: int) -> sqlite3.Row | None:
    with connect() as con:
        return con.execute(
            "SELECT * FROM watched_sources WHERE id = ?", (source_id,)
        ).fetchone()


def create_watched_source(*, name: str, kind: str, source_path: str, dest_path: str,
                          config_json: str, interval_kind: str, interval_value: str,
                          schedule_json: str,
                          owner_user_id: int, created_by_user_id: int) -> int:
    with connect() as con:
        cur = con.execute(
            """INSERT INTO watched_sources
               (name, kind, source_path, dest_path, config_json,
                interval_kind, interval_value, schedule_json, enabled,
                owner_user_id, created_by_user_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (name, kind, source_path, dest_path, config_json,
             interval_kind, interval_value, schedule_json,
             owner_user_id, created_by_user_id, _now()),
        )
        con.commit()
        return cur.lastrowid


def update_watched_source(source_id: int, *, name: str, source_path: str, dest_path: str,
                          config_json: str, interval_kind: str, interval_value: str,
                          schedule_json: str, enabled: bool) -> None:
    with connect() as con:
        con.execute(
            """UPDATE watched_sources
               SET name = ?, source_path = ?, dest_path = ?, config_json = ?,
                   interval_kind = ?, interval_value = ?, schedule_json = ?, enabled = ?
               WHERE id = ?""",
            (name, source_path, dest_path, config_json,
             interval_kind, interval_value, schedule_json,
             1 if enabled else 0,
             source_id),
        )
        con.commit()


def set_watched_source_pause(source_id: int, pause_until: str | None) -> None:
    """Patch only the pause_until field inside schedule_json (no full edit needed)."""
    row = get_watched_source(source_id)
    if row is None or not row["schedule_json"]:
        return
    import json
    sched = json.loads(row["schedule_json"])
    sched["pause_until"] = pause_until
    with connect() as con:
        con.execute(
            "UPDATE watched_sources SET schedule_json = ? WHERE id = ?",
            (json.dumps(sched), source_id),
        )
        con.commit()


def delete_watched_source(source_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM watched_sources WHERE id = ?", (source_id,))
        con.commit()


def record_watched_source_run(source_id: int, status: str, message: str | None) -> None:
    with connect() as con:
        con.execute(
            """UPDATE watched_sources
               SET last_run_at = ?, last_run_status = ?, last_run_message = ?
               WHERE id = ?""",
            (_now(), status, message, source_id),
        )
        con.commit()


def file_already_processed(source_id: int, file_hash: str) -> bool:
    with connect() as con:
        row = con.execute(
            "SELECT 1 FROM processed_files WHERE watched_source_id = ? AND file_hash = ?",
            (source_id, file_hash),
        ).fetchone()
    return row is not None


def mark_file_processed(source_id: int, file_hash: str, original_path: str,
                        job_id: int | None) -> None:
    with connect() as con:
        con.execute(
            """INSERT OR IGNORE INTO processed_files
               (watched_source_id, file_hash, original_path, job_id, processed_at)
               VALUES (?, ?, ?, ?, ?)""",
            (source_id, file_hash, original_path, job_id, _now()),
        )
        con.commit()


# ----------------------------------------------------------------------
# History filters (Phase 1)
# ----------------------------------------------------------------------
def search_jobs(*, user_id: int | None,
                q: str | None = None,
                prod_from: str | None = None,
                prod_to: str | None = None,
                status: str | None = None,
                limit: int = 200) -> list[sqlite3.Row]:
    where = []
    args: list = []
    if user_id is not None:
        where.append("user_id = ?")
        args.append(user_id)
    if q:
        where.append("(enhancement_name LIKE ? OR input_name LIKE ?)")
        args.extend([f"%{q}%", f"%{q}%"])
    if prod_from:
        where.append("prod_date >= ?")
        args.append(prod_from)
    if prod_to:
        where.append("prod_date <= ?")
        args.append(prod_to)
    if status:
        where.append("status = ?")
        args.append(status)
    clause = "WHERE " + " AND ".join(where) if where else ""
    args.append(limit)
    sql = f"""
        SELECT j.*, u.username AS owner_username, ws.name AS source_name
        FROM jobs j
        LEFT JOIN users u           ON u.id = j.user_id
        LEFT JOIN watched_sources ws ON ws.id = j.watched_source_id
        {clause}
        ORDER BY j.created_at DESC
        LIMIT ?
    """
    with connect() as con:
        return con.execute(sql, tuple(args)).fetchall()


# ----------------------------------------------------------------------
# Settings (key-value, runtime feature flags)
# ----------------------------------------------------------------------
def setting_get(key: str, default: str | None = None) -> str | None:
    with connect() as con:
        row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def setting_set(key: str, value: str) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        con.commit()


# ----------------------------------------------------------------------
# Teams
# ----------------------------------------------------------------------
def create_team(name: str, description: str | None, created_by: int) -> int:
    with connect() as con:
        cur = con.execute(
            "INSERT INTO teams (name, description, created_at, created_by_user_id) VALUES (?, ?, ?, ?)",
            (name, description, _now(), created_by),
        )
        con.commit()
        return cur.lastrowid


def get_team(team_id: int) -> sqlite3.Row | None:
    with connect() as con:
        return con.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()


def get_team_by_name(name: str) -> sqlite3.Row | None:
    with connect() as con:
        return con.execute("SELECT * FROM teams WHERE name = ?", (name,)).fetchone()


def list_teams() -> list[sqlite3.Row]:
    with connect() as con:
        return con.execute(
            """SELECT t.*,
                      (SELECT COUNT(*) FROM users WHERE team_id = t.id AND approval_status = 'approved') AS member_count,
                      (SELECT username FROM users WHERE team_id = t.id AND team_role = 'leader' LIMIT 1) AS leader_username
               FROM teams t ORDER BY t.name"""
        ).fetchall()


def update_team(team_id: int, name: str, description: str | None) -> None:
    with connect() as con:
        con.execute(
            "UPDATE teams SET name = ?, description = ? WHERE id = ?",
            (name, description, team_id),
        )
        con.commit()


def delete_team(team_id: int) -> None:
    with connect() as con:
        # Detach members before deleting
        con.execute("UPDATE users SET team_id = NULL, team_role = 'member', approval_status = 'approved' WHERE team_id = ?", (team_id,))
        con.execute("DELETE FROM join_requests WHERE team_id = ?", (team_id,))
        con.execute("DELETE FROM teams WHERE id = ?", (team_id,))
        con.commit()


def get_team_members(team_id: int) -> list[sqlite3.Row]:
    with connect() as con:
        return con.execute(
            """SELECT * FROM users WHERE team_id = ? AND approval_status = 'approved' ORDER BY team_role DESC, username""",
            (team_id,),
        ).fetchall()


def get_team_leader(team_id: int) -> sqlite3.Row | None:
    with connect() as con:
        return con.execute(
            "SELECT * FROM users WHERE team_id = ? AND team_role = 'leader' LIMIT 1",
            (team_id,),
        ).fetchone()


def assign_team_leader(user_id: int, team_id: int) -> None:
    with connect() as con:
        # Demote current leader to member
        con.execute(
            "UPDATE users SET team_role = 'member' WHERE team_id = ? AND team_role = 'leader'",
            (team_id,),
        )
        # Promote the target user
        con.execute(
            "UPDATE users SET team_id = ?, team_role = 'leader', approval_status = 'approved' WHERE id = ?",
            (team_id, user_id),
        )
        con.commit()


def remove_from_team(user_id: int) -> None:
    with connect() as con:
        con.execute(
            "UPDATE users SET team_id = NULL, team_role = 'member', approval_status = 'approved' WHERE id = ?",
            (user_id,),
        )
        con.commit()


# ----------------------------------------------------------------------
# Join requests
# ----------------------------------------------------------------------
def create_join_request(user_id: int, team_id: int) -> int:
    with connect() as con:
        cur = con.execute(
            "INSERT INTO join_requests (user_id, team_id, status, requested_at) VALUES (?, ?, 'pending', ?)",
            (user_id, team_id, _now()),
        )
        con.commit()
        return cur.lastrowid


def get_join_request(request_id: int) -> sqlite3.Row | None:
    with connect() as con:
        return con.execute(
            """SELECT jr.*, u.username, u.email, u.full_name, u.employee_code,
                      t.name AS team_name
               FROM join_requests jr
               JOIN users u  ON u.id  = jr.user_id
               JOIN teams t  ON t.id  = jr.team_id
               WHERE jr.id = ?""",
            (request_id,),
        ).fetchone()


def list_join_requests_for_team(team_id: int) -> list[sqlite3.Row]:
    with connect() as con:
        return con.execute(
            """SELECT jr.*, u.username, u.email, u.full_name, u.employee_code
               FROM join_requests jr
               JOIN users u ON u.id = jr.user_id
               WHERE jr.team_id = ? AND jr.status = 'pending'
               ORDER BY jr.requested_at""",
            (team_id,),
        ).fetchall()


def list_all_join_requests() -> list[sqlite3.Row]:
    with connect() as con:
        return con.execute(
            """SELECT jr.*, u.username, u.email, u.full_name, u.employee_code,
                      t.name AS team_name
               FROM join_requests jr
               JOIN users u ON u.id = jr.user_id
               JOIN teams t ON t.id = jr.team_id
               ORDER BY jr.status ASC, jr.requested_at DESC"""
        ).fetchall()


def approve_join_request(request_id: int, reviewed_by: int) -> None:
    with connect() as con:
        req = con.execute("SELECT * FROM join_requests WHERE id = ?", (request_id,)).fetchone()
        if req is None:
            return
        con.execute(
            "UPDATE join_requests SET status = 'approved', reviewed_at = ?, reviewed_by_user_id = ? WHERE id = ?",
            (_now(), reviewed_by, request_id),
        )
        con.execute(
            "UPDATE users SET team_id = ?, approval_status = 'approved' WHERE id = ?",
            (req["team_id"], req["user_id"]),
        )
        con.commit()


def reject_join_request(request_id: int, reviewed_by: int) -> None:
    with connect() as con:
        req = con.execute("SELECT * FROM join_requests WHERE id = ?", (request_id,)).fetchone()
        if req is None:
            return
        con.execute(
            "UPDATE join_requests SET status = 'rejected', reviewed_at = ?, reviewed_by_user_id = ? WHERE id = ?",
            (_now(), reviewed_by, request_id),
        )
        con.execute(
            "UPDATE users SET approval_status = 'rejected', team_id = NULL WHERE id = ?",
            (req["user_id"],),
        )
        con.commit()


def get_pending_request_for_user(user_id: int) -> sqlite3.Row | None:
    with connect() as con:
        return con.execute(
            """SELECT jr.*, t.name AS team_name
               FROM join_requests jr
               JOIN teams t ON t.id = jr.team_id
               WHERE jr.user_id = ? AND jr.status = 'pending'
               ORDER BY jr.requested_at DESC LIMIT 1""",
            (user_id,),
        ).fetchone()


def get_latest_join_request_for_user(user_id: int) -> sqlite3.Row | None:
    """Return the most recent join request for a user regardless of status."""
    with connect() as con:
        return con.execute(
            """SELECT jr.*, t.name AS team_name
               FROM join_requests jr
               JOIN teams t ON t.id = jr.team_id
               WHERE jr.user_id = ?
               ORDER BY jr.requested_at DESC LIMIT 1""",
            (user_id,),
        ).fetchone()


def count_pending_join_requests() -> int:
    with connect() as con:
        return con.execute(
            "SELECT COUNT(*) c FROM join_requests WHERE status = 'pending'"
        ).fetchone()["c"]


def stats_for_team(team_id: int) -> dict:
    with connect() as con:
        member_ids = [
            r["id"] for r in con.execute(
                "SELECT id FROM users WHERE team_id = ? AND approval_status = 'approved'",
                (team_id,),
            ).fetchall()
        ]
        if not member_ids:
            return {"jobs": 0, "deletes": 0, "downloads": 0, "members": 0}
        ph = ",".join("?" * len(member_ids))
        jobs = con.execute(
            f"SELECT COUNT(*) c FROM jobs WHERE user_id IN ({ph})", member_ids
        ).fetchone()["c"]
        deletes = con.execute(
            f"SELECT COALESCE(SUM(delete_count),0) c FROM jobs WHERE user_id IN ({ph})", member_ids
        ).fetchone()["c"]
        downloads = con.execute(
            f"SELECT COUNT(*) c FROM downloads WHERE user_id IN ({ph})", member_ids
        ).fetchone()["c"]
    return {"jobs": jobs, "deletes": deletes, "downloads": downloads, "members": len(member_ids)}


def list_jobs_for_team(team_id: int, limit: int = 10) -> list[sqlite3.Row]:
    with connect() as con:
        return con.execute(
            """SELECT j.*, u.username
               FROM jobs j
               JOIN users u ON u.id = j.user_id
               WHERE u.team_id = ? AND u.approval_status = 'approved'
               ORDER BY j.created_at DESC LIMIT ?""",
            (team_id, limit),
        ).fetchall()


def jobs_per_day_team(team_id: int, days: int = 30) -> list[dict]:
    start = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days - 1)).date().isoformat()
    with connect() as con:
        rows = con.execute(
            """SELECT substr(j.created_at,1,10) d, COUNT(*) c
               FROM jobs j JOIN users u ON u.id = j.user_id
               WHERE u.team_id = ? AND u.approval_status = 'approved'
                 AND substr(j.created_at,1,10) >= ?
               GROUP BY d""",
            (team_id, start),
        ).fetchall()
    counts = {r["d"]: r["c"] for r in rows}
    out = []
    for i in range(days):
        d = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days - 1 - i)).date().isoformat()
        out.append({"date": d, "count": counts.get(d, 0)})
    return out


# ----------------------------------------------------------------------
# Notifications
# ----------------------------------------------------------------------
def create_notification(user_id: int, kind: str, message: str,
                        link: str | None = None, ref_id: int | None = None) -> int:
    with connect() as con:
        cur = con.execute(
            "INSERT INTO notifications (user_id, kind, message, link, created_at, ref_id) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, kind, message, link, _now(), ref_id),
        )
        con.commit()
        return cur.lastrowid


def list_notifications(user_id: int, limit: int = 20) -> list[sqlite3.Row]:
    with connect() as con:
        return con.execute(
            "SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def count_unread_notifications(user_id: int) -> int:
    with connect() as con:
        return con.execute(
            "SELECT COUNT(*) c FROM notifications WHERE user_id = ? AND is_read = 0",
            (user_id,),
        ).fetchone()["c"]


def mark_notifications_read(user_id: int) -> None:
    with connect() as con:
        con.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ?", (user_id,))
        con.commit()


def mark_notification_read(notification_id: int) -> None:
    with connect() as con:
        con.execute("UPDATE notifications SET is_read = 1 WHERE id = ?", (notification_id,))
        con.commit()
