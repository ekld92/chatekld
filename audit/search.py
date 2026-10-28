"""Cross-report free-text search over one cached audit scan.

The audit's reports are *views* of a single ``Inventory``; a user hunting
for one paper otherwise has to guess which tab it surfaced in. This module
builds a flat, searchable corpus over every report at once and answers a query
against it.

Design notes:

* **The corpus is built from the SAME serialized payloads the report tabs
  render** (:mod:`audit.serialize`), never from a parallel field-extraction
  path. A search hit therefore always corresponds to a row the user can go and
  look at — the two cannot drift as report shapes evolve.
* **Matching is accent- and case-insensitive.** The target vault is bilingual
  French/English, so ``etude`` must find ``étude`` and an NFD-stored macOS
  filename must match its NFC-typed counterpart (the normalization boundary
  documented on ``core.utils.nfc``; folded here rather than imported because the
  vendored tree depends only on ``audit/config.py``).
* **Every token must match** (AND), each as a substring, so adding words
  narrows rather than widens.
* Building the corpus is the expensive half (``unread_unzoterod`` may fall back
  to per-PDF disk reads when the scan skipped annotation counting), so callers
  are expected to cache it across keystrokes — see ``api/routes/audit.py``.
"""

from __future__ import annotations

import unicodedata
from typing import Any, Iterable

from .config import Settings
from .engine import duplicates as eng_duplicates
from .engine import inventory as eng_inventory
from .engine.reports import (
    dead_curation as r_dead_curation,
    multi_pdf as r_multi_pdf,
    not_in_zotero as r_not_in_zotero,
    finder_tag_drift as r_finder_tag_drift,
    note_tag_drift as r_note_tag_drift,
    read_unzoterod as r_read_unzoterod,
    tag_harmony as r_tag_harmony,
    tag_hygiene as r_tag_hygiene,
    unread_unzoterod as r_unread_unzoterod,
    zotero_no_pdf as r_zotero_no_pdf,
    zotero_unread as r_zotero_unread,
)
from . import serialize

# Report order in the response — mirrors the tab grouping in index.html
# (Reconciliation, Gaps, Curation) so results read in the same order as the
# tablist the user is looking at.
REPORT_ORDER: tuple[str, ...] = (
    "library_documents",
    "inventory",
    "note_tag_drift",
    "finder_tag_drift",
    "duplicates",
    "near_duplicates",
    "unread_unzoterod",
    "zotero_unread",
    "read_unzoterod",
    "zotero_no_pdf",
    "ambiguous_pdfs",
    "skipped_pdfs",
    "tag_hygiene",
    "tag_harmony",
    "orphan_notes",
    "orphan_zotero_items",
    "zotero_attachments",
    "multi_pdf",
    "not_in_zotero",
    "dead_curation",
)

# Result caps. Per-group keeps one dense report (inventory is thousands of
# rows) from burying the others; the overall cap bounds the response for a
# one-letter query. Both are reported back so the UI can say "showing N of M".
MAX_ROWS_PER_GROUP = 50
MAX_ROWS_TOTAL = 300

MIN_QUERY_LEN = 2
MAX_QUERY_LEN = 200


