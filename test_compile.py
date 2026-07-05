"""Tests for deck compilation and repair logic (log parsing, routing, hermetic mocks)."""
from unittest import mock
import pytest
from flask import Flask

from deckgen.compile import parse_latex_log, build_repair_messages
from api.routes.deck import deck_bp


def test_parse_latex_log_handles_critical_errors():
    log_text = r"""
This is pdfTeX, Version 3.141592653-2.6-1.40.25 (TeX Live 2023) (preloaded format=pdflatex)
entering extended mode
(./deck.tex
LaTeX2e <2023-11-01>
L3 programming layer <2023-10-23>
! Undefined control sequence.
l.15 \tableofcontens
                    
Here is some more log text.
! LaTeX Error: Environment frame undefined.

l.25 \begin{frame}
                  
)
"""
    errors = parse_latex_log(log_text)
    assert len(errors) == 2
    assert "Undefined control sequence." in errors[0]
    assert "l.15 \\tableofcontens" in errors[0]
    assert "LaTeX Error: Environment frame undefined." in errors[1]
    assert "l.25 \\begin{frame}" in errors[1]


def test_parse_latex_log_handles_relevant_warnings():
    log_text = """
LaTeX Warning: Citation 'xyz' on page 1 undefined on input line 25.
LaTeX Warning: Reference `abc' on page 2 undefined on input line 30.
LaTeX Warning: Some unrelated font warning we do not care about.
"""
    errors = parse_latex_log(log_text)
    assert len(errors) == 2
    assert "Citation 'xyz'" in errors[0]
    assert "Reference `abc'" in errors[1]


def test_build_repair_messages():
    tex = "\\documentclass{beamer}\\begin{document}\\end{document}"
    errors = ["Undefined control sequence \\foo"]
    msgs = build_repair_messages(tex, errors)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert "<deck>" in msgs[0]["content"]
    assert "Undefined control sequence \\foo" in msgs[0]["content"]


@pytest.fixture
def client():
    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(deck_bp)

    # Item 4.1: origin guard is now a before_request hook; register it on
    # the test app and patch at the canonical source location.
    from api.security import register_origin_guard
    register_origin_guard(app)

    with mock.patch("api.security.origin_is_local", return_value=True):
        yield app.test_client()


def test_api_compile_available(client):
    # find_latexmk (PATH-independent discovery — the frozen Finder-launched
    # app has no TeX dirs on PATH) is the single availability source.
    with mock.patch("api.routes.deck.find_latexmk", return_value="/Library/TeX/texbin/latexmk"):
        res = client.get("/api/deck/compile-available")
        assert res.status_code == 200
        assert res.json == {"available": True}

    with mock.patch("api.routes.deck.find_latexmk", return_value=None):
        res = client.get("/api/deck/compile-available")
        assert res.status_code == 200
        assert res.json == {"available": False}


def test_api_deck_compile_fix_requires_confirm_and_valid_deck(client):
    # This test asserts the REQUEST-validation contract (confirm + deck path),
    # not latexmk availability. The route checks find_latexmk() first as a
    # defense-in-depth backstop (the UI already gates on /compile-available), so
    # on a host WITHOUT a LaTeX suite (e.g. the CI Linux runner) that check would
    # short-circuit with the "latexmk not available" error before the confirm
    # check is ever reached — masking the contract under test. Mock latexmk as
    # present so the validation path is exercised deterministically on any host
    # (this is what makes the test hermetic rather than latexmk-dependent).
    with mock.patch("api.routes.deck.find_latexmk", return_value="/usr/bin/latexmk"):
        # Missing confirm
        res = client.post("/api/deck/compile-fix", json={"deck_path": "foo.tex"})
        assert res.status_code == 400
        assert "confirm: true" in res.json["error"]

        # Invalid path
        res = client.post("/api/deck/compile-fix", json={"deck_path": "foo.tex", "confirm": True})
        assert res.status_code == 400
        assert "Invalid or unreadable deck path" in res.json["error"]


