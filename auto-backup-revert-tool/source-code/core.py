"""
Core logic: archive extraction, natural-sort traversal, DELETE collection,
and BACKUP/REVERT script generation.

Exposed entry points:
    prepare_input(src_path, work_dir) -> Path        # extracts archive or returns source as-is
    collect_deletes(root, add_file_headers=True)     # -> CollectResult
    generate_backup_revert(delete_sql_text)          # -> GenerateResult
"""

from __future__ import annotations

import re
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

# ----------------------------------------------------------------------
# Regex from the original BKP_REVERT.py — kept byte-for-byte compatible.
# ----------------------------------------------------------------------
DELETE_RE = re.compile(
    r"""DELETE\s+FROM\s+
        (?P<table>(?:["'`\[\]]?[\w\.\-]+["'`\[\]]?))
        # Optional table alias (e.g. "DELETE FROM offers o WHERE o.id = ..."):
        # any bareword that ISN'T one of the SQL keywords that legally follow
        # the table name in a DELETE statement.
        (?:\s+(?P<alias>(?!(?:WHERE|RETURNING|USING|WHEN|ON|AS)\b)[\w$]+))?
        (?:\s+WHERE\s+(?P<where>.+?))?
        \s*;?\s*$""",
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)


# ----------------------------------------------------------------------
# Natural sort
# ----------------------------------------------------------------------
_NAT_SPLIT = re.compile(r"(\d+)")


def natural_key(name: str) -> list:
    """Sort key so '2_' < '10_' and '01_' < '02_'."""
    parts = _NAT_SPLIT.split(name)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


# ----------------------------------------------------------------------
# Archive handling
# ----------------------------------------------------------------------
def prepare_input(src: Path, work_dir: Path) -> Path:
    """
    Normalize the user-provided input into a directory or single .sql file.

    - .7z archive  -> extracted into work_dir/extracted/
    - .zip archive -> extracted into work_dir/extracted/
    - directory    -> returned as-is
    - .sql file    -> returned as-is
    """
    src = Path(src).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(f"Input not found: {src}")

    if src.is_dir():
        return src

    suffix = src.suffix.lower()
    if suffix == ".sql":
        return src

    dest = work_dir / "extracted"
    dest.mkdir(parents=True, exist_ok=True)

    if suffix == ".7z":
        import py7zr  # lazy import so missing dep only bites archive users

        with py7zr.SevenZipFile(src, mode="r") as archive:
            archive.extractall(path=dest)
        return dest

    if suffix == ".zip":
        with zipfile.ZipFile(src) as zf:
            zf.extractall(dest)
        return dest

    raise ValueError(f"Unsupported input type: {suffix or '(no extension)'} — expected .7z, .zip, .sql, or a folder.")


def iter_sql_files(root: Path) -> Iterable[Path]:
    """Yield .sql files under root, naturally sorted by (folder path, filename)."""
    root = Path(root)
    if root.is_file():
        if root.suffix.lower() == ".sql":
            yield root
        return

    all_files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".sql"]

    def sort_key(p: Path):
        rel_parts = p.relative_to(root).parts
        return tuple(natural_key(part) for part in rel_parts)

    for p in sorted(all_files, key=sort_key):
        yield p


# ----------------------------------------------------------------------
# DELETE collection
# ----------------------------------------------------------------------
@dataclass
class CollectResult:
    delete_sql: str
    files_scanned: list[dict] = field(default_factory=list)  # [{path, deletes}]
    total_deletes: int = 0
    warnings: list[str] = field(default_factory=list)


