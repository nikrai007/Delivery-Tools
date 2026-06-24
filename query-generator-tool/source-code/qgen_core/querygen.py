"""Large Payload Query Generator -- turns a raw payload into a single Standard SQL
``UPDATE`` and an Oracle PL/SQL block that stores the value as an NCLOB assembled
from fixed-size chunks.

This module is a **drop-in replacement for a legacy production script**, so the
output is reproduced *byte-for-byte*: the quoting, the chunk boundaries, and even
the (irregular) whitespace of the PL/SQL boilerplate are intentional and frozen in
``tests/vectors/querygen_sample_payload.txt`` + ``tests/test_querygen.py``.

Like :mod:`crypto`, :mod:`transforms`, and :mod:`operations`, everything here is a
**pure function** -- no UI, no I/O -- so the webview, the tkinter fallback, and the
CLI all call the same code and the format can be unit-tested without a screen.

CHUNK ORDER (verified against the legacy sample, and it matters):
    The legacy slices the **raw** payload at the chunk size and escapes each chunk
    *afterwards* -- i.e. boundaries are counted on the raw string, not on the
    escaped one. In the reference sample the 3000-char cut lands at raw offset 3000,
    mid ``maskFormatID`` (``maskForma`` | ``tID``). (The original written spec said
    "escape, then slice the escaped text"; the real output disproves that -- the two
    differ whenever single quotes precede a boundary.) A welcome side effect: because
    each chunk is escaped on its own, a boundary can never bisect an escaped ``''``
    pair, so every chunk always wraps into a valid Oracle literal.

DO NOT "pretty-print" or "optimise" the output. Production deploy pipelines diff it.
"""

# Defaults -- mirror the legacy script (and pre-fill the UI fields).
DEFAULT_TABLE = "designer"
DEFAULT_COLUMN = "designconfig"
DEFAULT_FILTER_COLUMN = "designerid"
DEFAULT_FILTER_CONDITION = "= 4"
DEFAULT_CHUNK_SIZE = 3000

# The Oracle boilerplate, captured verbatim from the legacy output. The leading
# whitespace is deliberately irregular (tabs on the Declare/Begin/end lines, spaces
# on the declaration line) -- this is what the legacy emitted, so we keep it exactly.
# Only the four ``{...}`` slots are filled in.
PLSQL_SKELETON = (
    "Declare\n"
    "\t\t\tv_sql nclob;\n"
    "            {declarations}\n"
    "\t\t\tBegin\n"
    "{assignments}\n"
    "\t\t\t\t{concatenation}\n"
    "\t\t\t\t {update}\n"
    "\t\t\tend;"
)


def escape_sql(raw):
    """Double every single quote so the text is safe inside a SQL literal.

    ``it's`` -> ``it''s``. Used for the whole payload (Standard SQL) and for each
    chunk individually (Oracle). Note it distributes over concatenation --
    ``escape_sql(a) + escape_sql(b) == escape_sql(a + b)`` -- so the joined Oracle
    chunks reproduce exactly the Standard SQL's escaped value.
    """
    return raw.replace("'", "''")


def build_standard_sql(
    escaped,
    table=DEFAULT_TABLE,
    column=DEFAULT_COLUMN,
    filter_column=DEFAULT_FILTER_COLUMN,
    filter_condition=DEFAULT_FILTER_CONDITION,
):
    """The single-statement UPDATE.

    Template (lowercase ``update``/``set``, uppercase ``WHERE``, trailing ``;``)::

        update {table} set {column} = '{escaped}' WHERE {filter_column} {filter_condition};

    ``escaped`` is the *whole* payload after :func:`escape_sql`. The
    ``filter_condition`` is injected verbatim right after the filter column, so
    ``designerid`` + ``= 4`` -> ``designerid = 4``.
    """
    return (
        f"update {table} set {column} = '{escaped}' "
        f"WHERE {filter_column} {filter_condition};"
    )


