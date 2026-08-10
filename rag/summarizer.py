"""Single-Paper summary streaming.

Owns ``summarise_stream`` (the token generator behind ``/api/summarise``) and
``build_prompt``. Given already-extracted (and untrusted) PDF text plus the
prompt/audience/language/generation knobs, it trims the text to a budget, builds
the user message from the structured/legacy prompt presets, and streams the
summary one token at a time through the active provider.

Two provider families are handled with one signature:
  * **Local** (``ollama`` / ``lm_studio``) — streamed via the legacy
    ``core.providers`` chat path, with per-backend kwarg shaping (Ollama uses
    ``num_ctx`` / ``repeat_penalty`` / ``num_predict``; LM Studio uses
    ``max_tokens``). The document is fitted to the backend's context window
    via ``core.llm.budget`` (Ollama: the ``num_ctx`` this call sends; LM
    Studio: the ``context_window`` config key) and any trim is reported
    through ``info_cb`` — never silent.
  * **Online** (``openai`` / ``anthropic`` / ``google``) — routed through
    ``_stream_online``, which drives the unified ``core.llm`` provider layer with
    fallback-before-first-token. ``_stream_online`` is the model the RAG-free
    Plain Chat helper (``core/llm/chat.py``) was later based on.

This module is summary-only; vault RAG retrieval lives in ``rag/engine.py``.
"""
import logging
import re
from typing import Generator, Optional
from core.providers import get_provider
from core.providers.base import local_request_timeout
from core.config import load_config
from core.constants import PAPER_LOCAL_STALL_TIMEOUT_S
from core.llm.factory import is_online
from core.llm.policy import parse_policy_from_config
from core.llm.prompt import build_summary_user_message
from core.llm.types import LLMRequest

logger = logging.getLogger(__name__)

def _truncate_at_references(text: str) -> tuple[str, int]:
    """Strip a trailing bibliography/reference section to save tokens.

    Returns ``(kept_text, dropped_chars)``.

    B5 (2026-07-13): cut at the LAST reference heading that sits past the 50%
    mark, not the FIRST match anywhere. The old first-match-wins cut silently
    dropped 90%+ of a textbook whose chapter 1 had a "References" heading on
    page 30 of 500 (and any appendices after a mid-document bibliography). Only
    a heading in the document's tail is a real end-matter bibliography; anything
    earlier is a section reference list that must be kept. The caller surfaces
    the dropped-char count through ``info_cb`` — "clip loudly, never silently".
    """
    ref_marker = re.compile(
        r"\n(?:References|Bibliography|Works Cited)\s*\n", re.IGNORECASE
    )
    half = len(text) // 2
    cut = -1
    # No break: keep overwriting so `cut` lands on the LAST qualifying heading
    # (a chapter-12 reference list at 55% must not swallow the chapters and
    # real bibliography after it).
    for m in ref_marker.finditer(text):
        if m.start() >= half:
            cut = m.start()
    if cut < 0:
        return text, 0
    return text[:cut], len(text) - cut

def build_prompt(text: str, user_template: str, doc_type: str, focus_question: str = "") -> str:
    """Inject extracted text into either structured or legacy prompt presets."""
    return build_summary_user_message(
        document_text=text,
        user_template=user_template,
        doc_type=doc_type,
        focus_question=focus_question,
    )

