"""
Team Management — Flask blueprint ``teams``.

Handles team CRUD (admin), join-request workflow (admin + team leader),
team leader dashboard, and pending-approval page for new registrants.
"""

from __future__ import annotations

import logging
from pathlib import Path

from urllib.parse import urlparse

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

import models
from decorators import admin_required, team_leader_required


def _safe_next(fallback: str) -> str:
    """Return `next` from the form only if it is a relative (internal) URL."""
    raw = request.form.get("next") or request.referrer or ""
    if raw:
        parsed = urlparse(raw)
        if not parsed.netloc:
            return raw
    return fallback

log = logging.getLogger("teams")

teams_bp = Blueprint(
    "teams", __name__,
    url_prefix="/teams",
    template_folder=str(Path(__file__).resolve().parents[1] / "templates"),
)


def _client_ip() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr or "")


# -----------------------------------------------------------------------
# Pending approval page (accessible to any logged-in pending user)
# -----------------------------------------------------------------------
@teams_bp.route("/pending")
@login_required
def pending_approval():
    req = models.get_latest_join_request_for_user(current_user.id)
    return render_template("pending_approval.html", join_request=req)


# -----------------------------------------------------------------------
# Team leader — own-team dashboard
# -----------------------------------------------------------------------
@teams_bp.route("/my")
@team_leader_required
def my_team():
    if current_user.is_admin and not current_user.team_id:
        # Admin with no team yet — redirect to admin teams list
        return redirect(url_for("teams.admin_teams"))
    team = models.get_team(current_user.team_id)
    if team is None:
        flash("You are not assigned to any team.", "warning")
        return redirect(url_for("abr.dashboard"))
    members = models.get_team_members(current_user.team_id)
    pending = models.list_join_requests_for_team(current_user.team_id)
    return render_template("team_dashboard.html", team=team, members=members, pending_count=len(pending))


@teams_bp.route("/my/requests")
@team_leader_required
def my_team_requests():
    if not current_user.team_id:
        flash("You are not assigned to any team.", "warning")
        return redirect(url_for("abr.dashboard"))
    team = models.get_team(current_user.team_id)
    requests_list = models.list_join_requests_for_team(current_user.team_id)
    return render_template("team_requests.html", team=team, requests=requests_list)


@teams_bp.route("/my/requests/<int:req_id>/approve", methods=["POST"])
@team_leader_required
def my_team_approve(req_id: int):
    req = models.get_join_request(req_id)
    if req is None or req["team_id"] != current_user.team_id:
        abort(403)
    models.approve_join_request(req_id, reviewed_by=current_user.id)
    models.create_notification(
        req["user_id"], "approved",
        f"Your request to join '{req['team_name']}' was approved.",
        link=url_for("abr.dashboard"),
    )
    flash(f"Approved {req['username']} into the team.", "success")
    return redirect(url_for("teams.my_team_requests"))


@teams_bp.route("/my/requests/<int:req_id>/reject", methods=["POST"])
@team_leader_required
def my_team_reject(req_id: int):
    req = models.get_join_request(req_id)
    if req is None or req["team_id"] != current_user.team_id:
        abort(403)
    models.reject_join_request(req_id, reviewed_by=current_user.id)
    models.create_notification(
        req["user_id"], "rejected",
        f"Your request to join '{req['team_name']}' was not approved.",
    )
    flash(f"Rejected request from {req['username']}.", "info")
    return redirect(url_for("teams.my_team_requests"))


# -----------------------------------------------------------------------
# Admin — team list
# -----------------------------------------------------------------------
@teams_bp.route("/admin")
@admin_required
def admin_teams():
    teams = models.list_teams()
    all_users = models.list_users()
    pending_count = sum(1 for u in all_users if _row_str(u, "approval_status") == "pending")
    return render_template("admin_teams.html", teams=teams, all_users=all_users,
                           pending_count=pending_count)


# -----------------------------------------------------------------------
# Admin — create team
# -----------------------------------------------------------------------
@teams_bp.route("/admin/new", methods=["GET", "POST"])
@admin_required
def admin_team_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip() or None
        if not name:
            flash("Team name is required.", "error")
            return render_template("admin_team_form.html", team=None)
        if models.get_team_by_name(name):
            flash("A team with that name already exists.", "error")
            return render_template("admin_team_form.html", team=None)
        models.create_team(name, description, created_by=current_user.id)
        flash(f"Team '{name}' created.", "success")
        return redirect(url_for("teams.admin_teams"))
    return render_template("admin_team_form.html", team=None)


