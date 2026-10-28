"""Batch E (2026-08-26): Obsidian reference notes in step with Zotero.

Two measured constraints drive nearly every assertion here, and both are
counter-intuitive enough that a future change is likely to break them:

* **mtime cannot detect staleness in this vault** — the tagging campaigns
  rewrote every note's frontmatter, so mtime reports 0 stale where content
  reports 3. Nothing here may use a timestamp.
* **the user edits notes after import** — in every measured drift case the
  Obsidian body is LONGER than the Zotero note, so an equality test would
  flag ordinary editing and a regenerating writer would delete real work.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from notesync.compare import (
    DEFAULT_CONTAINMENT, compare_note, containment, note_body,
)
from notesync.render import (
    DEFAULT_TEMPLATE, PLACEHOLDERS, render_body, render_frontmatter,
    render_note, zotero_note_text,
)


def _record(key="smith2020", *, title="A Title", authors=("Smith, Ann",),
            year="2020", container="J Psych", doi="10.1/x", notes=("body",),
            tags=("dep",), has_note=True):
    child = [SimpleNamespace(body_plain=n) for n in notes]
    item = SimpleNamespace(title=title, container=container, doi=doi,
                           date=f"{year}-04-00" if year else "",
                           child_notes=child, tags=list(tags))
    bib = SimpleNamespace(title=title, year=year, authors=list(authors),
                          doi=doi)
    return SimpleNamespace(citation_key=key, bib_entry=bib, zotero_item=item,
                           obsidian_note=SimpleNamespace(path="/v/x.md") if has_note else None,
                           obs_tags=set(tags))


# --- rendering -------------------------------------------------------------

def test_renders_the_template_shape():
    text = render_note(_record())
    assert text.startswith("---\ntags: [dep]\n---\n")
    assert "**A Title**" in text
    assert ">smith2020" in text
    assert "*Smith 2020*" in text
    assert "J Psych" in text
    assert "10.1/x" in text
    # Marked layout since Phase 2 (2026-09-04): the reading note sits between
    # the two markers, and the note ends with the END marker.
    from notesync.compare import split_note_parts
    assert split_note_parts(text)[1] == "\nbody\n"
    assert text.rstrip().endswith("<!-- zotero-note:end -->")


def test_a_line_whose_placeholders_are_all_empty_is_dropped():
    """The plugin emitted `Error: format can only be applied to dates` into a
    shipped note for an undated item; substituting blindly leaves `> * *`.
    Neither is acceptable output."""
    body = render_body(_record(authors=(), year="", container="", doi=""))
    assert "* *" not in body
    assert not any(line.strip() in (">", "> *", "> * *") and "*" in line
                   for line in body.splitlines() if "*" in line)
    # The title and the notes still render.
    assert "**A Title**" in body


def test_a_line_with_no_placeholder_is_always_kept():
    """`---` and a bare `>` are layout, not content."""
    body = render_body(_record(authors=(), year="", container="", doi=""))
    assert "---" in body


def test_bibtex_brace_protection_and_latex_accents_are_cleaned():
    """A bib title IS LaTeX source: without cleaning, a note reads
    `Beck {{Depression Inventory}}` and `schizophr\\'enie`."""
    rec = _record(title="Beck {{Depression Inventory}} ({{BDI}})",
                  authors=("B{\\o}ge, Kerem",))
    body = render_body(rec)
    assert "{{" not in body and "}}" not in body
    assert "Depression Inventory" in body


def test_an_unknown_placeholder_is_left_verbatim():
    """A typo in the user's template must be VISIBLE, not silently emptied."""
    body = render_body(_record(), template="x {{nosuchthing}} y")
    assert "{{nosuchthing}}" in body


def test_every_documented_placeholder_resolves():
    body = render_body(_record(), template=" ".join(
        "{{%s}}" % name for name in PLACEHOLDERS))
    for name in PLACEHOLDERS:
        assert "{{%s}}" % name not in body, name


