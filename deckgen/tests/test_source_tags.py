"""Pure-logic tests for the deterministic source-tag rewrite (2026-08-13).

The section prompts ask the model to attribute claims inline as
``(source: note-name.md)``. ``rewrite_source_tags`` turns that contract into
real citations: a filename that IS a bib key becomes ``\\citefoot{key}``, and
everything else is grouped into one footer per frame instead of littering the
bullets. No server, no model — run with:  python -m pytest deckgen/tests/ -v
"""
from __future__ import annotations

from deckgen.assemble import assemble_with_template, rewrite_source_tags
from deckgen.latex import REFLINE_PROVIDECOMMAND, SectionOutput

# A stand-in for the vault's Better BibTeX naming: the note filename IS the key.
BIB = {
    "santomauroUpdatedTrendsGlobal2026": ("Santomauro, D. et al.", "2026", "Trends"),
    "bouyerRegressionLogistique2024": ("Bouyer, J.", "2024", "Régression"),
}


def _frame(*lines: str) -> str:
    return "\\begin{frame}{Titre}\n" + "\n".join(lines) + "\n\\end{frame}\n"


# --- the two halves of the rewrite ------------------------------------------

def test_bib_key_source_becomes_a_real_citation():
    out = rewrite_source_tags(
        _frame("\\item Prévalence 4.4% (source: santomauroUpdatedTrendsGlobal2026.md)"),
        BIB,
    )
    assert "\\citefoot{santomauroUpdatedTrendsGlobal2026}" in out
    assert "(source:" not in out
    assert ".md" not in out


def test_non_bib_source_moves_to_a_grouped_frame_footer():
    out = rewrite_source_tags(
        _frame("\\item Sur-risque x2 (source: cours_depression.md)"), BIB
    )
    # Left the bullet...
    assert "(source:" not in out
    assert "\\item Sur-risque x2" in out
    # ...and landed in a footer, escaped, just before \end{frame}.
    assert "\\refline{Source : cours\\_depression.md}" in out
    assert out.index("\\refline") < out.index("\\end{frame}")


def test_mixed_frame_splits_the_two_kinds():
    out = rewrite_source_tags(
        _frame(
            "\\item A (source: bouyerRegressionLogistique2024.md)",
            "\\item B (source: notes_perso.md)",
        ),
        BIB,
    )
    assert "\\citefoot{bouyerRegressionLogistique2024}" in out
    assert "\\refline{Source : notes\\_perso.md}" in out
    assert "\\refline{Source : bouyer" not in out, "a bib key must not be footered"


def test_footer_groups_and_dedupes_per_frame():
    body = _frame(
        "\\item A (source: a_note.md)",
        "\\item B (source: b_note.md)",
        "\\item C (source: a_note.md)",
    )
    out = rewrite_source_tags(body, BIB)
    assert out.count("\\refline") == 1
    assert "\\refline{Source : a\\_note.md; b\\_note.md}" in out


def test_each_frame_gets_its_own_footer():
    out = rewrite_source_tags(
        _frame("\\item A (source: x.md)") + _frame("\\item B (source: y.md)"), BIB
    )
    assert out.count("\\refline") == 2
    assert "\\refline{Source : x.md}" in out and "\\refline{Source : y.md}" in out


# --- safety properties -------------------------------------------------------

def test_body_without_any_tag_is_returned_byte_identical():
    """Citations off, or a well-behaved model: the legacy path is untouched."""
    body = _frame("\\item Plain bullet with no attribution")
    assert rewrite_source_tags(body, BIB) is body


def test_rewrite_is_idempotent():
    body = _frame(
        "\\item A (source: santomauroUpdatedTrendsGlobal2026.md)",
        "\\item B (source: autre_note.md)",
    )
    once = rewrite_source_tags(body, BIB)
    assert rewrite_source_tags(once, BIB) == once


def test_pdf_attachment_extension_resolves_to_the_same_key():
    out = rewrite_source_tags(
        _frame("\\item A (source: bouyerRegressionLogistique2024.pdf)"), BIB
    )
    assert "\\citefoot{bouyerRegressionLogistique2024}" in out


def test_empty_bib_index_still_cleans_the_bullets():
    """With no bibliography every source is a leftover — none may stay inline."""
    out = rewrite_source_tags(_frame("\\item A (source: quelque_note.md)"), {})
    assert "(source:" not in out
    assert "\\refline{Source : quelque\\_note.md}" in out


def test_vfill_mode_needs_no_macro():
    """The augment binding: never touches a user deck's preamble."""
    out = rewrite_source_tags(
        _frame("\\item A (source: n.md)"), BIB, footer_mode="vfill"
    )
    assert "\\refline" not in out
    assert "\\vfill{\\tiny\\textcolor{gray}{Source : n.md}}" in out


