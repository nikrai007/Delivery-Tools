"""
Release Tracker — external-database data layer (engine-agnostic).

Each Release Tracker *project* points at a real external database (PostgreSQL,
MySQL/MariaDB, SQL Server, Oracle — or SQLite for local/dev), configured by an
Admin/Team Lead. This module owns everything that touches that external DB:

  * building (and caching) a SQLAlchemy engine from a saved connection config,
  * dynamically creating the project's ``release_tracker_<slug>`` table,
  * all record CRUD, filtering, sorting, pagination, bulk update and soft delete.

It reuses the platform's ``db_providers`` (URL building + driver detection), so
adding an engine is a data change there, not code here. Every column type is a
portable SQLAlchemy generic type, so the same schema materialises correctly on
every supported engine.

Nothing here imports Flask — it is a pure service layer, unit-testable in
isolation. Callers (``rt_routes``) pass already-validated Python values
(``datetime.date`` for dates, ``int`` for batch numbers, ``str`` for text).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import threading
from typing import Any

from sqlalchemy import (Column, Date, Integer, MetaData, String, Table, Text,
                        and_, create_engine, delete, func, insert, or_, select,
                        update)

import db_providers
from rt_service import (DATE_FIELDS, EDITABLE_FIELDS, RECORD_COLUMNS,
                        TEXT_SEARCH_FIELDS)

log = logging.getLogger("release-tracker.db")

# ----------------------------------------------------------------------
# Engine cache — one engine per distinct connection config (thread-safe).
# ----------------------------------------------------------------------
_engines: dict[str, Any] = {}
_engines_lock = threading.Lock()


def _engine_key(provider: str, cfg: dict) -> str:
    parts = [provider] + [f"{k}={cfg.get(k)!r}" for k in sorted(cfg) if k != "password"]
    # A short salted hash of the password (never the plaintext) so a password
    # rotation rebuilds the cached engine, without keeping secrets in the key.
    pw = (cfg.get("password") or "").encode("utf-8")
    parts.append("pw=" + hashlib.sha256(pw).hexdigest()[:12])
    return "|".join(parts)


def get_engine(provider: str, cfg: dict):
    key = _engine_key(provider, cfg)
    with _engines_lock:
        eng = _engines.get(key)
        if eng is None:
            url = db_providers.build_sqlalchemy_url(provider, cfg)
            eng = create_engine(url, pool_pre_ping=True, future=True)
            _engines[key] = eng
        return eng


def test_connection(provider: str, cfg: dict) -> dict:
    """Thin pass-through to the platform provider registry."""
    return db_providers.test_connection(provider, cfg)


# ----------------------------------------------------------------------
# Table definition — built on demand against a fresh MetaData.
# ----------------------------------------------------------------------
def _table(table_name: str) -> Table:
    md = MetaData()
    return Table(
        table_name, md,
        Column("s_no", Integer, primary_key=True, autoincrement=True),
        Column("enhancement_id", String(120), nullable=False),
        Column("release_subject", Text, nullable=False),
        Column("category", String(60), nullable=False),
        Column("other_category", String(200)),
        Column("sent_by", String(150), nullable=False),
        Column("batch_number", Integer, nullable=False),
        Column("crm_delivery_date", Date, nullable=False),
        Column("sit_date", Date),
        Column("uat_date", Date),
        Column("preprod_date", Date),
        Column("prod_live_date", Date),
        Column("upload_date", Date, nullable=False),        # for dynamic grouping
        Column("created_by", String(150)),
        Column("created_date", String(40)),
        Column("updated_by", String(150)),
        Column("updated_date", String(40)),
        Column("is_deleted", Integer, nullable=False, default=0),
    )


def create_table(provider: str, cfg: dict, table_name: str) -> None:
    """Create the project's release-tracker table if it does not already exist."""
    eng = get_engine(provider, cfg)
    tbl = _table(table_name)
    tbl.metadata.create_all(eng, checkfirst=True)
    log.info("Ensured Release Tracker table %s on %s.", table_name, provider)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _iso(v: Any) -> Any:
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()[:10] if isinstance(v, _dt.date) and not isinstance(v, _dt.datetime) else v.isoformat()
    return v


def _row_to_dict(row_mapping) -> dict:
    return {k: _iso(v) for k, v in dict(row_mapping).items()}


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


