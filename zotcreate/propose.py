"""Turn what is already known about a PDF into a Zotero item draft.

**Offline by construction.** Every input here comes from the file itself or
from memos the audit scan already wrote — the cached accession resolution
(``BASE_DIR/audit/accession_cache.json``), the embedded/printed DOI, the
page-1 title. Nothing in this module reaches the network, and the create
route never triggers a lookup: identifying online stays the accession tab's
own separately-gated verb, so a user who never enables it can still make
Zotero items out of the PDFs on their disk.

**The evidence tier decides whether a candidate may fill the form.** That is
the whole safety argument, and it is measured rather than assumed. Of the
298 curated ``pending`` PDFs on the maintainer's library (2026-09-11), the
cached resolutions grade out as:

===================  =====  ====================================
tier                 rows   what it actually is
===================  =====  ====================================
``xmp_doi``            33   a DOI carried INSIDE the file
``printed_doi``         1   the topmost identifier on page 1
``title_only``         34   a title matched; nothing corroborated
``filename_only``     162   a Crossref guess from surname+year
``ambiguous``          15   refused: two plausible winners
(none)                 52   nothing found
===================  =====  ====================================

So the majority answer is a *guess from a filename*. One sampled row offers
three unrelated papers by three different people named "Abbot" — prefilling
from that would create an item for a DIFFERENT PAPER and link the user's
PDF to it, which no later review catches, because the form would look
filled-in and plausible. :data:`PREFILL_TIERS` is therefore the identifier
grades plus the one corroborated title grade; everything else is *offered*
as a numbered suggestion the user must pick, and never fills anything on
its own.
"""

from __future__ import annotations

import re

from audit.engine.bridge import (
    _LEADING_NUM_PREFIX_RE,
    _TRAILING_COPY_RE,
)

#: Tiers whose top candidate may fill the create form unasked: an identifier
#: carried by the file resolved to exactly this record, or a title match that
#: the filename's surname+year independently corroborated. See the module
#: docstring for why the rest are suggestions rather than prefill.
PREFILL_TIERS = ("confirmed", "bib_doi", "xmp_doi", "printed_doi", "title_filename")

#: Registry placeholders that are not journal names. Crossref returns this
#: as the `container-title` of a WITHDRAWN record, and it reached the form as
#: the publication a paper appeared in — see :func:`draft_from_resolution`
#: for the measurement. Lowercased for comparison.
_WITHDRAWN_CONTAINERS = frozenset({"crossref listing of deleted dois"})

#: How a tier reads to a human, on the one line that says why the form looks
#: the way it does. Deliberately not the tier name: "filename_only" tells the
#: user nothing about the risk it carries.
TIER_BASIS = {
    "confirmed": "your saved confirmation for this physical PDF",
    "bib_doi": "an identifier in the file, already in your bibliography",
    "xmp_doi": "a DOI stored inside the file",
    "printed_doi": "the identifier printed on page 1",
    "title_filename": "a title match, corroborated by the filename",
    "title_only": "a title match, with nothing corroborating it",
    "filename_only": "a search on the filename's surname and year — often a "
                     "different paper",
    "ambiguous": "two records matched equally well",
}


def withdrawn_reason(candidate: dict | None) -> str | None:
    """Why this registry record cannot be used, in words, or ``None``.

    A DOI can be real while the record behind it is gone. Measured on the
    live library 2026-09-11: 28 of the 34 identifier-grade ``pending`` PDFs
    resolve to Crossref's *Listing of Deleted DOIs* — a valid EM-Consulte
    DOI whose record was withdrawn, so it carries no title, no authors and
    the year 2000.

    The two signals are the placeholder container and a missing title. Year
    2000 on its own is deliberately NOT one: plenty of real papers are from
    2000, and the measurement never showed the year carrying the decision.
    """
    cand = candidate or {}
    container = str(cand.get("container") or "").strip().lower()
    if container in _WITHDRAWN_CONTAINERS:
        return ("the registry record is withdrawn "
                f"({cand.get('container')})")
    if not str(cand.get("title") or "").strip():
        return "the registry record carries no title"
    return None


def item_type_for(candidate: dict | None, klass: str | None) -> str:
    """The Zotero item type for a candidate, falling back to the PDF's class.

    Asked in this order because the evidence differs in strength: a journal
    name is a positive statement by the registry that this is an article; an
    arXiv id with no journal says it is a preprint; and with neither, the
    only thing left is what the file physically looks like.
    """
    cand = candidate or {}
    if cand.get("container"):
        return "journalArticle"
    if cand.get("arxiv_id"):
        return "preprint"
    return {
        "browser_print": "webpage", "book": "book", "office_export": "report",
        "scan": "document",
    }.get(klass or "", "journalArticle")


def creators_from(candidate: dict | None) -> list[dict]:
    """Candidate author strings → Zotero creators.

    ``accession.sources`` normalises toward ``"Last, First"``, so a comma is
    a reliable split. Without one the string is sent as a single-field
    ``name``: a one-token author is usually an organisation, and guessing a
    surname out of "World Health Organization" writes a wrong record that
    looks right.
    """
    out: list[dict] = []
    for raw in (candidate or {}).get("authors") or ():
        name = str(raw).strip()
        if not name:
            continue
        head, sep, tail = name.partition(",")
        if sep:
            out.append({"creatorType": "author", "lastName": head.strip(),
                        "firstName": tail.strip()})
        else:
            out.append({"creatorType": "author", "name": name})
    return out


