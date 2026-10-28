"""Coverage for the cross-source inventory builder.

Light tests that hit a real but tiny vault — the engine itself is
exercised end-to-end by the kb_harmonizer port; this file focuses on
the wiring contracts we modified after vendoring (empty
``biblio_skip_prefix`` guard, ``cancel_fn`` plumbing).
"""

from __future__ import annotations

from pathlib import Path

from audit.config import Settings
from audit.engine import inventory


def _make_vault(tmp_path: Path) -> Path:
    """Build a minimal vault matching the default subpath conventions."""
    biblio = tmp_path / "Z_attachments" / "biblio_articles"
    biblio.mkdir(parents=True)
    # _master.bib at the vault root matches the neutral default master_bib_path.
    (tmp_path / "_master.bib").write_text(
        "@article{smith2020, title={X}, year={2020}, author={Smith, J}}",
        encoding="utf-8",
    )
    return biblio


def test_empty_skip_prefix_does_not_skip_every_pdf(tmp_path: Path) -> None:
    """Regression: an empty ``biblio_skip_prefix`` would cause
    ``str.startswith("")`` to be True for every filename, collapsing
    ``pdfs_active`` to an empty list.  The engine must treat empty
    prefix as "skip nothing"."""
    biblio = _make_vault(tmp_path)
    (biblio / "paper.pdf").write_bytes(b"%PDF-1.4 fake")
    (biblio / "another.pdf").write_bytes(b"%PDF-1.4 fake")

    settings = Settings(vault_root=tmp_path.resolve(), zotero_sqlite=tmp_path / "absent.sqlite", biblio_skip_prefix="")
    inv = inventory.build_inventory(settings, count_annotations=False)

    assert len(inv.pdfs_skipped) == 0
    # Both PDFs end up unmapped (no matching bib entry) but they are at
    # least *considered* — the bug would have skipped them entirely.
    assert len(inv.bridge.unmapped_pdfs) == 2


def test_skip_prefix_filters_matching_pdfs(tmp_path: Path) -> None:
    biblio = _make_vault(tmp_path)
    (biblio / "z_item_skip_me.pdf").write_bytes(b"%PDF-1.4 fake")
    (biblio / "regular.pdf").write_bytes(b"%PDF-1.4 fake")

    settings = Settings(vault_root=tmp_path.resolve(), zotero_sqlite=tmp_path / "absent.sqlite", biblio_skip_prefix="z_item")
    inv = inventory.build_inventory(settings, count_annotations=False)

    assert [p.name for p in inv.pdfs_skipped] == ["z_item_skip_me.pdf"]
    assert "regular.pdf" in {p.name for p in inv.bridge.unmapped_pdfs}


def test_skip_prefix_is_case_insensitive(tmp_path: Path) -> None:
    """B3 (2026-07-13): a prefix saved with user case (``Z_item``) must still
    match the lower()ed filenames — the compare lowercases both sides."""
    biblio = _make_vault(tmp_path)
    (biblio / "z_item_skip_me.pdf").write_bytes(b"%PDF-1.4 fake")
    (biblio / "regular.pdf").write_bytes(b"%PDF-1.4 fake")

    settings = Settings(vault_root=tmp_path.resolve(), zotero_sqlite=tmp_path / "absent.sqlite", biblio_skip_prefix="Z_item")
    inv = inventory.build_inventory(settings, count_annotations=False)

    assert [p.name for p in inv.pdfs_skipped] == ["z_item_skip_me.pdf"]


def test_cancel_fn_short_circuits_inventory(tmp_path: Path) -> None:
    """``build_inventory`` checks the cancel callback between phases."""
    _make_vault(tmp_path)
    settings = Settings(vault_root=tmp_path.resolve(), zotero_sqlite=tmp_path / "absent.sqlite")
    # Cancellation is checked AFTER the bib walk inside the annotations
    # loop.  With no PDFs the loop never runs, so the cancel is no-op —
    # we just verify the call signature accepts the callback.
    inv = inventory.build_inventory(
        settings, count_annotations=False, cancel_fn=lambda: True
    )
    assert inv.records is not None


