"""Contract for /api/zotlink/* — removing a file from a Zotero item.

The three properties worth pinning, all of them safety rather than behaviour:

1. **Trash, never delete.** The only write verb this path can reach is
   ``PATCH {"deleted": 1}``. A ``DELETE`` would destroy an ``imported_*``
   attachment's bytes, its key and its annotations, and re-creating it
   afterwards mints a new key — so the round trip would not be an undo.
2. **Annotations are a separate decision.** They hang off the attachment and go
   to the trash with it, so a row that has them needs its own acknowledgement
   on top of ``confirm``.
3. **The client cannot name an arbitrary Zotero item.** Only attachments the
   last scan saw are addressable, so a stray key reaches no network call.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import flask

from api.routes.zotlink import zotlink_bp

_HEADERS = {"X-Requested-With": "ChatEKLD"}


def _app():
    app = flask.Flask(__name__)
    app.register_blueprint(zotlink_bp)
    return app


def _row(item_key, *, annotations=0, owned=True, link_mode="imported_file"):
    return SimpleNamespace(
        item_key=item_key, parent_key="PARENT1", parent_title="A paper",
        parent_citation_key="smith2020", parent_item_type="journalArticle",
        link_mode=link_mode, content_type="application/pdf",
        filename=f"{item_key}.pdf", raw_path=None,
        annotation_count=annotations, file_owned_by_zotero=owned,
        library_type="user", group_id=None)



class _Resp:
    """Adapts the de-link SSE stream to the JSON shape these tests assert on.

    ``POST /api/zotlink/trash`` became a stream on 2026-08-30 so its network
    phase stops occupying a Waitress worker (and stops running under the vault
    op-lock). Pre-stream refusals — 400/404/409/429/503 — still answer with a
    real status code and a JSON body, so both shapes reach here.

    ``get_json()`` returns the terminal ``{"zotlink": ...}`` payload for a
    stream, or the ``{"error": ...}`` of an in-stream failure, so an assertion
    written against the old synchronous contract still reads naturally.
    """

    def __init__(self, raw_response):
        self.status_code = raw_response.status_code
        self.frames = []
        self._json = None
        if raw_response.mimetype == "text/event-stream":
            import json as _json
            for line in raw_response.get_data().decode("utf-8").splitlines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    self.frames.append(_json.loads(line[6:]))
            for f in self.frames:
                if "zotlink" in f:
                    self._json = f["zotlink"]
                elif "error" in f:
                    self._json = {"error": f["error"]}
        else:
            self._json = raw_response.get_json()

    def get_json(self):
        return self._json

    @property
    def infos(self):
        return [f["info"] for f in self.frames if "info" in f]


class _ZotlinkFixture(unittest.TestCase):
    """Fixtures only — no tests. Subclassed by the suites below.

    Split out 2026-08-30: the new suites need `setUp`, `_ctx` and `_post`, and
    subclassing the test-bearing class would silently re-run all of its cases
    under each new class name (slower, and a failure would be reported against
    a suite that never wrote it).
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zotlink-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.cfg = {"tagging_archive_dir": str(self.tmp / "archive")}
        self.client = _app().test_client()

    def _ctx(self, rows):
        import contextlib
        from audit.manager import audit_manager
        from rag.vault import obsidian_manager
        inv = SimpleNamespace(zotero_attachments=list(rows))
        settings = SimpleNamespace(vault_root=self.vault)
        st = contextlib.ExitStack()
        st.enter_context(mock.patch("api.routes.zotlink.load_config",
                                    return_value=self.cfg))
        st.enter_context(mock.patch.object(audit_manager, "get_inventory",
                                           return_value=(inv, settings)))
        st.enter_context(mock.patch.object(obsidian_manager, "try_acquire_lock",
                                           return_value=1))
        st.enter_context(mock.patch.object(obsidian_manager, "release_lock"))
        # The batch keeper ACTS on a False heartbeat by abandoning the run
        # (that is the point of it), and the real method cannot validate the
        # mocked epoch — so without this every batch stops before its first
        # write and the suite would assert against an empty run.
        st.enter_context(mock.patch.object(
            obsidian_manager, "heartbeat", return_value=True))
        return st

    def _post(self, body, rows, *, get_item=None, set_deleted=None):
        from tagging import zotero_write
        transport = SimpleNamespace(kind="web", base_url="https://api.zotero.org",
                                    api_key="k", is_web=True, can_write=True)
        with self._ctx(rows) as st:
            st.enter_context(mock.patch.object(
                zotero_write, "resolve_transport", return_value=transport))
            st.enter_context(mock.patch.object(
                zotero_write, "resolve_user_id", return_value=7))
            self.get_item = st.enter_context(mock.patch.object(
                zotero_write, "get_item",
                side_effect=get_item or (lambda *a, **k: (
                    {"itemType": "attachment", "linkMode": "imported_file",
                     "parentItem": "PARENT1", "filename": "x.pdf"}, 42))))
            self.set_deleted = st.enter_context(mock.patch.object(
                zotero_write, "set_item_deleted",
                side_effect=set_deleted or (lambda *a, **k: 43)))
            r = self.client.post("/api/zotlink/trash", json=body,
                                 headers=_HEADERS)
            # DRAIN INSIDE the patch scope. The SSE worker runs on a daemon
            # thread that only executes while the response is being consumed,
            # so draining after `with` exits would run it against the REAL
            # zotero_write / journal (the trap recorded in CLAUDE.md's Tests
            # section). Everything the worker resolves at call time — which is
            # all of it here — would silently fall through to the real thing.
            return _Resp(r)


class TestZotlink(_ZotlinkFixture):
    # --- gates ----------------------------------------------------------

    def test_requires_confirm(self):
        r = self._post({"item_keys": ["ATT1"]}, [_row("ATT1")])
        self.assertEqual(r.status_code, 400)
        self.set_deleted.assert_not_called()

    def test_404s_without_a_scan(self):
        from audit.manager import audit_manager
        with mock.patch.object(audit_manager, "get_inventory",
                               return_value=(None, None)):
            r = self.client.post("/api/zotlink/trash",
                                 json={"item_keys": ["ATT1"], "confirm": True},
                                 headers=_HEADERS)
        self.assertEqual(r.status_code, 404)

    def test_a_key_the_scan_never_saw_reaches_no_network_call(self):
        r = self._post({"item_keys": ["MADEUP"], "confirm": True}, [_row("ATT1")])
        self.assertEqual(r.status_code, 404)
        self.get_item.assert_not_called()
        self.set_deleted.assert_not_called()

    def test_rejects_a_malformed_item_key(self):
        for bad in ("../x", "a/b", "a b", ""):
            r = self._post({"item_keys": [bad], "confirm": True}, [_row("ATT1")])
            self.assertEqual(r.status_code, 400, bad)

    # --- the annotation gate --------------------------------------------

    def test_annotations_need_their_own_acknowledgement(self):
        r = self._post({"item_keys": ["ATT1"], "confirm": True},
                       [_row("ATT1", annotations=3)])
        self.assertEqual(r.status_code, 409)
        d = r.get_json()
        self.assertEqual(d["reason"], "would_trash_annotations")
        self.assertEqual(d["annotation_count"], 3)
        self.set_deleted.assert_not_called()

    def test_annotated_attachment_proceeds_once_acknowledged(self):
        r = self._post({"item_keys": ["ATT1"], "confirm": True,
                        "accept_annotation_loss": True},
                       [_row("ATT1", annotations=3)])
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["trashed"], 1)

    def test_an_unannotated_attachment_needs_only_confirm(self):
        r = self._post({"item_keys": ["ATT1"], "confirm": True}, [_row("ATT1")])
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["trashed"], 1)

    # --- trash, not delete ----------------------------------------------

    def test_the_write_is_a_trash_not_a_delete(self):
        r = self._post({"item_keys": ["ATT1"], "confirm": True}, [_row("ATT1")])
        self.assertEqual(r.status_code, 200)
        # deleted=True, guarded by the version from the fresh GET — never a
        # version echoed from a previous write.
        args, kwargs = self.set_deleted.call_args
        self.assertIs(args[3], True)
        self.assertEqual(args[4], 42)

    def test_refuses_to_trash_a_non_attachment(self):
        """Pointed at a parent item this verb would bin the bibliographic
        record, so the item type is re-checked against Zotero itself."""
        r = self._post({"item_keys": ["ATT1"], "confirm": True}, [_row("ATT1")],
                       get_item=lambda *a, **k: ({"itemType": "journalArticle"}, 42))
        self.assertEqual(r.status_code, 200)
        res = r.get_json()["results"]["ATT1"]
        self.assertFalse(res["ok"])
        self.set_deleted.assert_not_called()

    def test_an_already_trashed_attachment_is_skipped_not_rewritten(self):
        r = self._post({"item_keys": ["ATT1"], "confirm": True}, [_row("ATT1")],
                       get_item=lambda *a, **k: (
                           {"itemType": "attachment", "deleted": 1}, 42))
        self.assertEqual(r.get_json()["results"]["ATT1"]["status"], "skipped")
        self.set_deleted.assert_not_called()

    # --- journal --------------------------------------------------------

    def test_the_write_is_journalled_and_revertible(self):
        from tagging import journal
        r = self._post({"item_keys": ["ATT1"], "confirm": True}, [_row("ATT1")])
        self.assertEqual(r.status_code, 200, r.get_json())
        man = journal.load(self.vault, self.cfg)
        ops = [o for o in man["ops"] if o["kind"] == "zotero_attachment_trash"]
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["state"], "applied")
        # `op_id`, not `id`: the tagging journal indexes on that key, and an op
        # under the wrong one is invisible to Restore and to prune.
        self.assertIn("op_id", ops[0])

    def test_a_failed_patch_leaves_no_op_claiming_a_trash_that_never_happened(self):
        """The op is journalled BEFORE the PATCH (crash trail), so a failed
        PATCH must flip it to ``failed`` — at which point ``prune`` drops it in
        phase 1, since there is nothing to restore. What must never survive is
        an ``applied`` record for a write Zotero refused: the Restore panel
        would offer an undo that untrashes an item nobody trashed."""
        from tagging import journal, zotero_write
        r = self._post(
            {"item_keys": ["ATT1"], "confirm": True}, [_row("ATT1")],
            set_deleted=mock.Mock(side_effect=zotero_write.ZoteroUnavailable("down")))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["results"]["ATT1"]["ok"])
        man = journal.load(self.vault, self.cfg)
        applied = [o for o in man["ops"]
                   if o["kind"] == "zotero_attachment_trash"
                   and o.get("state") == "applied"]
        self.assertEqual(applied, [])

    def test_applied_attachment_ops_are_never_evicted_by_prune(self):
        """Their restore mapping lives INLINE in the op, with no snapshot file
        behind it — eviction would destroy the only record of what to restore."""
        from tagging import journal
        self.assertIn("zotero_attachment_trash", journal.EXTERNAL_OP_KINDS)

    def test_revert_restores_the_attachment(self):
        from tagging import zotero_write
        from tagging.writers import _revert_zotero_attachment
        op = {"kind": "zotero_attachment_trash", "item_key": "ATT1",
              "lib_path": "users/7", "base_url": "https://api.zotero.org",
              "transport": "local", "state": "applied", "filename": "x.pdf"}
        with mock.patch.object(zotero_write, "get_item",
                               return_value=({"itemType": "attachment",
                                              "deleted": 1}, 44)), \
             mock.patch.object(zotero_write, "set_item_deleted",
                               return_value=45) as setd:
            res = _revert_zotero_attachment(self.vault, self.cfg, op)
        self.assertTrue(res["ok"], res)
        self.assertEqual(op["state"], "reverted")
        self.assertIs(setd.call_args[0][3], False)     # deleted=False — restore

    def test_revert_of_an_untrashed_attachment_is_a_skip(self):
        from tagging import zotero_write
        from tagging.writers import _revert_zotero_attachment
        op = {"kind": "zotero_attachment_trash", "item_key": "ATT1",
              "transport": "local", "state": "applied"}
        with mock.patch.object(zotero_write, "get_item",
                               return_value=({"itemType": "attachment"}, 44)), \
             mock.patch.object(zotero_write, "set_item_deleted") as setd:
            res = _revert_zotero_attachment(self.vault, self.cfg, op)
        self.assertEqual(res["status"], "skipped")
        setd.assert_not_called()

    def test_the_op_is_persisted_BEFORE_its_patch(self):
        """Journal-before-write, and the ordering is the whole point.

        If the save were deferred to the end of the batch, a crash after N
        PATCHes would leave N attachments trashed in Zotero with no journal
        record to restore them from — the exact hazard this ordering closes.
        Cheap since the journal became SQLite: a save writes one changed row.
        """
        from tagging import journal
        order = []
        real_save = journal.save
        with mock.patch.object(journal, "save",
                               side_effect=lambda *a, **k: (order.append("save"),
                                                            real_save(*a, **k))[1]):
            r = self._post({"item_keys": ["ATT1"], "confirm": True},
                           [_row("ATT1")],
                           set_deleted=lambda *a, **k: order.append("patch") or 43)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIn("save", order)
        self.assertIn("patch", order)
        self.assertLess(order.index("save"), order.index("patch"),
                        f"op must be journalled before the PATCH; got {order}")

    def test_the_writer_refuses_a_save_it_cannot_perform(self):
        """`defer_save=False` without vault_root/cfg silently skipped the save
        before this guard — a caller asking for the crash trail got none."""
        from tagging.writers import zotero_attachment_commit_locked
        prepared = {"commit": {"base": "b", "transport": SimpleNamespace(
            base_url="b", kind="local"), "api_key": None, "lib_path": "users/0",
            "item_key": "A", "version": 1, "link_mode": "linked_file",
            "parent_item": "P", "filename": "x.pdf", "title": ""}}
        with self.assertRaises(ValueError):
            zotero_attachment_commit_locked(prepared, {"ops": []},
                                            defer_save=False)

    # --- status ---------------------------------------------------------

    def test_status_reports_whether_the_transport_can_write_at_all(self):
        from tagging import zotero_write
        transport = SimpleNamespace(kind="local", base_url="http://127.0.0.1:23119",
                                    api_key=None, is_web=False, can_write=False)
        with self._ctx([_row("ATT1", annotations=2)]) as st:
            st.enter_context(mock.patch.object(
                zotero_write, "resolve_transport", return_value=transport))
            d = self.client.get("/api/zotlink/status", headers=_HEADERS).get_json()
        self.assertEqual(d["count"], 1)
        self.assertEqual(d["with_annotations"], 1)
        self.assertFalse(d["can_write"])
        self.assertIn("read-only", d["note"])


