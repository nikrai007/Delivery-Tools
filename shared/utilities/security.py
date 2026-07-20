"""
Security primitives for enterprise hardening (dependency-free).

Contains:
  * Configurable password-policy validation (`validate_password`).
  * RFC 6238 TOTP (time-based 2FA) implemented with the standard library —
    secret generation, provisioning URI, and code verification with a ±1 step
    window. No third-party OTP package required.
  * Small helpers for lockout / rate-limit thresholds read from settings.

All policy knobs live in the `settings` table (admin-editable) with safe
defaults, so behaviour can be tuned from the UI without code changes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

import models

# ----------------------------------------------------------------------
# Settings helpers (with defaults)
# ----------------------------------------------------------------------
_DEFAULTS = {
    "security.pw_min_length":     "8",
    "security.pw_require_upper":  "0",
    "security.pw_require_lower":  "0",
    "security.pw_require_digit":  "0",
    "security.pw_require_symbol": "0",
    "security.lockout_threshold": "5",     # failed attempts before lock
    "security.lockout_minutes":   "15",    # lock duration
    "security.ratelimit_max":     "10",    # attempts per IP per window
    "security.ratelimit_window":  "300",   # window seconds (5 min)
    "security.require_admin_2fa": "0",      # force 2FA enrolment for admins
    "security.session_timeout_minutes": "5",
}


def get_int(key: str) -> int:
    raw = None
    try:
        raw = models.setting_get(key)
    except Exception:  # noqa: BLE001
        raw = None
    if raw is None or raw == "":
        raw = _DEFAULTS.get(key, "0")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(_DEFAULTS.get(key, "0"))


def get_bool(key: str) -> bool:
    raw = None
    try:
        raw = models.setting_get(key)
    except Exception:  # noqa: BLE001
        raw = None
    if raw is None or raw == "":
        raw = _DEFAULTS.get(key, "0")
    return str(raw) == "1"


def password_policy() -> dict:
    return {
        "min_length":     get_int("security.pw_min_length"),
        "require_upper":  get_bool("security.pw_require_upper"),
        "require_lower":  get_bool("security.pw_require_lower"),
        "require_digit":  get_bool("security.pw_require_digit"),
        "require_symbol": get_bool("security.pw_require_symbol"),
    }


def describe_policy() -> str:
    p = password_policy()
    bits = [f"at least {p['min_length']} characters"]
    if p["require_upper"]:  bits.append("an uppercase letter")
    if p["require_lower"]:  bits.append("a lowercase letter")
    if p["require_digit"]:  bits.append("a digit")
    if p["require_symbol"]: bits.append("a symbol")
    return "Password must contain " + ", ".join(bits) + "."


def validate_password(password: str) -> list[str]:
    """Return a list of human error strings ([] == valid) against the policy."""
    p = password_policy()
    errors: list[str] = []
    if len(password) < p["min_length"]:
        errors.append(f"Password must be at least {p['min_length']} characters.")
    if p["require_upper"] and not any(c.isupper() for c in password):
        errors.append("Password must contain an uppercase letter.")
    if p["require_lower"] and not any(c.islower() for c in password):
        errors.append("Password must contain a lowercase letter.")
    if p["require_digit"] and not any(c.isdigit() for c in password):
        errors.append("Password must contain a digit.")
    if p["require_symbol"] and all(c.isalnum() for c in password):
        errors.append("Password must contain a symbol.")
    return errors


# ----------------------------------------------------------------------
# TOTP (RFC 6238) — stdlib only
# ----------------------------------------------------------------------
_TOTP_STEP = 30
_TOTP_DIGITS = 6


def generate_totp_secret() -> str:
    """A fresh base32 secret (no padding) suitable for authenticator apps."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    # Re-pad the base32 secret to a multiple of 8 for decoding.
    pad = "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(secret_b32.upper() + pad)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** _TOTP_DIGITS)
    return str(code_int).zfill(_TOTP_DIGITS)


def verify_totp(secret_b32: str, code: str, *, window: int = 1) -> bool:
    """Verify a 6-digit TOTP code, allowing ±`window` steps for clock skew."""
    if not secret_b32 or not code:
        return False
    code = code.strip().replace(" ", "")
    if not code.isdigit():
        return False
    counter = int(time.time()) // _TOTP_STEP
    for drift in range(-window, window + 1):
        try:
            if hmac.compare_digest(_hotp(secret_b32, counter + drift), code):
                return True
        except Exception:  # noqa: BLE001 — malformed secret, etc.
            return False
    return False


def provisioning_uri(secret_b32: str, account: str, issuer: str) -> str:
    """otpauth:// URI for QR-code enrolment in an authenticator app."""
    label = quote(f"{issuer}:{account}")
    return (f"otpauth://totp/{label}?secret={secret_b32}"
            f"&issuer={quote(issuer)}&digits={_TOTP_DIGITS}&period={_TOTP_STEP}")
