"""Tests for the Zotero item-create subsystem (`zotcreate/`)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zotcreate import attempts, propose


class _Store:
    """Per-test attempt store under a throwaway BASE_DIR."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch("core.constants.BASE_DIR", self._tmp.name)
        self._patch.start()
        return self

    def __exit__(self, *a):
        self._patch.stop()
        self._tmp.cleanup()
        return False


class TestAttemptStore(unittest.TestCase):
    """The duplicate barrier. Every test here is a way to create twice."""

    VAULT = "/tmp/vault"
    SHA = "a" * 64

    def _reserve(self, sha=None, pdf_rel="biblio_articles/x.pdf"):
        return attempts.reserve(
            self.VAULT, sha or self.SHA, pdf_rel=pdf_rel,
            item_type="journalArticle", title="A paper",
            library="users/1", base="https://api.zotero.org")

    def test_a_first_reservation_is_fresh(self):
        with _Store():
            identity, stage, retained = self._reserve()
            self.assertEqual(stage, attempts.STAGE_FRESH)
            self.assertIsNone(retained)
            self.assertTrue(identity)

    def test_a_second_reservation_before_any_ack_refuses(self):
        """The lost-response case: a parent MAY exist and we have no key for
        it, so retrying would duplicate silently."""
        with _Store():
            self._reserve()
            _identity, stage, retained = self._reserve()
            self.assertEqual(stage, attempts.STAGE_PARENT_UNKNOWN)
            self.assertIsNone(retained["parent_key"])
            self.assertIn("may exist", attempts.block_message(stage, retained))

    def test_after_a_parent_ack_the_attempt_is_resumable_not_blocked(self):
        with _Store():
            identity, _stage, _r = self._reserve()
            attempts.acknowledge_parent(identity, key="PARENT01", version=9)
            _identity, stage, retained = self._reserve()
            self.assertEqual(stage, attempts.STAGE_PARENT_DONE)
            self.assertEqual(retained["parent_key"], "PARENT01")

    def test_a_completed_create_names_both_keys(self):
        with _Store():
            identity, _s, _r = self._reserve()
            attempts.acknowledge_parent(identity, key="PARENT01")
            attempts.acknowledge_attachment(identity, key="ATTACH01")
            _identity, stage, retained = self._reserve()
            self.assertEqual(stage, attempts.STAGE_COMPLETE)
            msg = attempts.block_message(stage, retained)
            self.assertIn("PARENT01", msg)
            self.assertIn("ATTACH01", msg)

    def test_identity_is_the_bytes_not_the_path(self):
        """A renamed PDF must not buy a second create."""
        with _Store():
            identity, _s, _r = self._reserve(pdf_rel="biblio_articles/old.pdf")
            again, stage, _r = self._reserve(pdf_rel="biblio_articles/new.pdf")
            self.assertEqual(identity, again)
            self.assertEqual(stage, attempts.STAGE_PARENT_UNKNOWN)

    def test_a_different_pdf_is_a_different_subject(self):
        with _Store():
            self._reserve()
            _identity, stage, _r = self._reserve(sha="b" * 64)
            self.assertEqual(stage, attempts.STAGE_FRESH)

    def test_acknowledging_twice_raises_rather_than_overwriting(self):
        """An overwrite would replace a real key with a second one — exactly
        the duplicate this store exists to make impossible."""
        with _Store():
            identity, _s, _r = self._reserve()
            attempts.acknowledge_parent(identity, key="PARENT01")
            with self.assertRaises(RuntimeError):
                attempts.acknowledge_parent(identity, key="PARENT02")

    def test_lookup_creates_nothing(self):
        with _Store():
            identity, stage, retained = attempts.lookup(self.VAULT, self.SHA)
            self.assertEqual(stage, attempts.STAGE_FRESH)
            self.assertIsNone(retained)
            self.assertFalse(attempts.store_path().exists())
            # And a later reserve is still fresh — the lookup left no row.
            self.assertEqual(self._reserve()[1], attempts.STAGE_FRESH)
            self.assertEqual(attempts.lookup(self.VAULT, self.SHA)[0], identity)

    def test_an_empty_store_reads_as_empty_rather_than_raising(self):
        """`reserve` creates the file on connect and the table in the next
        statement; a crash between the two must not block every future create
        behind an opaque 500."""
        with _Store():
            path = attempts.store_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"")
            _identity, stage, retained = attempts.lookup(self.VAULT, self.SHA)
            self.assertEqual(stage, attempts.STAGE_FRESH)
            self.assertIsNone(retained)

    def test_a_corrupt_store_raises_rather_than_allowing_a_create(self):
        with _Store():
            path = attempts.store_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"SQLite format 3\x00" + b"garbage" * 200)
            with self.assertRaises(Exception):
                attempts.lookup(self.VAULT, self.SHA)


if __name__ == "__main__":
    unittest.main()


class TestItemCreateRevert(unittest.TestCase):
    """`_revert_zotero_item_create_*` — a trash of the parent, refused
    whenever the item is no longer provably the one we made."""

    CREATION = {"key": "PARENT01", "itemType": "journalArticle",
                "title": "A paper", "version": 9}

    def _op(self, **over):
        op = {"op_id": "op1", "kind": "zotero_item_create",
              "item_key": "PARENT01", "item_type": "journalArticle",
              "title": "A paper", "lib_path": "users/1",
              "base_url": "https://api.zotero.org", "transport": "web",
              "attachment_key": "ATTACH01", "version_after": 9,
              "creation_data": dict(self.CREATION), "state": "applied"}
        op.update(over)
        return op

    def _prepare(self, op, *, data=None, version=9, children=(), env_key="k"):
        from tagging import writers
        data = dict(self.CREATION) if data is None else data
        with mock.patch.dict("os.environ", {"ZOTERO_API_KEY": env_key}), \
                mock.patch.object(writers.zotero_write, "get_item",
                                  return_value=(data, version)), \
                mock.patch.object(writers.zotero_write, "get_item_children",
                                  return_value=list(children)):
            return writers._revert_zotero_item_create_prepare({}, op)

    def test_an_unchanged_item_with_only_our_attachment_is_trashable(self):
        prepared = self._prepare(self._op(),
                                 children=[{"key": "ATTACH01", "deleted": 1}])
        self.assertIn("commit", prepared)

    def test_an_already_trashed_item_is_a_skip_not_an_error(self):
        data = dict(self.CREATION, deleted=1)
        prepared = self._prepare(self._op(), data=data)
        self.assertTrue(prepared["early"]["ok"])
        self.assertEqual(prepared["early"]["status"], "skipped")
        self.assertEqual(prepared["state"], "reverted")

    def test_an_edited_item_is_refused(self):
        data = dict(self.CREATION, title="Retitled by hand")
        prepared = self._prepare(self._op(), data=data)
        self.assertEqual(prepared["early"]["status"], "conflict")

    def test_a_child_we_did_not_create_refuses_the_trash(self):
        """Zotero trashes children with their parent, so a note the user added
        afterwards would go with it."""
        prepared = self._prepare(self._op(), children=[
            {"key": "ATTACH01"}, {"key": "USERNOTE", "itemType": "note"}])
        self.assertEqual(prepared["early"]["status"], "conflict")
        self.assertIn("added after", prepared["early"]["message"])

    def test_an_unverified_creation_baseline_is_refused(self):
        prepared = self._prepare(self._op(creation_data=None))
        self.assertEqual(prepared["early"]["status"], "conflict")

    def test_a_better_bibtex_citation_key_writeback_still_trashes(self):
        """BBT writes citationKey (and bumps version / dateModified) after a
        create. Treating that as "the item changed" refused the parent
        revert while the attachment op trashed the file — a fileless parent
        (2026-09-19 review A2)."""
        data = dict(self.CREATION, citationKey="smithAPaper2020",
                    dateModified="2026-09-19 12:00:00", version=12)
        prepared = self._prepare(self._op(), data=data, version=12)
        self.assertIn("commit", prepared, prepared)

    def test_a_web_op_without_a_key_refuses_before_any_read(self):
        from tagging import writers
        with mock.patch.dict("os.environ", {}, clear=True):
            prepared = writers._revert_zotero_item_create_prepare({}, self._op())
        self.assertFalse(prepared["early"]["ok"])

    def test_the_commit_is_a_trash_never_a_delete(self):
        from tagging import writers
        calls = {}

        def _set_deleted(base, lib, key, deleted, version, api_key=None):
            calls.update(key=key, deleted=deleted, version=version)
            return version + 1

        op = self._op()
        prepared = {"commit": {"base": "https://api.zotero.org", "lib_path": "users/1",
                               "key": "PARENT01", "api_key": "k", "version": 9}}
        with mock.patch.object(writers.zotero_write, "set_item_deleted", _set_deleted), \
                mock.patch("tagging.local_api.origin", return_value=False), \
                mock.patch("core.utils.log_vault_write"):
            result = writers._revert_zotero_item_create_commit(prepared, op)
        self.assertTrue(result["ok"])
        self.assertEqual(calls, {"key": "PARENT01", "deleted": True, "version": 9})
        self.assertEqual(op["state"], "reverted")

    def test_the_op_kind_is_never_evictable_while_applied(self):
        """Its restore mapping lives inline with no snapshot behind it, so
        pruning it would destroy the only record of what to trash."""
        from tagging.journal import EXTERNAL_OP_KINDS
        self.assertIn("zotero_item_create", EXTERNAL_OP_KINDS)


