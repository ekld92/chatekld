"""Axis-ii additions: hash-anchored mapping recovery + frontmatter tag merge."""
from __future__ import annotations

from pathlib import Path

from api.routes._audit_tagwrite import merge_tags_into_frontmatter
from audit.engine import bridge


class _Settings:
    def __init__(self, vault: Path, mapping: Path):
        self.vault_root = vault
        self.mapping_file = mapping
        self.zotero_notes_dir = vault / "Z_Zotero_Notes"
        self.ignored_dirs: set[str] = set()


def test_hash_anchor_follows_rename_and_move(tmp_path: Path) -> None:
    vault = tmp_path
    (vault / "a").mkdir()
    pdf = vault / "a" / "orig.pdf"
    pdf.write_bytes(b"%PDF-1.4 unique-body-A")
    mapping = vault / "mapping.json"
    bridge.add_match(mapping, pdf, "smith2020", vault)

    # The stored mapping now carries a content hash.
    assert bridge._load_hashes(mapping)  # non-empty

    # Move + rename the file, same bytes.
    pdf.unlink()
    (vault / "b").mkdir()
    moved = vault / "b" / "renamed.pdf"
    moved.write_bytes(b"%PDF-1.4 unique-body-A")

    res = bridge.build_bridge(_Settings(vault, mapping), [], [moved])
    assert res.pdf_to_bib.get(moved) == "smith2020"
    assert res.source_per_pdf.get(moved) == "manual_hash"


def test_corrupt_mapping_is_preserved_before_overwrite(tmp_path: Path) -> None:
    """B10/#39 (2026-07-13): a writer must not clobber a corrupt-but-recoverable
    mapping.json — it is renamed aside first (mapping.json is unregenerable)."""
    vault = tmp_path
    pdf = vault / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4 body")
    mapping = vault / "mapping.json"
    mapping.write_text("{ this is not valid json ", encoding="utf-8")

    bridge.add_match(mapping, pdf, "keyA", vault)

    # The new mapping parses and holds the new entry.
    matches, _no, _h = bridge._load_mapping_full(mapping)
    assert matches.get("keyA")
    # The corrupt original was preserved, not lost.
    preserved = list(vault.glob("mapping.json.corrupt-*"))
    assert preserved, "corrupt mapping.json was not preserved"
    assert preserved[0].read_text(encoding="utf-8") == "{ this is not valid json "


def test_hash_anchor_recovers_no_match(tmp_path: Path) -> None:
    vault = tmp_path
    pdf = vault / "junk.pdf"
    pdf.write_bytes(b"not-in-bib-bytes")
    mapping = vault / "mapping.json"
    bridge.add_no_match(mapping, pdf, vault)

    pdf.unlink()
    moved = vault / "sub"
    moved.mkdir()
    moved_pdf = moved / "junk-renamed.pdf"
    moved_pdf.write_bytes(b"not-in-bib-bytes")

    res = bridge.build_bridge(_Settings(vault, mapping), [], [moved_pdf])
    assert moved_pdf in res.confirmed_no_match_pdfs
    assert moved_pdf not in res.unmapped_pdfs


def test_hash_anchor_no_recovery_when_path_present(tmp_path: Path) -> None:
    # A present path is claimed directly; a *different* file at a new path is
    # NOT hijacked by an unrelated hash.
    vault = tmp_path
    pdf = vault / "a.pdf"
    pdf.write_bytes(b"content-A")
    mapping = vault / "mapping.json"
    bridge.add_match(mapping, pdf, "keyA", vault)

    other = vault / "b.pdf"
    other.write_bytes(b"content-B-different")

    res = bridge.build_bridge(_Settings(vault, mapping), [], [pdf, other])
    assert res.pdf_to_bib.get(pdf) == "keyA"
    assert res.source_per_pdf.get(pdf) == "manual"  # by path, not hash
    assert other in res.unmapped_pdfs


