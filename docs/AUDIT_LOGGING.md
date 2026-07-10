# Enterprise Audit Logging (Feature #1)

Comprehensive, append-only audit trail of critical administrative and security
events, searchable by authorized administrators only.

## What is captured

Every audit row records **who / when / where / what / before / after**:

| Field          | Meaning                                                        |
|----------------|----------------------------------------------------------------|
| `created_at`   | UTC timestamp (ISO-8601, `…Z`)                                 |
| `user_id`      | Acting user id (NULL for anonymous / system)                   |
| `username`     | Denormalized actor name — survives user deletion               |
| `actor_role`   | Actor's role at the time of the action                         |
| `ip_address`   | Client IP (honours `X-Forwarded-For`)                          |
| `category`     | `auth · user · team · tool · approval · config · security`     |
| `action`       | Dotted verb, e.g. `user.role_changed`, `auth.login_failed`     |
| `target_type` / `target_id` / `target_label` | The object acted on           |
| `old_value` / `new_value` | Previous / new state (JSON) where applicable        |
| `details`      | Extra context (JSON)                                           |
| `status`       | `success` or `failure`                                         |

### Events currently recorded

- **Auth:** login, failed login, logout (manual vs. session-timeout),
  password-reset requested/completed, password changed.
- **Users:** self-registration, admin create/update/delete, admin password
  reset, profile update.
- **Teams:** create/update/delete, leader assignment, member removal.
- **Approvals:** join-request approved/rejected (admin & team-leader paths).
- **Tools:** create/update/enable/disable/delete, access (team-gating) changes.
- **Config:** storage paths, retention, system limits, logo set/reset, watched
  source create/update/delete, audit CSV export.

## Schema changes

One new table (additive; created idempotently by `models.init_db()` via
`CREATE TABLE IF NOT EXISTS` — **no migration step required**, no existing table
altered):

```
audit_log(id, created_at, user_id, username, actor_role, ip_address,
          category, action, target_type, target_id, target_label,
          old_value, new_value, details, status)
```

Indexes: `created_at DESC`, `category`, `action`, `user_id`, `status`.

## New code

- **`shared/utilities/audit.py`** — the helper every blueprint imports
  (`import audit`). `audit.record(action, category=…, target_type=…, …)`
  auto-captures actor + IP from the Flask request context. **It never raises
  into the caller** — a failed audit write is logged and swallowed, so auditing
  can never break the operation it records. Works outside a request too
  (e.g. the scheduler): actor/IP resolve to `None`.
- **`models.py`** — `record_audit(...)`, `search_audit_log(...)`,
  `count_audit_log(...)`, `distinct_audit_categories()`,
  `distinct_audit_actions()`.

## New APIs / routes (admin-only, `@admin_required`)

| Route | Purpose |
|-------|---------|
| `GET /admin/audit` | Searchable viewer: filter by text, category, action, user, status, date range; paginated (100/page). |
| `GET /admin/audit/export.csv` | Compliance CSV export of the current filter (capped at 50k rows). |

Reached from **Admin console → Security & compliance → Audit log**.

## Configuration

None required. The trail is on by default and stored in the existing SQLite DB.

## Backward compatibility

Purely additive — new table, new module, new admin screen, and one-line hooks at
existing seams. No existing signature, route, or template changed. Existing
functionality is unaffected.

---

# Roadmap — remaining enterprise features (#2–#9)

Confirmed delivery model: **ship in risk-ascending order; full build per phase.**
Each phase is backward-compatible and independently shippable.