# -----------------------------------------------------------------------
# Admin — edit team
# -----------------------------------------------------------------------
@teams_bp.route("/admin/<int:team_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_team_edit(team_id: int):
    team = models.get_team(team_id)
    if team is None:
        abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip() or None
        if not name:
            flash("Team name is required.", "error")
            return render_template("admin_team_form.html", team=team)
        existing = models.get_team_by_name(name)
        if existing and existing["id"] != team_id:
            flash("Another team already uses that name.", "error")
            return render_template("admin_team_form.html", team=team)
        models.update_team(team_id, name, description)
        flash("Team updated.", "success")
        return redirect(url_for("teams.admin_teams"))
    return render_template("admin_team_form.html", team=team)


# -----------------------------------------------------------------------
# Admin — delete team
# -----------------------------------------------------------------------
@teams_bp.route("/admin/<int:team_id>/delete", methods=["POST"])
@admin_required
def admin_team_delete(team_id: int):
    team = models.get_team(team_id)
    if team is None:
        abort(404)
    models.delete_team(team_id)
    flash(f"Team '{team['name']}' deleted.", "info")
    return redirect(url_for("teams.admin_teams"))


# -----------------------------------------------------------------------
# Admin — assign/change team leader
# -----------------------------------------------------------------------
@teams_bp.route("/admin/<int:team_id>/set-leader", methods=["POST"])
@admin_required
def admin_set_leader(team_id: int):
    team = models.get_team(team_id)
    if team is None:
        abort(404)
    user_id = request.form.get("user_id", type=int)
    if not user_id:
        flash("Select a user to assign as team leader.", "error")
        return redirect(url_for("teams.admin_teams"))
    models.assign_team_leader(user_id, team_id)
    flash("Team leader assigned.", "success")
    return redirect(url_for("teams.admin_teams"))


# -----------------------------------------------------------------------
# Admin — remove user from team
# -----------------------------------------------------------------------
@teams_bp.route("/admin/users/<int:user_id>/remove-team", methods=["POST"])
@admin_required
def admin_remove_from_team(user_id: int):
    models.remove_from_team(user_id)
    flash("User removed from team.", "info")
    return redirect(url_for("teams.admin_teams"))


# -----------------------------------------------------------------------
# Admin — join requests (all teams)
# -----------------------------------------------------------------------
@teams_bp.route("/admin/requests")
@admin_required
def admin_requests():
    requests_list = models.list_all_join_requests()
    return render_template("admin_requests.html", requests=requests_list)


@teams_bp.route("/admin/requests/<int:req_id>/approve", methods=["POST"])
@admin_required
def admin_approve(req_id: int):
    req = models.get_join_request(req_id)
    if req is None:
        abort(404)
    models.approve_join_request(req_id, reviewed_by=current_user.id)
    models.create_notification(
        req["user_id"], "approved",
        f"Your request to join '{req['team_name']}' was approved.",
        link=url_for("abr.dashboard"),
    )
    flash(f"Approved {req['username']}.", "success")
    return redirect(url_for("teams.admin_requests"))


@teams_bp.route("/admin/requests/<int:req_id>/reject", methods=["POST"])
@admin_required
def admin_reject(req_id: int):
    req = models.get_join_request(req_id)
    if req is None:
        abort(404)
    models.reject_join_request(req_id, reviewed_by=current_user.id)
    models.create_notification(
        req["user_id"], "rejected",
        f"Your request to join '{req['team_name']}' was not approved.",
    )
    flash(f"Rejected {req['username']}.", "info")
    return redirect(url_for("teams.admin_requests"))


# -----------------------------------------------------------------------
# Notifications — mark all read (AJAX or form POST)
# -----------------------------------------------------------------------
@teams_bp.route("/notifications/read", methods=["POST"])
@login_required
def mark_notifications_read():
    models.mark_notifications_read(current_user.id)
    return redirect(_safe_next(url_for("abr.dashboard")))


# -----------------------------------------------------------------------
# Helper
# -----------------------------------------------------------------------
def _row_str(row, key: str, default: str = "") -> str:
    try:
        v = row[key]
        return v if v is not None else default
    except IndexError:
        return default
