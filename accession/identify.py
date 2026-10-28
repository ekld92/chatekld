"""Turn what a PDF discloses about itself into ranked resolution queries.

Pure by contract: no network, no filesystem, no app imports. The caller
supplies the three evidence artifacts — the embedded-metadata DOI (from
``audit.core.pdf_identifiers.extract_embedded_doi`` / the ``doi_cache``
memo), the page-1 text (the injected ``pdf_text_provider`` seam), and the
filename stem — and gets back the ladder's queries in confidence order.

The only imports beyond stdlib are the audit's **pure shared recipes**
(``normalise_doi``, ``extract_printed_identifier``, the filename
author+year parser). Reusing them is load-bearing, not tidiness: the
bib-check in ``candidates.py`` joins the query DOI against ``BibEntry.doi``,
so both sides must canonicalise identically, and the filename corroboration
must agree with the bridge's author+year rung about what a stem means.

Ranking mirrors the plan's ladder and Phase 1's evidence-class lesson:

1. ``doi`` / ``meta``   — the publisher asserting an identity for THIS file.
2. ``doi`` / ``text`` or ``arxiv`` / ``text`` — an identifier printed on
   page 1; topmost match wins, and it may belong to a neighbouring article
   (which is why the evidence class rides the query — the tiering downstream
   is ``xmp_doi`` vs ``printed_doi``).
3. ``title`` / ``text`` — a heuristic title candidate, corroborated
   downstream by the filename's (surname, year) — available on 100% of the
   measured orphan set thanks to the vault's naming discipline.
4. ``authoryear`` / ``filename`` — the no-text fallback (rung 5): search by
   surname and year and let the eye do the last step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from audit.core.bib import normalise_doi
from audit.core.pdf_identifiers import extract_printed_identifier
from audit.engine.bridge import _filename_authoryear_candidates

__all__ = ["Query", "identify", "extract_title"]

#: Printed-identifier extraction is bounded to this prefix of the supplied
#: text. The caller's text is cache-first and may be the WHOLE document (the
#: indexer's cache capped at 20k chars), and a DOI that only appears beyond
#: the first page is references-grade evidence, not the paper's own —
#: measured on the live library: 31 of 178 orphan DOI queries built from
#: whole-document text hit a bib DOI that the audit's true page-1-bounded
#: rung had (correctly) not linked, and only 13 of those 31 corroborated the
#: filename. Bounding here keeps the ``printed_doi`` tier meaning what
#: Phase 1 established: an identifier ON PAGE 1. Deep-text DOIs are not
#: lost — those PDFs have text, so the title query still reaches them and
#: grades the match honestly (a title winner whose DOI is in the bib is a
#: Link at title confidence, never bulk-checked).
_IDENTIFIER_SCAN_CHARS = 4000


@dataclass(frozen=True)
class Query:
    """One resolution attempt, in the ladder's vocabulary.

    ``kind`` ∈ ``{"doi", "arxiv", "isbn", "title", "authoryear"}`` selects
    which sources can serve it; ``value`` is the search string (a normalised
    DOI, a bare arXiv id, a normalised ISBN-13, a title candidate, or a
    surname key). ``evidence``
    (``meta`` / ``text`` / ``filename``) records where the value came from —
    the tier assignment downstream depends on it, per the Phase 1 lesson
    that an embedded DOI and a printed DOI are different kinds of evidence.

    ``fn_authors`` / ``fn_year`` carry the filename-derived corroboration on
    EVERY query (both the joined and first-token author keys, matching the
    bridge's two-candidate convention), so the scorer never re-parses stems.
    """

    kind: str
    value: str
    evidence: str
    fn_authors: tuple[str, ...] = field(default=())
    fn_year: str | None = None
    #: Optional venue/series constraint for a title search (Batch D,
    #: 2026-08-26). Measured on the maintainer's 20 EMC entries: an
    #: unconstrained Crossref title search lands on the right DOI 10 times and
    #: returns a CONFIDENT WRONG record 4 times ("Personnalité narcissique"
    #: matched a Kernberg volume at 0.92); adding the container takes it to 14
    #: right and 1 wrong. Generic French clinical nouns are the failure mode,
    #: and the constraint is a safety improvement as much as a recall one.
    #: Only ever user-supplied — nothing in the app can infer it for an
    #: unidentified PDF.
    container: str | None = None


# ── page-1 title extraction ────────────────────────────────────────────────
#
# Deliberately modest: the downstream tiers absorb its imprecision (an
# uncorroborated title match is card-only, never bulk), so the heuristic
# only has to be right often enough to be worth a keyless lookup. Every
# rule below was calibrated against the live library's mapped PDFs — the
# extracted title scored against that PDF's own bib record — and the first
# draft's failures are what shaped it: the longest surviving line loses to
# feature-explainer boilerplate (NEJM), author lines evade an initials-only
# detector when they use degree suffixes ("Marica Leone, BSc; …") or
# collapsed superscripts ("Campbell,1"), body sentences win on length
# unless lowercase starts and mid-line sentence boundaries are filtered,
# and split titles whose second half is short ("… Disorders / and Toxic
# Effects") never merge under a survivors-only merge rule.

_TITLE_SCAN_LINES = 30      # how deep into page 1 the scan looks
_TITLE_MIN_CHARS = 20
_TITLE_MAX_CHARS = 250
_TITLE_MERGED_MAX = 300
_TITLE_MAX_MERGES = 2
_TITLE_MAX_DIGIT_RATIO = 0.2
_TITLE_ANCHOR_GAP = 4       # title sits within this many lines of the authors
_TITLE_FALLBACK_MAX_IDX = 3  # without an author anchor, only the page's very
#                              first lines are trustworthy (measured: picks at
#                              lines 0–3 score like anchored ones, 67% ≥0.65;
#                              lines 4+ collapse to ≤26% — body prose wins on
#                              length once the scan leaves the masthead)

# Lines that can never be part of a title, wherever they occur: identifiers,
# addresses, licensing/reprint and submission-history boilerplate (EN + FR —
# the target vault is bilingual).
_HARD_SKIP_RE = re.compile(
    r"(?i)(?:\b10\.\d{4,9}/|https?://|www\.|@|\bdoi\b|\bissn\b|\bisbn\b|"
    r"©|\(c\)\s*\d{4}|\ball rights reserved\b|\bcreative commons\b|"
    r"\bopen access\b|\bdownloaded from\b|\bjournal homepage\b|"
    r"\bcontents lists\b|\bavailable online\b|\bcorresponding author\b|"
    r"\breceived\b|\baccepted\b|\bpublished online\b|\bpreprint\b|"
    r"\bpermission\b|\breprints?\b|\brepublish\b|\bnon-commercial\b|"
    r"\bhigh-quality copies\b|\bterms of use\b|\bcopyright\b|"
    r"\bclicking here\b|\bconflicts? of interest\b|\bfunding\b|"
    r"\bsupplementary\b|\bthis copy is for\b|\bcite this article\b|"
    r"\bto cite\b|\bdeclares?\b|\bcompeting interests?\b|\bgrants? from\b|"
    r"^(?:we |dear |to the editor)|"
    r"\bre[çc]u le\b|\baccept[ée] le\b|\bdisponible en ligne\b)"
)

# Article-type / section labels: skipped only when the line IS the label
# (short), because a real title may contain the same words ("A case report
# of…"). Hitting an abstract/keywords label also ENDS the scan — everything
# after it is body text, and the abstract's first sentence is the classic
# false title.
_LABEL_RE = re.compile(
    r"(?i)^(?:original (?:article|research|investigation)|research article|"
    r"review(?: article)?|systematic review|meta-analysis|editorial|"
    r"commentary|perspective|correspondence|case report|short communication|"
    r"brief report|letter to the editor|article original|article|"
    r"rapid communication|clinical study|special article|hypothesis|"
    r"clinical practice|research)\W*$"
)
_STOP_LABEL_RE = re.compile(
    r"(?i)^(?:abstract|summary|r[ée]sum[ée]|keywords?|mots[- ]cl[ée]s|"
    r"highlights|graphical abstract|introduction|background)\b\W*"
)

# Author-line shapes, each needing ≥2 hits: "A." initials, degree suffixes
# ("Marica Leone, BSc; Ralf Kuja-Halkola, PhD"), or the collapsed-superscript
# residue PDF extraction leaves after names ("Campbell,1" / "Garras1,").
_INITIALS_RE = re.compile(r"\b[A-Z]\.")
_DEGREE_RE = re.compile(
    r",\s*(?:MD|M\.D\.|PhD|Ph\.D\.|DPhil|BSc|B\.Sc\.|MSc|M\.Sc\.|MPH|"
    r"M\.P\.H\.|MBBS|MBChB|DrPH|RN|DO|PharmD|FRCP\w*|FRANZCP)\b")
_SUPERSCRIPT_RESIDUE_RE = re.compile(
    r"[a-z]\d\b|[a-z],[a-z]\b|[a-z]\d,|[a-z],\d|\w\d,\d")

# Body prose: a sentence boundary mid-line ("… clinical problem. Evidence")
# — the lowercase letter before the period spares "U.S. Adults"-style
# abbreviations inside real titles.
_SENTENCE_RE = re.compile(r"[a-z]\.\s+[A-Z]")

# Affiliation lines ("Department of Psychiatry, University of Ottawa,
# Canada") routinely outlast the title on raw length, so they need their own
# filter — but only in the comma-listed shape, because "Depression among
# university students" is a perfectly good title. EN + FR keywords.
_AFFILIATION_RE = re.compile(
    r"(?i)\b(?:department|dept\.|university|universit[ée]|institute|"
    r"institut|hospital|h[ôo]pital|faculty|facult[ée]|school of|"
    r"laboratoire|laboratory|inserm|cnrs|college|clinic|unit[ée] de|"
    r"direction r[ée]gionale)\b"
)


def _digit_ratio(line: str) -> float:
    alnum = [c for c in line if c.isalnum()]
    if not alnum:
        return 1.0
    return sum(c.isdigit() for c in alnum) / len(alnum)


_NAME_CHUNK_RE = re.compile(r"^[A-Z][\w'’-]*(?:\s+[A-Z][\w'’.-]*){1,3}$")


def _looks_like_name_list(line: str) -> bool:
    """Full-name author lists without initials or degrees ("Felicity
    Waite, Emma Černis, Thomas Kabir, …"): ≥3 comma/and-separated chunks of
    2–4 capitalized tokens each, and namey chunks dominating the line —
    single-token chunks keep "Statistics, Structure, and Abstraction" safe.
    """
    chunks = [c.strip() for c in re.split(r",|\band\b|&", line) if c.strip()]
    if len(chunks) < 3:
        return False
    namey = sum(1 for c in chunks if _NAME_CHUNK_RE.match(c))
    return namey >= 3 and namey / len(chunks) >= 0.75


def _looks_like_authors(line: str) -> bool:
    if len(_INITIALS_RE.findall(line)) >= 2:
        return True
    if len(_DEGREE_RE.findall(line)) >= 2:
        return True
    if len(_SUPERSCRIPT_RESIDUE_RE.findall(line)) >= 2:
        return True
    return _looks_like_name_list(line)


def _starts_lower(line: str) -> bool:
    for c in line:
        if c.isalpha():
            return c.islower()
    return False


def _classify(line: str) -> str:
    """``skip`` | ``author`` | ``short`` | ``lower`` | ``ok``."""
    if _HARD_SKIP_RE.search(line):
        return "skip"
    if _looks_like_authors(line):
        return "author"
    if len(line) < 40 and _LABEL_RE.match(line):
        return "skip"
    if len(line) > _TITLE_MAX_CHARS or _digit_ratio(line) > _TITLE_MAX_DIGIT_RATIO:
        return "skip"
    if _SENTENCE_RE.search(line):
        return "skip"
    if "," in line and _AFFILIATION_RE.search(line):
        return "skip"
    if _starts_lower(line):
        return "lower"       # body prose — but a valid title CONTINUATION
    if len(line) < _TITLE_MIN_CHARS:
        return "short"       # too short to stand alone; may continue a title
    return "ok"


def extract_title(page1_text: str | None) -> str | None:
    """A title candidate from page-1 text, or ``None`` when nothing is safe.

    Scans the first ``_TITLE_SCAN_LINES`` non-empty lines, stopping at an
    abstract/keywords label, and classifies each. Selection is
    **author-anchored**: the title on a real page 1 is the surviving line
    immediately preceding the author block, so when an author-shaped line is
    found, the nearest preceding survivor (within ``_TITLE_ANCHOR_GAP``
    lines) wins; with no author line the longest survivor does. The pick is
    then merged forward over up to ``_TITLE_MAX_MERGES`` continuation lines
    — a lowercase start ("and Toxic Effects") or an unterminated pick plus a
    surviving next line — bounded so it can never swallow a paragraph.
    """
    if not page1_text:
        return None
    raw_lines = [ln.strip() for ln in page1_text.splitlines()]
    lines: list[str] = []
    classes: list[str] = []
    for ln in raw_lines:
        if not ln:
            continue
        if _STOP_LABEL_RE.match(ln):
            break
        lines.append(ln)
        classes.append(_classify(ln))
        if len(lines) >= _TITLE_SCAN_LINES:
            break

    survivors = [i for i, c in enumerate(classes) if c == "ok"]
    if not survivors:
        return None
    author_idxs = [i for i, c in enumerate(classes) if c == "author"]

    base: int | None = None
    if author_idxs:
        anchor = author_idxs[-1]
        prior = [i for i in survivors
                 if i < anchor and anchor - i <= _TITLE_ANCHOR_GAP]
        if prior:
            base = prior[-1]           # the survivor closest to the authors
    if base is None:
        early = [i for i in survivors if i <= _TITLE_FALLBACK_MAX_IDX]
        if not early:
            return None                # nothing safe — let rung 5 handle it
        base = max(early, key=lambda i: len(lines[i]))

    title = lines[base]
    j = base + 1
    merges = 0
    while j < len(lines) and merges < _TITLE_MAX_MERGES:
        cls, ln = classes[j], lines[j]
        if cls not in ("ok", "short", "lower"):
            break
        wraps = cls == "lower" or not title.rstrip().endswith((".", "?", "!"))
        if not wraps or len(title) + 1 + len(ln) > _TITLE_MERGED_MAX:
            break
        title = f"{title} {ln}"
        merges += 1
        j += 1
    return re.sub(r"\s+", " ", title).strip() or None


# ── the ranked query builder ───────────────────────────────────────────────


def identify(
    embedded_doi: str | None,
    page1_text: str | None,
    filename_stem: str,
) -> list[Query]:
    """Ranked queries for one PDF, best evidence first (see module docstring).

    ``embedded_doi`` is expected already normalised (it comes from
    ``extract_embedded_doi`` / the ``doi_cache`` memo, both of which run it
    through ``normalise_doi``); a raw value is re-normalised defensively. A
    printed DOI identical to the embedded one is deduped — it adds no new
    evidence. An empty return means the PDF disclosed nothing usable, not
    even a parseable filename.
    """
    fn_cands = _filename_authoryear_candidates(filename_stem)
    fn_authors = tuple(a for a, _y in fn_cands)
    fn_year = fn_cands[0][1] if fn_cands else None

    def _q(kind: str, value: str, evidence: str) -> Query:
        return Query(kind=kind, value=value, evidence=evidence,
                     fn_authors=fn_authors, fn_year=fn_year)

    queries: list[Query] = []

    meta_doi = normalise_doi(embedded_doi) if embedded_doi else None
    if meta_doi:
        queries.append(_q("doi", meta_doi, "meta"))

    printed = extract_printed_identifier(
        (page1_text or "")[:_IDENTIFIER_SCAN_CHARS])
    if printed:
        if printed.startswith("doi:"):
            printed_doi = normalise_doi(printed[len("doi:"):])
            if printed_doi and printed_doi != meta_doi:
                queries.append(_q("doi", printed_doi, "text"))
        elif printed.startswith("arxiv:"):
            queries.append(_q("arxiv", printed[len("arxiv:"):], "text"))

    # ISBN before the title (Batch D): a valid check digit is publisher-
    # asserted identity, the same class of evidence as an embedded DOI, and it
    # is the ONLY resolver books and chapters have.
    isbn = extract_isbn(page1_text)
    if isbn:
        queries.append(_q("isbn", isbn, "text"))

    title = extract_title(page1_text)
    if title:
        queries.append(_q("title", title, "text"))

    if fn_authors and fn_year:
        queries.append(_q("authoryear", fn_authors[0], "filename"))

    return queries


# ── ISBN ───────────────────────────────────────────────────────────────────
#
# Books and book chapters have no resolver at all in the shipped ladder
# (Crossref / Europe PMC / arXiv / HAL / theses.fr are article catalogues),
# and they are a real share of the residual: 27% of orphan PDFs run over 30
# pages against 8% of mapped ones, with a 90th percentile of 300 pages.
#
# A bare 13-digit run is NOT an ISBN — page-1 text is full of phone numbers,
# accession numbers and grant ids — so the check digit is what makes this
# usable at all. Both forms are accepted and normalised to ISBN-13, which is
# what Open Library and Google Books key on.

_ISBN_RE = re.compile(
    r"(?<![0-9])(?:ISBN(?:-1[03])?:?\s*)?"
    r"((?:97[89][- ]?)?(?:[0-9][- ]?){9}[0-9Xx])(?![0-9])")


def _isbn10_ok(digits: str) -> bool:
    total = sum((10 - i) * (10 if c in "Xx" else int(c))
                for i, c in enumerate(digits))
    return total % 11 == 0


def _isbn13_ok(digits: str) -> bool:
    total = sum((1 if i % 2 == 0 else 3) * int(c)
                for i, c in enumerate(digits))
    return total % 10 == 0


def normalise_isbn(raw: str) -> str | None:
    """Validate an ISBN-10/13 and return it as bare ISBN-13 digits, else None.

    The check digit is the whole point: without it a 10- or 13-digit run from
    page-1 text resolves to nothing and wastes a network call per orphan.
    """
    digits = re.sub(r"[^0-9Xx]", "", raw or "")
    if len(digits) == 13:
        return digits if digits.isdigit() and _isbn13_ok(digits) else None
    if len(digits) == 10:
        if not _isbn10_ok(digits):
            return None
        core = "978" + digits[:9]
        checksum = (10 - sum((1 if i % 2 == 0 else 3) * int(c)
                             for i, c in enumerate(core)) % 10) % 10
        return core + str(checksum)
    return None


def _isbn_group_shape_ok(run: str) -> bool:
    """Whether a SEPARATED digit run is shaped like a printed ISBN.

    A printed ISBN's last hyphen/space group is the single check character
    (``978-1-58562-345-7``, ``0-306-40615-2``). A US toll-free number is 3-3-4
    and a printing-impression line is 2-2-2-2-2, so this is what separates
    them — the check digit alone does not: ``800-445-9714`` passes the ISBN-10
    checksum, and on the live library one such number sat above a book's real
    ISBN and was returned instead of it. An unseparated run has no shape to
    judge and is accepted.
    """
    groups = [g for g in re.split(r"[- ]", run) if g]
    return len(groups) == 1 or len(groups[-1]) == 1


def extract_isbn(text: str | None) -> str | None:
    """The best VALID ISBN in *text*, normalised to ISBN-13, else None.

    Bounded like every other page-1 identifier read: cache-first text can be
    the whole document, and an ISBN found in a reference list belongs to a
    cited work, not to this one.

    Selection is deliberately not "first valid": a bare 10- or 13-digit run
    passes the check digit by chance often enough to matter on a copyright
    page full of phone numbers, LCCNs and impression lines. So —

    * an explicitly LABELLED run (``ISBN …``) is decisive and wins outright,
      shape notwithstanding: the label is the publisher telling us what this
      is, and real books print malformed groupings (``ISBN 9782340-061033``);
    * an unlabelled run must additionally be shaped like an ISBN
      (:func:`_isbn_group_shape_ok`), which is what excludes phone numbers;
    * otherwise the first surviving candidate wins, as before.

    Measured over all 371 residual PDFs: 45 ISBNs before, 45 after, none lost,
    one repaired (``meyer_2019.pdf``: a toll-free number → the real ISBN) and
    four switched from a print ISBN to the labelled e-ISBN of the same work,
    which resolves identically on Open Library and Google Books.
    """
    if not text:
        return None
    fallback = None
    for m in _ISBN_RE.finditer(text[:_IDENTIFIER_SCAN_CHARS]):
        run = m.group(1)
        labelled = m.group(0) != run          # an "ISBN…" prefix was consumed
        if not labelled and not _isbn_group_shape_ok(run):
            continue
        got = normalise_isbn(run)
        if not got:
            continue
        if labelled:
            return got
        if fallback is None:
            fallback = got
    return fallback