# ----------------------------------------------------------------------
# Filter builder (shared by search + count + export)
# ----------------------------------------------------------------------
def _build_where(tbl: Table, f: dict):
    conds = [tbl.c.is_deleted == 0]
    q = (f.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        ors = [tbl.c[name].like(like) for name in TEXT_SEARCH_FIELDS]
        # batch number: match numeric equality if q is an int
        if q.isdigit():
            ors.append(tbl.c.batch_number == int(q))
        conds.append(or_(*ors))
    if f.get("category"):
        conds.append(tbl.c.category == f["category"])
    if f.get("sent_by"):
        conds.append(tbl.c.sent_by == f["sent_by"])
    if f.get("enhancement_id"):
        conds.append(tbl.c.enhancement_id.like(f"%{f['enhancement_id']}%"))
    if f.get("batch_number") is not None:
        conds.append(tbl.c.batch_number == f["batch_number"])
    if f.get("batch_from") is not None:
        conds.append(tbl.c.batch_number >= f["batch_from"])
    if f.get("batch_to") is not None:
        conds.append(tbl.c.batch_number <= f["batch_to"])
    # Per-field date ranges: keys like "crm_delivery_date_from" / "_to".
    for name in DATE_FIELDS:
        lo, hi = f.get(f"{name}_from"), f.get(f"{name}_to")
        if lo is not None:
            conds.append(tbl.c[name] >= lo)
        if hi is not None:
            conds.append(tbl.c[name] <= hi)
        if f.get(name) is not None:        # exact date match
            conds.append(tbl.c[name] == f[name])
    return and_(*conds)


def search_records(provider: str, cfg: dict, table_name: str, *, filters: dict,
                   sort: str = "s_no", direction: str = "desc",
                   limit: int = 50, offset: int = 0) -> list[dict]:
    eng = get_engine(provider, cfg)
    tbl = _table(table_name)
    col = tbl.c[sort] if sort in RECORD_COLUMNS else tbl.c.s_no
    order = col.asc() if str(direction).lower() == "asc" else col.desc()
    stmt = (select(tbl).where(_build_where(tbl, filters))
            .order_by(order, tbl.c.s_no.asc())
            .limit(limit).offset(offset))
    with eng.connect() as con:
        return [_row_to_dict(r._mapping) for r in con.execute(stmt)]


def all_records(provider: str, cfg: dict, table_name: str, *, filters: dict,
                sort: str = "batch_number", direction: str = "asc") -> list[dict]:
    """Unpaginated fetch for export (bounded by the filter set)."""
    eng = get_engine(provider, cfg)
    tbl = _table(table_name)
    col = tbl.c[sort] if sort in RECORD_COLUMNS else tbl.c.batch_number
    order = col.asc() if str(direction).lower() == "asc" else col.desc()
    stmt = select(tbl).where(_build_where(tbl, filters)).order_by(order, tbl.c.s_no.asc())
    with eng.connect() as con:
        return [_row_to_dict(r._mapping) for r in con.execute(stmt)]


def count_records(provider: str, cfg: dict, table_name: str, *, filters: dict) -> int:
    eng = get_engine(provider, cfg)
    tbl = _table(table_name)
    stmt = select(func.count()).select_from(tbl).where(_build_where(tbl, filters))
    with eng.connect() as con:
        return int(con.execute(stmt).scalar_one())


def existing_batch_numbers(provider: str, cfg: dict, table_name: str) -> set[int]:
    """Live (non-deleted) batch numbers — used to reject duplicates on insert."""
    eng = get_engine(provider, cfg)
    tbl = _table(table_name)
    stmt = select(tbl.c.batch_number).where(tbl.c.is_deleted == 0)
    with eng.connect() as con:
        return {int(r[0]) for r in con.execute(stmt) if r[0] is not None}


def stats(provider: str, cfg: dict, table_name: str) -> dict:
    """Dashboard KPIs for the current project (live rows only)."""
    eng = get_engine(provider, cfg)
    tbl = _table(table_name)
    today = _dt.date.today()
    month_start = today.replace(day=1)
    week_cutoff = ((_dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
                    - _dt.timedelta(days=7)).isoformat(timespec="seconds") + "Z")
    base = tbl.c.is_deleted == 0

    def cnt(con, cond):
        return int(con.execute(select(func.count()).select_from(tbl).where(cond)).scalar_one())

    with eng.connect() as con:
        total = cnt(con, base)
        delivered_month = cnt(con, and_(base, tbl.c.prod_live_date >= month_start,
                                        tbl.c.prod_live_date <= today))
        awaiting_prod = cnt(con, and_(base, tbl.c.prod_live_date.is_(None)))
        added_week = cnt(con, and_(base, tbl.c.created_date >= week_cutoff))
    return {"total": total, "delivered_this_month": delivered_month,
            "awaiting_prod": awaiting_prod, "added_this_week": added_week}


def batch_gaps(provider: str, cfg: dict, table_name: str) -> dict:
    """Find missing batch numbers between the lowest and highest uploaded batch.

    Returns ``{min, max, present, missing: [ints], count}`` for live rows. If the
    project has no records, min/max are None and missing is empty.
    """
    present = existing_batch_numbers(provider, cfg, table_name)
    if not present:
        return {"min": None, "max": None, "present": 0, "missing": [], "count": 0}
    lo, hi = min(present), max(present)
    missing = [n for n in range(lo, hi + 1) if n not in present]
    return {"min": lo, "max": hi, "present": len(present), "missing": missing, "count": len(missing)}


def distinct_values(provider: str, cfg: dict, table_name: str, column: str) -> list[str]:
    if column not in RECORD_COLUMNS:
        return []
    eng = get_engine(provider, cfg)
    tbl = _table(table_name)
    stmt = (select(tbl.c[column]).where(and_(tbl.c.is_deleted == 0, tbl.c[column].isnot(None)))
            .distinct().order_by(tbl.c[column]))
    with eng.connect() as con:
        return [str(r[0]) for r in con.execute(stmt) if r[0] not in (None, "")]


# ----------------------------------------------------------------------
# Writes
# ----------------------------------------------------------------------
def insert_records(provider: str, cfg: dict, table_name: str, rows: list[dict],
                   *, created_by: str | None) -> int:
    """Insert already-validated rows in one transaction. Stamps audit columns."""
    if not rows:
        return 0
    eng = get_engine(provider, cfg)
    tbl = _table(table_name)
    now = _now_iso()
    today = _dt.date.today()
    payload = []
    for r in rows:
        rec = {c: r.get(c) for c in RECORD_COLUMNS if c in tbl.c and c != "s_no"}
        if not rec.get("upload_date"):
            rec["upload_date"] = today
        rec["created_by"] = created_by
        rec["created_date"] = now
        rec["updated_by"] = created_by
        rec["updated_date"] = now
        rec["is_deleted"] = 0
        payload.append(rec)
    with eng.begin() as con:
        con.execute(insert(tbl), payload)
    return len(payload)


def update_record(provider: str, cfg: dict, table_name: str, s_no: int,
                  changes: dict, *, updated_by: str | None) -> int:
    """Update selected editable columns of one row. Returns affected row count."""
    fields = {k: v for k, v in changes.items() if k in EDITABLE_FIELDS}
    if not fields:
        return 0
    fields["updated_by"] = updated_by
    fields["updated_date"] = _now_iso()
    eng = get_engine(provider, cfg)
    tbl = _table(table_name)
    stmt = update(tbl).where(and_(tbl.c.s_no == s_no, tbl.c.is_deleted == 0)).values(**fields)
    with eng.begin() as con:
        return con.execute(stmt).rowcount


def bulk_update_by_batch(provider: str, cfg: dict, table_name: str,
                         updates: list[dict], *, updated_by: str | None) -> dict:
    """Bulk update keyed on ``batch_number``. Each item must carry ``batch_number``
    plus one or more updatable columns; only present columns are written (existing
    values are never blanked). Unknown batch numbers are skipped.

    Returns ``{updated, skipped, skipped_batches}``.
    """
    eng = get_engine(provider, cfg)
    tbl = _table(table_name)
    now = _now_iso()
    updated = skipped = 0
    skipped_batches: list[int] = []
    with eng.begin() as con:
        for item in updates:
            batch = item.get("batch_number")
            cols = {k: v for k, v in item.items() if k in EDITABLE_FIELDS and k != "batch_number"}
            if batch is None or not cols:
                skipped += 1
                continue
            cols["updated_by"] = updated_by
            cols["updated_date"] = now
            stmt = (update(tbl)
                    .where(and_(tbl.c.batch_number == batch, tbl.c.is_deleted == 0))
                    .values(**cols))
            rc = con.execute(stmt).rowcount
            if rc and rc > 0:
                updated += rc
            else:
                skipped += 1
                skipped_batches.append(batch)
    return {"updated": updated, "skipped": skipped, "skipped_batches": skipped_batches}


def soft_delete(provider: str, cfg: dict, table_name: str, s_nos: list[int],
                *, deleted_by: str | None) -> int:
    if not s_nos:
        return 0
    eng = get_engine(provider, cfg)
    tbl = _table(table_name)
    stmt = (update(tbl).where(tbl.c.s_no.in_(s_nos))
            .values(is_deleted=1, updated_by=deleted_by, updated_date=_now_iso()))
    with eng.begin() as con:
        return con.execute(stmt).rowcount


def hard_delete_all(provider: str, cfg: dict, table_name: str) -> None:
    """Test helper — physically empties the table (not used by the UI)."""
    eng = get_engine(provider, cfg)
    tbl = _table(table_name)
    with eng.begin() as con:
        con.execute(delete(tbl))
