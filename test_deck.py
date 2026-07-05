"""App-coupled tests for the Deck Generator: in-process runner + route validators.

Runs inside the hermetic suite (root conftest.py points CHATEKLD_BASE_DIR at a
temp dir before app import), so importing rag.vault / api.routes.deck is safe.
``run_agent_loop`` is mocked — no model, no real retrieval.
"""
from __future__ import annotations

import os
from unittest import mock

from core.agent import InfoEvent, IterationEvent, TokenEvent


def test_inprocess_runner_accumulates_and_forwards_events():
    from deckgen import inprocess
    from deckgen.result import ChatResult

    def fake_loop(*, on_event, **kwargs):
        on_event(IterationEvent(index=1))
        on_event(TokenEvent(text="Hello "))
        on_event(TokenEvent(text="world"))
        on_event(InfoEvent(text="a note"))
        return mock.Mock(iteration_count=1)

    seen = []
    with mock.patch.object(inprocess, "run_agent_loop", side_effect=fake_loop):
        runner = inprocess.InProcessChatRunner(cfg={"provider": "ollama"})
        result = runner.chat(
            "hi", system_prompt="sp", provider="ollama", model="m",
            embed="e", agent=True, max_iters=3, on_event=seen.append,
        )

    assert isinstance(result, ChatResult)
    assert result.text == "Hello world"
    assert "a note" in result.infos
    assert result.iterations == 1
    # on_event saw the SSE-dict shape, not AgentEvent objects.
    assert {"token": "Hello "} in seen
    assert {"info": "a note"} in seen
    assert {"iteration": 1} in seen


def test_inprocess_runner_surfaces_loop_exception():
    from deckgen import inprocess

    with mock.patch.object(inprocess, "run_agent_loop", side_effect=RuntimeError("boom")):
        runner = inprocess.InProcessChatRunner(cfg={"provider": "ollama"})
        result = runner.chat("hi", provider="ollama", model="m", embed="e")
    assert result.error and "boom" in result.error


def test_inprocess_runner_temperature_lands_on_vault_chat_temperature_key():
    """The agent loop reads cfg['vault_chat_temperature'] (not 'temperature'),
    so a per-turn override must be written there or it is silently ignored."""
    from deckgen import inprocess

    captured = {}

    def fake_loop(*, cfg, on_event, **kwargs):
        captured["cfg"] = cfg
        return mock.Mock(iteration_count=0)

    base_cfg = {"provider": "ollama", "vault_chat_temperature": 0.3}
    with mock.patch.object(inprocess, "run_agent_loop", side_effect=fake_loop):
        runner = inprocess.InProcessChatRunner(cfg=base_cfg)
        runner.chat("hi", provider="ollama", model="m", embed="e", temperature=0.9)

    assert captured["cfg"]["vault_chat_temperature"] == 0.9
    # Caller's dict is not mutated (runner is reused across sections).
    assert base_cfg["vault_chat_temperature"] == 0.3


def test_inprocess_runner_max_tokens_lands_on_online_max_tokens_key():
    """The agent loop reads its output cap from cfg['online_max_tokens'] (local
    included), so the runner's max_tokens override must land on that key."""
    from deckgen import inprocess

    captured = {}

    def fake_loop(*, cfg, on_event, **kwargs):
        captured["cfg"] = cfg
        return mock.Mock(iteration_count=0)

    base_cfg = {"provider": "ollama", "online_max_tokens": 4096}
    with mock.patch.object(inprocess, "run_agent_loop", side_effect=fake_loop):
        runner = inprocess.InProcessChatRunner(cfg=base_cfg, max_tokens=2048)
        runner.chat("hi", provider="ollama", model="m", embed="e")

    assert captured["cfg"]["online_max_tokens"] == 2048
    # Caller's dict is not mutated (runner is reused across sections).
    assert base_cfg["online_max_tokens"] == 4096


def test_inprocess_runner_max_tokens_none_leaves_config_untouched():
    """max_tokens=None (the default) must not inject online_max_tokens."""
    from deckgen import inprocess

    captured = {}

    def fake_loop(*, cfg, on_event, **kwargs):
        captured["cfg"] = cfg
        return mock.Mock(iteration_count=0)

    base_cfg = {"provider": "ollama", "online_max_tokens": 4096}
    with mock.patch.object(inprocess, "run_agent_loop", side_effect=fake_loop):
        runner = inprocess.InProcessChatRunner(cfg=base_cfg)
        runner.chat("hi", provider="ollama", model="m", embed="e")

    assert captured["cfg"]["online_max_tokens"] == 4096


def test_resolve_template_path(tmp_path):
    from api.routes.deck import _resolve_template_path

    tex = tmp_path / "deck.tex"
    tex.write_text("x", encoding="utf-8")
    assert _resolve_template_path(str(tex)) == os.path.realpath(str(tex))

    # Wrong extension / missing / non-string are rejected.
    other = tmp_path / "deck.pdf"
    other.write_text("x", encoding="utf-8")
    assert _resolve_template_path(str(other)) is None
    assert _resolve_template_path(str(tmp_path / "missing.tex")) is None
    assert _resolve_template_path(None) is None
    assert _resolve_template_path("relative/deck.tex") is None


def test_resolve_out_dir_rejects_system_and_missing(tmp_path):
    from api.routes.deck import _resolve_out_dir

    assert _resolve_out_dir(str(tmp_path)) == os.path.realpath(str(tmp_path))
    assert _resolve_out_dir("/usr") is None
    assert _resolve_out_dir("/") is None
    assert _resolve_out_dir(str(tmp_path / "nope")) is None
    assert _resolve_out_dir(str(tmp_path / "deck.tex")) is None  # a file, not a dir


def test_deck_generate_requires_topic_and_template():
    """The route rejects missing topic/template with 400 before any model call."""
    from app import app

    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    r = client.post("/api/deck/generate", json={"template_tex": "x"}, headers=headers)
    assert r.status_code == 400
    r = client.post("/api/deck/generate", json={"topic": "T"}, headers=headers)
    assert r.status_code == 400


def test_deck_routes_require_local_origin():
    from app import app

    client = app.test_client()
    # No X-Requested-With header -> origin check fails -> 403.
    r = client.post("/api/deck/load-template", json={"path": "/x.tex"})
    assert r.status_code == 403


# --- LLM .tex integrity review (deckgen/review.py) --------------------------

_DOC_OK = (
    "\\documentclass{beamer}\n\\begin{document}\n"
    "\\section{Intro}\n\\begin{frame}{T}\nbody\n\\end{frame}\n"
    "\\end{document}\n"
)


def test_review_parse_extracts_issues_and_repaired_block():
    from deckgen.review import parse_review

    raw = (
        "ISSUES:\n"
        "- Unbalanced frame in section 1\n"
        "- Stray brace on the title slide\n\n"
        "```latex\n" + _DOC_OK + "```\n"
    )
    res = parse_review(raw)
    assert res.issues == [
        "Unbalanced frame in section 1",
        "Stray brace on the title slide",
    ]
    assert res.repaired_tex and "\\begin{document}" in res.repaired_tex


def test_review_parse_none_yields_no_issues_no_repair():
    from deckgen.review import parse_review

    assert parse_review("ISSUES:\nnone\n").issues == []
    assert parse_review("ISSUES:\nnone\n").repaired_tex is None


def test_review_parse_ignores_truncated_unclosed_block():
    from deckgen.review import parse_review

    # A code block whose closing fence never arrived must not be mistaken for a
    # repaired document (it would drop the unseen tail).
    raw = "ISSUES:\n- something\n\n```latex\n\\documentclass{beamer}\n\\begin{document}\n"
    res = parse_review(raw)
    assert res.repaired_tex is None
    assert res.repair_truncated is True   # cut-off repair is flagged (B4)


