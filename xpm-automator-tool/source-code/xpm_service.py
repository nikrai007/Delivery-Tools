"""
XPM Automator — application service / worker orchestration.

Bridges the pure service layer (``xpm_core``) with persistence (``xpm_store``)
and the live progress registry. Runs each job on a daemon thread and reports
progress step-by-step so the web UI can stream it.

Design guarantees:
  * The XPM **password never leaves the worker thread's closure** — it is not
    written to the registry snapshot, the DB, or the audit log.
  * Cooperative cancellation: the worker checks the registry's cancel flag
    between files and passes a ``should_cancel`` callback into the HTTP client
    so an in-flight request is abandoned promptly.
  * Every terminal outcome persists a full processing timeline for the detail
    view, then flips the DB row + registry to the terminal status.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime
from pathlib import Path

import xpm_store as store
from xpm_core import batch as batch_mod
from xpm_core.client import XPMClient, XPMError
from xpm_core.config import XPMConfig
from xpm_core.pipeline import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    registry,
)

log = logging.getLogger("xpm.service")

_NAT_RE = re.compile(r"(\d+)")


def natural_key(name: str):
    """Sort key so '01_' precedes '10_' (matches XPM's numeric-prefix batch order)."""
    return [int(t) if t.isdigit() else t.lower() for t in _NAT_RE.split(name)]


def _steps_snapshot(run_id: int) -> list:
    rp = registry.get(run_id)
    return list(rp.steps) if rp else []


# ----------------------------------------------------------------------
# Upload run
# ----------------------------------------------------------------------
def start_upload_run(run_id: int, cfg: XPMConfig, file_paths: list[str],
                     work_dir: str) -> None:
    """Spawn the background worker for an upload run. Returns immediately."""
    t = threading.Thread(
        target=_run_upload, args=(run_id, cfg, list(file_paths), work_dir),
        name=f"xpm-upload-{run_id}", daemon=True,
    )
    t.start()


def _run_upload(run_id: int, cfg: XPMConfig, file_paths: list[str], work_dir: str) -> None:
    names = [Path(p).name for p in file_paths]
    registry.start(run_id, names)

    def emit(msg: str, level: int = logging.INFO) -> None:
        registry.add_step(run_id, msg, level)

    store.mark_started(run_id)
    registry.set(run_id, status="processing", phase="connecting", percent=3,
                 message="Connecting to XPM…")

    client = XPMClient(cfg, log=emit, should_cancel=lambda: registry.should_cancel(run_id))
    uploaded, failed = 0, 0
    output_file = output_name = None
    output_size = None
    try:
        client.login()
        registry.set(run_id, phase="project", percent=12, message="Selecting project…")
        client.select_project()

        total = len(file_paths)
        registry.set(run_id, phase="uploading", percent=15,
                     message=f"Uploading {total} file(s)…")
        for idx, fp in enumerate(file_paths, start=1):
            if registry.should_cancel(run_id):
                raise XPMError("Run cancelled by user.")
            name = Path(fp).name
            emit(f"— [{idx}/{total}] {name} —")
            outcome = client.upload_file_with_retry(fp)
            if outcome.ok:
                uploaded += 1
                registry.set_file_status(run_id, name, "uploaded")
                store.set_file_status(run_id, name, "uploaded")
            else:
                failed += 1
                registry.set_file_status(run_id, name, "failed", outcome.error)
                store.set_file_status(run_id, name, "failed", outcome.error)
            registry.set(run_id, percent=15 + int(65 * idx / max(1, total)),
                         message=f"Uploaded {uploaded}/{total} ({failed} failed)")

        emit(f"Upload phase complete: {uploaded} succeeded, {failed} failed.",
             logging.WARNING if failed else logging.INFO)

        if uploaded > 0 and not registry.should_cancel(run_id):
            registry.set(run_id, phase="downloading", percent=85,
                         message="Downloading consolidated script…")
            content, bf, bt = client.download_consolidated(uploaded)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            project = re.sub(r"[^A-Za-z0-9]+", "_", cfg.project_name or "migration").strip("_")
            out_path = Path(work_dir) / f"{project}_consolidated_{bf}_{bt}_{ts}.sql"
            out_path.write_bytes(content)
            output_file = str(out_path)
            output_name = out_path.name
            output_size = len(content)
            store.set_batch_range(run_id, bf, bt)
            emit(f"Saved consolidated script for batches #{bf}–#{bt} ({output_size:,} bytes).")
        elif uploaded == 0:
            emit("Skipping download — no files uploaded successfully.", logging.WARNING)

        # Terminal state
        if registry.should_cancel(run_id):
            _finish(run_id, STATUS_CANCELLED, uploaded, failed, output_file, output_name,
                    output_size, error="Cancelled by user.",
                    remarks=f"{uploaded} uploaded before cancel.")
            return
        if uploaded == 0:
            _finish(run_id, STATUS_FAILED, uploaded, failed, None, None, None,
                    error="No files were uploaded successfully.",
                    remarks="All uploads failed.")
            return
        remarks = "All files uploaded." if failed == 0 else \
            f"Partial success — {uploaded} uploaded, {failed} failed."
        _finish(run_id, STATUS_COMPLETED, uploaded, failed, output_file, output_name,
                output_size, error=None, remarks=remarks)

    except XPMError as exc:
        cancelled = registry.should_cancel(run_id)
        status = STATUS_CANCELLED if cancelled else STATUS_FAILED
        emit(str(exc), logging.ERROR)
        _finish(run_id, status, uploaded, failed, output_file, output_name, output_size,
                error=str(exc),
                remarks="Cancelled by user." if cancelled else "Run failed.")
    except Exception as exc:  # noqa: BLE001
        log.exception("XPM upload run %s crashed", run_id)
        emit(f"Unexpected error: {exc}", logging.ERROR)
        _finish(run_id, STATUS_FAILED, uploaded, failed, output_file, output_name, output_size,
                error=str(exc), remarks="Unexpected worker error.")
    finally:
        client.close()


# ----------------------------------------------------------------------
# Batch-range download run
# ----------------------------------------------------------------------
def start_batch_download_run(run_id: int, cfg: XPMConfig, batch_from: int, batch_to: int,
                             work_dir: str) -> None:
    t = threading.Thread(
        target=_run_batch_download, args=(run_id, cfg, batch_from, batch_to, work_dir),
        name=f"xpm-batch-{run_id}", daemon=True,
    )
    t.start()


def _run_batch_download(run_id: int, cfg: XPMConfig, batch_from: int, batch_to: int,
                        work_dir: str) -> None:
    registry.start(run_id, [])

    def emit(msg: str, level: int = logging.INFO) -> None:
        registry.add_step(run_id, msg, level)

    store.mark_started(run_id)
    registry.set(run_id, status="processing", phase="connecting", percent=5,
                 message="Connecting to XPM…")
    client = XPMClient(cfg, log=emit, should_cancel=lambda: registry.should_cancel(run_id))
    try:
        client.login()
        registry.set(run_id, phase="project", percent=20, message="Selecting project…")
        client.select_project()
        registry.set(run_id, phase="downloading", percent=40,
                     message=f"Downloading batches #{batch_from}–#{batch_to}…")
        content = client.download_batch_range(batch_from, batch_to)

        if registry.should_cancel(run_id):
            _finish(run_id, STATUS_CANCELLED, 0, 0, None, None, None,
                    error="Cancelled by user.", remarks="Cancelled during download.")
            return

        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = content.decode("latin-1")
        project = re.sub(r"[^A-Za-z0-9]+", "_", cfg.project_name or "migration").strip("_")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = Path(work_dir) / f"{project}_{batch_from}_{batch_to}_{ts}.sql"
        out_path.write_text(text, encoding="utf-8")
        emit(f"Saved merged script ({len(content):,} bytes).")
        _finish(run_id, STATUS_COMPLETED, 0, 0, str(out_path), out_path.name, len(content),
                error=None, remarks=f"Batches #{batch_from}–#{batch_to} merged.")
    except XPMError as exc:
        cancelled = registry.should_cancel(run_id)
        status = STATUS_CANCELLED if cancelled else STATUS_FAILED
        emit(str(exc), logging.ERROR)
        _finish(run_id, status, 0, 0, None, None, None, error=str(exc),
                remarks="Cancelled by user." if cancelled else "Download failed.")
    except Exception as exc:  # noqa: BLE001
        log.exception("XPM batch download run %s crashed", run_id)
        emit(f"Unexpected error: {exc}", logging.ERROR)
        _finish(run_id, STATUS_FAILED, 0, 0, None, None, None, error=str(exc),
                remarks="Unexpected worker error.")
    finally:
        client.close()


# ----------------------------------------------------------------------
def _finish(run_id: int, status: str, uploaded: int, failed: int,
            output_file, output_name, output_size, error, remarks) -> None:
    steps = _steps_snapshot(run_id)
    store.finish_run(run_id, status=status, files_uploaded=uploaded, files_failed=failed,
                     output_file=output_file, output_name=output_name,
                     output_size_bytes=output_size, error_message=error,
                     remarks=remarks, steps=steps)
    pct = 100 if status == STATUS_COMPLETED else registry.get(run_id).percent if registry.get(run_id) else 100
    registry.set(run_id, status=status, phase="done", percent=pct,
                 message=remarks or status.title())


# ----------------------------------------------------------------------
# Batch Explorer — synchronous live listing (runs in the request thread).
# ----------------------------------------------------------------------
def list_batches(cfg: XPMConfig) -> list[dict]:
    """Log in, switch project, and return every script in the project (newest
    first) as plain dicts. Fail-fast config so a VPN outage doesn't hang the
    request. Raises XPMError on failure."""
    fast = XPMConfig(**{**cfg.__dict__, "max_retries": 0, "timeout": 15})
    client = XPMClient(fast, log=lambda m, l=logging.INFO: log.info(m))
    try:
        client.login()
        client.select_project()
        return [b.as_dict() for b in client.list_all_batches()]
    finally:
        client.close()


def _fast_client(cfg: XPMConfig) -> XPMClient:
    fast = XPMConfig(**{**cfg.__dict__, "max_retries": 0, "timeout": 15})
    return XPMClient(fast, log=lambda m, l=logging.INFO: log.info(m))


def fetch_projects(cfg: XPMConfig) -> list[dict]:
    """Live list of XPM projects [{id, name}] for the config picker."""
    client = _fast_client(cfg)
    try:
        return client.list_projects()
    finally:
        client.close()


def fetch_processes(cfg: XPMConfig) -> list[dict]:
    """Live list of processes [{value, name}] for the selected project."""
    client = _fast_client(cfg)
    try:
        return client.list_processes()
    finally:
        client.close()


# ----------------------------------------------------------------------
# Batch-number helper re-exported for the routes layer.
# ----------------------------------------------------------------------
def new_batch_number() -> str:
    return batch_mod.generate()
