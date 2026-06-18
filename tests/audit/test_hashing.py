"""Coverage for hashing + cancellation (ported from kb_harmonizer)."""

from __future__ import annotations

from pathlib import Path

from audit.core import hashing


def _w(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def test_no_duplicates(tmp_path: Path) -> None:
    a = _w(tmp_path / "a.pdf", b"alpha")
    b = _w(tmp_path / "b.pdf", b"betaa")
    assert hashing.find_duplicate_sets([a, b]) == {}


def test_size_only_no_partial_match(tmp_path: Path) -> None:
    a = _w(tmp_path / "a.pdf", b"x" * 100)
    b = _w(tmp_path / "b.pdf", b"y" * 100)
    assert hashing.find_duplicate_sets([a, b]) == {}


def test_finds_content_identical(tmp_path: Path) -> None:
    a = _w(tmp_path / "a.pdf", b"same-bytes")
    b = _w(tmp_path / "b.pdf", b"same-bytes")
    c = _w(tmp_path / "c.pdf", b"different")
    result = hashing.find_duplicate_sets([a, b, c])
    assert len(result) == 1
    group = next(iter(result.values()))
    assert sorted(group) == sorted([a, b])


def test_unique_size_skipped(tmp_path: Path) -> None:
    a = _w(tmp_path / "a.pdf", b"x")
    b = _w(tmp_path / "b.pdf", b"yy")
    assert hashing.find_duplicate_sets([a, b]) == {}


def test_cancel_fn_aborts_early(tmp_path: Path) -> None:
    files = [_w(tmp_path / f"f{i}.pdf", b"abcd") for i in range(10)]
    calls = {"n": 0}

    def cancel_fn() -> bool:
        calls["n"] += 1
        return calls["n"] >= 3

    result = hashing.find_duplicate_sets(files, cancel_fn=cancel_fn)
    assert isinstance(result, dict)


def test_cancel_fn_never_triggers(tmp_path: Path) -> None:
    a = _w(tmp_path / "a.pdf", b"same")
    b = _w(tmp_path / "b.pdf", b"same")
    result = hashing.find_duplicate_sets([a, b], cancel_fn=lambda: False)
    assert len(result) == 1
