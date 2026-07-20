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
    last_password_change_at  TEXT,
    team_id                  INTEGER,
    team_role                TEXT DEFAULT 'member',
    approval_status          TEXT DEFAULT 'approved',
    employee_code            TEXT,
    full_name                TEXT,
    avatar_filename          TEXT,
    failed_login_count       INTEGER NOT NULL DEFAULT 0,
    locked_until             TEXT,
    must_change_password     INTEGER NOT NULL DEFAULT 0,
    totp_secret              TEXT,
    totp_enabled             INTEGER NOT NULL DEFAULT 0
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
    bundle_file         TEXT,            -- ZIP of all artefacts in numbered folder structure
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

-- Dynamic tool portal: every tool card on the dashboard is a row here.
-- launch_type / launch_config describe how the tool is opened:
--   internal      -> {"endpoint": "abr.dashboard"}     (Flask blueprint endpoint)
--   external_url  -> {"url": "https://jira.corp/"}      (opens in a new tab)
--   folder_path   -> {"path": "ReportGenerator/", "port": 5010}  (Phase 2 runner)
--   executable    -> {"cmd": "C:/tools/tool.exe"}       (Phase 2 runner)
CREATE TABLE IF NOT EXISTS portal_tools (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    slug                TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    description         TEXT,
    icon                TEXT NOT NULL DEFAULT 'apps',
    icon_type           TEXT NOT NULL DEFAULT 'symbol',    -- 'symbol' (material) | 'image' (uploaded)
    tags_json           TEXT,                              -- JSON array of strings
    status              TEXT NOT NULL DEFAULT 'live',      -- 'live' | 'soon'
    launch_type         TEXT NOT NULL DEFAULT 'internal',  -- internal|external_url|folder_path|executable
    launch_config       TEXT,                              -- JSON, shape depends on launch_type
    display_order       INTEGER NOT NULL DEFAULT 0,
    is_enabled          INTEGER NOT NULL DEFAULT 1,
    requires_team       INTEGER NOT NULL DEFAULT 0,        -- 0 = visible to everyone; 1 = team-gated
    created_at          TEXT NOT NULL,
    updated_at          TEXT,
    created_by_user_id  INTEGER
);

-- Which teams may see a team-gated tool. A row with team_id IS NULL grants the
-- tool to every team. Only consulted when portal_tools.requires_team = 1.
CREATE TABLE IF NOT EXISTS tool_access (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_id     INTEGER NOT NULL,
    team_id     INTEGER,
    granted_at  TEXT NOT NULL,
    granted_by  INTEGER,
    FOREIGN KEY (tool_id) REFERENCES portal_tools(id),
    FOREIGN KEY (team_id) REFERENCES teams(id),
    UNIQUE (tool_id, team_id)
);

-- Team-leader "further restriction": deny a specific tool to a specific member.
-- Absence of a row (or restricted = 0) means the member keeps normal access.
CREATE TABLE IF NOT EXISTS tool_user_restrictions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_id         INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    restricted      INTEGER NOT NULL DEFAULT 1,
    set_by_user_id  INTEGER,
    set_at          TEXT NOT NULL,
    FOREIGN KEY (tool_id) REFERENCES portal_tools(id),
    FOREIGN KEY (user_id) REFERENCES users(id),
    UNIQUE (tool_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_portal_tools_order    ON portal_tools(display_order);
CREATE INDEX IF NOT EXISTS idx_tool_access_tool      ON tool_access(tool_id);
CREATE INDEX IF NOT EXISTS idx_tool_access_team      ON tool_access(team_id);
CREATE INDEX IF NOT EXISTS idx_tool_restrict_user    ON tool_user_restrictions(user_id);

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

-- Enterprise audit trail: one row per critical administrative/security event.
-- Append-only by convention (no update/delete API). ``username``/``actor_role``
-- are denormalized so a record stays meaningful even after the user is deleted.
--   category ∈ auth | user | team | tool | approval | config | security | general
--   status   ∈ success | failure
-- old_value / new_value / details hold JSON (or plain text) captured at the seam.
CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    user_id       INTEGER,                 -- actor (NULL for anonymous / system)
    username      TEXT,                    -- denormalized actor name (survives deletion)
    actor_role    TEXT,                    -- 'admin' | 'user' | ... at time of action
    ip_address    TEXT,
    category      TEXT NOT NULL DEFAULT 'general',
    action        TEXT NOT NULL,           -- dotted verb, e.g. 'user.role_changed'
    target_type   TEXT,                    -- 'user' | 'team' | 'tool' | 'source' | ...
    target_id     INTEGER,
    target_label  TEXT,                    -- human label of the target (name/username)
    old_value     TEXT,                    -- JSON/text — previous state (where applicable)
    new_value     TEXT,                    -- JSON/text — new state (where applicable)
    details       TEXT,                    -- JSON/text — extra context
    status        TEXT NOT NULL DEFAULT 'success'
);

