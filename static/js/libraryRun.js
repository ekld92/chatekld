// Library: observation → text preparation → reviewed writes → confirmed search.
//
// Library-workflow plan, lot 6 (2026-09-21). A thin surface over the lot 5
// routes (`/api/library/*`): it never computes a write itself. The review/apply
// engine below is preceded by preparation and followed by a fresh index preview:
//
//   1. Analyse changes — begin a session (with the model chosen for THIS run,
//      never the global provider), observe, propose names / tags, plan the
//      note mirror.
//   2. Review — every proposal is a row the user ticks; nothing is pre-ticked
//      (plan §4: human review of names and tags, with a global "select all").
//   3. Apply selected — confirmed inline, naming every destination; the
//      result is shown per row (done / failed / uncertain) with an absolute
//      time. The last session can be recovered from this browser; server
//      termination before a durable receipt can still leave uncertainty.
//
// Imports only api.js and ui.js (the module hierarchy, tests/js/
// moduleHierarchy.test.js); audit.js mounts it for the `library_update`
// section. All text goes through textContent; nothing is parsed as HTML.

import { secureFetch, consumeSSE } from './api.js';
import { confirmInline, announceStatus, statusLine, relativeStamp, taskBegin, taskEnd, openModal } from './ui.js';

const LOCAL = ['ollama', 'lm_studio'];
const ONLINE = ['openai', 'anthropic', 'google', 'deepseek', 'zai', 'openrouter'];
const PREFERENCES = 'papermind-library-preferences';
function preferences() {
    try { return JSON.parse(localStorage.getItem(PREFERENCES) || '{}'); } catch (_) { return {}; }
}
function savePreference(key, value) {
    try { localStorage.setItem(PREFERENCES, JSON.stringify({...preferences(), [key]: value})); } catch (_) { /* optional preference */ }
}

const LAST_SESSION = 'papermind-library-last-session';
// A choice for the next launch in this page, not a persisted consent or a
// statement about an operation already running.
let nextOnlineChoice = false;
const mountCleanups = new WeakMap();

/** Per-mount state, reset on every mount (a new analysis is a new review). */
function _freshState() {
    return { session: null, review: null, busy: false, abort: null };
}

function el(tag, attrs = {}, text = null) {
    const n = document.createElement(tag);
    // WKWebView may skip implicit button/checkbox tab stops under macOS's
    // keyboard-navigation setting. Explicit tab stops keep this workflow
    // usable without changing the user's system preferences.
    if (['button', 'input', 'select'].includes(tag) && !('tabindex' in attrs)) n.setAttribute('tabindex', '0');
    for (const [k, v] of Object.entries(attrs)) {
        if (k === 'class') n.className = v;
        else if (v !== false && v != null) n.setAttribute(k, v === true ? '' : String(v));
    }
    if (text != null) n.textContent = String(text);
    return n;
}

async function authorizeDesktop(button, access) {
    try {
        const response = await secureFetch('/api/zotlink/local-status');
        const state = await response.json();
        if (!response.ok || !state.supports_writes || !state.server_id) throw new Error(state.error || 'Open Zotero Desktop and enable its local API.');
        confirmInline(button, {
            message: 'Request authorization from this Zotero Desktop server? Allow permits one write; Always Allow permits writes for this session. No writes are retried automatically.',
            confirmLabel: 'Authorize Desktop',
            onConfirm: async () => {
                try {
                    const granted = await secureFetch('/api/zotlink/authorize-local', {method: 'POST', body: JSON.stringify({confirm: true, server_id: state.server_id})});
                    const result = await granted.json();
                    if (!granted.ok) throw new Error(result.error || 'Authorization failed.');
                    access.textContent = 'Authorization requested. Check access again before writing.';
                } catch (error) { access.textContent = error.message; }
            },
        });
    } catch (error) { access.textContent = error.message; }
}

/** Saved evidence only: preparation totals never stand for corpus coverage. */
export function mountLibraryReadiness(host) {
    const section = el('details', {'aria-label': 'Search status'});
    section.appendChild(el('summary', {}, 'Search status'));
    section.appendChild(el('p', {class: 'control-hint'}, 'Refresh status reads saved evidence only. It does not scan, extract or launch an update.'));
    const status = el('p', {role: 'status', 'aria-live': 'polite'}, 'Checking saved evidence…');
    const counts = el('p', {class: 'control-hint'},
        'Current vault totals — discovered files: unknown; complete extraction caches: unknown; '
        + 'sources represented in the current index: unknown; pending or changed: unknown; deferred: unknown. '
        + 'These totals have not been measured by this status read.');
    const details = el('details');
    details.appendChild(el('summary', {}, 'Count scope and freshness'));
    const freshness = el('p', {class: 'control-hint'});
    details.append(counts, freshness);
    const refresh = el('button', {type: 'button', class: 'btn btn-outline'}, 'Refresh status');
    const read = async () => {
        if (refresh.disabled) return;
        refresh.disabled = true;
        status.textContent = 'Checking saved evidence…';
        freshness.textContent = '';
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 10000);
        try {
            const response = await secureFetch('/api/obsidian/readiness', {signal: controller.signal});
            if (!response.ok) throw new Error('Status unavailable');
            const evidence = await response.json();
            status.textContent = `${evidence.message || 'Search readiness unknown.'} `
                + (Number.isSafeInteger(evidence.recorded_sources) && evidence.recorded_sources >= 0
                    ? `Saved manifest: ${evidence.recorded_sources} distinct sources with chunks. ` : 'Saved source count unavailable. ')
                + 'Current searchable sources: unknown.';
            freshness.textContent = `Manifest recorded: ${evidence.manifest_at || 'unknown'}; index recorded: ${evidence.indexed_at || 'unknown'}. `
                + 'Scope: saved evidence for the configured vault, not a live inventory. '
                + `Observed: ${evidence.observed_at || 'unknown'}. `
                + (evidence.next_step || 'Review a search update to check changes.');
        } catch (_) {
            status.textContent = 'Search evidence unavailable. Current counts remain unknown. Refresh status to try again, or review a search update to check current changes.';
        } finally { clearTimeout(timer); refresh.disabled = false; }
    };
    refresh.addEventListener('click', read);
    section.append(status, details, refresh);
    host.appendChild(section);
    void read();
}

export function extractionPreviewCounts(preview) {
    const count = key => Number.isInteger(preview[key]) && preview[key] >= 0 ? preview[key] : 'unknown';
    return `Extraction preview scope: ${count('discovered_count')} discovered PDF/image sources; `
        + `${count('source_count')} selected for preparation; ${count('reused_count')} complete caches reused; `
        + `${count('pending_count')} previously deferred within that selection. `
        + 'Observed for this preview only; these are not counts of searchable documents. ';
}

