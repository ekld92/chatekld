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
    assert [p.name for p in tmp_path.iterdir() if p.is_file()] == ["mapping.json"]


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