# Online providers take very large contexts; past this bound the map-reduce
# path kicks in anyway (reliability + cost bound, not a hard model limit).
# Overridable via the config-only `online_doc_window_tokens` key so an online
# model with a SMALLER real context than this default doesn't receive
# over-window map parts (provider-side truncation/400 we can't see).
_ONLINE_DOC_WINDOW_TOKENS = 100_000
# Per-part output cap on map passes — partial notes, not full summaries.
_MAP_OUTPUT_TOKENS = 1024
# Token overlap between adjacent parts so a finding straddling a cut isn't lost.
_MAP_OVERLAP_TOKENS = 200
# Emit a keep-alive info during a long silent map pass: the paper route's SSE
# consumer stall guard (~330 s) sees no frames while a map pass generates
# internally, so a slow local pass must announce it is alive. The beat runs on
# its own daemon thread (not per-token) because the dangerous silence is
# precisely the window in which NO token arrives — a token-driven beat can
# never fire during the pre-first-token stall it exists to cover.
_MAP_HEARTBEAT_S = 60.0
# Hard ceiling on map parts per run. Without it a PDF_MAX_PAGES-scale document
# with a small context window degenerates into thousands of sequential LLM
# calls (hours of uncancellable work before the reduce). Past the cap the
# tail parts are skipped WITH an explicit coverage notice — same "clip loudly,
# never silently" stance as the reduce-phase clip below.
_MAP_MAX_PARTS = 64


def summarise_stream(
    text: str,
    model: str,
    system_prompt: str = "You are a helpful assistant.",
    user_template: Optional[str] = None,
    doc_type: str = "Research Paper",
    audience_modifier: str = "",
    language: str = "English",
    focus_question: str = "",
    temperature: float = 0.3,
    max_tokens: Optional[int] = None,
    num_ctx: int = 32768,
    top_p: float = 0.9,
    repeat_penalty: float = 1.1,
    provider_name: str = "ollama",
    info_cb=None,
    should_stop=None,
) -> Generator[str, None, None]:
    """Stream summary tokens via the active LLM provider.

    Pipeline: strip the bibliography (``_truncate_at_references``), measure the
    document against the provider's usable context budget (``core.llm.budget``;
    Ollama's window is the ``num_ctx`` this call sends, LM Studio's the
    ``context_window`` config key, online providers the config-overridable
    ``online_doc_window_tokens`` bound), then either

    * **fits** → one single-pass summary (``_single_pass_stream``), or
    * **over budget** → **map-reduce** (WP-C3, ``_map_reduce_stream``): the
      document is split into window-sized parts, each part is summarised into
      concise notes (progress via ``info_cb``), and the final summary is
      synthesised from the combined notes. Slower; any clipping (the
      ``_MAP_MAX_PARTS`` cap, the single-level reduce) is reported through
      ``info_cb`` — the pre-2026-07 behaviour clipped the tail silently
      (local) or shipped an over-window prompt (online).

    *should_stop* (callable → bool) is polled between parts and per streamed
    map token so an abandoned request stops burning LLM calls mid map-reduce —
    the map phase yields nothing, so the SSE worker's per-yielded-token cancel
    check can never fire during it.
    """
    text, _dropped_ref_chars = _truncate_at_references(text)
    if _dropped_ref_chars and info_cb:
        info_cb(f"Stripped a trailing bibliography ({_dropped_ref_chars:,} chars) before summarising.")
    if user_template is None:
        user_template = "Summarize the key findings."

    sys_p = system_prompt + audience_modifier
    if language and language != "English":
        sys_p += f" Write in {language}."

    from core.llm.budget import count_text_tokens, document_budget_tokens

    if is_online(provider_name):
        try:
            window = int(load_config().get("online_doc_window_tokens") or 0)
        except (TypeError, ValueError):
            window = 0
        if window <= 0:
            window = _ONLINE_DOC_WINDOW_TOKENS
    elif provider_name == "ollama":
        window = num_ctx
    else:
        window = load_config().get("context_window", 32768)
    budget = document_budget_tokens(window, max_tokens)
    # Measure-only: the over-budget branch re-splits `text` itself, so decoding
    # a budget-sized trim here (the old fit_text_to_budget call) was pure waste.
    total_tokens = count_text_tokens(text)
    over_budget = total_tokens > budget

    common = dict(
        model=model,
        sys_p=sys_p,
        doc_type=doc_type,
        focus_question=focus_question,
        temperature=temperature,
        max_tokens=max_tokens,
        num_ctx=num_ctx,
        top_p=top_p,
        repeat_penalty=repeat_penalty,
        provider_name=provider_name,
        info_cb=info_cb,
    )
    if over_budget:
        yield from _map_reduce_stream(
            text=text,
            budget=budget,
            total_tokens=total_tokens,
            window=window,
            user_template=user_template,
            should_stop=should_stop,
            **common,
        )
        return
    yield from _single_pass_stream(text=text, user_template=user_template, **common)