/** Mount the run into `host`. Returns the state object (tests read it). */
export function mountLibraryRun(host) {
    mountCleanups.get(host)?.();
    mountCleanups.delete(host);
    const st = _freshState();
    host.textContent = '';
    const mountMarker = el('span', {hidden: true});
    host.appendChild(mountMarker);
    const ownsMount = () => host.isConnected && host.contains(mountMarker);
    host.appendChild(el('h3', {}, 'Review library update'));
    host.appendChild(el('p', { class: 'control-hint' },
        '1 · Check changes and associations → 2 · Prepare document text → 3 · Review names, tags and optional notes → 4 · Apply selected changes → 5 · Update search. '
        + 'Each write requires review and confirmation. Unchanged text and the existing embedding model are reused.'));
    const scan = el('button', {type: 'button', class: 'btn btn-outline'}, 'Refresh library scan');
    scan.addEventListener('click', () => confirmInline(scan, {
        message: 'After Attanger and Better BibTeX finish, scan the library and save a fresh Audit report. This reads library files and updates application scan caches.',
        confirmLabel: 'Run Scan', tone: 'confirm',
        onConfirm: () => document.getElementById('audit-run-btn')?.click(),
    }));
    const advanced = el('details', {class: 'library-run-advanced'});
    advanced.appendChild(el('summary', {}, 'Scan, settings and recovery ?'));
    advanced.appendChild(scan);
    const settings = el('button', {type: 'button', class: 'btn btn-outline'}, 'Open settings and recovery');
    settings.addEventListener('click', () => openModal('settings-modal'));
    advanced.appendChild(settings);
    advanced.appendChild(el('p', {class: 'control-hint'}, 'Run a fresh scan after Attanger and Better BibTeX finish. The daily review covers tracked changes; it does not certify historical names.'));
    host.appendChild(advanced);
    const update = el('button', {type: 'button', class: 'btn btn-outline', id: 'library-daily-update', tabindex: '0'}, 'Search only: review update');
    const prepare = el('button', {type: 'button', class: 'btn btn-primary', id: 'library-daily-prepare', tabindex: '0'}, 'Review library update');
    const online = el('fieldset', {class: 'library-run-block library-run-online'});
    online.appendChild(el('legend', {}, 'Allow online OCR and figure analysis in the next preview?'));
    for (const [value, label] of [['yes', 'Yes'], ['no', 'No']]) {
        const radio = el('input', {type: 'radio', name: 'library-online-choice',
            id: 'library-online-' + value, value, checked: (value === 'yes') === nextOnlineChoice});
        radio.addEventListener('change', () => { if (radio.checked) nextOnlineChoice = value === 'yes'; });
        const choice = el('label', {for: radio.id});
        choice.append(radio, ' ' + label);
        online.appendChild(choice);
    }
    host.appendChild(online);
    host.appendChild(prepare);
    advanced.appendChild(update);
    host.appendChild(el('p', {class: 'control-hint'},
        'No online calls unless you choose Yes and authorize the preview. Documents needing online processing remain pending when you choose No.'));
    const scope = el('p', {class: 'control-hint'},
        'The preview checks new or changed documents and reuses complete text. Online tags require a separate decision in the optional review.');
    host.appendChild(scope);
    const observedScope = el('p', {class: 'control-hint', id: 'library-observation-scope'},
        'Daily scope: new/changed items and outstanding tracked work. Historical naming/tagging compliance is not certified.');
    host.appendChild(observedScope);
    const exceptions = el('details', {id: 'library-extraction-exceptions'});
    exceptions.appendChild(el('summary', {}, 'Extraction exceptions and scope'));
    const exceptionList = el('div'); exceptions.appendChild(exceptionList);
    host.appendChild(exceptions);

    const associations = el('button', {type: 'button', class: 'btn btn-outline'}, 'Review document associations');
    associations.addEventListener('click', () => document.querySelector('[data-section=library_documents]')?.click());
    exceptions.appendChild(associations);
    const history = el('button', {type: 'button', class: 'btn btn-outline'}, 'All extraction outcomes and targeted retries');
    history.addEventListener('click', () => {
        openModal('index-settings-modal');
        document.dispatchEvent(new CustomEvent('library-extraction-history'));
    });
    exceptions.appendChild(history);
    let preparing = false;
    let extractionBusy = false;
    const syncPrepare = () => {
        prepare.disabled = preparing || extractionBusy || st.busy;
        update.disabled = prepare.disabled || !!st.continuationBlocked;
        const skipControl = host.querySelector('#library-skip-proposals');
        if (skipControl) skipControl.disabled = prepare.disabled || !!st.continuationBlocked;
    };
    update.addEventListener('click', async () => {
        if (preparing || extractionBusy || st.busy || st.continuationBlocked) return;
        preparing = true;
        syncPrepare();
        scope.textContent = 'Checking changed documents… Review the preview before starting.';
        host.querySelector('#library-preparation-next').textContent = 'Review the search preview and confirm. No proposal analysis or library file changes are required.';
        try {
            let pending = Promise.resolve();
            document.dispatchEvent(new CustomEvent('library-index-preview', {detail: {
                waitUntil: task => { pending = task; }, anchor: update, prepareOnly: false, initiatingAction: 'search_only',
                onlineChoice: host.querySelector('#library-online-yes').checked,
                onPreview: preview => {
                    if (!ownsMount()) return;
                    st.extractionPreview = preview;
                    scope.textContent = extractionPreviewCounts(preview) + 'Review and confirm below.';
                }
            }}));
            await pending;
        } catch (error) { scope.textContent = error.message; }
        finally { preparing = false; syncPrepare(); }
    });
    const busyChanged = event => {
        if (!host.isConnected) { document.removeEventListener('library-extraction-busy', busyChanged); return; }
        extractionBusy = event.detail.busy;
        syncPrepare();
    };
    document.addEventListener('library-extraction-busy', busyChanged);
    prepare.addEventListener('click', async () => {
        if (st.busy || preparing || extractionBusy) return;
        preparing = true;
        syncPrepare();
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 130000);
        scope.textContent = 'Checking changes and associations… No extraction has started.';
        taskBegin('library-observation', scope.textContent);
        try {
            // Observation precedes preparation, without invoking a model or a library writer.
            const r = await secureFetch('/api/library/sessions', {method: 'POST', body: '{}', signal: controller.signal});
            const session = await r.json();
            if (!r.ok) throw new Error(session.error || 'Observation session unavailable');
            if (!ownsMount()) return;
            const observed = await secureFetch('/api/library/observe', {method: 'POST', signal: controller.signal,
                body: JSON.stringify({session: session.session.id})});
            const observation = await observed.json();
            if (!observed.ok) throw new Error(observation.error || 'Observation refused');
            if (!ownsMount()) return;
            clearTimeout(timer);
            const newCount = observation.new ? Object.values(observation.new).reduce((n, rows) => n + rows.length, 0) : 'unknown';
            observedScope.textContent = `Observed now: ${newCount} new units; ${observation.modified_notes?.length ?? 'unknown'} modified notes; `
                + `${observation.open?.length ?? 'unknown'} tracked units with outstanding work; ${observation.settling?.length ?? 'unknown'} still settling. `
                + (observation.initialized ? 'First observation established a baseline; it did not certify historical names or tags. ' : '')
                + 'Review document associations for ambiguous or unmapped documents. Preparation counts below cover PDF/image text, not the whole library.';
            scope.textContent = `Daily scope: ${(observation.open || []).length} tracked items with outstanding work; ${(observation.settling || []).length} still settling. Preparing the text preview…`;
            taskEnd('library-observation');
            let pending = Promise.resolve();
            document.dispatchEvent(new CustomEvent('library-index-preview', {detail: {
                waitUntil: task => { pending = task; },
                anchor: prepare, prepareOnly: true, initiatingAction: 'library_review',
                onlineChoice: host.querySelector('#library-online-yes').checked,
                onPreview: preview => {
                    if (!ownsMount()) return;
                    st.extractionPreview = preview;
                    scope.textContent = extractionPreviewCounts(preview) + 'Historical naming compliance has not been checked.';
                    exceptionList.replaceChildren();
                    for (const source of preview.sources || []) {
                        exceptionList.appendChild(el('p', {}, `${source.source}: ${source.known_failure?.reason || source.selection_reason || 'preparation needed'}`));
                    }
                    if (preview.sources_truncated) exceptionList.appendChild(el('p', {}, 'Preview limited to 100 documents. Full outcomes remain in extraction history.'));
                }
            }}));
            await pending;
        } catch (error) { scope.textContent = error.name === 'AbortError'
            ? 'Observation timed out. It may still be finishing on the server; inspect status before trying again.' : error.message; }
        finally { clearTimeout(timer); taskEnd('library-observation'); preparing = false; syncPrepare(); }
    });
    const extractionStatus = event => {
        if (!host.isConnected) { document.removeEventListener('library-extraction-status', extractionStatus); return; }
        const result = event.detail;
        extractionBusy = result.busy ?? (['running', 'promoting', 'indexing'].includes(result.state)
            || (['indexed', 'completed_with_omissions'].includes(result.state) && result.bm25_status === 'preparing'));
        syncPrepare();
        const allDeferred = result.source_count > 0 && result.deferred_count >= result.source_count;
        if (result.prepare_only && result.state === 'text_prepared' && !allDeferred && !st.review && !st.hasApplied) {
            prepare.className = 'btn btn-outline';
            form.hidden = false;
        }
        const next = host.querySelector('#library-preparation-next');
        if (next) next.textContent = result.state === 'unknown' ? 'Status unknown. Wait for reconnection before starting or retrying work.' : allDeferred && result.state === 'text_prepared' ? 'No new source text was prepared. Review exceptions and explicitly retry after resolving the cause. Existing-text review remains available under Scan, settings and recovery.' : ['promotion_or_index_incomplete', 'index_incomplete'].includes(result.state)
            ? 'Next: inspect History and saved recovery evidence before continuing. Completed cache replacements have not been undone.'
            : ['failed', 'extraction_incomplete', 'interrupted', 'time_limit', 'cancelled'].includes(result.state)
            ? (result.failure?.failure_scope || result.failure_scope) === 'global'
                ? 'Preparation stopped. Review the operation error below before retrying. Unattempted documents are not defective; no new search update completed.'
                : 'Next: review the document outcomes below before retrying.'
            : ['indexed', 'completed_with_omissions'].includes(result.state)
                ? result.bm25_status === 'preparing' ? 'Search update is finishing. Wait for lexical search preparation; do not start another update.'
                    : !['ready', 'disabled'].includes(result.bm25_status) ? 'Vector search updated; lexical search readiness is unknown. Inspect operation details before treating the update as complete.'
                    : result.state === 'completed_with_omissions' ? 'Search updated with pending documents. Review their outcomes below.'
                        : 'Search update finished. Names, tags and note changes are optional.'
                : result.state === 'idle' ? 'Start with Review library update. Preparation never updates search. Review or skip proposals, then confirm the final search update.'
                    : result.state === 'running' || result.state === 'promoting' || result.state === 'indexing'
                        ? 'Work is in progress. Wait for the result before starting another update.'
                        : 'Optional: review proposals, apply your selection, then review a search update.';
        const failures = result.failure
            ? [result.failure, ...(result.source_errors || []).filter(row => row.source !== result.failure.source || row.error_detail !== result.failure.error_detail)]
            : result.source_errors?.length ? result.source_errors : result.error_detail ? [result] : result.recent_results || [];
        exceptionList.replaceChildren(...failures.map(row => {
            const card = el('article', {class: 'source-result'});
            const context = [row.provider, row.model, row.http_status ? `HTTP ${row.http_status}` : '', row.provider_code].filter(Boolean).join(' · ');
            const detail = row.error_detail || row.error || (row.status === 'request_failed'
                ? 'The provider error details were not recorded for this older attempt.' : (row.status || '').replaceAll('_', ' '));
            card.append(el('strong', {}, row.source || 'Preparation error'), el('p', {}, context), el('p', {}, detail));
            if (row.next_step) card.append(el('p', {}, `Next: ${row.next_step}`));
            else if (row.status === 'request_failed') card.append(el('p', {}, 'Next: check provider settings, then explicitly retry this document to obtain a current error.'));
            if (['initial_memory_pressure', 'critical_memory_pressure', 'swap_growth_over_512_mib',
                'memory_monitor_unavailable'].includes(row.stop_reason)) {
                const check = el('button', {type: 'button', class: 'btn btn-outline'}, 'Check memory');
                const memory = el('p', {role: 'status'});
                check.addEventListener('click', async () => {
                    check.disabled = true;
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), 5000);
                    try {
                        const response = await secureFetch('/api/online-ocr/resources', {signal: controller.signal});
                        const state = await response.json();
                        if (!response.ok) throw new Error(state.error || 'Memory check unavailable');
                        memory.textContent = (state.message || 'Memory status unavailable.') + ' No operation was started.';
                    } catch (error) { memory.textContent = 'Memory check unavailable. No operation was started.'; }
                    finally { clearTimeout(timer); check.disabled = false; }
                });
                card.append(check, memory);
            }
            if (row.failure_scope === 'global' && row.http_status) {
                const settings = el('button', {type:'button', class:'btn btn-outline'}, 'Review extraction settings');
                settings.addEventListener('click', () => openModal('settings-modal'));
                card.append(settings);
            }
            return card;
        }));
        if (!prepare.disabled && (result.failure || result.global_failure || result.failed_count || result.deferred_count)) {
            exceptions.open = true;
            history.className = 'btn btn-primary';
        }
    };
    document.addEventListener('library-extraction-status', extractionStatus);
    mountCleanups.set(host, () => {
        document.removeEventListener('library-extraction-status', extractionStatus);
        document.removeEventListener('library-extraction-busy', busyChanged);
    });
    host.appendChild(el('p', {id: 'library-preparation-status', role: 'status', class: 'control-hint'}));
    host.appendChild(el('p', {id: 'library-preparation-next', class: 'control-hint'}, 'Start with Review library update. Preparation never updates search. Review or skip proposals, then confirm the final search update.'));
    host.appendChild(el('h4', {}, '3 · Review names, tags and optional notes'));
    host.appendChild(el('p', {class: 'control-hint'},
        'Use preparation before reviewing library changes. You can skip this when you only want to update search. Applying proposals still requires selecting rows and confirming.'));
    mountLibraryReadiness(host);
    const undo = el('details', { class: 'library-run-undo' });
    undo.appendChild(el('summary', {}, 'How to undo'));
    const ul = el('ul');
    for (const line of [
        'Renames and note creations: Settings → History and Restore.',
        'Tags and pdf: lists: Tagging → Restore.',
        'Rewritten note regions (the mirror): the vault’s git.',
        'Analysis itself only records app tracking, cache and spend, and may call the selected model.',
    ]) ul.appendChild(el('li', {}, line));
    undo.appendChild(ul);
    host.appendChild(undo);

    // "Recover last run": a small line ABOVE the form (lot 5), shown when
    // this browser remembers a session.
    let savedSession = null;
    try { savedSession = localStorage.getItem(LAST_SESSION); } catch (_) { /* storage unavailable */ }
    const recoveryLine = el('p', { class: 'control-hint library-run-recovery' });
    const recover = el('button', { type: 'button', class: 'btn btn-outline btn-sm' }, 'Recover last run');
    recoveryLine.append(recover, ' Reads the last session opened in this browser. Older sessions remain available through the CLI.');
    recoveryLine.hidden = !savedSession;
    host.appendChild(recoveryLine);
    const showRecovery = () => { recoveryLine.hidden = false; };

    // ── Step 1: what to propose, and the model for this run ─────────────
    const form = el('fieldset', { class: 'library-run-model' });
    form.appendChild(el('legend', {}, 'Daily scope: new/changed items and outstanding tracked work'));
    const what = el('div', { class: 'library-run-block' });
    what.appendChild(el('h4', {}, 'What to propose'));
    const tagBox = el('input', { type: 'checkbox', id: 'library-run-tags', checked: preferences().proposeTags === true });
    tagBox.addEventListener('change', () => savePreference('proposeTags', tagBox.checked));
    const tagLabel = el('label', { for: 'library-run-tags' }, ' Propose tags for new imports');
    const row = el('div', { class: 'library-run-row' });
    row.append(tagBox, tagLabel);
    what.appendChild(row);
    what.appendChild(el('p', { class: 'control-hint' }, 'Names and the note mirror are always analysed.'));
    form.appendChild(what);

    const modelBlock = el('div', { class: 'library-run-block', id: 'library-run-model-block' });
    modelBlock.appendChild(el('h4', {}, 'Model'));
    // Visible labels (audit remediation 2026-09-23, U13): a placeholder and
    // an aria-label were the only names, so a sighted user saw no label.
    const provider = el('select', { id: 'library-run-provider' });
    for (const [group, names] of [['On this Mac', LOCAL], ['Online (paid, sends text)', ONLINE]]) {
        const og = el('optgroup', { label: group });
        for (const n of names) og.appendChild(el('option', { value: n }, n));
        provider.appendChild(og);
    }
    const model = el('input', { type: 'text', id: 'library-run-model', placeholder: 'model id, e.g. llama3.1:8b',
        autocomplete: 'off', list: 'library-run-models' });
    const models = el('datalist', { id: 'library-run-models' });
    const modelHint = el('p', { class: 'control-hint', id: 'library-run-model-hint' });
    const budget = el('input', { type: 'number', id: 'library-run-budget', min: '0', max: '100', step: '0.01',
        placeholder: 'USD limit' });
    const budgetLabel = el('label', { for: 'library-run-budget' }, 'Spending limit (USD) ');
    const noLimit = el('input', { type: 'checkbox', id: 'library-run-no-limit' });
    const noLimitLabel = el('label', { for: 'library-run-no-limit' }, ' No spending limit — I accept the cost');
    const costRow = el('div', { class: 'library-run-row', id: 'library-run-cost' });
    costRow.append(budgetLabel, budget, noLimit, noLimitLabel);
    const onlineNote = el('p', { class: 'control-hint', id: 'library-run-online-note' });
    const isOnline = () => ONLINE.includes(provider.value) || /(-|:)cloud$|:cloud/i.test(model.value);
    // Priced model ids (GET /api/pricing), read once. An UNPRICED online
    // model cannot be held to a spending limit, so only "No spending limit"
    // runs it (audit remediation 2026-09-23, L3): its budget field is hidden
    // and the box is required. `null` until known — then nothing is hidden,
    // and the server (the authority) refuses with the same sentence.
    // Fetched with the model list (nothing is fetched until tags are wanted).
    let priced = null;
    let pricingAsked = false;
    const isUnpriced = () => isOnline() && priced !== null && !priced.has(model.value.trim());
    const loadPricing = async () => {
        if (pricingAsked) return;
        pricingAsked = true;
        try {
            const r = await secureFetch('/api/pricing');
            const d = await r.json().catch(() => ({}));
            if (r.ok && d && d.models && owned()) { priced = new Set(Object.keys(d.models)); syncOnline(); }
        } catch (_) { /* leave `priced` unknown */ }
    };
    const syncOnline = () => {
        const online = isOnline();
        // The limit and the "no limit" box exist only for a choice that costs
        // money; a local model shows neither (2026-09-22).
        costRow.hidden = !online;
        budget.hidden = isUnpriced();
        budgetLabel.hidden = budget.hidden;
        onlineNote.textContent = online
            ? 'Online: each paper’s first pages (or its metadata) are sent to ' + provider.value
              + '. A spending limit is an estimate, not a billing guarantee. With no limit, every '
              + 'proposal is paid for; the spend is still reported (unknown for an unpriced model).'
            : 'Local provider: uses your configured endpoint. If the model is down, the run says so — it never switches to an online provider.';
    };
    // The model list for the chosen provider, prefilled with the configured
    // model (`configured` from GET /api/models?provider=); free text stays
    // allowed, and a typed value is never overwritten by a later list.
    let modelsSeq = 0;
    const loadModels = async () => {
        const seq = ++modelsSeq;
        loadPricing();
        const name = provider.value;
        modelHint.textContent = 'Listing models…';
        try {
            const r = await secureFetch('/api/models?provider=' + encodeURIComponent(name));
            const d = await r.json().catch(() => ({}));
            if (seq !== modelsSeq || !owned()) return;
            models.textContent = '';
            const list = Array.isArray(d.models) ? d.models : [];
            for (const m of list) models.appendChild(el('option', { value: String(m) }));
            if ((model.value === '' || model.dataset.auto === '1') && typeof d.configured === 'string' && d.configured) {
                model.value = d.configured; model.dataset.auto = '1';
                syncOnline();
            }
            modelHint.textContent = r.ok
                ? (list.length ? `${list.length} model(s) listed for ${name}; the configured one is prefilled.` : `No model listed for ${name}; type an id.`)
                : (d.error || 'Model list unavailable; type an id.');
        } catch (exc) {
            if (seq === modelsSeq && owned()) modelHint.textContent = 'Model list unavailable; type an id.';
        }
    };
    // Ticking "no limit" clears and disables the limit: the two say opposite
    // things, and the server refuses them together.
    noLimit.addEventListener('change', () => {
        if (noLimit.checked) budget.value = '';
        budget.disabled = noLimit.checked;
    });
    provider.addEventListener('change', () => { syncOnline(); loadModels(); });
    model.addEventListener('input', () => { delete model.dataset.auto; syncOnline(); });
    const row2 = el('div', { class: 'library-run-row' });
    row2.append(el('label', { for: 'library-run-provider' }, 'Provider '), provider,
        el('label', { for: 'library-run-model' }, ' Model '), model, models);
    modelBlock.append(row2, modelHint, costRow, onlineNote);
    // Only when tags are wanted: the model exists for the tag proposals.
    modelBlock.hidden = !tagBox.checked;
    tagBox.addEventListener('change', () => {
        modelBlock.hidden = !tagBox.checked;
        if (tagBox.checked && !models.childElementCount) loadModels();
    });
    const modelDetails = el('details', {open: true});
    modelDetails.appendChild(el('summary', {}, 'Tag model and external-call authorization'));
    modelDetails.appendChild(modelBlock);
    form.appendChild(modelDetails);
    syncOnline();
    const analyseBtn = el('button', { type: 'button', class: 'btn btn-primary', id: 'library-run-analyse' },
        'Analyse changes');
    form.appendChild(analyseBtn);
    form.hidden = true;
    host.appendChild(form);
    const cachedReview = el('button', {type: 'button', class: 'btn btn-outline'}, 'Review using existing extraction');
    cachedReview.addEventListener('click', () => {
        form.hidden = false;
        prepare.className = 'btn btn-outline';
        scope.textContent = 'Reviewing available extraction only. Missing text may limit proposals; no extraction is implied.';
    });
    advanced.appendChild(cachedReview);
    const existing = el('details');
    existing.appendChild(el('summary', {}, 'Separately review existing-library naming and tagging'));
    existing.appendChild(el('p', {class: 'control-hint'}, 'Daily tracking does not certify historical compliance. These existing review tools use their own selected scope and confirmations; opening them does not reset tracking or authorize model calls.'));
    for (const [label, selector] of [['Review existing names', '[data-section="library_checks"]'], ['Review existing tags', '[data-report="tagging"]']]) {
        const button = el('button', {type: 'button', class: 'btn btn-outline'}, label);
        button.addEventListener('click', () => document.querySelector(selector)?.click());
        existing.appendChild(button);
    }
    advanced.appendChild(existing);
    const skip = el('button', {type: 'button', class: 'btn btn-outline', id: 'library-skip-proposals'}, 'Skip proposals and review search update');
    skip.addEventListener('click', () => document.dispatchEvent(new CustomEvent('library-index-preview', {
        detail: {anchor: skip, prepareOnly: false, initiatingAction: 'final_search_update', onlineChoice: host.querySelector('#library-online-yes').checked}
    })));
    form.appendChild(skip);

    document.dispatchEvent(new CustomEvent('library-extraction-refresh'));
    // Stop (audit remediation 2026-09-23, U5, maintainer decision: analyse
    // AND apply). Aborting the request closes the stream; the server's SSE
    // guard cancels the worker, which stops before its next write and keeps
    // its partial result on the session (read back after an apply stop).
    const stopBtn = el('button', { type: 'button', class: 'btn btn-outline', id: 'library-run-stop', hidden: true },
        'Stop');
    stopBtn.addEventListener('click', () => { if (st.abort) st.abort.abort(); });
    host.appendChild(stopBtn);

    const status = el('div', { id: 'library-run-status' });
    host.appendChild(status);
    // Filled from the server's `(i/N)` counters (U6); hidden otherwise.
    const progress = el('progress', { id: 'library-run-progress', max: '1', value: '0', hidden: true,
        'aria-label': 'Progress of the run' });
    host.appendChild(progress);
    const review = el('div', { id: 'library-run-review' });
    host.appendChild(review);
    const results = el('div', { id: 'library-run-results' });
    host.appendChild(results);
    // A departed mount must never announce, focus, or unlock another view.
    const owned = () => host.isConnected && host.contains(status);
    const active = () => owned() && (!host.closest('.content-area')
        || host.closest('.content-area').classList.contains('active'));

    // `at` is MILLISECONDS (ui.js::statusLine / relativeStamp): seconds made
    // the age decades and the stamp was never drawn (review 2026-09-21).
    const say = (text, tone = 'info') => {
        if (st.busy) taskBegin('library-' + (st.session || 'preparing'), text);
        if (!active()) return;
        statusLine(status, { tone, text, at: Date.now() });
        announceStatus(text);
    };
    // A progress frame (U6): the visible line and the bar follow EVERY frame;
    // the screen reader hears every 25th and the last (the audit.js
    // `_announceBatchProgress` rule) — a line per paper was a flood.
    const sayProgress = (text) => {
        taskBegin('library-' + (st.session || 'preparing'), text);
        if (!active()) return;
        statusLine(status, { tone: 'info', text, at: Date.now() });
        const m = /\((\d+)\/(\d+)\)\s*$/.exec(String(text));
        if (!m) { progress.hidden = true; announceStatus(text); return; }
        const i = Number(m[1]); const n = Number(m[2]);
        progress.hidden = false; progress.max = n; progress.value = i;
        if (i === n || i % 25 === 0) announceStatus(text);
    };
    status.setAttribute('tabindex', '-1');
    const setBusy = (on) => {
        st.busy = on;
        if (!on) { taskEnd('library-' + (st.session || 'preparing')); taskEnd('library-preparing'); }
        if (!owned()) return;
        // Inputs too, not only buttons; a control marked `data-locked` (an
        // already-applied review) stays disabled when the run ends.
        for (const b of host.querySelectorAll('button, input, select')) {
            b.disabled = on || b.dataset.locked === '1';
        }
        if (!on && noLimit.checked) budget.disabled = true;
        stopBtn.hidden = !on;
        stopBtn.disabled = false;
        if (!on) { progress.hidden = true; st.abort = null; }
        analyseBtn.textContent = on ? 'Working…' : 'Analyse changes';
        syncPrepare();
        // Disabling the focused control drops focus to <body>; keep it on the
        // status line, where progress is announced (review 2026-09-21).
        if (on && active()) status.focus();
    };

    // Restore the saved run (audit remediation 2026-09-23, U2/U3, maintainer
    // decision: automatically on re-entry). A section or tab change remounts
    // this panel, and the review the user was ticking used to vanish. The
    // session keeps its latest analysis and apply: whichever is NEWER is
    // shown — a review when no apply followed it (its rows are still live),
    // otherwise the apply's result. `quiet` (the automatic restore) paints
    // without focus or announcement; the button announces as before.
    const restoreSaved = async ({ quiet = false } = {}) => {
        if (!savedSession) return;
        const r = await secureFetch('/api/library/sessions/' + encodeURIComponent(savedSession));
        const d = await r.json().catch(() => ({}));
        if (!owned()) return;
        if (!r.ok) { if (!quiet) say(d.error || 'Saved run unavailable for this library.', 'error'); return; }
        const sess = d.session || {};
        const last = (sess.applies || []).slice(-1)[0];
        const analysis = sess.last_analysis;
        if (analysis && analysis.result && (!last || analysis.at > last.at)) {
            st.session = savedSession;
            st.review = analysis.result;
            form.hidden = true;
            prepare.className = "btn btn-outline";
            renderReview(review, st, analysis.result, { say, sayProgress, setBusy, results, active, status, retry: retryFailed,
                quiet: quiet || !active(), restoredAt: analysis.at * 1000 });
            syncPrepare();
        } else if (last) {
            st.session = savedSession;
            st.hasApplied = true;
            st.continuationBlocked = true;
            syncPrepare();
            form.hidden = true;
            prepare.className = "btn btn-outline";
            renderResults(results, last.result, say, new Map(), last.at * 1000, {
                quiet: quiet || !active(), retry: retryFailed, requireReview: sess.applies.length > 1 });
        } else if (!quiet) {
            say('This session has no saved apply result. An interrupted write may still be unresolved; inspect before retrying.', 'warn');
        }
    };
    async function retryFailed(row, anchor) {
        if (st.busy) return;
        const targets = Object.fromEntries(['finder', 'zotero', 'note'].map(k => [k, row.targets?.[k] === 'failed']));
        if (targets.zotero) {
            try {
                const response = await secureFetch('/api/tagging/zotero-available');
                const access = await response.json();
                if (!response.ok || access.writable !== true) {
                    const message = results.querySelector('#library-retry-access');
                    const button = results.querySelector('#library-retry-authorize');
                    message.textContent = access.note || 'Zotero Desktop access is unavailable.';
                    button.hidden = access.mode === 'web';
                    if (button.hidden) message.append(' Change transport and refresh the scan before retrying.');
                    (button.hidden ? message : button).focus();
                    return;
                }
            } catch (_) {
                const message = results.querySelector('#library-retry-access');
                message.textContent = 'Zotero access could not be verified.';
                message.focus();
                return;
            }
        }
        if (!anchor.isConnected) return;
        confirmInline(anchor, {message: `Retry only the failed tag destinations for ${row.citation_key}: ${Object.keys(targets).filter(k => targets[k]).join(', ')}?`,
            confirmLabel: 'Retry failed destinations', onConfirm: () => runApply(st, {
                tags: [{unit: row.unit, citation_key: row.citation_key, tags: row.tags, targets, retry_failed_only: true}],
            }, {say, sayProgress, setBusy, results, active, status, fs: review, keyOf: new Map(), retry: retryFailed, requireReview: true})});
    }
    recover.addEventListener('click', async () => {
        if (st.busy || !savedSession) return;
        setBusy(true);
        say('Reading the saved run…');
        try { await restoreSaved(); }
        catch (exc) { say('Could not read the saved run: ' + (exc.message || exc), 'error'); }
        finally { setBusy(false); }
    });
    if (savedSession) restoreSaved({ quiet: true }).catch(() => { /* the button remains */ });

    analyseBtn.addEventListener('click', async () => {
        if (st.busy) return;
        const wantTags = tagBox.checked;
        const llm = wantTags ? { provider: provider.value, model: model.value.trim() } : null;
        // `no_limit` and `budget_usd` never ride together: the box clears the
        // field, and the server refuses the pair anyway.
        if (llm && isOnline() && noLimit.checked) llm.no_limit = true;
        else if (llm && budget.value !== '') llm.budget_usd = Number(budget.value);
        if (wantTags && !llm.model) { say('Choose the model that proposes tags for this run.', 'error'); model.focus(); return; }
        if (wantTags && isUnpriced() && !noLimit.checked) {
            say(`${llm.model} has no price, so a spending limit cannot bound it. Tick “No spending limit” to accept its cost, or choose a priced model.`, 'error');
            noLimit.focus(); return;
        }
        setBusy(true);
        review.textContent = '';
        results.textContent = '';
        let errored = false;
        try {
            const r = await secureFetch('/api/library/sessions', { method: 'POST',
                body: JSON.stringify(llm ? { llm } : {}) });
            const d = await r.json().catch(() => ({}));
            if (!r.ok) { say(d.error || 'Could not begin the run.', 'error'); return; }
            if (!owned()) return;
            const previous = savedSession;
            st.session = d.session.id;
            if (!active()) return;
            showRecovery();
            recover.disabled = true;
            say('Analysing…');
            let done = null;
            st.abort = new AbortController();
            try {
                await consumeSSE('/api/library/analyze', { requireDone: false, method: 'POST', signal: st.abort.signal,
                    body: JSON.stringify({ session: st.session, propose_tags: wantTags }) }, {
                    onInfo: (m) => sayProgress(m),
                    onOther: (f) => { if (f.library_analyze) done = f.library_analyze; },
                    // U4: remembered, so the "no analysis received" line does
                    // not overwrite the error the user needs to read.
                    onError: (e) => { errored = true; say(e, 'error'); },
                });
            } catch (exc) {
                if (exc && exc.name === 'AbortError') { errored = true; say('Stopped. Nothing was written; analyse again when ready.', 'warn'); }
                else throw exc;
            }
            if (done) {
                // U4: the browser remembers a session only once it has a
                // COMPLETED analysis; a failed one keeps the previous id, so
                // "Recover last run" still opens something real.
                savedSession = st.session;
                try { localStorage.setItem(LAST_SESSION, savedSession); } catch (_) { /* CLI recovery remains available */ }
            } else {
                savedSession = previous;
            }
            if (done && active()) {
                st.review = done;
                form.hidden = true;
                renderReview(review, st, done, { say, sayProgress, setBusy, results, active, status, retry: retryFailed });
            } else if (!done && !errored) say('No completed analysis was received. Analyse again to obtain a review.', 'error');
        } catch (exc) {
            say(String(exc && exc.message || exc), 'error');
        } finally {
            setBusy(false);
        }
    });
    return st;
}

