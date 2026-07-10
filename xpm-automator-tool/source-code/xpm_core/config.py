"""
XPMConfig — the connection/project configuration value object for one XPM run.

Kept deliberately free of Flask and the DB. The web layer builds an
``XPMConfig`` from the submitted form (falling back to admin-saved defaults in
the platform ``settings`` table), validates it, and hands it to the pipeline.

Security: the password lives only on the in-memory config passed to the worker
thread — it is never written to the database or the audit log.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

# Defaults mirror the proven standalone tool so an operator can run immediately
# on the Noida network. All are overridable per-run and via admin settings.
# `password` is pre-fillable for convenience (parity with the desktop tool) but
# is NEVER persisted — see `redacted()` and the empty SETTING_KEYS entry.
DEFAULTS = {
    "base_url": "http://192.168.0.28/xpm",
    "username": "nikhil.kumar@businessnext.com",
    "password": "acid_qa",
    "project_id": "105",
    "project_name": "SBC-10x Enhancements",
    "process_name": "SBC-10x Enhancements_Hotfix",
    "delay": "2",
}

# Platform settings keys (namespaced) used to persist non-secret defaults so an
# admin sets them once. Password is intentionally absent — never persisted.
SETTING_KEYS = {
    "base_url": "xpm.base_url",
    "username": "xpm.username",
    "project_id": "xpm.project_id",
    "project_name": "xpm.project_name",
    "process_name": "xpm.process_name",
    "delay": "xpm.delay",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# XPM WebForms endpoints are all relative to base_url.
_PATHS = {
    "login": "/login.aspx",
    "change_project": "/globals/changeproject.aspx",
    "new_script": "/buildmanagement/newmigrationscript.aspx?sId=-1",
    "script_list": "/buildmanagement/migrationscriptlist.aspx?opt=2&Id=0",
    "download": "/buildmanagement/getmigrationscript.aspx",
}


@dataclass
class XPMConfig:
    base_url: str = DEFAULTS["base_url"]
    username: str = DEFAULTS["username"]
    password: str = ""                       # in-memory only, never persisted
    project_id: str = DEFAULTS["project_id"]
    project_name: str = DEFAULTS["project_name"]
    process_name: str = DEFAULTS["process_name"]
    delay: float = float(DEFAULTS["delay"])
    timeout: int = 30                        # per-request seconds (short ops)
    upload_timeout: int = 120                # per-request seconds (uploads)
    download_timeout: int = 300              # per-request seconds (downloads)
    max_retries: int = 2                     # extra attempts on transient errors
    verify_tls: bool = True

    # -- derived URLs --------------------------------------------------------
    @property
    def base(self) -> str:
        return self.base_url.rstrip("/")

    def url(self, key: str) -> str:
        return f"{self.base}{_PATHS[key]}"

    @property
    def origin(self) -> str:
        p = urlparse(self.base_url)
        return f"{p.scheme}://{p.netloc}"

    # -- validation ----------------------------------------------------------
    def validate(self, *, require_password: bool = True) -> list[str]:
        """Return a list of human-readable problems (empty == valid)."""
        errs: list[str] = []
        p = urlparse(self.base_url)
        if not (p.scheme in ("http", "https") and p.netloc):
            errs.append("XPM URL must be a valid http(s) URL, e.g. http://192.168.0.28/xpm")
        if not (self.username or "").strip():
            errs.append("Username is required.")
        if require_password and not (self.password or "").strip():
            errs.append("Password is required (it is used for this run only and never stored).")
        if not str(self.project_id or "").strip():
            errs.append("Project ID is required.")
        try:
            if float(self.delay) < 0:
                errs.append("Delay between uploads cannot be negative.")
        except (TypeError, ValueError):
            errs.append("Delay must be a number of seconds.")
        return errs

    def redacted(self) -> dict:
        """A dict snapshot safe to persist / audit (no password)."""
        return {
            "base_url": self.base_url,
            "username": self.username,
            "project_id": str(self.project_id),
            "project_name": self.project_name,
            "process_name": self.process_name,
            "delay": self.delay,
        }

    # -- construction --------------------------------------------------------
    @classmethod
    def from_form(cls, form, *, defaults: dict | None = None) -> "XPMConfig":
        """Build from a Flask ``request.form`` (or any ``.get`` mapping),
        backfilling blanks from admin-saved defaults then package defaults."""
        d = {**DEFAULTS, **(defaults or {})}

        def pick(key: str) -> str:
            v = (form.get(key) or "").strip()
            return v if v else str(d.get(key, ""))

        try:
            delay = float(pick("delay") or 0)
        except (TypeError, ValueError):
            delay = float(DEFAULTS["delay"])

        return cls(
            base_url=pick("base_url"),
            username=pick("username"),
            password=(form.get("password") or ""),   # not stripped: preserve exact secret
            project_id=pick("project_id"),
            project_name=pick("project_name"),
            process_name=pick("process_name"),
            delay=delay,
        )