def test_review_parse_no_issue_substring_is_not_dropped():
    # Regression (B3): prose that merely CONTAINS "none" must NOT be treated as a
    # no-issue marker — the real finding has to survive.
    from deckgen.review import parse_review

    res = parse_review("The document has none of the required frames closed.\n")
    assert res.issues == ["The document has none of the required frames closed."]
    # A genuine whole-body affirmation still yields no issues, in EN and FR.
    assert parse_review("None.\n").issues == []
    assert parse_review("No issues found.\n").issues == []
    assert parse_review("Aucun problème détecté.\n").issues == []


def test_review_screen_accepts_clean_repair():
    from deckgen.review import screen_repair

    original = _DOC_OK.replace("body", "broken {")
    accepted, warnings = screen_repair(original, _DOC_OK)
    assert accepted == _DOC_OK
    assert warnings == []  # balanced frames + one document env


def test_review_screen_rejects_new_dangerous_macro():
    from deckgen.review import screen_repair

    malicious = _DOC_OK.replace("body", "\\write18{rm -rf /}")
    accepted, warnings = screen_repair(_DOC_OK, malicious)
    assert accepted is None
    assert warnings and "unsafe" in warnings[0].lower()


def test_review_screen_rejects_repair_with_no_frames():
    from deckgen.review import screen_repair

    no_frames = "\\documentclass{beamer}\n\\begin{document}\nhi\n\\end{document}\n"
    accepted, _ = screen_repair(_DOC_OK, no_frames)
    assert accepted is None


def test_review_screen_noop_when_identical_or_empty():
    from deckgen.review import screen_repair

    assert screen_repair(_DOC_OK, _DOC_OK) == (None, [])
    assert screen_repair(_DOC_OK, "") == (None, [])
    assert screen_repair(_DOC_OK, None) == (None, [])


def test_review_build_messages_wraps_and_flags_truncation():
    from deckgen.review import build_review_messages, REVIEW_MAX_CHARS

    msgs, truncated = build_review_messages(_DOC_OK)
    assert not truncated
    assert msgs[0]["role"] == "user" and "<deck>" in msgs[0]["content"]

    _, truncated2 = build_review_messages("x" * (REVIEW_MAX_CHARS + 10))
    assert truncated2 is True


def test_review_screen_rejects_newly_introduced_extended_macros():
    # The widened denylist (openout/special/directlua/luaexec/write) must be
    # screened the same as write18: a repair that INTRODUCES one is refused.
    from deckgen.review import screen_repair

    for macro in ("\\directlua{tex.print('x')}", "\\openout15=evil.tex",
                  "\\special{dvi:...}", "\\write16{x}"):
        bad = _DOC_OK.replace("body", macro)
        accepted, warnings = screen_repair(_DOC_OK, bad)
        assert accepted is None, macro
        assert warnings and "unsafe" in warnings[0].lower(), macro


def test_find_dangerous_macros_catches_csname_and_atinput_obfuscation():
    # The \csname write18\endcsname construction expands to \write18 but has no
    # backslash before "write18", so the plain \macro regex missed it. The shared
    # detector must catch it (and \@@input / \@input).
    from deckgen.assemble import find_dangerous_macros

    assert find_dangerous_macros(r"\csname write18\endcsname")        # shell-escape, obfuscated
    assert find_dangerous_macros(r"\csname input\endcsname{/etc/passwd}")
    assert find_dangerous_macros(r"\@@input{x}") and find_dangerous_macros(r"\@input{x}")
    # Benign \csname and a commented-out one are NOT flagged.
    assert not find_dangerous_macros(r"\csname mymacro\endcsname")
    assert not find_dangerous_macros(r"% \csname write18\endcsname")
    assert not find_dangerous_macros(r"\csname includegraphics\endcsname")  # letter-suffixed, safe


def test_review_screen_rejects_csname_obfuscated_shell_escape():
    # Regression for the headline security gap: a repair smuggling a shell-escape
    # via \csname must be refused, just like a bare \write18.
    from deckgen.review import screen_repair

    bad = _DOC_OK.replace("body", r"\csname write18\endcsname{rm -rf /}")
    accepted, warnings = screen_repair(_DOC_OK, bad)
    assert accepted is None
    assert warnings and "unsafe" in warnings[0].lower()


def test_review_screen_allows_repair_keeping_an_original_dangerous_macro():
    # screen_repair only blocks NEWLY-introduced macros: a \special present in
    # BOTH the original and the repair is not "introduced", so the repair stands
    # (a legitimate template using one is never falsely refused).
    from deckgen.review import screen_repair

    orig = _DOC_OK.replace("body", "\\special{dvi:x}\nbody")
    repaired = _DOC_OK.replace("body", "\\special{dvi:x}\nfixed")
    accepted, _ = screen_repair(orig, repaired)
    assert accepted == repaired


# --- _run_integrity_review: scaffold-before-review + deadline + degrade ------

def test_review_deadline_aborts_and_offers_no_repair():
    # A review that streams past its deadline must abandon the (partial) answer
    # and offer NO repair, with a clear error — the deck is already scaffolded by
    # the caller, so a slow review never costs the user the deck.
    import threading
    from api.routes import deck as deck_mod

    def fake_stream(**_kw):
        yield "ISSUES:\n- something\n"
        yield "```latex\n" + _DOC_OK + "```\n"  # a repair that is never consumed

    cancel = threading.Event()
    # First monotonic() call sets the deadline base (0.0 + 1.0 = 1.0); the
    # per-token check then jumps to 100.0 > 1.0 and trips immediately.
    with mock.patch.object(deck_mod, "stream_chat_messages", fake_stream), \
            mock.patch.object(deck_mod.time, "monotonic", side_effect=[0.0, 100.0]):
        payload = deck_mod._run_integrity_review(
            _DOC_OK, provider="ollama", model="m", max_tokens=256,
            cfg={}, cancel=cancel, deadline_s=1.0,
        )
    assert "timed out" in payload["error"].lower()
    assert payload["changed"] is False
    assert not payload["repaired_tex"]


def test_review_large_deck_degrades_to_issues_only_note():
    # An answer with issues but a cut-off (unterminated) repair fence keeps the
    # issues and adds a "too large for an auto-repair" note rather than looking
    # like "nothing fixable".
    import threading
    from api.routes import deck as deck_mod

    raw = "ISSUES:\n- problem A\n\n```latex\n" + _DOC_OK  # no closing fence

    def fake_stream(**_kw):
        yield raw

    cancel = threading.Event()
    with mock.patch.object(deck_mod, "stream_chat_messages", fake_stream):
        payload = deck_mod._run_integrity_review(
            _DOC_OK, provider="ollama", model="m", max_tokens=256,
            cfg={}, cancel=cancel,
        )
    assert payload["issues"] == ["problem A"]
    assert payload["changed"] is False
    assert any("too large" in w.lower() for w in payload["repaired_warnings"])


# --- /api/deck/apply-repair -------------------------------------------------

def test_apply_repair_requires_confirm_and_valid_dir(tmp_path):
    from app import app

    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}

    # Missing confirm -> 400.
    r = client.post("/api/deck/apply-repair", json={
        "out_dir": str(tmp_path), "deck_name": "d", "tex": _DOC_OK,
    }, headers=headers)
    assert r.status_code == 400

    # Confirmed but bad out_dir -> 400.
    r = client.post("/api/deck/apply-repair", json={
        "out_dir": "/usr", "deck_name": "d", "tex": _DOC_OK, "confirm": True,
    }, headers=headers)
    assert r.status_code == 400

    # Origin guard.
    r = client.post("/api/deck/apply-repair", json={"confirm": True})
    assert r.status_code == 403


