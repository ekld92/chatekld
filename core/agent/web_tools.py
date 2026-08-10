"""Optional ``web_search`` tool for the vault agent (2026-07-22, plan Batch C).

One provider-agnostic client-side tool backed by the Tavily search API
(https://api.tavily.com) — deliberately NOT the providers' native server-side
web-search tools: a client-side tool is provider-uniform (one implementation
for OpenAI/Anthropic/Google/DeepSeek/Z.AI), needs no per-adapter wire-format
work, and — decisively — its results flow through the loop's ``wrap_untrusted``
nonce'd guard exactly like vault content, whereas server-side results would
bypass that injection defense entirely. Native server tools (Anthropic
web_search, Gemini grounding, OpenAI Responses) remain a possible v2 —
recorded in CLAUDE.md's Agent Mode section (the plan doc that first recorded
it shipped and was deleted 2026-07-23).

Gating (`web_search_available`): the feature is OFF unless all three hold —
the ``vault_web_search_enabled`` config knob is on, the active chat provider
is ONLINE (maintainer decision 2026-07-22: local models don't get the tool for
now), and ``TAVILY_API_KEY`` is present in the environment. The key follows
the provider-key discipline: env-only at call time, never persisted, never
returned by an API, redacted from logs (``core/llm/redact.py`` knows the
``tvly-`` shape).

Egress note: this is a deliberate, documented exception to the app's
"external HTTP = LLM providers + Zotero" posture — one keyed, opt-in,
loopback-independent search endpoint.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from core.agent.tools import ToolSpec
from core.llm.types import ToolSchema

API_KEY_ENV = "TAVILY_API_KEY"
_TAVILY_ENDPOINT = "https://api.tavily.com/search"

# Per-call bounds. The HTTP timeout is additionally clipped to the turn's
# remaining wall-clock budget (a search must never outlive its agent turn).
_HTTP_TIMEOUT_S = 20.0
_MIN_BUDGET_S = 5.0
_SNIPPET_CHARS = 1200
_MAX_RESULTS_CEILING = 8
_QUERY_MAX_CHARS = 400
# Read cap BEFORE json parsing (2026-07-23 audit): the per-result snippet caps
# only applied after a full-body read+parse, so a hostile/huge response from
# the (fixed, keyed) endpoint could buffer unbounded memory. 8 results × ~a few
# KB each fits in a fraction of this; anything larger is not a legal reply.
_MAX_BODY_BYTES = 2_000_000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse ALL redirects (2026-07-23 audit): the default handler carries
    the Authorization header to the Location target — and permits an
    https→http downgrade — so a 3xx from a compromised endpoint could
    exfiltrate the Bearer key. The API never legitimately redirects; a 3xx
    surfaces as the same status-only HTTPError as any other non-200."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


@dataclass(frozen=True)
class WebToolContext:
    """Per-turn config for the web tool (mirrors ``VaultToolContext``)."""
    max_results: int = 5
    deadline_monotonic_s: Optional[float] = None


def api_key_from_env() -> str:
    """The Tavily key, read from the environment at call time (never stored)."""
    return (os.environ.get(API_KEY_ENV) or "").strip()


def web_search_available(cfg: dict, provider_name: str,
                         enabled_override: bool | None = None) -> tuple[bool, str]:
    """Return ``(available, reason_when_not)`` for the web tool's triple gate.

    ``enabled_override`` is the caller's already-resolved body-override value
    (2026-07-23 audit): without it, this gate re-read the CONFIG knob and a
    body ``web_search_enabled: true`` sent before the debounced Settings save
    landed was silently ignored — the documented body override only worked in
    the disable direction.
    """
    enabled = (enabled_override if enabled_override is not None
               else bool(cfg.get("vault_web_search_enabled", False)))
    if not enabled:
        return False, "disabled"
    from core.config import is_online_provider
    if not is_online_provider(provider_name):
        return False, "local_provider"
    if not api_key_from_env():
        return False, "no_key"
    return True, ""


_WEB_SEARCH_SCHEMA = ToolSchema(
    name="web_search",
    description=(
        "Search the public web for CURRENT or external information the vault "
        "cannot contain (recent publications, guidelines, news, definitions). "
        "Returns result titles, URLs, and content snippets. Prefer "
        "vault_search for anything about the user's own notes; use this only "
        "when fresh or outside knowledge is genuinely needed, and cite the "
        "returned URLs in your answer."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Web search query.",
            },
            "max_results": {
                "type": "integer",
                "description": "How many results to return (1-8).",
                "minimum": 1,
                "maximum": _MAX_RESULTS_CEILING,
            },
        },
        "required": ["query"],
    },
)


def _http_search(query: str, max_results: int, api_key: str, timeout_s: float) -> dict:
    """One Tavily POST. Split out so tests can patch it without network."""
    payload = json.dumps({
        "query": query,
        "max_results": max_results,
        # Snippet-oriented content, no LLM-generated answer (we only want
        # source material; synthesis is the agent's job).
        "include_answer": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        _TAVILY_ENDPOINT,
        data=payload,
        headers={
            "Content-Type": "application/json",
            # Header, not body: keeps the key out of any payload logging.
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    # _OPENER (not urlopen): redirects refused — see _NoRedirect.
    with _OPENER.open(req, timeout=timeout_s) as resp:
        body = resp.read(_MAX_BODY_BYTES + 1)
        if len(body) > _MAX_BODY_BYTES:
            raise RuntimeError("Web search response exceeded the size cap.")
        return json.loads(body.decode("utf-8"))


def build_web_tools(ctx: WebToolContext) -> list[ToolSpec]:
    """The (single-element) web tool list, bound to *ctx*.

    The runner returns a JSON string; the loop truncates it and wraps it in
    the nonce'd untrusted-content guard — web pages are the canonical
    prompt-injection vector, so the runner must never pre-trust or re-format
    results beyond snipping. Failures raise, which the loop converts into an
    ``is_error`` observation (also wrapped) that the model can recover from.
    """
    def _run(args: dict) -> str:
        query = str(args["query"] or "").strip()[:_QUERY_MAX_CHARS]
        if not query:
            raise ValueError("web_search needs a non-empty query.")
        cap = max(1, min(int(ctx.max_results or 5), _MAX_RESULTS_CEILING))
        max_results = max(1, min(int(args.get("max_results", cap)), cap))
        api_key = api_key_from_env()
        if not api_key:
            raise RuntimeError(
                f"Web search is unavailable: {API_KEY_ENV} is not set.")
        timeout_s = _HTTP_TIMEOUT_S
        if ctx.deadline_monotonic_s is not None:
            remaining = ctx.deadline_monotonic_s - time.monotonic()
            if remaining < _MIN_BUDGET_S:
                raise RuntimeError(
                    "Not enough time left in this turn for a web search.")
            timeout_s = min(timeout_s, remaining)
        try:
            data = _http_search(query, max_results, api_key, timeout_s)
        except urllib.error.HTTPError as exc:
            # Never echo the response body (attacker-influenced); status only.
            raise RuntimeError(f"Web search failed (HTTP {exc.code}).") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"Web search failed: {exc.__class__.__name__}.") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("Web search returned malformed JSON.") from exc

        raw = data.get("results") if isinstance(data, dict) else None
        results = []
        for i, r in enumerate((raw or [])[:max_results], start=1):
            if not isinstance(r, dict):
                continue
            snippet = str(r.get("content") or "")
            truncated = len(snippet) > _SNIPPET_CHARS
            results.append({
                "index": i,
                "title": str(r.get("title") or "")[:300],
                "url": str(r.get("url") or "")[:500],
                "snippet": snippet[:_SNIPPET_CHARS] + (" ..." if truncated else ""),
            })
        return json.dumps({
            "results": results,
            "result_count": len(results),
        }, ensure_ascii=False)

    return [ToolSpec(schema=_WEB_SEARCH_SCHEMA, runner=_run, max_output_chars=12000)]
