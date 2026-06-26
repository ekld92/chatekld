"""Plain Chat — a RAG-free multi-turn conversation with the configured LLM.

``POST /api/plainchat`` streams tokens (SSE) for the Plain Chat panel.  The
server is stateless: the browser owns the conversation and sends the full
(capped) message array on every turn.  No vault retrieval, no agent loop,
no tools — just the message history through
``core.llm.chat.stream_chat_messages``.

The SSE skeleton (queue + daemon worker + consumer with a stall guard)
mirrors ``api/routes/vault.py``'s ``/api/obsidian/chat`` handler, stripped
of every agent / iteration / tool branch: only ``{info}`` / ``{token}`` /
``{error}`` frames plus the ``[DONE]`` sentinel.
"""
import json
import queue
import threading

from flask import Blueprint, Response, jsonify, request

from api.security import origin_is_local, sanitise_error_msg
from api.validators import coerce_float_in_range, coerce_string_max_len
from core.config import load_config, resolve_chat_model
from core.constants import (
    SYSTEM_PROMPT_LIMIT,
    # Shared SSE stall-guard timing. Plain chat has no agent wall-clock — the
    # consumer get is its ONLY time guard (exactly like the single-shot RAG
    # path), so it uses the floored base + margin directly. One definition, no
    # cross-route private import. Rationale at the definition site.
    SSE_SINGLE_SHOT_FLOOR_S,
    SSE_STALL_MARGIN_S,
)
from core.llm.chat import stream_chat_messages

plainchat_bp = Blueprint("plainchat", __name__)

_ALLOWED_ROLES = {"user", "assistant"}
_MAX_MESSAGES = 20          # keep only the most recent N turns
_MSG_CONTENT_MAX_LEN = 24000  # per-message content cap (defense-in-depth)


def _validate_messages(raw) -> list[dict] | None:
    """Coerce a raw ``messages`` body into a clean, capped, provider-safe array.

    Returns ``None`` when *raw* is not a usable list of
    ``{role ∈ {user, assistant}, content: str}`` entries.

    Pipeline:

    1. **Structural validation.** A malformed entry (non-dict, bad role,
       non-string/empty content) rejects the *whole* request rather than being
       silently dropped — silently dropping a turn would change the model's
       context invisibly. Content is stripped and capped to
       :data:`_MSG_CONTENT_MAX_LEN`. NOTE: the cap is applied to *every* message
       including a re-sent assistant turn, so a very long prior answer is
       silently clipped when used as context — an intentional payload bound, not
       a bug, but worth knowing when debugging "the model forgot the long answer".
    2. **Window.** Truncate to the last :data:`_MAX_MESSAGES` turns.
    3. **Provider-shape normalization.** Anthropic and Gemini require the first
       message to be ``user`` and reject consecutive same-role turns; OpenAI and
       the flattened local path are tolerant. A window sliced mid-exchange can
       begin on an ``assistant`` turn, and a turn whose model reply was empty
       (the UI does not record an empty assistant turn — see the route) can leave
       two ``user`` turns adjacent. So we (a) drop leading non-user turns, then
       (b) merge consecutive same-role turns into one (joining content), re-capping
       the merged content. Both are no-ops for a normal alternating log.

    Returns ``None`` if normalization empties the window (e.g. an all-assistant
    body), which the route surfaces as a 400.
    """
    if not isinstance(raw, list) or not raw:
        return None
    cleaned: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            return None
        role = entry.get("role")
        if role not in _ALLOWED_ROLES:
            return None
        content = coerce_string_max_len(entry.get("content"), _MSG_CONTENT_MAX_LEN)
        if content is None or content == "":
            return None
        cleaned.append({"role": role, "content": content})

    window = cleaned[-_MAX_MESSAGES:]

    # (a) Drop leading assistant turn(s) so the window begins on a user message.
    while window and window[0]["role"] != "user":
        window.pop(0)
    if not window:
        return None

    # (b) Merge consecutive same-role turns. The join keeps both contents but
    #     re-caps to _MSG_CONTENT_MAX_LEN so the merge cannot defeat the per-
    #     message bound. (Plain string slice, not coerce_*, to avoid re-stripping
    #     content that was already validated above.)
    merged: list[dict] = []
    for msg in window:
        if merged and merged[-1]["role"] == msg["role"]:
            joined = (merged[-1]["content"] + "\n\n" + msg["content"])[:_MSG_CONTENT_MAX_LEN]
            merged[-1] = {"role": msg["role"], "content": joined}
        else:
            merged.append(dict(msg))
    return merged


