"""Obsidian vault connector (read-only)."""

# Walks the vault's markdown notes and extracts the two dimensions the audit
# cares about: the note's YAML-frontmatter ``tags`` (reconciled against Zotero
# child-note tags by ``reports/note_tag_drift``) and the basenames of every
# ``.pdf`` it links to (wikilink or markdown link). Nothing here ever writes a
# note; the connector only ``read_text``s files.
#
# Two cross-cutting facilities make repeat scans cheap and malformed notes
# visible:
# - a process-wide ``(path, mtime_ns)`` note cache (``_NOTE_CACHE``), so the
#   two independent walks of ``Z_Zotero_Notes`` in one scan (bridge + inventory)
#   and unchanged notes across runs are parsed only once;
# - a drainable, deduped, bounded ``_parse_warnings`` buffer that the audit
#   manager surfaces in the status feed — a note whose YAML fails to parse is
#   skipped, so its tags vanish from the drift report, and the user must be told.

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any, Optional

from ruamel.yaml import YAML

logger = logging.getLogger(__name__)

# Frontmatter-parse warnings collected for the UI.  parse_frontmatter logs
# each failure, but a log line never reaches the Library Audit status feed —
# the manager drains this buffer after the inventory phase so the user sees
# which notes were skipped.  Bounded; deduped on drain.
_parse_warnings: list[str] = []
_parse_warnings_lock = threading.Lock()
_PARSE_WARNINGS_MAX = 100

# Per-call handoff from parse_frontmatter to read_note so the cached entry
# can remember its warning and re-surface it on later scans (a cache hit
# skips the parse, but the underlying file is still malformed).
_local = threading.local()

# (mtime_ns, NoteInfo|None, warning|None) per path.  bridge.py and
# inventory.py both walk the same Z_Zotero_Notes directory within a single
# scan, and the vault is rescanned on every run — caching by mtime makes the
# second walk (and unchanged-note rescans) free.
_NOTE_CACHE: dict[Path, tuple[int, Optional["NoteInfo"], Optional[str]]] = {}
_NOTE_CACHE_MAX = 50_000


def _record_parse_warning(msg: str) -> None:
    """Append a frontmatter-parse warning under the lock, up to the cap.

    Lock-guarded because parsing runs on the scan worker thread while the
    manager may drain concurrently. Silently drops past ``_PARSE_WARNINGS_MAX``
    so a vault full of malformed notes cannot grow the buffer unbounded.
    """
    with _parse_warnings_lock:
        if len(_parse_warnings) < _PARSE_WARNINGS_MAX:
            _parse_warnings.append(msg)


def drain_parse_warnings() -> list[str]:
    """Return-and-clear the collected warnings, deduped, insertion-ordered."""
    with _parse_warnings_lock:
        out = list(dict.fromkeys(_parse_warnings))
        _parse_warnings.clear()
    return out

WIKI_PDF_RE = re.compile(r"\[\[([^\]|#]*\.pdf)(?:\||#)?.*?\]\]", re.IGNORECASE)
MD_PDF_RE = re.compile(r"\[.*?\]\((.*?\.pdf)\)", re.IGNORECASE)
# Allow optional BOM or whitespace before ---
YAML_RE = re.compile(r"^\s*(?:﻿)?---\s*\n(.*?)\n---\s*\n", re.DOTALL)

_yaml = YAML(typ="rt")
_yaml.preserve_quotes = True


@dataclass
class NoteInfo:
    """Parsed view of one markdown note.

    ``tags`` is the normalized YAML ``tags`` list (``#`` stripped);
    ``pdf_links`` is the set of *basenames* of referenced PDFs (the bridge
    matches on filename, not path); ``frontmatter`` is a plain-``dict`` copy
    of the YAML (not the heavier ruamel ``CommentedMap``) so downstream
    consumers can read pointer fields without depending on ruamel types.
    """

    path: Path
    tags: list[str] = field(default_factory=list)
    pdf_links: set[str] = field(default_factory=set)
    has_frontmatter: bool = False
    frontmatter: dict[str, Any] = field(default_factory=dict)


def walk_markdown(root: Path, ignored: frozenset[str]) -> Iterator[Path]:
    """Yield every ``.md`` under ``root`` whose path crosses no ignored dir.

    ``ignored`` is matched against each *path component* (so ``.obsidian``,
    ``.trash`` etc. are pruned wherever they appear in the tree), not just the
    top level. Lazy generator — callers can stop early without walking the
    whole vault.
    """
    for p in root.rglob("*.md"):
        if any(part in ignored for part in p.relative_to(root).parts):
            continue
        yield p