class TestNoDeleteVerbExists(unittest.TestCase):
    """A source ratchet, because this is the property no runtime test can prove
    absent: the de-link path must never gain an HTTP DELETE.

    It matches the CODE SHAPES a delete would take (``method="DELETE"``, a
    ``.delete(`` call) rather than the bare word, because all three files
    DISCUSS at length why delete is refused — a bare-word search would match the
    explanation. Each assertion is paired with a positive control proving the
    same search finds the PATCH that IS there, so the ratchet cannot pass
    vacuously (the first version of this test stripped every string literal to
    skip the prose, which also stripped ``method="DELETE"`` — it would have
    passed against a file that deleted on every call).
    """

    ROOT = Path(__file__).resolve().parent
    _DELETE_SHAPES = ('method="DELETE"', "method='DELETE'", ".delete(")

    def _assert_no_delete(self, rel, *, control):
        src = (self.ROOT / rel).read_text(encoding="utf-8")
        self.assertIn(control, src,
                      f"{rel}: positive control missing — this ratchet would "
                      f"pass vacuously; re-anchor it")
        for shape in self._DELETE_SHAPES:
            self.assertNotIn(shape, src, f"{rel} must never delete: found {shape!r}")

    def test_zotero_client_offers_no_delete_verb(self):
        self._assert_no_delete("tagging/zotero_write.py", control='method="PATCH"')

    def test_the_attachment_writer_never_deletes(self):
        self._assert_no_delete("tagging/writers.py", control="set_item_deleted")

    def test_the_delink_route_never_deletes(self):
        self._assert_no_delete("api/routes/zotlink.py",
                               control="zotero_attachment_prepare")


