"""Near-duplicate candidate detection (plan 2026-08-05/5).

The exact-duplicate scan (``duplicates.py``) sees only byte-identical files —
any watermark, annotation, OCR pass or re-download makes two copies of the
same paper invisible to it. This engine produces *candidates* from
deterministic, labelled evidence tiers (external survey 2026-08-05: the
Zotero-recipe title match, pdf2doi-style identifier extraction, and a
normalized-text key; MinHash/LSH deliberately NOT used at personal-library
scale). Every candidate carries its reason; nothing is ever merged or
deleted — the report is read-only advice with a persisted per-group dismiss
(zoplicate's "non-duplicate marklist" idea, see ``dup_decisions.py``).

Vendored-tree rule: stdlib only; everything app-side (page-1 PDF text) is
injected as ``pdf_text_provider`` by the route layer.

Tiers:

* ``page1_text`` — two byte-different PDFs whose FIRST-page text normalises
  to the same token key (accent-folded, casefolded, alphabetic tokens ≥3
  chars; digits dropped, which is what kills per-download watermark
  timestamps/IPs). Catches re-downloads and metadata-stamped copies.
* ``identifier`` — two PDFs whose page-1 text carries the same DOI or arXiv
  id (version suffix ignored, so v1/v2 pair up). Catches preprint-vs-
  published when the id is printed. First match nearest the top of the page
  (pdf2doi's known failure is grabbing a *cited* DOI further down).
* ``bib_doi`` — two ``.bib`` records sharing a DOI (record-level).
* ``multi_pdf`` — one citation key with ≥2 byte-distinct PDFs attached (the
  inventory join already knows this; it was never surfaced as a duplicate
  signal).
* ``record_title`` / ``title_only`` — two records whose normalised titles
  match; ``record_title`` when confirmed Zotero-style (year within ±1 AND
  same first-author surname), ``title_only`` when unconfirmed (kept, lower
  confidence, rather than dropped).
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings
from ..core import hashing as core_hashing
from .bridge import _filename_authoryear_candidates, _normalize_author_lastname
from .duplicates import DuplicateSet, candidate_pdfs
from .inventory import Inventory, _norm_title

logger = logging.getLogger(__name__)

# Fewer alphabetic tokens than this on page 1 is too weak to act as an
# identity key (a bare scan artifact / title-only cover page) — counted with
# the no-text PDFs in the report meta rather than silently ignored.
_MIN_PAGE1_TOKENS = 20

# The identity key / similarity input is the FIRST this-many tokens, not the
# whole provider payload (2026-08-05 verification finding): the injected
# provider is cache-first, so an already-indexed PDF yields up to ~20k chars
# of WHOLE-document cached text while an unindexed byte-different copy yields
# genuine page-1-only text — unbounded, the two sides of exactly the
# re-downloaded-copy case could never key equal. 150 tokens sits inside page 1
# of essentially any scholarly PDF (an abstract alone is ~150-250 words), so
# both sides reduce to the same "start of the document" prefix. Residual,
# disclosed: a sparse cover page yielding <150 tokens on the fresh side can
# still mismatch a cached copy whose prefix spills onto page 2.
_KEY_TOKENS = 150

_TOKEN_RE = re.compile(r"[a-z]{3,}")
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>]+", re.IGNORECASE)
_ARXIV_RE = re.compile(r"arxiv[:\s/]*(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)


def _fold_tokens(text: str) -> list[str]:
    """Accent-folded, casefolded alphabetic tokens (≥3 chars) of *text*."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _TOKEN_RE.findall(stripped.casefold())


def _tokens_digest(tokens: list[str]) -> str:
    """sha256 over the joined token list — the page-1 identity key's core."""
    return hashlib.sha256(" ".join(tokens).encode("utf-8")).hexdigest()


