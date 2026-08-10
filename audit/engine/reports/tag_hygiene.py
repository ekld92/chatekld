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
    """Frequency rows (total desc) + top co-occurrence pairs."""

    rows: list[TagHygieneRow] = field(default_factory=list)
    cooccurrence: list[dict] = field(default_factory=list)  # {a, b, count}
    vocab_size: int = 0


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

    return TagHygieneReport(
        rows=rows,
        cooccurrence=pairs[:_COOCCUR_TOP],
        vocab_size=len(vocab or {}),
    )
