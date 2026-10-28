"""Disposable filesystem/SQLite management regressions; no live library or HTTP."""

import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from test_pdf_reconciliation import world as _world, mutate

from accession import pdf_management as pm, reconciliation as rec, review

world = _world


@pytest.fixture(autouse=True)
def _readable_annotation_counts():
    """The fixture PDFs are placeholder bytes that no PDF parser opens. Until
    2026-09-17 their "unknown" annotation count was silently read as zero;
    now unknown blocks removal (review B3), so these fixtures state the count
    they always implied. Tests of the unknown case patch it back to −1."""
    with patch("audit.core.pdf_annotations.count_annotations", return_value=0):
        yield


@pytest.fixture
def copies(world):
    settings, survivor = world
    redundant = survivor.with_name("redundant.pdf")
    redundant.write_bytes(survivor.read_bytes())
    settings.mapping_file.write_text(
        json.dumps(
            {
                "matches": {
                    "KeyOne": [
                        "biblio_articles/one.pdf",
                        "biblio_articles/redundant.pdf",
                    ]
                },
                "pending": ["biblio_articles/unrelated.pdf"],
            }
        )
    )
    return settings, survivor, redundant


def proposal(copies, action="delete"):
    s, keep, remove = copies
    return pm.preview(
        s,
        "KeyOne",
        keep.relative_to(s.vault_root).as_posix(),
        {remove.relative_to(s.vault_root).as_posix(): action},
    )


def apply(copies, proposed=None, action="delete"):
    s, keep, remove = copies
    proposed = proposed or proposal(copies, action)
    return pm.apply(
        s,
        "KeyOne",
        keep.relative_to(s.vault_root).as_posix(),
        {remove.relative_to(s.vault_root).as_posix(): action},
        proposed["token"],
        confirm=True,
    )


def test_reads_do_not_initialize_stores_or_mutate_files(copies):
    s, keep, remove = copies
    before = s.mapping_file.read_bytes()
    data, _ = pm.entry(s, "KeyOne")
    preview = proposal(copies)
    assert len(data["rows"]) == 2 and preview["executable"]
    assert s.mapping_file.read_bytes() == before
    assert not rec.store_path().exists() and not pm.recovery_root().exists()
    assert keep.exists() and remove.exists()


def test_delete_moves_real_bytes_and_restore_preserves_other_decisions(copies):
    s, keep, remove = copies
    original = pm._mapping(s, "biblio_articles/redundant.pdf")
    op = apply(copies)
    assert op["status"] == "complete", op
    assert not remove.exists() and keep.exists()
    recovered = __import__("pathlib").Path(op["steps"][0]["recovery"])
    assert recovered.read_bytes() == keep.read_bytes()
    mapping = json.loads(s.mapping_file.read_text())
    assert mapping["matches"]["KeyOne"] == ["biblio_articles/one.pdf"]
    assert mapping["pending"] == ["biblio_articles/unrelated.pdf"]
    assert pm.restore(s, op["id"], confirm=True)["status"] == "restored"
    assert remove.read_bytes() == keep.read_bytes() and not recovered.exists()
    assert pm._mapping(s, "biblio_articles/redundant.pdf") == original


@pytest.mark.parametrize(
    "change", ["note", "other_mapping", "live_attachment", "unresolved"]
)
def test_delete_refuses_shared_or_unverifiable_references(copies, change):
    s, keep, remove = copies
    if change == "note":
        (s.vault_root / "note.md").write_text("[[redundant.pdf]]")
    elif change == "other_mapping":
        data = json.loads(s.mapping_file.read_text())
        data["matches"]["Other"] = ["biblio_articles/redundant.pdf"]
        s.mapping_file.write_text(json.dumps(data))
    else:
        mutate(s, "INSERT INTO items VALUES (4,?,1,2)", ("ATTACH04",))
        mutate(
            s,
            "INSERT INTO itemAttachments VALUES (4,1,?,2)",
            (
                str(remove)
                if change == "live_attachment"
                else "attachments:unresolved.pdf",
            ),
        )
    p = proposal(copies)
    assert p["blockers"] and not p["executable"]
    with pytest.raises(ValueError):
        apply(copies, p)
    assert keep.exists() and remove.exists()


def test_survivor_cannot_be_removed(copies):
    s, keep, _ = copies
    with pytest.raises(ValueError, match="survivor"):
        pm.preview(
            s,
            "KeyOne",
            "biblio_articles/one.pdf",
            {"biblio_articles/one.pdf": "delete"},
        )
    assert keep.exists()


def test_stale_preview_does_not_remove_replaced_file(copies):
    s, keep, remove = copies
    p = proposal(copies)
    remove.write_bytes(b"new content")
    with pytest.raises(review.ReviewConflict):
        apply(copies, p)
    assert remove.read_bytes() == b"new content"


def test_restore_never_overwrites_new_file(copies):
    s, _, remove = copies
    op = apply(copies)
    remove.write_bytes(b"replacement")
    result = pm.restore(s, op["id"], confirm=True)
    assert result["status"] == "needs_inspection"
    assert remove.read_bytes() == b"replacement"


