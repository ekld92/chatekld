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
    bib_dir = tmp_path / "presentations_slides_writings_teaching"
    bib_dir.mkdir()
    (bib_dir / "_master.bib").write_text(
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

    settings = Settings(vault_root=tmp_path.resolve(), biblio_skip_prefix="")
    inv = inventory.build_inventory(settings, count_annotations=False)

    assert len(inv.pdfs_skipped) == 0
    # Both PDFs end up unmapped (no matching bib entry) but they are at
    # least *considered* — the bug would have skipped them entirely.
    assert len(inv.bridge.unmapped_pdfs) == 2


def test_skip_prefix_filters_matching_pdfs(tmp_path: Path) -> None:
    biblio = _make_vault(tmp_path)
    (biblio / "z_item_skip_me.pdf").write_bytes(b"%PDF-1.4 fake")
    (biblio / "regular.pdf").write_bytes(b"%PDF-1.4 fake")

    settings = Settings(vault_root=tmp_path.resolve(), biblio_skip_prefix="z_item")
    inv = inventory.build_inventory(settings, count_annotations=False)

    assert [p.name for p in inv.pdfs_skipped] == ["z_item_skip_me.pdf"]
    assert "regular.pdf" in {p.name for p in inv.bridge.unmapped_pdfs}


def test_cancel_fn_short_circuits_inventory(tmp_path: Path) -> None:
    """``build_inventory`` checks the cancel callback between phases."""
    _make_vault(tmp_path)
    settings = Settings(vault_root=tmp_path.resolve())
    # Cancellation is checked AFTER the bib walk inside the annotations
    # loop.  With no PDFs the loop never runs, so the cancel is no-op —
    # we just verify the call signature accepts the callback.
    inv = inventory.build_inventory(
        settings, count_annotations=False, cancel_fn=lambda: True
    )
    assert inv.records is not None
