"""Resolve a note's embedded/linked PDFs → their cached extracted text (request c).

The indexer already extracts every vault PDF's text once and caches it at
``obsidian_cache/pdf_cache/<vault_key>/<sha256>.txt`` (whole-file) or
``…/<sha256>-pSSSSS-EEEEE.txt`` (per page range). This module reuses that cache —
via the indexer's own ``ObsidianVaultManager._read_pdf_text`` (cache-first, with a
bounded fresh extract fallback) — so the PDF-summary action never re-extracts an
already-indexed PDF.

It is the **sole** chokepoint for the PDF reuse, mirroring how ``cache.py`` is the
chokepoint for image-description reuse. Read-only: it never writes the vault and
never writes the PDF cache (the fallback extract path inside ``_read_pdf_text``
caches, but that is the indexer's own behaviour, not a refactor write).

PDF embeds are resolved with the whole-vault ``build_link_index`` (all files, not
just images) since attachments live in a central folder and Obsidian resolves a
bare ``[[doc.pdf]]`` vault-wide.
"""
from __future__ import annotations

import os
from pathlib import Path

from rag.vault import obsidian_manager

from refactor.resolver import scan_embeds

# Bounded fresh-extract budget for the cache-miss fallback; llm_edit caps the text
# further before sending it to the model, so this only bounds a cold extraction.
_PDF_CHAR_BUDGET = 48000


def _has_cached_text(pdf_path: Path, vault_root: Path) -> bool:
    """True if extracted text for this PDF is already cached (no extraction).

    Consults the indexer's persisted signature map (like ``_read_pdf_text``)
    so listing a note's PDF refs never re-hashes an unchanged large PDF.
    """
    try:
        rel = pdf_path.relative_to(vault_root).as_posix()
    except ValueError:
        rel = None
    try:
        sig = obsidian_manager._pdf_file_signature(
            pdf_path, obsidian_manager._persisted_pdf_signatures(), rel
        )
    except OSError:
        return False
    cache_file = obsidian_manager._pdf_cache_file(vault_root, sig)
    legacy = obsidian_manager._legacy_pdf_cache_file(vault_root, sig)
    if cache_file.exists() or legacy.exists():
        return True
    digest = str(sig.get("sha256", ""))
    try:
        return bool(digest) and any(cache_file.parent.glob(f"{digest}-p*.txt"))
    except OSError:
        return False


def list_pdf_refs(note_text: str, note_path: Path, vault_root: Path,
                  link_index: dict[str, list[str]]) -> list[dict]:
    """Return the note's resolvable PDF embeds/links (document order, deduped).

    Each item: ``{raw, target, rel_path, line, cached}``. Only ``.pdf`` targets
    that resolve to a file under the vault are returned; an unresolved or
    non-PDF embed is skipped. ``cached`` reports whether extracted text already
    exists (so the UI can warn that a summary may trigger a cold extraction).
    """
    out: list[dict] = []
    seen: set[str] = set()
    for occ in scan_embeds(note_text, note_path, vault_root, link_index):
        rel_path = occ.get("rel_path") or ""
        ext = os.path.splitext(rel_path or occ.get("target", ""))[1].lower()
        if ext != ".pdf" or not rel_path:
            continue
        if rel_path in seen:
            continue
        seen.add(rel_path)
        pdf_path = vault_root / rel_path
        if not pdf_path.is_file():
            continue
        out.append({
            "raw": occ.get("raw", ""),
            "target": occ.get("target", ""),
            "rel_path": rel_path,
            "line": occ.get("line", 0),
            "cached": _has_cached_text(pdf_path, vault_root),
        })
    return out


def get_pdf_text(pdf_rel: str, vault_root: Path) -> tuple[str, bool]:
    """Return ``(text, truncated)`` for the PDF at *pdf_rel* (cache-first).

    Reuses the indexer's ``_read_pdf_text`` (cache hit → returned verbatim;
    miss → a bounded fresh extract). Raises ``OSError`` on an unreadable PDF —
    the caller maps it to a clean error.
    """
    text, truncated = obsidian_manager._read_pdf_text(
        Path(vault_root) / pdf_rel, Path(vault_root), _PDF_CHAR_BUDGET)
    return text or "", bool(truncated)