def test_lost_mapping_write_after_move_is_recoverable(copies):
    s, keep, remove = copies
    with patch.object(
        pm, "_set_mapping", side_effect=OSError("simulated write failure")
    ):
        op = apply(copies)
    assert op["status"] == "needs_inspection" and not remove.exists()
    assert pm.restore(s, op["id"], confirm=True)["status"] == "restored"
    assert keep.read_bytes() == remove.read_bytes()


def test_retired_pdf_does_not_return_to_registration(copies):
    s, _, remove = copies
    rec.refresh(s)
    op = apply(copies)
    assert op["status"] == "complete"
    rec.refresh(s)
    retired = next(
        r for r in rec.rows(s, rec.saved(s)) if r["pdf"]["rel"].endswith(remove.name)
    )
    assert retired["state"] == "Retired" and not retired["can_create"]


def test_unmap_preserves_file_and_can_restore(copies):
    s, _, remove = copies
    op = apply(copies, action="unmap")
    assert op["status"] == "complete", op
    assert remove.exists()
    assert pm.restore(s, op["id"], confirm=True)["status"] == "restored"


def test_symlinks_and_hardlinks_refuse(copies):
    s, keep, remove = copies
    remove.unlink()
    remove.symlink_to(keep)
    with pytest.raises(ValueError):
        proposal(copies)
    remove.unlink()
    os.link(keep, remove)
    with pytest.raises(ValueError):
        proposal(copies)


def test_report_read_never_refreshes_or_applies(world):
    from api.routes.pending import workspace

    s, _ = world
    rec.refresh(s)
    before = s.mapping_file.read_bytes()
    with (
        patch.object(rec, "refresh", side_effect=AssertionError("read refreshed")),
        patch.object(rec, "use_association", side_effect=AssertionError("read wrote")),
    ):
        result = workspace(s)
    assert result["count"] == 1 and s.mapping_file.read_bytes() == before


def test_refresh_does_not_auto_map(world):
    from api.routes.pending import workspace

    s, _ = world
    before = s.mapping_file.read_bytes()
    with patch.object(
        rec, "use_association", side_effect=AssertionError("automatic mapping")
    ):
        result = workspace(s, force=True)
    assert result["freshness"]["available"] and s.mapping_file.read_bytes() == before


def test_refresh_join_failure_keeps_snapshot_and_marks_unavailable(world):
    s, _ = world
    rec.refresh(s)
    saved = rec.saved(s)
    with patch.object(
        rec, "rows", side_effect=review.ReviewConflict("export in progress")
    ):
        result = rec.refresh(s)
    assert not result["available"] and rec.saved(s) == saved
    assert rec.availability(s)["error"] == "export in progress"


def test_bibliography_touch_during_parse_is_accepted_but_new_bytes_are_reparsed(world):
    s, _ = world
    original = rec.bib.parse_bib_text
    rec._bib_cached = None

    def touch(text):
        os.utime(
            s.master_bib,
            ns=(
                s.master_bib.stat().st_atime_ns,
                s.master_bib.stat().st_mtime_ns + 10000,
            ),
        )
        return original(text)

    with patch.object(rec.bib, "parse_bib_text", side_effect=touch) as parse:
        rows, identity = rec.bibliography_snapshot(s.master_bib)
        assert rows[0].citation_key == "KeyOne"
        assert identity == review.identity(s.master_bib)
        rec.bibliography_snapshot(s.master_bib)
        assert parse.call_count == 1
    rec._bib_cached = None

    def replace(text):
        s.master_bib.write_text("@article{NewKey,title={New}}")
        return original(text)

    with patch.object(rec.bib, "parse_bib_text", side_effect=replace):
        entries, _ = rec.bibliography_snapshot(s.master_bib)
    assert entries[0].citation_key == "NewKey"


def test_changing_bibliography_leaves_report_readable(world):
    from api.routes.pending import workspace
    from accession import projection

    s, _ = world
    rec.refresh(s)
    projection._cached = None
    with patch.object(
        rec,
        "bibliography_snapshot",
        side_effect=review.ReviewConflict("export changing"),
    ):
        result = workspace(s)
    assert (
        not result["freshness"]["available"]
        and "export changing" in result["freshness"]["error"]
    )
    assert not result["rows"]


def test_unmap_then_second_step_failure_restores_only_own_changes(copies):
    s, _, remove = copies
    other = remove.with_name("third.pdf")
    other.write_bytes(b"third")
    data = json.loads(s.mapping_file.read_text())
    data["matches"]["KeyOne"].append("biblio_articles/third.pdf")
    s.mapping_file.write_text(json.dumps(data))
    actions = {
        "biblio_articles/redundant.pdf": "unmap",
        "biblio_articles/third.pdf": "delete",
    }
    p = pm.preview(s, "KeyOne", "biblio_articles/one.pdf", actions)
    original = pm._set_mapping
    calls = []

    def fail_second(*args):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("second write failed")
        return original(*args)

    with patch.object(pm, "_set_mapping", side_effect=fail_second):
        op = pm.apply(
            s, "KeyOne", "biblio_articles/one.pdf", actions, p["token"], confirm=True
        )
    assert op["status"] == "needs_inspection" and op["steps"][0]["status"] == "complete"
    assert not other.exists()
    assert pm.restore(s, op["id"], confirm=True)["status"] == "restored"
    assert other.exists() and remove.exists()