def test_hash_recovery_size_prefilter_skips_wrong_size(tmp_path: Path, monkeypatch) -> None:
    """Lot 2.2: a candidate whose size matches no orphan is never full-hashed."""
    vault = tmp_path
    pdf = vault / "a.pdf"
    pdf.write_bytes(b"orphan-content-AAAA")
    mapping = vault / "mapping.json"
    bridge.add_match(mapping, pdf, "keyA", vault)
    pdf.unlink()  # orphan it

    # Two candidates: right size (the moved file) and wrong size (unrelated).
    moved = vault / "moved.pdf"
    moved.write_bytes(b"orphan-content-AAAA")
    other = vault / "other.pdf"
    other.write_bytes(b"completely different length content")

    hashed: list[Path] = []
    real_full_hash = bridge.core_hashing.full_hash
    monkeypatch.setattr(
        bridge.core_hashing, "full_hash",
        lambda p: (hashed.append(p), real_full_hash(p))[1],
    )
    res = bridge.build_bridge(_Settings(vault, mapping), [], [moved, other])
    assert res.pdf_to_bib.get(moved) == "keyA"
    assert moved in hashed and other not in hashed  # prefilter screened `other`
    assert res.mapping_orphans == 1


def test_hash_recovery_refuses_duplicate_digest(tmp_path: Path) -> None:
    """Lot 2.2: two orphans with identical bytes under DIFFERENT keys are
    ambiguous — recovery must refuse, not pick an iteration-order winner."""
    vault = tmp_path
    p1 = vault / "one.pdf"
    p1.write_bytes(b"same-bytes")
    p2 = vault / "two.pdf"
    p2.write_bytes(b"same-bytes")
    mapping = vault / "mapping.json"
    bridge.add_match(mapping, p1, "keyA", vault)
    bridge.add_match(mapping, p2, "keyB", vault)
    p1.unlink()
    p2.unlink()

    moved = vault / "moved.pdf"
    moved.write_bytes(b"same-bytes")
    res = bridge.build_bridge(_Settings(vault, mapping), [], [moved])
    assert moved not in res.pdf_to_bib          # not guessed
    assert moved in res.unmapped_pdfs           # resurfaces for re-curation


def test_refresh_mapping_hashes_updates_present_entries(tmp_path: Path) -> None:
    """Lot 2.1: an annotate-in-place is re-anchored by the refresh, so a later
    move is still recoverable."""
    vault = tmp_path
    pdf = vault / "a.pdf"
    pdf.write_bytes(b"original bytes")
    mapping = vault / "mapping.json"
    bridge.add_match(mapping, pdf, "keyA", vault)

    pdf.write_bytes(b"original bytes + annotation stream")  # "annotate"
    assert bridge.refresh_mapping_hashes(mapping, vault) == 1

    pdf.unlink()
    moved = vault / "moved.pdf"
    moved.write_bytes(b"original bytes + annotation stream")
    res = bridge.build_bridge(_Settings(vault, mapping), [], [moved])
    assert res.pdf_to_bib.get(moved) == "keyA"  # recovered via the REFRESHED hash


def test_legacy_string_hashes_still_load(tmp_path: Path) -> None:
    """Lot 2.4: pre-lot mapping files stored `hashes: {rel: "sha"}` — they
    must keep loading (normalised to the dict shape, size=None)."""
    import json
    vault = tmp_path
    pdf = vault / "a.pdf"
    pdf.write_bytes(b"legacy content")
    from audit.core.hashing import full_hash
    mapping = vault / "mapping.json"
    mapping.write_text(json.dumps({
        "matches": {"keyA": ["a.pdf"]},
        "no_match": [],
        "hashes": {"a.pdf": full_hash(pdf)},
    }))
    pdf.unlink()
    moved = vault / "b.pdf"
    moved.write_bytes(b"legacy content")
    res = bridge.build_bridge(_Settings(vault, mapping), [], [moved])
    assert res.pdf_to_bib.get(moved) == "keyA"


