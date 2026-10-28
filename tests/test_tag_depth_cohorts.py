"""Batch A (2026-08-26): selecting papers by how many tags they carry.

Everything upstream of this treated tags as a BOOLEAN — ``_untagged`` and
``tag_coverage`` both only asked "any tags at all?" — so a paper with one tag
was indistinguishable from a paper with nine, and the CLI's ``--include-tagged``
had to demand an explicit key file because without a depth bound it would
re-propose the whole library.

The invariant that runs through all of it: **the table, the deck, the dashboard
and the CLI must agree on what "a paper with 2 tags" means.** If they disagree,
the cohort you select in the UI is not the cohort the batch processes.
"""

from __future__ import annotations

import pytest

from tagging.batch import BatchOptions, tag_depth, _in_depth_range, _row_targets


ALL = ("note", "finder", "zotero")


def row(*, finder=(), note=None, zotero=None, has_pdf=True, key="k"):
    return {
        "citation_key": key,
        "has_pdf": has_pdf,
        "finder_tags": list(finder),
        "note": ({"present": True, "tags": list(note)} if note is not None
                 else {"present": False, "tags": []}),
        "zotero": ({"present": True, "parent_tags": list(zotero)}
                   if zotero is not None else {"present": False, "parent_tags": []}),
    }


# --- what "depth" means ----------------------------------------------------

def test_depth_is_the_union_across_surfaces_not_the_sum():
    """The same tag on two surfaces is ONE tag. A sum would rank a
    well-harmonized paper as three times better described than it is."""
    r = row(finder=["dep", "scz"], note=["dep"], zotero=["dep", "scz"])
    assert tag_depth(r, ALL) == 2


def test_depth_folds_case_and_hash_like_the_reports_do():
    r = row(finder=["#Dep"], note=["dep"], zotero=["DEP"])
    assert tag_depth(r, ALL) == 1


def test_a_surface_the_record_does_not_have_contributes_nothing():
    """A record with no reference note is not a zero-tag note; it has no note.
    Counting the absence would push every PDF-less entry into the untagged
    cohort and spend a propose call on each."""
    r = row(finder=["dep"], note=None, zotero=None)
    assert tag_depth(r, ALL) == 1


def test_depth_is_scoped_to_the_requested_targets():
    """`--targets note` must not count Finder tags — otherwise a paper whose
    note is empty looks tagged and is skipped."""
    r = row(finder=["dep", "scz", "mood"], note=[], zotero=None)
    assert tag_depth(r, ("note",)) == 0
    assert tag_depth(r, ALL) == 3


def test_a_pdfless_row_never_counts_finder_tags():
    r = row(finder=["dep"], has_pdf=False, note=["scz"])
    # Defensive: the projection sends [] for a PDF-less row, but the depth
    # function must not depend on that to be correct.
    assert tag_depth(r, ALL) == 1


# --- the range gate --------------------------------------------------------

@pytest.mark.parametrize("tags,lo,hi,expected", [
    (0, None, 2, True), (2, None, 2, True), (3, None, 2, False),
    (0, 1, None, False), (1, 1, None, True),
    (2, 1, 3, True), (4, 1, 3, False),
    (5, None, None, True),          # no bounds = everything
])
def test_depth_range_bounds(tags, lo, hi, expected):
    r = row(note=[f"t{i}" for i in range(tags)])
    opts = BatchOptions(min_tags=lo, max_tags=hi)
    assert _in_depth_range(r, ALL, opts) is expected


def test_max_tags_zero_is_exactly_the_untagged_cohort():
    """`--max-tags 0` must not read as "no bound" — the classic zero-vs-unset
    confusion that made an empty --targets list mean "all targets on"."""
    opts = BatchOptions(max_tags=0)
    assert _in_depth_range(row(note=[]), ALL, opts) is True
    assert _in_depth_range(row(note=["dep"]), ALL, opts) is False


def test_depth_and_row_targets_agree_on_a_pdfless_row():
    r = row(has_pdf=False, note=["dep"], zotero=["dep", "scz"])
    wanted = _row_targets(r, ALL)
    assert "finder" not in wanted
    assert tag_depth(r, wanted) == 2


# --- the dashboard histogram must agree with the batch ---------------------

