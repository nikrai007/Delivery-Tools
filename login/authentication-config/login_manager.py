"""
Flask-Login configuration for the Delivery Toolbox platform.

Holds the shared ``LoginManager`` instance, the ``User`` session model, and
the user-loader. Kept separate from the auth *routes* (login/source-code/
auth.py) so the session/identity wiring is a clear, reusable unit.
"""

from __future__ import annotations

from flask_login import LoginManager, UserMixin

import models

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message_category = "warning"


def _row_get(row, key, default=None):
    """Safe sqlite3.Row accessor — returns default when the column is absent."""
    try:
        return row[key]
    except IndexError:
        return default


class User(UserMixin):
    def __init__(self, row):
        self.id = row["id"]
        self.username = row["username"]
        self.email = row["email"]
        self.role = row["role"]
        self.is_active_flag = bool(row["is_active"])
        self.team_id = _row_get(row, "team_id")
        self.team_role = _row_get(row, "team_role", "member")
        self.approval_status = _row_get(row, "approval_status", "approved")
        self.full_name = _row_get(row, "full_name")
        self.employee_code = _row_get(row, "employee_code")

    @property
    def is_active(self):
        return self.is_active_flag

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def is_team_leader(self):
        return self.team_role == "leader"

    @property
    def is_pending_approval(self):
        return self.approval_status == "pending"

    def get_id(self):
        return str(self.id)


@login_manager.user_loader
def load_user(user_id: str):
    row = models.get_user(int(user_id))
    if row is None:
        return None
    return User(row)