def test_merge_tags_block_list(tmp_path: Path) -> None:
    import yaml
    text = "---\ntitle: X\ntags:\n  - a\n  - b\n---\nbody\n"
    new_text, added, reason = merge_tags_into_frontmatter(text, ["c", "a"])
    assert added == ["c"]  # "a" already present
    assert reason == ""
    fm = new_text.split("---")[1]
    assert yaml.safe_load(fm)["tags"] == ["a", "b", "c"]
    assert new_text.endswith("body\n")  # body preserved


def test_merge_tags_flow_and_scalar_and_missing() -> None:
    import yaml
    # flow
    nt, added, _ = merge_tags_into_frontmatter("---\ntags: [a]\n---\nb\n", ["c"])
    assert yaml.safe_load(nt.split("---")[1])["tags"] == ["a", "c"]
    # scalar
    nt, added, _ = merge_tags_into_frontmatter("---\ntags: solo\n---\nb\n", ["x"])
    assert yaml.safe_load(nt.split("---")[1])["tags"] == ["solo", "x"]
    # no tags key
    nt, added, _ = merge_tags_into_frontmatter("---\ntitle: X\n---\nb\n", ["a"])
    assert yaml.safe_load(nt.split("---")[1])["tags"] == ["a"]
    # no frontmatter at all
    nt, added, _ = merge_tags_into_frontmatter("just body\n", ["a", "b"])
    assert yaml.safe_load(nt.split("---")[1])["tags"] == ["a", "b"]
    assert nt.endswith("just body\n")


def test_merge_tags_frontmatter_fence_at_eof_no_double_block() -> None:
    """B10/#37 (2026-07-13): a frontmatter-only note whose closing fence sits at
    EOF with no trailing newline is recognised as frontmatter — merge into the
    existing block, never prepend a SECOND ``---`` block."""
    import yaml
    text = "---\ntags: [a]\n---"   # no final newline after the closing fence
    nt, added, reason = merge_tags_into_frontmatter(text, ["b"])
    assert reason == ""
    assert nt.count("---") == 2   # still exactly one fence pair
    assert yaml.safe_load(nt.split("---")[1])["tags"] == ["a", "b"]


def test_merge_tags_space_separated_scalar_not_fused() -> None:
    """A3 (2026-07-13): a whitespace-separated scalar is MULTIPLE tags (matching
    the scan reader), never one fused ``[anxiety depression, new]`` value."""
    import yaml
    nt, added, reason = merge_tags_into_frontmatter(
        "---\ntags: anxiety depression\n---\nbody\n", ["stress"]
    )
    assert reason == ""
    tags = yaml.safe_load(nt.split("---")[1])["tags"]
    assert tags == ["anxiety", "depression", "stress"]
    # No token contains a space → nothing was fused.
    assert all(" " not in t for t in tags)
    # And the two originals are now seen as present: re-applying them is a no-op.
    nt2, added2, _ = merge_tags_into_frontmatter(nt, ["anxiety", "depression"])
    assert added2 == []


def test_merge_tags_noop_when_present() -> None:
    text = "---\ntags:\n  - a\n---\nbody\n"
    nt, added, reason = merge_tags_into_frontmatter(text, ["a"])
    assert added == []
    assert nt == text  # byte-identical no-op


def test_merge_tags_case_insensitive_dedup() -> None:
    text = "---\ntags:\n  - Anxiety\n---\nbody\n"
    nt, added, _ = merge_tags_into_frontmatter(text, ["anxiety", "Sleep"])
    assert added == ["Sleep"]


def test_merge_tags_quotes_yaml_special_chars() -> None:
    """Review lot 1.2: a tag with a colon MUST be quoted — an unquoted block
    item `- to read: urgent` parses as a YAML mapping, silently corrupting the
    frontmatter the writer exists to fix."""
    import yaml
    nt, added, reason = merge_tags_into_frontmatter(
        "---\ntags:\n  - a\n---\nbody\n", ["to read: urgent"])
    assert reason == "" and added == ["to read: urgent"]
    parsed = yaml.safe_load(nt.split("---")[1])["tags"]
    assert parsed == ["a", "to read: urgent"]  # a STRING, not a nested map
    # Hash + quote chars survive the round-trip too (flow shape).
    nt, added, _ = merge_tags_into_frontmatter("---\ntags: [a]\n---\nb\n", ["c#s", "it's"])
    parsed = yaml.safe_load(nt.split("---")[1])["tags"]
    assert parsed == ["a", "c#s", "it's"]


