"""Cached vault file resolution shared by presentation and report generation."""
from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path

from core.config import load_config
from core.constants import OBSIDIAN_EXCLUDED_DIR_NAMES, VAULT_IMAGE_EXTS
from core.utils import nfc
from rag.vault import (
    obsidian_manager,
)


def excluded_dirs(vault_root: Path) -> set[str]:
    """Return user exclusions using the indexer's normalization rules."""
    cfg = load_config()
    return obsidian_manager._normalised_excluded_dirs(
        vault_root, cfg.get("vault_exclude_dirs", [])
    )


def is_excluded(rel: str, excluded: set[str]) -> bool:
    """True if vault-relative *rel* sits in (or under) any excluded dir."""
    return any(rel == e or rel.startswith(e + "/") for e in excluded)


def build_name_index(
    vault_root: Path, excluded: set[str] | None = None
) -> dict[str, list[str]]:
    """Build ``basename.lower() -> [vault-relative path]`` for the vault's images.

    Read-only single ``rglob`` over *vault_root*, restricted to image extensions
    and skipping both the indexer's reserved excluded dirs (``.git`` etc.) **and**
    the user's configured ``vault_exclude_dirs`` (*excluded*; computed lazily when
    omitted) — byte-for-byte the same skip set the loader applies, so a bare name
    never resolves to an image the indexer would not have described. The map
    covers the **whole** vault because Obsidian
    resolves a bare ``![[img.png]]`` against the entire vault, and attachments
    live in a central folder outside the scoped sub-folder.
    """
    if excluded is None:
        excluded = excluded_dirs(vault_root)
    name_index: dict[str, list[str]] = {}
    for p in vault_root.rglob("*"):
        if not p.is_file():
            continue
        # Reserved-name dirs are checked on path PARTS (they can appear at any
        # depth); user exclusions are vault-relative prefixes, checked on rel.
        if any(part in OBSIDIAN_EXCLUDED_DIR_NAMES for part in p.parts):
            continue
        if p.suffix.lower() in VAULT_IMAGE_EXTS:
            try:
                rel = p.relative_to(vault_root).as_posix()
            except ValueError:
                continue
            if is_excluded(rel, excluded):
                continue
            name_index.setdefault(nfc(p.name).lower(), []).append(rel)  # B2
    return name_index


def build_link_index(
    vault_root: Path, excluded: set[str] | None = None
) -> dict[str, list[str]]:
    """Build ``basename.lower() -> [vault-relative path]`` over **all** files.

    The broader sibling of ``build_name_index`` (which is image-only): wikilink
    hygiene (``hygiene.link_notes``) needs to know whether a ``[[note]]`` /
    ``[[doc.pdf]]`` target resolves to *any* vault file, not just an image, so
    this indexes every file's basename. Same read-only single ``rglob`` + the
    same reserved-dir / user-exclusion skip set as ``build_name_index``, so a
    link is only ever called broken when no file with that basename exists in a
    folder the indexer would actually look at.
    """
    if excluded is None:
        excluded = excluded_dirs(vault_root)
    link_index: dict[str, list[str]] = {}
    for p in vault_root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in OBSIDIAN_EXCLUDED_DIR_NAMES for part in p.parts):
            continue
        try:
            rel = p.relative_to(vault_root).as_posix()
        except ValueError:
            continue
        if is_excluded(rel, excluded):
            continue
        link_index.setdefault(nfc(p.name).lower(), []).append(rel)  # B2
    return link_index


