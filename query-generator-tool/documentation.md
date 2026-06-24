# Query Generator (blueprint: `qgen`)

Turns a large raw payload (XML / JSON / text) into **(a)** one Standard SQL
`UPDATE` and **(b)** an Oracle PL/SQL block that rebuilds the value as an NCLOB
from fixed-size chunks. The output reproduces a legacy production script
**byte-for-byte** — no pretty-printing, no whitespace "fixes"; pipelines diff it.

## Layout

| Path | Role |
|---|---|
| `source-code/qgen_core/querygen.py` | The generator, ported **verbatim** (escape, chunk, Standard SQL, the frozen PL/SQL skeleton). |
| `source-code/qgen_routes.py` | Flask blueprint `qgen`. `GET /tools/query-generator/` (page) + `POST /tools/query-generator/api` (JSON). |
| `templates/query_generator.html` | The UI (extends `templates/tool_base.html`). |

## Core logic (frozen)

- `escape_sql(s)` doubles every single quote (`it's` → `it''s`).
- **Standard SQL**: escape the whole payload →
  `update {table} set {column} = '{escaped}' WHERE {filter_col} {filter_cond};`
  (lowercase `update`/`set`, uppercase `WHERE`, trailing `;`; the filter
  condition is injected verbatim after the column).
- **Chunking**: slice the **raw** payload by character count, then `escape_sql`
  **each chunk** afterwards (never escape-then-slice) — so a boundary can never
  split an escaped `''` pair. Empty payload → `[""]`.
- **Oracle PL/SQL**: fill the exact `PLSQL_SKELETON` (irregular tabs/spaces are
  intentional) — `v_sql1..v_sqln` NCLOB declarations, per-chunk assignments,
  `   || ` concatenation, then the UPDATE.

## Defaults

table `designer`, column `designconfig`, filter column `designerid`, filter
condition `= 4`, chunk size `3000`.

## Features

Five config fields + Raw Payload with a **live character count**; **Generate**
disabled until the payload is non-empty (and while generating); two output
**sub-tabs** (Standard SQL / Oracle PL/SQL); readout
`{n} chunk(s) · {chars} chars · size {n}`; **Copy** the visible sub-tab; the 5
config fields (not the payload) persist in `localStorage` (`qgen-config`).
Error messages: `Chunk size must be a whole number` / `… greater than 0`.

## Acceptance vectors (verified server-side)

- `build_standard_sql(escape_sql("a'b"))` → `update designer set designconfig = 'a''b' WHERE designerid = 4;`
- `chunk_text("abcdefg", 3)` → `["abc","def","g"]`; `chunk_text("", 3000)` → `[""]`.
- `generate_queries("ab'c", chunk_size=2)` → 2 chunks; PL/SQL contains
  `v_sql1 :='ab'; v_sql2 :='''c';`.
