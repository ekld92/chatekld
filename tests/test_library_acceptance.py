"""Disposable production-route integration; provider/HTTP/xattr boundaries fake only."""

import os
import json
import time
import sqlite3
import subprocess
import urllib.parse
from pathlib import Path
from contextlib import ExitStack
from unittest import mock
import flask
import test_library_imports as fixtures
from core.constants import CONFIG_FILE
from core.llm.base import StreamingResponse
from audit.manager import audit_manager


# This integration harness intentionally exercises the private rename routes.
import importlib.util
import pytest

if importlib.util.find_spec('maintenance') is None:
    pytest.skip('Private Maintenance integration is absent from this artifact', allow_module_level=True)



requires_birthtime = pytest.mark.skipif(
    not hasattr(Path(__file__).stat(), "st_birthtime"),
    reason="Requires stable file birth time; exercised by macos-platform CI.")

class Reply:
    def __init__(self, payload, version=1):
        self.payload = payload
        self.headers = {"Last-Modified-Version": str(version), "Zotero-Server-ID": "fixture-db"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self):
        return json.dumps(self.payload).encode()


class Harness:
    def __init__(self, root):
        self.root = root
        self.lib = fixtures.Library(root)
        self.calls = []
        self.writes = []
        self.tags = {}
        self.versions = {}
        self.fail_tags = False
        self.lib.settings.zotero_notes_dir.mkdir()
        self.cfg = {
            "obsidian_vault_path": str(self.lib.vault),
            "audit_zotero_sqlite": str(self.lib.db),
            "audit_zotero_storage": str(root / "storage"),
            "notesync_zotero_authoritative": True,
            "tagging_zotero_transport": "local",
            "tagging_archive_dir": str(root / "tags-archive"),
            "refactor_archive_dir": str(root / "refactor-archive"),
            "tagging_candidate_tags": 0,
            "tagging_fewshot_k": 0,
            "provider": "ollama",
            "llm": "fixture-model",
            "tagging_propose_max_tokens": 128,
        }
        self.previous_config = (
            Path(CONFIG_FILE).read_bytes() if Path(CONFIG_FILE).exists() else None
        )
        Path(CONFIG_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(CONFIG_FILE).write_text(json.dumps(self.cfg))
        (self.lib.vault / "_tags.md").write_text(
            "| Description | Tags |\n|---|---|\n| Depression | #dep |\n"
        )
        (self.lib.vault / "_master.bib").write_text("")
        self.app = flask.Flask(__name__)
        from api.routes.audit import audit_bp
        from api.routes.library_workflow import library_workflow_bp
        from api.routes.maintenance import maintenance_bp
        from api.routes.tagging import tagging_bp
        from api.routes.notesync import notesync_bp

        for bp in (
            audit_bp,
            library_workflow_bp,
            maintenance_bp,
            tagging_bp,
            notesync_bp,
        ):
            self.app.register_blueprint(bp)
        self.client = self.app.test_client()

    def __enter__(self):
        self.st = ExitStack()
        self.st.enter_context(
            mock.patch.dict(os.environ, {"ZOTERO_API_KEY": "fixture-only-no-real-key"})
        )
        # The records come from the fixture SQLite database, so keep the
        # HTTP transport and authorization bound to that same Desktop ID.
        from tagging import local_api, zotero_write
        self.st.enter_context(mock.patch.dict(local_api._grants, {
            (local_api.origin(zotero_write.DEFAULT_BASE_URL), "fixture-db"):
                ("F" * 32, True),
        }, clear=True))
        self.st.enter_context(
            mock.patch("tagging.zotero_write._opener.open", side_effect=self.http)
        )
        self.st.enter_context(
            mock.patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("unexpected real network"),
            )
        )
        self.st.enter_context(
            mock.patch(
                "requests.sessions.Session.request",
                side_effect=AssertionError("unexpected real network"),
            )
        )
        self.st.enter_context(
            mock.patch("core.llm.factory.get_llm_provider", return_value=self)
        )
        self.st.enter_context(
            mock.patch(
                "api.routes._finder_tagwrite.read_finder_tags_full",
                side_effect=lambda p: list(self.tags.get(Path(p).stat().st_ino, [])),
            )
        )
        self.st.enter_context(
            mock.patch(
                "api.routes._finder_tagwrite.write_finder_tags", side_effect=self.xattr
            )
        )
        self.st.enter_context(
            mock.patch("api.routes._finder_tagwrite.is_macos", return_value=True)
        )
        audit_manager.reset_to_idle()
        return self

    def __exit__(self, *a):
        audit_manager.reset_to_idle()
        self.st.close()
        if self.previous_config is None:
            Path(CONFIG_FILE).unlink(missing_ok=True)
        else:
            Path(CONFIG_FILE).write_bytes(self.previous_config)

    def stream(self, request):
        self.calls.append(request)
        return StreamingResponse(
            response_gen=iter(['[{"tag":"dep","confidence":0.9}]'])
        )

    def xattr(self, p, tags):
        self.tags[Path(p).stat().st_ino] = list(tags)
        self.writes.append(("finder", str(p)))

    def http(self, req, timeout):
        path = urllib.parse.urlparse(req.full_url).path
        if path.endswith("/keys/current"):
            return Reply({"userID": 42, "access": {"user": {"write": True}}})
        key = path.split("/")[-1]
        with sqlite3.connect(self.lib.db) as db:
            iid, typ, version = db.execute(
                "SELECT itemID,itemTypeID,version FROM items WHERE key=?", (key,)
            ).fetchone()
            data = {
                "key": key,
                "version": version,
                "tags": [
                    {"tag": x[0]}
                    for x in db.execute(
                        "SELECT t.name FROM tags t JOIN itemTags it USING(tagID) WHERE it.itemID=?",
                        (iid,),
                    )
                ],
            }
            if typ == 2:
                parent, pdf = db.execute(
                    "SELECT parentItemID,path FROM itemAttachments WHERE itemID=?",
                    (iid,),
                ).fetchone()
                data.update(
                    itemType="attachment",
                    linkMode="linked_file",
                    path=pdf,
                    parentItem=db.execute(
                        "SELECT key FROM items WHERE itemID=?", (parent,)
                    ).fetchone()[0],
                    title="fixture",
                )
            else:
                data.update(itemType="journalArticle")
            if req.get_method() == "PATCH":
                body = json.loads(req.data)
                if self.fail_tags and "tags" in body:
                    raise OSError("injected network cut")
                expected = int(req.get_header("If-unmodified-since-version"))
                assert expected == version
                if "path" in body:
                    db.execute(
                        "UPDATE itemAttachments SET path=? WHERE itemID=?",
                        (body["path"], iid),
                    )
                if "tags" in body:
                    db.execute("DELETE FROM itemTags WHERE itemID=?", (iid,))
                    for t in body["tags"]:
                        tagid = db.execute(
                            "SELECT COALESCE(MAX(tagID),0)+1 FROM tags"
                        ).fetchone()[0]
                        db.execute("INSERT INTO tags VALUES (?,?)", (tagid, t["tag"]))
                        db.execute("INSERT INTO itemTags VALUES (?,?)", (iid, tagid))
                db.execute("UPDATE items SET version=version+1 WHERE itemID=?", (iid,))
                version += 1
                self.writes.append(("zotero", key, body))
            return Reply({"data": data, "version": version}, version)

    def add(self, key, cite, name=None, author="Smith", year=2026):
        iid = self.lib.entry(key, cite, "Depression " + cite)
        with (self.lib.vault / "_master.bib").open("a") as f:
            f.write(
                "@article{"
                + cite
                + ", title={Depression "
                + cite
                + "}, author={"
                + author
                + ", Ann}, year={"
                + str(year)
                + "}}\n"
            )
        if name:
            att, p = self.lib.pdf(iid, "P" + key[1:], name)
            import fitz

            doc = fitz.open()
            page = doc.new_page()
            page.insert_text(
                (72, 72),
                ("A study of depression and mood in adults. " + cite + "\n") * 10,
            )
            doc.save(str(p) + ".tmp")
            doc.close()
            os.replace(str(p) + ".tmp", p)
            return iid, att, p
        return iid, None, None

    def post(self, path, body):
        r = self.client.post(path, json=body, headers={"X-Requested-With": "ChatEKLD"})
        assert r.status_code == 200, (path, r.status_code, r.get_data(as_text=True))
        if r.mimetype == "text/event-stream":
            return [
                json.loads(s[6:])
                for s in r.get_data(as_text=True).splitlines()
                if s.startswith("data: ") and s != "data: [DONE]"
            ]
        return r.get_json()

    def term(self, path, body, key):
        return [f[key] for f in self.post(path, body) if key in f][-1]

    def scan(self):
        self.post(
            "/api/audit/scan",
            {
                "count_annotations": False,
                "include_duplicates": False,
                "include_near_duplicates": False,
            },
        )
        end = time.time() + 30
        while audit_manager.is_scanning() and time.time() < end:
            time.sleep(0.01)
        assert not audit_manager.is_scanning()
        inv, settings = audit_manager.get_inventory()
        assert inv is not None
        return inv

    def session(self, provider="ollama", model="fixture-model"):
        choice = {"provider": provider, "model": model}
        if provider == "openai":
            choice["budget_usd"] = 1
        return self.post("/api/library/sessions", {"llm": choice})["session"]["id"]

    def analyze(self, sid, tags=True):
        return self.term(
            "/api/library/analyze",
            {"session": sid, "propose_tags": tags},
            "library_analyze",
        )

    def apply(self, sid, a):
        return self.term(
            "/api/library/apply",
            {"session": sid, "confirm": True, "approvals": a},
            "library_apply",
        )

    def git(self):
        for args in [
            ("init",),
            ("add", "."),
            (
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "fixture",
            ),
        ]:
            subprocess.run(
                ["git", "-C", str(self.lib.vault), *args],
                check=True,
                capture_output=True,
            )


