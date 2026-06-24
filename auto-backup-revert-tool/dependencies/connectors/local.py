"""
Local-folder / network-share connector.

`source_path` is a directory on the same machine the app runs on (could be a
mounted SMB share). The connector lists candidate files inside it (recursively
up to a small depth so we don't accidentally crawl gigabyte trees) and hands
them back as on-disk paths the orchestrator can read directly.

Stability check: a file is considered ready only when its size has not changed
between two stat() reads ``stability_seconds`` apart. This avoids picking up
half-written uploads from another process.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator


class LocalFolderConnector:
    kind = "local"
    name = "Local folder / network share"

    # Maximum directory depth when walking. Catches common 1- or 2-level
    # release-folder layouts without ever traversing huge trees.
    MAX_DEPTH = 2

    def validate(self, source_path: str, dest_path: str, config: dict) -> list[str]:
        errors: list[str] = []
        sp = Path(source_path).expanduser()
        dp = Path(dest_path).expanduser()
        if not sp.exists():
            errors.append(f"Source path does not exist: {source_path}")
        elif not sp.is_dir():
            errors.append(f"Source path is not a directory: {source_path}")
        if not dp.exists():
            try:
                dp.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                errors.append(f"Cannot create destination path: {exc}")
        return errors

    def discover(self, *, source_path: str, config: dict,
                 stability_seconds: int = 30) -> Iterator[Path]:
        """Yield candidate file paths inside source_path."""
        from . import is_candidate_file  # avoid circular at module load

        root = Path(source_path).expanduser()
        if not root.is_dir():
            return

        # Optional sub-path filter — if config["sub_path"] is set, only walk
        # that sub-tree. Useful when one folder hosts multiple unrelated
        # release queues.
        sub_path = (config or {}).get("sub_path") or ""
        scan_root = (root / sub_path).resolve() if sub_path else root

        if not scan_root.is_dir() or not _is_under(scan_root, root):
            return

        for p in _walk(scan_root, max_depth=self.MAX_DEPTH):
            if not is_candidate_file(p):
                continue
            if not _is_stable(p, stability_seconds):
                continue
            yield p

    def deliver(self, *, dest_path: str, config: dict, bundle_path: Path) -> str:
        """
        Copy the generated bundle ZIP into the destination directory.
        Returns the final destination path as a string.
        """
        from shutil import copy2
        dest = Path(dest_path).expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / bundle_path.name
        copy2(bundle_path, target)
        return str(target)


def _walk(root: Path, *, max_depth: int) -> Iterator[Path]:
    """Depth-limited walk so we don't crawl gigabyte trees by mistake."""
    base_depth = len(root.parts)
    for p in root.rglob("*"):
        depth = len(p.parts) - base_depth
        if depth > max_depth:
            continue
        yield p


def _is_stable(p: Path, seconds: int) -> bool:
    """True if the file's size hasn't changed in ``seconds``."""
    try:
        first = p.stat().st_size
    except OSError:
        return False
    time.sleep(min(seconds, 2))   # quick check; real stability via mtime gap below
    try:
        mtime = p.stat().st_mtime
        second = p.stat().st_size
    except OSError:
        return False
    if first != second:
        return False
    return (time.time() - mtime) >= max(0, seconds - 2)


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
