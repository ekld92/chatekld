"""Batch A of the 2026-09-23 CLI proof-run fixes: the library run.

A1 — a PDF that already carries its derived name closes its rename step
     (keyed on the renamer's machine ``code``, never on its prose).
A2 — a unit tagged outside any run closes its tag step at analyze.
A3 — leftover imported attachments are a reviewable group, not "settling".

Pure orchestrator tests over the lot 5 fake transport; the classifier tests
run ``build_observation``/``record_pass`` over the lot 2 disposable library.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_library_workflow_lot5 import FakeT  # noqa: E402

from accession import library_run  # noqa: E402


def _preview(rows_by_rel):
    def f(rels):
        return 200, {"rows": [dict(rows_by_rel[r], rel=r) for r in rels]}
    return f


OPEN_A = {"id": "attachment:1:A", "steps": ["rename"], "rel": "Z/biblio/x.pdf", "citation_key": "k"}
OPEN_B = {"id": "attachment:1:B", "steps": ["rename"], "rel": "Z/biblio/b.pdf", "citation_key": "k"}


# ---------------------------------------------------------------------------
# A1
# ---------------------------------------------------------------------------

def test_an_already_named_pdf_closes_its_rename_step_as_skipped():
    f = FakeT([OPEN_A], rename_preview=_preview({"x.pdf": {
        "name": "x.pdf", "proposal": None, "can_apply": False, "code": "already_named",
        "reason": "already carries the derived name"}}))
    out = library_run.analyze(f.transport(), "s")
    assert f.steps == [("attachment:1:A", "rename", "skipped",
                        {"reason": "already_named", "name": "x.pdf"})]
    assert out["renames"] == []
    assert out["closed_renames"] == ["attachment:1:A"]


def test_the_closure_keys_on_the_code_not_on_the_prose():
    # A reworded reason must not change anything; a missing code closes nothing.
    f = FakeT([dict(OPEN_A, steps=["rename"]), dict(OPEN_B, steps=["rename"])], rename_preview=_preview({
        "x.pdf": {"name": "x.pdf", "proposal": None, "can_apply": False,
                  "code": "already_named", "reason": "something else entirely"},
        "b.pdf": {"name": "b.pdf", "proposal": None, "can_apply": False,
                  "reason": "already carries the derived name"}}))
    out = library_run.analyze(f.transport(), "s")
    assert [s[0] for s in f.steps] == ["attachment:1:A"]
    assert [r["unit"] for r in out["renames"]] == ["attachment:1:B"]


def test_an_applicable_row_is_untouched():
    f = FakeT([dict(OPEN_A, steps=["rename"])])
    out = library_run.analyze(f.transport(), "s")
    assert f.steps == []
    assert out["renames"][0]["can_apply"] and not out.get("closed_renames")


def test_a_row_refused_for_another_reason_stays_open():
    f = FakeT([OPEN_A], rename_preview=_preview({"x.pdf": {
        "name": "x.pdf", "proposal": None, "can_apply": False,
        "reason": "the bib entry carries no usable year"}}))
    out = library_run.analyze(f.transport(), "s")
    assert f.steps == []
    assert out["renames"][0]["reason"] == "the bib entry carries no usable year"


def test_the_pre_fix_same_name_shape_still_reads_as_already_named():
    f = FakeT([OPEN_A], rename_preview=_preview({"x.pdf": {
        "name": "x.pdf", "proposal": "x.pdf", "can_apply": True}}))
    out = library_run.analyze(f.transport(), "s")
    row = out["renames"][0]
    assert row["already_named"] is True and row["can_apply"] is False


def test_a_tracker_refusal_keeps_the_row_listed():
    f = FakeT([OPEN_A], rename_preview=_preview({"x.pdf": {
        "name": "x.pdf", "proposal": None, "can_apply": False, "code": "already_named",
        "reason": "already carries the derived name"}}))
    t = f.transport()
    t.record_step = lambda *a, **k: (400, {"error": "unknown unit"})
    out = library_run.analyze(t, "s")
    assert not out.get("closed_renames")
    assert out["renames"][0]["already_named"] is True


# ---------------------------------------------------------------------------
# A2
# ---------------------------------------------------------------------------

ENTRY_ROW = {"id": "entry:1:F", "steps": ["tag"], "citation_key": "kf"}
ATT_ROW = {"id": "attachment:1:A", "steps": ["tag"], "rel": "Z/biblio/x.pdf", "citation_key": "k"}


def _state(**targets):
    """tag_state answer: target=(present, tags)."""
    return 200, {"targets": {k: {"present": p, "tags": list(tags)}
                             for k, (p, tags) in targets.items()}}


def _analyze(rows, state, **kw):
    f = FakeT([dict(r, steps=list(r["steps"])) for r in rows])
    t = f.transport()
    if state is not None:
        t.tag_state = lambda key, unit: state
    out = library_run.analyze(t, "s", propose_tags=True, **kw)
    return f, out


def test_an_entry_tagged_outside_a_run_closes_and_is_not_proposed():
    f, out = _analyze([ENTRY_ROW], _state(zotero=(True, ["integrity"]), note=(False, []),
                                          finder=(False, [])))
    assert f.steps == [("entry:1:F", "tag", "skipped",
                        {"reason": "tagged_outside_run", "tags": {"zotero": ["integrity"]}})]
    assert "propose" not in [c[0] for c in f.calls]
    assert out["closed_tags"] == [{"unit": "entry:1:F", "citation_key": "kf",
                                   "tags": {"zotero": ["integrity"]}}]
    assert out["tags"] == []


def test_an_entry_with_only_non_vocabulary_tags_still_owes():
    # The transport returns only VOCABULARY tags; connector tags are dropped
    # there, so an entry carrying only those reads as untagged here.
    f, out = _analyze([ENTRY_ROW], _state(zotero=(True, []), note=(False, [])))
    assert f.steps == []
    assert [c[0] for c in f.calls].count("propose") == 1


def test_an_entry_with_a_note_owes_the_note_too():
    f, out = _analyze([ENTRY_ROW], _state(zotero=(True, ["integrity"]), note=(True, [])))
    assert f.steps == [] and not out.get("closed_tags")


def test_a_new_pdf_on_an_already_tagged_entry_still_owes_finder():
    f, out = _analyze([ATT_ROW], _state(zotero=(True, ["integrity"]), note=(True, ["integrity"]),
                                        finder=(True, [])))
    assert f.steps == [] and not out.get("closed_tags")
    assert [c[0] for c in f.calls].count("propose") == 1


def test_a_biblio_attachment_tagged_everywhere_closes():
    f, out = _analyze([ATT_ROW], _state(zotero=(True, ["a"]), note=(False, []),
                                        finder=(True, ["a"])))
    assert f.steps[0][:3] == ("attachment:1:A", "tag", "skipped")
    assert f.steps[0][3]["tags"] == {"zotero": ["a"], "finder": ["a"]}


def test_an_attachment_outside_biblio_owes_zotero_only():
    f = FakeT([dict(ATT_ROW, steps=["tag"])])
    f.units["attachment:1:A"] = dict(f.units["attachment:1:A"], in_biblio=False)
    t = f.transport()
    t.tag_state = lambda key, unit: _state(zotero=(True, ["a"]), note=(True, []),
                                           finder=(True, []))
    library_run.analyze(t, "s", propose_tags=True)
    assert f.steps[0][:3] == ("attachment:1:A", "tag", "skipped")


def test_nothing_is_closed_when_no_owed_target_exists():
    f, out = _analyze([ENTRY_ROW], _state(zotero=(False, []), note=(False, [])))
    assert f.steps == []


def test_an_unreadable_vocabulary_closes_nothing():
    f, out = _analyze([ENTRY_ROW], (503, {"error": "The tag vocabulary cannot be read."}))
    assert f.steps == [] and not out.get("closed_tags")
    assert [c[0] for c in f.calls].count("propose") == 1


def test_an_unavailable_tag_state_leaves_the_analysis_unchanged():
    f, out = _analyze([ENTRY_ROW], None)  # the Transport default
    assert f.steps == [] and not out.get("closed_tags")
    assert [c[0] for c in f.calls].count("propose") == 1


def test_the_closure_also_runs_without_proposing_tags():
    f = FakeT([dict(ENTRY_ROW, steps=["tag"])])
    t = f.transport()
    t.tag_state = lambda key, unit: _state(zotero=(True, ["a"]), note=(False, []))
    out = library_run.analyze(t, "s")
    assert out["closed_tags"][0]["unit"] == "entry:1:F"


# The server-side half: vocabulary gate + per-PDF Finder tags.

def _rec(zot_tags=(), note_tags=None):
    from types import SimpleNamespace
    return SimpleNamespace(
        zotero_item=SimpleNamespace(tags=set(zot_tags)),
        obsidian_note=None if note_tags is None else SimpleNamespace(tags=list(note_tags)),
        obs_tags=set(note_tags or ()))


def test_tag_state_keeps_only_vocabulary_tags(tmp_path, monkeypatch):
    from api.routes import library_workflow as lw
    (tmp_path / "_tags.md").write_text(
        "| Description | Tag |\n|---|---|\n| intégrité | #integrity |\n", encoding="utf-8")
    code, body = lw.tag_state_for(tmp_path, {}, _rec({"integrity", "Imported", "#Integrity"}),
                                  {"kind": "entry"})
    assert code == 200
    assert body["targets"]["zotero"] == {"present": True, "tags": ["integrity"]}
    assert body["targets"]["note"]["present"] is False


def test_tag_state_refuses_without_a_vocabulary(tmp_path):
    from api.routes import library_workflow as lw
    code, _body = lw.tag_state_for(tmp_path, {}, _rec({"integrity"}), {"kind": "entry"})
    assert code == 503


def test_tag_state_reads_the_units_own_pdf_finder_tags(tmp_path, monkeypatch):
    from api.routes import library_workflow as lw
    (tmp_path / "_tags.md").write_text(
        "| Description | Tag |\n|---|---|\n| x | #a |\n", encoding="utf-8")
    pdf = tmp_path / "Z" / "x.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"%PDF")
    seen = []
    monkeypatch.setattr("audit.core.finder_tags.read_tag_names",
                        lambda p: seen.append(Path(p)) or set())
    code, body = lw.tag_state_for(tmp_path, {}, _rec({"a"}),
                                  {"kind": "attachment", "rel": "Z/x.pdf", "in_biblio": True})
    assert seen == [pdf]
    assert body["targets"]["finder"] == {"present": True, "tags": []}


# ---------------------------------------------------------------------------
# A3 — leftover imported attachments
# ---------------------------------------------------------------------------

import sqlite3  # noqa: E402

import pytest  # noqa: E402

import test_library_imports as _imp  # noqa: E402
from accession import imports  # noqa: E402


def _imported(lib, parent_iid, key, name, *, annotations=0):
    """An `imported_url` copy whose storage file does not exist (the live
    MZHLRTT9 / UL9ZM74E shape)."""
    iid = lib._id()
    with sqlite3.connect(lib.db) as con:
        con.execute('INSERT INTO items VALUES (?,?,2,?,?,1,1)', (iid, key, 'd', 'd'))
        con.execute('INSERT INTO itemAttachments VALUES (?,?,?,?,1)',
                    (iid, parent_iid, f'storage:{name}', 'application/pdf'))
        con.execute('CREATE TABLE IF NOT EXISTS itemAnnotations (itemID INTEGER, parentItemID INTEGER)')
        for n in range(annotations):
            con.execute('INSERT INTO itemAnnotations VALUES (?,?)', (90_000 + iid * 10 + n, iid))
    return iid


@pytest.fixture
def lib2(tmp_path):
    lib = _imp.Library(tmp_path)
    with sqlite3.connect(lib.db) as con:
        con.execute('CREATE TABLE itemAnnotations (itemID INTEGER, parentItemID INTEGER)')
    lib.run()  # the baseline
    return lib


def test_an_imported_copy_beside_a_live_linked_copy_is_a_leftover(lib2):
    e = lib2.entry('MART0001', 'martiCarvajal2026')
    lib2.pdf(e, 'XNET7KNZ', 'marti_carvajal_2026.pdf')
    _imported(lib2, e, 'MZHLRTT9', 'martí-carvajal_2026.pdf')
    _, report = lib2.run()
    assert report['settling'] == []
    [row] = report['leftovers']
    assert row == {'unit': 'attachment:1:MZHLRTT9', 'item_key': 'MZHLRTT9',
                   'parent': 'entry:1:MART0001', 'citation_key': 'martiCarvajal2026',
                   'stored_path': 'storage:martí-carvajal_2026.pdf',
                   'linked_copy_rel': 'Z_attachments/biblio_articles/marti_carvajal_2026.pdf',
                   'annotations': 0}


def test_a_lone_imported_copy_is_still_settling(lib2):
    e = lib2.entry('CHEN0001', 'chen2026')
    _imported(lib2, e, 'UL9ZM74E', 'chen_2026.pdf')
    _, report = lib2.run()
    assert report['settling'] == ['attachment:1:UL9ZM74E'] and report['leftovers'] == []


def test_a_linked_sibling_outside_biblio_does_not_qualify(lib2, tmp_path):
    e = lib2.entry('CHEN0002', 'chen2026b')
    outside = lib2.vault / 'elsewhere.pdf'
    outside.write_bytes(b'%PDF-1.4 elsewhere')
    iid = lib2._id()
    with sqlite3.connect(lib2.db) as con:
        con.execute('INSERT INTO items VALUES (?,?,2,?,?,1,1)', (iid, 'OUTS0001', 'd', 'd'))
        con.execute('INSERT INTO itemAttachments VALUES (?,?,?,?,2)',
                    (iid, e, str(outside), 'application/pdf'))
    _imported(lib2, e, 'IMPO0002', 'chen_2026.pdf')
    _, report = lib2.run()
    assert report['leftovers'] == [] and report['settling'] == ['attachment:1:IMPO0002']


def test_the_leftover_facts_are_not_persisted_on_the_unit(lib2):
    e = lib2.entry('MART0003', 'm3')
    lib2.pdf(e, 'LINK0003', 'm_2026.pdf')
    _imported(lib2, e, 'IMPO0003', 'm.pdf', annotations=2)
    _, report = lib2.run()
    assert report['leftovers'][0]['annotations'] == 2
    unit = imports.unit(lib2.settings, 'attachment:1:IMPO0003')
    assert 'stored_path' not in unit and 'annotations' not in unit


LEFT = {"unit": "attachment:1:L", "item_key": "LLLL0001", "parent": "entry:1:E",
        "citation_key": "k", "stored_path": "storage:x.pdf",
        "linked_copy_rel": "Z/biblio/x.pdf", "annotations": 0}


def test_analyze_lists_leftovers_and_drops_them_from_blocked():
    f = FakeT([], observe=(200, {"open": [], "settling": [], "leftovers": [
        LEFT, dict(LEFT, unit="attachment:1:M", item_key="MMMM0001", annotations=3)]}))
    out = library_run.analyze(f.transport(), "s")
    assert out["blocked"] == []
    by = {r["unit"]: r for r in out["leftovers"]}
    assert by["attachment:1:L"]["selectable"] is True
    assert by["attachment:1:M"]["selectable"] is False
    assert "annotation" in by["attachment:1:M"]["reason"]


def test_an_unknown_annotation_count_is_not_selectable():
    f = FakeT([], observe=(200, {"open": [], "leftovers": [dict(LEFT, annotations=None)]}))
    out = library_run.analyze(f.transport(), "s")
    assert out["leftovers"][0]["selectable"] is False


def _apply(approved, *, fresh, trash=(200, {"ok": True, "batch_id": "B1",
                                            "results": {"LLLL0001": {"ok": True}}})):
    f = FakeT([])
    t = f.transport()
    calls = []
    t.leftovers = lambda: (200, {"rows": fresh})
    t.trash_attachments = lambda keys: calls.append(list(keys)) or trash
    return library_run.apply(t, "s", {"leftovers": approved}), calls


def test_apply_trashes_exactly_the_approved_keys():
    res, calls = _apply(["attachment:1:L"], fresh=[LEFT, dict(LEFT, unit="attachment:1:Z",
                                                               item_key="ZZZZ0001")])
    assert calls == [["LLLL0001"]]
    assert res["leftovers"] == [{"unit": "attachment:1:L", "item_key": "LLLL0001",
                                 "status": "done", "batch_id": "B1"}]
    assert res["leftovers_undo"] == "papermind tagging restore-batch B1 --confirm"


def test_apply_refuses_a_row_that_no_longer_qualifies():
    res, calls = _apply(["attachment:1:L"], fresh=[])
    assert calls == []
    assert res["leftovers"][0]["status"] == "failed"
    assert "no longer" in res["leftovers"][0]["reason"]


def test_apply_never_trashes_an_annotated_leftover():
    res, calls = _apply(["attachment:1:L"], fresh=[dict(LEFT, annotations=4)])
    assert calls == [] and res["leftovers"][0]["status"] == "failed"


def test_a_failed_trash_result_is_a_failed_row():
    res, _calls = _apply(["attachment:1:L"], fresh=[LEFT],
                         trash=(200, {"ok": True, "batch_id": "B1",
                                      "results": {"LLLL0001": {"ok": False, "error": "412"}}}))
    assert res["leftovers"][0]["status"] == "failed"


def test_a_server_error_during_the_trash_is_uncertain():
    res, _calls = _apply(["attachment:1:L"], fresh=[LEFT], trash=(500, {"error": "boom"}))
    assert res["leftovers"][0]["status"] == "uncertain"


def test_leftovers_are_applied_after_renames():
    f = FakeT()
    t = f.transport()
    order = []
    t.leftovers = lambda: (200, {"rows": [LEFT]})
    t.trash_attachments = lambda keys: order.append("trash") or (
        200, {"ok": True, "batch_id": "B", "results": {k: {"ok": True} for k in keys}})
    t.rename = lambda rel, name: order.append("rename") or (200, {"new_rel": "Z/biblio/" + name})
    library_run.apply(t, "s", {"renames": [{"unit": "attachment:1:A", "rel": "x.pdf",
                                            "new_name": "y.pdf"}],
                               "leftovers": ["attachment:1:L"]})
    assert order == ["rename", "trash"]


def test_the_in_process_trash_transport_reads_the_zotlink_terminal(tmp_path):
    """The transport posts to the real `/api/zotlink/trash` (confirm, no
    annotation acceptance) and returns its terminal frame."""
    from types import SimpleNamespace
    from unittest import mock

    from api.routes import library_workflow as lw
    from app import create_app
    from audit.manager import audit_manager

    app = create_app()
    vault = tmp_path / "vault"
    (vault / "Z" / "biblio").mkdir(parents=True)
    settings = SimpleNamespace(vault_root=vault, biblio_articles_dir=vault / "Z" / "biblio")
    inv = SimpleNamespace(zotero_attachments=[SimpleNamespace(item_key="LLLL0001",
                                                              annotation_count=0)])
    sent = []

    def fake_run(put, cancel, keys, index, settings_, cfg):
        sent.append(list(keys))
        put({"zotlink": {"ok": True, "batch_id": "B9", "results": {"LLLL0001": {"ok": True}}}})

    with mock.patch.object(audit_manager, "get_inventory", return_value=(inv, settings)), \
            mock.patch("api.routes.zotlink.load_config",
                       return_value={"tagging_archive_dir": str(tmp_path / "archive")}), \
            mock.patch("api.routes.zotlink._run_trash", fake_run):
        t = lw._transport(settings, app)
        code, body = t.trash_attachments(["LLLL0001"])
    assert code == 200 and body["batch_id"] == "B9"
    assert sent == [["LLLL0001"]]


def test_the_in_process_trash_transport_passes_a_refusal_through(tmp_path):
    from types import SimpleNamespace
    from unittest import mock

    from api.routes import library_workflow as lw
    from app import create_app
    from audit.manager import audit_manager

    app = create_app()
    vault = tmp_path / "vault"
    (vault / "Z" / "biblio").mkdir(parents=True)
    settings = SimpleNamespace(vault_root=vault, biblio_articles_dir=vault / "Z" / "biblio")
    inv = SimpleNamespace(zotero_attachments=[SimpleNamespace(item_key="LLLL0001",
                                                              annotation_count=3)])
    with mock.patch.object(audit_manager, "get_inventory", return_value=(inv, settings)):
        code, body = lw._transport(settings, app).trash_attachments(["LLLL0001"])
    assert code == 409 and body["reason"] == "would_trash_annotations"


def test_an_editor_only_refusal_stays_open_with_its_reason():
    # B4 cross-check: the new refusal is not "already_named", so the library
    # run keeps the unit's rename step open and shows the reason.
    f = FakeT([dict(OPEN_A, steps=["rename"])], rename_preview=_preview({"x.pdf": {
        "name": "x.pdf", "proposal": None, "can_apply": False, "code": "no_author",
        "reason": "the bib entry has no author (editors only) — rename it by hand"}}))
    out = library_run.analyze(f.transport(), "s")
    assert f.steps == [] and not out.get("closed_renames")
    assert out["renames"][0]["reason"].startswith("the bib entry has no author")
