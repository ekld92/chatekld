"""Image-bytes hashing + description-cache reuse (the §13 reuse surface).

The durable, vector-backend-independent reuse surface is the on-disk
``obsidian_cache/image_cache/<vault_key>/<sha256-of-image-bytes>.txt`` written by
the indexer (``rag/vault.py``). This module:

* hashes an image's bytes the same way the indexer does (``digest_for``),
  honouring the 20 MB cap and, on macOS, **skipping un-materialized iCloud
  placeholders by default** so the read-only plan never triggers a surprise
  multi-GB download (especially while indexing is in flight);
* reads the indexer's prose description (``read_description``) — cache-only, no
  live vision fallback;
* reads/writes the refactor tool's own per-mode caches
  (``<sha256>.table.txt`` / ``<sha256>.redescribe.txt``) so a user-chosen
  re-extraction persists **without ever touching the indexer's
  ``<sha256>.txt``** (which would change what a future index run embeds).

Every path is under ``obsidian_cache/`` (the app cache dir). Nothing here writes
the vault.
"""
from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from core.constants import OBSIDIAN_CACHE_DIR
from rag.vault import obsidian_manager

# Single source of truth for the status string values (see result.STATUS_*).
from refactor.result import (
    STATUS_DATALESS,
    STATUS_MISSING,
    STATUS_OK,
    STATUS_READ_ERROR,
    STATUS_TOO_BIG,
)

# macOS: ``st_flags & SF_DATALESS`` marks an iCloud placeholder whose bytes are
# not present locally. Reading such a file blocks while it downloads. We detect
# it via the raw mask (the ``stat`` module does not export the constant) and
# degrade to "treat as not extracted" rather than force a download in the
# read-only plan. On platforms without ``st_flags`` the check is a no-op.
_SF_DATALESS = 0x40000000

# Per-mode caches the refactor tool owns (distinct from the indexer's base
# ``<sha256>.txt``). Restricted set so a mode string can never escape the
# filename it builds. ``classify`` stores a single canonical label string.
_ALLOWED_MODES = frozenset({"table", "redescribe", "classify"})

# --- image-digest memo (Track 5.1, 2026-07-04) ------------------------------
# DEFECT: ``digest_for`` read + sha256-hashed the FULL image bytes on every
# call, and the per-image OCR-inclusion panel debounce-re-analyzes a note
# (``plan.analyze_one`` → ``digest_for`` per embedded image) on EVERY checkbox
# toggle — so a user flipping a few checkboxes on an image-heavy note re-read
# tens of MB from disk per click for bytes that had not changed.
# FIX: memoize ``(path, size, mtime_ns) → sha256`` and consult it after the
# dataless/size-cap checks (which already stat fresh, never from the memo).
# SAFE W.R.T. STATE: the key embeds the fresh stat's ``(size, mtime_ns)``, so
# any content change mints a new key and re-hashes — a hit can only serve the
# digest of the exact bytes currently on disk (APFS mtime is ns-resolution; a
# same-size same-mtime_ns content swap is not achievable by normal editing).
# Entries are evicted LRU at ``_DIGEST_MEMO_MAX`` and the whole memo is dropped
# by ``resolver.invalidate_index_cache`` (archive/restore move files; vault
# switch) purely to bound memory — correctness never depends on invalidation.
# INVARIANT (pinned by test_refactor.py::test_digest_memo_*): a memo hit
# returns the same digest a fresh read would, and a changed file (different
# size or mtime_ns) is always re-hashed.
_DIGEST_MEMO_MAX = 4096
_digest_memo_lock = threading.Lock()
_digest_memo: dict[tuple[str, int, int], str] = {}


def _digest_memo_get(key: tuple[str, int, int]) -> str | None:
    with _digest_memo_lock:
        digest = _digest_memo.pop(key, None)
        if digest is not None:
            _digest_memo[key] = digest  # re-insert = mark most-recently-used
        return digest


def _digest_memo_put(key: tuple[str, int, int], digest: str) -> None:
    with _digest_memo_lock:
        _digest_memo.pop(key, None)
        _digest_memo[key] = digest
        while len(_digest_memo) > _DIGEST_MEMO_MAX:
            _digest_memo.pop(next(iter(_digest_memo)))


def clear_digest_memo() -> None:
    """Drop every memoized digest (memory bound only — see memo notes above)."""
    with _digest_memo_lock:
        _digest_memo.clear()