def text_key(text: str) -> str | None:
    """Normalised page-1 identity key, or None when the text is too thin.

    Digits never survive (the token regex is alphabetic-only), so the
    variable parts of publisher watermarks — download dates, IPs, page
    furniture — cancel out while their constant words match on both copies.
    Keyed over the first ``_KEY_TOKENS`` tokens only — see that constant for
    the cache-first/fresh-page asymmetry it neutralises.
    """
    tokens = _fold_tokens(text or "")
    if len(tokens) < _MIN_PAGE1_TOKENS:
        return None
    return _tokens_digest(tokens[:_KEY_TOKENS])


def extract_identifier(text: str) -> str | None:
    """First DOI or arXiv id printed on the page, canonicalised, else None."""
    if not text:
        return None
    doi = _DOI_RE.search(text)
    arxiv = _ARXIV_RE.search(text)
    # Nearest the top of the page wins (cited papers' DOIs come later).
    best = None
    if doi and arxiv:
        best = doi if doi.start() < arxiv.start() else arxiv
    else:
        best = doi or arxiv
    if best is None:
        return None
    if best is doi:
        return "doi:" + doi.group(0).lower().rstrip(".,;)]}")
    return "arxiv:" + arxiv.group(1)


@dataclass
class NearDupCandidate:
    """One labelled candidate group — files OR citation keys, never merged."""

    kind: str                 # page1_text | identifier | bib_doi | multi_pdf | record_title | title_only | content_similarity
    evidence: str             # human-readable reason, shown verbatim in the UI
    pair_id: str              # stable dismissal key (see _pair_id)
    paths: list[Path] = field(default_factory=list)
    citation_keys: list[str] = field(default_factory=list)
    similarity: float | None = None   # plan 6 attaches a rapidfuzz %


@dataclass
class NearDupReport:
    candidates: list[NearDupCandidate] = field(default_factory=list)
    no_text_count: int = 0     # PDFs the text tiers could not read (no silent cap)
    dismissed_count: int = 0   # groups hidden by a persisted user decision
    compared_pairs: int = 0    # plan 6: similarity pairs actually scored
    similarity_available: bool = True  # plan 6: False when rapidfuzz is absent


def _pair_id(kind: str, member_ids: list[str]) -> str:
    """Stable dismissal key: kind + sorted member ids (file sha256 — survives
    rename via the hash memo — or citation keys for record-level kinds)."""
    return kind + ":" + "+".join(sorted(member_ids))


def _load_fuzz():
    """Lazy, gated rapidfuzz import (plan 2026-08-05/6).

    Returns the ``rapidfuzz.fuzz`` module or ``None`` when the dependency is
    absent — an env installed before the pin must keep the deterministic
    tiers, with the gap DISCLOSED via ``report.similarity_available``. Tests
    monkeypatch this to simulate absence.
    """
    try:
        from rapidfuzz import fuzz
        return fuzz
    except Exception:  # noqa: BLE001 — a broken/ABI-mismatched wheel raises
        return None    # OSError/RuntimeError, not ImportError; degrade, don't sink the scan


# Jaccard pre-filter inside a block: cheaper than a rapidfuzz call, prunes
# clearly-unrelated same-author-year pairs before any scoring.
_JACCARD_PREFILTER = 0.35