def test_dashboard_buckets_and_batch_depth_agree():
    """The dashboard says "486 papers with 2 tags"; `--max-tags 2` must select
    those same papers. Both sides are exercised here on one input so a change
    to either definition fails rather than silently diverging."""
    from audit.serialize import _depth_bucket

    cases = [
        (row(note=[]), "0"),
        (row(note=["a"]), "1"),
        (row(note=["a", "b"]), "2"),
        (row(note=["a", "b", "c"]), "3-4"),
        (row(note=["a", "b", "c", "d", "e"]), "5+"),
    ]
    for r, bucket in cases:
        depth = tag_depth(r, ALL)
        assert _depth_bucket(depth) == bucket
        # And the "2 or fewer" cohort is exactly buckets 0/1/2.
        under = _in_depth_range(r, ALL, BatchOptions(max_tags=2))
        assert under is (bucket in ("0", "1", "2"))


# --- the dashboard's OWN pass, not just the bucket function ----------------

class _FakeItem:
    def __init__(self, tags):
        self.tags = list(tags)
        self.child_notes = []


class _FakeNote:
    tags: list = []


class _FakeRecord:
    """The shape `tag_coverage` reads: a `Record`, not an inventory row."""

    def __init__(self, *, note=None, finder=(), zotero=None, has_pdf=True):
        self.obsidian_note = _FakeNote() if note is not None else None
        self.obs_tags = set(note or ())
        self.finder_tag_set = set(finder)
        self.pdf_paths = ["x.pdf"] if has_pdf else []
        self.zotero_item = _FakeItem(zotero) if zotero is not None else None
        self.bib_keywords = set()
        self.zotero_note_tags = set()


def _coverage(records):
    from types import SimpleNamespace

    from audit.serialize import tag_coverage
    inv = SimpleNamespace(records={str(i): r for i, r in enumerate(records)})
    return tag_coverage(inv)


@pytest.mark.parametrize("note,finder,zotero,expected", [
    # The same tag on two surfaces is ONE tag…
    (["dep"], ["dep"], ["dep"], "1"),
    # …including across a case difference…
    (["dep"], ["Dep"], ["DEP"], "1"),
    # …a '#' prefix (Obsidian writes them, Zotero does not)…
    (["dep"], ["#dep"], ["##dep"], "1"),
    # …and an NFD/NFC split, which is the live one: macOS stores Finder tags
    # decomposed while the note's YAML is composed.
    (["dép"], ["dép"], None, "1"),
    (["a", "b"], ["c"], None, "3-4"),
])
def test_the_dashboard_histogram_uses_the_SAME_definition_as_the_batch(
        note, finder, zotero, expected):
    """The real cross-check.

    The previous version of this only fed `tag_depth`'s output into
    `_depth_bucket` — it could not see that `tag_coverage` computed its own
    union from RAW strings, so the dashboard bucket and `--max-tags N` could
    disagree over any case, '#' or NFD variant while the test stayed green.
    Here the dashboard's actual pass runs, and its answer is compared against
    the batch's on the same data.
    """
    from audit.serialize import _depth_bucket

    rec = _FakeRecord(note=note, finder=finder, zotero=zotero)
    got = _coverage([rec])["tag_depth"]["union"]
    assert got[expected] == 1, f"dashboard put it in {got}, expected {expected}"

    inv_row = row(note=note, finder=finder, zotero=zotero)
    assert _depth_bucket(tag_depth(inv_row, ALL)) == expected


def test_a_surface_the_record_lacks_is_absent_from_that_surfaces_histogram():
    """A record with no note is not a zero-tag note. Same rule as the batch's,
    and the reason the per-surface denominators differ from `records_total`."""
    cov = _coverage([_FakeRecord(note=None, finder=["dep"], zotero=None)])
    assert sum(cov["tag_depth"]["note"].values()) == 0
    assert cov["tag_depth"]["finder"]["1"] == 1


def test_depth_buckets_cover_every_count_exactly_once():
    from audit.serialize import TAG_DEPTH_BUCKETS, _depth_bucket
    seen = {_depth_bucket(n) for n in range(0, 50)}
    assert seen == set(TAG_DEPTH_BUCKETS)