@requires_birthtime
def test_real_route_acceptance(tmp_path):
    with Harness(tmp_path) as h:
        old, _, _ = h.add("AAAA1111", "old2020", author="Old", year=2020)
        h.lib.note(old, "NOTEA111", "<p>Old reading.</p>")
        h.scan()
        sid = h.session()
        initial = h.analyze(sid)
        assert initial["tags"] == [] and len(h.calls) == 0
        baseline = h.apply(
            sid, {"mirror": {"plan_digest": initial["mirror"]["plan_digest"]}}
        )
        assert baseline["mirror"]["status"] == "done", baseline
        h.git()
        one, _, _ = h.add("BBBB1111", "smithOne2026", "download-one.pdf")
        two, _, _ = h.add("CCCC1111", "smithTwo2026", "download-two.pdf")
        _, added_pdf = h.lib.pdf(old, "PDFOLD11", "old-attachment.pdf")
        import fitz

        doc = fitz.open()
        page = doc.new_page()
        page.insert_text(
            (72, 72),
            "Depression outcomes from the old cohort were substantially improved. " * 3,
        )
        doc.save(str(added_pdf) + ".tmp")
        doc.close()
        os.replace(str(added_pdf) + ".tmp", added_pdf)
        h.lib.note(one, "NOTEB111", "<p>First new note.</p>")
        h.lib.note(two, "NOTEC111", "<p>Second new note.</p>")
        with sqlite3.connect(h.lib.db) as db:
            db.execute(
                "UPDATE itemNotes SET note='<p>Updated reading.</p>' WHERE itemID=(SELECT itemID FROM items WHERE key='NOTEA111')"
            )
        inv = h.scan()
        assert len(inv.records) == 3
        analysis = h.analyze(sid)
        assert not analysis["blocked"], analysis
        names = [r["proposal"] for r in analysis["renames"] if r["can_apply"]]
        assert len(names) == len(set(names)) == 3, analysis
        assert len(h.calls) == 3, analysis
        h.analyze(sid)
        assert len(h.calls) == 3
        approvals = {
            "renames": [
                {"unit": r["unit"], "rel": r["rel"], "new_name": r["proposal"]}
                for r in analysis["renames"]
                if r["can_apply"]
            ],
            "tags": [
                {"unit": r["unit"], "citation_key": r["citation_key"], "tags": ["dep"]}
                for r in analysis["tags"]
            ],
            "mirror": {"plan_digest": analysis["mirror"]["plan_digest"]},
        }
        result = h.apply(sid, approvals)
        (tmp_path / "result.json").write_text(json.dumps(result, indent=2))
        assert all(r["status"] == "done" for r in result["renames"]), result
        assert all(r["status"] == "done" for r in result["tags"]), result
        assert result["mirror"]["status"] == "done", result
        h.scan()
        final = h.analyze(sid)
        assert final["tags"] == [], final
        assert (
            final["mirror"]["to_create"]
            == final["mirror"]["to_refresh"]
            == final["mirror"]["to_baseline"]
            == 0
        ), final
        assert len(h.calls) == 3


