# Delivery Toolbox — Platform & Feature Catalogue

**Audience:** Client technical team / stakeholder presentation.
**Product:** Delivery Toolbox — *"one login, many safe tools."*
**Last updated:** 2026-07-11.

A self-hosted, single-sign-in web platform that hosts a growing suite of
independent internal tools behind one account, one navigation, one design
language, and one audit trail. Built for internal delivery teams **and** external
client users performing operational database-release tasks safely.

---

## 1. Platform at a glance

| Area | Capability |
|---|---|
| **Access** | Single login for the whole suite; role-based access to each tool |
| **Roles** | User · Team · Team Lead · Admin (see §3) |
| **Security** | PBKDF2-SHA256 passwords, optional TOTP 2FA, login rate-limiting + lockout, per-IP throttling, full audit log |
| **Tools** | AutoBackupRevert, Encrypt/Decrypt, Query Generator, XPM Automator, Release Tracker (+ Team & Admin consoles) |
| **Governance** | Every sensitive action is audit-logged with actor, IP, before/after values |
| **Deployment** | Single Flask process (waitress + nginx + TLS), Oracle Cloud (Ubuntu 22.04) |
| **Data** | Platform metadata in SQLite; Release Tracker can target external client databases |
| **UX** | Modern, premium, responsive, light/dark, WCAG 2.1 AA-oriented |

---

## 2. Access, navigation & branding

- **One login** unlocks every tool the user is permitted to use.
- **Data-driven navigation** — the sidebar and the "All tools" hub show *only*
  the tools a given user may open, based on role + team + per-user grants.
- **Consistent chrome** — every page shares the same top bar (tool name,
  notifications bell, light/dark toggle, profile menu), sidebar, and footer.
- **Configurable branding** — admins upload the platform logo; the sidebar
  tagline is configurable.
- **Sessions** — inactivity auto-logout (default 5 min, admin-configurable) with
  a "keep me signed in" warning modal.

---

## 3. Roles & permission hierarchy

| Capability | User | Team Lead | Admin |
|---|:--:|:--:|:--:|
| Use tools granted to them | ✅ | ✅ | ✅ |
| See own work (jobs, runs, records) | ✅ | ✅ | ✅ |
| See **team-wide** work + team dashboard | — | ✅ | ✅ (all) |
| Approve/reject team join requests | — | ✅ (own team) | ✅ (all) |
| Restrict which tools a team member sees | — | ✅ (own team) | ✅ |
| Create/edit/delete teams, assign leaders | — | — | ✅ |
| Manage users, roles, bulk import | — | — | ✅ |
| Platform settings, storage, email, security policy | — | — | ✅ |
| Release Tracker: database configuration & record delete | — | ✅ | ✅ |

Authorization is enforced **server-side** on every route (not just hidden in the
UI). New self-registered users who pick a team start **pending** until approved.

---

## 4. The tools

### 4.1 AutoBackupRevert — FK-safe Oracle rollback generator
**Purpose:** turn a migration bundle into matched, reviewable rollback scripts so
a release can be safely undone.
**Primary users:** delivery/DB engineers.
**Key features:**
- Input by **file upload** (`.7z` / `.zip` / `.sql`) or a **server-side path**.
- Scans files in natural order; collects every `DELETE FROM …` into a reviewable
  `delete.sql`.
- Generates timestamped **BACKUP.sql** (CREATE-TABLE-AS-SELECT snapshots),
  **REVERT.sql** (DELETE + conditional INSERT-from-backup), optional **CLEANUP**
  (drop-backup), plus harvested **ALTER** statements and stored-code
  (**PROCEDURES.txt**, with a de-duplicated "unique names" view).
- Packs everything into a structured **BUNDLE.zip** (01_Backup / 02_Migration /
  03_Revert / 04_Drop_Backup + root artefacts).
- Review-before-generate workflow; per-file previews with truncation for large
  scripts.
- **Dashboard** with stat cards, jobs-per-day trend, top tables, recent jobs.
- **History** with search + date/status filters; downloads are ownership-scoped.
- **Scheduler** watches configured sources for new bundles (admin-managed).
- Background cleanup honours retention policy (configurable days).

### 4.2 Encrypt / Decrypt Utility — AES-256-CBC
**Purpose:** encrypt/decrypt strings and nonces, **byte-for-byte interoperable**
with the organisation's legacy C# tool.
**Key features:** server-side AES-256-CBC (+ PKCS7) so the key never reaches the
browser; encrypt, decrypt, and nonce generation; URL-safe token transforms;
exact error messages mirrored from the desktop tool.

### 4.3 Query Chunker — Oracle NCLOB rebuild
**Purpose:** turn a large raw payload into (a) one Standard SQL `UPDATE` and (b)
an Oracle PL/SQL block that rebuilds the value as an NCLOB from fixed-size chunks.
**Key features:** byte-for-byte reproducible output; configurable chunk size with
validation; safe (it *emits* SQL text, never executes it).

### 4.4 XPM Automator — bulk migration-script upload
**Purpose:** bulk-upload `.sql`/`.txt` migration scripts to the XPM CRM in order,
and manage the consolidated download.
**Key features:**
- Multi-file upload (`secure_filename`, `.sql`/`.txt` whitelist, empty-file
  rejection) in deterministic order.
