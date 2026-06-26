/**
 * Obsidian Vault tab: indexing lifecycle, the live retrieval/generation knobs,
 * vault RAG chat (with optional agent-mode trace rendering), exclusions, and the
 * indexed-materials manifest. Imports only ui.js + api.js + config.js (per the JS
 * module hierarchy).
 *
 * Key non-obvious points (see inline comments):
 *  - the live query-knob values are sent in EVERY /api/obsidian/chat body, so a
 *    save failure never changes what the next Send uses;
 *  - the fetch-abort budget is computed LIVE at send time in _chatAbortMs() from
 *    the wall-clock control, never cached at init, so a Settings change applies
 *    on the next Send without reload (it is the outermost link of the timeout
 *    chain: agent deadline ≤ server consumer stall ≤ this abort);
 *  - _renderAnswer falls back to plain text when the vendored `marked` is absent;
 *  - all user-controlled strings (paths, folder names, answers) go through
 *    createElement/textContent or sanitiseHtml — never raw innerHTML.
 */
import { secureFetch, readSSE } from './api.js';
import { taskBegin, taskEnd, setStatusA11y, openModal, closeModal, sanitiseHtml, copyToClipboard, showTaskError, clearTaskError } from './ui.js';
import { getActiveProvider, getSelectedEmbed, getSelectedModel, saveSelectedModels } from './config.js';

let _isQuerying = false;
let _vaultPollDeadline = 0;
let _chatParamsSaveTimer = null;
let _prewarmPolling = false;
let _prewarmInFlight = false;

const _PREWARM_STAGE_LABELS = {
    idle: 'Starting up…',
    loading_index: 'Loading vault index…',
    building_bm25: 'Building lexical (BM25) retriever…',
    loading_reranker: 'Loading cross-encoder reranker…',
};

const _CHAT_PARAM_DEFAULTS = {
    top_k: 6,
    similarity_cutoff: 0.25,
    prompt_mode: 'strict',
    temperature: 0.3,
    system_prompt: '',
    hybrid_enabled: true,
    reranker_enabled: true,
    agent_enabled: false,
    agent_max_iterations: 6,
    mmr_enabled: false,
    mmr_lambda: 0.5,
    query_expansion: false,
    num_queries: 3,
    rerank_pool_ceiling: 50,
    wikilink_expansion: false,
};

const _SYSTEM_PROMPT_MAX = 4000;

function _readNumber(id, fallback) {
    const el = document.getElementById(id);
    if (!el) return fallback;
    const v = parseFloat(el.value);
    return Number.isFinite(v) ? v : fallback;
}

function _readSelect(id, fallback) {
    const el = document.getElementById(id);
    return el && el.value ? el.value : fallback;
}

function _readText(id, fallback, maxLen) {
    const el = document.getElementById(id);
    if (!el) return fallback;
    const raw = (el.value || '').trim();
    if (typeof maxLen === 'number' && raw.length > maxLen) {
        return raw.slice(0, maxLen);
    }
    return raw;
}

function _readCheckbox(id, fallback) {
    const el = document.getElementById(id);
    return el ? !!el.checked : !!fallback;
}

// Per-result snippet cap for the agent trace UI. The server already caps
// tool outputs (vault.search snippets at 800 chars, registry-level
// truncation at the per-tool max), so this is a UI-only display limit
// to keep the collapsed trace readable.
const _TRACE_RESULT_SNIPPET_CHARS = 400;

function _ensureAgentTrace(traceCtx, botMsg, chat) {
    if (traceCtx.trace) return traceCtx.trace;
    const trace = document.createElement('details');
    trace.className = 'agent-trace';
    trace.open = false;
    const summary = document.createElement('summary');
    summary.textContent = 'Agent reasoning · 0 iterations';
    trace._summary = summary;
    trace.appendChild(summary);
    chat.insertBefore(trace, botMsg);
    traceCtx.trace = trace;
    chat.scrollTop = chat.scrollHeight;
    return trace;
}

function _renderAgentIteration(index, traceCtx, botMsg, chat) {
    const trace = _ensureAgentTrace(traceCtx, botMsg, chat);
    traceCtx.iterCount = index;
    if (trace._summary) {
        trace._summary.textContent = `Agent reasoning · ${index} iteration${index === 1 ? '' : 's'}`;
    }
    const iterDiv = document.createElement('div');
    iterDiv.className = 'agent-iteration';
    const header = document.createElement('div');
    header.className = 'agent-iter-header';
    header.textContent = `Iteration ${index}`;
    iterDiv.appendChild(header);
    trace.appendChild(iterDiv);
    traceCtx.currentIter = iterDiv;
    chat.scrollTop = chat.scrollHeight;
}

function _renderAgentThought(text, traceCtx, botMsg, chat) {
    if (!text) return;
    const iter = traceCtx.currentIter || _ensureAgentTrace(traceCtx, botMsg, chat);
    const div = document.createElement('div');
    div.className = 'agent-thought';
    div.textContent = text;
    iter.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
}

function _renderAgentToolCall(call, traceCtx, botMsg, chat) {
    if (!call || typeof call !== 'object') return;
    const iter = traceCtx.currentIter || _ensureAgentTrace(traceCtx, botMsg, chat);
    const div = document.createElement('div');
    div.className = 'agent-tool-call';
    const code = document.createElement('code');
    let argsText = '';
    try {
        argsText = JSON.stringify(call.arguments ?? {});
    } catch (e) {
        argsText = '{}';
    }
    code.textContent = `${call.name || '?'}(${argsText})`;
    div.appendChild(code);
    iter.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
}

