# Sub-tool Blueprint — build every new Delivery Toolbox tool the same way

> **Read this before creating any new tool.** It is the single source of truth for
> how a sub-tool must look, behave, and integrate so the whole suite stays
> consistent. Pair it with `PROJECT_MEMORY.md` (architecture), `DESIGN.md`
> (visual tokens), and `PRODUCT.md` (positioning). Release Tracker
> (`release-tracker-tool/`) and XPM Automator (`xpm-automator-tool/`) are the
> reference implementations — copy their shape.

---

## 0. Golden rules (the consistency contract)

1. **Isolated blueprint, zero impact.** A new tool never edits `models.py`, never
   changes another tool, and never touches `To_Ship/`. It owns its own tables.
2. **Reuse the platform, don't reinvent it.** Auth, roles, nav, audit,
   notifications, design system, DB-provider registry, settings, screen-content
   CMS — all already exist. Use them.
3. **Server-side authorization always.** UI hiding is not security. Guard every
   route with `@login_required` and the right role decorator.
4. **Match the design system.** Iris-indigo `brand`, zinc neutrals, Inter for UI,
   JetBrains Mono for IDs/dates/SQL, Material Symbols icons, `rounded-2xl` cards,
   light + dark, responsive. No new colours, no Heroicons, no CDNs beyond what
   `base.html` already loads.

---

## 1. Folder structure & wiring (the 6-step seam)

```
<tool-name>-tool/                 # hyphenated, top-level
  source-code/
    <slug>_routes.py              # blueprint: pages + JSON APIs + role guards
    <slug>_store.py               # owns tables via models.connect() + portal card
    <slug>_service.py             # pure logic (NO Flask) — unit-testable
    <slug>_core/                  # optional sub-package for heavier logic
  templates/
    <slug>_dashboard.html         # main page
    <slug>_macros.html            # shared header/toolbar macros
    <slug>_*.html                 # other screens (config, detail, history)
  documentation.md
```

Wire it in **exactly** these places (mirror XPM/Release Tracker):
1. `app.py` → append `<tool>/source-code` to `_CODE_DIRS`.
2. `app.py` → `from <slug>_routes import <slug>_bp` + `import <slug>_store`.
3. `app.py` → `<slug>_store.init_store()` (after the other stores) + `app.register_blueprint(<slug>_bp)`.
4. `<slug>_store.py` → idempotent `CREATE TABLE IF NOT EXISTS` (lazy `_ensure()`),
   plus `ensure_registered()` that upserts a `portal_tools` card.
5. `landing-page/source-code/landing_routes.py` → add a `LANDING_TOOLS` card.
6. `templates/base.html` → add a `{% if '<slug>' in accessible_tool_slugs and
   request.endpoint.startswith('<slug>.') %}` sub-nav block.

Blueprint: `url_prefix="/tools/<tool-name>"`, `template_folder` = the tool's
`templates/`. Slug is short (`abr`, `xpm`, `rt`); blueprint name == slug;
endpoints are `slug.view`.

---

## 2. Page layout & the header convention  ⚠️ (learned the hard way)

The platform top bar **already renders the page title** (`{% block page_title %}`).
So inside `{% block content %}`:

- **DO NOT repeat the tool name as a big content heading.** That produces the
  "two headers on one page" bug. (Fixed in Release Tracker 2026-07-11.)
- **Top bar = the name** — either the tool name (single-page tools) or the
  specific page/section (`Dashboard`, `History`, `Configuration`). XPM does this:
  top bar "XPM Automator", content section "Processing history".
- **Content top row = a toolbar**, not a title block: left = context (project
  selector / short subtitle), right = primary actions. See
  `rt_dashboard.html` "Toolbar (project + actions)".

Standard vertical rhythm of a dashboard page:
1. Toolbar row (context left, actions right).
2. **KPI cards** — 2–4 stat cards, `rounded-2xl`, label-caps + big **mono** value +
   small icon in a tinted square. Live from a `/api/stats` endpoint.
3. **Primary input** — a **collapsible** card (e.g. "Add … record") with a chevron.
4. **Records card** — its own **collapsible** header ("… records" + section
   actions), then toolbar (search + Filters + toggles + page-size), an optional
   filters panel, the grid, and a pagination footer.
5. Modals (import/bulk/etc.) + a toast container.

---

## 3. Data grid conventions

- Client-rendered from a paginated JSON API (`/api/records`). Never load the whole
  table.
- Page sizes **50 / 100 / 200 / 500** (default 50). Sticky header. Independent
  vertical + horizontal scroll.
- **Global search** (debounced ~350 ms) + a **Filters** panel (category, owner,
  ranges, date ranges) with a filter-count badge.
- **Sortable** columns (click header), server-side sort against a whitelist.
- **Inline editing** on the editable columns only (transparent inputs; date inputs
  for dates). Persist on `change` to `/api/…/<id>/update`; flash the cell green.
- **Multi-row select** + a scoped bulk action (e.g. delete) that appears only when
  rows are selected and the user is permitted.
