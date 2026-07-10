"""
Batch Number generation, validation and filename slugging.

The requirements reinterpret "Batch Number" as an application-generated,
human-readable identifier for one upload session — auto-populated after upload,
editable by the user, and used as the default basis for download filenames.

Format:  ``XPM-YYYYMMDD-HHMMSS-XXX``  (XXX = 3 random base32 chars)
This is sortable, collision-resistant and readable at a glance.
"""

from __future__ import annotations

import re
import secrets
import string
from datetime import datetime

_ALPHABET = string.ascii_uppercase + string.digits
# Batch numbers a user may type: letters, digits, dot, dash, underscore, 3..64.
_VALID_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")


def generate(now: datetime | None = None) -> str:
    """Return a fresh, effectively-unique batch number."""
    now = now or datetime.now()
    suffix = "".join(secrets.choice(_ALPHABET) for _ in range(3))
    return f"XPM-{now:%Y%m%d}-{now:%H%M%S}-{suffix}"


def normalise(value: str) -> str:
    """Trim + collapse a user-entered batch number to a safe token."""
    return (value or "").strip()


def is_valid(value: str) -> bool:
    return bool(_VALID_RE.match(normalise(value)))


def slug_for_filename(value: str) -> str:
    """Make a batch number safe to embed in a filename (never empty)."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", normalise(value)).strip("._-")
    return s or "XPM_BATCH"