function _renderAgentToolResult(result, traceCtx, botMsg, chat) {
    if (!result || typeof result !== 'object') return;
    const iter = traceCtx.currentIter || _ensureAgentTrace(traceCtx, botMsg, chat);
    const div = document.createElement('div');
    div.className = 'agent-tool-result' + (result.is_error ? ' agent-tool-result-error' : '');
    const pre = document.createElement('pre');
    const raw = String(result.content || '');
    const snippet = raw.length > _TRACE_RESULT_SNIPPET_CHARS
        ? raw.slice(0, _TRACE_RESULT_SNIPPET_CHARS) + ' …'
        : raw;
    pre.textContent = snippet;
    if (result.is_error) {
        const tag = document.createElement('span');
        tag.className = 'agent-tool-result-tag';
        tag.textContent = 'error';
        div.appendChild(tag);
    } else if (result.truncated) {
        const tag = document.createElement('span');
        tag.className = 'agent-tool-result-tag';
        tag.textContent = 'truncated';
        div.appendChild(tag);
    }
    div.appendChild(pre);
    iter.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
}

function getVaultChatParams() {
    return {
        top_k: Math.round(_readNumber('vault-top-k', _CHAT_PARAM_DEFAULTS.top_k)),
        similarity_cutoff: _readNumber('vault-cutoff', _CHAT_PARAM_DEFAULTS.similarity_cutoff),
        prompt_mode: _readSelect('vault-prompt-mode', _CHAT_PARAM_DEFAULTS.prompt_mode),
        temperature: _readNumber('vault-temp', _CHAT_PARAM_DEFAULTS.temperature),
        system_prompt: _readText('vault-system-prompt', _CHAT_PARAM_DEFAULTS.system_prompt, _SYSTEM_PROMPT_MAX),
        hybrid_enabled: _readCheckbox('vault-hybrid-enabled', _CHAT_PARAM_DEFAULTS.hybrid_enabled),
        reranker_enabled: _readCheckbox('vault-reranker-enabled', _CHAT_PARAM_DEFAULTS.reranker_enabled),
        agent_enabled: _readCheckbox('vault-agent-enabled', _CHAT_PARAM_DEFAULTS.agent_enabled),
        agent_max_iterations: Math.round(_readNumber('vault-agent-max-iter', _CHAT_PARAM_DEFAULTS.agent_max_iterations)),
        mmr_enabled: _readCheckbox('vault-mmr-enabled', _CHAT_PARAM_DEFAULTS.mmr_enabled),
        mmr_lambda: _readNumber('vault-mmr-lambda', _CHAT_PARAM_DEFAULTS.mmr_lambda),
        query_expansion: _readCheckbox('vault-query-expansion', _CHAT_PARAM_DEFAULTS.query_expansion),
        num_queries: Math.round(_readNumber('vault-num-queries', _CHAT_PARAM_DEFAULTS.num_queries)),
        rerank_pool_ceiling: Math.round(_readNumber('vault-rerank-pool', _CHAT_PARAM_DEFAULTS.rerank_pool_ceiling)),
        wikilink_expansion: _readCheckbox('vault-wikilink-enabled', _CHAT_PARAM_DEFAULTS.wikilink_expansion),
    };
}

function _setRange(el, label, requested, formatter) {
    // Assign, then read back el.value so the label reflects the browser-
    // clamped value when the persisted config is out of the slider's range.
    // Otherwise the label could show e.g. "20" while the thumb pins at max,
    // and getVaultChatParams() would later read the clamped value, sending
    // a number that disagrees with what the user sees.
    if (!el) return;
    el.value = requested;
    const effective = el.value;
    if (label) label.textContent = formatter ? formatter(effective) : effective;
}

function _setSelect(el, requested, fallback) {
    // <select>.value silently becomes '' when the assigned option does not
    // exist.  Validate against the option list and fall back to a known-good
    // value rather than leaving the control showing nothing.
    if (!el) return;
    const allowed = Array.from(el.options).map((o) => o.value);
    el.value = allowed.includes(requested) ? requested : fallback;
}

/**
 * Restore the persisted vault-chat knobs from config into their controls,
 * normalising out-of-range/invalid values (see the inline note on browser
 * range clamping and the deliberate exclusion of the fetch-abort budget).
 */
