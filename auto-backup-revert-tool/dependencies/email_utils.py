"""
Lightweight email sender for password-reset (and any future notifications).

Reads SMTP_HOST/PORT/USER/PASSWORD/FROM/STARTTLS from the environment.
If SMTP isn't configured or sending fails, the message is logged to stderr
with a clear marker so an admin can hand it over manually — the feature
still works on day one without SMTP setup.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger("autobackuprevert.email")


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST"))


def send(to: str, subject: str, body: str) -> bool:
    """Send an email. Returns True on real send, False on console fallback."""
    if not to:
        log.info("[email] skipped — no recipient address")
        _print_fallback(to, subject, body, reason="no recipient")
        return False

    if not smtp_configured():
        _print_fallback(to, subject, body, reason="SMTP_HOST not configured")
        return False

    msg = EmailMessage()
    msg["From"] = os.getenv("SMTP_FROM", os.getenv("SMTP_USER", "no-reply@local"))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    use_tls = os.getenv("SMTP_STARTTLS", "1") not in ("0", "false", "False", "no")

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