# ---------------------------------------------------------------------------
# parse_latex_log — context must stop at the blank line that terminates a TeX
# error block (the old skip-blanks loop glued unrelated wrapped log lines
# onto the error shown to the model)


def test_parse_latex_log_context_stops_at_blank_line():
    log_text = (
        "! Undefined control sequence.\n"
        "l.15 \\tableofcontens\n"
        "\n"
        "  this indented line belongs to a DIFFERENT wrapped log entry\n"
    )
    errors = parse_latex_log(log_text)
    assert len(errors) == 1
    assert "DIFFERENT wrapped log entry" not in errors[0]


def test_is_missing_file_error():
    from deckgen.compile import is_missing_file_error

    assert is_missing_file_error("! LaTeX Error: File `cress-style.sty' not found.")
    assert is_missing_file_error("LaTeX Warning: File 'x.bib' not found")
    assert not is_missing_file_error("! Undefined control sequence.")
    # A missing IMAGE is repairable (comment the includegraphics) — only
    # .sty/.cls/.bib are suite-resolution noise.
    assert not is_missing_file_error("! LaTeX Error: File `brain.png' not found.")


# ---------------------------------------------------------------------------
# compile-fix worker loop (mocked latexmk + LLM): verify-before-done, backup,
# config-only model, missing-file short-circuit, lock behaviour


_DECK = (
    "\\documentclass{beamer}\n"
    "\\begin{document}\n"
    "\\begin{frame}{T}\n\\brokenmacro\n\\end{frame}\n"
    "\\end{document}\n"
)
_FIXED = _DECK.replace("\\brokenmacro", "fixed body")
_REPAIR_REPLY = "ISSUES:\n- undefined macro\n\n```latex\n" + _FIXED + "```\n"


def _sse_frames(resp):
    import json as _json
    frames = []
    for line in resp.get_data(as_text=True).splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            frames.append(_json.loads(line[len("data: "):]))
    return frames


def _post_compile_fix(client, deck_path, sha, headers=None):
    return client.post(
        "/api/deck/compile-fix",
        json={"deck_path": str(deck_path), "base_sha256": sha, "confirm": True},
        headers=headers or {},
    )


def _deck_sha(path):
    import hashlib
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def test_compile_fix_repairs_then_verifies(client, tmp_path):
    """fail → repair → write → SECOND compile verifies the written repair
    (the pre-fix loop could end right after an unverified write)."""
    import api.routes.deck as deckmod

    deck = tmp_path / "talk" / "main.tex"   # non-scaffold layout on purpose
    deck.parent.mkdir()
    deck.write_text(_DECK, encoding="utf-8")
    sha = _deck_sha(deck)

    compiles = []

    def _fake_compile(path, engine="pdflatex", timeout=180):
        compiles.append(engine)
        # Pass 1 fails with a repairable error; pass 2 (verification) succeeds.
        if len(compiles) == 1:
            return False, "! Undefined control sequence.\nl.4 \\brokenmacro\n"
        return True, "ok"

    calls = {}

    def _fake_stream(**kwargs):
        calls.update(kwargs)
        yield _REPAIR_REPLY

    with mock.patch.object(deckmod, "find_latexmk", return_value="/x/latexmk"), \
         mock.patch.object(deckmod, "compile_latex", side_effect=_fake_compile), \
         mock.patch.object(deckmod, "stream_chat_messages", _fake_stream):
        resp = _post_compile_fix(client, deck, sha)
        assert resp.status_code == 200
        frames = _sse_frames(resp)

    terminal = [f for f in frames if "compile" in f][-1]["compile"]
    assert terminal["success"] is True
    assert terminal["changed"] is True
    assert terminal["iterations"] == 2          # repair verified by pass 2
    # write_deck_at wrote back to the EXACT file (non-scaffold layout works).
    assert "fixed body" in deck.read_text(encoding="utf-8")
    assert (tmp_path / "talk" / "main.tex.bak").read_text(encoding="utf-8") == _DECK
    # Config-only model resolution: body carried no overrides, and the LLM
    # call used the config-resolved model, not anything client-supplied.
    from core.config import load_config, resolve_chat_model
    cfg = load_config()
    assert calls["model"] == resolve_chat_model(cfg, cfg.get("provider", "ollama"))
    # Lock released: a second identical request must not 409 on the lock
    # (it 409s on the sha, which proves the read-under-lock path ran).
    with mock.patch.object(deckmod, "find_latexmk", return_value="/x/latexmk"):
        again = _post_compile_fix(client, deck, sha)
    assert again.status_code == 409
    assert "changed" in (again.get_json() or {}).get("error", "")