CREATE INDEX IF NOT EXISTS idx_audit_created   ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_category  ON audit_log(category);
CREATE INDEX IF NOT EXISTS idx_audit_action    ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_user      ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_status    ON audit_log(status);

-- Tool usage analytics: one row per tool launch (opened from a card or the
-- sidebar via the central launcher). tool_slug is denormalized so analytics
-- survive a tool being deleted; team_id snapshots the user's team at launch.
CREATE TABLE IF NOT EXISTS tool_launches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_id      INTEGER,
    tool_slug    TEXT NOT NULL,
    user_id      INTEGER,
    team_id      INTEGER,
    launched_at  TEXT NOT NULL,
    ip_address   TEXT
);
CREATE INDEX IF NOT EXISTS idx_launches_tool  ON tool_launches(tool_id);
CREATE INDEX IF NOT EXISTS idx_launches_user  ON tool_launches(user_id);
CREATE INDEX IF NOT EXISTS idx_launches_team  ON tool_launches(team_id);
CREATE INDEX IF NOT EXISTS idx_launches_at    ON tool_launches(launched_at DESC);

-- Security: raw authentication attempts, for per-IP rate limiting and forensics.
-- (Per-user lockout state lives on the users row: failed_login_count/locked_until.)
CREATE TABLE IF NOT EXISTS login_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT,
    ip_address   TEXT,
    success      INTEGER NOT NULL DEFAULT 0,
    attempted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip_address, attempted_at);
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
    "ALTER TABLE users ADD COLUMN avatar_filename TEXT",
    # security hardening migrations
    "ALTER TABLE users ADD COLUMN failed_login_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN locked_until TEXT",
    "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN totp_secret TEXT",
    "ALTER TABLE users ADD COLUMN totp_enabled INTEGER NOT NULL DEFAULT 0",
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
                approval_status: str = "approved",
                must_change_password: bool = False) -> int:
    pw_hash = generate_password_hash(password)
    with connect() as con:
        cur = con.execute(
            """INSERT INTO users (username, email, password_hash, role, created_at,
                                  created_by_user_id, last_password_change_at,
                                  full_name, employee_code, team_id, approval_status,
                                  must_change_password)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (username, email, pw_hash, role, _now(), created_by, _now(),
             full_name, employee_code, team_id, approval_status,
             1 if must_change_password else 0),
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


def set_user_avatar(user_id: int, filename: str | None) -> None:
    """Set (or clear, with None) a user's uploaded avatar filename."""
    with connect() as con:
        con.execute("UPDATE users SET avatar_filename = ? WHERE id = ?", (filename, user_id))
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
# Security: authentication checks, lockout, 2FA, forced password change
# ----------------------------------------------------------------------
def check_user_password(username: str, password: str) -> sqlite3.Row | None:
    """Pure credential check: return the row iff the account is active and the
    password matches. Does NOT touch last_login / failed counters — the caller
    orchestrates lockout, 2FA and login recording."""
    row = get_user_by_username(username)
    if row is None or not row["is_active"]:
        return None
    return row if check_password_hash(row["password_hash"], password) else None


def set_last_login(user_id: int) -> None:
    with connect() as con:
        con.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (_now(), user_id))
        con.commit()


