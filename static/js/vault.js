/**
 * Obsidian Vault tab: indexing lifecycle, the live retrieval/generation knobs,
 * vault RAG chat (with optional agent-mode trace rendering), exclusions, and the
 * indexed-materials manifest. Imports only ui.js + api.js + config.js (per the JS
 * module hierarchy).
 *
 * Key non-obvious points (see inline comments):
 *  - the live query-knob values are sent in EVERY /api/obsidian/chat body, so a
 *    save failure never changes what the next Send uses;
 *  - the silence budget is supplied by the SSE response; provider policy stays server-side.
 *  - _renderAnswer falls back to plain text when the vendored `marked` is absent;
 *  - all user-controlled strings (paths, folder names, answers) go through
 *    createElement/textContent or sanitiseHtml — never raw innerHTML.
 */
import { secureFetch, consumeSSE, safeJson, logError } from './api.js';
import { taskBegin, taskEnd, taskUpdate, setStatusA11y, announceStatus, openModal, closeModal, sanitiseHtml, copyToClipboard, enhanceCodeBlocks, showTaskError, clearTaskError, makeAnswerRenderer, buildConversationExport, confirmInline } from './ui.js';
import { getActiveProvider, getSelectedEmbed, getSelectedModel, saveSelectedModels } from './config.js';

let _onlineOcrPreview = null;
let _onlineOcrPoll = null;
let _onlinePollDelay = null;
let _onlineSubmitting = false;
let _onlineBusy = false;
let _onlineAnnouncedPhase = null;
let _onlineMounted = false;
const _onlineSelection = new Set();
const _onlineOcrModels = {
    deepseek: ['deepseek-flash'],
    openai: ['gpt-5-mini'],
    google: ['gemini-2.5-flash-lite', 'gemini-3.8-flash'],
    openrouter: ['google/gemini-3.8-flash']
};
let _onlineSettingsSave = Promise.resolve();

function publishExtractionBusy() {
    document.dispatchEvent(new CustomEvent('library-extraction-busy', {
        detail: {busy: _onlineSubmitting || _onlineBusy}
    }));
}

function busyRejection(response, body) {
    return response.status === 409 && /active|running|owns/i.test(body?.error || '');
}

// Bound the whole JSON read, including the response body. No automatic replay.
async function onlineJson(url, options = {}, timeoutMs = 30000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const response = await secureFetch(url, {...options, signal: controller.signal});
        const body = await safeJson(response);
        if (controller.signal.aborted) throw new DOMException('Request timed out', 'AbortError');
        return {response, body};
    } finally { clearTimeout(timer); }
}

export function applyOnlineVisualSettings(config) {
    if (!_onlineMounted) {
        _onlineMounted = true;
        refreshOnlineHistory();
        refreshIndexLibraryFreshness();
        document.addEventListener('visibilitychange', () => { if (!document.hidden) void pollOnlineOcr(); });
        window.addEventListener('online', () => void pollOnlineOcr());
        window.addEventListener('focus', () => void pollOnlineOcr());
        pollOnlineOcr();
    }
    const showMode = () => {
        const mode = document.getElementById('daily-index-mode');
        if (mode) mode.textContent = document.getElementById('vault-online-visual')?.checked
            ? 'Online OCR enabled — scanned or complex pages will be sent to the configured cloud provider.'
            : 'Local extraction; online OCR disabled.';
    };
    const enabled = document.getElementById('vault-online-visual');
    if (enabled) {
        enabled.checked = config.vault_online_visual_enabled === true;
        showMode();
        enabled.onchange = async () => {
            try {
                const response = await secureFetch('/api/config', {
                    method: 'POST', body: JSON.stringify({
                        vault_online_visual_enabled: enabled.checked
                    })
                });
                if (!response.ok) throw new Error('Could not save online index mode');
            } catch (error) {
                enabled.checked = !enabled.checked;
                _onlineOcrMessage(error.message);
            }
            showMode();
        };
    }
    for (const [prefix, providerKey, modelKey] of [
        ['online-ocr', 'online_ocr_provider', 'online_ocr_model'],
        ['online-vision', 'online_vision_provider', 'online_vision_model']
    ]) {
        const provider = document.getElementById(`${prefix}-provider`);
        const model = document.getElementById(`${prefix}-model`);
        if (!provider || !model) continue;
        provider.value = config[providerKey] || 'deepseek';
        model.replaceChildren();
        for (const name of _onlineOcrModels[provider.value] || []) {
            const option = document.createElement('option');
            option.value = name;
            option.textContent = name;
            model.appendChild(option);
        }
        model.value = config[modelKey] || model.options[0]?.value || '';
        model.onchange = () => {
            saveOnlineVisualSettings().catch(error => _onlineOcrMessage(error.message));
        };
    }
}

export function saveOnlineVisualSettings() {
    const payload = {
        online_ocr_provider: document.getElementById('online-ocr-provider')?.value,
        online_ocr_model: document.getElementById('online-ocr-model')?.value,
        online_vision_provider: document.getElementById('online-vision-provider')?.value,
        online_vision_model: document.getElementById('online-vision-model')?.value
    };
    _onlineSettingsSave = _onlineSettingsSave.catch(() => {}).then(async () => {
        const {response} = await onlineJson('/api/config', {
            method: 'POST', body: JSON.stringify(payload)
        });
        if (!response.ok) throw new Error('Could not save online OCR/vision models');
    });
    return _onlineSettingsSave;
}

export function onlineOcrProviderChanged() {
    const provider = document.getElementById('online-ocr-provider')?.value;
    const select = document.getElementById('online-ocr-model');
    if (!select) return;
    select.replaceChildren();
    for (const model of _onlineOcrModels[provider] || []) {
        const option = document.createElement('option');
        option.value = model;
        option.textContent = model;
        select.appendChild(option);
    }
    saveOnlineVisualSettings().catch(error => _onlineOcrMessage(error.message));
}

export function onlineVisionProviderChanged() {
    const provider = document.getElementById('online-vision-provider')?.value;
    const select = document.getElementById('online-vision-model');
    if (!select) return;
    select.replaceChildren();
    for (const model of _onlineOcrModels[provider] || []) {
        const option = document.createElement('option');
        option.value = model;
        option.textContent = model;
        select.appendChild(option);
    }
    saveOnlineVisualSettings().catch(error => _onlineOcrMessage(error.message));
}

export async function advancedOnlineIndex() {
    return startOnlineIndex(false);
}

async function startOnlineIndex(daily = true, noExternal = false, entryAnchor = null, prepareOnly = false, onPreview = null, onlineChoice = null, retryPaths = null, action = undefined) {
    const anchor = entryAnchor || document.getElementById(daily ? 'vault-index-btn' : 'advanced-index-btn');
    if (!anchor || _onlineSubmitting) return;
    if (_onlineBusy) { _onlineOcrMessage("An extraction or index update is already active."); return; }
    _onlineSubmitting = true;
    publishExtractionBusy();
    _onlineOcrMessage('Preparing extraction preview… Please wait.');
    if (onlineChoice !== null) noExternal = !onlineChoice;
    const models = {
        provider: document.getElementById('online-ocr-provider')?.value,
        model: document.getElementById('online-ocr-model')?.value,
        vision_provider: document.getElementById('online-vision-provider')?.value,
        vision_model: document.getElementById('online-vision-model')?.value
    };
    const panel = document.getElementById('online-ocr-panel');
    const fullRebuild = !daily && document.getElementById('vault-online-full-rebuild')?.checked === true;
    const reextract = !!retryPaths || (!daily && document.getElementById('online-ocr-reextract')?.checked === true);
    const operationMode = daily ? (prepareOnly ? 'prepare_text' : 'update_search') : undefined;
    const initiatingAction = retryPaths ? 'targeted_retry' : action;
    const paths = retryPaths || (!daily && _onlineSelection.size ? [..._onlineSelection].sort() : undefined);
    if (panel && !daily) panel.open = true;
    try {
        await saveOnlineVisualSettings();
        const {response, body: preview} = await onlineJson('/api/online-ocr/session/preview', {
            method: 'POST', body: JSON.stringify({ run_index: true, reextract, paths, operation_mode: operationMode, initiating_action: initiatingAction })
        }, 130000);
        if (!response.ok) throw new Error(preview?.error || 'Online index preview failed');
        if (!anchor.isConnected) return;
        if (onPreview) onPreview(preview);
        const externalEligible = daily && !retryPaths ? (preview.external_eligible ?? preview.eligible) : preview.eligible;
        _onlineOcrPreview = { ...preview, scope: 'session', run_index: true };
        _onlineOcrMessage(`${preview.source_count} documents to check; ${preview.reused_count || 0} caches reused; ` +
            `${preview.pending_count || 0} previously deferred, not retried. ` +
            (noExternal || !externalEligible ? 'No external calls. Documents needing online processing remain pending.' :
                `${externalEligible} eligible images/pages. At most ${preview.maximum_attempts ?? preview.eligible * 2} attempts; cost unknown. Review before authorizing.`));
        const start = async (withoutExternal) => {
                if (_onlineSubmitting || _onlineBusy) return;
                _onlineSubmitting = true;
                publishExtractionBusy();
                _onlineOcrMessage(prepareOnly
                    ? 'Starting the confirmed preparation…'
                    : 'Starting the confirmed search update…');
                let startResponseKnown = false;
                let busyRejected = false;
                try {
                    const {response: result, body} = await onlineJson('/api/online-ocr/session/start', {
                        method: 'POST', body: JSON.stringify({
                            run_index: true, reextract, paths, operation_mode: operationMode, initiating_action: initiatingAction, prepare_only: prepareOnly, confirm_external: !withoutExternal,
                            no_external: withoutExternal,
                            confirm_index: !prepareOnly, full_rebuild: fullRebuild,
                            confirm_full_rebuild: fullRebuild, retry_failed: !!retryPaths || !daily,
                            session_token: preview.session_token,
                            provider: models.provider,
                            model: models.model,
                            vision_provider: models.vision_provider,
                            vision_model: models.vision_model
                        })
                    }, 130000);
                    startResponseKnown = true;
                    if (!result.ok) {
                        busyRejected = busyRejection(result, body);
                        throw new Error(body?.error || 'Index could not start');
                    }
                    rememberOnlineRun(body.run_id);
                    if (_onlineOcrPoll) clearInterval(_onlineOcrPoll);
                    _onlineOcrPoll = setInterval(pollOnlineOcr, 1000);
                    await pollOnlineOcr();
                } catch (error) {
                    if (!startResponseKnown) {
                        _onlineBusy = true;
                        _onlineOcrNotice('Start response unavailable; the operation may have started. Status is checked without repeating the launch.');
                        await pollOnlineOcr();
                    } else if (busyRejected) {
                        announceStatus(error.message + ' Checking the active operation; launch was not retried.');
                        await pollOnlineOcr();
                    } else _onlineOcrMessage(error.message);
                } finally { _onlineSubmitting = false; publishExtractionBusy(); }
        };
        const strip = confirmInline(anchor, {
            message: `${reextract ? "Re-extract selected text" : "Extract new/changed sources and reuse successful caches"}. ` +
                (prepareOnly ? 'Save complete extraction caches for review; the search index stays unchanged. ' :
                    `${fullRebuild ? 'Archive and rebuild' : 'Update'} the index using local ${getSelectedEmbed()} embeddings. `) +
                `${preview.reused_count || 0} caches reused; ${preview.pending_count || 0} unchanged failures deferred. ` +
                (noExternal || !externalEligible ? (retryPaths && externalEligible ? `Optional online retry uses ${models.provider} / ${models.model} for OCR and ${models.vision_provider} / ${models.vision_model} for figures, up to ${preview.maximum_attempts ?? preview.eligible * 2} attempts; cost unknown. Choose explicitly below. ` : '') + 'No external calls. Sources requiring OCR remain pending; existing indexed passages are retained when compatible.' :
                    `OCR: ${models.provider} / ${models.model}. Vision: ${models.vision_provider} / ${models.vision_model}. Up to ${preview.maximum_attempts ?? preview.eligible * 2} attempts; cost unknown.`),
            confirmLabel: fullRebuild ? 'Start full online rebuild' : prepareOnly ? (noExternal || !externalEligible ? 'Prepare available text' : 'Authorize and prepare text') : noExternal || !externalEligible ? 'Update using available text' : 'Authorize and update', tone: 'confirm',
            onConfirm: () => start(noExternal || !externalEligible)

        });
        if (daily && externalEligible && (!noExternal || retryPaths) && strip) {
            const offline = document.createElement('button');
            offline.type = 'button';
            offline.className = 'btn btn-outline btn-sm';
            if (anchor.closest('.audit-daily-active')) offline.tabIndex = 0;
            offline.textContent = noExternal ? 'Authorize OCR and prepare text' : 'Continue without online calls';
            offline.addEventListener('click', () => { strip.remove(); anchor.focus(); start(!noExternal); });
            strip.querySelector('.inline-confirm-actions').appendChild(offline);
        }

    } catch (error) {
        _onlineOcrNotice(error.name === 'AbortError' ? 'Preview timed out. No extraction was started; review status before trying again.' : error.message);
    } finally {
        _onlineSubmitting = false;
        publishExtractionBusy();
    }
}

