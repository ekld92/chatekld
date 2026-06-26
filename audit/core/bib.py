"""Forgiving BibTeX parser for _master.bib.

Uses bibtexparser to load the master bib file, extracting
`title`, `year`, `author`, and `keywords` fields.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import bibtexparser
from bibtexparser.bparser import BibTexParser


@dataclass
class BibEntry:
    """One parsed ``_master.bib`` record, keyed by its Better BibTeX key.

    ``citation_key`` is the inventory's primary join key. ``authors`` are
    raw ``"Last, First"`` strings in author order (the bridge normalizes the
    first author's surname for filename matching). ``keywords`` is a set
    because the BBT-exported keywords equal the Zotero parent's tags, and the
    audit compares them as unordered sets.
    """

    citation_key: str
    entry_type: str
    title: str | None = None
    year: str | None = None
    authors: list[str] = field(default_factory=list)
    keywords: set[str] = field(default_factory=set)


def _strip_braces(s: str) -> str:
    """Peel BibTeX's protective ``{...}`` / ``"..."`` wrappers off a value.

    BibTeX field values are often brace- or quote-wrapped (sometimes nested,
    e.g. ``{{Title}}``) to protect casing; the loop strips every balanced
    outer layer so the stored value is the bare text.
    """
    s = s.strip()
    while len(s) >= 2 and s[0] == "{" and s[-1] == "}":
        s = s[1:-1].strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1].strip()
    return s


def _split_authors(raw: str) -> list[str]:
    """Split a BibTeX ``author`` field on the ``and`` separator, in order.

    BibTeX joins authors with the literal word ``and``; the surrounding
    whitespace match avoids splitting names that merely contain "and". Empty
    parts are dropped and each name is brace-stripped.
    """
    parts = re.split(r"\s+and\s+", raw)
    return [_strip_braces(p) for p in parts if p.strip()]


def _split_keywords(raw: str) -> set[str]:
    """Split a keywords field into a normalized set (``#`` prefix dropped).

    Returns a *set* because keyword order is meaningless and the audit
    compares against Zotero tags as unordered sets.
    """
    # BBT exports keywords comma-separated; tolerate `;` too.
    parts = re.split(r"[,;]", raw)
    return {_strip_braces(p).lstrip("#") for p in parts if p.strip()}


def parse_bib(path: Path) -> list[BibEntry]:
    """Parse ``_master.bib`` into a list of :class:`BibEntry`, forgivingly.

    Read-only and total: a missing/unreadable file or a fatal bibtexparser
    error returns ``[]`` rather than raising, so one broken bib never aborts a
    scan. Entries without an ID are dropped. Field names are homogenized to
    lowercase; ``keyword`` is accepted as a fallback for ``keywords``.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    parser = BibTexParser(common_strings=True)
    parser.ignore_nonstandard_types = False
    parser.homogenize_fields = True  # Lowercase field names

    try:
        bib_database = bibtexparser.loads(text, parser=parser)
    except Exception:
        return []

    entries: list[BibEntry] = []
    for entry in bib_database.entries:
        key = entry.get("ID")
        if not key:
            continue
        entry_type = entry.get("ENTRYTYPE", "").lower()
        title = _strip_braces(entry.get("title", "")) or None
        year = _strip_braces(entry.get("year", "")) or None

        authors = _split_authors(entry.get("author", ""))
        keywords = _split_keywords(entry.get("keywords") or entry.get("keyword", ""))

        entries.append(
            BibEntry(
                citation_key=key.strip(),
                entry_type=entry_type,
                title=title,
                year=year,
                authors=authors,
                keywords=keywords,
            )
        )

    return entries


def index_by_key(entries: list[BibEntry]) -> dict[str, BibEntry]:
    """Build a ``{citation_key: BibEntry}`` lookup; later duplicates win."""
    return {e.citation_key: e for e in entries}


def iter_entries(path: Path) -> Iterator[BibEntry]:
    """Stream the parsed entries (convenience wrapper over :func:`parse_bib`)."""
    yield from parse_bib(path)