def increment_failed_login(user_id: int) -> int:
    with connect() as con:
        con.execute("UPDATE users SET failed_login_count = COALESCE(failed_login_count,0) + 1 WHERE id = ?", (user_id,))
        con.commit()
        return con.execute("SELECT failed_login_count c FROM users WHERE id = ?", (user_id,)).fetchone()["c"]


def reset_failed_login(user_id: int) -> None:
    with connect() as con:
        con.execute("UPDATE users SET failed_login_count = 0, locked_until = NULL WHERE id = ?", (user_id,))
        con.commit()


def lock_user(user_id: int, minutes: int) -> str:
    until = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=minutes)).isoformat(timespec="seconds") + "Z"
    with connect() as con:
        con.execute("UPDATE users SET locked_until = ? WHERE id = ?", (until, user_id))
        con.commit()
    return until


def user_lock_remaining(row) -> int:
    """Seconds remaining on a user's lock (0 if not locked / expired)."""
    lu = _row_val(row, "locked_until")
    if not lu:
        return 0
    try:
        until = datetime.fromisoformat(lu.rstrip("Z"))
    except ValueError:
        return 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return max(0, int((until - now).total_seconds()))


def set_must_change_password(user_id: int, value: bool) -> None:
    with connect() as con:
        con.execute("UPDATE users SET must_change_password = ? WHERE id = ?", (1 if value else 0, user_id))
        con.commit()


def set_totp(user_id: int, secret: str | None, enabled: bool) -> None:
    with connect() as con:
        con.execute("UPDATE users SET totp_secret = ?, totp_enabled = ? WHERE id = ?",
                    (secret, 1 if enabled else 0, user_id))
        con.commit()


def record_login_attempt(username: str | None, ip: str | None, success: bool) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO login_attempts (username, ip_address, success, attempted_at) VALUES (?, ?, ?, ?)",
            (username, ip, 1 if success else 0, _now()),
        )
        con.commit()


def count_recent_attempts(ip: str | None, window_seconds: int) -> int:
    if not ip:
        return 0
    since = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=window_seconds)).isoformat(timespec="seconds") + "Z"
    with connect() as con:
        return con.execute(
            "SELECT COUNT(*) c FROM login_attempts WHERE ip_address = ? AND attempted_at >= ?",
            (ip, since),
        ).fetchone()["c"]


def _row_val(row, key, default=None):
    """Safe sqlite3.Row accessor (column may be absent on very old rows)."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


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


def search_jobs_for_team(team_id: int, *, q: str | None = None,
                         prod_from: str | None = None, prod_to: str | None = None,
                         status: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
    """Return jobs for all approved team members, with optional filters."""
    where = ["u.team_id = ?", "u.approval_status = 'approved'"]
    args: list = [team_id]
    if q:
        where.append("(j.enhancement_name LIKE ? OR j.input_name LIKE ? OR u.username LIKE ?)")
        args.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    if prod_from:
        where.append("j.prod_date >= ?")
        args.append(prod_from)
    if prod_to:
        where.append("j.prod_date <= ?")
        args.append(prod_to)
    if status:
        where.append("j.status = ?")
        args.append(status)
    clause = "WHERE " + " AND ".join(where)
    args.append(limit)
    sql = f"""
        SELECT j.*, u.username
        FROM jobs j
        JOIN users u ON u.id = j.user_id
        {clause}
        ORDER BY j.created_at DESC
        LIMIT ?
    """
    with connect() as con:
        return con.execute(sql, tuple(args)).fetchall()


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


# ----------------------------------------------------------------------
# Portal tools (dynamic, admin-managed tool registry)
# ----------------------------------------------------------------------
import re as _re_slug  # noqa: E402


def slugify(name: str) -> str:
    """Turn a display name into a URL-safe slug ('Report Generator' -> 'report-generator')."""
    s = _re_slug.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "tool"


def list_portal_tools() -> list[sqlite3.Row]:
    """Every tool, enabled or not — for the admin management screen."""
    with connect() as con:
        return con.execute(
            "SELECT * FROM portal_tools ORDER BY display_order, name"
        ).fetchall()


def get_portal_tool(tool_id: int) -> sqlite3.Row | None:
    with connect() as con:
        return con.execute("SELECT * FROM portal_tools WHERE id = ?", (tool_id,)).fetchone()


def get_portal_tool_by_slug(slug: str) -> sqlite3.Row | None:
    with connect() as con:
        return con.execute("SELECT * FROM portal_tools WHERE slug = ?", (slug,)).fetchone()


def create_portal_tool(*, slug: str, name: str, description: str | None,
                       icon: str, icon_type: str, tags: list[str] | None,
                       status: str, launch_type: str, launch_config: dict | None,
                       display_order: int, requires_team: bool,
                       created_by: int | None) -> int:
    with connect() as con:
        cur = con.execute(
            """INSERT INTO portal_tools
               (slug, name, description, icon, icon_type, tags_json, status,
                launch_type, launch_config, display_order, is_enabled,
                requires_team, created_at, created_by_user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (slug, name, description, icon, icon_type,
             json.dumps(tags or []), status, launch_type,
             json.dumps(launch_config or {}), display_order,
             1 if requires_team else 0, _now(), created_by),
        )
        con.commit()
        return cur.lastrowid