/** One review group: a heading with its count and its own all/none, then a
 *  table of the SAME shape for every group (lot 5, 2026-09-22). */
function groupTable(fs, title, count, columns, { fixed = false } = {}) {
    const head = el('div', { class: 'library-run-group-head' });
    head.appendChild(el('h4', {}, `${title} (${count})`));
    const table = el('table', { class: 'audit-table library-run-group' });
    const hr = el('tr');
    for (const c of columns) hr.appendChild(el('th', { scope: 'col' }, c));
    table.appendChild(hr);
    const boxes = () => [...table.querySelectorAll('input[type=checkbox][data-kind]')];
    // `fixed` (U15): a group of ONE fixed row (the mirror) has nothing for
    // All/None to do but repeat its own checkbox.
    if (count > 0 && !fixed) {
        const all = el('button', { type: 'button', class: 'btn btn-outline btn-sm' }, 'All');
        all.setAttribute('aria-label', `Select all ${title}`);
        all.addEventListener('click', () => { for (const cb of boxes()) cb.checked = true; });
        const none = el('button', { type: 'button', class: 'btn btn-outline btn-sm' }, 'None');
        none.setAttribute('aria-label', `Select no ${title}`);
        none.addEventListener('click', () => { for (const cb of boxes()) cb.checked = false; });
        head.append(all, none);
    }
    fs.append(head, table);
    return table;
}

