# LLM Provider Layer — Deep Notes

Loaded on demand when working under `core/llm/`. The cross-cutting provider invariants (online = chat-only, embeddings local, API keys env-only, per-provider model keys) live in the root `CLAUDE.md`.

## Local adapters

- Ollama resolves bare model names to installed tags (`OllamaProvider.resolve_model`). LM Studio uses raw model IDs from config and `/v1/models`.
- `_LMStudioOpenAI.metadata` (in `core/providers/lms.py`) forces `is_chat_model=True`. LlamaIndex's upstream `is_chat_model()` helper only recognises OpenAI model name patterns, so without the override any non-OpenAI-named LM Studio model (e.g. `google/gemma-4-e4b`) gets routed to the legacy `/v1/completions` endpoint, which LM Studio answers with an empty stream and surfaces as a spurious "No relevant content found" in vault chat.
- The `local` adapter in `core/llm/adapters/local.py` wraps the existing `core.providers.Provider` so the legacy stream-chat path keeps working; on the tool branch it bypasses the flatten-to-prompt path and calls `ollama.chat()` / LM Studio's OpenAI client directly with structured `messages` + `tools`.

## Fallback policy

- Policy comes from `fallback_provider` + `fallback_on` config keys (default `["timeout", "network", "rate_limit", "server_error"]`). Non-transient errors (`auth`, `invalid_request`, `quota`) always surface immediately.
- Hard quota / billing exhaustion is detected by `core.llm.base.looks_like_quota` (OpenAI `insufficient_quota`, Anthropic "credit balance", billing strings) and mapped to the terminal `ErrorCategory.QUOTA` — kept out of `fallback_on` and non-retryable — so it is not confused with a transient `rate_limit` (e.g. a Gemini per-minute 429 stays retryable).
- The policy is built by `core.llm.policy.parse_policy_from_config(cfg, primary_override=...)` and used by both `rag.summarizer._stream_online` and `rag.engine._OnlineStreamingResponse._stream`. Both streaming sites only fall back **before the first token**; a mid-stream failure after ≥1 token re-raises (the route emits a structured SSE error) rather than re-streaming the whole answer.
- Online generation params: `online_timeout_s` (default 60), `online_max_retries` (default 3, exponential backoff with jitter via `core/llm/retry.py`), `online_max_tokens` (default 4096 — applied when a route does not set max_tokens explicitly).
- Local request timeout: the `local` adapter does not honour `LLMRequest.timeout_s`; instead the underlying `OllamaProvider`/`LMStudioProvider` read `local_request_timeout_s` via `core.providers.base.local_request_timeout()` and apply it to their ollama/openai clients + LlamaIndex LLMs (0 = leave SDK default). This is the only timeout lever for local generation — the wall-clock cap (`agent_wall_clock_s`, in the route layer) is the coarse backstop above it. See the timeout-chain note in the root `CLAUDE.md` Limits section.

## Usage / cost tracking

- Token / USD-cost usage is recorded by `core.llm.usage.usage_tracker` for every request (online and local). The tracker writes append-only JSONL to `BASE_DIR/llm_usage.jsonl` and exposes a window-scoped summary on `/api/usage`. Parsed disk records are cached by the file's `(size, mtime_ns)` so the UI poll does not re-parse the whole log; usage records carry a per-record `uid` so the in-memory ring and on-disk JSONL never double- or under-count.
- Pricing per million tokens is in `core/llm/usage.py::PRICING_TABLE` and is overridable via the `llm_pricing_overrides` config key. Anthropic figures were verified against platform.claude.com 2026-05 (the Opus tier is $5/$25 since Opus 4.5 — NOT the Claude-3-era $15/$75); unknown models cost out at 0.0 rather than a fabricated number.
- `CURATED_MODELS` in each online adapter must list only ACTIVE models — retired IDs 404 at the provider. The Claude 3.x IDs were removed 2026-06 for this reason.

## Tool wire format (agent mode)

Each adapter translates the agent's provider-agnostic `LLMRequest.tools` / `tool_history` into its native dialect via helpers in `core/llm/tool_schema.py` (`build_openai_messages` / `build_anthropic_messages` / `build_gemini_contents` plus per-provider `jsonschema_to_*` and `parse_*` parsers). The Anthropic adapter rebuilds `tool_use` / `tool_result` content blocks; Google uses `functionCall` / `functionResponse` parts and synthesises IDs (Gemini ties responses by name, not ID). Adapters expose `supports_tool_use()` and route structured tool calls only when `LLMRequest.tools` is non-empty; the empty default keeps every existing caller on the original code path.