def build_file_index(
    vault_root: Path, excluded: set[str] | None = None
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Build the (name_index, link_index) pair in a **single** ``rglob`` pass.

    Perf: ``build_name_index`` (images only) and ``build_link_index`` (all files)
    are always needed together by ``build_plan`` / ``analyze_one`` and previously
    did **two** independent whole-vault walks. Because ``link_index`` is a strict
    superset of ``name_index`` (all files vs. image files, identical skip set),
    one walk can populate both — halving the directory I/O of every plan and every
    single-note re-analyze. This function is intentionally byte-for-byte
    equivalent in *output* to calling the two builders separately: it applies the
    exact same reserved-dir / user-exclusion filtering, appends to ``link_index``
    for every file, and additionally to ``name_index`` when the suffix is an image
    ext — so no consumer can observe a different resolution than before. Iteration
    order over one ``rglob`` matches a single builder's, so the per-basename path
    lists are ordered identically to the old two-walk path (the walk order was
    already shared). Safe because it only removes a redundant second traversal;
    it does not change *what* is indexed.
    """
    if excluded is None:
        excluded = excluded_dirs(vault_root)
    name_index: dict[str, list[str]] = {}
    link_index: dict[str, list[str]] = {}
    for p in vault_root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in OBSIDIAN_EXCLUDED_DIR_NAMES for part in p.parts):
            continue
        try:
            rel = p.relative_to(vault_root).as_posix()
        except ValueError:
            continue
        if is_excluded(rel, excluded):
            continue
        # B2 (2026-07-13): NFC-normalize the basename key so an NFD on-disk name
        # matches an NFC link target (parity with rag.vault's name_index and
        # _resolve_md_attachment, which now NFC-normalize the lookup target).
        name = nfc(p.name).lower()
        link_index.setdefault(name, []).append(rel)
        if p.suffix.lower() in VAULT_IMAGE_EXTS:
            name_index.setdefault(name, []).append(rel)
    return name_index, link_index


# Cached indexes are keyed by resolved vault root and exclusions, bounded to two
# entries, and shared read-only. File-set-changing writes invalidate explicitly;
# external changes are observed on the next requested refresh. Builds happen
# outside the mutex; cache mutation alone holds it.
_INDEX_CACHE_MAX = 2
_index_cache_lock = threading.Lock()
_index_cache: OrderedDict = OrderedDict()


def _index_cache_key(vault_root: Path, excluded: set[str]) -> tuple:
    # Resolve the path so ".", symlinks and trailing slashes don't mint distinct
    # entries for the same vault; fold the exclusion set in so a config change to
    # vault_exclude_dirs is a natural cache miss (different key → fresh walk).
    try:
        root = str(Path(vault_root).resolve())
    except OSError:
        root = str(vault_root)
    return (root, frozenset(excluded))


def get_file_index(
    vault_root: Path,
    excluded: set[str] | None = None,
    *,
    refresh: bool = False,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Cached accessor for the (name_index, link_index) pair (see cache notes above).

    *refresh=True* forces a fresh single-pass walk and repopulates the entry —
    used by ``build_plan`` so the explicit re-scan action never serves a stale
    index. *refresh=False* (the default, used by ``analyze_one`` and the LLM-action
    routes) returns the cached pair when present, else builds and stores it. The
    returned dicts are shared, read-only references (never mutate them).
    """
    if excluded is None:
        excluded = excluded_dirs(vault_root)
    key = _index_cache_key(vault_root, excluded)
    if not refresh:
        with _index_cache_lock:
            cached = _index_cache.get(key)
            if cached is not None:
                _index_cache.move_to_end(key)   # most-recently-used
        if cached is not None:
            return cached
    # Build outside the lock: the walk is slow and pure; two concurrent misses
    # racing to build is harmless (idempotent) and far cheaper than serializing
    # every walk behind the lock.
    pair = build_file_index(vault_root, excluded)
    with _index_cache_lock:
        _index_cache.pop(key, None)
        _index_cache[key] = pair
        while len(_index_cache) > _INDEX_CACHE_MAX:
            _index_cache.popitem(last=False)   # evict LRU: costs a rebuild
    return pair


def invalidate_index_cache(vault_root: Path | None = None) -> None:
    """Drop one vault's indexes, or all indexes after a vault switch.

    The next file-resolution request rebuilds the invalidated entry lazily.
    """
    with _index_cache_lock:
        if vault_root is None:
            _index_cache.clear()
            return
        try:
            root = str(Path(vault_root).resolve())
        except OSError:
            root = str(vault_root)
        for k in [k for k in _index_cache if k[0] == root]:
            del _index_cache[k]
