"""Obsidian vault blueprint: indexing control + the vault-chat SSE route.

This module owns the **canonical** streaming-chat pattern the rest of the app
mirrors (``api/routes/plainchat.py`` is a stripped copy, ``api/routes/deck.py``
reuses the same stall model). The shape, repeated in every SSE route:

    request thread (the Flask generator) ── consumes ──►  event_q  ◄── produces ── daemon worker thread

* A **daemon worker** (``_run_chat`` for single-shot RAG, ``_run_agent`` for the
  ReAct loop) runs the slow generation off the request thread and ``_put``s
  ``{token}`` / ``{info}`` / ``{error}`` / agent-trace dicts onto a **bounded**
  ``queue.Queue`` (``maxsize=512`` → back-pressure) plus a ``_DONE`` sentinel in
  its ``finally``.
* The **consumer** (the ``generate`` generator) ``get``s with a stall timeout and
  re-emits each item as an SSE ``data:`` frame, ending with ``[DONE]``.
* A shared ``threading.Event`` (``cancel``) is the only cross-thread signal: the
  consumer sets it on timeout / client disconnect / terminal error, and the worker
  checks it to stop pumping. Running generation on a separate thread is what lets
  the consumer enforce a wall-clock/stall bound the blocking model call cannot.

Indexing is guarded separately by the manager's ``RagOperationLock`` (TTL 3600 s):
``api_obsidian_index`` waits out any in-flight run, then admits exactly one indexer
thread. ``_resolve_chat_params`` resolves every live retrieval/generation knob
body→config→default so a Settings change applies on the next Send with no reload.
"""
import threading
import time
import os
from pathlib import Path
from flask import Blueprint, jsonify, request
from api.security import sanitise_error_msg
from api.sse import run_sse_worker
from core.config import load_config
from core.constants import (
    DEFAULT_EMBED,
    VAULT_SYSTEM_PROMPT_LIMIT,
    # Shared SSE stall-guard timing (single source of truth in core/constants.py;
    # aliased to the historical private names so the usage + comments below are
    # undisturbed). The full rationale lives at the definition site.
    SSE_STALL_MARGIN_S as _STALL_MARGIN_S,
    SSE_SINGLE_SHOT_FLOOR_S as _SINGLE_SHOT_FLOOR_S,
)
from api.validators import (
    MISSING,
    coerce_bool,
    coerce_enum,
    coerce_float_in_range,
    coerce_int_in_range,
    coerce_string_max_len,
    first_valid,
)
from rag.vault import obsidian_manager
from core.agent import (
    AgentCapabilityState,
    DoneEvent,
    ErrorEvent,
    InfoEvent,
    IterationEvent,
    ThoughtEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
    ToolRegistry,
    VaultToolContext,
    build_vault_tools,
    run_agent_loop,
)

# Per-session counter shared across vault chat requests. The agent loop
# updates it on each turn so the capability-warning info event fires
# at most once per session after two consecutive RAG fallbacks.
_agent_capability_state = AgentCapabilityState()

_CHAT_TOKEN_TIMEOUT_S = 300  # default agent wall-clock cap (config: agent_wall_clock_s)
# _STALL_MARGIN_S / _SINGLE_SHOT_FLOOR_S are imported from core/constants.py
# (SSE_STALL_MARGIN_S / SSE_SINGLE_SHOT_FLOOR_S) so the vault, deck, and
# plain-chat SSE routes share one definition; see the comment there for the
# full timeout-chain rationale. consumer_timeout_s below derives from them.
_EMPTY_RESPONSE_SENTINEL = "Empty Response"  # LlamaIndex default for no-context responses
_NO_CONTENT_MSG = "No relevant content found in your vault for this query."
_NO_AGENT_ANSWER_MSG = (
    "Agent did not produce a final answer — see the reasoning trace above for details."
)

_PROMPT_MODES = frozenset({"strict", "balanced", "exploratory", "concise"})
_TOP_K_MIN, _TOP_K_MAX = 1, 32
_CUTOFF_MIN, _CUTOFF_MAX = 0.0, 1.0
_TEMP_MIN, _TEMP_MAX = 0.0, 2.0
_AGENT_ITER_MIN, _AGENT_ITER_MAX = 1, 12
_MMR_MIN, _MMR_MAX = 0.0, 1.0
_NUM_QUERIES_MIN, _NUM_QUERIES_MAX = 1, 5
_RERANK_CEILING_MIN, _RERANK_CEILING_MAX = 10, 200