# --- route contract --------------------------------------------------------

import contextlib  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import flask  # noqa: E402

from api.routes.zotcreate import zotcreate_bp  # noqa: E402

_HEADERS = {"X-Requested-With": "ChatEKLD"}


def _app():
    app = flask.Flask(__name__)
    app.register_blueprint(zotcreate_bp)
    return app


class _Parent:
    """What `zotero_item_create_post` hands back."""

    library_type, group_id = "user", None

    def __init__(self, key="PARENT01"):
        self.item_key = key
        self.item_type = "journalArticle"
        self.title = "A paper"
        self.doi = ""
        self.server_id = "fixture-db"


class _CreateRouteHarness(unittest.TestCase):
    """Fixtures for the create route: a throwaway vault + attempt store, and
    a `_world` that stands in for Zotero, the journal and the op-lock.

    Underscore-named so pytest collects it through its subclasses only —
    otherwise every subclass re-runs the whole gate suite."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / "vault"
        (self.vault / "biblio_articles").mkdir(parents=True)
        self.pdf = self.vault / "biblio_articles" / "smith_2020.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 fake")
        self.settings = SimpleNamespace(vault_root=self.vault, mapping_file=Path(self.tmp.name)/"mapping.json")
        self.client = _app().test_client()
        base = mock.patch("core.constants.BASE_DIR", self.tmp.name)
        base.start()
        self.addCleanup(base.stop)

    # -- harness ----------------------------------------------------------
    @contextlib.contextmanager
    def _world(self, *, scanning=False, scanned=True, orphan=True,
               gate=(None, None, []), parent=_Parent, att_key="ATTACH01",
               att_early=None, parent_result=None, row_mapping=None,
               parent_fail_status=None, parent_fail_code=None):
        inv = SimpleNamespace(bridge=SimpleNamespace(unmapped_pdfs=[self.pdf],
                                                     pending_pdfs=[]))
        posted = {"parents": 0, "attachments": 0}

        def _post_parent(prepared):
            from tagging import zotero_write as zw
            # The batch route runs its proof once, BEFORE the reservation, and
            # sets no second gate; the single create sets one. Mirror the real
            # post's catch so a before_dispatch timeout is unreachable, not a
            # 500 (2026-09-19 review A4).
            if prepared["commit"].get("before_dispatch"):
                try:
                    prepared["commit"]["before_dispatch"]()
                except zw.ZoteroUnavailable:
                    return {"ok": False, "status": "unreachable",
                            "message": "Zotero response unavailable."}, None
            posted["parents"] += 1
            if parent_result is not None:
                from tagging.zotero_write import CreatedItemKey
                prepared['commit']['acknowledge'](CreatedItemKey('PARENT01', {'key':'PARENT01'}, 9))
                return dict(parent_result), None
            if parent is None:
                return {"ok": False, "status": parent_fail_status or "error",
                        "http_code": parent_fail_code,
                        "message": "Zotero refused."}, None
            from tagging.zotero_write import CreatedItemKey
            prepared['commit']['acknowledge'](CreatedItemKey('PARENT01', {'key':'PARENT01'}, 9))
            return ({"ok": True, "status": "created", "item_key": "PARENT01",
                     "version": 9, "creation_data": {"key": "PARENT01"}},
                    parent())

        def _post_att(prepared):
            prepared["commit"]["before_dispatch"]()
            posted["attachments"] += 1
            if att_key is None:
                return {"ok": False, "status": "error",
                        "message": "Attach failed."}, None, None
            prepared["commit"]["acknowledge"](att_key)
            return {"ok": True, "status": "attached"}, att_key, 10

        with mock.patch('accession.reconciliation.observe', return_value={'database':'fixture-db','items':{}}), \
                mock.patch('accession.reconciliation.rows', return_value=[dict(state='Needs identification',pdf={'rel':'biblio_articles/smith_2020.pdf'},associations=[],imported_copies=[],mapping=row_mapping or {'keys':[],'no_match':False,'exclude':[],'pending':False})]), \
                mock.patch("api.routes.zotcreate.audit_manager") as am, \
                mock.patch("api.routes.pending.audit_manager") as pending_am, \
                mock.patch("api.routes.accession._resolve_orphan",
                           return_value=((self.pdf, "biblio_articles/smith_2020.pdf")
                                         if orphan else None)), \
                mock.patch("api.routes.zotlink.rename_gate", return_value=gate), \
                mock.patch("tagging.writers.zotero_item_create_prepare",
                           return_value={"commit": {
                               "transport": SimpleNamespace(
                                   base_url="http://localhost:23119/api", kind="local",
                                   is_web=False, api_key="k", can_write=True),
                               "api_key": "k", "lib_path": "users/1",
                               "item_type": "journalArticle", "fields": {},
                               "creators": []}}), \
                mock.patch("tagging.writers.zotero_item_create_post", _post_parent), \
                mock.patch("tagging.writers.zotero_attachment_create_prepare",
                           return_value=(att_early or {"commit": {
                               "path": str(self.pdf), "parent_key": "PARENT01",
                               "lib_path": "users/1", "title": self.pdf.name,
                               "transport": SimpleNamespace(
                                   base_url="https://api.zotero.org", kind="web")}})), \
                mock.patch("tagging.writers.zotero_attachment_create_post", _post_att), \
                mock.patch("tagging.writers.zotero_attachment_create_journal",
                           side_effect=lambda **kw: {"op_id": "opA"}), \
                mock.patch("tagging.journal.load", return_value={"ops": []}), \
                mock.patch("tagging.journal.save") as saved, \
                mock.patch("tagging.journal.prune"), \
                mock.patch("rag.vault.obsidian_manager.try_acquire_lock",
                           return_value=1), \
                mock.patch("rag.vault.obsidian_manager.release_lock"):
            am.is_scanning.return_value = scanning
            am.get_inventory.return_value = ((inv, self.settings) if scanned
                                             else (None, None))
            pending_am.get_inventory.return_value = am.get_inventory.return_value
            self.saved = saved
            yield posted

    def _create(self, **over):
        from accession.review import identity
        body = {"target":{"base":"http://localhost:23119/api", "library":"users/1", "server_id":"fixture-db"}, "physical": identity(self.pdf), "review_token": None, "pdf": "biblio_articles/smith_2020.pdf",
                "item_type": "journalArticle",
                "fields": {"title": "A paper"}, "confirm": True}
        body.update(over)
        return self.client.post("/api/zotcreate/create", json=body,
                                headers=_HEADERS)

    def _sha(self):
        from accession.review import identity
        return identity(self.pdf)["sha256"]



class TestCreateRoute(_CreateRouteHarness):
    """The gates, and the property the whole route exists for: one create per
    PDF, whatever the network does."""

    # -- gates ------------------------------------------------------------
    def test_confirm_is_required(self):
        with self._world() as posted:
            r = self._create(confirm=False)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(posted["parents"], 0)

    def test_a_running_scan_refuses(self):
        with self._world(scanning=True):
            self.assertEqual(self._create().status_code, 409)

    def test_no_scan_refuses(self):
        with self._world(scanned=False):
            self.assertEqual(self._create().status_code, 404)

    def test_a_pdf_that_is_not_an_orphan_of_this_scan_refuses(self):
        with self._world(orphan=False) as posted:
            self.assertEqual(self._create().status_code, 400)
        self.assertEqual(posted["parents"], 0)

    def test_an_unknown_item_type_refuses_before_any_write(self):
        with self._world() as posted:
            r = self._create(item_type="tweet")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(posted["parents"], 0)

    def test_a_missing_title_refuses(self):
        with self._world():
            self.assertEqual(self._create(fields={}).status_code, 400)

    def test_the_rename_gate_refusal_is_returned_verbatim(self):
        """Zotero renaming the vault PDF the moment a link lands is the
        incident this gate exists for — a create must not route round it."""
        import json as _json
        refusal = (flask.Response(
            _json.dumps({"error": "renames",
                         "reason": "zotero_renames_linked_files"}),
            mimetype="application/json"), 409)
        with self._world(gate=(refusal, None, [])) as posted:
            r = self._create()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "zotero_renames_linked_files")
        self.assertEqual(posted["parents"], 0)

    def test_a_filename_the_plugin_rule_does_not_cover_refuses(self):
        with self._world(gate=(None, [r"^never_matches$"], [])) as posted:
            r = self._create()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "plugin_would_rename")
        self.assertEqual(posted["parents"], 0)

    def test_a_dry_run_writes_nothing_and_reserves_nothing(self):
        with self._world() as posted:
            r = self._create(dry_run=True, confirm=False)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["dry_run"])
        self.assertEqual(posted["parents"], 0)
        self.assertFalse(attempts.store_path().exists())

    # -- the barrier ------------------------------------------------------
    def test_the_happy_path_creates_both_and_journals_one_batch(self):
        with self._world() as posted:
            r = self._create()
        body = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual((body["item_key"], body["attachment_key"]),
                         ("PARENT01", "ATTACH01"))
        self.assertTrue(body["batch_id"])
        self.assertEqual(posted, {"parents": 1, "attachments": 1})

    def test_a_second_create_for_the_same_pdf_never_posts_again(self):
        with self._world():
            self._create()
        with self._world() as posted:
            r = self._create()
        self.assertEqual(r.status_code, 409)
        self.assertIn("PARENT01", r.get_json()["message"])
        self.assertEqual(posted, {"parents": 0, "attachments": 0})

    def test_a_failed_attachment_leaves_the_parent_resumable(self):
        """The middle state: the item exists, the link does not. A retry must
        attach to the parent we already have, never mint a second one."""
        with self._world(att_key=None) as posted:
            r = self._create()
            self.assertFalse(r.get_json()["ok"])
            self.assertEqual(r.get_json()["item_key"], "PARENT01")
            self.assertEqual(posted["parents"], 1)

        with mock.patch("tagging.writers.zotero_write.get_item",
                        return_value=({"itemType": "journalArticle",
                                       "title": "A paper"}, 9)), \
                self._world() as posted:
            r = self._create()
        self.assertEqual(r.status_code, 409)
        self.assertFalse(r.get_json()["ok"])
        self.assertEqual(posted, {"parents": 0, "attachments": 0})

    def test_a_failed_parent_create_does_not_block_a_retry_forever(self):
        """A refusal we SAW is not the ambiguous case — but the reservation
        still stands, so the retry is reported rather than silently doubled."""
        with self._world(parent=None) as posted:
            r = self._create()
        self.assertFalse(r.get_json()["ok"])
        self.assertEqual(posted["parents"], 1)
        _id, stage, _retained = attempts.lookup(self.vault, self._sha())
        self.assertEqual(stage, attempts.STAGE_PARENT_UNKNOWN)

    def test_a_definitive_4xx_retires_as_never_created(self):
        """A 4xx from create_parent_item is a refusal we SAW: nothing was
        minted. Leaving it at parent_unknown blocked the PDF behind a 6-min
        settle (2026-09-19 review A5)."""
        with self._world(parent=None, parent_fail_status="refused",
                         parent_fail_code=403) as posted:
            r = self._create()
        self.assertFalse(r.get_json()["ok"])
        self.assertTrue(r.get_json().get("never_created"), r.get_json())
        self.assertEqual(posted["parents"], 1)
        _id, stage, _retained = attempts.lookup(self.vault, self._sha())
        self.assertEqual(stage, attempts.STAGE_FRESH)


class TestParentPostRefusalCodes(unittest.TestCase):
    """Which HTTP codes prove nothing was minted.

    `refused` is what lets the caller retire the reservation, and a retired
    reservation is what lets a LATER attempt send a second unkeyed POST. So
    the set has to be codes Zotero itself returns before writing — not "any
    4xx", which swept in 408 and 409, either of which a proxy can return
    after the body already reached Zotero (2026-09-20).
    """

    def _post(self, code):
        from tagging import writers, zotero_write

        prepared = {"commit": {
            "transport": SimpleNamespace(base_url="https://api.zotero.org",
                                         is_web=True, kind="web"),
            "api_key": "k", "lib_path": "users/1",
            "item_type": "journalArticle", "fields": {}, "creators": []}}
        with mock.patch.object(
                zotero_write, "create_parent_item",
                side_effect=zotero_write.ZoteroError(f"HTTP {code}", http_code=code)):
            result, parent = writers.zotero_item_create_post(prepared)
        self.assertIsNone(parent)
        return result["status"]

    def test_a_code_zotero_returns_before_writing_is_refused(self):
        for code in (400, 403, 404, 422, 501):
            with self.subTest(code=code):
                self.assertEqual(self._post(code), "refused")

    def test_a_timeout_or_conflict_is_not_proof_that_nothing_was_minted(self):
        for code in (408, 409, 423, 425, 451):
            with self.subTest(code=code):
                self.assertEqual(self._post(code), "error")

    def test_a_5xx_is_not_refused(self):
        for code in (500, 502, 504):
            with self.subTest(code=code):
                self.assertEqual(self._post(code), "error")


class TestProposeFromCache(unittest.TestCase):
    """`zotcreate/propose.py` — the offline draft, and the tier gate.

    The gate is the point. On the live library 162 of 298 pending PDFs carry
    a `filename_only` resolution, which is a Crossref search on a surname and
    a year: one sampled row offers three unrelated papers by three different
    people named "Abbot". Filling the form from one would create an item for
    a DIFFERENT paper with the user's PDF linked to it, and the form would
    look filled-in and plausible.
    """

    JOURNAL = {"source": "crossref", "title": "A paper", "year": 2024,
               "doi": "10.1/x", "container": "Journal of Things",
               "url": "https://doi.org/10.1/x",
               "authors": ["Jardri, Renaud", "Dubé, François"]}
    ARXIV = {"source": "arxiv", "title": "A preprint", "year": 2025,
             "arxiv_id": "2501.00001", "authors": ["Doe, Jane"]}

    def _res(self, tier, outcome="candidates", cands=None):
        return {"outcome": outcome, "tier": tier,
                "candidates": cands if cands is not None else [self.JOURNAL]}

    def test_an_identifier_tier_fills_the_form(self):
        d = propose.draft_from_resolution(self._res("xmp_doi"), klass="publisher")
        self.assertEqual(d["prefilled_from"], "candidate")
        self.assertEqual(d["item_type"], "journalArticle")
        self.assertEqual(d["fields"]["DOI"], "10.1/x")
        self.assertEqual(d["fields"]["publicationTitle"], "Journal of Things")
        self.assertEqual(d["fields"]["date"], "2024")
        self.assertEqual(d["creators"][0],
                         {"creatorType": "author", "lastName": "Jardri",
                          "firstName": "Renaud"})
        self.assertEqual(d["suggestions"], [])

    def test_a_filename_only_guess_never_fills_the_form(self):
        d = propose.draft_from_resolution(
            self._res("filename_only"), klass="publisher",
            fallback_title="What page 1 actually says")
        self.assertEqual(d["prefilled_from"], "pdf")
        self.assertEqual(d["fields"]["title"], "What page 1 actually says")
        self.assertNotIn("DOI", d["fields"])
        self.assertEqual(d["creators"], [])
        # …but the guess is still OFFERED, so the user can pick it knowingly.
        self.assertEqual(len(d["suggestions"]), 1)

    def test_a_title_only_match_is_a_suggestion_not_a_prefill(self):
        d = propose.draft_from_resolution(self._res("title_only"), klass=None)
        self.assertEqual(d["prefilled_from"], "pdf")
        self.assertEqual(len(d["suggestions"]), 1)

    def test_an_ambiguous_resolution_offers_every_candidate_and_fills_none(self):
        d = propose.draft_from_resolution(
            {"outcome": "ambiguous", "tier": "ambiguous",
             "candidates": [self.JOURNAL, self.ARXIV]}, klass=None)
        self.assertEqual(d["prefilled_from"], "pdf")
        self.assertEqual(len(d["suggestions"]), 2)

    def test_the_basis_line_names_the_risk_not_the_tier(self):
        d = propose.draft_from_resolution(self._res("filename_only"), klass=None)
        self.assertIn("different paper", d["basis"])

    def test_with_no_resolution_at_all_the_pdf_still_fills_what_it_knows(self):
        d = propose.draft_from_resolution(
            None, klass="browser_print", fallback_title="A printed page",
            source_url="https://example.org/p")
        self.assertEqual(d["item_type"], "webpage")
        self.assertEqual(d["fields"]["url"], "https://example.org/p")
        self.assertEqual(d["suggestions"], [])

    def test_item_type_prefers_the_registrys_evidence_over_the_class(self):
        """A journal name is a positive statement that this is an article; a
        physical class is only what the file looks like."""
        self.assertEqual(
            propose.item_type_for(self.JOURNAL, "office_export"),
            "journalArticle")
        self.assertEqual(propose.item_type_for(self.ARXIV, "book"), "preprint")
        self.assertEqual(propose.item_type_for(None, "book"), "book")

    def test_a_one_token_author_is_an_organisation_not_a_guessed_surname(self):
        out = propose.creators_from({"authors": ["World Health Organization"]})
        self.assertEqual(out, [{"creatorType": "author",
                                "name": "World Health Organization"}])

    def test_empty_candidate_values_are_omitted_rather_than_blanked(self):
        out = propose.fields_from({"title": "T", "doi": None, "container": ""})
        self.assertEqual(out, {"title": "T"})

    def test_picking_a_suggestion_maps_it_exactly_like_a_prefill(self):
        """The difference between the two is who chose it, not how it reads."""
        picked = propose.suggestion_draft(self.JOURNAL, klass=None)
        auto = propose.draft_from_resolution(self._res("xmp_doi"), klass=None)
        self.assertEqual(picked["fields"], auto["fields"])
        self.assertEqual(picked["creators"], auto["creators"])
        self.assertEqual(picked["item_type"], auto["item_type"])


class TestFilenameFallback(unittest.TestCase):
    """`from_filename_stem` — the last offline source, for the 214 pending
    PDFs no candidate could identify.

    It must NOT be the bridge's join key: that one normalises for MATCHING
    (NFKD, punctuation stripped, lowercased), which is correct as a key and
    wrong as a name — and this value is written into a record a human reads.
    """

    def test_the_vault_convention_yields_a_first_author_and_a_year(self):
        self.assertEqual(propose.from_filename_stem("smith_2020"),
                         {"lastName": "Smith", "date": "2020",
                          "more_authors": False})

    def test_a_hyphenated_surname_keeps_its_hyphen(self):
        """The bridge's key for this is `dasmunshi`."""
        self.assertEqual(propose.from_filename_stem("das-munshi_2019")["lastName"],
                         "Das-Munshi")

    def test_a_diacritic_survives(self):
        """The bridge's key for this is `lvvik` — NFKD drops the ø."""
        self.assertEqual(propose.from_filename_stem("løvvik_2014")["lastName"],
                         "Løvvik")

    def test_only_the_first_author_is_taken_and_the_rest_is_disclosed(self):
        got = propose.from_filename_stem("smith_jones_2018")
        self.assertEqual(got["lastName"], "Smith")
        self.assertTrue(got["more_authors"])

    def test_a_duplicate_marker_is_not_part_of_the_surname(self):
        self.assertEqual(propose.from_filename_stem("2 taylor_2024")["lastName"],
                         "Taylor")

    def test_a_name_that_does_not_follow_the_convention_yields_nothing(self):
        self.assertEqual(propose.from_filename_stem("a_paper"), {})
        self.assertEqual(propose.from_filename_stem("_2019"), {})

    def test_the_draft_uses_it_only_when_no_candidate_filled_the_form(self):
        weak = {"outcome": "candidates", "tier": "filename_only",
                "candidates": [{"title": "Some other paper", "doi": "10.9/z"}]}
        d = propose.draft_from_resolution(weak, klass=None,
                                          fallback_title="Page 1 title",
                                          filename_stem="smith_2020")
        self.assertTrue(d["from_filename"])
        self.assertEqual(d["fields"]["date"], "2020")
        self.assertEqual(d["creators"],
                         [{"creatorType": "author", "lastName": "Smith",
                           "firstName": ""}])
        # …and the weak candidate still did not fill anything.
        self.assertNotIn("DOI", d["fields"])

        strong = {"outcome": "candidates", "tier": "xmp_doi",
                  "candidates": [{"title": "The real paper", "year": 1999,
                                  "doi": "10.1/x", "authors": ["Real, A"]}]}
        d2 = propose.draft_from_resolution(strong, klass=None,
                                           filename_stem="smith_2020")
        self.assertFalse(d2["from_filename"])
        self.assertEqual(d2["fields"]["date"], "1999")   # the record, not the name