def test_restore_refuses_edited_mapping(copies):
    s, _, _ = copies
    op = apply(copies)
    data = json.loads(s.mapping_file.read_text())
    data["matches"]["New"] = ["biblio_articles/redundant.pdf"]
    s.mapping_file.write_text(json.dumps(data))
    before = s.mapping_file.read_bytes()
    assert pm.restore(s, op["id"], confirm=True)["status"] == "needs_inspection"
    assert s.mapping_file.read_bytes() == before


def test_trash_requires_survivor_and_explicit_annotation_consent(copies):
    s, keep, remove = copies
    mutate(s, "INSERT INTO items VALUES (4,?,1,2)", ("ATTACH04",))
    mutate(s, "INSERT INTO itemAttachments VALUES (4,1,?,2)", (str(remove),))
    from types import SimpleNamespace
    from tagging import zotero_write as zw
    from tagging.local_api import Version

    data = dict(
        itemType="attachment",
        parentItem="PARENT01",
        path=str(remove),
        linkMode="linked_file",
        title="Duplicate",
    )
    get = Mock(return_value=(data, Version(2, "fixture-db")))
    with (
        patch(
            "tagging.zotero_write.resolve_transport",
            return_value=SimpleNamespace(base_url="http://fixture", is_web=False, kind="local", can_write=True),
        ),
        patch.object(zw, "get_item", get),
        patch.object(zw, "set_item_deleted") as write,
    ):
        with pytest.raises(ValueError, match="survive"):
            pm.trash_preview(s, {}, "KeyOne", "ATTACH01", "biblio_articles/one.pdf")
        p = pm.trash_preview(s, {}, "KeyOne", "ATTACH04", "biblio_articles/one.pdf")
        with pytest.raises(ValueError, match="annotations"):
            pm.trash(
                s,
                {},
                "KeyOne",
                "ATTACH04",
                "biblio_articles/one.pdf",
                p["token"],
                confirm=True,
            )
        write.assert_not_called()

        def dispatch(*args, **kwargs):
            assert pm.operations(s)[0]["status"] == "sent_or_unknown"
            get.return_value = ({**data, "deleted": 1}, Version(3, "fixture-db"))

        write.side_effect = dispatch
        op = pm.trash(
            s,
            {},
            "KeyOne",
            "ATTACH04",
            "biblio_articles/one.pdf",
            p["token"],
            confirm=True,
            accept_annotation_loss=True,
        )
        assert op["status"] == "complete" and keep.exists() and remove.exists()
        assert write.call_count == 1


def test_link_retains_reserved_key_and_does_not_retry_unknown_create(copies):
    s, _, remove = copies
    from types import SimpleNamespace
    from tagging import writers

    transport = SimpleNamespace(base_url="http://fixture", is_web=False, kind="local", can_write=True)

    def prepare(*args):
        return {
            "commit": dict(
                transport=transport,
                api_key=None,
                lib_path="users/0",
                parent_key="PARENT01",
                path=str(remove),
                title="redundant.pdf",
                server_id="fixture-db",
            )
        }

    p = pm.link_preview(s, "KeyOne", "biblio_articles/redundant.pdf")
    with (
        patch("tagging.zotero_write.resolve_transport", return_value=transport),
        patch.object(writers, "zotero_attachment_create_prepare", side_effect=prepare),
        patch.object(
            writers,
            "zotero_attachment_create_post",
            side_effect=OSError("lost response"),
        ) as write,
    ):
        op = pm.link(
            s, {}, "KeyOne", "biblio_articles/redundant.pdf", p["token"], confirm=True
        )
        assert op["status"] == "needs_inspection" and len(op["attachment"]) == 8
        p = pm.link_preview(s, "KeyOne", "biblio_articles/redundant.pdf")
        with pytest.raises(review.ReviewConflict, match="uncertain"):
            pm.link(
                s,
                {},
                "KeyOne",
                "biblio_articles/redundant.pdf",
                p["token"],
                confirm=True,
            )
        assert write.call_count == 1


def test_process_exit_after_move_retains_recoverable_record(copies):
    import subprocess
    import sys

    s, keep, remove = copies
    script = """import os,sys
from pathlib import Path
from audit.config import Settings
from accession import pdf_management as pm
import audit.core.pdf_annotations as pa
pa.count_annotations=lambda path: 0  # placeholder bytes; see _readable_annotation_counts
s=Settings(vault_root=Path(sys.argv[1]),attachments_subdir='',zotero_sqlite=Path(sys.argv[2]),zotero_storage=Path(sys.argv[3]),mapping_file_override=Path(sys.argv[4]))
p=pm.preview(s,'KeyOne','biblio_articles/one.pdf',{'biblio_articles/redundant.pdf':'delete'})
pm._set_mapping=lambda *args: os._exit(73)
pm.apply(s,'KeyOne','biblio_articles/one.pdf',{'biblio_articles/redundant.pdf':'delete'},p['token'],confirm=True)
"""
    env = dict(os.environ, CHATEKLD_BASE_DIR=str(rec.store_path().parent.parent))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(s.vault_root),
            str(s.zotero_sqlite),
            str(s.zotero_storage),
            str(s.mapping_file),
        ],
        env=env,
        timeout=30,
        capture_output=True,
    )
    assert result.returncode == 73, result.stderr
    assert not remove.exists() and keep.exists()
    op = pm.operations(s)[0]
    assert op["steps"][0]["status"] == "moved"
    assert pm.restore(s, op["id"], confirm=True)["status"] == "restored"
    assert remove.read_bytes() == keep.read_bytes()


