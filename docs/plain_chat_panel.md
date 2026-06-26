# Plain Chat Panel — Implementation Plan

> Status: **planned, not yet built** (design dated 2026-06-22). Feasibility assessed against
> the live codebase; no app code has been modified. Captures the locked design decisions so
> implementation can start from a fixed spec.

A new ChatEKLD panel: a simple **multi-turn chat with the configured LLM, no RAG**. The user
talks to the active provider/model directly; conversation context carries across turns. No
vault retrieval, no agent loop, no tools.

---

## Key finding that de-risks multi-turn

The unified `core/llm` layer already supports multi-turn natively. `LLMRequest.messages` is a
`list[dict]`, online adapters pass it through as native message arrays, and the **local**
adapter (`core/llm/adapters/local.py:418-427`) flattens the full history into a single prompt
with `[assistant]`-style role tags. So passing a full conversation array works for Ollama,
LM Studio, OpenAI, Anthropic, and Google with **zero new model-layer code**. The only things
missing are a thin streaming helper, a route, and a UI panel.

---

## Design decisions (locked — defaults flagged at the end)

1. **Stateless server, client owns history.** The browser keeps the message array and sends
   the full (capped) history on every turn. No DB, no session state — matches the existing
   per-request architecture. Conversation is ephemeral (lost on reload), like vault chat.
2. **Reuse the globally-configured provider + model** (`resolve_chat_model(cfg, provider)`),
   same as vault chat. No per-panel model selector in the MVP.
3. **Minimal knobs:** a `chat_system_prompt` and `chat_temperature` only, persisted as config
   defaults; no inline UI panel (consistent with the post-refactor "knobs live in Settings"
   convention).
