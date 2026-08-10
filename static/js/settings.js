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
import { announceError, announceStatus } from './ui.js';


let _saveTimer = null;
// Number of saves requested but not yet resolved. A COUNTER, not a boolean:
// saves are serialised through _saveChain, so a second request can be queued
// while the first is still on the wire and a boolean's `finally` would clear
// the guard with work still pending. Incremented SYNCHRONOUSLY in saveSettings
// (before the chain's microtask) so the gate covers the whole request, not just
// the network window. refreshSettings() must not re-apply the server's
// pre-save view over controls the user just edited.
let _savesPending = 0;

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
    ['set-vision-failure-cooldown', 'vision_failure_cooldown_s', 'int'],
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
    ['docgen-max-sections', 'docgen_max_sections', 'int'],
    ['docgen-agent-iters', 'docgen_agent_max_iterations', 'int'],
    ['docgen-temp', 'docgen_temperature', 'float'],
    ['docgen-section-max-tokens', 'docgen_section_max_tokens', 'int'],
    ['docgen-section-attempts', 'docgen_section_max_attempts', 'int'],
    ['docgen-retry-backoff', 'docgen_retry_backoff_s', 'int'],
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
    // Biblio Tagging (Library Audit sub-view). Propose model/params + the
    // Zotero local-API base + the restore-archive dir. Config-only knobs.
    ['set-tagging-propose-model', 'tagging_propose_model', 'string'],
    ['set-tagging-max-tags', 'tagging_max_tags', 'int'],
    ['set-tagging-min-conf', 'tagging_min_conf', 'float'],
    ['set-tagging-text-max-chars', 'tagging_text_max_chars', 'int'],
    // max-tokens/temperature were config-only until 2026-07-18 — with no UI
    // field, the materialised old default (300) was un-fixable from the app
    // and starved Gemini thinking models into empty proposals.
    ['set-tagging-propose-max-tokens', 'tagging_propose_max_tokens', 'int'],
    ['set-tagging-propose-temperature', 'tagging_propose_temperature', 'float'],
    ['set-tagging-candidate-tags', 'tagging_candidate_tags', 'int'],
    ['set-tagging-fewshot-k', 'tagging_fewshot_k', 'int'],
    ['set-tagging-zotero-base', 'tagging_zotero_api_base', 'string'],
    ['set-tagging-archive-dir', 'tagging_archive_dir', 'string'],
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
    ['set-prompt-capture-enabled', 'prompt_capture_enabled'],
];

// Populate the settings-owned controls from /api/config and wire auto-save.
// Receives the already-fetched config object from app.js init (no extra GET).
export function initSettings(cfg) {
    _stampSectionCounts();
    _wireSettingsFilter();
    _applyConfig(cfg);
    _wire();
    _wireLogViewer();
    _wireReopenRefresh();
}

/** Populate every settings-owned control from a config object. Split out of
 * initSettings (2026-07-26 UX audit) because the modal is now re-synced from
 * the server on each OPEN: initSettings ran once at boot and never again, so a
 * server-side drop (see saveSettings) left a rejected value sitting in the
 * input for the rest of the session, silently disagreeing with what the server
 * actually holds. Idempotent — every setter is a plain assignment. */
function _applyConfig(cfg) {
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
    // 0 is a MEANINGFUL value here (disables the cooldown), so ?? not ||.
    _setVal('set-vision-failure-cooldown', cfg.vision_failure_cooldown_s ?? 30);
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
    _setVal('docgen-max-sections', cfg.docgen_max_sections ?? 8);
    _setVal('docgen-agent-iters', cfg.docgen_agent_max_iterations ?? 6);
    _setVal('docgen-temp', cfg.docgen_temperature ?? 0.3);
    _setVal('docgen-section-max-tokens', cfg.docgen_section_max_tokens ?? 4096);
    _setVal('docgen-section-attempts', cfg.docgen_section_max_attempts ?? 3);
    _setVal('docgen-retry-backoff', cfg.docgen_retry_backoff_s ?? 3);
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
    // Biblio Tagging knobs. `??` so an explicitly-empty propose model
    // ("" ⇒ chat model) / archive dir ("" ⇒ default) stays empty.
    _setVal('set-tagging-propose-model', cfg.tagging_propose_model ?? '');
    _setVal('set-tagging-max-tags', cfg.tagging_max_tags ?? 4);
    _setVal('set-tagging-min-conf', cfg.tagging_min_conf ?? 0.35);
    _setVal('set-tagging-text-max-chars', cfg.tagging_text_max_chars ?? 6000);
    _setVal('set-tagging-propose-max-tokens', cfg.tagging_propose_max_tokens ?? 1536);
    _setVal('set-tagging-propose-temperature', cfg.tagging_propose_temperature ?? 0.2);
    _setVal('set-tagging-candidate-tags', cfg.tagging_candidate_tags ?? 20);
    _setVal('set-tagging-fewshot-k', cfg.tagging_fewshot_k ?? 3);
    _setVal('set-tagging-zotero-base', cfg.tagging_zotero_api_base ?? 'http://127.0.0.1:23119');
    _setVal('set-tagging-archive-dir', cfg.tagging_archive_dir ?? '');

    _setSelectVal('set-reranker-device', cfg.vault_reranker_device ?? 'auto');
    _setSelectVal('set-vector-backend', cfg.vault_vector_backend ?? 'simple');
    _setSelectVal('set-fallback-provider', cfg.fallback_provider ?? '');
    _setCheck('set-prewarm-enabled', cfg.vault_prewarm_enabled ?? true);
    _setCheck('set-deck-review-enabled', cfg.deck_review_enabled ?? false);
    _setCheck('set-refactor-table-double-read', cfg.refactor_table_double_read ?? true);
    _setCheck('set-prompt-capture-enabled', cfg.prompt_capture_enabled ?? true);

    const fo = Array.isArray(cfg.fallback_on) ? cfg.fallback_on : [];
    for (const cat of _FALLBACK_CATEGORIES) {
        _setCheck(`set-fallback-on-${cat}`, fo.includes(cat));
    }
}

