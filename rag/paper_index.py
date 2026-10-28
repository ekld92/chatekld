"""Paper labels for RAG: the audit's PDF↔bib↔note join, persisted per vault.

The indexer stamps a chunk with nothing but its vault-relative ``source``, so the
model sees ``Z_attachments/biblio_articles/smith_2020.pdf`` and the deck
generator can only turn a filename into a citation when the filename happens to
BE a citation key. The Library Audit already knows which record every mapped PDF
and reference note belongs to; this module writes that join to one small JSON
file per vault after each completed scan, so retrieval-side code can label a
chunk ("Smith et al., 2020, Title [smithTitle2020]") and the citation pass can
cite a PDF by its key.

Three rules shape it:

* **Nothing here changes retrieval.** Labels are applied to COPIES of the
  post-rerank nodes (``rag/engine.py``); the index, the ranking and the chunk
  ids are untouched, and the file is regenerable (written atomically, no fsync).
* **``rag`` never imports the audit manager.** :func:`build` takes the
  ``(Inventory, Settings)`` pair as data; the listener that calls it lives in
  ``api/routes/audit.py``. Display helpers are imported lazily at build time so
  the chat path, which only ever calls :func:`load`, pulls in nothing extra.
* **A basename is only a key when it is unambiguous.** The same filename exists
  in other vault folders (a 2022 thesis folder holds copies of biblio PDFs), and
  a bare name is how a model tends to write a source, so a basename mapping to
  two keys — or occurring twice anywhere in the vault — is dropped rather than
  guessed.

The labels reflect the LAST scan: a file renamed or imported since is simply
unlabelled until the next one.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

VERSION = 1

#: Title display bound — the ``deckgen.template._MAX_TITLE_CHARS`` precedent. A
#: label rides every labelled chunk into the prompt, so it stays short.
_MAX_TITLE_CHARS = 200


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _fold(name: str) -> str:
    """Basename lookup key: NFC + casefold (APFS filenames arrive NFD)."""
    return _nfc(name).casefold()


def norm_source(rel: str) -> str:
    """Canonical form of a vault-relative path: NFC, forward slashes."""
    return _nfc(str(rel).replace("\\", "/"))


def index_dir() -> str:
    """``BASE_DIR/rag/paper_index`` — resolved per call so test overrides hold."""
    from core import constants
    return os.path.join(constants.BASE_DIR, "rag", "paper_index")


def _resolved_root(vault_root: Any) -> Optional[Path]:
    try:
        return Path(str(vault_root)).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def index_path(vault_root: Any) -> Optional[str]:
    """Where the index for *vault_root* lives, or ``None`` if unresolvable."""
    from rag.storage_io import vault_key

    root = _resolved_root(vault_root)
    if root is None:
        return None
    return os.path.join(index_dir(), f"{vault_key(root)}.json")


def paper_label(title: Optional[str], authors: Optional[str],
                year: Optional[str]) -> str:
    """``"Smith et al., 2020, Title"`` from already-cleaned parts; missing parts skipped."""
    parts = [p.strip() for p in (authors, year, title) if p and p.strip()]
    return ", ".join(parts)


def _rel_under(path: Any, root: Path) -> Optional[str]:
    """*path* relative to *root* as a canonical posix string, or ``None``."""
    p = Path(str(path))
    try:
        rel = p.relative_to(root)
    except ValueError:
        try:
            rel = Path(os.path.realpath(str(p))).relative_to(root)
        except (ValueError, OSError):
            return None
    return norm_source(rel.as_posix())


def _display(rec: Any) -> dict:
    """Cleaned ``{title, authors, year, label}`` for one audit ``Record``."""
    from widget.snapshot import format_authors
    from widget.textclean import clean_display

    bib = getattr(rec, "bib_entry", None)
    zot = getattr(rec, "zotero_item", None)
    raw_title = (getattr(bib, "title", None) if bib else None) or (
        getattr(zot, "title", None) if zot else None)
    title = clean_display(raw_title) if raw_title else None
    if title and len(title) > _MAX_TITLE_CHARS:
        title = title[: _MAX_TITLE_CHARS - 1].rstrip() + "…"
    raw_authors = list(getattr(bib, "authors", None) or []) if bib else []
    authors = format_authors(
        (clean_display(a) or a) for a in raw_authors) if raw_authors else None
    year = (str(getattr(bib, "year", "") or "").strip() or None) if bib else None
    return {
        "title": title,
        "authors": authors,
        "year": year,
        "label": paper_label(title, authors, year),
    }


def build(inv: Any, settings: Any, *,
          ambiguous_basenames: Iterable[str] = (),
          built_at: Optional[str] = None) -> dict:
    """Build the index payload from one ``(Inventory, Settings)`` pair. Pure.

    ``sources`` covers every mapped PDF and every reference note, keyed by
    vault-relative NFC posix path. A path claimed by two keys is dropped (the
    join is ambiguous about it). ``basenames`` keeps a folded basename only when
    it names exactly ONE key here and is not in *ambiguous_basenames* (the
    vault-wide duplicate-name set, ``_audit_tagwrite.ambiguous_pdf_basenames``).
    """
    root = _resolved_root(settings.vault_root) or Path(str(settings.vault_root))
    ambiguous = {_fold(b) for b in ambiguous_basenames}
    sources: dict[str, str] = {}
    conflicted: set[str] = set()
    by_base: dict[str, set[str]] = {}
    papers: dict[str, dict] = {}

    for key, rec in sorted((getattr(inv, "records", None) or {}).items()):
        paths = list(getattr(rec, "pdf_paths", None) or [])
        note = getattr(rec, "obsidian_note", None)
        if note is not None and getattr(note, "path", None):
            paths.append(note.path)
        rels = [r for r in (_rel_under(p, root) for p in paths) if r]
        if not rels:
            continue
        papers[key] = _display(rec)
        for rel in rels:
            prior = sources.get(rel)
            if prior is not None and prior != key:
                conflicted.add(rel)
            sources.setdefault(rel, key)
            by_base.setdefault(_fold(rel.rsplit("/", 1)[-1]), set()).add(key)
    for rel in conflicted:
        sources.pop(rel, None)

    basenames = {
        base: next(iter(keys))
        for base, keys in sorted(by_base.items())
        if len(keys) == 1 and base not in ambiguous
    }
    return {
        "version": VERSION,
        "vault": str(root),
        "built_at": built_at or datetime.now(timezone.utc).isoformat(),
        "sources": sources,
        "basenames": basenames,
        "papers": papers,
    }


def write(data: dict) -> Optional[str]:
    """Persist *data* atomically (no fsync — regenerable) for its own vault."""
    from core.utils import write_text_atomic

    path = index_path(data.get("vault", ""))
    if path is None:
        return None
    write_text_atomic(path, json.dumps(data, ensure_ascii=False), fsync=False)
    return path


class PaperIndex:
    """Read-side view of one loaded index. Lookups never raise."""

    def __init__(self, data: dict) -> None:
        self.vault: str = data["vault"]
        self.built_at: str = data.get("built_at", "")
        self.sources: dict[str, str] = data["sources"]
        self.basenames: dict[str, str] = data["basenames"]
        self.papers: dict[str, dict] = data["papers"]

    def key_for_source(self, rel: Any) -> Optional[str]:
        """Exact vault-relative path → key (the chat path's only lookup)."""
        if not rel:
            return None
        return self.sources.get(norm_source(str(rel)))

    def resolve(self, name: Any) -> Optional[str]:
        """A source name as a model writes it: full path first, then basename."""
        if not name:
            return None
        text = norm_source(str(name)).strip()
        key = self.sources.get(text)
        if key:
            return key
        return self.basenames.get(_fold(text.rsplit("/", 1)[-1]))

    def label(self, key: Optional[str]) -> Optional[str]:
        paper = self.papers.get(key or "")
        label = (paper or {}).get("label")
        return label if isinstance(label, str) and label else None

    def paper_for_source(self, rel: Any) -> Optional[dict]:
        """``{key, label}`` for an indexed chunk's ``source``, or ``None``."""
        key = self.key_for_source(rel)
        label = self.label(key) if key else None
        if not key or not label:
            return None
        return {"key": key, "label": label}


#: The ``metadata["paper"]`` string ``rag.engine._label_nodes`` writes:
#: ``"<label> [<key>]"``. Citation keys carry no spaces or brackets.
_PAPER_FIELD_RE = re.compile(r"^(?P<label>.+) \[(?P<key>[^\[\]\s]+)\]$", re.S)


def paper_field(metadata: Any) -> Optional[dict]:
    """``{key, label}`` parsed back from a labelled chunk's metadata, or ``None``.

    The additive ``paper`` field of the agent's ``vault_search`` results and of
    the chat ``retrieval`` frame; one parse so the two surfaces agree.
    """
    raw = metadata.get("paper") if isinstance(metadata, dict) else None
    if not isinstance(raw, str):
        return None
    m = _PAPER_FIELD_RE.match(raw.strip())
    if not m:
        return None
    label = m.group("label").strip()
    if not label:
        return None
    return {"key": m.group("key"), "label": label}


_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[tuple, Optional[PaperIndex]]] = {}


