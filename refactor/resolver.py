"""Embed → real-file resolution, reusing the indexer's Obsidian resolver.

§13 of the design verified two reuse points, both exercised here:

* ``obsidian_manager._resolve_md_attachment(...)`` is the indexer's
  Obsidian-shortest-path resolver and is cleanly callable on the singleton
  outside the chat/index path (``rag/vault.py``). This module is the **sole**
  chokepoint for that private-method call.
* The ``name_index`` it consumes (``basename.lower() -> [vault-relative paths]``)
  is a plain dict we rebuild here with a read-only ``rglob``, mirroring the
  loader's construction (``rag/vault.py`` MD-attachment branch).

Unlike the indexer's ``_extract_md_attachments`` (which returns a deduped list),
``scan_embeds`` preserves **every textual occurrence** with its line number and
character span so the planner can inline an extracted-text callout immediately
beneath each embed.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from urllib.parse import unquote

from core.config import load_config
from core.constants import OBSIDIAN_EXCLUDED_DIR_NAMES, VAULT_IMAGE_EXTS
from rag.vault import (
    _INLINE_LINK_RE,
    _NON_ATTACHMENT_SCHEMES,
    _OBSIDIAN_WIKILINK_RE,
    obsidian_manager,
)


def excluded_dirs(vault_root: Path) -> set[str]:
    """User-configured ``vault_exclude_dirs``, normalized to vault-relative posix.

    Reuses the indexer's own normalizer so the analyzer sees **exactly** the set
    the indexer's ``_should_skip_path`` applies. Without this the analyzer would
    resolve embeds to — and analyze notes inside — folders the user deliberately
    excluded from indexing, producing spurious "not extracted" rows (the indexer
    never described those images) and, crucially for Phase 2, would later target
    excluded folders for writes.
    """
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
    covers the **whole** vault (not just the refactor scope) because Obsidian
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
            name_index.setdefault(p.name.lower(), []).append(rel)
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
        link_index.setdefault(p.name.lower(), []).append(rel)
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
        name = p.name.lower()
        link_index.setdefault(name, []).append(rel)
        if p.suffix.lower() in VAULT_IMAGE_EXTS:
            name_index.setdefault(name, []).append(rel)
    return name_index, link_index


# --- Cached whole-vault file index -----------------------------------------
# The (name_index, link_index) pair is a pure function of the vault's file set +
# the user's exclusion configuration. Rebuilding it costs a full-vault walk, and
# the per-image OCR-inclusion panel fires ``analyze_one`` (which needs it) on a
# DEBOUNCED per-toggle basis — so a user flipping a few checkboxes on one note
# would otherwise trigger several full-vault walks for a single-note refresh.
#
# We cache the pair keyed by (resolved vault path, exclusion set). Safety rests
# on three facts, all documented at the call sites:
#   1. ``build_plan`` (the explicit "Run plan" action) ALWAYS refreshes the cache
#      (refresh=True) — a user asking to re-scan gets a fresh walk, and that walk
#      repopulates the entry that the subsequent debounced ``analyze_one`` bursts
#      reuse. So the cache is warmed by the very action that precedes the toggles.
#   2. Toggling an image's include checkbox does NOT change the vault file set, so
#      the cached index stays correct across a toggle session.
#   3. File-set-changing Phase-2 writes (archive moves an image out + adds a
#      thumbnail; restore reverses it) invalidate the cache explicitly at the
#      route layer. Content-only writes (apply/normalize) don't touch the file
#      set, so they need no invalidation.
# Residual staleness window: a file added/removed in Obsidian *externally* between
# a plan run and a later ``analyze_one`` without a re-plan — self-heals on the
# next plan (which refreshes) and never mis-resolves in a way the old uncached
# path wouldn't also produce for a file added mid-session. The cached dicts MUST
# be treated as read-only by consumers (they are shared references); every
# consumer (``scan_embeds`` / ``_resolve_md_attachment`` / ``hygiene.link_notes``)
# only ever ``.get()``s them, so this holds.
_index_cache_lock = threading.Lock()
_index_cache: dict[tuple, tuple[dict[str, list[str]], dict[str, list[str]]]] = {}


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
            return cached
    # Build outside the lock: the walk is slow and pure; two concurrent misses
    # racing to build is harmless (idempotent) and far cheaper than serializing
    # every walk behind the lock.
    pair = build_file_index(vault_root, excluded)
    with _index_cache_lock:
        _index_cache[key] = pair
    return pair


def invalidate_index_cache(vault_root: Path | None = None) -> None:
    """Drop cached file indexes so the next access rebuilds from disk.

    Called after a Phase-2 write that changes the vault file set (archive/restore).
    With *vault_root* given, only that vault's entries are dropped (keyed by the
    resolved path prefix); with None, the whole cache is cleared (e.g. on vault
    switch). Clearing is cheap — the cost is deferred to the next plan/re-analyze,
    which is exactly when a fresh view is wanted.

    Also drops the image-digest memo (``cache.clear_digest_memo``, Track 5.1):
    the memo is self-validating via ``(size, mtime_ns)`` so this is a memory
    bound, not a correctness requirement — archive/restore/vault-switch are the
    natural points where remembered digests stop being useful. Local import:
    ``cache`` imports ``rag.vault`` (not this module), so the edge is one-way,
    but keeping it lazy avoids ordering surprises at package import time.
    """
    from refactor.cache import clear_digest_memo
    clear_digest_memo()
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


def _normalize_target(raw_target: str) -> str:
    """Mirror ``_extract_md_attachments._accept``: trim, percent-decode, drop
    anchor/block suffixes; return "" for empty or non-filesystem schemes."""
    target = raw_target.strip()
    if not target:
        return ""
    target = unquote(target)
    target = target.split("#", 1)[0].split("^", 1)[0].strip()
    if not target:
        return ""
    lowered = target.lower()
    if any(lowered.startswith(sch) for sch in _NON_ATTACHMENT_SCHEMES):
        return ""
    return target


def _line_of(text: str, pos: int) -> int:
    """1-based line number of character offset *pos*."""
    return text.count("\n", 0, pos) + 1


def link_target_basenames(text: str) -> frozenset:
    """Lowercased basenames of every link/embed target in *text* (both shapes).

    The archive reference-sweep index (Track 5.3, ``refactor/archive.py``) stores
    this per note so the move-safety gate can locate candidate referencers
    without re-reading the whole vault per archive click. Extraction mirrors
    ``scan_embeds`` exactly — same two regexes, same ``_normalize_target``
    (trim/percent-decode/anchor-strip) — which is what makes the candidate set a
    provable superset of anything ``scan_embeds`` could later resolve: a
    resolved occurrence's target basename is always in this set (resolution
    never changes a target's basename; the bare-name index lookup keys on it).
    """
    out: set[str] = set()
    for m in _OBSIDIAN_WIKILINK_RE.finditer(text):
        target = _normalize_target(m.group(1))
        if target:
            out.add(os.path.basename(target).lower())
    for m in _INLINE_LINK_RE.finditer(text):
        target = _normalize_target(m.group(1).split(" ", 1)[0])
        if target:
            out.add(os.path.basename(target).lower())
    return frozenset(out)


def scan_embeds(
    note_text: str,
    note_path: Path,
    vault_root: Path,
    name_index: dict[str, list[str]],
) -> list[dict]:
    """Return every embed occurrence in *note_text*, in document order.

    Each item: ``{raw, target, rel_path, line, start, end, is_image}`` where
    ``rel_path`` is the resolved vault-relative path ("" if unresolved) and
    ``is_image`` reflects the resolved (or, if unresolved, the link) extension.
    Both ``![[wikilink]]`` and ``![](inline)`` shapes are covered, deduped by
    span so a link matched by both regexes is reported once.
    """
    occurrences: list[dict] = []
    seen_spans: set[tuple[int, int]] = set()

    def _add(raw_target: str, match) -> None:
        span = match.span()
        if span in seen_spans:
            return
        target = _normalize_target(raw_target)
        if not target:
            return
        seen_spans.add(span)
        rel_path = obsidian_manager._resolve_md_attachment(
            target, note_path, vault_root, name_index=name_index
        )
        ext = os.path.splitext(rel_path or target)[1].lower()
        # The inline-link regex matches "[](x)", not the leading "!" of an image
        # embed (the wikilink regex already captures its optional "!"). Restore
        # it for display fidelity by peeking at the char before the match, so the
        # UI shows "![](7A27.png)" rather than "[](7A27.png)".
        raw = match.group(0)
        if not raw.startswith("!") and span[0] > 0 and note_text[span[0] - 1] == "!":
            raw = "!" + raw
        occurrences.append({
            "raw": raw,
            "target": target,
            "rel_path": rel_path,
            "line": _line_of(note_text, span[0]),
            "start": span[0],
            "end": span[1],
            "is_image": ext in VAULT_IMAGE_EXTS,
        })

    for m in _OBSIDIAN_WIKILINK_RE.finditer(note_text):
        _add(m.group(1), m)
    for m in _INLINE_LINK_RE.finditer(note_text):
        # Strip an optional title segment: [label](url "title").
        _add(m.group(1).split(" ", 1)[0], m)

    occurrences.sort(key=lambda o: o["start"])
    return occurrences
