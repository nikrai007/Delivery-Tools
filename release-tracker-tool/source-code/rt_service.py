"""
Release Tracker — domain logic: field spec, validation, batch-range expansion,
import normalisation and dynamic grouping.

Pure Python (no Flask, no DB) so it is trivially unit-testable and shared by the
data layer (``rt_db``), the I/O layer (``rt_io``) and the routes (``rt_routes``).
This is the single source of truth for *what a Release Tracker record is* — the
column set, which fields are mandatory/editable/date-typed, and the accepted
category values.
"""

from __future__ import annotations

import datetime as _dt
import re

# ----------------------------------------------------------------------
# Canonical schema description (kept in sync with rt_db._table)
# ----------------------------------------------------------------------
RECORD_COLUMNS = [
    "s_no", "enhancement_id", "release_subject", "category", "other_category",
    "sent_by", "batch_number", "crm_delivery_date", "sit_date", "uat_date",
    "preprod_date", "prod_live_date", "upload_date",
    "created_by", "created_date", "updated_by", "updated_date", "is_deleted",
]

# User-filterable date columns (each supports exact + <field>_from / <field>_to).
DATE_FIELDS = ["crm_delivery_date", "sit_date", "uat_date", "preprod_date", "prod_live_date"]

# Columns a user may change via inline edit / bulk update.
EDITABLE_FIELDS = ["enhancement_id", "release_subject",
                   "sit_date", "uat_date", "preprod_date", "prod_live_date"]

# Columns scanned by the global search box.
TEXT_SEARCH_FIELDS = ["enhancement_id", "release_subject", "sent_by", "category"]

CATEGORIES = ["Release", "Hotfix", "Prod Fix", "Other"]
_CATEGORY_LOOKUP = {c.lower(): c for c in CATEGORIES}

# Grid column display metadata (order = grid order).
GRID_COLUMNS = [
    {"key": "enhancement_id", "label": "Enhancement / Case ID", "type": "text", "editable": True},
    {"key": "release_subject", "label": "Release Mail Subject", "type": "text", "editable": True},
    {"key": "category", "label": "Category", "type": "text", "editable": False},
    {"key": "sent_by", "label": "Sent By", "type": "text", "editable": False},
    {"key": "batch_number", "label": "Batch Number", "type": "int", "editable": False},
    {"key": "crm_delivery_date", "label": "CRM Delivery Date", "type": "date", "editable": False},
    {"key": "sit_date", "label": "SIT Execution Date", "type": "date", "editable": True},
    {"key": "uat_date", "label": "UAT Execution Date", "type": "date", "editable": True},
    {"key": "preprod_date", "label": "PreProd Execution Date", "type": "date", "editable": True},
    {"key": "prod_live_date", "label": "Prod Live Date", "type": "date", "editable": True},
]

# Import / bulk-update header aliases -> internal column. Headers are normalised
# (lowercased, non-alphanumerics stripped) before lookup, so "SIT Date",
# "sit_date" and "SIT  Execution Date" all map to the same column.
_ALIAS_SOURCES = {
    "enhancement_id": ["enhancement id", "enhancement/case id", "enhancement case id",
                       "case id", "enhancement", "enh id"],
    "release_subject": ["release mail subject", "mail subject", "subject", "release subject"],
    "category": ["category"],
    "other_category": ["other category"],
    "sent_by": ["sent by", "sentby", "sender", "employee id", "employee"],
    "batch_number": ["batch number", "batch no", "batch", "batchno"],
    "crm_delivery_date": ["crm delivery date", "crm date", "delivery date", "crm"],
    "sit_date": ["sit execution date", "sit date", "sit"],
    "uat_date": ["uat execution date", "uat date", "uat"],
    "preprod_date": ["preprod execution date", "preprod date", "pre prod date", "preprod", "pre prod"],
    "prod_live_date": ["prod live date", "production live date", "prod date", "go live date", "prod live"],
}


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").strip().lower())


HEADER_ALIASES = {}
for _col, _names in _ALIAS_SOURCES.items():
    for _n in _names:
        HEADER_ALIASES[_norm(_n)] = _col


def map_headers(headers: list) -> dict:
    """Map a row of raw header cells to ``{internal_column: index}``."""
    out = {}
    for idx, h in enumerate(headers):
        col = HEADER_ALIASES.get(_norm(h))
        if col and col not in out:
            out[col] = idx
    return out


