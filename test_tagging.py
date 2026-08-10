"""Tests for the biblio Tagging subsystem (slice 1 — read-only).

Covers the pure ``_tags.md`` → ``{tag: gloss}`` vocabulary parser, and the two
read-only endpoints: ``/api/tagging/vocab`` (controlled vocabulary) and
``/api/tagging/inventory`` (biblio-scoped projection of the cached audit scan).

Hermetic: builds a minimal Flask app registering only ``tagging_bp`` and mocks
``audit_manager.get_inventory`` / the config + vault-root resolution, so nothing
touches the user's real app data or requires a live audit scan.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import flask

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from api.routes.tagging import tagging_bp  # noqa: E402
from tagging.vocab import build_vocab  # noqa: E402

_HEADERS = {"X-Requested-With": "ChatEKLD"}

_SAMPLE_TAGS_MD = """---
tags: []
---
# Tags

| Description                            | Tags             | Fichiers Associés |
| -------------------------------------- | ---------------- | ----------------- |
| Dépression, EDC, épisode dépressif     | #dep, #mood      | [[dep]]           |
| Trouble bipolaire, cyclothymie         | #bipo, #mood     | [[bipo]]          |
| Schizophrénie, troubles psychotiques   | #scz             | [[scz]]           |
"""


def _build_app() -> flask.Flask:
    """Minimal app with only the tagging blueprint (no global origin guard)."""
    app = flask.Flask(__name__)
    app.register_blueprint(tagging_bp)
    return app


class TestBuildVocab(unittest.TestCase):
    def test_parses_tag_ids_and_glosses(self):
        vocab = build_vocab(_SAMPLE_TAGS_MD)
        self.assertEqual(vocab["dep"], "Dépression, EDC, épisode dépressif")
        self.assertEqual(vocab["scz"], "Schizophrénie, troubles psychotiques")
        # A tag in two rows accumulates both glosses, deduped and joined.
        self.assertIn("Dépression", vocab["mood"])
        self.assertIn("Trouble bipolaire", vocab["mood"])
        self.assertEqual(vocab["mood"].count(" / "), 1)

    def test_strips_hash_and_drops_header(self):
        vocab = build_vocab(_SAMPLE_TAGS_MD)
        self.assertNotIn("#dep", vocab)
        self.assertNotIn("Tags", vocab)          # header row skipped
        self.assertNotIn("Description", vocab)

    def test_empty_and_garbage_are_safe(self):
        self.assertEqual(build_vocab(""), {})
        self.assertEqual(build_vocab("not a table at all"), {})

    def test_glossless_tag_reaches_the_vocabulary(self):
        # 2026-07-19: parse_tags skipped rows with no Description, so a
        # legitimately-defined gloss-less tag could neither be proposed nor
        # applied (the vocab is the write gate) — with no error anywhere.
        table = ("| Description | Tags |\n| --- | --- |\n"
                 "| | #newtag |\n"
                 "| Dépression | #dep |\n")
        vocab = build_vocab(table)
        self.assertIn("newtag", vocab)
        self.assertEqual(vocab["newtag"], "")
        self.assertEqual(vocab["dep"], "Dépression")

    def test_space_containing_tag_id_is_skipped(self):
        # Tag ids must be single tokens: the client parser and the frontmatter
        # scalar merge both split on whitespace, so a space-id could never
        # round-trip — the gate enforces the invariant instead of offering it.
        table = ("| Description | Tags |\n| --- | --- |\n"
                 "| Broken | #foo bar |\n"
                 "| Fine | #ok |\n")
        with self.assertLogs("tagging.vocab", level="WARNING"):
            vocab = build_vocab(table)
        self.assertNotIn("foo bar", vocab)
        self.assertIn("ok", vocab)


class TestVocabEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tagging-vocab-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.vault.mkdir(parents=True, exist_ok=True)
        self.client = _build_app().test_client()

    def test_returns_vocab_from_tags_file(self):
        (self.vault / "_tags.md").write_text(_SAMPLE_TAGS_MD, encoding="utf-8")
        with mock.patch("api.routes.tagging.load_config",
                        return_value={"vault_thesaurus_tags_path": "_tags.md"}), \
             mock.patch("api.routes.tagging._vault_root", return_value=self.vault):
            r = self.client.get("/api/tagging/vocab", headers=_HEADERS)
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        tags = {row["tag"] for row in d["vocab"]}
        self.assertEqual(tags, {"dep", "mood", "bipo", "scz"})
        self.assertEqual(d["count"], 4)
        self.assertEqual(d["source"], "_tags.md")

    def test_404_when_tags_file_missing(self):
        with mock.patch("api.routes.tagging.load_config",
                        return_value={"vault_thesaurus_tags_path": "_tags.md"}), \
             mock.patch("api.routes.tagging._vault_root", return_value=self.vault):
            r = self.client.get("/api/tagging/vocab", headers=_HEADERS)
        self.assertEqual(r.status_code, 404)

    def test_rejects_traversal_path(self):
        with mock.patch("api.routes.tagging.load_config",
                        return_value={"vault_thesaurus_tags_path": "../escape.md"}), \
             mock.patch("api.routes.tagging._vault_root", return_value=self.vault):
            r = self.client.get("/api/tagging/vocab", headers=_HEADERS)
        self.assertEqual(r.status_code, 404)


class TestInventoryEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tagging-inv-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = (self.tmp / "vault").resolve()
        self.biblio = self.vault / "Z_attachments" / "biblio_articles"
        self.biblio.mkdir(parents=True, exist_ok=True)
        self.client = _build_app().test_client()

    def _settings(self):
        return SimpleNamespace(vault_root=self.vault, biblio_articles_dir=self.biblio)

    def _record(self, key, pdf_names, *, note_tags=None, finder=None,
                zot_parent=None, zot_child=None):
        from audit.core.obsidian import NoteInfo
        from audit.engine.inventory import Record

        rec = Record(citation_key=key)
        rec.pdf_paths = [self.biblio / n for n in pdf_names]
        rec.finder_tag_set = set(finder or [])
        rec.bib_entry = SimpleNamespace(
            title=f"Title {key}", year="2020", authors=["Smith"], keywords=set())
        if note_tags is not None:
            note = self.vault / "Z_Zotero_Notes" / f"{key}.md"
            rec.obsidian_note = NoteInfo(path=note, tags=list(note_tags),
                                         has_frontmatter=True)
        if zot_parent is not None or zot_child is not None:
            rec.zotero_item = SimpleNamespace(
                item_key=f"ZK-{key}",
                tags=list(zot_parent or []),
                child_notes=[SimpleNamespace(tags=list(zot_child or []))],
            )
        return rec

    def test_404_without_scan(self):
        with mock.patch.object(
            __import__("audit.manager", fromlist=["audit_manager"]).audit_manager,
            "get_inventory", return_value=(None, None)
        ):
            r = self.client.get("/api/tagging/inventory", headers=_HEADERS)
        self.assertEqual(r.status_code, 404)

    def test_projects_biblio_records_with_all_tag_sources(self):
        rec = self._record(
            "smith2020", ["smith_2020.pdf"],
            note_tags=["dep"], finder=["anxiety"],
            zot_parent=["mood"], zot_child=["dep", "sleep"],
        )
        inv = mock.MagicMock()
        inv.records = {"smith2020": rec}
        from audit.manager import audit_manager
        with mock.patch.object(audit_manager, "get_inventory",
                               return_value=(inv, self._settings())):
            r = self.client.get("/api/tagging/inventory", headers=_HEADERS)
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertEqual(d["count"], 1)
        row = d["rows"][0]
        self.assertEqual(row["citation_key"], "smith2020")
        self.assertEqual(row["finder_tags"], ["anxiety"])
        self.assertEqual(row["note"]["tags"], ["dep"])
        self.assertEqual(row["note"]["present"], True)
        self.assertEqual(row["note"]["rel"], "Z_Zotero_Notes/smith2020.md")
        self.assertEqual(row["zotero"]["item_key"], "ZK-smith2020")
        self.assertEqual(row["zotero"]["parent_tags"], ["mood"])
        self.assertEqual(row["zotero"]["child_note_tags"], ["dep", "sleep"])
        self.assertEqual(row["pdf_paths"][0]["rel"],
                         "Z_attachments/biblio_articles/smith_2020.pdf")

    def test_excludes_records_with_no_biblio_pdf(self):
        # A record whose only PDF lives OUTSIDE biblio_articles/ is out of scope.
        outside = self._record("elsewhere2019", [])
        outside.pdf_paths = [self.vault / "other" / "x.pdf"]
        inv = mock.MagicMock()
        inv.records = {"elsewhere2019": outside}
        from audit.manager import audit_manager
        with mock.patch.object(audit_manager, "get_inventory",
                               return_value=(inv, self._settings())):
            r = self.client.get("/api/tagging/inventory", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["count"], 0)


class TestProposeParse(unittest.TestCase):
    def test_constrains_to_vocab_dedups_clamps(self):
        from tagging.propose import parse_proposal
        vocab = {"dep": "d", "mood": "m"}
        raw = ('noise [{"tag":"#Dep","confidence":1.5},'
               '{"tag":"dep","confidence":0.4},'          # dup, dropped
               '{"tag":"notreal","confidence":0.9},'      # out-of-vocab, dropped
               '{"tag":"mood","confidence":"x"}] tail')   # bad conf -> 0.5
        out = parse_proposal(raw, vocab, max_tags=4)
        self.assertEqual([o["tag"] for o in out], ["dep", "mood"])
        self.assertEqual(out[0]["confidence"], 1.0)   # clamped
        self.assertEqual(out[1]["confidence"], 0.5)   # default

    def test_malformed_yields_empty(self):
        from tagging.propose import parse_proposal
        self.assertEqual(parse_proposal("not json", {"dep": "d"}, 4), [])
        self.assertEqual(parse_proposal("", {"dep": "d"}, 4), [])

    def test_caps_max_tags(self):
        from tagging.propose import parse_proposal
        vocab = {f"t{i}": "x" for i in range(6)}
        raw = json_dumps_arr(vocab)
        out = parse_proposal(raw, vocab, max_tags=2)
        self.assertEqual(len(out), 2)


def json_dumps_arr(vocab):
    import json
    return json.dumps([{"tag": t, "confidence": 0.9} for t in vocab])


class TestProposeEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tagging-propose-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.biblio = self.vault / "Z_attachments" / "biblio_articles"
        self.biblio.mkdir(parents=True, exist_ok=True)
        self.client = _build_app().test_client()

    def _inv(self, with_pdf=True):
        from audit.engine.inventory import Record
        rec = Record(citation_key="smith2020")
        rec.pdf_paths = [self.biblio / "smith_2020.pdf"] if with_pdf else []
        rec.bib_entry = SimpleNamespace(title="A depression study", year="2020",
                                        authors=["Smith"], keywords=set())
        inv = mock.MagicMock()
        inv.records = {"smith2020": rec}
        settings = SimpleNamespace(vault_root=self.vault, biblio_articles_dir=self.biblio)
        return inv, settings

    def _post(self, body, *, proposed=None,
              text="a long paper about depression and mood disorders in adult patients",
              with_pdf=True, vocab=None):
        from audit.manager import audit_manager
        vocab = vocab if vocab is not None else {"dep": "Depression", "mood": "Mood"}
        patches = [
            mock.patch.object(audit_manager, "get_inventory",
                              return_value=self._inv(with_pdf)),
            # Knobs at 0 keep the endpoint hermetic: the candidate/exemplar
            # selection would otherwise construct a real local embedder.
            mock.patch("api.routes.tagging.load_config",
                       return_value={"provider": "ollama",
                                     "tagging_candidate_tags": 0,
                                     "tagging_fewshot_k": 0}),
            mock.patch("api.routes.tagging._read_vocab",
                       return_value=(vocab, "_tags.md")),
            mock.patch("api.routes.tagging.text_source.read_pdf_text",
                       return_value=(text, False)),
            mock.patch("api.routes.tagging.tag_propose.propose_tags",
                       return_value=(proposed if proposed is not None else [])),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return self.client.post("/api/tagging/propose", json=body, headers=_HEADERS)

    def test_status_ok(self):
        r = self._post({"citation_key": "smith2020"},
                       proposed=[{"tag": "dep", "confidence": 0.9}])
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertEqual(d["status"], "ok")
        self.assertEqual(d["proposed"][0]["tag"], "dep")

    def test_status_low_confidence(self):
        r = self._post({"citation_key": "smith2020"},
                       proposed=[{"tag": "dep", "confidence": 0.2}])
        self.assertEqual(r.get_json()["status"], "low_confidence")

    def test_status_empty(self):
        r = self._post({"citation_key": "smith2020"}, proposed=[])
        self.assertEqual(r.get_json()["status"], "empty")

    def test_no_text_short_circuits(self):
        r = self._post({"citation_key": "smith2020"}, text="   ")
        d = r.get_json()
        self.assertEqual(d["status"], "no_text")
        self.assertEqual(d["proposed"], [])

    def test_404_when_no_biblio_pdf(self):
        r = self._post({"citation_key": "smith2020"}, with_pdf=False)
        self.assertEqual(r.status_code, 404)


class TestNoteTagWrite(unittest.TestCase):
    """Slice 2: journalled note-frontmatter tag writes + conservative restore."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tagging-write-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.archive = self.tmp / "archive"   # sibling of vault → outside it
        (self.vault / "Z_Zotero_Notes").mkdir(parents=True, exist_ok=True)
        self.biblio = self.vault / "Z_attachments" / "biblio_articles"
        self.biblio.mkdir(parents=True, exist_ok=True)
        (self.vault / "_tags.md").write_text(_SAMPLE_TAGS_MD, encoding="utf-8")
        self.note = self.vault / "Z_Zotero_Notes" / "smith2020.md"
        self.note.write_text("---\ntags:\n  - existing\n---\nbody\n", encoding="utf-8")
        self.client = _build_app().test_client()
        self.cfg = {
            "vault_thesaurus_tags_path": "_tags.md",
            "tagging_archive_dir": str(self.archive),
        }

    def _inv(self):
        from audit.core.obsidian import NoteInfo
        from audit.engine.inventory import Record
        rec = Record(citation_key="smith2020")
        rec.pdf_paths = [self.biblio / "smith_2020.pdf"]
        rec.obsidian_note = NoteInfo(path=self.note, tags=["existing"],
                                     has_frontmatter=True)
        inv = mock.MagicMock()
        inv.records = {"smith2020": rec}
        settings = SimpleNamespace(vault_root=self.vault,
                                   biblio_articles_dir=self.biblio)
        return inv, settings

    def _patches(self):
        from audit.manager import audit_manager
        return (
            mock.patch("api.routes.tagging.load_config", return_value=self.cfg),
            # _prepare_tag_write lives in tagging.writers since the 2026-07-19
            # extraction — its own config lookup needs the same patch.
            mock.patch("tagging.writers.load_config", return_value=self.cfg),
            mock.patch("api.routes.tagging._vault_root", return_value=self.vault),
            mock.patch.object(audit_manager, "get_inventory",
                              return_value=self._inv()),
        )

    def _apply(self, body):
        with contextlib.ExitStack() as st:
            for p in self._patches():
                st.enter_context(p)
            return self.client.post("/api/tagging/apply-note-tags",
                                    json=body, headers=_HEADERS)

    def test_confirm_required(self):
        r = self._apply({"citation_key": "smith2020", "tags": ["dep"]})
        self.assertEqual(r.status_code, 400)

    def test_happy_path_writes_and_journals(self):
        r = self._apply({"citation_key": "smith2020",
                         "tags": ["dep", "notreal"], "confirm": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertEqual(d["added"], ["dep"])
        self.assertEqual(d["rejected"], ["notreal"])   # out-of-vocab dropped
        self.assertTrue(d["op_id"])
        import yaml
        fm = yaml.safe_load(self.note.read_text(encoding="utf-8").split("---")[1])
        self.assertEqual(fm["tags"], ["existing", "dep"])
        # Manifest records the op.
        with contextlib.ExitStack() as st:
            for p in self._patches():
                st.enter_context(p)
            m = self.client.get("/api/tagging/manifest", headers=_HEADERS).get_json()
        self.assertEqual(m["count"], 1)
        self.assertEqual(m["ops"][0]["kind"], "note_tag")
        self.assertEqual(m["ops"][0]["state"], "applied")

    def test_all_out_of_vocab_rejected(self):
        r = self._apply({"citation_key": "smith2020",
                         "tags": ["notreal", "alsobad"], "confirm": True})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(sorted(r.get_json()["rejected"]), ["alsobad", "notreal"])

    def test_stale_diff_409(self):
        r = self._apply({"citation_key": "smith2020", "tags": ["dep"],
                         "confirm": True, "content_sha256": "deadbeef"})
        self.assertEqual(r.status_code, 409)

    def test_restore_reverts_to_original(self):
        original = self.note.read_text(encoding="utf-8")
        r = self._apply({"citation_key": "smith2020", "tags": ["dep"],
                         "confirm": True})
        op_id = r.get_json()["op_id"]
        self.assertNotEqual(self.note.read_text(encoding="utf-8"), original)
        with contextlib.ExitStack() as st:
            for p in self._patches():
                st.enter_context(p)
            rr = self.client.post("/api/tagging/restore",
                                  json={"op_id": op_id, "confirm": True},
                                  headers=_HEADERS)
        self.assertEqual(rr.status_code, 200, rr.get_json())
        self.assertEqual(rr.get_json()["status"], "reverted")
        self.assertEqual(self.note.read_text(encoding="utf-8"), original)

    def test_restore_skips_when_note_edited_after(self):
        r = self._apply({"citation_key": "smith2020", "tags": ["dep"],
                         "confirm": True})
        op_id = r.get_json()["op_id"]
        # Simulate a later hand-edit: on-disk no longer matches hash_after.
        self.note.write_text("---\ntags:\n  - existing\n  - dep\n  - manual\n---\nedited\n",
                             encoding="utf-8")
        after_edit = self.note.read_text(encoding="utf-8")
        with contextlib.ExitStack() as st:
            for p in self._patches():
                st.enter_context(p)
            rr = self.client.post("/api/tagging/restore",
                                  json={"op_id": op_id, "confirm": True},
                                  headers=_HEADERS)
        self.assertEqual(rr.status_code, 200)
        self.assertEqual(rr.get_json()["status"], "skipped")
        # The hand-edit is preserved (not clobbered).
        self.assertEqual(self.note.read_text(encoding="utf-8"), after_edit)


class TestAddVocabTag(unittest.TestCase):
    """Drift-fix (2026-08-06): the journalled ``_tags.md`` vocabulary add —
    ``retire-tag``'s mirror. Confirm gate, single-token shape gate, duplicate
    no-op, journal op kind ``vocab_add`` + snapshot restore, lock 503."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tagging-vocabadd-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.archive = self.tmp / "archive"
        self.vault.mkdir(parents=True, exist_ok=True)
        (self.vault / "_tags.md").write_text(_SAMPLE_TAGS_MD, encoding="utf-8")
        self.client = _build_app().test_client()
        self.cfg = {
            "vault_thesaurus_tags_path": "_tags.md",
            "tagging_archive_dir": str(self.archive),
        }

    def _patches(self):
        return (
            mock.patch("api.routes.tagging.load_config", return_value=self.cfg),
            mock.patch("tagging.writers.load_config", return_value=self.cfg),
            mock.patch("api.routes.tagging._vault_root", return_value=self.vault),
        )

    def _post(self, body):
        with contextlib.ExitStack() as st:
            for p in self._patches():
                st.enter_context(p)
            return self.client.post("/api/tagging/add-vocab-tag",
                                    json=body, headers=_HEADERS)

    def test_confirm_and_shape_gates(self):
        self.assertEqual(self._post({"tag": "sleep"}).status_code, 400)
        self.assertEqual(
            self._post({"tag": "two words", "confirm": True}).status_code, 400)
        self.assertEqual(self._post({"confirm": True}).status_code, 400)

    def test_happy_path_appends_journals_and_restores(self):
        original = (self.vault / "_tags.md").read_text(encoding="utf-8")
        r = self._post({"tag": "#sleep-quality", "gloss": "Sleep quality",
                        "confirm": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertEqual(d["tag"], "sleep-quality")
        self.assertTrue(d["op_id"])
        new_text = (self.vault / "_tags.md").read_text(encoding="utf-8")
        self.assertIn("#sleep-quality", new_text)
        # The appended row must round-trip through the vocabulary parser —
        # it is the writers' security gate, so a malformed row would make the
        # freshly-added tag silently unusable.
        self.assertIn("sleep-quality", build_vocab(new_text))
        with contextlib.ExitStack() as st:
            for p in self._patches():
                st.enter_context(p)
            m = self.client.get("/api/tagging/manifest",
                                headers=_HEADERS).get_json()
            self.assertEqual(m["ops"][0]["kind"], "vocab_add")
            self.assertEqual(m["ops"][0]["state"], "applied")
            rr = self.client.post(
                "/api/tagging/restore",
                json={"op_id": d["op_id"], "confirm": True}, headers=_HEADERS)
        self.assertEqual(rr.status_code, 200, rr.get_json())
        self.assertEqual(rr.get_json()["status"], "reverted")
        self.assertEqual(
            (self.vault / "_tags.md").read_text(encoding="utf-8"), original)

    def test_duplicate_is_a_no_op_with_signal(self):
        original = (self.vault / "_tags.md").read_text(encoding="utf-8")
        r = self._post({"tag": "DEP", "confirm": True})  # exists as #dep
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json()["already"])
        self.assertNotIn("op_id", r.get_json())
        self.assertEqual(
            (self.vault / "_tags.md").read_text(encoding="utf-8"), original)

    def test_missing_vocab_404(self):
        (self.vault / "_tags.md").unlink()
        r = self._post({"tag": "sleep", "confirm": True})
        self.assertEqual(r.status_code, 404)

    def test_lock_busy_503(self):
        with contextlib.ExitStack() as st:
            for p in self._patches():
                st.enter_context(p)
            st.enter_context(mock.patch(
                "rag.vault.obsidian_manager.try_acquire_lock",
                return_value=None))
            r = self.client.post(
                "/api/tagging/add-vocab-tag",
                json={"tag": "sleep", "confirm": True}, headers=_HEADERS)
        self.assertEqual(r.status_code, 503)


class TestAddTagToVocabPure(unittest.TestCase):
    """The pure text transform behind add-vocab-tag."""

    def test_appends_matching_column_count(self):
        from tagging.vocab import add_tag_to_vocab
        new_text, status = add_tag_to_vocab(_SAMPLE_TAGS_MD, "sleep", "Sommeil")
        self.assertEqual(status, "added")
        last_row = [ln for ln in new_text.split("\n") if ln.strip().startswith("|")][-1]
        self.assertEqual(len(last_row.split("|")), len("| a | b | c |".split("|")))
        self.assertIn("#sleep", last_row)
        self.assertIn("Sommeil", last_row)

    def test_header_only_table_inserts_below_separator(self):
        from tagging.vocab import add_tag_to_vocab
        text = "| Description | Tags |\n|---|---|\n"
        new_text, status = add_tag_to_vocab(text, "sleep")
        self.assertEqual(status, "added")
        lines = new_text.split("\n")
        self.assertTrue(lines[1].strip().startswith("|-") or "---" in lines[1])
        self.assertIn("#sleep", lines[2])
        self.assertIn("sleep", build_vocab(new_text))

    def test_invalid_and_exists(self):
        from tagging.vocab import add_tag_to_vocab
        self.assertEqual(add_tag_to_vocab(_SAMPLE_TAGS_MD, "a b")[1], "invalid")
        self.assertEqual(add_tag_to_vocab("no table here", "x")[1], "invalid")
        self.assertEqual(add_tag_to_vocab(_SAMPLE_TAGS_MD, "dep")[1], "exists")
        # Pipe characters in a gloss cannot break the table row.
        new_text, _ = add_tag_to_vocab(_SAMPLE_TAGS_MD, "x", "a|b")
        self.assertIn("a/b", new_text)


class TestRemoveTagsFromFrontmatter(unittest.TestCase):
    """Tag deletion (2026-07-22): the removal splicer's shapes + guards."""

    def _remove(self, text, tags):
        from api.routes._audit_tagwrite import remove_tags_from_frontmatter
        return remove_tags_from_frontmatter(text, tags)

    def test_block_list_removes_matching_item(self):
        text = "---\ntitle: T\ntags:\n  - keep\n  - Drop\n---\nbody\n"
        new, removed, reason = self._remove(text, ["drop"])
        self.assertEqual(reason, "")
        self.assertEqual(removed, ["Drop"])           # case-insensitive match
        self.assertIn("  - keep\n", new)
        self.assertNotIn("Drop", new)
        self.assertIn("title: T", new)                # sibling untouched

    def test_block_list_last_tag_leaves_empty_flow_list(self):
        text = "---\ntags:\n  - only\n---\nbody\n"
        new, removed, _ = self._remove(text, ["only"])
        self.assertEqual(removed, ["only"])
        self.assertIn("tags: []", new)
        self.assertIn("body", new)

    def test_flow_list_and_scalar(self):
        new, removed, _ = self._remove("---\ntags: [a, b, c]\n---\nx\n", ["b"])
        self.assertEqual(removed, ["b"])
        self.assertIn("tags: [a, c]", new)
        new2, removed2, _ = self._remove("---\ntags: a b\n---\nx\n", ["a"])
        self.assertEqual(removed2, ["a"])
        self.assertIn("tags: [b]", new2)

    def test_absent_tag_and_no_frontmatter_are_noops(self):
        text = "---\ntags: [a]\n---\nx\n"
        new, removed, reason = self._remove(text, ["zzz"])
        self.assertEqual((new, removed, reason), (text, [], ""))
        bare = "no frontmatter here\n"
        new2, removed2, _ = self._remove(bare, ["a"])
        self.assertEqual((new2, removed2), (bare, []))

    def test_refusals_mirror_the_add_path(self):
        # Quoted flow list, singular tag: key — refuse, never guess.
        new, _, reason = self._remove("---\ntags: ['a', 'b']\n---\nx\n", ["a"])
        self.assertIsNone(new)
        self.assertIn("quoted", reason)
        new2, _, reason2 = self._remove("---\ntag: a\n---\nx\n", ["a"])
        self.assertIsNone(new2)
        self.assertIn("singular", reason2)

    def test_quoted_block_item_roundtrip(self):
        text = "---\ntags:\n  - \"l'apostrophe\"\n  - keep\n---\nx\n"
        new, removed, reason = self._remove(text, ["l'apostrophe"])
        self.assertEqual(reason, "")
        self.assertEqual(removed, ["l'apostrophe"])
        self.assertIn("  - keep", new)


class TestTagRemovalEndpoint(unittest.TestCase):
    """POST /api/tagging/remove-tags — no vocab gate, journalled, restorable."""

    # Same fixture family as TestNoteTagWrite.
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tagging-rm-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.archive = self.tmp / "archive"
        (self.vault / "Z_Zotero_Notes").mkdir(parents=True, exist_ok=True)
        self.biblio = self.vault / "Z_attachments" / "biblio_articles"
        self.biblio.mkdir(parents=True, exist_ok=True)
        (self.vault / "_tags.md").write_text(_SAMPLE_TAGS_MD, encoding="utf-8")
        self.note = self.vault / "Z_Zotero_Notes" / "smith2020.md"
        # 'obsolete' is deliberately NOT in _SAMPLE_TAGS_MD — removal must not
        # be vocabulary-gated (the tag worth deleting often left the vocab).
        self.note.write_text("---\ntags:\n  - keep\n  - obsolete\n---\nbody\n",
                             encoding="utf-8")
        self.client = _build_app().test_client()
        self.cfg = {
            "vault_thesaurus_tags_path": "_tags.md",
            "tagging_archive_dir": str(self.archive),
        }

    def _inv(self):
        from audit.core.obsidian import NoteInfo
        from audit.engine.inventory import Record
        rec = Record(citation_key="smith2020")
        rec.pdf_paths = [self.biblio / "smith_2020.pdf"]
        rec.obsidian_note = NoteInfo(path=self.note, tags=["keep", "obsolete"],
                                     has_frontmatter=True)
        inv = mock.MagicMock()
        inv.records = {"smith2020": rec}
        settings = SimpleNamespace(vault_root=self.vault,
                                   biblio_articles_dir=self.biblio)
        return inv, settings

    def _patches(self):
        from audit.manager import audit_manager
        return (
            mock.patch("api.routes.tagging.load_config", return_value=self.cfg),
            mock.patch("tagging.writers.load_config", return_value=self.cfg),
            mock.patch("api.routes.tagging._vault_root", return_value=self.vault),
            mock.patch.object(audit_manager, "get_inventory",
                              return_value=self._inv()),
        )

    def _remove(self, body):
        with contextlib.ExitStack() as st:
            for p in self._patches():
                st.enter_context(p)
            return self.client.post("/api/tagging/remove-tags",
                                    json=body, headers=_HEADERS)

    def test_confirm_required(self):
        r = self._remove({"citation_key": "smith2020", "tags": ["obsolete"],
                          "targets": {"note": True, "finder": False, "zotero": False}})
        self.assertEqual(r.status_code, 400)

    def test_out_of_vocab_tag_is_removable_and_restorable(self):
        original = self.note.read_text(encoding="utf-8")
        r = self._remove({"citation_key": "smith2020", "tags": ["obsolete"],
                          "targets": {"note": True, "finder": False, "zotero": False},
                          "confirm": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        sub = r.get_json()["results"]["note"]
        self.assertTrue(sub["ok"], sub)
        self.assertEqual(sub["removed"], ["obsolete"])
        import yaml
        fm = yaml.safe_load(self.note.read_text(encoding="utf-8").split("---")[1])
        self.assertEqual(fm["tags"], ["keep"])
        # Journalled as note_untag; restore re-adds the tag byte-identically.
        with contextlib.ExitStack() as st:
            for p in self._patches():
                st.enter_context(p)
            m = self.client.get("/api/tagging/manifest", headers=_HEADERS).get_json()
            self.assertEqual(m["ops"][0]["kind"], "note_untag")
            rr = self.client.post("/api/tagging/restore",
                                  json={"op_id": sub["op_id"], "confirm": True},
                                  headers=_HEADERS)
        self.assertEqual(rr.get_json()["status"], "reverted")
        self.assertEqual(self.note.read_text(encoding="utf-8"), original)

    def test_absent_tag_is_a_noop_not_an_error(self):
        r = self._remove({"citation_key": "smith2020", "tags": ["neverthere"],
                          "targets": {"note": True, "finder": False, "zotero": False},
                          "confirm": True})
        self.assertEqual(r.status_code, 200)
        sub = r.get_json()["results"]["note"]
        self.assertTrue(sub["ok"])
        self.assertEqual(sub["removed"], [])

    def test_untag_kinds_survive_prune_cap(self):
        from tagging import journal
        # An applied finder_untag op must never be cap-evicted (its inline
        # before-lists are the only restore mapping — same carve-out as the
        # add-direction external ops).
        manifest = {"version": 1, "ops": []}
        for i in range(journal._MAX_OPS + 5):
            manifest["ops"].append({"op_id": f"op{i}", "kind": "finder_untag",
                                    "state": "applied", "pdfs": []})
        dropped = journal.prune(self.vault, self.cfg, manifest)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(manifest["ops"]), journal._MAX_OPS + 5)


class TestJournalGuards(unittest.TestCase):
    """2026-07-20 audit: absent-vs-unreadable manifest + cap-eviction carve-out."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tagging-journal-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.archive = self.tmp / "archive"
        (self.vault / "Z_Zotero_Notes").mkdir(parents=True, exist_ok=True)
        self.biblio = self.vault / "Z_attachments" / "biblio_articles"
        self.biblio.mkdir(parents=True, exist_ok=True)
        (self.vault / "_tags.md").write_text(_SAMPLE_TAGS_MD, encoding="utf-8")
        self.note = self.vault / "Z_Zotero_Notes" / "smith2020.md"
        self.note.write_text("---\ntags:\n  - existing\n---\nbody\n", encoding="utf-8")
        self.client = _build_app().test_client()
        self.cfg = {
            "vault_thesaurus_tags_path": "_tags.md",
            "tagging_archive_dir": str(self.archive),
        }

    def _inv(self):
        from audit.core.obsidian import NoteInfo
        from audit.engine.inventory import Record
        rec = Record(citation_key="smith2020")
        rec.pdf_paths = [self.biblio / "smith_2020.pdf"]
        rec.obsidian_note = NoteInfo(path=self.note, tags=["existing"],
                                     has_frontmatter=True)
        inv = mock.MagicMock()
        inv.records = {"smith2020": rec}
        settings = SimpleNamespace(vault_root=self.vault,
                                   biblio_articles_dir=self.biblio)
        return inv, settings

    def _patches(self):
        from audit.manager import audit_manager
        return (
            mock.patch("api.routes.tagging.load_config", return_value=self.cfg),
            mock.patch("tagging.writers.load_config", return_value=self.cfg),
            mock.patch("api.routes.tagging._vault_root", return_value=self.vault),
            mock.patch.object(audit_manager, "get_inventory",
                              return_value=self._inv()),
        )

    def test_absent_manifest_loads_fresh(self):
        from tagging import journal
        m = journal.load(self.vault, self.cfg)
        self.assertEqual(m["ops"], [])

    def test_corrupt_manifest_raises_and_write_returns_503(self):
        from tagging import journal
        p = journal.manifest_path(self.vault, self.cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        corrupt = "{this is not json"
        p.write_text(corrupt, encoding="utf-8")
        with self.assertRaises(journal.JournalUnreadableError):
            journal.load(self.vault, self.cfg)
        with contextlib.ExitStack() as st:
            for patch_ in self._patches():
                st.enter_context(patch_)
            r = self.client.post("/api/tagging/apply-note-tags",
                                 json={"citation_key": "smith2020",
                                       "tags": ["dep"], "confirm": True},
                                 headers=_HEADERS)
        self.assertEqual(r.status_code, 503, r.get_json())
        # The real manifest bytes were NOT overwritten…
        self.assertEqual(p.read_text(encoding="utf-8"), corrupt)
        # …and the note write never ran.
        self.assertNotIn("dep", self.note.read_text(encoding="utf-8"))

    def test_wrong_shape_manifest_raises(self):
        from tagging import journal
        p = journal.manifest_path(self.vault, self.cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"ops": "not-a-list"}', encoding="utf-8")
        with self.assertRaises(journal.JournalUnreadableError):
            journal.load(self.vault, self.cfg)

    def test_prune_cap_never_evicts_applied_external_ops(self):
        """Applied finder/zotero ops carry their before-lists INLINE (no
        snapshot file), so cap eviction would destroy the only restore
        mapping — they must survive; snapshot-backed note ops stay evictable."""
        from tagging import journal
        manifest = {"ops": [
            {"op_id": "1", "kind": "finder_tag", "state": "applied"},
            {"op_id": "2", "kind": "zotero_tag", "state": "applied"},
            {"op_id": "3", "kind": "note_tag", "state": "applied"},
            {"op_id": "4", "kind": "note_tag", "state": "applied"},
        ]}
        with mock.patch.object(journal, "_MAX_OPS", 1):
            dropped = journal.prune(self.vault, self.cfg, manifest)
        kept = [op["op_id"] for op in manifest["ops"]]
        self.assertIn("1", kept)
        self.assertIn("2", kept)
        self.assertEqual(dropped, 2)          # only the two note ops evicted
        # Phase 1 still reclaims a REVERTED external op (spent — its
        # before-lists have served their purpose).
        manifest2 = {"ops": [
            {"op_id": "5", "kind": "finder_tag", "state": "reverted"},
        ]}
        journal.prune(self.vault, self.cfg, manifest2)
        self.assertEqual(manifest2["ops"], [])


class TestFinderPartialTruth(unittest.TestCase):
    """2026-07-20 audit: a half-failed Finder write must not read as a clean
    success — ``added`` reports only tags that landed, per-PDF failures ride
    ``partial``/``failed`` and survive the apply-all projection."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tagging-partial-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.archive = self.tmp / "archive"
        self.biblio = self.vault / "Z_attachments" / "biblio_articles"
        self.biblio.mkdir(parents=True, exist_ok=True)
        self.pdf1 = self.biblio / "a.pdf"
        self.pdf2 = self.biblio / "b.pdf"
        self.pdf1.write_bytes(b"%PDF-1.4 a")
        self.pdf2.write_bytes(b"%PDF-1.4 b")
        self.cfg = {"tagging_archive_dir": str(self.archive)}

    def test_partial_failure_reports_only_landed_tags(self):
        from tagging import journal, writers

        rec = SimpleNamespace(pdf_paths=[self.pdf1, self.pdf2])

        def _write(path, tags):
            if path.endswith("b.pdf"):
                raise RuntimeError("xattr write failed")

        with mock.patch("api.routes._finder_tagwrite.read_finder_tags_full",
                        return_value=[]), \
             mock.patch("api.routes._finder_tagwrite.write_finder_tags",
                        side_effect=_write):
            result, status = writers._finder_write_locked(
                rec=rec, valid=["dep"], rejected=[], cfg=self.cfg,
                vault_root=self.vault, biblio_dir=self.biblio,
                citation_key="smith2020",
            )
        self.assertEqual(status, 200)
        self.assertTrue(result["partial"])
        self.assertEqual(result["added"], ["dep"])       # landed on a.pdf
        self.assertEqual(len(result["failed"]), 1)
        self.assertIn("b.pdf", result["failed"][0]["rel"])
        # The projection apply-all uses keeps the failure summary.
        projected = writers._target_result(result, status)
        self.assertTrue(projected["partial"])
        self.assertEqual(len(projected["failed"]), 1)
        # The journalled op's summary matches what actually landed.
        manifest = journal.load(self.vault, self.cfg)
        self.assertEqual(manifest["ops"][-1]["added"], ["dep"])

    def test_all_landed_has_no_partial_flag(self):
        from tagging import writers
        rec = SimpleNamespace(pdf_paths=[self.pdf1])
        with mock.patch("api.routes._finder_tagwrite.read_finder_tags_full",
                        return_value=[]), \
             mock.patch("api.routes._finder_tagwrite.write_finder_tags"):
            result, status = writers._finder_write_locked(
                rec=rec, valid=["dep"], rejected=[], cfg=self.cfg,
                vault_root=self.vault, biblio_dir=self.biblio,
                citation_key="smith2020",
            )
        self.assertEqual(status, 200)
        self.assertNotIn("partial", result)
        projected = writers._target_result(result, status)
        self.assertNotIn("partial", projected)


@unittest.skipUnless(sys.platform == "darwin", "Finder tags are macOS-only")
class TestFinderTagWrite(unittest.TestCase):
    """Slice 4: journalled PDF Finder-tag write + restore (real xattr on macOS)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tagging-finder-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.archive = self.tmp / "archive"
        self.biblio = self.vault / "Z_attachments" / "biblio_articles"
        self.biblio.mkdir(parents=True, exist_ok=True)
        self.pdf = self.biblio / "smith_2020.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 fake\n")
        self.client = _build_app().test_client()
        self.cfg = {
            "vault_thesaurus_tags_path": "_tags.md",
            "tagging_archive_dir": str(self.archive),
        }
        (self.vault / "_tags.md").write_text(_SAMPLE_TAGS_MD, encoding="utf-8")

    def _inv(self):
        from audit.engine.inventory import Record
        rec = Record(citation_key="smith2020")
        rec.pdf_paths = [self.pdf]
        rec.bib_entry = SimpleNamespace(title="t", year="2020", authors=["Smith"],
                                        keywords=set())
        inv = mock.MagicMock()
        inv.records = {"smith2020": rec}
        settings = SimpleNamespace(vault_root=self.vault, biblio_articles_dir=self.biblio)
        return inv, settings

    def _patches(self):
        from audit.manager import audit_manager
        # NOTE: indexes [0]/[1] are used directly by the manifest-read tests —
        # keep (route load_config, route _vault_root) first.
        return (
            mock.patch("api.routes.tagging.load_config", return_value=self.cfg),
            mock.patch("api.routes.tagging._vault_root", return_value=self.vault),
            mock.patch("api.routes.tagging._read_vocab",
                       return_value=({"dep": "Depression", "mood": "Mood"}, "_tags.md")),
            # The 2026-07-19 extraction: _prepare_tag_write resolves config +
            # vocabulary in tagging.writers — patch that namespace too.
            mock.patch("tagging.writers.load_config", return_value=self.cfg),
            mock.patch("tagging.writers._read_vocab",
                       return_value=({"dep": "Depression", "mood": "Mood"}, "_tags.md")),
            mock.patch.object(audit_manager, "get_inventory", return_value=self._inv()),
        )

    def _apply(self, body):
        with contextlib.ExitStack() as st:
            for p in self._patches():
                st.enter_context(p)
            return self.client.post("/api/tagging/apply-finder-tags",
                                    json=body, headers=_HEADERS)

    def test_finder_roundtrip_helper(self):
        from api.routes import _finder_tagwrite
        self.assertEqual(_finder_tagwrite.read_finder_tags(str(self.pdf)), [])
        _finder_tagwrite.write_finder_tags(str(self.pdf), ["dep", "mood"])
        self.assertEqual(sorted(_finder_tagwrite.read_finder_tags(str(self.pdf))),
                         ["dep", "mood"])

    def test_apply_writes_finder_tags_and_journals(self):
        r = self._apply({"citation_key": "smith2020",
                         "tags": ["dep", "notreal"], "confirm": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertEqual(d["added"], ["dep"])
        self.assertEqual(d["rejected"], ["notreal"])
        from api.routes import _finder_tagwrite
        self.assertIn("dep", _finder_tagwrite.read_finder_tags(str(self.pdf)))
        with self._patches()[0], self._patches()[1]:
            m = self.client.get("/api/tagging/manifest", headers=_HEADERS).get_json()
        self.assertEqual(m["ops"][0]["kind"], "finder_tag")

    def test_macos_gate_501(self):
        with mock.patch("api.routes._finder_tagwrite.is_macos", return_value=False):
            r = self._apply({"citation_key": "smith2020", "tags": ["dep"], "confirm": True})
        self.assertEqual(r.status_code, 501)

    def test_restore_reverts_finder_tags(self):
        r = self._apply({"citation_key": "smith2020", "tags": ["dep"], "confirm": True})
        op_id = r.get_json()["op_id"]
        from api.routes import _finder_tagwrite
        self.assertIn("dep", _finder_tagwrite.read_finder_tags(str(self.pdf)))
        with self._patches()[0], self._patches()[1]:
            rr = self.client.post("/api/tagging/restore",
                                  json={"op_id": op_id, "confirm": True}, headers=_HEADERS)
        self.assertEqual(rr.status_code, 200, rr.get_json())
        self.assertEqual(rr.get_json()["status"], "reverted")
        self.assertEqual(_finder_tagwrite.read_finder_tags(str(self.pdf)), [])

    def test_union_preserves_existing_finder_tags(self):
        from api.routes import _finder_tagwrite
        _finder_tagwrite.write_finder_tags(str(self.pdf), ["manual_keep"])
        self._apply({"citation_key": "smith2020", "tags": ["dep"], "confirm": True})
        tags = _finder_tagwrite.read_finder_tags(str(self.pdf))
        self.assertIn("manual_keep", tags)  # union, not replace
        self.assertIn("dep", tags)


class TestZoteroWriteUnit(unittest.TestCase):
    def test_library_path_routing(self):
        from tagging import zotero_write
        user = SimpleNamespace(library_type="user", group_id=None)
        group = SimpleNamespace(library_type="group", group_id=42)
        bare = SimpleNamespace()
        self.assertEqual(zotero_write.library_path(user), "users/0")
        self.assertEqual(zotero_write.library_path(group), "groups/42")
        self.assertEqual(zotero_write.library_path(bare), "users/0")

    def test_probe_true_on_http_error_false_on_urlerror(self):
        import urllib.error
        from tagging import zotero_write
        with mock.patch("tagging.zotero_write._urlopen",
                        side_effect=urllib.error.URLError("refused")):
            self.assertFalse(zotero_write.probe("http://127.0.0.1:23119"))
        with mock.patch("tagging.zotero_write._urlopen",
                        side_effect=urllib.error.HTTPError("u", 404, "nf", None, None)):
            self.assertTrue(zotero_write.probe("http://127.0.0.1:23119"))


class TestZoteroTransport(unittest.TestCase):
    """The local API is read-only before Zotero 10 (verified: 501 on PATCH),
    so the web transport is the working write path. Pin the selection rules."""

    def _no_key(self):
        return mock.patch.dict(os.environ, {"ZOTERO_API_KEY": ""}, clear=False)

    def _with_key(self, key="k" * 24):
        return mock.patch.dict(os.environ, {"ZOTERO_API_KEY": key}, clear=False)

    def test_auto_prefers_web_when_key_present(self):
        from tagging import zotero_write
        with self._with_key():
            t = zotero_write.resolve_transport({"tagging_zotero_transport": "auto"})
        self.assertEqual(t.kind, "web")
        self.assertTrue(t.base_url.startswith("https://api.zotero.org"))
        self.assertTrue(t.can_write)

    def test_auto_falls_back_to_local_without_key(self):
        from tagging import zotero_write
        with self._no_key():
            t = zotero_write.resolve_transport({"tagging_zotero_transport": "auto"})
        self.assertEqual(t.kind, "local")
        self.assertIsNone(t.api_key)

    def test_explicit_local_ignores_key(self):
        """Pinning 'local' must not silently route to the web API — it is the
        escape hatch for a user who does not want their key used."""
        from tagging import zotero_write
        with self._with_key():
            t = zotero_write.resolve_transport({"tagging_zotero_transport": "local"})
        self.assertEqual(t.kind, "local")
        self.assertIsNone(t.api_key)

    def test_web_without_key_cannot_write(self):
        from tagging import zotero_write
        with self._no_key():
            t = zotero_write.resolve_transport({"tagging_zotero_transport": "web"})
        self.assertEqual(t.kind, "web")
        self.assertFalse(t.can_write)

    def test_transport_repr_never_leaks_the_key(self):
        from tagging import zotero_write
        with self._with_key("SECRETKEY" + "x" * 15):
            t = zotero_write.resolve_transport({"tagging_zotero_transport": "web"})
        self.assertNotIn("SECRETKEY", repr(t))

    def test_library_path_uses_real_user_id_on_web(self):
        """api.zotero.org rejects the local API's magic users/0."""
        from tagging import zotero_write
        user = SimpleNamespace(library_type="user", group_id=None)
        self.assertEqual(zotero_write.library_path(user, user_id=7666987),
                         "users/7666987")
        group = SimpleNamespace(library_type="group", group_id=42)
        self.assertEqual(zotero_write.library_path(group, user_id=7666987),
                         "groups/42")

    def test_web_base_must_be_https(self):
        """E2: the key-bearing web base is https-validated at call time — a
        hand-edited http/file base is refused rather than sent the API key."""
        from tagging import zotero_write
        with self._with_key():
            for bad in ("http://attacker.example", "file:///etc/passwd",
                        "ftp://x"):
                with self.assertRaises(zotero_write.ZoteroError):
                    zotero_write.resolve_transport({
                        "tagging_zotero_transport": "web",
                        "tagging_zotero_web_api_base": bad,
                    })
            # https and the empty default are accepted.
            t = zotero_write.resolve_transport({
                "tagging_zotero_transport": "web",
                "tagging_zotero_web_api_base": "https://api.zotero.org",
            })
            self.assertEqual(t.base_url, "https://api.zotero.org")

    def test_redirects_are_refused_not_followed(self):
        """E1: a 30x from a Zotero endpoint must NOT be followed — stdlib would
        forward the Zotero-API-Key header to the redirect target."""
        import urllib.error
        from tagging import zotero_write
        # Drive a real request through the no-redirect opener; a 302 handler
        # raises HTTPError instead of chasing the Location.
        req = urllib.request.Request("https://api.zotero.org/api/users/1/items/K")
        redirect = urllib.error.HTTPError(
            req.full_url, 302, "Found",
            {"Location": "http://attacker.example/steal"}, None)
        with mock.patch.object(zotero_write._opener, "open", side_effect=redirect):
            # The opener itself raises (we assert the module never silently
            # follows) — get_item_tags maps it to ZoteroError, not a success.
            with self.assertRaises(zotero_write.ZoteroError):
                zotero_write.get_item_tags_full(
                    "https://api.zotero.org", "users/1", "K", api_key="k")

    def test_no_redirect_handler_raises_on_redirect(self):
        """The redirect handler fails closed for any 30x code."""
        import urllib.error
        from tagging import zotero_write
        handler = zotero_write._NoRedirect()
        req = urllib.request.Request("https://api.zotero.org/api/x")
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(
                req, None, 301, "Moved", {}, "http://evil.example/")

    def test_set_item_tags_sends_api_key_header(self):
        from tagging import zotero_write
        captured = {}

        def _fake(req, timeout=None):
            captured["headers"] = dict(req.headers)
            captured["method"] = req.get_method()
            return _FakeResp({"Last-Modified-Version": "99"})

        with mock.patch("tagging.zotero_write._urlopen", _fake):
            v = zotero_write.set_item_tags(
                "https://api.zotero.org", "users/1", "K", ["dep"], 98, api_key="abc123")
        self.assertEqual(v, 99)
        self.assertEqual(captured["method"], "PATCH")
        # urllib title-cases header names.
        self.assertEqual(captured["headers"].get("Zotero-api-key"), "abc123")
        self.assertEqual(captured["headers"].get("If-unmodified-since-version"), "98")

    def test_local_501_becomes_write_unsupported(self):
        """The documented Zotero-9 blocker gets its own exception so the UI can
        say 'configure a key' instead of an opaque 502."""
        import urllib.error
        from tagging import zotero_write
        with mock.patch("tagging.zotero_write._urlopen",
                        side_effect=urllib.error.HTTPError("u", 501, "nope", {}, None)):
            with self.assertRaises(zotero_write.ZoteroWriteUnsupported) as cm:
                zotero_write.set_item_tags(
                    "http://127.0.0.1:23119", "users/0", "K", ["dep"], 1)
        self.assertIsInstance(cm.exception, zotero_write.ZoteroError)  # stays a 502-class

    def test_rate_limit_captures_retry_after(self):
        import email.message
        import urllib.error
        from tagging import zotero_write
        hdrs = email.message.Message()
        hdrs["Retry-After"] = "7"
        with mock.patch("tagging.zotero_write._urlopen",
                        side_effect=urllib.error.HTTPError("u", 429, "slow", hdrs, None)):
            with self.assertRaises(zotero_write.ZoteroRateLimited) as cm:
                zotero_write.set_item_tags(
                    "https://api.zotero.org", "users/1", "K", ["dep"], 1, api_key="k")
        self.assertEqual(cm.exception.retry_after_s, 7.0)

    def test_rate_limit_arms_the_read_backoff(self):
        """P5.1 (2026-07-30): the 429 itself must arm the shared backoff.

        Only successful responses' `Backoff` header used to arm it, so the one
        signal that actually means "you are being throttled" was captured onto
        the exception and dropped — the next read re-hit the same 429 at once.
        """
        import email.message
        import time as _time
        import urllib.error
        from tagging import zotero_write

        zotero_write._backoff_until = 0.0
        self.addCleanup(setattr, zotero_write, "_backoff_until", 0.0)
        hdrs = email.message.Message()
        hdrs["Retry-After"] = "9"
        with mock.patch("tagging.zotero_write._urlopen",
                        side_effect=urllib.error.HTTPError("u", 429, "slow", hdrs, None)):
            with self.assertRaises(zotero_write.ZoteroRateLimited):
                zotero_write.get_item_tags_full(
                    "https://api.zotero.org", "users/1", "K", api_key="k")
        remaining = zotero_write._backoff_until - _time.monotonic()
        self.assertGreater(remaining, 0.0)
        self.assertLessEqual(remaining, 9.0)

    def test_backoff_wait_stays_bounded(self):
        """A hostile/huge Retry-After must not park a read for minutes."""
        from tagging import zotero_write
        zotero_write._backoff_until = 0.0
        self.addCleanup(setattr, zotero_write, "_backoff_until", 0.0)
        zotero_write._arm_backoff(3600)
        slept = []
        with mock.patch("tagging.zotero_write.time.sleep", slept.append):
            zotero_write._await_backoff()
        self.assertTrue(slept)
        self.assertLessEqual(slept[0], zotero_write._MAX_BACKOFF_WAIT_S)

    def test_user_id_cache_expires_so_a_revoked_key_stops_looking_available(self):
        """P5.2 (2026-07-30): the cache doubles as "this key authenticates",
        so an unbounded entry let a REVOKED key report the Zotero target as
        available until the app restarted."""
        from tagging import zotero_write

        zotero_write._user_id_cache.clear()
        self.addCleanup(zotero_write._user_id_cache.clear)
        calls = []

        def _fake(req, timeout=None):
            calls.append(req.full_url)
            return _FakeResp({}, payload={"userID": 42, "access": {"user": {"write": True}}})

        with mock.patch("tagging.zotero_write._urlopen", _fake):
            self.assertEqual(
                zotero_write.resolve_user_id("https://api.zotero.org", "k"), 42)
            # Second call inside the TTL is served from cache — no network.
            self.assertEqual(
                zotero_write.resolve_user_id("https://api.zotero.org", "k"), 42)
            self.assertEqual(len(calls), 1)
            # Past the TTL it revalidates.
            with mock.patch("tagging.zotero_write._USER_ID_TTL_S", -1):
                zotero_write._user_id_cache.clear()
                zotero_write.resolve_user_id("https://api.zotero.org", "k")
                zotero_write.resolve_user_id("https://api.zotero.org", "k")
        self.assertEqual(len(calls), 3)

    def test_user_id_cache_still_keys_on_the_key_fingerprint(self):
        """A ROTATED key must not reuse the old identity (pre-existing
        contract the TTL must not weaken)."""
        from tagging import zotero_write
        zotero_write._user_id_cache.clear()
        self.addCleanup(zotero_write._user_id_cache.clear)
        seen = []

        def _fake(req, timeout=None):
            seen.append(req.headers.get("Zotero-api-key"))
            return _FakeResp({}, payload={"userID": 7, "access": {"user": {"write": True}}})

        with mock.patch("tagging.zotero_write._urlopen", _fake):
            zotero_write.resolve_user_id("https://api.zotero.org", "old-key")
            zotero_write.resolve_user_id("https://api.zotero.org", "new-key")
        self.assertEqual(seen, ["old-key", "new-key"])

    def test_auth_failure_is_not_retryable_category(self):
        import urllib.error
        from tagging import zotero_write
        with mock.patch("tagging.zotero_write._urlopen",
                        side_effect=urllib.error.HTTPError("u", 403, "forbidden", {}, None)):
            with self.assertRaises(zotero_write.ZoteroAuthError):
                zotero_write.get_item_tags_full(
                    "https://api.zotero.org", "users/1", "K", api_key="k")

    def test_web_probe_false_without_working_key(self):
        """Reachable-but-unauthenticated is NOT available: clicking the write
        button would be guaranteed to fail."""
        import urllib.error
        from tagging import zotero_write
        with mock.patch("tagging.zotero_write._urlopen",
                        side_effect=urllib.error.HTTPError("u", 403, "forbidden", {}, None)):
            self.assertFalse(zotero_write.probe(
                "https://api.zotero.org", timeout=1.0, api_key="bad"))


class _FakeResp:
    """Minimal urlopen context-manager stand-in."""

    def __init__(self, headers=None, payload=None):
        self.headers = headers or {}
        self._payload = payload

    def read(self):
        return json.dumps(self._payload or {}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


class TestZoteroEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tagging-zot-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.archive = self.tmp / "archive"
        self.vault.mkdir(parents=True, exist_ok=True)
        self.client = _build_app().test_client()
        self.cfg = {
            "vault_thesaurus_tags_path": "_tags.md",
            "tagging_archive_dir": str(self.archive),
            "tagging_zotero_api_base": "http://127.0.0.1:23119",
        }

    def _inv(self, with_zot=True):
        from audit.engine.inventory import Record
        rec = Record(citation_key="smith2020")
        if with_zot:
            rec.zotero_item = SimpleNamespace(
                item_key="ZK1", library_type="user", group_id=None,
                tags=["existing"], child_notes=[])
        inv = mock.MagicMock()
        inv.records = {"smith2020": rec}
        settings = SimpleNamespace(vault_root=self.vault,
                                   biblio_articles_dir=self.vault / "b")
        return inv, settings

    def _patches(self, with_zot=True):
        from audit.manager import audit_manager
        return [
            mock.patch("api.routes.tagging.load_config", return_value=self.cfg),
            mock.patch("api.routes.tagging._vault_root", return_value=self.vault),
            mock.patch("api.routes.tagging._read_vocab",
                       return_value=({"dep": "Depression"}, "_tags.md")),
            # 2026-07-19 extraction: _prepare_tag_write + _zotero_* live in
            # tagging.writers — patch its config/vocab lookups too.
            mock.patch("tagging.writers.load_config", return_value=self.cfg),
            mock.patch("tagging.writers._read_vocab",
                       return_value=({"dep": "Depression"}, "_tags.md")),
            mock.patch.object(audit_manager, "get_inventory",
                              return_value=self._inv(with_zot)),
        ]

    def _post(self, path, body, extra=()):
        ps = self._patches() + list(extra)
        for p in ps:
            p.start()
            self.addCleanup(p.stop)
        return self.client.post(path, json=body, headers=_HEADERS)

    def test_zotero_read_runs_before_the_op_lock(self):
        """2026-07-19 restructure: the GET must not run under the op-lock.

        Pre-fix, both the read and the PATCH (each a 15 s-timeout HTTP call to
        local Zotero) ran while holding obsidian_manager's op-lock, so a
        wedged Zotero stalled all indexing and every other tag write. The
        version-guarded PATCH makes the early read safe.
        """
        from rag.vault import obsidian_manager
        order = []
        real_acquire = obsidian_manager.try_acquire_lock

        def _spy_read(*a, **kw):
            order.append("read")
            return 10, [{"tag": "existing"}]

        def _spy_acquire(*a, **kw):
            order.append("lock")
            return real_acquire(*a, **kw)

        gp = mock.patch("api.routes.tagging.zotero_write.get_item_tags_full",
                        side_effect=_spy_read)
        sp = mock.patch("api.routes.tagging.zotero_write.set_item_tags", return_value=11)
        lp = mock.patch.object(obsidian_manager, "try_acquire_lock",
                               side_effect=_spy_acquire)
        r = self._post("/api/tagging/apply-zotero-tags",
                       {"citation_key": "smith2020", "tags": ["dep"], "confirm": True},
                       extra=[gp, sp, lp])
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(order, ["read", "lock"])

    def test_web_write_never_journals_the_api_key(self):
        """The restore manifest is a plain JSON file in the archive dir — a key
        landing there would outlive the process and defeat the env-only rule."""
        from tagging import journal
        key = "ZKEYSECRET" + "z" * 14
        gp = mock.patch("api.routes.tagging.zotero_write.get_item_tags_full",
                        return_value=(10, [{"tag": "existing"}]))
        sp = mock.patch("api.routes.tagging.zotero_write.set_item_tags", return_value=11)
        uid = mock.patch("tagging.zotero_write.resolve_user_id", return_value=7666987)
        env = mock.patch.dict(os.environ, {"ZOTERO_API_KEY": key}, clear=False)
        r = self._post("/api/tagging/apply-zotero-tags",
                       {"citation_key": "smith2020", "tags": ["dep"], "confirm": True},
                       extra=[gp, sp, uid, env])
        self.assertEqual(r.status_code, 200, r.get_json())
        manifest = journal.load(self.vault, self.cfg)
        ops = [o for o in manifest["ops"] if o.get("kind") == "zotero_tag"]
        self.assertTrue(ops)
        blob = json.dumps(manifest)
        self.assertNotIn(key, blob)
        # It DOES record the transport, so the revert knows to re-read a key.
        self.assertEqual(ops[-1].get("transport"), "web")
        # ...and the real userID library path, not the local magic users/0.
        self.assertEqual(ops[-1].get("lib_path"), "users/7666987")

    def test_zotero_unreachable_returns_503_without_taking_the_lock(self):
        from tagging import zotero_write as zw
        from rag.vault import obsidian_manager
        gp = mock.patch("api.routes.tagging.zotero_write.get_item_tags_full",
                        side_effect=zw.ZoteroUnavailable("closed"))
        lp = mock.patch.object(obsidian_manager, "try_acquire_lock")
        r = self._post("/api/tagging/apply-zotero-tags",
                       {"citation_key": "smith2020", "tags": ["dep"], "confirm": True},
                       extra=[gp, lp])
        self.assertEqual(r.status_code, 503)
        # An early exit never even touches the lock.
        obsidian_manager.try_acquire_lock.assert_not_called()

    def test_apply_unions_and_journals(self):
        # The apply path reads the FULL tag dicts (metadata round-trip) — the
        # existing tag carries `type: 1` (automatic) that must survive the PATCH.
        set_calls = []

        def _capture_set(base, lib, key, tags, version, **kw):
            set_calls.append(tags)
            return 11

        gp = mock.patch("api.routes.tagging.zotero_write.get_item_tags_full",
                        return_value=(10, [{"tag": "existing", "type": 1}]))
        sp = mock.patch("api.routes.tagging.zotero_write.set_item_tags",
                        side_effect=_capture_set)
        r = self._post("/api/tagging/apply-zotero-tags",
                       {"citation_key": "smith2020", "tags": ["dep", "notreal"],
                        "confirm": True}, extra=[gp, sp])
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertEqual(d["added"], ["dep"])
        self.assertEqual(d["rejected"], ["notreal"])
        # The PATCH payload keeps the existing tag's dict VERBATIM (metadata
        # preserved) and appends the new tag as a bare name (set_item_tags
        # wraps names itself).
        self.assertEqual(set_calls, [[{"tag": "existing", "type": 1}, "dep"]])
        with mock.patch("api.routes.tagging.load_config", return_value=self.cfg), \
             mock.patch("api.routes.tagging._vault_root", return_value=self.vault):
            m = self.client.get("/api/tagging/manifest", headers=_HEADERS).get_json()
        self.assertEqual(m["ops"][0]["kind"], "zotero_tag")

    def test_unreachable_503(self):
        from tagging import zotero_write
        # get_item_tags_full is what the apply path actually calls (2026-07-18
        # metadata-preserving read); patching the old name-only get_item_tags
        # let the REAL full-read dial a live Zotero and fail 502 on machines
        # where Zotero is running.
        gp = mock.patch("api.routes.tagging.zotero_write.get_item_tags_full",
                        side_effect=zotero_write.ZoteroUnavailable("down"))
        r = self._post("/api/tagging/apply-zotero-tags",
                       {"citation_key": "smith2020", "tags": ["dep"], "confirm": True},
                       extra=[gp])
        self.assertEqual(r.status_code, 503)

    def test_version_conflict_409(self):
        from tagging import zotero_write
        gp = mock.patch("api.routes.tagging.zotero_write.get_item_tags_full",
                        return_value=(10, [{"tag": "existing"}]))
        sp = mock.patch("api.routes.tagging.zotero_write.set_item_tags",
                        side_effect=zotero_write.ZoteroVersionConflict("412"))
        r = self._post("/api/tagging/apply-zotero-tags",
                       {"citation_key": "smith2020", "tags": ["dep"], "confirm": True},
                       extra=[gp, sp])
        self.assertEqual(r.status_code, 409)

    def test_404_when_no_zotero_item(self):
        from audit.manager import audit_manager
        for p in [mock.patch("api.routes.tagging.load_config", return_value=self.cfg),
                  mock.patch("tagging.writers.load_config", return_value=self.cfg),
                  mock.patch("tagging.writers._read_vocab",
                             return_value=({"dep": "d"}, "_tags.md")),
                  mock.patch.object(audit_manager, "get_inventory",
                                    return_value=self._inv(with_zot=False))]:
            p.start()
            self.addCleanup(p.stop)
        r = self.client.post("/api/tagging/apply-zotero-tags",
                             json={"citation_key": "smith2020", "tags": ["dep"],
                                   "confirm": True}, headers=_HEADERS)
        self.assertEqual(r.status_code, 404)

    def test_restore_reverts_zotero(self):
        gp = mock.patch("api.routes.tagging.zotero_write.get_item_tags_full",
                        return_value=(10, [{"tag": "existing", "type": 1}]))
        sp = mock.patch("api.routes.tagging.zotero_write.set_item_tags", return_value=11)
        r = self._post("/api/tagging/apply-zotero-tags",
                       {"citation_key": "smith2020", "tags": ["dep"], "confirm": True},
                       extra=[gp, sp])
        op_id = r.get_json()["op_id"]
        # Restore: item still at 'after' (existing, dep) -> revert to 'before'.
        # The revert compares NAMES (get_item_tags) but PATCHes the op's
        # before_raw dicts so the automatic-tag metadata comes back too.
        with mock.patch("api.routes.tagging.load_config", return_value=self.cfg), \
             mock.patch("api.routes.tagging._vault_root", return_value=self.vault), \
             mock.patch("api.routes.tagging.zotero_write.get_item_tags",
                        return_value=(11, ["existing", "dep"])), \
             mock.patch("api.routes.tagging.zotero_write.set_item_tags",
                        return_value=12) as sp2:
            rr = self.client.post("/api/tagging/restore",
                                  json={"op_id": op_id, "confirm": True}, headers=_HEADERS)
        self.assertEqual(rr.status_code, 200, rr.get_json())
        self.assertEqual(rr.get_json()["status"], "reverted")
        # Reverted to the prior tag set — as raw dicts, metadata intact.
        self.assertEqual(sp2.call_args[0][3], [{"tag": "existing", "type": 1}])


class TestApplyAll(unittest.TestCase):
    """Fan-out /api/tagging/apply-all: one decision → note (+ skips) under one lock."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tagging-all-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.archive = self.tmp / "archive"
        (self.vault / "Z_Zotero_Notes").mkdir(parents=True, exist_ok=True)
        self.biblio = self.vault / "Z_attachments" / "biblio_articles"
        self.biblio.mkdir(parents=True, exist_ok=True)
        (self.vault / "_tags.md").write_text(_SAMPLE_TAGS_MD, encoding="utf-8")
        self.note = self.vault / "Z_Zotero_Notes" / "smith2020.md"
        self.note.write_text("---\ntags:\n  - existing\n---\nbody\n", encoding="utf-8")
        self.pdf = self.biblio / "smith_2020.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 fake\n")
        self.client = _build_app().test_client()
        self.cfg = {"vault_thesaurus_tags_path": "_tags.md",
                    "tagging_archive_dir": str(self.archive)}

    def _inv(self):
        from audit.core.obsidian import NoteInfo
        from audit.engine.inventory import Record
        rec = Record(citation_key="smith2020")
        rec.pdf_paths = [self.pdf]
        rec.obsidian_note = NoteInfo(path=self.note, tags=["existing"], has_frontmatter=True)
        rec.zotero_item = None  # → zotero target skipped "no_item"
        inv = mock.MagicMock()
        inv.records = {"smith2020": rec}
        settings = SimpleNamespace(vault_root=self.vault, biblio_articles_dir=self.biblio)
        return inv, settings

    def _patches(self):
        from audit.manager import audit_manager
        return (
            mock.patch("api.routes.tagging.load_config", return_value=self.cfg),
            # 2026-07-19 extraction: _prepare_tag_write's own config lookup.
            mock.patch("tagging.writers.load_config", return_value=self.cfg),
            mock.patch("api.routes.tagging._vault_root", return_value=self.vault),
            mock.patch.object(audit_manager, "get_inventory", return_value=self._inv()),
        )

    def _post(self, body):
        with contextlib.ExitStack() as st:
            for p in self._patches():
                st.enter_context(p)
            return self.client.post("/api/tagging/apply-all", json=body, headers=_HEADERS)

    def test_confirm_required(self):
        r = self._post({"citation_key": "smith2020", "tags": ["dep"], "targets": {"note": True}})
        self.assertEqual(r.status_code, 400)

    def test_all_out_of_vocab_400(self):
        r = self._post({"citation_key": "smith2020", "tags": ["nope"], "confirm": True})
        self.assertEqual(r.status_code, 400)

    def test_fans_out_note_and_skips_absent_targets(self):
        # Finder off (platform-independent), Zotero on but absent → skipped.
        r = self._post({"citation_key": "smith2020", "tags": ["dep", "notreal"],
                        "targets": {"note": True, "finder": False, "zotero": True},
                        "confirm": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        res = r.get_json()["results"]
        self.assertTrue(res["note"]["ok"])
        self.assertEqual(res["note"]["added"], ["dep"])
        self.assertEqual(res["note"]["rejected"], ["notreal"])  # vocab gate still runs
        self.assertTrue(res["note"]["op_id"])
        self.assertEqual(res["zotero"]["skipped"], "no_item")
        self.assertNotIn("finder", res)  # target turned off
        # The note was actually written, and a note_tag op journalled.
        import yaml
        fm = yaml.safe_load(self.note.read_text(encoding="utf-8").split("---")[1])
        self.assertEqual(fm["tags"], ["existing", "dep"])
        with contextlib.ExitStack() as st:
            for p in self._patches():
                st.enter_context(p)
            m = self.client.get("/api/tagging/manifest", headers=_HEADERS).get_json()
        self.assertEqual(m["ops"][0]["kind"], "note_tag")

    def test_default_targets_all_on(self):
        # No `targets` key ⇒ every target on; note lands, zotero skips (no item).
        r = self._post({"citation_key": "smith2020", "tags": ["dep"], "confirm": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        res = r.get_json()["results"]
        self.assertTrue(res["note"]["ok"])
        self.assertIn("skipped", res["zotero"])  # no zotero item present
        self.assertIn("finder", res)  # attempted (writes on macOS, skips elsewhere)

    @unittest.skipUnless(sys.platform == "darwin", "Finder tags are macOS-only")
    def test_apply_all_loads_the_manifest_once_for_the_whole_fanout(self):
        # 2026-07-19: each helper used to load→prune→save its own copy of the
        # manifest — three full rewrite cycles per Accept. apply-all now loads
        # once and passes the shared manifest; each helper still appends+saves
        # (crash-trail) and both ops land with distinct ids.
        from tagging import journal as journal_mod
        real_load = journal_mod.load
        with mock.patch("api.routes.tagging.journal.load",
                        side_effect=real_load) as load_spy:
            r = self._post({"citation_key": "smith2020", "tags": ["dep"],
                            "targets": {"note": True, "finder": True, "zotero": False},
                            "confirm": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        res = r.get_json()["results"]
        self.assertTrue(res["note"]["ok"])
        self.assertTrue(res["finder"]["ok"])
        self.assertEqual(load_spy.call_count, 1)
        manifest = real_load(self.vault, self.cfg)
        kinds = sorted(op["kind"] for op in manifest["ops"])
        self.assertEqual(kinds, ["finder_tag", "note_tag"])
        op_ids = {op["op_id"] for op in manifest["ops"]}
        self.assertEqual(len(op_ids), 2)  # shared manifest, distinct ids

    def test_late_target_crash_does_not_hide_landed_note_write(self):
        """A later target's unexpected crash must stay per-target (2026-07-19).

        Pre-fix, one try wrapped all three targets: a Zotero-stage TypeError
        after the note write had committed + journalled returned HTTP 500, so
        the client reported total failure and never learned the note op's id —
        while the write had permanently landed.
        """
        inv, settings = self._inv()
        inv.records["smith2020"].zotero_item = SimpleNamespace(
            item_key="K1", library_type="user", group_id=None)
        from audit.manager import audit_manager
        with mock.patch("api.routes.tagging.load_config", return_value=self.cfg), \
             mock.patch("tagging.writers.load_config", return_value=self.cfg), \
             mock.patch("api.routes.tagging._vault_root", return_value=self.vault), \
             mock.patch.object(audit_manager, "get_inventory", return_value=(inv, settings)), \
             mock.patch("api.routes.tagging._zotero_prepare",
                        side_effect=TypeError("boom")):
            r = self.client.post(
                "/api/tagging/apply-all",
                json={"citation_key": "smith2020", "tags": ["dep"],
                      "targets": {"note": True, "finder": False, "zotero": True},
                      "confirm": True},
                headers=_HEADERS)
        self.assertEqual(r.status_code, 200, r.get_json())
        res = r.get_json()["results"]
        self.assertTrue(res["note"]["ok"])           # the landed write is reported
        self.assertTrue(res["note"]["op_id"])        # undo handle reaches the client
        self.assertFalse(res["zotero"]["ok"])        # the crash stays in its target
        self.assertIn("error", res["zotero"])
        # The note write really landed and is journalled.
        self.assertIn("dep", self.note.read_text(encoding="utf-8"))


class TestSnippetEndpoint(unittest.TestCase):
    """/api/tagging/snippet — bounded, cache-first PDF-text excerpt for the deck card."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tagging-snip-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.biblio = self.vault / "Z_attachments" / "biblio_articles"
        self.biblio.mkdir(parents=True, exist_ok=True)
        self.pdf = self.biblio / "smith_2020.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 fake\n")
        self.client = _build_app().test_client()
        self.cfg = {"vault_thesaurus_tags_path": "_tags.md"}

    def _inv(self, with_pdf=True):
        from audit.engine.inventory import Record
        rec = Record(citation_key="smith2020")
        rec.pdf_paths = [self.pdf] if with_pdf else []
        inv = mock.MagicMock()
        inv.records = {"smith2020": rec}
        settings = SimpleNamespace(vault_root=self.vault, biblio_articles_dir=self.biblio)
        return inv, settings

    def _post(self, body, with_pdf=True):
        from audit.manager import audit_manager
        with mock.patch("api.routes.tagging.load_config", return_value=self.cfg), \
             mock.patch.object(audit_manager, "get_inventory", return_value=self._inv(with_pdf)):
            return self.client.post("/api/tagging/snippet", json=body, headers=_HEADERS)

    def test_returns_snippet(self):
        with mock.patch("api.routes.tagging.text_source.read_pdf_text",
                        return_value=("Hello world excerpt", False)):
            r = self._post({"citation_key": "smith2020"})
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertEqual(d["snippet"], "Hello world excerpt")
        self.assertGreater(d["chars"], 0)

    def test_no_biblio_pdf_empty(self):
        r = self._post({"citation_key": "smith2020"}, with_pdf=False)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["snippet"], "")
        self.assertEqual(r.get_json()["chars"], 0)

    def test_unknown_key_404(self):
        r = self._post({"citation_key": "nope"})
        self.assertEqual(r.status_code, 404)


class TestPdfEndpoints(unittest.TestCase):
    """/api/tagging/pdf + open-pdf — inventory-derived, realpath-re-checked."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tagging-pdf-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.biblio = self.vault / "Z_attachments" / "biblio_articles"
        self.biblio.mkdir(parents=True, exist_ok=True)
        self.pdf = self.biblio / "smith_2020.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 fake body\n")
        self.client = _build_app().test_client()

    def _inv(self, pdf_paths=None):
        from audit.engine.inventory import Record
        rec = Record(citation_key="smith2020")
        rec.pdf_paths = list(pdf_paths) if pdf_paths is not None else [self.pdf]
        inv = mock.MagicMock()
        inv.records = {"smith2020": rec}
        settings = SimpleNamespace(vault_root=self.vault, biblio_articles_dir=self.biblio)
        return inv, settings

    def _get(self, key, pdf_paths=None):
        from audit.manager import audit_manager
        with mock.patch.object(audit_manager, "get_inventory",
                               return_value=self._inv(pdf_paths)):
            return self.client.get(f"/api/tagging/pdf?citation_key={key}",
                                   headers=_HEADERS)

    def test_serves_pdf_bytes(self):
        r = self._get("smith2020")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/pdf")
        self.assertTrue(r.data.startswith(b"%PDF-1.4"))

    def test_unknown_key_404(self):
        self.assertEqual(self._get("nope").status_code, 404)

    def test_no_biblio_pdf_404(self):
        self.assertEqual(self._get("smith2020", pdf_paths=[]).status_code, 404)

    def test_symlink_escaping_biblio_dir_404(self):
        # The inventory path is inside biblio_articles/ but its realpath
        # escapes it: the independent realpath re-check must refuse.
        outside = self.tmp / "outside.pdf"
        outside.write_bytes(b"%PDF-1.4 outside\n")
        link = self.biblio / "sneaky.pdf"
        link.symlink_to(outside)
        self.assertEqual(self._get("smith2020", pdf_paths=[link]).status_code, 404)

    def test_open_pdf_requires_valid_key(self):
        from audit.manager import audit_manager
        with mock.patch.object(audit_manager, "get_inventory",
                               return_value=self._inv()), \
             mock.patch("api.routes.tagging.subprocess.Popen") as popen:
            r = self.client.post("/api/tagging/open-pdf",
                                 json={"citation_key": "smith2020"}, headers=_HEADERS)
        if sys.platform == "darwin":
            self.assertEqual(r.status_code, 200)
            popen.assert_called_once()
            self.assertEqual(popen.call_args.args[0][0], "open")
        else:
            self.assertEqual(r.status_code, 501)
            popen.assert_not_called()


class TestProposeGateLeak(unittest.TestCase):
    """The single-flight gate must be released if the worker thread never starts."""

    def test_gate_released_when_thread_start_raises(self):
        from api.routes import tagging as tagging_mod
        with mock.patch.object(tagging_mod.threading.Thread, "start",
                               side_effect=RuntimeError("can't start new thread")):
            with self.assertRaises(RuntimeError):
                tagging_mod._run_propose_bounded(lambda c: [])
        # The gate must be free again: a normal run should NOT report busy.
        result, timed_out, busy = tagging_mod._run_propose_bounded(lambda c: ["ok"])
        self.assertFalse(busy)
        self.assertFalse(timed_out)
        self.assertEqual(result, ["ok"])


@unittest.skipUnless(sys.platform == "darwin", "Finder tags are macOS-only")
class TestFinderRevertConservatism(unittest.TestCase):
    """An all-skipped finder revert must keep the op 'applied' (retryable)."""

    def test_all_skipped_keeps_op_applied(self):
        from api.routes.tagging import _revert_finder_tag
        # Current xattr state won't match 'after' (file doesn't exist → tags []),
        # so every PDF skips. The op must NOT flip to reverted — flipping would
        # let journal.prune reclaim the only restore mapping without a restore.
        op = {"op_id": "x", "kind": "finder_tag", "state": "applied",
              "pdfs": [{"abs": "/nonexistent/fake.pdf", "rel": "fake.pdf",
                        "before": [], "after": ["dep"]}]}
        result = _revert_finder_tag(Path("/tmp"), {}, op)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(op["state"], "applied")


class TestFinderRevertFailureStates(unittest.TestCase):
    """A real I/O failure is a FAILURE, not a skip (2026-07-19).

    Pre-fix, a one-of-two revert with one OSError flipped the op to
    ``reverted`` — journal.prune then reclaimed the before-lists that were the
    failed PDF's ONLY restore mapping. Any failure must keep the op
    ``applied`` (retryable); retry is idempotent because a PDF already
    reverted no longer matches ``after`` and skips.
    """

    def _op(self):
        return {"op_id": "x", "kind": "finder_tag", "state": "applied",
                "pdfs": [
                    {"abs": "/v/a.pdf", "rel": "a.pdf",
                     "before": ["old"], "after": ["dep", "old"]},
                    {"abs": "/v/b.pdf", "rel": "b.pdf",
                     "before": [], "after": ["dep"]},
                ]}

    def test_partial_failure_keeps_op_applied_then_retry_completes(self):
        from api.routes import tagging as tagging_mod
        op = self._op()
        reads = {"/v/a.pdf": ["dep", "old"], "/v/b.pdf": ["dep"]}
        with mock.patch("api.routes._finder_tagwrite.is_macos", return_value=True), \
             mock.patch("tagging.writers.os.path.exists", return_value=True), \
             mock.patch("api.routes._finder_tagwrite.read_finder_tags_full",
                        side_effect=lambda p: reads[p]), \
             mock.patch("api.routes._finder_tagwrite.write_finder_tags",
                        side_effect=[None, RuntimeError("io")]) as w:
            result = tagging_mod._revert_finder_tag(Path("/v"), {}, op)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["ok"])
        self.assertEqual((result["reverted"], result["failed"]), (1, 1))
        self.assertEqual(op["state"], "applied")  # prune-safe, retryable
        self.assertEqual(w.call_count, 2)

        # Retry after the failure clears: a.pdf is already back at `before`
        # (skips), b.pdf still matches `after` and now reverts cleanly.
        reads["/v/a.pdf"] = ["old"]
        with mock.patch("api.routes._finder_tagwrite.is_macos", return_value=True), \
             mock.patch("tagging.writers.os.path.exists", return_value=True), \
             mock.patch("api.routes._finder_tagwrite.read_finder_tags_full",
                        side_effect=lambda p: reads[p]), \
             mock.patch("api.routes._finder_tagwrite.write_finder_tags") as w2:
            result2 = tagging_mod._revert_finder_tag(Path("/v"), {}, op)
        self.assertEqual(result2["status"], "reverted")
        self.assertEqual(op["state"], "reverted")
        w2.assert_called_once_with("/v/b.pdf", [])

    def test_read_failure_is_a_failure_not_a_skip(self):
        """2026-07-22 audit M1: an unreadable xattr READ must tally `failed`
        (op stays applied/prune-safe), never `skipped` — a swallowed read
        looked like 'tags changed' and let a one-of-two revert flip the op to
        reverted, after which prune destroyed the unrestored PDF's only
        before-list."""
        from api.routes import tagging as tagging_mod
        op = self._op()

        def _read(p):
            if p == "/v/b.pdf":
                raise RuntimeError("xattr read failed: timeout")
            return {"/v/a.pdf": ["dep", "old"]}[p]

        with mock.patch("api.routes._finder_tagwrite.is_macos", return_value=True), \
             mock.patch("tagging.writers.os.path.exists", return_value=True), \
             mock.patch("api.routes._finder_tagwrite.read_finder_tags_full",
                        side_effect=_read), \
             mock.patch("api.routes._finder_tagwrite.write_finder_tags") as w:
            result = tagging_mod._revert_finder_tag(Path("/v"), {}, op)
        # a.pdf reverted; b.pdf FAILED (not skipped) — the op must stay applied.
        self.assertEqual((result["reverted"], result["failed"]), (1, 1))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(op["state"], "applied")
        w.assert_called_once()

    def test_missing_file_is_a_skip_not_a_failure(self):
        """A deleted PDF has nothing left to restore — it must stay a SKIP so
        an op whose files are all gone doesn't fail-retry forever."""
        from api.routes import tagging as tagging_mod
        op = {"op_id": "x", "kind": "finder_tag", "state": "applied",
              "pdfs": [{"abs": "/nonexistent/gone.pdf", "rel": "gone.pdf",
                        "before": [], "after": ["dep"]}]}
        with mock.patch("api.routes._finder_tagwrite.is_macos", return_value=True):
            result = tagging_mod._revert_finder_tag(Path("/v"), {}, op)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual((result["reverted"], result["failed"]), (0, 0))
        self.assertEqual(op["state"], "applied")


@unittest.skipUnless(sys.platform == "darwin", "Finder tags are macOS-only")
class TestFinderUnionCaseSafety(unittest.TestCase):
    """`after` unions `added` (case-safe), never `valid` (case-dupe hazard)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tagging-case-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.biblio = self.vault / "Z_attachments" / "biblio_articles"
        self.biblio.mkdir(parents=True, exist_ok=True)
        self.pdf = self.biblio / "smith_2020.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 fake\n")

    def test_case_variant_not_duplicated(self):
        from api.routes import _finder_tagwrite
        from api.routes.tagging import _finder_write_locked
        from audit.engine.inventory import Record
        # PDF already carries lowercase 'dep'; the canonical vocab id is 'Dep'.
        _finder_tagwrite.write_finder_tags(str(self.pdf), ["dep"])
        rec = Record(citation_key="smith2020")
        rec.pdf_paths = [self.pdf]
        archive = self.tmp / "archive"
        result, status = _finder_write_locked(
            rec=rec, valid=["Dep", "mood"], rejected=[],
            cfg={"tagging_archive_dir": str(archive)},
            vault_root=self.vault, biblio_dir=self.biblio, citation_key="smith2020")
        self.assertEqual(status, 200, result)
        tags = _finder_tagwrite.read_finder_tags(str(self.pdf))
        lower = [t.lower() for t in tags]
        self.assertEqual(lower.count("dep"), 1)  # no 'dep' + 'Dep' duplicate pair
        self.assertIn("mood", lower)


@unittest.skipUnless(sys.platform == "darwin", "Finder tags are macOS-only")
class TestFinderColourPreservation(unittest.TestCase):
    """The write/restore round-trip must keep raw colour-suffixed entries.

    Pre-2026-07-19, the writer unioned bare NAMES and rewrote the whole xattr
    array, silently flattening every colour the user assigned in Finder. The
    fix mirrors the Zotero writer's ``before_raw`` pattern.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tagging-colour-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.archive = self.tmp / "archive"
        self.biblio = self.vault / "Z_attachments" / "biblio_articles"
        self.biblio.mkdir(parents=True, exist_ok=True)
        self.pdf = self.biblio / "smith_2020.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 fake\n")
        self.cfg = {"tagging_archive_dir": str(self.archive)}

    def _write_locked(self, valid):
        from api.routes.tagging import _finder_write_locked
        from audit.engine.inventory import Record
        rec = Record(citation_key="smith2020")
        rec.pdf_paths = [self.pdf]
        return _finder_write_locked(
            rec=rec, valid=valid, rejected=[], cfg=self.cfg,
            vault_root=self.vault, biblio_dir=self.biblio, citation_key="smith2020")

    def test_write_preserves_colour_suffix_and_restore_round_trips(self):
        from api.routes import _finder_tagwrite
        from api.routes.tagging import _revert_finder_tag
        from tagging import journal
        # A user-coloured tag (colour index 3) already on the PDF.
        _finder_tagwrite.write_finder_tags(str(self.pdf), ["keep\n3"])
        result, status = self._write_locked(["dep"])
        self.assertEqual(status, 200, result)
        raw = _finder_tagwrite.read_finder_tags_full(str(self.pdf))
        self.assertIn("keep\n3", raw)  # colour survived the union write
        self.assertIn("dep", raw)
        # Revert restores the exact raw prior state (colour included).
        manifest = journal.load(self.vault, self.cfg)
        op = manifest["ops"][-1]
        self.assertEqual(op["pdfs"][0]["before_raw"], ["keep\n3"])
        rev = _revert_finder_tag(self.vault, self.cfg, op)
        self.assertEqual(rev["status"], "reverted", rev)
        self.assertEqual(_finder_tagwrite.read_finder_tags_full(str(self.pdf)),
                         ["keep\n3"])

    def test_legacy_name_only_op_still_reverts(self):
        from api.routes import _finder_tagwrite
        from api.routes.tagging import _revert_finder_tag
        # Simulate a pre-fix op: names only, no before_raw/after_raw.
        _finder_tagwrite.write_finder_tags(str(self.pdf), ["keep", "dep"])
        op = {"op_id": "legacy", "kind": "finder_tag", "state": "applied",
              "pdfs": [{"abs": str(self.pdf), "rel": "smith_2020.pdf",
                        "before": ["keep"], "after": ["dep", "keep"]}]}
        rev = _revert_finder_tag(self.vault, self.cfg, op)
        self.assertEqual(rev["status"], "reverted", rev)
        self.assertEqual(_finder_tagwrite.read_finder_tags(str(self.pdf)), ["keep"])


class TestFinderReadFailureGuard(unittest.TestCase):
    """A failed xattr READ must refuse the write, never union against []."""

    def test_missing_attr_reads_empty_but_error_raises(self):
        from api.routes import _finder_tagwrite
        missing = mock.Mock(returncode=1, stdout=b"", stderr=b"No such xattr: tags")
        broken = mock.Mock(returncode=1, stdout=b"", stderr=b"Operation not permitted")
        with mock.patch("api.routes._finder_tagwrite.subprocess.run", return_value=missing):
            self.assertEqual(_finder_tagwrite.read_finder_tags_full("/x.pdf"), [])
        with mock.patch("api.routes._finder_tagwrite.subprocess.run", return_value=broken):
            with self.assertRaises(RuntimeError):
                _finder_tagwrite.read_finder_tags_full("/x.pdf")
            # The lenient name-only view still degrades to [] (display path).
            self.assertEqual(_finder_tagwrite.read_finder_tags("/x.pdf"), [])

    def test_write_refused_when_read_fails(self):
        from api.routes.tagging import _finder_write_locked
        from audit.engine.inventory import Record
        tmp = Path(tempfile.mkdtemp(prefix="tagging-readfail-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        vault = tmp / "vault"
        biblio = vault / "Z_attachments" / "biblio_articles"
        biblio.mkdir(parents=True, exist_ok=True)
        pdf = biblio / "smith_2020.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake\n")
        rec = Record(citation_key="smith2020")
        rec.pdf_paths = [pdf]
        with mock.patch("api.routes._finder_tagwrite.read_finder_tags_full",
                        side_effect=RuntimeError("xattr read failed: timeout")), \
             mock.patch("api.routes._finder_tagwrite.write_finder_tags") as w:
            result, status = _finder_write_locked(
                rec=rec, valid=["dep"], rejected=[],
                cfg={"tagging_archive_dir": str(tmp / "archive")},
                vault_root=vault, biblio_dir=biblio, citation_key="smith2020")
        self.assertEqual(status, 500)
        self.assertIn("error", result["pdfs"][0])
        w.assert_not_called()  # never clobber a set we could not read


class TestConfigMigration2to3(unittest.TestCase):
    """2→3 resets ONLY the old materialised propose-token default (300)."""

    def test_resets_stale_300(self):
        from core.config import _DEFAULTS, _migrate_2_to_3
        out = _migrate_2_to_3({"tagging_propose_max_tokens": 300, "provider": "google"})
        self.assertEqual(out["tagging_propose_max_tokens"],
                         _DEFAULTS["tagging_propose_max_tokens"])
        self.assertEqual(out["provider"], "google")

    def test_preserves_user_chosen_value(self):
        from core.config import _migrate_2_to_3
        out = _migrate_2_to_3({"tagging_propose_max_tokens": 512})
        self.assertEqual(out["tagging_propose_max_tokens"], 512)

    def test_full_chain_from_version_2(self):
        from core.config import CONFIG_VERSION, _apply_migrations
        data, changed = _apply_migrations(
            {"config_version": 2, "tagging_propose_max_tokens": 300})
        self.assertTrue(changed)
        self.assertEqual(data["config_version"], CONFIG_VERSION)
        self.assertNotEqual(data["tagging_propose_max_tokens"], 300)


class TestUsageWorkflowPersistence(unittest.TestCase):
    """Usage records carry the workflow tag through the ring AND the JSONL."""

    def test_workflow_round_trips(self):
        import json as _json
        from core.llm.types import LLMUsage
        from core.llm.usage import UsageTracker
        tmp = Path(tempfile.mkdtemp(prefix="usage-wf-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        tracker = UsageTracker()
        tracker.configure(str(tmp / "usage.jsonl"))
        rec = tracker.record(
            provider="google", model="gemini-2.5-flash",
            usage=LLMUsage(input_tokens=10, output_tokens=5),
            latency_ms=1, stream=True, workflow="tagging_propose")
        self.assertEqual(rec.workflow, "tagging_propose")
        line = (tmp / "usage.jsonl").read_text(encoding="utf-8").strip()
        self.assertEqual(_json.loads(line)["workflow"], "tagging_propose")
        # Legacy rows (no workflow key) must still parse.
        self.assertEqual(len(tracker._disk_records()), 1)


class TestExtractMarkitdownGate(unittest.TestCase):
    """``use_markitdown=False`` must gate every MarkItDown fallback tier."""

    def _run(self, use_markitdown):
        import pdf_extractor
        doc = mock.MagicMock()
        doc.needs_pass = False
        doc.__len__ = mock.Mock(return_value=5)
        ctx = mock.MagicMock()
        ctx.__enter__ = mock.Mock(return_value=doc)
        ctx.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(pdf_extractor.fitz, "open", return_value=ctx), \
             mock.patch.object(pdf_extractor, "_collect_all_blocks",
                               return_value=([], 0.0)), \
             mock.patch.object(pdf_extractor, "_try_markitdown_extraction",
                               return_value=None) as mid:
            out = pdf_extractor.extract_structured_from_pdf(
                "/tmp/x.pdf", char_budget=1000, end_page=3,
                use_markitdown=use_markitdown)
        return out, mid

    def test_gated_off(self):
        out, mid = self._run(False)
        mid.assert_not_called()
        self.assertFalse(out.full_text)

    def test_default_on(self):
        _out, mid = self._run(True)
        mid.assert_called_once()

    def test_every_site_is_guarded(self):
        # Source ratchet: a future MarkItDown call site added without the
        # use_markitdown guard reintroduces the unbounded whole-file pdfminer
        # pass this flag exists to prevent.
        import inspect

        import pdf_extractor
        src = inspect.getsource(pdf_extractor.extract_structured_from_pdf)
        self.assertEqual(src.count("_try_markitdown_extraction("), 3)
        self.assertEqual(src.count("if use_markitdown else None"), 3)


class TestReadPdfTextFreshBounds(unittest.TestCase):
    """``_read_pdf_text``: page bound relayed; ``truncated`` derived, not relayed."""

    def setUp(self):
        import rag.vault as rv
        self.rv = rv
        self.tmp = Path(tempfile.mkdtemp(prefix="ts-vault-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.pdf = self.tmp / "paper.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 fake body for hashing")

    def _call(self, **kwargs):
        return self.rv.obsidian_manager._read_pdf_text(
            self.pdf, self.tmp, char_budget=5000, **kwargs)

    def test_relays_bounds_and_derives_truncated(self):
        sections = SimpleNamespace(full_text="short text " * 10,
                                   truncated=False, page_count=10)
        with mock.patch.object(self.rv, "extract_structured_from_pdf",
                               return_value=sections) as ex:
            _text, truncated = self._call(fresh_max_pages=3, use_markitdown=False)
        self.assertTrue(truncated)  # 10-page doc bounded to 3 pages
        kw = ex.call_args.kwargs
        self.assertEqual(kw["end_page"], 3)
        self.assertFalse(kw["use_markitdown"])

    def test_defaults_are_byte_identical_for_existing_callers(self):
        sections = SimpleNamespace(full_text="x" * 50, truncated=False, page_count=10)
        with mock.patch.object(self.rv, "extract_structured_from_pdf",
                               return_value=sections) as ex:
            _text, truncated = self._call()
        self.assertFalse(truncated)
        kw = ex.call_args.kwargs
        self.assertEqual(kw["end_page"], self.rv.EXTRACT_MAX_PAGES_PER_CALL)
        self.assertTrue(kw["use_markitdown"])


class TestNotextMarker(unittest.TestCase):
    """text_source: the ``.notext`` negative cache fast-fails known no-text PDFs."""

    def setUp(self):
        import rag.vault as rv

        from tagging import text_source
        self.rv = rv
        self.ts = text_source
        self.tmp = Path(tempfile.mkdtemp(prefix="ts-marker-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.pdf = self.tmp / "scanned.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 scanned fake")
        mgr = rv.obsidian_manager
        sig = mgr._pdf_file_signature(self.pdf, {}, None)
        self.cache_file = mgr._pdf_cache_file(self.tmp, sig)
        self.digest = str(sig["sha256"])
        self.marker = self.cache_file.with_name(f"{self.digest}.notext")
        self.range_file = self.cache_file.with_name(f"{self.digest}-p00000-00999.txt")
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        for p in (self.cache_file, self.marker, self.range_file):
            if p.exists():
                p.unlink()

    @staticmethod
    def _empty_sections():
        return SimpleNamespace(full_text="", truncated=False, page_count=5)

    def test_marker_written_then_fast_fails(self):
        with mock.patch.object(self.rv, "extract_structured_from_pdf",
                               return_value=self._empty_sections()) as ex:
            text, _ = self.ts.read_pdf_text(self.pdf, self.tmp, 1400, fresh_max_pages=3)
            self.assertEqual(text, "")
            self.assertTrue(self.marker.exists())
            self.assertEqual(ex.call_count, 1)
            text2, _ = self.ts.read_pdf_text(self.pdf, self.tmp, 1400, fresh_max_pages=3)
            self.assertEqual(text2, "")
            self.assertEqual(ex.call_count, 1)  # marker fast-fail: no re-scan

    def test_real_cache_wins_over_marker(self):
        self.marker.write_text("{}", encoding="utf-8")
        self.cache_file.write_text("OCR text recovered later by the indexer",
                                   encoding="utf-8")
        with mock.patch.object(self.rv, "extract_structured_from_pdf") as ex:
            text, _ = self.ts.read_pdf_text(self.pdf, self.tmp, 1400)
        self.assertIn("OCR text", text)
        ex.assert_not_called()

    def test_per_range_cache_counts_as_real(self):
        # A >1000-page scanned PDF OCR'd by the indexer produces ONLY per-range
        # cache files (same sha); the marker must not shadow them.
        self.marker.write_text("{}", encoding="utf-8")
        self.range_file.write_text("range OCR text", encoding="utf-8")
        with mock.patch.object(self.rv, "extract_structured_from_pdf") as ex:
            text, _ = self.ts.read_pdf_text(self.pdf, self.tmp, 1400)
        self.assertIn("range OCR text", text)
        ex.assert_not_called()

    def test_deeper_request_ignores_shallow_marker(self):
        """2026-07-20 audit: a 3-page snippet marker must NOT suppress the
        8-page propose extraction — the marker's ``pages_scanned`` gates the
        fast-fail, and a still-empty deeper scan rewrites it deeper."""
        with mock.patch.object(self.rv, "extract_structured_from_pdf",
                               return_value=self._empty_sections()) as ex:
            self.ts.read_pdf_text(self.pdf, self.tmp, 1400, fresh_max_pages=3)
            self.assertEqual(ex.call_count, 1)
            # Deeper request re-extracts despite the marker…
            self.ts.read_pdf_text(self.pdf, self.tmp, 1400, fresh_max_pages=8)
            self.assertEqual(ex.call_count, 2)
            # …then the rewritten (deeper) marker fast-fails the same depth…
            self.ts.read_pdf_text(self.pdf, self.tmp, 1400, fresh_max_pages=8)
            self.assertEqual(ex.call_count, 2)
            # …and covers shallower requests too.
            self.ts.read_pdf_text(self.pdf, self.tmp, 1400, fresh_max_pages=3)
            self.assertEqual(ex.call_count, 2)

    def test_legacy_marker_without_depth_rescans_once_then_heals(self):
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text("{}", encoding="utf-8")  # pre-fix marker shape
        with mock.patch.object(self.rv, "extract_structured_from_pdf",
                               return_value=self._empty_sections()) as ex:
            self.ts.read_pdf_text(self.pdf, self.tmp, 1400, fresh_max_pages=3)
            self.assertEqual(ex.call_count, 1)  # depth-unknown ⇒ one re-scan
            self.ts.read_pdf_text(self.pdf, self.tmp, 1400, fresh_max_pages=3)
            self.assertEqual(ex.call_count, 1)  # rewritten marker fast-fails

    def test_no_marker_when_text_found(self):
        secs = SimpleNamespace(
            full_text="A real abstract with plenty of extractable text.",
            truncated=False, page_count=2)
        with mock.patch.object(self.rv, "extract_structured_from_pdf",
                               return_value=secs):
            text, _ = self.ts.read_pdf_text(self.pdf, self.tmp, 1400, fresh_max_pages=3)
        self.assertTrue(text)
        self.assertFalse(self.marker.exists())

    def test_concurrent_fresh_extracts_run_once(self):
        # 2026-07-19 stampede fix: the snippet route runs on the request thread
        # with no gate, so rapid card navigation issued concurrent extractions
        # of the same uncached PDF (each a synchronous PyMuPDF pass) racing the
        # .notext write. Under _FRESH_EXTRACT_LOCK the second caller re-probes
        # and finds the first's marker instead of re-scanning.
        import threading as th
        import time as _t
        started = th.Event()
        release = th.Event()
        calls = []

        def _slow_extract(*a, **k):
            calls.append(1)
            started.set()
            release.wait(5)
            return self._empty_sections()

        results = {}

        def _run(name):
            results[name] = self.ts.read_pdf_text(
                self.pdf, self.tmp, 1400, fresh_max_pages=3)

        with mock.patch.object(self.rv, "extract_structured_from_pdf",
                               side_effect=_slow_extract):
            t1 = th.Thread(target=_run, args=("a",))
            t1.start()
            self.assertTrue(started.wait(5))   # t1 is inside the extraction
            t2 = th.Thread(target=_run, args=("b",))
            t2.start()
            _t.sleep(0.1)                       # let t2 park on the lock
            release.set()
            t1.join(5)
            t2.join(5)
        self.assertEqual(len(calls), 1)         # exactly one extraction ran
        # t1 ran the real page-bounded extract (3 of 5 pages ⇒ truncated=True);
        # t2 re-probed under the lock and took the marker fast-fail.
        self.assertEqual(results["a"], ("", True))
        self.assertEqual(results["b"], ("", False))
        self.assertTrue(self.marker.exists())


class TestCandidateSelection(unittest.TestCase):
    """tagging/candidates.py — ranking, journal mining, degradation, caching."""

    def setUp(self):
        from tagging import candidates
        self.c = candidates
        # The gloss memo is module-level state; isolate tests from each other.
        self.c._GLOSS_MEMO.update(key=None, vectors=None)

    def test_cosine_ranking_keeps_top_n_in_vocab_order(self):
        vocab = {"dep": "d", "mood": "m", "scz": "s"}
        vectors = {"dep": [1.0, 0.0], "mood": [0.9, 0.1], "scz": [0.0, 1.0]}
        emb = mock.Mock()
        emb.get_query_embedding.return_value = [1.0, 0.0]
        with mock.patch.object(self.c, "gloss_embeddings", return_value=vectors):
            out = self.c._select_candidates(
                {}, Path("/tmp"), Path("/tmp/_tags.md"), "text", vocab, 2, emb)
        self.assertEqual(list(out), ["dep", "mood"])

    def test_zero_or_oversized_n_returns_whole_vocab_untouched(self):
        vocab = {"dep": "d", "mood": "m"}
        emb = mock.Mock()
        for n in (0, 2, 99):
            out = self.c._select_candidates(
                {}, Path("/tmp"), Path("/tmp/_tags.md"), "text", vocab, n, emb)
            self.assertEqual(out, vocab)
        emb.get_query_embedding.assert_not_called()

    def test_journal_mining_aggregates_three_kinds_and_skips_reverted(self):
        manifest = {"ops": [
            {"kind": "note_tag", "citation_key": "a", "added": ["dep"],
             "state": "applied", "ts": "2026-01-01"},
            {"kind": "zotero_tag", "citation_key": "a", "added": ["mood"],
             "state": "applied", "ts": "2026-01-02"},
            {"kind": "finder_tag", "citation_key": "b", "added": ["scz"],
             "state": "reverted", "ts": "2026-01-03"},
            {"kind": "note_tag", "citation_key": "c", "added": [],
             "state": "applied"},
        ]}
        with mock.patch("tagging.journal.load", return_value=manifest):
            out = self.c.mine_journal_exemplars(Path("/tmp/vault"), {})
        self.assertEqual([e["key"] for e in out], ["a"])
        self.assertEqual(out[0]["tags"], ["dep", "mood"])
        self.assertEqual(out[0]["ts"], "2026-01-02")

    def test_propose_context_degrades_on_any_embed_failure(self):
        vocab = {"dep": "d"}
        with mock.patch.object(self.c, "_embedder",
                               side_effect=RuntimeError("backend down")):
            eff, ex = self.c.propose_context(
                cfg={}, vault_root=Path("/tmp"), tags_path=Path("/tmp/_tags.md"),
                inv=None, paper_text="t", vocab=vocab, candidate_n=5, fewshot_k=3)
        self.assertEqual(eff, vocab)
        self.assertEqual(ex, [])

    def test_knobs_off_never_touch_the_embedder(self):
        vocab = {"dep": "d"}
        with mock.patch.object(self.c, "_embedder") as emb:
            eff, ex = self.c.propose_context(
                cfg={}, vault_root=Path("/tmp"), tags_path=Path("/tmp/_tags.md"),
                inv=None, paper_text="t", vocab=vocab, candidate_n=0, fewshot_k=0)
        emb.assert_not_called()
        self.assertEqual(eff, vocab)
        self.assertEqual(ex, [])

    def test_propose_context_polls_cancel_between_stages(self):
        # 2026-07-19: a 504'd request's cancel token used to be dead until the
        # LLM stream began — the embed pipeline ran to completion for nobody,
        # pinning the single-flight gate. Cancelled up-front: no embedder at
        # all. Cancelled after the candidate stage: exemplars are skipped.
        vocab = {"dep": "d"}
        with mock.patch.object(self.c, "_embedder") as emb:
            eff, ex = self.c.propose_context(
                cfg={}, vault_root=Path("/tmp"), tags_path=Path("/tmp/_tags.md"),
                inv=None, paper_text="t", vocab=vocab, candidate_n=5, fewshot_k=3,
                should_cancel=lambda: True)
        emb.assert_not_called()
        self.assertEqual((eff, ex), (vocab, []))

        calls = iter([False, True])  # first poll passes, second cancels
        with mock.patch.object(self.c, "_embedder", return_value=mock.Mock()), \
             mock.patch.object(self.c, "_select_candidates",
                               return_value={"dep": "d"}) as sc, \
             mock.patch.object(self.c, "_select_exemplars") as se:
            eff, ex = self.c.propose_context(
                cfg={}, vault_root=Path("/tmp"), tags_path=Path("/tmp/_tags.md"),
                inv=None, paper_text="t", vocab=vocab, candidate_n=1, fewshot_k=3,
                should_cancel=lambda: next(calls))
        sc.assert_called_once()
        se.assert_not_called()
        self.assertEqual((eff, ex), ({"dep": "d"}, []))

    def test_concurrent_gloss_builds_embed_once(self):
        # 2026-07-19 DCL fix: _MEMO_LOCK only guarded the dict assignment; two
        # concurrent misses each ran the expensive embed batch. Under
        # _BUILD_LOCK the second builder re-checks the memo and returns the
        # first's vectors without touching the embedder.
        import threading as th
        import time as _t
        tmp = Path(tempfile.mkdtemp(prefix="tagging-dcl-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        tags = tmp / "_tags.md"
        tags.write_text("| Description | Tags |\n|---|---|\n| Dep | #dep |\n",
                        encoding="utf-8")
        vocab = {"dep": "Dep"}
        started = th.Event()
        release = th.Event()
        emb = mock.Mock()

        def _slow_batch(texts):
            started.set()
            release.wait(5)
            return [[1.0, 0.0] for _ in texts]

        emb.get_text_embedding_batch.side_effect = _slow_batch
        results = {}

        def _run(name):
            results[name] = self.c.gloss_embeddings({}, vocab, tags, tmp, embedder=emb)

        t1 = th.Thread(target=_run, args=("a",))
        t1.start()
        self.assertTrue(started.wait(5))
        t2 = th.Thread(target=_run, args=("b",))
        t2.start()
        _t.sleep(0.1)  # let t2 park on _BUILD_LOCK
        release.set()
        t1.join(5)
        t2.join(5)
        self.assertEqual(emb.get_text_embedding_batch.call_count, 1)
        self.assertEqual(results["a"], results["b"])

    def test_gloss_cache_invalidates_on_mtime_and_model(self):
        import os as _os
        tmp = Path(tempfile.mkdtemp(prefix="tagging-gloss-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        tags = tmp / "_tags.md"
        tags.write_text("| Description | Tags |\n|---|---|\n| Dep | #dep |\n",
                        encoding="utf-8")
        vocab = {"dep": "Dep"}
        emb = mock.Mock()
        emb.get_text_embedding_batch.return_value = [[0.1, 0.2]]
        cfg = {"embed": "model-a"}
        self.c.gloss_embeddings(cfg, vocab, tags, tmp, embedder=emb)
        self.c.gloss_embeddings(cfg, vocab, tags, tmp, embedder=emb)
        self.assertEqual(emb.get_text_embedding_batch.call_count, 1)  # cached
        _os.utime(tags, ns=(1, 1))  # mtime change ⇒ rebuild
        self.c.gloss_embeddings(cfg, vocab, tags, tmp, embedder=emb)
        self.assertEqual(emb.get_text_embedding_batch.call_count, 2)
        self.c.gloss_embeddings({"embed": "model-b"}, vocab, tags, tmp, embedder=emb)
        self.assertEqual(emb.get_text_embedding_batch.call_count, 3)  # model switch


class TestProposePromptShape(unittest.TestCase):
    """build_messages: knobs-off byte-identity + exemplar block placement."""

    def test_no_exemplars_is_byte_identical_to_historical_prompt(self):
        from tagging.propose import build_messages
        a = build_messages("T", "X", {"dep": "d"}, 4)
        b = build_messages("T", "X", {"dep": "d"}, 4, exemplars=None)
        c = build_messages("T", "X", {"dep": "d"}, 4, exemplars=[])
        self.assertEqual(a, b)
        self.assertEqual(a, c)

    def test_exemplar_block_sits_between_legend_and_paper(self):
        from tagging.propose import build_messages
        msgs = build_messages("T", "X", {"dep": "d"}, 4,
                              exemplars=[("Paper A", ["dep", "mood"])])
        content = msgs[0]["content"]
        self.assertIn("EXAMPLES from this library", content)
        self.assertIn("Paper A → dep, mood", content)
        self.assertLess(content.index("Controlled vocabulary"),
                        content.index("EXAMPLES"))
        self.assertLess(content.index("EXAMPLES"), content.index("PAPER TITLE"))


if __name__ == "__main__":
    unittest.main()


class TestRetireTag(unittest.TestCase):
    """Batch G (2026-07-22): vault-wide tag retirement + batch restore."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tagging-retire-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.archive = self.tmp / "archive"
        (self.vault / "Z_Zotero_Notes").mkdir(parents=True, exist_ok=True)
        self.biblio = self.vault / "Z_attachments" / "biblio_articles"
        self.biblio.mkdir(parents=True, exist_ok=True)
        (self.vault / "_tags.md").write_text(
            "| Description | Tags |\n|---|---|\n| Obsolete stuff | #legacy |\n"
            "| Mood | #mood |\n", encoding="utf-8")
        self.notes = {}
        for key in ("a2020", "b2021"):
            note = self.vault / "Z_Zotero_Notes" / f"{key}.md"
            note.write_text("---\ntags:\n  - keep\n  - legacy\n---\nbody\n",
                            encoding="utf-8")
            self.notes[key] = note
        self.client = _build_app().test_client()
        self.cfg = {
            "vault_thesaurus_tags_path": "_tags.md",
            "tagging_archive_dir": str(self.archive),
        }

    def _inv(self):
        from audit.core.obsidian import NoteInfo
        from audit.engine.inventory import Record
        recs = {}
        for key, note in self.notes.items():
            rec = Record(citation_key=key)
            rec.pdf_paths = []
            rec.obsidian_note = NoteInfo(path=note, tags=["keep", "legacy"],
                                         has_frontmatter=True)
            recs[key] = rec
        inv = mock.MagicMock()
        inv.records = recs
        settings = SimpleNamespace(vault_root=self.vault,
                                   biblio_articles_dir=self.biblio)
        return inv, settings

    def _patches(self):
        from audit.manager import audit_manager
        return (
            mock.patch("api.routes.tagging.load_config", return_value=self.cfg),
            mock.patch("tagging.writers.load_config", return_value=self.cfg),
            mock.patch("api.routes.tagging._vault_root", return_value=self.vault),
            mock.patch.object(audit_manager, "get_inventory",
                              return_value=self._inv()),
        )

    @staticmethod
    def _frames(raw):
        import json as _json
        out = []
        for line in raw.decode("utf-8").splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                out.append(_json.loads(line[6:]))
        return out

    def test_vocab_row_edit_is_targeted(self):
        from tagging.vocab import retire_tag_from_vocab
        text = (self.vault / "_tags.md").read_text(encoding="utf-8")
        new, rows = retire_tag_from_vocab(text, "legacy")
        self.assertEqual(rows, 1)
        self.assertNotIn("#legacy", new)
        self.assertIn("#mood", new)          # other rows untouched
        self.assertIn("Obsolete stuff", new)  # description survives

    def test_retire_and_batch_restore_round_trip(self):
        import yaml
        originals = {k: n.read_text(encoding="utf-8") for k, n in self.notes.items()}
        with contextlib.ExitStack() as st:
            for p in self._patches():
                st.enter_context(p)
            r = self.client.post(
                "/api/tagging/retire-tag",
                json={"tag": "legacy",
                      "targets": {"note": True, "finder": False, "zotero": False},
                      "include_vocab": True, "confirm": True},
                headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        frames = self._frames(r.get_data())
        terms = [f["retire"] for f in frames if "retire" in f]
        self.assertEqual(len(terms), 1)
        t = terms[0]
        self.assertEqual(t["results"]["note"]["removed"], 2)
        self.assertEqual(t["vocab_rows"], 1)
        self.assertTrue(t["batch_id"])
        self.assertEqual(t["op_count"], 3)   # 2 notes + 1 vocab edit
        for note in self.notes.values():
            fm = yaml.safe_load(note.read_text(encoding="utf-8").split("---")[1])
            self.assertEqual(fm["tags"], ["keep"])
        self.assertNotIn("#legacy", (self.vault / "_tags.md").read_text(encoding="utf-8"))

        # Every op carries the batch id; batch restore re-adds everything.
        from tagging import journal
        man = journal.load(self.vault, self.cfg)
        stamped = [op for op in man["ops"] if op.get("batch_id") == t["batch_id"]]
        self.assertEqual(len(stamped), 3)
        self.assertIn("vocab_retire", {op["kind"] for op in stamped})

        with contextlib.ExitStack() as st:
            for p in self._patches():
                st.enter_context(p)
            rr = self.client.post("/api/tagging/restore",
                                  json={"batch_id": t["batch_id"], "confirm": True},
                                  headers=_HEADERS)
        self.assertEqual(rr.status_code, 200)
        # Batch restore streams since 2026-07-30 (Plan 5): per-op {info}
        # progress + a terminal {restore} frame carrying the old JSON payload.
        rframes = self._frames(rr.get_data())
        rterms = [f["restore"] for f in rframes if "restore" in f]
        self.assertEqual(len(rterms), 1)
        d = rterms[0]
        self.assertEqual(d["reverted"], 3)
        self.assertEqual(d["total"], 3)
        self.assertTrue([f for f in rframes if "info" in f], "no progress frames")
        for key, note in self.notes.items():
            self.assertEqual(note.read_text(encoding="utf-8"), originals[key])
        self.assertIn("#legacy", (self.vault / "_tags.md").read_text(encoding="utf-8"))

    def test_batch_restore_with_a_zotero_op_reaches_phase_two(self):
        """Regression: phase 1 called the SSE skeleton's ``threading.Event``
        as ``cancel()``.

        That line is only reached when the batch contains at least one Zotero
        op, so the note+vocab round-trip above never touched it. With a Zotero
        op present it raised ``TypeError: 'Event' object is not callable``,
        which ``_runner``'s blanket handler turned into an ``{"error"}`` frame
        BEFORE the op-lock was taken — so a retire batch that had already
        mutated notes, Finder tags and Zotero items reverted **nothing**, and
        the one-click Undo was silently unavailable (HTTP 200 throughout).
        """
        from tagging import journal
        man = journal.load(self.vault, self.cfg)
        op_id = journal.new_op_id(man)
        man["ops"].append({
            "op_id": op_id, "kind": "zotero_untag", "state": "applied",
            "batch_id": "BZ", "item_key": "KA", "citation_key": "a2020",
            "base_url": "http://z", "lib_path": "users/1", "transport": "local",
            "before_raw": [{"tag": "legacy"}], "before": ["legacy"], "after": [],
        })
        journal.save(self.vault, self.cfg, man)

        reads = []

        def _get_item_tags(base, lib_path, item_key, api_key=None):
            reads.append(item_key)
            return 9, []          # matches op["after"] → prepare yields a commit

        with contextlib.ExitStack() as st:
            for p in self._patches():
                st.enter_context(p)
            st.enter_context(mock.patch(
                "tagging.zotero_write.get_item_tags", _get_item_tags))
            r = self.client.post("/api/tagging/restore",
                                 json={"batch_id": "BZ", "confirm": True},
                                 headers=_HEADERS)

        # The discriminator: pre-fix the TypeError aborted the worker in phase
        # 1, so the stream carried a top-level {"error"} and NO terminal frame
        # and the op-lock was never even taken. Post-fix phase 1 completes and
        # phase 2 runs to its terminal frame. (Whether the individual PATCH
        # succeeds is a separate concern, covered by the retire-Zotero tests —
        # here the network is deliberately left unstubbed and that op simply
        # reports a per-op error, which is still a phase-2 outcome.)
        self.assertEqual(r.status_code, 200)
        frames = self._frames(r.get_data())
        self.assertEqual([f for f in frames if "error" in f], [],
                         "phase 1 raised before the lock was taken")
        self.assertEqual(reads, ["KA"], "phase 1 (pre-lock Zotero read) never ran")
        terms = [f["restore"] for f in frames if "restore" in f]
        self.assertEqual(len(terms), 1, "no terminal restore frame — phase 2 never ran")
        self.assertEqual(terms[0]["total"], 1, "phase 2 processed no ops")

    def test_batch_restore_validation_errors_stay_json(self):
        """A decision made BEFORE streaming must still be a real status code —
        an SSE 200 carrying an error frame would look like success to any
        client that checks `response.ok` (the CLI/MCP driver's single-op path
        relies on that, and only the JS batch caller consumes frames)."""
        with contextlib.ExitStack() as st:
            for p in self._patches():
                st.enter_context(p)
            r = self.client.post("/api/tagging/restore",
                                 json={"batch_id": "nosuchbatch", "confirm": True},
                                 headers=_HEADERS)
        self.assertEqual(r.status_code, 404)
        self.assertIn("batch id", (r.get_json() or {}).get("error", ""))

    def test_single_op_restore_still_returns_json(self):
        """The driver only ever sends op_id; that branch must stay JSON."""
        with contextlib.ExitStack() as st:
            for p in self._patches():
                st.enter_context(p)
            r = self.client.post("/api/tagging/restore",
                                 json={"op_id": "nosuchop", "confirm": True},
                                 headers=_HEADERS)
        self.assertIn(r.status_code, (404, 400))
        self.assertIsInstance(r.get_json(), dict)

    def test_retire_requires_confirm(self):
        with contextlib.ExitStack() as st:
            for p in self._patches():
                st.enter_context(p)
            r = self.client.post("/api/tagging/retire-tag",
                                 json={"tag": "legacy"}, headers=_HEADERS)
        self.assertEqual(r.status_code, 400)

    def test_retire_ops_carry_batch_id_at_every_save(self):
        """2026-07-23 audit: batch_id must be IN the op at its first save.

        The original code stamped batch_id after the writer's own save, so a
        cancel between two records persisted the last op applied-but-unstamped
        — invisible to the one-click batch restore. Wrapping journal.save
        asserts the invariant at every persistence point, which is exactly
        what a mid-run cancel would observe.
        """
        from tagging import journal
        real_save = journal.save
        violations = []

        def _checked_save(vault_root, cfg, manifest):
            for op in manifest.get("ops", []):
                if op.get("kind") in ("note_untag", "vocab_retire") \
                        and op.get("state") == "applied" \
                        and not op.get("batch_id"):
                    violations.append(op.get("op_id"))
            real_save(vault_root, cfg, manifest)

        with contextlib.ExitStack() as st:
            for p in self._patches():
                st.enter_context(p)
            st.enter_context(mock.patch("tagging.journal.save", _checked_save))
            r = self.client.post(
                "/api/tagging/retire-tag",
                json={"tag": "legacy",
                      "targets": {"note": True, "finder": False, "zotero": False},
                      "include_vocab": True, "confirm": True},
                headers=_HEADERS)
            # Consume INSIDE the patch scope — the SSE worker runs lazily as
            # the body is drained, not at post() time.
            raw = r.get_data()
        self.assertEqual(r.status_code, 200)
        terms = [f["retire"] for f in self._frames(raw) if "retire" in f]
        self.assertEqual(terms[0]["op_count"], 3)
        self.assertEqual(violations, [])

    def test_prune_spares_the_protected_batch(self):
        """2026-07-23 audit: a >cap batch must not evict its own undo.

        prune(protect_batch_id=...) exempts the batch's applied ops from the
        cap for that call only; a later unprotected prune enforces the cap
        normally.
        """
        from tagging import journal
        older = [{"op_id": f"old-{i}", "kind": "note_untag", "state": "applied",
                  "note_rel": f"n{i}.md", "hash_after": "x"}
                 for i in range(20)]
        batch = [{"op_id": f"b-{i}", "kind": "note_untag", "state": "applied",
                  "note_rel": f"m{i}.md", "hash_after": "x", "batch_id": "B"}
                 for i in range(210)]
        manifest = {"version": 1, "ops": older + batch}
        journal.prune(self.vault, self.cfg, manifest, protect_batch_id="B")
        kept_ids = {op["op_id"] for op in manifest["ops"]}
        self.assertTrue(all(f"b-{i}" in kept_ids for i in range(210)),
                        "protected batch ops were evicted")
        self.assertEqual(len(manifest["ops"]), 210)  # the 20 older ops paid

        journal.prune(self.vault, self.cfg, manifest)  # next, unprotected prune
        self.assertEqual(len(manifest["ops"]), 200)   # cap enforced again

    def test_prune_spares_the_op_this_request_just_appended(self):
        """A single write must not evict its OWN undo.

        Applied finder/zotero ops are never evictable (their before-lists live
        inline, so eviction destroys the only restore mapping), and the triage
        deck journals two of them per Accept. After ~100 accepts the manifest
        is entirely protected ops, so the next write's `note_tag` op is the
        only evictable record — the cap loop dropped it and unlinked its
        snapshot BEFORE the note was overwritten, while the response still
        returned its `op_id`. The undo 404'd and the snapshot was gone.
        """
        from tagging import journal
        full = [{"op_id": f"z-{i}", "kind": "finder_tag", "state": "applied",
                 "pdfs": []} for i in range(200)]
        fresh = {"op_id": "new-1", "kind": "note_tag", "state": "applied",
                 "note_rel": "n.md", "hash_after": "x"}

        manifest = {"version": 1, "ops": full + [fresh]}
        journal.prune(self.vault, self.cfg, manifest,
                      protect_op_ids={"new-1"})
        self.assertIn("new-1", {op["op_id"] for op in manifest["ops"]},
                      "the write evicted the very op it just journalled")

        # Without the exemption the cap still bites — the carve-out is
        # per-call, so the op ages out normally on a LATER write's prune.
        manifest2 = {"version": 1, "ops": full + [dict(fresh)]}
        journal.prune(self.vault, self.cfg, manifest2)
        self.assertNotIn("new-1", {op["op_id"] for op in manifest2["ops"]})

    def test_zotero_patches_run_lock_free_and_outcomes_are_recorded(self):
        """2026-07-23 audit Batch 2: retire's Zotero PATCHes must not hold
        the obsidian op-lock (they froze every vault write for the whole
        batch), and each PATCH outcome must land on the journalled op —
        success gets version_after, failure flips state to failed."""
        from rag.vault import obsidian_manager

        prepared_by_key = {
            "a2020": {"base": "http://z", "lib_path": "users/1", "item_key": "KA",
                      "api_key": None, "transport": "local", "version": 7,
                      "before_raw": [{"tag": "legacy"}], "before": ["legacy"],
                      "removed": ["legacy"], "after": [], "after_payload": []},
            "b2021": {"base": "http://z", "lib_path": "users/1", "item_key": "KB",
                      "api_key": None, "transport": "local", "version": 9,
                      "before_raw": [{"tag": "legacy"}], "before": ["legacy"],
                      "removed": ["legacy"], "after": [], "after_payload": []},
        }
        lock_free_during_patch = []

        def _fake_prepare(*, rec, valid, cfg, citation_key):
            return prepared_by_key[citation_key], None, None

        def _fake_patch(prepared):
            # Proves the op-lock is NOT held while the network write runs:
            # if we can acquire it here, the worker released it first.
            ep = obsidian_manager.try_acquire_lock(ttl=5)
            lock_free_during_patch.append(bool(ep))
            if ep:
                obsidian_manager.release_lock(ep)
            if prepared["item_key"] == "KA":
                return {"ok": True}, 200, 42
            return {"error": "boom"}, 502, None

        inv, settings = self._inv()
        for key in prepared_by_key:
            inv.records[key].zotero_item = SimpleNamespace(tags=["legacy"])

        from audit.manager import audit_manager
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch("api.routes.tagging.load_config",
                                        return_value=self.cfg))
            st.enter_context(mock.patch("tagging.writers.load_config",
                                        return_value=self.cfg))
            st.enter_context(mock.patch("api.routes.tagging._vault_root",
                                        return_value=self.vault))
            st.enter_context(mock.patch.object(audit_manager, "get_inventory",
                                               return_value=(inv, settings)))
            st.enter_context(mock.patch(
                "api.routes.tagging._zotero_remove_prepare", _fake_prepare))
            st.enter_context(mock.patch(
                "api.routes.tagging._zotero_remove_patch", _fake_patch))
            r = self.client.post(
                "/api/tagging/retire-tag",
                json={"tag": "legacy",
                      "targets": {"note": False, "finder": False, "zotero": True},
                      "include_vocab": False, "confirm": True},
                headers=_HEADERS)
            raw = r.get_data()
        self.assertEqual(r.status_code, 200)
        t = [f["retire"] for f in self._frames(raw) if "retire" in f][0]
        self.assertEqual(t["results"]["zotero"]["removed"], 1)
        self.assertEqual(t["results"]["zotero"]["failed"], 1)
        self.assertEqual(lock_free_during_patch, [True, True])

        from tagging import journal
        man = journal.load(self.vault, self.cfg)
        zops = {op["item_key"]: op for op in man["ops"]
                if op["kind"] == "zotero_untag"}
        self.assertEqual(zops["KA"]["state"], "applied")
        self.assertEqual(zops["KA"]["version_after"], 42)
        self.assertEqual(zops["KA"]["batch_id"], t["batch_id"])
        # The failed PATCH must never leave an "applied" claim — phase 4
        # flips it to failed (and its prune may then drop it as spent).
        applied = {k for k, o in zops.items() if o["state"] == "applied"}
        self.assertEqual(applied, {"KA"})

    def test_vocab_write_failure_is_reported_and_journalled_failed(self):
        """2026-07-23 audit: a failed _tags.md write must not report success.

        The op must flip to state="failed" (so restore/prune treat it as
        spent) and the terminal frame must report vocab_rows=0 + a failure —
        the original code left the op "applied" and the frame claiming the
        rows were edited.
        """
        import core.utils as core_utils
        real_write = core_utils.write_text_atomic

        def _failing_write(path, text):
            if str(path).endswith("_tags.md"):
                raise OSError("disk full")
            return real_write(path, text)

        with contextlib.ExitStack() as st:
            for p in self._patches():
                st.enter_context(p)
            st.enter_context(mock.patch("core.utils.write_text_atomic",
                                        _failing_write))
            r = self.client.post(
                "/api/tagging/retire-tag",
                json={"tag": "legacy",
                      "targets": {"note": True, "finder": False, "zotero": False},
                      "include_vocab": True, "confirm": True},
                headers=_HEADERS)
            # Consume INSIDE the patch scope — the SSE worker runs lazily as
            # the body is drained, not at post() time.
            raw = r.get_data()
        self.assertEqual(r.status_code, 200)
        t = [f["retire"] for f in self._frames(raw) if "retire" in f][0]
        self.assertEqual(t["vocab_rows"], 0)
        self.assertEqual(t["op_count"], 2)  # the 2 notes only
        self.assertTrue(any(f.startswith("vocab:") for f in t["failures"]))
        # _tags.md untouched; no APPLIED vocab op may survive (the failed op
        # is legitimately dropped as spent by the end-of-batch prune — what
        # must never remain is an "applied" claim for an edit that never
        # landed).
        self.assertIn("#legacy",
                      (self.vault / "_tags.md").read_text(encoding="utf-8"))
        from tagging import journal
        man = journal.load(self.vault, self.cfg)
        applied_vops = [op for op in man["ops"]
                        if op["kind"] == "vocab_retire"
                        and op["state"] == "applied"]
        self.assertEqual(applied_vops, [])


class TestTagKeyNFCNormalisation(unittest.TestCase):
    """P3.5 (2026-07-30): tag comparison is NFC-insensitive.

    macOS stores Finder tags (and Zotero can hold) accented text in NFD while
    `_tags.md` and the browser send NFC, so a `.lower()`-only comparison saw
    two different tags — unioning a visual duplicate, and missing the tag a
    removal targeted (2026-07-20 audit deferral).
    """

    # « rétablissement » in each normalisation form.
    NFC = unicodedata.normalize("NFC", "rétablissement")
    NFD = unicodedata.normalize("NFD", "rétablissement")

    def setUp(self):
        self.assertNotEqual(self.NFC, self.NFD)  # the forms really do differ

    def test_tag_key_folds_both_forms_together(self):
        from tagging.writers import _tag_key
        self.assertEqual(_tag_key(self.NFC), _tag_key(self.NFD))
        self.assertEqual(_tag_key("RÉTABLISSEMENT"), _tag_key(self.NFD))

    def test_nfd_request_maps_to_the_nfc_vocab_id(self):
        from tagging.writers import _canonicalise_tags
        valid, rejected = _canonicalise_tags([self.NFD], {self.NFC: "gloss"})
        self.assertEqual(valid, [self.NFC])   # the vocab's own spelling wins
        self.assertEqual(rejected, [])

    def test_nfc_request_maps_to_an_nfd_vocab_id(self):
        from tagging.writers import _canonicalise_tags
        valid, rejected = _canonicalise_tags([self.NFC], {self.NFD: "gloss"})
        self.assertEqual(valid, [self.NFD])
        self.assertEqual(rejected, [])

    def test_mixed_forms_dedupe_to_one_tag(self):
        from tagging.writers import _canonicalise_tags
        valid, _ = _canonicalise_tags([self.NFC, self.NFD, "#" + self.NFD],
                                      {self.NFC: "g"})
        self.assertEqual(valid, [self.NFC])

    def test_out_of_vocabulary_still_rejected(self):
        # The security gate must be unchanged by the normalisation fix.
        from tagging.writers import _canonicalise_tags
        valid, rejected = _canonicalise_tags(["not-a-real-tag"], {self.NFC: "g"})
        self.assertEqual(valid, [])
        self.assertEqual(rejected, ["not-a-real-tag"])

    def test_removal_targets_dedupe_across_forms(self):
        from tagging.writers import _canonicalise_removals
        out = _canonicalise_removals([self.NFC, self.NFD])
        self.assertEqual(len(out), 1)

    def test_ascii_behaviour_is_byte_identical(self):
        # No churn for the overwhelmingly common ASCII case.
        from tagging.writers import _canonicalise_tags
        valid, rejected = _canonicalise_tags(["#Methods", "methods", "Nope"],
                                             {"methods": "g"})
        self.assertEqual(valid, ["methods"])
        self.assertEqual(rejected, ["Nope"])
