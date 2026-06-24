"""
Git connector.

`source_path` is a Git URL (HTTPS).  Authentication is via a PAT (Personal
Access Token) stored in the watched-source's config_json — the orchestrator
keeps it server-side and never exposes it through the UI after creation.

On each poll:
  - The repo is cloned (or fetched if already present) to a per-source cache
    directory under ``UPLOAD_ROOT/_scheduler_cache/<source_id>/``.
  - The configured branch is checked out.
  - Files under the optional ``sub_path`` are scanned for ``.sql`` / ``.zip``
    / ``.7z`` candidates and yielded.

The dest_path is currently a local folder where the bundle ZIP is dropped.
Pushing the generated bundle back to the repo is intentionally out of scope
for the first iteration of Phase 3 (branch protection, commit author, signed
commits all complicate "just push it" significantly — easier as a follow-up).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit, urlunsplit


class GitConnector:
    kind = "git"
    name = "Git repository"

    MAX_DEPTH = 4

    def validate(self, source_path: str, dest_path: str, config: dict) -> list[str]:
        errors: list[str] = []
        if not source_path or not (
            source_path.startswith("http://") or source_path.startswith("https://")
            or source_path.startswith("git@")
        ):
            errors.append("Source must be a Git HTTPS URL or git@ SSH URL.")
        dp = Path(dest_path).expanduser()
        if not dp.exists():
            try:
                dp.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                errors.append(f"Cannot create destination path: {exc}")
        if shutil.which("git") is None:
            errors.append("`git` binary is not on PATH on the server.")
        return errors

    def discover(self, *, source_path: str, config: dict,
                 cache_root: Path | None = None,
                 stability_seconds: int = 0) -> Iterator[Path]:
        """Sync the repo and yield candidate files."""
        from . import is_candidate_file

        if cache_root is None:
            raise RuntimeError("GitConnector.discover requires cache_root")
        cache_root.mkdir(parents=True, exist_ok=True)

        branch   = (config or {}).get("branch") or "main"
        sub_path = (config or {}).get("sub_path") or ""
        pat      = (config or {}).get("pat") or ""

        url_with_creds = _embed_pat(source_path, pat) if pat else source_path

        git_dir = cache_root / ".git"
        if git_dir.is_dir():
            _git(cache_root, ["remote", "set-url", "origin", url_with_creds])
            _git(cache_root, ["fetch", "--prune", "origin"])
            _git(cache_root, ["checkout", "-B", branch, f"origin/{branch}"])
        else:
            _git(cache_root.parent, [
                "clone", "--depth", "1", "--branch", branch,
                url_with_creds, str(cache_root),
            ])

        scan_root = (cache_root / sub_path).resolve() if sub_path else cache_root.resolve()
        if not scan_root.is_dir() or not _is_under(scan_root, cache_root):
            return
        base_depth = len(scan_root.parts)
        for p in scan_root.rglob("*"):
            # Skip .git internals defensively
            if ".git" in p.parts:
                continue
            depth = len(p.parts) - base_depth
            if depth > self.MAX_DEPTH:
                continue
            if is_candidate_file(p):
                yield p

    def deliver(self, *, dest_path: str, config: dict, bundle_path: Path) -> str:
        from shutil import copy2
        dest = Path(dest_path).expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / bundle_path.name
        copy2(bundle_path, target)
        return str(target)


def _git(cwd: Path, args: list[str]) -> str:
    """Run a git command and return stdout. Raises on non-zero exit."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"            # never block waiting for creds
    env.setdefault("GIT_ASKPASS", "/bin/true")  # silence credential helpers
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True, text=True, env=env, timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout


def _embed_pat(url: str, pat: str) -> str:
    """Embed a PAT into an HTTPS URL as `https://x-access-token:<pat>@host/...`."""
    if not url.startswith(("http://", "https://")):
        return url
    parts = urlsplit(url)
    # Strip any existing userinfo, then re-add the PAT
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    netloc = f"x-access-token:{pat}@{netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
