"""Independent PDF ⇄ record authentication with passage fingerprints (phase 5).

Deterministic and LLM-free. A catalogue record never stands for the PDF: this
module reads the PDF's own pages and reports WHERE the record's DOI, title and
author surnames are printed — page, offset, verbatim excerpt and a fingerprint
of the folded passage — so a reviewer, a model judgment and a later re-check
can all point at the same bytes.

Two traps it is built to name rather than hide:

* A DOI printed only in the reference list is a CITED paper's DOI, not this
  document's — the misleading-cited-reference case. It is reported as
  ``reference_list_only`` and never counts as authentication.
* Same bytes, different metadata: the file hash is reported beside every
  location so a renamed or replaced file cannot inherit an old verdict.

Bounded: at most ``MAX_PAGES`` pages and ``MAX_CHARS`` characters are read
(page order, first pages first), and the report says when it stopped early.
PyMuPDF is imported lazily so importing this module costs nothing.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from audit.core.bib import normalise_doi

MAX_PAGES = 40
MAX_CHARS = 400_000
EXCERPT_CONTEXT = 160
#: Pages that count as the document's own front matter: a DOI or title found
#: only later is treated as a reference-list hit.
FRONT_MATTER_PAGES = 2
MIN_TITLE_CHARS = 12

_WS = re.compile(r"\s+")
#: How close (in characters of the page text) an author surname must be to
#: the title hit for title + author to authenticate (2026-09-17 review, F8).
AUTHOR_TITLE_WINDOW = 1500
_DOI = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.IGNORECASE)


def fold(text: str) -> str:
    """Accent-, case- and punctuation-folded text for matching; whitespace runs
    collapse to one space. Symmetric: applied to the page AND the needle."""
    decomposed = unicodedata.normalize("NFD", text or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    # The SAME character class as _folded_with_map (str.isalnum after NFD and
    # combining-mark removal), so a Greek or other non-ASCII letter in a title
    # survives in the needle as it does in the page (2026-09-17 review, F7).
    return _WS.sub(" ", "".join(c if c.isalnum() else " " for c in stripped)).strip()


def passage_fingerprint(excerpt: str) -> str:
    """sha256 of the FOLDED excerpt: stable across re-extraction whitespace
    differences, distinct for different passages."""
    return hashlib.sha256(fold(excerpt).encode("utf-8")).hexdigest()


@dataclass
class Location:
    kind: str
    value: str
    page: int
    offset: int
    excerpt: str
    fingerprint: str
    page_sha256: str
    in_front_matter: bool

    def to_json(self) -> dict:
        return dict(kind=self.kind, value=self.value, page=self.page, offset=self.offset,
                    excerpt=self.excerpt, passage_fingerprint=self.fingerprint,
                    page_sha256=self.page_sha256, in_front_matter=self.in_front_matter)


@dataclass
class Scan:
    sha256: str
    page_count: int
    pages: list[tuple[int, str]] = field(default_factory=list)
    truncated: bool = False
    error: str | None = None


def scan_pdf(path: Path, *, max_pages: int = MAX_PAGES, max_chars: int = MAX_CHARS) -> Scan:
    """Read page texts in order, bounded. ``error`` is set (and ``pages`` empty)
    when the file cannot be opened; the caller reports ``unavailable``."""
    path = Path(path)
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        return Scan(sha256="", page_count=0, error=f"unreadable file ({type(exc).__name__})")
    try:
        import fitz  # PyMuPDF
    except ImportError:  # pragma: no cover — pinned dependency
        return Scan(sha256=digest, page_count=0, error="PyMuPDF unavailable")
    scan = Scan(sha256=digest, page_count=0)
    total = 0
    try:
        with fitz.open(str(path)) as doc:
            scan.page_count = doc.page_count
            for number, page in enumerate(doc, start=1):
                if number > max_pages or total >= max_chars:
                    scan.truncated = True
                    break
                text = page.get_text() or ""
                if total + len(text) > max_chars:
                    text = text[: max_chars - total]
                    scan.truncated = True
                total += len(text)
                scan.pages.append((number, text))
    except Exception as exc:  # noqa: BLE001 — a corrupt PDF is data, not a crash
        scan.error = f"PDF could not be read ({type(exc).__name__})"
        scan.pages = []
    return scan


def _excerpt(text: str, start: int, end: int) -> str:
    lo, hi = max(0, start - EXCERPT_CONTEXT), min(len(text), end + EXCERPT_CONTEXT)
    return _WS.sub(" ", text[lo:hi]).strip()


def _folded_with_map(text: str) -> tuple[str, list[int]]:
    """Fold *text* character by character so a match in the folded string maps
    back to an offset in the original (needed for the verbatim excerpt)."""
    out: list[str] = []
    positions: list[int] = []
    pending_space = False
    for index, ch in enumerate(text):
        decomposed = unicodedata.normalize("NFD", ch)
        base = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
        for piece in base:
            if piece.isalnum():
                if pending_space and out:
                    out.append(" ")
                    positions.append(index)
                pending_space = False
                out.append(piece)
                positions.append(index)
            else:
                pending_space = True
    return "".join(out), positions


def locate_text(scan: Scan, kind: str, value: str) -> Location | None:
    """First page carrying the folded *value* as a whole-word run. ``None`` when
    the needle is too short to mean anything or is absent."""
    needle = fold(value)
    if len(needle) < (MIN_TITLE_CHARS if kind == "title" else 3):
        return None
    for number, text in scan.pages:
        folded, positions = _folded_with_map(text)
        start = 0
        while True:
            at = folded.find(needle, start)
            if at < 0:
                break
            before_ok = at == 0 or folded[at - 1] == " "
            after_ok = at + len(needle) == len(folded) or folded[at + len(needle)] == " "
            if before_ok and after_ok:
                first = positions[at]
                last = positions[at + len(needle) - 1] + 1
                excerpt = _excerpt(text, first, last)
                return Location(kind, value, number, first, excerpt, passage_fingerprint(excerpt),
                                hashlib.sha256(text.encode("utf-8")).hexdigest(), number <= FRONT_MATTER_PAGES)
            start = at + 1
    return None


def locate_near(scan: Scan, kind: str, value: str, page: int, lo: int, hi: int, *,
                exclude: tuple[int, int] | None = None) -> Location | None:
    """The folded *value* as a whole word on *page* within original offsets
    [lo, hi), skipping a hit that overlaps *exclude* (the title's own span)."""
    needle = fold(value)
    if len(needle) < 3:
        return None
    for number, text in scan.pages:
        if number != page:
            continue
        folded, positions = _folded_with_map(text)
        start = 0
        while True:
            at = folded.find(needle, start)
            if at < 0:
                return None
            start = at + 1
            before_ok = at == 0 or folded[at - 1] == " "
            after_ok = at + len(needle) == len(folded) or folded[at + len(needle)] == " "
            first = positions[at]
            last = positions[at + len(needle) - 1] + 1
            if not (before_ok and after_ok) or not lo <= first < hi:
                continue
            if exclude is not None and first < exclude[1] and last > exclude[0]:
                continue
            excerpt = _excerpt(text, first, last)
            return Location(kind, value, number, first, excerpt, passage_fingerprint(excerpt),
                            hashlib.sha256(text.encode("utf-8")).hexdigest(), number <= FRONT_MATTER_PAGES)
    return None


def locate_doi(scan: Scan, doi: str) -> tuple[Location | None, list[Location]]:
    """The record's DOI on the pages: ``(front-matter hit or None, all hits)``.
    Every printed DOI on the scanned pages is compared after normalisation."""
    wanted = normalise_doi(doi or "")
    hits: list[Location] = []
    if not wanted:
        return None, hits
    for number, text in scan.pages:
        for match in _DOI.finditer(text):
            printed = normalise_doi(match.group(0).rstrip(".,;)]}"))
            if printed != wanted:
                continue
            excerpt = _excerpt(text, match.start(), match.end())
            hits.append(Location("doi", wanted, number, match.start(), excerpt, passage_fingerprint(excerpt),
                                 hashlib.sha256(text.encode("utf-8")).hexdigest(), number <= FRONT_MATTER_PAGES))
    front = next((h for h in hits if h.in_front_matter), None)
    return front, hits


def surnames(creators) -> list[str]:
    """Author surnames from Zotero-style creators, a list of names or a string."""
    out: list[str] = []
    if isinstance(creators, str):
        # BibTeX shape: "Last, First and Last, First"; a name without a comma
        # is "First Last", whose surname is the final token.
        for part in re.split(r"\s+and\s+|;", creators):
            part = part.strip()
            if not part:
                continue
            token = part.split(",")[0].strip() if "," in part else part.split()[-1]
            if token:
                out.append(token)
        return [s for s in out if len(fold(s)) >= 3]
    for creator in creators or []:
        if isinstance(creator, dict):
            name = creator.get("lastName") or creator.get("name") or ""
        else:
            name = str(creator)
        name = name.strip()
        if name:
            out.append(name.split(",")[0].strip())
    return [s for s in out if len(fold(s)) >= 3]


def authenticate(path: Path, current: dict, *, rel: str | None = None) -> dict:
    """Authenticate one PDF against the record's own ``current`` fields.

    Returns a JSON-able report: file hash, pages read, per-needle locations and
    a ``status`` in ``authenticated`` (DOI in the front matter, or the title in
    the front matter with at least one author surname printed within
    ``AUTHOR_TITLE_WINDOW`` characters of it on the same page, never a word of
    the title itself), ``reference_list_only`` (the DOI or
    title appears only past the front matter), ``partial`` (title or authors
    only), ``not_found`` or ``unavailable``. Every location carries a passage
    fingerprint; ``sources`` lists the located passages in the campaign's
    source shape (``kind: pdf_passage``) so a model judgment can cite them and
    the citation check can verify the excerpt against the page text.
    """
    scan = scan_pdf(path)
    label = rel or str(path)
    report: dict = dict(pdf=label, pdf_sha256=scan.sha256, page_count=scan.page_count,
                        pages_scanned=len(scan.pages), truncated=scan.truncated, locations=[],
                        sources=[], independent_of_catalogue=True)
    if scan.error or not scan.pages:
        return dict(report, status="unavailable", reason=scan.error or "no extractable page text",
                    next_action="Open the PDF yourself; absence of text is not absence of the document.")
    doi_front, doi_hits = locate_doi(scan, str(current.get("DOI") or current.get("doi") or ""))
    title = str(current.get("title") or "")
    title_hit = locate_text(scan, "title", title) if title else None
    names = surnames(current.get("creators") or current.get("authors") or current.get("author") or [])
    author_hits = [h for h in (locate_text(scan, "author", n) for n in names) if h is not None]
    locations = [*doi_hits, *([title_hit] if title_hit else []), *author_hits]
    report["locations"] = [loc.to_json() for loc in locations]
    wanted = normalise_doi(str(current.get("DOI") or current.get("doi") or ""))
    report["doi"] = dict(value=wanted, front_matter=doi_front is not None, hits=len(doi_hits),
                         pages=sorted({h.page for h in doi_hits}),
                         # The DOI's own verdict, separate from the document's: a
                         # title-authenticated document can still carry a cited
                         # paper's DOI in its record (the corpus trap `cited_doi`).
                         status=("none" if not wanted else "front_matter" if doi_front is not None
                                 else "reference_list_only" if doi_hits else "absent"))
    report["title"] = dict(value=title, found=title_hit is not None,
                           front_matter=bool(title_hit and title_hit.in_front_matter),
                           page=title_hit.page if title_hit else None)
    report["authors"] = dict(surnames=names, found=[h.value for h in author_hits],
                             missing=[n for n in names if n not in {h.value for h in author_hits}],
                             front_matter=[h.value for h in author_hits if h.in_front_matter])
    front_author = any(h.in_front_matter for h in author_hits)
    # Title + author authenticates only when an author surname is printed near
    # the title on the title's page, and not as a word of the title itself.
    near_title = []
    if title_hit is not None and title_hit.in_front_matter:
        span = (title_hit.offset, title_hit.offset + len(title))
        near_title = [h for h in (locate_near(scan, "author", n, title_hit.page,
                                              title_hit.offset - AUTHOR_TITLE_WINDOW,
                                              span[1] + AUTHOR_TITLE_WINDOW, exclude=span) for n in names)
                      if h is not None]
    report["authors"]["near_title"] = [h.value for h in near_title]
    if doi_front is not None or (title_hit is not None and title_hit.in_front_matter and near_title):
        status, reason = "authenticated", "The record's identifiers are printed in the document's front matter."
    elif doi_hits and doi_front is None:
        status, reason = "reference_list_only", ("The record's DOI appears only past the front matter — "
                                                 "a cited reference, not this document's identity.")
    elif title_hit is not None and not title_hit.in_front_matter and not front_author:
        status, reason = "reference_list_only", "The title appears only past the front matter."
    elif title_hit is not None or author_hits:
        status, reason = "partial", "Only part of the record is printed where the document identifies itself."
    else:
        status, reason = "not_found", "Neither DOI, title nor authors were found on the scanned pages."
    for index, loc in enumerate(locations):
        report["sources"].append(dict(
            id=f"pdf:{scan.sha256[:12]}:p{loc.page}:{loc.kind}:{index}",
            locator=f"{label} page {loc.page} offset {loc.offset}", text=loc.excerpt, page=loc.page,
            kind="pdf_passage", acquisition="pdf_page_scan", passage_fingerprint=loc.fingerprint,
            page_sha256=loc.page_sha256, pdf_sha256=scan.sha256))
    return dict(report, status=status, reason=reason, applicable=False,
                next_action=("Compare the located passages with the record; a located passage is evidence for review, "
                             "not an approval."))
