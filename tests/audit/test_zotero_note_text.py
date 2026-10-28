"""``_strip_html`` must not fuse the words on either side of a markup break.

Zotero note bodies are HTML. Rendering them with a bare ``get_text()``
concatenated across ``<br>`` and across adjacent block elements, producing
words that exist in neither store — ``appendicitisLebrun``,
``criteriainclusion``, ``depressionutile``. That mattered twice over: the text
is written into the vault by ``notesync.render.render_note``, and it is one
side of ``notesync.compare``'s word-containment measure, where a fused word is
permanent, un-closable drift.

Measured on the maintainer's library (2026-08-28) the artifact reported 91
notes as "the Zotero note has words this note lacks" where the true figure was
16 — so these assertions guard a number the UI shows, not just a string.
"""
from __future__ import annotations

from audit.core.zotero import _strip_html


class TestNoBrFusion:
    def test_br_becomes_a_line_break(self):
        html = "<p>reliability of appendicitis<br>Lebrun : we treat AI models</p>"
        out = _strip_html(html)
        assert "appendicitisLebrun" not in out
        assert "appendicitis" in out.split() or "appendicitis\nLebrun" in out
        assert "Lebrun" in out

    def test_adjacent_blocks_do_not_fuse(self):
        html = "<div><p>Key Inclusion and Exclusion Criteria</p><p>Inclusion age</p></div>"
        out = _strip_html(html)
        assert "CriteriaInclusion" not in out
        assert "Criteria" in out and "Inclusion age" in out

    def test_list_items_do_not_fuse(self):
        html = "<ul><li>premier</li><li>second</li></ul>"
        out = _strip_html(html)
        assert "premiersecond" not in out
        assert "premier" in out and "second" in out


class TestStableEnoughToWriteIntoTheVault:
    """The output is written verbatim into a note, so its whitespace matters."""

    def test_blank_runs_collapse_and_trailing_space_goes(self):
        html = "<p>un   </p><div></div><div></div><div></div><p>deux</p>"
        out = _strip_html(html)
        assert "\n\n\n" not in out
        assert not any(line.endswith(" ") for line in out.split("\n"))
        assert "un" in out and "deux" in out

    def test_inline_markup_still_joins_a_word(self):
        """A break must come from the MARKUP, not be invented between spans:
        ``<em>anti</em>depressant`` is one word and must stay one."""
        assert "antidepressant" in _strip_html("<p><em>anti</em>depressant</p>")

    def test_empty_and_none_are_empty(self):
        assert _strip_html(None) == ""
        assert _strip_html("") == ""