function renderReview(host, st, review, ctx) {
    host.textContent = '';
    // `quiet` (a review restored on re-entry, U2): painted, never announced
    // or focused — the user did not just ask for it.
    const say = ctx.quiet ? () => {} : ctx.say;
    const focus = (node) => { if (!ctx.quiet) node.focus(); };
    if (ctx.restoredAt) host.appendChild(el('p', { class: 'control-hint' },
        `Restored review from ${new Date(ctx.restoredAt).toLocaleString()} — analyse again if the library changed since.`));
    const blocked = review.blocked || [];
    st.continuationBlocked = !review.observation || !!review.cancelled || blocked.length > 0;
    const obs = review.observation;
    for (const b of blocked) host.appendChild(el('p', { class: 'warning-banner' }, b));
    if (review.cancelled) host.appendChild(el('p', { class: 'warning-banner' }, 'The analysis was cancelled before it finished.'));
    if (!obs) {
        // Stopped before observing (scan running, no scan, Zotero unreadable):
        // there is nothing to review, and "nothing new" would be a claim
        // nobody checked (review 2026-09-21).
        say(blocked.join(' ') || (review.cancelled ? 'The analysis was cancelled.' : 'No observation was returned. Analyse again.'), 'warn');
        focus(ctx.status);
        return;
    }
    if (obs.initialized) {
        // Through the app's one outcome line (U12), not a hand-built copy of
        // its classes (which lacked the glyph that carries the tone).
        host.appendChild(statusLine(el('p'), { tone: 'info', text:
            'First run: today’s library was recorded as the starting point for new-import names and tags. '
            + 'The mirror below may still offer existing notes.' }));
    }
    const newCounts = Object.entries(obs.new || {}).filter(([, v]) => v && v.length);
    host.appendChild(el('p', {}, newCounts.length
        ? 'New since the last run: ' + newCounts.map(([k, v]) => `${v.length} ${k.replace(/_/g, ' ')}`).join(' · ')
        : 'Nothing new since the last run.'));

    const articles = new Map((review.renames || []).map(row => [row.unit, {...row}]));
    for (const row of review.tags || []) articles.set(row.unit, {...articles.get(row.unit), ...row});
    if (articles.size) {
        const overview = el('details', {class: 'library-run-block library-article-overview'});
        overview.appendChild(el('summary', {}, 'Article overview · names, tags and extraction'));
        overview.appendChild(el('p', {class: 'control-hint'}, 'Extraction is the preparation preview, not an accuracy guarantee. See the preparation result above for completion. Review and select writes in the groups below.'));
        const table = groupTable(overview, 'Articles', articles.size, ['Article', 'Current name', 'Proposed name', 'Tags', 'Extraction at preview', 'Action'], {fixed: true});
        const sources = new Map((st.extractionPreview?.sources || []).map(row => [row.source, row]));
        for (const row of articles.values()) {
            const source = sources.get(row.rel);
            const extraction = source?.known_failure ? 'Known failure · deferred' : source ?
                (source.eligible ? 'OCR required' : 'Native text available') : 'Not checked individually';
            const tr = el('tr');
            for (const value of [row.citation_key || row.unit, row.name || row.rel || '—',
                row.proposal || '—', (row.proposed || []).map(tag => tag.tag).join(', ') || '—', extraction,
                row.error || row.reason || 'Review proposed values']) tr.appendChild(el('td', {}, value));
            table.appendChild(tr);
        }
        host.appendChild(overview);
    }

    const skip = el('button', {type: 'button', class: 'btn btn-outline'}, 'Skip selected proposals and review search update');
    skip.addEventListener('click', () => document.dispatchEvent(new CustomEvent('library-index-preview', {
        detail: {anchor: skip, prepareOnly: false, initiatingAction: 'final_search_update', onlineChoice: false}
    })));
    // Uncertain tracked writes need resolution, not a route around the result gate.
    skip.disabled = !!review.cancelled || blocked.length > 0;
    if (skip.disabled) skip.dataset.locked = '1';
    const fs = el('fieldset', { class: 'library-run-review' });
    const legend = el('legend', { tabindex: '-1' }, '3 · Review — tick what should be written');
    fs.appendChild(legend);
    fs.appendChild(skip);
    host.appendChild(fs);
    let seq = 0;
    const nextId = () => `library-run-cb-${++seq}`;   // never derived from data: no collisions
    const keyOf = new Map((review.tags || []).map(r => [r.unit, r.citation_key]));
    for (const r of review.renames || []) if (r.citation_key) keyOf.set(r.unit, r.citation_key);
    const groupsPresent = [];

    // Names
    const renames = (review.renames || []).filter(r => r.can_apply);
    if (review.renames_unavailable) fs.appendChild(el('p', { class: 'control-hint' }, 'Renaming: ' + review.renames_unavailable));
    if (!renames.length) fs.appendChild(el('p', {class: 'control-hint'}, 'No rename proposed in this tracked selection. Historical library naming has not been certified.'));
    if (renames.length) {
        groupsPresent.push('names');
        const t = groupTable(fs, 'Names', renames.length, ['Write', 'Now', 'Proposed', 'Source']);
        for (const r of renames) {
            const tr = el('tr');
            const cb = el('input', { type: 'checkbox', id: nextId(), 'data-kind': 'rename', 'data-unit': r.unit,
                'aria-label': `Rename ${r.name} to ${r.proposal}` });
            cb._row = r;
            const td = el('td'); td.appendChild(cb); tr.appendChild(td);
            tr.appendChild(el('td', {}, r.name));
            tr.appendChild(el('td', {}, r.proposal));
            tr.appendChild(el('td', { class: 'control-hint' }, r.reason || 'from the record'));
            t.appendChild(tr);
        }
    }
    const refused = (review.renames || []).filter(r => !r.can_apply && !r.already_named);
    if (refused.length) {
        const det = el('details');
        det.appendChild(el('summary', {}, `${refused.length} file(s) the renamer will not rename`));
        for (const r of refused) det.appendChild(el('p', {}, `${r.rel}: ${r.error || r.reason || 'refused'}`));
        fs.appendChild(det);
    }

    // PDF links in existing notes (lot 2): absent/stale rows are selectable;
    // dirty/unreadable ones are listed with the reason, no box.
    const pdfRows = ((review.pdf_links || {}).rows || []);
    const pdfSelectable = pdfRows.filter(r => r.selectable);
    if (pdfRows.length) {
        groupsPresent.push('PDF links');
        const t = groupTable(fs, 'PDF links in notes', pdfSelectable.length, ['Write', 'Now', 'Proposed', 'Source']);
        for (const r of pdfRows) {
            const tr = el('tr');
            const td = el('td');
            if (r.selectable) {
                const cb = el('input', { type: 'checkbox', id: nextId(), 'data-kind': 'pdf', 'data-key': r.citation_key,
                    'aria-label': `Write the pdf: list of ${r.note || r.citation_key}` });
                cb._row = r;
                td.appendChild(cb);
            } else {
                td.appendChild(el('span', { class: 'control-hint' }, r.reason || r.state));
            }
            tr.appendChild(td);
            tr.appendChild(el('td', {}, `${r.note || r.citation_key}: ${(r.listed || []).join(', ') || 'no pdf: list'}`));
            tr.appendChild(el('td', {}, (r.expected || []).join(', ')));
            tr.appendChild(el('td', { class: 'control-hint' }, 'the scan’s PDF↔record join'));
            t.appendChild(tr);
        }
    } else if ((review.pdf_links || {}).error) {
        fs.appendChild(el('p', { class: 'control-hint' }, 'PDF links: ' + review.pdf_links.error));
    }

    // Tags: one row per paper with checkable chips, and the three
    // destinations above (all on by default — maintainer decision, 2026-09-22).
    const tags = review.tags || [];
    const SOURCE = { pdf: 'from the PDF text', metadata: 'from metadata only' };
    const STATUS = { no_text: 'no usable text or metadata — nothing proposed',
        empty: 'the model proposed nothing (asking again may help)' };
    const TARGETS = [['finder', 'Finder tags'], ['zotero', 'Zotero entry'], ['note', 'Obsidian note']];
    const targetBoxes = {};
    if (tags.length) {
        groupsPresent.push('tags');
        const withTags = tags.filter(r => (r.proposed || []).length).length;
        const t = groupTable(fs, 'Tags', withTags, ['Write', 'Paper', 'Proposed', 'Source']);
        const tg = el('div', { class: 'library-run-row', role: 'group', 'aria-label': 'Tag destinations' });
        tg.appendChild(el('span', {}, 'Write tags to:'));
        for (const [key, label] of TARGETS) {
            const id = `library-run-target-${key}`;
            const cb = el('input', { type: 'checkbox', id, checked: preferences()[key] ?? (key === 'zotero') });
            cb.addEventListener('change', () => savePreference(key, cb.checked));
            targetBoxes[key] = cb;
            tg.append(cb, el('label', { for: id }, ' ' + label));
        }
        tg.appendChild(el('span', { class: 'control-hint' }, ' Existing tags are kept.'));
        fs.insertBefore(tg, t);
        for (const r of tags) {
            const key = r.citation_key || r.unit;
            const tr = el('tr');
            const td = el('td');
            const chips = el('div', { class: 'library-run-chips', role: 'group', 'aria-label': `Tags for ${key}` });
            if (r.error) {
                chips.appendChild(el('span', { class: 'control-hint' }, r.error));
            } else if ((r.proposed || []).length) {
                for (const p of r.proposed) {
                    const id = nextId();
                    const cb = el('input', { type: 'checkbox', id, 'data-kind': 'tag', 'data-unit': r.unit, value: p.tag,
                        'aria-label': `${p.tag} for ${key}` });
                    cb._row = r;
                    const chip = el('label', { for: id, class: 'library-run-chip' });
                    chip.append(cb, ' ' + p.tag);
                    chips.appendChild(chip);
                }
            } else {
                chips.appendChild(el('span', { class: 'control-hint' }, STATUS[r.status] || 'nothing proposed'));
            }
            // U15: the ticks are the chips; this cell only says so to a
            // screen reader (an arrow glyph was read aloud as noise).
            if ((r.proposed || []).length) td.appendChild(el('span', { class: 'sr-only' }, 'Tick the tags to write in the Proposed column'));
            else td.textContent = '—';
            tr.appendChild(td);
            tr.appendChild(el('td', {}, key));
            const chipsCell = el('td');
            chipsCell.appendChild(chips);
            tr.appendChild(chipsCell);
            const bits = [SOURCE[r.source], r.from_cache ? 'reused, nothing sent'
                : r.sent ? (r.sent.online ? `sent to ${r.sent.provider}` : 'local model') : null].filter(Boolean);
            tr.appendChild(el('td', { class: 'control-hint' }, bits.join('; ') || '—'));
            t.appendChild(tr);
        }
        const spend = tags.map(r => r.spend).filter(Boolean).pop();
        // A charge that could not be recorded (audit remediation 2026-09-23,
        // L4) is said out loud: the spend is then unknown, and a limited
        // session stops proposing.
        const unrecorded = tags.some(r => r.spend_error || r.spend_unrecorded);
        if (spend || unrecorded) fs.appendChild(el('p', { class: unrecorded ? 'warning-banner' : 'control-hint' },
            unrecorded ? 'The cost of a proposal could not be recorded: this session’s spend is now unknown, '
                + 'and a session with a spending limit proposes nothing more.'
                : spend.unknown ? 'Spend: unknown (unpriced model).' : `Spend this session: $${Number(spend.usd || 0).toFixed(4)}`));
    }

    // Mirror — offered only when the reviewed plan would do something.
    const m = review.mirror || {};
    const work = Number(m.to_create || 0) + Number(m.to_refresh || 0) + Number(m.to_baseline || 0)
        + Number(m.stale_bases || 0);
    if (m.plan_digest) {
        const parts = [`${m.to_create || 0} to create`, `${m.to_refresh || 0} to refresh`, `${m.to_baseline || 0} sync points`];
        if ((m.lossy_markup || []).length) parts.push(`${m.lossy_markup.length} with content that does not convert exactly (images, markup)`);
        const listed = Object.entries(m.listed || {}).filter(([, v]) => v && v.length);
        if (work > 0) {
            groupsPresent.push('the notes mirror');
            const t = groupTable(fs, 'Notes mirror', 1, ['Write', 'Now', 'Proposed', 'Source'], { fixed: true });
            const tr = el('tr');
            // One mirror per review: a fixed id is safe here.
            const mb = el('input', { type: 'checkbox', id: 'library-run-mirror', 'data-kind': 'mirror',
                'aria-label': 'Mirror Zotero notes into Obsidian' });
            const td = el('td'); td.appendChild(mb); tr.appendChild(td);
            tr.appendChild(el('td', {}, 'Zotero notes → Obsidian'));
            tr.appendChild(el('td', {}, parts.join(' · ')));
            tr.appendChild(el('td', { class: 'control-hint' }, 'today’s Zotero reading notes'));
            t.appendChild(tr);
        } else {
            fs.appendChild(el('h4', {}, 'Notes mirror (0)'));
            fs.appendChild(el('p', {}, 'The notes are in step: nothing to mirror.'));
        }
        if (listed.length) fs.appendChild(el('p', { class: 'control-hint' },
            'Listed, not written: ' + listed.map(([k, v]) => `${v.length} ${k.replace(/_/g, ' ')}`).join(' · ')));
    } else if (m.error) {
        fs.appendChild(el('p', { class: 'control-hint' }, 'Notes: ' + m.error));
    }

    // Leftover imported attachments (CLI proof run 2026-09-23, finding 4):
    // an imported copy whose file is gone while the same record already has
    // its linked copy in the biblio folder. Unticked by default and left out
    // of the global "Select all": approving one TRASHES it in Zotero
    // (reversible from Tagging → Restore). An annotated one is listed only.
    const leftovers = review.leftovers || [];
    if (leftovers.length) {
        groupsPresent.push('leftover attachments');
        const t = groupTable(fs, 'Leftover Zotero attachments', leftovers.filter(r => r.selectable).length,
            ['Trash', 'Now', 'Linked copy kept', 'Paper']);
        for (const r of leftovers) {
            const tr = el('tr');
            const td = el('td');
            if (r.selectable) {
                const cb = el('input', { type: 'checkbox', id: nextId(), 'data-kind': 'leftover', 'data-unit': r.unit,
                    'aria-label': `Trash the leftover attachment ${r.stored_path || r.item_key}` });
                td.appendChild(cb);
            } else {
                td.appendChild(el('span', { class: 'control-hint' }, r.reason || 'not selectable'));
            }
            tr.appendChild(td);
            tr.appendChild(el('td', {}, `${r.stored_path || r.item_key} (file missing)`));
            tr.appendChild(el('td', {}, r.linked_copy_rel || ''));
            tr.appendChild(el('td', { class: 'control-hint' }, r.citation_key || r.parent || ''));
            t.appendChild(tr);
        }
    }

    const boxes = () => [...fs.querySelectorAll('input[type=checkbox][data-kind]')];
    if (!boxes().length) {
        fs.appendChild(el('p', { class: 'control-hint' }, 'Nothing to apply.'));
        say(blocked.length ? blocked.join(' ') : 'Analysis complete. Nothing to apply.', blocked.length ? 'warn' : 'ok');
        const index = el('button', {type: 'button', class: 'btn btn-primary'}, 'Review search index update');
        index.addEventListener('click', () => document.dispatchEvent(new CustomEvent('library-index-preview', {detail: {anchor: index, initiatingAction: 'final_search_update', onlineChoice: document.getElementById('library-online-yes')?.checked === true}})));
        index.disabled = !!review.cancelled || blocked.length > 0;
        if (index.disabled) index.dataset.locked = '1';
        fs.appendChild(index);
        focus(legend);
        return;
    }
    // Two buttons, not one toggle (U8): a toggle labelled "Select all" that
    // sometimes UNselects said the opposite of what it did.
    const all = el('button', { type: 'button', class: 'btn btn-outline', id: 'library-run-select-all' },
        groupsPresent.some(g => g !== 'leftover attachments')
            ? `Select all (${groupsPresent.filter(g => g !== 'leftover attachments').join(', ')})` : 'Select all');
    // A leftover is never ticked by "Select all": each trash is its own decision.
    all.addEventListener('click', () => { for (const b of boxes()) if (b.dataset.kind !== 'leftover') b.checked = true; });
    const none = el('button', { type: 'button', class: 'btn btn-outline', id: 'library-run-select-none' },
        'Select none');
    none.addEventListener('click', () => { for (const b of boxes()) b.checked = false; });
    const apply = el('button', { type: 'button', class: 'btn btn-primary', id: 'library-run-apply' }, '4 · Apply selected');
    const access = el('div', {id: 'library-zotero-access', role: 'status', tabindex: '-1', class: 'control-hint'});
    const authorize = el('button', {type: 'button', class: 'btn btn-outline', hidden: true}, 'Authorize Zotero Desktop…');
    authorize.addEventListener('click', () => authorizeDesktop(authorize, access));
    fs.append(all, none, apply, access, authorize);
    say('Analysis complete. Review the proposed changes.');
    focus(legend);

    apply.addEventListener('click', async () => {
        const picked = boxes().filter(b => b.checked);
        const renameRows = picked.filter(b => b.dataset.kind === 'rename').map(b => b._row);
        const tagMap = new Map();
        for (const b of picked.filter(b => b.dataset.kind === 'tag')) {
            const r = b._row;
            if (!tagMap.has(r.unit)) tagMap.set(r.unit, { unit: r.unit, citation_key: r.citation_key, tags: [] });
            tagMap.get(r.unit).tags.push(b.value);
        }
        const withMirror = picked.some(b => b.dataset.kind === 'mirror');
        const pdfKeys = picked.filter(b => b.dataset.kind === 'pdf').map(b => b.dataset.key);
        const targets = Object.fromEntries(TARGETS.map(([k]) => [k, !!(targetBoxes[k] && targetBoxes[k].checked)]));
        for (const row of tagMap.values()) row.targets = { ...targets };
        const leftoverUnits = picked.filter(b => b.dataset.kind === 'leftover').map(b => b.dataset.unit);
        if (!renameRows.length && !tagMap.size && !withMirror && !pdfKeys.length && !leftoverUnits.length) { ctx.say('Tick at least one row.', 'warn'); return; }
        if (tagMap.size && !Object.values(targets).some(Boolean)) { ctx.say('Tick at least one tag destination.', 'warn'); return; }
        if (tagMap.size && targets.zotero) {
            access.textContent = 'Checking Zotero Desktop access…';
            apply.disabled = true;
            try {
                const response = await secureFetch('/api/tagging/zotero-available');
                const readiness = await response.json();
                if (!fs.isConnected) return;
                if (!response.ok || readiness.writable !== true) {
                    access.textContent = readiness.note || 'Zotero access could not be verified. Open Zotero Desktop and authorize access before applying.';
                    authorize.hidden = readiness.mode === 'web';
                    if (readiness.mode === 'web') access.append(' Change the Zotero transport in settings and refresh the library scan before using Desktop selections.');
                    (authorize.hidden ? access : authorize).focus();
                    return;
                }
                authorize.hidden = true;
                access.textContent = 'Zotero Desktop access verified for this session.';
            } catch (_) {
                access.textContent = 'Zotero access could not be verified. Check the connection before applying.';
                authorize.hidden = false;
                if (fs.isConnected) authorize.focus();
                return;
            } finally { apply.disabled = false; }
        }
        // Apply order (lot 2): mirror → pdf: links → tags → renames.
        const approvals = {
            mirror: withMirror ? { plan_digest: m.plan_digest, units: review.mirror_units || [],
                // The notes this mirror creates: only their ticked tags ride it.
                create_keys: Array.isArray(m.create_keys) ? m.create_keys : [] } : null,
            pdf_links: pdfKeys,
            tags: [...tagMap.values()],
            renames: renameRows.map(r => ({ unit: r.unit, rel: r.rel, new_name: r.proposal })),
        };
        if (leftoverUnits.length) approvals.leftovers = leftoverUnits;
        const parts = [];
        if (withMirror) parts.push('mirror Zotero notes into Obsidian (rewrites are undone from git)');
        if (pdfKeys.length) parts.push(`write the pdf: list into ${pdfKeys.length} note(s)`);
        if (tagMap.size) parts.push(`add tags to ${tagMap.size} paper(s) in ${TARGETS.filter(([k]) => targets[k]).map(([, l]) => l).join(', ')}`);
        if (renameRows.length) parts.push(`rename ${renameRows.length} PDF(s), update their Obsidian references and move their Zotero links`);
        if (leftoverUnits.length) parts.push(`move ${leftoverUnits.length} leftover Zotero attachment(s) to Zotero’s trash `
            + '(their linked copies stay; reversible from Tagging → Restore)');
        confirmInline(apply, {
            message: 'This will ' + parts.join('; ') + '.',
            // U7: the mirror REPLACES note regions (undone only from git), so
            // its confirm is the danger tone; renames/tags alone stay
            // 'confirm' (the maintenance.js convention).
            confirmLabel: 'Apply', tone: (withMirror || leftoverUnits.length) ? 'danger' : 'confirm',
            onConfirm: () => runApply(st, approvals, { ...ctx, fs, keyOf }),
        });
    });
}

