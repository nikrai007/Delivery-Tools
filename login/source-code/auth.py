"""Authentication: login, register, logout, forgot-password, password reset.

Blueprint ``auth``. Session/identity wiring (LoginManager, User, user_loader)
lives in login/authentication-config/login_manager.py.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

import email_utils
import models
from login_manager import User, login_manager  # noqa: F401  (re-exported for the factory)

# Auth templates live one level up at login/templates/.
auth_bp = Blueprint(
    "auth", __name__,
    template_folder=str(Path(__file__).resolve().parents[1] / "templates"),
)


USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")


def _client_ip() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr or "")


def _base_url() -> str:
    return os.getenv("BASE_URL", request.host_url.rstrip("/"))


def _after_login_url() -> str:
    """Return the correct post-login destination for an authenticated user."""
    if getattr(current_user, "is_pending_approval", False):
        return url_for("teams.pending_approval")
    return url_for("abr.dashboard")


def _safe_next() -> str:
    """Return validated ?next= URL (relative paths only) or the default destination."""
    raw = request.args.get("next", "")
    if raw:
        parsed = urlparse(raw)
        if not parsed.netloc:
            return raw
    return _after_login_url()


# ----------------------------------------------------------------------
# Login / logout / register
# ----------------------------------------------------------------------
@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(_after_login_url())

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        remember = bool(request.form.get("remember"))
        row = models.verify_user(username, password)
        if row is None:
            flash("Invalid username or password.", "error")
            return render_template("login.html", username=username)
        login_user(User(row), remember=remember)
        return redirect(_safe_next())

    return render_template("login.html")


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(_after_login_url())

    teams = models.list_teams()

    if request.method == "POST":
        username      = request.form.get("username", "").strip()
        full_name     = request.form.get("full_name", "").strip()
        employee_code = request.form.get("employee_code", "").strip()
        email         = request.form.get("email", "").strip()
        password      = request.form.get("password", "")
        confirm       = request.form.get("confirm", "")
        team_id_raw   = request.form.get("team_id", "").strip()
        team_id       = int(team_id_raw) if team_id_raw.isdigit() else None

        errors = []
        if not USERNAME_RE.match(username):
            errors.append("Username must be 3–32 chars, letters/digits/._- only.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if password != confirm:
            errors.append("Passwords do not match.")
        if models.username_exists(username):
            errors.append("Username is already taken.")
        if team_id and models.get_team(team_id) is None:
            errors.append("Selected team does not exist.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("register.html", username=username, email=email,
                                   full_name=full_name, employee_code=employee_code,
                                   teams=teams, selected_team=team_id)

        # Determine approval status: if team chosen, request is pending; otherwise approved.
        approval_status = "pending" if team_id else "approved"
        uid = models.create_user(
            username, email, password,
            full_name=full_name or None,
            employee_code=employee_code or None,
            team_id=team_id,
            approval_status=approval_status,
        )
        row = models.get_user(uid)
        login_user(User(row))

        if team_id:
            # Create join request
            req_id = models.create_join_request(uid, team_id)
            team = models.get_team(team_id)
            # Notify team leader
            leader = models.get_team_leader(team_id)
            if leader:
                models.create_notification(
                    leader["id"], "join_request",
                    f"{username} wants to join '{team['name']}'.",
                    link="/teams/my/requests",
                    ref_id=req_id,
                )
                email_utils.send(
                    to=leader["email"] or "",
                    subject=f"New join request — {team['name']}",
                    body=(
                        f"Hello {leader['username']},\n\n"
                        f"{username} ({employee_code or 'no code'}) has requested to join '{team['name']}'.\n"
                        f"Log in to review the request.\n"
                    ),
                )
            # Notify all admins
            for admin in models.list_users():
                if admin["role"] == "admin":
                    models.create_notification(
                        admin["id"], "join_request",
                        f"{username} requested to join '{team['name']}'.",
                        link="/teams/admin/requests",
                        ref_id=req_id,
                    )
            flash("Your account was created. Waiting for team leader approval.", "info")
            return redirect(url_for("teams.pending_approval"))
        else:
            flash("Welcome — your account was created.", "success")
            return redirect(url_for("abr.dashboard"))

    return render_template("register.html", teams=teams, username="", email="",
                           full_name="", employee_code="", selected_team=None)


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


# ----------------------------------------------------------------------
# Forgot password
# ----------------------------------------------------------------------
@auth_bp.route("/forgot", methods=["GET", "POST"])
def forgot():
    if current_user.is_authenticated:
        return redirect(_after_login_url())

    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        ttl = int(models.setting_get("auth.reset_token_ttl") or os.getenv("RESET_TOKEN_TTL_MINUTES", "60"))
        row = models.get_user_by_username(identifier) or models.get_user_by_email(identifier)

        # Always show the same generic message — no account-enumeration leak.
        if row is not None and row["is_active"]:
            token = models.create_password_reset(row["id"], ttl_minutes=ttl, ip=_client_ip())
            link = f"{_base_url()}{url_for('auth.reset', token=token)}"
            email_utils.send(
                to=row["email"] or "",
                subject=f"Reset your {os.getenv('APP_NAME','AutoBackupRevert')} password",
                body=(
                    f"Hello {row['username']},\n\n"
                    f"Use the link below to reset your password. It expires in {ttl} minutes.\n\n"
                    f"{link}\n\n"
                    f"If you didn't request this, you can ignore this email.\n"
                ),
            )
        flash("If that account exists, we've sent a password-reset link to its email.", "info")
        return redirect(url_for("auth.login"))

    return render_template("forgot.html")


@auth_bp.route("/reset/<token>", methods=["GET", "POST"])
def reset(token: str):
    if current_user.is_authenticated:
        return redirect(_after_login_url())

    user_row = models.consume_password_reset(token) if request.method == "POST" else None
    # On GET we don't consume; we just check the link looks plausible.
    if request.method == "GET":
        return render_template("reset.html", token=token)

    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")
    if len(password) < 8 or password != confirm:
        flash("Passwords must match and be at least 8 characters.", "error")
        return render_template("reset.html", token=token)

    if user_row is None:
        flash("That reset link is invalid or has expired.", "error")
        return redirect(url_for("auth.forgot"))

    models.set_password(user_row["id"], password)
    flash("Password updated — sign in with your new password.", "success")
    return redirect(url_for("auth.login"))