def _split_text_tokens(
    text: str,
    part_tokens: int,
    overlap: int = _MAP_OVERLAP_TOKENS,
    max_parts=None,
) -> tuple:
    """Split *text* into ≤``part_tokens``-token parts with a small overlap.

    Token-boundary splits via tiktoken when available; a 4-chars-per-token
    character split otherwise. Overlap keeps a finding that straddles a cut
    from vanishing from both parts.

    Returns ``(parts, clipped, planned_parts)``. When covering the whole text
    would need more than *max_parts* parts, only the first *max_parts* are
    produced and ``clipped`` is True so the caller can SAY what was skipped.
    Within the cap, part sizes are EQUALISED (same part count, same coverage,
    evenly-sized parts) — the old greedy budget-sized-then-remainder split
    could end on a ~overlap-token stub that still paid a full LLM call.
    """
    overlap = max(0, min(overlap, part_tokens // 2))

    def _plan(total: int, size_cap: int, ov: int) -> tuple:
        """(n_parts, part_size, step, planned, clipped) for a *total*-unit text."""
        step0 = max(1, size_cap - ov)
        planned = -(-(total - ov) // step0)  # ceil division
        clipped = bool(max_parts) and planned > max_parts
        n = max_parts if clipped else planned
        if clipped:
            size = size_cap  # maximise coverage of the kept prefix
        else:
            size = min(size_cap, -(-(total - ov) // n) + ov)
        return n, size, max(1, size - ov), planned, clipped

    try:
        from core.llm.usage import _cl100k_encoding

        enc = _cl100k_encoding()
        tokens = enc.encode(text)
        total = len(tokens)
        if total <= part_tokens:
            return [text], False, 1
        n, size, step, planned, clipped = _plan(total, part_tokens, overlap)
        parts = []
        for i in range(n):
            window = tokens[i * step:i * step + size]
            if not window:
                break
            parts.append(enc.decode(window))
        del tokens  # the token list dwarfs the text (~28 B/int); free it early
        return parts, clipped, planned
    except Exception:
        chars, ov = part_tokens * 4, overlap * 4
        total = len(text)
        if total <= chars:
            return [text], False, 1
        n, size, step, planned, clipped = _plan(total, chars, ov)
        parts = []
        for i in range(n):
            window = text[i * step:i * step + size]
            if not window:
                break
            parts.append(window)
        return parts, clipped, planned


def _map_reduce_stream(
    *,
    text: str,
    budget: int,
    total_tokens: int,
    window,
    user_template: str,
    model: str,
    sys_p: str,
    doc_type: str,
    focus_question: str,
    temperature: float,
    max_tokens: Optional[int],
    num_ctx: int,
    top_p: float,
    repeat_penalty: float,
    provider_name: str,
    info_cb=None,
    should_stop=None,
) -> Generator[str, None, None]:
    """Map-reduce summarisation of an over-window document (WP-C3).

    Map: each part gets one bounded summarisation call whose output is
    consumed internally (only ``info_cb`` progress reaches the user). Reduce:
    the combined part notes are streamed through the user's actual template.
    More calls, but no call ever exceeds the window, and any coverage loss
    (the ``_MAP_MAX_PARTS`` cap, the single-level reduce clip) is announced.

    Cancellation: *should_stop* is polled before each part, per streamed map
    token, and per reduce token — the map phase yields nothing to the SSE
    consumer, so without this poll an abandoned request would run every
    remaining LLM call to completion. A liveness heartbeat runs on its own
    daemon thread so a SILENT stall (pre-first-token, when no token-driven
    code executes) still feeds the route's SSE stall guard.
    """
    import threading
    import time as _time

    def _emit(msg: str) -> None:
        if info_cb is not None:
            try:
                info_cb(msg)
            except Exception:
                logger.debug("info_cb failed", exc_info=True)

    def _stopped() -> bool:
        return should_stop is not None and should_stop()

    from core.llm.budget import fit_text_to_budget

    parts, clipped, planned = _split_text_tokens(text, budget, max_parts=_MAP_MAX_PARTS)
    n = len(parts)
    _emit(
        f"Document (~{total_tokens} tokens) exceeds the usable context "
        f"(~{budget} tokens of window {window}) — map-reduce summarisation "
        f"over {n} parts; this takes longer."
    )
    if clipped:
        coverage_pct = max(1, min(100, round(100 * n / max(planned, 1))))
        _emit(
            f"The document is very large: covering it fully would need {planned} "
            f"parts but the per-run cap is {_MAP_MAX_PARTS} — only the first {n} "
            f"parts (~{coverage_pct}% of the document) are summarised. Raise the "
            f"context window (num_ctx / context_window) or summarise the document "
            f"in sections to cover the rest."
        )

    # Liveness heartbeat: emits only after a full silent interval, off-thread,
    # because the stall it covers is exactly the window where no token arrives.
    # `hb_last` is bumped on every token/stage so a flowing stream stays quiet.
    # (Two single-element cells, not one mixed dict — closure-mutable state
    # that types cleanly; GIL-atomic list item assignment, no lock needed.)
    hb_stop = threading.Event()
    hb_label: list[str] = ["Preparing parts"]
    hb_last: list[float] = [_time.monotonic()]

    def _beat_loop() -> None:
        # Poll at a fraction of the interval so stop() is responsive; the
        # info_cb sink (an SSE queue put) is thread-safe.
        while not hb_stop.wait(min(5.0, _MAP_HEARTBEAT_S / 4)):
            if _time.monotonic() - hb_last[0] >= _MAP_HEARTBEAT_S:
                hb_last[0] = _time.monotonic()
                _emit(f"{hb_label[0]}: still working…")

    threading.Thread(target=_beat_loop, name="map-reduce-heartbeat", daemon=True).start()

    try:
        map_output_cap = min(max_tokens or _MAP_OUTPUT_TOKENS, _MAP_OUTPUT_TOKENS)
        partials = []
        for i, part in enumerate(parts, 1):
            if _stopped():
                return
            hb_label[0] = f"Part {i}/{n}"
            hb_last[0] = _time.monotonic()
            _emit(f"Summarising part {i}/{n}…")
            map_template = (
                f"You are processing PART {i} of {n} of a longer document; the other "
                "parts are handled separately. Extract the key content of THIS PART "
                "as concise bullet notes: objectives, methods, findings (keep exact "
                "numbers), limitations, conclusions. Do not write an introduction or "
                "a conclusion for the whole document.\n\n{text}"
            )
            buf = []
            gen = _single_pass_stream(
                text=part,
                user_template=map_template,
                model=model,
                sys_p=sys_p,
                doc_type=doc_type,
                focus_question=focus_question,
                temperature=temperature,
                max_tokens=map_output_cap,
                num_ctx=num_ctx,
                top_p=top_p,
                repeat_penalty=repeat_penalty,
                provider_name=provider_name,
                info_cb=info_cb,
            )
            try:
                for token in gen:
                    if _stopped():
                        return  # finally closes gen (aborts the HTTP stream)
                    buf.append(token)
                    hb_last[0] = _time.monotonic()
            finally:
                # Explicit close so an early return tears down the provider
                # stream now, not at some later GC pass.
                gen.close()
            partials.append("".join(buf).strip())

        if _stopped():
            return

        combined = "\n\n".join(
            f"### Notes from part {i}/{n}\n{p}"
            for i, p in enumerate(partials, 1)
            if p
        )
        combined = (
            "NOTE: the text below consists of sequential partial notes extracted "
            "from ONE longer document. Synthesise them into a single coherent "
            "summary, as if summarising the original document.\n\n" + combined
        )
        fitted, over, kept, tot = fit_text_to_budget(combined, budget)
        if over:
            # Single-level reduce by design; an extreme corpus can still overflow
            # the combined notes — clip WITH a notice rather than fail or hide it.
            _emit(
                f"Combined part notes still exceed the window: kept the first "
                f"{kept} of ~{tot} tokens for the final synthesis."
            )
            combined = fitted

        _emit(f"Synthesising the final summary from {n} part notes…")
        hb_label[0] = "Final synthesis"
        hb_last[0] = _time.monotonic()
        gen = _single_pass_stream(
            text=combined,
            user_template=user_template,
            model=model,
            sys_p=sys_p,
            doc_type=doc_type,
            focus_question=focus_question,
            temperature=temperature,
            max_tokens=max_tokens,
            num_ctx=num_ctx,
            top_p=top_p,
            repeat_penalty=repeat_penalty,
            provider_name=provider_name,
            info_cb=info_cb,
        )
        try:
            for token in gen:
                if _stopped():
                    return
                hb_last[0] = _time.monotonic()
                yield token
        finally:
            gen.close()
    finally:
        hb_stop.set()


def _single_pass_stream(
    *,
    text: str,
    user_template: str,
    model: str,
    sys_p: str,
    doc_type: str,
    focus_question: str,
    temperature: float,
    max_tokens: Optional[int],
    num_ctx: int,
    top_p: float,
    repeat_penalty: float,
    provider_name: str,
    info_cb=None,
) -> Generator[str, None, None]:
    """One summarisation call over *text* (already known to fit the window).

    Dispatch is by provider family: online providers route to
    ``_stream_online`` (which owns fallback); local providers stream directly
    with backend-specific generation kwargs — Ollama maps
    ``max_tokens``→``num_predict`` and forwards ``num_ctx`` /
    ``repeat_penalty``, while LM Studio takes ``max_tokens`` and its
    OpenAI-shaped stream is unwrapped from ``choices[0].delta.content`` (vs.
    Ollama's ``message.content``).
    """
    prompt = build_prompt(text, user_template, doc_type, focus_question)

    if is_online(provider_name):
        yield from _stream_online(
            prompt=prompt,
            system_prompt=sys_p,
            provider_name=provider_name,
            model=model,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            info_cb=info_cb,
        )
        return

    provider = get_provider(provider_name)

    kwargs = {
        "temperature": temperature,
        "top_p": top_p,
    }
    if provider_name == "ollama":
        kwargs["num_ctx"] = num_ctx
        kwargs["repeat_penalty"] = repeat_penalty
        if max_tokens:
            kwargs["num_predict"] = max_tokens
    elif max_tokens:
        kwargs["max_tokens"] = max_tokens

    # Stall floor: /api/summarise streams synchronously in the request
    # generator with no consumer-side stall guard (unlike vault/plainchat/deck),
    # so a connected-but-wedged local model could hang the worker thread until
    # the browser aborts. Pass an explicit per-read timeout — the user-set
    # local_request_timeout_s when positive, else the floor — so a silent
    # backend trips after PAPER_LOCAL_STALL_TIMEOUT_S of no tokens.
    stall_timeout = local_request_timeout() or PAPER_LOCAL_STALL_TIMEOUT_S

    # Prompt Hub capture (local bypass): the local single-paper path sends the
    # system prompt via ``provider.stream_chat``, NOT through core.llm.factory,
    # so the factory capture seam never sees it — record it directly here.
    from core import prompt_capture

    prompt_capture.record(
        "paper_summary", sys_p, provider=provider_name, model=model,
        query=user_template,
    )

    stream = provider.stream_chat(
        model=model,
        prompt=prompt,
        system_prompt=sys_p,
        request_timeout=stall_timeout,
        **kwargs
    )

    # Usage visibility (axis vi): the local single-paper path bypasses the
    # core.llm adapter seam (direct provider.stream_chat), so record the call
    # ourselves so it shows up in /api/usage. Local models are unpriced ($0),
    # so this is a visibility record, not a cost one. Tokens are estimated;
    # accumulate the answer for the output estimate and record in a finally so
    # an aborted/failed stream still logs. Coverage is complete for the local
    # paper path by construction: _map_reduce_stream routes every map part AND
    # the final reduce through this same function, so each model call gets its
    # own usage row (N+1 rows for an N-part map-reduce — intentional; each is
    # a real inference).
    import time as _time
    from core.llm.usage import record_local_usage
    _start = _time.monotonic()
    _out_parts: list[str] = []
    _ok = True
    _err = ""
    try:
        if provider_name == "lm_studio":
            for chunk in stream:
                # Guard against keep-alive / malformed chunks (empty choices, or a
                # delta carrying no content) so a benign frame can't raise mid-stream.
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                content = getattr(delta, "content", None) if delta is not None else None
                if content:
                    _out_parts.append(content)
                    yield content
        else:
            # Ollama
            for chunk in stream:
                message = getattr(chunk, "message", None)
                content = getattr(message, "content", None) if message is not None else None
                if content:
                    _out_parts.append(content)
                    yield content
    except GeneratorExit:
        # Client abort (consumer closed the generator): still recorded, but as
        # a labelled failure — an aborted answer must not look like a
        # completed one in /api/usage.
        _ok = False
        _err = "aborted"
        raise
    except Exception:
        _ok = False
        _err = "stream_error"
        raise
    finally:
        record_local_usage(
            provider_name, model,
            input_text=(sys_p or "") + "\n" + prompt,
            output_text="".join(_out_parts),
            latency_ms=int((_time.monotonic() - _start) * 1000),
            stream=True,
            success=_ok,
            error_category="" if _ok else _err,
            # Tag the local path like the online one so the route's per-answer
            # usage frame (records_since(workflow="paper_summary")) sees it.
            workflow="paper_summary",
        )


def _stream_online(
    *,
    prompt: str,
    system_prompt: str,
    provider_name: str,
    model: str,
    temperature: float,
    top_p: float,
    max_tokens: Optional[int],
    info_cb=None,
) -> Generator[str, None, None]:
    """Stream tokens through an online LLMProvider, with optional fallback."""
    # Builds one ``LLMRequest`` from the prompt + generation knobs (timeout and
    # default ``max_tokens`` resolved from the ``online_*`` config keys), streams
    # it through the primary provider, and on an ``LLMError`` retries on the
    # policy-configured fallback **only before the first token has streamed**
    # (``yielded_any``) — re-streaming after partial output would duplicate the
    # answer, so it re-raises and lets the route emit a structured error.  The
    # fallback request re-resolves the model name for the fallback provider
    # (``resolve_chat_model``).  This single-request / pre-first-token-fallback
    # shape is the template later reused by
    # ``rag/engine.py::_OnlineStreamingResponse`` and the RAG-free
    # ``core/llm/chat.py`` Plain Chat helper.
    cfg = load_config()
    timeout_s = float(cfg.get("online_timeout_s", 60) or 60)
    effective_max_tokens = max_tokens or int(cfg.get("online_max_tokens", 4096) or 4096)

    policy = parse_policy_from_config(cfg, primary_override=provider_name)
    request = LLMRequest(
        model=model,
        system_prompt=system_prompt,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        top_p=top_p,
        max_tokens=effective_max_tokens,
        timeout_s=timeout_s,
        # Prompt Hub tag — the online summary shares the factory capture seam.
        workflow="paper_summary",
    )

    def _emit(msg: str) -> None:
        if info_cb is not None:
            try:
                info_cb(msg)
            except Exception:
                logger.debug("info_cb failed", exc_info=True)

    def on_fallback(cat: str, fallback: str) -> None:
        _emit(f"primary provider {provider_name} failed ({cat}); falling back to {fallback}")

    from core.llm.factory import stream_with_fallback
    yield from stream_with_fallback(
        provider_name=provider_name,
        request=request,
        policy=policy,
        cfg=cfg,
        on_fallback=on_fallback,
        log_context="online summary fallback"
    )
