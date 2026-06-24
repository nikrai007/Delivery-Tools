"""
Encrypt Decrypt Utility — Flask blueprint ``edu``.

AES-256-CBC encrypt / decrypt / nonce, byte-for-byte interoperable with the
original C#/desktop tool. The crypto runs **server-side** using the exact same
tested modules the desktop app uses (edu_core.operations) — so the AES key never
reaches the browser and the output is guaranteed identical. The page's JS is the
UI only; it POSTs to ``/api`` which returns ``{ok, value}`` / ``{ok, error}``,
mirroring the original pywebview bridge contract verbatim.
"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from edu_core.operations import decrypt_string, encrypt_string, generate_nonce

_HERE = Path(__file__).resolve().parents[1]  # encrypt-decrypt-tool/

edu_bp = Blueprint(
    "edu", __name__,
    url_prefix="/tools/encrypt-decrypt",
    template_folder=str(_HERE / "templates"),
)


@edu_bp.route("/")
@login_required
def index():
    return render_template("encrypt_decrypt.html")


@edu_bp.route("/api", methods=["POST"])
@login_required
def api():
    """Mirror of webview_ui.Api: encrypt / decrypt / nonce."""
    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "").strip()
    text = data.get("text") or ""
    custom_iv = data.get("custom_iv") or ""
    user_id = data.get("user_id") or ""

    if action == "encrypt":
        try:
            return jsonify(ok=True, value=encrypt_string(text, custom_iv, user_id))
        except Exception as exc:  # noqa: BLE001
            return jsonify(ok=False, error=str(exc))
    if action == "decrypt":
        try:
            return jsonify(ok=True, value=decrypt_string(text, custom_iv, user_id))
        except Exception as exc:  # noqa: BLE001
            # Same hint the desktop bridge appends on a failed decrypt.
            return jsonify(ok=False, error=f"{exc}  (check the token / IV / User Id)")
    if action == "nonce":
        try:
            return jsonify(ok=True, value=generate_nonce())
        except Exception as exc:  # noqa: BLE001
            return jsonify(ok=False, error=str(exc))

    return jsonify(ok=False, error="Unknown action")