def test_article_mode_footnotes_instead_of_grouping():
    """Prose has no frames to group into; \\autocite is the article's citation."""
    body = (
        "\\section{Méthodes}\nUn point (source: bouyerRegressionLogistique2024.md) "
        "et un autre (source: notes.md).\n"
    )
    out = rewrite_source_tags(
        body, BIB, cite_macro="autocite", footer_mode="footnote"
    )
    assert "\\autocite{bouyerRegressionLogistique2024}" in out
    assert "\\footnote{Source : notes.md}" in out
    assert "(source:" not in out


def test_indentation_survives_the_rewrite():
    """Tag removal must not reflow the LaTeX — indentation is meaningful to read."""
    body = (
        "\\begin{frame}{T}\n"
        "  \\begin{itemize}\n"
        "    \\item A (source: n.md)\n"
        "  \\end{itemize}\n"
        "\\end{frame}\n"
    )
    out = rewrite_source_tags(body, BIB)
    assert "  \\begin{itemize}" in out
    assert "    \\item A" in out
    assert "\\item A \n" not in out, "no trailing space where the tag was"


def test_tag_outside_any_frame_is_left_alone_not_guessed_at():
    """No frame to attach a footer to -> leave the prose as the model wrote it."""
    body = "Intro prose (source: orpheline.md)\n" + _frame("\\item A")
    out = rewrite_source_tags(body, BIB)
    assert "(source: orpheline.md)" in out


def test_debris_cleanup_is_scoped_to_the_tag_sites():
    """The cleanup must not rewrite text AWAY from a removed tag: French
    « mot : » spacing, tabular alignment runs and punctuation-initial
    indentation are all correct LaTeX the model wrote on purpose. The pre-fix
    frame-global regexes collapsed all three."""
    body = (
        "\\begin{frame}{T}\n"
        "  \\item Prévalence : 4{,}4 \\% (source: n.md)\n"
        "  \\begin{tabular}{ll}\n"
        "    a    & b \\\\\n"
        "  \\end{tabular}\n"
        "    ; suite ponctuée\n"
        "\\end{frame}\n"
    )
    out = rewrite_source_tags(body, BIB)
    assert "Prévalence : 4{,}4 \\%" in out, "French space-before-colon must survive"
    assert "    a    & b" in out, "tabular alignment spacing must survive"
    assert "    ; suite ponctuée" in out, "punctuation-initial indentation must survive"
    assert "(source:" not in out and "\\refline{Source : n.md}" in out


def test_body_with_no_complete_frame_keeps_its_tags():
    """A truncated section (no \\end{frame}) has nowhere to put a footer.
    Attribution must be preserved in place, never deleted — pre-fix the branch
    stripped every tag and discarded the leftovers."""
    body = "\\begin{frame}{T}\n\\item A (source: coupe.md)\n"
    out = rewrite_source_tags(body, BIB)
    assert out == body


def test_existing_footer_merges_instead_of_stacking():
    """An augment cycle can feed a frame that already carries a footer back
    through the pass together with NEW tags: merge, never stack."""
    body = (
        "\\begin{frame}{T}\n"
        "\\item A\n"
        "\\item B (source: nouveau.md)\n"
        "\\refline{Source : ancien.md}\\end{frame}\n"
    )
    out = rewrite_source_tags(body, BIB)
    assert out.count("\\refline") == 1
    assert "\\refline{Source : ancien.md; nouveau.md}" in out


def test_footer_names_escape_backslash():
    """A model-invented name carrying a backslash must not become an active macro."""
    out = rewrite_source_tags(_frame("\\item A (source: \\alpha_note.md)"), BIB)
    assert "\\refline{Source : \\textbackslash{}alpha\\_note.md}" in out


# --- preamble self-containment ----------------------------------------------

def test_assemble_injects_providecommand_only_when_refline_is_used():
    used = assemble_with_template(
        [SectionOutput(title="S", body=_frame("x \\refline{Source : a.md}"))],
        preamble="\\documentclass{beamer}",
        opening="\\begin{document}",
        closing="\\end{document}",
    )
    assert REFLINE_PROVIDECOMMAND in used
    assert used.index(REFLINE_PROVIDECOMMAND) < used.index("\\begin{document}")

    unused = assemble_with_template(
        [SectionOutput(title="S", body=_frame("\\item plain"))],
        preamble="\\documentclass{beamer}",
        opening="\\begin{document}",
        closing="\\end{document}",
    )
    assert "\\refline" not in unused


