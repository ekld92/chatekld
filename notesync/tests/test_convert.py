"""``notesync.convert`` — the one HTML ⇄ Markdown pair the sync path uses.

Fixtures are SYNTHETIC. Every shape here is one that was measured live on
2026-09-04 (spike S2) as either a fixed point or a named failure class; none
carries vault text.
"""

from __future__ import annotations

import re

import pytest

from notesync import convert
from notesync.compare import region_delta

SHELL = convert.ZOTERO_NOTE_PREFIX + "{}" + convert.ZOTERO_NOTE_SUFFIX


def _roundtrip(html: str) -> tuple[str, str]:
    md1 = convert.html_to_md(html)
    md2 = convert.html_to_md(convert.md_to_html(md1))
    return md1, md2


class TestHtmlToMarkdown:
    def test_dash_bullets_and_nested_list(self):
        md = convert.html_to_md(SHELL.format(
            "<p>intro</p><ul><li>one<ul><li>nested</li></ul></li><li>two</li></ul>"))
        assert "- one" in md and "- two" in md
        assert "* " not in md
        assert re.search(r"^\s+- nested$", md, re.M)

    def test_shell_divs_are_transparent(self):
        assert convert.html_to_md(SHELL.format("<p>hello</p>")) == "hello"

    def test_bold_and_italic(self):
        md = convert.html_to_md("<p><strong>b</strong> and <em>i</em></p>")
        assert md == "**b** and *i*"

    def test_img_node_passes_through_verbatim(self):
        tag = '<img alt="" data-attachment-key="ABCD1234" width="120" height="60"/>'
        md = convert.html_to_md(SHELL.format(f"<p>{tag}</p>"))
        assert "data-attachment-key=\"ABCD1234\"" in md
        assert "![" not in md

    def test_leading_gt_and_plus_are_escaped(self):
        # ``>20`` is a lab value, ``+ fqt`` is shorthand for "more frequent";
        # unescaped they become a blockquote and a list on the way back.
        md = convert.html_to_md("<p>&gt;20 : severe</p><p>+ fqt que MDD</p>")
        assert md.splitlines()[0].startswith("\\>")
        assert md.splitlines()[-1].startswith("\\+")

    def test_wikilinks_stored_as_text_come_back_as_wikilinks(self):
        # `[[citekey]]` pushed from a region is plain text in Zotero; the
        # converter used to hand it back as `\[\[citekey\]\]`, which
        # Obsidian renders literally. A lone `[x]` keeps its escape.
        md = convert.html_to_md(convert.md_to_html("cite [[wangEmulation2023]] and [x] here"))
        assert "[[wangEmulation2023]]" in md
        assert "\\[\\[" not in md
        assert "\\[x\\]" in md

    def test_nbsp_becomes_entity(self):
        md = convert.html_to_md("<p>&nbsp; &nbsp;- pseudo indent</p>")
        assert " " not in md
        assert md.startswith("&nbsp;")

    def test_empty_and_none(self):
        assert convert.html_to_md("") == ""
        assert convert.html_to_md(None) == ""

    def test_memoised_and_deterministic(self):
        html = "<p>same</p>"
        assert convert.html_to_md(html) is convert.html_to_md(html)


class TestMarkdownToHtml:
    def test_matches_observed_native_serialization_without_weakening_readback(self):
        # Literal native 10.0.2 fixture observation, not derived from constants.
        assert convert.md_to_html('[Reference](https://example.invalid/fixture)') == (
            '<div data-schema-version="9"><p><a href="https://example.invalid/fixture" '
            'rel="noopener noreferrer nofollow">Reference</a></p>\n</div>')
        from notesync.safety import content_issues
        assert not content_issues(convert.md_to_html('[Reference](https://example.invalid/fixture)'))

    @pytest.mark.parametrize('attrs', ['rel="author"', 'rel="noopener"',
                                      'rel="noopener noreferrer nofollow" target="_blank"',
                                      'data-citation="special"'])
    def test_unrecognized_link_attributes_still_require_review(self, attrs):
        from notesync.safety import content_issues
        assert content_issues(f'<p><a href="https://example.invalid" {attrs}>Reference</a></p>')

    def test_raw_link_attributes_are_not_rewritten(self):
        link = '<a href="https://example.invalid" rel="author">Reference</a>'
        assert link in convert.md_to_html(link)

    def test_wrapped_in_zotero_shell(self):
        html = convert.md_to_html("hi")
        assert html.startswith(convert.ZOTERO_NOTE_PREFIX)
        assert html.endswith(convert.ZOTERO_NOTE_SUFFIX)
        assert "<p>hi</p>" in html

    def test_raw_html_and_entities_pass_through(self):
        tag = '<img alt="" data-attachment-key="ABCD1234" width="1" height="1"/>'
        html = convert.md_to_html(f"x\n\n{tag}\n\n&nbsp;&nbsp;y")
        assert tag in html
        # markdown-it decodes the entity to U+00A0 on output; html_to_md
        # re-encodes it, which is what makes the pair a fixed point.
        assert "\u00a0\u00a0y" in html

    def test_table_renders(self):
        html = convert.md_to_html("| a | b |\n|---|---|\n| 1 | 2 |")
        assert "<table>" in html