def test_management_routes_require_confirm_and_get_is_read_only(copies, monkeypatch):
    from flask import Flask
    from types import SimpleNamespace
    from api.routes import pending

    s, _, _ = copies
    inv = SimpleNamespace(
        records={
            "KeyOne": SimpleNamespace(zotero_item=SimpleNamespace(item_key="PARENT01"))
        }
    )
    monkeypatch.setattr(pending, "context", lambda: (inv, s))
    monkeypatch.setattr(pending.audit_manager, "is_scanning", lambda: False)
    app = Flask(__name__)
    app.register_blueprint(pending.pending_bp)
    client = app.test_client()
    before = s.mapping_file.read_bytes()
    assert client.get("/api/accession/pdfs/entry?key=KeyOne").status_code == 200
    assert not rec.store_path().exists()
    payload = {
        "key": "KeyOne",
        "action": "cleanup",
        "survivor": "biblio_articles/one.pdf",
        "actions": {"biblio_articles/redundant.pdf": "delete"},
    }
    response = client.post("/api/accession/pdfs/preview", json=payload)
    assert response.status_code == 200
    payload["token"] = response.json["token"]
    assert client.post("/api/accession/pdfs/apply", json=payload).status_code == 400
    assert s.mapping_file.read_bytes() == before
    payload["confirm"] = True
    response = client.post("/api/accession/pdfs/apply", json=payload)
    assert (
        response.status_code == 200
        and response.json["operation"]["status"] == "complete"
    )


def test_unrendered_import_template_is_not_a_malformed_note(tmp_path):
    from audit.core import obsidian

    path = tmp_path / "import_template.md"
    path.write_text(
        '---\ntags:\n{%- if tags.length > 0 %}\n  - "{{ t.tag }}"\n{%- endif %}\n---\n'
    )
    obsidian.drain_parse_warnings()
    assert obsidian.read_note(path) is None
    assert obsidian.drain_parse_warnings() == []
    malformed = tmp_path / "research_note.md"
    malformed.write_text("---\ntags: [broken\n---\n")
    obsidian.read_note(malformed)
    assert obsidian.drain_parse_warnings()


def test_replacement_at_rename_is_returned_without_mapping_change(copies):
    s, _, remove = copies
    before = s.mapping_file.read_bytes()
    rename = os.rename

    def replace_then_move(source, destination):
        source.write_bytes(b"concurrent edit")
        rename(source, destination)

    with patch.object(pm.os, "rename", side_effect=replace_then_move):
        op = apply(copies)
    assert op["status"] == "needs_inspection"
    assert remove.read_bytes() == b"concurrent edit"
    assert s.mapping_file.read_bytes() == before


def test_duplicate_attachment_record_can_be_trashed_while_same_file_survives(copies):
    s, keep, _ = copies
    mutate(s, "INSERT INTO items VALUES (4,?,1,2)", ("ATTACH04",))
    mutate(s, "INSERT INTO itemAttachments VALUES (4,1,?,2)", (str(keep),))
    from types import SimpleNamespace
    from tagging import zotero_write as zw
    from tagging.local_api import Version

    data = dict(
        itemType="attachment",
        parentItem="PARENT01",
        path=str(keep),
        linkMode="linked_file",
    )
    with (
        patch(
            "tagging.zotero_write.resolve_transport",
            return_value=SimpleNamespace(base_url="http://fixture", is_web=False, kind="local", can_write=True),
        ),
        patch.object(zw, "get_item", return_value=(data, Version(1, "fixture-db"))),
    ):
        plan = pm.trash_preview(s, {}, "KeyOne", "ATTACH04", "biblio_articles/one.pdf")
    assert plan["executable"]
    assert len(plan["plan"]["survivor"]["attachments"]) == 2


def test_restore_rejects_recovery_directory_symlink(copies):
    """Even the interrupted link/unlink recovery branch must validate ancestry."""
    import os
    from pathlib import Path
    s, _, removed = copies
    op = apply(copies)
    dest = Path(op['steps'][0]['recovery'])
    real_folder = dest.parent.with_name(dest.parent.name + '-moved')
    dest.parent.rename(real_folder)
    dest.parent.symlink_to(real_folder, target_is_directory=True)
    # Simulate a crash after restore's link but before unlink.
    os.link(real_folder / dest.name, removed)
    result = pm.restore(s, op['id'], confirm=True)
    assert result['status'] == 'needs_inspection'
    assert (real_folder / dest.name).exists() and removed.exists()


# --- 2026-09-17 review, A2: a refused link create must not strand the entry ---

def _link_harness(copies, post):
    from contextlib import ExitStack
    from types import SimpleNamespace
    from tagging import writers

    s, _, remove = copies
    transport = SimpleNamespace(base_url="http://fixture", is_web=False, kind="local", can_write=True)

    def prepare(*args):
        return {"commit": dict(transport=transport, api_key=None, lib_path="users/0",
                               parent_key="PARENT01", path=str(remove), title="redundant.pdf",
                               server_id="fixture-db")}

    stack = ExitStack()
    stack.enter_context(patch("tagging.zotero_write.resolve_transport", return_value=transport))
    stack.enter_context(patch.object(writers, "zotero_attachment_create_prepare", side_effect=prepare))
    stack.enter_context(patch.object(writers, "zotero_attachment_create_post", side_effect=post))
    return s, stack