@requires_birthtime
def test_network_failure_retains_destination_results_and_inspected_retry(tmp_path):
    with Harness(tmp_path) as h:
        h.scan()
        sid = h.session("openai", "gpt-4o-mini")
        h.analyze(sid)
        _, _, pdf = h.add("DDDD1111", "jones2026", "download.pdf", author="Jones")
        h.scan()
        a = h.analyze(sid)
        assert len(h.calls) == 1
        h.fail_tags = True
        r = h.apply(
            sid,
            {
                "renames": [
                    {"unit": x["unit"], "rel": x["rel"], "new_name": x["proposal"]}
                    for x in a["renames"]
                    if x["can_apply"]
                ],
                "tags": [
                    {
                        "unit": x["unit"],
                        "citation_key": x["citation_key"],
                        "tags": ["dep"],
                    }
                    for x in a["tags"]
                ],
            },
        )
        assert r["renames"][0]["status"] == "done", r
        assert r["tags"][0]["targets"] == {"finder": "done", "zotero": "uncertain", "note": "skipped"}, r
        sid_record = h.client.get("/api/library/sessions/" + sid).get_json()["session"]
        assert (
            sid_record["applies"][-1]["result"]["tags"][0]["targets"]
            == r["tags"][0]["targets"]
        ), sid_record
        # Inspect both destinations before explicitly selecting the failed one.
        assert h.tags[(h.lib.biblio / "jones_2026.pdf").stat().st_ino] == ["dep"]
        with sqlite3.connect(h.lib.db) as db:
            assert db.execute("SELECT count(*) FROM itemTags").fetchone()[0] == 0
        # A transport cut cannot prove non-publication. Resolve only after
        # inspecting the fixture database, through the confirmed public route.
        resolved = h.client.post("/api/library/steps", json={
            "session": sid, "unit": a["tags"][0]["unit"],
            "step": "tag", "status": "failed", "confirm": True,
            "detail": {"inspection": "fixture itemTags is empty",
                       "targets": {"finder": "done", "zotero": "failed", "note": "skipped"}},
        })
        assert resolved.status_code == 200, resolved.get_json()
        h.fail_tags = False
        h.scan()
        b = h.analyze(sid)
        assert len(h.calls) == 1, b
        r = h.apply(
            sid,
            {
                "tags": [
                    {
                        "unit": x["unit"],
                        "citation_key": x["citation_key"],
                        "tags": ["dep"],
                        "targets": {"finder": False, "zotero": True, "note": False},
                    }
                    for x in b["tags"]
                ]
            },
        )
        assert r["tags"][0]["status"] == "done", r
        assert len([w for w in h.writes if w[0] == "finder"]) == 1
        assert len([w for w in h.writes if w[0] == "zotero" and "tags" in w[2]]) == 1
        assert h.analyze(sid)["tags"] == []


