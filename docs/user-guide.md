# User Guide — Delivery Toolbox

## The hub

Open the site root (`/`) — the **Delivery Toolbox** landing page. It lists every
available tool as a card. Live tools (a green **LIVE** pill) are clickable;
others show **SOON**. Use the theme toggle (top right) to switch light/dark — your
choice is remembered. Tip: append `?theme=dark` or `?theme=light` to deep-link a
theme.

## Signing in

- **Sign in** / **Register** from the navbar or any tool card.
- Forgot your password? Use **Forgot password** — you'll get a reset link by email
  (if the account exists; the message is the same either way, by design).

## Tool: AutoBackupRevert

The first live tool. It turns an Oracle migration bundle into a matched set of
rollback scripts.

### Manual run
1. Sign in → click **AutoBackupRevert** (or **Dashboard**).
2. **New job** → fill the two mandatory fields: **Enhancement name** and
   **Production loading date**.
3. Upload a `.sql` / `.zip` / `.7z` (≤ 500 MB) or point at a server-side path.
4. **Review** the detected `delete.sql`.
5. **Generate** → download the single **BUNDLE_*.zip** containing
   `BACKUP.sql`, `REVERT.sql`, `CLEANUP.sql`, `ALTERS.sql`, `PROCEDURES.txt`,
   the original source, and a `MANIFEST.json`.

### What the DBA does with the bundle
Run `BACKUP.sql` first (snapshots affected rows), then the migration. If a
rollback is needed, run `REVERT.sql` (FK-safe by construction). After the
rollback window passes, run `CLEANUP.sql` to drop the snapshot tables.

### History
**History** lists your jobs with filters (search, production-date range, status).
Admins can pass `?all=1` to see everyone's jobs.

## Admin

Admins get extra navigation:
- **User activity** — all users' jobs, KPIs, charts.
- **Manage users** — create / activate / deactivate / reset passwords.
- **Watched sources** — register a local folder or Git repo for the scheduler to
  poll. Configure cadence (specific times on chosen days, every-N-minutes, or
  cron), with a live "next 5 fires" preview. **Run now**, **Snooze 24h**, and
  **Resume** are available per source.

For the full AutoBackupRevert reference (architecture, schema, scheduler design,
security posture, roadmap), see
[auto-backup-revert-tool/documentation.md](../auto-backup-revert-tool/documentation.md).
