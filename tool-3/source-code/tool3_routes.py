"""
Tool-3 — scaffold for the next Delivery Toolbox tool.

This is a working placeholder blueprint that shows the wiring a new tool
needs. To turn it into a real tool:

  1. Add the routes/logic here (or split into more modules in this folder).
  2. Give it its own templates/ folder and point ``template_folder`` at it.
  3. Register it in Delivery-Tools/app.py (already done for this scaffold).
  4. Add a card for it to ``LANDING_TOOLS`` in
     landing-page/source-code/landing_routes.py (set status="live" and
     endpoint="tool3.home").
"""

from __future__ import annotations

from flask import Blueprint, redirect, url_for
from flask_login import login_required

tool3_bp = Blueprint("tool3", __name__, url_prefix="/tools/tool-3")


@tool3_bp.route("/")
@login_required
def home():
    # Placeholder — not yet built. Bounce back to the hub so the route is live
    # but harmless until the tool is implemented.
    return redirect(url_for("landing.index"))
