"""Deck Generator window — turn the vault into a Beamer ``.tex`` deck.

This blueprint is the in-app face of ``deckgen``: it loads a user-supplied
Beamer template (which the user may edit in the app before generating), drives
the deckgen orchestration in-process via
:class:`deckgen.inprocess.InProcessChatRunner`, and scaffolds the result into a
``<slug>/`` project folder under the user's LaTeX suite (emit-only — the user
compiles with ``make``).

Streaming uses the same SSE contract as ``/api/obsidian/chat`` (``info`` /
``error`` / agent-trace frames), plus a terminal ``{"deck": {...}}`` frame that
carries the assembled ``.tex``, validation warnings and the written paths.
"""
import json
import os
import queue
import threading

from flask import Blueprint, Response, jsonify, request

from api.security import origin_is_local, sanitise_error_msg
from api.validators import (
    coerce_bool,
    coerce_float_in_range,
    coerce_int_in_range,
    coerce_non_empty_string,
    coerce_string_max_len,
)
from core.config import load_config, resolve_chat_model
from core.constants import DEFAULT_EMBED
from rag.vault import obsidian_manager

from deckgen.assemble import assemble_with_template, validate
from deckgen.inprocess import InProcessChatRunner
from deckgen.outline import OutlineError, request_outline
from deckgen.scaffold import ScaffoldError, scaffold_deck, slugify
from deckgen.sections import generate_section
from deckgen.template import (
    TemplateError,
    bib_candidates_block,
    find_suite_root,
    load_template_parts,
    macro_cheatsheet,
    relevant_bib_keys,
)

deck_bp = Blueprint("deck", __name__)

# Index states in which a usable index exists on disk (mirror deckgen CLI).
_USABLE_STATES = {"done", "paused_partial"}
_IN_PROGRESS_STATES = {"running", "scanning", "embedding", "paused", "paused_scan"}

# Input caps.
_TEMPLATE_MAX_BYTES = 1_000_000          # a template/preamble the user may edit
_TOPIC_MAX = 500
_INSTRUCTIONS_MAX = 8_000
_AUDIENCE_MAX = 200
_DECK_NAME_MAX = 120
_PATH_MAX = 4096
_TEMPLATE_EXTS = (".tex", ".sty")

_MAX_SECTIONS_MIN, _MAX_SECTIONS_MAX = 1, 20
_AGENT_ITER_MIN, _AGENT_ITER_MAX = 1, 12
_TEMP_MIN, _TEMP_MAX = 0.0, 2.0

# Absolute prefixes we refuse to scaffold into (system locations). Kept narrow:
# the vault, the user's home, and temp dirs (under /private/var on macOS) must
# all remain valid targets. Defence-in-depth only — the slug can't escape and we
# only ever create a new subdir + two files, never delete.
_DENY_PREFIXES = (
    "/System", "/usr", "/bin", "/sbin", "/etc", "/private/etc",
)

_CHAT_TOKEN_TIMEOUT_S = 300  # default per-turn wall-clock cap (config: agent_wall_clock_s)
# Consumer waits this much longer than the effective stall base so the worker's
# own structured timeout/error fires first (mirrors api/routes/vault.py).
_STALL_MARGIN_S = 30
# Floor for the consumer's per-event wait — the no-events-at-all backstop never
# drops below this even if the per-turn cap is lowered. Each deck turn is still
# bounded by turn_timeout_s inside the agent loop; this only guards total event
# silence (e.g. a hung local call when local_request_timeout_s is 0). Mirrors
# api/routes/vault.py so both SSE routes share one stall model.
_SINGLE_SHOT_FLOOR_S = 300


def _resolve_template_path(raw) -> str | None:
    """Coerce/validate a template path: absolute, existing ``.tex``/``.sty`` file."""
    s = coerce_string_max_len(raw, _PATH_MAX)
    if not s:
        return None
    if any(ch in s for ch in ("\x00", "\n", "\r")):
        return None
    try:
        expanded = os.path.realpath(os.path.expanduser(s))
    except (OSError, ValueError):
        return None
    if not os.path.isabs(expanded) or not os.path.isfile(expanded):
        return None
    if os.path.splitext(expanded)[1].lower() not in _TEMPLATE_EXTS:
        return None
    return expanded