def test_refused_link_create_leaves_mapping_untouched_and_entry_usable(copies):
    s, stack = _link_harness(copies, lambda prepared: (
        {"ok": False, "status": "unsupported", "message": "primaryData not loaded"}, None, None))
    before = s.mapping_file.read_bytes()
    rel = "biblio_articles/redundant.pdf"
    with stack:
        p = pm.link_preview(s, "KeyOne", rel)
        op = pm.link(s, {}, "KeyOne", rel, p["token"], confirm=True)
        assert op["status"] == "failed", op
        assert s.mapping_file.read_bytes() == before
        pm.require_settled(s, "KeyOne")  # the entry is usable again
        with pytest.raises(review.ReviewConflict, match="created nothing"):
            pm.link(s, {}, "KeyOne", rel, pm.link_preview(s, "KeyOne", rel)["token"], confirm=True)


def test_acknowledged_create_with_failed_readback_stays_blocking_and_names_the_op(copies):
    s, stack = _link_harness(copies, lambda prepared: (
        {"ok": True, "status": "attached", "item_key": "ATTNEW01", "verification_pending": True}, "ATTNEW01", None))
    rel = "biblio_articles/redundant.pdf"
    with stack:
        p = pm.link_preview(s, "KeyOne", rel)
        op = pm.link(s, {}, "KeyOne", rel, p["token"], confirm=True)
        assert op["status"] == "needs_inspection"
        with pytest.raises(review.ReviewConflict) as blocked:
            pm.require_settled(s, "KeyOne")
        assert op["id"] in str(blocked.value) and "Inspect" in str(blocked.value)


def test_inspect_settles_an_unacknowledged_link_whose_reserved_key_is_absent(copies):
    from tagging import zotero_write as zw

    s, stack = _link_harness(copies, OSError("lost response"))
    before = s.mapping_file.read_bytes()
    rel = "biblio_articles/redundant.pdf"
    with stack:
        p = pm.link_preview(s, "KeyOne", rel)
        op = pm.link(s, {}, "KeyOne", rel, p["token"], confirm=True)
        assert op["status"] == "needs_inspection"
        assert s.mapping_file.read_bytes() == before
        with patch.object(zw, "get_item", side_effect=zw.ZoteroNotFound("404")):
            result = pm.inspect(s, {}, op["id"])
        assert result["failed"] and not result["completable"]
        pm.require_settled(s, "KeyOne")
        restored = pm.restore(s, op["id"], confirm=True)
        assert restored["status"] == "restored" and s.mapping_file.read_bytes() == before


def test_restore_of_a_legacy_failed_link_reverts_the_premature_mapping(copies):
    s, _, _ = copies
    rel = "biblio_articles/redundant.pdf"
    plan = {"rel": rel, "mapping": pm._mapping(s, rel)}
    from audit.engine import bridge
    bridge.add_match(s.mapping_file, s.vault_root / rel, "KeyOne", s.vault_root, source="manual")
    assert pm._mapping(s, rel) != plan["mapping"]
    pm._save(dict(id="legacy01", kind="link", scope=rec.scope(s), key="KeyOne", plan=plan, status="failed"))
    op = pm.restore(s, "legacy01", confirm=True)
    assert op["status"] == "restored" and pm._mapping(s, rel) == plan["mapping"]


def test_transport_refuses_the_web_library(copies):
    from types import SimpleNamespace

    with patch("tagging.zotero_write.resolve_transport",
               return_value=SimpleNamespace(base_url="https://api.zotero.org", is_web=True, can_write=True)):
        with pytest.raises(ValueError, match="never switches to a Web library"):
            pm._transport({})


# --- 2026-09-17 review, B4: note references survive bad bytes, walked once ---

def test_non_utf8_note_does_not_break_the_panel_and_still_counts_as_a_reference(copies):
    s, keep, remove = copies
    (s.vault_root / "latin1.md").write_bytes(b"Voir redundant.pdf \xff\xfe caf\xe9\n")
    data, _ = pm.entry(s, "KeyOne")
    row = {r["rel"]: r for r in data["rows"]}["biblio_articles/redundant.pdf"]
    assert row["note_dependencies"] == ["latin1.md"]
    blocked = proposal(copies)
    assert not blocked["executable"] and any("referenced by vault notes" in b for b in blocked["blockers"])