/** An applied review is spent: its rows describe a library that no longer
 *  exists, and sending them again would record a done rename as failed
 *  (review 2026-09-21). Locked controls stay disabled after the run. */
function lockReview(fs, note) {
    for (const c of fs.querySelectorAll('input, button')) { c.dataset.locked = '1'; c.disabled = true; }
    fs.appendChild(el('p', { class: 'control-hint' }, note));
}

async function runApply(st, approvals, ctx) {
    ctx.setBusy(true);
    ctx.say('Applying…');
    let res = null;
    let failure = null;
    let started = false;
    let stopped = false;
    const begun = Date.now();
    let resultAt = begun;
    st.abort = new AbortController();
    try {
        await consumeSSE('/api/library/apply', { requireDone: false, method: 'POST', signal: st.abort.signal,
            body: JSON.stringify({ session: st.session, confirm: true, approvals }) }, {
            onOpen: () => { started = true; },
            onInfo: (m) => (ctx.sayProgress || ctx.say)(m),
            onOther: (f) => { if (f.library_apply) res = f.library_apply; },
            onError: (e) => { failure = e; ctx.say(e, 'error'); },
        });
    } catch (exc) {
        if (exc && exc.name === 'AbortError') { stopped = true; }
        else { failure = String(exc && exc.message || exc); ctx.say(failure, 'error'); }
    }
    if (!started && !stopped && failure) {
        // U1: refused BEFORE the run began (400/404/429/503 — another run in
        // progress, a busy store): nothing was written, so the review stays
        // editable and no "may already have been written" banner is shown.
        ctx.setBusy(false);
        return;
    }
    // Neutral: the run ended, however it ended (U1).
    lockReview(ctx.fs, 'Run finished — analyse again to see what is still open.');
    // A completed retry of one destination does not certify the rest of a
    // previous partial run. Only a fresh review can clear this gate.
    st.continuationBlocked = true;
    if (!res) {
        // The stream ended without a result (or was stopped, U5). The worker
        // may still be finishing — a Stop is seen at its next write — and it
        // keeps its result on the session: read it back, a few times.
        if (stopped) ctx.say('Stopping… reading what was applied.', 'warn');
        for (let attempt = 0; attempt < (stopped ? 10 : 1) && !res; attempt++) {
            if (attempt) await new Promise((r) => setTimeout(r, 400));
            try {
                const r = await secureFetch('/api/library/sessions/' + encodeURIComponent(st.session));
                const d = await r.json().catch(() => ({}));
                const last = ((d.session || {}).applies || []).slice(-1)[0];
                if (last && last.at * 1000 >= begun - 1000) { res = last.result; resultAt = last.at * 1000; }
            } catch (_) { /* reported below */ }
        }
    }
    ctx.setBusy(false);
    // U3: always painted while this mount owns the host — a user who switched
    // section and back found a stale "Applying…"; only the focus and the
    // announcement wait for the panel to be visible.
    if (!ctx.results.isConnected) return;
    const quiet = !ctx.active();
    if (res) { st.hasApplied = true; renderResults(ctx.results, res, ctx.say, ctx.keyOf, resultAt, { quiet, retry: ctx.retry, requireReview: ctx.requireReview }); }
    else {
        ctx.results.textContent = '';
        ctx.results.appendChild(el('p', { class: 'warning-banner', tabindex: '-1' },
            stopped ? 'Stopped. The saved result is not available yet: run a new analysis — it lists only what is still open.'
                : `The run did not report a result${failure ? ` (${failure})` : ''}. Some rows may already have `
                + 'been written. Run a new analysis: it lists only what is still open.'));
        if (!quiet) ctx.results.firstChild.focus();
    }
}