def _scaffold_existing(tmp_path):
    """Simulate a prior generation: write the deck + Makefile on disk; return paths+hash."""
    import hashlib
    from deckgen.scaffold import scaffold_deck
    scaffold_deck(str(tmp_path), "my_deck", _DOC_OK)
    written = tmp_path / "my_deck" / "my_deck.tex"
    makefile = tmp_path / "my_deck" / "Makefile"
    base = hashlib.sha256(written.read_bytes()).hexdigest()
    return written, makefile, base


def test_apply_repair_writes_tex_only_and_keeps_makefile(tmp_path):
    from app import app

    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    written, makefile, base = _scaffold_existing(tmp_path)
    mk_before = makefile.read_text(encoding="utf-8")

    repaired = _DOC_OK.replace("body", "fixed body")
    r = client.post("/api/deck/apply-repair", json={
        "out_dir": str(tmp_path), "deck_name": "My Deck", "tex": repaired,
        "base_sha256": base, "confirm": True,
    }, headers=headers)
    assert r.status_code == 200, r.get_json()
    assert "fixed body" in written.read_text(encoding="utf-8")
    assert makefile.read_text(encoding="utf-8") == mk_before   # Makefile NOT clobbered


def test_apply_repair_refuses_stale_deck(tmp_path):
    from app import app

    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    written, _makefile, _base = _scaffold_existing(tmp_path)

    r = client.post("/api/deck/apply-repair", json={
        "out_dir": str(tmp_path), "deck_name": "My Deck",
        "tex": _DOC_OK.replace("body", "fixed"),
        "base_sha256": "deadbeef" * 8, "confirm": True,   # wrong token
    }, headers=headers)
    assert r.status_code == 409
    assert "body" in written.read_text(encoding="utf-8")    # left untouched


def test_apply_repair_rescreens_and_blocks_smuggled_macro(tmp_path):
    from app import app

    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    written, _makefile, base = _scaffold_existing(tmp_path)

    evil = _DOC_OK.replace("body", "\\write18{rm -rf /}")
    r = client.post("/api/deck/apply-repair", json={
        "out_dir": str(tmp_path), "deck_name": "My Deck", "tex": evil,
        "base_sha256": base, "confirm": True,
    }, headers=headers)
    assert r.status_code == 400
    assert "write18" not in written.read_text(encoding="utf-8")  # not written


def test_apply_repair_requires_base_sha256(tmp_path):
    from app import app

    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    _scaffold_existing(tmp_path)
    r = client.post("/api/deck/apply-repair", json={
        "out_dir": str(tmp_path), "deck_name": "My Deck",
        "tex": _DOC_OK.replace("body", "x"), "confirm": True,
    }, headers=headers)
    assert r.status_code == 400


# --- review wired into /api/deck/generate -----------------------------------

def _sse_frames(text):
    """Parse `data: {...}` JSON frames out of an SSE response body."""
    import json
    frames = []
    for line in text.splitlines():
        if line.startswith("data: ") and not line.endswith("[DONE]"):
            try:
                frames.append(json.loads(line[len("data: "):]))
            except ValueError:
                pass
    return frames


def test_generate_runs_review_and_attaches_repair_frame(tmp_path):
    """With review_enabled, the terminal deck frame carries the screened repair."""
    from app import app
    import api.routes.deck as deckmod
    from deckgen.outline import Section
    from deckgen.assemble import SectionOutput

    template = (
        "\\documentclass{beamer}\n\\begin{document}\n"
        "\\begin{frame}\\titlepage\\end{frame}\n"
        "\\begin{frame}{Outline}\\tableofcontents\\end{frame}\n"
        "\\end{document}\n"
    )
    section = SectionOutput(
        title="Intro",
        body="\\section{Intro}\n\\begin{frame}{T}\nbody\n\\end{frame}",
        placeholder=False,
    )
    review_response = "ISSUES:\n- fixed a stray brace\n\n```latex\n" + _DOC_OK + "```\n"

    def fake_stream(**kwargs):
        yield review_response

    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    with mock.patch.object(deckmod, "request_outline", return_value=([Section(title="Intro", points=["a"])], "")), \
         mock.patch.object(deckmod, "generate_section", return_value=section), \
         mock.patch.object(deckmod, "stream_chat_messages", side_effect=fake_stream), \
         mock.patch.object(deckmod.obsidian_manager, "get_status", return_value="done"):
        r = client.post("/api/deck/generate", json={
            "topic": "T", "template_tex": template, "out_dir": str(tmp_path),
            "deck_name": "reviewed", "review_enabled": True, "citations_enabled": False,
        }, headers=headers)
        body = r.get_data(as_text=True)

    frames = _sse_frames(body)
    deck = next((f["deck"] for f in frames if "deck" in f), None)
    assert deck is not None
    assert deck["review"] and deck["review"]["ran"] is True
    assert deck["review"]["issues"] == ["fixed a stray brace"]
    assert deck["review"]["changed"] is True
    assert "\\begin{document}" in deck["review"]["repaired_tex"]


def test_generate_skips_review_when_disabled(tmp_path):
    from app import app
    import api.routes.deck as deckmod
    from deckgen.outline import Section
    from deckgen.assemble import SectionOutput

    template = (
        "\\documentclass{beamer}\n\\begin{document}\n"
        "\\begin{frame}{Outline}\\tableofcontents\\end{frame}\n"
        "\\end{document}\n"
    )
    section = SectionOutput(title="Intro", body="\\section{Intro}\n\\begin{frame}{T}\nbody\n\\end{frame}")

    called = {"review": False}

    def fake_stream(**kwargs):
        called["review"] = True
        yield "ISSUES:\nnone\n"

    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    with mock.patch.object(deckmod, "request_outline", return_value=([Section(title="Intro", points=["a"])], "")), \
         mock.patch.object(deckmod, "generate_section", return_value=section), \
         mock.patch.object(deckmod, "stream_chat_messages", side_effect=fake_stream), \
         mock.patch.object(deckmod.obsidian_manager, "get_status", return_value="done"):
        r = client.post("/api/deck/generate", json={
            "topic": "T", "template_tex": template, "out_dir": str(tmp_path),
            "deck_name": "noreview", "review_enabled": False, "citations_enabled": False,
        }, headers=headers)
        body = r.get_data(as_text=True)

    frames = _sse_frames(body)
    deck = next((f["deck"] for f in frames if "deck" in f), None)
    assert deck is not None
    assert deck.get("review") is None
    assert called["review"] is False  # the review LLM call never fired


def test_generate_section_error_event_is_not_fatal(tmp_path):
    """A per-section ``{"error"}`` event must NOT abort the whole deck.

    The agent loop emits an ErrorEvent on a provider failure (LM Studio memory
    hiccup / timeout); the shared SSE consumer treats any ``{"error"}`` frame as
    terminal. The route relabels a turn-level error to a non-fatal ``{"info"}``
    so generate_section's placeholder path keeps the deck assembling — the
    terminal deck frame still arrives, and the error surfaces as a ⚠ info.
    """
    from app import app
    import api.routes.deck as deckmod
    from deckgen.outline import Section
    from deckgen.assemble import SectionOutput

    template = (
        "\\documentclass{beamer}\n\\begin{document}\n"
        "\\begin{frame}{Outline}\\tableofcontents\\end{frame}\n"
        "\\end{document}\n"
    )

    def fake_section(client, **kwargs):
        # Emit a turn-level error event exactly like the agent loop would, then
        # degrade to a placeholder frame (the real generate_section's behaviour).
        on_event = kwargs.get("on_event")
        if on_event is not None:
            on_event({"error": "LM Studio call timed out"})
        return SectionOutput(
            title="Intro",
            body="\\section{Intro}\n\\begin{frame}{T}\nplaceholder\n\\end{frame}",
            placeholder=True,
        )

    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    with mock.patch.object(deckmod, "request_outline", return_value=([Section(title="Intro", points=["a"])], "")), \
         mock.patch.object(deckmod, "generate_section", side_effect=fake_section), \
         mock.patch.object(deckmod.obsidian_manager, "get_status", return_value="done"):
        r = client.post("/api/deck/generate", json={
            "topic": "T", "template_tex": template, "out_dir": str(tmp_path),
            "deck_name": "resilient", "review_enabled": False, "citations_enabled": False,
        }, headers=headers)
        body = r.get_data(as_text=True)

    frames = _sse_frames(body)
    # The deck still landed despite the section error.
    deck = next((f["deck"] for f in frames if "deck" in f), None)
    assert deck is not None
    assert deck["placeholder_count"] == 1
    # No fatal error frame reached the client; the error was relabelled to info.
    assert not any("error" in f for f in frames)
    assert any("error" not in f and "LM Studio call timed out" in f.get("info", "") for f in frames)


