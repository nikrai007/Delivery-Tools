-- Delivery Toolbox - migration ledger (from models.py ALTERS).
-- Each runs before SCHEMA on every boot; "column already exists" errors are swallowed.

ALTER TABLE users ADD COLUMN created_by_user_id INTEGER
ALTER TABLE users ADD COLUMN last_login_at TEXT
ALTER TABLE users ADD COLUMN last_password_change_at TEXT
ALTER TABLE jobs  ADD COLUMN source TEXT NOT NULL DEFAULT 'web'
ALTER TABLE jobs  ADD COLUMN api_token_id INTEGER
ALTER TABLE jobs  ADD COLUMN alters_count INTEGER DEFAULT 0
ALTER TABLE jobs  ADD COLUMN procedures_count INTEGER DEFAULT 0
ALTER TABLE jobs  ADD COLUMN alters_sql_file TEXT
ALTER TABLE jobs  ADD COLUMN procedures_file TEXT
ALTER TABLE jobs  ADD COLUMN cleanup_sql_file TEXT
ALTER TABLE jobs  ADD COLUMN enhancement_name TEXT
ALTER TABLE jobs  ADD COLUMN prod_date TEXT
ALTER TABLE jobs  ADD COLUMN bundle_file TEXT
ALTER TABLE jobs  ADD COLUMN watched_source_id INTEGER
ALTER TABLE watched_sources ADD COLUMN schedule_json TEXT
