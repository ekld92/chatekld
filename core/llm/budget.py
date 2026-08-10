"""Context-window budgeting helpers (WP-C1/C2/C5).

Ollama silently truncates any prompt that exceeds the request's ``num_ctx``
(default ~4096) — no error, no flag, just a clipped prompt (ollama/ollama
#8099, #4967). The only defence is client-side: estimate the prompt's tokens,
reserve output headroom, and size ``num_ctx`` per request. LM Studio has no
per-request knob at all, so there the same estimate drives *input trimming*
instead (``fit_text_to_budget``), replacing the old hardcoded 2800-token clip.

Estimates use tiktoken's cl100k when available (via
:func:`core.llm.usage.estimate_tokens`) — local tokenizers differ, so a
safety margin is applied on top. Everything here is pure and stdlib-only
besides that reuse.
"""
import math

from core.llm.usage import estimate_tokens

# Local model tokenizers (llama/qwen/gemma) differ from cl100k by up to ~10%;
# over-estimating is safe (a slightly larger num_ctx / smaller input budget),
# under-estimating silently truncates.
SAFETY_MARGIN = 1.15

# Fixed allowance for chat-template scaffolding (role markers, tool-schema
# JSON envelopes, BOS/EOS) that the plain-text estimate can't see.
PROMPT_OVERHEAD_TOKENS = 256

# Output reserve when the caller sets no max_tokens (matches the
# online_max_tokens default).
DEFAULT_OUTPUT_RESERVE = 4096

# num_ctx values are quantised to this ladder: every distinct num_ctx value
# makes Ollama reload the model, so a continuous per-request size would
# thrash reloads. Doubling rungs keep at most ~6 distinct loads.
NUM_CTX_LADDER = (4096, 8192, 16384, 32768, 65536, 131072)


def estimate_prompt_tokens(*parts) -> int:
    """Safety-adjusted token estimate for a prompt built from *parts*.

    Non-string parts are ``str()``-ed (message dicts, tool payloads); falsy
    parts are skipped.
    """
    total = 0
    for part in parts:
        if not part:
            continue
        if not isinstance(part, str):
            part = str(part)
        total += estimate_tokens(part)
    return int(math.ceil(total * SAFETY_MARGIN)) + PROMPT_OVERHEAD_TOKENS


def pick_num_ctx(prompt_tokens: int, max_output_tokens, ceiling) -> int:
    """Smallest ladder rung that fits prompt + output, capped at *ceiling*.

    *ceiling* is the user's ``context_window`` config (what the local model
    was validated to handle); a need beyond it returns the ceiling itself —
    the caller cannot do better without splitting the work.
    """
    reserve = max_output_tokens if max_output_tokens and max_output_tokens > 0 else DEFAULT_OUTPUT_RESERVE
    needed = max(0, int(prompt_tokens)) + int(reserve)
    try:
        ceiling = int(ceiling)
    except (TypeError, ValueError):
        ceiling = 0
    if ceiling <= 0:
        ceiling = NUM_CTX_LADDER[-1]
    ceiling = max(ceiling, NUM_CTX_LADDER[0])
    for rung in NUM_CTX_LADDER:
        if rung >= needed:
            return min(rung, ceiling)
    return ceiling


def document_budget_tokens(context_window, output_reserve=None) -> int:
    """How many raw cl100k tokens of *document text* fit in *context_window*.

    Subtracts the output reserve and prompt overhead, then divides out the
    safety margin (the budget is compared against un-margined tiktoken
    counts). Never below a small floor so a pathological config still sends
    something.
    """
    reserve = output_reserve if output_reserve and output_reserve > 0 else DEFAULT_OUTPUT_RESERVE
    try:
        window = int(context_window)
    except (TypeError, ValueError):
        window = 0
    if window <= 0:
        window = 32768
    usable = window - reserve - PROMPT_OVERHEAD_TOKENS
    return max(512, int(usable / SAFETY_MARGIN))


def count_text_tokens(text: str) -> int:
    """Raw cl100k token count of *text* (no margin); 4-chars-per-token fallback.

    The measure-only half of :func:`fit_text_to_budget` for callers that need
    the fit DECISION but not the trimmed text — the summariser's over-budget
    branch used to decode a budget-sized string just to discard it (map-reduce
    re-splits the original text itself), a pure allocation waste on the very
    documents where memory matters most.
    """
    if not text:
        return 0
    try:
        from core.llm.usage import _cl100k_encoding

        return len(_cl100k_encoding().encode(text))
    except Exception:
        return max(1, len(text) // 4)


def fit_text_to_budget(text: str, budget_tokens: int):
    """Trim *text* to *budget_tokens* cl100k tokens on a token boundary.

    Returns ``(fitted_text, truncated, kept_tokens, total_tokens)`` so the
    caller can SAY that (and how much) was dropped — the silent-clip
    anti-pattern is what this module exists to remove. Falls back to a
    4-chars-per-token character slice when tiktoken is unavailable.
    """
    if not text:
        return text, False, 0, 0
    if budget_tokens <= 0:
        return text, False, 0, 0
    try:
        from core.llm.usage import _cl100k_encoding

        enc = _cl100k_encoding()
        tokens = enc.encode(text)
        total = len(tokens)
        if total <= budget_tokens:
            return text, False, total, total
        return enc.decode(tokens[:budget_tokens]), True, budget_tokens, total
    except Exception:
        total = max(1, len(text) // 4)
        if total <= budget_tokens:
            return text, False, total, total
        return text[: budget_tokens * 4], True, budget_tokens, total
