// Settings window: the single editor for LLM/RAG parameters that previously
// had no UI (online timeout/retries/tokens, fallback policy, reranker
// device/model, vector backend, prewarm, vision/OCR timeout + token caps)
// plus the persistence for the Single Paper and Deck generation knobs
// (paper_* / deck_*), which the existing summarizer.js / deck.js still read by
// id and send per-request.
//
// The vault chat knobs, OCR/Vision selects and provider/model selectors live
// inside the same modal but are owned by vault.js / config.js respectively —
// this module deliberately does NOT touch them (no double-save).  Per the JS
// module hierarchy this file imports only api.js (and nothing else from the
// project).
import { secureFetch, logError } from './api.js';
import { announceError } from './ui.js';


let _saveTimer = null;

const _FALLBACK_CATEGORIES = ['timeout', 'network', 'rate_limit', 'server_error'];

// [control id, config key, 'int' | 'float' | 'string']
const _NUMERIC_FIELDS = [
    ['set-online-timeout', 'online_timeout_s', 'int'],
    ['set-online-retries', 'online_max_retries', 'int'],
    ['set-online-max-tokens', 'online_max_tokens', 'int'],
    ['set-agent-wall-clock', 'agent_wall_clock_s', 'int'],
    ['set-local-timeout', 'local_request_timeout_s', 'int'],
    // Local-backend base URLs. Empty ⇒ env var then localhost default (resolved
    // server-side). String-typed so an empty value persists as "" (disable override).
    ['set-ollama-host', 'ollama_host', 'string'],
    ['set-lm-studio-host', 'lm_studio_host', 'string'],
    ['set-vision-timeout', 'vision_timeout_s', 'int'],
    ['set-vision-max-tokens', 'vision_max_tokens', 'int'],
    ['set-ocr-max-tokens', 'ocr_max_tokens', 'int'],
    ['set-reranker-model', 'vault_reranker_model', 'string'],
    // Vault thesaurus / primer — config-only knobs (the live thesaurus/primer
    // ENABLE toggles are owned by vault.js as body overrides; these are the
    // file paths + depth + content overrides, persisted to config only).
    ['set-thesaurus-abbrev-path', 'vault_thesaurus_abbrev_path', 'string'],
    ['set-thesaurus-tags-path', 'vault_thesaurus_tags_path', 'string'],
    ['set-thesaurus-max-variants', 'vault_thesaurus_max_variants', 'int'],
    ['set-primer-max-chars', 'vault_primer_max_chars', 'int'],
    ['set-primer-header', 'vault_primer_header', 'string'],
    ['set-primer-core-terms', 'vault_primer_core_terms', 'string'],
    ['doc-ctx', 'paper_num_ctx', 'int'],
    ['doc-predict', 'paper_max_tokens', 'int'],
    ['doc-temp', 'paper_temperature', 'float'],
    ['doc-top-p', 'paper_top_p', 'float'],
    ['doc-repeat-penalty', 'paper_repeat_penalty', 'float'],
    ['deck-max-sections', 'deck_max_sections', 'int'],
    ['deck-agent-iters', 'deck_agent_max_iterations', 'int'],
    ['deck-temp', 'deck_temperature', 'float'],
    ['deck-section-max-tokens', 'deck_section_max_tokens', 'int'],
    ['deck-section-attempts', 'deck_section_max_attempts', 'int'],
    ['deck-retry-backoff', 'deck_retry_backoff_s', 'int'],
    ['set-chat-temp', 'chat_temperature', 'float'],
    ['set-chat-system-prompt', 'chat_system_prompt', 'string'],
    // Note Refactor. The scope sub-folder and the scope-wide strip-preamble
    // toggle are owned by the Note Refactor tab (refactor.js) and deliberately
    // NOT mirrored here (no double-save), like the vault knobs above.
    ['set-refactor-extract-model', 'refactor_extract_model', 'string'],
    ['set-refactor-review-model', 'refactor_review_model', 'string'],
    ['set-refactor-review-max-tokens', 'refactor_review_max_tokens', 'int'],
    ['set-refactor-rewrite-max-tokens', 'refactor_rewrite_max_tokens', 'int'],
    ['set-refactor-thumb-max-side', 'refactor_thumb_max_side', 'int'],
    ['set-refactor-archive-dir', 'refactor_archive_dir', 'string'],
];
const _SELECT_FIELDS = [
    ['set-reranker-device', 'vault_reranker_device'],
    ['set-vector-backend', 'vault_vector_backend'],
    ['set-fallback-provider', 'fallback_provider'],
];
const _BOOL_FIELDS = [
    ['set-prewarm-enabled', 'vault_prewarm_enabled'],
    ['set-deck-review-enabled', 'deck_review_enabled'],
    ['set-refactor-table-double-read', 'refactor_table_double_read'],
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
    // `??` so a persisted empty host ("" ⇒ use env var / localhost) stays empty
    // rather than reverting to the placeholder.
    _setVal('set-ollama-host', cfg.ollama_host ?? '');
    _setVal('set-lm-studio-host', cfg.lm_studio_host ?? '');
    _setVal('set-vision-timeout', cfg.vision_timeout_s ?? 120);
    _setVal('set-vision-max-tokens', cfg.vision_max_tokens ?? 1536);
    _setVal('set-ocr-max-tokens', cfg.ocr_max_tokens ?? 4096);
    _setVal('set-reranker-model', cfg.vault_reranker_model ?? '');
    // Vault thesaurus / primer config-only knobs. `??` so a persisted empty
    // header/core-terms ("" ⇒ built-in defaults) and an explicitly-cleared file
    // path ("" ⇒ slot disabled) stay empty rather than reverting to the default.
    _setVal('set-thesaurus-abbrev-path', cfg.vault_thesaurus_abbrev_path ?? '_abreviations.md');
    _setVal('set-thesaurus-tags-path', cfg.vault_thesaurus_tags_path ?? '_tags.md');
    _setVal('set-thesaurus-max-variants', cfg.vault_thesaurus_max_variants ?? 3);
    _setVal('set-primer-max-chars', cfg.vault_primer_max_chars ?? 1500);
    _setVal('set-primer-header', cfg.vault_primer_header ?? '');
    _setVal('set-primer-core-terms', cfg.vault_primer_core_terms ?? '');
    _setVal('doc-ctx', cfg.paper_num_ctx ?? 32768);
    _setVal('doc-predict', cfg.paper_max_tokens ?? 4096);
    _setVal('doc-temp', cfg.paper_temperature ?? 0.3);
    _setVal('doc-top-p', cfg.paper_top_p ?? 0.9);
    _setVal('doc-repeat-penalty', cfg.paper_repeat_penalty ?? 1.1);
    _setVal('deck-max-sections', cfg.deck_max_sections ?? 8);
    _setVal('deck-agent-iters', cfg.deck_agent_max_iterations ?? 6);
    _setVal('deck-temp', cfg.deck_temperature ?? 0.3);
    _setVal('deck-section-max-tokens', cfg.deck_section_max_tokens ?? 2048);
    _setVal('deck-section-attempts', cfg.deck_section_max_attempts ?? 3);
    _setVal('deck-retry-backoff', cfg.deck_retry_backoff_s ?? 3);
    // Plain Chat knobs. `??` (nullish) so a persisted empty system prompt ("")
    // stays empty rather than reverting to the default placeholder text.
    _setVal('set-chat-temp', cfg.chat_temperature ?? 0.3);
    _setVal('set-chat-system-prompt', cfg.chat_system_prompt ?? 'You are a helpful assistant.');
    // Note Refactor knobs. `??` so a persisted empty model/dir ("" ⇒ fall back
    // to the vision/chat model / default archive dir) stays empty.
    _setVal('set-refactor-extract-model', cfg.refactor_extract_model ?? '');
    _setVal('set-refactor-review-model', cfg.refactor_review_model ?? '');
    _setVal('set-refactor-review-max-tokens', cfg.refactor_review_max_tokens ?? 1024);
    _setVal('set-refactor-rewrite-max-tokens', cfg.refactor_rewrite_max_tokens ?? 4096);
    _setVal('set-refactor-thumb-max-side', cfg.refactor_thumb_max_side ?? 384);
    _setVal('set-refactor-archive-dir', cfg.refactor_archive_dir ?? '');

    _setSelectVal('set-reranker-device', cfg.vault_reranker_device ?? 'auto');
    _setSelectVal('set-vector-backend', cfg.vault_vector_backend ?? 'simple');
    _setSelectVal('set-fallback-provider', cfg.fallback_provider ?? '');
    _setCheck('set-prewarm-enabled', cfg.vault_prewarm_enabled ?? true);
    _setCheck('set-deck-review-enabled', cfg.deck_review_enabled ?? false);
    _setCheck('set-refactor-table-double-read', cfg.refactor_table_double_read ?? true);

    const fo = Array.isArray(cfg.fallback_on) ? cfg.fallback_on : [];
    for (const cat of _FALLBACK_CATEGORIES) {
        _setCheck(`set-fallback-on-${cat}`, fo.includes(cat));
    }
    _wire();
    _wireLogViewer();
}