export function applyVaultChatParams(cfg) {
    // Restore persisted values into the controls.  Missing keys fall back to
    // the live default constants so a partially-populated config never leaves
    // the UI showing 'undefined'.  Out-of-range numerics are normalised via
    // the browser's own range clamping on read-back; invalid prompt_mode is
    // replaced with the default.
    // NOTE: the chat fetch-abort is NOT derived here — it is computed live at
    // send time in _chatAbortMs() so a wall-clock change made in the Settings
    // modal (which never re-runs this function) takes effect on the next Send
    // without a page reload.
    const params = {
        top_k: cfg.vault_top_k ?? _CHAT_PARAM_DEFAULTS.top_k,
        similarity_cutoff: cfg.vault_similarity_cutoff ?? _CHAT_PARAM_DEFAULTS.similarity_cutoff,
        prompt_mode: cfg.vault_prompt_mode ?? _CHAT_PARAM_DEFAULTS.prompt_mode,
        temperature: cfg.vault_chat_temperature ?? _CHAT_PARAM_DEFAULTS.temperature,
        system_prompt: cfg.vault_chat_system_prompt ?? _CHAT_PARAM_DEFAULTS.system_prompt,
        hybrid_enabled: cfg.vault_hybrid_enabled ?? _CHAT_PARAM_DEFAULTS.hybrid_enabled,
        reranker_enabled: cfg.vault_reranker_enabled ?? _CHAT_PARAM_DEFAULTS.reranker_enabled,
        agent_enabled: cfg.vault_agent_enabled ?? _CHAT_PARAM_DEFAULTS.agent_enabled,
        agent_max_iterations: cfg.vault_agent_max_iterations ?? _CHAT_PARAM_DEFAULTS.agent_max_iterations,
        mmr_enabled: cfg.vault_mmr_enabled ?? _CHAT_PARAM_DEFAULTS.mmr_enabled,
        mmr_lambda: cfg.vault_mmr_lambda ?? _CHAT_PARAM_DEFAULTS.mmr_lambda,
        query_expansion: cfg.vault_query_expansion ?? _CHAT_PARAM_DEFAULTS.query_expansion,
        num_queries: cfg.vault_num_queries ?? _CHAT_PARAM_DEFAULTS.num_queries,
        rerank_pool_ceiling: cfg.vault_rerank_pool_ceiling ?? _CHAT_PARAM_DEFAULTS.rerank_pool_ceiling,
        wikilink_expansion: cfg.vault_wikilink_expansion ?? _CHAT_PARAM_DEFAULTS.wikilink_expansion,
    };
    _setRange(
        document.getElementById('vault-top-k'),
        document.getElementById('vault-top-k-value'),
        params.top_k,
        (v) => String(parseInt(v, 10)),
    );
    _setRange(
        document.getElementById('vault-cutoff'),
        document.getElementById('vault-cutoff-value'),
        params.similarity_cutoff,
        (v) => Number(v).toFixed(2),
    );
    _setSelect(
        document.getElementById('vault-prompt-mode'),
        params.prompt_mode,
        _CHAT_PARAM_DEFAULTS.prompt_mode,
    );
    _setRange(
        document.getElementById('vault-temp'),
        document.getElementById('vault-temp-value'),
        params.temperature,
        (v) => Number(v).toFixed(1),
    );
    const sysEl = document.getElementById('vault-system-prompt');
    if (sysEl) sysEl.value = typeof params.system_prompt === 'string' ? params.system_prompt : '';
    const hybridEl = document.getElementById('vault-hybrid-enabled');
    if (hybridEl) hybridEl.checked = !!params.hybrid_enabled;
    const rerankerEl = document.getElementById('vault-reranker-enabled');
    if (rerankerEl) rerankerEl.checked = !!params.reranker_enabled;
    const agentEl = document.getElementById('vault-agent-enabled');
    if (agentEl) agentEl.checked = !!params.agent_enabled;
    _setRange(
        document.getElementById('vault-agent-max-iter'),
        document.getElementById('vault-agent-max-iter-value'),
        params.agent_max_iterations,
        (v) => String(parseInt(v, 10)),
    );
    const mmrEnEl = document.getElementById('vault-mmr-enabled');
    if (mmrEnEl) mmrEnEl.checked = !!params.mmr_enabled;
    _setRange(
        document.getElementById('vault-mmr-lambda'),
        document.getElementById('vault-mmr-lambda-value'),
        params.mmr_lambda,
        (v) => Number(v).toFixed(1),
    );
    const qexpEl = document.getElementById('vault-query-expansion');
    if (qexpEl) qexpEl.checked = !!params.query_expansion;
    _setRange(
        document.getElementById('vault-num-queries'),
        document.getElementById('vault-num-queries-value'),
        params.num_queries,
        (v) => String(parseInt(v, 10)),
    );
    _setRange(
        document.getElementById('vault-rerank-pool'),
        document.getElementById('vault-rerank-pool-value'),
        params.rerank_pool_ceiling,
        (v) => String(parseInt(v, 10)),
    );
    const wlEl = document.getElementById('vault-wikilink-enabled');
    if (wlEl) wlEl.checked = !!params.wikilink_expansion;
}

function _saveVaultChatParams() {
    const p = getVaultChatParams();
    secureFetch('/api/config', {
        method: 'POST',
        body: JSON.stringify({
            vault_top_k: p.top_k,
            vault_similarity_cutoff: p.similarity_cutoff,
            vault_prompt_mode: p.prompt_mode,
            vault_chat_temperature: p.temperature,
            vault_chat_system_prompt: p.system_prompt,
            vault_hybrid_enabled: p.hybrid_enabled,
            vault_reranker_enabled: p.reranker_enabled,
            vault_agent_enabled: p.agent_enabled,
            vault_agent_max_iterations: p.agent_max_iterations,
            vault_mmr_enabled: p.mmr_enabled,
            vault_mmr_lambda: p.mmr_lambda,
            vault_query_expansion: p.query_expansion,
            vault_num_queries: p.num_queries,
            vault_rerank_pool_ceiling: p.rerank_pool_ceiling,
            vault_wikilink_expansion: p.wikilink_expansion,
        }),
    }).catch(() => { /* best-effort; the request body is still authoritative */ });
}

/**
 * Wire the vault-chat knob controls: live slider-label updates plus debounced
 * persistence to /api/config (persistence is a UX nicety — see the inline note).
 */