def test_several_child_notes_are_joined():
    assert zotero_note_text(_record(notes=("one", "two"))) == "one\n\ntwo"


def test_frontmatter_is_the_flow_list_shape_the_vault_uses():
    assert render_frontmatter(["b", "a"]) == "---\ntags: [b, a]\n---\n"
    assert render_frontmatter([]) == "---\ntags: []\n---\n"
    # A leading '#' is stripped, matching the audit's own tag normalisation.
    assert render_frontmatter(["#dep"]) == "---\ntags: [dep]\n---\n"


def test_frontmatter_refuses_a_tag_that_would_break_the_flow_list():
    assert render_frontmatter(['a"b', "c]d", "ok"]) == "---\ntags: [ok]\n---\n"


def test_the_default_template_is_used_when_none_is_configured():
    assert render_body(_record(), "") == render_body(_record(), DEFAULT_TEMPLATE)


# --- comparison ------------------------------------------------------------

def test_a_record_with_no_reading_note_is_ok_not_missing():
    """The maintainer's rule: an Obsidian note exists exactly when a reading
    note does. 5,078 records have none, and demanding notes for them would
    invent an enormous amount of work out of nothing."""
    assert compare_note("k", "", None).state == "ok"
    assert compare_note("k", "   ", None).state == "ok"


def test_a_reading_note_with_no_obsidian_note_is_missing():
    assert compare_note("k", "some text", None).state == "missing"


def test_an_edited_longer_note_is_in_step():
    """The commonest real case: the user adds their own commentary after
    import. An equality test would flag every one of these."""
    st = compare_note("k", "typologie des mesures",
                      "---\ntags: [a]\n---\ntypologie des mesures. "
                      "Beaucoup de commentaire personnel ajouté ensuite.")
    assert st.state == "ok"


def test_a_note_that_never_received_the_zotero_text_is_drifted():
    st = compare_note("k", "typologie COA rare specifique",
                      "---\ntags: []\n---\nSomething else entirely here.")
    assert st.state == "drifted"
    assert st.containment < DEFAULT_CONTAINMENT


def test_reordered_words_are_in_step():
    """Measured: a two-word reading note interleaved into a sentence
    ("rapport envoyé par papa" → "rapport / billet envoyé par papa") read as
    total drift under a substring test. That test was removed for this."""
    st = compare_note("k", "rapport envoyé par papa",
                      "---\ntags: []\n---\nrapport / billet envoyé par papa")
    assert st.state == "ok"


def test_comparison_is_accent_and_case_folded():
    """The vault is bilingual FR/EN; a re-encoding must not read as drift."""
    assert containment("dépression sévère", "DEPRESSION SEVERE") == 1.0


def test_frontmatter_is_excluded_from_the_comparison():
    """Every tag write rewrites the frontmatter. Including it would make each
    tagged note look drifted — and it is what makes mtime useless here."""
    raw = "---\ntags: [alpha, beta, gamma]\n---\nthe body"
    assert note_body(raw).strip() == "the body"
    assert compare_note("k", "the body", raw).state == "ok"


def test_a_note_without_frontmatter_is_compared_whole():
    assert note_body("no frontmatter here") == "no frontmatter here"


def test_an_empty_reading_note_can_never_make_a_note_look_drifted():
    assert containment("", "anything") == 1.0


def test_the_threshold_is_a_parameter():
    st_strict = compare_note("k", "one two three four five six seven eight",
                             "---\n---\none two three four five six seven",
                             threshold=1.0)
    st_loose = compare_note("k", "one two three four five six seven eight",
                            "---\n---\none two three four five six seven",
                            threshold=0.5)
    assert st_strict.state == "drifted"
    assert st_loose.state == "ok"


@pytest.mark.parametrize("bad", [None, ""])
def test_a_missing_note_body_never_raises(bad):
    assert compare_note("k", "text", bad).state in ("missing", "drifted")
