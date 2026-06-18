import logging
import re
from typing import Generator, Optional
from core.providers import get_provider
from core.config import load_config
from core.llm.factory import get_llm_provider, is_online
from core.llm.policy import parse_policy_from_config
from core.llm.prompt import build_summary_user_message
from core.llm.types import LLMError, LLMRequest

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

    stream = provider.stream_chat(
        model=model,
        prompt=prompt,
        system_prompt=sys_p,
        **kwargs
    )

    if provider_name == "lm_studio":
        for chunk in stream:
            content = chunk.choices[0].delta.content
            if content:
                yield content
    else:
        # Ollama
        for chunk in stream:
            content = chunk.message.content
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
    from core.config import resolve_chat_model
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
    )

    def _emit(msg: str) -> None:
        if info_cb is not None:
            try:
                info_cb(msg)
            except Exception:
                logger.debug("info_cb failed", exc_info=True)

    primary = get_llm_provider(provider_name, cfg=cfg)
    yielded_any = False
    try:
        stream = primary.stream(request)
        for token in stream.response_gen:
            yielded_any = True
            yield token
        return
    except LLMError as err:
        # Only fall back *before* the first token reaches the client.  Once
        # ≥1 token has streamed, re-streaming through the fallback would
        # duplicate the answer, so re-raise and let the route surface a
        # structured error after the partial output.
        if yielded_any:
            raise
        if not policy.should_fall_back(err) or policy.fallback is None:
            raise
        _emit(f"primary provider {provider_name} failed ({err.category.value}); falling back to {policy.fallback}")
        logger.warning(
            "online summary fallback %s -> %s: %s",
            provider_name,
            policy.fallback,
            err.message,
        )

    fallback = get_llm_provider(policy.fallback, cfg=cfg)
    fallback_request = LLMRequest(
        model=resolve_chat_model(cfg, policy.fallback),
        system_prompt=system_prompt,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        top_p=top_p,
        max_tokens=effective_max_tokens,
        timeout_s=timeout_s,
    )
    yield from fallback.stream(fallback_request).response_gen