/** Re-sync the modal from /api/config every time it is OPENED.
 *
 * A MutationObserver on the overlay's class list rather than a click handler on
 * the trigger: the modal has two entry points (the inline
 * onclick="openModal('settings-modal')" button and the ⌘K palette action in
 * app.js) and ui.js::openModal is generic, so watching the state the open
 * actually sets is the only way to catch both without reaching across the
 * module hierarchy. Skipped while a save is pending/in flight — refreshing then
 * would overwrite the very keystrokes queued for the next POST. */
function _wireReopenRefresh() {
    const overlay = document.getElementById('settings-modal');
    if (!overlay || overlay.dataset.refreshWired === '1') return;
    if (typeof MutationObserver !== 'function') return;
    overlay.dataset.refreshWired = '1';
    let wasOpen = overlay.classList.contains('open');
    new MutationObserver(() => {
        const isOpen = overlay.classList.contains('open');
        if (isOpen && !wasOpen) refreshSettings();
        wasOpen = isOpen;
    }).observe(overlay, { attributes: true, attributeFilter: ['class'] });
}

/** GET /api/config and re-apply it to the controls. Never throws (a failed
 * refresh leaves the last-known values in place — strictly better than a blank
 * modal), and never fights an in-flight save. */
export async function refreshSettings() {
    if (_saveTimer || _savesPending) return;
    try {
        const resp = await secureFetch('/api/config');
        if (!resp.ok) return;
        const cfg = await resp.json();
        // Re-check AFTER the awaits (2026-07-29 audit 1.3): a keystroke landing
        // while the GET was on the wire sets _saveTimer (the 400 ms debounce) —
        // applying the stale response now would overwrite the very characters
        // just typed, and the pending debounce would then PERSIST the reverted
        // value. The user's in-flight edit always wins over a pre-edit GET;
        // the next open re-syncs anyway.
        if (_saveTimer || _savesPending) return;
        if (cfg && typeof cfg === 'object') {
            _applyConfig(cfg);
            // The server is now the visible truth again, so any stale
            // "not saved" marker from an earlier rejected value is obsolete.
            _clearDroppedFlags();
        }
    } catch (e) {
        logError('Settings refresh failed', e);
    }
}

// Read-only application-log viewer (the "Application log" settings section).
// Independent of the config save loop: it only GETs /api/log/tail and renders
// the server-redacted tail. Lazy — loads on the first expand of the section and
// on explicit Refresh, so opening the modal never triggers a fetch by itself.
// Card Catalog (2026-07-20): stamp a control count into each settings-section
// summary so the accordion is scannable ("what's in here, how much") without
// opening it. Computed from the live DOM, so it never drifts from the markup;
// idempotent (re-running replaces the span).
/** G4 (2026-07-22 audit): live filter over the ~84 settings controls.
 *
 * Matches each .control-group's full text (label + hint), hides non-matching
 * rows, hides sections with no match and auto-OPENS sections with one — so
 * "vision timeout" is one keystroke away instead of a ten-section hunt.
 * Clearing restores every row and each section's pre-filter open state.
 * Wire-once via a dataset marker on the input (module flags go stale if the
 * node is ever replaced — the auditSearch lesson).
 *
 * DEFECT FIXED (2026-07-26 UX audit): the reveal used to walk only
 * `details.settings-section` while the COUNT walked every `.control-group`
 * inside them — including the 17 rows nested one level deeper in
 * `<details class="advanced-controls">` (Vault Chat · fine-tuning, which holds
 * `set-reranker-model`, the documented discovery path). Searching "reranker"
 * therefore reported "N matching setting(s)" both visually and through
 * announceStatus while the matched rows stayed inside a closed nested details:
 * counted but never revealed. The invariant is now explicit — EVERY ancestor
 * <details> of a matched row is opened, so the count and what the user can see
 * are the same set. Pinned by tests/js/settingsFilter.test.js. */
