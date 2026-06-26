"""Sticky, per-vault image ignore-list sidecar (Phase 1.5).

The refactor hub lets the user mark an image as "ignore" — e.g. a handwritten
scan the local vision model cannot reliably OCR — so the read-only plan greys it
out, skips inlining a callout for it, and drops it from the "not extracted" /
"likely table" candidate counts.

The list is **rel_path-keyed** (vault-relative posix paths) and persisted as JSON
under ``BASE_DIR/obsidian_cache/refactor/<vault_key>/ignore_list.json`` — the app
cache dir, **never the vault** (Phase 1 writes zero vault files). It is keyed by
vault (like the image-description cache) so two vaults never share a list. Reuses
the indexer's atomic text writer + vault-cache-key helpers on the singleton,
exactly as ``cache.py`` does (so the vendored reuse surface stays in one place).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from core.constants import OBSIDIAN_CACHE_DIR
from rag.vault import obsidian_manager

_IGNORE_FILENAME = "ignore_list.json"

# Serialize the sidecar's read-modify-write. Each ``_save`` is atomic on its own
# (``_atomic_write_text`` does a temp-sibling + ``os.replace``), but ``add`` /
# ``remove`` do load → mutate → save as three steps, and the app is served by
# waitress with many worker threads — so two concurrent toggles of *different*
# images could each load the same snapshot and the second save would clobber the
# first's entry (a lost update). Holding this lock across the whole sequence makes
# it atomic within the process (single-process server ⇒ a threading.Lock is
# sufficient; no cross-process coordination needed). Toggling the *same* image
# concurrently is already idempotent (set add/discard), but the lock covers it too.
# The pure-read paths (``load_ignored`` / ``list_ignored``) intentionally do NOT
# take the lock: ``os.replace`` is atomic, so a reader always sees either the old
# or the new complete file, never a torn one.
_LOCK = threading.Lock()


def _ignore_file(vault_root: Path) -> Path:
    vault_key = obsidian_manager._vault_cache_key(vault_root)
    return Path(OBSIDIAN_CACHE_DIR) / "refactor" / vault_key / _IGNORE_FILENAME


def load_ignored(vault_root: Path) -> set[str]:
    """Set of ignored vault-relative image paths (empty if absent / corrupt)."""
    try:
        raw = _ignore_file(vault_root).read_text(encoding="utf-8")
    except OSError:
        return set()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return set()
    images = data.get("images") if isinstance(data, dict) else None
    if not isinstance(images, list):
        return set()
    return {s for s in images if isinstance(s, str) and s}


def list_ignored(vault_root: Path) -> list[str]:
    """Sorted list form of :func:`load_ignored` (for JSON responses)."""
    return sorted(load_ignored(vault_root))


def _save(vault_root: Path, ignored: set[str]) -> list[str]:
    ordered = sorted(ignored)
    obsidian_manager._atomic_write_text(
        _ignore_file(vault_root), json.dumps({"images": ordered}, ensure_ascii=False)
    )
    return ordered


def add(vault_root: Path, rel: str) -> list[str]:
    """Add *rel* to the ignore-list; return the new sorted list.

    The whole load → mutate → save runs under ``_LOCK`` so a concurrent toggle
    of a *different* image cannot clobber this addition (see ``_LOCK``).
    """
    with _LOCK:
        ignored = load_ignored(vault_root)
        ignored.add(rel)
        return _save(vault_root, ignored)


def remove(vault_root: Path, rel: str) -> list[str]:
    """Remove *rel* from the ignore-list; return the new sorted list.

    Same lock discipline as :func:`add` — the load → mutate → save is atomic
    within the process so a concurrent toggle can't resurrect a removed entry.
    """
    with _LOCK:
        ignored = load_ignored(vault_root)
        ignored.discard(rel)
        return _save(vault_root, ignored)