class TestRoundTrip:
    @pytest.mark.parametrize("body", [
        "<p>plain</p>",
        "<p>a</p><ul><li>one<ul><li>nested</li></ul></li><li>two</li></ul>",
        "<p><strong>b</strong> and <em>i</em></p>",
        "<p>&gt;20 : severe</p><p>+ grosses diff</p>",
        "<p>&nbsp; &nbsp;- pseudo indent</p><p>&nbsp; &nbsp; &nbsp; &nbsp;- deeper</p>",
        '<p><img alt="" data-attachment-key="ABCD1234" width="120" height="60"/></p>',
        "<p>déjà — “quotes”</p>",
        "<ol><li>first</li><li>second</li></ol>",
        '<p><a href="https://example.org/x">a link</a></p>',
    ])
    def test_fixed_point(self, body):
        md1, md2 = _roundtrip(SHELL.format(body))
        assert md1 == md2

    def test_bold_adjacent_to_punctuation_is_the_known_lossy_class(self):
        # CommonMark cannot close a bold run against ``’`` followed by a word;
        # the round trip degrades it to literal asterisks. Documented in the
        # plan as accepted loss — this test pins that it is the ONLY thing
        # that changes (the words survive).
        md1, md2 = _roundtrip("<p><strong>enjeux : ’</strong>Other</p>")
        assert md1 != md2
        assert md1.replace("**", "") == md2.replace("\\*\\*", "")


class TestCompareIgnoresImgAttributes:
    def test_img_tag_is_not_words(self):
        tag = '<img alt="" data-attachment-key="ABCD1234" width="120" height="60"/>'
        region = "the note text"
        reference = f"the note text\n\n{tag}"
        assert region_delta(region, reference) == (0, 0)
        assert convert.strip_img_tags(reference).strip() == "the note text"


def test_converter_id_names_both_packages():
    assert "markdownify=" in convert.CONVERTER_ID
    assert "markdown-it-py=" in convert.CONVERTER_ID


class TestCompareFoldsNbsp:
    def test_entity_and_character_are_both_spaces(self):
        # The plugin wrote U+00A0 into the vault; convert emits ``&nbsp;``.
        # Left unfolded, "nbsp" counted as a word on one side only and 111
        # live notes read as "there is something to import".
        region = "  - pseudo indent\n\ntext"
        reference = "&nbsp;&nbsp;- pseudo indent\n\ntext"
        assert region_delta(region, reference) == (0, 0)


class TestRenderPrefersHtml:
    def test_zotero_note_text_renders_markdown_from_body_html(self):
        from types import SimpleNamespace

        from notesync.render import zotero_note_text
        note = SimpleNamespace(body_html="<p><strong>b</strong></p><ul><li>x</li></ul>",
                               body_plain="b\nx")
        rec = SimpleNamespace(zotero_item=SimpleNamespace(child_notes=[note]))
        assert zotero_note_text(rec) == "**b**\n\n- x"

    def test_zotero_note_text_falls_back_to_plain(self):
        from types import SimpleNamespace

        from notesync.render import zotero_note_text
        rec = SimpleNamespace(zotero_item=SimpleNamespace(
            child_notes=[SimpleNamespace(body_plain="plain only")]))
        assert zotero_note_text(rec) == "plain only"