def test_refused_base_write_then_real_mirror_retry(tmp_path):
    from notesync import base_store

    with Harness(tmp_path) as h:
        h.scan()
        sid = h.session()
        h.analyze(sid)
        entry, _, _ = h.add("EEEE1111", "lee2026", author="Lee")
        h.lib.note(entry, "NOTEE111", "<p>Reading notes.</p>")
        h.scan()
        a = h.analyze(sid, tags=False)
        with mock.patch.object(
            base_store, "save", side_effect=OSError("injected base disk failure")
        ):
            failed = h.apply(
                sid, {"mirror": {"plan_digest": a["mirror"]["plan_digest"]}}
            )
        assert failed["mirror"]["status"] == "partial", failed
        assert (
            failed["mirror"]["created"] == 1 and failed["mirror"]["bases_written"] == 0
        ), failed
        note = h.lib.settings.zotero_notes_dir / "lee2026.md"
        content = note.read_bytes()
        stamp = note.stat().st_mtime_ns
        # The file landed, so retry only the missing app-state base after review.
        b = h.analyze(sid, tags=False)
        assert b["mirror"]["to_create"] == 0, b
        resumed = h.apply(sid, {"mirror": {"plan_digest": b["mirror"]["plan_digest"]}})
        assert resumed["mirror"]["status"] == "done", resumed
        assert note.read_bytes() == content and note.stat().st_mtime_ns == stamp
        assert h.analyze(sid, tags=False)["mirror"]["to_baseline"] == 0


