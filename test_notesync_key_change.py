"""Reference notes left under an old citation key (2026-09-19).

When Better BibTeX regenerates a key, the note named after the old key no
longer joins its record. The note's ``zotero-key`` names its Zotero ITEM,
whose key never changes, so the new name is derived, not guessed. Pinned
here: the derivation, the refusals (name taken, other notes link to the old
name, no such item), the byte-exact write and its undo through the refactor
journal.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import flask

from api.routes.notesync import notesync_bp

_HEADERS = {"X-Requested-With": "ChatEKLD"}

_NOTE = ("---\ntags: [lithium]\nzotero-key: ABCD1234\nzotero-note-key: WXYZ5678\n---\n\n"
         "**Lithium and suicide**\n\n>smithOld2020\n\n> *Smith 2020*\n\n---\n"
         "<!-- zotero-note:begin -->\nmy reading notes\n<!-- zotero-note:end -->\n")


class TestKeyChange(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="keychange-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.notes = self.vault / "Z_Zotero_Notes"
        self.notes.mkdir(parents=True)
        self.cfg = {"refactor_archive_dir": str(self.tmp / "archive")}
        app = flask.Flask(__name__)
        app.register_blueprint(notesync_bp)
        self.client = app.test_client()
        self.old = self.notes / "smithOld2020.md"
        self.old.write_bytes(_NOTE.encode())

    def _inv(self, item_key="ABCD1234"):
        rec = SimpleNamespace(citation_key="smithNew2020", obsidian_note=None,
                              bib_entry=SimpleNamespace(title="Lithium"),
                              zotero_item=SimpleNamespace(item_key=item_key))
        inv = SimpleNamespace(records={"smithNew2020": rec},
                              notes_without_record=[self.old])
        return inv, SimpleNamespace(vault_root=self.vault, zotero_notes_dir=self.notes)

    def _call(self, method, path, body=None, *, inv=None):
        from audit.manager import audit_manager
        from rag.vault import obsidian_manager
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch("api.routes.notesync.load_config", return_value=self.cfg))
            st.enter_context(mock.patch.object(audit_manager, "get_inventory",
                                               return_value=inv or self._inv()))
            st.enter_context(mock.patch.object(audit_manager, "is_scanning", return_value=False))
            st.enter_context(mock.patch.object(obsidian_manager, "try_acquire_lock",
                                               return_value=1))
            st.enter_context(mock.patch.object(obsidian_manager, "release_lock"))
            if method == "GET":
                return self.client.get(path, headers=_HEADERS)
            return self.client.post(path, json=body, headers=_HEADERS)

    def _rename(self, **body):
        return self._call("POST", "/api/notesync/rename-note",
                          {"citation_key": "smithOld2020", **body})

    def test_the_new_key_is_derived_from_the_notes_zotero_item(self):
        r = self._call("GET", "/api/notesync/key-changes")
        row = r.get_json()["rows"][0]
        self.assertEqual((row["old_key"], row["new_key"], row["state"]),
                         ("smithOld2020", "smithNew2020", "ready"))
        self.assertEqual(row["new_rel"], "Z_Zotero_Notes/smithNew2020.md")

    def test_a_dry_run_writes_nothing(self):
        r = self._rename(dry_run=True)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["plan"]["changes"], ["alias smithOld2020", "header line"])
        self.assertEqual(self.old.read_bytes(), _NOTE.encode())
        self.assertFalse((self.notes / "smithNew2020.md").exists())

    def test_confirm_is_required(self):
        self.assertEqual(self._rename().status_code, 400)

    def test_the_rename_keeps_the_old_key_as_an_alias_and_undoes_exactly(self):
        from vault_history import journal
        r = self._rename(confirm=True)
        self.assertEqual(r.status_code, 200, r.get_json())
        new = self.notes / "smithNew2020.md"
        self.assertFalse(self.old.exists())
        text = new.read_text(encoding="utf-8")
        self.assertIn("aliases:\n  - smithOld2020\n", text)
        self.assertIn("\n>smithNew2020\n", text)
        self.assertNotIn(">smithOld2020", text)
        # Everything else is the note it was, byte for byte.
        self.assertEqual(text.replace("aliases:\n  - smithOld2020\n", "")
                         .replace(">smithNew2020", ">smithOld2020"), _NOTE)
        op = journal.find_op(journal.load(self.vault, self.cfg), r.get_json()["op_id"])
        self.assertEqual(op["kind"], "rename_note")
        res = journal.revert_op(self.vault, self.cfg, op)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.old.read_bytes(), _NOTE.encode())
        self.assertFalse(new.exists())

    def test_the_undo_leaves_a_note_edited_after_the_rename_alone(self):
        from vault_history import journal
        r = self._rename(confirm=True)
        new = self.notes / "smithNew2020.md"
        new.write_text(new.read_text(encoding="utf-8") + "later thought\n", encoding="utf-8")
        op = journal.find_op(journal.load(self.vault, self.cfg), r.get_json()["op_id"])
        res = journal.revert_op(self.vault, self.cfg, op)
        self.assertEqual(res["status"], "skipped")
        self.assertTrue(new.exists() and not self.old.exists())

    def test_a_taken_name_refuses(self):
        (self.notes / "smithNew2020.md").write_text("mine\n", encoding="utf-8")
        r = self._rename(confirm=True)
        self.assertEqual((r.status_code, r.get_json()["reason"]), (409, "target_exists"))
        self.assertTrue(self.old.exists())

    def test_notes_linking_to_the_old_name_refuse_and_are_named(self):
        (self.vault / "projects").mkdir()
        (self.vault / "projects" / "draft.md").write_text(
            "see [[smithOld2020|Smith]]\n", encoding="utf-8")
        r = self._rename(confirm=True)
        body = r.get_json()
        self.assertEqual((r.status_code, body["reason"]), (409, "linked_from"))
        self.assertEqual(body["row"]["linked_from"], ["projects/draft.md"])
        self.assertTrue(self.old.exists())

    def test_a_mixed_case_wikilink_still_refuses(self):
        (self.vault / "projects").mkdir()
        (self.vault / "projects" / "draft.md").write_text(
            "see [[SmithOld2020]]\n", encoding="utf-8")
        r = self._rename(confirm=True)
        body = r.get_json()
        self.assertEqual((r.status_code, body["reason"]), (409, "linked_from"))
        self.assertEqual(body["row"]["linked_from"], ["projects/draft.md"])
        self.assertTrue(self.old.exists())

    def test_a_citekey_line_is_rewritten(self):
        self.old.write_text(
            _NOTE.replace("zotero-note-key: WXYZ5678\n",
                          "zotero-note-key: WXYZ5678\ncitekey: smithOld2020\n"),
            encoding="utf-8")
        r = self._rename(confirm=True)
        self.assertEqual(r.status_code, 200, r.get_json())
        text = (self.notes / "smithNew2020.md").read_text(encoding="utf-8")
        self.assertIn("citekey: smithNew2020", text)
        self.assertNotIn("citekey: smithOld2020", text)
        self.assertIn(">smithNew2020", text)

    def test_an_item_the_scan_does_not_know_refuses(self):
        r = self._call("POST", "/api/notesync/rename-note",
                       {"citation_key": "smithOld2020", "confirm": True},
                       inv=self._inv(item_key="ZZZZ9999"))
        self.assertEqual((r.status_code, r.get_json()["reason"]), (409, "no_item"))


if __name__ == "__main__":   # pragma: no cover
    unittest.main()
