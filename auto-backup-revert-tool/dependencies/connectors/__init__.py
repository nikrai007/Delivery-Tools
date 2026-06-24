"""
Connectors for watched sources (Phase 2 + 3).

A connector knows how to:
  - poll an external location (local folder, Git repo, …) for new .sql / .zip
    / .7z bundles,
  - hand each candidate file to the orchestrator as a local on-disk path
    plus a stable identifier so the idempotency layer can skip files we've
    already processed.

The orchestrator is responsible for hashing, running the file through
``core.collect_deletes`` + ``core.generate_backup_revert``, packaging the
bundle ZIP, and depositing the bundle at the watched-source's destination
path.  Connectors are intentionally passive — they don't talk to the DB.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .local import LocalFolderConnector
from .git_repo import GitConnector


@dataclass
class Candidate:
    """One discovered file ready for processing."""
    local_path: Path        # absolute on-disk path the orchestrator can read
    original_path: str      # human-readable origin (display + dedup key)
    file_hash: str          # SHA-256 of the file contents


_ALLOWED_EXT = {".sql", ".zip", ".7z"}


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_candidate_file(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in _ALLOWED_EXT


def get_connector(kind: str):
    """Factory: return an instance of the connector class for ``kind``."""
    if kind == "local":
        return LocalFolderConnector()
    if kind == "git":
        return GitConnector()
    raise ValueError(f"Unknown connector kind: {kind!r}")


def parse_config(config_json: str | None) -> dict:
    if not config_json:
        return {}
    try:
        return json.loads(config_json)
    except json.JSONDecodeError:
        return {}


__all__ = [
    "Candidate",
    "LocalFolderConnector",
    "GitConnector",
    "get_connector",
    "parse_config",
    "sha256_of",
    "is_candidate_file",
]