class TestWithdrawnRecords(unittest.TestCase):
    """A DOI can be real while its record is gone.

    Measured on the live library 2026-09-11: 28 of the 34 identifier-grade
    pending PDFs resolve to Crossref's "Listing of Deleted DOIs" — a valid
    EM-Consulte DOI whose record was withdrawn, so it carries no title, no
    authors and the year 2000. This arrived through the tier the prefill
    trusts MOST, and wrote that container name and that year into a form for
    a file whose own name says 2017.
    """

    WITHDRAWN = {"source": "crossref", "title": None, "authors": [],
                 "year": 2000, "doi": "10.1016/s1634-6939(17)48034-3",
                 "container": "CrossRef Listing of Deleted DOIs",
                 "url": "https://doi.org/10.1016/s1634-6939(17)48034-3"}

    def test_a_titleless_record_never_fills_the_form_whatever_its_tier(self):
        d = propose.draft_from_resolution(
            {"outcome": "candidates", "tier": "xmp_doi",
             "candidates": [self.WITHDRAWN]},
            klass="publisher", fallback_doi="10.1016/s1634-6939(17)48034-3",
            filename_stem="lemarie_2017")
        self.assertEqual(d["prefilled_from"], "pdf")
        self.assertNotIn("publicationTitle", d["fields"])
        # The file's own name says 2017; the withdrawn record said 2000.
        self.assertEqual(d["fields"]["date"], "2017")
        # The DOI still reaches the form — it came from the FILE, not from
        # this record, and it is the one thing that is definitely right.
        self.assertEqual(d["fields"]["DOI"], "10.1016/s1634-6939(17)48034-3")

    def test_the_registry_placeholder_is_never_written_as_a_journal_name(self):
        out = propose.fields_from({"title": "T", "container":
                                   "CrossRef Listing of Deleted DOIs"})
        self.assertNotIn("publicationTitle", out)
        self.assertEqual(out["title"], "T")