def fold(value: Any) -> str:
    """Case- and accent-folded form of *value* for substring comparison.

    NFD-decomposes, drops combining marks (so ``é`` → ``e``), then casefolds.
    Applied to BOTH the corpus and the query, so the comparison is symmetric.
    Non-strings degrade to their ``str()`` form rather than raising — the
    corpus carries ints (years, counts) alongside text.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    decomposed = unicodedata.normalize("NFD", value)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold()


def _hay(*parts: Any) -> str:
    """Join *parts* into one folded haystack, flattening lists and dropping None."""
    chunks: list[str] = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, (list, tuple, set)):
            chunks.extend(fold(p) for p in part if p is not None)
        else:
            chunks.append(fold(part))
    return "\n".join(c for c in chunks if c)


class _Entry:
    """One searchable row: its folded haystack plus the UI projection."""

    __slots__ = ("report", "haystack", "row")

    def __init__(self, report: str, haystack: str, row: dict[str, Any]) -> None:
        self.report = report
        self.haystack = haystack
        self.row = row


def _row(
    label: str | None,
    detail: str | None = None,
    *,
    citation_key: str | None = None,
    pdf_rel: str | None = None,
) -> dict[str, Any]:
    """The uniform projection every hit is rendered as.

    Deliberately narrow: the search panel is an index, not a second rendering
    of each report. ``citation_key`` / ``pdf_rel`` are the two identifiers the
    client can act on (jump to the tab, reveal the file).
    """
    return {
        "label": label or "(untitled)",
        "detail": detail or "",
        "citation_key": citation_key,
        "pdf_rel": pdf_rel,
    }


def _author_year(author: Any, year: Any) -> str:
    """``"Smith · 2020"`` from whichever of the two is present."""
    return " · ".join(str(p) for p in (author, year) if p)


class CorpusCancelled(Exception):
    """Raised by :func:`build_corpus` when its caller asked it to stop."""


def build_corpus(
    inv: eng_inventory.Inventory,
    settings: Settings,
    *,
    vocab: Any = None,
    duplicates: list[eng_duplicates.DuplicateSet] | None = None,
    near_duplicates: Any = None,
    drift_include_parent_tags: bool = False,
    drift_synonyms: dict[str, str] | None = None,
    should_cancel: Any = None,
) -> list[_Entry]:
    """Build the flat searchable corpus over every report of one cached scan.

    ``vocab`` (the controlled vocabulary for ``tag_hygiene``) and ``duplicates``
    are passed in rather than resolved here: the former lives app-side, the
    latter is a separately-cached scan phase that may have been skipped.
    ``drift_include_parent_tags`` / ``drift_synonyms`` mirror the drift report
    routes' options (same injection rule) so a search hit always corresponds to
    a row the drift tabs actually render.

    ``should_cancel`` (optional, 2026-09-02) is polled BETWEEN report sections.
    A build whose scan has been replaced keeps a superseded Inventory alive for
    the rest of its run — two multi-hundred-megabyte inventories at once — and
    its result is discarded anyway. Returning early raises
    :class:`CorpusCancelled` rather than a partial corpus, because a partial
    one is indistinguishable from a complete one at the call site and would
    silently hide rows the reports do render.
    """
    entries: list[_Entry] = []

    def _check():
        if should_cancel is not None and should_cancel():
            raise CorpusCancelled()

    # -- inventory -------------------------------------------------------
    for r in serialize.inventory_records(inv, settings):
        pdf_rels = [p.get("rel") for p in r.get("pdf_paths") or []]
        tags = (
            (r.get("finder_tags") or [])
            + (r.get("obs_tags") or [])
            + (r.get("zotero_note_tags") or [])
            + (r.get("bib_keywords") or [])
        )
        entries.append(_Entry(
            "inventory",
            _hay(r.get("citation_key"), r.get("title"), r.get("first_author"),
                 r.get("year"), r.get("entry_type"), pdf_rels, tags),
            _row(
                r.get("title") or r.get("citation_key"),
                _author_year(r.get("first_author"), r.get("year")),
                citation_key=r.get("citation_key"),
                pdf_rel=pdf_rels[0] if pdf_rels else None,
            ),
        ))

    _check()

    # -- note_tag_drift --------------------------------------------------
    drift_rows = r_note_tag_drift.find_drift(
        inv,
        include_parent_tags=drift_include_parent_tags,
        synonyms=drift_synonyms,
    )
    for r in serialize.report_note_tag_drift(drift_rows):
        missing = r.get("missing_in_obs") or []
        variant_surfaces = [s for v in (r.get("variants") or [])
                            for s in (v.get("zotero"), v.get("note")) if s]
        entries.append(_Entry(
            "note_tag_drift",
            _hay(r.get("citation_key"), r.get("title"), r.get("author"),
                 r.get("zotero_note_tags"), r.get("obs_tags"), missing,
                 variant_surfaces, r.get("note_only")),
            _row(
                r.get("title") or r.get("citation_key"),
                f"missing: {', '.join(missing)}" if missing else r.get("author"),
                citation_key=r.get("citation_key"),
            ),
        ))

    _check()

    # -- finder_tag_drift ------------------------------------------------
    # vocab_id_for is deliberately omitted (vendored-tree rule: the resolver
    # lives app-side) — the search haystack needs the surfaces, not the ids.
    finder_rows = r_finder_tag_drift.find_drift(inv, synonyms=drift_synonyms)
    for r in serialize.report_finder_tag_drift(finder_rows, settings)["rows"]:
        f_only = r.get("finder_only") or []
        n_only = r.get("note_only") or []
        variant_surfaces = [s for v in (r.get("variants") or [])
                            for s in (v.get("finder"), v.get("note")) if s]
        detail_bits = []
        if f_only:
            detail_bits.append(f"finder-only: {', '.join(f_only)}")
        if n_only:
            detail_bits.append(f"note-only: {', '.join(n_only)}")
        entries.append(_Entry(
            "finder_tag_drift",
            _hay(r.get("citation_key"), r.get("title"), r.get("author"),
                 r.get("finder_tags"), r.get("obs_tags"), variant_surfaces),
            _row(
                r.get("title") or r.get("citation_key"),
                "; ".join(detail_bits) or r.get("author"),
                citation_key=r.get("citation_key"),
            ),
        ))

    _check()

    # -- annotation-based PDF reports ------------------------------------
    # Same precomputed-annotations contract as the report routes: serve from
    # inv.unmapped_annotations, which falls back to a lazy disk read only for
    # paths absent from it.
    unread = serialize.report_unread_unzoterod(
        r_unread_unzoterod.find(inv, settings, annotations=inv.unmapped_annotations),
        settings,
    )
    for r in unread.get("rows") or []:
        pdf = r.get("pdf") or {}
        entries.append(_Entry(
            "unread_unzoterod",
            _hay(pdf.get("rel"), pdf.get("name")),
            _row(pdf.get("name"), f"{r.get('annotations')} annotations",
                 pdf_rel=pdf.get("rel")),
        ))

    read = serialize.report_read_unzoterod(
        r_read_unzoterod.find(inv, settings, annotations=inv.unmapped_annotations),
        settings,
    )
    for r in read.get("rows") or []:
        pdf = r.get("pdf") or {}
        entries.append(_Entry(
            "read_unzoterod",
            _hay(pdf.get("rel"), pdf.get("name")),
            _row(pdf.get("name"), f"{r.get('annotations')} annotations",
                 pdf_rel=pdf.get("rel")),
        ))

    _check()

    # -- bib/Zotero gap reports ------------------------------------------
    for r in (serialize.report_zotero_unread(r_zotero_unread.find(inv))
              .get("rows") or []):
        entries.append(_Entry(
            "zotero_unread",
            _hay(r.get("citation_key"), r.get("title"), r.get("author"), r.get("year")),
            _row(r.get("title") or r.get("citation_key"),
                 _author_year(r.get("author"), r.get("year")),
                 citation_key=r.get("citation_key")),
        ))

    for r in (serialize.report_zotero_no_pdf(r_zotero_no_pdf.find(inv))
              .get("rows") or []):
        entries.append(_Entry(
            "zotero_no_pdf",
            _hay(r.get("citation_key"), r.get("title"), r.get("author"), r.get("year")),
            _row(r.get("title") or r.get("citation_key"),
                 _author_year(r.get("author"), r.get("year")),
                 citation_key=r.get("citation_key")),
        ))

    _check()

    # -- curation reports -------------------------------------------------
    ambiguous = serialize.report_ambiguous_pdfs(inv, settings)
    for r in ambiguous.get("rows") or []:
        pdf = r.get("pdf") or {}
        cands = r.get("candidates") or []
        keys = [c.get("citation_key") for c in cands]
        entries.append(_Entry(
            "ambiguous_pdfs",
            _hay(pdf.get("rel"), pdf.get("name"), keys,
                 [c.get("title") for c in cands]),
            _row(pdf.get("name"), f"{len(cands)} candidates: {', '.join(k for k in keys if k)}",
                 pdf_rel=pdf.get("rel")),
        ))

    skipped = serialize.report_skipped_pdfs(inv, settings)
    for r in skipped.get("rows") or []:
        pdf = r.get("pdf") or {}
        entries.append(_Entry(
            "skipped_pdfs",
            _hay(pdf.get("rel"), pdf.get("name")),
            _row(pdf.get("name"), pdf.get("rel"), pdf_rel=pdf.get("rel")),
        ))

    for r in (serialize.report_tag_harmony(
            r_tag_harmony.find_harmony(inv, synonyms=drift_synonyms),
            settings).get("rows") or []):
        entries.append(_Entry(
            "tag_harmony",
            _hay(r.get("citation_key"), r.get("title"), r.get("author"),
                 r.get("union")),
            _row(r.get("citation_key"),
                 # Joined so a bib-less record (no author) does not render a
                 # dangling " · " after the count.
                 " · ".join(p for p in (
                     f"{r.get('write_count')} tag(s) to propagate",
                     _author_year(r.get("author"), None)) if p),
                 citation_key=r.get("citation_key")),
        ))

    # Orphan buckets. Searchable because "where did that note go?" is exactly
    # the question an orphan answers, and a row absent from the corpus reads to
    # the user as "the vault does not contain it".
    for r in (serialize.orphan_notes(inv, settings).get("rows") or []):
        entries.append(_Entry(
            "orphan_notes",
            _hay(r.get("rel"), r.get("name")),
            _row(r.get("name"), r.get("rel")),
        ))

    for r in (serialize.orphan_zotero_items(inv).get("rows") or []):
        tags = r.get("tags") or []
        entries.append(_Entry(
            "orphan_zotero_items",
            _hay(r.get("title"), r.get("citation_key"), r.get("item_key"), tags),
            _row(r.get("title") or r.get("item_key"),
                 " · ".join(x for x in (
                     r.get("citation_key"),
                     f"{len(tags)} tags" if tags else None) if x),
                 citation_key=r.get("citation_key")),
        ))

    # Every file Zotero holds against an item. In the corpus for the same
    # reason the orphan buckets are: a filename absent from a search that
    # claims to cover every report reads as "Zotero does not have it", and
    # this is the one report that can answer "which item is holding that PDF?".
    for r in (serialize.zotero_attachments(inv).get("rows") or []):
        entries.append(_Entry(
            "zotero_attachments",
            _hay(r.get("filename"), r.get("path"), r.get("parent_title"),
                 r.get("citation_key"), r.get("item_key"), r.get("link_mode")),
            _row(r.get("filename") or r.get("item_key"),
                 " · ".join(x for x in (
                     r.get("link_mode"),
                     r.get("parent_title"),
                     (f"{r.get('annotation_count')} annotations"
                      if r.get("annotation_count") else None)) if x),
                 citation_key=r.get("citation_key")),
        ))

    # 2026-09-06 harmonization reports. Rendered without their route-side
    # enrichments (the Possible Duplicates decision needs the hash memo, the
    # accession join lives app-side), which changes nothing about WHICH rows
    # exist — only the detail text — so a hit still names a row the tab shows.
    for r in (serialize.report_multi_pdf(r_multi_pdf.find(inv), settings)
              .get("rows") or []):
        files = r.get("files") or []
        entries.append(_Entry(
            "multi_pdf",
            _hay(r.get("citation_key"), r.get("title"), r.get("author"),
                 [f.get("name") for f in files], [f.get("rel") for f in files]),
            _row(r.get("citation_key"),
                 f"{len(files)} PDFs: " + ", ".join(f.get("name") or "" for f in files),
                 citation_key=r.get("citation_key")),
        ))

    for r in (serialize.report_not_in_zotero(
            r_not_in_zotero.find(inv, settings), settings).get("rows") or []):
        pdf = r.get("pdf") or {}
        entries.append(_Entry(
            "not_in_zotero",
            _hay(pdf.get("rel"), pdf.get("name")),
            _row(pdf.get("name"), pdf.get("rel"), pdf_rel=pdf.get("rel")),
        ))

    for r in (serialize.report_dead_curation(
            r_dead_curation.find(inv, settings), settings).get("rows") or []):
        pdf = r.get("pdf") or {}
        entries.append(_Entry(
            "dead_curation",
            _hay(pdf.get("rel"), pdf.get("name"), r.get("citation_key"), r.get("kind")),
            _row(pdf.get("name"),
                 " · ".join(x for x in (r.get("kind"), r.get("citation_key")) if x),
                 citation_key=r.get("citation_key"), pdf_rel=pdf.get("rel")),
        ))

    try:
        hygiene = serialize.report_tag_hygiene(r_tag_hygiene.find(inv, vocab))
    except Exception:  # noqa: BLE001 — hygiene is advisory; never fail a search
        hygiene = {"rows": []}
    for r in hygiene.get("rows") or []:
        variants = r.get("variants") or []
        entries.append(_Entry(
            "tag_hygiene",
            _hay(variants, r.get("vocab_tag"), r.get("near_vocab"), r.get("fingerprint")),
            _row(", ".join(str(v) for v in variants),
                 f"{r.get('total')} uses" + ("" if r.get("in_vocab") else " · not in vocabulary")),
        ))

    _check()

    # -- duplicates -------------------------------------------------------
    for d in (serialize.report_duplicates(duplicates or [], settings).get("rows") or []):
        paths = d.get("paths") or []
        rels = [p.get("rel") for p in paths]
        names = [p.get("name") for p in paths]
        entries.append(_Entry(
            "duplicates",
            _hay(rels, names, d.get("content_hash")),
            _row(names[0] if names else (d.get("content_hash") or "")[:12],
                 f"{len(paths)} copies",
                 pdf_rel=rels[0] if rels else None),
        ))

    _check()

    # -- near_duplicates --------------------------------------------------
    # Like duplicates, a separately-cached, skippable phase passed in by the
    # route (None when skipped).
    if near_duplicates is not None:
        for r in (serialize.report_near_duplicates(near_duplicates, settings)
                  .get("rows") or []):
            paths = r.get("paths") or []
            rels = [p.get("rel") for p in paths]
            names = [p.get("name") for p in paths]
            keys = r.get("citation_keys") or []
            entries.append(_Entry(
                "near_duplicates",
                _hay(rels, names, keys, r.get("evidence"), r.get("kind")),
                _row(
                    names[0] if names else (keys[0] if keys else "(group)"),
                    r.get("evidence"),
                    citation_key=keys[0] if keys else None,
                    pdf_rel=rels[0] if rels else None,
                ),
            ))

    return entries


#: Reports whose visible row set is narrowed at RENDER time by a decision
#: sidecar rather than at scan time. The corpus is memoised per cached scan
#: while such a decision applies on the next report LOAD, so the filtering
#: cannot happen during the build without either rebuilding the corpus on
#: every dismissal or letting search disagree with the tab.
_KEY_FILTERED_REPORTS = frozenset({"zotero_no_pdf"})


def search(corpus: Iterable[_Entry], query: str, *,
           hidden_keys: set[str] | frozenset[str] | None = None) -> dict[str, Any]:
    """Answer *query* against *corpus*, grouped by report and capped.

    Returns ``{"q", "total", "shown", "truncated", "groups":[…]}`` where each
    group is ``{"report", "count", "truncated", "rows":[…]}``. ``count`` is the
    group's true match count even when ``rows`` was capped, so the UI can say
    how much it is hiding rather than silently truncating.

    ``hidden_keys`` are citation keys the user has dismissed from a
    render-time-filtered report (currently the "needs no local PDF" decisions
    behind *Bib Entries Without PDFs*). Applied HERE rather than in
    :func:`build_corpus` because the corpus is memoised per cached scan while a
    dismissal takes effect on the next report load: filtering at build time
    would force a full rebuild per decision. Dropping them keeps the corpus
    invariant that a hit always corresponds to a row the user can go and look
    at — without it, a dismissed entry stayed findable in search and led to a
    report that no longer listed it.
    """
    tokens = [t for t in fold(query).split() if t]
    if not tokens:
        return {"q": query, "total": 0, "shown": 0, "truncated": False, "groups": []}
    hidden = hidden_keys or frozenset()

    by_report: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    total = 0
    for entry in corpus:
        hay = entry.haystack
        if not all(tok in hay for tok in tokens):
            continue
        if (hidden and entry.report in _KEY_FILTERED_REPORTS
                and entry.row.get("citation_key") in hidden):
            continue
        total += 1
        counts[entry.report] = counts.get(entry.report, 0) + 1
        rows = by_report.setdefault(entry.report, [])
        if len(rows) < MAX_ROWS_PER_GROUP:
            rows.append(entry.row)

    groups: list[dict[str, Any]] = []
    shown = 0
    for name in REPORT_ORDER:
        rows = by_report.get(name)
        if not rows:
            continue
        # Enforce the overall cap across groups, in REPORT_ORDER, so the
        # truncation is deterministic rather than dict-insertion dependent.
        room = MAX_ROWS_TOTAL - shown
        if room <= 0:
            rows = []
        elif len(rows) > room:
            rows = rows[:room]
        shown += len(rows)
        groups.append({
            "report": name,
            "count": counts.get(name, 0),
            "truncated": len(rows) < counts.get(name, 0),
            "rows": rows,
        })

    return {
        "q": query,
        "total": total,
        "shown": shown,
        "truncated": shown < total,
        "groups": groups,
    }