def test_apply_with_three_steps_walks_the_vault_once(copies):
    from pathlib import Path

    s, keep, remove = copies
    extra = []
    for n in (2, 3):
        copy = remove.with_name(f"redundant{n}.pdf")
        copy.write_bytes(remove.read_bytes())
        extra.append(copy.relative_to(s.vault_root).as_posix())
    mapping = json.loads(s.mapping_file.read_text())
    mapping["matches"]["KeyOne"] += extra
    s.mapping_file.write_text(json.dumps(mapping))
    actions = {remove.relative_to(s.vault_root).as_posix(): "unmap", **{rel: "unmap" for rel in extra}}
    walks = []
    real_rglob, real_walk = Path.rglob, os.walk

    def rglob(self, pattern, *a, **k):
        if pattern == "*.md":
            walks.append("rglob")
        return real_rglob(self, pattern, *a, **k)

    def walk(top, *a, **k):
        walks.append("walk")
        return real_walk(top, *a, **k)

    token = pm.preview(s, "KeyOne", "biblio_articles/one.pdf", actions)["token"]
    with patch.object(Path, "rglob", rglob), patch("os.walk", walk):
        op = pm.apply(s, "KeyOne", "biblio_articles/one.pdf", actions, token, confirm=True)
    assert op["status"] == "complete", op
    assert len(walks) == 1, walks


# --- 2026-09-17 review, B5: physical moves hold the vault op-lock -----------

def test_busy_vault_refuses_before_any_move_or_operation(copies):
    from rag.vault import obsidian_manager

    s, keep, remove = copies
    proposed = proposal(copies)
    with patch.object(obsidian_manager, "try_acquire_lock", return_value=None):
        with pytest.raises(pm.VaultBusy):
            apply(copies, proposed)
    assert remove.exists() and pm.operations(s) == []


def test_move_happens_under_the_vault_lock(copies):
    from rag.vault import obsidian_manager

    s, keep, remove = copies
    held = {"now": False}
    seen = []
    real_acquire, real_release, real_rename = obsidian_manager.try_acquire_lock, obsidian_manager.release_lock, os.rename

    def acquire(ttl=3600):
        epoch = real_acquire(ttl=ttl)
        held["now"] = bool(epoch)
        return epoch

    def release(epoch):
        held["now"] = False
        return real_release(epoch)

    def rename(src, dst, *a, **k):
        seen.append(held["now"])
        return real_rename(src, dst, *a, **k)

    proposed = proposal(copies)
    with patch.object(obsidian_manager, "try_acquire_lock", side_effect=acquire), \
            patch.object(obsidian_manager, "release_lock", side_effect=release), \
            patch("os.rename", rename):
        op = apply(copies, proposed)
        assert op["status"] == "complete" and seen == [True]
        seen.clear()
        assert pm.restore(s, op["id"], confirm=True)["status"] == "restored"
    assert not held["now"] and remove.exists()


def test_managed_apply_answers_503_when_the_vault_is_busy(copies):
    import flask
    from api.routes import pending
    from rag.vault import obsidian_manager

    s, keep, remove = copies
    app = flask.Flask(__name__)
    app.register_blueprint(pending.pending_bp)
    proposed = proposal(copies)
    body = dict(action="cleanup", key="KeyOne", survivor="biblio_articles/one.pdf",
                actions={remove.relative_to(s.vault_root).as_posix(): "delete"},
                token=proposed["token"], confirm=True)
    with patch.object(pending, "_managed_entry", return_value=(s, "KeyOne", None)), \
            patch.object(pending.audit_manager, "is_scanning", return_value=False), \
            patch.object(obsidian_manager, "try_acquire_lock", return_value=None):
        r = app.test_client().post("/api/accession/pdfs/apply", json=body,
                                   headers={"X-Requested-With": "ChatEKLD"})
    assert r.status_code == 503, r.get_data(as_text=True)
    assert remove.exists()



# --- 2026-09-17 review, E2: inspect/complete, later-verb blocking, routes ----

def _trash_harness(s, remove, *, fail_dispatch):
    from contextlib import ExitStack
    from types import SimpleNamespace
    from tagging import zotero_write as zw
    from tagging.local_api import Version

    mutate(s, "INSERT INTO items VALUES (4,?,1,2)", ("ATTACH04",))
    mutate(s, "INSERT INTO itemAttachments VALUES (4,1,?,2)", (str(remove),))
    data = dict(itemType="attachment", parentItem="PARENT01", path=str(remove), linkMode="linked_file", title="Duplicate")
    get = Mock(return_value=(data, Version(2, "fixture-db")))
    stack = ExitStack()
    stack.enter_context(patch("tagging.zotero_write.resolve_transport",
                              return_value=SimpleNamespace(base_url="http://fixture", is_web=False, kind="local", can_write=True)))
    stack.enter_context(patch.object(zw, "get_item", get))
    stack.enter_context(patch.object(zw, "set_item_deleted", side_effect=zw.ZoteroUnavailable("lost") if fail_dispatch else None))
    return stack, get, data


def test_inspect_and_complete_settle_an_uncertain_trash_and_unblock_the_entry(copies):
    from tagging.local_api import Version

    s, keep, remove = copies
    stack, get, data = _trash_harness(s, remove, fail_dispatch=True)
    with stack:
        p = pm.trash_preview(s, {}, "KeyOne", "ATTACH04", "biblio_articles/one.pdf")
        op = pm.trash(s, {}, "KeyOne", "ATTACH04", "biblio_articles/one.pdf", p["token"],
                      confirm=True, accept_annotation_loss=True)
        assert op["status"] == "needs_inspection"
        # A later verb on the same entry is blocked and told which op to clear.
        with pytest.raises(review.ReviewConflict) as blocked:
            apply(copies, action="unmap")
        assert op["id"] in str(blocked.value)
        get.return_value = ({**data, "deleted": 1}, Version(3, "fixture-db"))
        seen = pm.inspect(s, {}, op["id"])
        assert seen["completable"]
        with pytest.raises(review.ReviewConflict):
            pm.complete(s, {}, op["id"], "stale-token", confirm=True)
        with pytest.raises(ValueError):
            pm.complete(s, {}, op["id"], seen["token"])
        done = pm.complete(s, {}, op["id"], seen["token"], confirm=True)
    assert done["status"] == "complete" and done["prior_status"] == "needs_inspection"
    pm.require_settled(s, "KeyOne")