def test_external_edit_invalidates_review_without_overwrite(tmp_path):
    with Harness(tmp_path) as h:
        entry, _, _ = h.add("FFFF1111", "wong2026", author="Wong")
        noteid = h.lib.note(entry, "NOTEF111", "<p>Original.</p>")
        h.scan()
        sid = h.session()
        a = h.analyze(sid, tags=False)
        h.apply(sid, {"mirror": {"plan_digest": a["mirror"]["plan_digest"]}})
        h.git()
        h.lib.edit_note(noteid, "<p>Changed in Zotero.</p>")
        a = h.analyze(sid, tags=False)
        note = h.lib.settings.zotero_notes_dir / "wong2026.md"
        note.write_text(note.read_text() + "\nExternal personal tail.\n")
        edited = note.read_bytes()
        refused = h.apply(sid, {"mirror": {"plan_digest": a["mirror"]["plan_digest"]}})
        assert refused["mirror"]["status"] == "failed", refused
        assert refused["mirror"]["stopped"] == "plan_changed", refused
        assert note.read_bytes() == edited
        h.git()
        reviewed = h.analyze(sid, tags=False)
        resumed = h.apply(
            sid, {"mirror": {"plan_digest": reviewed["mirror"]["plan_digest"]}}
        )
        assert resumed["mirror"]["status"] == "done", resumed
        assert "Changed in Zotero." in note.read_text() and note.read_text().endswith(
            "External personal tail.\n"
        )


def test_disconnect_keeps_landed_step_and_reopens_only_unreached_work(tmp_path):
    import threading

    with Harness(tmp_path) as h:
        h.scan()
        sid = h.session()
        h.analyze(sid)
        h.add("GGGG1111", "green2026", "green_2026.pdf", author="Green")
        h.add("HHHH1111", "hall2026", "hall_2026.pdf", author="Hall")
        h.scan()
        a = h.analyze(sid)
        approvals = {
            "tags": [
                {"unit": x["unit"], "citation_key": x["citation_key"], "tags": ["dep"]}
                for x in a["tags"]
            ]
        }
        entered = threading.Event()
        release = threading.Event()
        original = h.xattr

        def blocked(path, tags):
            original(path, tags)
            entered.set()
            assert release.wait(10)

        with mock.patch(
            "api.routes._finder_tagwrite.write_finder_tags", side_effect=blocked
        ):
            response = h.client.post(
                "/api/library/apply",
                json={"session": sid, "confirm": True, "approvals": approvals},
                headers={"X-Requested-With": "ChatEKLD"},
                buffered=False,
            )
            assert entered.wait(10)
            response.close()
            release.set()
            deadline = time.time() + 10
            while time.time() < deadline:
                saved = h.client.get("/api/library/sessions/" + sid).get_json()[
                    "session"
                ]
                if saved.get("applies"):
                    break
                time.sleep(0.01)
        result = saved["applies"][-1]["result"]
        assert (
            result["cancelled"]
            and len(result["tags"]) == 1
            and len(result["not_reached"]) == 1
        ), result
        assert result["tags"][0]["status"] == "done", result
        b = h.analyze(sid)
        assert len(b["tags"]) == 1 and len(h.calls) == 2, b
        retry = h.apply(
            sid,
            {
                "tags": [
                    {
                        "unit": x["unit"],
                        "citation_key": x["citation_key"],
                        "tags": ["dep"],
                    }
                    for x in b["tags"]
                ]
            },
        )
        assert retry["tags"][0]["status"] == "done"
        assert len([x for x in h.writes if x[0] == "finder"]) == 2
        assert h.analyze(sid)["tags"] == []