def _format_agent_usage(budget) -> str:
    """Render the per-turn :class:`~core.agent.budget.UsageBudget` as a
    short SSE info line. Cost is suppressed when zero (local models)
    so we don't show ``$0.0000`` on every Ollama turn.
    """
    parts = [
        f"Agent: {budget.iteration_count} iteration{'s' if budget.iteration_count != 1 else ''}",
        f"{budget.input_tokens} in / {budget.output_tokens} out tokens",
    ]
    if budget.estimated_cost_usd > 0.0:
        parts.append(f"${budget.estimated_cost_usd:.4f}")
    return " · ".join(parts)


def _agent_event_to_queue_item(event):
    """Convert an :class:`~core.agent.protocol.AgentEvent` into the same
    ``dict`` queue-item shape :func:`_run_chat` uses, so the downstream
    SSE consumer can pattern-match on a single set of keys.

    Tool-call / tool-result events serialise their nested dataclasses
    (``ToolCall``, ``ToolResult``) to plain dicts so ``json.dumps`` in
    the consumer doesn't choke. ``DoneEvent`` returns ``None`` because
    the worker's finally block emits the queue's ``_DONE`` sentinel
    separately.
    """
    if isinstance(event, IterationEvent):
        return {"iteration": event.index}
    if isinstance(event, ThoughtEvent):
        return {"thought": event.text}
    if isinstance(event, ToolCallEvent):
        return {"tool_call": {
            "id": event.call.id,
            "name": event.call.name,
            "arguments": event.call.arguments,
        }}
    if isinstance(event, ToolResultEvent):
        return {"tool_result": {
            "tool_call_id": event.result.tool_call_id,
            "content": event.result.content,
            "is_error": event.result.is_error,
            "truncated": event.truncated,
        }}
    if isinstance(event, TokenEvent):
        return {"token": event.text}
    if isinstance(event, InfoEvent):
        return {"info": event.text}
    if isinstance(event, ErrorEvent):
        return {"error": event.text}
    if isinstance(event, DoneEvent):
        return None
    return None