def test_recovery_storage_on_another_volume_is_a_blocker_before_the_confirm(copies):
    """2026-09-17 review, F2: the cross-volume refusal happened mid-apply, after
    the operation was minted; it is now a preview blocker and re-checked before
    the operation is saved."""
    s, keep, remove = copies
    proposed = proposal(copies)
    assert proposed["executable"]
    with patch.object(pm, "_recovery_device", return_value=-1):
        blocked = proposal(copies)
        assert not blocked["executable"]
        assert any("another volume" in b for b in blocked["blockers"])
        with pytest.raises(ValueError, match="another volume"):
            apply(copies, proposed)
    assert remove.exists() and pm.operations(s) == []


def test_management_routes_restore_inspect_complete_and_candidates(copies):
    import flask
    from types import SimpleNamespace
    from api.routes import pending

    s, keep, remove = copies
    app = flask.Flask(__name__)
    app.register_blueprint(pending.pending_bp)
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    op = apply(copies)
    assert op["status"] == "complete" and not remove.exists()
    inventory = SimpleNamespace(records={"KeyOne": SimpleNamespace(zotero_item=None)})
    with patch.object(pending, "context", return_value=(inventory, s)), \
            patch.object(pending.audit_manager, "is_scanning", return_value=False):
        found = client.get("/api/accession/pdfs/candidates?q=ONE", headers=headers).get_json()
        assert found["paths"] == ["biblio_articles/one.pdf"] and found["total"] == 1
        assert client.post("/api/accession/pdfs/restore", json={"operation": op["id"]}, headers=headers).status_code == 400
        restored = client.post("/api/accession/pdfs/restore", json={"operation": op["id"], "confirm": True}, headers=headers)
        assert restored.status_code == 200 and restored.get_json()["operation"]["status"] == "restored"
        assert remove.exists()
        # Inspect/complete accept only retained Zotero operations.
        assert client.post("/api/accession/pdfs/inspect", json={"operation": op["id"]}, headers=headers).status_code == 400
        assert client.post("/api/accession/pdfs/complete", json={"operation": op["id"], "token": "x", "confirm": True},
                           headers=headers).status_code == 400


def test_trash_preview_token_ignores_changing_comparison_facts(copies):
    """2026-09-17 review, F3: trash_preview digested the whole survivor row, so
    a note gaining a reference to the survivor between preview and apply
    invalidated the token though nothing about the removal changed."""
    s, keep, remove = copies
    stack, _get, _data = _trash_harness(s, remove, fail_dispatch=False)
    with stack:
        first = pm.trash_preview(s, {}, "KeyOne", "ATTACH04", "biblio_articles/one.pdf")
        (s.vault_root / "mentions.md").write_text("see one.pdf", encoding="utf-8")
        second = pm.trash_preview(s, {}, "KeyOne", "ATTACH04", "biblio_articles/one.pdf")
    assert first["token"] == second["token"]
    assert set(first["plan"]["survivor"]) <= set(pm.STABLE_SURVIVOR_KEYS)


def test_symlink_rule_applies_below_the_vault_root_only(copies, tmp_path):
    """2026-09-17 review, F5: the walk also inspected the vault root and every
    ancestor above it, so a vault reached through a symlinked parent refused
    every PDF; a symlink inside the vault must still refuse."""
    from dataclasses import replace

    s, keep, remove = copies
    # A symlinked GRANDPARENT: the old walk skipped only vault_root.parent.
    alias = tmp_path / "alias_grandparent"
    alias.symlink_to(s.vault_root.parent.parent, target_is_directory=True)
    through_alias = replace(s, vault_root=alias / s.vault_root.parent.name / s.vault_root.name)
    assert pm._path(through_alias, "biblio_articles/one.pdf").name == "one.pdf"
    inner = s.vault_root / "biblio_articles" / "linked_dir"
    inner.symlink_to(s.vault_root / "biblio_articles", target_is_directory=True)
    with pytest.raises(ValueError, match="Symlink"):
        pm._path(s, "biblio_articles/linked_dir/one.pdf")


def test_operation_audit_line_names_the_pdf_and_the_operation(copies):
    """2026-09-17 review, F4: the op id was logged as the path."""
    s, keep, remove = copies
    with patch("core.utils.log_vault_write") as log:
        op = apply(copies)
    category, path, detail = log.call_args.args
    assert category == "pdf_management_cleanup" and path == "biblio_articles/redundant.pdf"
    assert op["id"] in detail and "complete" in detail


