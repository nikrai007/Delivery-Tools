"""
In-memory run progress registry — the live view the web UI polls.

Kept DB- and Flask-free: the persistent record of a run lives in SQLite
(``xpm_store``); this registry only holds the *transient* progress of runs that
are currently executing in a worker thread, so the status page can stream a
step-by-step timeline and a percentage without hammering the database.

Thread-safe. Entries are pruned when a run finishes (after a short grace period
so the final poll still sees the terminal state).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Terminal + transient status vocabulary (shared with the DB layer / UI).
STATUS_UPLOADED = "uploaded"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

TERMINAL = {STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED}

_LEVEL_NAME = {
    logging.DEBUG: "debug", logging.INFO: "info",
    logging.WARNING: "warning", logging.ERROR: "error", logging.CRITICAL: "error",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


@dataclass
class RunProgress:
    run_id: int
    status: str = STATUS_PROCESSING
    phase: str = "starting"
    percent: int = 0
    message: str = "Starting…"
    steps: list = field(default_factory=list)      # [{ts, level, message}]
    files: list = field(default_factory=list)      # [{name, status, error}]
    cancel_requested: bool = False
    started_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    finished_at: str | None = None

    def public(self) -> dict:
        """A JSON-serialisable snapshot (no secrets held here by design)."""
        return {
            "run_id": self.run_id,
            "status": self.status,
            "phase": self.phase,
            "percent": self.percent,
            "message": self.message,
            "steps": list(self.steps),
            "files": list(self.files),
            "cancel_requested": self.cancel_requested,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "done": self.status in TERMINAL,
        }


class Registry:
    """Thread-safe registry of in-flight runs, keyed by run_id."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs: dict[int, RunProgress] = {}
        self._reap_after_s = 120  # keep terminal entries briefly for the last poll

    def start(self, run_id: int, file_names: list[str]) -> RunProgress:
        with self._lock:
            rp = RunProgress(
                run_id=run_id,
                files=[{"name": n, "status": "pending", "error": None} for n in file_names],
            )
            self._runs[run_id] = rp
            return rp

    def get(self, run_id: int) -> RunProgress | None:
        with self._lock:
            return self._runs.get(run_id)

    def add_step(self, run_id: int, message: str, level: int = logging.INFO) -> None:
        with self._lock:
            rp = self._runs.get(run_id)
            if not rp:
                return
            rp.steps.append({"ts": _now_iso(), "level": _LEVEL_NAME.get(level, "info"),
                             "message": message})
            rp.message = message
            rp.updated_at = _now_iso()

    def set(self, run_id: int, *, status: str | None = None, phase: str | None = None,
            percent: int | None = None, message: str | None = None) -> None:
        with self._lock:
            rp = self._runs.get(run_id)
            if not rp:
                return
            if status is not None:
                rp.status = status
            if phase is not None:
                rp.phase = phase
            if percent is not None:
                rp.percent = max(0, min(100, int(percent)))
            if message is not None:
                rp.message = message
            rp.updated_at = _now_iso()
            if rp.status in TERMINAL and rp.finished_at is None:
                rp.finished_at = _now_iso()

    def set_file_status(self, run_id: int, name: str, status: str,
                        error: str | None = None) -> None:
        with self._lock:
            rp = self._runs.get(run_id)
            if not rp:
                return
            for f in rp.files:
                if f["name"] == name and f["status"] == "pending":
                    f["status"] = status
                    f["error"] = error
                    break
            else:
                for f in rp.files:
                    if f["name"] == name:
                        f["status"] = status
                        f["error"] = error
                        break
            rp.updated_at = _now_iso()

    def request_cancel(self, run_id: int) -> bool:
        with self._lock:
            rp = self._runs.get(run_id)
            if not rp or rp.status in TERMINAL:
                return False
            rp.cancel_requested = True
            return True

    def should_cancel(self, run_id: int) -> bool:
        with self._lock:
            rp = self._runs.get(run_id)
            return bool(rp and rp.cancel_requested)

    def reap(self) -> None:
        """Drop terminal entries older than the grace period."""
        cutoff = time.time() - self._reap_after_s
        with self._lock:
            for rid in list(self._runs):
                rp = self._runs[rid]
                if rp.status in TERMINAL and rp.finished_at:
                    try:
                        ft = datetime.fromisoformat(rp.finished_at.rstrip("Z"))
                        if ft.replace(tzinfo=timezone.utc).timestamp() < cutoff:
                            del self._runs[rid]
                    except (ValueError, KeyError):
                        pass


# Process-wide singleton the web layer and workers share.
registry = Registry()
