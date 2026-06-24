# Database

The platform's persistence layer — a single **SQLite** file shared by every tool.

## Layout

| Path | Role |
|---|---|
| `database-config/models.py` | The access layer: connection, `SCHEMA`, `ALTERS`, and all query helpers (`init_db`, `create_job`, `search_jobs`, `list_users`, watched-source CRUD, …). |
| `schema/schema.sql` | Canonical `CREATE TABLE` / `CREATE INDEX` script, exported from `models.SCHEMA`. Fresh installs run this verbatim. |
| `migrations/migrations.sql` | Forward-only `ALTER TABLE … ADD COLUMN …` ledger, exported from `models.ALTERS`. Runs **before** the schema on every boot; "column already exists" errors are swallowed. |
| `seed-data/` | Bootstrap notes (admin user). |
| `data/app.db` | The live database file. |

> `schema.sql` and `migrations.sql` are **generated artefacts** for reference and
> review. The runtime source of truth is the `SCHEMA` / `ALTERS` constants in
> `models.py`, applied by `init_db()` on every start (idempotent).

## Tables (summary)

| Table | Purpose |
|---|---|
| `users` | accounts: username, email, PBKDF2 hash, role, activity flags |
| `jobs` | one per run: input metadata, enhancement_name + prod_date, generated-file paths, bundle, status, source |
| `downloads` | audit log: `(job_id, user_id, filename, downloaded_at, ip)` |
| `password_resets` | short-lived hashed reset tokens |
| `watched_sources` | scheduler sources: kind (local/git), paths, `schedule_json`, enabled, last-run status |
| `processed_files` | SHA-256 idempotency manifest for the scheduler |
| `settings` | runtime key/value flags |

## Regenerating the exports

```bash
cd Delivery-Tools
python - <<'PY'
import sys; sys.path.insert(0, "database/database-config")
import models, pathlib
pathlib.Path("database/schema/schema.sql").write_text(models.SCHEMA, encoding="utf-8")
pathlib.Path("database/migrations/migrations.sql").write_text("\n".join(models.ALTERS), encoding="utf-8")
PY
```

## Outgrowing SQLite

Single process, < 50 writes/sec → stay on SQLite. If you need multiple
processes, HA, or replication, migrate to PostgreSQL — only the connection layer
in `models.py` changes; the SQL is standard.