| # | Feature | Status | Approach summary | Regression risk |
|---|---------|--------|------------------|-----------------|
| 1 | Enterprise Audit Logging | ✅ done | `audit_log` table + `audit` helper + hooks + `/admin/audit` + CSV. | Low |
| 3 | Tool Usage Analytics | ✅ done | `tool_launches` table; central launcher records opens; `/admin/analytics`. | Low |
| 5 | Email Notifications | ✅ done | DB-configurable SMTP (`/admin/email`) + `$placeholder` templates + event wiring. | Low |
| 6 | Enhanced Tool Mgmt UI | ✅ done | SortableJS drag-drop → `/tools/reorder`; client search/type/status filters; dashboard search+tag filter; display_order auto. | Low |
| 8 | Live System Status | ✅ done | `health.py` probe framework (DB/scheduler/per-tool) + `/admin/status` + `status.json` auto-refresh. | Low |
| 4 | Real Tool Execution | ✅ done | `launcher.py` process manager (start/stop/restart, health probe, graceful shutdown, per-tool logs) + `/portal-admin/runtime`; launcher redirects to live web apps. | Medium |
| 7 | Security Hardening | ✅ done | `security.py`: password policy, per-IP rate limiting, account lockout, RFC-6238 TOTP 2FA, force-password-change, configurable session timeout, mandatory-admin-2FA + `/admin/security`. | Medium (auth) |
| 2 | Enterprise Auth & Provisioning | ⛔ removed | External identity providers (LDAP/OAuth2/SAML) were **removed** at the owner's request — the platform uses local username/password only. The **CSV bulk import** at `/admin/users/bulk` (local id/password provisioning) was kept. | — |
| 9 | Configurable DB Provider | ✅ done | `db_providers.py` (SQLite/PG/MySQL/MSSQL/Oracle/Mongo) + `db_migrate.py` (reflect→create→copy, progress, rollback) + `/admin/database` test/save/migrate. SQLite stays live; cut-over is documented below. | High (data layer) |

## Medium/high batch — new modules, deps & caveats (shipped)

**New modules:** `shared/utilities/security.py`, `shared/utilities/launcher.py`,
`database/database-config/db_providers.py`, `database/database-config/db_migrate.py`.
(#2's `auth_providers.py` was removed — see below.)
**New user columns (additive ALTERs):** `failed_login_count`, `locked_until`,
`must_change_password`, `totp_secret`, `totp_enabled`. **New table:** `login_attempts`.
**Settings keys:** `security.*`, `db.target_provider`, `db.config.<pid>`.
**New deps (requirements.txt):** SQLAlchemy, psycopg[binary], PyMySQL, oracledb, pymongo. Optional/deployment-specific: pyodbc (MS SQL). (ldap3/Authlib/requests were dropped with #2.)

**Caveats (by design, documented):**
- **#9 cut-over:** the migration *copies* all data to the target and SQLite stays the live datastore. The raw-SQL layer in `models.py` uses some SQLite-specific SQL, so *promoting* the target to primary is a deliberate future step (repository backends per dialect), not automatic on migrate — this keeps a failed migration from ever taking the app down.
- **#9 MS SQL** driver: registry + admin config are present; MSSQL connections activate once `pyodbc` is installed on the host (needs system ODBC libs).
- **#2 (enterprise auth) was removed** — login is local username/password only. The `security.*` password/lockout/2FA hardening (#7) still applies to local login. The bulk CSV import remains for provisioning local accounts.

## Low-risk batch — new tables, routes & files (shipped)

**Tables:** `audit_log`, `tool_launches` (both additive, `CREATE TABLE IF NOT EXISTS`).
**Settings keys:** `smtp.*`, `email.tpl.<event>.*`, `status.refresh_seconds`.
**New modules:** `shared/utilities/audit.py`, `shared/utilities/health.py`; rewritten `email_utils.py` (config-driven + template `notify()`).
**New admin routes:** `/admin/audit`(+`.csv`), `/admin/analytics`, `/admin/email`, `/admin/status`(+`.json`, `/status/settings`), `/portal-admin/tools/reorder`.
**Behaviour change (intentional, low-risk):** live tool cards + sidebar links now open via the central launcher `/launch/<slug>` (records analytics, then 302s to the real target). Broken internal endpoints still render non-clickable.

**#9 note:** the current data layer is raw `sqlite3` in `models.py`. Introducing
a provider/repository interface is a large, cross-cutting change and will be
staged carefully (interface first, SQLite adapter proving parity, then
additional providers + migration tooling) to guarantee zero data loss and no
behavioural drift.