/** "Mark resolved…" for one uncertain row (audit remediation 2026-09-23,
 *  L2). An uncertain write blocks its paper until someone inspects it; this
 *  is how the user records what they found — it landed (done) or it did not
 *  (failed, retryable). Two confirmed choices, one POST to the resolve-only
 *  `/api/library/steps`. The server refuses anything but an uncertain step. */
function resolveControl(session, ref, outcomeCell, say) {
    const wrap = el('span', { class: 'library-run-resolve' });
    const open = el('button', { type: 'button', class: 'audit-link-btn' }, 'Mark resolved…');
    const choices = el('span', { hidden: true });
    const send = async (status, btn) => {
        try {
            const r = await secureFetch('/api/library/steps', { method: 'POST',
                body: JSON.stringify({ session, unit: ref.unit, step: ref.step, status, confirm: true }) });
            const d = await r.json().catch(() => ({}));
            if (!r.ok) { say(d.error || `Could not record it (HTTP ${r.status}).`, 'error'); btn.focus(); return; }
            outcomeCell.dataset.status = status;
            outcomeCell.textContent = status === 'done' ? 'Done (resolved by hand)' : 'Failed (resolved by hand) — retry from a new analysis';
            wrap.textContent = '';
            say(status === 'done' ? 'Recorded as done.' : 'Recorded as failed; the next analysis offers it again.', 'ok');
        } catch (exc) { say(String(exc && exc.message || exc), 'error'); }
    };
    const landed = el('button', { type: 'button', class: 'audit-link-btn' }, 'It landed');
    const notLanded = el('button', { type: 'button', class: 'audit-link-btn' }, 'It did not land');
    landed.addEventListener('click', () => confirmInline(landed, {
        message: 'Record this step as done? Only do this after checking that the write really happened.',
        confirmLabel: 'Record done', tone: 'confirm', onConfirm: () => send('done', landed) }));
    notLanded.addEventListener('click', () => confirmInline(notLanded, {
        message: 'Record this step as failed? The next analysis will offer it again.',
        confirmLabel: 'Record failed', tone: 'confirm', onConfirm: () => send('failed', notLanded) }));
    open.addEventListener('click', () => { choices.hidden = false; open.hidden = true; landed.focus(); });
    choices.append(' ', landed, ' ', notLanded);
    wrap.append(' ', open, choices);
    return wrap;
}

