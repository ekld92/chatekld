"""The reference note's ``pdf:`` wikilink list, and the rename that follows it.

A reference note has always named its Zotero item in frontmatter; this names
its FILE, so the note links to its own PDF one click away in Obsidian.

The property that makes it safe to write at scale is the interaction with the
renamer, and it is the whole reason the field has ONE machine-written shape:
`maintenance/rename.py` refuses to rename a PDF referenced from any note, so
writing this field into 1 236 notes would otherwise make every biblio PDF
permanently un-renameable. The rename route recognises exactly this shape and
rewrites it inside its own journalled op; ANY other reference — a body embed,
a markdown link, a `.canvas` node, a hand-edited entry — still refuses.

The tests below are aimed at the four ways that bargain could silently break:
the shape stops round-tripping, the classifier gets more permissive than the
sweep it narrows, a rewrite half-applies, or the undo stops covering both
halves.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from api.routes._audit_tagwrite import (
    ambiguous_pdf_basenames,
    expected_pdf_links,
    pdf_list_is_clean,
    read_pdf_links,
    rewrite_pdf_link,
    set_pdf_links,
)

NOTE = ("---\ntags: [dep, x]\nzotero-key: ABCD1234\n"
        "zotero-note-key: WXYZ5678\n---\n**Title**\n\n> body\n")


class TestPdfLinkFrontmatter(unittest.TestCase):
    def test_write_read_and_remove_round_trip_exactly(self):
        out, reason = set_pdf_links(NOTE, ["smith_2020.pdf", "smith_2_2020.pdf"])
        self.assertIsNone(reason)
        self.assertEqual(read_pdf_links(out),
                         ["smith_2020.pdf", "smith_2_2020.pdf"])
        # Every sibling key survives byte-for-byte, and removal is the exact
        # inverse — the guard both halves of the write rest on.
        self.assertIn("tags: [dep, x]", out)
        self.assertIn("zotero-note-key: WXYZ5678", out)
        self.assertIn("**Title**\n\n> body\n", out)
        back, reason = set_pdf_links(out, [])
        self.assertIsNone(reason)
        self.assertEqual(back, NOTE)

    def test_the_wikilink_is_quoted_because_yaml_would_read_it_as_a_list(self):
        """`- [[x]]` unquoted parses as a NESTED LIST, so the round-trip guard
        would refuse the write — the quoting is not cosmetic."""
        import yaml
        out, _ = set_pdf_links(NOTE, ["smith_2020.pdf"])
        fm = out.split("---\n")[1]
        self.assertEqual(yaml.safe_load(fm)["pdf"], ["[[smith_2020.pdf]]"])
        self.assertIn('- "[[smith_2020.pdf]]"', out)

    def test_a_hand_edited_list_is_refused_never_overwritten(self):
        dirty = NOTE.replace("---\n**Title**",
                             "pdf:\n  - my own note about the file\n---\n**Title**")
        self.assertFalse(pdf_list_is_clean(dirty))
        out, reason = set_pdf_links(dirty, ["smith_2020.pdf"])
        self.assertIsNone(out)
        self.assertEqual(reason, "dirty_list")
        # And it is invisible to the reader, so no caller can conclude it
        # understands a list it only partly parses.
        self.assertEqual(read_pdf_links(dirty), [])

    def test_a_scalar_pdf_key_is_refused(self):
        scalar = NOTE.replace("---\n**Title**", "pdf: smith_2020.pdf\n---\n**Title**")
        self.assertFalse(pdf_list_is_clean(scalar))
        self.assertEqual(set_pdf_links(scalar, ["x_2020.pdf"])[1], "dirty_list")

    def test_names_that_could_break_the_shape_are_refused(self):
        for bad in ('a"b.pdf', "a[b.pdf", "a|b.pdf", "a\nb.pdf", ""):
            self.assertEqual(set_pdf_links(NOTE, [bad])[1], "invalid_name", bad)

    def test_no_frontmatter_is_refused(self):
        self.assertEqual(set_pdf_links("# plain note\n", ["x_2020.pdf"])[1],
                         "no_frontmatter")

    def test_rewrite_swaps_one_basename_and_leaves_the_rest(self):
        out, _ = set_pdf_links(NOTE, ["a_2020.pdf", "b_2021.pdf"])
        new, changed, reason = rewrite_pdf_link(out, "a_2020.pdf", "a_1_2020.pdf")
        self.assertTrue(changed)
        self.assertIsNone(reason)
        self.assertEqual(read_pdf_links(new), ["a_1_2020.pdf", "b_2021.pdf"])

    def test_rewrite_is_a_no_op_for_a_name_the_list_does_not_hold(self):
        out, _ = set_pdf_links(NOTE, ["a_2020.pdf"])
        new, changed, reason = rewrite_pdf_link(out, "other_2020.pdf", "x_2020.pdf")
        self.assertFalse(changed)
        self.assertIsNone(reason)
        self.assertEqual(new, out)


RELATED = ('---\ntags: [dep]\nrelated_notes:\n  - "[[tdah_adhd]]"\n  - "[[smith_2020.pdf]]"\n'
           'zotero-key: ABCD1234\n---\n**Title**\n')


class TestAmbiguousNames(unittest.TestCase):
    """A basename shared by two files is written as the file's vault path
    (2026-09-18: 23 reference notes named a PDF whose basename also exists,
    with different bytes, in a 2022 thesis folder)."""

    def _vault(self, tmp, files):
        v = Path(tmp)
        for rel in files:
            (v / rel).parent.mkdir(parents=True, exist_ok=True)
            (v / rel).write_bytes(rel.encode())
        return v

    def test_only_a_name_found_twice_is_ambiguous(self):
        with TemporaryDirectory() as tmp:
            v = self._vault(tmp, ["b/caceda_2020.pdf", "t/Caceda_2020.pdf",
                                  "b/solo_2021.pdf", ".trash/solo_2021.pdf",
                                  "b/note.md", "t/note.md"])
            # case-folded; a hidden folder (Obsidian does not index it) never
            # counts; only PDFs are considered.
            self.assertEqual(ambiguous_pdf_basenames(v), {"caceda_2020.pdf"})

    def test_expected_entries_use_a_path_only_for_the_ambiguous_name(self):
        with TemporaryDirectory() as tmp:
            v = self._vault(tmp, ["b/caceda_2020.pdf", "t/caceda_2020.pdf",
                                  "b/solo_2021.pdf"])
            amb = ambiguous_pdf_basenames(v)
            self.assertEqual(
                expected_pdf_links([v / "b/solo_2021.pdf", v / "b/caceda_2020.pdf"],
                                   v, amb),
                ["b/caceda_2020.pdf", "solo_2021.pdf"])

    def test_a_path_entry_round_trips(self):
        out, reason = set_pdf_links(NOTE, ["Z_a/biblio/caceda_2020.pdf"])
        self.assertIsNone(reason)
        self.assertIn('  - "[[Z_a/biblio/caceda_2020.pdf]]"\n', out)
        self.assertEqual(read_pdf_links(out), ["Z_a/biblio/caceda_2020.pdf"])
        self.assertTrue(pdf_list_is_clean(out))

    def test_rewrite_keeps_the_directory_of_a_path_entry(self):
        linked, _ = set_pdf_links(NOTE, ["b/caceda_2020.pdf", "solo_2021.pdf"])
        out, changed, reason = rewrite_pdf_link(
            linked, "caceda_2020.pdf", "caceda_2_2020.pdf",
            old_rel="b/caceda_2020.pdf")
        self.assertIsNone(reason)
        self.assertTrue(changed)
        self.assertEqual(read_pdf_links(out), ["b/caceda_2_2020.pdf", "solo_2021.pdf"])

    def test_rewrite_leaves_a_same_named_file_elsewhere_alone(self):
        linked, _ = set_pdf_links(NOTE, ["t/caceda_2020.pdf"])
        out, changed, _ = rewrite_pdf_link(
            linked, "caceda_2020.pdf", "caceda_2_2020.pdf",
            old_rel="b/caceda_2020.pdf")
        self.assertFalse(changed)
        self.assertEqual(out, linked)


class TestOtherWikilinkLists(unittest.TestCase):
    """Live 2026-09-18: 10 notes carry a `related_notes:` list in exactly the
    pdf: list's shape. Read as ours, they reported a "stale" pdf list."""

    def test_a_related_notes_list_is_not_the_pdf_list(self):
        self.assertEqual(read_pdf_links(RELATED), [])
        self.assertTrue(pdf_list_is_clean(RELATED))

    def test_writing_adds_a_pdf_list_and_leaves_related_notes_alone(self):
        out, reason = set_pdf_links(RELATED, ["smith_2020.pdf"])
        self.assertIsNone(reason)
        self.assertIn('related_notes:\n  - "[[tdah_adhd]]"\n  - "[[smith_2020.pdf]]"\n', out)
        self.assertEqual(read_pdf_links(out), ["smith_2020.pdf"])

    def test_a_rename_does_not_rewrite_a_related_notes_entry(self):
        out, changed, reason = rewrite_pdf_link(RELATED, "smith_2020.pdf", "smith_2_2020.pdf")
        self.assertEqual((out, changed, reason), (RELATED, False, None))