def parse_frontmatter(
    text: str, path: Path | None = None
) -> tuple[dict[str, Any], str] | tuple[None, str]:
    """Split leading ``--- ... ---`` YAML frontmatter from the note body.

    Returns ``(frontmatter_dict, body)`` on success, or ``(None, text)`` when
    there is no frontmatter block, the YAML fails to parse, or it parses to a
    non-mapping. A parse failure is *recorded* two ways: a single-line message
    is stashed on ``_local.last_warning`` (so :func:`read_note` can attach it
    to the cache entry and re-surface it on cache hits) and pushed into the
    drainable ``_parse_warnings`` buffer for the UI. The single-line form is
    used deliberately — a full multi-line ruamel error would swamp the status
    feed. A malformed note is treated as having no frontmatter, so its tags are
    silently absent unless the warning is shown.
    """
    _local.last_warning = None
    m = YAML_RE.match(text)
    if not m:
        return None, text
    try:
        data = _yaml.load(StringIO(m.group(1)))
    except Exception as e:
        loc = f" in {path}" if path else ""
        logger.warning(f"Failed to parse YAML frontmatter{loc}: {e}")
        # Short, single-line form for the UI feed (the full ruamel error
        # spans several lines and would swamp the status list).
        first_line = str(e).splitlines()[0] if str(e) else type(e).__name__
        msg = f"Malformed YAML frontmatter in {path.name if path else '<note>'}: {first_line}"
        _local.last_warning = msg
        _record_parse_warning(msg)
        return None, text
    if not isinstance(data, dict):
        return None, text
    return data, text[m.end() :]


def _extract_tags(fm: dict[str, Any]) -> list[str]:
    """Obsidian YAML tags can be: `tags: foo`, `tags: [a, b]`, or a YAML list."""
    val = fm.get("tags")
    if val is None:
        return []
    if isinstance(val, str):
        return [t.strip().lstrip("#") for t in re.split(r"[,\s]+", val) if t.strip()]
    if isinstance(val, list):
        return [str(t).strip().lstrip("#") for t in val if str(t).strip()]
    return []


def find_pdf_links(text: str) -> set[str]:
    """Return basenames of all .pdf references (wikilinks and markdown links)."""
    found: set[str] = set()
    for m in WIKI_PDF_RE.findall(text):
        found.add(Path(m.strip()).name)
    for m in MD_PDF_RE.findall(text):
        cleaned = m.split("?")[0].split("#")[0]
        found.add(Path(cleaned).name.replace("%20", " ").strip())
    return {f for f in found if f}


def read_note(path: Path) -> NoteInfo | None:
    """Parse one note, served from the ``(path, mtime_ns)`` cache when fresh.

    Returns ``None`` if the file cannot be ``stat``-ed or read. The cache makes
    the second of the two per-scan walks of ``Z_Zotero_Notes`` (bridge then
    inventory) and unchanged notes across runs free; a hit on a note that was
    malformed re-records its stored warning so a never-fixed note doesn't
    silently look healthy on the next scan. The cache is cleared wholesale once
    it reaches ``_NOTE_CACHE_MAX`` rather than evicted entry-by-entry — simple
    and bounded, and a scan re-warms it anyway.
    """
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return None
    cached = _NOTE_CACHE.get(path)
    if cached is not None and cached[0] == mtime_ns:
        _, note, warning = cached
        if warning:
            # The file is still malformed — resurface the warning so a
            # later scan doesn't silently look "fixed".
            _record_parse_warning(warning)
        return note
    note = _read_note_uncached(path)
    if len(_NOTE_CACHE) >= _NOTE_CACHE_MAX:
        _NOTE_CACHE.clear()
    _NOTE_CACHE[path] = (mtime_ns, note, getattr(_local, "last_warning", None))
    return note


def _read_note_uncached(path: Path) -> NoteInfo | None:
    """Read + parse one note from disk (no cache consultation).

    Returns ``None`` for an unreadable / non-UTF-8 file. PDF links are scanned
    from the *whole* note text (frontmatter pointers and inline body links
    both count); tags come only from parsed frontmatter. The frontmatter is
    down-converted to a plain ``dict`` so callers never carry ruamel types.
    """
    _local.last_warning = None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    fm, body = parse_frontmatter(text, path=path)
    note = NoteInfo(path=path)
    if fm is not None:
        note.has_frontmatter = True
        note.tags = _extract_tags(fm)
        # Keep a plain-Python copy of the frontmatter for downstream consumers.
        # ruamel.yaml types (CommentedMap) behave like dict but are heavier.
        try:
            note.frontmatter = dict(fm)
        except Exception as e:
            logger.warning(f"Failed to convert frontmatter dict in {path}: {e}")
            note.frontmatter = {}
    note.pdf_links = find_pdf_links(text)
    return note


def scan_vault(root: Path, ignored: frozenset[str]) -> Iterator[NoteInfo]:
    """Yield a :class:`NoteInfo` for every readable note under ``root``.

    The CLI ``--check obsidian`` path; the engine instead indexes notes by
    stem via its own walks. Unreadable notes are skipped silently.
    """
    for p in walk_markdown(root, ignored):
        n = read_note(p)
        if n is not None:
            yield n