def update_portal_tool(tool_id: int, *, name: str, description: str | None,
                       icon: str, icon_type: str, tags: list[str] | None,
                       status: str, launch_type: str, launch_config: dict | None,
                       display_order: int, requires_team: bool) -> None:
    with connect() as con:
        con.execute(
            """UPDATE portal_tools
               SET name = ?, description = ?, icon = ?, icon_type = ?,
                   tags_json = ?, status = ?, launch_type = ?, launch_config = ?,
                   display_order = ?, requires_team = ?, updated_at = ?
               WHERE id = ?""",
            (name, description, icon, icon_type, json.dumps(tags or []),
             status, launch_type, json.dumps(launch_config or {}),
             display_order, 1 if requires_team else 0, _now(), tool_id),
        )
        con.commit()


def set_portal_tool_enabled(tool_id: int, enabled: bool) -> None:
    with connect() as con:
        con.execute("UPDATE portal_tools SET is_enabled = ?, updated_at = ? WHERE id = ?",
                    (1 if enabled else 0, _now(), tool_id))
        con.commit()


def delete_portal_tool(tool_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM tool_access WHERE tool_id = ?", (tool_id,))
        con.execute("DELETE FROM tool_user_restrictions WHERE tool_id = ?", (tool_id,))
        con.execute("DELETE FROM portal_tools WHERE id = ?", (tool_id,))
        con.commit()


def next_portal_tool_order() -> int:
    with connect() as con:
        row = con.execute("SELECT COALESCE(MAX(display_order), -1) + 1 AS n FROM portal_tools").fetchone()
    return int(row["n"])


def set_tool_order(ordered_ids: list[int]) -> None:
    """Persist a new tool ordering — display_order becomes each tool's index in
    the given id list. Ids not present are left untouched (then float to the end
    on next read since they keep their old, now-higher, order)."""
    with connect() as con:
        for i, tid in enumerate(ordered_ids):
            con.execute("UPDATE portal_tools SET display_order = ?, updated_at = ? WHERE id = ?",
                        (i, _now(), int(tid)))
        con.commit()


def seed_portal_tools(seed_list: list[dict]) -> None:
    """First-run population of portal_tools from the legacy hardcoded list.

    Idempotent: does nothing once the table has any rows, so it never fights
    with admin edits. Maps the old LANDING_TOOLS dict shape onto the new
    schema (endpoint -> internal launch_config)."""
    with connect() as con:
        count = con.execute("SELECT COUNT(*) c FROM portal_tools").fetchone()["c"]
        if count > 0:
            return
        order = 0
        seen: set[str] = set()
        for t in seed_list:
            endpoint = t.get("endpoint")
            slug = (endpoint.split(".")[0] if endpoint else slugify(t["name"]))
            base_slug = slug
            n = 2
            while slug in seen:
                slug = f"{base_slug}-{n}"; n += 1
            seen.add(slug)
            launch_config = {"endpoint": endpoint} if endpoint else {}
            con.execute(
                """INSERT OR IGNORE INTO portal_tools
                   (slug, name, description, icon, icon_type, tags_json, status,
                    launch_type, launch_config, display_order, is_enabled,
                    requires_team, created_at)
                   VALUES (?, ?, ?, ?, 'symbol', ?, ?, 'internal', ?, ?, 1, 0, ?)""",
                (slug, t["name"], t.get("desc"), t.get("icon", "apps"),
                 json.dumps(t.get("tags", [])), t.get("status", "live"),
                 json.dumps(launch_config), order, _now()),
            )
            order += 1
        con.commit()


# ----------------------------------------------------------------------
# Tool access (team grants) + per-user restrictions
# ----------------------------------------------------------------------
def list_tool_access_team_ids(tool_id: int) -> list:
    """Team ids granted access to a tool. A stored NULL (all-teams) is returned as the string 'all'."""
    with connect() as con:
        rows = con.execute("SELECT team_id FROM tool_access WHERE tool_id = ?", (tool_id,)).fetchall()
    return ["all" if r["team_id"] is None else r["team_id"] for r in rows]


def set_tool_access(tool_id: int, team_ids: list, granted_by: int | None) -> None:
    """Replace the full set of team grants for a tool. Pass ['all'] to grant to every team."""
    with connect() as con:
        con.execute("DELETE FROM tool_access WHERE tool_id = ?", (tool_id,))
        for tid in team_ids:
            db_tid = None if tid in ("all", None) else int(tid)
            con.execute(
                "INSERT OR IGNORE INTO tool_access (tool_id, team_id, granted_at, granted_by) VALUES (?, ?, ?, ?)",
                (tool_id, db_tid, _now(), granted_by),
            )
        con.commit()


def _granted_tool_ids_for_team(con, team_id: int | None) -> set:
    """Internal: tool ids whose team-grant matches this team (or an all-teams grant)."""
    granted = set()
    for r in con.execute("SELECT tool_id, team_id FROM tool_access").fetchall():
        if r["team_id"] is None or (team_id is not None and r["team_id"] == team_id):
            granted.add(r["tool_id"])
    return granted


def set_user_tool_restriction(tool_id: int, user_id: int, restricted: bool,
                              set_by: int | None) -> None:
    with connect() as con:
        if restricted:
            con.execute(
                """INSERT INTO tool_user_restrictions (tool_id, user_id, restricted, set_by_user_id, set_at)
                   VALUES (?, ?, 1, ?, ?)
                   ON CONFLICT(tool_id, user_id) DO UPDATE SET restricted = 1, set_by_user_id = excluded.set_by_user_id, set_at = excluded.set_at""",
                (tool_id, user_id, set_by, _now()),
            )
        else:
            con.execute("DELETE FROM tool_user_restrictions WHERE tool_id = ? AND user_id = ?",
                        (tool_id, user_id))
        con.commit()


def get_restricted_tool_ids_for_user(user_id: int) -> set:
    with connect() as con:
        rows = con.execute(
            "SELECT tool_id FROM tool_user_restrictions WHERE user_id = ? AND restricted = 1",
            (user_id,),
        ).fetchall()
    return {r["tool_id"] for r in rows}


def list_tools_for_team(team_id: int | None) -> list[sqlite3.Row]:
    """Enabled tools visible to a team as a whole (global tools + team-granted tools)."""
    with connect() as con:
        tools = con.execute(
            "SELECT * FROM portal_tools WHERE is_enabled = 1 ORDER BY display_order, name"
        ).fetchall()
        granted = _granted_tool_ids_for_team(con, team_id)
    return [t for t in tools if (not t["requires_team"]) or t["id"] in granted]


def list_accessible_tools(user) -> list[sqlite3.Row]:
    """Enabled tools the given user may see.

    user is a flask_login User (has .is_admin, .is_team_leader, .team_id, .id)
    or None for an anonymous visitor. Rules:
      - anonymous / no team  -> only tools with requires_team = 0
      - admin                -> all enabled tools
      - member / leader      -> global tools + tools granted to their team,
                                minus any leader-imposed per-user restriction
                                (leaders are never self-restricted)
    """
    with connect() as con:
        tools = con.execute(
            "SELECT * FROM portal_tools WHERE is_enabled = 1 ORDER BY display_order, name"
        ).fetchall()

        if user is None or not getattr(user, "is_authenticated", False):
            return [t for t in tools if not t["requires_team"]]

        if getattr(user, "is_admin", False):
            return list(tools)

        team_id = getattr(user, "team_id", None)
        granted = _granted_tool_ids_for_team(con, team_id)
        restricted: set = set()
        if not getattr(user, "is_team_leader", False):
            uid = int(getattr(user, "id"))
            rr = con.execute(
                "SELECT tool_id FROM tool_user_restrictions WHERE user_id = ? AND restricted = 1",
                (uid,),
            ).fetchall()
            restricted = {r["tool_id"] for r in rr}

    out = []
    for t in tools:
        if t["id"] in restricted:
            continue
        if not t["requires_team"] or t["id"] in granted:
            out.append(t)
    return out


def get_accessible_tool_slugs(user) -> set:
    return {t["slug"] for t in list_accessible_tools(user)}


# ----------------------------------------------------------------------
# Audit log (enterprise audit trail)
# ----------------------------------------------------------------------
def record_audit(*, action: str, category: str = "general",
                 user_id: int | None = None, username: str | None = None,
                 actor_role: str | None = None, ip_address: str | None = None,
                 target_type: str | None = None, target_id: int | None = None,
                 target_label: str | None = None,
                 old_value: str | None = None, new_value: str | None = None,
                 details: str | None = None, status: str = "success") -> int:
    """Insert one audit row. Low-level — most callers use the ``audit`` helper
    module which auto-captures the actor and client IP from the request."""
    with connect() as con:
        cur = con.execute(
            """INSERT INTO audit_log
               (created_at, user_id, username, actor_role, ip_address,
                category, action, target_type, target_id, target_label,
                old_value, new_value, details, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_now(), user_id, username, actor_role, ip_address,
             category, action, target_type, target_id, target_label,
             old_value, new_value, details, status),
        )
        con.commit()
        return cur.lastrowid


def _audit_where(*, q, category, action, user_id, status, date_from, date_to):
    """Build the shared WHERE clause + args for search/count."""
    where: list[str] = []
    args: list = []
    if q:
        where.append("(username LIKE ? OR action LIKE ? OR target_label LIKE ? "
                     "OR ip_address LIKE ? OR details LIKE ?)")
        like = f"%{q}%"
        args.extend([like, like, like, like, like])
    if category:
        where.append("category = ?"); args.append(category)
    if action:
        where.append("action = ?"); args.append(action)
    if user_id is not None:
        where.append("user_id = ?"); args.append(user_id)
    if status:
        where.append("status = ?"); args.append(status)
    if date_from:
        where.append("substr(created_at, 1, 10) >= ?"); args.append(date_from)
    if date_to:
        where.append("substr(created_at, 1, 10) <= ?"); args.append(date_to)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    return clause, args


def search_audit_log(*, q: str | None = None, category: str | None = None,
                     action: str | None = None, user_id: int | None = None,
                     status: str | None = None,
                     date_from: str | None = None, date_to: str | None = None,
                     limit: int = 100, offset: int = 0) -> list[sqlite3.Row]:
    clause, args = _audit_where(q=q, category=category, action=action,
                                user_id=user_id, status=status,
                                date_from=date_from, date_to=date_to)
    args.extend([limit, offset])
    with connect() as con:
        return con.execute(
            f"SELECT * FROM audit_log {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(args),
        ).fetchall()


def count_audit_log(*, q: str | None = None, category: str | None = None,
                    action: str | None = None, user_id: int | None = None,
                    status: str | None = None,
                    date_from: str | None = None, date_to: str | None = None) -> int:
    clause, args = _audit_where(q=q, category=category, action=action,
                                user_id=user_id, status=status,
                                date_from=date_from, date_to=date_to)
    with connect() as con:
        return con.execute(
            f"SELECT COUNT(*) c FROM audit_log {clause}", tuple(args)
        ).fetchone()["c"]


def distinct_audit_categories() -> list[str]:
    with connect() as con:
        rows = con.execute(
            "SELECT DISTINCT category FROM audit_log WHERE category IS NOT NULL ORDER BY category"
        ).fetchall()
    return [r["category"] for r in rows]


def distinct_audit_actions() -> list[str]:
    with connect() as con:
        rows = con.execute(
            "SELECT DISTINCT action FROM audit_log WHERE action IS NOT NULL ORDER BY action"
        ).fetchall()
    return [r["action"] for r in rows]


# ----------------------------------------------------------------------
# Tool usage analytics
# ----------------------------------------------------------------------
def record_tool_launch(tool_id: int | None, tool_slug: str, user_id: int | None,
                       team_id: int | None, ip: str | None) -> None:
    with connect() as con:
        con.execute(
            """INSERT INTO tool_launches (tool_id, tool_slug, user_id, team_id, launched_at, ip_address)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (tool_id, tool_slug, user_id, team_id, _now(), ip),
        )
        con.commit()


def analytics_overview(days: int = 30) -> dict:
    since = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).isoformat(timespec="seconds") + "Z"
    with connect() as con:
        total   = con.execute("SELECT COUNT(*) c FROM tool_launches").fetchone()["c"]
        window  = con.execute("SELECT COUNT(*) c FROM tool_launches WHERE launched_at >= ?", (since,)).fetchone()["c"]
        users   = con.execute("SELECT COUNT(DISTINCT user_id) c FROM tool_launches WHERE user_id IS NOT NULL").fetchone()["c"]
        tools   = con.execute("SELECT COUNT(DISTINCT tool_slug) c FROM tool_launches").fetchone()["c"]
    return {"total_launches": total, "window_launches": window,
            "active_users": users, "tools_used": tools, "window_days": days}


