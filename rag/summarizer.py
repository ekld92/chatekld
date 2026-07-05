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
    ``max_tokens`` and gets an extra ~2800-token input trim because its context
    is smaller).
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

def _truncate_at_references(text: str) -> str:
    """Strip bibliography/reference sections to save tokens."""
    ref_markers = [
        r"\nReferences\n", r"\nBibliography\n", r"\nWorks Cited\n",
        r"\nREFERENCES\n", r"\nBIBLIOGRAPHY\n"
    ]
    for marker in ref_markers:
        parts = re.split(marker, text, flags=re.IGNORECASE)
        if len(parts) > 1:
            text = parts[0]
            break
    return text

def _truncate_to_token_budget(text: str, token_budget: int) -> str:
    """Hard-cap *text* to ``token_budget`` cl100k tokens (no-op when ≤ 0).

    Used to keep a long document inside a small-context backend's window
    (LM Studio). Tokenizes with tiktoken's ``cl100k_base`` and decodes the head
    slice so the cut lands on a token boundary; if tiktoken is unavailable it
    degrades to a coarse ``token_budget * 4`` character slice (≈4 chars/token).
    """
    if token_budget <= 0:
        return text
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        tokens = enc.encode(text)
        if len(tokens) <= token_budget:
            return text
        return enc.decode(tokens[:token_budget])
    except Exception:
        approx_chars = token_budget * 4
        return text[:approx_chars]

def build_prompt(text: str, user_template: str, doc_type: str, focus_question: str = "") -> str:
    """Inject extracted text into either structured or legacy prompt presets."""
    return build_summary_user_message(
        document_text=text,
        user_template=user_template,
        doc_type=doc_type,
        focus_question=focus_question,
    )

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
) -> Generator[str, None, None]:
    """Stream summary tokens via the active LLM provider."""
    # Pipeline: strip the bibliography (``_truncate_at_references``, saves tokens
    # on the part of a paper that rarely informs a summary), apply the LM Studio
    # input cap, build the user message from ``user_template`` + ``doc_type`` +
    # ``focus_question``, and assemble the system prompt (base +
    # ``audience_modifier``, plus a "Write in {language}." suffix for non-English
    # output).  Dispatch is by provider family: online providers route to
    # ``_stream_online`` (which owns fallback); local providers stream directly
    # with backend-specific generation kwargs — Ollama maps
    # ``max_tokens``→``num_predict`` and forwards ``num_ctx`` / ``repeat_penalty``,
    # while LM Studio takes ``max_tokens`` and its OpenAI-shaped stream is
    # unwrapped from ``choices[0].delta.content`` (vs. Ollama's
    # ``message.content``).  ``info_cb`` receives stage/fallback notices.
    text = _truncate_at_references(text)
    if provider_name == "lm_studio":
        text = _truncate_to_token_budget(text, 2800)

    if user_template is None:
        user_template = "Summarize the key findings."

    prompt = build_prompt(text, user_template, doc_type, focus_question)

    sys_p = system_prompt + audience_modifier
    if language and language != "English":
        sys_p += f" Write in {language}."

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
                yield content
    else:
        # Ollama
        for chunk in stream:
            message = getattr(chunk, "message", None)
            content = getattr(message, "content", None) if message is not None else None
            if content:
                yield content


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