- App-generated, **editable Batch Number** (`XPM-YYYYMMDD-HHMMSS-XXX`) that drives
  download filenames.
- Live **project/process discovery** ("Fetch from XPM") with dynamic dropdowns.
- Consolidated-script download + a batch-range download/merge mode.
- **Processing history** (per-run + per-file status) with export; runs are
  owned by their creator (admins see all).
- **Batch Explorer** to browse every migration script in the project (paged).
- Security: the XPM password is **never persisted** (in-thread only); only a
  redacted config snapshot is stored; key actions audited.

### 4.5 Release Tracker — enhancement release tracking *(flagship, most recent)*
**Purpose:** track enhancement/case releases across **CRM → SIT → UAT → PreProd →
Production**, per project, with rich reporting.
**Primary users:** delivery managers, release coordinators, QA.
**Key features:**
- **Per-project external databases** — Admin/Team Lead configures a real database
  (PostgreSQL / MySQL / SQL Server / Oracle / SQLite) per project; the connection
  is validated and the Release Tracker table is provisioned automatically.
  Connection passwords are **AES-256-GCM encrypted at rest** and never shown again.
  (Oracle connects by **Service Name** — Oracle Cloud ready — with SID fallback.)
- **KPI cards** — Total Releases · Delivered This Month · Awaiting Prod · Added
  This Week.
- **Manual entry** with **batch-range expansion**: a value like `84-90` creates
  one record per batch, copying every other field. Category = Release / Hotfix /
  Prod Fix / Other (segmented control); "Sent By" defaults to the current user and
  is restricted to same-team members.
- **Data grid** — pagination (50/100/200/500), sorting, global search, column &
  date-range filters, sticky header, inline editing (Enhancement ID, Subject,
  SIT/UAT/PreProd/Prod dates), multi-row select, skeleton loaders.
- **Dynamic grouping** — records sharing Enhancement ID + Upload Date + Category
  collapse to a single row showing the batch range (e.g. `84-90`), expandable to
  individual batches.
- **Import** (CSV/Excel) with full validation, duplicate-batch protection, and a
  downloadable error report (Inserted / Skipped / Failed summary).
- **Bulk Update** (CSV/Excel) keyed on Batch Number — updates only the columns
  present, never blanks existing values, skips unknown batches.
- **Export** (Excel/CSV) honouring all active filters, via an interactive format
  chooser.
- **Missing batches** — finds gaps in the batch sequence between the lowest and
  highest uploaded batch, viewable as chips and exportable to CSV.
- **CSV templates** for Import and Bulk Update.
- **Collapsible** entry form and records view; **delete** is soft and restricted
  to Team Lead/Admin with confirmation; per-row and platform-level **audit trail**.

### 4.6 Team Management
Team CRUD (admin), the **join-request approval workflow** (admin + team lead),
per-member tool-access control, and team leader dashboards (team stats, team jobs,
join requests). All actions are team-scoped and audited.

### 4.7 Admin Console
The single administration entry point: **user activity**, **user management**
(+ bulk import), **tool portal** (register/enable/order tools, per-tool & per-user
access), **watched sources** + scheduler, **storage & retention**, **branding /
logo**, **email & notification templates** (with live preview), **security policy**
(password rules, lockout, 2FA enforcement, rate limits), **screen content** CMS,
**database provider configuration & migration**, **audit log** viewer, **analytics**,
and **system status**.

---

## 5. Cross-cutting platform features

- **Audit trail** — a unified, queryable log of every sensitive action (actor,
  role, IP, target, before/after, status) across all tools.
- **Notifications** — in-app bell + configurable email templates (join requests,
  approvals, password reset, role change, etc.) with live preview.
- **Security** — PBKDF2-SHA256 (600k iterations) passwords; optional TOTP 2FA with
  admin-enforceable policy; login rate-limiting, account lockout, per-IP throttle;
  no account-enumeration on password reset; single-use hashed reset tokens;
  open-redirect protection; CSRF-safe destructive actions via confirmations;
  parameterised SQL throughout; validated + size-limited uploads.
- **Configurability** — platform name/tagline, logo, session timeout, retention,
  email SMTP + templates, security thresholds, and on-screen copy are all
  admin-editable without code changes.
- **Design system** — the "Glass Instrument": premium glass/aurora framing for
  auth/chrome, crisp utilitarian surfaces for tasks; Iris-indigo action colour over
  zinc neutrals; Inter for UI, JetBrains Mono for IDs/dates/SQL; responsive,
  light/dark, accessible.

---

## 6. Extensibility

New tools are added as **isolated blueprints** with zero impact on existing tools
(no shared-schema changes). Each new tool inherits auth, navigation, the design
system, audit logging, notifications, and role enforcement automatically, and must
follow the shared **Sub-tool Blueprint** (see `docs/SUBTOOL_BLUEPRINT.md`) so the
whole suite stays visually and behaviourally consistent.

---

*Prepared for client technical review. For architecture and operations see
`docs/architecture.md`, `docs/deployment-guide.md`, and `PROJECT_MEMORY.md`.*