4. **Unified streaming path** through `get_llm_provider().stream()` for both local and online
   (not the summariser's local/online split), with the same "fall back only before the first
   token" policy.

---

## Data flow

```
plainchat.js  ──POST /api/plainchat {messages:[...], system_prompt?, temperature?}──▶  api/routes/plainchat.py
   (keeps full          SSE: {info}/{token}/{error} ... [DONE]                          │
    history array) ◀──────────────────────────────────────────────────────────────────┤
                                                                                        ▼
                                                          core/llm/chat.py::stream_chat_messages()
                                                          → get_llm_provider(provider).stream(LLMRequest(messages=...))
                                                          → fallback before first token (policy)
                                                          → usage_tracker fires automatically inside adapter
```

---

## Backend changes

### 1. New helper: `core/llm/chat.py` (~50 lines)

A RAG-free streaming function, modelled on `rag/summarizer.py::_stream_online` (lines 127-197)
but unified for local+online (so it's hermetically testable with a mocked provider):

```python
def stream_chat_messages(*, messages, system_prompt, provider_name, model,
                         temperature, max_tokens=None, cfg=None, info_cb=None) -> Generator[str, None, None]:
    cfg = cfg or load_config()
    policy = parse_policy_from_config(cfg, primary_override=provider_name)
    request = LLMRequest(model=model, system_prompt=system_prompt, messages=messages,
                         temperature=temperature, max_tokens=max_tokens,
                         timeout_s=float(cfg.get("online_timeout_s", 60) or 60))
    primary = get_llm_provider(provider_name, cfg=cfg)
    yielded = False
    try:
        for tok in primary.stream(request).response_gen:
            yielded = True; yield tok
        return
    except LLMError as err:
        if yielded or not policy.should_fall_back(err) or policy.fallback is None:
            raise
        # emit info, build fallback_request with resolve_chat_model(cfg, policy.fallback), stream it
```

Usage tracking and cost are automatic inside the adapter — no extra wiring.

### 2. New blueprint: `api/routes/plainchat.py` (~90 lines)

- Route `POST /api/plainchat`, opens with the standard `if not origin_is_local(): return 403`
  guard (`api/security.py`).
- **Input validation** (the part that doesn't exist yet):
  - `messages` must be a list of `{role ∈ {user,assistant}, content:str}`; coerce/reject
    malformed entries.
  - Cap each `content` via `coerce_string_max_len` and cap the array to the last **N=20**
    messages (backend guard even though the client also caps).
  - `system_prompt` capped at `SYSTEM_PROMPT_LIMIT` (4000, `core/constants.py:115`).
  - `temperature` via `coerce_float_in_range(…, 0.0, 2.0)` → falls back to persisted
    `chat_temperature` (`api/validators.py`).
- **SSE generator:** copy the queue + daemon-worker + consumer skeleton from
  `api/routes/vault.py:345-551`, stripped of all agent/iteration/tool branches — only
  `{info}`, `{token}`, `{error}` and `[DONE]`. Reuse the existing consumer-timeout constants
  (`_SINGLE_SHOT_FLOOR_S`, `_STALL_MARGIN_S`) so a slow first token isn't starved. Worker
  calls `stream_chat_messages(...)`.

### 3. Register blueprint — `app.py`

Two lines: `from api.routes.plainchat import plainchat_bp` and
`app.register_blueprint(plainchat_bp)` (near `app.py:24` and `app.py:67`).

### 4. Config defaults — `core/config.py`

Add alongside the `paper_*` block (`core/config.py:229-233`):

```python
"chat_temperature": 0.3,
"chat_system_prompt": "You are a helpful assistant.",
```

### 5. (Optional) Settings validation — `api/routes/config.py`

If exposing the two knobs in the LLM Settings modal, add `chat_temperature` (float 0-2) and
`chat_system_prompt` (string, max 4000) to `_validate_llm_config_keys` so out-of-range values
are dropped, per the documented pattern. Skippable for a barebones MVP.

---

## Frontend changes

### 6. Tab markup — `templates/index.html`

Following the exact pattern at lines 93-97 and 102:

```html
<button id="tab-chat" class="tab" role="tab" aria-selected="false"
        aria-controls="chat-tab" tabindex="-1" onclick="showTab('chat')">Chat</button>
```

```html
<div id="chat-tab" class="content-area" role="tabpanel" aria-labelledby="tab-chat" tabindex="0">
  <!-- card: #plainchat-history, input-bar with #plainchat-input + Send + a "New chat" button -->
</div>
```

### 7. New module — `static/js/plainchat.js` (~120 lines)

- **Imports only `ui.js` + `api.js`** (JS Module Hierarchy rule).
- Holds `let history = []` (the `{role,content}` array). `chatPlain()`:
  1. push `{role:'user', content}`, render the user bubble + an empty bot bubble;
  2. `secureFetch('/api/plainchat', {body: JSON.stringify({messages: history.slice(-20), temperature, system_prompt})})`;
  3. consume with the existing `readSSE()` generator from `api.js` — reuse verbatim, plus an
     `AbortController` timeout from `_chatAbortMs()`-style logic;
  4. accumulate tokens, render markdown via the `marked` + plain-text-fallback approach
     borrowed from `vault.js::_renderAnswer`;
  5. on stream end, push `{role:'assistant', content: fullAnswer}` into `history` and attach a
     copy button.
- `newChat()` clears `history` and the DOM (a "New chat" button).
- Reuse `_showVaultError`-style retry rendering.

### 8. Wiring — `static/js/app.js`

`window.chatPlain = PlainChat.chatPlain; window.plainchatNew = PlainChat.newChat;` and an
optional `PlainChat.init(config)` to seed temperature/system-prompt from config.

---

## Multi-turn specifics & edge cases

- **History assembly:** client sends `history.slice(-20)`; the **assistant turns are
  included**, so the model sees prior context. Backend re-caps to N=20 and per-message length
  as defense-in-depth.
- **Token growth:** the 20-turn cap bounds context; for local Ollama, `num_ctx` is the model's
  configured window — long histories simply truncate at the provider. Acceptable for MVP; a
  token-budget trim can be added later if needed.
- **Mid-stream failure:** if ≥1 token already streamed, no fallback (would duplicate) — surface
  a structured SSE error after the partial, exactly as the summariser does.
- **Abort/timeout:** `AbortController` + the floored consumer timeout; the worker's `cancel`
  event stops token pumping on disconnect.
- **System prompt:** unlike vault chat (where it's a *prefix* over a safety preamble), plain
  chat has no retrieval grounding to protect, so `chat_system_prompt` is used as the **full**
  system prompt — simpler and matches user expectation.

---

## Tests

A new hermetic `test_plainchat.py` (mirroring `test_vault_regressions.py` / `test_deck.py`):

- `stream_chat_messages` with a mocked `LLMProvider` — verifies the full `messages` array
  reaches `LLMRequest`, fallback-before-first-token, and no-fallback-after-first-token.
- Route: 403 without `origin_is_local`; 400 on malformed `messages`; SSE frames + `[DONE]`;
  per-message and array-length caps enforced; `system_prompt` capped at 4000.
- Add `test_plainchat.py` to the pytest line in `CLAUDE.md`, and `api/routes/plainchat.py` +
  `core/llm/chat.py` to the `py_compile` list.

---

## Sequencing & effort

| Step | Files | Est. |
|---|---|---|
| 1. Helper + unit test | `core/llm/chat.py`, `test_plainchat.py` | 1h |
| 2. Route + blueprint + config defaults | `api/routes/plainchat.py`, `app.py`, `core/config.py` | 1.5h |
| 3. UI (tab, module, wiring) | `index.html`, `plainchat.js`, `app.js` | 1.5h |
| 4. Route tests + docs (CLAUDE.md ownership lines, API Routes, SSE Contract) | tests, `CLAUDE.md` | 1h |

**~5h / under a day.** No new dependencies, no schema changes, no RAG/lock interaction.

---

## Decisions defaulted (change any before building)

- **No conversation persistence** (ephemeral, lost on reload). Persisting across restarts would
  mean new storage — out of scope unless wanted.
- **No per-panel model selector** — reuses the global provider/model. Easy to add later.
- **20-turn history cap.** Reasonable bound; adjustable.
- **Two Settings knobs** (`chat_temperature`, `chat_system_prompt`); the Settings-modal wiring
  (step 5) is optional for the MVP.

---

## Reuse map (where each piece comes from)

| New piece | Reuses / mirrors |
|---|---|
| `core/llm/chat.py::stream_chat_messages` | `rag/summarizer.py::_stream_online` (127-197) |
| SSE route skeleton | `api/routes/vault.py:345-551` (agent branches removed) |
| Local-origin guard | `api/security.py::origin_is_local` |
| Input coercion | `api/validators.py` (`coerce_string_max_len`, `coerce_float_in_range`) |
| Blueprint registration | `app.py:24,67` |
| Config defaults | `core/config.py:229-233` (`paper_*` block) |
| Tab markup | `templates/index.html:93-102` |
| SSE consume + secure headers | `static/js/api.js` (`readSSE`, `secureFetch`) |
| Markdown render + error/retry UI | `static/js/vault.js` (`_renderAnswer`, `_showVaultError`) |
| Module wiring | `static/js/app.js` (`window.*` handlers) |
