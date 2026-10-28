"""Forgiving BibTeX parser for _master.bib.

Uses bibtexparser to load the master bib file, extracting
`title`, `year`, `author`, and `keywords` fields.
"""

from __future__ import annotations

import re
import threading
import unicodedata
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
    # Normalised DOI (lowercased, ``https://doi.org/`` / ``doi:`` prefixes
    # stripped) — additive 2026-08-05, feeds the near-duplicate identifier
    # tier (two entries sharing a DOI = record-level duplicate candidate).
    doi: str | None = None
    # Raw ``"Last, First"`` editors — additive 2026-09-23, so the renamer can
    # say "editors only" about an edited book instead of calling it unmapped
    # (CLI proof run, finding 7a). Nothing names a file after an editor.
    editors: list[str] = field(default_factory=list)
    corporate_authors: list[str] = field(default_factory=list)


def normalise_doi(raw: str) -> str | None:
    """Canonicalise a DOI string: strip URL/``doi:`` prefixes, lowercase.

    Returns ``None`` for anything that does not look like a ``10.xxxx/...``
    DOI after stripping — a malformed field must not create a spurious
    identifier-match key.

    Control and format characters are dropped anywhere in the string
    (2026-08-19): PDF text extraction leaves them INSIDE captured
    identifiers — measured live, BMJ page-1 DOIs came out as
    ``10.1136/bmj.l4378\\x08`` (a literal backspace, which ``\\s`` does not
    cover, so the regex capture keeps it) — and a poisoned key silently
    fails every join forever while printing identically in any log.
    """
    s = "".join(
        c for c in (raw or "") if unicodedata.category(c)[0] != "C").strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/",
                   "http://dx.doi.org/", "doi:"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):].strip()
            break
    s = s.lower().rstrip(".,;")
    return s if s.startswith("10.") and "/" in s else None


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

    return parse_bib_text(text)


def parse_bib_text(text: str) -> list[BibEntry]:
    """Parse an already captured bibliography without reopening its path."""
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
                corporate_authors=[_strip_braces(part) for part in
                    re.split(r"\s+and\s+", entry.get("author", ""))
                    if part.strip().startswith("{") and part.strip().endswith("}")],
                keywords=keywords,
                doi=normalise_doi(_strip_braces(entry.get("doi", ""))),
                editors=_split_authors(entry.get("editor", "")),
            )
        )

    return entries


#: Stat-keyed memo behind :func:`parse_bib_keys`. Module-scoped and tiny (one
#: frozenset of citation keys per bib file), stdlib-only, so the vendored tree's
#: "depends on audit/config.py alone" rule is untouched.
_KEYS_MEMO: dict[str, tuple[tuple, frozenset]] = {}
_KEYS_MEMO_LOCK = threading.Lock()


def parse_bib_keys(path: Path) -> frozenset[str]:
    """The bib's citation keys, memoised on the file's ``(size, mtime_ns)``.

    # PERF (2026-09-02): a full bib parse per validation.
    #   Before:    `POST /api/audit/mapping` re-parsed the WHOLE bibliography
    #              on every triage click, only to test one citation key for
    #              membership. Measured on a 6141-entry / 7.2 MB `_master.bib`
    #              (the maintainer's): 3900 ms per request, on a Waitress
    #              worker, for a set-membership test. The private maintenance
    #              renamer does the same thing per rename.
    #   Change:    a frozenset of keys memoised on the file's
    #              (size, mtime_ns). Measured 3900 ms -> 0.001 ms on a hit.
    #   Safe because:
    #              the answer is derived purely from that file, and an edit
    #              moves both size and mtime_ns — so the live-bib rejection
    #              guarantee stands: a key removed from the bibliography is
    #              rejected on the next click without a rescan, exactly as
    #              before. A `frozenset` is returned, not the parsed entry
    #              list, so no caller can mutate shared state.
    #   Preserves: "the live-bib rejection guarantee stays because mtime is the
    #              key" — and `parse_bib` itself is untouched, so every other
    #              consumer (the scan, which needs the entries) is unchanged.
    #   Lock/thread contract:
    #              a short module lock around the dict only; the parse runs
    #              OUTSIDE it, so two concurrent misses may both parse (a
    #              wasted parse, never a wrong answer or a blocked thread).
    #   Pinned by: test_audit.py::TestBibKeyMemo
    """
    try:
        st = path.stat()
        sig = (str(path), st.st_size, st.st_mtime_ns)
    except OSError:
        return frozenset()
    with _KEYS_MEMO_LOCK:
        hit = _KEYS_MEMO.get(sig[0])
        if hit is not None and hit[0] == sig:
            return hit[1]
    keys = frozenset(e.citation_key for e in parse_bib(path))
    with _KEYS_MEMO_LOCK:
        _KEYS_MEMO[sig[0]] = (sig, keys)
    return keys


def index_by_key(entries: list[BibEntry]) -> dict[str, BibEntry]:
    """Build a ``{citation_key: BibEntry}`` lookup; later duplicates win."""
    return {e.citation_key: e for e in entries}


def iter_entries(path: Path) -> Iterator[BibEntry]:
    """Stream the parsed entries (convenience wrapper over :func:`parse_bib`)."""
    yield from parse_bib(path)


def institution_acronym(name: str) -> str | None:
    """Use a recorded acronym, or the explicitly supported WHO identity."""
    cleaned = name.strip().strip("{}").strip()
    if cleaned.casefold() in {"world health organization", "who"}:
        return "WHO"
    if "," in cleaned:
        return None
    match = re.fullmatch(r".+\s+\(([A-Z][A-Z0-9]{1,11})\)", cleaned)
    if match:
        return match[1]
    return None


def naming_author(entry) -> str | None:
    """An individual first; otherwise an evidenced institutional acronym."""
    authors = list(entry.authors or [])
    corporate = set(getattr(entry, "corporate_authors", []) or [])
    individuals = [author for author in authors
                   if author not in corporate and not institution_acronym(author)]
    if individuals:
        return individuals[0]
    return next((acronym for author in authors
                 if (acronym := institution_acronym(author))), None)
