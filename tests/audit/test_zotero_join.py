"""The bib↔Zotero join: citation key primary, normalized title as fallback.

Until 2026-08-12 the join was by normalized title alone, first-occurrence-wins.
That is a heuristic standing in for an authoritative link — Better BibTeX
*derives* the ``_master.bib`` entry key from the Zotero item's ``citationKey``
field, so the key is the join — and the heuristic was measurably wrong on the
maintainer's library: 23 Zotero items were claimed by 2+ bib keys, affecting 51
records. Four different papers titled "Schizophrenia" (Owen 2016, Marder 2019,
Leucht 2025, Jauhar 2022) all resolved to one item, and because
``apply-zotero-tags`` writes to ``rec.zotero_item.item_key``, three of the four
wrote their tags onto the wrong Zotero item.

These tests pin the three properties that make that impossible:
key wins, collisions no longer mis-join, and a key-less item still falls back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from audit.config import Settings
from audit.core import zotero
from audit.engine import inventory


def _item(item_key: str, title: str, citation_key: str | None = None,
          tags: list[str] | None = None) -> zotero.ZoteroItem:
    return zotero.ZoteroItem(
        item_key=item_key,
        item_type="journalArticle",
        title=title,
        citation_key=citation_key,
        tags=tags or [],
    )


def _vault_with_bib(tmp_path: Path, entries: str) -> Settings:
    """Minimal vault + a stub Zotero DB path that merely has to exist.

    ``build_inventory`` only reads Zotero when the configured DB exists, and
    ``fake_zotero`` replaces the reader, so an empty file is enough.
    """
    (tmp_path / "Z_attachments" / "biblio_articles").mkdir(parents=True)
    (tmp_path / "_master.bib").write_text(entries, encoding="utf-8")
    db = tmp_path / "zotero.sqlite"
    db.write_bytes(b"")
    return Settings(
        vault_root=tmp_path.resolve(),
        biblio_skip_prefix="z_item",
        zotero_sqlite=db,
    )


@pytest.fixture()
def fake_zotero(monkeypatch):
    """Make ``build_inventory`` read a supplied item list instead of SQLite."""
    def _install(items: list[zotero.ZoteroItem]) -> None:
        monkeypatch.setattr(
            inventory.zotero, "read_items", lambda *_a, **_k: list(items)
        )
    return _install


# --------------------------------------------------------------------------
# The index helpers
# --------------------------------------------------------------------------

def test_citation_key_index_is_first_wins_and_skips_keyless_items():
    """A Zotero library CAN hold two items with the same BBT key.

    BBT only guarantees uniqueness at export time, and a duplicated item keeps
    its pinned key — two such pairs exist in the maintainer's library. The
    policy is deterministic first-wins so a scan stays reproducible; the loser
    is left UNJOINED, never mis-joined onto someone else's record.
    """
    idx = inventory._index_zotero_by_citation_key([
        _item("AAA", "First", citation_key="dupKey2020"),
        _item("BBB", "Second", citation_key="dupKey2020"),
        _item("CCC", "No key at all", citation_key=None),
        _item("DDD", "Blank key", citation_key="   "),
    ])
    assert idx["dupKey2020"].item_key == "AAA"
    assert len(idx) == 1


# --------------------------------------------------------------------------
# The join
# --------------------------------------------------------------------------

def test_identical_titles_no_longer_collapse_onto_one_item(tmp_path, fake_zotero):
    """The live bug, reduced: four papers, one title, four distinct items.

    Pre-fix all four records shared the first item's key, so tagging any of
    them wrote onto whichever won. Each must now hold its own.
    """
    settings = _vault_with_bib(tmp_path, "\n".join(
        f"@article{{{k}, title={{Schizophrenia}}, year={{{y}}}, author={{{a}}}}}"
        for k, y, a in (
            ("owenSchizophrenia2016", "2016", "Owen, M"),
            ("marderSchizophrenia2019", "2019", "Marder, S"),
            ("leuchtSchizophrenia2025", "2025", "Leucht, S"),
            ("jauharSchizophrenia2022", "2022", "Jauhar, S"),
        )
    ))
    fake_zotero([
        _item("KEYOWEN", "Schizophrenia", citation_key="owenSchizophrenia2016"),
        _item("KEYMARDER", "Schizophrenia", citation_key="marderSchizophrenia2019"),
        _item("KEYLEUCHT", "Schizophrenia", citation_key="leuchtSchizophrenia2025"),
        _item("KEYJAUHAR", "Schizophrenia", citation_key="jauharSchizophrenia2022"),
    ])

    inv = inventory.build_inventory(settings, count_annotations=False)

    got = {k: r.zotero_item.item_key for k, r in inv.records.items()}
    assert got == {
        "owenSchizophrenia2016": "KEYOWEN",
        "marderSchizophrenia2019": "KEYMARDER",
        "leuchtSchizophrenia2025": "KEYLEUCHT",
        "jauharSchizophrenia2022": "KEYJAUHAR",
    }
    assert len(set(got.values())) == 4, "each paper must hold a distinct item"
    assert inv.zotero_matched_by_key == 4
    assert inv.zotero_matched_by_title == 0


def test_citation_key_beats_title_when_they_disagree(tmp_path, fake_zotero):
    """Titles drift between the two stores; the key does not."""
    settings = _vault_with_bib(
        tmp_path,
        "@article{smith2020, title={The original title}, year={2020}, "
        "author={Smith, J}}",
    )
    fake_zotero([
        # A decoy whose title matches the bib exactly but is a different item.
        _item("DECOY", "The original title", citation_key="other2019"),
        _item("RIGHT", "A retitled version", citation_key="smith2020"),
    ])

    inv = inventory.build_inventory(settings, count_annotations=False)

    assert inv.records["smith2020"].zotero_item.item_key == "RIGHT"
    assert inv.zotero_matched_by_key == 1
    assert inv.zotero_matched_by_title == 0


def test_keyless_item_still_joins_by_title(tmp_path, fake_zotero):
    """The fallback survives for items predating BBT / with no key generated."""
    settings = _vault_with_bib(
        tmp_path,
        "@article{smith2020, title={A studied thing}, year={2020}, "
        "author={Smith, J}}",
    )
    fake_zotero([_item("OLDITEM", "A studied thing", citation_key=None)])

    inv = inventory.build_inventory(settings, count_annotations=False)

    assert inv.records["smith2020"].zotero_item.item_key == "OLDITEM"
    assert inv.zotero_matched_by_key == 0
    assert inv.zotero_matched_by_title == 1


def test_no_match_leaves_the_record_unjoined(tmp_path, fake_zotero):
    """Neither key nor title: unjoined is the correct answer, not a guess.

    The write path reads this as `skipped: "no_item"`, which is exactly right —
    far better than the mis-join it replaces.
    """
    settings = _vault_with_bib(
        tmp_path,
        "@article{smith2020, title={Alpha}, year={2020}, author={Smith, J}}",
    )
    fake_zotero([_item("ELSE", "Beta", citation_key="jones1999")])

    inv = inventory.build_inventory(settings, count_annotations=False)

    assert inv.records["smith2020"].zotero_item is None
    assert inv.zotero_matched_by_key == 0
    assert inv.zotero_matched_by_title == 0


# --------------------------------------------------------------------------
# The fallback's own refusals (2026-08-12 review, M1)
# --------------------------------------------------------------------------

def test_stale_bib_key_does_not_steal_a_keyed_item_by_title(tmp_path, fake_zotero):
    """The surviving half of the mis-join bug.

    `_master.bib` holds a STALE key — the item was re-keyed in BBT after the
    last export — and another paper with the SAME title is properly keyed. The
    key lookup misses, and the old fallback matched against every item
    including keyed ones, so the stale record joined onto Owen's item: two
    records owning one item again, and `apply-zotero-tags` writing Smith's tags
    onto Owen's paper.

    The item is already claimed, so the fallback must refuse. Unjoined is
    recoverable (re-export the bib); a wrong write is not.
    """
    settings = _vault_with_bib(tmp_path, "\n".join((
        "@article{smith2020, title={Schizophrenia}, year={2020}, author={Smith, J}}",
        "@article{owen2016, title={Schizophrenia}, year={2016}, author={Owen, M}}",
    )))
    fake_zotero([
        _item("KEYOWEN", "Schizophrenia", citation_key="owen2016"),
        # Smith's item now carries the re-keyed `smith2020a`, so `smith2020`
        # matches nothing by key.
        _item("KEYSMITH", "Schizophrenia", citation_key="smith2020a"),
    ])

    inv = inventory.build_inventory(settings, count_annotations=False)

    assert inv.records["owen2016"].zotero_item.item_key == "KEYOWEN"
    assert inv.records["smith2020"].zotero_item is None
    assert inv.zotero_matched_by_key == 1
    assert inv.zotero_matched_by_title == 0
    assert inv.zotero_title_refused == 1


def test_ambiguous_title_is_refused_rather_than_guessed(tmp_path, fake_zotero):
    """Two key-less items share a title: no evidence picks between them."""
    settings = _vault_with_bib(
        tmp_path,
        "@article{smith2020, title={Schizophrenia}, year={2020}, author={Smith, J}}",
    )
    fake_zotero([
        _item("ONE", "Schizophrenia", citation_key=None),
        _item("TWO", "Schizophrenia", citation_key=None),
    ])

    inv = inventory.build_inventory(settings, count_annotations=False)

    assert inv.records["smith2020"].zotero_item is None
    assert inv.zotero_matched_by_title == 0
    assert inv.zotero_title_refused == 1


def test_two_records_contending_for_one_item_both_refuse(tmp_path, fake_zotero):
    """Deterministic refusal beats an order-dependent winner.

    `all_keys` is a set, so "first one wins the item" would hand it to a
    different record depending on hash order — a scan that is not reproducible
    across runs of the same library.
    """
    settings = _vault_with_bib(tmp_path, "\n".join((
        "@article{a2020, title={Schizophrenia}, year={2020}, author={Smith, J}}",
        "@article{b2021, title={Schizophrenia}, year={2021}, author={Jones, K}}",
    )))
    fake_zotero([_item("ONLY", "Schizophrenia", citation_key=None)])

    inv = inventory.build_inventory(settings, count_annotations=False)

    assert inv.records["a2020"].zotero_item is None
    assert inv.records["b2021"].zotero_item is None
    assert inv.zotero_matched_by_title == 0
    assert inv.zotero_title_refused == 2


def test_title_diagnostic_counts_records_not_distinct_titles(tmp_path, fake_zotero):
    """The two diagnostics are advertised as a ratio; count the same unit.

    `zotero_matched_by_title` counted distinct normalized titles while
    `zotero_matched_by_key` counted records, so the "rising title share" the
    docstring tells you to watch had different denominators on each axis.
    """
    settings = _vault_with_bib(tmp_path, "\n".join((
        "@article{a2020, title={Alpha}, year={2020}, author={Smith, J}}",
        "@article{b2021, title={Beta}, year={2021}, author={Jones, K}}",
    )))
    fake_zotero([
        _item("IA", "Alpha", citation_key=None),
        _item("IB", "Beta", citation_key=None),
    ])

    inv = inventory.build_inventory(settings, count_annotations=False)

    assert inv.zotero_matched_by_title == 2
    assert inv.zotero_title_refused == 0
