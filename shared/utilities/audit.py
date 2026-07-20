"""
Enterprise audit-logging helper.

A thin, dependency-light wrapper over ``models.record_audit`` that automatically
captures the acting user and client IP from the Flask request context, so
recording a critical event is a single call at the seam where it happens:

    import audit

    audit.record("user.role_changed", category=audit.CAT_USER,
                 target_type="user", target_id=uid, target_label=username,
                 old_value={"role": old_role}, new_value={"role": new_role})

Design guarantees:
  * **Never raises into the caller.** A failure to persist an audit row is
    swallowed and logged — auditing must never break the operation it records.
  * **Context-safe.** Works inside a request (auto actor + IP) and outside one
    (e.g. the background scheduler), where actor/IP simply resolve to None.
  * **Backward compatible.** Pure addition; nothing else imports this yet.

Categories are exposed as CAT_* constants for consistency across blueprints.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import models

log = logging.getLogger("audit")

# Canonical event categories (kept as constants to avoid typo drift).
CAT_AUTH     = "auth"
CAT_USER     = "user"
CAT_TEAM     = "team"
CAT_TOOL     = "tool"
CAT_APPROVAL = "approval"
CAT_CONFIG   = "config"
CAT_SECURITY = "security"
CAT_GENERAL  = "general"

STATUS_SUCCESS = "success"
STATUS_FAILURE = "failure"


def _to_text(value: Any) -> str | None:
    """Serialize a value for storage: str passes through, everything else -> JSON."""
    if value is None or isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _actor() -> tuple[int | None, str | None, str | None]:
    """(user_id, username, role) from the request context, or (None, None, None)."""
    try:
        from flask import has_request_context
        from flask_login import current_user
        if has_request_context() and getattr(current_user, "is_authenticated", False):
            return (
                getattr(current_user, "id", None),
                getattr(current_user, "username", None),
                getattr(current_user, "role", None),
            )
    except Exception:  # noqa: BLE001 — never let context lookup break auditing
        pass
    return None, None, None


def client_ip() -> str | None:
    """Best-effort client IP from the current request (honours X-Forwarded-For)."""
    try:
        from flask import has_request_context, request
        if has_request_context():
            return request.headers.get("X-Forwarded-For", request.remote_addr or "") or None
    except Exception:  # noqa: BLE001
        pass
    return None


def record(action: str, *, category: str = CAT_GENERAL,
           target_type: str | None = None, target_id: int | None = None,
           target_label: str | None = None,
           old_value: Any = None, new_value: Any = None, details: Any = None,
           status: str = STATUS_SUCCESS,
           actor_id: int | None = None, actor_name: str | None = None,
           actor_role: str | None = None, ip: str | None = None) -> None:
    """Record an audit event. Actor and IP are auto-captured from the request
    unless explicitly overridden (e.g. a failed login has no authenticated
    actor, so pass ``actor_name`` = the attempted username)."""
    uid, uname, urole = _actor()
    if actor_id is not None:
        uid = actor_id
    if actor_name is not None:
        uname = actor_name
    if actor_role is not None:
        urole = actor_role

    try:
        models.record_audit(
            action=action, category=category,
            user_id=uid, username=uname, actor_role=urole,
            ip_address=ip if ip is not None else client_ip(),
            target_type=target_type, target_id=target_id, target_label=target_label,
            old_value=_to_text(old_value), new_value=_to_text(new_value),
            details=_to_text(details), status=status,
        )
    except Exception:  # noqa: BLE001 — auditing must never break the caller
        log.exception("[audit] failed to record action=%s target=%s/%s",
                      action, target_type, target_id)