class _FakeRunnerEmitsError:
    """InProcessChatRunner stand-in whose .chat emits a turn-level {"error"}
    (a transient provider blip) via on_event BEFORE returning usable text —
    exercises the augment worker's relabel-to-info parity with generate (m5)."""
    def __init__(self, body, err="LM Studio call timed out"):
        self._body, self._err = body, err

    def __call__(self, *a, **k):
        return self

    def chat(self, *a, **k):
        from deckgen.result import ChatResult
        on_event = k.get("on_event")
        if on_event is not None:
            on_event({"error": self._err})
        return ChatResult(text=self._body, error=None)


def test_augment_turn_error_event_is_not_fatal(tmp_path):
    """A turn-level ``{"error"}`` during an augment turn (transient provider
    blip) must NOT abort the whole augmentation — the worker relabels it to a
    non-fatal ⚠ info (parity with generate, 2026-07-05 audit m5). The proposal
    frame still arrives; only a truly-empty run yields a terminal error."""
    from app import app
    import api.routes.deck as deckmod
    deck, _sha = _write_deck(tmp_path)
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    new_body = ("\\section{Intro}\n\\begin{frame}{Intro}\n"
                "  \\begin{itemize}\\item deepened detail\\end{itemize}\n\\end{frame}")
    with mock.patch.object(deckmod, "InProcessChatRunner", _FakeRunnerEmitsError(new_body)), \
         mock.patch.object(deckmod.obsidian_manager, "get_status", return_value="done"):
        r = client.post("/api/deck/augment", json={
            "deck_path": str(deck), "instruction": "deepen it", "operation": "deepen",
            "scope": "section", "section_index": 0, "citations_enabled": False,
        }, headers=headers)
        frames = _sse_frames(r.get_data(as_text=True))
    # The proposal still landed despite the turn error.
    aug = next((f["augment"] for f in frames if "augment" in f), None)
    assert aug is not None and "deepened detail" in aug["proposed_tex"]
    # No fatal error frame; the error was relabelled to a ⚠ info.
    assert not any("error" in f for f in frames)
    assert any("LM Studio call timed out" in f.get("info", "") for f in frames)


def _ckpt_template():
    return (
        "\\documentclass{beamer}\n\\begin{document}\n"
        "\\begin{frame}{Outline}\\tableofcontents\\end{frame}\n"
        "\\end{document}\n"
    )


def test_generate_resumes_from_checkpoint(tmp_path):
    """A pre-seeded checkpoint (outline + section 1) is reused: the outline turn
    is skipped and only the missing section is generated."""
    from app import app
    import api.routes.deck as deckmod
    from deckgen import checkpoint
    from deckgen.outline import Section
    from deckgen.assemble import SectionOutput

    # Seed a checkpoint at a fixed key (compute_job_key is patched to match).
    sections = [Section(title="Intro", points=["a"]), Section(title="Methods", points=["b"])]
    manifest = checkpoint.new_manifest(
        job_key="ckpt-resume", topic="T", slug="resumed", out_dir=str(tmp_path), sections=sections,
    )
    checkpoint.set_section(manifest, 1, SectionOutput(
        title="Intro", body="\\section{Intro}\n\\begin{frame}{I}\nsaved\n\\end{frame}",
        placeholder=False,
    ))
    checkpoint.save(deckmod._checkpoints_dir(), manifest)

    gen = mock.Mock(return_value=SectionOutput(
        title="Methods", body="\\section{Methods}\n\\begin{frame}{M}\nfresh\n\\end{frame}",
    ))
    outline_mock = mock.Mock(return_value=(sections, ""))

    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    with mock.patch.object(deckmod.checkpoint, "compute_job_key", return_value="ckpt-resume"), \
         mock.patch.object(deckmod, "request_outline", outline_mock), \
         mock.patch.object(deckmod, "generate_section", gen), \
         mock.patch.object(deckmod.obsidian_manager, "get_status", return_value="done"):
        r = client.post("/api/deck/generate", json={
            "topic": "T", "template_tex": _ckpt_template(), "out_dir": str(tmp_path),
            "deck_name": "resumed", "review_enabled": False, "citations_enabled": False,
        }, headers=headers)
        body = r.get_data(as_text=True)

    frames = _sse_frames(body)
    deck = next((f["deck"] for f in frames if "deck" in f), None)
    assert deck is not None
    assert deck["resumed"] is True and deck["reused_sections"] == 1
    assert deck["section_count"] == 2
    outline_mock.assert_not_called()        # saved outline reused, no outline turn
    assert gen.call_count == 1              # only the missing section regenerated
    # The reused section's saved body made it into the deck.
    assert "saved" in deck["tex"] and "fresh" in deck["tex"]


def test_generate_resume_retries_placeholder_section(tmp_path):
    """A placeholder (failed) section in the checkpoint is RETRIED on resume, not
    reused — the whole point of resume is to recover a transiently-failed section,
    so a placeholder must never be treated as 'already generated'. Only real
    sections count toward reused_sections."""
    from app import app
    import api.routes.deck as deckmod
    from deckgen import checkpoint
    from deckgen.outline import Section
    from deckgen.assemble import SectionOutput

    sections = [Section(title="Intro", points=["a"]), Section(title="Methods", points=["b"])]
    manifest = checkpoint.new_manifest(
        job_key="ckpt-ph", topic="T", slug="phretry", out_dir=str(tmp_path), sections=sections,
    )
    # Section 1 = real saved content; section 2 = a placeholder a prior failed run
    # left behind (a legacy/pre-fix checkpoint could also hold one).
    checkpoint.set_section(manifest, 1, SectionOutput(
        title="Intro", body="\\section{Intro}\n\\begin{frame}{I}\nsaved\n\\end{frame}",
        placeholder=False,
    ))
    checkpoint.set_section(manifest, 2, SectionOutput(
        title="Methods", body="\\section{Methods}\n\\begin{frame}{M}\nno content generated\n\\end{frame}",
        placeholder=True,
    ))
    checkpoint.save(deckmod._checkpoints_dir(), manifest)

    gen = mock.Mock(return_value=SectionOutput(
        title="Methods", body="\\section{Methods}\n\\begin{frame}{M}\nregenerated\n\\end{frame}",
    ))
    outline_mock = mock.Mock(return_value=(sections, ""))

    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    with mock.patch.object(deckmod.checkpoint, "compute_job_key", return_value="ckpt-ph"), \
         mock.patch.object(deckmod, "request_outline", outline_mock), \
         mock.patch.object(deckmod, "generate_section", gen), \
         mock.patch.object(deckmod.obsidian_manager, "get_status", return_value="done"):
        r = client.post("/api/deck/generate", json={
            "topic": "T", "template_tex": _ckpt_template(), "out_dir": str(tmp_path),
            "deck_name": "phretry", "review_enabled": False, "citations_enabled": False,
        }, headers=headers)
        body = r.get_data(as_text=True)

    frames = _sse_frames(body)
    deck = next((f["deck"] for f in frames if "deck" in f), None)
    assert deck is not None
    outline_mock.assert_not_called()          # saved outline reused
    # Only the real section 1 counts as reused; the placeholder section 2 is retried.
    assert deck["reused_sections"] == 1
    assert gen.call_count == 1                # section 2 regenerated, section 1 reused
    assert "saved" in deck["tex"]            # real section kept verbatim
    assert "regenerated" in deck["tex"]      # placeholder replaced by fresh content
    assert "no content generated" not in deck["tex"]  # stale placeholder body gone


