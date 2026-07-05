/**
 * Prompt Hub — read-only system-prompt transparency panel.
 *
 * Fetches GET /api/prompts (the in-memory capture of the effective system
 * prompt last sent per workflow) and renders one collapsible row per workflow.
 * A row that has never fired this session shows a "not captured yet"
 * placeholder so the full workflow set is always visible.
 *
 * Module-hierarchy rule (CLAUDE.md §JS Module Hierarchy): this module imports
 * ONLY ui.js + api.js. Prompt text is user/vault-derived and can be large, so
 * it is rendered exclusively via textContent — never innerHTML — which also
 * makes the panel injection-proof by construction.
 */
import { announceStatus, makeLatestGate } from './ui.js';
import { secureFetch, safeJson, logError } from './api.js';

// Latest-wins gate: a slow /api/prompts response from an earlier Refresh must
// not overwrite a newer one (the makeLatestGate discipline used by audit.js).
const _loadGate = makeLatestGate();

/**
 * Format an epoch-seconds capture time into a short local time string.
 * Returns "" for a missing/invalid stamp so the caller can omit the line.
 */
function _fmtCapturedAt(epochSeconds) {
    if (!epochSeconds || typeof epochSeconds !== 'number') return '';
    try {
        // Locale-formatted in the viewer's timezone; the server stores UTC epoch.
        return new Date(epochSeconds * 1000).toLocaleString();
    } catch (_) {
        return '';
    }
}

/** Create an element with an optional class and textContent (never innerHTML). */
function _el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
}

/** Build the small role/status badge shown in a row's summary. */
function _badge(text, className) {
    const b = _el('span', `prompt-badge ${className}`, text);
    return b;
}

/**
 * Render one workflow row. Captured rows open by default and expose the prompt
 * in a <pre> with a Copy button; un-captured rows show a muted placeholder.
 */
function _renderRow(wf) {
    const row = _el('details', 'prompt-row');
    // Open captured rows so the user sees content immediately; leave the
    // never-run placeholders collapsed to reduce noise.
    if (wf.captured) row.open = true;

    const summary = _el('summary', 'prompt-row-summary');
    summary.appendChild(_el('span', 'prompt-row-label', wf.label));
    // Role badge: system prompts vs the vision/OCR user-role instructions.
    if (wf.role === 'user-instruction') {
        summary.appendChild(_badge('user-instruction', 'prompt-badge-userrole'));
    } else {
        summary.appendChild(_badge('system', 'prompt-badge-system'));
    }
    if (wf.captured) {
        const pm = [wf.provider, wf.model].filter(Boolean).join(' · ');
        if (pm) summary.appendChild(_badge(pm, 'prompt-badge-model'));
    } else {
        summary.appendChild(_badge('not captured yet', 'prompt-badge-empty'));
    }
    row.appendChild(summary);

    const body = _el('div', 'prompt-row-body');
    body.appendChild(_el('p', 'prompt-row-desc', wf.description));

    if (!wf.captured) {
        body.appendChild(_el('p', 'prompt-row-placeholder',
            'Run this workflow at least once this session to capture the exact prompt sent.'));
        row.appendChild(body);
        return row;
    }

    // Meta line: capture time, retrieved-context size, and the query it answered.
    const metaBits = [];
    const when = _fmtCapturedAt(wf.captured_at);
    if (when) metaBits.push(`sent ${when}`);
    if (wf.context_chunks) metaBits.push(`${wf.context_chunks} context chunk(s)`);
    if (wf.query) metaBits.push(`query: "${wf.query}"`);
    if (metaBits.length) body.appendChild(_el('p', 'prompt-row-meta', metaBits.join(' — ')));

    // Server-supplied note (e.g. the local-vault "slots filled at query time").
    if (wf.note) body.appendChild(_el('p', 'prompt-row-note', wf.note));

    const pre = _el('pre', 'prompt-row-text', wf.system_prompt || '');
    body.appendChild(pre);

    const copyBtn = _el('button', 'btn btn-outline btn-sm prompt-copy-btn', 'Copy');
    copyBtn.type = 'button';
    copyBtn.addEventListener('click', () => {
        // navigator.clipboard is available in the PyWebView renderer; degrade
        // silently (the text is still selectable) if a write is rejected.
        try {
            navigator.clipboard.writeText(wf.system_prompt || '');
            copyBtn.textContent = 'Copied';
            setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1200);
        } catch (_) { /* selection fallback — no-op */ }
    });
    body.appendChild(copyBtn);

    row.appendChild(body);
    return row;
}

/**
 * Fetch /api/prompts and (re)render the panel. Safe to call repeatedly; the
 * latest-wins gate discards a stale in-flight response. Never throws — a fetch
 * failure renders an inline error line instead of breaking the tab.
 */
export async function loadPrompts() {
    const list = document.getElementById('prompts-list');
    if (!list) return;
    // enter() returns an isCurrent() predicate; a later loadPrompts() supersedes
    // this one, so a slow response never overwrites a newer render.
    const isCurrent = _loadGate.enter();
    announceStatus('Loading prompts…');
    let data;
    try {
        const resp = await secureFetch('/api/prompts');
        data = await safeJson(resp);
    } catch (err) {
        logError('prompts load failed', err);
        if (isCurrent()) {
            list.replaceChildren(_el('p', 'prompt-row-placeholder',
                'Could not load prompts. Is the app server running?'));
        }
        return;
    }
    // Only the newest Refresh gets to paint (older responses are abandoned).
    if (!isCurrent()) return;

    const workflows = (data && Array.isArray(data.workflows)) ? data.workflows : [];
    const frag = document.createDocumentFragment();

    if (data && data.enabled === false) {
        frag.appendChild(_el('p', 'prompt-row-note',
            'Prompt capture is disabled in LLM Settings — rows will not fill in until it is re-enabled.'));
    }
    if (!workflows.length) {
        frag.appendChild(_el('p', 'prompt-row-placeholder', 'No workflows to display.'));
    } else {
        for (const wf of workflows) frag.appendChild(_renderRow(wf));
    }
    list.replaceChildren(frag);
    const nCaptured = workflows.filter((w) => w.captured).length;
    announceStatus(`Prompt Hub loaded: ${nCaptured} of ${workflows.length} captured.`);
}