def _resolve_chat_params(data: dict, cfg: dict) -> dict:
    """Pull live retrieval/generation knobs from request body or config.

    The request body wins **when its value is valid**; an invalid body value
    falls back to the persisted config value, and only a missing-or-invalid
    config value falls back to the engine default (i.e. the key is omitted
    from the returned dict so ``stream_chat`` uses its own kwarg default).
    Returns a dict suitable for ``**kwargs`` into ``stream_chat``.
    """
    out: dict = {}

    def _resolve(body_key: str, cfg_key: str, coerce):
        return first_valid((
            (data.get(body_key, MISSING), coerce),
            (cfg.get(cfg_key, MISSING), coerce),
        ))

    top_k = _resolve(
        "top_k", "vault_top_k",
        lambda v: coerce_int_in_range(v, _TOP_K_MIN, _TOP_K_MAX),
    )
    if top_k is not MISSING:
        out["top_k"] = top_k
        out["top_k_explicit"] = True

    cutoff = _resolve(
        "similarity_cutoff", "vault_similarity_cutoff",
        lambda v: coerce_float_in_range(v, _CUTOFF_MIN, _CUTOFF_MAX),
    )
    if cutoff is not MISSING:
        out["similarity_cutoff"] = cutoff

    mode = _resolve(
        "prompt_mode", "vault_prompt_mode",
        lambda v: coerce_enum(v, _PROMPT_MODES),
    )
    if mode is not MISSING:
        out["prompt_mode"] = mode

    temp = _resolve(
        "temperature", "vault_chat_temperature",
        lambda v: coerce_float_in_range(v, _TEMP_MIN, _TEMP_MAX),
    )
    if temp is not MISSING:
        out["temperature"] = temp

    hybrid = _resolve(
        "hybrid_enabled", "vault_hybrid_enabled",
        coerce_bool,
    )
    if hybrid is not MISSING:
        out["hybrid_enabled"] = hybrid

    reranker = _resolve(
        "reranker_enabled", "vault_reranker_enabled",
        coerce_bool,
    )
    if reranker is not MISSING:
        out["reranker_enabled"] = reranker

    # MMR diversity (query-time): gated by mmr_enabled; the lambda is the
    # MMR threshold applied to the dense leg when enabled.
    mmr_enabled = _resolve(
        "mmr_enabled", "vault_mmr_enabled",
        coerce_bool,
    )
    if mmr_enabled is not MISSING:
        out["mmr_enabled"] = mmr_enabled

    mmr_lambda = _resolve(
        "mmr_lambda", "vault_mmr_lambda",
        lambda v: coerce_float_in_range(v, _MMR_MIN, _MMR_MAX),
    )
    if mmr_lambda is not MISSING:
        out["mmr_lambda"] = mmr_lambda

    query_expansion = _resolve(
        "query_expansion", "vault_query_expansion",
        coerce_bool,
    )
    if query_expansion is not MISSING:
        out["query_expansion"] = query_expansion

    num_queries = _resolve(
        "num_queries", "vault_num_queries",
        lambda v: coerce_int_in_range(v, _NUM_QUERIES_MIN, _NUM_QUERIES_MAX),
    )
    if num_queries is not MISSING:
        out["num_queries"] = num_queries

    # Reranker candidate-pool ceiling: a live per-request override (sent in
    # the chat body) so changing the slider takes effect on the same Send,
    # not after the debounced config save.
    rerank_pool_ceiling = _resolve(
        "rerank_pool_ceiling", "vault_rerank_pool_ceiling",
        lambda v: coerce_int_in_range(v, _RERANK_CEILING_MIN, _RERANK_CEILING_MAX),
    )
    if rerank_pool_ceiling is not MISSING:
        out["rerank_pool_ceiling"] = rerank_pool_ceiling

    # Wikilink graph expansion (query-time, no reindex): live per-Send toggle
    # so flipping it in the UI takes effect on the next message. The caps live
    # in config only (read by the engine), so they are not body overrides. The
    # agent's active vault_search is intentionally not threaded this knob —
    # parity with mmr_enabled / query_expansion, which are also single-shot
    # only; it still applies to the agent's RAG fallback via stream_chat.
    wikilink_expansion = _resolve(
        "wikilink_expansion", "vault_wikilink_expansion",
        coerce_bool,
    )
    if wikilink_expansion is not MISSING:
        out["wikilink_expansion"] = wikilink_expansion

    # Thesaurus query expansion (query-time, no reindex): live per-Send toggle
    # like wikilink. max_variants is config-only (read by the engine). Not
    # threaded to the agent's active vault_search — parity with wikilink /
    # mmr / query_expansion (single-shot only; still applies to the agent's RAG
    # fallback via stream_chat).
    thesaurus_expansion = _resolve(
        "thesaurus_expansion", "vault_thesaurus_expansion",
        coerce_bool,
    )
    if thesaurus_expansion is not MISSING:
        out["thesaurus_expansion"] = thesaurus_expansion

    # System-prompt primer (query-time, no reindex): live per-Send toggle. The
    # glossary budget (vault_primer_max_chars) is config-only (read by the
    # engine). Like expansion, it applies to single-shot RAG and the agent's
    # RAG fallback via stream_chat, not the agent's active vault_search.
    primer_enabled = _resolve(
        "primer_enabled", "vault_primer_enabled",
        coerce_bool,
    )
    if primer_enabled is not MISSING:
        out["primer_enabled"] = primer_enabled

    custom_sys = _resolve(
        "system_prompt", "vault_chat_system_prompt",
        lambda v: coerce_string_max_len(v, VAULT_SYSTEM_PROMPT_LIMIT),
    )
    if custom_sys is not MISSING:
        out["custom_system_prompt"] = custom_sys

    # Reranker model name is config-only.  Body override would let a
    # malicious page swap in arbitrary HuggingFace repos to download —
    # require the user to set it via /api/config (which is also local-only,
    # but at least it's UI-visible rather than per-request invisible).
    rr_model = cfg.get("vault_reranker_model")
    if isinstance(rr_model, str) and rr_model.strip():
        out["reranker_model"] = rr_model.strip()

    agent_enabled = _resolve(
        "agent_enabled", "vault_agent_enabled",
        coerce_bool,
    )
    if agent_enabled is not MISSING:
        out["agent_enabled"] = agent_enabled

    agent_iters = _resolve(
        "agent_max_iterations", "vault_agent_max_iterations",
        lambda v: coerce_int_in_range(v, _AGENT_ITER_MIN, _AGENT_ITER_MAX),
    )
    if agent_iters is not MISSING:
        out["agent_max_iterations"] = agent_iters

    return out