// Kept separately from live status: reconnecting must not erase launch evidence.
function _onlineOcrNotice(message) {
    const anchor = document.getElementById('library-preparation-status') || document.getElementById('online-ocr-status');
    if (anchor) {
        let notice = document.getElementById('library-operation-notice');
        if (!notice) {
            notice = document.createElement('p');
            notice.id = 'library-operation-notice';
            notice.className = 'control-hint';
            notice.setAttribute('role', 'status');
            anchor.after(notice);
        }
        notice.textContent = message;
    }
    _onlineOcrMessage(message);
}

function _onlineOcrMessage(message, announce = true) {
    const libraryStatus = document.getElementById('library-index-status') || document.getElementById('library-preparation-status');
    if (libraryStatus) libraryStatus.textContent = message;
    const dailyStatus = document.getElementById('daily-index-status');
    if (dailyStatus) dailyStatus.textContent = message;
    const target = document.getElementById('online-ocr-status');
    if (target && target.textContent !== message) target.textContent = message;
    const announcer = document.getElementById('online-ocr-announcement');
    if (announce && announcer && announcer.textContent !== message) announcer.textContent = message;
}

export async function checkExtractionMemory(button) {
    const target = document.getElementById('daily-memory-status');
    if (!target || !button) return;
    button.disabled = true;
    target.textContent = 'Checking memory…';
    try {
        const {response, body} = await onlineJson('/api/online-ocr/resources', {}, 5000);
        if (!response.ok) throw new Error('Memory check unavailable');
        target.textContent = (body.message || 'Memory status unavailable.') + ' No operation was started.';
    } catch (_) { target.textContent = 'Memory check unavailable. No operation was started.'; }
    finally { button.disabled = false; }
}

export async function previewOnlineOcr() {
    _onlineOcrPreview = null;
    const scope = document.getElementById('online-ocr-scope')?.value || 'file';
    const path = document.getElementById('online-ocr-path')?.value.trim();
    const list = document.getElementById('online-ocr-preview-list');
    if (list) list.replaceChildren();
    _onlineOcrMessage('Checking native text and existing extraction…');
    try {
        const response = await secureFetch(scope === 'session' ?
            '/api/online-ocr/session/preview' : '/api/online-ocr/preview', {
            method: 'POST', body: JSON.stringify({ path,
                reextract: document.getElementById('online-ocr-reextract')?.checked === true,
                provider: document.getElementById('online-ocr-provider')?.value,
                model: document.getElementById('online-ocr-model')?.value,
                page_start: Number(document.getElementById('online-page-start')?.value || 1),
                page_end: Number(document.getElementById('online-page-end')?.value) || undefined })
        });
        const result = await safeJson(response);
        if (!response.ok) throw new Error(result?.error || 'Preview failed');
        _onlineOcrPreview = { ...result, scope };
        if (list) for (const row of result.sources || [result]) {
            const item = document.createElement('li');
            item.textContent = `${row.source}: ${row.eligible} eligible` +
                (row.eligible_pages?.length ? ` (pages ${row.eligible_pages.join(', ')})` : '') +
                (row.has_cached_text ? ' (cached text retained)' : '');
            list.appendChild(item);
        }
        _onlineOcrMessage(`${result.eligible} eligible of ${result.pages} page(s)/image(s)` +
            (scope === 'session' ? ` across ${result.source_count} sources. ` : '. ') +
            (result.has_cached_text ? 'Existing cached text found; select re-extraction to send.' :
                'Only eligible image content will be sent.'));
    } catch (error) {
        _onlineOcrMessage(error.message);
    }
}

export async function startOnlineOcr() {
    const scope = document.getElementById('online-ocr-scope')?.value || 'file';
    const path = document.getElementById('online-ocr-path')?.value.trim();
    if (!_onlineOcrPreview || _onlineOcrPreview.scope !== scope ||
        (scope === 'file' && _onlineOcrPreview.source !== path)) {
        _onlineOcrMessage('Preview this source before sending it.');
        return;
    }
    const provider = document.getElementById('online-ocr-provider')?.value;
    const model = document.getElementById('online-ocr-model')?.value.trim();
    const reextract = !!document.getElementById('online-ocr-reextract')?.checked;
    if (scope === 'file' && _onlineOcrPreview.has_cached_text && !reextract) {
        _onlineOcrMessage('Existing text is retained. Select re-extraction to send this source.');
        return;
    }
    const retryValue = document.getElementById('online-ocr-retry-page')?.value;
    const retryPage = retryValue ? Number(retryValue) : null;
    const retry = !!document.getElementById('online-ocr-retry-image')?.checked;
    try {
        const response = await secureFetch(scope === 'session' ?
            '/api/online-ocr/session/start' : '/api/online-ocr/start', {
            method: 'POST', body: JSON.stringify({ path, provider, model, reextract,
                retry_page: retryPage, retry, confirm_external: true,
                preview_token: _onlineOcrPreview.preview_token,
                page_start: _onlineOcrPreview.page_range?.[0], page_end: _onlineOcrPreview.page_range?.[1],
                source_sha256: _onlineOcrPreview.source_sha256,
                session_token: _onlineOcrPreview.session_token })
        });
        const result = await safeJson(response);
        if (!response.ok) {
            if (busyRejection(response, result)) {
                announceStatus(result.error + ' Following the active operation; launch was not retried.');
                await pollOnlineOcr();
                return;
            }
            throw new Error(result?.error || 'Could not start extraction');
        }
        rememberOnlineRun(result.run_id);
        _onlineOcrMessage('Sending eligible image content externally. Cancel stops further calls.');
        if (_onlineOcrPoll) clearInterval(_onlineOcrPoll);
        _onlineOcrPoll = setInterval(pollOnlineOcr, 1000);
        await pollOnlineOcr();
    } catch (error) {
        _onlineOcrMessage(error.message);
    }
}

let _onlineExpectedRun = null;
try { _onlineExpectedRun = localStorage.getItem('papermind-extraction-run'); } catch (_) { /* optional */ }
let _onlineTask = null;
let _onlinePollInFlight = false;

function rememberOnlineRun(runId) {
    _onlineExpectedRun = runId || null;
    try {
        if (_onlineExpectedRun) localStorage.setItem('papermind-extraction-run', _onlineExpectedRun);
        else localStorage.removeItem('papermind-extraction-run');
    } catch (_) { /* optional recovery preference */ }
}