def _valid(data: Any, vault: str) -> bool:
    return (
        isinstance(data, dict)
        and data.get("version") == VERSION
        and data.get("vault") == vault
        and isinstance(data.get("sources"), dict)
        and isinstance(data.get("basenames"), dict)
        and isinstance(data.get("papers"), dict)
    )


def load(vault_root: Any) -> Optional[PaperIndex]:
    """The index for *vault_root*, or ``None`` when missing/corrupt/foreign/old.

    Cached on the file's ``(path, size, mtime_ns)`` so every chat turn after the
    first is a ``stat``; a rewrite after the next scan invalidates it.
    """
    if not vault_root:
        return None
    root = _resolved_root(vault_root)
    path = index_path(vault_root)
    if root is None or path is None:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    sig = (path, st.st_size, st.st_mtime_ns)
    with _CACHE_LOCK:
        hit = _CACHE.get(path)
        if hit is not None and hit[0] == sig:
            return hit[1]
    result: Optional[PaperIndex] = None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if _valid(data, str(root)):
            result = PaperIndex(data)
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("Paper index %s is unreadable; labels disabled.", path)
        result = None
    with _CACHE_LOCK:
        _CACHE[path] = (sig, result)
    return result


def clear_cache() -> None:
    """Drop the parsed-file cache (tests)."""
    with _CACHE_LOCK:
        _CACHE.clear()