vault_bp = Blueprint('vault', __name__)

@vault_bp.route("/api/obsidian/status")
def api_obsidian_status():
    """Poll indexing state, progress messages, and warnings (local-origin only)."""
    return jsonify(obsidian_manager.get_status_payload())

@vault_bp.route("/api/obsidian/materials")
def api_obsidian_materials():
    """List the files recorded in the current vault index manifest."""
    return jsonify(obsidian_manager.get_indexed_materials())

@vault_bp.route("/api/obsidian/index", methods=["POST"])
def api_obsidian_index():
    """Admit exactly one background indexing run, then return immediately.

    Lock discipline (the subtle part): a *cancel* force-releases the op-lock before
    its background thread finishes its final persist, so a reindex fired right after
    a cancel could race the cancelled run's checkpoint on the same index dir. We
    therefore ``wait_for_indexing`` for any in-flight run to fully exit (503 on
    timeout), THEN ``try_acquire_lock`` (503 if still held), and only then spawn the
    daemon indexer thread — which releases the lock in its own ``finally``.
    """
    from core.config import resolve_chat_model
    data = request.get_json(silent=True) or {}
    cfg = load_config()
    provider = data.get("provider") or cfg.get("provider", "ollama")
    llm = data.get("llm") or resolve_chat_model(cfg, provider)
    embed = data.get("embed") or cfg.get("embed", DEFAULT_EMBED)
    if not obsidian_manager.get_vault_path():
        return jsonify({"error": "Vault path not configured"}), 400
    # A cancel force-releases the op lock BEFORE its background thread finishes the
    # final persist, so a reindex started right after a cancel could run its
    # setup/archive concurrently with the cancelled run's checkpoint on the same
    # index dir.  Wait for any in-flight run to fully exit first (mirrors the
    # reset path's guard).  A normal cancel's thread exits in well under a second.
    if not obsidian_manager.wait_for_indexing(timeout=30.0):
        return jsonify({"error": "A previous indexing run is still finishing; please retry shortly."}), 503
    # Per-operation epoch token (improvement plan 2026-07-04, item 1.5): the
    # token this acquisition returned lives on THIS closure's stack and is the
    # only epoch this run may heartbeat/release with. Previously the epoch sat
    # on the shared manager singleton, so a cancel + new acquisition (e.g. a
    # refactor Apply) overwrote it and this finally released the NEW holder's
    # lock mid-operation. Invariant: run_index can only release the
    # acquisition it was admitted under; a stale release is a no-op.
    op_epoch = obsidian_manager.try_acquire_lock(ttl=3600)
    if not op_epoch:
        return jsonify({"error": "An indexing operation is already in progress"}), 503
    def run_index():
        try:
            obsidian_manager.index_vault(
                llm, embed, provider_name=provider, op_epoch=op_epoch
            )
        finally:
            obsidian_manager.release_lock(op_epoch)
    t = threading.Thread(target=run_index, daemon=True)
    obsidian_manager.register_index_thread(t)
    t.start()
    return jsonify({"ok": True})