def fields_from(candidate: dict | None) -> dict[str, str]:
    """Candidate → Zotero fields. Empty values are omitted, never blanked."""
    cand = candidate or {}
    container = cand.get("container")
    if container and container.strip().lower() in _WITHDRAWN_CONTAINERS:
        container = None
    pairs = (
        ("title", cand.get("title")),
        ("date", cand.get("year")),
        ("publicationTitle", container),
        ("DOI", cand.get("doi")),
        ("url", cand.get("url")),
    )
    return {k: str(v) for k, v in pairs if v not in (None, "")}


def from_filename_stem(stem: str) -> dict:
    """First author and year from the vault's ``surname_year`` convention.

    The last offline source for a PDF nothing else could identify, and a real
    one here: the convention is enforced by the Maintenance renamer, so the
    name encodes a decision the user made rather than a guess this app is
    making. It gives a Zotero item its two most load-bearing fields when the
    only alternative is leaving them blank.

    Deliberately NOT the bridge's join key. ``_filename_authoryear_candidates``
    normalises for MATCHING — NFKD, punctuation stripped, lowercased — which
    turns ``løvvik`` into ``lvvik`` and ``das-munshi`` into ``dasmunshi``.
    Those are correct keys and wrong names, and this value is written into a
    record a human will read, so the raw token is capitalised part by part
    instead. Only the FIRST token is used: a ``smith_jones_2018`` stem names
    two authors and nothing says which order the bibliography wants them in.
    """
    from audit.engine.bridge import _filename_authoryear_candidates

    if not _filename_authoryear_candidates(stem):
        return {}
    s = _TRAILING_COPY_RE.sub("", _LEADING_NUM_PREFIX_RE.sub("", stem))
    years = list(re.finditer(r"(?:19|20)\d{2}", s))
    if not years:
        return {}
    year = years[-1].group(0)
    tokens = [t for t in s[: years[-1].start()].split("_") if t]
    if not tokens:
        return {}
    # The bridge's own cleanup lives in `_normalize_author_lastname`, which
    # also lowercases and NFKD-folds — no use for a display name. A duplicate
    # marker like `2 taylor_2024` therefore still has its counter here.
    token = re.sub(r"^[\W\d_]+", "", tokens[0], flags=re.UNICODE).strip()
    if not token:
        return {}
    surname = "-".join(part[:1].upper() + part[1:]
                       for part in token.split("-") if part)
    return {"lastName": surname, "date": year,
            "more_authors": len(tokens) > 1}


def draft_from_resolution(resolution: dict | None, *, klass: str | None,
                          fallback_title: str | None = None,
                          fallback_doi: str | None = None,
                          source_url: str | None = None,
                          filename_stem: str | None = None) -> dict:
    """The create form's opening state. ``{prefill, suggestions, basis, …}``.

    ``prefill`` is filled from a candidate ONLY at a tier in
    :data:`PREFILL_TIERS`; otherwise it holds what the PDF itself yielded
    (its page-1 title, its own DOI, its recovered URL) and every candidate
    is listed under ``suggestions`` for the user to pick. ``prefilled_from``
    names which of the two happened, so the UI can say so rather than
    presenting a guess and a fact in the same typeface.
    """
    res = resolution or {}
    tier = res.get("tier")
    outcome = res.get("outcome")
    candidates = [c for c in (res.get("candidates") or []) if isinstance(c, dict)]
    top = candidates[0] if candidates else None

    # A candidate with no TITLE is not a usable record, whatever its tier.
    # Measured on the live library 2026-09-11: 28 of the 34 identifier-grade
    # pending rows resolve to Crossref's "Listing of Deleted DOIs" — a real
    # DOI whose record has been withdrawn, so it carries no title, no authors
    # and the year 2000. Prefilling from one wrote that container name and
    # that year into the user's library over a file whose own name says 2017:
    # exactly the "looks filled-in and plausible" failure PREFILL_TIERS
    # exists to prevent, arriving through the tier it trusts most. The DOI
    # itself is still real and still reaches the form — it was extracted from
    # the file, not from this record.
    may_prefill = (outcome == "candidates" and tier in PREFILL_TIERS
                   and top is not None and withdrawn_reason(top) is None)
    if may_prefill:
        fields = fields_from(top)
        creators = creators_from(top)
        item_type = item_type_for(top, klass)
    else:
        fields = {}
        if fallback_title:
            fields["title"] = str(fallback_title)
        if fallback_doi:
            fields["DOI"] = str(fallback_doi)
        if source_url:
            fields["url"] = str(source_url)
        creators = []
        from_name = from_filename_stem(filename_stem or "")
        if from_name:
            fields.setdefault("date", from_name["date"])
            creators = [{"creatorType": "author",
                         "lastName": from_name["lastName"], "firstName": ""}]
        item_type = item_type_for(None, klass)

    return {
        "item_type": item_type,
        "fields": fields,
        "creators": creators,
        "prefilled_from": "candidate" if may_prefill else "pdf",
        "tier": tier,
        "outcome": outcome,
        "basis": TIER_BASIS.get(tier or "") if tier else None,
        "suggestions": [] if may_prefill else candidates[:5],
        # Whether the author/date in the form came from the file's NAME. The
        # UI says so: a surname read off a filename is the user's own earlier
        # decision, not a record anyone looked up.
        "from_filename": bool(not may_prefill
                              and from_filename_stem(filename_stem or "")),
    }


def suggestion_draft(candidate: dict, *, klass: str | None) -> dict:
    """What one PICKED suggestion fills in. Same mapping as a prefill — the
    difference between the two is who chose it, not how it is read."""
    return {
        "item_type": item_type_for(candidate, klass),
        "fields": fields_from(candidate),
        "creators": creators_from(candidate),
    }