export function wireVaultChatParamControls() {
    // Live label updates + debounced persistence.  Persistence is a UX
    // nicety; the live values are sent in every /api/obsidian/chat body
    // regardless of save success, so a transient save failure never alters
    // what the next Send actually uses.
    const debounce = () => {
        if (_chatParamsSaveTimer) clearTimeout(_chatParamsSaveTimer);
        _chatParamsSaveTimer = setTimeout(_saveVaultChatParams, 400);
    };
    const bind = (id, labelId, formatter) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.addEventListener('input', () => {
            if (labelId) {
                const lbl = document.getElementById(labelId);
                if (lbl) lbl.textContent = formatter ? formatter(el.value) : el.value;
            }
            debounce();
        });
        el.addEventListener('change', debounce);
    };
    bind('vault-top-k', 'vault-top-k-value', (v) => String(parseInt(v, 10)));
    bind('vault-cutoff', 'vault-cutoff-value', (v) => Number(v).toFixed(2));
    bind('vault-temp', 'vault-temp-value', (v) => Number(v).toFixed(1));
    bind('vault-prompt-mode', null, null);
    bind('vault-system-prompt', null, null);
    bind('vault-mmr-lambda', 'vault-mmr-lambda-value', (v) => Number(v).toFixed(1));
    bind('vault-num-queries', 'vault-num-queries-value', (v) => String(parseInt(v, 10)));
    bind('vault-rerank-pool', 'vault-rerank-pool-value', (v) => String(parseInt(v, 10)));
    bind('vault-agent-max-iter', 'vault-agent-max-iter-value', (v) => String(parseInt(v, 10)));
    const hybridCb = document.getElementById('vault-hybrid-enabled');
    if (hybridCb) hybridCb.addEventListener('change', debounce);
    const rerankerCb = document.getElementById('vault-reranker-enabled');
    if (rerankerCb) rerankerCb.addEventListener('change', debounce);
    const agentCb = document.getElementById('vault-agent-enabled');
    if (agentCb) agentCb.addEventListener('change', debounce);
    const mmrCb = document.getElementById('vault-mmr-enabled');
    if (mmrCb) mmrCb.addEventListener('change', debounce);
    const qexpCb = document.getElementById('vault-query-expansion');
    if (qexpCb) qexpCb.addEventListener('change', debounce);
    const wlCb = document.getElementById('vault-wikilink-enabled');
    if (wlCb) wlCb.addEventListener('change', debounce);
}

function _updateIndexButtons(state) {
    const indexBtn  = document.getElementById('vault-index-btn');
    const pauseBtn  = document.getElementById('vault-pause-btn');
    const resumeBtn = document.getElementById('vault-resume-btn');
    const cancelBtn = document.getElementById('vault-cancel-btn');
    if (!indexBtn) return;
    const running = state === 'running' || state === 'scanning' || state === 'embedding';
    const paused  = state === 'paused' || state === 'paused_partial';
    // paused_scan: the run already exited; only the Index button is offered
    // so the user can restart the scan.  No Cancel — there's nothing to cancel.
    indexBtn.style.display  = (running || paused) ? 'none' : '';
    pauseBtn.style.display  = running ? '' : 'none';
    resumeBtn.style.display = paused  ? '' : 'none';
    cancelBtn.style.display = (running || paused) ? '' : 'none';
}

export async function pickVaultFolder() {
    const displayEl = document.getElementById('vault-path-display');
    try {
        const resp = await secureFetch('/api/native-pick-folder', { method: 'POST' });
        const data = await resp.json();
        if (data.path) {
            const setResp = await secureFetch('/api/config', {
                method: 'POST',
                body: JSON.stringify({ obsidian_vault_path: data.path })
            });
            if (setResp.ok) {
                displayEl.textContent = data.path;
            }
        }
    } catch (e) {
        console.error('Folder pick error:', e);
    }
}

/**
 * Kick off (or resume) a vault index build and start polling its status. Sets a
 * 24 h poll deadline as a runaway backstop; on a start failure surfaces a
 * retryable error in the vault error boundary and resets the buttons to idle.
 */
export async function indexVault() {
    if (_isQuerying) return;
    
    const statusDiv = document.getElementById('obsidian-status-msg');
    _isQuerying = true;
    clearTaskError(document.getElementById('vault-error-boundary'));
    taskBegin('vault-index', 'Indexing vault…');

    try {
        await saveSelectedModels();
        const resp = await secureFetch('/api/obsidian/index', {
            method: 'POST',
            body: JSON.stringify({
                provider: getActiveProvider(),
                llm: getSelectedModel(),
                embed: getSelectedEmbed()
            })
        });
        if (resp.ok) {
            _updateIndexButtons('running');
            _vaultPollDeadline = Date.now() + 24 * 60 * 60 * 1000;
            pollVaultStatus();
        } else {
            const data = await resp.json().catch(() => ({}));
            setStatusA11y(statusDiv, '', false);
            _showVaultError(data.error || 'Indexing could not start.', indexVault);
            _isQuerying = false;
            taskEnd('vault-index');
            _updateIndexButtons('idle');
        }
    } catch (e) {
        setStatusA11y(statusDiv, '', false);
        _showVaultError('Index error: ' + e.message, indexVault);
        _isQuerying = false;
        taskEnd('vault-index');
        _updateIndexButtons('idle');
    }
}