class TestResumeKeepsItsUndo(unittest.TestCase):
    """A create that could not be verified must still be undoable.

    `zotero_item_create_post` holds the POST's own acknowledgement — the
    server's `data` object and its version — even when the follow-up readback
    fails. Dropping it makes the attempt resumable but its restore op
    permanently unverifiable, so the batch reports a clean success while the
    undo it promises refuses forever.
    """

    def test_an_unverified_post_keeps_its_creation_evidence(self):
        from tagging import writers, zotero_write

        created = zotero_write.CreatedItemKey(
            "PARENT01", {"key": "PARENT01", "itemType": "book"}, 9)
        prepared = {"commit": {
            "transport": SimpleNamespace(base_url="https://api.zotero.org",
                                         kind="web", is_web=True, api_key="k",
                                         can_write=True),
            "api_key": "k", "lib_path": "users/1", "item_type": "book",
            "fields": {"title": "T"}, "creators": []}}
        with mock.patch.object(writers.zotero_write, "create_parent_item",
                               return_value=created), \
                mock.patch.object(writers.zotero_write, "get_item",
                                  side_effect=zotero_write.ZoteroUnavailable("down")), \
                mock.patch("core.utils.log_vault_write"):
            result, parent = writers.zotero_item_create_post(prepared)
        self.assertIsNone(parent)
        self.assertEqual(result["item_key"], "PARENT01")
        # The evidence the POST already gave us must survive the failed read,
        # or the resumed create's undo can never be authorised.
        self.assertEqual(result["creation_data"],
                         {"key": "PARENT01", "itemType": "book"})
        self.assertEqual(result["version"], 9)


