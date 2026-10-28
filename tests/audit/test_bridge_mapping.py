"""Coverage for the mapping.json edit helpers (ported from kb_harmonizer)."""

from __future__ import annotations

import json
from pathlib import Path

from audit.engine import bridge


def _read(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def test_add_match_creates_file(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    pdf = vault / "Z_attachments" / "smith_2020.pdf"
    pdf.parent.mkdir()
    pdf.touch()
    mapping = tmp_path / "mapping.json"

    bridge.add_match(mapping, pdf, "smith2020", vault)

    data = _read(mapping)
    assert data["matches"] == {"smith2020": ["Z_attachments/smith_2020.pdf"]}
    assert data["no_match"] == []


def test_add_match_appends_dedup(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    pdf = vault / "smith_2020.pdf"
    pdf.touch()
    mapping = tmp_path / "mapping.json"

    bridge.add_match(mapping, pdf, "smith2020", vault)
    bridge.add_match(mapping, pdf, "smith2020", vault)  # duplicate; should noop

    data = _read(mapping)
    assert data["matches"]["smith2020"] == ["smith_2020.pdf"]


def test_add_match_clears_existing_no_match(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    pdf = vault / "smith_2020.pdf"
    pdf.touch()
    mapping = tmp_path / "mapping.json"

    bridge.add_no_match(mapping, pdf, vault)
    assert "smith_2020.pdf" in _read(mapping)["no_match"]

    bridge.add_match(mapping, pdf, "smith2020", vault)
    data = _read(mapping)
    assert data["no_match"] == []
    assert data["matches"] == {"smith2020": ["smith_2020.pdf"]}


def test_add_no_match_clears_existing_match(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    pdf = vault / "smith_2020.pdf"
    pdf.touch()
    mapping = tmp_path / "mapping.json"

    bridge.add_match(mapping, pdf, "smith2020", vault)
    bridge.add_no_match(mapping, pdf, vault)

    data = _read(mapping)
    assert data["matches"] == {}
    assert data["no_match"] == ["smith_2020.pdf"]


def test_add_no_match_removes_key_when_emptied(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    pdf1 = vault / "a.pdf"
    pdf2 = vault / "b.pdf"
    pdf1.touch()
    pdf2.touch()
    mapping = tmp_path / "mapping.json"

    bridge.add_match(mapping, pdf1, "key1", vault)
    bridge.add_match(mapping, pdf2, "key1", vault)
    bridge.add_no_match(mapping, pdf1, vault)

    data = _read(mapping)
    assert data["matches"] == {"key1": ["b.pdf"]}
    assert data["no_match"] == ["a.pdf"]


def test_save_mapping_failed_replace_preserves_existing(
    tmp_path: Path, monkeypatch
) -> None:
    """A crash during the atomic promote must leave the prior mapping intact.

    mapping.json holds hand-curated matches that a rescan cannot regenerate,
    so save_mapping writes via temp-sibling + os.replace; this simulates the
    process dying at the rename and asserts no data is lost and no temp file
    is left behind.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    pdf = vault / "smith_2020.pdf"
    pdf.touch()
    mapping = tmp_path / "mapping.json"
    bridge.add_match(mapping, pdf, "smith2020", vault)
    before = _read(mapping)

    monkeypatch.setattr(bridge.os, "replace", _raise_oserror)
    try:
        bridge.save_mapping(mapping, {"other": ["x.pdf"]}, set())
    except OSError:
        pass
    else:  # pragma: no cover - the injected failure must propagate
        raise AssertionError("expected OSError from injected os.replace failure")

    assert _read(mapping) == before
    assert {p.name for p in tmp_path.iterdir() if p.is_file()} == {"mapping.json", "mapping.json.lock"}


def _raise_oserror(*_args, **_kwargs):
    raise OSError("simulated crash at rename")


def test_path_outside_vault_falls_back_to_absolute(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    pdf = tmp_path / "outside.pdf"
    pdf.touch()
    mapping = tmp_path / "mapping.json"

    bridge.add_no_match(mapping, pdf, vault)
    data = _read(mapping)
    assert data["no_match"] == [str(pdf.resolve())]


def test_reassigning_a_pdf_sweeps_the_previous_key(tmp_path: Path) -> None:
    """A PDF belongs to exactly ONE citation key.

    ``add_no_matches`` already sweeps a PDF out of every key's match list
    before recording its decision; ``add_matches`` did not, so re-assigning a
    PDF from key A to key B left it listed under BOTH and the next scan gave
    both records the PDF (double-counted in drift, tagging, Finder unions and
    duplicate detection). Reachable from the ambiguous-PDF picker and, since
    the accession work made re-linking the primary action, from every Link.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    pdf = vault / "smith_2020.pdf"
    pdf.touch()
    mapping = tmp_path / "mapping.json"

    bridge.add_match(mapping, pdf, "smithWrongKey2020", vault)
    bridge.add_match(mapping, pdf, "smithRightKey2021", vault)

    data = _read(mapping)
    assert data["matches"] == {"smithRightKey2021": ["smith_2020.pdf"]}, (
        "re-assignment must move the PDF, not duplicate it"
    )


def test_reassigning_leaves_other_pdfs_of_the_old_key_alone(tmp_path: Path) -> None:
    """The sweep is per-PDF: a key legitimately holding several PDFs keeps them."""
    vault = tmp_path / "vault"
    vault.mkdir()
    moved, kept = vault / "a_2020.pdf", vault / "b_2020.pdf"
    moved.touch()
    kept.touch()
    mapping = tmp_path / "mapping.json"

    bridge.add_matches(mapping, [(moved, "oldKey"), (kept, "oldKey")], vault)
    bridge.add_match(mapping, moved, "newKey", vault)

    data = _read(mapping)
    assert data["matches"]["oldKey"] == ["b_2020.pdf"]
    assert data["matches"]["newKey"] == ["a_2020.pdf"]


# ── provenance annotation (2026-08-19 review, pairings board) ────────────────


def test_add_match_source_annotation_round_trips_into_the_bridge(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    pdf = vault / "Z_attachments" / "smith_2020.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.touch()
    mapping = tmp_path / "mapping.json"

    bridge.add_match(mapping, pdf, "smith2020", vault, source="accession")
    data = _read(mapping)
    assert data["sources"] == {"Z_attachments/smith_2020.pdf": "accession"}

    # The next scan stamps the label into source_per_pdf.
    class _Settings:
        vault_root = vault
        mapping_file = mapping
        zotero_notes_dir = vault / "no_notes"
        ignored_dirs: set[str] = set()

    result = bridge.build_bridge(_Settings(), [], [pdf])
    assert result.source_per_pdf[pdf] == "accession"


def test_default_add_match_stores_no_sources_key(tmp_path: Path) -> None:
    # "manual" is the implied label — a plain hand curation keeps the legacy
    # file shape byte-compatible (no sources key at all).
    vault = tmp_path / "vault"
    vault.mkdir()
    pdf = vault / "smith_2020.pdf"
    pdf.touch()
    mapping = tmp_path / "mapping.json"
    bridge.add_match(mapping, pdf, "smith2020", vault)
    assert "sources" not in _read(mapping)


def test_hand_recuration_clears_an_accession_label(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    pdf = vault / "smith_2020.pdf"
    pdf.touch()
    mapping = tmp_path / "mapping.json"
    bridge.add_match(mapping, pdf, "smith2020", vault, source="accession")
    bridge.add_match(mapping, pdf, "other2021", vault)   # hand re-assign
    assert "sources" not in _read(mapping)


def test_no_match_and_clear_drop_the_label(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    pdf = vault / "smith_2020.pdf"
    pdf.touch()
    mapping = tmp_path / "mapping.json"

    bridge.add_match(mapping, pdf, "smith2020", vault, source="accession")
    bridge.add_no_match(mapping, pdf, vault)
    assert "sources" not in _read(mapping)

    bridge.add_match(mapping, pdf, "smith2020", vault, source="accession")
    assert _read(mapping)["sources"]
    bridge.clear_curation(mapping, pdf, vault)
    assert "sources" not in _read(mapping)