@vault_bp.route("/api/obsidian/chat", methods=["POST"])
def api_obsidian_chat():
    """Stream a vault answer as SSE — single-shot RAG or (opt-in) the ReAct agent.

    Resolves the per-request knobs (``_resolve_chat_params``), picks the worker
    (``_run_agent`` when ``agent_enabled`` else ``_run_chat``), then runs the
    queue+worker+consumer loop described in the module docstring. The agent path is
    additionally bounded by ``wall_clock_s`` (the exact user cap); the consumer's
    stall timeout is floored at ``_SINGLE_SHOT_FLOOR_S`` so lowering that cap can
    never starve a slow single-shot first token (the two share this one consumer).
    """
    data = request.get_json(silent=True)
    if not data or "message" not in data:
        return jsonify({"error": "Missing message"}), 400
    message = data["message"]
    cfg = load_config()
    from core.config import resolve_chat_model
    provider = data.get("provider") or cfg.get("provider", "ollama")
    llm = data.get("llm") or resolve_chat_model(cfg, provider)
    embed = data.get("embed") or cfg.get("embed", DEFAULT_EMBED)
    chat_params = _resolve_chat_params(data, cfg)
    # Pop agent-mode keys before chat_params is forwarded to stream_chat
    # (which does not accept them as kwargs).
    agent_enabled = bool(chat_params.pop("agent_enabled", False))
    agent_max_iters = int(chat_params.pop("agent_max_iterations", 6))
    # Resolve the wall-clock cap defensively: coerce_int_in_range rejects
    # NaN/Inf/strings and clamps to the validated range, so a hand-edited
    # config.json (which bypasses the POST /api/config validator) can never
    # crash this route or yield a negative/instant deadline. `or` covers the
    # None-on-bad-input and the impossible-0 cases.
    wall_clock_s = coerce_int_in_range(cfg.get("agent_wall_clock_s"), 30, 1800) or _CHAT_TOKEN_TIMEOUT_S
    # Consumer stall guard floored so lowering the agent cap can't starve the
    # single-shot path (see _SINGLE_SHOT_FLOOR_S). The agent deadline stays the
    # exact user cap; only this backstop is floored.
    consumer_timeout_s = max(wall_clock_s, _SINGLE_SHOT_FLOOR_S) + _STALL_MARGIN_S

    def _vault_worker(put, cancel):
        found_any = False
        errored = False
        def _put(item):
            nonlocal found_any, errored
            if isinstance(item, dict):
                if item.get("token"):
                    found_any = True
                if item.get("error"):
                    errored = True
            put(item)
            
        def _stage(text: str) -> None:
            if text:
                _put({"info": text})

        def _run_chat():
            try:
                response = obsidian_manager.stream_chat(
                    message,
                    llm,
                    embed,
                    provider_name=provider,
                    stage_cb=_stage,
                    **chat_params,
                )
                chunks = []
                if hasattr(response, "source_nodes") and response.source_nodes:
                    from rag.engine import SimpleQueryEngine
                    chunks = SimpleQueryEngine._nodes_to_chunks(response.source_nodes)
                elif hasattr(response, "used_chunks") and response.used_chunks:
                    chunks = response.used_chunks

                if chunks:
                    retrieval_data = []
                    for c in chunks:
                        retrieval_data.append({
                            "source": c.source,
                            "score": c.score,
                            "is_image": c.metadata.get("is_image", False),
                        })
                    _put({"retrieval": retrieval_data})

                if hasattr(response, 'response_gen'):
                    _stage("Waiting for model response…")
                    for tok in response.response_gen:
                        if cancel.is_set():
                            break
                        if tok:
                            _put({"token": tok})
                else:
                    text = str(response)
                    if not text or text == _EMPTY_RESPONSE_SENTINEL:
                        text = _NO_CONTENT_MSG
                    _put({"token": text})
            except Exception as exc:
                if not cancel.is_set():
                    _put({"error": sanitise_error_msg(exc)})

        def _run_agent():
            try:
                deadline = time.monotonic() + wall_clock_s
                ctx = VaultToolContext(
                    llm_name=llm,
                    embed_name=embed,
                    provider_name=provider,
                    similarity_cutoff=chat_params.get("similarity_cutoff", 0.25),
                    hybrid_enabled=chat_params.get("hybrid_enabled", True),
                    reranker_enabled=chat_params.get("reranker_enabled", True),
                    reranker_model=chat_params.get("reranker_model", ""),
                    deadline_monotonic_s=deadline,
                )
                tools = ToolRegistry(build_vault_tools(obsidian_manager, ctx))

                def _rag_fallback():
                    fallback_params = {
                        k: v for k, v in chat_params.items()
                        if k not in ("agent_enabled", "agent_max_iterations")
                    }
                    response = obsidian_manager.stream_chat(
                        message, llm, embed,
                        provider_name=provider,
                        stage_cb=_stage,
                        **fallback_params,
                    )
                    if hasattr(response, 'response_gen'):
                        yield from response.response_gen
                    else:
                        text = str(response)
                        if not text or text == _EMPTY_RESPONSE_SENTINEL:
                            text = _NO_CONTENT_MSG
                        yield text

                def _on_agent_event(ev):
                    item = _agent_event_to_queue_item(ev)
                    if item is not None:
                        _put(item)

                budget = run_agent_loop(
                    user_message=message,
                    provider_name=provider,
                    model=llm,
                    user_system_prompt=chat_params.get("custom_system_prompt", ""),
                    temperature=chat_params.get("temperature"),
                    tools=tools,
                    cfg=cfg,
                    on_event=_on_agent_event,
                    max_iterations=agent_max_iters,
                    deadline_monotonic_s=deadline,
                    rag_fallback_fn=_rag_fallback,
                    capability_state=_agent_capability_state,
                    cancel_event=cancel,
                )
                if not cancel.is_set() and budget.iteration_count > 0:
                    _put({"info": _format_agent_usage(budget)})
            except Exception as exc:
                if not cancel.is_set():
                    _put({"error": sanitise_error_msg(exc)})

        try:
            if agent_enabled:
                _run_agent()
            else:
                _run_chat()
        except Exception:
            errored = True
            raise
        finally:
            if not found_any and not errored and not cancel.is_set():
                placeholder = _NO_AGENT_ANSWER_MSG if agent_enabled else _NO_CONTENT_MSG
                _put({"token": placeholder})

    preflight_msgs = []
    index_state = obsidian_manager.get_status()
    if index_state in ("running", "scanning", "embedding", "paused", "paused_scan", "paused_partial"):
        preflight_msgs.append(
            "Indexing is still in progress — answers may miss "
            "content from files not yet indexed."
        )

    return run_sse_worker(
        _vault_worker,
        consumer_timeout_s=consumer_timeout_s,
        preflight_msgs=preflight_msgs,
    )