def _strip_sql_noise(text: str) -> str:
    """Remove /* */ block comments and -- line comments, preserving string literals."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    out = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        c = text[i]
        if in_string:
            if c == "'":
                if i + 1 < n and text[i + 1] == "'":
                    out.append("''")
                    i += 2
                    continue
                in_string = False
            out.append(c)
            i += 1
        else:
            if c == "'":
                in_string = True
                out.append(c)
                i += 1
            elif c == "-" and i + 1 < n and text[i + 1] == "-":
                while i < n and text[i] != "\n":
                    i += 1
            else:
                out.append(c)
                i += 1
    return "".join(out)


def _read_text(path: Path) -> str:
    """Read text tolerantly (UTF-8 with fallback to latin-1)."""
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="latin-1", errors="replace")


# ----------------------------------------------------------------------
# Skip filter — audit / log / lookup tables that must NOT participate in
# the BACKUP/REVERT ordering (no rollback semantics on append-only data).
# ----------------------------------------------------------------------
_SKIP_TABLE_RE = re.compile(r"(?:^SBC_|_LT$|_LOG$)", re.IGNORECASE)


def _bare_table_name(raw: str) -> str:
    """Strip quotes/brackets and schema prefix → bare table name."""
    t = raw.strip().strip('"').strip("'").strip("`").strip("[").strip("]")
    if "." in t:
        t = t.rsplit(".", 1)[-1]
        t = t.strip('"').strip("'").strip("`").strip("[").strip("]")
    return t


def _should_skip_table(raw: str) -> bool:
    """True if the table is `_LT` / `_LOG` / `SBC_*` (case-insensitive)."""
    return bool(_SKIP_TABLE_RE.search(_bare_table_name(raw)))


def _extract_delete_blocks(content: str) -> list[list[str]]:
    """
    Return DELETE statements grouped into BLOCKS in source order.

    A block is a run of consecutive DELETE statements; any *non-DELETE*
    statement (UPDATE, COMMIT, ALTER, …) closes the current block. Empty
    statements (from `;;` or a trailing `;`) do NOT split a block.

    Preserving block boundaries lets REVERT reverse INSERTs *within each
    block* — restoring parent rows before children — without scrambling
    independent transaction groups across blocks.
    """
    stripped = _strip_sql_noise(content)
    # Oracle SQL*Plus: a lone '/' on its own line acts as a statement terminator.
    # Drop it so '/' doesn't attach to the following DELETE after split(';').
    stripped = re.sub(r"^\s*/\s*$", "", stripped, flags=re.MULTILINE)

    blocks: list[list[str]] = []
    current: list[str] = []
    for stmt in stripped.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        if re.match(r"\s*DELETE\s+FROM\b", stmt, re.IGNORECASE):
            current.append(" ".join(stmt.split()) + ";")
        else:
            if current:
                blocks.append(current)
                current = []
    if current:
        blocks.append(current)
    return blocks


# Regex for ALTER TABLE — captures the statement verbatim, re-terminated with ;.
_ALTER_RE = re.compile(r"^\s*ALTER\s+TABLE\b", re.IGNORECASE)


def _extract_alters(content: str) -> list[str]:
    """Return ALTER TABLE statements found in content, each re-terminated with ';'."""
    stripped = _strip_sql_noise(content)
    stripped = re.sub(r"^\s*/\s*$", "", stripped, flags=re.MULTILINE)

    alters = []
    for stmt in stripped.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        if _ALTER_RE.match(stmt):
            # Preserve multi-line formatting if any — just trim trailing whitespace.
            alters.append(stmt.rstrip() + ";")
    return alters


# Regex for stored-code CREATE statements.
# Matches:
#   CREATE [OR REPLACE] [EDITIONABLE|NONEDITIONABLE] PROCEDURE|FUNCTION|PACKAGE [BODY]|TRIGGER <name>
# Captures the type (group 1) and the qualified name (group 2).
_PROC_RE = re.compile(
    r"""\bCREATE\s+
        (?:OR\s+REPLACE\s+)?
        (?:EDITIONABLE\s+|NONEDITIONABLE\s+)?
        (?P<kind>PROCEDURE|FUNCTION|PACKAGE(?:\s+BODY)?|TRIGGER)
        \s+
        (?P<name>
            (?:"[A-Za-z0-9_$]+"|[A-Za-z][\w$]*)        # schema (or just name)
            (?:\s*\.\s*(?:"[A-Za-z0-9_$]+"|[A-Za-z][\w$]*))?   # optional .name
        )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _extract_procedures(content: str) -> list[tuple[str, str, int]]:
    """
    Return a list of (kind, qualified_name, line_number) for every stored-code
    definition in the given file content.
    """
    # Strip block comments and line comments first; string literals are preserved
    # but that's fine — CREATE PROCEDURE inside a string literal is extremely rare
    # and would also be parsed correctly by Oracle's compiler as a comment.
    stripped = _strip_sql_noise(content)
    hits: list[tuple[str, str, int]] = []
    for m in _PROC_RE.finditer(stripped):
        kind = re.sub(r"\s+", " ", m.group("kind").upper())
        name = re.sub(r"\s+", "", m.group("name"))
        line = stripped.count("\n", 0, m.start()) + 1
        hits.append((kind, name, line))
    return hits


