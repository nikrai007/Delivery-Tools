"""
Admin-editable screen content — a generic, reusable text/CMS layer.

Static on-screen copy (titles, headers, descriptions, instructions, help text,
notes, informational messages) becomes administrator-editable *without a
deployment* by resolving each piece of copy through this helper:

    {{ content('xpm_upload', 'help_title', 'What happens next') }}

Resolution: an admin override in the platform ``settings`` table
(key ``screen.<screen>.<field>``) wins; otherwise the template's built-in
default is used. So every call is safe even before any admin edits anything.

Adding a new editable screen is data-only — register it in ``SCREENS`` and use
``content()`` in the template plus the shared ``edit_button`` macro. No new
tables, no schema change, no architectural change: it reuses the existing
key-value settings store and admin console framework.
"""

from __future__ import annotations

import models

_PREFIX = "screen."
MAX_LEN = 8000  # per-field cap (validation)

# Registry of editable screens. Each field: key, label, type ('text'|'textarea'),
# and the built-in default (kept in sync with the template's fallback text).
SCREENS: dict[str, dict] = {
    "xpm_upload": {
        "label": "XPM Automator — New upload",
        "endpoint": "xpm.new_run",
        "fields": [
            {"key": "help_title", "label": "Help panel heading", "type": "text",
             "default": "What happens next"},
            {"key": "vpn_note", "label": "VPN note", "type": "textarea",
             "default": "XPM is reachable only on the Noida office VPN. If a run can't "
                        "connect, check the VPN and retry — no partial state is committed."},
        ],
    },
    "xpm_explorer": {
        "label": "XPM Automator — Batch explorer",
        "endpoint": "xpm.explorer",
        "fields": [
            {"key": "hint", "label": "Toolbar hint", "type": "textarea",
             "default": "Fetch to pick a project live — its processes load automatically. "
                        "Requires the Noida office VPN."},
        ],
    },
}


def _skey(screen: str, field: str) -> str:
    return f"{_PREFIX}{screen}.{field}"


def get(screen: str, field: str, default: str = "") -> str:
    """Resolve one piece of copy: admin override (settings) → template default."""
    try:
        v = models.setting_get(_skey(screen, field))
    except Exception:  # noqa: BLE001 — settings unavailable: fall back to default
        v = None
    return v if v not in (None, "") else default


def set(screen: str, field: str, value: str) -> None:  # noqa: A001 — mirrors get()
    models.setting_set(_skey(screen, field), (value or "").strip())


def has_screen(screen: str) -> bool:
    return screen in SCREENS


def screen_fields(screen: str) -> list[dict]:
    return SCREENS.get(screen, {}).get("fields", [])


def list_screens() -> list[dict]:
    """Every registered editable screen, with a flag for whether it's customised."""
    out = []
    for key, meta in SCREENS.items():
        customised = any(
            get(key, f["key"], "__default__") != f.get("default", "")
            and get(key, f["key"], "__default__") != "__default__"
            for f in meta["fields"]
        )
        out.append({"key": key, "label": meta["label"],
                    "endpoint": meta.get("endpoint"),
                    "field_count": len(meta["fields"]), "customised": customised})
    return out


def get_values(screen: str) -> dict[str, str]:
    """Current effective value for each field of a screen (override or default)."""
    return {f["key"]: get(screen, f["key"], f.get("default", ""))
            for f in screen_fields(screen)}