def test_a_cleanup_whose_restore_cannot_finish_can_be_inspected_and_completed(copies):
    """2026-09-17 review, F1: a cleanup stuck at needs_inspection had no exit —
    inspect/complete accepted only Zotero operations."""
    s, keep, remove = copies
    op = apply(copies)
    assert op["status"] == "complete"
    Path(op["steps"][0]["recovery"]).unlink()      # the recovery copy is gone
    stuck = pm.restore(s, op["id"], confirm=True)
    assert stuck["status"] == "needs_inspection"
    seen = pm.inspect(s, {}, op["id"])
    assert seen["completable"] and "nothing is left to restore" in seen["reason"]
    done = pm.complete(s, {}, op["id"], seen["token"], confirm=True)
    assert done["status"] == "complete" and done["prior_status"] == "needs_inspection"
    pm.require_settled(s, "KeyOne")


def test_a_cleanup_with_a_remaining_recovery_copy_names_the_manual_step(copies):
    s, keep, remove = copies
    op = apply(copies)
    remove.write_bytes(b"%PDF-someone-else")                # original path now occupied
    stuck = pm.restore(s, op["id"], confirm=True)
    assert stuck["status"] == "needs_inspection"
    seen = pm.inspect(s, {}, op["id"])
    assert not seen["completable"] and "review it by hand" in seen["reason"]


# -- Annual cleanup T6 (2026-09-17): the holding folder ---------------------


def test_holding_lists_a_moved_file_with_its_origin_hash_and_decision(copies):
    s, keep, remove = copies
    rel = remove.relative_to(s.vault_root).as_posix()
    sha = review.identity(remove)["sha256"]
    size = remove.stat().st_size
    proposed = proposal(copies)
    op = pm.apply(s, "KeyOne", keep.relative_to(s.vault_root).as_posix(), {rel: "delete"},
                  proposed["token"], confirm=True, decision_ref="01-duplicates.jsonl#grp-7")
    assert op["status"] == "complete" and op["decision_ref"] == "01-duplicates.jsonl#grp-7"
    listing = pm.holding(s)
    assert listing["held_count"] == 1 and listing["held_bytes"] == size
    assert listing["root"] == str(pm.recovery_root()) and listing["same_volume"] is True
    [entry] = listing["operations"]
    assert entry["decision_ref"] == "01-duplicates.jsonl#grp-7"
    [held] = entry["files"]
    assert held["rel"] == rel and held["sha256"] == sha and held["present"] is True
    assert Path(held["recovery"]).is_relative_to(pm.recovery_root())
    assert pm.restore(s, op["id"], confirm=True)["status"] == "restored"
    assert pm.holding(s)["held_count"] == 0


def test_a_malformed_decision_ref_refuses_before_anything_moves(copies):
    s, keep, remove = copies
    proposed = proposal(copies)
    with pytest.raises(ValueError):
        pm.apply(s, "KeyOne", keep.relative_to(s.vault_root).as_posix(),
                 {remove.relative_to(s.vault_root).as_posix(): "delete"},
                 proposed["token"], confirm=True, decision_ref="row\n1")
    assert remove.exists() and not pm.recovery_root().exists()
    assert pm.operations(s) == []


def test_holding_is_read_only_and_reports_unsettled_operations(copies):
    s, _keep, _remove = copies
    listing = pm.holding(s)
    assert listing["held_count"] == 0 and listing["unsettled_count"] == 0
    assert not rec.store_path().exists() and not pm.recovery_root().exists()
    pm._save(dict(id="op-x", kind="trash", scope=rec.scope(s), key="KeyOne",
                  plan={}, status="needs_inspection", at=1.0, steps=[], error="lost response"))
    listing = pm.holding(s)
    assert listing["unsettled"] == [dict(id="op-x", kind="trash", key="KeyOne",
                                         status="needs_inspection", error="lost response")]


def test_holding_route_and_decision_ref_pass_through(copies):
    import flask
    from types import SimpleNamespace
    from api.routes import pending

    s, keep, remove = copies
    app = flask.Flask(__name__)
    app.register_blueprint(pending.pending_bp)
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    rel = remove.relative_to(s.vault_root).as_posix()
    inventory = SimpleNamespace(records={"KeyOne": SimpleNamespace(zotero_item=None)})
    with patch.object(pending, "context", return_value=(inventory, s)), \
            patch.object(pending.audit_manager, "is_scanning", return_value=False):
        token = proposal(copies)["token"]
        applied = client.post("/api/accession/pdfs/apply", headers=headers, json={
            "key": "KeyOne", "action": "cleanup", "survivor": keep.relative_to(s.vault_root).as_posix(),
            "actions": {rel: "delete"}, "token": token, "confirm": True, "decision_ref": "row-9"})
        assert applied.status_code == 200, applied.get_json()
        assert applied.get_json()["operation"]["decision_ref"] == "row-9"
        listing = client.get("/api/accession/pdfs/holding", headers=headers).get_json()
    assert listing["held_count"] == 1
    assert listing["operations"][0]["files"][0]["rel"] == rel


def test_pdf_management_never_hardcodes_users_0():
    """A6: link/trash must use ``_zotero_lib_path``, never a personal-library
    literal. The docstring that forbids it may mention the string; quoted
    literals may not."""
    src = Path("accession/pdf_management.py").read_text(encoding="utf-8")
    assert "'users/0'" not in src and '"users/0"' not in src
    assert "def _lib_path(" in src