# Regex for ALTER TRIGGER (any action: ENABLE / DISABLE / COMPILE / RENAME).
# Captures the trigger's qualified name only — we discard the action because
# REVERT is going to drive its own DISABLE/ENABLE cycle.
_ALTER_TRIGGER_RE = re.compile(
    r"""\bALTER\s+TRIGGER\s+
        (?P<name>
            (?:"[A-Za-z0-9_$]+"|[A-Za-z][\w$]*)
            (?:\s*\.\s*(?:"[A-Za-z0-9_$]+"|[A-Za-z][\w$]*))?
        )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _extract_alter_trigger_names(content: str) -> list[str]:
    """Return trigger names referenced by `ALTER TRIGGER <name> …` in content."""
    stripped = _strip_sql_noise(content)
    return [re.sub(r"\s+", "", m.group("name")) for m in _ALTER_TRIGGER_RE.finditer(stripped)]


@dataclass
class AltersResult:
    alter_sql: str
    files_scanned: list[dict] = field(default_factory=list)  # [{path, alters}]
    total_alters: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class ProceduresResult:
    procedures_text: str
    procedures_unique_text: str = ""
    procedures: list[dict] = field(default_factory=list)   # [{kind, name, path, line}]
    total: int = 0
    unique_names: int = 0
    warnings: list[str] = field(default_factory=list)


def collect_alters(root: Path, add_file_headers: bool = True) -> AltersResult:
    """Walk root, gather ALTER TABLE statements in traversal order."""
    root = Path(root)
    res = AltersResult(alter_sql="")
    chunks: list[str] = []

    for sql_path in iter_sql_files(root):
        try:
            content = _read_text(sql_path)
        except Exception as exc:  # noqa: BLE001
            res.warnings.append(f"Could not read {sql_path}: {exc}")
            continue

        alters = _extract_alters(content)
        rel = sql_path.relative_to(root if root.is_dir() else root.parent)
        rel_str = str(rel).replace("\\", "/")
        res.files_scanned.append({"path": rel_str, "alters": len(alters)})
        if not alters:
            continue

        if add_file_headers:
            chunks.append(
                "-- " + "=" * 78 + "\n"
                f"-- FILE: {rel_str}\n"
                "-- " + "=" * 78
            )
        chunks.extend(alters)
        chunks.append("")
        res.total_alters += len(alters)

    res.alter_sql = "\n".join(chunks).rstrip() + ("\n" if chunks else "")
    return res


def collect_triggers(root: Path) -> list[str]:
    """
    Walk root, return a *unique* list of trigger names found in the bundle,
    union of:
      - CREATE [OR REPLACE] TRIGGER <name>
      - ALTER TRIGGER <name> …
    Used to wrap REVERT.sql with DISABLE/ENABLE blocks so re-inserts don't
    re-fire audit / cascade triggers during a rollback.
    """
    root = Path(root)
    seen: set[str] = set()
    ordered: list[str] = []

    for sql_path in iter_sql_files(root):
        try:
            content = _read_text(sql_path)
        except Exception:  # noqa: BLE001
            continue
        for kind, name, _line in _extract_procedures(content):
            if kind == "TRIGGER":
                key = name.upper()
                if key not in seen:
                    seen.add(key); ordered.append(name)
        for name in _extract_alter_trigger_names(content):
            key = name.upper()
            if key not in seen:
                seen.add(key); ordered.append(name)
    return ordered


def collect_procedures(root: Path) -> ProceduresResult:
    """Walk root, gather stored-code definitions (PROCEDURE/FUNCTION/PACKAGE/TRIGGER)."""
    root = Path(root)
    res = ProceduresResult(procedures_text="")
    rows: list[dict] = []

    for sql_path in iter_sql_files(root):
        try:
            content = _read_text(sql_path)
        except Exception as exc:  # noqa: BLE001
            res.warnings.append(f"Could not read {sql_path}: {exc}")
            continue
        rel = sql_path.relative_to(root if root.is_dir() else root.parent)
        rel_str = str(rel).replace("\\", "/")
        for kind, name, line in _extract_procedures(content):
            rows.append({"kind": kind, "name": name, "path": rel_str, "line": line})

    res.procedures = rows
    res.total = len(rows)
    res.unique_names = len({(r["kind"], r["name"].upper()) for r in rows})

    # ── Full listing (every definition with its location) ────────────────────
    full_header = (
        "# Stored procedures / functions / packages / triggers found in the bundle\n"
        f"# Total definitions: {res.total}    Unique names: {res.unique_names}\n"
        f"# {'TYPE':<14} {'NAME':<48} LOCATION\n"
        f"# {'-'*14} {'-'*48} {'-'*40}\n"
    )
    if rows:
        lines = [full_header]
        for r in rows:
            lines.append(f"{r['kind']:<14} {r['name']:<48} {r['path']}:{r['line']}")
        res.procedures_text = "\n".join(lines) + "\n"
    else:
        res.procedures_text = full_header + "# (no stored-code definitions found in the bundle)\n"

    # ── Unique-names listing (deduped by (TYPE, UPPER(name))) ────────────────
    # Same key as ``unique_names`` count above so the totals tally.  Sort by
    # TYPE then NAME so the file is reproducibly identical across re-runs of
    # the same bundle.
    seen: set[tuple[str, str]] = set()
    unique_rows: list[dict] = []
    for r in rows:
        key = (r["kind"], r["name"].upper())
        if key in seen:
            continue
        seen.add(key)
        unique_rows.append(r)
    unique_rows.sort(key=lambda r: (r["kind"], r["name"].upper()))

    uniq_header = (
        "# Stored-code UNIQUE names (deduped by TYPE + NAME, case-insensitive)\n"
        f"# Filtered from {res.total} definition(s) → {len(unique_rows)} unique name(s)\n"
        f"# {'TYPE':<14} NAME\n"
        f"# {'-'*14} {'-'*48}\n"
    )
    if unique_rows:
        ulines = [uniq_header]
        for r in unique_rows:
            ulines.append(f"{r['kind']:<14} {r['name']}")
        res.procedures_unique_text = "\n".join(ulines) + "\n"
    else:
        res.procedures_unique_text = uniq_header + "# (no stored-code definitions found in the bundle)\n"

    return res


def collect_deletes(root: Path, add_file_headers: bool = True) -> CollectResult:
    """
    Walk root (file or directory), gather DELETE statements in traversal order.

    Output preserves *block structure*: where a file contains multiple
    DELETE runs separated by non-DELETE statements, each run becomes its
    own block. Blocks within a file are separated by a ``-- ---- BLOCK ----``
    marker; blocks across files are separated by the file header.
    """
    root = Path(root)
    result = CollectResult(delete_sql="")
    chunks: list[str] = []

    for sql_path in iter_sql_files(root):
        try:
            content = _read_text(sql_path)
        except Exception as exc:  # noqa: BLE001
            result.warnings.append(f"Could not read {sql_path}: {exc}")
            continue

        blocks = _extract_delete_blocks(content)
        total_in_file = sum(len(b) for b in blocks)
        rel = sql_path.relative_to(root if root.is_dir() else root.parent)
        rel_str = str(rel).replace("\\", "/")

        result.files_scanned.append({"path": rel_str, "deletes": total_in_file})

        if not blocks:
            continue

        if add_file_headers:
            chunks.append(
                "-- " + "=" * 78 + "\n"
                f"-- FILE: {rel_str}\n"
                "-- " + "=" * 78
            )
        for i, block in enumerate(blocks):
            if i > 0:
                chunks.append("-- ---- BLOCK ----")
            chunks.extend(block)
        chunks.append("")  # blank line between files

        result.total_deletes += total_in_file

    result.delete_sql = "\n".join(chunks).rstrip() + ("\n" if chunks else "")
    return result


# ----------------------------------------------------------------------
# BACKUP / REVERT generation (ported from BKP_REVERT.py, same output shape)
# ----------------------------------------------------------------------
@dataclass
class GenerateResult:
    backup_sql: str
    revert_sql: str
    cleanup_sql: str = ""
    unique_tables: int = 0
    revert_statements: int = 0
    warnings: list[str] = field(default_factory=list)


def _normalize_table_name(raw: str) -> str:
    return raw.strip()


def _backup_line(table: str, sysdate: str) -> str:
    return f"CREATE TABLE BKP_{table}_{sysdate} AS (SELECT * FROM {table});"


def _format_revert_insert(sql_stmt: str, backup_table: str) -> str:
    """
    Build *just the INSERT* block that restores rows for one DELETE statement.

    The DELETE itself is preserved by the caller (we want all DELETEs grouped
    in source order at the top of each block, and INSERTs grouped in reverse
    order at the bottom — so this helper no longer emits the DELETE half).

    If the source DELETE used a table alias (``DELETE FROM offers o WHERE
    o.id = …``), the same alias is threaded into the BKP SELECT and the
    NOT EXISTS sub-query so the WHERE predicate continues to bind correctly.
    """
    m = DELETE_RE.search(sql_stmt.strip())
    if not m:
        return f"-- WARNING: unable to parse DELETE to auto-generate INSERT: {sql_stmt.strip()[:80]}…"
    table = _normalize_table_name(m.group("table"))
    alias = (m.group("alias") or "").strip()
    where = m.group("where")
    a = f" {alias}" if alias else ""

    if where:
        where = where.rstrip().rstrip(";").strip()
        return (
            f"INSERT INTO {table}\n"
            f"(SELECT * FROM {backup_table}{a}\n"
            f" WHERE {where}\n"
            f"   AND NOT EXISTS (SELECT 1 FROM {table}{a} WHERE {where}));"
        )
    return (
        f"INSERT INTO {table}\n"
        f"(SELECT * FROM {backup_table}\n"
        f" WHERE NOT EXISTS (SELECT 1 FROM {table}));"
    )


def _default_sysdate() -> str:
    """
    Timestamp used in backup-table names. Format: ``YYMMDDHH`` (8 digits,
    2-digit year + month + day + 24-hour clock).

    Why 8 digits and not 12: Oracle ≤ 12.1 caps identifiers at **30 characters**.
    A 12-digit suffix on ``BKP_<table>_<ts>`` blows past 30 chars on common
    table names (e.g. ``BKP_CIS_ScheduledJob_260609123431`` is 33). Keeping it
    to YYMMDDHH leaves ~21 chars for the table name and stays compatible with
    older Oracle versions.

    Trade-off: two runs within the SAME hour will collide on the backup-table
    name and BACKUP.sql's ``CREATE TABLE`` would fail with ORA-00955
    (name already used by an existing object). Drop the existing BKP_* tables
    or pass an explicit ``sysdate=`` override if you need to re-run in the
    same hour.
    """
    return datetime.now().strftime("%y%m%d%H")


def generate_backup_revert(
    delete_sql_text: str,
    sysdate: str | None = None,
    triggers: list[str] | None = None,
) -> GenerateResult:
    """
    Consume a delete.sql text and produce BACKUP.sql + REVERT.sql strings.

    If `triggers` is non-empty, REVERT.sql is wrapped with
    ``ALTER TRIGGER <name> DISABLE;`` at the top and ``ALTER TRIGGER <name>
    ENABLE;`` at the bottom — so the restoring INSERTs do not re-fire audit /
    cascade triggers detected in the original bundle.
    """
    sysdate = sysdate or _default_sysdate()
    res = GenerateResult(backup_sql="", revert_sql="")

    backup_lines: list[str] = []
    seen_backups: set[str] = set()

    # blocks[i] is the i-th DELETE block in source order.
    # Each entry within a block is (source_file_or_None, delete_sql, insert_sql).
    # When emitting REVERT, for each block we emit:
    #   1. All DELETEs in source order (child → parent) — clears the targets
    #      so the restore can land cleanly.
    #   2. An Oracle SQL*Plus `/` terminator.
    #   3. All INSERTs in REVERSE source order (parent → child) — FK-safe
    #      restore.
    # Block boundaries from the source bundle are preserved.
    # Passthrough non-DELETE statements (rare; only via raw API input) carry
    # insert_sql="" and are emitted inline at their source position.
    blocks: list[list[tuple[str | None, str, str]]] = [[]]
    current_file: str | None = None

    file_header_re  = re.compile(r"^\s*--\s*FILE:\s*(.+?)\s*$")
    block_header_re = re.compile(r"^\s*--\s*-+\s*BLOCK\s*-+\s*$", re.IGNORECASE)

    # Tables omitted from BACKUP+REVERT entirely (audit/log/lookup).
    skipped_tables: list[str] = []
    seen_skipped: set[str] = set()

    def _close_block_if_dirty() -> None:
        """Open a fresh block iff the current one already has entries."""
        if blocks[-1]:
            blocks.append([])

    for stmt, header in _split_preserving_headers(delete_sql_text):
        if header:
            if m := file_header_re.match(header):
                current_file = m.group(1).strip()
                _close_block_if_dirty()
            elif block_header_re.match(header):
                _close_block_if_dirty()
            continue

        stripped = stmt.strip()
        if not stripped:
            continue

        m = DELETE_RE.search(stripped)
        if not m:
            # Not a DELETE — keep verbatim so nothing is lost, but isolate it
            # from the surrounding block: close the current block, emit the
            # passthrough as its own single-entry block (no INSERT half), then
            # open a fresh block for whatever follows.
            _close_block_if_dirty()
            blocks[-1].append((current_file, stripped.rstrip(";") + ";", ""))
            blocks.append([])
            res.warnings.append(f"Non-DELETE statement passed through: {stripped[:80]}…")
            continue

        raw_table = m.group("table")
        if _should_skip_table(raw_table):
            bare = _bare_table_name(raw_table)
            if bare.upper() not in seen_skipped:
                seen_skipped.add(bare.upper())
                skipped_tables.append(bare)
            continue  # don't back up, don't revert — fully ignored

        table = _normalize_table_name(raw_table)
        backup_name = f"BKP_{table}_{sysdate}"
        if backup_name not in seen_backups:
            backup_lines.append(_backup_line(table, sysdate))
            seen_backups.add(backup_name)

        delete_sql = stripped.rstrip(";") + ";"
        insert_sql = _format_revert_insert(delete_sql, backup_name)
        blocks[-1].append((current_file, delete_sql, insert_sql))
        res.revert_statements += 1

    # Drop empty blocks (e.g., consecutive headers, or a file with only skipped
    # tables).  Result: only blocks that actually produced REVERT entries.
    blocks = [b for b in blocks if b]

    # ── Emit REVERT.sql ───────────────────────────────────────────────────────
    rule = "-- " + "=" * 78
    revert_chunks: list[str] = [
        rule,
        "-- REVERT.sql",
        "-- Each DELETE block from the source bundle is replayed below in two halves:",
        "--   1. Original DELETE statements in source order (child → parent) — clears",
        "--      target rows so the restore lands cleanly.",
        "--   2. Matching INSERTs in REVERSE source order (parent → child) — restores",
        "--      from BKP_* snapshots while satisfying FK constraints during rollback.",
        "-- Block boundaries from the source bundle are preserved.",
        "--",
        "-- ASSUMPTION: the original migration's DELETE statements are already written",
        "-- in child → parent order (they must be, or the forward migration would have",
        "-- failed with ORA-02292). REVERT echoes that order verbatim in the top half",
        "-- of each block; the bottom half flips it for FK-safe restoration.",
        rule,
        "",
    ]
    if skipped_tables:
        revert_chunks.append("-- " + "-" * 78)
        revert_chunks.append(
            f"-- OMITTED from BACKUP/REVERT — {len(skipped_tables)} audit/log/lookup "
            "table(s) matching"
        )
        revert_chunks.append("-- the `_LT` / `_LOG` / `SBC_*` patterns:")
        line = "--   "
        for name in skipped_tables:
            candidate = f"{line}{name}, "
            if len(candidate) > 78 and line.strip(" -,") != "":
                revert_chunks.append(line.rstrip(", "))
                line = f"--   {name}, "
            else:
                line = candidate
        if line.strip(" -,") != "":
            revert_chunks.append(line.rstrip(", "))
        revert_chunks.append("-- " + "-" * 78)
        revert_chunks.append("")

    # Number only blocks that actually produce INSERTs.  Passthrough atoms
    # (non-DELETE) get a separate marker and don't consume a block number.
    total_real_blocks = sum(1 for b in blocks if any(e[2] for e in b))
    real_block_num = 0
    first_emitted = True

    for block in blocks:
        has_inserts = any(e[2] for e in block)

        if not first_emitted:
            revert_chunks.append("")  # extra blank → 2 blank lines between blocks
        first_emitted = False

        # Source-file annotations (one per distinct file in this block).
        src_files = list(dict.fromkeys(e[0] for e in block if e[0]))
        for sf in src_files:
            revert_chunks.append(f"-- (from FILE: {sf})")

        if has_inserts:
            real_block_num += 1
            revert_chunks.append(f"--Block {real_block_num} of {total_real_blocks}")
            # DELETEs in original source order (child → parent).
            for _src, del_sql, _ins in block:
                revert_chunks.append(del_sql)
            # Oracle SQL*Plus statement separator between the two halves.
            revert_chunks.append("/")
            revert_chunks.append("")
            # INSERTs in REVERSE source order (parent → child).
            for _src, _del, ins_sql in reversed(block):
                if ins_sql:
                    revert_chunks.append(ins_sql)
                    revert_chunks.append("")
        else:
            revert_chunks.append(
                "-- (passthrough: non-DELETE statement(s) preserved in source position)"
            )
            for _src, raw_sql, _ in block:
                revert_chunks.append(raw_sql)
                revert_chunks.append("")

    res.backup_sql = "\n".join(backup_lines) + ("\n" if backup_lines else "")
    res.revert_sql = "\n".join(revert_chunks).rstrip() + "\n" if blocks else ""
    res.unique_tables = len(seen_backups)
    if skipped_tables:
        res.warnings.append(
            f"Skipped {len(skipped_tables)} audit/log/lookup table(s) from "
            f"BACKUP+REVERT: {', '.join(skipped_tables)}"
        )

    # ── CLEANUP.sql ───────────────────────────────────────────────────────────
    # One DROP per BKP_* snapshot table created by BACKUP.sql, in the same
    # order BACKUP.sql created them.  Emitted ONLY when at least one backup
    # table was created — empty bundles get no CLEANUP file.
    if seen_backups:
        # Walk backup_lines (not the set) so DROP order matches CREATE order.
        bkp_table_names: list[str] = []
        bkp_re = re.compile(r"CREATE TABLE (BKP_\S+) AS", re.IGNORECASE)
        for line in backup_lines:
            m = bkp_re.search(line)
            if m:
                bkp_table_names.append(m.group(1))

        cleanup_rule = "-- " + "=" * 78
        cleanup_chunks: list[str] = [
            cleanup_rule,
            "-- CLEANUP.sql",
            f"-- Drops the {len(bkp_table_names)} BKP_* snapshot table(s) created by",
            "-- the matching BACKUP.sql.  Run this ONLY after you are confident the",
            "-- rollback window has passed and the snapshots are no longer needed.",
            "--",
            "-- Tip: append `PURGE` to skip Oracle's recycle bin (without PURGE the",
            "-- dropped table is kept in USER_RECYCLEBIN and still consumes",
            "-- tablespace until it is purged).",
            cleanup_rule,
            "",
        ]
        for name in bkp_table_names:
            cleanup_chunks.append(f"DROP TABLE {name};")
            cleanup_chunks.append("/")
        res.cleanup_sql = "\n".join(cleanup_chunks).rstrip() + "\n"

    # Optional trigger DISABLE/ENABLE wrapping. We dedupe here too in case the
    # caller passes the raw list.
    if triggers:
        unique_trigs: list[str] = []
        seen: set[str] = set()
        for t in triggers:
            key = t.upper()
            if key not in seen:
                seen.add(key); unique_trigs.append(t)

        rule = "-- " + "=" * 78
        disable_block = "\n".join([
            rule,
            f"-- TRIGGERS — disable {len(unique_trigs)} trigger(s) detected in the bundle",
            "-- so the restoring INSERTs below don't re-fire audit / cascade logic.",
            "-- Re-enabled at the bottom of this script.",
            rule,
            *[f"ALTER TRIGGER {name} DISABLE;" for name in unique_trigs],
            "",
        ])
        enable_block = "\n".join([
            "",
            rule,
            "-- TRIGGERS — re-enable everything we disabled at the top.",
            rule,
            *[f"ALTER TRIGGER {name} ENABLE;" for name in unique_trigs],
            "",
        ])
        res.revert_sql = disable_block + "\n" + res.revert_sql + enable_block

    return res


def _split_preserving_headers(text: str):
    """
    Yield (statement, header_line) tuples in order.

    Header lines are the '-- FILE:' comment blocks we inject during collection.
    For each non-header region, we split on ';' to produce statements.
    """
    # Walk line-by-line: emit header comment blocks as separate tokens so revert.sql
    # can keep the same traceability structure.
    buf: list[str] = []

    def flush_buf():
        joined = "\n".join(buf).strip()
        buf.clear()
        if not joined:
            return []
        # split on ';' respecting that our DELETEs are single-line statements
        parts = [p.strip() for p in joined.split(";") if p.strip()]
        return parts

    for line in text.splitlines():
        if line.strip().startswith("--"):
            # accumulated body first
            for stmt in flush_buf():
                yield (stmt, None)
            yield (None, line)
        else:
            buf.append(line)

    for stmt in flush_buf():
        yield (stmt, None)


# ----------------------------------------------------------------------
# Filename helpers
# ----------------------------------------------------------------------
def timestamped(prefix: str, base: str, ext: str = ".sql", now: datetime | None = None) -> str:
    now = now or datetime.now()
    safe_base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._-") or "job"
    return f"{prefix}_{safe_base}_{now.strftime('%Y%m%d_%H%M%S')}{ext}"


# ----------------------------------------------------------------------
# Convenience cleanup
# ----------------------------------------------------------------------
def safe_rmtree(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass
