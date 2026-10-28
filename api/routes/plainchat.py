"""Plain Chat — a RAG-free multi-turn conversation with the configured LLM.

``POST /api/plainchat`` streams tokens (SSE) for the Plain Chat panel.  The
server is stateless: the browser owns the conversation and sends the full
(capped) message array on every turn.  No vault retrieval, no agent loop,
no tools — just the message history through
``core.llm.chat.stream_chat_messages``.

**Attachments (2026-08-29).** A message may carry ``attachment_ids``: ids minted
by ``POST /api/chat-attachment``, which already extracted the file to text. The
route inflates them here — the client never sends file content, so the server
owns what reaches the model (``apply-staged``'s posture). Each file's text is
wrapped by ``core.agent.tools.wrap_untrusted`` exactly as vault and web-search
content is: a user-supplied PDF is a prompt-injection vector. Ids ride the
MESSAGE, not the request, so a document attached three turns ago is still in
context on the fourth without the client re-uploading or re-sending its text.
None of this adds retrieval, an agent loop or a tool: the block is plain text
prepended to a user turn.

The SSE skeleton (queue + daemon worker + consumer with a stall guard)
mirrors ``api/routes/vault.py``'s ``/api/obsidian/chat`` handler, stripped
of every agent / iteration / tool branch: only ``{info}`` / ``{token}`` /
``{error}`` frames plus the ``[DONE]`` sentinel.
"""

import logging
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from api.security import sanitise_error_msg
from api.validators import coerce_float_in_range, coerce_string_max_len
from core.config import load_config, resolve_chat_model
from core.llm.usage import usage_frame_payload, usage_tracker
from api.sse import online_aware_floor_s, run_sse_worker
from core.constants import (
    CHAT_ATTACHMENT_MAX_PER_MESSAGE,
    CHAT_ATTACHMENT_REQUEST_MAX_CHARS,
    SYSTEM_PROMPT_LIMIT,
    # Shared SSE stall-guard timing. Plain chat has no agent wall-clock — the
    # consumer get is its ONLY time guard (exactly like the single-shot RAG
    # path), so it uses the floored base + margin directly. One definition, no
    # cross-route private import. Rationale at the definition site.
    SSE_STALL_MARGIN_S,
)
from core.llm.chat import stream_chat_messages
from services.attachment_service import (
    build_attachment_block,
    fetch_attachments_map,
)

logger = logging.getLogger("chatekld")

plainchat_bp = Blueprint("plainchat", __name__)

_ALLOWED_ROLES = {"user", "assistant"}
_MAX_MESSAGES = 20          # keep only the most recent N turns
_MSG_CONTENT_MAX_LEN = 24000  # per-message content cap (defense-in-depth)


def _validate_attachment_ids(raw) -> list[str] | None:
    """Coerce one message's ``attachment_ids``. ``None`` rejects the request.

    Absent means "no attachments" and is the overwhelmingly common case, so it
    yields ``[]`` rather than an error. Anything PRESENT but malformed rejects,
    matching the rule the surrounding validator applies to content: silently
    dropping an attachment would change what the model was shown without
    changing anything the user can see.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            return None
        item = item.strip()
        # Ids are minted as uuid4 by the upload route; a bounded, plainly-shaped
        # id keeps a hostile value out of the SQL parameter list and out of logs.
        if not item or len(item) > 64:
            return None
        if item not in out:
            out.append(item)
    if len(out) > CHAT_ATTACHMENT_MAX_PER_MESSAGE:
        return None
    return out


def _inflate_attachments(messages: list[dict]) -> tuple[list[dict], list[str], str | None]:
    """Prepend each message's attachment text to its content.

    Returns ``(messages, missing_filenames_or_ids, error)``. The text is fetched
    server-side from the ids — the client never sends file bytes — and each file
    is wrapped by ``wrap_untrusted`` before it reaches the model.

    Two deliberate asymmetries:

    * An id with no row (expired past the TTL, or already deleted) is NOT fatal.
      It is reported back so the user is told, in the transcript, which document
      the answer was written without. Refusing the whole turn would strand a
      long conversation the moment a 24-hour-old attachment aged out; answering
      silently would let the model say "the report does not mention X" about a
      report it never received.
    * Exceeding the whole-request character budget IS fatal, with the figures
      named. N files each inside their own cap still add up to a prompt the
      provider will reject, and a refusal the user can act on beats a provider
      error they cannot read.
    """
    # ALWAYS project down to {role, content}: `attachment_ids` is this module's
    # internal bookkeeping and must never reach stream_chat_messages, which
    # hands the array to the provider adapters. Returning the validated list
    # unchanged on the no-attachment fast path leaked the key into every plain
    # chat request — caught by the existing contract test, which asserts the
    # exact array the helper receives.
    plain = [{"role": m["role"], "content": m["content"]} for m in messages]
    if not any(m["attachment_ids"] for m in messages):
        return plain, [], None

    # ONE query for every id in the request. Per-message fetching took the
    # global DB_LOCK up to 20 times for a single turn.
    by_id = fetch_attachments_map(
        [i for m in messages for i in m["attachment_ids"]])

    total = 0
    out: list[dict] = []
    missing: set[str] = set()
    for msg in messages:
        ids = msg["attachment_ids"]
        if not ids:
            out.append({"role": msg["role"], "content": msg["content"]})
            continue
        rows = [by_id[i] for i in ids if i in by_id]
        # A SET, not a list: one file re-sent on every turn of a conversation
        # appears on many messages, and extending a list counted it once per
        # message — so a single expired attachment was announced as "3 earlier
        # attachment(s) are no longer available". In a frame whose entire job is
        # to tell the user honestly what the answer was written without, an
        # inflated count undermines the sentence it appears in.
        missing.update(i for i in ids if i not in by_id)
        if not rows:
            out.append({"role": msg["role"], "content": msg["content"]})
            continue
        block = build_attachment_block(rows)
        total += len(block)
        if total > CHAT_ATTACHMENT_REQUEST_MAX_CHARS:
            return plain, sorted(missing), (
                f"The attached files add up to more than "
                f"{CHAT_ATTACHMENT_REQUEST_MAX_CHARS:,} characters of text, which "
                "is more than one request can carry. This counts every file "
                "still in the conversation, not just the ones on this message — "
                "an earlier turn's attachment cannot be removed, so if the "
                "composer is empty, start a new chat.")
        out.append({"role": msg["role"], "content": f"{block}\n\n{msg['content']}"})
    return out, sorted(missing), None


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
        ids = _validate_attachment_ids(entry.get("attachment_ids"))
        if ids is None:
            return None
        cleaned.append({"role": role, "content": content, "attachment_ids": ids})

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
            # The merge must carry BOTH turns' attachments. Keeping only one
            # side's would drop a document the user attached, and the drop
            # would be invisible: the text still merges, the answer still
            # streams, and the model simply never sees the file. Order is
            # preserved and duplicates collapse.
            #
            # NOT re-capped to CHAT_ATTACHMENT_MAX_PER_MESSAGE (2026-08-29
            # review). That cap bounds what a USER may attach to ONE message and
            # is enforced per submitted message by _validate_attachment_ids,
            # above. Re-applying it here, after a merge THIS FUNCTION performed,
            # could only destroy something the user did supply: the slice took
            # from the end, so a 4-attachment turn merged with a 1-attachment
            # one silently dropped the NEWEST file — the exact failure the
            # paragraph above claims to prevent — while the answer streamed
            # normally and nothing said the document was missing.
            #
            # The bound that actually protects the provider is
            # CHAT_ATTACHMENT_REQUEST_MAX_CHARS, enforced in
            # _inflate_attachments and unaffected by this. The blast radius
            # stays bounded anyway: each submitted message is capped and
            # deduped, the window is _MAX_MESSAGES turns, and the character
            # budget caps the text that results.
            ids = list(dict.fromkeys(
                merged[-1]["attachment_ids"] + msg["attachment_ids"]
            ))
            merged[-1] = {"role": msg["role"], "content": joined,
                          "attachment_ids": ids}
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

    # Inflate BEFORE streaming so an over-budget request keeps a real status
    # code instead of a mid-stream error frame (the notesync/accession policy:
    # decide what can be decided while an HTTP status still means something).
    messages, missing_attachments, attach_error = _inflate_attachments(messages)
    if attach_error:
        return jsonify({"error": attach_error}), 400

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
    # online_aware_floor_s widens the floor to online_timeout_s so a legal
    # slow online first token isn't killed by the app (timeout-chain repair,
    # 2026-07-22 audit M2).
    consumer_timeout_s = online_aware_floor_s(cfg) + SSE_STALL_MARGIN_S

    def _plainchat_worker(put, cancel):
        def _stage(text: str) -> None:
            if text:
                put({"info": text})

        # An attachment that aged out (or was deleted) is announced in the
        # transcript, never swallowed: the answer below was written WITHOUT it,
        # and a user who cannot see that will read a confident "the document
        # does not say" as a statement about their document.
        if missing_attachments:
            put({"info": (
                f"{len(missing_attachments)} earlier attachment(s) are no longer "
                "available and were not included in this answer. Attachments are "
                "kept for 24 hours; re-attach the file to ask about it again.")})

        # Export provenance (2026-07-22): the client resolves neither provider
        # nor model (both server-side from config), so announce them up front —
        # the panel stores the frame on the assistant turn for MD/TXT exports.
        put({"meta": {
            "provider": provider,
            "model": model,
            "temperature": temperature,
        }})
        start_iso = datetime.now(timezone.utc).isoformat()
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
            return
        if cancel.is_set():
            return
        # Best-effort real token/cost figures for this answer: the adapters
        # recorded the call(s) into the tracker ring tagged workflow=plain_chat;
        # sum everything since our start stamp. Never fails the stream.
        try:
            payload = usage_frame_payload(
                usage_tracker.records_since(start_iso, workflow="plain_chat")
            )
            if payload:
                put({"usage": payload})
        except Exception:
            logger.debug("plainchat usage frame failed", exc_info=True)
    return run_sse_worker(
        _plainchat_worker,
        consumer_timeout_s=consumer_timeout_s,
    )
