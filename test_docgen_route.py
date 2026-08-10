"""App-coupled tests for the Doc Generator route (article/report).

Hermetic (root conftest.py points CHATEKLD_BASE_DIR at a temp dir before app
import). The generation worker is exercised end-to-end with the deckgen engine
mocked at the docgen.generate boundary — no model, no retrieval, no writes to the
user's tree (scaffold lands under tmp_path).
"""
from __future__ import annotations

import json
import os
from unittest import mock

from deckgen.outline import Section
from deckgen.latex import SectionOutput


_HEADERS = {"X-Requested-With": "ChatEKLD"}

_ARTICLE_TEMPLATE = (
    "\\documentclass{article}\n"
    "\\title{Placeholder}\n"
    "\\begin{document}\n"
    "\\maketitle\n"
    "\\section{Example}\nold\n"
    "\\end{document}\n"
)


# --- path validators (shared helper) ---------------------------------------- #

def test_resolve_template_path(tmp_path):
    from api.routes._latexdoc import resolve_template_path

    tex = tmp_path / "paper.tex"
    tex.write_text("x", encoding="utf-8")
    assert resolve_template_path(str(tex)) == os.path.realpath(str(tex))
    other = tmp_path / "paper.pdf"
    other.write_text("x", encoding="utf-8")
    assert resolve_template_path(str(other)) is None
    assert resolve_template_path(str(tmp_path / "missing.tex")) is None
    assert resolve_template_path(None) is None
    assert resolve_template_path("relative/paper.tex") is None


def test_resolve_out_dir_rejects_system_and_missing(tmp_path):
    from api.routes._latexdoc import resolve_out_dir

    assert resolve_out_dir(str(tmp_path)) == os.path.realpath(str(tmp_path))
    assert resolve_out_dir("/usr") is None
    assert resolve_out_dir("/") is None
    assert resolve_out_dir(str(tmp_path / "nope")) is None
    assert resolve_out_dir(str(tmp_path / "paper.tex")) is None


# --- route guards ----------------------------------------------------------- #

def test_generate_requires_topic_and_template():
    from app import app

    client = app.test_client()
    r = client.post("/api/docgen/generate", json={"template_tex": "x"}, headers=_HEADERS)
    assert r.status_code == 400
    r = client.post("/api/docgen/generate", json={"topic": "T"}, headers=_HEADERS)
    assert r.status_code == 400


def test_generate_requires_valid_out_dir():
    from app import app

    client = app.test_client()
    r = client.post(
        "/api/docgen/generate",
        json={"topic": "T", "template_tex": _ARTICLE_TEMPLATE, "out_dir": "/usr"},
        headers=_HEADERS,
    )
    assert r.status_code == 400


def test_routes_require_local_origin():
    from app import app

    client = app.test_client()
    r = client.post("/api/docgen/load-template", json={"path": "/x.tex"})
    assert r.status_code == 403


def test_load_template_rejects_bad_path():
    from app import app

    client = app.test_client()
    r = client.post("/api/docgen/load-template", json={"path": "/nope/x.tex"}, headers=_HEADERS)
    assert r.status_code == 400


# --- end-to-end generate (mocked engine) ------------------------------------ #

def _sse_frames(resp):
    frames = []
    for line in resp.get_data(as_text=True).splitlines():
        if line.startswith("data: ") and line[6:] != "[DONE]":
            frames.append(json.loads(line[6:]))
    return frames


def test_generate_streams_doc_frame_and_scaffolds(tmp_path):
    """A full generate run: mocked outline + one prose section → a {"doc"} frame
    and a scaffolded <slug>/ folder under the chosen out_dir."""
    from app import app

    def fake_outline(client, **kw):
        return [Section(title="Intro", points=["a"])], mock.Mock()

    def fake_section(client, **kw):
        return SectionOutput(
            title="Intro",
            body="\\section{Intro}\nGrounded prose (source: a.md).",
            placeholder=False,
        )

    client = app.test_client()
    # The SSE worker runs in a daemon thread whose functions execute lazily as
    # the response is consumed — so get_data() MUST be called inside the patch
    # context, else the real (unmocked) engine runs.
    with mock.patch("api.routes.docgen.request_outline", fake_outline), \
         mock.patch("api.routes.docgen.generate_section", fake_section):
        r = client.post(
            "/api/docgen/generate",
            json={
                "topic": "My Report",
                "template_tex": _ARTICLE_TEMPLATE,
                "out_dir": str(tmp_path),
                "doc_name": "my-report",
                "citations_enabled": False,
                "force_fresh": True,
            },
            headers=_HEADERS,
        )
        assert r.status_code == 200
        frames = _sse_frames(r)
    doc_frames = [f for f in frames if "doc" in f]
    assert len(doc_frames) == 1
    doc = doc_frames[0]["doc"]
    assert doc["section_count"] == 1
    assert doc["placeholder_count"] == 0
    assert "\\section{Intro}" in doc["tex"]
    assert "\\documentclass{article}" in doc["tex"]
    assert "\\begin{frame}" not in doc["tex"]        # article, not beamer
    # Scaffolded on disk.
    assert doc["tex_path"] and os.path.isfile(doc["tex_path"])
    assert doc["tex_path"].endswith(os.path.join("my-report", "my-report.tex"))


