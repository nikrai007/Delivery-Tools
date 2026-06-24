# User Guide — Delivery Toolbox

## The hub

Open the site root (`/`) — the **Delivery Toolbox** landing page. It lists every
available tool as a card. Live tools (a green **LIVE** pill) are clickable;
others show **SOON**. Use the theme toggle (top right) to switch light/dark — your
choice is remembered. Tip: append `?theme=dark` or `?theme=light` to deep-link a
theme.

## Signing in

- **Sign in** / **Register** from the navbar or any tool card.
- All registration fields (username, full name, employee code, email, password) are mandatory.
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
4. **Review** the detected DELETE statements.
5. **Generate** → download the single **BUNDLE_*.zip**. The ZIP is organised in
   numbered folders:
   - `01_Backup/01_Backup.sql` — row snapshots to run *before* the migration
   - `02_Migration/` — your original migration scripts
   - `03_Revert/01_Revert.sql` — FK-safe rollback script
   - `04_Drop_Backup/01_Cleanup.sql` — drops the snapshot tables after the rollback window
   - `ALTERS.sql`, `PROCEDURES.txt` — ALTER statements and stored-code index at the root

### What the DBA does with the bundle
Run `01_Backup/01_Backup.sql` first (snapshots affected rows), then the migration.
If a rollback is needed, run `03_Revert/01_Revert.sql` (FK-safe by construction).
After the rollback window passes, run `04_Drop_Backup/01_Cleanup.sql` to drop the
snapshot tables.

### History
**History** lists your jobs with filters (search, production-date range, status).
Admins can pass `?all=1` to see everyone's jobs.

## Team Leaders

If you are assigned as a team leader you get two extra dashboards:

- **Team Dashboard** (`/teams/my`) — team-wide KPI tiles, 30-day activity chart,
  recent team jobs, and a full member list.
- **Team Jobs** (`/teams/my/jobs`) — filterable listing of every team member's
  jobs with download buttons for each individual artefact or the full bundle ZIP.
  Access is scoped to your own team only.

You can also approve or reject join requests from the **Join Requests** link in
the sidebar.

## Admin

Admins get extra navigation:

- **User activity** — all users' jobs, KPIs, charts.
- **Manage users** — create / activate / deactivate / reset passwords.
- **Teams** — create and manage teams, assign team leaders, remove members.
- **Logo** — upload a custom platform logo (PNG/JPG/SVG/WebP/GIF, max 5 MB).
  The uploaded logo is reflected instantly across every page of the platform.
- **Watched sources** — register a local folder or Git repo for the scheduler to
  poll. Configure cadence (specific times on chosen days, every-N-minutes, or
  cron), with a live "next 5 fires" preview. **Run now**, **Snooze 24h**, and
  **Resume** are available per source.

For the full AutoBackupRevert reference (architecture, schema, scheduler design,
security posture, roadmap), see
[auto-backup-revert-tool/documentation.md](../auto-backup-revert-tool/documentation.md).