class TestResumeJournalsAVerifiableOp(_CreateRouteHarness):
    """End to end: an unverified create, resumed, must journal a restore op
    the revert can actually authorise."""

    def test_the_resumed_create_carries_a_usable_creation_baseline(self):
        unverified = {"ok": False, "status": "unverified", "item_key": "PARENT01",
                      "version": 9, "creation_data": {"key": "PARENT01"},
                      "message": "created but not read back"}
        with self._world(parent_result=unverified) as posted:
            r = self._create()
            self.assertFalse(r.get_json()["ok"])
            self.assertEqual(posted["parents"], 1)

        with mock.patch("tagging.writers.zotero_write.get_item",
                        return_value=({"itemType": "journalArticle",
                                       "title": "A paper"}, 9)), \
                self._world() as posted:
            r = self._create()
            self.assertEqual(posted["parents"], 0)     # no second parent
            self.assertFalse(self.saved.called)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(posted['attachments'], 0)
        # Incomplete/changed original parent evidence cannot grant undo or resume.



class TestParentPassesTheRealAttachGate(unittest.TestCase):
    """The claim PR1 rests on, exercised rather than asserted.

    `NewParent` exists so a just-created item goes through the SAME
    `zotero_attachment_create_prepare` gate a scanned item does — identity
    re-verified, group library refused, server_id compared. The route tests
    mock both writers, so nothing there would notice if the stand-in failed
    that gate; only the HTTP layer is mocked here.
    """

    def _run(self, *, title="A paper", readback_title=None, group=False):
        from tagging import local_api, writers, zotero_write

        transport = SimpleNamespace(base_url="https://api.zotero.org",
                                    kind="web", is_web=True, api_key="k",
                                    can_write=True)
        created = zotero_write.CreatedItemKey(
            "PARENT01", {"key": "PARENT01", "itemType": "journalArticle"}, 9)
        item = {"itemType": "journalArticle",
                "title": readback_title if readback_title is not None else title,
                "DOI": "10.1/x"}
        att = zotero_write.CreatedItemKey("ATTACH01", {"key": "ATTACH01"}, 10)
        with mock.patch.object(writers.zotero_write, "resolve_transport",
                               return_value=transport), \
                mock.patch.object(writers.zotero_write, "resolve_user_id",
                                  return_value=1), \
                mock.patch.object(writers.zotero_write, "create_parent_item",
                                  return_value=created), \
                mock.patch.object(writers.zotero_write, "get_item",
                                  return_value=(item, local_api.Version(9, None))), \
                mock.patch.object(writers.zotero_write,
                                  "create_linked_file_attachment",
                                  return_value=att) as mk_att, \
                mock.patch("core.utils.log_vault_write"):
            prepared = writers.zotero_item_create_prepare(
                {}, item_type="journalArticle",
                fields={"title": title, "DOI": "10.1/x"}, creators=[])
            self.assertIn("commit", prepared, prepared)
            result, parent = writers.zotero_item_create_post(prepared)
            if parent is None:
                return result, None, None
            if group:
                parent.library_type, parent.group_id = "group", 484650
            att_prepared = writers.zotero_attachment_create_prepare(
                {}, parent, Path("/v/biblio_articles/a.pdf"))
            if "early" in att_prepared:
                return result, att_prepared["early"], None
            att_result, key, _v = writers.zotero_attachment_create_post(att_prepared)
            return result, att_result, (key, mk_att.call_args)

    def test_a_new_parent_resolves_the_personal_library_on_both_writers(self):
        created, att_result, (key, call) = self._run()
        self.assertEqual(created["item_key"], "PARENT01")
        self.assertTrue(att_result["ok"])
        self.assertEqual(str(key), "ATTACH01")
        # The web transport needs the real numeric userID; the magic users/0
        # the local API accepts is 404 on api.zotero.org.
        self.assertEqual(call.args[1], "users/1")
        self.assertEqual(call.args[2], "PARENT01")

    def test_a_parent_that_reads_back_as_something_else_is_not_linked(self):
        created, att_result, _ = self._run(readback_title="Not what we sent")
        self.assertEqual(created["status"], "unverified")
        self.assertEqual(created["item_key"], "PARENT01")
        self.assertIsNone(att_result)
        # …and the POST's own evidence is kept, or the resume loses its undo.
        self.assertEqual(created["creation_data"],
                         {"key": "PARENT01", "itemType": "journalArticle"})

    def test_the_gate_still_refuses_a_group_library(self):
        """Linked files cannot be used in a group library (spike S2, live
        403). The stand-in must not be a way round that."""
        _created, early, _ = self._run(group=True)
        self.assertEqual(early["reason"], "group_library")