@requires_birthtime
def test_a_rename_after_the_mirror_keeps_both_the_refresh_and_the_new_name(tmp_path):
    """Follow-ups lot 2 (2026-09-22): the run mirrors FIRST and renames LAST,
    and the rename route rewrites the app-shaped `pdf:` entry in its own op —
    so a rename no longer invalidates the reviewed mirror plan (the retired
    Known Issue), and the note ends up refreshed AND pointing at the new name."""
    with Harness(tmp_path) as h:
        entry, _, _ = h.add("JJJJ1111", "jones2020", author="Jones", year=2020)
        noteid = h.lib.note(entry, "NOTEJ111", "<p>Original.</p>")
        h.scan()
        sid = h.session()
        a = h.analyze(sid, tags=False)
        h.apply(sid, {"mirror": {"plan_digest": a["mirror"]["plan_digest"]}})
        _, pdf = h.lib.pdf(entry, "PDFJ1111", "wrong-name.pdf")
        note = h.lib.settings.zotero_notes_dir / "jones2020.md"
        note.write_text(
            note.read_text().replace(
                "---\n", '---\npdf:\n  - "[[wrong-name.pdf]]"\n', 1
            )
        )
        h.git()
        h.lib.edit_note(noteid, "<p>Updated.</p>")
        h.scan()
        a = h.analyze(sid, tags=False)
        assert a["renames"][0]["can_apply"], a
        result = h.apply(
            sid,
            {
                "renames": [
                    {"unit": x["unit"], "rel": x["rel"], "new_name": x["proposal"]}
                    for x in a["renames"]
                    if x["can_apply"]
                ],
                "mirror": {"plan_digest": a["mirror"]["plan_digest"]},
            },
        )
        assert result["mirror"]["status"] == "done", result
        assert result["renames"][0]["status"] == "done", result
        text = note.read_text()
        assert "[[jones_2020.pdf]]" in text and "[[wrong-name.pdf]]" not in text
        assert "Updated." in text and "Original." not in text
        # The run's own writes left the note uncommitted; the result says so.
        assert result["notes_written"] >= 1, result


