"""
Release Tracker — at-rest encryption for saved database-connection passwords.

Project database configurations are persisted so future operations can reconnect
without re-prompting. The connection *password* must never be stored in clear
text, so it is encrypted here with AES-256-GCM using a key derived from the
platform ``SECRET_KEY`` (the same secret that signs sessions). This reuses
``Cryptodome`` which already ships in the environment (the Encrypt/Decrypt tool
depends on it) — no new dependency.

Ciphertext format (URL-safe base64): ``nonce(12) || tag(16) || ciphertext``.
Decryption is defensive: any tampering or key change yields ``""`` rather than
raising into a request handler.
"""

from __future__ import annotations

import base64
import hashlib
import logging

from Cryptodome.Cipher import AES
from Cryptodome.Random import get_random_bytes

import constants

log = logging.getLogger("release-tracker.secrets")

_NONCE_BYTES = 12
_TAG_BYTES = 16


def _key() -> bytes:
    """Derive a stable 32-byte AES key from the platform secret."""
    return hashlib.sha256(constants.SECRET_KEY.encode("utf-8")).digest()


def encrypt(plaintext: str | None) -> str:
    """Encrypt a secret to a URL-safe base64 token. Empty input -> ""."""
    if not plaintext:
        return ""
    nonce = get_random_bytes(_NONCE_BYTES)
    cipher = AES.new(_key(), AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(plaintext.encode("utf-8"))
    return base64.urlsafe_b64encode(nonce + tag + ct).decode("ascii")


def decrypt(token: str | None) -> str:
    """Decrypt a token produced by :func:`encrypt`. Any failure -> "" (never raises)."""
    if not token:
        return ""
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        nonce, tag, ct = (raw[:_NONCE_BYTES],
                          raw[_NONCE_BYTES:_NONCE_BYTES + _TAG_BYTES],
                          raw[_NONCE_BYTES + _TAG_BYTES:])
        cipher = AES.new(_key(), AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ct, tag).decode("utf-8")
    except Exception:  # noqa: BLE001 — bad/rotated key or tampered token
        log.warning("Failed to decrypt a stored connection secret (key rotated or data tampered).")
        return ""