def find_near_duplicates(
    settings: Settings,
    inv: Inventory | None,
    exact_sets: list[DuplicateSet] | None,
    *,
    pdf_text_provider: Callable[[Path], str | None] | None = None,
    hash_cache: dict[str, Any] | None = None,
    dismissed: set[str] | None = None,
    ratio_cutoff: int = 88,
    cancel_fn: Callable[[], bool] | None = None,
    progress_fn: Callable[[str], None] | None = None,
) -> NearDupReport:
    """Run every tier; return labelled candidates minus dismissed groups.

    ``pdf_text_provider=None`` (the CLI path) skips the two text tiers —
    hash-only degradation, stated in the report meta by the route. A file
    group whose members are all byte-identical is excluded (already the exact
    report's row) — that exclusion is the ≥2-distinct-sha rule in
    ``_add_file_group``, NOT a lookup in *exact_sets*; the exact sets are
    consumed only as a free source of already-known full hashes (every member
    of a ``DuplicateSet`` carries its ``content_hash``), sparing a re-hash.
    Dismissed groups are excluded with a count.

    ``ratio_cutoff`` (plan 2026-08-05/6, ``audit_near_dup_ratio``, default 88
    per the paperless-ngx ladder) gates the rapidfuzz ``content_similarity``
    sweep; the same scorer also annotates every deterministic file group with
    its weakest-link pairwise ``similarity`` so the UI can rank groups.
    """
    dismissed = dismissed or set()
    report = NearDupReport()

    def _cancelled() -> bool:
        return cancel_fn is not None and cancel_fn()

    known_sha: dict[Path, str] = {
        p: ds.content_hash for ds in (exact_sets or []) for p in ds.paths}

    def _sha(p: Path) -> str | None:
        return known_sha.get(p) or core_hashing.cached_full_hash(p, hash_cache)

    def _add_file_group(
        kind: str, paths: list[Path], evidence: str,
        similarity: float | None = None,
    ) -> None:
        shas = {}
        for p in paths:
            h = _sha(p)
            if h:
                shas[p] = h
        # Byte-identical members collapse to one sha — a group needs >=2
        # DISTINCT contents to add anything over the exact report.
        if len(set(shas.values())) < 2:
            return
        pid = _pair_id(kind, list(set(shas.values())))
        if pid in dismissed:
            report.dismissed_count += 1
            return
        report.candidates.append(NearDupCandidate(
            kind=kind, evidence=evidence, pair_id=pid,
            paths=sorted(shas.keys()), similarity=similarity))

    def _add_record_group(kind: str, keys: list[str], evidence: str) -> None:
        pid = _pair_id(kind, keys)
        if pid in dismissed:
            report.dismissed_count += 1
            return
        report.candidates.append(NearDupCandidate(
            kind=kind, evidence=evidence, pair_id=pid,
            citation_keys=sorted(keys)))

    # ---- text tiers (page1_text + identifier), one provider call per PDF --
    tokens_by_pdf: dict[Path, list[str]] = {}
    if pdf_text_provider is not None:
        pdfs = candidate_pdfs(settings)
        by_text_key: dict[str, list[Path]] = defaultdict(list)
        by_ident: dict[str, list[Path]] = defaultdict(list)
        for i, p in enumerate(pdfs):
            if _cancelled():
                break
            if progress_fn and i and i % 200 == 0:
                progress_fn(f"Near-duplicates: read {i}/{len(pdfs)} first pages...")
            text = pdf_text_provider(p)
            if not text:
                report.no_text_count += 1
                continue
            tokens = _fold_tokens(text)
            if len(tokens) < _MIN_PAGE1_TOKENS:
                report.no_text_count += 1
            else:
                # First-_KEY_TOKENS prefix everywhere (key, Jaccard, ratio) —
                # both the cached-whole-doc and fresh-page-1 provider shapes
                # reduce to the same document prefix.
                tokens_by_pdf[p] = tokens[:_KEY_TOKENS]
                by_text_key[_tokens_digest(tokens_by_pdf[p])].append(p)
            ident = extract_identifier(text)
            if ident:
                by_ident[ident].append(p)
        for paths in by_text_key.values():
            if len(paths) >= 2:
                _add_file_group(
                    "page1_text", paths,
                    "identical first-page text after normalisation "
                    "(watermarks/timestamps cancelled out)")
        for ident, paths in by_ident.items():
            if len(paths) >= 2:
                _add_file_group(
                    "identifier", paths, f"same identifier on page 1: {ident}")

    # ---- record-level tiers over the inventory ----------------------------
    if inv is not None and not _cancelled():
        by_doi: dict[str, list[str]] = defaultdict(list)
        by_title: dict[str, list[str]] = defaultdict(list)
        for key, rec in inv.records.items():
            if rec.pdf_paths:
                _add_file_group(
                    "multi_pdf", list(rec.pdf_paths),
                    f"{len(rec.pdf_paths)} PDFs attached to citation key {key}")
            be = rec.bib_entry
            if be is None:
                continue
            if getattr(be, "doi", None):
                by_doi[be.doi].append(key)
            if be.title:
                nt = _norm_title(be.title)
                if nt:
                    by_title[nt].append(key)
        for doi, keys in by_doi.items():
            if len(keys) >= 2:
                _add_record_group(
                    "bib_doi", keys, f"same DOI in the .bib: {doi}")
        for _nt, keys in by_title.items():
            if len(keys) < 2:
                continue
            confirmed = _titles_confirmed(inv, keys)
            kind = "record_title" if confirmed else "title_only"
            title = (inv.records[keys[0]].bib_entry.title or "")[:80]
            evidence = (
                f"same normalised title: “{title}”"
                + (" — year ±1 + first author agree (Zotero-style confirmation)"
                   if confirmed else " — year/author NOT confirmed"))
            _add_record_group(kind, keys, evidence)

    # ---- content-similarity tier (plan 2026-08-05/6) ----------------------
    fuzz = _load_fuzz()
    if fuzz is None:
        report.similarity_available = False
    elif tokens_by_pdf and not _cancelled():
        strings = {p: " ".join(t) for p, t in tokens_by_pdf.items()}
        sets = {p: set(t) for p, t in tokens_by_pdf.items()}

        # (a) Annotate every file-level candidate with its weakest-link
        # pairwise ratio so the UI can rank groups by confidence.
        for c in report.candidates:
            texts = [strings[p] for p in c.paths if p in strings]
            if len(texts) < 2:
                continue
            worst = 100.0
            for i in range(len(texts)):
                for j in range(i + 1, len(texts)):
                    worst = min(worst, fuzz.ratio(texts[i], texts[j]))
                    report.compared_pairs += 1
            c.similarity = worst

        # (b) Blocking sweep — the catcher for same-article/different-origin
        # PDFs with no shared identifier and different page furniture. NEVER
        # full-corpus O(n²): pairs are compared only inside a filename
        # author+year block (the bridge's own candidate key), pre-filtered by
        # token-set Jaccard, then scored with score_cutoff so rapidfuzz
        # short-circuits (paperless-ngx's trick). Pairs already claimed by a
        # deterministic tier are skipped.
        existing_pairs: set[frozenset] = set()
        for c in report.candidates:
            for i, p in enumerate(c.paths):
                for q in c.paths[i + 1:]:
                    existing_pairs.add(frozenset((p, q)))
        blocks: dict[tuple[str, str], list[Path]] = defaultdict(list)
        for p in strings:
            cands = _filename_authoryear_candidates(p.stem)
            if cands:
                blocks[cands[0]].append(p)   # joined-multi-token form only
        for members in blocks.values():
            if _cancelled():
                break
            if len(members) < 2:
                continue
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    a, b = members[i], members[j]
                    if frozenset((a, b)) in existing_pairs:
                        continue
                    inter = len(sets[a] & sets[b])
                    union = len(sets[a] | sets[b]) or 1
                    if inter / union < _JACCARD_PREFILTER:
                        continue
                    score = fuzz.ratio(
                        strings[a], strings[b], score_cutoff=ratio_cutoff)
                    report.compared_pairs += 1
                    if score:
                        _add_file_group(
                            "content_similarity", [a, b],
                            f"first-page content {score:.0f}% similar "
                            "(same author-year filename block)",
                            similarity=score)

    report.candidates.sort(key=lambda c: (c.kind, c.pair_id))
    return report


def _titles_confirmed(inv: Inventory, keys: list[str]) -> bool:
    """Zotero-recipe confirmation across a title-collision group: every pair
    within year ±1 AND sharing a normalised first-author surname. Missing
    year/author on any member fails confirmation (conservative)."""
    years: list[int] = []
    authors: set[str] = set()
    for k in keys:
        be = inv.records[k].bib_entry
        if be is None or not be.year or not be.authors:
            return False
        m = re.search(r"(19|20)\d{2}", be.year)
        if not m:
            return False
        years.append(int(m.group(0)))
        authors.add(_normalize_author_lastname(be.authors[0]))
    if max(years) - min(years) > 1:
        return False
    return len(authors) == 1 and "" not in authors