def digest_for(rel_path: str, vault_root: Path, *, materialize: bool = False) -> dict:
    """Return ``{digest, size, status}`` for the image at *rel_path*.

    ``status`` is one of the ``result.STATUS_*`` values. With
    ``materialize=False`` (the default, used by the read-only plan) a dataless
    iCloud placeholder returns ``STATUS_DATALESS`` **without reading bytes**, so
    the plan never triggers a network download; with ``materialize=True`` (the
    explicit per-image extract path) the bytes are always read. A single
    ``stat`` is reused for the dataless check, the size-cap check, and the read
    decision — never re-stat'd.
    """
    p = vault_root / rel_path
    try:
        st = p.stat()
    except OSError:
        # Covers a non-existent path (a broken/parent-relative embed) and any
        # other stat failure — both mean "no bytes to hash, nothing cached".
        return {"digest": "", "size": -1, "status": STATUS_MISSING}
    size = st.st_size
    # macOS dataless (iCloud placeholder) check on the already-fetched stat:
    # reading such a file would block on a download, which the plan must avoid.
    # ``getattr(..., 0)`` makes this a no-op on platforms (e.g. Linux) whose
    # ``stat_result`` has no ``st_flags`` field, so the check never raises.
    if not materialize and (getattr(st, "st_flags", 0) & _SF_DATALESS):
        return {"digest": "", "size": size, "status": STATUS_DATALESS}
    if size > obsidian_manager._IMAGE_MAX_BYTES:
        # The indexer never described over-cap images, so there is nothing to
        # reuse; flag rather than read a potentially huge file.
        return {"digest": "", "size": size, "status": STATUS_TOO_BIG}
    # Memo lookup sits AFTER the dataless/size-cap gates so those decisions are
    # always made on the fresh stat, never on a remembered one. The key's
    # (size, mtime_ns) self-validates the entry against the current bytes.
    memo_key = (str(p), size, st.st_mtime_ns)
    memoized = _digest_memo_get(memo_key)
    if memoized is not None:
        return {"digest": memoized, "size": size, "status": STATUS_OK}
    try:
        data = p.read_bytes()
    except OSError:
        return {"digest": "", "size": size, "status": STATUS_READ_ERROR}
    # The cache key is the sha256 of the raw image bytes — byte-identical to how
    # the indexer keys ``<sha256>.txt`` (rag/vault.py), which is what makes the
    # description reuse work without re-running vision.
    digest = hashlib.sha256(data).hexdigest()
    _digest_memo_put(memo_key, digest)
    return {"digest": digest, "size": size, "status": STATUS_OK}


def read_description(digest: str, vault_root: Path) -> str:
    """The indexer's prose description for *digest* (cache-only, primary+legacy)."""
    if not digest:
        return ""
    return obsidian_manager._read_first_text_file([
        obsidian_manager._image_cache_file(vault_root, digest),
        obsidian_manager._legacy_image_cache_file(vault_root, digest),
    ])


def _mode_cache_file(vault_root: Path, digest: str, mode: str) -> Path:
    """Path of a per-mode cache file, e.g. ``…/<vault_key>/<sha256>.table.txt``.

    Sits in the same per-vault ``image_cache`` dir as the indexer's base
    ``<sha256>.txt`` but with a ``.<mode>.txt`` suffix, so a refactor extraction
    never overwrites what the indexer would re-embed. Callers restrict *mode* to
    ``_ALLOWED_MODES`` before building the filename.
    """
    vault_key = obsidian_manager._vault_cache_key(vault_root)
    return Path(OBSIDIAN_CACHE_DIR) / "image_cache" / vault_key / f"{digest}.{mode}.txt"


def read_mode(digest: str, vault_root: Path, mode: str) -> str:
    """Read a refactor per-mode cache (``table`` / ``redescribe``); "" if absent."""
    if not digest or mode not in _ALLOWED_MODES:
        return ""
    return obsidian_manager._read_first_text_file([
        _mode_cache_file(vault_root, digest, mode)
    ])


def write_mode(digest: str, vault_root: Path, mode: str, text: str) -> None:
    """Persist a user-chosen re-extraction atomically under ``obsidian_cache/``.

    Raises ``ValueError`` on an unknown mode (defence: the mode becomes part of
    the filename). Never touches the indexer's base ``<sha256>.txt``.
    """
    if mode not in _ALLOWED_MODES:
        raise ValueError(f"unknown refactor cache mode: {mode!r}")
    if not digest:
        raise ValueError("empty digest")
    obsidian_manager._atomic_write_text(
        _mode_cache_file(vault_root, digest, mode), text
    )


def best_description(digest: str, vault_root: Path) -> str:
    """Prefer a refactor re-description, else the indexer's base description."""
    return read_mode(digest, vault_root, "redescribe") or read_description(digest, vault_root)