def test_generate_success_deletes_checkpoint(tmp_path):
    """A clean generation leaves no resumable checkpoint behind."""
    from app import app
    import api.routes.deck as deckmod
    from deckgen.outline import Section
    from deckgen.assemble import SectionOutput
    import os

    section = SectionOutput(title="Intro", body="\\section{Intro}\n\\begin{frame}{T}\nb\n\\end{frame}")
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    with mock.patch.object(deckmod.checkpoint, "compute_job_key", return_value="ckpt-clean"), \
         mock.patch.object(deckmod, "request_outline", return_value=([Section(title="Intro", points=["a"])], "")), \
         mock.patch.object(deckmod, "generate_section", return_value=section), \
         mock.patch.object(deckmod.obsidian_manager, "get_status", return_value="done"):
        r = client.post("/api/deck/generate", json={
            "topic": "T", "template_tex": _ckpt_template(), "out_dir": str(tmp_path),
            "deck_name": "clean", "review_enabled": False, "citations_enabled": False,
        }, headers=headers)
        body = r.get_data(as_text=True)

    frames = _sse_frames(body)
    deck = next((f["deck"] for f in frames if "deck" in f), None)
    assert deck is not None and deck["resumed"] is False
    assert not os.path.exists(os.path.join(deckmod._checkpoints_dir(), "ckpt-clean.json"))


def test_generate_force_fresh_ignores_checkpoint(tmp_path):
    """force_fresh discards an existing checkpoint and re-designs the outline."""
    from app import app
    import api.routes.deck as deckmod
    from deckgen import checkpoint
    from deckgen.outline import Section
    from deckgen.assemble import SectionOutput

    seeded = [Section(title="Old", points=[])]
    manifest = checkpoint.new_manifest(
        job_key="ckpt-fresh", topic="T", slug="fresh", out_dir=str(tmp_path), sections=seeded,
    )
    checkpoint.set_section(manifest, 1, SectionOutput(title="Old", body="\\section{Old}"))
    checkpoint.save(deckmod._checkpoints_dir(), manifest)

    outline_mock = mock.Mock(return_value=([Section(title="New", points=["a"])], ""))
    section = SectionOutput(title="New", body="\\section{New}\n\\begin{frame}{T}\nb\n\\end{frame}")
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    with mock.patch.object(deckmod.checkpoint, "compute_job_key", return_value="ckpt-fresh"), \
         mock.patch.object(deckmod, "request_outline", outline_mock), \
         mock.patch.object(deckmod, "generate_section", return_value=section), \
         mock.patch.object(deckmod.obsidian_manager, "get_status", return_value="done"):
        r = client.post("/api/deck/generate", json={
            "topic": "T", "template_tex": _ckpt_template(), "out_dir": str(tmp_path),
            "deck_name": "fresh", "review_enabled": False, "citations_enabled": False,
            "force_fresh": True,
        }, headers=headers)
        body = r.get_data(as_text=True)

    frames = _sse_frames(body)
    deck = next((f["deck"] for f in frames if "deck" in f), None)
    assert deck is not None
    assert deck["resumed"] is False and deck["reused_sections"] == 0
    outline_mock.assert_called_once()  # the seeded outline was discarded


# ---------------------------------------------------------------------------
# Augment an existing deck (/api/deck/deck-sections, /augment, /apply-augment)
# ---------------------------------------------------------------------------

_AUG_DECK = (
    "\\documentclass{beamer}\n\\usetheme{Madrid}\n\\title{Demo}\n"
    "\\begin{document}\n\n\\frame{\\titlepage}\n\n"
    "\\begin{frame}{Outline}\n  \\tableofcontents\n\\end{frame}\n\n"
    "\\section{Intro}\n\\begin{frame}{Intro}\n  \\begin{itemize}\\item what it is\\end{itemize}\n\\end{frame}\n\n"
    "\\section{Methods}\n\\begin{frame}{Methods}\n  \\begin{itemize}\\item how we did it\\end{itemize}\n\\end{frame}\n\n"
    "\\end{document}\n"
)


def _write_deck(tmp_path, name="deck.tex", text=_AUG_DECK):
    import hashlib
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p, hashlib.sha256(p.read_bytes()).hexdigest()


class _FakeRunner:
    """Stand-in for InProcessChatRunner: .chat returns a canned ChatResult."""
    def __init__(self, body, error=None):
        self._body, self._error = body, error

    def __call__(self, *args, **kwargs):  # construct -> returns self
        return self

    def chat(self, *args, **kwargs):
        from deckgen.result import ChatResult
        return ChatResult(text=self._body, error=self._error)


def _preview_augment(client, headers, deck, body_tex, **overrides):
    """Drive one /augment preview with a mocked model; return (augment_frame, frames)."""
    import api.routes.deck as deckmod
    payload = {
        "deck_path": str(deck), "instruction": "deepen it", "operation": "deepen",
        "scope": "section", "section_index": 0, "citations_enabled": False,
    }
    payload.update(overrides)
    with mock.patch.object(deckmod, "InProcessChatRunner", _FakeRunner(body_tex)), \
         mock.patch.object(deckmod.obsidian_manager, "get_status", return_value="done"):
        r = client.post("/api/deck/augment", json=payload, headers=headers)
        frames = _sse_frames(r.get_data(as_text=True))
    aug = next((f["augment"] for f in frames if "augment" in f), None)
    return aug, frames


def test_resolve_existing_deck(tmp_path):
    from api.routes.deck import _resolve_existing_deck
    import os
    deck = tmp_path / "d.tex"
    deck.write_text(_AUG_DECK, encoding="utf-8")
    assert _resolve_existing_deck(str(deck)) == os.path.realpath(str(deck))
    sty = tmp_path / "x.sty"
    sty.write_text("x", encoding="utf-8")
    assert _resolve_existing_deck(str(sty)) is None
    assert _resolve_existing_deck(str(tmp_path / "missing.tex")) is None
    assert _resolve_existing_deck("relative/d.tex") is None
    assert _resolve_existing_deck("/usr/x.tex") is None
    assert _resolve_existing_deck(None) is None


def test_read_deck_strict_rejects_non_utf8_and_oversize(tmp_path):
    from api.routes.deck import _read_deck_strict, _DeckReadError
    bad = tmp_path / "bad.tex"
    bad.write_bytes(b"\\begin{document}\xff\\end{document}\n")  # invalid UTF-8 byte
    import pytest
    with pytest.raises(_DeckReadError):
        _read_deck_strict(str(bad))
    big = tmp_path / "big.tex"
    big.write_bytes(b"x" * (1_000_001))
    with pytest.raises(_DeckReadError):
        _read_deck_strict(str(big))


def test_deck_sections_lists_titles(tmp_path):
    from app import app
    deck, sha = _write_deck(tmp_path)
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    r = client.post("/api/deck/deck-sections", json={"deck_path": str(deck)}, headers=headers)
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert [s["title"] for s in d["sections"]] == ["Intro", "Methods"]
    assert d["deck_sha256"] == sha