async function pollVaultStatus() {
    if (!_isQuerying) return;

    if (Date.now() > _vaultPollDeadline) {
        _isQuerying = false;
        taskEnd('vault-index');
        setStatusA11y(document.getElementById('obsidian-status-msg'), 'Indexing timed out.', true);
        return;
    }

    try {
        const resp = await secureFetch('/api/obsidian/status');
        const data = await resp.json();
        const statusEl = document.getElementById('obsidian-status-msg');
        if (Array.isArray(data.messages) && data.messages.length > 0) {
            setStatusA11y(statusEl, data.messages[data.messages.length - 1], false);
        }
        renderVaultWarnings(Array.isArray(data.warnings) ? data.warnings : []);

        if (data.state === 'done') {
            _isQuerying = false;
            taskEnd('vault-index');
            _updateIndexButtons('done');
            setStatusA11y(document.getElementById('obsidian-status-msg'), '', false);
            refreshVaultMaterials();
            flashSidebarStatus('Indexing complete.');
            return;
        } else if (data.state === 'error') {
            _isQuerying = false;
            taskEnd('vault-index');
            _updateIndexButtons('error');
            setStatusA11y(document.getElementById('obsidian-status-msg'), '', false);
            _showVaultError('Indexing failed.', indexVault);
            return;
        } else if (data.state === 'paused' || data.state === 'paused_partial') {
            _isQuerying = false;
            taskEnd('vault-index');
            _updateIndexButtons('paused');
            return;
        } else if (data.state === 'paused_scan') {
            _isQuerying = false;
            taskEnd('vault-index');
            _updateIndexButtons('paused_scan');
            setStatusA11y(document.getElementById('obsidian-status-msg'), 'Indexing paused before embedding began. Start indexing again to continue; extraction caches were preserved.', false);
            return;
        }

        setTimeout(pollVaultStatus, 2000);
    } catch (e) {
        setTimeout(pollVaultStatus, 5000);
    }
}

function _renderPrewarmBanner(status, message) {
    const banner = document.getElementById('prewarm-banner');
    if (!banner) return;
    const sendBtn = document.getElementById('vault-send-btn');

    // `idle` is a terminal not-in-flight state — either no prewarm has
    // been kicked off yet, or one was reset after /api/reset.  Don't
    // disable Send or show a banner for it; only the explicitly-in-flight
    // stages below should gate the UI.
    const inFlight =
        status === 'loading_index' ||
        status === 'building_bm25' ||
        status === 'loading_reranker';
    _prewarmInFlight = inFlight;

    if (inFlight) {
        const label = message || _PREWARM_STAGE_LABELS[status] || 'Warming vault…';
        banner.classList.remove('prewarm-error');
        banner.innerHTML = '';
        const spinner = document.createElement('span');
        spinner.className = 'prewarm-spinner';
        const text = document.createElement('span');
        text.textContent = label;
        banner.append(spinner, text);
        banner.hidden = false;
        if (sendBtn) {
            sendBtn.disabled = true;
            sendBtn.title = label;
        }
        return;
    }

    if (status === 'error') {
        banner.classList.add('prewarm-error');
        banner.textContent = message || 'Vault warm-up failed; the first chat may be slow.';
        banner.hidden = false;
        if (sendBtn) {
            sendBtn.disabled = false;
            sendBtn.removeAttribute('title');
        }
        return;
    }

    // ready / skipped — clear UI.
    banner.hidden = true;
    banner.textContent = '';
    banner.classList.remove('prewarm-error');
    if (sendBtn) {
        sendBtn.disabled = false;
        sendBtn.removeAttribute('title');
    }
}

/**
 * Poll /api/obsidian/status until prewarm settles, driving the prewarm banner and
 * gating the Send button while the index/BM25/reranker stages load. Self-guards
 * against concurrent pollers; treats ready/skipped/idle/error as terminal.
 */
export async function pollPrewarmStatus() {
    if (_prewarmPolling) return;
    _prewarmPolling = true;
    const POLL_MS = 1500;
    try {
        while (true) {
            let data;
            try {
                const resp = await secureFetch('/api/obsidian/status');
                data = await resp.json();
            } catch (e) {
                await new Promise(r => setTimeout(r, POLL_MS * 2));
                continue;
            }
            const status = data.prewarm_status || 'idle';
            const message = data.prewarm_message || '';
            _renderPrewarmBanner(status, message);
            // `idle` is treated as terminal too — see _renderPrewarmBanner.
            if (status === 'ready' || status === 'skipped' || status === 'idle' || status === 'error') {
                break;
            }
            await new Promise(r => setTimeout(r, POLL_MS));
        }
    } finally {
        _prewarmPolling = false;
    }
}

export function toggleVaultMaterials() {
    const container = document.getElementById('materials-list-container');
    const btn = document.getElementById('materials-toggle-btn');
    if (!container || !btn) return;
    const collapsed = container.classList.toggle('collapsed');
    btn.textContent = collapsed ? 'Show' : 'Hide';
    if (!collapsed && container.childElementCount === 0) refreshVaultMaterials();
}

export async function refreshVaultMaterials() {
    const container = document.getElementById('materials-list-container');
    if (!container) return;
    container.innerHTML = '<div class="materials-empty">Loading…</div>';
    try {
        const resp = await secureFetch('/api/obsidian/materials');
        const data = await resp.json();
        renderVaultMaterials(data.materials || []);
    } catch (e) {
        container.innerHTML = '<div class="materials-empty">Could not load indexed materials.</div>';
    }
}

function renderVaultMaterials(materials) {
    const container = document.getElementById('materials-list-container');
    if (!container) return;
    container.innerHTML = '';
    if (!materials.length) {
        container.innerHTML = '<div class="materials-empty">No indexed materials found.</div>';
        return;
    }
    for (const mat of materials) {
        const item = document.createElement('div');
        item.className = 'material-item';
        const source = document.createElement('div');
        source.className = 'material-source';
        source.title = mat.source || '';
        source.textContent = mat.source || '(unknown source)';
        const meta = document.createElement('div');
        meta.className = 'material-meta';
        meta.textContent = `${mat.extension || ''} · ${mat.chunk_count || 0} chunks`;
        item.append(source, meta);
        container.appendChild(item);
    }
}

function flashSidebarStatus(message) {
    const bar = document.getElementById('activity-bar');
    const label = document.getElementById('activity-label-text');
    if (!bar || !label) return;
    label.textContent = message;
    bar.style.display = 'block';
    window.setTimeout(() => {
        if (label.textContent === message) {
            label.textContent = '';
            bar.style.display = 'none';
        }
    }, 7000);
}