def test_compile_fix_missing_suite_file_skips_llm(client, tmp_path):
    """Missing .sty errors are not LLM-fixable — the loop must stop without
    calling the model (which would 'fix' by deleting the \\usepackage)."""
    import api.routes.deck as deckmod

    deck = tmp_path / "d" / "d.tex"
    deck.parent.mkdir()
    deck.write_text(_DECK, encoding="utf-8")

    llm_called = {"n": 0}

    def _fake_stream(**kwargs):
        llm_called["n"] += 1
        yield "nope"

    with mock.patch.object(deckmod, "find_latexmk", return_value="/x/latexmk"), \
         mock.patch.object(
             deckmod, "compile_latex",
             return_value=(False, "! LaTeX Error: File `cress-style.sty' not found.\n"),
         ), \
         mock.patch.object(deckmod, "stream_chat_messages", _fake_stream):
        resp = _post_compile_fix(client, deck, _deck_sha(deck))
        frames = _sse_frames(resp)

    terminal = [f for f in frames if "compile" in f][-1]["compile"]
    assert terminal["success"] is False
    assert terminal["changed"] is False
    assert llm_called["n"] == 0
    assert deck.read_text(encoding="utf-8") == _DECK  # untouched


def test_compile_fix_409_when_deck_op_busy(client, tmp_path):
    import api.routes.deck as deckmod

    deck = tmp_path / "d" / "d.tex"
    deck.parent.mkdir()
    deck.write_text(_DECK, encoding="utf-8")

    assert deckmod._DECK_OP_LOCK.acquire(blocking=False)
    try:
        with mock.patch.object(deckmod, "find_latexmk", return_value="/x/latexmk"):
            resp = _post_compile_fix(client, deck, _deck_sha(deck))
        assert resp.status_code == 409
        assert "already running" in (resp.get_json() or {}).get("error", "")
    finally:
        deckmod._DECK_OP_LOCK.release()


def test_compile_fix_stale_sha_releases_lock(client, tmp_path):
    import api.routes.deck as deckmod

    deck = tmp_path / "d" / "d.tex"
    deck.parent.mkdir()
    deck.write_text(_DECK, encoding="utf-8")

    with mock.patch.object(deckmod, "find_latexmk", return_value="/x/latexmk"):
        resp = _post_compile_fix(client, deck, "0" * 64)
    assert resp.status_code == 409
    # The early 409 must have released the lock, or every later deck op 409s.
    assert deckmod._DECK_OP_LOCK.acquire(blocking=False)
    deckmod._DECK_OP_LOCK.release()


def test_compile_available_requires_local_origin():
    """The availability probe is origin-gated like every other deck route.

    Item 4.1: the gate is now the app-level before_request hook, not an
    inline check in each handler.
    """
    app = Flask(__name__)
    app.secret_key = "t"
    app.register_blueprint(deck_bp)
    from api.security import register_origin_guard
    register_origin_guard(app)
    with mock.patch("api.security.origin_is_local", return_value=False):
        resp = app.test_client().get("/api/deck/compile-available")
    assert resp.status_code == 403