def test_deck_sections_non_utf8_is_400(tmp_path):
    from app import app
    bad = tmp_path / "bad.tex"
    bad.write_bytes(b"\\begin{document}\xff\\end{document}\n")
    client = app.test_client()
    r = client.post("/api/deck/deck-sections", json={"deck_path": str(bad)},
                    headers={"X-Requested-With": "ChatEKLD"})
    assert r.status_code == 400
    assert "UTF-8" in (r.get_json() or {}).get("error", "")


def test_deck_sections_requires_local_origin(tmp_path):
    from app import app
    deck, _ = _write_deck(tmp_path)
    client = app.test_client()
    r = client.post("/api/deck/deck-sections", json={"deck_path": str(deck)})
    assert r.status_code == 403


def test_augment_requires_instruction_and_deck(tmp_path):
    from app import app
    deck, _ = _write_deck(tmp_path)
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    r = client.post("/api/deck/augment", json={"deck_path": str(deck)}, headers=headers)
    assert r.status_code == 400
    r = client.post("/api/deck/augment", json={"instruction": "deepen"}, headers=headers)
    assert r.status_code == 400


def test_augment_invalid_operation_or_scope_400(tmp_path):
    from app import app
    deck, _ = _write_deck(tmp_path)
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    r = client.post("/api/deck/augment", json={
        "deck_path": str(deck), "instruction": "x", "operation": "nonsense",
    }, headers=headers)
    assert r.status_code == 400
    r = client.post("/api/deck/augment", json={
        "deck_path": str(deck), "instruction": "x", "scope": "nonsense",
    }, headers=headers)
    assert r.status_code == 400


def test_augment_section_scope_previews_and_stages(tmp_path):
    from app import app
    import api.routes.deck as deckmod
    deck, sha = _write_deck(tmp_path)
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    new_body = "\\section{Intro}\n\\begin{frame}{Intro}\n  \\begin{itemize}\\item deepened detail\\end{itemize}\n\\end{frame}"
    aug, _ = _preview_augment(client, headers, deck, new_body)
    assert aug is not None and aug["changed"] is True
    assert "deepened detail" in aug["proposed_tex"]
    assert "\\section{Methods}" in aug["proposed_tex"]   # untouched section preserved
    assert aug["deck_sha256"] == sha
    # Preview wrote nothing to the deck, but DID stage the proposal server-side.
    import hashlib
    assert hashlib.sha256(deck.read_bytes()).hexdigest() == sha
    stage = deckmod._load_stage(str(deck))
    assert stage and stage["base_sha256"] == sha and "deepened detail" in stage["proposed_tex"]


def test_augment_whole_scope_deepen(tmp_path):
    from app import app
    deck, _ = _write_deck(tmp_path)
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    # Whole-deck replace of the section region with two fresh sections.
    region = ("\\section{One}\n\\begin{frame}{One}\\end{frame}\n\n"
              "\\section{Two}\n\\begin{frame}{Two}\\end{frame}")
    aug, _ = _preview_augment(client, headers, deck, region, scope="whole")
    assert aug is not None and aug["changed"] is True
    tex = aug["proposed_tex"]
    assert "\\section{One}" in tex and "\\section{Two}" in tex
    assert "\\section{Intro}" not in tex and "\\section{Methods}" not in tex
    assert tex.startswith("\\documentclass")   # preamble/opening preserved
    assert "\\end{document}" in tex             # closing preserved


def test_augment_new_section_inserts(tmp_path):
    from app import app
    deck, _ = _write_deck(tmp_path)
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    new_body = "\\section{Prognosis}\n\\begin{frame}{Prognosis}\n  \\begin{itemize}\\item outcomes\\end{itemize}\n\\end{frame}"
    aug, _ = _preview_augment(client, headers, deck, new_body, operation="new_section", scope="whole")
    assert aug is not None and aug["changed"] is True
    tex = aug["proposed_tex"]
    assert "\\section{Prognosis}" in tex
    assert "\\section{Intro}" in tex and "\\section{Methods}" in tex
    assert tex.index("\\section{Prognosis}") > tex.index("\\section{Methods}")
    assert tex.index("\\section{Prognosis}") < tex.index("\\end{document}")


def test_augment_zero_section_deepen_errors(tmp_path):
    from app import app
    deck, _ = _write_deck(tmp_path, "nosec.tex",
                          "\\documentclass{beamer}\n\\begin{document}\n"
                          "\\begin{frame}{T}\\titlepage\\end{frame}\n\\end{document}\n")
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    aug, frames = _preview_augment(client, headers, deck, "\\section{X}\n\\begin{frame}{X}\\end{frame}",
                                   scope="section")
    assert aug is None
    assert any("no \\section" in f.get("error", "") for f in frames if "error" in f)


def test_augment_section_index_out_of_range_errors(tmp_path):
    from app import app
    deck, _ = _write_deck(tmp_path)
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    aug, frames = _preview_augment(client, headers, deck, "\\section{X}\n\\begin{frame}{X}\\end{frame}",
                                   scope="section", section_index=9)
    assert aug is None
    assert any("out of range" in f.get("error", "") for f in frames if "error" in f)


def test_augment_truncation_refused_for_replace(tmp_path):
    from app import app
    # A section region larger than AUGMENT_MAX_SOURCE_CHARS; a whole-deck deepen
    # must refuse rather than overwrite the whole region from a clipped view.
    big_section = "\\section{Big}\n\\begin{frame}{B}\n" + ("word " * 9000) + "\n\\end{frame}\n"
    deck_text = (
        "\\documentclass{beamer}\n\\begin{document}\n"
        "\\begin{frame}{Outline}\\tableofcontents\\end{frame}\n\n"
        + big_section +
        "\\end{document}\n"
    )
    deck, sha = _write_deck(tmp_path, "big.tex", deck_text)
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    aug, frames = _preview_augment(client, headers, deck, "\\section{New}\n\\begin{frame}{N}\\end{frame}",
                                   scope="whole", operation="deepen")
    assert aug is None
    assert any("too large" in f.get("error", "") for f in frames if "error" in f)
    import hashlib
    assert hashlib.sha256(deck.read_bytes()).hexdigest() == sha   # untouched


def test_augment_warnings_deduped(tmp_path):
    from app import app
    deck, _ = _write_deck(tmp_path)
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    # An unbalanced-frame body: validate() (whole doc) AND screen_repair's internal
    # validate() both flag it; the route must report it ONCE, not twice.
    bad = "\\section{Intro}\n\\begin{frame}{Intro}\nunclosed\n\\begin{frame}{Extra}\\end{frame}"
    aug, _ = _preview_augment(client, headers, deck, bad)
    assert aug is not None
    frame_warnings = [w for w in aug["warnings"] if "Unbalanced frame" in w]
    assert len(frame_warnings) == 1


def test_augment_counts_flag_frame_drop(tmp_path):
    from app import app
    # Intro has TWO frames; a deepen returning ONE frame drops the count.
    deck_text = (
        "\\documentclass{beamer}\n\\begin{document}\n"
        "\\begin{frame}{Outline}\\tableofcontents\\end{frame}\n\n"
        "\\section{Intro}\n\\begin{frame}{A}\\end{frame}\n\\begin{frame}{B}\\end{frame}\n\n"
        "\\end{document}\n"
    )
    deck, _ = _write_deck(tmp_path, "two.tex", deck_text)
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    aug, _ = _preview_augment(client, headers, deck,
                              "\\section{Intro}\n\\begin{frame}{A}\\end{frame}", scope="section")
    assert aug is not None and aug["changed"] is True
    assert aug["counts"]["frames_after"] < aug["counts"]["frames_before"]


def test_augment_no_usable_content_errors(tmp_path):
    from app import app
    deck, _ = _write_deck(tmp_path)
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    aug, frames = _preview_augment(client, headers, deck, "just prose, no beamer")
    assert aug is None
    assert any("error" in f for f in frames)