def test_merge_tags_refuses_unsupported_shapes() -> None:
    """Shapes the splice can't handle safely are refused, never mangled."""
    # Scalar with a trailing comment — conversion would eat the comment.
    nt, _, reason = merge_tags_into_frontmatter("---\ntags: foo # c\n---\nb\n", ["x"])
    assert nt is None and "comment" in reason
    # Flow list with quoted items — naive `.index(']')` can land inside them.
    nt, _, reason = merge_tags_into_frontmatter('---\ntags: ["a, b"]\n---\nb\n', ["x"])
    assert nt is None and "quoted" in reason


def test_merge_tags_comment_on_tags_head_is_block() -> None:
    """`tags:  # none` is an empty block-list head: items insert below it and
    the comment stays on its line."""
    import yaml
    nt, added, reason = merge_tags_into_frontmatter(
        "---\ntags: # none\ntitle: X\n---\nb\n", ["a"])
    assert reason == "" and added == ["a"]
    fmd = yaml.safe_load(nt.split("---")[1])
    assert fmd["tags"] == ["a"] and fmd["title"] == "X"


def test_merge_tags_accented_tag_stays_plain() -> None:
    """French tags (the maintainer's vault) must NOT get needless quoting."""
    nt, added, _ = merge_tags_into_frontmatter("---\ntags:\n  - a\n---\nb\n", ["dépression"])
    assert added == ["dépression"]
    assert "- dépression" in nt and "'" not in nt.split("---")[1]


def test_merge_tags_empty_frontmatter_no_double_block() -> None:
    """`---\\n---` is EMPTY frontmatter, not no-frontmatter (2026-07-19).

    The old regex needed `\\n---` after a non-empty body, so the empty block
    failed to match and a SECOND `---` block was prepended — the same
    misclassification class as the B10 fence-at-EOF fix.
    """
    import yaml
    nt, added, reason = merge_tags_into_frontmatter("---\n---\nbody\n", ["a"])
    assert reason == "" and added == ["a"]
    assert nt.count("---") == 2  # still exactly one frontmatter block
    fmd = yaml.safe_load(nt.split("---")[1])
    assert fmd["tags"] == ["a"]
    assert nt.endswith("body\n")


def test_merge_tags_refuses_singular_tag_key() -> None:
    """A note with only the singular `tag:` key refuses rather than creating a
    parallel `tags:` block that silently ignores the existing tags."""
    nt, added, reason = merge_tags_into_frontmatter(
        "---\ntag: existing\n---\nb\n", ["a"])
    assert nt is None and added == []
    assert "tag:" in reason


def test_merge_tags_sibling_key_guard_refuses_collateral_edits(monkeypatch) -> None:
    """The round-trip guard also pins NON-tags keys (2026-07-19).

    The tag-set check alone proved nothing about siblings: simulate a splice
    bug that renames `title:` while producing the right tag set — the merge
    must refuse instead of writing it.
    """
    import api.routes._audit_tagwrite as mod
    real = mod._merge_unvalidated

    def _buggy(text, fm, wanted):
        new_text, added, reason = real(text, fm, wanted)
        if new_text:
            new_text = new_text.replace("title: X", "titl: X")
        return new_text, added, reason

    monkeypatch.setattr(mod, "_merge_unvalidated", _buggy)
    nt, added, reason = merge_tags_into_frontmatter(
        "---\ntitle: X\ntags:\n  - a\n---\nb\n", ["c"])
    assert nt is None and added == []
    assert "sibling" in reason
