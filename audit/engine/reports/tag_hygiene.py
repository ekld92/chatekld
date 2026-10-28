"""Tag hygiene: tag frequency across the four tag stores + neighbour clusters.

Read-only analytics over the cached inventory (no vault/Zotero/Finder access
at report time): every observed tag — PDF Finder tags, Obsidian note YAML
tags, Zotero parent tags, Zotero child-note tags, bib keywords — is grouped by
a normalisation *fingerprint* (accent/case/punctuation/token-order
insensitive, OpenRefine's fingerprint-keying recipe), counted per source,
checked against the controlled ``_tags.md`` vocabulary, and clustered:
a fingerprint with several surface variants (``Self-Harm`` / ``self_harm``)
or an out-of-vocabulary fingerprint close to a vocabulary tag is a merge
candidate for the user to review. Top tag co-occurrence pairs (papers sharing
both tags) round out the picture.

The fingerprint recipe lives in ``audit/engine/normalize.py`` since plan
2026-08-05/2 (shared with the Note Tag Drift comparison — one definition of
"same tag" across the tag reports); it is still vendored-tree-local and
stdlib-only, per the ``audit/config.py``-only dependency rule. ``_fingerprint``
stays importable from this module as an alias.
"""

from __future__ import annotations

import difflib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

from ..inventory import Inventory
from ..normalize import fingerprint as _fingerprint

# An out-of-vocabulary fingerprint at least this SequenceMatcher-similar to a
# vocabulary fingerprint is reported as a near-miss neighbour (stdlib pass —
# embedding similarity is a possible later refinement, not this report).
# Why 0.85 (empirical, 2026-07-19 tuning on the maintainer's vault): at 0.80
# unrelated short stems started pairing (e.g. `sleep`↔`slp` false neighbours
# drowned the real typo/variant hits), while everything the report exists to
# catch — plural/singular, accent drift, one-letter typos — still clears
# 0.85 on fingerprints of these lengths. Advisory-only, so a missed neighbour
# costs a hygiene hint, never data.
_NEIGHBOUR_SIMILARITY = 0.85
# Co-occurrence pairs kept: sharing >= _COOCCUR_MIN_KEYS papers, top N shown.
_COOCCUR_MIN_KEYS = 2
_COOCCUR_TOP = 30

# Per-source keys, in display order. "zotero" = the parent item's tags,
# "zotero_notes" = tags on the parent's child notes, "bib" = BibTeX keywords.
SOURCES = ("finder", "note", "zotero", "zotero_notes", "bib")


@dataclass
class TagHygieneRow:
    """One fingerprint-collapsed tag with its per-source paper counts."""

    fingerprint: str
    variants: list[str]          # observed surface forms, most frequent first
    counts: dict[str, int]       # source -> number of papers carrying it there
    total: int                   # sum over sources (a 3-source tag counts 3)
    in_vocab: bool
    vocab_tag: str | None        # canonical ``_tags.md`` id when in_vocab
    near_vocab: str | None = None  # closest vocab tag when NOT in vocab


@dataclass
class TagHygieneReport:
    """Frequency rows (total desc) + top co-occurrence pairs.

    ``unmapped_rows`` is a SEPARATE bucket, deliberately not folded into
    ``rows`` — see :func:`find`.
    """

    rows: list[TagHygieneRow] = field(default_factory=list)
    cooccurrence: list[dict] = field(default_factory=list)  # {a, b, count}
    vocab_size: int = 0
    # Finder tags on biblio PDFs the bridge never mapped to a citation key.
    unmapped_rows: list[TagHygieneRow] = field(default_factory=list)
    unmapped_pdf_count: int = 0        # PDFs scanned for the bucket
    unmapped_tagged_count: int = 0     # of those, how many carry >=1 Finder tag


