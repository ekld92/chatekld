"""``notesync.migrate`` — the layout migration and its exact inverse.

The property that matters is ``unmigrate_note(migrate_note(x)) == x`` for
every note shape the vault holds, because the migration is NOT journalled
(1,235 notes cannot ride a 200-op retention window) and this inverse is, with
the vault's git, the whole undo story. Fixtures are synthetic.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from notesync import migrate
from notesync.compare import (
    REGION_BEGIN,
    REGION_END,
    compare_note,
    split_note,
    split_note_parts,
)
from notesync.render import render_note, render_region

K = dict(item_key="ABCD1234", note_key="WXYZ5678")

SHAPES = [
    "---\ntags: [a, b]\n---\n\n**T**\n\n>k\n\n---\n\nzotero text\n",
    "---\ntags: []\n---\n---\nregion",                     # no trailing newline
    "---\ntags: [a]\n---\n\n**T**\n\n---\n",                # empty region
    "---\ntags: [a]\n---\n\n**T**\n\n---\n\n- one\n- two\n\n",  # trailing blanks
    "---\r\ntags: []\r\n---\r\n\r\n**T**\r\n\r\n---\r\n\r\nregion\r\n",  # CRLF
    "---\ntags: [x]\nother: 1\n---\n\n**T**\n\n---\n\ntext\n",  # sibling key
]


class TestRoundTrip:
    @pytest.mark.parametrize("raw", SHAPES)
    def test_unmigrate_is_the_exact_inverse(self, raw):
        new, reason = migrate.migrate_note(raw, **K)
        assert reason is None, reason
        assert new != raw
        back, tail = migrate.unmigrate_note(new)
        assert tail is None
        assert back == raw

    @pytest.mark.parametrize("raw", SHAPES)
    def test_region_bytes_survive_untouched(self, raw):
        before = split_note_parts(raw)
        new, _ = migrate.migrate_note(raw, **K)
        after = split_note_parts(new)
        assert after is not None
        assert after[1] == before[1]
        assert after[0] + after[1] + after[2] == new

    def test_idempotent(self):
        new, _ = migrate.migrate_note(SHAPES[0], **K)
        assert migrate.migrate_note(new, **K) == (None, "already_migrated")


class TestFrontmatter:
    def test_keys_appended_and_tags_line_untouched(self):
        new, _ = migrate.migrate_note(SHAPES[0], **K)
        head = new.split("\n---\n", 1)[0]
        assert head == "---\ntags: [a, b]\nzotero-key: ABCD1234\nzotero-note-key: WXYZ5678"

    def test_crlf_keys_use_crlf(self):
        new, _ = migrate.migrate_note(SHAPES[4], **K)
        assert "\r\nzotero-key: ABCD1234\r\nzotero-note-key: WXYZ5678\r\n---\r\n" in new
        assert "\n\n" not in new.replace("\r\n", "")  # no stray LF-only lines

    def test_conflicting_existing_key_is_refused(self):
        raw = "---\ntags: []\nzotero-key: OTHER123\n---\n\n**T**\n\n---\n\ntext\n"
        assert migrate.migrate_note(raw, **K) == (None, "key_conflict")

    def test_same_existing_key_is_left_alone(self):
        raw = "---\ntags: []\nzotero-key: ABCD1234\n---\n\n**T**\n\n---\n\ntext\n"
        new, reason = migrate.migrate_note(raw, **K)
        assert reason is None
        assert new.count("zotero-key:") == 1

    @pytest.mark.parametrize("bad", ["abc", "ABCD123", "ABCD1234\nx: 1", "", None])
    def test_invalid_key_refused(self, bad):
        assert migrate.migrate_note(SHAPES[0], item_key=bad, note_key="WXYZ5678")[1] == "invalid_key"

    def test_no_frontmatter_refused(self):
        assert migrate.migrate_note("**T**\n\n---\n\ntext\n", **K) == (None, "no_frontmatter")

    def test_unparsed_shape_refused(self):
        assert migrate.migrate_note("---\ntags: []\n---\n\nno rule\n", **K) == (None, "unparsed")
        assert migrate.migrate_note("---\ntags: []\n---\n\na\n\n---\n\nb\n\n---\n\nc", **K) == (None, "unparsed")


class TestMarkedSplit:
    def test_user_text_below_end_marker_is_the_suffix(self):
        new, _ = migrate.migrate_note(SHAPES[0], **K)
        with_tail = new + "\nmy own commentary\n\n---\n\nwith a rule of my own\n"
        parts = split_note_parts(with_tail)
        assert parts is not None
        assert parts[1] == "\nzotero text\n"        # region unchanged, byte for byte
        assert parts[2].startswith("\n" + REGION_END)
        assert "my own commentary" in parts[2]
        assert parts[0] + parts[1] + parts[2] == with_tail

    def test_unmigrate_keeps_a_tail_and_says_so(self):
        new, _ = migrate.migrate_note(SHAPES[0], **K)
        back, reason = migrate.unmigrate_note(new + "tail\n")
        assert reason == "tail_kept"
        assert back == SHAPES[0] + "tail\n"

    def test_split_note_returns_region_only_for_marked(self):
        new, _ = migrate.migrate_note(SHAPES[0], **K)
        assert split_note(new)[1] == "\nzotero text\n"

    @pytest.mark.parametrize("raw", [
        f"---\ntags: []\n---\n{REGION_BEGIN}\ntext\n",               # no END
        f"---\ntags: []\n---\ntext {REGION_BEGIN}\n\n{REGION_END}\n", # BEGIN mid-line
        f"---\ntags: []\n---\n{REGION_BEGIN}\n{REGION_BEGIN}\n{REGION_END}\n",
        f"---\ntags: []\n{REGION_BEGIN}\n{REGION_END}\n",            # unterminated fm
    ])
    def test_malformed_marker_layouts_are_refused(self, raw):
        assert split_note_parts(raw) is None

    def test_empty_region_directly_followed_by_end(self):
        raw = f"---\ntags: []\n---\n{REGION_BEGIN}\n{REGION_END}\n"
        assert split_note_parts(raw) == (f"---\ntags: []\n---\n{REGION_BEGIN}\n", "", f"{REGION_END}\n")


def _rec(notes="alpha beta"):
    return SimpleNamespace(
        citation_key="k2020",
        bib_entry=SimpleNamespace(title="T", year="2020", authors=["Smith, A"], doi="10.1/x"),
        zotero_item=SimpleNamespace(title="T", container="J", doi="10.1/x", date="2020",
                                    item_key="ABCD1234",
                                    child_notes=[SimpleNamespace(body_plain=notes, note_key="WXYZ5678")]),
        obs_tags={"a"})


class TestRenderIsMarked:
    def test_default_template_renders_a_marked_note_with_keys(self):
        text = render_note(_rec())
        assert text.startswith("---\ntags: [a]\nzotero-key: ABCD1234\nzotero-note-key: WXYZ5678\n---\n")
        parts = split_note_parts(text)
        assert parts is not None and parts[1] == "\nalpha beta\n"
        assert migrate.migrate_note(text, **K) == (None, "already_migrated")

    def test_render_region_is_the_bare_note_text(self):
        region, reason = render_region(_rec("alpha beta"))
        assert reason is None
        assert region == "\nalpha beta\n"          # the migrated notes' shape
        legacy, reason = render_region(_rec("alpha beta"), marked_target=False)
        assert reason is None
        assert legacy == "\nalpha beta\n"          # identical bytes for an unmarked note
        assert REGION_BEGIN not in region and REGION_END not in region

    def test_compare_note_reports_marked(self):
        text = render_note(_rec())
        st = compare_note("k2020", "alpha beta", text)
        assert st.marked is True and st.sync == "ok"
        st2 = compare_note("k2020", "alpha beta", SHAPES[0])
        assert st2.marked is False


def test_template_hint_carries_the_markers():
    assert migrate.TEMPLATE_HINT.startswith(REGION_BEGIN)
    assert migrate.TEMPLATE_HINT.endswith(REGION_END)
