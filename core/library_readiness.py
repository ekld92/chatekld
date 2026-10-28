"""Cheap, observational Library evidence. Never recover or rebuild a manifest."""

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_MAX_EVIDENCE_BYTES = 8 * 1024 * 1024


def _signature(path: Path) -> tuple:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Evidence is not a regular file")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_object(path: Path) -> dict:
    # Bound request-thread work even for an unexpectedly large/corrupt manifest.
    # Nonblocking open also avoids waiting on a FIFO substituted after stat.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Evidence is not a regular file")
        raw = stream.read(_MAX_EVIDENCE_BYTES + 1)
    if len(raw) > _MAX_EVIDENCE_BYTES:
        raise ValueError("Evidence exceeds the status read limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Evidence is not an object")
    return value


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Invalid evidence timestamp")
    datetime.fromisoformat(value)
    return value


def library_readiness(index_dir: str, vault_path: str | None) -> dict:
    """Describe saved evidence, not current corpus coverage or service health.

    The two existing files have no shared generation identifier. Even matching
    vault paths cannot certify that the manifest represents today's index.
    Unknown counts stay null; no directory walks, cache reads, model loads,
    manager recovery, journal writes or message-draining status calls occur.
    """
    result: dict[str, Any] = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Configured vault; saved index evidence only",
        "state": "unavailable",
        "recorded_sources": None,
        "indexed_at": None,
        "manifest_at": None,
        "counts": dict.fromkeys(("discovered", "complete_caches", "searchable",
                                 "pending_changed", "deferred")),
        "next_step": "Use Search only: review update to check current changes; confirm separately to update.",
    }
    if not vault_path:
        result.update(state="not_configured", message="Choose a vault in Library settings.",
                      next_step="Open Settings → Library paths and audit settings to choose a vault.")
        return result
    try:
        paths = [Path(index_dir) / name for name in ("obsidian_meta.json", "indexed_materials.json")]
        before = [_signature(path) for path in paths]
        meta, manifest = [_read_object(path) for path in paths]
        if before != [_signature(path) for path in paths]:
            raise ValueError("Evidence changed during observation")
        # Stored paths are normalized by the writer. Do not resolve files in
        # the corpus or adopt legacy evidence whose vault binding is unknown.
        if meta.get("vault_path") != vault_path or manifest.get("vault_path") != vault_path:
            result.update(state="stale", message="Saved evidence has a different or unknown vault binding.")
            return result
        indexed_at = _timestamp(meta.get("indexed_at"))
        manifest_at = _timestamp(manifest.get("indexed_at"))
        if any(key in meta and type(meta[key]) is not bool for key in ("partial", "has_vector_data")):
            raise ValueError("Invalid metadata flags")
        rows = manifest.get("materials")
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("source"), str)
            or not row["source"] or type(row.get("chunk_count")) is not int
            or row["chunk_count"] <= 0 for row in rows
        ):
            raise ValueError("Invalid source evidence")
        result["recorded_sources"] = len({row["source"] for row in rows})
        result.update(indexed_at=indexed_at, manifest_at=manifest_at)
        if meta.get("partial") is True:
            result.update(state="partial", message="Saved index metadata records an incomplete update. Review unfinished preparation and index history.")
        elif meta.get("has_vector_data") is True:
            result.update(state="snapshot", message="Saved vector data is recorded; current search coverage and lexical readiness are unverified.")
        else:
            result.update(state="unknown", message="Saved metadata does not establish vector-search readiness.")
    except (OSError, ValueError, TypeError, RecursionError):
        result.update(state="unavailable", message="Saved index evidence is missing, unreadable or invalid.")
    return result
