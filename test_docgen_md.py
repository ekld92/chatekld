"""docgen Markdown vault-write: the journalled create_note op + the generate-md route.

Hermetic (root conftest.py points CHATEKLD_BASE_DIR at a temp dir). The create
op is a real vault write into a tmp vault; the route's generation engine is
mocked at the docgen.markdown boundary and the obsidian op-lock is stubbed.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from deckgen.outline import Section
from deckgen.latex import SectionOutput

_HEADERS = {"X-Requested-With": "ChatEKLD"}


@pytest.fixture
def vault(tmp_path) -> Path:
    root = Path(tmp_path / "vault_owner").resolve()
    (root / "notes").mkdir(parents=True)
    return root


def _cfg(tmp_path) -> dict:
    # Archive dir explicitly OUTSIDE the vault (journal refuses inside-vault).
    return {"refactor_archive_dir": str(tmp_path / "arch")}


# --- journal create_note op ------------------------------------------------- #

def test_create_note_writes_and_journals(vault, tmp_path):
    from vault_history import journal
    from vault_history.create_note import create_note

    cfg = _cfg(tmp_path)
    man = journal.load(vault, cfg)
    op_id, status, err = create_note(vault, cfg, "notes/new.md", "# T\n\n## S\nbody\n", man)
    assert status == "applied" and err is None
    assert (vault / "notes" / "new.md").read_text(encoding="utf-8").startswith("# T")
    reloaded = journal.load(vault, cfg)
    op = journal.find_op(reloaded, op_id)
    assert op and op["kind"] == "create_note" and op["state"] == "applied"


def test_create_note_refuses_existing(vault, tmp_path):
    from vault_history import journal
    from vault_history.create_note import create_note

    cfg = _cfg(tmp_path)
    (vault / "notes" / "dup.md").write_text("existing\n", encoding="utf-8")
    man = journal.load(vault, cfg)
    op_id, status, err = create_note(vault, cfg, "notes/dup.md", "new\n", man)
    assert status == "failed" and "already exists" in err
    assert (vault / "notes" / "dup.md").read_text(encoding="utf-8") == "existing\n"


def test_revert_create_note_deletes_the_file(vault, tmp_path):
    from vault_history import journal
    from vault_history.create_note import create_note

    cfg = _cfg(tmp_path)
    man = journal.load(vault, cfg)
    op_id, status, _ = create_note(vault, cfg, "notes/gen.md", "# G\n", man)
    assert status == "applied"
    op = journal.find_op(journal.load(vault, cfg), op_id)
    res = journal.revert_op(vault, cfg, op)
    assert res["ok"] and res["status"] == "reverted"
    assert not (vault / "notes" / "gen.md").exists()


def test_revert_create_note_skips_when_edited(vault, tmp_path):
    from vault_history import journal
    from vault_history.create_note import create_note

    cfg = _cfg(tmp_path)
    man = journal.load(vault, cfg)
    op_id, _, _ = create_note(vault, cfg, "notes/gen.md", "# G\n", man)
    (vault / "notes" / "gen.md").write_text("# G\nedited by hand\n", encoding="utf-8")
    op = journal.find_op(journal.load(vault, cfg), op_id)
    res = journal.revert_op(vault, cfg, op)
    assert not res["ok"] and res["status"] == "skipped"
    assert (vault / "notes" / "gen.md").exists()   # user's edit preserved


def test_revert_create_note_idempotent_when_gone(vault, tmp_path):
    from vault_history import journal
    from vault_history.create_note import create_note

    cfg = _cfg(tmp_path)
    man = journal.load(vault, cfg)
    op_id, _, _ = create_note(vault, cfg, "notes/gen.md", "# G\n", man)
    (vault / "notes" / "gen.md").unlink()
    op = journal.find_op(journal.load(vault, cfg), op_id)
    res = journal.revert_op(vault, cfg, op)
    assert res["ok"] and res["status"] == "reverted"


# --- route guards ----------------------------------------------------------- #

def test_generate_md_requires_topic_and_confirm(vault):
    from app import app
    import api.routes.docgen as route

    client = app.test_client()
    with mock.patch.object(route.obsidian_manager, "get_vault_path", return_value=str(vault)):
        r = client.post("/api/docgen/generate-md", json={"confirm": True}, headers=_HEADERS)
        assert r.status_code == 400
        r = client.post("/api/docgen/generate-md", json={"topic": "T"}, headers=_HEADERS)
        assert r.status_code == 400


def test_generate_md_409_on_existing_note(vault):
    from app import app
    import api.routes.docgen as route

    (vault / "notes" / "my-note.md").write_text("x\n", encoding="utf-8")
    client = app.test_client()
    with mock.patch.object(route.obsidian_manager, "get_vault_path", return_value=str(vault)):
        r = client.post("/api/docgen/generate-md", json={
            "topic": "T", "confirm": True, "doc_name": "my-note", "scope_subdir": "notes",
        }, headers=_HEADERS)
        assert r.status_code == 409


def _sse_frames(resp):
    out = []
    for line in resp.get_data(as_text=True).splitlines():
        if line.startswith("data: ") and line[6:] != "[DONE]":
            out.append(json.loads(line[6:]))
    return out


@pytest.mark.parametrize("output_kind", ["report", "deck"])
def test_generate_md_writes_note_and_is_restorable(vault, output_kind):
    from app import app
    import api.routes.docgen as route
    from vault_history import journal

    def fake_outline(client, **kw):
        return [Section(title="Intro", points=["a"]), Section(title="Body", points=["b"])], mock.Mock()

    def fake_section(client, **kw):
        t = kw["section"].title
        return SectionOutput(title=t, body=f"## {t}\nGrounded prose (source: a.md).", placeholder=False)

    client = app.test_client()
    with mock.patch.object(route.obsidian_manager, "get_vault_path", return_value=str(vault)), \
         mock.patch.object(route.obsidian_manager, "try_acquire_lock", return_value=1), \
         mock.patch.object(route.obsidian_manager, "release_lock", return_value=None), \
         mock.patch.object(route.markdown_slides if output_kind == "deck" else route.md, "request_outline", fake_outline), \
         mock.patch.object(route.markdown_slides if output_kind == "deck" else route.md, "generate_section", fake_section):
        r = client.post("/api/docgen/generate-md", json={
            "topic": "My Note", "doc_name": "my-note", "doc_title": "My Note", "output_kind": output_kind,
            "scope_subdir": "notes", "confirm": True, "force_fresh": True,
        }, headers=_HEADERS)
        assert r.status_code == 200
        frames = _sse_frames(r)

    md_frames = [f for f in frames if "doc_md" in f]
    assert len(md_frames) == 1
    payload = md_frames[0]["doc_md"]
    assert payload["rel"] == "notes/my-note.md"
    assert payload["section_count"] == 2

    written = (vault / "notes" / "my-note.md")
    assert written.exists()
    text = written.read_text(encoding="utf-8")
    # Provenance frontmatter (2026-07-22) precedes the title; both stamped
    # keys and the legacy body shape must be present.
    assert text.startswith("---\n")
    assert 'generated_by: "CHERQAiN docgen"' in text
    assert "model:" in text and "provider:" in text
    assert "\n# My Note\n" in text
    assert "## Intro" in text and "## Body" in text
    if output_kind == "deck":
        assert "\n\n---\n\n## Body" in text
    assert "\\begin{frame}" not in text and "\\documentclass" not in text

    # The write is journalled + reversible via the shared Restore path.
    cfg = {}  # default archive dir (hermetic BASE_DIR, outside the vault)
    man = journal.load(vault, cfg)
    op = journal.find_op(man, payload["op_id"])
    assert op and op["kind"] == "create_note"
    res = journal.revert_op(vault, cfg, op)
    assert res["ok"]
    assert not written.exists()
