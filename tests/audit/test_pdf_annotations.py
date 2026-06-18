"""Coverage for the AnnotationsResult / read_annotations contract
(ported from kb_harmonizer)."""

from __future__ import annotations

from pathlib import Path

from audit.core import pdf_annotations


def test_missing_file_returns_missing_error(tmp_path: Path) -> None:
    res = pdf_annotations.read_annotations(tmp_path / "nope.pdf")
    assert res.count == -1
    assert res.error == "missing"


def test_corrupt_file_returns_corrupt_error(tmp_path: Path) -> None:
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"not actually a pdf")
    res = pdf_annotations.read_annotations(p)
    assert res.count == -1
    assert res.error in {"corrupt", "other"}


def test_count_annotations_back_compat_signature(tmp_path: Path) -> None:
    assert pdf_annotations.count_annotations(tmp_path / "nope.pdf") == -1


def test_looks_read_returns_false_on_unreadable(tmp_path: Path) -> None:
    assert pdf_annotations.looks_read(tmp_path / "nope.pdf", threshold=1) is False