// Frontend fetch-abort backstop. It must remain the OUTERMOST link of the
// timeout chain (agent deadline ≤ server consumer stall ≤ this abort) so the
// server's structured timeout event always arrives before the client gives up.
// The server consumer waits max(cap, 300) + 30 s; this waits 30 s beyond that,
// i.e. max(cap, 300) + 60 s. The floor (300) mirrors _SINGLE_SHOT_FLOOR_S in
// api/routes/vault.py; the margin keeps the ordering.
const _CHAT_TIMEOUT_MARGIN_S = 60;
const _CHAT_STALL_FLOOR_S = 300;

// Compute the abort budget LIVE from the agent-wall-clock control (kept in sync
// with config by settings.js). Reading at send time — rather than caching a
// value at init — is what lets a wall-clock change in the Settings modal take
// effect immediately, since this module is never re-initialised mid-session.
function _chatAbortMs() {
    const el = document.getElementById('set-agent-wall-clock');
    const wc = el ? Number(el.value) : NaN;
    const base = (Number.isFinite(wc) && wc > 0)
        ? Math.max(wc, _CHAT_STALL_FLOOR_S)
        : _CHAT_STALL_FLOOR_S;
    return (base + _CHAT_TIMEOUT_MARGIN_S) * 1000;
}

function _attachCopyButton(messageEl, getText, ariaLabel) {
    // Idempotent: a re-render mid-stream should not stack buttons.
    const existing = messageEl.querySelector(':scope > .copy-btn');
    if (existing) existing.remove();
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'copy-btn';
    btn.textContent = 'Copy';
    btn.setAttribute('aria-label', ariaLabel || 'Copy message');
    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const text = typeof getText === 'function' ? getText() : String(getText || '');
        if (text) copyToClipboard(text, btn);
    });
    messageEl.appendChild(btn);
    return btn;
}

function _renderAnswer(botMsg, fullAnswer) {
    // marked is vendored locally (static/js/vendor/marked.min.js), but a
    // load failure must never cost the user the answer: fall back to plain
    // text rather than throwing ReferenceError mid-stream.
    if (typeof marked !== 'undefined' && typeof marked.parse === 'function') {
        botMsg.innerHTML = sanitiseHtml(marked.parse(fullAnswer));
    } else {
        botMsg.textContent = fullAnswer;
    }
}

// Surface a recoverable vault error in the shared boundary. `retry`, when
// given, becomes a "Retry" button; a "Dismiss" button always clears it.
function _showVaultError(message, retry) {
    const el = document.getElementById('vault-error-boundary');
    const actions = [];
    if (typeof retry === 'function') {
        actions.push({ label: 'Retry', primary: true, onClick: () => { clearTaskError(el); retry(); } });
    }
    actions.push({ label: 'Dismiss', onClick: () => clearTaskError(el) });
    showTaskError(el, message, actions);
}

/**
 * Send a vault RAG question and stream the answer. Reads the live query knobs
 * (sent in this request's body — see the module banner), enforces the live
 * fetch-abort budget, and consumes the SSE stream: `{info}` notices, the
 * agent-trace frames (iteration/thought/tool_call/tool_result) rendered lazily
 * into a collapsible <details>, and `{token}` answer chunks. On error the partial
 * bot bubble is replaced with a retryable error boundary that re-asks once.
 */
