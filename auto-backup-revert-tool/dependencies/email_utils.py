"""
Platform email sender + configurable notification templates.

Configuration precedence for every SMTP setting (host, port, user, …):

    1. Admin UI  — values saved in the `settings` table (keys `smtp.*`)
    2. Environment — SMTP_HOST / SMTP_PORT / … (legacy / bootstrap)
    3. Built-in default

So an operator can configure the mailer entirely from **Admin → Email &
notifications** with no server access, while existing env-based deploys keep
working untouched. If nothing is configured (or a send fails), the message is
printed to stderr with a clear marker so it can be delivered manually — the
platform still works on day one without SMTP.

Notification templates (subject + body) are also admin-overridable per event
(keys `email.tpl.<event>.subject` / `.body`); `notify()` renders the template
with a `$placeholder` context and sends it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import smtplib
from email.message import EmailMessage
from string import Template

import models

log = logging.getLogger("autobackuprevert.email")


# ----------------------------------------------------------------------
# Configuration (DB setting > env var > default)
# ----------------------------------------------------------------------
# Maps a logical field -> (settings key, env var, default).
_FIELDS = {
    "host":     ("smtp.host",     "SMTP_HOST",     ""),
    "port":     ("smtp.port",     "SMTP_PORT",     "587"),
    "user":     ("smtp.user",     "SMTP_USER",     ""),
    "password": ("smtp.password", "SMTP_PASSWORD", ""),
    "from":     ("smtp.from",     "SMTP_FROM",     ""),
    "starttls": ("smtp.starttls", "SMTP_STARTTLS", "1"),
}


def _get(field: str) -> str:
    key, env, default = _FIELDS[field]
    try:
        v = models.setting_get(key)
    except Exception:  # noqa: BLE001 — settings unavailable (e.g. pre-init): fall back
        v = None
    if v is None or v == "":
        v = os.getenv(env)
    return v if v not in (None, "") else default


def get_config(*, redact_password: bool = True) -> dict:
    """Effective mailer config for display in the admin form. The password is
    never echoed back — the form shows whether one is set, not its value."""
    cfg = {f: _get(f) for f in _FIELDS}
    cfg["port"] = int(cfg["port"] or "587") if str(cfg["port"]).isdigit() else 587
    cfg["starttls"] = str(cfg["starttls"]) not in ("0", "false", "False", "no", "")
    cfg["password_set"] = bool(_get("password"))
    if redact_password:
        cfg.pop("password", None)
    return cfg


def enabled() -> bool:
    """Master switch: sending is on when explicitly enabled AND a host exists.
    Defaults to enabled when a host is configured (backward compatible)."""
    flag = None
    try:
        flag = models.setting_get("smtp.enabled")
    except Exception:  # noqa: BLE001
        flag = None
    host_present = bool(_get("host"))
    if flag is None:
        return host_present
    return flag == "1" and host_present


def smtp_configured() -> bool:
    return bool(_get("host"))


# ----------------------------------------------------------------------
# Low-level send
# ----------------------------------------------------------------------
def send(to: str, subject: str, body: str) -> bool:
    """Send a plain-text email. Returns True on a real send, False on the
    console fallback (no recipient / not configured / SMTP error)."""
    if not to:
        _print_fallback(to, subject, body, reason="no recipient")
        return False
    if not enabled():
        _print_fallback(to, subject, body, reason="SMTP not enabled/configured")
        return False

    msg = EmailMessage()
    msg["From"] = _get("from") or _get("user") or "no-reply@local"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    host = _get("host")
    port = int(_get("port") or "587")
    user = _get("user")
    password = _get("password")
    use_tls = str(_get("starttls")) not in ("0", "false", "False", "no", "")

    try:
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.ehlo()
            if use_tls:
                s.starttls()
                s.ehlo()
            if user and password:
                s.login(user, password)
            s.send_message(msg)
        log.info("[email] sent to %s subject=%r", to, subject)
        return True
    except Exception as exc:  # noqa: BLE001
        log.exception("[email] FAILED via SMTP — falling back to console")
        _print_fallback(to, subject, body, reason=f"SMTP error: {exc}")
        return False


def send_test(to: str) -> tuple[bool, str]:
    """Send a diagnostic email; returns (ok, human message) for the admin UI."""
    if not smtp_configured():
        return False, "No SMTP host configured yet."
    ok = send(
        to,
        "Test email — Delivery Toolbox",
        "This is a test email from the Delivery Toolbox mailer.\n\n"
        "If you received it, SMTP is configured correctly.",
    )
    if ok:
        return True, f"Test email sent to {to}."
    return False, ("Send failed or fell back to console — check the app log and "
                   "your SMTP settings.")


# ----------------------------------------------------------------------
# Notification templates (admin-overridable per event)
# ----------------------------------------------------------------------
# Each template uses $placeholder substitution (string.Template) so admins can
# freely include literal text without brace-escaping. Unknown placeholders are
# left intact (safe_substitute).
DEFAULT_TEMPLATES: dict[str, dict[str, str]] = {
    "password_reset": {
        "subject": "Reset your $app_name password",
        "body": ("Hello $username,\n\n"
                 "Use the link below to reset your password. It expires in "
                 "$ttl minutes.\n\n$link\n\n"
                 "If you didn't request this, you can ignore this email."),
    },
    "account_created": {
        "subject": "Your $app_name account is ready",
        "body": ("Hello $full_name,\n\n"
                 "An account has been created for you on $platform_name.\n"
                 "Username: $username\n\n"
                 "$note\n\nSign in: $login_url"),
    },
    "join_request": {
        "subject": "New join request — $team",
        "body": ("Hello $leader,\n\n"
                 "$username ($employee_code) has requested to join '$team'.\n"
                 "Log in to review the request: $link"),
    },
    "join_approved": {
        "subject": "You've been approved for $team",
        "body": ("Hello $username,\n\n"
                 "Your request to join '$team' on $platform_name was approved.\n\n"
                 "Open the platform: $login_url"),
    },
    "join_rejected": {
        "subject": "Update on your $team request",
        "body": ("Hello $username,\n\n"
                 "Your request to join '$team' was not approved.\n"
                 "Contact your administrator if you believe this is a mistake."),
    },
    "role_changed": {
        "subject": "Your $app_name role has changed",
        "body": ("Hello $username,\n\n"
                 "Your role on $platform_name changed from '$old_role' to "
                 "'$new_role'."),
    },
    "password_reset_by_admin": {
        "subject": "Your $app_name password was reset",
        "body": ("Hello $username,\n\n"
                 "An administrator has reset your password. You'll receive the "
                 "new password through a secure channel. If this was unexpected, "
                 "contact your administrator immediately."),
    },
    "tool_assigned": {
        "subject": "Tools assigned on $platform_name",
        "body": ("Hello $username,\n\n"
                 "You now have access to the following tool(s): $tools\n\n"
                 "Open the platform: $login_url"),
    },
    "admin_event": {
        "subject": "$app_name — $title",
        "body": "$message",
    },
}

# Human labels for the admin template editor.
TEMPLATE_LABELS = {
    "password_reset":          "Password reset (link)",
    "account_created":         "Account created",
    "join_request":            "Join request (to leader/admin)",
    "join_approved":           "Join request approved",
    "join_rejected":           "Join request rejected",
    "role_changed":            "Role changed",
    "password_reset_by_admin": "Password reset by admin",
    "tool_assigned":           "Tool assigned",
    "admin_event":             "Generic admin event",
}


def get_template(event: str) -> dict[str, str]:
    """Return {'subject','body'} for an event — admin override or built-in default.
    Works for both built-in events and admin-created custom templates (whose
    subject/body live in the same ``email.tpl.<key>.*`` settings)."""
    d = DEFAULT_TEMPLATES.get(event, {"subject": "$app_name notification", "body": "$message"})
    subject = default_subject = d["subject"]
    body = default_body = d["body"]
    try:
        subject = models.setting_get(f"email.tpl.{event}.subject") or default_subject
        body = models.setting_get(f"email.tpl.{event}.body") or default_body
    except Exception:  # noqa: BLE001
        pass
    return {"subject": subject, "body": body}


# ----------------------------------------------------------------------
# Custom templates (admin-managed, in addition to the built-in events)
# ----------------------------------------------------------------------
# The index of custom templates lives in one settings row as JSON; each
# template's subject/body reuse the same ``email.tpl.<key>.*`` keys as the
# built-ins, so the existing render()/notify() pipeline handles them unchanged.
CUSTOM_INDEX_KEY = "email.custom_templates"
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


def list_custom_templates() -> list[dict]:
    """Return ``[{'key','label'}, …]`` for admin-created templates."""
    try:
        raw = models.setting_get(CUSTOM_INDEX_KEY)
        data = json.loads(raw) if raw else []
        return [t for t in data if isinstance(t, dict) and t.get("key")]
    except Exception:  # noqa: BLE001
        return []


def is_custom_key(key: str) -> bool:
    return any(t["key"] == key for t in list_custom_templates())


def _save_index(items: list[dict]) -> None:
    models.setting_set(CUSTOM_INDEX_KEY, json.dumps(items))


def add_custom_template(key: str, label: str, subject: str, body: str) -> tuple[bool, str]:
    key = (key or "").strip().lower()
    label = (label or "").strip() or key
    subject = (subject or "").strip()
    body = (body or "").strip()
    if not _KEY_RE.match(key):
        return False, "Key must be 2–40 chars: start with a letter; lowercase letters, digits, underscore."
    if key in DEFAULT_TEMPLATES or key in TEMPLATE_LABELS:
        return False, "That key is reserved by a built-in template."
    if is_custom_key(key):
        return False, "A custom template with that key already exists."
    if not subject or not body:
        return False, "Subject and body are required."
    items = list_custom_templates()
    items.append({"key": key, "label": label})
    _save_index(items)
    models.setting_set(f"email.tpl.{key}.subject", subject)
    models.setting_set(f"email.tpl.{key}.body", body)
    return True, f"Template '{label}' added."


def update_custom_template(key: str, label: str, subject: str, body: str) -> tuple[bool, str]:
    items = list_custom_templates()
    if not any(t["key"] == key for t in items):
        return False, "Template not found."
    subject = (subject or "").strip()
    body = (body or "").strip()
    if not subject or not body:
        return False, "Subject and body are required."
    for t in items:
        if t["key"] == key and (label or "").strip():
            t["label"] = label.strip()
    _save_index(items)
    models.setting_set(f"email.tpl.{key}.subject", subject)
    models.setting_set(f"email.tpl.{key}.body", body)
    return True, "Template updated."


def delete_custom_template(key: str) -> tuple[bool, str]:
    items = list_custom_templates()
    if not any(t["key"] == key for t in items):
        return False, "Template not found."
    _save_index([t for t in items if t["key"] != key])
    models.setting_set(f"email.tpl.{key}.subject", "")
    models.setting_set(f"email.tpl.{key}.body", "")
    return True, "Template deleted."


def sample_context() -> dict[str, str]:
    """Representative placeholder values for the live preview."""
    return {
        "username": "jdoe", "full_name": "Jane Doe", "email": "jane.doe@example.com",
        "team": "Migrations", "leader": "Team Lead", "employee_code": "EC1234",
        "old_role": "user", "new_role": "admin", "ttl": "60",
        "link": "https://app.example.com/reset/abc123",
        "note": "Please sign in and change your password.",
        "tools": "XPM Automator, AutoBackupRevert",
        "title": "Heads up", "message": "This is a sample message body.",
    }


def preview(event: str, context: dict | None = None) -> tuple[str, str]:
    """Render (subject, body) for an event using sample placeholder values."""
    return render(event, {**sample_context(), **(context or {})})


def render_strings(subject: str, body: str, context: dict | None = None) -> tuple[str, str]:
    """Render arbitrary subject/body strings (e.g. the admin's unsaved editor
    content) with branding + sample placeholders — powers the live preview."""
    ctx = {**_base_context(), **sample_context(), **(context or {})}
    return (Template(subject or "").safe_substitute(ctx),
            Template(body or "").safe_substitute(ctx))


def _base_context() -> dict[str, str]:
    """Branding placeholders available to every template."""
    try:
        import constants
        platform = constants.PLATFORM_NAME
        app = constants.APP_NAME
    except Exception:  # noqa: BLE001
        platform, app = "Delivery Toolbox", "AutoBackupRevert"
    ctx = {"platform_name": platform, "app_name": app, "login_url": ""}
    try:
        from flask import url_for
        ctx["login_url"] = url_for("auth.login", _external=True)
    except Exception:  # noqa: BLE001
        pass
    return ctx


def render(event: str, context: dict) -> tuple[str, str]:
    """Render (subject, body) for an event with $placeholder substitution."""
    tpl = get_template(event)
    ctx = {**_base_context(), **{k: ("" if v is None else str(v)) for k, v in context.items()}}
    subject = Template(tpl["subject"]).safe_substitute(ctx)
    body = Template(tpl["body"]).safe_substitute(ctx)
    return subject, body


def notify(event: str, to: str, **context) -> bool:
    """Render a configurable template for `event` and send it to `to`."""
    subject, body = render(event, context)
    return send(to, subject, body)


def notify_admins(event: str, **context) -> None:
    """Send an event notification to every active admin (best effort)."""
    try:
        for u in models.list_users():
            if u["role"] == "admin" and u["is_active"] and u["email"]:
                notify(event, u["email"], **context)
    except Exception:  # noqa: BLE001
        log.exception("[email] notify_admins failed for event=%s", event)


def _print_fallback(to: str, subject: str, body: str, reason: str) -> None:
    bar = "=" * 78
    print(
        f"\n{bar}\n"
        f"[EMAIL FALLBACK] {reason}\n"
        f"To:      {to}\n"
        f"Subject: {subject}\n"
        f"--\n{body}\n{bar}\n",
        flush=True,
    )
