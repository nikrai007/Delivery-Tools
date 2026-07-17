"""
Release Tracker — project registry (metadata) persisted in the platform SQLite DB.

Self-contained and zero-impact on ``models.py`` (same pattern as ``xpm_store``):
the tool owns one table, ``rt_projects``, created idempotently on first use via the
platform's ``models.connect()`` context manager. **No edits to the platform schema.**

Each row is one "project" — a named Release Tracker target database. The actual
release records live in the *external* database described by the row (see
``rt_db``); this table only holds the connection configuration (password
encrypted via ``rt_secrets``) plus audit metadata.

Access to create/modify projects is restricted to Admin / Team Lead at the route
layer (``rt_routes``); this module is pure persistence.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone

import models  # platform DB helpers — reused, never modified
import rt_secrets

log = logging.getLogger("release-tracker.store")

TOOL_SLUG = "rt"
TOOL_ENDPOINT = "rt.dashboard"

# Config keys whose values are safe to expose to the client (password excluded).
_PUBLIC_CFG_KEYS = ("host", "port", "database", "service_name", "username", "extra", "path")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rt_projects (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    slug          TEXT NOT NULL UNIQUE,          -- used to name the external table
    provider      TEXT NOT NULL,                 -- db_providers id (sqlite|postgresql|mysql|...)
    table_name    TEXT NOT NULL,                 -- release_tracker_<slug>
    config_json   TEXT NOT NULL,                 -- connection cfg; password encrypted
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_by    INTEGER,
    created_by_name TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_rt_projects_active ON rt_projects(is_active, name);
"""

_initialised = False


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


def _ensure() -> None:
    global _initialised
    if _initialised:
        return
    with models.connect() as con:
        con.executescript(_SCHEMA)
        con.commit()
    _initialised = True


def init_store() -> None:
    """Eager init for the app factory: create the table + register the portal card."""
    _ensure()
    ensure_registered()


# ----------------------------------------------------------------------
# Slug helper
# ----------------------------------------------------------------------
def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return s or "project"


def table_name_for(slug: str) -> str:
    return f"release_tracker_{slug}"


# ----------------------------------------------------------------------
# Portal registration (idempotent)
# ----------------------------------------------------------------------
def ensure_registered() -> None:
    try:
        if models.get_portal_tool_by_slug(TOOL_SLUG) is not None:
            return
        models.create_portal_tool(
            slug=TOOL_SLUG,
            name="Release Tracker",
            description=("Track enhancement/case releases across CRM, SIT, UAT, PreProd and "
                         "Production — per-project databases, manual entry with batch-range "
                         "expansion, import/export, bulk update, grouping and a full audit trail."),
            icon="rocket_launch",
            icon_type="symbol",
            tags=["Release", "Tracker", "Batch"],
            status="live",
            launch_type="internal",
            launch_config={"endpoint": TOOL_ENDPOINT},
            display_order=models.next_portal_tool_order(),
            requires_team=False,
            created_by=None,
        )
    except Exception:  # noqa: BLE001 — registration must never break startup
        log.exception("Failed to register Release Tracker portal tool")


# ----------------------------------------------------------------------
# Config (de)serialization
# ----------------------------------------------------------------------
def _encode_config(cfg: dict) -> str:
    """Store config with the password field encrypted at rest."""
    safe = {k: cfg.get(k) for k in _PUBLIC_CFG_KEYS if cfg.get(k) not in (None, "")}
    safe["password_enc"] = rt_secrets.encrypt(cfg.get("password") or "")
    return json.dumps(safe)


def _decode_config(config_json: str, *, reveal_password: bool = False) -> dict:
    try:
        raw = json.loads(config_json or "{}")
    except (TypeError, ValueError):
        raw = {}
    cfg = {k: raw.get(k) for k in _PUBLIC_CFG_KEYS if raw.get(k) is not None}
    if reveal_password:
        cfg["password"] = rt_secrets.decrypt(raw.get("password_enc"))
    return cfg


# ----------------------------------------------------------------------
# CRUD
# ----------------------------------------------------------------------
def create_project(*, name: str, provider: str, cfg: dict,
                   created_by: int | None, created_by_name: str | None) -> int:
    _ensure()
    slug = slugify(name)
    with models.connect() as con:
        # Ensure slug uniqueness (append a counter on collision).
        base, n = slug, 1
        while con.execute("SELECT 1 FROM rt_projects WHERE slug = ?", (slug,)).fetchone():
            n += 1
            slug = f"{base}_{n}"
        table_name = table_name_for(slug)
        cur = con.execute(
            """INSERT INTO rt_projects
               (name, slug, provider, table_name, config_json, is_active,
                created_by, created_by_name, created_at)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (name.strip(), slug, provider, table_name, _encode_config(cfg),
             created_by, created_by_name, _now()),
        )
        con.commit()
        return cur.lastrowid


def update_project_config(project_id: int, *, cfg: dict) -> None:
    _ensure()
    with models.connect() as con:
        con.execute("UPDATE rt_projects SET config_json = ?, updated_at = ? WHERE id = ?",
                    (_encode_config(cfg), _now(), project_id))
        con.commit()


def set_active(project_id: int, active: bool) -> None:
    _ensure()
    with models.connect() as con:
        con.execute("UPDATE rt_projects SET is_active = ?, updated_at = ? WHERE id = ?",
                    (1 if active else 0, _now(), project_id))
        con.commit()


def delete_project(project_id: int) -> None:
    """Remove the project registration. The external table itself is left intact
    (deliberately — dropping a customer database table is never done implicitly)."""
    _ensure()
    with models.connect() as con:
        con.execute("DELETE FROM rt_projects WHERE id = ?", (project_id,))
        con.commit()


def get_project(project_id: int) -> sqlite3.Row | None:
    _ensure()
    with models.connect() as con:
        return con.execute("SELECT * FROM rt_projects WHERE id = ?", (project_id,)).fetchone()


def list_projects(active_only: bool = True) -> list[sqlite3.Row]:
    _ensure()
    with models.connect() as con:
        if active_only:
            return con.execute(
                "SELECT * FROM rt_projects WHERE is_active = 1 ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return con.execute("SELECT * FROM rt_projects ORDER BY name COLLATE NOCASE").fetchall()


def project_connection(project_id: int) -> tuple[str, dict] | None:
    """Return ``(provider, cfg-with-decrypted-password)`` for connecting, or None."""
    row = get_project(project_id)
    if row is None:
        return None
    return row["provider"], _decode_config(row["config_json"], reveal_password=True)


def project_public_config(row: sqlite3.Row) -> dict:
    """Password-free config for display in the UI."""
    return _decode_config(row["config_json"], reveal_password=False)