def test_assemble_does_not_redefine_a_template_supplied_refline():
    out = assemble_with_template(
        [SectionOutput(title="S", body=_frame("x \\refline{Source : a.md}"))],
        preamble="\\documentclass{beamer}\n\\newcommand{\\refline}[1]{#1}",
        opening="\\begin{document}",
        closing="\\end{document}",
    )
    assert REFLINE_PROVIDECOMMAND not in out


# --- paper index resolution (2026-09-23) --------------------------------------
# The audit's PDF↔bib join lets a PDF or note PATH resolve to its key, so a
# model citing ``smith_2020.pdf`` is cited as the paper it is.

class _FakeIndex:
    """Duck-typed stand-in for ``rag.paper_index.PaperIndex``."""

    def __init__(self, sources=None, basenames=None, labels=None):
        self.sources = sources or {}
        self.basenames = basenames or {}
        self.labels = labels or {}

    def resolve(self, name):
        if name in self.sources:
            return self.sources[name]
        return self.basenames.get(name.rsplit("/", 1)[-1].casefold())

    def label(self, key):
        return self.labels.get(key)


IDX = _FakeIndex(
    sources={
        "Z_attachments/biblio_articles/bouyer_2024.pdf": "bouyerRegressionLogistique2024",
        "Z_attachments/biblio_articles/elsewhere_2019.pdf": "elsewhereKey2019",
    },
    basenames={
        "bouyer_2024.pdf": "bouyerRegressionLogistique2024",
        "elsewhere_2019.pdf": "elsewhereKey2019",
    },
    labels={
        "bouyerRegressionLogistique2024": "Bouyer, 2024, Régression",
        "elsewhereKey2019": "Else & Where, 2019, A Title",
    },
)


def test_pdf_basename_resolves_to_its_citation():
    out = rewrite_source_tags(
        _frame("\\item A (source: bouyer_2024.pdf)"), BIB, paper_index=IDX)
    assert "\\citefoot{bouyerRegressionLogistique2024}" in out
    assert "\\refline" not in out


def test_pdf_path_form_resolves_to_its_citation():
    out = rewrite_source_tags(
        _frame("\\item A (source: Z_attachments/biblio_articles/bouyer_2024.pdf)"),
        BIB, paper_index=IDX)
    assert "\\citefoot{bouyerRegressionLogistique2024}" in out
    assert "biblio" not in out


def test_mixed_tag_cites_the_pdf_and_footers_the_note():
    out = rewrite_source_tags(
        _frame("\\item A (sources: bouyer_2024.pdf; notes_perso.md)"),
        BIB, paper_index=IDX)
    assert "\\citefoot{bouyerRegressionLogistique2024}" in out
    assert "\\refline{Source : notes\\_perso.md}" in out


def test_known_key_outside_the_template_bib_footers_its_label():
    out = rewrite_source_tags(
        _frame("\\item A (source: elsewhere_2019.pdf)"), BIB, paper_index=IDX)
    assert "\\citefoot{elsewhereKey2019}" not in out
    assert "\\refline{Source : Else \\& Where, 2019, A Title}" in out
    assert "elsewhere\\_2019.pdf" not in out


def test_ambiguous_basename_is_not_resolved():
    # The index drops a basename that names two keys; the rewrite must then
    # leave the name as written rather than guess.
    out = rewrite_source_tags(
        _frame("\\item A (source: other_folder/unknown_2020.pdf)"),
        BIB, paper_index=IDX)
    assert "\\citefoot" not in out
    assert "\\refline{Source : other\\_folder/unknown\\_2020.pdf}" in out


def test_citations_off_never_cites_but_labels_the_footer():
    out = rewrite_source_tags(
        _frame("\\item A (source: bouyer_2024.pdf)"), {}, paper_index=IDX)
    assert "\\citefoot" not in out
    assert "\\refline{Source : Bouyer, 2024, Régression}" in out


def test_no_index_is_byte_identical():
    body = _frame(
        "\\item A (source: bouyer_2024.pdf)",
        "\\item B (source: santomauroUpdatedTrendsGlobal2026.md)",
    )
    assert rewrite_source_tags(body, BIB, paper_index=None) == \
        rewrite_source_tags(body, BIB)
    assert "\\refline{Source : bouyer\\_2024.pdf}" in rewrite_source_tags(body, BIB)


def test_no_tag_returns_the_same_object_with_an_index():
    body = _frame("\\item plain")
    assert rewrite_source_tags(body, BIB, paper_index=IDX) is body


