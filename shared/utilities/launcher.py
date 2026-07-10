"""
Tool execution framework — managed child processes for portal tools.

Extensible launcher: a `ProcessManager` supervises long-running child processes
for tools whose `launch_type` is `folder_path` (a Python web app) or
`executable` (a local program). `internal` and `external_url` tools need no
process and are handled by the normal request/redirect path.

Design:
  * **Process management** — start/stop/restart, PID + start-time tracking,
    graceful shutdown (SIGTERM/terminate → wait → kill), orphan cleanup at exit.
  * **Secure execution** — commands are spawned from admin-configured tool rows
    only, as an argument *list* (never `shell=True`), so there is no shell
    injection surface. Paths are validated before launch.
  * **Health monitoring** — process liveness plus, for web apps with a port, an
    HTTP probe of the local port.
  * **Logging** — each tool's stdout/stderr streams to logs/tools/<slug>.log.
  * **Error reporting** — a crashed process's exit code + last log lines are
    surfaced to admins.

Adding a new execution type = add a branch in `_build_spec`.

NOTE: this runs processes on the app host. Only administrators can register or
start tools, so the command source is trusted; nonetheless the manager never
uses a shell and validates the target before spawning.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import shlex
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("launcher")

# Where per-tool logs are written (set by init()).
_LOG_DIR: Path | None = None
_lock = threading.Lock()
# slug -> running process record
_procs: dict[str, dict] = {}


def init(log_dir: Path) -> None:
    global _LOG_DIR
    _LOG_DIR = Path(log_dir)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


class LaunchError(Exception):
    """Raised when a tool cannot be started (bad config / missing target)."""


def _build_spec(slug: str, launch_type: str, cfg: dict) -> dict:
    """Turn a tool's launch_config into a concrete spawn spec.
    Returns {argv, cwd, env, url}. Raises LaunchError on invalid config."""
    env = dict(os.environ)

    if launch_type == "folder_path":
        path = (cfg.get("path") or "").strip()
        if not path or not Path(path).is_dir():
            raise LaunchError(f"Folder path does not exist: {path or '(empty)'}")
        entry = (cfg.get("entry") or "app.py").strip()
        entry_path = Path(path) / entry
        if not entry_path.is_file():
            raise LaunchError(f"Entry file not found: {entry}")
        port = cfg.get("port")
        url = None
        if port:
            env["PORT"] = str(port)
            url = f"http://127.0.0.1:{port}"
        # Run with the same interpreter that runs the portal.
        return {"argv": [sys.executable, entry], "cwd": path, "env": env, "url": url}

    if launch_type == "executable":
        cmd = (cfg.get("cmd") or "").strip()
        if not cmd:
            raise LaunchError("No command configured.")
        # Parse to an argv list — never run through a shell.
        argv = shlex.split(cmd, posix=(os.name != "nt"))
        exe = argv[0] if argv else ""
        if not exe or not Path(exe).exists():
            raise LaunchError(f"Executable not found: {exe or '(empty)'}")
        port = cfg.get("port")
        url = f"http://127.0.0.1:{port}" if port else None
        if port:
            env["PORT"] = str(port)
        return {"argv": argv, "cwd": str(Path(exe).parent), "env": env, "url": url}

    raise LaunchError(f"Launch type '{launch_type}' is not process-backed.")


def is_running(slug: str) -> bool:
    rec = _procs.get(slug)
    return bool(rec and rec["popen"].poll() is None)


def start(tool_row) -> dict:
    """Start (or no-op if already running) the process for a tool. Returns status."""
    if _LOG_DIR is None:
        raise LaunchError("Launcher not initialized.")
    slug = tool_row["slug"]
    launch_type = tool_row["launch_type"]
    try:
        cfg = json.loads(tool_row["launch_config"] or "{}")
    except (TypeError, ValueError):
        cfg = {}

    with _lock:
        if is_running(slug):
            return status(slug)
        spec = _build_spec(slug, launch_type, cfg)
        log_path = _LOG_DIR / f"{slug}.log"
        logf = open(log_path, "a", encoding="utf-8", errors="ignore")
        logf.write(f"\n=== {_now()} starting: {' '.join(spec['argv'])} (cwd={spec['cwd']}) ===\n")
        logf.flush()
        try:
            popen = subprocess.Popen(
                spec["argv"], cwd=spec["cwd"], env=spec["env"],
                stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001
            logf.close()
            raise LaunchError(f"Failed to start: {exc}") from exc
        _procs[slug] = {
            "popen": popen, "logf": logf, "log_path": str(log_path),
            "url": spec["url"], "started_at": _now(), "argv": spec["argv"],
        }
        log.info("[launcher] started %s pid=%s", slug, popen.pid)
    return status(slug)


def stop(slug: str, *, timeout: float = 5.0) -> bool:
    """Gracefully stop a tool's process (terminate → wait → kill)."""
    with _lock:
        rec = _procs.get(slug)
        if not rec:
            return False
        popen = rec["popen"]
        if popen.poll() is None:
            popen.terminate()
            try:
                popen.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                popen.kill()
                popen.wait(timeout=timeout)
        try:
            rec["logf"].write(f"=== {_now()} stopped ===\n")
            rec["logf"].close()
        except Exception:  # noqa: BLE001
            pass
        _procs.pop(slug, None)
        log.info("[launcher] stopped %s", slug)
    return True


def restart(tool_row) -> dict:
    stop(tool_row["slug"])
    return start(tool_row)


def _http_ok(url: str) -> tuple[bool, int | None]:
    start_t = time.perf_counter()
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "DeliveryToolbox-Launcher"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            ms = int((time.perf_counter() - start_t) * 1000)
            return (resp.getcode() or 0) < 500, ms
    except Exception:  # noqa: BLE001
        return False, None


def status(slug: str) -> dict:
    """Current runtime status for a tool (safe to call for any slug)."""
    rec = _procs.get(slug)
    if not rec:
        return {"slug": slug, "running": False, "state": "stopped"}
    popen = rec["popen"]
    rc = popen.poll()
    if rc is not None:
        return {"slug": slug, "running": False, "state": "crashed" if rc else "exited",
                "exit_code": rc, "url": rec.get("url"), "started_at": rec["started_at"],
                "log_path": rec["log_path"]}
    health = {"state": "running"}
    if rec.get("url"):
        ok, ms = _http_ok(rec["url"])
        health = {"state": "healthy" if ok else "starting", "response_ms": ms}
    return {"slug": slug, "running": True, "pid": popen.pid,
            "url": rec.get("url"), "started_at": rec["started_at"],
            "log_path": rec["log_path"], **health}


def tail_log(slug: str, lines: int = 200) -> str:
    rec = _procs.get(slug)
    path = rec["log_path"] if rec else (str(_LOG_DIR / f"{slug}.log") if _LOG_DIR else None)
    if not path or not Path(path).exists():
        return "(no log yet)"
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return "".join(f.readlines()[-lines:])
    except OSError as exc:
        return f"(could not read log: {exc})"


def shutdown_all() -> None:
    for slug in list(_procs.keys()):
        try:
            stop(slug, timeout=3)
        except Exception:  # noqa: BLE001
            pass


atexit.register(shutdown_all)