def test_compile_preview_rejects_bad_path():
    """compile-preview 400s on a non-.tex / missing path (or a missing latexmk)."""
    from app import app

    client = app.test_client()
    r = client.post("/api/docgen/compile-preview", json={"deck_path": "/etc/passwd"}, headers=_HEADERS)
    assert r.status_code == 400


def test_compile_fix_rejects_bad_input():
    """compile-fix 400s without confirm / base_sha / a valid path (or no latexmk).

    Robust whether or not a TeX suite is installed: a missing latexmk 400s first,
    and every other guard here is also 400 — it never reaches a real compile."""
    from app import app

    client = app.test_client()
    r = client.post("/api/docgen/compile-fix", json={"deck_path": "/etc/passwd"}, headers=_HEADERS)
    assert r.status_code == 400


def test_preview_pdf_rejects_bad_path_and_missing_pdf(tmp_path):
    """preview-pdf 400s on an invalid path and 404s when no PDF was built."""
    from app import app

    client = app.test_client()
    r = client.get("/api/docgen/preview-pdf?deck_path=/nope/doc.tex", headers=_HEADERS)
    assert r.status_code == 400
    doc = tmp_path / "doc.tex"
    doc.write_text("\\documentclass{article}\\begin{document}\\end{document}\n")
    r = client.get(f"/api/docgen/preview-pdf?deck_path={doc}", headers=_HEADERS)
    assert r.status_code == 404


_AUG_DOC = (
    "\\documentclass{article}\n"
    "\\title{T}\n"
    "\\begin{document}\n"
    "\\maketitle\n"
    "\\section{Intro}\n"
    "old intro prose.\n"
    "\\section{Body}\n"
    "old body prose.\n"
    "\\end{document}\n"
)


def test_doc_sections_lists_sections(tmp_path):
    from app import app

    doc = tmp_path / "art.tex"
    doc.write_text(_AUG_DOC, encoding="utf-8")
    client = app.test_client()
    r = client.post("/api/docgen/doc-sections", json={"deck_path": str(doc)}, headers=_HEADERS)
    assert r.status_code == 200
    d = r.get_json()
    assert [s["title"] for s in d["sections"]] == ["Intro", "Body"]
    assert d["deck_sha256"]


def test_augment_requires_instruction(tmp_path):
    from app import app

    doc = tmp_path / "art.tex"
    doc.write_text(_AUG_DOC, encoding="utf-8")
    client = app.test_client()
    r = client.post("/api/docgen/augment", json={"deck_path": str(doc)}, headers=_HEADERS)
    assert r.status_code == 400


def test_apply_augment_requires_confirm_and_stage(tmp_path):
    from app import app

    doc = tmp_path / "art.tex"
    doc.write_text(_AUG_DOC, encoding="utf-8")
    client = app.test_client()
    r = client.post("/api/docgen/apply-augment",
                    json={"deck_path": str(doc), "base_sha256": "x"}, headers=_HEADERS)
    assert r.status_code == 400   # confirm missing
    r = client.post("/api/docgen/apply-augment",
                    json={"deck_path": str(doc), "base_sha256": "x", "confirm": True}, headers=_HEADERS)
    assert r.status_code == 400   # no proposed_sha256 (preview-binding guard)
    r = client.post("/api/docgen/apply-augment",
                    json={"deck_path": str(doc), "base_sha256": "x",
                          "proposed_sha256": "0" * 64, "confirm": True}, headers=_HEADERS)
    assert r.status_code == 400   # no staged proposal


def test_augment_preview_then_apply_roundtrip(tmp_path):
    from app import app
    import api.routes.docgen as route
    from deckgen.result import ChatResult

    doc = tmp_path / "art.tex"
    doc.write_text(_AUG_DOC, encoding="utf-8")

    class _FakeRunner:
        def __init__(self, *a, **k):
            pass

        def chat(self, message, **kw):
            # A deepened Intro section (valid prose article section).
            return ChatResult(text="\\section{Intro}\nnew deeper intro prose (source: a.md).")

    client = app.test_client()
    with mock.patch.object(route, "InProcessChatRunner", _FakeRunner), \
         mock.patch.object(route.obsidian_manager, "get_vault_path", return_value=str(tmp_path)):
        r = client.post("/api/docgen/augment", json={
            "deck_path": str(doc), "instruction": "deepen the intro",
            "operation": "deepen", "scope": "section", "section_index": 0,
            "citations_enabled": False,
        }, headers=_HEADERS)
        assert r.status_code == 200
        frames = _sse_frames(r)

    aug = [f["augment"] for f in frames if "augment" in f]
    assert len(aug) == 1
    payload = aug[0]
    assert payload["changed"] is True
    assert "new deeper intro prose" in payload["proposed_tex"]
    base_sha = payload["deck_sha256"]
    assert payload["proposed_sha256"]   # the preview-binding token rides the frame

    # A wrong preview-binding token 409s (audit #5: staging is one slot per
    # doc — apply must be bound to the exact proposal this preview showed).
    r = client.post("/api/docgen/apply-augment", json={
        "deck_path": str(doc), "base_sha256": base_sha,
        "proposed_sha256": "0" * 64, "confirm": True,
    }, headers=_HEADERS)
    assert r.status_code == 409
    assert "new deeper intro prose" not in doc.read_text(encoding="utf-8")

    # Apply the staged proposal with the matching token.
    r = client.post("/api/docgen/apply-augment", json={
        "deck_path": str(doc), "base_sha256": base_sha,
        "proposed_sha256": payload["proposed_sha256"], "confirm": True,
    }, headers=_HEADERS)
    assert r.status_code == 200
    assert "new deeper intro prose" in doc.read_text(encoding="utf-8")
    assert (tmp_path / "art.tex.bak").exists()   # backup before overwrite


