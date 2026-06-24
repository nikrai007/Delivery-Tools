"""
Delivery Toolbox — central configuration constants.

Single source of truth for every tool. All paths are anchored at the
Delivery-Tools/ project root (two levels up from this file:
shared/constants/constants.py -> shared/ -> Delivery-Tools/).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# shared/constants/constants.py  ->  parents[2] == Delivery-Tools/
ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

# --- Branding -----------------------------------------------------------
PLATFORM_NAME = os.getenv("PLATFORM_NAME", "Delivery Toolbox")
APP_NAME      = os.getenv("APP_NAME", "AutoBackupRevert")
APP_OWNER     = os.getenv("APP_OWNER", "Nikhil Kumar (EC2845)")
APP_COMPANY   = os.getenv("APP_COMPANY", "Personal Dev Corporation ltd")
VERSION       = os.getenv("APP_VERSION", "2.0.0")

# --- Filesystem layout --------------------------------------------------
UPLOAD_ROOT = (ROOT / os.getenv("UPLOAD_ROOT", "uploads")).resolve()
DATA_DIR    = (ROOT / os.getenv("DATA_DIR", "database/data")).resolve()
DB_PATH     = DATA_DIR / "app.db"

# --- Flask / runtime ----------------------------------------------------
SECRET_KEY          = os.getenv("FLASK_SECRET_KEY", "dev-insecure-change-me")
MAX_UPLOAD_MB       = int(os.getenv("MAX_UPLOAD_MB", "500"))
CLEANUP_MINUTES     = int(os.getenv("CLEANUP_MINUTES", "60"))
JOB_RETENTION_DAYS  = int(os.getenv("JOB_RETENTION_DAYS", "30"))
RESET_TOKEN_TTL_MIN = int(os.getenv("RESET_TOKEN_TTL_MINUTES", "60"))

# --- Admin bootstrap ----------------------------------------------------
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_EMAIL    = os.getenv("ADMIN_EMAIL", "admin@local")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")

# --- AutoBackupRevert tool ---------------------------------------------
ALLOWED_UPLOAD_EXT = {".7z", ".zip", ".sql"}

# --- Brand assets -------------------------------------------------------
BRAND_DIR = ROOT / "static" / "brand"
