"""The region split and the region renderer — the two pieces an update rides on.

``split_note`` decides WHERE the app's territory ends and the user's begins, and
``render_region`` decides WHAT goes there. Both are pure, and both have a
failure mode that is silent rather than loud: a wrong split writes over the
user's writing, and a wrong render writes a truncated note. So they are tested
directly here rather than only through the route.
"""

from __future__ import annotations

from types import SimpleNamespace

from notesync.compare import compare_note, split_note
from notesync.render import render_note, render_region


def _rec(notes="alpha beta", key="k2020"):
    return SimpleNamespace(
        citation_key=key,
        bib_entry=SimpleNamespace(title="T", year="2020", authors=["Smith, A"],
                                  doi="10.1/x"),
        zotero_item=SimpleNamespace(title="T", container="J", doi="10.1/x",
                                    date="2020", child_notes=[
                                        SimpleNamespace(body_plain=notes)]),
        obs_tags={"a"})


class TestSplitNote:
    def test_prefix_plus_region_is_the_original_byte_for_byte(self):
        """The invariant the whole update rests on: replacing only the region
        cannot perturb a single byte anywhere else in the note."""
        for raw in (
            "---\ntags: [a, b]\n---\n\n**T**\n\n>k\n\n---\n\nzotero text\n",
            "---\ntags: []\n---\n---\nregion",             # no blank lines
            "**T**\n\n---\n\nno frontmatter at all\n",
            "---\ntags: [a]\n---\n\n**T**\n\n---\n",        # empty region
        ):
            split = split_note(raw)
            assert split is not None, raw
            assert split[0] + split[1] == raw

    def test_crlf_survives_the_round_trip(self):
        """A CRLF note must not be silently LF-converted by a region edit."""
        raw = "---\r\ntags: []\r\n---\r\n\r\n**T**\r\n\r\n---\r\n\r\nregion\r\n"
        split = split_note(raw)
        assert split is not None
        assert split[0] + split[1] == raw
        assert "\r\n" in split[1]

    def test_refuses_a_note_with_no_rule(self):
        assert split_note("---\ntags: []\n---\n\njust a body\n") is None
        assert split_note("no frontmatter, no rule") is None

    def test_refuses_a_note_with_more_than_one_rule(self):
        """Ambiguous: there is no way to tell which half is the app's."""
        assert split_note("---\ntags: []\n---\n\na\n\n---\n\nb\n\n---\n\nc") is None

    def test_refuses_none(self):
        assert split_note(None) is None

    def test_the_frontmatter_close_is_not_mistaken_for_the_rule(self):
        raw = "---\ntags: [a]\n---\n\nheader\n\n---\n\nregion\n"
        prefix, region = split_note(raw)
        assert region == "\nregion\n"
        assert prefix.startswith("---\ntags: [a]\n---\n")


class TestSyncClassification:
    def _state(self, zotero, region):
        raw = f"---\ntags: []\n---\n\n**T**\n\n---\n{region}\n"
        return compare_note("k", zotero, raw)

    def test_nothing_to_do(self):
        assert self._state("alpha beta", "alpha beta").sync == "ok"

    def test_something_to_import_and_nothing_to_lose(self):
        st = self._state("alpha beta gamma", "alpha beta")
        assert st.sync == "update"
        assert (st.missing_words, st.extra_words) == (1, 0)

    def test_something_to_import_and_something_to_lose(self):
        st = self._state("alpha beta", "alpha mynote")
        assert st.sync == "conflict"
        assert (st.missing_words, st.extra_words) == (1, 1)

    def test_the_user_annotated_and_there_is_nothing_to_import(self):
        st = self._state("alpha", "alpha mynote")
        assert st.sync == "annotated"

    def test_an_unrecognised_shape_is_never_classified_as_updatable(self):
        st = compare_note("k", "alpha", "---\ntags: []\n---\n\nno rule\n")
        assert st.sync == "unparsed"
        assert st.region_found is False

    def test_a_missing_note_stays_missing(self):
        assert compare_note("k", "alpha", None).sync == "missing"

    def test_the_generated_header_is_not_counted_as_the_users_writing(self):
        """The counts are measured over the REGION. If they were measured over
        the body, the header's generated title/journal words would read as user
        additions and every note in the library would look annotated."""
        raw = ("---\ntags: []\n---\n\n**A Very Distinctive Title**\n\n"
               ">somekey\n\n> *Smith 2020*\n\n---\nalpha\n")
        st = compare_note("k", "alpha", raw)
        assert st.extra_words == 0
        assert st.sync == "ok"