def test_augment_busy_lock_returns_409(tmp_path):
    from app import app
    import api.routes.deck as deckmod
    deck, _ = _write_deck(tmp_path)
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    assert deckmod._DECK_OP_LOCK.acquire(blocking=False)
    try:
        r = client.post("/api/deck/augment", json={
            "deck_path": str(deck), "instruction": "deepen", "citations_enabled": False,
        }, headers=headers)
        assert r.status_code == 409
    finally:
        deckmod._DECK_OP_LOCK.release()


# --- apply-augment (staging-based) ------------------------------------------

def test_apply_augment_writes_from_staging_with_backup(tmp_path):
    from app import app
    deck, sha = _write_deck(tmp_path)
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    new_body = "\\section{Intro}\n\\begin{frame}{Intro}\n  \\begin{itemize}\\item deepened\\end{itemize}\n\\end{frame}"
    aug, _ = _preview_augment(client, headers, deck, new_body)
    assert aug["changed"]

    r = client.post("/api/deck/apply-augment", json={
        "deck_path": str(deck), "base_sha256": aug["deck_sha256"], "confirm": True,
    }, headers=headers)
    assert r.status_code == 200, r.get_json()
    written = deck.read_text(encoding="utf-8")
    assert "deepened" in written
    # Backup holds the ORIGINAL content.
    bak = deck.parent / "deck.tex.bak"
    assert bak.exists() and "what it is" in bak.read_text(encoding="utf-8")


def test_apply_augment_requires_confirm_and_base(tmp_path):
    from app import app
    deck, sha = _write_deck(tmp_path)
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    r = client.post("/api/deck/apply-augment", json={"deck_path": str(deck), "base_sha256": sha}, headers=headers)
    assert r.status_code == 400  # no confirm
    r = client.post("/api/deck/apply-augment", json={"deck_path": str(deck), "confirm": True}, headers=headers)
    assert r.status_code == 400  # no base_sha256


def test_apply_augment_no_stage_is_400(tmp_path):
    from app import app
    deck, sha = _write_deck(tmp_path)
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    r = client.post("/api/deck/apply-augment", json={
        "deck_path": str(deck), "base_sha256": sha, "confirm": True,
    }, headers=headers)
    assert r.status_code == 400
    assert "Preview" in (r.get_json() or {}).get("error", "")


def test_apply_augment_refuses_stale_deck(tmp_path):
    from app import app
    deck, sha = _write_deck(tmp_path)
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    aug, _ = _preview_augment(client, headers, deck,
                              "\\section{Intro}\n\\begin{frame}{Intro}\\item deepened\\end{frame}")
    # Change the deck on disk after staging.
    deck.write_text(_AUG_DECK.replace("Demo", "Edited"), encoding="utf-8")
    r = client.post("/api/deck/apply-augment", json={
        "deck_path": str(deck), "base_sha256": aug["deck_sha256"], "confirm": True,
    }, headers=headers)
    assert r.status_code == 409


def test_apply_augment_rescreens_tampered_stage(tmp_path):
    """Defence in depth: if the staged proposal is tampered to carry a dangerous
    macro (bypassing the preview screen), apply re-screens and refuses."""
    from app import app
    import api.routes.deck as deckmod
    deck, sha = _write_deck(tmp_path)
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    evil = _AUG_DECK.replace("how we did it", "\\write18{rm -rf /}")
    deckmod._stage_augment(str(deck), sha, evil)   # bypass the preview screen
    r = client.post("/api/deck/apply-augment", json={
        "deck_path": str(deck), "base_sha256": sha, "confirm": True,
    }, headers=headers)
    assert r.status_code == 400
    assert "write18" not in deck.read_text(encoding="utf-8")


# --- apply-repair regression for the strict-read (UTF-8 / sha) change --------

def test_apply_repair_non_utf8_on_disk_is_400_not_500(tmp_path):
    from app import app
    import hashlib
    from deckgen.scaffold import scaffold_deck
    scaffold_deck(str(tmp_path), "rdeck", _DOC_OK)
    written = tmp_path / "rdeck" / "rdeck.tex"
    written.write_bytes(b"\\begin{document}\xff\\end{document}\n")   # corrupt to non-UTF-8
    base = hashlib.sha256(written.read_bytes()).hexdigest()
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    r = client.post("/api/deck/apply-repair", json={
        "out_dir": str(tmp_path), "deck_name": "rdeck",
        "tex": _DOC_OK.replace("body", "x"), "base_sha256": base, "confirm": True,
    }, headers=headers)
    assert r.status_code == 400
    assert "UTF-8" in (r.get_json() or {}).get("error", "")


# ---------------------------------------------------------------------------
# _resolve_and_copy_deck_figures — the vault-figure pipeline must never break
# a deck: no vault => untouched; deck-local/suite figures survive; vault
# figures are copied in; everything skipped is surfaced as a warning.


def _run_fig_resolver(tex, project_dir, vault_path):
    from unittest import mock
    import api.routes.deck as deckmod
    with mock.patch.object(
        deckmod.obsidian_manager, "get_vault_path", return_value=vault_path
    ):
        # Item 2.9 widened the return to a 4-tuple (…, pending_copies); the
        # legacy assertions here only care about the first three.
        out, resolved, warnings, _pending = deckmod._resolve_and_copy_deck_figures(
            tex, str(project_dir))
        return out, resolved, warnings


def test_fig_resolver_no_vault_is_a_noop(tmp_path):
    tex = "\\includegraphics{anything.png}\n\\end{frame}"
    out, resolved, warnings = _run_fig_resolver(tex, tmp_path, "")
    # Pre-fix behaviour commented out EVERY figure when no vault was
    # configured — destroying hand-written decks. Now: byte-identical.
    assert out == tex
    assert resolved == set()
    assert warnings == []