def test_201_row_preview_reserves_destinations_across_chunks(tmp_path):
    with Harness(tmp_path) as h:
        h.scan()
        sid = h.session()
        h.analyze(sid, tags=False)
        for i in range(201):
            author = (
                "Smith"
                if i in (0, 200)
                else "Author" + chr(65 + i // 26) + chr(65 + i % 26)
            )
            entry, _, _ = h.add(f"E{i:07d}", f"paper{i}2026", author=author)
            h.lib.pdf(entry, f"P{i:07d}", f"download-{i:03}.pdf")
        h.scan()
        a = h.analyze(sid, tags=False)
        rows = [r for r in a["renames"] if r["can_apply"]]
        assert len(rows) == 201, a
        names = [r["proposal"] for r in rows]
        assert len(set(names)) == len(names), [
            (r["rel"], r["proposal"]) for r in rows if r["proposal"].startswith("smith")
        ]


def test_a_record_added_after_the_scan_stays_open_until_a_rescan(tmp_path):
    """Audit remediation 2026-09-23 (L1, maintainer decision): a Zotero record
    + reading note added AFTER the last scan has no sync row, so the mirror
    cannot mirror it. Its note unit used to close `done` anyway (absent from
    `failed` and `listed`) and never reopened; now it stays open as "rescan
    needed", the rest of the run proceeds, and the run after a rescan
    mirrors and closes it."""
    with Harness(tmp_path) as h:
        h.scan()
        sid = h.session()
        h.analyze(sid, tags=False)
        entry, _, _ = h.add("KKKK1111", "late2026", author="Late")
        h.lib.note(entry, "NOTEK111", "<p>Written after the scan.</p>")
        a = h.analyze(sid, tags=False)
        assert "note:1:NOTEK111" in a["mirror_units"], a
        result = h.apply(sid, {"mirror": {"plan_digest": a["mirror"]["plan_digest"]}})
        assert result["mirror"]["open"] == [
            {"unit": "note:1:NOTEK111", "citation_key": "late2026", "reason": "rescan needed"}
        ], result
        note = h.lib.settings.zotero_notes_dir / "late2026.md"
        assert not note.exists()
        again = h.analyze(sid, tags=False)
        assert "note:1:NOTEK111" in [o["id"] for o in again["observation"]["open"]]

        h.scan()
        b = h.analyze(sid, tags=False)
        done = h.apply(sid, {"mirror": {"plan_digest": b["mirror"]["plan_digest"]}})
        assert done["mirror"]["status"] == "done" and done["mirror"]["open"] == [], done
        assert "Written after the scan." in note.read_text()
        final = h.analyze(sid, tags=False)
        assert "note:1:NOTEK111" not in [o["id"] for o in final["observation"]["open"]]


@requires_birthtime
def test_a_rename_chain_crossing_the_preview_chunk_boundary(tmp_path):
    """Audit remediation 2026-09-23 (N3): the renamer's preview is chunked at
    200 rows. Row 0 frees `smith_2026.pdf` (its file belongs to Jones); row
    200, in the NEXT chunk, is the Smith paper that wants that name. Only
    reservations used to cross a chunk, so row 200 was proposed a collision
    name (`smith_2_2026.pdf`) the single-request preview never produces."""
    with Harness(tmp_path) as h:
        h.scan()
        sid = h.session()
        h.analyze(sid, tags=False)
        h.add("J0000000", "jones2026", "smith_2026.pdf", author="Jones", year=2026)
        for i in range(1, 200):
            author = "Author" + chr(65 + i // 26) + chr(65 + i % 26)
            h.add(f"F{i:07d}", f"filler{i}2026", f"download-{i:03}.pdf", author=author)
        h.add("S0000200", "smith2026", "download-200.pdf", author="Smith", year=2026)
        h.scan()
        a = h.analyze(sid, tags=False)
        by_unit = {r["unit"]: r for r in a["renames"]}
        assert by_unit["attachment:1:P0000000"]["proposal"] == "jones_2026.pdf", a["renames"][0]
        assert by_unit["attachment:1:P0000200"]["proposal"] == "smith_2026.pdf", by_unit["attachment:1:P0000200"]
        rows = [r for r in a["renames"] if r["can_apply"]]
        result = h.apply(sid, {"renames": [{"unit": r["unit"], "rel": r["rel"],
                                            "new_name": r["proposal"]} for r in rows]})
        assert all(r["status"] == "done" for r in result["renames"]), result
        assert (h.lib.biblio / "smith_2026.pdf").is_file()
        assert (h.lib.biblio / "jones_2026.pdf").is_file()


def test_an_absent_pdf_list_is_offered_approved_and_then_current(tmp_path):
    """Audit remediation 2026-09-23 (N13): the in-process `apply-pdf-links`
    transport was the one production path no acceptance test exercised. An
    existing note without a `pdf:` list appears as a selectable `absent` row,
    the approved key gets the list through the real route, and the next
    analysis reads it `current` (no row). Coverage, not a regression test:
    the path already worked; nothing proved it end to end."""
    with Harness(tmp_path) as h:
        entry, _, _ = h.add("QQQQ1111", "park2026", "park_2026.pdf", author="Park")
        h.lib.note(entry, "NOTEQ111", "<p>Park reading.</p>")
        h.scan()
        sid = h.session()
        a = h.analyze(sid, tags=False)
        assert a["mirror"], a["blocked"]
        h.apply(sid, {"mirror": {"plan_digest": a["mirror"]["plan_digest"]}})
        note = h.lib.settings.zotero_notes_dir / "park2026.md"
        from api.routes._audit_tagwrite import read_pdf_links, set_pdf_links
        # An existing note that lost its list (e.g. written before the list
        # existed): remove it, commit, rescan so the scan knows the note.
        stripped, _why = set_pdf_links(note.read_text(), [])
        note.write_text(stripped)
        h.git()
        h.scan()
        b = h.analyze(sid, tags=False)
        rows = {r["citation_key"]: r for r in b["pdf_links"]["rows"]}
        assert rows["park2026"]["state"] == "absent" and rows["park2026"]["selectable"], b["pdf_links"]
        result = h.apply(sid, {"pdf_links": ["park2026"]})
        assert result["pdf_links"] == [{"citation_key": "park2026", "status": "done",
                                        "changed": True, "reason": None}], result
        assert read_pdf_links(note.read_text()) == ["park_2026.pdf"]
        c = h.analyze(sid, tags=False)
        assert "park2026" not in {r["citation_key"] for r in c["pdf_links"]["rows"]}


@requires_birthtime
def test_prepared_text_and_reviewed_writes_reach_final_index(tmp_path, monkeypatch):
    """Real scan, admission, extraction, writers, promotion and index; fake I/O only."""
    from api.routes import online_ocr as route
    from rag import vault as vault_module
    from core import providers
    from core.online_ocr import OnlineOCR
    from llama_index.core.embeddings import MockEmbedding
    from suite_helpers import join_post_index_daemons

    class Provider:
        def get_models(self):
            return ['fixture-embed'], None

        def get_embedding(self, _name):
            return MockEmbedding(embed_dim=8)

    import tempfile
    with tempfile.TemporaryDirectory(prefix=".review-fixture-", dir=Path.cwd()) as directory, Harness(Path(directory)) as h:
        h.cfg.update(embed='fixture-embed', embed_provider='ollama',
                     vault_vector_backend='simple', vault_hybrid_enabled=False,
                     vault_image_exts=[])
        Path(CONFIG_FILE).write_text(json.dumps(h.cfg))
        manager = vault_module.ObsidianVaultManager()
        manager.restore_vault_path(str(h.lib.vault))
        monkeypatch.setattr(route, 'obsidian_manager', manager)
        monkeypatch.setattr(route, '_job', {'state': 'idle'})
        monkeypatch.setattr(providers, 'get_provider', lambda _: Provider())
        monkeypatch.setattr(vault_module, 'get_provider', lambda _: Provider())
        for module in (route, vault_module):
            monkeypatch.setattr(module, 'OBSIDIAN_CACHE_DIR', str(tmp_path / 'text-cache'))
            monkeypatch.setattr(module, 'OBSIDIAN_INDEX_DIR', str(tmp_path / 'index'))
        monkeypatch.setattr(route, 'BASE_DIR', str(tmp_path / 'runs'))
        monkeypatch.setattr(OnlineOCR, 'recognize', lambda *a, **k: pytest.fail('Online OCR forbidden'))
        h.app.register_blueprint(route.online_ocr_bp)
        # Baseline contains no items. Only the following additions are reviewed.
        h.scan()
        sid = h.session()
        h.analyze(sid, tags=False)
        entry, _, source = h.add('ZZZZ1111', 'smith2026', 'download.pdf')
        h.lib.note(entry, 'NOTEZ111', '<p>Reviewed clinical evidence.</p>')
        h.scan()
        extraction_calls = []
        extract = route.extract_pages
        def counted(*args, **kwargs):
            extraction_calls.append(args)
            return extract(*args, **kwargs)
        monkeypatch.setattr(route, 'extract_pages', counted)

        def run(mode):
            preview = h.post('/api/online-ocr/session/preview', {
                'run_index': True, 'operation_mode': mode})
            started = h.post('/api/online-ocr/session/start', {
                'run_index': True, 'operation_mode': mode, 'no_external': True,
                'confirm_index': mode == 'update_search', 'retry_failed': False,
                'session_token': preview['session_token']})
            assert manager.wait_for_indexing(timeout=15)
            join_post_index_daemons()
            assert route._job['state'] == ('text_prepared' if mode == 'prepare_text' else 'indexed'), (route._job, manager.get_progress_snapshot(), manager.drain_status_messages())
            return preview, started

        prepared, _ = run('prepare_text')
        assert prepared['source_count'] == 1
        assert len(extraction_calls) == 1
        assert manager._index is None
        analysis = h.analyze(sid)
        assert not analysis['blocked'], analysis
        result = h.apply(sid, {
            'renames': [{'unit': r['unit'], 'rel': r['rel'], 'new_name': r['proposal']}
                        for r in analysis['renames'] if r['can_apply']],
            'tags': [{'unit': r['unit'], 'citation_key': r['citation_key'], 'tags': ['dep'],
                      'targets': {'note': True, 'finder': True, 'zotero': True}}
                     for r in analysis['tags']],
            'mirror': {'plan_digest': analysis['mirror']['plan_digest']},
        })
        assert result['mirror']['status'] == 'done', result
        assert all(r['status'] == 'done' for r in result['tags'] + result['renames']), result
        assert not source.exists()
        final, _ = run('update_search')
        assert final['source_count'] == 0 and final['reused_count'] == 1
        assert len(extraction_calls) == 1
        nodes = list(manager._index.docstore.docs.values())
        texts = '\n'.join(node.get_content() for node in nodes)
        sources = {node.metadata.get('source', '') for node in nodes}
        assert any(str(s).endswith('smith_2026.pdf') for s in sources), sources
        assert not any(str(s).endswith('download.pdf') for s in sources), sources
        assert 'Reviewed clinical evidence' in texts and 'dep' in texts
        assert 'A study of depression' in texts
        # A second confirmed update exercises incremental reuse below the handoff.
        run('update_search')
        assert '0 embedded' in ' '.join(manager.drain_status_messages())
        assert len(extraction_calls) == 1