export async function chatWithVault() {
    if (_isQuerying) {
        const statusDiv = document.getElementById('obsidian-status-msg');
        if (statusDiv) setStatusA11y(statusDiv, 'Please wait for the current response to complete.', false);
        return;
    }
    // Enter-key submissions bypass the disabled Send button, so guard
    // here too while prewarm is still walking through the in-flight
    // stages (loading_index / building_bm25 / loading_reranker).
    if (_prewarmInFlight) {
        const statusDiv = document.getElementById('obsidian-status-msg');
        if (statusDiv) setStatusA11y(statusDiv, 'Vault is still warming up. Please wait a moment.', false);
        return;
    }
    const input = document.getElementById('obsidian-input');
    const question = input.value.trim();
    if (!question) return;

    _isQuerying = true;
    clearTaskError(document.getElementById('vault-error-boundary'));
    const chat = document.getElementById('obsidian-chat');

    const userMsg = document.createElement('div');
    userMsg.className = 'message message-user';
    userMsg.textContent = question;
    chat.appendChild(userMsg);
    _attachCopyButton(userMsg, () => question, 'Copy question');
    input.value = '';

    const botMsg = document.createElement('div');
    botMsg.className = 'message message-bot';
    botMsg.innerHTML = '<span class="typing-indicator"><span></span></span>';
    chat.appendChild(botMsg);
    chat.scrollTop = chat.scrollHeight;

    taskBegin('vault-chat', 'Thinking…');

    const controller = new AbortController();
    const chatTimeout = setTimeout(() => controller.abort(), _chatAbortMs());

    try {
        await saveSelectedModels();
        const chatParams = getVaultChatParams();
        const resp = await secureFetch('/api/obsidian/chat', {
            method: 'POST',
            signal: controller.signal,
            body: JSON.stringify({
                message: question,
                provider: getActiveProvider(),
                llm: getSelectedModel(),
                embed: getSelectedEmbed(),
                top_k: chatParams.top_k,
                similarity_cutoff: chatParams.similarity_cutoff,
                prompt_mode: chatParams.prompt_mode,
                temperature: chatParams.temperature,
                system_prompt: chatParams.system_prompt,
                hybrid_enabled: chatParams.hybrid_enabled,
                reranker_enabled: chatParams.reranker_enabled,
                agent_enabled: chatParams.agent_enabled,
                agent_max_iterations: chatParams.agent_max_iterations,
                mmr_enabled: chatParams.mmr_enabled,
                mmr_lambda: chatParams.mmr_lambda,
                query_expansion: chatParams.query_expansion,
                num_queries: chatParams.num_queries,
                rerank_pool_ceiling: chatParams.rerank_pool_ceiling,
            })
        });
        if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            throw new Error(data.error || 'Vault chat failed.');
        }

        let fullAnswer = '';
        // Agent-mode rendering state. Created lazily on the first
        // `iteration` event so non-agent chats are visually identical
        // to today.
        const traceCtx = { trace: null, currentIter: null, iterCount: 0 };

        for await (const payload of readSSE(resp)) {
            if (payload.error) {
                throw new Error(payload.error);
            } else if (payload.info) {
                const note = document.createElement('div');
                note.className = 'message message-info';
                note.setAttribute('role', 'status');
                note.textContent = payload.info;
                chat.insertBefore(note, botMsg);
                chat.scrollTop = chat.scrollHeight;
            } else if (payload.iteration !== undefined) {
                _renderAgentIteration(payload.iteration, traceCtx, botMsg, chat);
            } else if (typeof payload.thought === 'string') {
                _renderAgentThought(payload.thought, traceCtx, botMsg, chat);
            } else if (payload.tool_call) {
                _renderAgentToolCall(payload.tool_call, traceCtx, botMsg, chat);
            } else if (payload.tool_result) {
                _renderAgentToolResult(payload.tool_result, traceCtx, botMsg, chat);
            } else if (payload.token) {
                if (fullAnswer === '') botMsg.innerHTML = '';
                fullAnswer += payload.token;
                _renderAnswer(botMsg, fullAnswer);
            }
        }
        // Attach the copy button only after streaming completes so the
        // per-token innerHTML re-render does not clobber it.  Copies the
        // raw markdown (fullAnswer) rather than the rendered HTML so the
        // user pastes something useful into their notes.
        if (fullAnswer) {
            _attachCopyButton(botMsg, () => fullAnswer, 'Copy answer');
        }
    } catch (e) {
        // Replace the empty/partial bot bubble with an actionable error
        // boundary. Retry removes the question bubble and re-asks; the
        // user's original text is restored to the input first.
        botMsg.remove();
        const msg = e.name === 'AbortError'
            ? 'Response timed out. The model may be overloaded — please try again.'
            : ('Error: ' + e.message);
        _showVaultError(msg, () => {
            userMsg.remove();
            if (input) input.value = question;
            chatWithVault();
        });
    } finally {
        clearTimeout(chatTimeout);
        _isQuerying = false;
        taskEnd('vault-chat');
        chat.scrollTop = chat.scrollHeight;
    }
}

export function clearVaultChat() {
    // Server holds no chat history (each /api/obsidian/chat call is independent),
    // so clearing is purely a DOM operation. We refuse while a query is in flight
    // so the in-progress bot message is not orphaned mid-stream.
    if (_isQuerying) {
        const statusDiv = document.getElementById('obsidian-status-msg');
        if (statusDiv) setStatusA11y(statusDiv, 'Wait for the current response to finish before clearing.', false);
        return;
    }
    const chat = document.getElementById('obsidian-chat');
    if (chat) chat.innerHTML = '';
    const statusDiv = document.getElementById('obsidian-status-msg');
    if (statusDiv) setStatusA11y(statusDiv, '', false);
}

export async function pauseVaultIndex() {
    try {
        await secureFetch('/api/obsidian/pause', { method: 'POST' });
    } catch (e) {
        console.error('Pause error:', e);
    }
}

export async function resumeVaultIndex() {
    await indexVault();
}

export async function cancelVaultIndex() {
    try {
        await secureFetch('/api/obsidian/cancel', { method: 'POST' });
    } catch (e) {
        console.error('Cancel error:', e);
    }
    _isQuerying = false;
    taskEnd('vault-index');
    _updateIndexButtons('idle');
    setStatusA11y(document.getElementById('obsidian-status-msg'), 'Indexing cancelled.', false);
}

export async function addExclusion() {
    const status = document.getElementById('excl-status');
    if (status) status.textContent = '';
    try {
        const cfgResp = await secureFetch('/api/config');
        const cfg = await cfgResp.json();
        const vaultPath = cfg.obsidian_vault_path || '';
        if (!vaultPath) {
            if (status) status.textContent = 'Choose a vault folder before adding exclusions.';
            return;
        }
        const resp = await secureFetch('/api/native-pick-folder', {
            method: 'POST',
            body: JSON.stringify({ constrain_to_vault: true, base_path: vaultPath })
        });
        const data = await resp.json();
        if (!resp.ok) {
            if (status) status.textContent = data.error || 'Could not add exclusion.';
            return;
        }
        if (data.cancelled) return;
        if (data.path) {
            const currentExcl = [...(cfg.vault_exclude_dirs || [])];
            const relPath = data.relative_path || toVaultRelativePath(vaultPath, data.path);
            if (!relPath) {
                if (status) status.textContent = 'Select a subfolder inside the vault.';
                return;
            }
            if (relPath && !currentExcl.includes(relPath)) {
                currentExcl.push(relPath);
                const saveResp = await secureFetch('/api/config', {
                    method: 'POST',
                    body: JSON.stringify({ vault_exclude_dirs: currentExcl })
                });
                if (!saveResp.ok) {
                    const saveData = await saveResp.json().catch(() => ({}));
                    if (status) status.textContent = saveData.error || 'Could not save exclusion.';
                    return;
                }
                renderExclusions();
                if (status) status.textContent = `Excluded ${relPath}.`;
            } else if (status) {
                status.textContent = `${relPath} is already excluded.`;
            }
        }
    } catch (e) {
        if (status) status.textContent = 'Add exclusion error: ' + e.message;
        console.error('Add exclusion error:', e);
    }
}