def test_cancelled_scan_still_builds_every_record(tmp_path: Path) -> None:
    """2026-08-12 adjudicated finding 3, fixed 2026-08-13.

    The records loop used to ``break`` on cancel, dropping every remaining
    citation key — and ``all_keys`` is a set, so WHICH keys survived was
    hash-order-arbitrary. The manager stores a cancelled scan's inventory and
    every consumer gates on presence, never state, so reports, tag_coverage
    and the widget served that arbitrary subset as the whole library. A cancel
    now skips only the expensive per-record annotation reads (the thing worth
    cancelling): every record is still built, with its joins, and the skipped
    annotation counts stay at the documented "unknown" default (-1).
    """
    biblio = tmp_path / "Z_attachments" / "biblio_articles"
    biblio.mkdir(parents=True)
    (tmp_path / "_master.bib").write_text(
        "@article{smith2020, title={X}, year={2020}, author={Smith, J}}\n"
        "@article{jones2021, title={Y}, year={2021}, author={Jones, A}}\n"
        "@article{li2022, title={Z}, year={2022}, author={Li, B}}\n",
        encoding="utf-8",
    )
    # One filename-mapped PDF per entry, so under the OLD code the first
    # iterated record hit the cancel check and the break dropped the rest.
    for name in ("smith_2020.pdf", "jones_2021.pdf", "li_2022.pdf"):
        (biblio / name).write_bytes(b"%PDF-1.4 fake")

    settings = Settings(vault_root=tmp_path.resolve(), zotero_sqlite=tmp_path / "absent.sqlite")
    inv = inventory.build_inventory(
        settings, count_annotations=True, cancel_fn=lambda: True
    )

    assert set(inv.records) == {"smith2020", "jones2021", "li2022"}
    for rec in inv.records.values():
        assert rec.pdf_paths, "the filename join must still have run"
        assert rec.annotations_count_max == -1  # skipped, not fabricated


def test_zotero_attachments_are_flattened_across_every_item(tmp_path: Path,
                                                            monkeypatch) -> None:
    """The flat list is built from the FULL item list, before the join.

    Built from ``records`` instead, an attachment on an item no record claimed
    would be invisible — ``OrphanZoteroItem`` deliberately keeps only display
    fields, so there would be nowhere for it to live. That is the whole reason
    this is a separate list rather than something derived per citation key, and
    it is what this asserts: the unjoined item's attachment survives.
    """
    from types import SimpleNamespace

    from audit.core import zotero

    _make_vault(tmp_path)

    def _att(key, parent, mode, ann=0):
        return zotero.ZoteroAttachment(
            item_key=key, parent_key=parent, filename=f"{key}.pdf",
            storage_path=None, is_stored=False, content_type="application/pdf",
            link_mode=mode, annotation_count=ann, raw_path=None,
            title="Full Text PDF")

    joined = SimpleNamespace(
        item_key="P1", item_type="journalArticle", title="Joined",
        citation_key="smith2020", tags=[], child_notes=[],
        library_type="user", group_id=None, abstract=None,
        container=None, date=None, doi=None,
        attachments=[_att("ATT1", "P1", "imported_file", ann=2)])
    unjoined = SimpleNamespace(
        item_key="P2", item_type="webpage", title="No record claims me",
        citation_key=None, tags=[], child_notes=[],
        library_type="group", group_id=99, abstract=None,
        container=None, date=None, doi=None,
        attachments=[_att("ATT2", "P2", "linked_file")])

    settings = Settings(vault_root=tmp_path.resolve(),
                        zotero_sqlite=tmp_path / "zotero.sqlite")
    (tmp_path / "zotero.sqlite").write_bytes(b"")
    monkeypatch.setattr(inventory.zotero, "read_items",
                        lambda *a, **k: [joined, unjoined])

    inv = inventory.build_inventory(settings, count_annotations=False)

    rows = {r.item_key: r for r in inv.zotero_attachments}
    assert set(rows) == {"ATT1", "ATT2"}
    assert rows["ATT1"].parent_citation_key == "smith2020"
    assert rows["ATT1"].annotation_count == 2
    assert rows["ATT1"].file_owned_by_zotero is True
    # The unjoined item's attachment is present, with the group routing a
    # zotero://select deep link needs.
    assert rows["ATT2"].parent_citation_key is None
    assert rows["ATT2"].file_owned_by_zotero is False
    assert (rows["ATT2"].library_type, rows["ATT2"].group_id) == ("group", 99)
    # The attachment's OWN title rides the row, distinct from the path's
    # basename (annual cleanup T4: the phase-04b proposer compares the two).
    assert rows["ATT1"].title == "Full Text PDF"
    assert rows["ATT1"].filename == "ATT1.pdf"