class TestPreWriteFailuresAreCleanAndSilent(unittest.TestCase):
    """A failure BEFORE any PATCH must reach the client as a sanitised message,
    and must be distinguishable from a failure after Zotero was written to."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zotlink-pre-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.client = _app().test_client()

    def test_an_archive_dir_inside_the_vault_is_a_clean_500_not_a_traceback(self):
        from audit.manager import audit_manager
        from rag.vault import obsidian_manager
        from tagging import zotero_write
        # journal.archive_dir refuses an archive resolving inside the vault.
        cfg = {"tagging_archive_dir": str(self.vault / "inside")}
        inv = SimpleNamespace(zotero_attachments=[_row("ATT1")])
        transport = SimpleNamespace(kind="web", base_url="https://api.zotero.org",
                                    api_key="k", is_web=True, can_write=True)
        with mock.patch("api.routes.zotlink.load_config", return_value=cfg), \
             mock.patch.object(audit_manager, "get_inventory",
                               return_value=(inv, SimpleNamespace(vault_root=self.vault))), \
             mock.patch.object(obsidian_manager, "try_acquire_lock", return_value=1), \
             mock.patch.object(obsidian_manager, "release_lock"), \
             mock.patch.object(zotero_write, "resolve_transport", return_value=transport), \
             mock.patch.object(zotero_write, "resolve_user_id", return_value=7), \
             mock.patch.object(zotero_write, "get_item", return_value=(
                 {"itemType": "attachment"}, 42)), \
             mock.patch.object(zotero_write, "set_item_deleted") as setd:
            r = self.client.post("/api/zotlink/trash",
                                 json={"item_keys": ["ATT1"], "confirm": True},
                                 headers=_HEADERS)
        self.assertEqual(r.status_code, 500)
        self.assertIn("error", r.get_json())
        setd.assert_not_called()


class TestDeLinkLockChoreography(_ZotlinkFixture):
    """The four-phase discipline the 2026-08-30 audit restored.

    Before it, `_run_trash` PATCHed up to 200 attachments while holding the
    obsidian op-lock, with no heartbeat — freezing every vault write for the
    whole batch and able to outrun the 600 s TTL while still appending to the
    manifest. That is the pattern the 2026-07-23 audit removed from retire-tag;
    the de-link route re-introduced it.
    """

    def _lock_trace(self):
        """Record lock acquire/release and PATCH calls in one ordered list."""
        from rag.vault import obsidian_manager
        from tagging import zotero_write
        trace = []
        real_acq = mock.patch.object(
            obsidian_manager, "try_acquire_lock",
            side_effect=lambda **k: (trace.append("acquire"), 1)[1])
        real_rel = mock.patch.object(
            obsidian_manager, "release_lock",
            side_effect=lambda *a: trace.append("release"))
        real_hb = mock.patch.object(obsidian_manager, "heartbeat",
                                    return_value=True)
        real_patch = mock.patch.object(
            zotero_write, "set_item_deleted",
            side_effect=lambda *a, **k: (trace.append("PATCH"), 43)[1])
        return trace, (real_acq, real_rel, real_hb, real_patch)

    def test_every_patch_runs_OUTSIDE_the_op_lock(self):
        from audit.manager import audit_manager
        from tagging import zotero_write
        rows = [_row("ATT1"), _row("ATT2"), _row("ATT3")]
        inv = SimpleNamespace(zotero_attachments=rows)
        transport = SimpleNamespace(kind="web", base_url="https://api.zotero.org",
                                    api_key="k", is_web=True, can_write=True)
        trace, patches = self._lock_trace()
        with mock.patch("api.routes.zotlink.load_config", return_value=self.cfg), \
             mock.patch.object(audit_manager, "get_inventory",
                               return_value=(inv, SimpleNamespace(vault_root=self.vault))), \
             mock.patch.object(zotero_write, "resolve_transport", return_value=transport), \
             mock.patch.object(zotero_write, "resolve_user_id", return_value=7), \
             mock.patch.object(zotero_write, "get_item", return_value=(
                 {"itemType": "attachment", "linkMode": "imported_file",
                  "parentItem": "P", "filename": "x.pdf"}, 42)), \
             patches[0], patches[1], patches[2], patches[3]:
            r = _Resp(self.client.post(
                "/api/zotlink/trash",
                json={"item_keys": ["ATT1", "ATT2", "ATT3"], "confirm": True},
                headers=_HEADERS))

        self.assertEqual(r.get_json()["trashed"], 3, r.frames)
        # Phase 2 opens and CLOSES before any PATCH; phase 4 re-acquires after.
        first_patch = trace.index("PATCH")
        last_patch = len(trace) - 1 - trace[::-1].index("PATCH")
        self.assertEqual(trace[:2], ["acquire", "release"],
                         f"phase 2 must release before phase 3: {trace}")
        self.assertNotIn("acquire", trace[2:first_patch],
                         f"no lock may be held entering phase 3: {trace}")
        self.assertNotIn("acquire", trace[first_patch:last_patch],
                         f"the lock must stay free across the PATCHes: {trace}")
        self.assertIn("acquire", trace[last_patch:],
                      f"phase 4 must re-acquire to record outcomes: {trace}")

    def test_a_lapsed_lock_stops_the_batch_and_says_so(self):
        """The keeper ACTS on a False heartbeat. Silently continuing would mean
        appending to a manifest a second writer may now own."""
        from audit.manager import audit_manager
        from rag.vault import obsidian_manager
        from tagging import zotero_write
        rows = [_row("ATT1"), _row("ATT2"), _row("ATT3")]
        inv = SimpleNamespace(zotero_attachments=rows)
        transport = SimpleNamespace(kind="web", base_url="https://api.zotero.org",
                                    api_key="k", is_web=True, can_write=True)
        beats = {"n": 0}

        def _hb(_epoch):
            beats["n"] += 1
            return beats["n"] < 2          # the lock lapses on the 2nd record

        with mock.patch("api.routes.zotlink.load_config", return_value=self.cfg), \
             mock.patch.object(audit_manager, "get_inventory",
                               return_value=(inv, SimpleNamespace(vault_root=self.vault))), \
             mock.patch.object(obsidian_manager, "try_acquire_lock", return_value=1), \
             mock.patch.object(obsidian_manager, "release_lock"), \
             mock.patch.object(obsidian_manager, "heartbeat", side_effect=_hb), \
             mock.patch.object(zotero_write, "resolve_transport", return_value=transport), \
             mock.patch.object(zotero_write, "resolve_user_id", return_value=7), \
             mock.patch.object(zotero_write, "get_item", return_value=(
                 {"itemType": "attachment", "linkMode": "imported_file",
                  "parentItem": "P", "filename": "x.pdf"}, 42)), \
             mock.patch.object(zotero_write, "set_item_deleted", return_value=43) as sd:
            r = _Resp(self.client.post(
                "/api/zotlink/trash",
                json={"item_keys": ["ATT1", "ATT2", "ATT3"], "confirm": True},
                headers=_HEADERS))

        d = r.get_json()
        self.assertLess(sd.call_count, 3, "a lapsed lock must stop the batch")
        self.assertTrue(any("op-lock" in f for f in d["failures"]),
                        f"the lapse must be disclosed: {d['failures']}")

    def test_progress_is_streamed_per_attachment(self):
        r = self._post({"item_keys": ["ATT1"], "confirm": True}, [_row("ATT1")])
        self.assertTrue(any("reading ATT1" in i for i in r.infos), r.infos)
        self.assertTrue(any("trashing ATT1" in i for i in r.infos), r.infos)


class TestDuplicateKeysAreHonest(_ZotlinkFixture):
    def test_a_repeated_key_is_written_once_and_counted_once(self):
        """`prepared` is keyed by item key and so collapsed duplicates
        silently, while the annotation 409 summed per LISTED key — telling the
        user they would lose twice the highlights they actually would."""
        r = self._post({"item_keys": ["ATT1", "ATT1", "ATT1"], "confirm": True},
                       [_row("ATT1")])
        self.assertEqual(r.get_json()["trashed"], 1)
        self.assertEqual(self.set_deleted.call_count, 1)

    def test_the_annotation_warning_does_not_double_count_a_repeat(self):
        r = self._post({"item_keys": ["ATT1", "ATT1"], "confirm": True},
                       [_row("ATT1", annotations=3)])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["annotation_count"], 3)

    def test_a_non_ascii_key_is_refused_on_shape(self):
        r = self._post({"item_keys": ["ATT٣"], "confirm": True},
                       [_row("ATT1")])
        self.assertEqual(r.status_code, 400)


class TestTheGateIsNotLeaked(_ZotlinkFixture):
    """Every pre-stream refusal must leave the single-op gate free.

    Introduced and caught during the 2026-08-30 SSE conversion: the gate was
    acquired before the remaining validation, so each `return jsonify(...)`
    below it leaked the lock. One malformed request and every later de-link
    429'd for the process's lifetime with nothing actually running. The gate is
    now taken last, after every refusal has had its chance to return.
    """

    def _refusals(self):
        return [
            ({"item_keys": ["ATT1"]}, 400),                       # no confirm
            ({"item_keys": [], "confirm": True}, 400),            # empty list
            ({"item_keys": ["ATT٣"], "confirm": True}, 400),      # bad shape
            ({"item_keys": ["NOPE"], "confirm": True}, 404),      # not in scan
        ]

    def test_a_refused_request_leaves_the_gate_free(self):
        for body, expected in self._refusals():
            with self.subTest(body=body):
                r = self._post(body, [_row("ATT1")])
                self.assertEqual(r.status_code, expected)
        # The gate must still be takeable — a real de-link runs normally.
        ok = self._post({"item_keys": ["ATT1"], "confirm": True}, [_row("ATT1")])
        self.assertEqual(ok.status_code, 200, ok.get_json())
        self.assertEqual(ok.get_json()["trashed"], 1)

    def test_the_annotation_refusal_leaves_the_gate_free(self):
        r = self._post({"item_keys": ["ATT1"], "confirm": True},
                       [_row("ATT1", annotations=2)])
        self.assertEqual(r.status_code, 409)
        ok = self._post({"item_keys": ["ATT1"], "confirm": True,
                         "accept_annotation_loss": True},
                        [_row("ATT1", annotations=2)])
        self.assertEqual(ok.get_json()["trashed"], 1, ok.get_json())

    def test_a_client_that_disconnects_before_draining_leaves_the_gate_free(self):
        """The leak the 2026-09-02 perf review found.

        ``run_sse_worker`` spawns the worker on the generator's FIRST
        iteration, so a client that receives the 200 and then goes away
        without draining never runs it — and the release that used to live in
        the worker's own ``finally`` therefore never fired. Every later
        de-link answered 429 for the rest of the process's life with nothing
        running, and only a restart cleared it. ``_SSEOpGuard`` handles this
        case (release on stream end when no worker was spawned) but only for
        callers that hand it ``release=``.

        The response is built and closed WITHOUT iterating its body — the test
        client's ``post()`` drains the stream, which spawns the worker and so
        exercises a different path entirely.
        """
        from api.routes import zotlink as _z
        from tagging import zotero_write
        transport = SimpleNamespace(kind="web", base_url="https://api.zotero.org",
                                    api_key="k", is_web=True, can_write=True)
        app = _app()
        with self._ctx([_row("ATT1")]) as st:
            st.enter_context(mock.patch.object(
                zotero_write, "resolve_transport", return_value=transport))
            st.enter_context(mock.patch.object(
                zotero_write, "resolve_user_id", return_value=7))
            spawned = st.enter_context(mock.patch.object(zotero_write, "get_item"))
            st.enter_context(mock.patch.object(zotero_write, "set_item_deleted"))
            with app.test_request_context(
                    "/api/zotlink/trash", method="POST",
                    json={"item_keys": ["ATT1"], "confirm": True},
                    headers=_HEADERS):
                resp = app.full_dispatch_request()
                self.assertEqual(resp.status_code, 200)
                resp.close()          # the disconnect: nothing ever iterated
            self.assertFalse(spawned.called,
                             "the worker must not have run — this is the "
                             "never-iterated-generator case")
        acquired = _z._ZOTLINK_GATE.acquire(blocking=False)
        if acquired:
            _z._ZOTLINK_GATE.release()
        self.assertTrue(acquired, "the de-link gate leaked on a disconnect")


class TestZotlinkAttach(unittest.TestCase):
    """Phase 4 of the 2026-09-06 harmonization plan: linked-file attachments
    CREATED in Zotero for records' mapped PDFs. Absolute paths, personal
    library only, idempotent over already-linked PDFs, journalled after the
    POST as ``zotero_attachment_create`` (undo = trash), never a DELETE."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zotlink-attach-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.pdf = self.vault / "smith_2020.pdf"
        self.pdf.write_bytes(b"%PDF")
        self.cfg = {"tagging_archive_dir": str(self.tmp / "archive")}
        self.client = _app().test_client()
        self.remote = {}

    def _rec(self, key="smith2020", *, library="user", pdfs=None):
        zot = SimpleNamespace(item_key="PARENT1" if library == "user" else "GPARENT",
                              library_type=library,
                              group_id=(484650 if library == "group" else None),
                              title="A paper", item_type="journalArticle", doi=None)
        return SimpleNamespace(citation_key=key, zotero_item=zot,
                               pdf_paths=list(pdfs if pdfs is not None else [self.pdf]),
                               bib_entry=SimpleNamespace(title="A paper", year="2020"))

    def _run(self, body, records, *, attachments=(), get_item=None, create=None):
        import contextlib
        from audit.manager import audit_manager
        from rag.vault import obsidian_manager
        from tagging import zotero_write
        inv = SimpleNamespace(records={r.citation_key: r for r in records},
                              zotero_attachments=list(attachments))
        settings = SimpleNamespace(vault_root=self.vault, biblio_articles_dir=self.vault, mapping_file=self.tmp / "mapping.json")
        transport = SimpleNamespace(kind="web", base_url="https://api.zotero.org",
                                    api_key="k", is_web=True, can_write=True)
        def default_get(base, lib, key, **kwargs):
            if key == "PARENT1":
                return {"itemType": "journalArticle", "title": "A paper"}, 42
            if key not in self.remote:
                raise zotero_write.ZoteroNotFound("Not found (404)")
            return self.remote[key], 42
        def default_create(base, lib, parent, path, title, **kwargs):
            key = kwargs.get("item_key") or "NEWATT1"
            self.remote[key] = {"itemType": "attachment", "linkMode": "linked_file", "parentItem": parent, "path": path}
            return key
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch("api.routes.zotlink.load_config", return_value=self.cfg))
            st.enter_context(mock.patch("api.routes.zotlink.zotero_renames_linked_files",
                                        return_value=getattr(self, "renames", False)))
            st.enter_context(mock.patch("api.routes.zotlink.zotero_renaming_plugins",
                                        return_value=getattr(self, "plugins", [])))
            st.enter_context(mock.patch("api.routes.zotlink.attanger_skip_rules",
                                        return_value=getattr(self, "skip_rules", [])))
            # Patched too, or the gate reads the DEVELOPER's real prefs.js and
            # the suite silently depends on this machine's Zotero settings.
            st.enter_context(mock.patch("api.routes.zotlink.attanger_rule_problems",
                                        return_value=getattr(self, "rule_problems", [])))
            st.enter_context(mock.patch.object(audit_manager, "get_inventory",
                                               return_value=(inv, settings)))
            st.enter_context(mock.patch.object(obsidian_manager, "try_acquire_lock", return_value=1))
            st.enter_context(mock.patch.object(obsidian_manager, "release_lock"))
            st.enter_context(mock.patch.object(obsidian_manager, "heartbeat", return_value=True))
            st.enter_context(mock.patch.object(zotero_write, "resolve_transport", return_value=transport))
            st.enter_context(mock.patch.object(zotero_write, "resolve_user_id", return_value=7))
            self.get_item = st.enter_context(mock.patch.object(
                zotero_write, "get_item",
                side_effect=get_item or default_get))
            self.create = st.enter_context(mock.patch.object(
                zotero_write, "create_linked_file_attachment", side_effect=create or default_create))
            r = self.client.post("/api/zotlink/attach", json=body, headers=_HEADERS)
            # Drain INSIDE the patch scope (the SSE worker trap).
            status = r.status_code
            frames, payload = [], None
            if r.mimetype == "text/event-stream":
                import json as _json
                for line in r.get_data().decode("utf-8").splitlines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        f = _json.loads(line[6:])
                        frames.append(f)
                        if "zotlink_attach" in f:
                            payload = f["zotlink_attach"]
                        elif "error" in f:
                            payload = {"error": f["error"]}
            else:
                payload = r.get_json()
            return status, payload, frames

    def test_exact_file_selection_does_not_attach_other_pdfs_and_rechecks_live(self):
        from audit.engine import bridge
        other = self.vault / "other.pdf"
        other.write_bytes(b"%PDF-other")
        bridge.add_matches(self.tmp / "mapping.json", [(self.pdf, "smith2020"), (other, "smith2020")], self.vault)
        body = {"citation_keys": ["smith2020"], "pdfs": ["smith_2020.pdf"], "confirm": True}
        with mock.patch("tagging.zotero_write.linked_file_paths", return_value=set()):
            status, result, _ = self._run(body, [self._rec(pdfs=[self.pdf, other])])
        self.assertEqual(status, 200, result)
        self.assertEqual(self.create.call_count, 1)
        self.assertEqual(self.create.call_args[0][3], str(self.pdf))
        with mock.patch("tagging.zotero_write.linked_file_paths", return_value={str(self.pdf)}):
            status, result, _ = self._run(body, [self._rec(pdfs=[self.pdf, other])])
        self.assertEqual(status, 200, result)
        self.create.assert_not_called()

    def test_exact_file_changed_after_mapping_refuses_before_network(self):
        from audit.engine import bridge
        bridge.add_matches(self.tmp / "mapping.json", [(self.pdf, "smith2020")], self.vault)
        self.pdf.write_bytes(b"%PDF-replaced")
        status, result, _ = self._run({"citation_keys": ["smith2020"], "pdfs": [self.pdf.name], "confirm": True}, [self._rec()])
        self.assertEqual(status, 409, result)
        self.get_item.assert_not_called()
        self.create.assert_not_called()

    def test_exact_file_changed_during_parent_read_refuses_post(self):
        from audit.engine import bridge
        bridge.add_matches(self.tmp / "mapping.json", [(self.pdf, "smith2020")], self.vault)
        def changed(*args, **kwargs):
            self.pdf.write_bytes(b"%PDF-replaced")
            return {"itemType": "journalArticle", "title": "A paper"}, 42
        with mock.patch("tagging.zotero_write.linked_file_paths", return_value=set()):
            status, result, _ = self._run({"citation_keys": ["smith2020"], "pdfs": [self.pdf.name], "confirm": True}, [self._rec()], get_item=changed)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["attached"], 0)
        self.create.assert_not_called()

    def test_successful_create_failed_readback_keeps_key_and_restore_record(self):
        from tagging import zotero_write, journal
        def read(base, lib, key, **kwargs):
            if key == "PARENT1":
                return {"itemType": "journalArticle", "title": "A paper"}, 42
            raise zotero_write.ZoteroUnavailable("disconnected")
        status, result, _ = self._run({"citation_keys": ["smith2020"], "confirm": True}, [self._rec()], get_item=read)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["attached"], 1)
        self.assertTrue(result["results"]["smith2020"]["pdfs"][str(self.pdf)]["verification_pending"])
        self.assertEqual(journal.load(self.vault, self.cfg)["ops"][-1]["item_key"], "NEWATT1")

    def test_lost_post_response_recovers_reserved_key_without_duplicate(self):
        from audit.engine import bridge
        from tagging import zotero_write, journal
        bridge.add_matches(self.tmp / "mapping.json", [(self.pdf, "smith2020")], self.vault)
        body = {"citation_keys": ["smith2020"], "pdfs": [self.pdf.name], "confirm": True}
        def lost(base, lib, parent, path, title, **kwargs):
            self.remote[kwargs["item_key"]] = {"itemType": "attachment", "linkMode": "linked_file", "parentItem": parent, "path": path}
            raise zotero_write.ZoteroUnavailable("response lost after server commit")
        with mock.patch("tagging.zotero_write.linked_file_paths", return_value=set()):
            _, result, _ = self._run(body, [self._rec()], create=lost)
        self.assertEqual(result["attached"], 0)
        self.assertEqual(len(self.remote), 1)
        with mock.patch("tagging.zotero_write.linked_file_paths", return_value={str(self.pdf)}):
            _, result, _ = self._run(body, [self._rec()])
            self.assertEqual(result["recovered"], 1, result)
            self.create.assert_not_called()
            att = SimpleNamespace(item_key=next(iter(self.remote)), parent_key="PARENT1", link_mode="linked_file", raw_path=str(self.pdf))
            _, retry, _ = self._run(body, [self._rec()], attachments=[att])
        self.assertEqual(retry["recovered"], 1)
        self.assertFalse(retry["batch_id"])
        self.assertEqual(retry["restore_batch_ids"], [result["batch_id"]])
        self.create.assert_not_called()
        ops = [o for o in journal.load(self.vault, self.cfg)["ops"] if o["kind"] == "zotero_attachment_create"]
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["item_key"], next(iter(self.remote)))

    def test_intent_persistence_failure_prevents_post(self):
        from audit.engine import bridge
        bridge.add_matches(self.tmp / "mapping.json", [(self.pdf, "smith2020")], self.vault)
        with mock.patch("tagging.zotero_write.linked_file_paths", return_value=set()), mock.patch("tagging.attachment_intents.reserve", side_effect=OSError("disk full")):
            _, result, _ = self._run({"citation_keys": ["smith2020"], "pdfs": [self.pdf.name], "confirm": True}, [self._rec()])
        self.assertEqual(result["attached"], 0)
        self.create.assert_not_called()

    def test_change_during_reserved_key_lookup_prevents_post(self):
        from audit.engine import bridge
        from tagging import zotero_write
        bridge.add_matches(self.tmp / "mapping.json", [(self.pdf, "smith2020")], self.vault)
        def read(base, lib, key, **kwargs):
            if key == "PARENT1":
                return {"itemType": "journalArticle", "title": "A paper"}, 42
            self.pdf.write_bytes(b"%PDF-changed-during-lookup")
            raise zotero_write.ZoteroNotFound("Not found (404)")
        with mock.patch("tagging.zotero_write.linked_file_paths", return_value=set()):
            _, result, _ = self._run({"citation_keys": ["smith2020"], "pdfs": [self.pdf.name], "confirm": True}, [self._rec()], get_item=read)
        self.assertEqual(result["attached"], 0)
        self.create.assert_not_called()

    def test_journal_failure_retry_recovers_and_trashed_intent_refuses_duplicate(self):
        from audit.engine import bridge
        from tagging import journal
        bridge.add_matches(self.tmp / "mapping.json", [(self.pdf, "smith2020")], self.vault)
        body = {"citation_keys": ["smith2020"], "pdfs": [self.pdf.name], "confirm": True}
        with mock.patch("tagging.zotero_write.linked_file_paths", return_value=set()), mock.patch.object(journal, "save", side_effect=OSError("journal disk full")):
            _, result, _ = self._run(body, [self._rec()])
        self.assertEqual(result["attached"], 1)
        self.assertIn("journal_warning", result)
        self.assertFalse(result["batch_id"])
        with mock.patch("tagging.zotero_write.linked_file_paths", return_value={str(self.pdf)}):
            _, result, _ = self._run(body, [self._rec()])
        self.assertEqual(result["recovered"], 1)
        self.create.assert_not_called()
        self.remote[next(iter(self.remote))]["deleted"] = 1
        with mock.patch("tagging.zotero_write.linked_file_paths", return_value=set()):
            _, result, _ = self._run(body, [self._rec()])
        self.assertEqual(result["attached"], 0)
        self.assertEqual(result["results"]["smith2020"]["pdfs"][str(self.pdf)]["status"], "conflict")
        self.create.assert_not_called()

    def test_requires_confirm_and_a_known_record(self):
        st, d, _ = self._run({"citation_keys": ["smith2020"]}, [self._rec()])
        self.assertEqual(st, 400)
        st, d, _ = self._run({"citation_keys": ["nope"], "confirm": True}, [self._rec()])
        self.assertEqual(st, 404)
        self.create.assert_not_called()

    def test_exact_files_fail_closed_when_live_lookup_fails(self):
        from audit.engine import bridge
        other = self.vault / "other.pdf"
        other.write_bytes(b"%PDF-other")
        bridge.add_matches(self.tmp / "mapping.json", [(self.pdf, "smith2020"), (other, "smith2020")], self.vault)
        body = {"citation_keys": ["smith2020"], "pdfs": [self.pdf.name, other.name], "confirm": True}
        with mock.patch("tagging.zotero_write.linked_file_paths", side_effect=OSError("unavailable")):
            status, result, _ = self._run(body, [self._rec(pdfs=[self.pdf, other])])
        self.assertEqual(status, 200, result)
        self.create.assert_not_called()
        self.assertTrue(all(not p["ok"] for p in result["results"]["smith2020"]["pdfs"].values()))

    def test_creates_one_linked_file_per_pdf_with_the_absolute_path_and_journals_it(self):
        from tagging import journal
        st, d, _ = self._run({"citation_keys": ["smith2020"], "confirm": True}, [self._rec()])
        self.assertEqual(st, 200, d)
        self.assertEqual(d["attached"], 1)
        self.assertEqual(d["path_form"], "absolute")
        args = self.create.call_args[0]
        self.assertEqual(args[2], "PARENT1")
        self.assertEqual(args[3], str(self.pdf))          # absolute vault path
        self.assertEqual(d["results"]["smith2020"]["status"], "attached")
        pdfres = d["results"]["smith2020"]["pdfs"][str(self.pdf)]
        self.assertEqual(pdfres["item_key"], "NEWATT1")
        self.assertIn("op_id", pdfres)
        man = journal.load(self.vault, self.cfg)
        ops = [o for o in man["ops"] if o["kind"] == "zotero_attachment_create"]
        self.assertEqual(len(ops), 1)
        self.assertEqual((ops[0]["state"], ops[0]["item_key"], ops[0]["path"]),
                         ("applied", "NEWATT1", str(self.pdf)))
        self.assertEqual(ops[0]["batch_id"], d["batch_id"])

    def test_a_slash_in_a_citation_key_is_accepted(self):
        """The live bib carries `institutions/universities/…2021`; refusing `/`
        failed every selection that included it (2026-09-07 report)."""
        rec = self._rec("institutions/universities/cohortStudy2021")
        st, d, _ = self._run({"citation_keys": [rec.citation_key], "confirm": True}, [rec])
        self.assertEqual(st, 200, d)
        self.assertEqual(d["attached"], 1)
        st, d, _ = self._run({"citation_keys": ["bad\nkey"], "confirm": True}, [rec])
        self.assertEqual(st, 400)

    def test_refuses_while_zotero_would_rename_linked_files(self):
        """2026-09-07: Zotero's "Rename linked files" preference renamed five
        vault PDFs to `_.pdf` … `_ 5.pdf` the moment the links landed. The
        route refuses — dry run included — until the preference is off."""
        self.renames = True
        st, d, _ = self._run({"citation_keys": ["smith2020"], "confirm": True, "dry_run": True},
                             [self._rec()])
        self.assertEqual(st, 409, d)
        self.assertEqual(d["reason"], "zotero_renames_linked_files")
        self.create.assert_not_called()

    def test_refuses_while_a_renaming_plugin_is_installed_unless_acknowledged(self):
        """2026-09-07, second incident: Attanger renamed `ades_2017.pdf` to
        `adès_.pdf` with Zotero's own preference off. The plugin's settings
        cannot be read reliably, so presence is a refusal the caller overrides
        explicitly after disabling its auto-rename."""
        self.plugins = ["zoteroattanger"]
        st, d, _ = self._run({"citation_keys": ["smith2020"], "confirm": True}, [self._rec()])
        self.assertEqual(st, 409, d)
        self.assertEqual(d["reason"], "zotero_renaming_plugin")
        self.create.assert_not_called()
        st, d, _ = self._run({"citation_keys": ["smith2020"], "confirm": True,
                              "acknowledge_plugins": True}, [self._rec()])
        self.assertEqual(st, 200, d)
        self.assertEqual(d["attached"], 1)

    def test_an_attanger_skip_rule_lifts_the_refusal_per_covered_filename(self):
        """Attanger 1.5.0 returns before any rename when the filename matches a
        rule in its skip field. With such a rule the run proceeds, and only a
        PDF the rule does NOT cover is skipped — never renamed."""
        from api.routes import zotlink as zl
        self.plugins = ["zoteroattanger"]
        self.skip_rules = [zl.SUGGESTED_SKIP_RULE]
        odd = self.vault / "Smith et al. - 2020 - Title.pdf"
        odd.write_bytes(b"%PDF")
        rec = self._rec(pdfs=[self.pdf, odd])
        st, d, _ = self._run({"citation_keys": ["smith2020"], "confirm": True}, [rec])
        self.assertEqual(st, 200, d)
        self.assertEqual(d["attached"], 1)
        pdfs = d["results"]["smith2020"]["pdfs"]
        self.assertEqual(pdfs[str(self.pdf)]["status"], "attached")
        self.assertEqual(pdfs[str(odd)]["reason"], "plugin_would_rename")
        self.assertEqual(self.create.call_args[0][3], str(self.pdf))

    def test_the_refusal_names_the_rule_to_paste(self):
        from api.routes import zotlink as zl
        self.plugins = ["zoteroattanger"]
        st, d, _ = self._run({"citation_keys": ["smith2020"], "confirm": True}, [self._rec()])
        self.assertEqual(st, 409)
        self.assertEqual(d["suggested_rule"], zl.SUGGESTED_SKIP_RULE)
        # The suggested rule covers the whole vault convention, incl. counters.
        for stem in ("smith_2020", "bland_1_1998", "coviprev_txt_mai_2022",
                     "ema_chmp_2013", "has_1_2021"):
            self.assertTrue(zl._rule_matches([zl.SUGGESTED_SKIP_RULE], stem), stem)
        self.assertFalse(zl._rule_matches([zl.SUGGESTED_SKIP_RULE], "Smith et al. - 2020 - Title"))
        self.assertFalse(zl._rule_matches(["("], "smith_2020"))   # uncompilable = no match

    def test_a_broken_skip_rule_refuses_instead_of_skipping_every_pdf(self):
        """A padded rule passes the "is a rule set?" check but matches nothing,
        so the pre-fix gate proceeded and skipped every PDF as
        `plugin_would_rename` — a run that wrote nothing and blamed the files.
        The rule is the fault; say so, and refuse."""
        from api.routes import zotlink as zl
        self.plugins = ["zoteroattanger"]
        self.skip_rules = [zl.SUGGESTED_SKIP_RULE + " "]
        self.rule_problems = [{"kind": "padded", "fatal": True, "field": "rename",
                               "rule": zl.SUGGESTED_SKIP_RULE + " ",
                               "detail": "whitespace"}]
        st, d, _ = self._run({"citation_keys": ["smith2020"], "confirm": True},
                             [self._rec()])
        self.assertEqual(st, 409, d)
        self.assertEqual(d["reason"], "zotero_renaming_plugin_rule_broken")
        self.create.assert_not_called()

    def test_a_broken_rule_still_yields_to_an_explicit_acknowledgement(self):
        """`acknowledge_plugins` is the documented override for "I disabled the
        plugin's automatic rename myself"; a broken rule must not become a
        harder gate than a missing one."""
        from api.routes import zotlink as zl
        self.plugins = ["zoteroattanger"]
        self.skip_rules = [zl.SUGGESTED_SKIP_RULE + " "]
        self.rule_problems = [{"kind": "padded", "fatal": True, "field": "rename",
                               "rule": "x", "detail": "whitespace"}]
        st, d, _ = self._run({"citation_keys": ["smith2020"], "confirm": True,
                              "acknowledge_plugins": True}, [self._rec()])
        self.assertEqual(st, 200, d)
        self.assertEqual(d["attached"], 1)

    def test_an_advisory_problem_warns_but_does_not_refuse(self):
        """A rule only in the auto-move field protects from renaming, so the
        run is safe; the cost (connector downloads are no longer moved into the
        vault) is disclosed as an info line, never as a refusal."""
        from api.routes import zotlink as zl
        self.plugins = ["zoteroattanger"]
        self.skip_rules = [zl.SUGGESTED_SKIP_RULE]
        self.rule_problems = [{"kind": "auto_move_only", "fatal": False,
                               "field": "auto_move", "rule": "x",
                               "detail": "only in the auto-move field"}]
        st, d, frames = self._run({"citation_keys": ["smith2020"], "confirm": True},
                                  [self._rec()])
        self.assertEqual(st, 200, d)
        self.assertEqual(d["attached"], 1)
        infos = [f.get("info") for f in frames if "info" in f]
        self.assertIn("only in the auto-move field", infos)

    def test_skip_rule_reader_parses_the_comma_list(self):
        from api.routes import zotlink as zl
        with tempfile.TemporaryDirectory() as td:
            pj = Path(td) / "prefs.js"
            pj.write_text('user_pref("extensions.zotero.zoteroattanger.filenameSkipAutoMoveRenameRules", '
                          '"^[a-z]+_\\d{4}$, ^keep_me");\n'
                          'user_pref("extensions.zotero.zoteroattanger.filenameSkipRenameRules", '
                          '"^also_me, ^keep_me");\n', encoding="utf-8")
            with mock.patch.object(zl, "_zotero_prefs_paths", return_value=[pj]):
                # Union of BOTH fields, de-duplicated: either one stops a
                # rename. Ordered by FIELD (rename first), not by line position
                # in prefs.js — deterministic whatever order Zotero wrote them.
                # Order is not load-bearing anyway: `_rule_matches` is an any().
                self.assertEqual(zl.attanger_skip_rules(),
                                 ["^also_me", "^keep_me", "^[a-z]+_\\d{4}$"])
                self.assertEqual(zl.attanger_skip_rules_by_field(), {
                    "rename": ["^also_me", "^keep_me"],
                    "auto_move": ["^[a-z]+_\\d{4}$", "^keep_me"],
                })
            pj.write_text('user_pref("extensions.zotero.dataDir", "/x");\n', encoding="utf-8")
            with mock.patch.object(zl, "_zotero_prefs_paths", return_value=[pj]):
                self.assertEqual(zl.attanger_skip_rules(), [])

    def test_plugin_reader_finds_attanger_in_prefs(self):
        from api.routes import zotlink as zl
        with tempfile.TemporaryDirectory() as td:
            pj = Path(td) / "prefs.js"
            pj.write_text('user_pref("extensions.webextensions.uuids", "{\\"zoteroattanger@polygon.org\\":\\"x\\"}");\n',
                          encoding="utf-8")
            with mock.patch.object(zl, "_zotero_prefs_paths", return_value=[pj]):
                self.assertEqual(zl.zotero_renaming_plugins(), ["zoteroattanger"])
            pj.write_text('user_pref("extensions.zotero.dataDir", "/x");\n', encoding="utf-8")
            with mock.patch.object(zl, "_zotero_prefs_paths", return_value=[pj]):
                self.assertEqual(zl.zotero_renaming_plugins(), [])

    def test_prefs_reader_parses_the_boolean(self):
        from api.routes import zotlink as zl
        with tempfile.TemporaryDirectory() as td:
            pj = Path(td) / "prefs.js"
            pj.write_text('user_pref("extensions.zotero.autoRenameFiles.linked", true);\n', encoding="utf-8")
            with mock.patch.object(zl, "_zotero_prefs_paths", return_value=[pj]):
                self.assertIs(zl.zotero_renames_linked_files(), True)
            pj.write_text('user_pref("extensions.zotero.autoRenameFiles.linked", false);\n', encoding="utf-8")
            with mock.patch.object(zl, "_zotero_prefs_paths", return_value=[pj]):
                self.assertIs(zl.zotero_renames_linked_files(), False)
            pj.write_text('user_pref("extensions.zotero.dataDir", "/x");\n', encoding="utf-8")
            with mock.patch.object(zl, "_zotero_prefs_paths", return_value=[pj]):
                self.assertIs(zl.zotero_renames_linked_files(), False)
        with mock.patch.object(zl, "_zotero_prefs_paths", return_value=[]):
            self.assertIsNone(zl.zotero_renames_linked_files())

    def test_group_library_records_are_skipped_before_any_network(self):
        st, d, _ = self._run({"citation_keys": ["g2020"], "confirm": True},
                             [self._rec("g2020", library="group")])
        self.assertEqual(st, 200, d)
        self.assertEqual(d["results"]["g2020"]["reason"], "group_library")
        self.get_item.assert_not_called()
        self.create.assert_not_called()

    def test_an_already_linked_pdf_is_skipped_so_a_rerun_is_idempotent(self):
        att = SimpleNamespace(item_key="ATT1", parent_key="PARENT1", link_mode="linked_file",
                              raw_path=str(self.pdf))
        st, d, _ = self._run({"citation_keys": ["smith2020"], "confirm": True},
                             [self._rec()], attachments=[att])
        self.assertEqual(st, 200, d)
        self.assertEqual(d["results"]["smith2020"]["reason"], "already_linked")
        self.create.assert_not_called()

    def test_dry_run_plans_and_writes_nothing(self):
        from tagging import journal
        st, d, _ = self._run({"citation_keys": ["smith2020"], "confirm": True, "dry_run": True},
                             [self._rec()])
        self.assertEqual(st, 200, d)
        self.assertTrue(d["dry_run"])
        self.assertEqual(d["results"]["smith2020"]["status"], "planned")
        self.get_item.assert_not_called()
        self.create.assert_not_called()
        self.assertFalse((self.tmp / "archive").exists() and
                         any(o["kind"] == "zotero_attachment_create"
                             for o in journal.load(self.vault, self.cfg)["ops"]))

    def test_a_failed_post_leaves_no_op(self):
        from tagging import journal, zotero_write
        st, d, _ = self._run({"citation_keys": ["smith2020"], "confirm": True}, [self._rec()],
                             create=mock.Mock(side_effect=zotero_write.ZoteroUnavailable("down")))
        self.assertEqual(st, 200, d)
        self.assertEqual(d["attached"], 0)
        self.assertEqual(d["results"]["smith2020"]["status"], "failed")
        self.assertFalse(d["batch_id"])
        man = journal.load(self.vault, self.cfg)
        self.assertEqual([o for o in man["ops"] if o["kind"] == "zotero_attachment_create"], [])

    def test_the_kind_is_never_evicted_and_reverts_by_trash(self):
        from tagging import journal, zotero_write
        from tagging.writers import _revert_zotero_attachment_create
        self.assertIn("zotero_attachment_create", journal.EXTERNAL_OP_KINDS)
        op = {"kind": "zotero_attachment_create", "item_key": "NEWATT1",
              "lib_path": "users/7", "base_url": "https://api.zotero.org",
              "transport": "local", "server_id": "fixture", "state": "applied", "path": str(self.pdf),
              "parent_item": "PARENT1", "title": "PDF", "version_after": 44,
              "creation_data": {"itemType": "attachment", "path": str(self.pdf),
                                "parentItem": "PARENT1", "title": "PDF", "linkMode": "linked_file"}}
        with mock.patch.object(zotero_write, "get_item",
                               return_value=({"itemType": "attachment",
                                              "path": str(self.pdf), "parentItem": "PARENT1",
                                              "title": "PDF", "linkMode": "linked_file"}, 44)), \
             mock.patch.object(zotero_write, "get_item_children", return_value=[]), \
             mock.patch.object(zotero_write, "set_item_deleted", return_value=45) as setd:
            res = _revert_zotero_attachment_create(self.vault, self.cfg, op)
        self.assertTrue(res["ok"], res)
        self.assertEqual(op["state"], "reverted")
        self.assertIs(setd.call_args[0][3], True)      # deleted=True — a trash
        # Re-pointed since the write → skip, never trash someone's new link.
        op2 = dict(op, state="applied")
        with mock.patch.object(zotero_write, "get_item",
                               return_value=({"itemType": "attachment", "path": "/elsewhere.pdf"}, 44)), \
             mock.patch.object(zotero_write, "set_item_deleted") as setd:
            res = _revert_zotero_attachment_create(self.vault, self.cfg, op2)
        self.assertEqual(res["status"], "skipped")
        setd.assert_not_called()

    def test_candidates_route_reports_to_link_and_group_rows(self):
        from audit.manager import audit_manager
        from tagging import zotero_write
        att = SimpleNamespace(item_key="ATT1", parent_key="PARENT1", link_mode="linked_file",
                              raw_path=str(self.pdf))
        inv = SimpleNamespace(records={"smith2020": self._rec(),
                                       "g2020": self._rec("g2020", library="group")},
                              zotero_attachments=[att])
        transport = SimpleNamespace(kind="web", can_write=True)
        with mock.patch.object(audit_manager, "get_inventory", return_value=(inv, None)), \
             mock.patch("api.routes.zotlink.load_config", return_value={}), \
             mock.patch("api.routes.zotlink.zotero_renaming_plugins", return_value=[]), \
             mock.patch.object(zotero_write, "resolve_transport", return_value=transport):
            r = self.client.get("/api/zotlink/attach-candidates", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual((d["count"], d["group_count"], d["to_link"]), (2, 1, 1))
        rows = {x["citation_key"]: x for x in d["rows"]}
        self.assertTrue(rows["smith2020"]["pdfs"][0]["linked"])
        self.assertEqual(rows["g2020"]["to_link"], 1)


class TestRepoint(_ZotlinkFixture):
    """Re-pointing a BROKEN linked attachment (2026-09-07).

    A linked attachment is only a path, so anything that renames or moves the
    file orphans it with no sign anywhere: Zotero's own rename preference did
    that to five vault files on 2026-09-06 and the Attanger plugin to four
    more the next day, and all nine were repaired by hand with exactly this
    call. It shares the ``zotero_attachment_repath`` op and revert with the
    rename path, so there is one definition of "move this pointer".
    """

    def _linked_row(self, item_key="ATT1", path=None):
        r = _row(item_key, owned=False, link_mode="linked_file")
        r.raw_path = path or str(self.vault / "biblio" / "old_2020.pdf")
        return r

    def _settings(self):
        biblio = self.vault / "biblio"
        biblio.mkdir(parents=True, exist_ok=True)
        (biblio / "smith_2020.pdf").write_bytes(b"%PDF-1.4 a")
        (biblio / "jones_2021.pdf").write_bytes(b"%PDF-1.4 b")
        return SimpleNamespace(vault_root=self.vault, biblio_articles_dir=biblio,
                               attachments_subdir="", biblio_articles_subdir="biblio")

    def _ctx_with_settings(self, rows, settings):
        import contextlib
        from audit.manager import audit_manager
        from rag.vault import obsidian_manager
        inv = SimpleNamespace(zotero_attachments=list(rows))
        st = contextlib.ExitStack()
        st.enter_context(mock.patch("api.routes.zotlink.load_config",
                                    return_value=self.cfg))
        st.enter_context(mock.patch.object(audit_manager, "get_inventory",
                                           return_value=(inv, settings)))
        st.enter_context(mock.patch.object(obsidian_manager, "try_acquire_lock",
                                           return_value=1))
        st.enter_context(mock.patch.object(obsidian_manager, "release_lock"))
        return st

    def test_candidates_rank_against_the_name_zotero_remembers(self):
        settings = self._settings()
        row = self._linked_row(path=str(settings.biblio_articles_dir / "gone.pdf"))
        row.filename = "smith_2020.pdf"
        with self._ctx_with_settings([row], settings):
            r = self.client.get("/api/zotlink/repoint-candidates?item_key=ATT1",
                                headers=_HEADERS)
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertFalse(d["stored_exists"])
        self.assertEqual(d["candidates"][0]["rel"], "smith_2020.pdf")
        self.assertEqual(d["total"], 2)

    def test_candidates_refuse_a_key_the_scan_never_saw(self):
        settings = self._settings()
        with self._ctx_with_settings([self._linked_row()], settings):
            r = self.client.get("/api/zotlink/repoint-candidates?item_key=NOPE",
                                headers=_HEADERS)
        self.assertEqual(r.status_code, 404)

    def test_candidates_refuse_an_imported_attachment(self):
        """Zotero owns those bytes; its `path` is a storage: reference, so
        rewriting it would break the item rather than repair it."""
        settings = self._settings()
        row = _row("ATT1", owned=True, link_mode="imported_file")
        with self._ctx_with_settings([row], settings):
            r = self.client.get("/api/zotlink/repoint-candidates?item_key=ATT1",
                                headers=_HEADERS)
        self.assertEqual(r.status_code, 400)
        self.assertIn("linked file", r.get_json()["error"])

    def _repoint(self, body, rows, settings, *, get_item=None, set_path=None):
        from tagging import zotero_write
        transport = SimpleNamespace(kind="web", base_url="https://api.zotero.org",
                                    api_key="k", is_web=True, can_write=True)
        with self._ctx_with_settings(rows, settings) as st:
            st.enter_context(mock.patch.object(
                zotero_write, "resolve_transport", return_value=transport))
            st.enter_context(mock.patch.object(
                zotero_write, "resolve_user_id", return_value=7))
            st.enter_context(mock.patch.object(
                zotero_write, "get_item",
                side_effect=get_item or (lambda *a, **k: (
                    {"itemType": "attachment", "linkMode": "linked_file",
                     "parentItem": "PARENT1", "path": "/old/gone.pdf"}, 42))))
            self.set_path = st.enter_context(mock.patch.object(
                zotero_write, "set_attachment_path",
                side_effect=set_path or (lambda *a, **k: 43)))
            return self.client.post("/api/zotlink/repoint", json=body,
                                    headers=_HEADERS)

    def test_repoint_patches_the_path_and_journals_it(self):
        from tagging import journal as tjournal
        settings = self._settings()
        r = self._repoint({"item_key": "ATT1", "rel": "smith_2020.pdf",
                           "confirm": True}, [self._linked_row()], settings)
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertTrue(d["ok"])
        target = str((settings.biblio_articles_dir / "smith_2020.pdf").resolve())
        self.assertEqual(self.set_path.call_args[0][3], target)
        op = tjournal.find_op(tjournal.load(self.vault, self.cfg), d["op_id"])
        self.assertEqual(op["kind"], "zotero_attachment_repath")
        self.assertEqual((op["state"], op["before_path"], op["after_path"]),
                         ("applied", "/old/gone.pdf", target))
        self.assertEqual(op["version_after"], 43)

    def test_repoint_is_confirm_gated_and_scoped_to_the_biblio_folder(self):
        settings = self._settings()
        r = self._repoint({"item_key": "ATT1", "rel": "smith_2020.pdf"},
                          [self._linked_row()], settings)
        self.assertEqual(r.status_code, 400)
        r = self._repoint({"item_key": "ATT1", "rel": "../../etc/passwd",
                           "confirm": True}, [self._linked_row()], settings)
        self.assertIn(r.status_code, (400, 404))

    def test_a_failed_patch_leaves_the_op_failed(self):
        from tagging import journal as tjournal
        from tagging import zotero_write
        settings = self._settings()

        def boom(*a, **k):
            raise zotero_write.ZoteroVersionConflict("changed")

        r = self._repoint({"item_key": "ATT1", "rel": "smith_2020.pdf",
                           "confirm": True}, [self._linked_row()], settings,
                          set_path=boom)
        self.assertEqual(r.status_code, 409)
        d = r.get_json()
        op = tjournal.find_op(tjournal.load(self.vault, self.cfg), d["op_id"])
        self.assertEqual(op["state"], "failed")

    def test_the_revert_puts_the_stored_path_back(self):
        from tagging import writers as zw
        from tagging import zotero_write
        op = {"op_id": "1", "kind": "zotero_attachment_repath", "item_key": "ATT1",
              "lib_path": "users/7", "base_url": "https://api.zotero.org",
              "transport": "local", "before_path": "/old/gone.pdf",
              "after_path": "/new/there.pdf", "state": "applied"}
        with mock.patch.object(zotero_write, "get_item",
                               return_value=({"path": "/new/there.pdf"}, 44)), \
             mock.patch.object(zotero_write, "set_attachment_path",
                               return_value=45) as patched:
            res = zw._revert_zotero_attachment_repath(self.vault, self.cfg, op)
        self.assertTrue(res["ok"])
        self.assertEqual(op["state"], "reverted")
        self.assertEqual(patched.call_args[0][3], "/old/gone.pdf")

    def test_the_revert_refuses_when_someone_repointed_it_again(self):
        """Putting OUR old path back would silently discard their decision —
        the conservative-revert rule the note and tag reverts follow."""
        from tagging import writers as zw
        from tagging import zotero_write
        op = {"op_id": "1", "kind": "zotero_attachment_repath", "item_key": "ATT1",
              "lib_path": "users/7", "base_url": "https://api.zotero.org",
              "transport": "local", "before_path": "/old/gone.pdf",
              "after_path": "/new/there.pdf", "state": "applied"}
        with mock.patch.object(zotero_write, "get_item",
                               return_value=({"path": "/somewhere/else.pdf"}, 44)), \
             mock.patch.object(zotero_write, "set_attachment_path") as patched:
            res = zw._revert_zotero_attachment_repath(self.vault, self.cfg, op)
        patched.assert_not_called()
        self.assertEqual(op["state"], "applied")     # retryable, not spent
        self.assertIn("re-pointed again", res["message"])


class TestPrefStringDecoding(unittest.TestCase):
    """prefs.js is UTF-8 with raw non-ASCII; the reader must not round-trip it
    through a byte-oriented codec.

    The shipped reader used ``.encode("utf-8").decode("unicode_escape")``,
    which is latin-1-based: a rule naming ``pérez`` came back as ``pÃ©rez`` —
    a valid regex matching nothing. In this gate that means every PDF is
    skipped as ``plugin_would_rename`` and the run writes nothing while
    blaming the files, which is the "a rule that protects nothing" failure
    :class:`TestAttangerRuleDiagnostics` covers, reached by a route the
    diagnostic cannot see (the rule looks clean; only its BYTES are wrong).
    """

    def test_non_ascii_rule_survives(self):
        from api.routes import zotlink as zl
        self.assertEqual(zl._unescape_pref_string(r"^[a-z]*_p\u00e9rez_\\d{4}$"),
                         "^[a-z]*_p\u00e9rez_\\d{4}$")
        self.assertEqual(zl._unescape_pref_string("^[a-z]*_p\u00e9rez$"),
                         "^[a-z]*_p\u00e9rez$")

    def test_both_spellings_of_a_regex_escape_converge(self):
        """A rule may reach prefs.js as Mozilla-escaped ``\\d`` or, hand-edited,
        as a bare ``\d``. Both must mean the regex ``\d`` — an unknown escape is
        kept verbatim rather than swallowed."""
        from api.routes import zotlink as zl
        self.assertEqual(zl._unescape_pref_string(r"^x_\\d{4}$"), r"^x_\d{4}$")
        self.assertEqual(zl._unescape_pref_string(r"^x_\d{4}$"), r"^x_\d{4}$")

    def test_standard_escapes_decode(self):
        from api.routes import zotlink as zl
        self.assertEqual(zl._unescape_pref_string(r'a\"b'), 'a"b')
        self.assertEqual(zl._unescape_pref_string(r"a\nb"), "a\nb")
        self.assertEqual(zl._unescape_pref_string(r"a\x41b"), "aAb")
        self.assertEqual(zl._unescape_pref_string("plain"), "plain")

    def test_an_accented_rule_read_from_prefs_still_protects(self):
        """End to end through the reader: the vault holds
        ``pérez-carbonell_2022.pdf``, so an accented rule is reachable."""
        from api.routes import zotlink as zl
        with tempfile.TemporaryDirectory() as td:
            pj = Path(td) / "prefs.js"
            pj.write_text(
                'user_pref("extensions.zotero.zoteroattanger.'
                'filenameSkipRenameRules", "^p\u00e9rez_\\\\d{4}$");\n',
                encoding="utf-8")
            with mock.patch.object(zl, "_zotero_prefs_paths", return_value=[pj]):
                rules = zl.attanger_skip_rules()
                self.assertEqual(rules, ["^p\u00e9rez_\\d{4}$"])
                self.assertTrue(zl._rule_matches(rules, "p\u00e9rez_2022"))
                self.assertEqual(zl.attanger_rule_problems(), [])


class TestAttangerRuleDiagnostics(unittest.TestCase):
    """A skip rule that protects NOTHING, and one that protects at a cost.

    Both were met live on 2026-09-07 and neither announces itself: Attanger's
    preference pane renders a broken rule identically to a working one and the
    plugin logs nothing, because the padded form is still a VALID regex.
    """

    def _prefs(self, td, **fields):
        pj = Path(td) / "prefs.js"
        lines = []
        for field, value in fields.items():
            pref = ("extensions.zotero.zoteroattanger.filenameSkipRenameRules"
                    if field == "rename"
                    else "extensions.zotero.zoteroattanger."
                         "filenameSkipAutoMoveRenameRules")
            lines.append(f'user_pref("{pref}", "{value}");\n')
        pj.write_text("".join(lines), encoding="utf-8")
        return pj

    def test_a_padded_rule_is_read_verbatim_and_protects_nothing(self):
        """Attanger's getRuleList splits on `,\\s*` and does NOT trim, so a
        pasted trailing space makes `…$ ` — valid, unmatchable. Trimming it in
        this app would claim a protection the plugin does not give, so the
        reader must keep it verbatim and the diagnostic must flag it."""
        from api.routes import zotlink as zl
        with tempfile.TemporaryDirectory() as td:
            pj = self._prefs(td, rename=r"^[a-z]+_\\d{4}$ ")
            with mock.patch.object(zl, "_zotero_prefs_paths", return_value=[pj]):
                rules = zl.attanger_skip_rules()
                self.assertEqual(rules, ["^[a-z]+_\\d{4}$ "])   # NOT trimmed
                # The rule is the reason nothing is protected.
                self.assertFalse(zl._rule_matches(rules, "smith_2020"))
                self.assertTrue(zl._rule_matches([rules[0].strip()], "smith_2020"))
                problems = zl.attanger_rule_problems()
        self.assertEqual([p["kind"] for p in problems], ["padded"])
        self.assertTrue(problems[0]["fatal"])
        self.assertEqual(problems[0]["field"], "rename")

    def test_a_clean_rule_reports_no_problem(self):
        from api.routes import zotlink as zl
        with tempfile.TemporaryDirectory() as td:
            pj = self._prefs(td, rename=r"^[a-z]+_\\d{4}$")
            with mock.patch.object(zl, "_zotero_prefs_paths", return_value=[pj]):
                self.assertEqual(zl.attanger_rule_problems(), [])
                self.assertTrue(zl._rule_matches(zl.attanger_skip_rules(),
                                                 "smith_2020"))

    def test_a_rule_only_in_the_auto_move_field_is_advisory_not_fatal(self):
        """It DOES stop the rename, so the run is safe — but Attanger returns
        from processAddedAttachment before the move, so a connector download is
        never moved into the vault. Warn; never refuse."""
        from api.routes import zotlink as zl
        with tempfile.TemporaryDirectory() as td:
            pj = self._prefs(td, auto_move=r"^[a-z]+_\\d{4}$")
            with mock.patch.object(zl, "_zotero_prefs_paths", return_value=[pj]):
                problems = zl.attanger_rule_problems()
                by_field = zl.attanger_skip_rules_by_field()
                self.assertTrue(zl._rule_matches(zl.attanger_skip_rules(),
                                                 "smith_2020"))
        self.assertEqual([p["kind"] for p in problems], ["auto_move_only"])
        self.assertFalse(problems[0]["fatal"])
        self.assertEqual(by_field["rename"], [])
        self.assertEqual(by_field["auto_move"], ["^[a-z]+_\\d{4}$"])

    def test_the_recommended_field_alone_is_not_flagged(self):
        from api.routes import zotlink as zl
        with tempfile.TemporaryDirectory() as td:
            pj = self._prefs(td, rename=zl.SUGGESTED_SKIP_RULE.replace("\\", "\\\\"),
                             auto_move=zl.SUGGESTED_SKIP_RULE.replace("\\", "\\\\"))
            with mock.patch.object(zl, "_zotero_prefs_paths", return_value=[pj]):
                self.assertEqual(zl.attanger_rule_problems(), [])


class TestLinkedAttachmentFreshness(unittest.TestCase):
    def test_wal_only_change_invalidates_cached_lookup(self):
        from tagging import writers
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "zotero.sqlite"
            db.write_bytes(b"fixture database")
            wal = Path(str(db) + "-wal")
            wal.write_bytes(b"first")
            settings = SimpleNamespace(zotero_sqlite=db, zotero_storage=tmp)
            attachment = SimpleNamespace(link_mode="linked_file", raw_path="/paper.pdf", item_key="ATT1", title="PDF", annotation_count=0)
            parent = SimpleNamespace(attachments=[attachment], item_key="PARENT1", library_type="user", group_id=None)
            with mock.patch.object(writers, "_LINKED_BY_PATH", None), mock.patch("audit.core.zotero.read_items", side_effect=[[], [parent]]) as read:
                self.assertEqual(writers.find_linked_attachments(settings, "/paper.pdf"), ([], None))
                wal.write_bytes(b"second transaction")
                refs, error = writers.find_linked_attachments(settings, "/paper.pdf")
                self.assertIsNone(error)
                self.assertEqual([r.item_key for r in refs], ["ATT1"])
                self.assertEqual(read.call_count, 2)

    def test_database_change_during_read_is_not_cached_as_empty(self):
        from tagging import writers
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "zotero.sqlite"
            db.write_bytes(b"fixture database")
            settings = SimpleNamespace(zotero_sqlite=db, zotero_storage=tmp)
            def changing(*args):
                Path(str(db) + "-wal").write_bytes(b"new transaction")
                return []
            with mock.patch.object(writers, "_LINKED_BY_PATH", None), mock.patch("audit.core.zotero.read_items", side_effect=changing):
                refs, error = writers.find_linked_attachments(settings, "/paper.pdf")
                self.assertEqual(refs, [])
                self.assertIn("changed", error)
                self.assertIsNone(writers._LINKED_BY_PATH)

    def test_nfd_and_case_variants_match_the_same_file(self):
        """The scan pairs by realpath+NFC+casefold; a byte-exact lookup would
        miss the link and a rename would leave Zotero pointing at a missing
        file (2026-09-19 review A1)."""
        import unicodedata
        from tagging import writers
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "zotero.sqlite"
            db.write_bytes(b"fixture database")
            settings = SimpleNamespace(zotero_sqlite=db, zotero_storage=tmp)
            pdf = Path(tmp) / "café_2020.pdf"
            pdf.write_bytes(b"%PDF")
            stored = unicodedata.normalize("NFD", str(pdf)).upper().replace(".PDF", ".pdf")
            self.assertNotEqual(stored, str(pdf))
            attachment = SimpleNamespace(
                link_mode="linked_file", raw_path=stored, item_key="ATT1",
                title="PDF", annotation_count=0)
            parent = SimpleNamespace(
                attachments=[attachment], item_key="PARENT1",
                library_type="user", group_id=None)
            with mock.patch.object(writers, "_LINKED_BY_PATH", None), \
                    mock.patch("audit.core.zotero.read_items", return_value=[parent]):
                refs, error = writers.find_linked_attachments(settings, str(pdf))
            self.assertIsNone(error)
            self.assertEqual([r.item_key for r in refs], ["ATT1"])

    def test_a_case_variant_sibling_does_not_ride_the_same_rename(self):
        """The fold is lossy: on a CASE-SENSITIVE volume `A.pdf` and `a.pdf`
        are two files that land in one bucket, and a rename would repath both
        — aiming one item's pointer at a file it never owned. The scan's
        `zotero_link_index` drops such a clash; here the caller named an exact
        file, so the exact match wins (2026-09-20)."""
        from tagging import writers
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "zotero.sqlite"
            db.write_bytes(b"fixture database")
            settings = SimpleNamespace(zotero_sqlite=db, zotero_storage=tmp)
            lower = Path(tmp) / "paper_2020.pdf"
            lower.write_bytes(b"%PDF one")
            upper_raw = str(Path(tmp) / "PAPER_2020.pdf")
            atts = [
                SimpleNamespace(link_mode="linked_file", raw_path=str(lower),
                                item_key="ATT_LOWER", title="l", annotation_count=0),
                SimpleNamespace(link_mode="linked_file", raw_path=upper_raw,
                                item_key="ATT_UPPER", title="u", annotation_count=0),
            ]
            parent = SimpleNamespace(attachments=atts, item_key="PARENT1",
                                     library_type="user", group_id=None)
            with mock.patch.object(writers, "_LINKED_BY_PATH", None), \
                    mock.patch("audit.core.zotero.read_items", return_value=[parent]):
                refs, error = writers.find_linked_attachments(settings, str(lower))
            self.assertIsNone(error)
            self.assertEqual([r.item_key for r in refs], ["ATT_LOWER"])


class TestLocalZoteroAuthorization(unittest.TestCase):
    def test_status_does_not_request_authorization(self):
        from tagging import local_api
        with mock.patch("api.routes.zotlink.load_config", return_value={}), mock.patch.object(local_api, "capabilities", return_value={"supports_writes": True, "server_id": "fixture"}), mock.patch.object(local_api, "authorize") as authorize:
            response = _app().test_client().get("/api/zotlink/local-status", headers=_HEADERS)
            self.assertEqual(response.status_code, 200)
            authorize.assert_not_called()

    def test_authorization_requires_confirmation_and_never_returns_error_secrets(self):
        from tagging import local_api
        client = _app().test_client()
        with mock.patch("api.routes.zotlink.load_config", return_value={}), mock.patch.object(local_api, "authorize", side_effect=ValueError("secret-from-server-body")) as authorize:
            for body in ({}, {"confirm": "true", "server_id": "fixture"}, {"confirm": True}):
                response = client.post("/api/zotlink/authorize-local", json=body, headers=_HEADERS)
                self.assertEqual(response.status_code, 400)
            authorize.assert_not_called()
            response = client.post("/api/zotlink/authorize-local", json={"confirm": True, "server_id": "fixture"}, headers=_HEADERS)
            self.assertEqual(response.status_code, 409)
            self.assertNotIn("secret-from-server-body", response.get_data(as_text=True))

    def test_create_journal_retains_database_identity(self):
        from tagging import writers
        prepared = {"commit": {"server_id": "fixture", "transport": SimpleNamespace(base_url="http://127.0.0.1:23119", kind="local"), "lib_path": "users/0", "parent_key": "PARENT1", "path": "/fixture.pdf", "title": "PDF"}}
        manifest = {"ops": []}
        op = writers.zotero_attachment_create_journal(prepared=prepared, citation_key="fixture", att_key="ATT1", version=2, manifest=manifest)
        self.assertEqual(op["server_id"], "fixture")
        self.assertNotIn("api_key", op)


class _FakeTitles:
    """A stateful Zotero for the retitle tests: items, versions, a 412 guard."""

    def __init__(self, items):
        self.items = {k: dict(v) for k, v in items.items()}
        self.versions = {k: 100 for k in items}
        self.patches = []

    def get_item(self, base, lib, key, **_k):
        from tagging import zotero_write
        if key not in self.items:
            raise zotero_write.ZoteroError("HTTP 404")
        return dict(self.items[key]), self.versions[key]

    def set_attachment_title(self, base, lib, key, title, version, **_k):
        from tagging import zotero_write
        if version != self.versions[key]:
            raise zotero_write.ZoteroVersionConflict("412")
        self.patches.append((key, title))
        self.items[key]["title"] = title
        self.versions[key] += 1
        return self.versions[key]


class TestRetitle(_ZotlinkFixture):
    """T4 (annual cleanup, 2026-09-18): attachment title := its filename.

    Every rule here is load-bearing and was proven red against its own
    removal: the title comes from Zotero's PATH, never the request; each op
    is journalled with the before-title inline, under one batch id; a dry run
    writes nothing; the revert refuses a title changed again after us; and
    both restore routes (single op, batch) dispatch the kind.
    """

    def setUp(self):
        super().setUp()
        self._env = mock.patch.dict("os.environ", {"ZOTERO_API_KEY": "k" * 24})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _linked(self, key):
        r = _row(key, owned=False, link_mode="linked_file")
        r.raw_path = f"/v/biblio/{key.lower()}_2020.pdf"
        return r

    def _fake(self, **titles):
        return _FakeTitles({k: {"itemType": "attachment", "linkMode": "linked_file",
                                "parentItem": "P", "title": t,
                                "path": f"/v/biblio/{k.lower()}_2020.pdf"}
                            for k, t in titles.items()})

    def _retitle(self, body, rows, fake, *, patches=()):
        import json as _json
        from tagging import zotero_write
        transport = SimpleNamespace(kind="web", base_url="https://api.zotero.org",
                                    api_key="k", is_web=True, can_write=True)
        with self._ctx(rows) as st:
            st.enter_context(mock.patch.object(
                zotero_write, "resolve_transport", return_value=transport))
            st.enter_context(mock.patch.object(
                zotero_write, "resolve_user_id", return_value=7))
            st.enter_context(mock.patch.object(zotero_write, "get_item",
                                               side_effect=fake.get_item))
            st.enter_context(mock.patch.object(zotero_write, "set_attachment_title",
                                               side_effect=fake.set_attachment_title))
            for p in patches:
                st.enter_context(p)
            r = self.client.post("/api/zotlink/retitle", json=body, headers=_HEADERS)
            # Drain INSIDE the patch scope (the SSE worker runs only now).
            raw = r.get_data(as_text=True)
        if r.mimetype != "text/event-stream":
            return r.status_code, r.get_json(), []
        frames = [_json.loads(x[6:]) for x in raw.splitlines()
                  if x.startswith("data: {")]
        term = [f["zotlink_retitle"] for f in frames if "zotlink_retitle" in f]
        err = [f["error"] for f in frames if "error" in f]
        return r.status_code, (term[-1] if term else {"error": err}), frames

    def _ops(self):
        from tagging import journal as tjournal
        return [o for o in tjournal.load(self.vault, self.cfg)["ops"]
                if o.get("kind") == "zotero_attachment_retitle"]

    def test_the_title_is_the_filename_zotero_holds_never_the_request(self):
        fake = self._fake(ATT1="Full Text PDF")
        status, d, _ = self._retitle(
            {"item_keys": ["ATT1"], "confirm": True, "title": "INJECTED"},
            [self._linked("ATT1")], fake)
        self.assertEqual(status, 200)
        self.assertEqual(d["retitled"], 1, d)
        self.assertEqual(fake.patches, [("ATT1", "att1_2020.pdf")])

    def test_each_op_is_journalled_with_its_before_title_under_one_batch(self):
        from tagging.journal import EXTERNAL_OP_KINDS
        fake = self._fake(ATT1="Full Text PDF", ATT2="Texte intégral")
        _s, d, _ = self._retitle({"item_keys": ["ATT1", "ATT2"], "confirm": True},
                                 [self._linked("ATT1"), self._linked("ATT2")], fake)
        ops = self._ops()
        self.assertEqual(sorted(o["before_title"] for o in ops),
                         ["Full Text PDF", "Texte intégral"])
        self.assertEqual({o["batch_id"] for o in ops}, {d["batch_id"]})
        self.assertTrue(all(o["state"] == "applied" and o["version_after"] == 101
                            for o in ops), ops)
        self.assertIn("zotero_attachment_retitle", EXTERNAL_OP_KINDS)

    def test_a_dry_run_writes_nothing(self):
        fake = self._fake(ATT1="PDF")
        status, d, _ = self._retitle({"item_keys": ["ATT1"], "dry_run": True},
                                     [self._linked("ATT1")], fake)
        self.assertEqual(status, 200)
        self.assertEqual((d["would_retitle"], d["retitled"], d["batch_id"]), (1, 0, ""))
        self.assertEqual(d["results"]["ATT1"]["title"], "att1_2020.pdf")
        self.assertEqual(fake.patches, [])
        self.assertEqual(self._ops(), [])

    def test_a_title_already_equal_is_skipped_and_not_journalled(self):
        fake = self._fake(ATT1="att1_2020.pdf")
        _s, d, _ = self._retitle({"item_keys": ["ATT1"], "confirm": True},
                                 [self._linked("ATT1")], fake)
        self.assertEqual(d["results"]["ATT1"]["status"], "skipped")
        self.assertEqual((fake.patches, self._ops()), ([], []))

    def test_a_failed_patch_leaves_the_op_failed(self):
        from tagging import zotero_write
        fake = self._fake(ATT1="PDF")

        def conflict(*a, **k):
            raise zotero_write.ZoteroVersionConflict("412")

        _s, d, _ = self._retitle(
            {"item_keys": ["ATT1"], "confirm": True}, [self._linked("ATT1")], fake,
            patches=[mock.patch.object(zotero_write, "set_attachment_title",
                                       side_effect=conflict)])
        self.assertEqual(d["retitled"], 0)
        self.assertEqual(d["results"]["ATT1"]["status"], "conflict")
        # A failed op is evictable and the finalize prune may drop it; what
        # must never survive is an APPLIED op claiming a retitle that did not
        # happen (reverting it would "restore" a title never changed).
        self.assertFalse([o for o in self._ops() if o["state"] == "applied"])

    def test_refusals_before_any_network_and_the_gate_stays_free(self):
        from api.routes import zotlink as zl
        fake = self._fake(ATT1="PDF")
        rows = [self._linked("ATT1"), _row("IMP1")]
        cases = [
            ({"item_keys": ["ATT1"]}, 400),                                   # neither
            ({"item_keys": ["ATT1"], "confirm": True, "dry_run": True}, 400),  # both
            ({"item_keys": ["NOPE"], "confirm": True}, 404),
            ({"item_keys": ["IMP1"], "confirm": True}, 400),                   # imported
            ({"item_keys": ["A"] * (zl._RETITLE_MAX_KEYS + 1), "confirm": True}, 400),
        ]
        for body, want in cases:
            status, _d, _f = self._retitle(body, rows, fake)
            self.assertEqual(status, want, body)
        self.assertEqual(fake.patches, [])
        self.assertTrue(zl._ZOTLINK_GATE.acquire(blocking=False))
        zl._ZOTLINK_GATE.release()

    def test_every_patch_runs_outside_the_op_lock(self):
        from rag.vault import obsidian_manager
        trace = []
        fake = self._fake(ATT1="PDF", ATT2="PDF")
        real_set = fake.set_attachment_title
        fake.set_attachment_title = lambda *a, **k: (trace.append("PATCH"), real_set(*a, **k))[1]
        self._retitle({"item_keys": ["ATT1", "ATT2"], "confirm": True},
                      [self._linked("ATT1"), self._linked("ATT2")], fake,
                      patches=[
                          mock.patch.object(obsidian_manager, "try_acquire_lock",
                                            side_effect=lambda **k: (trace.append("acquire"), 1)[1]),
                          mock.patch.object(obsidian_manager, "release_lock",
                                            side_effect=lambda *a: trace.append("release"))])
        first = trace.index("PATCH")
        self.assertEqual(trace[:2], ["acquire", "release"], trace)
        self.assertNotIn("acquire", trace[2:first + 2], trace)
        self.assertEqual(trace[-2:], ["acquire", "release"], trace)

    def _revert_op(self, current_title):
        from tagging import writers as zw
        from tagging import zotero_write
        op = {"op_id": "1", "kind": "zotero_attachment_retitle", "item_key": "ATT1",
              "lib_path": "users/7", "base_url": "https://api.zotero.org",
              "transport": "web", "before_title": "Full Text PDF",
              "after_title": "att1_2020.pdf", "state": "applied"}
        fake = self._fake(ATT1=current_title)
        with mock.patch.object(zotero_write, "get_item", side_effect=fake.get_item), \
             mock.patch.object(zotero_write, "set_attachment_title",
                               side_effect=fake.set_attachment_title):
            res = zw._revert_zotero_attachment_retitle(self.vault, self.cfg, op)
        return op, res, fake

    def test_the_revert_puts_the_old_title_back(self):
        op, res, fake = self._revert_op("att1_2020.pdf")
        self.assertTrue(res["ok"], res)
        self.assertEqual((op["state"], fake.items["ATT1"]["title"]),
                         ("reverted", "Full Text PDF"))

    def test_the_revert_refuses_a_title_changed_again_after_us(self):
        op, res, fake = self._revert_op("A later human choice")
        self.assertEqual(fake.patches, [])
        self.assertEqual(op["state"], "applied")          # retryable, not spent
        self.assertIn("retitled again", res["message"])

    def _restore(self, body, fake):
        import json as _json
        from api.routes.tagging import tagging_bp
        from rag.vault import obsidian_manager
        from tagging import zotero_write
        app = flask.Flask(__name__)
        app.register_blueprint(tagging_bp)
        with mock.patch("api.routes.tagging.load_config", return_value=self.cfg), \
             mock.patch("api.routes.tagging._vault_root", return_value=self.vault), \
             mock.patch.object(obsidian_manager, "try_acquire_lock", return_value=1), \
             mock.patch.object(obsidian_manager, "release_lock"), \
             mock.patch.object(obsidian_manager, "heartbeat", return_value=True), \
             mock.patch.object(zotero_write, "get_item", side_effect=fake.get_item), \
             mock.patch.object(zotero_write, "set_attachment_title",
                               side_effect=fake.set_attachment_title):
            r = app.test_client().post("/api/tagging/restore", json=body,
                                       headers=_HEADERS)
            raw = r.get_data(as_text=True)
        if r.mimetype == "text/event-stream":
            return [_json.loads(x[6:]) for x in raw.splitlines() if x.startswith("data: {")]
        return r.get_json()

    def test_both_restore_routes_revert_a_retitle(self):
        fake = self._fake(ATT1="Full Text PDF", ATT2="PDF")
        _s, d, _ = self._retitle({"item_keys": ["ATT1", "ATT2"], "confirm": True},
                                 [self._linked("ATT1"), self._linked("ATT2")], fake)
        one = next(o for o in self._ops() if o["item_key"] == "ATT1")
        single = self._restore({"op_id": one["op_id"], "confirm": True}, fake)
        self.assertTrue(single.get("ok"), single)
        self.assertEqual(fake.items["ATT1"]["title"], "Full Text PDF")
        frames = self._restore({"batch_id": d["batch_id"], "confirm": True}, fake)
        term = [f["restore"] for f in frames if "restore" in f]
        self.assertTrue(term, frames)
        self.assertEqual(fake.items["ATT2"]["title"], "PDF")