def test_fig_resolver_keeps_deck_local_figures(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    project = tmp_path / "deck"
    (project / "figures").mkdir(parents=True)
    (project / "figures" / "local.png").write_bytes(b"png")
    tex = (
        "\\includegraphics{figures/local.png}\n"
        "\\includegraphics{invented.png}"
    )
    out, resolved, warnings = _run_fig_resolver(tex, project, str(vault))
    assert "% \\includegraphics{figures/local.png}" not in out
    assert "local.png" in resolved
    # The invented figure is commented out AND surfaced as a warning.
    assert "% \\includegraphics{invented.png}" in out
    assert any("invented.png" in w for w in warnings)


def test_fig_resolver_copies_from_vault(tmp_path):
    vault = tmp_path / "vault"
    (vault / "attachments").mkdir(parents=True)
    (vault / "attachments" / "brain.png").write_bytes(b"png-bytes")
    project = tmp_path / "deck"
    project.mkdir()
    tex = "\\includegraphics[width=0.8\\textwidth]{figures/brain.png}"
    out, resolved, warnings = _run_fig_resolver(tex, project, str(vault))
    assert out == tex  # resolved => untouched
    assert "brain.png" in resolved
    assert (project / "figures" / "brain.png").read_bytes() == b"png-bytes"
    assert warnings == []


def test_fig_resolver_ambiguous_basename_is_kept_with_warning(tmp_path):
    vault = tmp_path / "vault"
    (vault / "a").mkdir(parents=True)
    (vault / "b").mkdir(parents=True)
    (vault / "a" / "dup.png").write_bytes(b"a")
    (vault / "b" / "dup.png").write_bytes(b"b")
    project = tmp_path / "deck"
    project.mkdir()
    tex = "\\includegraphics{dup.png}"
    out, resolved, warnings = _run_fig_resolver(tex, project, str(vault))
    # Guessing between same-named vault images could silently embed the wrong
    # figure — keep the command, copy nothing, tell the user.
    assert out == tex
    assert "dup.png" in resolved
    assert not (project / "figures" / "dup.png").exists()
    assert any("dup.png" in w and "same-named" in w for w in warnings)


def test_fig_resolver_written_path_disambiguates(tmp_path):
    vault = tmp_path / "vault"
    (vault / "a").mkdir(parents=True)
    (vault / "b").mkdir(parents=True)
    (vault / "a" / "dup.png").write_bytes(b"right")
    (vault / "b" / "dup.png").write_bytes(b"wrong")
    project = tmp_path / "deck"
    project.mkdir()
    tex = "\\includegraphics{a/dup.png}"
    out, resolved, warnings = _run_fig_resolver(tex, project, str(vault))
    assert "dup.png" in resolved
    assert (project / "figures" / "dup.png").read_bytes() == b"right"
    assert warnings == []


# --------------------------------------------------------------------------- #
# item 2.2 — deck-op lock lifecycle (_DeckOpGuard + _run_deck_sse wiring)
# --------------------------------------------------------------------------- #
class TestDeckOpGuard:
    """Pinning tests for the joint lock-release ownership (improvement plan
    2026-07-04, item 2.2; promoted to the shared ``api.sse`` skeleton by 4.3 —
    the guard is now ``_SSEOpGuard`` and the runner ``run_sse_worker``, used
    by every streaming route, deck included).

    Defects pinned: (a) a Response whose generator is never iterated used to
    skip the generator-finally release entirely — every later deck op 409'd
    for the process lifetime; (b) a consumer stall/disconnect released the
    lock while the worker thread was still writing (zombie-writer race).
    Invariant: the deck-op lock is released exactly once, and only when the
    stream has ended AND any spawned worker has exited.
    """

    def _guard(self):
        from api.sse import _SSEOpGuard
        calls = []
        return _SSEOpGuard(lambda: calls.append(1)), calls

    def test_stream_end_alone_releases_when_no_worker_spawned(self):
        g, calls = self._guard()
        g.stream_finished()
        assert calls == [1]

    def test_worker_still_running_blocks_release_until_it_exits(self):
        # The zombie-writer scenario: stream dies first (stall/disconnect),
        # worker finishes later — the lock must be held for the whole gap.
        g, calls = self._guard()
        g.worker_spawned()
        g.stream_finished()
        assert calls == []          # worker alive ⇒ lock still held
        g.worker_finished()
        assert calls == [1]

    def test_release_fires_once_despite_duplicate_signals(self):
        # stream_finished arrives from BOTH the generator finally and
        # call_on_close — double release would raise on a threading.Lock.
        g, calls = self._guard()
        g.worker_spawned()
        g.worker_finished()
        g.stream_finished()
        g.stream_finished()
        g.stream_finished()
        assert calls == [1]

    def test_never_iterated_response_still_releases(self):
        # (a): close the Response WITHOUT pulling a single chunk. A closed,
        # never-started generator skips its body — call_on_close must cover it.
        from api import sse as ssemod
        calls = []

        def worker(put, cancel):  # would only run if the generator started
            put({"info": "x"})

        resp = ssemod.run_sse_worker(
            worker, consumer_timeout_s=5, preflight_msgs=[],
            release=lambda: calls.append(1))
        resp.close()
        assert calls == [1]

    def test_disconnect_mid_stream_defers_release_to_worker_exit(self):
        # (b) end-to-end: consume one frame, then close the response while the
        # worker is still alive. The release must NOT fire until the worker
        # exits its finally.
        import threading
        import time
        from api import sse as ssemod

        calls = []
        started = threading.Event()
        proceed = threading.Event()

        def worker(put, cancel):
            put({"info": "first"})
            started.set()
            proceed.wait(timeout=10)   # the test holds the worker "mid-write"

        resp = ssemod.run_sse_worker(
            worker, consumer_timeout_s=10, preflight_msgs=[],
            release=lambda: calls.append(1))
        it = iter(resp.response)
        next(it)                        # first frame → generator started
        assert started.wait(timeout=5)
        resp.close()                    # client disconnect
        assert calls == []              # worker still alive ⇒ lock held
        proceed.set()                   # worker exits
        deadline = time.monotonic() + 5
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls == [1]


# --------------------------------------------------------------------------- #
# item 2.9 — write-path parity (backup, preview writes nothing, sanitised errors)
# --------------------------------------------------------------------------- #
def test_apply_repair_writes_backup_of_original(tmp_path):
    # Parity with apply-augment/compile-fix: the one deck-overwriting path
    # with no .bak now leaves one, holding the ORIGINAL bytes.
    from app import app

    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    written, _makefile, base = _scaffold_existing(tmp_path)
    original = written.read_bytes()

    r = client.post("/api/deck/apply-repair", json={
        "out_dir": str(tmp_path), "deck_name": "My Deck",
        "tex": _DOC_OK.replace("body", "fixed body"),
        "base_sha256": base, "confirm": True,
    }, headers=headers)
    assert r.status_code == 200, r.get_json()
    bak = written.with_suffix(".tex.bak")
    assert bak.exists()
    assert bak.read_bytes() == original          # recovery copy = pre-repair deck
    assert r.get_json()["backup_path"].endswith(".tex.bak")


def test_fig_resolver_preview_mode_writes_nothing(tmp_path):
    # copy=False (the augment preview): same decisions and tex rewrite, but
    # the copy is DEFERRED — returned as pending, no file created.
    from unittest import mock
    import api.routes.deck as deckmod

    vault = tmp_path / "vault"
    (vault / "attachments").mkdir(parents=True)
    (vault / "attachments" / "brain.png").write_bytes(b"\x89PNG fake")
    project = tmp_path / "deck"
    project.mkdir()
    tex = "\\includegraphics{brain.png}"

    with mock.patch.object(deckmod.obsidian_manager, "get_vault_path",
                           return_value=str(vault)):
        out, resolved, warnings, pending = deckmod._resolve_and_copy_deck_figures(
            tex, str(project), copy=False)

    assert "brain.png" in resolved
    assert out == tex                                # kept, not commented out
    assert not (project / "figures").exists()        # NOTHING written
    assert len(pending) == 1
    src, dst = pending[0]
    assert src.endswith("brain.png") and dst.endswith(os.path.join("figures", "brain.png"))


def test_compile_repair_error_is_sanitised():
    # A provider exception can embed the request URL / API key; the compile
    # loop surfaces payload['error'] to the UI, so it must pass through
    # sanitise_error_msg like the integrity-review sibling.
    import threading
    from unittest import mock
    import api.routes.deck as deckmod

    secret = "sk-proj-SECRETSECRETSECRET123456"

    def boom(**_kw):
        raise RuntimeError(f"401 unauthorized for key {secret}")

    with mock.patch.object(deckmod, "stream_chat_messages", side_effect=boom):
        payload = deckmod._run_compile_repair(
            "\\documentclass{beamer}", ["err"], provider="openai", model="m",
            max_tokens=256, cfg={}, cancel=threading.Event())
    assert payload["error"]
    assert secret not in payload["error"]


def test_audit_mapping_concurrent_writers_lose_nothing(tmp_path):
    # Item 2.9: mapping.json is hand-curated and unregenerable; the
    # load->mutate->save writers now serialise on a module mutex, so
    # concurrent adds cannot interleave the RMW and drop entries.
    import threading
    from audit.engine import bridge

    mapping_file = tmp_path / "mapping.json"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    pdfs = []
    for i in range(10):
        p = vault_root / f"paper{i}.pdf"
        p.write_bytes(b"%PDF")
        pdfs.append(p)

    threads = [
        threading.Thread(
            target=bridge.add_match,
            args=(mapping_file, pdfs[i], f"key{i}", vault_root))
        for i in range(10)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    matches, _no_match = bridge._load_mapping(mapping_file)
    assert len(matches) == 10                    # nothing lost to a torn RMW
