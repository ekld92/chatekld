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
