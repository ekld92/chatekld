"""``/api/zotrecord/*`` — record merge (T8) and chapter-title correction (T9),
annual cleanup session 3 (2026-09-17).

Every test pins a way the verbs could damage the library: a title written
that the PDF does not print, a title written over one that moved in Zotero, a
child re-parented onto an item that already links the same file, the retired
side's Obsidian note silently lost, two reading notes fused into one item, a
merge that keeps going after a failed PATCH, an undo that does not put every
store back, and a dry run that writes.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import flask

from tagging import journal, zotero_write
from tagging.local_api import Version

_HEADERS = {"X-Requested-With": "ChatEKLD"}
BASE = "http://127.0.0.1:23119"


class FakeZotero:
    """The Zotero client surface the verbs use, over plain dicts with versions."""

    def __init__(self, items: dict[str, dict]):
        self.items = {k: dict(v) for k, v in items.items()}
        self.versions = {k: 10 for k in items}
        self.calls: list[tuple] = []
        self.fail_on: set[tuple] = set()
        self.lib_version = 100

    def _bump(self, key):
        self.lib_version += 1
        self.versions[key] = self.lib_version
        return self.lib_version

    def _check(self, key, version):
        if getattr(version, "server_id", None) != "fixture-db":
            raise zotero_write.ZoteroVersionConflict("Unbound fixture database version.")
        if key not in self.items:
            raise zotero_write.ZoteroError("HTTP 404: not found")
        if int(version) != self.versions[key]:
            raise zotero_write.ZoteroVersionConflict("Item changed in Zotero (412).")

    # reads
    def get_item(self, base, lib, key, timeout=15.0, api_key=None, server_id=None):
        if server_id != "fixture-db":
            raise zotero_write.ZoteroVersionConflict("Observed database changed.")
        self.calls.append(("get", key))
        if key not in self.items:
            raise zotero_write.ZoteroError("HTTP 404: not found")
        return dict(self.items[key]), Version(self.versions[key], "fixture-db")

    def get_item_children(self, base, lib, parent, timeout=15.0, api_key=None, server_id=None):
        if server_id != "fixture-db":
            raise zotero_write.ZoteroVersionConflict("Observed database changed.")
        self.calls.append(("children", parent))
        return [dict(d, key=k, version=self.versions.setdefault(k, 10)) for k, d in self.items.items()
                if d.get("parentItem") == parent and not d.get("deleted")]

    def get_item_tags_full(self, base, lib, key, timeout=15.0, api_key=None, server_id=None):
        assert server_id == "fixture-db"
        return Version(self.versions[key], "fixture-db"), list(self.items[key].get("tags") or [])

    def get_item_tags(self, base, lib, key, timeout=15.0, api_key=None, server_id=None):
        return Version(self.versions[key], "fixture-db"), [t["tag"] for t in self.items[key].get("tags") or []]

    # writes
    def set_item_parent(self, base, lib, key, parent, version, timeout=15.0, api_key=None):
        self.calls.append(("parent", key, parent))
        if ("parent", key) in self.fail_on:
            raise zotero_write.ZoteroUnavailable("connection refused")
        self._check(key, version)
        self.items[key]["parentItem"] = parent
        return self._bump(key)

    def set_item_tags(self, base, lib, key, tags, version, timeout=15.0, api_key=None):
        self.calls.append(("tags", key, tuple(t["tag"] if isinstance(t, dict) else t for t in tags)))
        self._check(key, version)
        self.items[key]["tags"] = [t if isinstance(t, dict) else {"tag": t} for t in tags]
        return self._bump(key)

    def set_item_fields(self, base, lib, key, fields, version, timeout=15.0, api_key=None):
        self.calls.append(("fields", key, tuple(sorted(fields))))
        self._check(key, version)
        self.items[key].update(fields)
        extra = getattr(self, "also_change", None)
        if extra:
            self.items[key].update(extra)
        return self._bump(key)

    def set_item_deleted(self, base, lib, key, deleted, version, timeout=15.0, api_key=None):
        self.calls.append(("deleted", key, deleted))
        self._check(key, version)
        self.items[key]["deleted"] = 1 if deleted else 0
        return self._bump(key)

    def resolve_user_id(self, *a, **k):
        return 0

    def writes(self):
        return [c for c in self.calls if c[0] in ("parent", "tags", "fields", "deleted")]


def _make_pdf(path: Path, lines: list[str]) -> None:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    y = 72
    for line in lines:
        page.insert_text((72, y), line, fontsize=11)
        y += 18
    doc.save(str(path))
    doc.close()


class _Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zotrecord-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.notes = self.vault / "Z_Zotero_Notes"
        self.notes.mkdir(parents=True)
        self.biblio = self.vault / "Z_attachments" / "biblio_articles"
        self.biblio.mkdir(parents=True)
        self.archive = self.tmp / "archive"
        # The fixture record, reads, guarded versions and restore journal
        # refer to one synthetic Desktop database, never a Web substitute.
        self.cfg = {"tagging_archive_dir": str(self.archive),
                    "tagging_zotero_transport": "local"}
        self._env = mock.patch.dict("os.environ", {"ZOTERO_API_KEY": "k" * 24})
        self._env.start()
        self.addCleanup(self._env.stop)
        from api.routes.zotrecord import zotrecord_bp
        app = flask.Flask(__name__)
        app.register_blueprint(zotrecord_bp)
        self.client = app.test_client()

    def _record(self, key, item_key, *, note=None, pdfs=(), title="T", item_type="bookSection"):
        from audit.core.obsidian import NoteInfo
        from audit.core.zotero import ZoteroItem
        from audit.engine.inventory import Record
        rec = Record(citation_key=key)
        rec.zotero_item = ZoteroItem(item_key=item_key, item_type=item_type, title=title,
                                     citation_key=key, library_type="user", server_id="fixture-db")
        if note is not None:
            rec.obsidian_note = NoteInfo(path=note, tags=[], has_frontmatter=True)
        rec.pdf_paths = list(pdfs)
        return rec

    @contextlib.contextmanager
    def _patched(self, records, fake):
        """The cached scan and the Zotero client, faked for the whole block —
        the reverts a test runs afterwards must never reach the network."""
        inv = mock.MagicMock()
        inv.records = records
        settings = SimpleNamespace(vault_root=self.vault, biblio_articles_dir=self.biblio,
                                   mapping_file=self.tmp / "mapping.json")
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch("api.routes.zotrecord.load_config", return_value=self.cfg))
            st.enter_context(mock.patch("tagging.writers.load_config", return_value=self.cfg))
            from audit.manager import audit_manager
            st.enter_context(mock.patch.object(audit_manager, "get_inventory", return_value=(inv, settings)))
            st.enter_context(mock.patch.object(audit_manager, "is_scanning", return_value=False))
            for name in ("get_item", "get_item_children", "get_item_tags_full", "get_item_tags",
                         "set_item_parent", "set_item_tags", "set_item_fields", "set_item_deleted",
                         "resolve_user_id"):
                st.enter_context(mock.patch.object(zotero_write, name, getattr(fake, name)))
            yield

    def _post(self, url, body, records, fake):
        with self._patched(records, fake):
            return self.client.post(url, json=body, headers=_HEADERS)


class TestRetitle(_Env):
    def setUp(self):
        super().setUp()
        self.pdf = self.biblio / "madigand_2019.pdf"
        _make_pdf(self.pdf, ["9. FORMES CLINIQUES DES SCHIZOPHRENIES", "", "Jeremy Madigand",
                             "in Sonia Dollfus, Les schizophrenies"])
        self.fake = FakeZotero({"58T9WL5I": {"itemType": "bookSection", "title": "Les Schizophrénies",
                                              "bookTitle": "Les schizophrénies", "tags": []}})
        self.records = {"madigandSchizophrenies2019": self._record(
            "madigandSchizophrenies2019", "58T9WL5I", pdfs=[self.pdf], title="Les Schizophrénies")}

    def test_changed_database_refuses_without_write(self):
        self.records['madigandSchizophrenies2019'].zotero_item.server_id = 'old-db'
        response = self._post('/api/zotrecord/retitle', {
            'citation_key': 'madigandSchizophrenies2019', 'title': 'New title',
            'accept_unauthenticated': True, 'confirm': True}, self.records, self.fake)
        self.assertGreaterEqual(response.status_code, 400)
        self.assertEqual(self.fake.writes(), [])

    def test_writes_an_authenticated_title_and_journals_a_revertible_op(self):
        r = self._post("/api/zotrecord/retitle", {
            "citation_key": "madigandSchizophrenies2019",
            "title": "Formes cliniques des schizophrénies", "confirm": True}, self.records, self.fake)
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["evidence"]["page"], 1)
        self.assertEqual(self.fake.items["58T9WL5I"]["title"], "Formes cliniques des schizophrénies")
        man = journal.load(self.vault, self.cfg)
        op = journal.find_op(man, d["op_id"])
        self.assertEqual(op["kind"], "zotero_metadata_write")
        self.assertEqual(op["before_fields"], {"title": "Les Schizophrénies"})
        self.assertEqual(op["state"], "applied")
        self.assertIsNotNone(op.get("version_after"))
        from tagging.writers import _revert_zotero_metadata
        with self._patched(self.records, self.fake):
            self.assertTrue(_revert_zotero_metadata(self.vault, self.cfg, op)["ok"])
        self.assertEqual(self.fake.items["58T9WL5I"]["title"], "Les Schizophrénies")

    def test_a_title_the_pdf_does_not_print_is_refused_unless_acknowledged(self):
        body = {"citation_key": "madigandSchizophrenies2019", "title": "Something else entirely",
                "confirm": True}
        r = self._post("/api/zotrecord/retitle", body, self.records, self.fake)
        self.assertEqual(r.status_code, 409, r.get_json())
        self.assertEqual(r.get_json()["reason"], "title_not_authenticated")
        self.assertEqual(self.fake.writes(), [])
        r = self._post("/api/zotrecord/retitle", dict(body, accept_unauthenticated=True),
                       self.records, self.fake)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertFalse(r.get_json()["evidence"]["found"])

    def test_a_title_that_moved_in_zotero_since_review_is_refused(self):
        self.fake.items["58T9WL5I"]["title"] = "Edited by hand meanwhile"
        r = self._post("/api/zotrecord/retitle", {
            "citation_key": "madigandSchizophrenies2019", "title": "Formes cliniques des schizophrénies",
            "expected_title": "Les Schizophrénies", "confirm": True}, self.records, self.fake)
        self.assertEqual(r.status_code, 409, r.get_json())
        self.assertEqual(r.get_json()["status"], "stale")
        self.assertEqual(self.fake.writes(), [])

    def test_dry_run_authenticates_and_writes_nothing(self):
        r = self._post("/api/zotrecord/retitle", {
            "citation_key": "madigandSchizophrenies2019", "title": "Formes cliniques des schizophrénies",
            "dry_run": True}, self.records, self.fake)
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertTrue(d["dry_run"])
        self.assertTrue(d["evidence"]["found"])
        self.assertEqual(d["current_title"], "Les Schizophrénies")
        self.assertEqual(self.fake.writes(), [])
        self.assertFalse((self.archive).exists())

    def test_confirm_is_required_and_a_record_without_zotero_item_is_refused(self):
        r = self._post("/api/zotrecord/retitle", {
            "citation_key": "madigandSchizophrenies2019", "title": "X y z"}, self.records, self.fake)
        self.assertEqual(r.status_code, 400)
        r = self._post("/api/zotrecord/retitle", {"citation_key": "nope", "title": "X y z",
                                                  "confirm": True}, self.records, self.fake)
        self.assertEqual(r.status_code, 404)


class TestMerge(_Env):
    def setUp(self):
        super().setUp()
        self.note = self.notes / "keep2023.md"
        self.note.write_text("---\ntags:\n  - textbooks\n---\n**Kept**\n", encoding="utf-8")
        shared = str(self.biblio / "kaplan_2022.pdf")
        self.fake = FakeZotero({
            "KEEP0001": {"itemType": "book", "title": "Kaplan", "extra": "", "citationKey": "keep2023",
                         "tags": [{"tag": "dep"}, {"tag": "textbooks", "type": 1}]},
            "KEEPATT1": {"itemType": "attachment", "linkMode": "linked_file", "parentItem": "KEEP0001",
                         "path": shared, "title": "kaplan_2022.pdf"},
            "KEEPNOTE": {"itemType": "note", "parentItem": "KEEP0001", "note": "<p>read</p>"},
            "RETIRE01": {"itemType": "book", "title": "Kaplan (dup)", "extra": "", "citationKey": "retire2022",
                         "tags": [{"tag": "psychopatho"}, {"tag": "textbooks"}]},
            "RETATT01": {"itemType": "attachment", "linkMode": "linked_file", "parentItem": "RETIRE01",
                         "path": shared, "title": "kaplan_2022.pdf"},
            "RETATT02": {"itemType": "attachment", "linkMode": "linked_file", "parentItem": "RETIRE01",
                         "path": str(self.biblio / "kaplan_extra.pdf"), "title": "kaplan_extra.pdf"},
        })
        self.records = {
            "keep2023": self._record("keep2023", "KEEP0001", note=self.note, item_type="book"),
            "retire2022": self._record("retire2022", "RETIRE01", pdfs=[self.biblio / "kaplan_2022.pdf"],
                                       item_type="book"),
        }

    def _merge(self, body=None, **kw):
        body = body or {"keep": "keep2023", "retire": "retire2022", "confirm": True}
        return self._post("/api/zotrecord/merge", body, self.records, self.fake, **kw)

    def test_merge_refuses_mismatched_selection_identity(self):
        self.records['retire2022'].zotero_item.server_id = 'old-db'
        response = self._merge()
        self.assertGreaterEqual(response.status_code, 400)
        self.assertEqual(self.fake.writes(), [])

    def test_lost_reparent_reply_keeps_unknown_evidence_after_prune(self):
        original = self.fake.set_item_parent
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise zotero_write.ZoteroUnavailable('reply lost after apply')
        self.fake.set_item_parent = lost
        response = self._merge()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.fake.items['RETATT02']['parentItem'], 'KEEP0001')
        self.assertNotIn("is in Zotero's trash", response.json['note'])
        manifest = journal.load(self.vault, self.cfg)
        op = next(o for o in manifest['ops'] if o['kind'] == 'zotero_child_reparent')
        self.assertEqual(op['state'], 'outcome_unknown')
        journal.prune(self.vault, self.cfg, manifest)
        self.assertIsNotNone(journal.find_op(manifest, op['op_id']))

    def test_lost_trash_reply_keeps_unknown_evidence_after_prune(self):
        original = self.fake.set_item_deleted
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise zotero_write.ZoteroUnavailable('reply lost after apply')
        self.fake.set_item_deleted = lost
        response = self._merge()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.fake.items['RETIRE01']['deleted'], 1)
        manifest = journal.load(self.vault, self.cfg)
        op = next(o for o in manifest['ops'] if o['kind'] == 'zotero_item_trash')
        self.assertEqual(op['state'], 'outcome_unknown')
        journal.prune(self.vault, self.cfg, manifest)
        self.assertIsNotNone(journal.find_op(manifest, op['op_id']))

    def test_merge_reparents_unions_aliases_and_trashes_and_is_undone_as_one_batch(self):
        r = self._merge()
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertTrue(d["ok"], d)
        # The attachment whose path the kept item already links rides the
        # trash with its parent; the other child moves.
        self.assertEqual(self.fake.items["RETATT02"]["parentItem"], "KEEP0001")
        self.assertEqual(self.fake.items["RETATT01"]["parentItem"], "RETIRE01")
        self.assertEqual(d["skipped_children"], ["RETATT01"])
        tags = [t["tag"] for t in self.fake.items["KEEP0001"]["tags"]]
        self.assertEqual(sorted(tags), ["dep", "psychopatho", "textbooks"])
        self.assertEqual(self.fake.items["KEEP0001"]["extra"], "tex.ids: retire2022")
        self.assertEqual(self.fake.items["RETIRE01"]["deleted"], 1)
        text = self.note.read_text(encoding="utf-8")
        self.assertIn("aliases:\n  - retire2022", text)
        self.assertIn("tags:\n  - textbooks", text)
        # One batch, every kind journalled, every op applied.
        man = journal.load(self.vault, self.cfg)
        ops = [op for op in man["ops"] if op.get("batch_id") == d["batch_id"]]
        self.assertEqual(sorted(op["kind"] for op in ops),
                         ["note_aliases", "zotero_child_reparent", "zotero_item_trash",
                          "zotero_metadata_write", "zotero_tag"])
        self.assertTrue(all(op["state"] == "applied" for op in ops))
        # Undo: newest first, each store back to before.
        from tagging.writers import (_revert_zotero_child_reparent, _revert_zotero_item_trash,
                                     _revert_zotero_metadata, _revert_zotero_tag)
        with self._patched(self.records, self.fake):
            for op in reversed(ops):
                fn = {"zotero_child_reparent": _revert_zotero_child_reparent,
                      "zotero_item_trash": _revert_zotero_item_trash,
                      "zotero_metadata_write": _revert_zotero_metadata,
                      "zotero_tag": _revert_zotero_tag,
                      "note_aliases": lambda v, c, o: journal.revert_op(v, c, o)}[op["kind"]]
                res = fn(self.vault, self.cfg, op)
                self.assertTrue(res["ok"], (op["kind"], res))
                self.assertEqual(op["state"], "reverted", op["kind"])
        self.assertEqual(self.fake.items["RETATT02"]["parentItem"], "RETIRE01")
        self.assertEqual(self.fake.items["RETIRE01"]["deleted"], 0)
        self.assertEqual(self.fake.items["KEEP0001"]["extra"], "")
        self.assertEqual([t["tag"] for t in self.fake.items["KEEP0001"]["tags"]], ["dep", "textbooks"])
        self.assertEqual(self.note.read_text(encoding="utf-8"), "---\ntags:\n  - textbooks\n---\n**Kept**\n")

    def test_changed_tags_during_merge_are_not_overwritten_by_version_refresh(self):
        original_read = self.fake.get_item_tags_full
        count = 0
        def read(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                self.fake.items["KEEP0001"]["tags"].append({"tag": "external-new"})
                self.fake._bump("KEEP0001")
            return original_read(*args, **kwargs)
        self.fake.get_item_tags_full = read
        response = self._merge()
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertIn({"tag": "external-new"}, self.fake.items["KEEP0001"]["tags"])
        self.assertFalse(any(call[0] == "tags" for call in self.fake.writes()))
        self.assertFalse(self.fake.items["RETIRE01"].get("deleted"))
        ops = journal.load(self.vault, self.cfg)["ops"]
        self.assertTrue(any(op["state"] == "applied" for op in ops))
        for op in ops:
            if op["kind"] in ("zotero_tag", "zotero_item_trash"):
                self.assertEqual(op["state"], "failed")

    def test_tag_timeout_retains_unknown_intent_and_stops_before_trash(self):
        with mock.patch("tagging.writers._zotero_patch", return_value=(
                {"error": "reply lost", "outcome_unknown": True}, 503, None)):
            result = self._merge()
        self.assertEqual(result.status_code, 503)
        ops = {op["kind"]: op for op in journal.load(self.vault, self.cfg)["ops"]}
        self.assertEqual(ops["zotero_tag"]["state"], "outcome_unknown")
        self.assertEqual(ops["zotero_item_trash"]["state"], "failed")
        self.assertFalse(self.fake.items["RETIRE01"].get("deleted"))

    def test_a_landed_extra_write_with_unselected_field_change_is_applied(self):
        """Zotero normalises on save: an Extra PATCH can promote citationKey.
        ``zotero_metadata_patch`` reports that as ok:False; treating it as a
        failed PATCH flipped a landed write and skipped the trash
        (2026-09-19 review A3)."""
        self.fake.also_change = {"citationKey": "promotedKey"}
        r = self._merge()
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertTrue(d["ok"], d)
        self.assertEqual(self.fake.items["KEEP0001"]["extra"], "tex.ids: retire2022")
        self.assertEqual(self.fake.items["KEEP0001"]["citationKey"], "promotedKey")
        self.assertEqual(self.fake.items["RETIRE01"]["deleted"], 1)
        man = journal.load(self.vault, self.cfg)
        ops = {op["kind"]: op for op in man["ops"] if op.get("batch_id") == d["batch_id"]}
        self.assertEqual(ops["zotero_metadata_write"]["state"], "applied")
        self.assertIn("citationKey", ops["zotero_metadata_write"]["unexpected_changes"])
        self.assertEqual(ops["zotero_item_trash"]["state"], "applied")

    def test_an_unexpected_leftover_child_refuses_the_trash(self):
        orig = self.fake.set_item_parent

        def wrap(base, lib, key, parent, version, timeout=15.0, api_key=None):
            ver = orig(base, lib, key, parent, version, timeout=timeout, api_key=api_key)
            self.fake.items["NEWCHILD"] = {
                "itemType": "attachment", "linkMode": "linked_file",
                "parentItem": "RETIRE01", "path": "/tmp/x.pdf", "title": "x.pdf"}
            self.fake.versions["NEWCHILD"] = 1
            return ver

        self.fake.set_item_parent = wrap
        r = self._merge()
        self.assertEqual(r.status_code, 409, r.get_json())
        d = r.get_json()
        self.assertFalse(d["ok"])
        self.assertEqual(d["steps"][-1]["status"], "leftover_children")
        self.assertIn("NEWCHILD", d["steps"][-1]["leftover"])
        self.assertNotEqual(self.fake.items["RETIRE01"].get("deleted"), 1)

    def test_refuses_when_the_retired_record_carries_the_obsidian_note(self):
        r = self._merge({"keep": "retire2022", "retire": "keep2023", "confirm": True})
        self.assertEqual(r.status_code, 409, r.get_json())
        self.assertEqual(r.get_json()["reason"], "note_on_retired_side")
        self.assertEqual(self.fake.writes(), [])

    def test_refuses_two_reading_notes_rather_than_fusing_them(self):
        self.fake.items["RETNOTE1"] = {"itemType": "note", "parentItem": "RETIRE01", "note": "<p>x</p>"}
        r = self._merge()
        self.assertEqual(r.status_code, 409, r.get_json())
        self.assertEqual(r.get_json()["reason"], "both_zotero_notes")
        self.assertEqual(self.fake.writes(), [])

    def test_refuses_same_key_unknown_key_and_group_library(self):
        r = self._merge({"keep": "keep2023", "retire": "keep2023", "confirm": True})
        self.assertEqual(r.status_code, 400)
        r = self._merge({"keep": "keep2023", "retire": "ghost", "confirm": True})
        self.assertEqual(r.status_code, 404)
        self.records["retire2022"].zotero_item.library_type = "group"
        self.records["retire2022"].zotero_item.group_id = 484650
        r = self._merge()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "group_library")
        self.assertEqual(self.fake.writes(), [])

    def test_a_failed_patch_stops_the_merge_and_flips_the_unrun_ops_failed(self):
        self.fake.fail_on.add(("parent", "RETATT02"))
        r = self._merge()
        self.assertEqual(r.status_code, 502, r.get_json())
        d = r.get_json()
        self.assertFalse(d["ok"])
        # Nothing after the failed reparent ran.
        self.assertEqual(self.fake.items["RETIRE01"].get("deleted", 0), 0)
        self.assertEqual(self.fake.items["KEEP0001"]["extra"], "")
        man = journal.load(self.vault, self.cfg)
        ops = {op["kind"]: op for op in man["ops"] if op.get("batch_id") == d["batch_id"]}
        self.assertEqual(ops["zotero_child_reparent"]["state"], "outcome_unknown")
        self.assertEqual(ops["zotero_item_trash"]["state"], "failed")
        self.assertEqual(ops["zotero_metadata_write"]["state"], "failed")
        self.assertEqual(ops["zotero_tag"]["state"], "failed")
        # The note write ran first (it is local) and stays revertible.
        self.assertEqual(ops["note_aliases"]["state"], "applied")

    def test_preview_and_dry_run_plan_without_writing(self):
        r = self._post("/api/zotrecord/merge-preview", {"keep": "keep2023", "retire": "retire2022"},
                       self.records, self.fake)
        self.assertEqual(r.status_code, 200, r.get_json())
        plan = r.get_json()["plan"]
        self.assertEqual(plan["reparent"], ["RETATT02"])
        self.assertEqual(plan["skipped_children"], ["RETATT01"])
        self.assertEqual(plan["tags_added"], ["psychopatho"])
        self.assertEqual(plan["extra_after"], "tex.ids: retire2022")
        self.assertEqual(plan["aliases_added"], ["retire2022"])
        r = self._merge({"keep": "keep2023", "retire": "retire2022", "dry_run": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json()["dry_run"])
        self.assertEqual(self.fake.writes(), [])
        self.assertFalse(self.archive.exists())
        self.assertNotIn("aliases", self.note.read_text(encoding="utf-8"))


class TestReverts(unittest.TestCase):
    """The two new external kinds refuse to undo over a later change."""

    def setUp(self):
        self.fake = FakeZotero({
            "CHILD001": {"itemType": "attachment", "parentItem": "NEWPAR01"},
            "PARENT01": {"itemType": "book", "deleted": 1},
        })
        self.cfg = {"tagging_zotero_transport": "local"}
        self.patches = [mock.patch.object(zotero_write, n, getattr(self.fake, n))
                        for n in ("get_item", "set_item_parent", "set_item_deleted")]
        self.patches.append(mock.patch.dict("os.environ", {"ZOTERO_API_KEY": "k" * 24}))
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def test_reparent_revert_refuses_a_child_moved_again(self):
        from tagging.writers import _revert_zotero_child_reparent
        op = {"kind": "zotero_child_reparent", "item_key": "CHILD001", "before_parent": "OLDPAR01",
              "after_parent": "NEWPAR01", "transport": "local", "server_id": "fixture-db", "base_url": BASE, "state": "applied"}
        self.fake.items["CHILD001"]["parentItem"] = "THIRD001"
        res = _revert_zotero_child_reparent(Path("/tmp"), self.cfg, op)
        self.assertFalse(res["ok"])
        self.assertEqual(op["state"], "applied")
        self.fake.items["CHILD001"]["parentItem"] = "NEWPAR01"
        res = _revert_zotero_child_reparent(Path("/tmp"), self.cfg, op)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.fake.items["CHILD001"]["parentItem"], "OLDPAR01")
        self.assertEqual(op["state"], "reverted")

    def test_item_trash_revert_restores_only_a_still_trashed_item(self):
        from tagging.writers import _revert_zotero_item_trash
        op = {"kind": "zotero_item_trash", "item_key": "PARENT01", "transport": "local", "server_id": "fixture-db",
              "base_url": BASE, "state": "applied"}
        res = _revert_zotero_item_trash(Path("/tmp"), self.cfg, op)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.fake.items["PARENT01"]["deleted"], 0)
        self.assertEqual(op["state"], "reverted")
        op2 = dict(op, state="applied")
        res = _revert_zotero_item_trash(Path("/tmp"), self.cfg, op2)
        self.assertEqual(res["status"], "skipped")
        self.assertEqual(op2["state"], "reverted")


class TestAliasesFrontmatter(unittest.TestCase):
    def test_add_aliases_shapes_and_round_trip(self):
        from api.routes._audit_tagwrite import add_aliases, read_aliases
        base = "---\ntags: [a]\n---\nbody\n"
        out, added, reason = add_aliases(base, ["k1"])
        self.assertIsNone(reason)
        self.assertEqual(added, ["k1"])
        self.assertEqual(read_aliases(out), ["k1"])
        self.assertIn("tags: [a]", out)
        out2, added2, _ = add_aliases(out, ["k1", "k2"])
        self.assertEqual(added2, ["k2"])
        self.assertEqual(read_aliases(out2), ["k1", "k2"])
        flow = "---\naliases: [x]\n---\nbody\n"
        out3, added3, _ = add_aliases(flow, ["y"])
        self.assertEqual(read_aliases(out3), ["x", "y"])
        scalar = "---\naliases: x\n---\nbody\n"
        out4, _, _ = add_aliases(scalar, ["y"])
        self.assertEqual(read_aliases(out4), ["x", "y"])
        self.assertEqual(add_aliases("no frontmatter\n", ["k"])[2], "no_frontmatter")


if __name__ == "__main__":
    unittest.main()


class TestBatchRestoreOfAMerge(_Env):
    """A merge writes TWO ops on the kept item (tags, Extra). The batch restore
    reads every op's version BEFORE its first PATCH, so the second revert on
    that item used to carry a stale guard and 412 — found on the disposable
    copy 2026-09-17. It must re-read an item it already touched."""

    def test_two_ops_on_one_item_both_revert(self):
        from api.routes.tagging import tagging_bp
        note = self.notes / "keep.md"
        note.write_text("---\ntags: [a]\n---\nbody\n", encoding="utf-8")
        fake = FakeZotero({
            "KEEP0001": {"itemType": "book", "title": "K", "extra": "", "tags": [{"tag": "a"}]},
            "RETIRE01": {"itemType": "book", "title": "R", "extra": "", "tags": [{"tag": "b"}]},
        })
        records = {"keep": self._record("keep", "KEEP0001", note=note, item_type="book"),
                   "retire": self._record("retire", "RETIRE01", item_type="book")}
        r = self._post("/api/zotrecord/merge", {"keep": "keep", "retire": "retire", "confirm": True},
                       records, fake)
        self.assertEqual(r.status_code, 200, r.get_json())
        batch = r.get_json()["batch_id"]
        app = flask.Flask(__name__)
        app.register_blueprint(tagging_bp)
        with self._patched(records, fake), \
                mock.patch("api.routes.tagging.load_config", return_value=self.cfg), \
                mock.patch("api.routes.tagging._vault_root", return_value=self.vault):
            resp = app.test_client().post("/api/tagging/restore", json={"batch_id": batch, "confirm": True},
                                          headers=_HEADERS)
            # Drain INSIDE the patch scope: the SSE worker runs only while the
            # response is consumed (the suite trap recorded in CLAUDE.md).
            frames = [json.loads(line[6:]) for line in resp.get_data(as_text=True).splitlines()
                      if line.startswith("data: {")]
        term = [f["restore"] for f in frames if "restore" in f]
        self.assertTrue(term, frames)
        by_kind = {x["kind"]: x for x in term[-1]["results"]}
        self.assertTrue(by_kind["zotero_tag"]["ok"], by_kind)
        self.assertTrue(by_kind["zotero_metadata_write"]["ok"], by_kind)
        self.assertTrue(by_kind["zotero_item_trash"]["ok"], by_kind)
        self.assertEqual(fake.items["KEEP0001"]["extra"], "")
        self.assertEqual([t["tag"] for t in fake.items["KEEP0001"]["tags"]], ["a"])
        self.assertEqual(fake.items["RETIRE01"]["deleted"], 0)
        self.assertEqual(note.read_text(encoding="utf-8"), "---\ntags: [a]\n---\nbody\n")


class TestTrash(_Env):
    """``/api/zotrecord/trash``: retire a standalone record (no PDF, no note)."""

    def setUp(self):
        super().setUp()
        self.fake = FakeZotero({"EMPTY001": {"itemType": "bookSection", "title": "Pathologies", "tags": []},
                                "KIDS0001": {"itemType": "book", "title": "K", "tags": []},
                                "KIDATT01": {"itemType": "attachment", "parentItem": "KIDS0001", "path": "/x.pdf"}})
        self.records = {"empty": self._record("empty", "EMPTY001"),
                        "kids": self._record("kids", "KIDS0001", item_type="book"),
                        "withpdf": self._record("withpdf", "EMPTY001", pdfs=[self.biblio / "a.pdf"])}

    def test_trashes_journals_and_reverts(self):
        r = self._post("/api/zotrecord/trash", {"citation_key": "empty", "confirm": True}, self.records, self.fake)
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertEqual(self.fake.items["EMPTY001"]["deleted"], 1)
        man = journal.load(self.vault, self.cfg)
        op = journal.find_op(man, d["op_id"])
        self.assertEqual(op["kind"], "zotero_item_trash")
        from tagging.writers import _revert_zotero_item_trash
        with self._patched(self.records, self.fake):
            self.assertTrue(_revert_zotero_item_trash(self.vault, self.cfg, op)["ok"])
        self.assertEqual(self.fake.items["EMPTY001"]["deleted"], 0)

    def test_refuses_a_record_with_a_pdf_or_children_and_dry_run_writes_nothing(self):
        r = self._post("/api/zotrecord/trash", {"citation_key": "withpdf", "confirm": True}, self.records, self.fake)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "record_has_pdf")
        r = self._post("/api/zotrecord/trash", {"citation_key": "kids", "confirm": True}, self.records, self.fake)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "item_has_children")
        r = self._post("/api/zotrecord/trash", {"citation_key": "kids", "confirm": True, "accept_children": True},
                       self.records, self.fake)
        self.assertEqual(r.status_code, 200, r.get_json())
        r = self._post("/api/zotrecord/trash", {"citation_key": "empty", "dry_run": True}, self.records, self.fake)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["dry_run"])
        self.assertEqual(self.fake.items["EMPTY001"].get("deleted", 0), 0)