def chunk_text(text, chunk_size=DEFAULT_CHUNK_SIZE):
    """Slice the **raw** payload into fixed-size pieces by character count.

    A cut is made at an exact character offset even if it falls mid-word or
    mid-tag -- the legacy makes no attempt to respect word/JSON/XML boundaries, and
    neither do we. The slice is on the *raw* text (escaping happens per chunk
    afterwards, see :func:`generate_queries`), which is why a boundary can never
    split an escaped ``''`` pair.

    Returns a list with at least one element (``[""]`` for empty input).
    """
    if int(chunk_size) <= 0:
        raise ValueError("Chunk size must be a positive integer")
    chunk_size = int(chunk_size)
    if text == "":
        return [""]
    return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]


def build_plsql(
    escaped_chunks,
    table=DEFAULT_TABLE,
    column=DEFAULT_COLUMN,
    filter_column=DEFAULT_FILTER_COLUMN,
    filter_condition=DEFAULT_FILTER_CONDITION,
):
    """The Oracle PL/SQL block, from a list of already-escaped chunk strings.

    For ``n`` chunks it declares ``v_sql1 .. v_sqln`` (NCLOB), assigns each chunk to
    one variable, concatenates them into ``v_sql``, then UPDATEs the column. Layout
    matches the legacy output exactly (see :data:`PLSQL_SKELETON`)::

        Declare
                    v_sql nclob;
                    v_sql1   nclob; v_sql2   nclob;
                    Begin
        v_sql1 :='<chunk1>'; v_sql2 :='<chunk2>';
                        v_sql := v_sql1   || v_sql2  ;
                         update <table> set <column> = v_sql WHERE <col> <cond>;
                    end;

    NOTE: the ``n >= 3`` spacing of the declaration/concatenation lines is *inferred*
    from a 2-chunk legacy sample (a clean join rule that reproduces it exactly). If a
    real >=3-chunk legacy run ever differs, it is a one-line change to the join
    separators below.
    """
    n = len(escaped_chunks)
    declarations = " ".join(f"v_sql{k}   nclob;" for k in range(1, n + 1))
    assignments = " ".join(
        f"v_sql{k} :='{escaped_chunks[k - 1]}';" for k in range(1, n + 1)
    )
    concatenation = (
        "v_sql := " + "   || ".join(f"v_sql{k}" for k in range(1, n + 1)) + "  ;"
    )
    update = (
        f"update {table} set {column} = v_sql "
        f"WHERE {filter_column} {filter_condition};"
    )
    return PLSQL_SKELETON.format(
        declarations=declarations,
        assignments=assignments,
        concatenation=concatenation,
        update=update,
    )


def generate_queries(
    raw,
    table=DEFAULT_TABLE,
    column=DEFAULT_COLUMN,
    filter_column=DEFAULT_FILTER_COLUMN,
    filter_condition=DEFAULT_FILTER_CONDITION,
    chunk_size=DEFAULT_CHUNK_SIZE,
):
    """Full pipeline. Returns a dict the front-ends render directly::

        {
          "standard_sql":   "...",  # the single UPDATE (escape whole payload)
          "plsql":          "...",  # the chunked Oracle block (slice raw, escape each)
          "chunk_count":    N,
          "raw_length":     L,      # chars in the raw payload
          "escaped_length": M,      # chars after escaping the whole payload
          "chunk_size":     chunk_size,
        }
    """
    escaped = escape_sql(raw)
    raw_chunks = chunk_text(raw, chunk_size)
    escaped_chunks = [escape_sql(chunk) for chunk in raw_chunks]
    return {
        "standard_sql": build_standard_sql(
            escaped, table, column, filter_column, filter_condition
        ),
        "plsql": build_plsql(
            escaped_chunks, table, column, filter_column, filter_condition
        ),
        "chunk_count": len(raw_chunks),
        "raw_length": len(raw),
        "escaped_length": len(escaped),
        "chunk_size": int(chunk_size),
    }