def test_augment_truncation_guard_refuses_overcap_replace(tmp_path):
    """Audit #21 pin: an over-cap whole-document deepen/table/reformat must be
    REFUSED — the model would only see a clipped view of the span it replaces,
    and applying that replace would silently drop the unseen tail."""
    from app import app
    import api.routes.docgen as route

    doc = tmp_path / "art.tex"
    doc.write_text(_AUG_DOC, encoding="utf-8")
    client = app.test_client()
    with mock.patch.object(route, "AUGMENT_MAX_SOURCE_CHARS", 10):
        r = client.post("/api/docgen/augment", json={
            "deck_path": str(doc), "instruction": "deepen everything",
            "operation": "deepen", "scope": "whole", "citations_enabled": False,
        }, headers=_HEADERS)
        assert r.status_code == 200
        frames = _sse_frames(r)
    errs = [f["error"] for f in frames if "error" in f]
    assert errs and "too large" in errs[0]
    assert not any("augment" in f for f in frames)


def test_augment_section_index_out_of_range_is_error(tmp_path):
    """Audit #21 pin: scope=section with an index past the document's sections
    must fail cleanly, not IndexError into a generic 500."""
    from app import app

    doc = tmp_path / "art.tex"
    doc.write_text(_AUG_DOC, encoding="utf-8")  # 2 sections
    client = app.test_client()
    r = client.post("/api/docgen/augment", json={
        "deck_path": str(doc), "instruction": "deepen it",
        "operation": "deepen", "scope": "section", "section_index": 5,
        "citations_enabled": False,
    }, headers=_HEADERS)
    assert r.status_code == 200
    frames = _sse_frames(r)
    errs = [f["error"] for f in frames if "error" in f]
    assert errs and "out of range" in errs[0]


def test_augment_relabels_turn_level_error_to_info(tmp_path):
    """Audit #21 pin: a turn-level {"error"} the agent loop emits mid-turn is
    relabelled to a non-fatal {"info"} — the shared SSE consumer treats any
    {"error"} frame as terminal, so without the relabel one transient provider
    blip would discard the whole augmentation (deck.py's historical
    random-failure mode; the docgen copy of the relabel was untested)."""
    from app import app
    import api.routes.docgen as route
    from deckgen.result import ChatResult

    doc = tmp_path / "art.tex"
    doc.write_text(_AUG_DOC, encoding="utf-8")

    class _BlippingRunner:
        def __init__(self, *a, **k):
            pass

        def chat(self, message, **kw):
            on_event = kw.get("on_event")
            if on_event:
                on_event({"error": "transient provider blip"})   # mid-turn blip
            return ChatResult(text="\\section{Intro}\nrecovered prose (source: a.md).")

    client = app.test_client()
    with mock.patch.object(route, "InProcessChatRunner", _BlippingRunner), \
         mock.patch.object(route.obsidian_manager, "get_vault_path", return_value=str(tmp_path)):
        r = client.post("/api/docgen/augment", json={
            "deck_path": str(doc), "instruction": "deepen the intro",
            "operation": "deepen", "scope": "section", "section_index": 0,
            "citations_enabled": False,
        }, headers=_HEADERS)
        assert r.status_code == 200
        frames = _sse_frames(r)

    # The blip arrives as a ⚠ info, never as a terminal error frame…
    assert any("info" in f and "⚠" in f["info"] for f in frames), frames
    assert not any("error" in f for f in frames), frames
    # …and the augmentation still completes.
    aug = [f["augment"] for f in frames if "augment" in f]
    assert aug and aug[0]["changed"] is True


def test_generate_second_run_returns_409_while_locked(tmp_path):
    """The op-lock yields a clean 409 for a concurrent generate."""
    from app import app
    import api.routes.docgen as route

    client = app.test_client()
    assert route._DOCGEN_OP_LOCK.acquire(blocking=False)
    try:
        r = client.post(
            "/api/docgen/generate",
            json={"topic": "T", "template_tex": _ARTICLE_TEMPLATE, "out_dir": str(tmp_path)},
            headers=_HEADERS,
        )
        assert r.status_code == 409
    finally:
        route._DOCGEN_OP_LOCK.release()