@plainchat_bp.route("/api/plainchat", methods=["POST"])
def api_plainchat():
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid request body"}), 400

    messages = _validate_messages(data.get("messages"))
    if messages is None:
        return jsonify({"error": "Invalid or empty messages"}), 400

    cfg = load_config()
    provider = cfg.get("provider", "ollama")
    model = resolve_chat_model(cfg, provider)

    # temperature: body (0.0-2.0) → persisted chat_temperature → hard default.
    temperature = coerce_float_in_range(data.get("temperature"), 0.0, 2.0)
    if temperature is None:
        temperature = coerce_float_in_range(cfg.get("chat_temperature"), 0.0, 2.0)
    if temperature is None:
        temperature = 0.3

    # system_prompt: body (capped) → persisted chat_system_prompt.
    sys_raw = data.get("system_prompt")
    if not isinstance(sys_raw, str):
        sys_raw = cfg.get("chat_system_prompt", "")
    system_prompt = coerce_string_max_len(sys_raw, SYSTEM_PROMPT_LIMIT) or ""

    # Plain chat has no agent wall-clock; the consumer get is the ONLY time
    # guard (like the single-shot RAG path), so it waits the floored base + the
    # stall margin. NOTE on worker lifetime: for a LOCAL provider with the
    # default local_request_timeout_s=0 there is no HTTP-level timeout, so a
    # wedged backend can leave the daemon worker thread running past this
    # deadline — the consumer still frees the client at consumer_timeout_s and
    # emits a clean error; the thread exits when the blocking call finally
    # returns. This matches vault.py's single-shot path (the architecture cannot
    # interrupt a blocking read from outside the call).
    consumer_timeout_s = SSE_SINGLE_SHOT_FLOOR_S + SSE_STALL_MARGIN_S

    def generate():
        cancel = threading.Event()
        # Bounded queue (back-pressure): a fast producer cannot grow memory
        # without bound if the client consumes slowly.
        event_q: queue.Queue = queue.Queue(maxsize=512)
        _DONE = object()  # unique sentinel marking the worker has finished

        def _put(item):
            # Block until the item is enqueued, BUT re-check ``cancel`` every 1 s
            # so a disconnected/timed-out client (consumer no longer draining,
            # queue full) cannot wedge the worker thread here forever — it bails
            # out and lets the worker reach its finally/_DONE.
            placed = False
            while not placed and not cancel.is_set():
                try:
                    event_q.put(item, timeout=1)
                    placed = True
                except queue.Full:
                    continue

        def _stage(text: str) -> None:
            # info_cb for stream_chat_messages — surfaces the fallback notice as
            # an {info} SSE frame.
            if text:
                _put({"info": text})

        def _run_chat():
            # Worker thread: pump tokens from the (RAG-free) chat helper into the
            # queue. Runs off the request thread so the consumer/generator can
            # apply the stall timeout independently.
            try:
                for tok in stream_chat_messages(
                    messages=messages,
                    system_prompt=system_prompt,
                    provider_name=provider,
                    model=model,
                    temperature=temperature,
                    cfg=cfg,
                    info_cb=_stage,
                ):
                    # Stop pumping the moment the consumer gave up (timeout /
                    # client disconnect) — avoids spending work on a dead stream.
                    if cancel.is_set():
                        break
                    if tok:
                        _put({"token": tok})
            except Exception as exc:
                # Surface a structured, key-redacted error frame — unless the
                # consumer already cancelled (then nobody is listening).
                if not cancel.is_set():
                    _put({"error": sanitise_error_msg(exc)})
            finally:
                # Always signal completion so the consumer's blocking get()
                # returns promptly instead of waiting out the full timeout.
                try:
                    event_q.put(_DONE, timeout=5)
                except queue.Full:
                    pass

        try:
            threading.Thread(target=_run_chat, daemon=True).start()
            while True:
                try:
                    item = event_q.get(timeout=consumer_timeout_s)
                except queue.Empty:
                    # No event at all within the stall window: the worker is
                    # silent (hung backend). Free the client with a clean error
                    # and signal the worker to stop pumping.
                    cancel.set()
                    yield f"data: {json.dumps({'error': 'Generation timed out — the model may be overloaded. Please try again.'})}\n\n"
                    break
                if item is _DONE:
                    break
                # Branch dispatch — only {info} / {token} / {error} exist (no
                # agent/iteration/tool frames in plain chat). An {error} is
                # terminal; cancel the worker and stop.
                if isinstance(item, dict) and item.get("error"):
                    cancel.set()
                    yield f"data: {json.dumps({'error': item['error']})}\n\n"
                    break
                if isinstance(item, dict) and item.get("info"):
                    yield f"data: {json.dumps({'info': item['info']})}\n\n"
                    continue
                if isinstance(item, dict) and item.get("token"):
                    yield f"data: {json.dumps({'token': item['token']})}\n\n"

            # Intentionally NO synthesized "no response" token here: the client
            # records every {token} into its conversation history, so a synthetic
            # placeholder would be re-sent to the model as a real assistant turn.
            # On an empty-but-clean stream we simply end with [DONE]; the frontend
            # detects the empty answer and renders a muted, non-recorded bubble.
            yield "data: [DONE]\n\n"
        except GeneratorExit:
            # Client disconnected mid-stream — signal the worker and propagate
            # (yielding here would raise from Werkzeug's close() path).
            cancel.set()
            raise
        except Exception as e:
            yield f"data: {json.dumps({'error': sanitise_error_msg(e)})}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            # Non-yielding cleanup only. The [DONE] sentinel is emitted on the
            # normal/error paths above; on GeneratorExit there is no consumer to
            # send it to.
            cancel.set()

    return Response(generate(), mimetype="text/event-stream")
