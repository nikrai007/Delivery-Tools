"""
Query Generator — Flask blueprint ``qgen``.

Turns a large raw payload into (a) one Standard SQL ``UPDATE`` and (b) an Oracle
PL/SQL block that rebuilds the value as an NCLOB from fixed-size chunks. Output is
reproduced **byte-for-byte** by the same tested ``qgen_core.querygen`` module the
desktop tool uses — no reformatting, pipelines diff this. The page POSTs to
``/api`` and gets ``{ok, value}`` / ``{ok, error}``, mirroring the desktop bridge.
"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from qgen_core.querygen import generate_queries

_HERE = Path(__file__).resolve().parents[1]  # query-generator-tool/

qgen_bp = Blueprint(
    "qgen", __name__,
    url_prefix="/tools/query-generator",
    template_folder=str(_HERE / "templates"),
)


@qgen_bp.route("/")
@login_required
def index():
    return render_template("query_generator.html")


@qgen_bp.route("/api", methods=["POST"])
@login_required
def api():
    """Mirror of webview_ui.Api.generate_queries (incl. exact error messages)."""
    data = request.get_json(silent=True) or {}
    raw = data.get("raw") or ""
    table = data.get("table") or ""
    column = data.get("column") or ""
    filter_column = data.get("filter_column") or ""
    filter_condition = data.get("filter_condition") or ""
    chunk_size = data.get("chunk_size", 3000)

    try:
        try:
            size = int(chunk_size)
        except (TypeError, ValueError):
            return jsonify(ok=False, error="Chunk size must be a whole number")
        if size <= 0:
            return jsonify(ok=False, error="Chunk size must be greater than 0")
        return jsonify(
            ok=True,
            value=generate_queries(
                raw,
                table=table,
                column=column,
                filter_column=filter_column,
                filter_condition=filter_condition,
                chunk_size=size,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))