function _wireSettingsFilter() {
    const input = document.getElementById('settings-filter');
    if (!input || input.dataset.wired) return;
    input.dataset.wired = '1';
    const countEl = document.getElementById('settings-filter-count');
    let timer = null;
    let filtering = false;
    const apply = () => {
        const q = input.value.trim().toLowerCase();
        const sections = document.querySelectorAll('#settings-modal details.settings-section');
        // ALL <details> in the modal, nested ones included — the open-state
        // memo must cover exactly the set we may force open, or clearing the
        // filter would leave a nested block permanently expanded.
        const allDetails = document.querySelectorAll('#settings-modal details');
        let shown = 0;
        if (q && !filtering) {
            // Entering filter mode: remember each block's open state once.
            allDetails.forEach((d) => { d.dataset.wasOpen = d.open ? '1' : ''; });
            filtering = true;
        }
        sections.forEach((sec) => {
            let secHas = false;
            sec.querySelectorAll('.control-group').forEach((row) => {
                const match = !q || row.textContent.toLowerCase().includes(q);
                row.hidden = !match;
                if (match && q) {
                    secHas = true;
                    shown += 1;
                    // Open every ancestor <details> up to (and including) the
                    // section — a row nested in .advanced-controls needs BOTH
                    // opened, and closest() from the parent walks the whole
                    // chain rather than assuming a fixed nesting depth.
                    let d = row.parentElement ? row.parentElement.closest('details') : null;
                    while (d) {
                        d.hidden = false;
                        d.open = true;
                        if (d === sec) break;
                        d = d.parentElement ? d.parentElement.closest('details') : null;
                    }
                }
                if (!q) secHas = true;
            });
            if (q) {
                sec.hidden = !secHas;
                if (secHas) sec.open = true;
                // A nested block whose every row was filtered out is hidden
                // outright: leaving it open-but-empty reads as a broken filter
                // (a summary with nothing under it).
                sec.querySelectorAll('details').forEach((d) => {
                    const has = Array.from(d.querySelectorAll('.control-group'))
                        .some((row) => !row.hidden);
                    d.hidden = !has;
                });
            } else {
                sec.hidden = false;
            }
        });
        if (!q && filtering) {
            filtering = false;
            allDetails.forEach((d) => {
                d.hidden = false;
                d.open = d.dataset.wasOpen === '1';
            });
        }
        if (countEl) countEl.textContent = q ? `${shown} match(es)` : '';
        if (q) announceStatus(`${shown} matching setting(s).`);
    };
    input.addEventListener('input', () => {
        window.clearTimeout(timer);
        timer = window.setTimeout(apply, 150);
    });
    input.addEventListener('keydown', (e) => {
        // Escape clears the filter (and stays in the modal — stopPropagation
        // keeps the modal's own Escape-to-close from firing on a non-empty
        // filter; an empty filter lets Escape close as usual).
        if (e.key === 'Escape' && input.value) {
            e.stopPropagation();
            input.value = '';
            window.clearTimeout(timer);
            apply();
        }
    });
}

function _stampSectionCounts() {
    document.querySelectorAll('#settings-modal .settings-section').forEach((sec) => {
        const summary = sec.querySelector(':scope > summary');
        if (!summary) return;
        const n = sec.querySelectorAll('input, select, textarea').length;
        if (!n) return;
        let span = summary.querySelector('.settings-count');
        if (!span) {
            span = document.createElement('span');
            span.className = 'settings-count';
            summary.appendChild(span);
        }
        span.textContent = `${n} control${n === 1 ? '' : 's'}`;
    });
}

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
    // Clear the debounce handle as soon as the save it scheduled actually runs:
    // it doubles as the "an edit is pending" gate for refreshSettings(), and a
    // stale non-null handle would suppress every future re-open refresh.
    _saveTimer = null;
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
    // The pending counter increments AFTER the payload build (2026-07-29 audit
    // 15a): incrementing first left every line of the DOM-reading loops above
    // inside the window between `+= 1` and the chain's `finally` decrement —
    // one throw there (a getter, a hostile field) leaked the counter forever
    // and permanently disabled refreshSettings' re-open refresh. From here to
    // the `.then()` registration nothing can throw, and the increment is still
    // synchronous in the same task, so the gate covers the whole request.
    _savesPending += 1;
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
            // 2026-07-26 UX audit (WCAG 3.3.1 Error Identification): the server
            // DROPS an out-of-range/malformed key and still answers 200, so
            // until now the only silent-failure mode left in this path was a
            // rejected VALUE, not a rejected request. `dropped` names them.
            const body = await resp.json().catch(() => null);
            _flagDropped(body && Array.isArray(body.dropped) ? body.dropped : []);
        } catch (e) {
            logError('Settings save failed', e);
            announceError('Settings could not be saved — the change is not persisted.');
        } finally {
            _savesPending -= 1;
        }
    });
    await _saveChain;
}

