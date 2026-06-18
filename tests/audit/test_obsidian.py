"""Coverage for the Obsidian vault parser (ported from kb_harmonizer)."""

from __future__ import annotations

from pathlib import Path

from audit.core import obsidian


def _w(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_frontmatter_yaml_list_tags(tmp_path: Path) -> None:
    p = _w(tmp_path, "n.md", "---\ntags: [alpha, beta]\n---\nbody")
    n = obsidian.read_note(p)
    assert n is not None
    assert n.has_frontmatter
    assert sorted(n.tags) == ["alpha", "beta"]


def test_frontmatter_inline_string_tags(tmp_path: Path) -> None:
    p = _w(tmp_path, "n.md", "---\ntags: alpha beta\n---\nbody")
    n = obsidian.read_note(p)
    assert n is not None
    assert sorted(n.tags) == ["alpha", "beta"]


def test_frontmatter_tags_strip_hash(tmp_path: Path) -> None:
    p = _w(tmp_path, "n.md", "---\ntags: ['#alpha', '#beta']\n---\nbody")
    n = obsidian.read_note(p)
    assert n is not None
    assert sorted(n.tags) == ["alpha", "beta"]


def test_no_frontmatter(tmp_path: Path) -> None:
    p = _w(tmp_path, "n.md", "no frontmatter here\n")
    n = obsidian.read_note(p)
    assert n is not None
    assert n.has_frontmatter is False
    assert n.tags == []


def test_broken_yaml_falls_back(tmp_path: Path) -> None:
    p = _w(tmp_path, "n.md", "---\n!!! broken yaml: : :\n---\nbody")
    n = obsidian.read_note(p)
    assert n is not None
    assert n.tags == []


def test_find_pdf_links_wikilink(tmp_path: Path) -> None:
    p = _w(tmp_path, "n.md", "Read [[smith_2020.pdf]] today.")
    n = obsidian.read_note(p)
    assert n is not None
    assert n.pdf_links == {"smith_2020.pdf"}


def test_find_pdf_links_wikilink_with_alias(tmp_path: Path) -> None:
    p = _w(tmp_path, "n.md", "Read [[smith_2020.pdf|Smith 2020]] today.")
    n = obsidian.read_note(p)
    assert n is not None
    assert n.pdf_links == {"smith_2020.pdf"}


def test_find_pdf_links_markdown_link(tmp_path: Path) -> None:
    p = _w(tmp_path, "n.md", "See [Smith](Z_attachments/smith%202020.pdf).")
    n = obsidian.read_note(p)
    assert n is not None
    assert n.pdf_links == {"smith 2020.pdf"}


def test_find_pdf_links_ignores_non_pdf(tmp_path: Path) -> None:
    p = _w(tmp_path, "n.md", "[[note]] and [[image.png]] and [[doc.pdf]]")
    n = obsidian.read_note(p)
    assert n is not None
    assert n.pdf_links == {"doc.pdf"}


def test_walk_markdown_skips_ignored_dirs(tmp_path: Path) -> None:
    _w(tmp_path, "ok.md", "x")
    _w(tmp_path, "sub/ok.md", "x")
    _w(tmp_path, ".obsidian/skip.md", "x")
    _w(tmp_path, ".trash/skip.md", "x")
    paths = sorted(obsidian.walk_markdown(tmp_path, frozenset({".obsidian", ".trash"})))
    assert [p.name for p in paths] == ["ok.md", "ok.md"]
    assert all(".obsidian" not in p.parts for p in paths)
    assert all(".trash" not in p.parts for p in paths)


def test_scan_vault_yields_notes(tmp_path: Path) -> None:
    _w(tmp_path, "a.md", "---\ntags: [x]\n---\nbody")
    _w(tmp_path, "b.md", "no frontmatter")
    notes = list(obsidian.scan_vault(tmp_path, frozenset()))
    assert len(notes) == 2
