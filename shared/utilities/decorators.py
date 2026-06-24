"""Cross-tool view decorators shared across the Delivery Toolbox platform."""

from __future__ import annotations

from functools import wraps

from flask import abort, redirect, request, url_for
from flask_login import current_user


def admin_required(view):
    """Allow only authenticated admins; bounce anonymous users to login."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login", next=request.url))
        if not getattr(current_user, "is_admin", False):
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def team_leader_required(view):
    """Allow only authenticated team leaders (or admins); bounce others."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login", next=request.url))
        if not (getattr(current_user, "is_team_leader", False)
                or getattr(current_user, "is_admin", False)):
            abort(403)
        return view(*args, **kwargs)
    return wrapper