export async function pollOnlineOcr() {
    if (_onlinePollInFlight) return;
    _onlinePollInFlight = true;
    try {
        const {response, body: result} = await onlineJson('/api/online-ocr/status');
        const states = new Set(['idle', 'evidence_unavailable', 'interrupted', 'status_unrecorded',
            'running', 'promoting', 'indexing', 'text_prepared', 'indexed', 'completed_with_omissions',
            'cancelled', 'promotion_or_index_incomplete', 'index_incomplete', 'time_limit',
            'extraction_incomplete', 'failed', 'done']);
        if (!response.ok || !result || !states.has(result.state)) throw new Error('Status unavailable');
        _onlineBusy = ['running', 'promoting', 'indexing'].includes(result.state)
            || (['indexed', 'completed_with_omissions'].includes(result.state) && result.bm25_status === 'preparing');
        const delay = _onlineBusy ? 1500 : 5000;
        if (!_onlineOcrPoll || _onlinePollDelay !== delay) {
            clearInterval(_onlineOcrPoll);
            _onlinePollDelay = delay;
            _onlineOcrPoll = setInterval(pollOnlineOcr, delay);
        }
        if (result.source && !_onlineOcrPreview) _onlineOcrPreview = { scope: 'file', source: result.source };
        if (result.scope === 'vault_session' && !_onlineOcrPreview) _onlineOcrPreview = { scope: 'session' };
        publishExtractionBusy();
        const labels = {idle: 'No extraction active', evidence_unavailable: 'Saved run evidence unavailable — inspect history',
            interrupted: 'Interrupted — inspect before retrying', status_unrecorded: 'Final status not recorded', running: 'Preparing text and figures',
            promoting: 'Saving complete extraction', indexing: 'Updating search',
            text_prepared: 'Preparation complete — review proposals', indexed: 'Vector index updated',
            completed_with_omissions: 'Vector index updated with exceptions', cancelled: 'Interrupted',
            promotion_or_index_incomplete: 'Cache publication interrupted — inspect recovery evidence',
            index_incomplete: 'Search update incomplete — inspect recovery evidence',
            time_limit: 'Time limit reached', extraction_incomplete: 'Preparation stopped — review failed documents',
            failed: 'Operation failed', done: 'Extraction complete'};
        const elapsed = result.started_at ? Math.max(0, Math.floor((result.finished_at || Date.now() / 1000) - result.started_at)) : null;
        const age = result.last_activity_at ? Math.max(0, Math.floor(Date.now() / 1000 - result.last_activity_at)) : null;
        const failureMessage = result.failure?.error_detail || result.error_detail || result.error || '';
        const resourceStop = ['initial_memory_pressure', 'critical_memory_pressure',
            'swap_growth_over_512_mib', 'memory_monitor_unavailable', 'text_memory_budget']
            .includes(result.failure?.stop_reason || result.stop_reason);
        const recovery = document.getElementById('daily-resource-actions');
        if (recovery) recovery.hidden = !resourceStop || _onlineBusy;
        if (result.state === 'extraction_incomplete' && resourceStop) {
            labels.extraction_incomplete = 'Preparation stopped — resource limit';
        } else if (result.state === 'extraction_incomplete' && (result.failure?.failure_scope || result.failure_scope) === 'global') {
            labels.extraction_incomplete = 'Preparation stopped — operation error';
        }
        const errorPreview = failureMessage.length > 160 ? failureMessage.slice(0, 160) + '… See error details.' : failureMessage;
        const allDeferred = result.source_count > 0 && result.deferred_count >= result.source_count;
        if (result.state === 'text_prepared' && allDeferred) labels.text_prepared = 'No new source text prepared — review exceptions';
        const progress = [labels[result.state] || result.state,
            ['indexed', 'completed_with_omissions'].includes(result.state) ? ({preparing: 'Preparing and saving lexical search…', ready: 'Lexical search ready and saved', disabled: 'Lexical search disabled', unknown: 'Lexical search readiness unknown'}[result.bm25_status] || 'Lexical search readiness unknown') : '',
            result.source_count > 0 && result.result_count != null ? `${result.result_count}/${result.source_count} documents examined for extraction` : '',
            result.state === 'running' ? result.current_source : '',
            result.state === 'indexing' ? result.index_progress?.message || result.index_progress?.phase || 'Indexing available text…' : '',
            _onlineBusy && elapsed !== null ? `${elapsed}s elapsed` : '',
            result.state === 'running' && age !== null ? `last activity ${age}s ago` : '',
            result.deferred_count ? `${result.deferred_count} deferred — existing passages retained when compatible` : '',
            result.promoted_count ? `${result.promoted_count} cache(s) replaced` : '',
            result.not_attempted_count ? `${result.not_attempted_count} not attempted` : '',
            result.global_failure || (result.failure && result.state === 'extraction_incomplete') ? 'Search update not started. Review the error below before retrying.' : '',
            errorPreview,
            resourceStop ? result.failure?.next_step || result.next_step || 'Review resource details before retrying. Unattempted documents are not defective.' : ''].filter(Boolean).join(' · ');
        const task = 'online-' + (result.run_id || 'current');
        if (_onlineTask && _onlineTask !== task) taskEnd(_onlineTask);
        if (_onlineBusy) {
            _onlineTask = task; taskBegin(task, [labels[result.state] || 'Processing documents', result.state === 'running' ? result.current_source?.split('/').pop() : null].filter(Boolean).join(' · '));
            taskUpdate(task, {message: progress, completed: result.state === 'running' ? result.result_count : null,
                total: result.state === 'running' ? result.source_count : null, indeterminate: result.state !== 'running'});
        } else if (_onlineTask) {
            taskUpdate(_onlineTask, {label: labels[result.state] || result.state, message: progress,
                completed: null, total: null, indeterminate: false});
            const outcome = ['indexed', 'completed_with_omissions'].includes(result.state) && !['ready', 'disabled'].includes(result.bm25_status) ? 'unknown'
                : ['indexed', 'text_prepared', 'done'].includes(result.state) && !result.deferred_count ? 'completed'
                : ['text_prepared', 'done'].includes(result.state) && result.deferred_count ? 'partial'
                : ['completed_with_omissions', 'promotion_or_index_incomplete', 'index_incomplete'].includes(result.state) ? 'partial'
                : result.state === 'cancelled' ? 'cancelled'
                : ['failed', 'extraction_incomplete', 'time_limit'].includes(result.state) ? 'failed' : 'unknown';
            taskEnd(_onlineTask, outcome); _onlineTask = null;
        }
        _onlineOcrMessage(progress, result.state !== _onlineAnnouncedPhase);
        document.dispatchEvent(new CustomEvent('library-extraction-status', {detail: {...result, busy: _onlineSubmitting || _onlineBusy, summary: progress}}));
        _onlineAnnouncedPhase = result.state;
        const exportButton = document.getElementById('online-export-current');
        if (exportButton) {
            exportButton.hidden = !result.export;
            exportButton.onclick = () => downloadOnlineExport('/api/online-ocr/export/current');
        }
        const promote = document.getElementById('online-ocr-promote');
        if (promote) promote.hidden = !!result.run_index || !(result.state === 'done' && (
            (result.coverage === 'processed_unverified' && result.export) ||
            (result.scope === 'vault_session' && result.completed_count > 0)));

    } catch (error) {
        const summary = 'Status unknown — connection lost. The operation may still be running. Reconnecting…';
        _onlineBusy = true;
        _onlineTask ||= 'online-status';
        taskBegin(_onlineTask, 'Status unknown');
        taskUpdate(_onlineTask, {message: summary, indeterminate: true});
        _onlineOcrMessage(summary);
        publishExtractionBusy();
        document.dispatchEvent(new CustomEvent('library-extraction-status', {detail: {state: 'unknown', busy: true, summary}}));
        if (!_onlineOcrPoll) _onlineOcrPoll = setInterval(pollOnlineOcr, 1500);
    } finally { _onlinePollInFlight = false; }
}

export function promoteOnlineOcr() {
    const anchor = document.getElementById('online-ocr-promote');
    if (!anchor || anchor.hidden || !_onlineOcrPreview) return;
    const session = _onlineOcrPreview.scope === 'session';
    confirmInline(anchor, {
        message: 'Use this completed transcription for the source and run an incremental vault index update? The previous cache is saved in a promotion receipt.',
        confirmLabel: 'Use text and index', tone: 'confirm',
        onConfirm: async () => {
            try {
                const response = await secureFetch(session ?
                    '/api/online-ocr/session/promote' : '/api/online-ocr/promote', {
                    method: 'POST', body: JSON.stringify({
                        path: _onlineOcrPreview.source, confirm_index: true })
                });
                const result = await safeJson(response);
                if (!response.ok) throw new Error(result?.error || 'Promotion refused');
                anchor.hidden = true;
                _onlineOcrMessage('Updating the text cache and incremental index…');
                if (_onlineOcrPoll) clearInterval(_onlineOcrPoll);
                _onlineOcrPoll = setInterval(pollOnlineOcr, 1000);
            } catch (error) {
                _onlineOcrMessage(error.message);
            }
        }
    });
}

export async function cancelOnlineOcr() {
    const response = await secureFetch('/api/online-ocr/cancel', { method: 'POST' });
    const result = await safeJson(response);
    _onlineOcrMessage(result?.phase === 'indexing'
        ? 'Index cancellation requested.'
        : result?.phase === 'promotion'
        ? 'Cache publication cancellation requested. Completed replacements remain recorded; inspect History before continuing.'
        : 'Cancellation requested. The current provider call may finish or time out.');
}

// Item 3.1 (improvement plan 2026-07-04): TWO flags, one per state machine.
// A single shared in-flight flag used to serve both chat-in-flight AND the
// indexing lifecycle — so a multi-hour index run blocked vault chat entirely
// (the server explicitly supports chat during indexing), and cancelVaultIndex
// force-cleared whichever guard happened to be held, including a live chat's.
// Safe: every site belongs unambiguously to exactly one machine (chat:
// chatWithVault/clearVaultChat; indexing: indexVault/pollVaultStatus/
// cancelVaultIndex/refreshIndexState re-arm); the transitions within each
// machine are unchanged. Invariant (pinned by tests/js/vaultFlags.test.js):
// an in-flight index run never refuses a chat, and cancelling an index run
// never clears the chat guard.
let _chatController = null;
let _stopRequested = false;

export function stopChat() {
    if (!_chatController) return;
    _stopRequested = true;
    const button = document.getElementById('vault-stop-btn');
    if (button) {
        if (document.activeElement === button) document.getElementById('obsidian-input')?.focus();
        button.disabled = true; button.textContent = 'Stop requested…';
    }
    announceStatus('Stop requested. The provider may still be finishing its current operation.');
    _chatController.abort();
}

let _isChatting = false;
let _isIndexing = false;
let _pollFailures = 0;
// Run-started latch (2026-08-12). The server now publishes a non-terminal state
// before /api/obsidian/index returns, so a terminal state on the first poll is
// no longer expected — but this poll loop is also re-armed against servers we
// don't control the timing of (a slow thread hand-off, an older build), and a
// terminal state read BEFORE the run is observed running is indistinguishable
// from the *previous* run's leftovers. Until a non-terminal state is seen, a
// terminal one is treated as "not started yet" and the poll is rescheduled,
// bounded by _MAX_PRESTART_POLLS so a genuinely instant run still terminates.
// The pre-start repoll is faster than the steady-state 2 s one: the whole
// allowance is worst-case latency for a run that really did fail before the
// first poll (a vault-path gate, say), so 5 × 400 ms bounds that at ~2 s.
let _indexRunObserved = false;
let _prestartPolls = 0;
const _MAX_PRESTART_POLLS = 5;
const _PRESTART_POLL_MS = 400;
// Client-side transcript for the "export conversation" action (axis iv). The
// server holds no vault-chat history (each POST is independent), so this array
// is the only place a full conversation exists — recorded here, exported by
// exportVaultConversation(), cleared by clearVaultChat(). Capped (oldest turns
// dropped) because a session can live for days: the export is a convenience,
// not an archival log, and unbounded growth would pin every answer's full
// text in memory forever.
const _vaultHistory = [];
const _VAULT_HISTORY_MAX_TURNS = 200;

function _pushVaultTurn(turn) {
    _vaultHistory.push(turn);
    if (_vaultHistory.length > _VAULT_HISTORY_MAX_TURNS) {
        _vaultHistory.splice(0, _vaultHistory.length - _VAULT_HISTORY_MAX_TURNS);
    }
}

// A partial failure records {user, assistant+"[interrupted]"} so the export
// keeps the text the user watched arrive. Retry then re-asks the SAME
// question, and its success path pushed the user turn a SECOND time — the
// export showed the question twice (2026-07-20 audit; fixed 2026-07-30).
// Remember which question already has a user turn on record; a retry of that
// exact question appends only the assistant side.
let _recordedRetryQuestion = null;

function _pushUserTurnOnce(question, fromRetry) {
    if (fromRetry && _recordedRetryQuestion === question) return;
    _pushVaultTurn({ role: 'user', content: question });
    _recordedRetryQuestion = null;
}
let _vaultPollDeadline = 0;
let _chatParamsSaveTimer = null;
let _prewarmPolling = false;
let _prewarmInFlight = false;

const _PREWARM_STAGE_LABELS = {
    idle: 'Starting up…',
    loading_index: 'Loading vault index…',
    building_bm25: 'Building lexical (BM25) retriever…',
    loading_reranker: 'Loading cross-encoder reranker…',
    building_wikilinks: 'Preparing note-link graph…',
};