@unittest.skipIf(importlib.util.find_spec("maintenance") is None,
                     "private Vault Maintenance is absent from public artifacts")
class TestReferenceClassifier(unittest.TestCase):
    """Which references the rename may rewrite, and which still refuse it."""

    def _vault(self, tmp, notes: dict):
        v = Path(tmp)
        for rel, text in notes.items():
            (v / rel).parent.mkdir(parents=True, exist_ok=True)
            (v / rel).write_text(text, encoding="utf-8")
        return v

    def _classify(self, v, rels, basename):
        from api.routes.maintenance import _classify_references
        return _classify_references(v, rels, basename)

    def test_an_app_written_entry_is_rewritable(self):
        linked, _ = set_pdf_links(NOTE, ["smith_2020.pdf"])
        with TemporaryDirectory() as tmp:
            v = self._vault(tmp, {"n.md": linked})
            rw, block = self._classify(v, ["n.md"], "smith_2020.pdf")
            self.assertEqual([r for r, _ in rw], ["n.md"])
            self.assertEqual(block, [])

    def test_a_path_entry_for_this_file_is_rewritable(self):
        linked, _ = set_pdf_links(NOTE, ["b/smith_2020.pdf"])
        with TemporaryDirectory() as tmp:
            v = self._vault(tmp, {"n.md": linked})
            from api.routes.maintenance import _classify_references
            rw, block = _classify_references(v, ["n.md"], "smith_2020.pdf",
                                             vault_rel="b/smith_2020.pdf")
            self.assertEqual([r for r, _ in rw], ["n.md"])
            self.assertEqual(block, [])

    def test_a_path_entry_for_another_same_named_file_blocks(self):
        """The substring sweep finds the basename, but the note links the
        OTHER copy: it is not a reference this rename may rewrite."""
        linked, _ = set_pdf_links(NOTE, ["t/smith_2020.pdf"])
        with TemporaryDirectory() as tmp:
            v = self._vault(tmp, {"n.md": linked})
            from api.routes.maintenance import _classify_references
            rw, block = _classify_references(v, ["n.md"], "smith_2020.pdf",
                                             vault_rel="b/smith_2020.pdf")
            self.assertEqual(rw, [])
            self.assertEqual(block, ["n.md"])

    def test_a_body_embed_beside_our_entry_still_refuses(self):
        """The classifier must never be more permissive than the sweep it
        narrows: it strips OUR block and re-runs the same needle test, so a
        second mention anywhere keeps the refusal."""
        linked, _ = set_pdf_links(NOTE, ["smith_2020.pdf"])
        with TemporaryDirectory() as tmp:
            v = self._vault(tmp, {"n.md": linked + "\n![[smith_2020.pdf]]\n"})
            rw, block = self._classify(v, ["n.md"], "smith_2020.pdf")
            self.assertEqual(rw, [])
            self.assertEqual(block, ["n.md"])

    def test_a_mention_in_a_code_fence_still_refuses(self):
        """`referencing_notes` deliberately does not strip code — a refusal
        gate errs conservative — and this must not quietly disagree."""
        linked, _ = set_pdf_links(NOTE, ["smith_2020.pdf"])
        with TemporaryDirectory() as tmp:
            v = self._vault(tmp, {"n.md": linked + "\n```\nsmith_2020.pdf\n```\n"})
            self.assertEqual(self._classify(v, ["n.md"], "smith_2020.pdf")[1],
                             ["n.md"])

    def test_a_percent_encoded_markdown_link_still_refuses(self):
        linked, _ = set_pdf_links(NOTE, ["smith 2020.pdf"])
        with TemporaryDirectory() as tmp:
            v = self._vault(tmp, {"n.md": linked + "\n[x](smith%202020.pdf)\n"})
            self.assertEqual(self._classify(v, ["n.md"], "smith 2020.pdf")[1],
                             ["n.md"])

    def test_a_canvas_hit_is_never_rewritable(self):
        with TemporaryDirectory() as tmp:
            v = self._vault(tmp, {"b.canvas": '{"nodes":[{"file":"smith_2020.pdf"}]}'})
            rw, block = self._classify(v, ["b.canvas"], "smith_2020.pdf")
            self.assertEqual(rw, [])
            self.assertEqual(block, ["b.canvas"])

    def test_a_hand_edited_list_blocks_rather_than_being_rewritten(self):
        dirty = NOTE.replace("---\n**Title**",
                             'pdf:\n  - "[[smith_2020.pdf]]"\n  - mine\n---\n**Title**')
        with TemporaryDirectory() as tmp:
            v = self._vault(tmp, {"n.md": dirty})
            self.assertEqual(self._classify(v, ["n.md"], "smith_2020.pdf")[1],
                             ["n.md"])

    def test_an_unreadable_note_blocks(self):
        with TemporaryDirectory() as tmp:
            v = Path(tmp)
            (v / "n.md").write_bytes(b"\xff\xfe not utf-8")
            self.assertEqual(self._classify(v, ["n.md"], "smith_2020.pdf")[1],
                             ["n.md"])


if __name__ == "__main__":   # pragma: no cover
    unittest.main()