def analytics_by_tool() -> list[dict]:
    """Per-tool usage: launches, distinct users, last-accessed. Includes tools
    with zero launches (LEFT JOIN from the registry) so adoption gaps are visible."""
    with connect() as con:
        rows = con.execute(
            """SELECT p.id AS tool_id, p.name, p.slug, p.is_enabled, p.status,
                      COUNT(l.id)               AS launches,
                      COUNT(DISTINCT l.user_id) AS unique_users,
                      MAX(l.launched_at)        AS last_at
               FROM portal_tools p
               LEFT JOIN tool_launches l ON l.tool_id = p.id
               GROUP BY p.id
               ORDER BY launches DESC, p.name"""
        ).fetchall()
    return [dict(r) for r in rows]


def analytics_by_user(limit: int = 20) -> list[dict]:
    with connect() as con:
        rows = con.execute(
            """SELECT l.user_id, u.username,
                      COUNT(*) AS launches,
                      COUNT(DISTINCT l.tool_slug) AS distinct_tools,
                      MAX(l.launched_at) AS last_at
               FROM tool_launches l
               LEFT JOIN users u ON u.id = l.user_id
               WHERE l.user_id IS NOT NULL
               GROUP BY l.user_id
               ORDER BY launches DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def analytics_by_team(limit: int = 20) -> list[dict]:
    with connect() as con:
        rows = con.execute(
            """SELECT l.team_id, t.name AS team_name,
                      COUNT(*) AS launches,
                      COUNT(DISTINCT l.user_id) AS unique_users
               FROM tool_launches l
               LEFT JOIN teams t ON t.id = l.team_id
               WHERE l.team_id IS NOT NULL
               GROUP BY l.team_id
               ORDER BY launches DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def analytics_launches_per_day(days: int = 30) -> list[dict]:
    start = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days - 1)).date().isoformat()
    with connect() as con:
        rows = con.execute(
            """SELECT substr(launched_at,1,10) d, COUNT(*) c
               FROM tool_launches WHERE substr(launched_at,1,10) >= ? GROUP BY d""",
            (start,),
        ).fetchall()
    counts = {r["d"]: r["c"] for r in rows}
    out = []
    for i in range(days):
        d = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days - 1 - i)).date().isoformat()
        out.append({"date": d, "count": counts.get(d, 0)})
    return out