// Config key → the control that writes it, derived from the same tuple tables
// the save path uses, so a new knob can never drift out of the error mapping.
function _controlForKey(key) {
    for (const [id, k] of [..._NUMERIC_FIELDS, ..._SELECT_FIELDS, ..._BOOL_FIELDS]) {
        if (k === key) return document.getElementById(id);
    }
    return null;
}

// Human name for the message: the control's own <label>, falling back to the
// raw config key (better an unfriendly key than an unattributable error).
function _labelForControl(el, key) {
    if (el && el.id) {
        const byFor = document.querySelector(`label[for="${el.id}"]`);
        if (byFor) return (byFor.textContent || '').trim() || key;
    }
    const group = el && el.closest ? el.closest('.control-group') : null;
    const label = group ? group.querySelector('label') : null;
    return (label && (label.textContent || '').trim()) || key;
}

function _clearDroppedFlags() {
    // Strip exactly OUR note ids from aria-describedby before removing the
    // notes (2026-07-29 audit 8): the attribute is shared with the control
    // hints wireControlHints attached, and a hand-authored id must survive —
    // so filter by the settings-field-error id prefix, never wipe the whole
    // attribute.
    document.querySelectorAll('#settings-modal .settings-field-error')
        .forEach((el) => el.remove());
    document.querySelectorAll('#settings-modal [aria-invalid="true"]')
        .forEach((el) => {
            el.removeAttribute('aria-invalid');
            const prev = el.getAttribute('aria-describedby');
            if (!prev) return;
            const kept = prev.split(/\s+/).filter(
                (id) => id && !id.startsWith('settings-field-error-'));
            if (kept.length) el.setAttribute('aria-describedby', kept.join(' '));
            else el.removeAttribute('aria-describedby');
        });
}

/** Mark every field the server refused, inline AND to assistive tech.
 *
 * Called on EVERY successful save (with an empty list on the happy path) so a
 * corrected value clears its own marker — a sticky error on a now-valid field
 * is its own defect. The visible node is created here, but the ANNOUNCEMENT
 * goes through announceError → the static #sr-error-announcer region: a
 * live-region node created and populated in the same tick can be missed by
 * WebKit, which is exactly what tests/js/liveRegions.test.js pins. */
function _flagDropped(keys) {
    _clearDroppedFlags();
    if (!keys.length) return;
    const names = [];
    for (const key of keys) {
        const el = _controlForKey(key);
        const name = _labelForControl(el, key);
        names.push(name);
        if (!el) continue;
        el.setAttribute('aria-invalid', 'true');
        const group = el.closest ? el.closest('.control-group') : null;
        const note = document.createElement('div');
        note.className = 'settings-field-error';
        // Plain text via textContent — the key is server-echoed, and this file
        // never builds markup from a response (the innerHTML rule).
        note.textContent = 'Not saved: value out of range or malformed. The previous setting is still in effect.';
        // Associate the error WITH the control (2026-07-29 audit 8, WCAG
        // 3.3.1): aria-invalid alone tells a screen-reader user the field is
        // wrong but not why — tabbing back after the one-shot announceError
        // read "invalid entry" plus the ordinary hint, never the reason. The
        // note id is appended to (never replaces) aria-describedby, the same
        // idiom wireControlHints uses, so the control-hint association
        // survives. The id embeds the config key: stable across re-renders,
        // and the prefix is what _clearDroppedFlags filters on.
        note.id = `settings-field-error-${key}`;
        const prev = el.getAttribute('aria-describedby');
        const ids = prev ? prev.split(/\s+/).filter(Boolean) : [];
        if (!ids.includes(note.id)) ids.push(note.id);
        el.setAttribute('aria-describedby', ids.join(' '));
        (group || el.parentElement || document.body).appendChild(note);
    }
    announceError(
        `${names.length} setting(s) were not saved — ${names.join(', ')}. `
        + 'The value is out of range or malformed; the previous setting is still in effect.',
    );
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