- **Skeleton loaders** while fetching; first-class **empty**, **filtered-empty**,
  and **error** states.
- **Mono + tabular** for IDs, numbers, dates. Category/status as **dot + label
  chips** with semantic colours (indigo/amber/rose/emerald/sky).
- **Grouping** when it aids reading (collapse a related set into a summary row with
  an expand caret).

---

## 4. Actions & UX patterns (production-grade)

- **Multi-choice actions use a real UI, never `confirm()`.** Export = a **dropdown**
  ("Excel (.xlsx)" / "CSV (.csv)"), not `confirm('OK = Excel…')`. (Fixed in Release
  Tracker 2026-07-11.)
- **Native `confirm()` is acceptable *only* for a destructive yes/no** (delete X).
  For anything richer, use a modal.
- **Import / Bulk** = a modal with **drag-and-drop** upload, a **downloadable CSV
  template**, and a clear Inserted/Skipped/Failed result + downloadable error
  report. Duplicate/unknown keys are skipped, never silently corrupt data; existing
  values are never blanked on partial updates.
- **Feedback = toasts** (success/error/warn/info), not `alert()`.
- **Keyboard**: `/` focuses search, `⌘/Ctrl+↵` submits the primary form, `Esc`
  closes modals. Show `kbd` hints.
- **Exports honour active filters** and are audit-logged.

---

## 5. Configuration screen pattern (if the tool has one)

- **Restricted to Admin / Team Lead** (`@team_leader_required`, which also admits
  admins). Normal users cannot view or reach it.
- If it configures a database/connection: reuse `db_providers` (SQLite / Postgres /
  MySQL / SQL Server / Oracle / Mongo). **Test the connection before saving**, then
  provision tables. Show driver-availability per provider. Oracle: offer a
  **Service Name** field (SID fallback).
- **Encrypt secrets at rest** (AES-256-GCM, key from `SECRET_KEY` — see
  `rt_secrets.py`); never return a saved password to the browser.
- Removing a config removes only the registration, not the customer's external
  data, unless explicitly intended.

---

## 6. Roles & hierarchy enforcement

- Decorators: `@login_required`, `@team_leader_required`, `@admin_required`
  (`shared/utilities/decorators.py`).
- **View/create/edit** for any permitted user; **configuration + destructive
  delete** for Team Lead/Admin; **platform settings** for Admin only.
- Ownership/scoping: a record/job/run belongs to its creator; team leads see their
  team's items; admins see all. Enforce with explicit checks (see ABR
  `_job_or_404`, Teams `my_team_download`).
- "Assigned to another person" fields (e.g. Release Tracker "Sent By") must be
  restricted to **same-team** members, validated server-side.

---

## 7. Configurability, audit & notifications

- **Settings** live in the `settings` KV store (`models.setting_get/set`); expose
  admin toggles there rather than hardcoding.
- **On-screen copy** can be admin-editable via the screen-content CMS
  (`screen_content.py` + `{{ content(screen, field, default) }}` + the
  `edit_button` macro).
- **Audit** every create/update/delete/config/export via `audit.record(action,
  category=CAT_*, target_*, old/new_value, details)` — it never raises.
- **Notifications**: use `models.create_notification` + `email_utils.notify` for
  cross-user events (requests, approvals). Reuse existing email templates.

---

## 8. Non-negotiable engineering standards

- `from __future__ import annotations`; module `log = logging.getLogger("<slug>")`;
  UTC ISO `_now()`.
- Pure service layer imports **no Flask** (keep it testable).
- Parameterised SQL only; validate + whitelist any dynamic identifier.
- Wrap risky I/O/DB calls; return `{ok:false,error}` JSON or a friendly flash;
  log the technical detail.
- Uploads: `secure_filename` + extension whitelist + size check + empty-file
  rejection.
- Test through the Flask test client (login → exercise every endpoint) before
  declaring done; run the cross-tool page-load regression.

---

## 9. New-tool checklist (copy into the PR)

- [ ] Folder + 6-step wiring done; app boots; blueprint + routes registered.
- [ ] Store owns its tables idempotently; portal card registered; `models.py` untouched.
- [ ] **No duplicate header** (top bar = name; content = toolbar).
- [ ] KPI cards + `/api/stats`.
- [ ] Grid: pagination/sort/search/filters/sticky/inline-edit/skeleton/empty states.
- [ ] Export = dropdown; Import/Bulk = modal + template + error report; toasts (no `alert`/choice-`confirm`).
- [ ] Config screen (if any) admin/lead-gated, test-before-save, secrets encrypted.
- [ ] Roles enforced server-side; ownership/team scoping correct.
- [ ] Audit on every sensitive action; notifications where cross-user.
- [ ] Light + dark, responsive, Material Symbols, brand-indigo, mono for IDs/dates.
- [ ] `documentation.md` written; `docs/PLATFORM_FEATURES.md` updated.
- [ ] Flask-test-client suite green; cross-tool regression green.