def test_labelled_footer_is_idempotent_and_merges_without_duplicating():
    once = rewrite_source_tags(
        _frame("\\item A (source: elsewhere_2019.pdf)"), BIB,
        paper_index=IDX, footer_mode="vfill")
    assert rewrite_source_tags(once, BIB, paper_index=IDX, footer_mode="vfill") == once
    # An augment cycle adds the same source again: the label (which carries
    # commas) must be recognised in the existing footer, not appended twice.
    again = once.replace("\\end{frame}", "\\item B (source: elsewhere_2019.pdf)\n\\end{frame}")
    merged = rewrite_source_tags(again, BIB, paper_index=IDX, footer_mode="vfill")
    assert merged.count("Else \\& Where, 2019, A Title") == 1


def test_article_footnote_mode_cites_or_labels():
    body = "\\section{S}\nA (source: bouyer_2024.pdf). B (source: elsewhere_2019.pdf).\n"
    out = rewrite_source_tags(
        body, BIB, cite_macro="autocite", footer_mode="footnote", paper_index=IDX)
    assert "\\autocite{bouyerRegressionLogistique2024}" in out
    assert "\\footnote{Source : Else \\& Where, 2019, A Title}" in out


# --- footer detection and merge (2026-09-23 review) ---------------------------

BRACE_IDX = _FakeIndex(
    basenames={"brace_2020.pdf": "braceKey", "slash_2021.pdf": "slashKey"},
    labels={"braceKey": "Smith, 2020, A {Study}", "slashKey": "Doe, 2021, C:\\temp"},
)


def _two_passes(body, mode, **kw):
    once = rewrite_source_tags(body, BIB, footer_mode=mode, paper_index=BRACE_IDX, **kw)
    twice = rewrite_source_tags(once, BIB, footer_mode=mode, paper_index=BRACE_IDX, **kw)
    return once, twice


def test_brace_label_footer_is_found_and_merged_not_stacked():
    for mode, marker in (("refline", "\\refline{"), ("vfill", "\\vfill{")):
        once, twice = _two_passes(_frame("\\item A (source: brace_2020.pdf)"), mode)
        assert once.count("Source : ") == 1 and twice == once
        again = once.replace("\\end{frame}", "\\item B (source: brace_2020.pdf)\n\\end{frame}")
        merged = rewrite_source_tags(again, BIB, footer_mode=mode, paper_index=BRACE_IDX)
        assert merged.count("Source : ") == 1, mode
        assert merged.count("Smith, 2020, A \\{Study\\}") == 1, mode
        assert merged.count(marker) == 1


def test_backslash_label_footer_is_found_and_merged_not_stacked():
    for mode in ("refline", "vfill"):
        once, twice = _two_passes(_frame("\\item A (source: slash_2021.pdf)"), mode)
        assert "\\textbackslash{}temp" in once and twice == once
        again = once.replace("\\end{frame}", "\\item B (source: slash_2021.pdf)\n\\end{frame}")
        merged = rewrite_source_tags(again, BIB, footer_mode=mode, paper_index=BRACE_IDX)
        assert merged.count("Source : ") == 1, mode
        assert merged.count("\\textbackslash{}temp") == 1, mode


def test_footnote_mode_brace_and_backslash_labels_are_idempotent():
    body = "\\section{S}\nA (source: brace_2020.pdf). B (source: slash_2021.pdf).\n"
    once, twice = _two_passes(body, "footnote", cite_macro="autocite")
    assert once.count("\\footnote{Source : ") == 2 and twice == once


def test_source_named_after_a_label_piece_survives_an_augment():
    """Footer ``Smith, 2020, notes`` + new tag ``(source: notes)``: the old
    run-based dedup dropped the tag and never footered it."""
    body = (
        "\\begin{frame}{T}\n"
        "\\item B (source: notes)\n"
        "\\refline{Source : Smith, 2020, notes}\\end{frame}\n"
    )
    out = rewrite_source_tags(body, BIB)
    assert "(source:" not in out
    assert "\\refline{Source : Smith, 2020, notes; notes}" in out


def test_legacy_comma_joined_filename_footer_is_split_on_read():
    body = (
        "\\begin{frame}{T}\n"
        "\\item B (source: b.md)\n"
        "\\refline{Source : a.md, b.md}\\end{frame}\n"
    )
    out = rewrite_source_tags(body, BIB)
    assert "\\refline{Source : a.md; b.md}" in out and out.count("b.md") == 1


def test_semicolon_inside_a_label_is_folded_to_a_comma():
    idx = _FakeIndex(basenames={"semi.pdf": "semiKey"}, labels={"semiKey": "A; B, 2020, T"})
    out = rewrite_source_tags(_frame("\\item X (source: semi.pdf)"), BIB, paper_index=idx)
    assert "\\refline{Source : A, B, 2020, T}" in out
