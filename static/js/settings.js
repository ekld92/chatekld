// Settings window: the single editor for LLM/RAG parameters that previously
// had no UI (online timeout/retries/tokens, fallback policy, reranker
// device/model, vector backend, prewarm) plus the persistence for the Single
// Paper and Deck generation knobs (paper_* / deck_*), which the existing
// summarizer.js / deck.js still read by id and send per-request.
//
// The vault chat knobs, OCR/Vision selects and provider/model selectors live
// inside the same modal but are owned by vault.js / config.js respectively —
// this module deliberately does NOT touch them (no double-save).  Per the JS
// module hierarchy this file imports only api.js (and nothing else from the
// project).
import { secureFetch } from './api.js';

let _saveTimer = null;

const _FALLBACK_CATEGORIES = ['timeout', 'network', 'rate_limit', 'server_error'];

// [control id, config key, 'int' | 'float' | 'string']
const _NUMERIC_FIELDS = [
    ['set-online-timeout', 'online_timeout_s', 'int'],
    ['set-online-retries', 'online_max_retries', 'int'],
    ['set-online-max-tokens', 'online_max_tokens', 'int'],
    ['set-agent-wall-clock', 'agent_wall_clock_s', 'int'],
    ['set-local-timeout', 'local_request_timeout_s', 'int'],
    ['set-reranker-model', 'vault_reranker_model', 'string'],
    ['doc-ctx', 'paper_num_ctx', 'int'],
    ['doc-predict', 'paper_max_tokens', 'int'],
    ['doc-temp', 'paper_temperature', 'float'],
    ['doc-top-p', 'paper_top_p', 'float'],
    ['doc-repeat-penalty', 'paper_repeat_penalty', 'float'],
    ['deck-max-sections', 'deck_max_sections', 'int'],
    ['deck-agent-iters', 'deck_agent_max_iterations', 'int'],
    ['deck-temp', 'deck_temperature', 'float'],
];
const _SELECT_FIELDS = [
    ['set-reranker-device', 'vault_reranker_device'],
    ['set-vector-backend', 'vault_vector_backend'],
    ['set-fallback-provider', 'fallback_provider'],
];
const _BOOL_FIELDS = [
    ['set-prewarm-enabled', 'vault_prewarm_enabled'],
];

// Populate the settings-owned controls from /api/config and wire auto-save.
// Receives the already-fetched config object from app.js init (no extra GET).
export function initSettings(cfg) {
    cfg = cfg || {};
    _setVal('set-online-timeout', cfg.online_timeout_s ?? 60);
    _setVal('set-online-retries', cfg.online_max_retries ?? 3);
    _setVal('set-online-max-tokens', cfg.online_max_tokens ?? 4096);
    _setVal('set-agent-wall-clock', cfg.agent_wall_clock_s ?? 300);
    _setVal('set-local-timeout', cfg.local_request_timeout_s ?? 0);
    _setVal('set-reranker-model', cfg.vault_reranker_model ?? '');
    _setVal('doc-ctx', cfg.paper_num_ctx ?? 32768);
    _setVal('doc-predict', cfg.paper_max_tokens ?? 4096);
    _setVal('doc-temp', cfg.paper_temperature ?? 0.3);
    _setVal('doc-top-p', cfg.paper_top_p ?? 0.9);
    _setVal('doc-repeat-penalty', cfg.paper_repeat_penalty ?? 1.1);
    _setVal('deck-max-sections', cfg.deck_max_sections ?? 8);
    _setVal('deck-agent-iters', cfg.deck_agent_max_iterations ?? 6);
    _setVal('deck-temp', cfg.deck_temperature ?? 0.3);

    _setSelectVal('set-reranker-device', cfg.vault_reranker_device ?? 'auto');
    _setSelectVal('set-vector-backend', cfg.vault_vector_backend ?? 'simple');
    _setSelectVal('set-fallback-provider', cfg.fallback_provider ?? '');
    _setCheck('set-prewarm-enabled', cfg.vault_prewarm_enabled ?? true);

    const fo = Array.isArray(cfg.fallback_on) ? cfg.fallback_on : [];
    for (const cat of _FALLBACK_CATEGORIES) {
        _setCheck(`set-fallback-on-${cat}`, fo.includes(cat));
    }
    _wire();
}

function _wire() {
    const debounce = () => {
        if (_saveTimer) clearTimeout(_saveTimer);
        _saveTimer = setTimeout(saveSettings, 400);
    };
    const ids = [
        ..._NUMERIC_FIELDS.map((f) => f[0]),
        ..._SELECT_FIELDS.map((f) => f[0]),
        ..._BOOL_FIELDS.map((f) => f[0]),
        ..._FALLBACK_CATEGORIES.map((c) => `set-fallback-on-${c}`),
    ];
    for (const id of ids) {
        const el = document.getElementById(id);
        if (!el) continue;
        el.addEventListener('change', debounce);
        if (el.type === 'range' || el.type === 'number' || el.type === 'text') {
            el.addEventListener('input', debounce);
        }
    }
}

// Gather every settings-owned control and persist to /api/config.  The server
// re-validates/clamps each key (api/routes/config.py::_validate_llm_config_keys),
// so a malformed value is dropped server-side rather than stored.
export async function saveSettings() {
    const payload = {};
    for (const [id, key, kind] of _NUMERIC_FIELDS) {
        const el = document.getElementById(id);
        if (!el) continue;
        if (kind === 'string') {
            payload[key] = String(el.value);
            continue;
        }
        const num = kind === 'int' ? parseInt(el.value, 10) : parseFloat(el.value);
        if (Number.isFinite(num)) payload[key] = num;
    }
    for (const [id, key] of _SELECT_FIELDS) {
        const el = document.getElementById(id);
        if (el) payload[key] = el.value;
    }
    for (const [id, key] of _BOOL_FIELDS) {
        const el = document.getElementById(id);
        if (el) payload[key] = !!el.checked;
    }
    const fo = [];
    for (const cat of _FALLBACK_CATEGORIES) {
        const el = document.getElementById(`set-fallback-on-${cat}`);
        if (el && el.checked) fo.push(cat);
    }
    payload.fallback_on = fo;
    try {
        await secureFetch('/api/config', {
            method: 'POST',
            body: JSON.stringify(payload),
        });
    } catch (_) { /* best-effort; the next run reads the persisted config */ }
}

function _setVal(id, val) {
    const el = document.getElementById(id);
    if (!el || val == null) return;
    el.value = val;
    // Keep a companion "<id>-value" slider label in sync (doc-temp etc.).
    const span = document.getElementById(`${id}-value`);
    if (span) span.textContent = el.value;
}

function _setSelectVal(id, val) {
    const el = document.getElementById(id);
    if (!el) return;
    const allowed = Array.from(el.options).map((o) => o.value);
    if (allowed.includes(String(val))) el.value = String(val);
}

function _setCheck(id, val) {
    const el = document.getElementById(id);
    if (el) el.checked = !!val;
}