function toVaultRelativePath(vaultPath, selectedPath) {
    if (!vaultPath || !selectedPath) return '';
    const normalVault = vaultPath.replace(/\/+$/, '');
    if (selectedPath === normalVault) return '';
    if (!selectedPath.startsWith(normalVault + '/')) return '';
    return selectedPath.slice(normalVault.length + 1).replace(/^\/+|\/+$/g, '');
}

function renderVaultWarnings(warnings) {
    const box = document.getElementById('vault-warning');
    if (!box) return;
    const filtered = warnings.filter(Boolean);
    box.textContent = filtered.join(' ');
    box.style.display = filtered.length ? 'block' : 'none';
}

export async function removeExclusion(path) {
    try {
        const cfgResp = await secureFetch('/api/config');
        const cfg = await cfgResp.json();
        const currentExcl = cfg.vault_exclude_dirs || [];
        const filtered = currentExcl.filter(p => p !== path);
        await secureFetch('/api/config', {
            method: 'POST',
            body: JSON.stringify({ vault_exclude_dirs: filtered })
        });
        renderExclusions();
    } catch (e) {
        console.error('Remove exclusion error:', e);
    }
}

/**
 * Re-render the exclusion list from /api/config. Each row is built with
 * createElement/textContent because vault folder names are user-controlled and
 * could otherwise inject markup (the canonical no-innerHTML-for-user-strings case).
 */
export async function renderExclusions() {
    const list = document.getElementById('excl-list');
    if (!list) return;

    try {
        const cfgResp = await secureFetch('/api/config');
        const cfg = await cfgResp.json();
        const exclusions = cfg.vault_exclude_dirs || [];

        list.innerHTML = '';
        if (exclusions.length === 0) {
            list.innerHTML = '<div style="color:var(--text-secondary);font-size:12px;">No exclusions configured.</div>';
        } else {
            exclusions.forEach(path => {
                // Built with createElement/textContent — vault folder names
                // are user-controlled and may contain HTML-special chars, so
                // interpolating them into innerHTML would inject markup.
                const item = document.createElement('div');
                item.className = 'rt-item';
                const name = document.createElement('span');
                name.className = 'rt-item-name';
                name.title = path;
                name.textContent = path.split('/').pop() || path;
                const btn = document.createElement('button');
                btn.className = 'btn btn-outline btn-sm';
                btn.dataset.path = path;
                btn.textContent = 'Remove';
                btn.onclick = () => removeExclusion(path);
                item.appendChild(name);
                item.appendChild(btn);
                list.appendChild(item);
            });
        }
    } catch (e) {
        console.error('Render exclusions failed:', e);
    }
}

export async function openImageExtsModal() {
    const input = document.getElementById('image-exts-input');
    const status = document.getElementById('image-exts-status');
    if (status) status.textContent = '';
    if (input) input.value = '';
    try {
        const cfgResp = await secureFetch('/api/config');
        const cfg = await cfgResp.json();
        const exts = Array.isArray(cfg.vault_image_exts) ? cfg.vault_image_exts : [];
        if (input) input.value = exts.join(', ');
    } catch (e) {
        if (status) status.textContent = 'Could not load current extensions.';
        console.error('Load image exts failed:', e);
    }
    openModal('image-exts-modal');
}

export async function saveImageExts() {
    const input = document.getElementById('image-exts-input');
    const status = document.getElementById('image-exts-status');
    if (!input) return;
    const tokens = input.value
        .split(/[,\s]+/)
        .map(t => t.trim())
        .filter(Boolean);
    try {
        const resp = await secureFetch('/api/config', {
            method: 'POST',
            body: JSON.stringify({ vault_image_exts: tokens })
        });
        if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            if (status) status.textContent = data.error || 'Could not save.';
            return;
        }
        if (status) status.textContent = 'Saved.';
        closeModal('image-exts-modal');
    } catch (e) {
        if (status) status.textContent = 'Save error: ' + e.message;
        console.error('Save image exts failed:', e);
    }
}

/**
 * One-shot status fetch at tab init: sets the index buttons, renders any warnings,
 * and paints the current prewarm banner up-front (so it shows even if prewarm
 * finished before the UI mounted), handing off to pollPrewarmStatus only when a
 * non-terminal prewarm stage is still in flight.
 */
export async function refreshIndexState() {
    try {
        const resp = await secureFetch('/api/obsidian/status');
        const data = await resp.json();
        if (data.vault_path) {
            const displayEl = document.getElementById('vault-path-display');
            if (displayEl) displayEl.textContent = data.vault_path;
        }
        _updateIndexButtons(data.state);
        if (Array.isArray(data.warnings) && data.warnings.length) {
            renderVaultWarnings(data.warnings);
        }
        // Render the current prewarm state up-front so the banner appears
        // even if the prewarm finished before the UI mounted.  pollPrewarmStatus
        // then takes over for the in-flight stages.
        _renderPrewarmBanner(data.prewarm_status || 'idle', data.prewarm_message || '');
        const ps = data.prewarm_status;
        if (ps && ps !== 'ready' && ps !== 'skipped' && ps !== 'idle' && ps !== 'error') {
            pollPrewarmStatus();
        }
    } catch (e) {
        console.error('Initial vault state fetch failed:', e);
    }
}