def find(inv: Inventory, vocab: dict[str, str] | None = None) -> TagHygieneReport:
    """Build the tag-hygiene report over the cached inventory.

    *vocab* is the parsed ``_tags.md`` ``{tag: gloss}`` map (None when the
    file is unconfigured/missing — the report still renders frequencies, just
    without in-vocabulary flags). Each source's tags are set-normalised per
    record (``zotero_item.tags`` is a list on the wire), so one paper counts
    at most once per source per tag.
    """
    variant_counts: dict[str, Counter] = defaultdict(Counter)
    source_counts: dict[str, Counter] = defaultdict(Counter)
    cooccur: Counter = Counter()

    for rec in inv.records.values():
        per_source = {
            "finder": set(rec.finder_tag_set),
            "note": set(rec.obs_tags),
            "zotero": set(rec.zotero_item.tags) if rec.zotero_item else set(),
            "zotero_notes": set(rec.zotero_note_tags),
            "bib": set(rec.bib_keywords),
        }
        rec_fps: set[str] = set()
        for source, tags in per_source.items():
            for raw in tags:
                tag = str(raw).strip().lstrip("#").strip()
                if not tag:
                    continue
                fp = _fingerprint(tag)
                if not fp:
                    continue
                variant_counts[fp][tag] += 1
                source_counts[fp][source] += 1
                rec_fps.add(fp)
        for a, b in combinations(sorted(rec_fps), 2):
            cooccur[(a, b)] += 1

    vocab_fps = {_fingerprint(t): t for t in (vocab or {})}
    vocab_fp_list = list(vocab_fps)

    rows: list[TagHygieneRow] = []
    for fp, variants in variant_counts.items():
        in_vocab = fp in vocab_fps
        near = None
        if not in_vocab and vocab_fp_list:
            match = difflib.get_close_matches(
                fp, vocab_fp_list, n=1, cutoff=_NEIGHBOUR_SIMILARITY)
            near = vocab_fps[match[0]] if match else None
        counts = dict(source_counts[fp])
        rows.append(TagHygieneRow(
            fingerprint=fp,
            variants=[v for v, _n in variants.most_common()],
            counts=counts,
            total=sum(counts.values()),
            in_vocab=in_vocab,
            vocab_tag=vocab_fps.get(fp),
            near_vocab=near,
        ))
    rows.sort(key=lambda r: (-r.total, r.fingerprint))

    pairs = [{"a": a, "b": b, "count": n}
             for (a, b), n in cooccur.items() if n >= _COOCCUR_MIN_KEYS]
    pairs.sort(key=lambda p: (-p["count"], p["a"], p["b"]))

    unmapped_rows, n_pdfs, n_tagged = _unmapped_finder_rows(inv, vocab_fps, vocab_fp_list)

    return TagHygieneReport(
        rows=rows,
        cooccurrence=pairs[:_COOCCUR_TOP],
        vocab_size=len(vocab or {}),
        unmapped_rows=unmapped_rows,
        unmapped_pdf_count=n_pdfs,
        unmapped_tagged_count=n_tagged,
    )


def _unmapped_finder_rows(inv: Inventory, vocab_fps: dict, vocab_fp_list: list):
    """Finder tags on biblio PDFs that have NO citation key. Returns (rows, n, tagged).

    The main report iterates ``inv.records``, the citation-key join, so a PDF
    the bridge never mapped contributes nothing to it — including its
    out-of-vocabulary tags. Measured on the maintainer's vault 2026-08-11: a
    direct sweep found 5 distinct out-of-vocab Finder tags where the report
    showed 2, the two whose PDFs happened to carry mapped keys. The report's
    "out of vocabulary" count was a floor presented as a total.

    These rows are kept **separate** rather than merged into ``rows`` (a
    deliberate maintainer decision, 2026-08-11): folding them in would move
    every hygiene figure in one release, breaking comparability with earlier
    runs and with the recorded audit baselines, and ``audit_library.py``'s
    ``--max-hygiene`` bar would silently change meaning. A separate bucket
    with its own count is the same disclose-what-you-omit posture the
    ``read_unzoterod`` report's meta line takes.

    Counts are per PDF (one file carrying a tag counts once), not per paper as
    in the main rows — an unmapped PDF *is* the unit, since there is no record
    to aggregate copies under. All three residual bridge buckets are included —
    ``unmapped_pdfs``, ``ambiguous_pdfs`` (multiple candidate keys, parked for
    triage) and ``confirmed_no_match_pdfs`` (curated "not in the bib") — because
    every one of them is equally unreachable by citation key today, which is the
    property that made their tags invisible in the first place.
    """
    from audit.core import finder_tags

    bridge = getattr(inv, "bridge", None)
    if bridge is None:
        return [], 0, 0
    paths: list = []
    for attr in ("unmapped_pdfs", "ambiguous_pdfs", "confirmed_no_match_pdfs"):
        # `unmapped_pdfs` / `confirmed_no_match_pdfs` are ``list[Path]`` and
        # `ambiguous_pdfs` is ``dict[Path, list[str]]`` — iterating the dict
        # yields its Path keys, so one loop covers all three. The isinstance
        # gate keeps a future shape change degrading to "fewer rows" rather
        # than crashing a report.
        for p in (getattr(bridge, attr, None) or []):
            if isinstance(p, Path):
                paths.append(p)

    variant_counts: dict[str, Counter] = defaultdict(Counter)
    source_counts: dict[str, Counter] = defaultdict(Counter)
    n_tagged = 0
    for p in paths:
        try:
            names = finder_tags.read_tag_names(p)
        except Exception:  # noqa: BLE001 — a report must not fail on one bad file
            continue
        if not names:
            continue
        n_tagged += 1
        for raw in names:
            tag = str(raw).strip().lstrip("#").strip()
            if not tag:
                continue
            fp = _fingerprint(tag)
            if not fp:
                continue
            variant_counts[fp][tag] += 1
            source_counts[fp]["finder"] += 1

    rows: list[TagHygieneRow] = []
    for fp, variants in variant_counts.items():
        in_vocab = fp in vocab_fps
        near = None
        if not in_vocab and vocab_fp_list:
            match = difflib.get_close_matches(
                fp, vocab_fp_list, n=1, cutoff=_NEIGHBOUR_SIMILARITY)
            near = vocab_fps[match[0]] if match else None
        counts = dict(source_counts[fp])
        rows.append(TagHygieneRow(
            fingerprint=fp,
            variants=[v for v, _n in variants.most_common()],
            counts=counts,
            total=sum(counts.values()),
            in_vocab=in_vocab,
            vocab_tag=vocab_fps.get(fp),
            near_vocab=near,
        ))
    rows.sort(key=lambda r: (-r.total, r.fingerprint))
    return rows, len(paths), n_tagged
