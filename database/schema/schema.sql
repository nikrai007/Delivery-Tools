-- Delivery Toolbox - canonical schema (generated from database-config/models.py SCHEMA)
-- Fresh installs run this verbatim via models.init_db().


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

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_user_created  ON jobs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_created       ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_enhancement   ON jobs(enhancement_name);
CREATE INDEX IF NOT EXISTS idx_jobs_prod_date     ON jobs(prod_date);
CREATE INDEX IF NOT EXISTS idx_downloads_job      ON downloads(job_id);
CREATE INDEX IF NOT EXISTS idx_resets_user        ON password_resets(user_id);
CREATE INDEX IF NOT EXISTS idx_tokens_prefix      ON api_tokens(prefix);
CREATE INDEX IF NOT EXISTS idx_processed_source   ON processed_files(watched_source_id);