def _resolve_out_dir(raw) -> str | None:
    """Coerce/validate an output dir: absolute, existing, not a system location."""
    s = coerce_string_max_len(raw, _PATH_MAX)
    if not s:
        return None
    if any(ch in s for ch in ("\x00", "\n", "\r")):
        return None
    try:
        expanded = os.path.realpath(os.path.expanduser(s))
    except (OSError, ValueError):
        return None
    if not os.path.isabs(expanded) or not os.path.isdir(expanded):
        return None
    if expanded == os.sep or any(
        expanded == p or expanded.startswith(p + os.sep) for p in _DENY_PREFIXES
    ):
        return None
    return expanded


def _read_template_file(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read(_TEMPLATE_MAX_BYTES)


@deck_bp.route("/api/deck/load-template", methods=["POST"])
def api_deck_load_template():
    """Read a template file and report its preamble-derived macros + bib."""
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json(silent=True) or {}
    path = _resolve_template_path(data.get("path"))
    if not path:
        return jsonify({"error": "Invalid or unreadable template path (.tex/.sty)."}), 400
    try:
        tex = _read_template_file(path)
    except OSError as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 400
    try:
        parts = load_template_parts(tex, path)
    except TemplateError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({
        "ok": True,
        "tex": tex,
        "path": path,
        "suite_root": find_suite_root(path) or "",
        "macros": [
            {"signature": m.signature(), "name": m.name, "description": m.description}
            for m in parts.macros
        ],
        "bib_keys_count": len(parts.bib_index),
        "opening_present": bool(parts.opening.strip()),
        "closing_present": bool(parts.closing.strip()),
    })


@deck_bp.route("/api/deck/generate", methods=["POST"])
def api_deck_generate():
    """Generate a deck from the (edited) template and stream progress as SSE."""
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json(silent=True) or {}

    topic = coerce_non_empty_string(data.get("topic"), _TOPIC_MAX)
    if not topic:
        return jsonify({"error": "A topic is required."}), 400
    template_tex = coerce_string_max_len(data.get("template_tex"), _TEMPLATE_MAX_BYTES)
    if not template_tex or not template_tex.strip():
        return jsonify({"error": "A Beamer template is required."}), 400

    instructions = coerce_string_max_len(data.get("instructions"), _INSTRUCTIONS_MAX) or ""
    audience = coerce_string_max_len(data.get("audience"), _AUDIENCE_MAX) or "the audience"

    # Template path is used only to resolve \usepackage{../common/...} and
    # \addbibresource{../_master.bib} and to infer the suite root; the *content*
    # used for generation is the (possibly edited) template_tex from the editor.
    template_path = _resolve_template_path(data.get("template_path")) or ""

    out_dir = _resolve_out_dir(data.get("out_dir"))
    if not out_dir:
        suite_root = find_suite_root(template_path) if template_path else None
        out_dir = _resolve_out_dir(suite_root) if suite_root else None
    if not out_dir:
        return jsonify({
            "error": "Could not determine an output folder. Choose one (it should be "
                     "the suite root that contains your common/ folder)."
        }), 400

    deck_name = coerce_string_max_len(data.get("deck_name"), _DECK_NAME_MAX) or topic
    slug = slugify(deck_name)
    overwrite = bool(coerce_bool(data.get("overwrite")) or False)

    cfg = load_config()
    # Generation knobs: request body wins, then the persisted ``deck_*``
    # defaults (set in the Settings window), then the hard-coded fallback.
    max_sections = (
        coerce_int_in_range(data.get("max_sections"), _MAX_SECTIONS_MIN, _MAX_SECTIONS_MAX)
        or coerce_int_in_range(cfg.get("deck_max_sections"), _MAX_SECTIONS_MIN, _MAX_SECTIONS_MAX)
        or 8
    )
    agent_iters = (
        coerce_int_in_range(data.get("agent_max_iterations"), _AGENT_ITER_MIN, _AGENT_ITER_MAX)
        or coerce_int_in_range(cfg.get("deck_agent_max_iterations"), _AGENT_ITER_MIN, _AGENT_ITER_MAX)
        or 6
    )
    # Temperature may legitimately be 0.0 (falsy), so resolve with an explicit
    # None check rather than ``or``.  When omitted everywhere it stays None and
    # the in-process runner falls back to vault_chat_temperature.
    temperature = coerce_float_in_range(data.get("temperature"), _TEMP_MIN, _TEMP_MAX)
    if temperature is None:
        temperature = coerce_float_in_range(cfg.get("deck_temperature"), _TEMP_MIN, _TEMP_MAX)
    citations_enabled = bool(coerce_bool(data.get("citations_enabled")))
    if data.get("citations_enabled") is None:
        citations_enabled = True  # default on

    provider = coerce_string_max_len(data.get("provider"), 40) or cfg.get("provider", "ollama")
    model = coerce_string_max_len(data.get("model"), 120) or resolve_chat_model(cfg, provider)
    embed = coerce_string_max_len(data.get("embed"), 120) or cfg.get("embed", DEFAULT_EMBED)
    # Defensive resolve: coerce_int_in_range rejects NaN/Inf/strings and clamps,
    # so a hand-edited config.json can't crash the route or yield a bad deadline.
    wall_clock_s = coerce_int_in_range(cfg.get("agent_wall_clock_s"), 30, 1800) or _CHAT_TOKEN_TIMEOUT_S
    # Floored stall backstop (see _SINGLE_SHOT_FLOOR_S); each turn is still
    # bounded by turn_timeout_s=wall_clock_s inside the in-process runner.
    consumer_timeout_s = max(wall_clock_s, _SINGLE_SHOT_FLOOR_S) + _STALL_MARGIN_S

    def generate():
        cancel = threading.Event()
        event_q: queue.Queue = queue.Queue(maxsize=1024)
        _DONE = object()

        def _put(item):
            while not cancel.is_set():
                try:
                    event_q.put(item, timeout=1)
                    return
                except queue.Full:
                    continue

        def _info(text):
            _put({"info": text})

        def _on_event(evt: dict):
            # deckgen passes through agent-trace + info/error dicts already in
            # SSE shape; forward verbatim.
            _put(evt)

        def _worker():
            try:
                # 1) Parse the (edited) template.
                try:
                    parts = load_template_parts(template_tex, template_path)
                except TemplateError as exc:
                    _put({"error": str(exc)})
                    return

                macros_block = macro_cheatsheet(parts.macros)
                use_bib = citations_enabled and bool(parts.bib_index)
                cite_mode = "bib" if use_bib else "prose"

                runner = InProcessChatRunner(cfg=cfg, cancel_event=cancel, turn_timeout_s=wall_clock_s)

                # 2) Outline.
                _info(f"Designing outline for “{topic}”…")
                try:
                    sections, _ = request_outline(
                        runner,
                        topic=topic,
                        instructions=instructions,
                        provider=provider,
                        model=model,
                        embed=embed,
                        max_iters=agent_iters,
                        temperature=temperature,
                        max_sections=max_sections,
                        on_event=_on_event,
                    )
                except OutlineError as exc:
                    _put({"error": str(exc)})
                    return
                if cancel.is_set():
                    return
                _info(f"Outline ready: {len(sections)} section(s).")

                # 3) Per-section generation.
                section_outputs = []
                for i, sec in enumerate(sections, start=1):
                    if cancel.is_set():
                        return
                    _info(f"Writing section {i}/{len(sections)}: {sec.title}")
                    candidate_block = ""
                    if use_bib:
                        seed = sec.title + " " + " ".join(sec.points)
                        candidate_block = bib_candidates_block(
                            parts.bib_index, relevant_bib_keys(parts.bib_index, seed)
                        )
                    out = generate_section(
                        runner,
                        index=i,
                        section=sec,
                        full_outline=sections,
                        topic=topic,
                        instructions=instructions,
                        audience=audience,
                        provider=provider,
                        model=model,
                        embed=embed,
                        max_iters=agent_iters,
                        temperature=temperature,
                        macros_block=macros_block,
                        cite_mode=cite_mode,
                        candidate_bib_block=candidate_block,
                        on_event=_on_event,
                    )
                    section_outputs.append(out)

                # 4) Assemble + validate.
                tex = assemble_with_template(
                    section_outputs,
                    preamble=parts.preamble,
                    opening=parts.opening,
                    closing=parts.closing,
                )
                generated_span = "\n\n".join(
                    s.body.strip() for s in section_outputs if s.body.strip()
                )
                warnings = validate(
                    tex,
                    generated_tex=generated_span,
                    known_bib_keys=parts.bib_keys if use_bib else None,
                )

                n_total = len(section_outputs)
                n_placeholder = sum(1 for s in section_outputs if s.placeholder)

                # 5) Scaffold into <out_dir>/<slug>/.
                scaffold_error = None
                paths = {}
                try:
                    paths = scaffold_deck(out_dir, slug, tex, overwrite=overwrite)
                except ScaffoldError as exc:
                    scaffold_error = str(exc)

                if paths and not paths.get("sibling_common", False):
                    warnings = list(warnings) + [
                        "The output folder has no sibling common/ directory — "
                        "\\usepackage{../common/cress-style} and ../_master.bib will "
                        "not resolve. Place the deck under your LaTeX suite root."
                    ]

                _put({"deck": {
                    "tex": tex,
                    "warnings": warnings,
                    "section_count": n_total,
                    "placeholder_count": n_placeholder,
                    "slug": slug,
                    "out_dir": out_dir,
                    "project_dir": paths.get("project_dir", ""),
                    "tex_path": paths.get("tex_path", ""),
                    "makefile_path": paths.get("makefile_path", ""),
                    "make_hint": (
                        f"cd {paths.get('project_dir', '')} && make view"
                        if paths.get("project_dir") else ""
                    ),
                    "scaffold_error": scaffold_error,
                }})
            except Exception as exc:  # noqa: BLE001 — surface anything as an SSE error
                if not cancel.is_set():
                    _put({"error": sanitise_error_msg(exc)})
            finally:
                try:
                    event_q.put(_DONE, timeout=5)
                except queue.Full:
                    pass

        # Preflight: warn (not block) when no usable index exists.
        try:
            state = obsidian_manager.get_status()
        except Exception:
            state = ""
        if state in _IN_PROGRESS_STATES:
            yield f"data: {json.dumps({'info': 'Indexing is still in progress — the deck may miss recently-added content.'})}\n\n"
        elif state not in _USABLE_STATES:
            yield f"data: {json.dumps({'info': 'No fully-built vault index detected — generation may return little content. Index your vault first for best results.'})}\n\n"

        threading.Thread(target=_worker, daemon=True).start()
        # The finally guarantees `cancel` is set on EVERY exit from the drain
        # loop — normal completion, an error/timeout break, or a GeneratorExit
        # when the client disconnects mid-stream. Without it, a disconnect would
        # leave the worker blocked forever in a full-queue `_put` (thread leak),
        # since GeneratorExit alone does not signal the worker.
        try:
            while True:
                try:
                    item = event_q.get(timeout=consumer_timeout_s)
                except queue.Empty:
                    cancel.set()
                    yield f"data: {json.dumps({'error': 'Deck generation timed out — the model may be overloaded. Please try again.'})}\n\n"
                    break
                if item is _DONE:
                    break
                yield f"data: {json.dumps(item)}\n\n"
                if isinstance(item, dict) and item.get("error"):
                    cancel.set()
                    break
        finally:
            cancel.set()
        yield "data: [DONE]\n\n"

    return Response(generate(), mimetype="text/event-stream")


@deck_bp.route("/api/deck/native-pick-file", methods=["POST"])
def api_deck_native_pick_file():
    """Native file picker for choosing a template ``.tex``/``.sty``."""
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    import webview
    if not webview.windows:
        return jsonify({"error": "No active window"}), 503
    result = webview.windows[0].create_file_dialog(
        webview.OPEN_DIALOG,
        allow_multiple=False,
        file_types=("LaTeX (*.tex;*.sty)", "All files (*.*)"),
    )
    if not result:
        return jsonify({"cancelled": True})
    chosen = result[0] if isinstance(result, (list, tuple)) else result
    if not os.path.isfile(chosen):
        return jsonify({"error": "Selected path is not a file"}), 400
    return jsonify({"ok": True, "path": chosen})


@deck_bp.route("/api/deck/native-pick-folder", methods=["POST"])
def api_deck_native_pick_folder():
    """Native folder picker for choosing the output (suite root) directory."""
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    import webview
    if not webview.windows:
        return jsonify({"error": "No active window"}), 503
    result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG, allow_multiple=False)
    if not result:
        return jsonify({"cancelled": True})
    chosen = result[0] if isinstance(result, (list, tuple)) else result
    if not os.path.isdir(chosen):
        return jsonify({"error": "Selected path is not a directory"}), 400
    return jsonify({"ok": True, "path": chosen})
