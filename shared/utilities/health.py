"""
Live system health checks for the status dashboard.

Extensible probe framework: `run_all_checks()` returns a flat list of check
dicts consumed by the status page and its JSON polling endpoint. Each probe is
isolated — one failing check never breaks the others or the page.

Check dict shape:
    {
      "key":        stable id,
      "group":      "Platform" | "Tools" | ...,
      "name":       human label,
      "type":       "database" | "scheduler" | "internal" | "external_url" | ...,
      "status":     "online" | "offline" | "degraded" | "unknown",
      "response_ms": int | None,
      "detail":     short human status,
      "error":      error string | None (admin-only in the UI),
      "checked_at": ISO-8601 UTC,
    }

Adding a new probe type = add a branch in `_check_tool` (or a new top-level
probe in `run_all_checks`). Nothing here is hardcoded to a specific tool.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import models

# External HTTP probes use a short timeout so the dashboard stays responsive.
_HTTP_TIMEOUT = 4.0


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


def refresh_seconds() -> int:
    """Admin-configurable auto-refresh interval (seconds), default 30."""
    raw = models.setting_get("status.refresh_seconds")
    try:
        return max(5, min(int(raw), 3600)) if raw else 30
    except (TypeError, ValueError):
        return 30


def _result(key, group, name, type_, status, *, response_ms=None, detail="", error=None):
    return {
        "key": key, "group": group, "name": name, "type": type_,
        "status": status, "response_ms": response_ms, "detail": detail,
        "error": error, "checked_at": _now(),
    }


# ----------------------------------------------------------------------
# Core infrastructure probes
# ----------------------------------------------------------------------
def _check_database():
    start = time.perf_counter()
    try:
        with models.connect() as con:
            con.execute("SELECT 1").fetchone()
        ms = int((time.perf_counter() - start) * 1000)
        return _result("db", "Platform", "Database", "database", "online",
                       response_ms=ms, detail="Query OK")
    except Exception as exc:  # noqa: BLE001
        return _result("db", "Platform", "Database", "database", "offline",
                       detail="Connection failed", error=str(exc))


def _check_scheduler():
    try:
        import scheduler as scheduler_mod
        sched = getattr(scheduler_mod, "_scheduler", None)
        if sched is None:
            return _result("scheduler", "Platform", "Background scheduler", "scheduler",
                           "unknown", detail="Not initialized")
        running = bool(getattr(sched, "running", False))
        jobs = len(sched.get_jobs()) if running else 0
        return _result("scheduler", "Platform", "Background scheduler", "scheduler",
                       "online" if running else "offline",
                       detail=f"{jobs} scheduled job(s)" if running else "Stopped")
    except Exception as exc:  # noqa: BLE001
        return _result("scheduler", "Platform", "Background scheduler", "scheduler",
                       "unknown", detail="Probe error", error=str(exc))


# ----------------------------------------------------------------------
# Per-tool probes (dispatched by launch_type)
# ----------------------------------------------------------------------
def _probe_http(url: str):
    start = time.perf_counter()
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "DeliveryToolbox-HealthCheck"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            ms = int((time.perf_counter() - start) * 1000)
            code = resp.getcode()
        if code and code < 400:
            return "online", ms, f"HTTP {code}", None
        return "degraded", ms, f"HTTP {code}", f"HTTP status {code}"
    except urllib.error.HTTPError as exc:  # got a response, just an error code
        ms = int((time.perf_counter() - start) * 1000)
        status = "degraded" if exc.code < 500 else "offline"
        return status, ms, f"HTTP {exc.code}", f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 — DNS, timeout, refused, …
        return "offline", None, "Unreachable", str(exc)


def _check_tool(tool):
    try:
        cfg = json.loads(tool["launch_config"] or "{}")
    except (TypeError, ValueError):
        cfg = {}
    lt = tool["launch_type"]
    key = f"tool:{tool['slug']}"
    name = tool["name"]

    if lt == "internal":
        endpoint = cfg.get("endpoint")
        try:
            from flask import current_app
            ok = any(r.endpoint == endpoint for r in current_app.url_map.iter_rules())
        except Exception:  # noqa: BLE001
            ok = False
        return _result(key, "Tools", name, "internal",
                       "online" if ok else "offline",
                       detail=(endpoint or "—") if ok else "Endpoint missing",
                       error=None if ok else f"Endpoint '{endpoint}' not registered")

    if lt == "external_url":
        url = cfg.get("url")
        if not url:
            return _result(key, "Tools", name, "external_url", "unknown", detail="No URL set")
        status, ms, detail, err = _probe_http(url)
        return _result(key, "Tools", name, "external_url", status,
                       response_ms=ms, detail=detail, error=err)

    if lt == "folder_path":
        path = cfg.get("path") or ""
        exists = bool(path) and Path(path).exists()
        return _result(key, "Tools", name, "folder_path",
                       "online" if exists else "offline",
                       detail=path or "No path set",
                       error=None if exists else "Path not found")

    if lt == "executable":
        cmd = (cfg.get("cmd") or "").strip()
        # Resolve the executable token (strip any trailing args).
        exe = cmd.split()[0] if cmd else ""
        exists = bool(exe) and Path(exe).exists()
        return _result(key, "Tools", name, "executable",
                       "online" if exists else "offline",
                       detail=exe or "No command set",
                       error=None if exists else "Executable not found")

    return _result(key, "Tools", name, lt or "unknown", "unknown", detail="Unknown launch type")


def run_all_checks(*, include_tools: bool = True) -> list[dict]:
    """Run every probe and return the flat result list."""
    checks = [_check_database(), _check_scheduler()]
    if include_tools:
        try:
            for tool in models.list_portal_tools():
                if tool["is_enabled"] and tool["status"] == "live":
                    checks.append(_check_tool(tool))
        except Exception:  # noqa: BLE001
            pass
    return checks


def summarize(checks: list[dict]) -> dict:
    """Aggregate counts + an overall banner state for the header."""
    counts = {"online": 0, "offline": 0, "degraded": 0, "unknown": 0}
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    if counts["offline"]:
        overall = "offline"
    elif counts["degraded"]:
        overall = "degraded"
    elif counts["online"]:
        overall = "online"
    else:
        overall = "unknown"
    return {"overall": overall, "counts": counts, "total": len(checks)}