// Read-only application-log viewer (the "Application log" settings section).
// Independent of the config save loop: it only GETs /api/log/tail and renders
// the server-redacted tail. Lazy — loads on the first expand of the section and
// on explicit Refresh, so opening the modal never triggers a fetch by itself.
function _wireLogViewer() {
    const section = document.getElementById('log-viewer-section');
    const out = document.getElementById('log-viewer-output');
    const meta = document.getElementById('log-viewer-meta');
    const linesSel = document.getElementById('log-viewer-lines');
    const refreshBtn = document.getElementById('log-viewer-refresh');
    const copyBtn = document.getElementById('log-viewer-copy');
    if (!section || !out || !linesSel || !refreshBtn) return;
    if (section.dataset.wired === '1') return;  // defensive: never double-bind if re-run
    section.dataset.wired = '1';

    let loadedOnce = false;
    const load = async () => {
        const n = parseInt(linesSel.value, 10) || 500;
        out.textContent = 'Loading…';
        if (meta) meta.textContent = '';
        try {
            const resp = await secureFetch(`/api/log/tail?lines=${n}`);
            const d = await resp.json();
            if (!resp.ok) { out.textContent = (d && d.error) || `Error ${resp.status}`; return; }
            out.textContent = d.exists ? (d.text || '(log is empty)') : '(no log file yet)';
            out.scrollTop = out.scrollHeight;  // jump to the newest lines
            if (meta) {
                const kb = Math.round((d.size || 0) / 1024);
                meta.textContent = `${d.lines} line(s) shown · file ${kb} KB`
                    + (d.truncated ? ' · showing only the most recent ~1 MB' : '');
            }
            loadedOnce = true;
        } catch (_) {
            out.textContent = 'Failed to load the log.';
        }
    };

    refreshBtn.addEventListener('click', load);
    linesSel.addEventListener('change', () => { if (loadedOnce) load(); });
    section.addEventListener('toggle', () => { if (section.open && !loadedOnce) load(); });
    if (copyBtn) {
        copyBtn.addEventListener('click', async () => {
            try {
                await navigator.clipboard.writeText(out.textContent || '');
                const prev = copyBtn.textContent;
                copyBtn.textContent = 'Copied';
                setTimeout(() => { copyBtn.textContent = prev; }, 1200);
            } catch (_) { /* clipboard unavailable; no-op */ }
        });
    }
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
        // Live-save on every keystroke/drag for text-like and range controls.
        // A <textarea> reports el.type === 'textarea' (NOT 'text'), so it must be
        // listed explicitly or the chat system-prompt would only save on blur.
        if (el.type === 'range' || el.type === 'number' || el.type === 'text' || el.type === 'textarea') {
            el.addEventListener('input', debounce);
        }
    }
}

// Gather every settings-owned control and persist to /api/config.  The server
// re-validates/clamps each key (api/routes/config.py::_validate_llm_config_keys),
// so a malformed value is dropped server-side rather than stored.
// Serialises saveSettings POSTs (item 3.8) — never rejects (errors handled inline).
let _saveChain = Promise.resolve();

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
    // Item 3.8 (improvement plan 2026-07-04): (a) CHAIN saves — two rapid
    // debounced edits used to race their POSTs, and the older payload could
    // arrive last and win; the promise chain serialises them in edit order
    // (each payload is built at send time, above, so the later save carries
    // the later DOM state). (b) SURFACE failures — the old silent catch meant
    // a dead server ate settings changes with zero feedback; now the failure
    // is logged and announced so the user knows the change did not persist.
    _saveChain = _saveChain.then(async () => {
        try {
            const resp = await secureFetch('/api/config', {
                method: 'POST',
                body: JSON.stringify(payload),
            });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        } catch (e) {
            logError('Settings save failed', e);
            announceError('Settings could not be saved — the change is not persisted.');
        }
    });
    await _saveChain;
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