class TestRenderRegion:
    def test_matches_what_a_create_would_write_below_the_rule(self):
        rec = _rec("alpha beta")
        region, reason = render_region(rec)
        assert reason is None
        assert split_note(render_note(rec))[1] == region

    def test_a_horizontal_rule_INSIDE_the_zotero_note_does_not_truncate_it(self):
        """The defect the sentinel exists to prevent.

        Splitting the FINISHED note finds whichever ``---`` comes first, and the
        reading note's own text is part of that document. Against a template
        with no rule of its own the naive split returned only the text after the
        note's internal rule — and the writer would have laid that truncated
        region over the real one, deleting everything above it.
        """
        rec = _rec("alpha\n\n---\n\nbeta")
        template = "**{{title}}**\n\n{{notes}}"        # no rule of its own
        region, reason = render_region(rec, template)
        assert region is None
        assert reason == "template_shape"
        # And the naive implementation would have silently dropped "alpha":
        naive = split_note(render_note(rec, template))
        assert naive is not None and "alpha" not in naive[1]

    def test_a_horizontal_rule_in_the_note_still_renders_under_a_normal_template(self):
        rec = _rec("alpha\n\n---\n\nbeta")
        region, reason = render_region(rec)
        assert reason is None
        assert "alpha" in region and "beta" in region

    def test_refuses_a_template_that_puts_the_notes_above_its_rule(self):
        region, reason = render_region(_rec(), "{{notes}}\n\n---\n\n**{{title}}**")
        assert region is None
        assert reason == "notes_not_below_rule"

    def test_no_sentinel_ever_reaches_the_output(self):
        region, _ = render_region(_rec("alpha"))
        assert "\x00" not in region


class TestUpdateGateSeesDigitsAndMarkup:
    """The gate that decides whether an update may run without an explicit
    overwrite acknowledgement.

    Every case here FAILED before 2026-08-29: ``fold_words``' ``[^\\W\\d_]+``
    dropped digits entirely and a word-set comparison cannot see markup, so a
    region whose only user edits were numeric or formatting reported
    ``extra_words == 0`` and was offered as a safe one-click update that
    silently deleted them.
    """

    def _note(self, region):
        return f"---\ntags: [x]\n---\n**T**\n\n---\n{region}\n"

    def test_a_number_the_user_added_is_content_at_risk(self):
        from notesync.compare import compare_note
        z = "Follow up at months. A new sentence in Zotero."
        st = compare_note("k", z, self._note("Follow up at 24 months."))
        assert st.extra_words > 0, "the added '24' must register as at risk"
        assert st.sync == "conflict"

    def test_a_digit_inside_a_token_survives_tokenisation(self):
        from notesync.compare import risk_words
        assert risk_words("dose 2019 p. 47 750mg") == {
            "dose", "2019", "p", "47", "750mg"}

    def test_bold_and_a_wikilink_are_content_at_risk(self):
        from notesync.compare import compare_note
        z = "Cohort of patients. A new sentence in Zotero."
        st = compare_note("k", z, self._note(
            "Cohort of **patients**. [[Cohort]] of patients."))
        assert st.extra_words > 0, "bold + wikilink must register as at risk"
        assert st.sync == "conflict"

    def test_structure_counts_for_loss_but_never_for_import(self):
        """Markup on the REFERENCE side must not invent work to import.

        Counting structure symmetrically was measured and rejected: the
        proposed region and the HTML-stripped Zotero note carry different
        markup by construction, so it manufactured phantom "something to
        import" rows out of pure formatting.
        """
        from notesync.compare import region_delta
        missing, extra = region_delta("plain text here", "- plain text here")
        assert missing == 0, "a bullet on the reference side is not an import"
        assert extra == 0

    def test_an_unchanged_region_is_still_in_step(self):
        """The hardened tokenizers must not manufacture drift."""
        from notesync.compare import compare_note
        z = "Exactly the same words."
        st = compare_note("k", z, self._note("Exactly the same words."))
        assert (st.missing_words, st.extra_words) == (0, 0)
        assert st.sync == "ok"


class TestSplitNoteRefusesMalformedShapes:
    def test_a_rule_as_the_final_line_reconstructs_exactly(self):
        """`prefix + region == raw` with no trailing newline after the rule.

        The original form appended one unconditionally, so a region-scoped
        write perturbed a byte outside the region while the note's sha256
        guard certified the altered bytes.
        """
        raw = "---\ntags: [a]\n---\n**T**\n\n---"
        split = split_note(raw)
        assert split is not None
        assert split[0] + split[1] == raw

    def test_unterminated_frontmatter_is_refused(self):
        """Its sole `---` is the OPENING fence, so splitting there would hand
        the frontmatter back as the Zotero region and an update would
        overwrite the tagging subsystem's tags."""
        assert split_note("---\ntags: [x]\nfoo bar\n") is None
        assert split_note("---\nhello\nworld\n") is None

    def test_a_four_dash_line_is_not_a_closing_fence(self):
        """`raw.find("\\n---")` also matched `\\n----` and `\\n---foo`, landing
        the body offset mid-line."""
        assert split_note("---\ntags: [a]\n----\nbody\n\n---\nregion\n") is None

    def test_a_note_with_no_frontmatter_still_splits(self):
        """The refusal above keys on an opening fence, so a note that simply
        has no frontmatter is unaffected."""
        raw = "**T**\n\n---\n\nbody\n"
        split = split_note(raw)
        assert split is not None
        assert split[0] + split[1] == raw
