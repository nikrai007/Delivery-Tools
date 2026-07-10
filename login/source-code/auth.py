"""Authentication: login, register, logout, forgot-password, password reset.

Blueprint ``auth``. Session/identity wiring (LoginManager, User, user_loader)
lives in login/authentication-config/login_manager.py.
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlparse

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.utils import secure_filename

import audit
import constants
import email_utils
import models
import security
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
    """Return the correct post-login destination for an authenticated user.

    Default landing is the platform Home (all-tools hub); a `?next=` deep link,
    when present and safe, still takes precedence (see ``_safe_next``)."""
    if getattr(current_user, "is_pending_approval", False):
        return url_for("teams.pending_approval")
    return url_for("landing.index")


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
def _render_login(**kw):
    """Render the login page (local username/password authentication)."""
    return render_template("login.html", **kw)


def _complete_login(row, remember: bool, next_url: str | None = None):
    """Finalize a successful (and 2FA-cleared) authentication."""
    login_user(User(row), remember=remember)
    models.set_last_login(row["id"])
    audit.record("auth.login", category=audit.CAT_AUTH,
                 target_type="user", target_id=row["id"], target_label=row["username"],
                 actor_id=row["id"], actor_name=row["username"], actor_role=row["role"])
    # A must-change-password account is redirected by the global guard; send
    # everyone else to their validated destination.
    if next_url:
        parsed = urlparse(next_url)
        if not parsed.netloc:
            return redirect(next_url)
    return redirect(_after_login_url())


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(_after_login_url())

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        remember = bool(request.form.get("remember"))
        ip = _client_ip()

        # 1) Per-IP rate limiting.
        window = security.get_int("security.ratelimit_window")
        max_attempts = security.get_int("security.ratelimit_max")
        if max_attempts > 0 and models.count_recent_attempts(ip, window) >= max_attempts:
            audit.record("auth.rate_limited", category=audit.CAT_SECURITY,
                         target_label=username or "(none)", status=audit.STATUS_FAILURE,
                         actor_name=username or None, ip=ip,
                         details={"window_seconds": window, "max": max_attempts})
            flash("Too many login attempts. Please wait a few minutes and try again.", "error")
            return _render_login(username=username), 429

        existing = models.get_user_by_username(username)

        # 2) Account lockout.
        if existing is not None:
            remaining = models.user_lock_remaining(existing)
            if remaining > 0:
                models.record_login_attempt(username, ip, False)
                audit.record("auth.login_blocked", category=audit.CAT_SECURITY,
                             target_type="user", target_id=existing["id"], target_label=username,
                             status=audit.STATUS_FAILURE, ip=ip,
                             details={"locked_seconds_remaining": remaining})
                mins = (remaining + 59) // 60
                flash(f"Account temporarily locked. Try again in {mins} minute(s).", "error")
                return _render_login(username=username)

        # 3) Credential check.
        row = models.check_user_password(username, password)
        if row is None:
            models.record_login_attempt(username, ip, False)
            if existing is not None:
                count = models.increment_failed_login(existing["id"])
                threshold = security.get_int("security.lockout_threshold")
                if threshold > 0 and count >= threshold:
                    mins = security.get_int("security.lockout_minutes")
                    models.lock_user(existing["id"], mins)
                    audit.record("auth.account_locked", category=audit.CAT_SECURITY,
                                 target_type="user", target_id=existing["id"], target_label=username,
                                 status=audit.STATUS_FAILURE, ip=ip,
                                 details={"failed_count": count, "locked_minutes": mins})
            audit.record("auth.login_failed", category=audit.CAT_AUTH,
                         target_type="user", target_label=username,
                         status=audit.STATUS_FAILURE, actor_name=username or None, ip=ip,
                         details={"reason": "invalid credentials"})
            flash("Invalid username or password.", "error")
            return _render_login(username=username)

        # 4) Success — clear counters, record attempt.
        models.record_login_attempt(username, ip, True)
        models.reset_failed_login(row["id"])

        # 5) Second factor, if enrolled.
        if row["totp_enabled"] and row["totp_secret"]:
            session["pending_2fa_user"] = row["id"]
            session["pending_2fa_remember"] = remember
            session["pending_2fa_next"] = request.args.get("next", "")
            return redirect(url_for("auth.two_factor"))

        return _complete_login(row, remember, request.args.get("next"))

    return _render_login()


@auth_bp.route("/login/2fa", methods=["GET", "POST"])
def two_factor():
    uid = session.get("pending_2fa_user")
    if not uid:
        return redirect(url_for("auth.login"))
    row = models.get_user(uid)
    if row is None:
        session.pop("pending_2fa_user", None)
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        code = request.form.get("code", "")
        if security.verify_totp(row["totp_secret"], code):
            remember = bool(session.pop("pending_2fa_remember", False))
            next_url = session.pop("pending_2fa_next", "") or None
            session.pop("pending_2fa_user", None)
            return _complete_login(row, remember, next_url)
        audit.record("auth.2fa_failed", category=audit.CAT_SECURITY,
                     target_type="user", target_id=row["id"], target_label=row["username"],
                     status=audit.STATUS_FAILURE, actor_name=row["username"], ip=_client_ip())
        flash("Invalid authentication code. Try again.", "error")

    return render_template("two_factor.html")


@auth_bp.route("/account/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    """Forced (or voluntary) password change. The global guard sends
    must_change_password users here until they set a new password."""
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        errors = []
        if models.check_user_password(current_user.username, current) is None:
            errors.append("Your current password is incorrect.")
        errors.extend(security.validate_password(new))
        if new != confirm:
            errors.append("New passwords do not match.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("change_password.html", policy=security.describe_policy())
        models.set_password(current_user.id, new)
        models.set_must_change_password(current_user.id, False)
        audit.record("auth.password_changed", category=audit.CAT_SECURITY,
                     target_type="user", target_id=current_user.id,
                     target_label=current_user.username,
                     details={"via": "forced_change" if current_user.must_change_password else "self_service"})
        flash("Password updated.", "success")
        return redirect(_after_login_url())
    return render_template("change_password.html", policy=security.describe_policy())


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
        if not full_name:
            errors.append("Full name is required.")
        elif len(full_name) < 2:
            errors.append("Full name must be at least 2 characters.")
        if not employee_code:
            errors.append("Employee code is required.")
        if not email:
            errors.append("Email address is required.")
        elif "@" not in email or "." not in email.split("@")[-1]:
            errors.append("Please enter a valid email address.")
        errors.extend(security.validate_password(password))
        if password != confirm:
            errors.append("Passwords do not match.")
        if models.username_exists(username):
            errors.append("Username is already taken.")
        if models.get_user_by_email(email) is not None:
            errors.append("An account with that email address already exists.")
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
        audit.record("user.registered", category=audit.CAT_USER,
                     target_type="user", target_id=uid, target_label=username,
                     actor_id=uid, actor_name=username,
                     new_value={"username": username, "email": email,
                                "employee_code": employee_code,
                                "team_id": team_id, "approval_status": approval_status},
                     details={"self_registration": True})

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
                    link=url_for("teams.my_team_requests"),
                    ref_id=req_id,
                )
                email_utils.notify(
                    "join_request", leader["email"] or "",
                    leader=leader["username"], username=username,
                    employee_code=employee_code or "no code", team=team["name"],
                    link=url_for("teams.my_team_requests", _external=True),
                )
            # Notify all admins
            for admin in models.list_users():
                if admin["role"] == "admin":
                    models.create_notification(
                        admin["id"], "join_request",
                        f"{username} requested to join '{team['name']}'.",
                        link=url_for("teams.admin_requests"),
                        ref_id=req_id,
                    )
            flash("Your account was created. Waiting for team leader approval.", "info")
            return redirect(url_for("teams.pending_approval"))
        else:
            flash("Welcome — your account was created.", "success")
            return redirect(_after_login_url())

    return render_template("register.html", teams=teams, username="", email="",
                           full_name="", employee_code="", selected_team=None)


@auth_bp.route("/account/security")
@login_required
def security_settings():
    """Per-user security: 2FA status + enrolment."""
    enrolling = None
    if not current_user.totp_enabled:
        # Generate (or reuse) a pending secret held in the session until verified.
        secret = session.get("totp_enroll_secret")
        if not secret:
            secret = security.generate_totp_secret()
            session["totp_enroll_secret"] = secret
        enrolling = {
            "secret": secret,
            "uri": security.provisioning_uri(secret, current_user.username, constants.PLATFORM_NAME),
        }
    return render_template("security.html", enrolling=enrolling,
                           require_admin_2fa=security.get_bool("security.require_admin_2fa"))


@auth_bp.route("/account/2fa/enable", methods=["POST"])
@login_required
def enable_2fa():
    secret = session.get("totp_enroll_secret")
    code = request.form.get("code", "")
    if not secret:
        flash("Enrolment expired — start again.", "error")
        return redirect(url_for("auth.security_settings"))
    if not security.verify_totp(secret, code):
        flash("That code didn't match. Scan the QR and try the current code.", "error")
        return redirect(url_for("auth.security_settings"))
    models.set_totp(current_user.id, secret, True)
    session.pop("totp_enroll_secret", None)
    audit.record("auth.2fa_enabled", category=audit.CAT_SECURITY,
                 target_type="user", target_id=current_user.id, target_label=current_user.username)
    flash("Two-factor authentication is now enabled.", "success")
    return redirect(url_for("auth.security_settings"))


@auth_bp.route("/account/2fa/disable", methods=["POST"])
@login_required
def disable_2fa():
    password = request.form.get("password", "")
    if models.check_user_password(current_user.username, password) is None:
        flash("Password incorrect — 2FA not changed.", "error")
        return redirect(url_for("auth.security_settings"))
    models.set_totp(current_user.id, None, False)
    audit.record("auth.2fa_disabled", category=audit.CAT_SECURITY,
                 target_type="user", target_id=current_user.id, target_label=current_user.username)
    flash("Two-factor authentication disabled.", "info")
    return redirect(url_for("auth.security_settings"))


@auth_bp.route("/logout")
@login_required
def logout():
    auto = request.args.get("auto") == "1"
    audit.record("auth.logout", category=audit.CAT_AUTH,
                 target_type="user", target_id=current_user.id,
                 target_label=current_user.username,
                 details={"trigger": "session_timeout" if auto else "manual"})
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
            audit.record("auth.password_reset_requested", category=audit.CAT_SECURITY,
                         target_type="user", target_id=row["id"], target_label=row["username"],
                         actor_id=row["id"], actor_name=row["username"],
                         details={"ttl_minutes": ttl})
            link = f"{_base_url()}{url_for('auth.reset', token=token)}"
            email_utils.notify(
                "password_reset", row["email"] or "",
                username=row["username"], link=link, ttl=ttl,
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
    pw_errors = security.validate_password(password)
    if password != confirm:
        pw_errors.append("Passwords do not match.")
    if pw_errors:
        for e in pw_errors:
            flash(e, "error")
        return render_template("reset.html", token=token)

    if user_row is None:
        flash("That reset link is invalid or has expired.", "error")
        return redirect(url_for("auth.forgot"))

    models.set_password(user_row["id"], password)
    audit.record("auth.password_reset_completed", category=audit.CAT_SECURITY,
                 target_type="user", target_id=user_row["id"], target_label=user_row["username"],
                 actor_id=user_row["id"], actor_name=user_row["username"],
                 details={"via": "reset_token"})
    flash("Password updated — sign in with your new password.", "success")
    return redirect(url_for("auth.login"))


# ----------------------------------------------------------------------
# Profile — view & edit (self-service, any authenticated user)
# ----------------------------------------------------------------------
def _save_avatar(file_storage, user_id: int) -> str:
    """Validate + persist an uploaded avatar; return the stored filename."""
    ext = Path(secure_filename(file_storage.filename or "")).suffix.lower()
    if ext not in constants.ALLOWED_AVATAR_EXT:
        raise ValueError("Unsupported image type. Use PNG, JPG, WEBP or GIF.")
    data = file_storage.read()
    if len(data) > constants.MAX_AVATAR_BYTES:
        raise ValueError("Image exceeds the 5 MB size limit.")
    constants.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"u{user_id}_{secrets.token_hex(6)}{ext}"
    (constants.AVATAR_DIR / fname).write_bytes(data)
    return fname


def _remove_avatar_file(filename: str | None) -> None:
    if filename:
        try:
            (constants.AVATAR_DIR / filename).unlink(missing_ok=True)
        except OSError:
            pass


@auth_bp.route("/profile")
@login_required
def profile():
    team = models.get_team(current_user.team_id) if current_user.team_id else None
    return render_template("profile.html", team=team)


@auth_bp.route("/profile/edit", methods=["GET", "POST"])
@login_required
def profile_edit():
    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        email     = (request.form.get("email") or "").strip()
        password  = request.form.get("password") or ""
        confirm   = request.form.get("confirm") or ""
        remove_avatar = request.form.get("remove_avatar") == "1"
        avatar_file   = request.files.get("avatar")

        errors = []
        if not full_name or len(full_name) < 2:
            errors.append("Full name is required (min 2 characters).")
        _eparts = email.split("@")
        if not email or len(_eparts) != 2 or not _eparts[0] or not _eparts[1]:
            errors.append("A valid email address is required.")
        else:
            existing = models.get_user_by_email(email)
            if existing is not None and existing["id"] != current_user.id:
                errors.append("That email address is already in use.")
        if password:
            errors.extend(security.validate_password(password))
            if password != confirm:
                errors.append("New passwords do not match.")

        # Validate avatar early so we don't half-apply on error.
        new_avatar = None
        if avatar_file and avatar_file.filename:
            try:
                new_avatar = _save_avatar(avatar_file, current_user.id)
            except ValueError as e:
                errors.append(str(e))

        if errors:
            for e in errors:
                flash(e, "error")
            _remove_avatar_file(new_avatar)   # roll back a saved file on validation failure
            return render_template("profile_edit.html")

        models.update_user(current_user.id, email=email, full_name=full_name)
        if password:
            models.set_password(current_user.id, password)
            audit.record("auth.password_changed", category=audit.CAT_SECURITY,
                         target_type="user", target_id=current_user.id,
                         target_label=current_user.username,
                         details={"via": "profile_self_service"})
        audit.record("user.profile_updated", category=audit.CAT_USER,
                     target_type="user", target_id=current_user.id,
                     target_label=current_user.username,
                     new_value={"full_name": full_name, "email": email})
        if new_avatar:
            _remove_avatar_file(current_user.avatar_filename)   # drop the old image
            models.set_user_avatar(current_user.id, new_avatar)
        elif remove_avatar:
            _remove_avatar_file(current_user.avatar_filename)
            models.set_user_avatar(current_user.id, None)

        flash("Profile updated.", "success")
        return redirect(url_for("auth.profile"))

    return render_template("profile_edit.html")