const _CHAT_PARAM_DEFAULTS = {
    // Matches the server default (CLAUDE.md Vault Rules: top_k default 8). Only
    // used when the #vault-top-k control is missing; the two must not drift.
    top_k: 8,
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
    thesaurus_expansion: false,
    primer_enabled: false,
    web_search_enabled: false,
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
// tool outputs (vault_search snippets at 800 chars, registry-level
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

// Card Catalog (2026-07-20): the retrieval panel is a numbered REFERENCE
// LIST — footnote number, source link + kind label, relevance bar + score —
// instead of the old inline-styled bullet dump with a raw float and an emoji
// image marker. Class-driven (styles.css .retrieval-*), createElement-only.
//
// Paper labels (2026-09-23): a chunk the audit's join knows carries an additive
// ``paper: {key, label}``; the row then shows "Smith et al., 2020, Title" with
// the vault path in the tooltip. textContent only — the label is bibliography
// data, never markup. Exported for tests/js/retrievalTrace.test.js.
export function _renderRetrievalTrace(retrieval, botMsg, chat) {
    if (!Array.isArray(retrieval) || retrieval.length === 0) return;
    const details = document.createElement('details');
    details.className = 'retrieval-trace';
    details.open = false;

    const summary = document.createElement('summary');
    summary.textContent = `Sources · ${retrieval.length} chunk${retrieval.length === 1 ? '' : 's'}`;
    details.appendChild(summary);

    const list = document.createElement('div');
    list.className = 'retrieval-list';

    retrieval.forEach((item, i) => {
        const row = document.createElement('div');
        row.className = 'retrieval-row';

        const num = document.createElement('span');
        num.className = 'retrieval-num';
        num.textContent = String(i + 1);
        row.appendChild(num);

        const src = document.createElement('div');
        src.className = 'retrieval-src';
        if (item.source) {
            // A real anchor whose href is the vault-relative source path: the
            // global link interceptor (ui.js) routes the click to
            // POST /api/open, which reveals the file in Finder — the anchor
            // itself never navigates.
            const link = document.createElement('a');
            link.href = item.source;
            link.title = 'Reveal in Finder';
            const label = (item.paper && typeof item.paper.label === 'string')
                ? item.paper.label.trim() : '';
            link.textContent = label || item.source;
            src.appendChild(link);
            if (label) {
                // The path is VISIBLE text under the label, never only a
                // tooltip: VoiceOver does not reliably read `title` and a
                // tooltip never reaches touch (review 2026-09-23).
                const path = document.createElement('span');
                path.className = 'retrieval-path';
                path.textContent = item.source;
                src.appendChild(path);
            }
        } else {
            const unk = document.createElement('span');
            unk.textContent = 'unknown source';
            src.appendChild(unk);
        }
        const kind = document.createElement('span');
        kind.className = 'retrieval-kind';
        kind.textContent = item.is_image
            ? 'image'
            : (String(item.source || '').toLowerCase().endsWith('.pdf') ? 'pdf' : 'note');
        src.appendChild(kind);
        row.appendChild(src);

        if (typeof item.score === 'number') {
            const rel = document.createElement('div');
            rel.className = 'retrieval-rel';
            const bar = document.createElement('span');
            bar.className = 'retrieval-rel-bar';
            const fill = document.createElement('span');
            fill.style.width = `${Math.round(Math.max(0, Math.min(1, item.score)) * 100)}%`;
            bar.appendChild(fill);
            const val = document.createElement('span');
            val.className = 'retrieval-rel-val';
            val.textContent = item.score.toFixed(2);
            rel.appendChild(bar);
            rel.appendChild(val);
            row.appendChild(rel);
        }
        list.appendChild(row);
    });

    details.appendChild(list);
    chat.insertBefore(details, botMsg);
    chat.scrollTop = chat.scrollHeight;
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
        thesaurus_expansion: _readCheckbox('vault-thesaurus-enabled', _CHAT_PARAM_DEFAULTS.thesaurus_expansion),
        primer_enabled: _readCheckbox('vault-primer-enabled', _CHAT_PARAM_DEFAULTS.primer_enabled),
        web_search_enabled: _readCheckbox('vault-websearch-enabled', _CHAT_PARAM_DEFAULTS.web_search_enabled),
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
        thesaurus_expansion: cfg.vault_thesaurus_expansion ?? _CHAT_PARAM_DEFAULTS.thesaurus_expansion,
        primer_enabled: cfg.vault_primer_enabled ?? _CHAT_PARAM_DEFAULTS.primer_enabled,
        web_search_enabled: cfg.vault_web_search_enabled ?? _CHAT_PARAM_DEFAULTS.web_search_enabled,
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
    const thEl = document.getElementById('vault-thesaurus-enabled');
    if (thEl) thEl.checked = !!params.thesaurus_expansion;
    const prEl = document.getElementById('vault-primer-enabled');
    if (prEl) prEl.checked = !!params.primer_enabled;
    const wsEl = document.getElementById('vault-websearch-enabled');
    if (wsEl) wsEl.checked = !!params.web_search_enabled;
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
            vault_thesaurus_expansion: p.thesaurus_expansion,
            vault_primer_enabled: p.primer_enabled,
            vault_web_search_enabled: p.web_search_enabled,
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
    const thCb = document.getElementById('vault-thesaurus-enabled');
    if (thCb) thCb.addEventListener('change', debounce);
    const prCb = document.getElementById('vault-primer-enabled');
    if (prCb) prCb.addEventListener('change', debounce);
    const wsCb = document.getElementById('vault-websearch-enabled');
    if (wsCb) wsCb.addEventListener('change', debounce);
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
    const statusEl = document.getElementById('obsidian-status-msg');
    try {
        const resp = await secureFetch('/api/native-pick-folder', { method: 'POST' });
        const data = await resp.json();
        if (!data.path) return;   // user cancelled the picker
        const setResp = await secureFetch('/api/config', {
            method: 'POST',
            body: JSON.stringify({ obsidian_vault_path: data.path })
        });
        if (setResp.ok) {
            displayEl.textContent = data.path;
            if (statusEl) setStatusA11y(statusEl, '', false);
        } else {
            // C1/#4 (2026-07-13): the server rejects broad roots / system dirs by
            // design — surface WHY instead of silently closing the picker.
            const err = await safeJson(setResp);
            const msg = (err && err.error) || `Could not set vault (HTTP ${setResp.status}).`;
            if (statusEl) setStatusA11y(statusEl, msg, true);
        }
    } catch (e) {
        logError('Folder pick failed', e);
        if (statusEl) setStatusA11y(statusEl, 'Could not open the folder picker (see console).', true);
    }
}

/**
 * Kick off (or resume) a vault index build and start polling its status. Sets a
 * 24 h poll deadline as a runaway backstop; on a start failure surfaces a
 * retryable error in the vault error boundary and resets the buttons to idle.
 */
// `forceFresh` (2026-07-29 audit 0.1) is sent ONLY by the confirm-gated
// "Rebuild from scratch" action offered on an `embed_backend_mismatch` error
// — it discards the vector index (archived server-side, not deleted) and
// re-embeds the whole vault, so no other caller may pass it.
export async function previewLocalIndex(anchor = null, prepareOnly = false, onPreview = null, onlineChoice = null, initiatingAction = undefined) {
    return startOnlineIndex(true, !document.getElementById('vault-online-visual')?.checked, anchor instanceof HTMLElement ? anchor : null, prepareOnly, onPreview, onlineChoice, null, initiatingAction);

}

export async function advancedLocalIndex() {
    const anchor = document.getElementById('advanced-local-index-btn');
    try {
        await saveSelectedModels();
        const response = await secureFetch('/api/obsidian/index-preview', {method: 'POST',
            body: JSON.stringify({provider: getActiveProvider(), embed: getSelectedEmbed()})});
        const result = await safeJson(response);
        if (!response.ok) throw new Error(result.error || 'Local index preview refused');
        if (!result.ready) {
            _showVaultIndexError({error_code: result.reason, error_detail: `Update refused: ${result.reason}. Existing index is unchanged.`});
            return;
        }
        confirmInline(anchor, {message: 'Explicit local OCR/vision update using the configured local models. ' + result.scope,
            confirmLabel: 'Update with local OCR', tone: 'confirm',
            onConfirm: () => indexVault(false, result.update_token, true)});
    } catch (error) { _onlineOcrMessage(error.message); }
}

export async function indexVault(forceFresh, updateToken, localVisual = false) {
    if (_isIndexing || _onlineBusy) return;
    if (!localVisual && document.getElementById('vault-online-visual')?.checked) {
        await startOnlineIndex();
        return;
    }

    const statusDiv = document.getElementById('obsidian-status-msg');
    _isIndexing = true;
    _indexRunObserved = false;
    _prestartPolls = 0;
    clearTaskError(document.getElementById('vault-error-boundary'));
    taskBegin('vault-index', 'Indexing vault…');
    announceStatus('Vault indexing started.');

    try {
        await saveSelectedModels();
        const resp = await secureFetch('/api/obsidian/index', {
            method: 'POST',
            body: JSON.stringify({
                provider: getActiveProvider(),
                llm: getSelectedModel(),
                embed: getSelectedEmbed(),
                visual_mode: 'offline', update_token: updateToken,
                // Strict-boolean so a stray event object from an onclick
                // handler (`indexVault` used directly as a listener receives
                // the Event as its first arg) can never read as "rebuild".
                force_fresh: forceFresh === true,
            })
        });
        if (resp.ok) {
            _updateIndexButtons('running');
            _setIndexProgress(true);
            _vaultPollDeadline = Date.now() + 24 * 60 * 60 * 1000;
            pollVaultStatus();
        } else if (resp.status === 503) {
            // A run is already in flight (the op-lock is held, or a just-cancelled
            // run is still finishing its final persist). Don't surface an error —
            // re-attach to the existing run by re-syncing from the server's real
            // state, which re-arms the activity bar + status polling when it's
            // genuinely running (same recovery path as a page reload mid-index).
            // If it has already settled, refreshIndexState restores idle buttons
            // so the user can simply click Index again.
            _isIndexing = false;
            taskEnd('vault-index');
            refreshIndexState();
        } else {
            const data = await resp.json().catch(() => ({}));
            setStatusA11y(statusDiv, '', false);
            _showVaultError(data.error || 'Indexing could not start.', indexVault);
            _isIndexing = false;
            taskEnd('vault-index'); _setIndexProgress(false);
            _updateIndexButtons('idle');
        }
    } catch (e) {
        setStatusA11y(statusDiv, '', false);
        _showVaultError('Index error: ' + e.message, indexVault);
        _isIndexing = false;
        taskEnd('vault-index'); _setIndexProgress(false);
        _updateIndexButtons('idle');
    }
}

// Toggle the indeterminate indexing activity bar. Driven purely by lifecycle
// (running vs terminal) since the status payload has no processed/total counts.
function _setIndexProgress(active) {
    const el = document.getElementById('vault-index-progress');
    if (el) el.hidden = !active;
}

async function pollVaultStatus() {
    if (!_isIndexing) { _setIndexProgress(false); return; }

    if (Date.now() > _vaultPollDeadline) {
        _isIndexing = false;
        taskEnd('vault-index'); _setIndexProgress(false);
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
        taskUpdate('vault-index', {message: data.error_detail || data.messages?.at(-1) || data.phase || data.state,
            indeterminate: ['running', 'scanning', 'embedding'].includes(data.state)});
        renderVaultWarnings(Array.isArray(data.warnings) ? data.warnings : []);
        // PERF (2026-09-02): one reader for the one-shot status feed.
        //   Before:    pollPrewarmStatus (1.5 s) GET the same DRAINING endpoint
        //              alongside this loop (2 s) and kept only two fields.
        //              Measured in tests/js/vaultStatusDrain.test.js against the
        //              pre-fix code: 9 GETs where 4 suffice, and 4 of 6 progress
        //              messages consumed by the prewarm loop and rendered
        //              nowhere — plus any warning raised in that window.
        //   Change:    while a run is in flight this loop is the single reader
        //              and paints the prewarm banner from its own payload; the
        //              prewarm loop suspends (see pollPrewarmStatus).
        //   Safe because:
        //              the payload already carries prewarm_status/prewarm_message,
        //              and the banner is only painted on behalf of a prewarm loop
        //              that is actually alive (_prewarmPolling) — so a tick can
        //              never re-enable Send for a panel no prewarm ever gated.
        //   Preserves: never two concurrent readers of a draining endpoint (the
        //              rule the widget plugin states and the app broke against
        //              itself). The run-started latch and the 400 ms pre-start
        //              repoll below are untouched.
        //   Lock/thread contract: unchanged — same single browser task queue.
        //   Pinned by: tests/js/vaultStatusDrain.test.js
        if (_prewarmPolling) {
            _renderPrewarmBanner(data.prewarm_status || 'idle',
                                 data.prewarm_message || '');
        }

        // Run-started latch: a terminal state seen before this run was ever
        // observed running is the PREVIOUS run's leftover, not this one's
        // outcome — acting on it ends the lifecycle of a run that is still
        // going (and, for 'error', renders the old error_detail against it).
        // Wait for the real state rather than reporting a stale one.
        if (data.state === 'running' || data.state === 'scanning' || data.state === 'embedding') {
            _indexRunObserved = true;
        } else if (!_indexRunObserved && _prestartPolls < _MAX_PRESTART_POLLS) {
            _prestartPolls += 1;
            setTimeout(pollVaultStatus, _PRESTART_POLL_MS);
            _pollFailures = 0;
            return;
        }

        if (data.state === 'done') {
            _isIndexing = false;
            taskEnd('vault-index', 'completed'); _setIndexProgress(false);
            _updateIndexButtons('done');
            setStatusA11y(document.getElementById('obsidian-status-msg'), '', false);
            // The first-run hint said "not indexed yet" until this moment; flip
            // it to the ask-a-question copy without waiting for a reload.
            _renderVaultEmptyState(data);
            refreshVaultMaterials();
            // The activity bar is NOT a live region (and must not become one:
            // it hosts every long task's ambient text) — so a multi-hour
            // index run used to end with zero SR feedback (2026-08-18).
            announceStatus('Vault indexing complete.');
            flashSidebarStatus('Indexing complete.');
            return;
        } else if (data.state === 'error') {
            _isIndexing = false;
            taskEnd('vault-index', 'failed'); _setIndexProgress(false);
            _updateIndexButtons('error');
            setStatusA11y(document.getElementById('obsidian-status-msg'), '', false);
            // 2026-07-29 audit 0.1: this branch used to blank the status line
            // (which had just received the server's one copy of the reason via
            // the destructive `messages` drain) and show a generic "Indexing
            // failed." + Retry — so the embed-backend gate's refusal, whose
            // whole value is its recovery instructions, was erased in the same
            // poll tick that delivered it, and Retry re-ran the refusing gate
            // forever. The persistent `error_detail`/`error_code` payload
            // fields carry the reason past the drain; the renderer keys its
            // actions on the code (a refusal offers Rebuild, not Retry).
            _showVaultIndexError(data);
            return;
        } else if (data.state === 'paused' || data.state === 'paused_partial') {
            _isIndexing = false;
            taskEnd('vault-index'); _setIndexProgress(false);
            _updateIndexButtons('paused');
            return;
        } else if (data.state === 'paused_scan') {
            _isIndexing = false;
            taskEnd('vault-index'); _setIndexProgress(false);
            _updateIndexButtons('paused_scan');
            setStatusA11y(document.getElementById('obsidian-status-msg'), 'Indexing paused before embedding began. Start indexing again to continue; extraction caches were preserved.', false);
            return;
        }

        setTimeout(pollVaultStatus, 2000);
        _pollFailures = 0;
    } catch (e) {
        // E7 (2026-07-22 audit): silent rescheduling left the "Indexing…"
        // progress strip asserting activity long after the server died. After
        // three consecutive failures, say so (and keep retrying — the
        // state is recoverable if the server comes back).
        _pollFailures = (_pollFailures || 0) + 1;
        if (_pollFailures === 3) {
            const statusDiv = document.getElementById('obsidian-status-msg');
            if (statusDiv) {
                setStatusA11y(statusDiv,
                    'Server unreachable — status unknown; retrying…', true);
            }
        }
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
        status === 'loading_reranker' ||
        status === 'building_wikilinks';
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
    // Item 3.8: hard deadline. The loop used to retry FOREVER — a server gone
    // away (or a prewarm wedged before its terminal state) kept this poll and
    // its banner alive for the app's lifetime. 30 min comfortably covers a
    // cold multi-GB index + BM25 + reranker load; past it, surface an honest
    // terminal error instead of polling into the void.
    const DEADLINE_MS = 30 * 60 * 1000;
    let deadline = Date.now() + DEADLINE_MS;
    try {
        while (true) {
            if (Date.now() > deadline) {
                _renderPrewarmBanner('error', 'Prewarm is not responding — relaunch the app or check the server.');
                return;
            }
            // PERF (2026-09-02): suspend rather than double-drain. See the
            // matching block in pollVaultStatus for the measurement. An index
            // run makes that loop the single reader of /api/obsidian/status;
            // a GET here would consume progress lines it never renders.
            // _isIndexing is true exactly while that loop is alive (every
            // terminal branch clears it, and its first line returns on false),
            // so this cannot suspend against a poller that has stopped.
            // The deadline is pushed out while suspended: prewarm state IS
            // still being observed, through the other loop's payload, so
            // accruing "not responding" here would be a false accusation.
            if (_isIndexing) {
                deadline = Date.now() + DEADLINE_MS;
                await new Promise(r => setTimeout(r, POLL_MS));
                continue;
            }
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
    btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');   // 6c
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

// Chat silence backstops are server-owned and consumed by api.js.

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



/**
 * Digit-blind identity of a progress/info message (2026-07-29 audit 11).
 *
 * Two frames share a key exactly when they differ only in numbers — the shape
 * every periodic ticker produces ("Still waiting … (30s)…" / "(60s)…", "[3/98]
 * …"). Used by the chat `onInfo` handler to update a heartbeat's bubble in
 * place instead of appending one per beat; two messages with any wording
 * difference keep distinct keys, so genuinely new stage labels are never
 * suppressed. Exported for tests.
 */
export function progressKey(msg) {
    return String(msg || '').replace(/\d+/g, '#');
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
 * Render an indexing-run error from the status payload's persistent
 * `error_detail`/`error_code` fields (2026-07-29 audit 0.1).
 *
 * The action set keys on the machine code, never on regexing the prose (the
 * SSE `size_cap` precedent): `embed_backend_mismatch` is a REFUSAL — retrying
 * re-runs the same gate and refuses identically, so offering Retry was
 * actively harmful. It gets a confirm-gated "Rebuild from scratch" instead
 * (→ `indexVault(true)` → the server's `force_fresh` opt-in; the old index is
 * archived server-side, not deleted). Every other code keeps Retry.
 */
function _showVaultIndexError(data) {
    const el = document.getElementById('vault-error-boundary');
    const message = (data && data.error_detail) || 'Indexing failed.';
    const code = (data && data.error_code) || '';
    if (!['embed_backend_mismatch', 'embed_model_mismatch', 'missing_embedding_identity',
        'missing_metadata', 'index_version_mismatch', 'vault_identity_mismatch', 'interrupted_store'].includes(code)) {
        _showVaultError(message, indexVault);
        return;
    }
    const actions = [
        {
            label: 'Rebuild from scratch…',
            primary: true,
            // `btn` is the pressed button (showTaskError hands it over) so the
            // confirm strip anchors where the user is looking. The rebuild is
            // hours of re-embedding — it MUST go through the confirmInline
            // ceremony like every other destructive action.
            onClick: (btn) => confirmInline(btn, {
                message: 'Re-embed the entire vault from scratch? The current '
                    + 'index is archived (not deleted); a large vault can take hours.',
                confirmLabel: 'Rebuild',
                onConfirm: () => { clearTaskError(el); indexVault(true); },
            }),
        },
        { label: 'Dismiss', onClick: () => clearTaskError(el) },
    ];
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
export async function chatWithVault(retryQuestion, opts = {}) {
    if (_isChatting) {
        const statusDiv = document.getElementById('obsidian-status-msg');
        if (statusDiv) setStatusA11y(statusDiv, 'Please wait for the current response to complete.', false);
        return;
    }
    // Enter-key submissions bypass the disabled Send button, so guard
    // here too while prewarm is still walking through the in-flight
    // stages (loading_index / building_bm25 / loading_reranker / building_wikilinks).
    if (_prewarmInFlight) {
        const statusDiv = document.getElementById('obsidian-status-msg');
        if (statusDiv) setStatusA11y(statusDiv, 'Vault is still warming up. Please wait a moment.', false);
        return;
    }
    const input = document.getElementById('obsidian-input');
    // Item 3.5: a Retry passes the failed question DIRECTLY (string guard —
    // DOM event handlers call this with an Event as first arg) so it never
    // has to round-trip through the input box and overwrite a fresh draft
    // the user typed after the failure.
    const fromRetry = typeof retryQuestion === 'string' && retryQuestion.trim() !== '';
    const question = fromRetry ? retryQuestion.trim() : input.value.trim();
    if (!question) return;

    _isChatting = true;
    // C5/#50 (2026-07-13): disable Send for the in-flight turn (parity with
    // plainchat's _setSending) so it doesn't look pressable during a multi-minute
    // agent turn. The Enter-key path is still guarded by the _isChatting check above.
    _setVaultSendDisabled(true);
    clearTaskError(document.getElementById('vault-error-boundary'));
    const chat = document.getElementById('obsidian-chat');
    _clearVaultEmptyState();

    // Retry after a KEPT partial (2026-08-18): the question bubble is already
    // on screen above the interrupted answer — re-rendering it doubled it.
    const questionOnScreen = fromRetry && opts.questionOnScreen === true;
    let userMsg = null;
    if (!questionOnScreen) {
        userMsg = document.createElement('div');
        userMsg.className = 'message message-user';
        userMsg.textContent = question;
        chat.appendChild(userMsg);
        _attachCopyButton(userMsg, () => question, 'Copy question');
    }
    if (!fromRetry) input.value = '';   // a retry leaves the draft untouched

    const botMsg = document.createElement('div');
    botMsg.className = 'message message-bot';
    botMsg.innerHTML = '<span class="typing-indicator"><span></span></span>';
    chat.appendChild(botMsg);
    chat.scrollTop = chat.scrollHeight;
    const answerRenderer = makeAnswerRenderer(botMsg);

    taskBegin('vault-chat', 'Thinking…');
    // Track 6b: the streamed answer renders silently for AT users — announce
    // the generation lifecycle through the global polite region (errors
    // already announce via showTaskError -> announceError).
    announceStatus('Generating response…');

    const controller = new AbortController();
    _chatController = controller;
    _stopRequested = false;

    const ownsTurn = () => _chatController === controller && !controller.signal.aborted;
    const transientInfoNodes = new Set();
    const clearTransientInfo = () => {
        for (const node of transientInfoNodes) node.remove();
        transientInfoNodes.clear();
    };
    let fullAnswer = '';
    let turnSources = [];   // retrieval sources for the export transcript
    // Export provenance (2026-07-22): server-confirmed provider/model, real
    // usage figures, and the agent tool-call list — all captured per turn so
    // the MD/TXT export can stamp what actually ran (the trace/retrieval DOM
    // panels are display-only and were previously lost on export).
    let turnMeta = null;
    let turnUsage = null;
    let turnTrace = [];
    try {
        await saveSelectedModels();
        const chatParams = getVaultChatParams();
        const bodyStr = JSON.stringify({
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
            wikilink_expansion: chatParams.wikilink_expansion,
            thesaurus_expansion: chatParams.thesaurus_expansion,
            primer_enabled: chatParams.primer_enabled,
            web_search_enabled: chatParams.web_search_enabled,
        });

        const traceCtx = { trace: null, currentIter: null, iterCount: 0 };
        // Progress-frame dedupe state (2026-07-29 audit 11) — per send, so a
        // new question always starts with a fresh bubble.
        let lastInfoNode = null;
        let lastInfoKey = '';

        await consumeSSE('/api/obsidian/chat', { method: 'POST', signal: controller.signal, body: bodyStr, serverBudget: true }, {
            onInfo: (info) => {
                if (!ownsTurn()) return;
                // Heartbeat dedupe (2026-07-29 audit 11): the reranker/BM25
                // load tickers emit the SAME sentence every 30 s with only the
                // elapsed-seconds figure changing — a 20-minute first-time
                // model download appended ~40 near-identical bubbles and fired
                // ~40 screen-reader announcements. A frame whose digit-blind
                // key matches the PREVIOUS info frame updates that bubble in
                // place and is not re-announced (the first occurrence was);
                // any genuinely different stage label still appends + announces
                // exactly as before — the key comparison is only against the
                // immediately preceding frame, so an A/B/A sequence renders
                // three bubbles, never a rewritten history.
                const key = progressKey(info);
                if (lastInfoNode && key && key === lastInfoKey) {
                    lastInfoNode.textContent = info;
                    chat.scrollTop = chat.scrollHeight;
                    return;
                }
                const note = document.createElement('div');
                note.className = 'message message-info';
                // NOT role="status" here (2026-07-26 UX audit): this node is
                // created, roled and populated in the SAME tick — the exact
                // defect class tests/js/liveRegions.test.js exists to prevent
                // (WebKit can miss a live region minted the same tick it is
                // filled), so the role was decorative and the message was
                // likely never announced. Consequence: during a slow stage —
                // a multi-GB reranker load, a provider fallback — a VoiceOver
                // user heard "Generating response…" then silence, with the one
                // notice that EXPLAINS the wait inaudible. The bubble stays
                // visual-only; the announcement goes through the static
                // #sr-status-announcer region, which ships in the markup.
                note.textContent = info;
                // As in Read, ellipsis-terminated info is live progress, not
                // a durable warning. Remove it when this turn terminates.
                if (/(…|\.\.\.)\s*$/.test(String(info).trim())) transientInfoNodes.add(note);
                chat.insertBefore(note, botMsg);
                chat.scrollTop = chat.scrollHeight;
                lastInfoNode = note;
                lastInfoKey = key;
                announceStatus(info);
            },
            onToken: (token) => {
                if (!ownsTurn()) return;
                if (fullAnswer === '') botMsg.innerHTML = '';
                fullAnswer += token;
                answerRenderer.update(fullAnswer);
            },
            onOther: (payload) => {
                if (!ownsTurn()) return;
                if (payload.retrieval) {
                    // Keep the full retrieval objects ({source, score,
                    // is_image, paper?}) for the export transcript (lot 4.3 +
                    // 2026-07-22): the Retrieval Context panel is DOM-only, so
                    // without this the export would lose the provenance —
                    // often the most valuable part of a vault answer.
                    turnSources = payload.retrieval
                        .filter((r) => r && r.source);
                    _renderRetrievalTrace(payload.retrieval, botMsg, chat);
                } else if (payload.meta) {
                    turnMeta = payload.meta;
                } else if (payload.usage) {
                    turnUsage = payload.usage;
                } else if (payload.iteration !== undefined) {
                    _renderAgentIteration(payload.iteration, traceCtx, botMsg, chat);
                } else if (typeof payload.thought === 'string') {
                    _renderAgentThought(payload.thought, traceCtx, botMsg, chat);
                } else if (payload.tool_call) {
                    turnTrace.push(payload.tool_call);
                    _renderAgentToolCall(payload.tool_call, traceCtx, botMsg, chat);
                } else if (payload.tool_result) {
                    if (payload.tool_result.is_error && turnTrace.length) {
                        // Mark the matching call so the export shows failures.
                        const match = turnTrace.find(
                            (c) => c.id === payload.tool_result.tool_call_id);
                        if (match) match.is_error = true;
                    }
                    _renderAgentToolResult(payload.tool_result, traceCtx, botMsg, chat);
                }
            },
            onError: (errMsg) => { throw new Error(errMsg); },
            onDone: () => {}
        });

        answerRenderer.flush();
        if (fullAnswer) {
            _attachCopyButton(botMsg, () => fullAnswer, 'Copy answer');
            enhanceCodeBlocks(botMsg);
            _pushUserTurnOnce(question, fromRetry);
            _pushVaultTurn({
                role: 'assistant', content: fullAnswer, sources: turnSources,
                meta: turnMeta || undefined,
                usage: turnUsage || undefined,
                trace: turnTrace.length ? turnTrace : undefined,
            });
            _recordedRetryQuestion = null;   // this question is fully answered
        }
        if (!fullAnswer) botMsg.textContent = '(No response from the model.)';
        announceStatus(fullAnswer ? 'Response ready.' : 'No response from the model.');
    } catch (e) {
        // Item 3.5: KEEP a partial answer. A mid-stream {error} after real
        // tokens used to remove the whole bubble — discarding content the
        // user watched arrive (and may already be reading). Now: with any
        // streamed text, render it final, mark it interrupted, and offer
        // Retry alongside; only an empty bubble is removed. Retry re-sends
        // the ORIGINAL question directly (see retryQuestion above) instead
        // of stuffing it back into the input, so a draft typed after the
        // failure is never clobbered.
        if (fullAnswer || turnSources.length) {
            answerRenderer.flush();
            if (!fullAnswer) botMsg.textContent = '(No response from the model.)';
            const note = document.createElement('div');
            note.className = 'muted';
            note.textContent = '⚠ Response interrupted — the text above is partial.';
            botMsg.appendChild(note);
            if (fullAnswer) _attachCopyButton(botMsg, () => fullAnswer, 'Copy answer');
            _pushUserTurnOnce(question, fromRetry);
            _pushVaultTurn({
                role: 'assistant', content: fullAnswer + '\n[interrupted]',
                sources: turnSources,
                meta: turnMeta || undefined,
                trace: turnTrace.length ? turnTrace : undefined,
            });
            // This question now has a user turn on record; a Retry of it must
            // not add a second one (a retry that ALSO fails partially appends
            // only its own [interrupted] assistant turn).
            _recordedRetryQuestion = question;
        } else {
            answerRenderer.cancel();
            botMsg.remove();
        }
        const msg = e.name === 'AbortError'
            ? (_stopRequested ? 'Stop requested — the response is interrupted. The provider may still be finishing its current operation.' : 'Response timed out. The model may be overloaded — please try again.')
            : ('Error: ' + e.message);
        _showVaultError(msg, () => {
            // keep the pair when a partial stays visible
            if (!fullAnswer && !turnSources.length && userMsg) userMsg.remove();
            chatWithVault(question, {
                questionOnScreen: !!fullAnswer || !!turnSources.length || questionOnScreen,
            });
        });
    } finally {
        clearTransientInfo();
        answerRenderer.cancel();
        if (_chatController !== controller) return;
        _chatController = null;
        _isChatting = false;
        _setVaultSendDisabled(false);
        taskEnd('vault-chat');
        chat.scrollTop = chat.scrollHeight;
    }
}

/** C5/#50: toggle the vault-chat Send button, unless prewarm already owns it. */
function _setVaultSendDisabled(disabled) {
    const stop = document.getElementById('vault-stop-btn');
    if (stop) {
        if (!_isChatting && document.activeElement === stop) document.getElementById('obsidian-input')?.focus();
        stop.hidden = !_isChatting; stop.disabled = false; stop.textContent = 'Stop';
    }
    const sendBtn = document.getElementById('vault-send-btn');
    if (!sendBtn) return;
    // Don't re-enable while prewarm is still in flight (it owns the button then).
    if (!disabled && _prewarmInFlight) return;
    // Disabling the focused element drops focus to <body> (2026-08-18):
    // park it on the input first so keyboard/SR position survives.
    if (disabled && document.activeElement === sendBtn) {
        const input = document.getElementById('obsidian-input');
        if (input) input.focus();
    }
    sendBtn.disabled = disabled;
}

export function clearVaultChat(confirmed = false) {
    const hasTranscript = _vaultHistory.length || document.querySelector('#obsidian-chat .message');
    if (!_isChatting && hasTranscript && !confirmed) {
        confirmInline(document.getElementById('vault-clear-chat-btn'), {
            message: 'Discard this conversation? Export it first if you want to keep it.',
            confirmLabel: 'Discard conversation',
            onConfirm: () => clearVaultChat(true),
        });
        return;
    }
    // Server holds no chat history (each /api/obsidian/chat call is independent),
    // so clearing is purely a DOM operation. We refuse while a query is in flight
    // so the in-progress bot message is not orphaned mid-stream.
    if (_isChatting) {
        const statusDiv = document.getElementById('obsidian-status-msg');
        if (statusDiv) setStatusA11y(statusDiv, 'Wait for the current response to finish before clearing.', false);
        return;
    }
    const chat = document.getElementById('obsidian-chat');
    if (chat) chat.innerHTML = '';
    _vaultHistory.length = 0;
    const statusDiv = document.getElementById('obsidian-status-msg');
    if (statusDiv) setStatusA11y(statusDiv, '', false);
}

/**
 * Export the current vault conversation to the user's Downloads as md or txt
 * (axis iv). Serialises the client-side transcript and reuses the existing
 * /api/export-summary writer (format-agnostic; it just writes the string).
 */
export async function exportVaultConversation(format) {
    if (!_vaultHistory.length) {
        const statusDiv = document.getElementById('obsidian-status-msg');
        if (statusDiv) setStatusA11y(statusDiv, 'No conversation to export yet.', false);
        return;
    }
    const statusDiv = document.getElementById('obsidian-status-msg');
    // Provenance header (2026-07-22): export date, last-confirmed model, and a
    // snapshot of the retrieval knobs as they are set NOW (per-turn meta lines
    // carry each answer's own model when it differed).
    const lastMeta = [..._vaultHistory].reverse().find((t) => t.meta)?.meta || null;
    const header = { 'Exported': new Date().toISOString() };
    if (lastMeta) {
        header['Model'] = [lastMeta.provider, lastMeta.model].filter(Boolean).join(' / ');
        if (lastMeta.agent_enabled) header['Mode'] = 'agent';
    }
    try {
        const p = getVaultChatParams();
        header['Retrieval'] = `top_k=${p.top_k} · mode=${p.prompt_mode} · hybrid=${p.hybrid_enabled} · reranker=${p.reranker_enabled}`;
    } catch (_) { /* knobs unavailable — header stays minimal */ }
    const { format: fmt, content } = buildConversationExport(
        _vaultHistory, format, 'Vault Conversation', { header });
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    try {
        // raw: true — the transcript carries its own title, and the file
        // should be VaultChat_<stamp>.md, not Summary_VaultChat_… with a
        // second injected heading (see /api/export-summary's raw flag).
        const r = await secureFetch('/api/export-summary', {
            method: 'POST',
            body: JSON.stringify({ format: fmt, filename: `VaultChat_${stamp}`, content, raw: true }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) {
            if (statusDiv) setStatusA11y(statusDiv, d.error || `Export failed (${r.status})`, true);
            return;
        }
        if (statusDiv) setStatusA11y(statusDiv, `Exported to ${d.path || 'Downloads'}`, false);
    } catch (err) {
        if (statusDiv) setStatusA11y(statusDiv, String(err), true);
    }
}

export async function pauseVaultIndex() {
    // C5/#41 (2026-07-13): give immediate feedback and surface failure.
    const statusEl = document.getElementById('obsidian-status-msg');
    if (statusEl) setStatusA11y(statusEl, 'Pausing…', false);
    try {
        const resp = await secureFetch('/api/obsidian/pause', { method: 'POST' });
        if (!resp.ok && statusEl) {
            const err = await safeJson(resp);
            setStatusA11y(statusEl, (err && err.error) || 'Pause failed.', true);
        }
    } catch (e) {
        logError('Pause failed', e);
        if (statusEl) setStatusA11y(statusEl, 'Pause failed (see console).', true);
    }
}

export async function resumeVaultIndex() {
    await indexVault();
}

export async function cancelVaultIndex() {
    // C1/#16 (2026-07-13): only claim "cancelled" when the POST actually
    // succeeded — on failure the server keeps indexing, so keep polling and say
    // so instead of falsely reporting the run dead.
    const statusEl = document.getElementById('obsidian-status-msg');
    let ok = false;
    try {
        const resp = await secureFetch('/api/obsidian/cancel', { method: 'POST' });
        ok = resp.ok;
        if (!ok && statusEl) {
            const err = await safeJson(resp);
            const detail = (err && err.error) ? ` (${err.error})` : '';
            setStatusA11y(statusEl, `Cancel failed — indexing continues${detail}.`, true);
        }
    } catch (e) {
        logError('Cancel failed', e);
        if (statusEl) setStatusA11y(statusEl, 'Cancel failed — indexing continues (see console).', true);
    }
    if (!ok) return;   // leave _isIndexing + polling alone; the run is still live
    // Only the INDEXING machine's guard — a live chat keeps its own flag.
    _isIndexing = false;
    taskEnd('vault-index'); _setIndexProgress(false);
    _updateIndexButtons('idle');
    if (statusEl) setStatusA11y(statusEl, 'Indexing cancelled.', false);
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
    const status = document.getElementById('excl-status');
    try {
        const cfgResp = await secureFetch('/api/config');
        const cfg = await cfgResp.json();
        const currentExcl = cfg.vault_exclude_dirs || [];
        const filtered = currentExcl.filter(p => p !== path);
        const resp = await secureFetch('/api/config', {
            method: 'POST',
            body: JSON.stringify({ vault_exclude_dirs: filtered })
        });
        // C1/#42 (2026-07-13): surface a failed save (contrast addExclusion,
        // which already does) — otherwise the row silently reappears on re-render.
        if (!resp.ok && status) {
            const err = await safeJson(resp);
            status.textContent = (err && err.error) || `Could not update exclusions (HTTP ${resp.status}).`;
        }
        renderExclusions();
    } catch (e) {
        logError('Remove exclusion failed', e);
        if (status) status.textContent = 'Could not update exclusions (see console).';
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
                // Row context (2026-08-18): N identical "Remove" names are
                // indistinguishable in Tab/forms mode.
                btn.setAttribute('aria-label', `Remove the exclusion for ${path}`);
                // Ceremony parity (Track 6e): removing an exclusion re-includes
                // the folder in the next index run — gate it like every other
                // config-mutating action instead of firing on a single click.
                btn.onclick = () => confirmInline(btn, {
                    message: `Remove the exclusion for "${path}"? The folder is re-included at the next index run.`,
                    confirmLabel: 'Remove',
                    tone: 'confirm',
                    onConfirm: () => removeExclusion(path),
                });
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
/* ---------------------------------------------------------------------------
 * First-run empty state for the vault transcript (2026-09-03 UI review).
 *
 * Defect: `#obsidian-chat` was an empty <div> on first run. This is the tab the
 * app opens on, and the Library Audit tab's own (good) empty state points a new
 * user straight here — so the first thing they saw was an empty box beside a
 * Send button, with nothing saying that answers come only from an index they
 * have not built.
 *
 * Decided from what /api/obsidian/status ALREADY carries (`vault_path` +
 * `state`) — no new server field, and no extra request. It is a HINT, not a
 * message: no `role`, so the two live regions on this panel (the status line
 * and the error boundary) stay the only things that announce; removed before
 * the first turn is appended; and never painted over a transcript that already
 * holds messages. Nothing is painted while a run is in flight — there the
 * progress bar and the status line are already speaking.
 * ------------------------------------------------------------------------- */
const _VAULT_RUN_STATES = ['running', 'scanning', 'embedding'];

function _renderVaultEmptyState(data) {
    const chat = document.getElementById('obsidian-chat');
    if (!chat) return;
    const existing = chat.querySelector('.chat-empty-state');
    // A real transcript wins: never paint over (or beside) actual messages.
    if (chat.querySelector('.message')) {
        if (existing) existing.remove();
        return;
    }
    const state = (data && data.state) || '';
    // Mid-run, and error, have their own dedicated surfaces on this panel.
    if (_VAULT_RUN_STATES.includes(state) || state === 'error') {
        if (existing) existing.remove();
        return;
    }
    const hasVault = !!(data && data.vault_path);
    const indexed = state === 'done' || state === 'paused_partial';
    let text;
    if (!hasVault) {
        text = 'No vault yet. Pick your Obsidian folder with Change… above, '
             + 'then click “Update Index”. Answers on this tab come only from '
             + 'what the index contains.';
    } else if (!indexed) {
        text = 'This vault is not indexed yet — click “Update Index”. Indexing is '
             + 'incremental, and you can pause or cancel a long first run.';
    } else {
        text = 'Ask a question about your notes. Each answer lists the notes it '
             + 'came from; retrieval and agent settings live in ⚙ Settings.';
    }
    const el = existing || document.createElement('div');
    el.className = 'chat-empty-state';
    el.textContent = text;
    if (!existing) chat.appendChild(el);
}

/** Drop the first-run hint the moment a real turn starts. */
function _clearVaultEmptyState() {
    const el = document.querySelector('#obsidian-chat .chat-empty-state');
    if (el) el.remove();
}

export async function refreshIndexState() {
    try {
        const resp = await secureFetch('/api/obsidian/status');
        const data = await resp.json();
        if (data.vault_path) {
            const displayEl = document.getElementById('vault-path-display');
            if (displayEl) displayEl.textContent = data.vault_path;
        }
        _updateIndexButtons(data.state);
        _renderVaultEmptyState(data);
        // A run already in flight (e.g. the page was reloaded mid-index, or the
        // run was started by a prior session) won't have gone through
        // indexVault(), so the activity bar + status polling were never armed.
        // Re-arm them here so the honest "working" signal reappears and status
        // messages keep flowing until a terminal state.
        if ((data.state === 'running' || data.state === 'scanning' || data.state === 'embedding') && !_isIndexing) {
            _isIndexing = true;
            // This branch only fires on a state that IS running, so the run is
            // observed by construction — the poll loop it arms must not spend
            // its pre-start allowance waiting for what we just saw.
            _indexRunObserved = true;
            _prestartPolls = 0;
            _setIndexProgress(true);
            _vaultPollDeadline = Date.now() + 24 * 60 * 60 * 1000;
            taskBegin('vault-index', 'Indexing vault…');
            pollVaultStatus();
        }
        if (Array.isArray(data.warnings) && data.warnings.length) {
            renderVaultWarnings(data.warnings);
        }
        // This one-shot GET drains the same one-shot feed, so anything it
        // takes is gone for the poll loop it may be about to arm. Render the
        // latest line rather than dropping the batch (2026-09-02).
        if (Array.isArray(data.messages) && data.messages.length > 0) {
            setStatusA11y(document.getElementById('obsidian-status-msg'),
                          data.messages[data.messages.length - 1], false);
        }
        // A run that errored before this tab mounted (or before a reload) left
        // its reason only in the persistent error_detail field — the live poll
        // that would have rendered it is long gone. Re-surface it here so the
        // refusal (and its Rebuild recovery) survives a reload (2026-07-29).
        if (data.state === 'error' && data.error_detail) {
            _showVaultIndexError(data);
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


export async function chooseOnlineSources(offset = 0) {
    const host = document.getElementById('online-source-picker');
    if (!host) return;
    try {
        const query = document.getElementById('online-source-search')?.value || '';
        const response = await secureFetch(`/api/online-ocr/sources?offset=${offset}&q=${encodeURIComponent(query)}`);
        const data = await safeJson(response);
        if (!response.ok) throw new Error(data.error);
        host.replaceChildren();
        for (const source of data.sources) {
            const label = document.createElement('label');
            const input = document.createElement('input');
            input.type = 'checkbox'; input.checked = _onlineSelection.has(source);
            input.addEventListener('change', () => input.checked ? _onlineSelection.add(source) : _onlineSelection.delete(source));
            label.append(input, document.createTextNode(source));
            host.append(label, document.createElement('br'));
        }
        if (data.next_offset != null) {
            const next = document.createElement('button'); next.textContent = 'Next 50 sources';
            next.addEventListener('click', () => chooseOnlineSources(data.next_offset)); host.append(next);
        }
    } catch (error) { _onlineOcrMessage(error.message); }
}

let _historyRequest = 0;

export async function refreshOnlineHistory(offset = 0, focus = false) {
    const host = document.getElementById('online-run-history');
    if (!host) return;
    const generation = ++_historyRequest;
    try {
        const {response, body: data} = await onlineJson('/api/online-ocr/runs' + (offset ? `?offset=${offset}` : ''));
        if (!response.ok) throw new Error(data.error);
        if (generation !== _historyRequest) return;
        host.replaceChildren();
        for (const run of data.runs) {
            const row = document.createElement('article'); row.className = 'run-card';
            const state = run.worker_state || run.state;
            const labels = {indexed: 'Search updated', completed_with_omissions: 'Completed with exceptions', extraction_incomplete: 'Text preparation incomplete', index_incomplete: 'Search update incomplete', promotion_or_index_incomplete: 'Cache publication interrupted — inspect recovery evidence', status_unrecorded: 'Final outcome unknown', done: 'Text ready', text_prepared: 'Text prepared', running: 'Running', failed: 'Needs attention', cancelled: 'Interrupted'};
            const label = labels[state] || 'Review outcome';
            const title = document.createElement('h4');
            title.textContent = `${run.prepare_only ? 'Text preparation' : run.full_rebuild ? 'Search index rebuild' : run.kind === 'file' ? 'Document extraction' : 'Search update'} · ${run.recorded_at ? new Date(run.recorded_at * 1000).toLocaleString() : 'Date unavailable'}`;
            const badge = document.createElement('span'); badge.className = 'operation-state'; badge.textContent = label;
            const summary = document.createElement('p');
            summary.textContent = `${run.source_count ?? 'Unknown number of'} documents selected.` +
                (run.deferred_count ? ` ${run.deferred_count} deferred. Previous passages are retained where available and compatible.` : '') +
                (run.status_note ? ` ${run.status_note}` : '');
            const technical = document.createElement('details');
            const technicalLabel = document.createElement('summary'); technicalLabel.textContent = 'Operation reference';
            const reference = document.createElement('p'); reference.textContent = `Run ${run.run_id} · ${state}`;
            technical.append(technicalLabel, reference);
            row.append(title, badge, summary);
            const inspect = document.createElement('button'); inspect.textContent = 'Review documents'; inspect.className = 'btn btn-primary';
            const detailHost = document.createElement('div');
            let detailRequest = 0;
            const showDetail = async (detailOffset = 0) => {
                const generation = ++detailRequest;
                inspect.disabled = true;
                inspect.textContent = 'Loading documents…';
                try {
                    const {response: res, body: detail} = await onlineJson(`/api/online-ocr/runs/${run.run_id}?offset=${detailOffset}&limit=50`);
                    if (!res.ok) throw new Error(detail.error);
                    if (generation !== detailRequest) return;
                    detailHost.replaceChildren();
                    const evidence = document.createElement('div');
                    evidence.tabIndex = -1;
                    evidence.textContent = `${detail.sources?.length || 0} documents shown. ${detail.source_count ?? 'Unknown'} documents in this operation.`;
                    const sourceRows = new Map();
                    for (const source of detail.sources || []) {
                        const sourceRow = document.createElement('div'); sourceRow.className = 'source-result';
                        const name = document.createElement('strong'); name.textContent = source.source;
                        const outcome = document.createElement('p');
                        const context = [source.provider, source.model, source.http_status ? `HTTP ${source.http_status}` : '', source.provider_code].filter(Boolean).join(' · ');
                        const error = source.error_detail || source.error || (source.status === 'request_failed' ? 'The provider error details were not recorded for this older attempt.' : '');
                        outcome.textContent = [context, (source.status || 'Status unavailable').replaceAll('_', ' '), error].filter(Boolean).join(' — ');
                        const next = document.createElement('p'); next.textContent = source.next_step || (source.status === 'request_failed' ? 'Check provider settings, then explicitly retry this document to obtain a current error. No retry runs automatically.' : '');
                        sourceRow.append(name, outcome, next); evidence.append(sourceRow); sourceRows.set(source.source, sourceRow);
                    }
                    detailHost.append(evidence);
                    const technical = document.createElement('details');
                    const technicalLabel = document.createElement('summary');
                    technicalLabel.textContent = 'Technical recovery evidence';
                    const raw = document.createElement('pre');
                    raw.textContent = JSON.stringify(detail, null, 2);
                    technical.append(technicalLabel, raw); detailHost.append(technical);
                    for (const source of detail.sources || []) if (source.export_url) {
                        const download = document.createElement('button'); download.textContent = `Review ${source.source}`;
                        download.addEventListener('click', () => downloadOnlineExport(source.export_url)); (sourceRows.get(source.source) || detailHost).append(download);
                    }
                    for (const source of detail.sources || []) {
                        if (!source.source_sha256) continue;
                        const report = document.createElement('button');
                        report.textContent = `Report bad extraction: ${source.source}`;
                        report.addEventListener('click', () => confirmInline(report, {
                            message: 'Defer this document until an explicit retry? Existing indexed passages and caches are retained. This records your assessment, not an automatic accuracy check.',
                            confirmLabel: 'Defer document', tone: 'confirm', onConfirm: async () => {
                                try {
                                    const response = await secureFetch('/api/online-ocr/report-bad', {method: 'POST',
                                        body: JSON.stringify({path: source.source, source_sha256: source.source_sha256, confirm: true})});
                                    const body = await safeJson(response);
                                    if (!response.ok) throw new Error(body.error || 'Decision refused');
                                    _onlineOcrMessage('Document deferred until explicit retry. Existing passages retained.');
                                    report.disabled = true;
                                } catch (error) { _onlineOcrMessage(error.message); }
                            }
                        }));
                        const retryOne = document.createElement('button');
                        retryOne.textContent = `Review targeted retry: ${source.source}`;
                        retryOne.addEventListener('click', () => startOnlineIndex(true,
                            true, retryOne, true, null, null, [source.source]));
                        (sourceRows.get(source.source) || detailHost).append(report, retryOne);
                    }
                    const nextOffset = detail.next_offset ?? detail.promotion_next_offset;
                    for (const [label, target] of [
                        ['Previous evidence page', detailOffset > 0 ? Math.max(0, detailOffset - 50) : null],
                        ['Next evidence page', nextOffset]
                    ]) if (target != null) {
                        const button = document.createElement('button'); button.textContent = label;
                        button.addEventListener('click', () => showDetail(target)); detailHost.append(button);
                    }
                    evidence.focus();
                } catch (error) { _onlineOcrMessage(error.name === 'AbortError' ? 'Document history timed out. Try loading it again; no extraction was started.' : error.message); }
                finally { if (generation === detailRequest) { inspect.disabled = false; inspect.textContent = 'Review documents'; } }
            };
            inspect.addEventListener('click', () => showDetail());
            row.append(inspect, detailHost);
            if (run.retry_allowed && run.state !== 'indexed') {
                const retry = document.createElement('button'); retry.textContent = 'Retry unfinished extraction';
                retry.addEventListener('click', () => confirmInline(retry, {
                    message: `Retry unfinished extraction using the same attempt bounds, then ${run.prepare_only ? 'prepare text without indexing' : run.kind !== 'session' ? 'export for review' : run.full_rebuild ? 'archive and rebuild the entire index' : 'update the index'}? Unknown previous calls may have been billed. Settings and sources must still match.`,
                    confirmLabel: 'Retry this run', tone: 'confirm', onConfirm: async () => {
                        retry.disabled = true;
                        try {
                            const response = await secureFetch(run.kind === 'file' ? '/api/online-ocr/start' : '/api/online-ocr/session/start', {method: 'POST', body: JSON.stringify({
                                run_index: run.kind !== 'extraction_session', resume_run_id: run.run_id, session_token: run.session_token,
                                reextract: run.kind === 'file' ? run.reextract : !run.routine, paths: run.paths, retry_failed: true,
                                path: run.source, source_sha256: run.source_sha256, preview_token: run.preview_token,
                                page_start: run.page_range?.[0], page_end: run.page_range?.[1],
                                confirm_external: !run.no_external, no_external: !!run.no_external, prepare_only: !!run.prepare_only,
                                operation_mode: run.initiating_action !== 'legacy_session' ? run.operation_mode : undefined,
                                initiating_action: run.initiating_action !== 'legacy_session' ? run.initiating_action : undefined,
                                confirm_index: run.initiating_action !== 'legacy_session' && run.operation_mode === 'prepare_text' ? false : true, full_rebuild: run.full_rebuild,
                                confirm_full_rebuild: run.full_rebuild,
                                provider: run.ocr?.provider, model: run.ocr?.model,
                                vision_provider: run.vision?.provider, vision_model: run.vision?.model
                            })});
                            const data = await safeJson(response);
                            if (!response.ok) throw new Error(data.error);
                            rememberOnlineRun(data.run_id || run.run_id);
                            await pollOnlineOcr();
                        } catch (error) { _onlineOcrMessage(error.message); }
                        finally { retry.disabled = false; }
                    }
                }));
                row.append(retry);
            }
            if (['file', 'extraction_session'].includes(run.kind) && run.state === 'done' && !run.promotion_barrier) {
                const promote = document.createElement('button'); promote.textContent = 'Use completed file text';
                promote.addEventListener('click', () => confirmInline(promote, {
                    message: 'Verify complete document coverage and promote this file, then update the index once? Other uncached binaries remain deferred.',
                    confirmLabel: 'Promote and index', tone: 'confirm',
                    onConfirm: () => recoverOnlineRun(promote, run.kind === 'file' ? '/api/online-ocr/promote' : '/api/online-ocr/session/promote', {run_id: run.run_id, path: run.source, confirm_index: true})
                }));
                row.append(promote);
            }
            if (run.index_continuation_available && !['indexed', 'completed_with_omissions'].includes(run.state)) {
                const resume = document.createElement('button'); resume.textContent = 'Finish interrupted search update';
                resume.addEventListener('click', () => confirmInline(resume, {
                    message: 'Finish the search update using the text already saved? No images are sent again. The app first verifies that the saved text and index settings still match; otherwise it stops without starting the update.',
                    confirmLabel: 'Finish search update', tone: 'confirm',
                    onConfirm: () => recoverOnlineRun(resume, '/api/online-ocr/session/continue-index', {run_id: run.run_id, confirm_index: true})
                }));
                row.append(resume);
            }
            if ((!run.kind || run.kind === 'session') && run.retry_allowed && run.state !== 'indexed') {
                const omit = document.createElement('button'); omit.textContent = 'Review exact omissions';
                omit.addEventListener('click', async () => {
                    omit.disabled = true;
                    try {
                        const omitted = [];
                        let offset = 0;
                        do {
                            const response = await secureFetch(`/api/online-ocr/runs/${run.run_id}?offset=${offset}&limit=100`);
                            const detail = await safeJson(response);
                            if (!response.ok) throw new Error(detail.error);
                            for (const source of detail.sources) if (!['processed_unverified', 'ocr'].includes(source.status)) omitted.push(source.source);
                            offset = detail.next_offset;
                        } while (offset != null);
                        if (!omitted.length) throw new Error('No unfinished sources are recorded.');
                        const list = document.createElement('pre'); list.textContent = omitted.sort().join('\n'); row.append(list);
                        confirmInline(omit, {
                            message: `Continue with exactly these ${omitted.length} deferred sources? ${run.prepare_only ? 'Save complete text for review; the search index stays unchanged.' : run.full_rebuild ? 'The previous index is archived; these sources are omitted from the new index.' : 'Existing chunks for these sources are retained if present; new content is not inserted.'}`,
                            confirmLabel: 'Accept these omissions', tone: 'confirm',
                            onConfirm: () => recoverOnlineRun(omit, '/api/online-ocr/session/continue-incomplete', {
                                run_id: run.run_id, session_token: run.session_token, omit_sources: omitted,
                                operation_mode: run.initiating_action !== 'legacy_session' ? run.operation_mode : undefined,
                                initiating_action: run.initiating_action !== 'legacy_session' ? run.initiating_action : undefined,
                                confirm_preparation: run.operation_mode === 'prepare_text',
                                confirm_index: run.initiating_action !== 'legacy_session' && run.operation_mode === 'prepare_text' ? false : true,
                                confirm_omissions: true, confirm_full_rebuild: run.full_rebuild
                            })
                        });
                    } catch (error) { _onlineOcrMessage(error.message); }
                    finally { omit.disabled = false; }
                });
                row.append(omit);
            }
            row.append(technical);
            for (const button of row.querySelectorAll('button')) if (!button.className) button.className = 'btn btn-outline';
            host.append(row);
        }
        if (!data.runs?.length) host.textContent = 'No saved operations yet.';
        for (const [label, target] of [
            ['Newer runs', offset > 0 ? Math.max(0, offset - 20) : null],
            ['Older runs', data.next_offset]
        ]) if (target != null) {
            const button = document.createElement('button'); button.textContent = label;
            button.addEventListener('click', () => refreshOnlineHistory(target, true)); host.append(button);
        }
        if (focus) { host.tabIndex = -1; host.focus(); }
    } catch (error) {
        if (generation === _historyRequest) host.textContent = error.message;
    }
}

async function recoverOnlineRun(button, endpoint, body) {
    if (_onlineSubmitting) return;
    _onlineSubmitting = true; button.disabled = true;
    try {
        const response = await secureFetch(endpoint, {method: 'POST', body: JSON.stringify(body)});
        const result = await safeJson(response);
        if (!response.ok) {
            if (busyRejection(response, result)) {
                announceStatus(result.error + ' Following the active operation; recovery was not replayed.');
                await pollOnlineOcr();
                return;
            }
            throw new Error(result.error);
        }
        rememberOnlineRun(result.run_id || body.run_id);
        await pollOnlineOcr();
    } catch (error) { _onlineOcrMessage(error.message); }
    finally { _onlineSubmitting = false; button.disabled = false; }
}

async function downloadOnlineExport(url) {
    try {
        const response = await secureFetch(url);
        if (!response.ok) throw new Error('Export unavailable');
        const blobUrl = URL.createObjectURL(await response.blob());
        const link = document.createElement('a'); link.href = blobUrl; link.download = 'extraction-review.md';
        document.body.append(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
    } catch (error) { _onlineOcrMessage(error.message); }
}

async function refreshIndexLibraryFreshness() {
    const host = document.getElementById('index-library-freshness');
    if (!host) return;
    try {
        const response = await secureFetch('/api/audit/summary');
        const result = await safeJson(response);
        if (!response.ok) return;
        host.textContent = `Last Audit: ${result.scanned_at ? new Date(result.scanned_at * 1000).toLocaleString() : 'unknown'}. ` +
            `Labels generated: ${result.paper_index_built_at || 'unknown'}. ` +
            'Current filesystem freshness is unknown. Indexing does not run Audit. Audit skip-prefix and search exclusions/reference-image scopes differ.';
    } catch { /* Keep the honest unknown-freshness text. */ }
}