@vault_bp.route("/api/native-pick-folder", methods=["POST"])
def api_native_pick_folder():
    """Open a native folder picker; optionally constrain the choice to inside the vault.

    When ``constrain_to_vault`` is set, the chosen path is resolved and required to be
    a *sub*-folder of the configured vault (the vault root itself is rejected),
    returning the vault-relative path for the caller to use as a scope.
    """
    import webview
    if not webview.windows:
        return jsonify({"error": "No active window"}), 503
    result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG, allow_multiple=False)
    if not result:
        return jsonify({"cancelled": True})
    chosen = result[0] if isinstance(result, (list, tuple)) else result
    if not os.path.isdir(chosen):
        return jsonify({"error": "Selected path is not a directory"}), 400
    data = request.get_json(silent=True) or {}
    base_path = data.get("base_path") or obsidian_manager.get_vault_path()
    rel_path = None
    if data.get("constrain_to_vault") and base_path:
        try:
            rel_path = Path(chosen).resolve().relative_to(Path(base_path).resolve()).as_posix()
        except ValueError:
            return jsonify({"error": "Selected folder must be inside the configured vault"}), 400
        if not rel_path or rel_path == ".":
            return jsonify({"error": "Select a subfolder inside the vault, not the vault root"}), 400
    return jsonify({"ok": True, "path": chosen, "relative_path": rel_path})

@vault_bp.route("/api/obsidian/pause", methods=["POST"])
def api_obsidian_pause():
    """Request a resumable pause of the in-flight indexing run."""
    ok = obsidian_manager.pause_indexing()
    return jsonify({"ok": ok})

@vault_bp.route("/api/obsidian/cancel", methods=["POST"])
def api_obsidian_cancel():
    """Cancel indexing and force-release the op-lock; report whether it was held."""
    was_held = obsidian_manager.cancel_indexing()
    return jsonify({"ok": True, "was_held": was_held})