class TestWebCreateNeedsACompleteObservation(_CreateRouteHarness):
    """The Web transport's absence proof.

    Until 2026-09-17 `_create_one` refused the Web transport outright: a
    local Zotero database may lag a sync, and "I did not find it" from a
    lagging copy is exactly the evidence a duplicate barrier must not
    accept. The replacement is not a weaker check but a different one —
    `web_snapshot.observe`, whose completeness is PROVEN (Total-Results and
    a stable Last-Modified-Version across every page of items and trash,
    then a re-probe of both), and `admission`, which refuses on a DOI
    already present including trash, an attachment already pointing at this
    exact file, an unresolved relative linked path, or an imported copy
    whose checksum is unknown or identical.

    Each test here asks whether a specific way of getting that wrong would
    be caught, not whether the happy path runs.
    """

    def _web_world(self, **kw):
        web = SimpleNamespace(base_url="https://api.zotero.org", kind="web",
                              is_web=True, api_key="k", can_write=True)
        outer = self._world(**kw)

        @contextlib.contextmanager
        def _cm():
            with outer as posted, mock.patch(
                    "tagging.writers.zotero_item_create_prepare",
                    return_value={"commit": {
                        "transport": web, "api_key": "k",
                        "lib_path": "users/7666987",
                        "item_type": "journalArticle", "fields": {},
                        "creators": []}}):
                yield posted
        return _cm()

    def _create_web(self, **over):
        over.setdefault("target", {"base": "https://api.zotero.org",
                                   "library": "users/7666987"})
        return self._create(**over)

    @staticmethod
    def _snapshot(items=None, *, complete=True, library="users/7666987"):
        import time as _t
        return {"base": "https://api.zotero.org", "library": library,
                "checked_at": _t.time(), "version": 119935, "complete": complete,
                "counts": {"items": len(items or {}), "items/trash": 0},
                "items": dict(items or {})}

    def test_a_clean_complete_observation_admits_the_create(self):
        with self._web_world() as posted, \
                mock.patch("accession.web_snapshot.observe",
                           return_value=self._snapshot()) as obs:
            r = self._create_web()
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(posted["parents"], 1)
        self.assertEqual(posted["attachments"], 1)
        # EXACTLY one complete observation, immediately before the
        # reservation. It briefly ran twice back to back (2026-09-19 A4's
        # first cut); on the live library that is a second multi-minute read
        # of every page of items and trash, proving what the first proved
        # microseconds earlier (2026-09-20).
        self.assertEqual(obs.call_count, 1)

    def test_an_observation_that_cannot_prove_completeness_refuses(self):
        """`admission` raises on an incomplete snapshot; that must stop the
        create rather than fall through to a POST."""
        with self._web_world() as posted, \
                mock.patch("accession.web_snapshot.observe",
                           return_value=self._snapshot(complete=False)):
            r = self._create_web()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(posted["parents"], 0)

    def test_a_library_that_cannot_be_read_refuses_and_posts_nothing(self):
        from tagging.zotero_write import ZoteroUnavailable
        with self._web_world() as posted, \
                mock.patch("accession.web_snapshot.observe",
                           side_effect=ZoteroUnavailable("down")):
            r = self._create_web()
        self.assertGreaterEqual(r.status_code, 400)
        self.assertEqual(posted["parents"], 0)

    def test_a_failed_absence_proof_reserves_nothing(self):
        """The absence proof used to run AFTER ``attempts.reserve``; a page
        timeout then left the PDF at ``parent_unknown`` — blocked behind a
        six-minute settle — though nothing had been sent (2026-09-19 A4).

        Red if the proof moves back after the reservation: the stage would be
        ``parent_unknown``, not ``fresh``."""
        from tagging.zotero_write import ZoteroUnavailable

        with self._web_world() as posted, \
                mock.patch("accession.web_snapshot.observe",
                           side_effect=ZoteroUnavailable("timed out paging items")):
            r = self._create_web()
        self.assertEqual(posted["parents"], 0)
        self.assertGreaterEqual(r.status_code, 400)
        _id, stage, _retained = attempts.lookup(self.vault, self._sha())
        self.assertEqual(stage, attempts.STAGE_FRESH)

    def test_a_doi_already_in_the_library_refuses(self):
        items = {"AAAA1111": {"itemType": "journalArticle",
                              "DOI": "10.1/dup", "deleted": False}}
        with self._web_world() as posted, \
                mock.patch("accession.web_snapshot.observe",
                           return_value=self._snapshot(items)):
            r = self._create_web(fields={"title": "A paper", "DOI": "10.1/dup"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("DOI", r.get_json()["error"])
        self.assertEqual(posted["parents"], 0)

    def test_a_doi_in_the_TRASH_also_refuses(self):
        """Trash is the half a lagging local database is worst at, and a
        trashed duplicate is still a duplicate once restored."""
        items = {"AAAA1111": {"itemType": "journalArticle",
                              "DOI": "10.1/dup", "deleted": True}}
        with self._web_world() as posted, \
                mock.patch("accession.web_snapshot.observe",
                           return_value=self._snapshot(items)):
            r = self._create_web(fields={"title": "A paper", "DOI": "10.1/dup"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(posted["parents"], 0)

    def test_a_file_already_linked_in_the_library_refuses(self):
        items = {"BBBB2222": {"itemType": "attachment", "linkMode": "linked_file",
                              "path": None, "deleted": False}}
        items["BBBB2222"]["path"] = str(self.pdf)
        with self._web_world() as posted, \
                mock.patch("accession.web_snapshot.observe",
                           return_value=self._snapshot(items)):
            r = self._create_web()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(posted["parents"], 0)

    def test_our_own_curation_still_refuses_whatever_zotero_holds(self):
        """The observation answers "does Zotero have it"; mapping.json
        answers "have I already decided about it". Both must gate."""
        self.settings.mapping_file.write_text(
            '{"matches": {"someKey2020": ["biblio_articles/smith_2020.pdf"]}}',
            encoding="utf-8")
        with self._web_world() as posted, \
                mock.patch("accession.web_snapshot.observe",
                           return_value=self._snapshot()):
            r = self._create_web()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(posted["parents"], 0)

    def test_an_exclusion_alone_does_not_refuse_the_create(self):
        """An exclusion means "not THAT record" — it stays in mapping.json
        and keeps gating the automatic ladder, but it is not a decision
        against creating A record (maintainer decision 2026-09-18)."""
        self.settings.mapping_file.write_text(
            '{"exclude": {"biblio_articles/smith_2020.pdf": ["wrongKey2020"]},'
            ' "pending": ["biblio_articles/smith_2020.pdf"]}', encoding="utf-8")
        with self._web_world() as posted, \
                mock.patch("accession.web_snapshot.observe",
                           return_value=self._snapshot()):
            r = self._create_web()
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(posted["parents"], 1)

    def test_not_for_zotero_still_refuses_the_create(self):
        self.settings.mapping_file.write_text(
            '{"no_match": ["biblio_articles/smith_2020.pdf"]}', encoding="utf-8")
        with self._web_world() as posted, \
                mock.patch("accession.web_snapshot.observe",
                           return_value=self._snapshot()):
            r = self._create_web()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(posted["parents"], 0)

    def test_on_desktop_an_exclusion_alone_does_not_refuse_the_create(self):
        self.settings.mapping_file.write_text(
            '{"exclude": {"biblio_articles/smith_2020.pdf": ["wrongKey2020"]},'
            ' "pending": ["biblio_articles/smith_2020.pdf"]}', encoding="utf-8")
        from accession import reconciliation
        from audit.engine import bridge
        state = reconciliation.mapping_state(
            bridge._load_mapping_all(self.settings.mapping_file),
            "biblio_articles/smith_2020.pdf")
        self.assertEqual(state["exclude"], ["wrongKey2020"])
        with self._world(row_mapping=state) as posted:
            r = self._create()
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(posted["parents"], 1)

    def test_on_desktop_not_for_zotero_still_refuses_the_create(self):
        with self._world(row_mapping={'keys': [], 'no_match': True,
                                      'exclude': [], 'pending': False}) as posted:
            r = self._create()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(posted["parents"], 0)

    def test_a_target_that_moved_since_the_preview_refuses(self):
        with self._web_world() as posted, \
                mock.patch("accession.web_snapshot.observe",
                           return_value=self._snapshot(library="users/999")):
            r = self._create_web()
        self.assertGreaterEqual(r.status_code, 400)
        self.assertEqual(posted["parents"], 0)

    def test_the_local_desktop_path_is_untouched(self):
        """The Web branch must not change what the Desktop transport does —
        its preflight still reads the local reconciliation workspace."""
        with self._world() as posted, \
                mock.patch("accession.web_snapshot.observe",
                           side_effect=AssertionError("must not observe on local")):
            r = self._create()
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(posted["parents"], 1)


# --- T2: batch create against ONE Web observation --------------------------

import json as _json  # noqa: E402


def _frames(resp):
    out = []
    for line in resp.get_data(as_text=True).splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            out.append(_json.loads(line[6:]))
    return out


class TestBatchProof(unittest.TestCase):
    """`BatchProof` is the whole absence argument for rows 2..N: a snapshot
    complete at V plus every change in (V, V'] is complete at V' — but only
    if every one of those changes is ours."""

    @staticmethod
    def _snap(version=100, items=None):
        import time as _t
        return {"base": "https://api.zotero.org", "library": "users/1", "complete": True,
                "checked_at": _t.time() - 800, "version": version,
                "items": dict(items or {})}

    def _proof(self, *changes):
        from accession import web_snapshot
        queue = list(changes)

        def fetch(since):
            changed, removed, bump = queue.pop(0)
            return {"since": since, "version": since + bump, "changed": changed,
                    "removed": removed}
        return web_snapshot.BatchProof(self._snap(), fetch)

    def test_our_own_creates_are_folded_in_and_the_snapshot_is_fresh_again(self):
        proof = self._proof(({}, [], 0), ({"P1": {"itemType": "book", "DOI": "10.1/x"},
                                           "A1": {"itemType": "attachment"}}, [], 2))
        proof.refresh()
        proof.claim("P1", "A1")
        snap = proof.refresh()
        self.assertEqual(snap["version"], 102)
        self.assertIn("P1", snap["items"])
        import time as _t
        self.assertLess(_t.time() - snap["checked_at"], 5)

    def test_an_item_we_did_not_create_stops_the_batch(self):
        from accession import web_snapshot
        proof = self._proof(({"ZZZZ9999": {"itemType": "book"}}, [], 1))
        with self.assertRaises(web_snapshot.ForeignChange):
            proof.refresh()

    def test_a_write_back_to_our_own_item_that_trashes_it_stops_the_batch(self):
        from accession import web_snapshot
        proof = self._proof(({"P1": {"itemType": "book", "deleted": True}}, [], 1))
        proof.claim("P1")
        with self.assertRaises(web_snapshot.ForeignChange):
            proof.refresh()

    def test_a_write_back_to_an_already_snapshotted_own_item_stops_the_batch(self):
        """Better BibTeX writes citationKey onto a parent this batch already
        snapshotted. That is an edit, not a create, and must stop the batch
        (2026-09-19 review A16)."""
        from accession import web_snapshot
        proof = web_snapshot.BatchProof(
            self._snap(items={"P1": {"itemType": "book"}}),
            lambda since: {"since": since, "version": since + 1, "removed": [],
                           "changed": {"P1": {"itemType": "book", "citationKey": "x"}}})
        proof.claim("P1")
        with self.assertRaises(web_snapshot.ForeignChange):
            proof.refresh()

    def test_a_permanent_deletion_stops_the_batch(self):
        from accession import web_snapshot
        proof = self._proof(({}, ["OLDKEY01"], 1))
        with self.assertRaises(web_snapshot.ForeignChange):
            proof.refresh()

    def test_a_delta_read_across_two_versions_refuses(self):
        from accession import web_snapshot
        from tagging import zotero_write
        answers = iter([([], 0, 100), ([], 0, 101), ({"items": []}, None, 101)])
        with mock.patch.object(web_snapshot, "_delta_get",
                               side_effect=lambda *a, **k: next(answers)):
            with self.assertRaises(zotero_write.ZoteroError):
                web_snapshot.delta("https://api.zotero.org", "users/1", 99, api_key="k")


class TestBatchCreateRoute(_CreateRouteHarness):
    """The batch route's stop rules, each proven red against its removal."""

    def setUp(self):
        super().setUp()
        self.pdfs = {}
        for name in ("aa_2020", "bb_2021", "cc_2022"):
            p = self.vault / "biblio_articles" / f"{name}.pdf"
            p.write_bytes(f"%PDF-1.4 {name}".encode())
            self.pdfs[f"biblio_articles/{name}.pdf"] = p

    @contextlib.contextmanager
    def _batch_world(self, *, deltas=None, **kw):
        web = SimpleNamespace(base_url="https://api.zotero.org", kind="web",
                              is_web=True, api_key="k", can_write=True)
        counter = {"deltas": 0, "observe": 0}
        script = list(deltas or [])

        def _delta(base, lib, since, api_key=None):
            counter["deltas"] += 1
            changed = script.pop(0) if script else {}
            if changed == "own":
                # Each create mints a new key (see _post_parent). Replaying
                # PARENT01 on a later row is a write-back to an item already
                # in the snapshot, which A16 stops the batch for.
                n = posted["parents"]
                changed = {f"PARENT{n:02d}": {"itemType": "journalArticle"},
                           f"ATTACH{n:02d}": {"itemType": "attachment"}}
            return {"since": since, "version": since + (2 if changed else 0),
                    "changed": changed, "removed": []}

        def _observe(*a, **k):
            counter["observe"] += 1
            return TestWebCreateNeedsACompleteObservation._snapshot()

        def _post_parent(prepared):
            from tagging import zotero_write as zw
            from tagging.zotero_write import CreatedItemKey
            if prepared["commit"].get("before_dispatch"):
                try:
                    prepared["commit"]["before_dispatch"]()
                except zw.ZoteroUnavailable:
                    return {"ok": False, "status": "unreachable",
                            "message": "Zotero response unavailable."}, None
            posted["parents"] += 1
            if kw.get("parent") is None and "parent" in kw:
                return {"ok": False, "status": kw.get("parent_fail_status") or "error",
                        "http_code": kw.get("parent_fail_code"),
                        "message": "Zotero refused."}, None
            key = f"PARENT{posted['parents']:02d}"
            prepared["commit"]["acknowledge"](
                CreatedItemKey(key, {"key": key}, 9))
            if kw.get("parent_result") is not None:
                return dict(kw["parent_result"]), None
            return ({"ok": True, "status": "created", "item_key": key,
                     "version": 9, "creation_data": {"key": key}},
                    _Parent(key))

        def _post_att(prepared):
            prepared["commit"]["before_dispatch"]()
            posted["attachments"] += 1
            if kw.get("att_key", "ATTACH01") is None:
                return {"ok": False, "status": "error",
                        "message": "Attach failed."}, None, None
            key = f"ATTACH{posted['parents']:02d}"
            prepared["commit"]["acknowledge"](key)
            return {"ok": True, "status": "attached"}, key, 10

        with self._world(**kw) as posted, \
                mock.patch("tagging.writers.zotero_item_create_prepare",
                           return_value={"commit": {
                               "transport": web, "api_key": "k",
                               "lib_path": "users/7666987",
                               "item_type": "journalArticle", "fields": {},
                               "creators": []}}), \
                mock.patch("tagging.writers.zotero_item_create_post",
                           _post_parent), \
                mock.patch("tagging.writers.zotero_attachment_create_post",
                           _post_att), \
                mock.patch("tagging.zotero_write.resolve_transport", return_value=web), \
                mock.patch("api.routes.zotcreate._resolve",
                           side_effect=lambda inv, s, raw: (
                               (self.pdfs[raw], raw) if raw in self.pdfs else None)), \
                mock.patch("accession.web_snapshot.observe", side_effect=_observe), \
                mock.patch("accession.web_snapshot.delta", side_effect=_delta):
            posted["counter"] = counter
            yield posted

    def _batch(self, rows=None, **over):
        rows = rows or [{"id": rel, "pdf": rel, "item_type": "journalArticle",
                         "fields": {"title": f"Paper {i}"}}
                        for i, rel in enumerate(self.pdfs)]
        body = {"rows": rows, "confirm": True}
        body.update(over)
        return self.client.post("/api/zotcreate/batch", json=body, headers=_HEADERS)

    @staticmethod
    def _by_status(frames):
        out = {}
        for f in frames:
            if "row" in f:
                out[f["row"]["id"]] = f["row"]["status"]
        return out

    @staticmethod
    def _terminal(frames):
        return next(f["zotcreate_batch"] for f in frames if "zotcreate_batch" in f)

    def test_one_observation_serves_every_row(self):
        with self._batch_world(deltas=[{}, "own", "own"]) as posted, \
                mock.patch("api.routes.pending.workspace") as workspace:
            frames = _frames(self._batch())
        # The per-create reconciliation rebuild (~42 MB, minutes) is skipped.
        workspace.assert_not_called()
        self.assertEqual(set(self._by_status(frames).values()), {"created"})
        self.assertEqual(posted["parents"], 3)
        self.assertEqual(posted["counter"]["observe"], 1)
        term = self._terminal(frames)
        self.assertTrue(term["ok"], term)
        # Small: never the ~42 MB reconciliation payload.
        self.assertNotIn("reconciliation", _json.dumps(frames))

    def test_a_foreign_change_stops_the_batch_before_the_next_create(self):
        with self._batch_world(deltas=[{}, {"FOREIGN1": {"itemType": "book"}}]) as posted:
            frames = _frames(self._batch())
        self.assertEqual(posted["parents"], 1)
        term = self._terminal(frames)
        self.assertFalse(term["ok"])
        self.assertIn("did not cause", term["stopped"])
        self.assertEqual(term["not_reached"], 2)
        # The stopped row reserved nothing: it can simply be re-run.
        from accession.review import identity
        sha = identity(self.pdfs["biblio_articles/bb_2021.pdf"])["sha256"]
        self.assertEqual(attempts.lookup(self.vault, sha)[1], attempts.STAGE_FRESH)

    def test_an_in_batch_duplicate_doi_is_refused_and_the_batch_continues(self):
        rows = [{"id": rel, "pdf": rel, "item_type": "journalArticle",
                 "fields": {"title": f"Paper {i}", "DOI": "10.1/same" if i < 2 else ""}}
                for i, rel in enumerate(self.pdfs)]
        with self._batch_world(deltas=[{}, "own", "own"]) as posted:
            frames = _frames(self._batch(rows))
        st = self._by_status(frames)
        self.assertEqual(st["biblio_articles/aa_2020.pdf"], "created")
        self.assertEqual(st["biblio_articles/bb_2021.pdf"], "refused")
        self.assertEqual(st["biblio_articles/cc_2022.pdf"], "created")
        self.assertEqual(posted["parents"], 2)

    def test_a_refused_parent_post_stops_the_batch_at_one_pdf(self):
        with self._batch_world(deltas=[{}], parent=None) as posted:
            frames = _frames(self._batch())
        self.assertEqual(posted["parents"], 1)
        st = self._by_status(frames)
        self.assertEqual(st, {"biblio_articles/aa_2020.pdf": "failed"})
        term = self._terminal(frames)
        self.assertFalse(term["ok"])
        self.assertEqual(term["not_reached"], 2)

    def test_not_for_zotero_refuses_that_row_only(self):
        self.settings.mapping_file.write_text(
            '{"no_match": ["biblio_articles/aa_2020.pdf"]}', encoding="utf-8")
        with self._batch_world(deltas=[{}, {}, "own"]) as posted:
            frames = _frames(self._batch())
        st = self._by_status(frames)
        self.assertEqual(st["biblio_articles/aa_2020.pdf"], "refused")
        self.assertEqual(st["biblio_articles/cc_2022.pdf"], "created")
        self.assertEqual(posted["parents"], 2)

    def test_an_existing_mapping_refuses_that_row_only(self):
        self.settings.mapping_file.write_text(
            '{"matches": {"k2021": ["biblio_articles/bb_2021.pdf"]}}', encoding="utf-8")
        with self._batch_world(deltas=[{}, "own", "own"]) as posted:
            frames = _frames(self._batch())
        st = self._by_status(frames)
        self.assertEqual(st["biblio_articles/bb_2021.pdf"], "refused")
        self.assertEqual(posted["parents"], 2)

    def test_a_completed_pdf_is_skipped_on_resume(self):
        from accession.review import identity
        sha = identity(self.pdfs["biblio_articles/aa_2020.pdf"])["sha256"]
        ident, _stage, _ = attempts.reserve(
            self.vault, sha, pdf_rel="biblio_articles/aa_2020.pdf", item_type="journalArticle",
            title="Paper 0", library="users/7666987", base="https://api.zotero.org")
        attempts.acknowledge_parent(ident, key="OLDPAR01")
        attempts.acknowledge_attachment(ident, key="OLDATT01")
        with self._batch_world(deltas=[{}, "own"]) as posted:
            frames = _frames(self._batch())
        st = self._by_status(frames)
        self.assertEqual(st["biblio_articles/aa_2020.pdf"], "already_created")
        self.assertEqual(posted["parents"], 2)

    def test_a_dry_run_costs_one_observation_and_posts_nothing(self):
        items = {"LINK0001": {"itemType": "attachment", "linkMode": "linked_file",
                              "path": str(self.pdfs["biblio_articles/cc_2022.pdf"])}}
        snap = TestWebCreateNeedsACompleteObservation._snapshot(items)
        with self._batch_world() as posted, \
                mock.patch("accession.web_snapshot.observe", return_value=snap) as obs:
            frames = _frames(self._batch(confirm=None, dry_run=True))
        st = self._by_status(frames)
        self.assertEqual(st["biblio_articles/aa_2020.pdf"], "admitted")
        self.assertEqual(st["biblio_articles/cc_2022.pdf"], "refused")
        self.assertEqual(posted["parents"], 0)
        self.assertEqual(obs.call_count, 1)
        self.assertEqual(posted["counter"]["deltas"], 0)

    def test_the_desktop_transport_is_refused(self):
        local = SimpleNamespace(base_url="http://localhost:23119/api", kind="local",
                                is_web=False, api_key=None, can_write=True)
        with self._world() as posted, \
                mock.patch("tagging.zotero_write.resolve_transport", return_value=local):
            r = self._batch()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(posted["parents"], 0)

    def test_a_repeated_pdf_is_refused_before_anything_runs(self):
        rel = next(iter(self.pdfs))
        row = {"pdf": rel, "item_type": "journalArticle", "fields": {"title": "x"}}
        with self._batch_world() as posted:
            r = self._batch([row, dict(row)])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(posted["parents"], 0)


class TestWebResumeRereadsTheParent(unittest.TestCase):
    """A Web attempt stores no server id; requiring one made every Web
    `parent_done` resume refuse forever."""

    def test_a_web_parent_is_reread_without_a_server_id(self):
        from api.routes import zotcreate as zc
        web = SimpleNamespace(base_url="https://api.zotero.org", is_web=True)
        prepared = {"commit": {"transport": web, "lib_path": "users/1", "api_key": "k"}}
        data = {"key": "PARENT01", "itemType": "book", "title": "T", "version": 5}
        retained = {"parent_key": "PARENT01", "base": web.base_url, "library": "users/1",
                    "server_id": None, "parent": {"creation_data": dict(data)}}
        with mock.patch("tagging.zotero_write.get_item", return_value=(dict(data), 5)):
            parent = zc._reread_parent(prepared, retained)
        self.assertIsNotNone(parent)
        self.assertEqual(parent.item_key, "PARENT01")