// Plain words for an outcome (lot 5). The raw status still appears, lower
// case, in the one-line summary, so a reader who knows the vocabulary finds it.
const OUTCOME = { done: 'Done', skipped: 'Not needed', failed: 'Failed', uncertain: 'Uncertain — check before retrying',
    partial: 'Partial', not_attempted: 'Not attempted' };
const outcomeWords = (status, detail) => {
    const word = OUTCOME[status] || String(status || 'unknown');
    if (status === 'failed' && detail) return `Failed — ${detail}`;
    return word;
};

function renderResults(host, res, say, keyOf = new Map(), at = Date.now(), { quiet = false, retry = null, requireReview = false } = {}) {
    host.textContent = '';
    // `quiet` (painted while the panel is hidden, or on re-entry): no focus,
    // no announcement (U3).
    if (quiet) say = () => {};
    const name = (unit) => keyOf.get(unit) || unit;
    // Groups in apply order: mirror → pdf: → tags → renames.
    const groups = [];
    if (res.mirror) {
        const mm = res.mirror;
        const detail = [mm.stopped ? `stopped (${mm.stopped})` : '', mm.git_detail,
                mm.error, `${mm.created || 0} created, ${mm.refreshed || 0} refreshed`,
                Object.keys(mm.failed || {}).length ? `failed: ${Object.keys(mm.failed).join(', ')}` : '',
                mm.git_restore_commit ? `rewrites undo from commit ${mm.git_restore_commit}` : ''].filter(Boolean).join('; ');
        groups.push(['Notes mirror', [['Zotero notes → Obsidian', mm.status, detail]]]);
    }
    if ((res.pdf_links || []).length) groups.push(['PDF links in notes', res.pdf_links.map(r => [r.citation_key, r.status,
        [r.changed === false && r.status === 'done' ? 'already current' : '', r.reason].filter(Boolean).join('; ')])]);
    // The 4th cell names the tracked (unit, step) so an uncertain row can be
    // resolved by hand (audit remediation 2026-09-23, L2).
    if ((res.tags || []).length) groups.push(['Tags', res.tags.map(r => [name(r.unit), r.status,
        [r.targets ? Object.entries(r.targets).map(([k, v]) => `${k}: ${v}`).join(', ') : '',
            r.reason, ...Object.values(r.reasons || {}), r.error, r.tracker].filter(Boolean).join('; '),
        { unit: r.unit, step: 'tag' }])]);
    if ((res.renames || []).length) groups.push(['Names', res.renames.map(r => [r.from ? `${r.from} → ${r.to}` : name(r.unit), r.status,
        [r.error, r.zotero, r.tracker].filter(Boolean).join('; '), { unit: r.unit, step: 'rename' }])]);
    if ((res.leftovers || []).length) groups.push(['Leftover Zotero attachments', res.leftovers.map(r => [
        r.item_key || name(r.unit), r.status,
        [r.reason, r.status === 'done' && res.leftovers_undo ? `undo: ${res.leftovers_undo}` : ''].filter(Boolean).join('; ')])]);
    const rows = groups.flatMap(([, g]) => g);
    const failed = rows.filter(r => r[1] !== 'done' && r[1] !== 'skipped');
    const counts = {};
    for (const r of rows) counts[r[1]] = (counts[r[1]] || 0) + 1;
    // Local time and "7 min ago" (U14, maintainer decision); the UTC
    // instant stays available in the tooltip.
    const heading = el('h4', { tabindex: '-1', title: new Date(at).toISOString().replace('T', ' ').slice(0, 19) + ' UTC' },
        `Result — ${new Date(at).toLocaleString()}${relativeStamp(at) ? ` (${relativeStamp(at)})` : ''}`);
    host.appendChild(heading);
    for (const row of res.tags || []) {
        if (retry && row.status === 'failed' && row.unit && row.citation_key && Array.isArray(row.tags)
                && Object.values(row.targets || {}).includes('failed')) {
            const button = el('button', {type: 'button', class: 'btn btn-outline'}, `Retry failed tag destinations for ${row.citation_key}…`);
            button.addEventListener('click', () => retry(row, button));
            host.appendChild(button);
        }
    }
    if (retry && (res.tags || []).some(row => row.status === 'failed' && row.targets?.zotero === 'failed')) {
        const access = el('p', {id: 'library-retry-access', role: 'status', tabindex: '-1', class: 'control-hint'});
        const authorize = el('button', {id: 'library-retry-authorize', type: 'button', class: 'btn btn-outline', hidden: true}, 'Authorize Zotero Desktop…');
        authorize.addEventListener('click', () => authorizeDesktop(authorize, access));
        host.append(access, authorize);
    }
    const indexLink = el('button', { type: 'button', class: 'btn btn-primary' }, 'Review search index update');
    indexLink.addEventListener('click', () => {
        document.dispatchEvent(new CustomEvent('library-index-preview', {detail: {anchor: indexLink, initiatingAction: 'final_search_update', onlineChoice: document.getElementById('library-online-yes')?.checked === true}}));
    });
    const unresolved = requireReview || failed.length || (res.ignored || []).length || res.cancelled || res.stopped || (res.not_reached || []).length
        || Object.keys(res.mirror?.note_tags_unwritten || {}).length || (res.mirror?.open || []).length
        || Object.keys(res.pdf_links_unwritten || res.mirror?.pdf_links_unwritten || {}).length;
    indexLink.disabled = !!unresolved;
    if (unresolved) indexLink.dataset.locked = '1';
    if (unresolved) host.appendChild(el('p', {class: 'warning-banner'}, 'Search continuation paused: inspect partial or uncertain outcomes, resolve them, then run a fresh review. No writes are retried automatically.'));
    host.appendChild(indexLink);
    host.appendChild(el('p', {id: 'library-index-status', role: 'status', class: 'control-hint'}));
    host.appendChild(el('p', { class: 'control-hint' },
        'Library changes do not update search or refresh Audit automatically. Use the final step above to preview and confirm the index update here. OCR authorization is separate from tag generation.'));

    const order = ['done', 'skipped', 'failed', 'uncertain', 'partial', 'not_attempted'];
    const summary = [...order, ...Object.keys(counts).filter(k => !order.includes(k))]
        .filter(k => counts[k] || k === 'done')
        .map(k => `${counts[k] || 0} ${k === 'skipped' ? 'not needed' : k.replace(/_/g, ' ')}`);
    host.appendChild(el('p', { class: 'library-run-summary' }, summary.join(' · ')));
    for (const [title, g] of groups) {
        host.appendChild(el('h5', {}, title));
        const t = el('table', { class: 'audit-table' });
        const head = el('tr');
        for (const h of ['Item', 'Outcome', 'Detail']) head.appendChild(el('th', { scope: 'col' }, h));
        t.appendChild(head);
        for (const [item, status, detail, ref] of g) {
            const tr = el('tr');
            tr.appendChild(el('td', {}, item == null ? '' : item));
            const outcome = el('td', { 'data-status': status || '' }, outcomeWords(status, status === 'failed' ? detail : ''));
            tr.appendChild(outcome);
            const cell = el('td', {}, status === 'failed' ? '' : (detail == null ? '' : detail));
            if (status === 'uncertain' && ref && ref.unit && res.session) {
                cell.appendChild(resolveControl(res.session, ref, outcome, say));
            }
            tr.appendChild(cell);
            t.appendChild(tr);
        }
        host.appendChild(t);
    }
    const unwritten = Object.entries((res.mirror && res.mirror.note_tags_unwritten) || {});
    if (unwritten.length) {
        // Ticked tags that did not land in a newly created note (review
        // 2026-09-22): the tag step reads those notes as "not needed", so
        // without this line the loss would be silent.
        const box = el('div', { class: 'warning-banner' });
        box.appendChild(el('p', {}, `Tags you ticked were not written into ${unwritten.length} new note(s):`));
        const ul = el('ul');
        for (const [key, why] of unwritten) ul.appendChild(el('li', {}, `${key}: ${why}`));
        box.appendChild(ul);
        host.appendChild(box);
    }
    // Note units the mirror left OPEN, each with its reason (audit
    // remediation 2026-09-23, L1): a note is closed only on the mirror's
    // positive per-note evidence, so a record the last scan does not know
    // stays open as "rescan needed" instead of being closed unmirrored.
    const leftOpen = (res.mirror && res.mirror.open) || [];
    if (leftOpen.length) {
        const box = el('div', { class: 'warning-banner' });
        box.appendChild(el('p', {}, `${leftOpen.length} note(s) stay open for the next run:`));
        const ul = el('ul');
        for (const o of leftOpen) ul.appendChild(el('li', {}, `${o.citation_key || name(o.unit)}: ${o.reason}`));
        box.appendChild(ul);
        host.appendChild(box);
    }
    const pdfUnwritten = Object.entries(res.pdf_links_unwritten || (res.mirror && res.mirror.pdf_links_unwritten) || {});
    if (pdfUnwritten.length) {
        // A created note whose pdf: list could not be inserted (N4).
        const box = el('div', { class: 'warning-banner' });
        box.appendChild(el('p', {}, `The pdf: list was not written into ${pdfUnwritten.length} new note(s):`));
        const ul = el('ul');
        for (const [key, why] of pdfUnwritten) ul.appendChild(el('li', {}, `${key}: ${why}`));
        box.appendChild(ul);
        host.appendChild(box);
    }
    if (res.stopped === 'scan_running') host.appendChild(el('p', { class: 'warning-banner' },
        res.blocked || 'An audit scan started; the run stopped before the next step.'));
    if ((res.ignored || []).length) host.appendChild(el('p', { class: 'control-hint' },
        `${res.ignored.length} duplicate row(s) were ignored.`));
    if (Number(res.notes_written || 0) > 0) host.appendChild(el('p', { class: 'control-hint' },
        `${res.notes_written} note(s) were written: they are uncommitted in the vault’s git, and the next `
        + 'mirror will not refresh them until the notes folder is committed.'));
    const stopped = res.cancelled || res.stopped || (res.not_reached || []).length;
    if (stopped) host.appendChild(el('p', { class: 'warning-banner' },
        `Stopped before ${(res.not_reached || []).length} item(s); run the analysis again to see what is still open.`));
    if (failed.some(r => r[1] === 'uncertain')) host.appendChild(el('p', { class: 'warning-banner' },
        'An outcome is uncertain (the request may have been applied). It will not be retried automatically: '
        + 'inspect it, then use “Mark resolved…” on its row. A rename is also settled from its journal '
        + 'by the next analysis when the journal can tell.'));
    if (!quiet) heading.focus();
    say(stopped ? 'Stopped before all selected items were reached — see the result table.'
        : failed.length ? `${failed.length} item(s) not done — see the result table.`
        : unwritten.length ? `Applied, but tags were not written into ${unwritten.length} new note(s) — see the result.`
        : leftOpen.length || pdfUnwritten.length ? 'Applied, but some notes stay open — see the result.'
        : 'All selected items were applied.',
        stopped || failed.length || unwritten.length || leftOpen.length || pdfUnwritten.length ? 'warn' : 'ok');
}
