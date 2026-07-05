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

from flask import Blueprint, jsonify, request

from api.security import sanitise_error_msg
from api.validators import coerce_float_in_range, coerce_string_max_len
from core.config import load_config, resolve_chat_model
from api.sse import run_sse_worker
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

    def _plainchat_worker(put, cancel):
        def _stage(text: str) -> None:
            if text:
                put({"info": text})

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
                if cancel.is_set():
                    break
                if tok:
                    put({"token": tok})
        except Exception as exc:
            if not cancel.is_set():
                put({"error": sanitise_error_msg(exc)})
    return run_sse_worker(
        _plainchat_worker,
        consumer_timeout_s=consumer_timeout_s,
    )