# ----------------------------------------------------------------------
# Parsing / validation primitives
# ----------------------------------------------------------------------
class ValidationError(ValueError):
    """Raised for a user-correctable validation problem (message is UI-safe)."""


_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%m/%d/%Y", "%d-%b-%Y", "%d %b %Y")


def parse_date(value, *, field: str = "date") -> _dt.date | None:
    """Parse a cell/string into a ``date``. Empty -> None. Invalid -> ValidationError."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    s = str(value).strip()
    if not s:
        return None
    s = s.split(" ")[0] if s.count(" ") and ":" in s else s  # drop a trailing time part
    for fmt in _DATE_FORMATS:
        try:
            return _dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValidationError(f"'{value}' is not a valid {field} (use YYYY-MM-DD).")


def normalize_category(value) -> str:
    c = _CATEGORY_LOOKUP.get(str(value or "").strip().lower())
    if not c:
        raise ValidationError(
            f"Category '{value}' is invalid. Allowed: {', '.join(CATEGORIES)}.")
    return c


_ENH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]*$")


def validate_enhancement_id(value) -> str:
    s = str(value or "").strip()
    if not s:
        raise ValidationError("Enhancement / Case ID is required.")
    if not _ENH_RE.match(s):
        raise ValidationError(f"Enhancement / Case ID '{s}' is not valid (alphanumeric only).")
    return s


def parse_batch_spec(text) -> list[int]:
    """Expand a batch specification into individual numbers.

    Accepts a single value ("84"), an inclusive range ("84-90") or a
    comma-separated mix ("84, 86, 90-92"). Raises ValidationError on bad input.
    """
    s = str(text or "").strip()
    if not s:
        raise ValidationError("Batch Number is required.")
    out: list[int] = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                raise ValidationError(f"Batch range '{part}' is reversed (start > end).")
            if hi - lo > 5000:
                raise ValidationError(f"Batch range '{part}' is too large (max 5000).")
            out.extend(range(lo, hi + 1))
        elif part.isdigit():
            out.append(int(part))
        else:
            raise ValidationError(f"'{part}' is not a valid batch number or range.")
    # De-duplicate while preserving order.
    seen, uniq = set(), []
    for n in out:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    if not uniq:
        raise ValidationError("Batch Number is required.")
    return uniq


def parse_batch_single(text) -> int:
    """Parse exactly one batch number (import/bulk-update rows are per-batch)."""
    s = str(text or "").strip()
    # Excel may hand us "84.0".
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    if not s.isdigit():
        raise ValidationError(f"'{text}' is not a valid Batch Number.")
    return int(s)


# ----------------------------------------------------------------------
# Manual entry -> records (with batch-range expansion)
# ----------------------------------------------------------------------
def build_manual_records(form: dict, *, sent_by: str) -> list[dict]:
    """Validate a manual-entry payload and expand it into one record per batch.

    Every non-batch field value is copied into every generated record.
    Raises ValidationError with a user-facing message on the first problem.
    """
    enhancement_id = validate_enhancement_id(form.get("enhancement_id"))
    subject = str(form.get("release_subject") or "").strip()
    if not subject:
        raise ValidationError("Release Mail Subject is required.")
    category = normalize_category(form.get("category"))
    other_category = str(form.get("other_category") or "").strip()
    if category == "Other" and not other_category:
        raise ValidationError("Other Category is required when Category is 'Other'.")
    if category != "Other":
        other_category = ""

    if not str(sent_by or "").strip():
        raise ValidationError("Sent By is required.")

    crm = parse_date(form.get("crm_delivery_date"), field="CRM Delivery Date")
    if crm is None:
        raise ValidationError("CRM Delivery Date is required.")
    sit = parse_date(form.get("sit_date"), field="SIT Execution Date")
    uat = parse_date(form.get("uat_date"), field="UAT Execution Date")
    preprod = parse_date(form.get("preprod_date"), field="PreProd Execution Date")
    prod = parse_date(form.get("prod_live_date"), field="Prod Live Date")

    batches = parse_batch_spec(form.get("batch_number"))

    base = {
        "enhancement_id": enhancement_id,
        "release_subject": subject,
        "category": category,
        "other_category": other_category,
        "sent_by": str(sent_by).strip(),
        "crm_delivery_date": crm,
        "sit_date": sit, "uat_date": uat, "preprod_date": preprod, "prod_live_date": prod,
    }
    return [{**base, "batch_number": b} for b in batches]


def validate_import_record(raw: dict, *, seen_batches: set, existing_batches: set) -> tuple[dict | None, str | None]:
    """Validate one normalised import row (dict keyed by internal column).

    Returns ``(record, None)`` on success or ``(None, error_message)`` on failure
    (including a duplicate batch number, which is never inserted).
    """
    try:
        rec = {
            "enhancement_id": validate_enhancement_id(raw.get("enhancement_id")),
            "release_subject": (str(raw.get("release_subject") or "").strip()),
            "category": normalize_category(raw.get("category")),
            "sent_by": str(raw.get("sent_by") or "").strip(),
            "batch_number": parse_batch_single(raw.get("batch_number")),
            "crm_delivery_date": parse_date(raw.get("crm_delivery_date"), field="CRM Delivery Date"),
            "sit_date": parse_date(raw.get("sit_date"), field="SIT Date"),
            "uat_date": parse_date(raw.get("uat_date"), field="UAT Date"),
            "preprod_date": parse_date(raw.get("preprod_date"), field="PreProd Date"),
            "prod_live_date": parse_date(raw.get("prod_live_date"), field="Prod Live Date"),
        }
    except ValidationError as exc:
        return None, str(exc)

    if not rec["release_subject"]:
        return None, "Release Mail Subject is required."
    if not rec["sent_by"]:
        return None, "Sent By is required."
    if rec["crm_delivery_date"] is None:
        return None, "CRM Delivery Date is required."
    if rec["category"] == "Other":
        rec["other_category"] = str(raw.get("other_category") or "").strip() or "Other"
    else:
        rec["other_category"] = ""

    b = rec["batch_number"]
    if b in existing_batches:
        return None, f"Batch Number {b} already exists (duplicate skipped)."
    if b in seen_batches:
        return None, f"Batch Number {b} is duplicated within the file."
    return rec, None


def normalize_bulk_row(raw: dict) -> tuple[dict | None, str | None]:
    """Validate one bulk-update row: needs a batch number + >=1 editable column."""
    try:
        batch = parse_batch_single(raw.get("batch_number"))
    except ValidationError as exc:
        return None, str(exc)
    item: dict = {"batch_number": batch}
    for col in ("enhancement_id", "release_subject",
                "sit_date", "uat_date", "preprod_date", "prod_live_date"):
        if col not in raw or raw.get(col) in (None, ""):
            continue
        try:
            if col in DATE_FIELDS:
                item[col] = parse_date(raw.get(col), field=col)
            elif col == "enhancement_id":
                item[col] = validate_enhancement_id(raw.get(col))
            else:
                item[col] = str(raw.get(col)).strip()
        except ValidationError as exc:
            return None, str(exc)
    if len(item) == 1:  # only the batch number, nothing to update
        return None, f"Batch {batch}: no updatable columns present."
    return item, None


# ----------------------------------------------------------------------
# Dynamic grouping (same Enhancement ID + Upload Date + Category)
# ----------------------------------------------------------------------
def compress_batches(numbers: list[int]) -> str:
    """Render a set of batch numbers as compact ranges: [84..90] -> '84-90'."""
    nums = sorted(set(int(n) for n in numbers if n is not None))
    if not nums:
        return ""
    parts, start, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(f"{start}-{prev}" if start != prev else f"{start}")
        start = prev = n
    parts.append(f"{start}-{prev}" if start != prev else f"{start}")
    return ", ".join(parts)


def group_records(rows: list[dict]) -> list[dict]:
    """Group rows sharing (Enhancement ID, Upload Date, Category).

    Returns a list of group dicts. A group with a single member is emitted with
    ``grouped=False``; a multi-member group carries a collapsed ``batch_summary``
    plus its expandable ``children`` (already ordered by batch number).
    """
    buckets: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for r in rows:
        key = (r.get("enhancement_id"), r.get("upload_date"), r.get("category"))
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(r)

    groups = []
    for key in order:
        members = sorted(buckets[key], key=lambda x: (x.get("batch_number") or 0))
        head = dict(members[0])
        groups.append({
            "grouped": len(members) > 1,
            "key": "|".join(str(k) for k in key),
            "enhancement_id": key[0],
            "upload_date": key[1],
            "category": key[2],
            "count": len(members),
            "batch_summary": compress_batches([m.get("batch_number") for m in members]),
            "head": head,
            "children": members,
        })
    return groups
