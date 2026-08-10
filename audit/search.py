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
    finder_tag_drift as r_finder_tag_drift,
    note_tag_drift as r_note_tag_drift,
    read_unzoterod as r_read_unzoterod,
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


def build_corpus(
    inv: eng_inventory.Inventory,
    settings: Settings,
    *,
    vocab: Any = None,
    duplicates: list[eng_duplicates.DuplicateSet] | None = None,
    near_duplicates: Any = None,
    drift_include_parent_tags: bool = False,
    drift_synonyms: dict[str, str] | None = None,
) -> list[_Entry]:
    """Build the flat searchable corpus over every report of one cached scan.

    ``vocab`` (the controlled vocabulary for ``tag_hygiene``) and ``duplicates``
    are passed in rather than resolved here: the former lives app-side, the
    latter is a separately-cached scan phase that may have been skipped.
    ``drift_include_parent_tags`` / ``drift_synonyms`` mirror the drift report
    routes' options (same injection rule) so a search hit always corresponds to
    a row the drift tabs actually render.
    """
    entries: list[_Entry] = []

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


def search(corpus: Iterable[_Entry], query: str) -> dict[str, Any]:
    """Answer *query* against *corpus*, grouped by report and capped.

    Returns ``{"q", "total", "shown", "truncated", "groups":[…]}`` where each
    group is ``{"report", "count", "truncated", "rows":[…]}``. ``count`` is the
    group's true match count even when ``rows`` was capped, so the UI can say
    how much it is hiding rather than silently truncating.
    """
    tokens = [t for t in fold(query).split() if t]
    if not tokens:
        return {"q": query, "total": 0, "shown": 0, "truncated": False, "groups": []}

    by_report: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    total = 0
    for entry in corpus:
        hay = entry.haystack
        if not all(tok in hay for tok in tokens):
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
