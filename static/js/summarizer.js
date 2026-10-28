/**
 * Single Paper tab: upload a PDF, stream a tunable summary, and export it.
 *
 * Per the JS module hierarchy this imports only ui.js + api.js (and config.js for
 * getActiveProvider — config is itself a ui.js/api.js-only module). The five
 * generation knobs (temperature/max_tokens/num_ctx/top_p/repeat_penalty) are read
 * by id from the controls settings.js owns (paper_* persistence) and sent
 * per-request; the server re-resolves them body → persisted paper_* → default.
 */
import { secureFetch, consumeSSE } from './api.js';
import { showTaskError, clearTaskError, copyToClipboard, announceStatus, announceError, taskBegin, taskUpdate, taskEnd, makeAnswerRenderer } from './ui.js';
import { getActiveProvider } from './config.js';

let _currentUploadId = null;
let _currentFilename = 'summary';
let _summaryText = ''; // Raw model Markdown: copy/export must not scrape rendered DOM.
let _summaryRenderer = null;
// Provenance of the LAST generated summary (the {"meta"}/{"usage"} SSE frames);
// stamped into exports. Cleared on reset/upload alongside _currentFilename.
let _lastSummaryMeta = null;
let _lastSummaryUsage = null;
let _summaryAbortController = null;
let _uploadAbortController = null;   // C5/#47: abortable in-flight upload

/** Populate the document-type <select> from /api/report-types (built-ins merged
 * with saved custom/overridden types server-side). Falls back to a lone
 * "Default" option on failure so the control is never empty. */
export async function loadReportTypes() {
    const select = document.getElementById('report-type-select');
    if (!select) return;
    try {
        const resp = await secureFetch('/api/report-types');
        const data = await resp.json();
        const reportTypes = data.report_types || data || [];
        select.innerHTML = '';
        for (const item of reportTypes) {
            const opt = document.createElement('option');
            opt.value = item.id;
            opt.textContent = item.name;
            select.appendChild(opt);
        }
    } catch (e) {
        select.innerHTML = '<option value="">Default</option>';
        console.error('Report types failed:', e);
    }
}

// Wire the upload dropzone for mouse, keyboard, and drag-and-drop. The
// zone is a role="button" element (see index.html) so keyboard users can
// trigger the native file picker; dropping a PDF funnels into the same
// uploadPDF() path the picker uses.
export function wireUploadDropzone() {
    const zone = document.getElementById('upload-overlay');
    const input = document.getElementById('pdf-upload');
    if (!zone || !input) return;

    zone.addEventListener('click', () => input.click());
    zone.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            input.click();
        }
    });

    zone.addEventListener('dragover', (e) => {
        e.preventDefault();
        zone.classList.add('dragover');
    });
    zone.addEventListener('dragleave', (e) => {
        // Ignore dragleave bubbling up from children still inside the zone.
        if (e.target === zone) zone.classList.remove('dragover');
    });
    zone.addEventListener('drop', (e) => {
        e.preventDefault();
        zone.classList.remove('dragover');
        const file = e.dataTransfer?.files?.[0];
        if (!file) return;
        if (!file.name.toLowerCase().endsWith('.pdf')) {
            // E8 (2026-07-22 audit): the old handler DESTROYED the dropzone
            // (hid the overlay, showed the empty summary view) to display
            // this one line — the only way back was the unhighlighted "New
            // Paper" button. Keep the dropzone; show the error inline on it.
            let err = document.getElementById('upload-drop-error');
            if (!err) {
                err = document.createElement('div');
                err.id = 'upload-drop-error';
                err.className = 'upload-drop-error';
                zone.appendChild(err);
            }
            const dropMsg = 'Only .pdf files can be uploaded — drop a PDF to continue.';
            err.textContent = dropMsg;
            // Static assertive region, not a role minted in the same tick as
            // the text write (unreliable in WebKit — liveRegions ratchet class).
            announceError(dropMsg);
            return;
        }
        input.files = e.dataTransfer.files;
        uploadPDF();
    });
}

/**
 * Upload the chosen PDF for server-side text extraction. Uses a raw fetch with
 * FormData (NOT secureFetch — multipart must not carry secureFetch's JSON
 * Content-Type), still sending the X-Requested-With header. On success caches the
 * returned upload_id/filename and enables the Summarise button.
 */
export async function uploadPDF() {
    const fileInput = document.getElementById('pdf-upload');
    const file = fileInput.files[0];
    if (!file || _summaryAbortController || _uploadAbortController) return;

    const content = document.getElementById('document-summary-content');
    content.innerHTML = '<div class="upload-spinner"></div><div>Extracting content…</div>';
    document.getElementById('upload-overlay').style.display = 'none';
    document.getElementById('summary-view').style.display = 'flex';

    const formData = new FormData();
    formData.append('file', file);

    // C5/#47 (2026-07-13): make the upload abortable so resetUpload() during an
    // in-flight upload can cancel it — otherwise a late resolve re-enables
    // Summarise and caches an upload_id while the tab shows the empty overlay.
    if (_uploadAbortController) { try { _uploadAbortController.abort(); } catch (_) {} }
    const uploadCtrl = new AbortController();
    _uploadAbortController = uploadCtrl;
    taskBegin('read-upload', 'Read — extracting PDF');
    document.getElementById('summarise-btn').disabled = true;

    try {
        const resp = await fetch('/api/upload', {
            method: 'POST',
            headers: { 'X-Requested-With': 'ChatEKLD' },
            body: formData,
            signal: uploadCtrl.signal
        });
        const data = await resp.json();
        // Superseded by a reset or a newer upload while we awaited — drop the
        // result so we don't mutate a torn-down / newer view.
        if (uploadCtrl.signal.aborted || _uploadAbortController !== uploadCtrl) return;
        if (data.upload_id) {
            // Item 3.8: free the superseded server-side upload. Re-uploading
            // used to just overwrite the id — the old extraction lingered on
            // disk until app reset. Fire-and-forget AFTER the new upload
            // succeeded (never delete the current doc on a failed replace).
            const prevId = _currentUploadId;
            if (prevId && prevId !== data.upload_id) {
                secureFetch('/api/upload/' + encodeURIComponent(prevId), { method: 'DELETE' })
                    .catch(() => { /* best-effort cleanup */ });
            }
            _summaryText = '';
            _lastSummaryMeta = null;
            _lastSummaryUsage = null;
            taskUpdate('read-upload', {message: 'Paper processed. Ready to summarise.'});
            taskEnd('read-upload', 'completed');
            _currentUploadId = data.upload_id;
            _currentFilename = data.filename || file.name || 'summary';
            content.textContent = 'Paper processed. Ready to summarise.';
            document.getElementById('summarise-btn').disabled = false;
            document.getElementById('export-summary-btn').disabled = true;
            document.getElementById('export-summary-md-btn').disabled = true;
        } else {
            throw new Error(data.error || 'No upload identifier returned.');
        }
    } catch (e) {
        if (_uploadAbortController !== uploadCtrl) return;
        taskUpdate('read-upload', {message: e.name === 'AbortError' ? 'Upload cancelled.' : e.message});
        taskEnd('read-upload', e.name === 'AbortError' ? 'cancelled' : 'failed');
        if (e.name !== 'AbortError') content.textContent = 'Upload error: ' + e.message;
    } finally {
        if (_uploadAbortController === uploadCtrl) _uploadAbortController = null;
    }
}

/**
 * Stream a summary of the uploaded PDF. Sends the content/instruction inputs plus
 * the generation knobs (read by id), then consumes the SSE `{token}`/`{error}`
 * stream into the summary pane. Repeated launches are ignored while busy.
 * Enables the export buttons only once non-empty text has actually arrived.
 */
export async function summarisePDF() {
    if (!_currentUploadId || _summaryAbortController || _uploadAbortController) return;
    
    const content = document.getElementById('document-summary-content');
    clearTaskError(document.getElementById('doc-error-boundary'));
    // Show a waiting indicator until the first token arrives — a large doc or
    // a cold model can take many seconds, and a blank pane reads as "stuck".
    content.innerHTML = '<span class="typing-indicator"><span></span></span>';
    // E1 (2026-07-22): stage line + persistent notices for the {info} frames
    // (map-reduce progress, coverage/clip warnings) — see the markup comment.
    const stageEl = document.getElementById('summarise-stage');
    const noticesEl = document.getElementById('summarise-notices');
    if (stageEl) { stageEl.hidden = true; stageEl.textContent = ''; }
    if (noticesEl) { noticesEl.hidden = true; noticesEl.textContent = ''; }
    const btn = document.getElementById('summarise-btn');
    const exportBtn = document.getElementById('export-summary-btn');
    const exportMdBtn = document.getElementById('export-summary-md-btn');
    btn.disabled = true;
    btn.textContent = 'Generating...';
    // Track 6b: streamed summary is silent to AT — announce start/ready.
    announceStatus('Generating summary…');
    exportBtn.disabled = true;
    exportMdBtn.disabled = true;

    if (_summaryAbortController) _summaryAbortController.abort();
    // Item 3.3: keep a LOCAL handle. The old code's finally nulled the
    // module field unconditionally — so when run B superseded run A, A's
    // late finally nulled B's controller, and a third run could no longer
    // abort B (two streams writing the same pane). The finally below only
    // clears the field if it still owns it (identity check).
    _summaryText = '';
    const renderer = makeAnswerRenderer(content);
    _summaryRenderer = renderer;
    const controller = new AbortController();
    _summaryAbortController = controller;
    const ownsView = () => _summaryAbortController === controller && !controller.signal.aborted;
    taskBegin('read-summary', 'Read — generating summary');

    // C5/#18 (2026-07-13): a dedicated Stop button so a long (map-reduce)
    // summary can be cancelled WITHOUT discarding the uploaded paper — the only
    // prior abort path was resetUpload(), which also clears the upload.
    let stopBtn = document.getElementById('summarise-stop-btn');
    if (!stopBtn) {
        stopBtn = document.createElement('button');
        stopBtn.id = 'summarise-stop-btn';
        stopBtn.type = 'button';
        stopBtn.className = 'btn btn-outline btn-sm';
        stopBtn.textContent = 'Stop';
        stopBtn.setAttribute('aria-label', 'Stop generating the summary');
        btn.parentElement.insertBefore(stopBtn, btn.nextSibling);
    }
    stopBtn.textContent = 'Stop';
    stopBtn.style.display = '';
    stopBtn.disabled = false;
    stopBtn.onclick = () => { controller.abort(); stopBtn.disabled = true; stopBtn.textContent = 'Stopping…'; };

    const payload = {
        upload_id: _currentUploadId,
        model: document.getElementById('model-select').value,
        provider: getActiveProvider(),
        preset: document.getElementById('preset-select').value,
        report_type_id: document.getElementById('report-type-select').value,
        audience: document.getElementById('audience-select').value,
        language: document.getElementById('language-select').value,
        focus_question: document.getElementById('doc-focus-question').value,
        temperature: parseFloat(document.getElementById('doc-temp').value),
        max_tokens: parsePositiveInt('doc-predict', 4096),
        num_ctx: parsePositiveInt('doc-ctx', 32768),
        top_p: parseFloat(document.getElementById('doc-top-p').value),
        repeat_penalty: parseFloat(document.getElementById('doc-repeat-penalty').value),
    };
    const systemPrompt = document.getElementById('doc-system-prompt').value.trim();
    if (systemPrompt) payload.system_prompt = systemPrompt;

    // Hoisted out of the try so the catch can tell "partial streamed text"
    // from "nothing arrived" — the vault/plainchat interrupted-partial pattern.
    let receivedText = false;
    try {
        _lastSummaryMeta = null;
        _lastSummaryUsage = null;
        await consumeSSE('/api/summarise', {
            method: 'POST',
            body: JSON.stringify(payload),
            signal: controller.signal
        }, {
            onInfo: (msg) => {
                // E1: the server engineered this path to be "slower, never
                // silent" — render it. Transient progress lines end with an
                // ellipsis ("Summarising part 3/12…") and replace each other
                // in the stage line; anything else is a NOTICE (coverage cap,
                // bibliography strip, fallback) that must persist beside the
                // finished summary — dropping it made a partial summary read
                // as complete.
                if (!ownsView() || !msg) return;
                taskUpdate('read-summary', {message: String(msg)});
                const transient = /(…|\.\.\.)\s*$/.test(String(msg).trim());
                if (transient) {
                    if (stageEl) { stageEl.hidden = false; stageEl.textContent = msg; }
                } else if (noticesEl) {
                    noticesEl.hidden = false;
                    const line = document.createElement('div');
                    line.textContent = msg;
                    noticesEl.appendChild(line);
                    announceStatus(msg);
                }
            },
            onToken: (token) => {
                if (!ownsView()) return;
                if (!receivedText) content.textContent = '';
                _summaryText += token;
                renderer.update(_summaryText);
                receivedText = _summaryText.trim() !== '';
            },
            onOther: (frame) => {
                // Export provenance (2026-07-22): the server resolves the
                // generation params body → paper_* config, so this frame is
                // the authoritative record of what ran.
                if (!ownsView()) return;
                if (frame && frame.meta) _lastSummaryMeta = frame.meta;
                if (frame && frame.usage) _lastSummaryUsage = frame.usage;
            },
            onError: (err) => { throw new Error(err); }
        });

        if (!ownsView()) return;
        renderer.flush();
        taskUpdate('read-summary', {message: receivedText ? 'Summary ready.' : 'No summary was generated.'});
        taskEnd('read-summary', receivedText ? 'completed' : 'failed');
        if (!receivedText) content.textContent = 'No summary was generated. Please try again.';
        const canExport = receivedText;
        exportBtn.disabled = !canExport;
        exportMdBtn.disabled = !canExport;
        announceStatus(receivedText ? 'Summary ready.' : 'No summary was generated.');
    } catch (e) {
        if (_summaryAbortController !== controller) return;
        taskUpdate('read-summary', {message: e.name === 'AbortError' ? 'Summary cancelled; any partial text is kept.' : e.message});
        taskEnd('read-summary', e.name === 'AbortError' ? 'cancelled' : (receivedText ? 'partial' : 'failed'));
        // 2026-08-12 adjudicated #2: both failure branches used to DESTROY
        // already-streamed text — Stop overwrote the pane, an error blanked
        // it — erasing a 90%-streamed map-reduce summary and re-spending the
        // whole generation on retry. Preserve any partial (the vault /
        // plainchat "⚠ interrupted" pattern); the note goes into the
        // persistent notices strip so exports of the pane stay clean.
        renderer.flush();
        const partial = receivedText && _summaryText.trim() !== '';
        const note = (msg) => {
            if (!noticesEl) return;
            noticesEl.hidden = false;
            const line = document.createElement('div');
            line.textContent = msg;
            noticesEl.appendChild(line);
        };
        if (e.name === 'AbortError') {
            // C5/#18: user Stop — keep the upload, show a non-error notice.
            if (partial) {
                note('⚠ Summary stopped — the text above is partial. The paper '
                    + 'is still loaded; click Summarise to regenerate.');
                announceStatus('Summary stopped; the partial text is kept.');
            } else {
                content.textContent = 'Summary cancelled. The paper is still loaded — click Summarise to try again.';
                announceStatus('Summary cancelled.');
            }
        } else {
            if (partial) {
                note('⚠ Summary interrupted — the text above is partial.');
            } else {
                content.textContent = '';
            }
            const el = document.getElementById('doc-error-boundary');
            showTaskError(el, 'Error: ' + e.message, [
                { label: 'Retry', primary: true, onClick: () => { clearTaskError(el); summarisePDF(); } },
                { label: 'Dismiss', onClick: () => clearTaskError(el) },
            ]);
        }
        if (partial) {
            // The kept text is the user's — exportable like a finished one.
            exportBtn.disabled = false;
            exportMdBtn.disabled = false;
        }
    } finally {
        renderer.cancel();
        if (_summaryAbortController !== controller) return;
        _summaryRenderer = null;
        btn.disabled = false;
        btn.textContent = 'Summarise';
        // The stage line is progress-only — clear it; NOTICES stay visible
        // beside the summary (that's their whole point).
        if (stageEl) { stageEl.hidden = true; stageEl.textContent = ''; }
        if (stopBtn) { stopBtn.style.display = 'none'; stopBtn.textContent = 'Stop'; }
        if (_summaryAbortController === controller) _summaryAbortController = null;
    }
}

/** Copy the generated summary text to the clipboard. No-op when empty. */
export function copySummary() {
    const btn = document.getElementById('summary-copy-btn');
    const text = _summaryText.trim();
    if (!text || !btn) return;
    copyToClipboard(text, btn);
}

function parsePositiveInt(id, fallback) {
    const value = parseInt(document.getElementById(id).value, 10);
    return Number.isFinite(value) && value > 0 ? value : fallback;
}

/**
 * Export the current summary text to the user's Downloads directory via
 * /api/export-summary. `format` is "txt" or "md"; the matching button shows
 * transient "Exporting…" / "Exported" feedback.
 */
export async function exportSummary(format = 'txt') {
    const text = _summaryText.trim();
    if (!text) return;

    const btn = document.getElementById(format === 'md' ? 'export-summary-md-btn' : 'export-summary-btn');
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Exporting...';

    // Provenance footer (2026-07-22): stamp the generating model + real usage
    // beneath the summary body. Plain lines so the txt export stays readable;
    // the server still injects its own "# Summary — …" heading for md.
    let exportText = text;
    if (_lastSummaryMeta) {
        const m = _lastSummaryMeta;
        const bits = [`Generated by CHERQAiN — ${[m.provider, m.model].filter(Boolean).join(' / ')}`];
        if (typeof m.temperature === 'number') bits.push(`temperature ${m.temperature}`);
        const u = _lastSummaryUsage;
        if (u && (u.input_tokens || u.output_tokens)) {
            bits.push(`${u.input_tokens || 0} in / ${u.output_tokens || 0} out tokens`);
            if (u.cost_usd > 0) bits.push(`$${Number(u.cost_usd).toFixed(4)}`);
        }
        bits.push(new Date().toISOString().slice(0, 10));
        exportText = `${text}\n\n---\n${bits.join(' · ')}\n`;
    }

    try {
        const resp = await secureFetch('/api/export-summary', {
            method: 'POST',
            body: JSON.stringify({ filename: _currentFilename, content: exportText, format })
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.error || 'Export failed.');
        btn.textContent = 'Exported';
        setTimeout(() => { btn.textContent = original; }, 1600);
    } catch (e) {
        // Item 3.8: never write the error INTO the summary pane — that
        // destroyed the generated text the user was trying to export. The
        // pane keeps the summary; the failure goes to the error boundary.
        const el = document.getElementById('doc-error-boundary');
        showTaskError(el, 'Export error: ' + e.message, [
            { label: 'Dismiss', onClick: () => clearTaskError(el) },
        ]);
        btn.textContent = original;
    } finally {
        btn.disabled = false;
    }
}

/**
 * Reset the Single Paper tab to its empty state and delete the server-side
 * upload row. Aborts any in-flight summarisation BEFORE the DELETE so the server
 * and DOM stop touching the row the request still references.
 */
export async function resetUpload() {
    _summaryRenderer?.cancel();
    _summaryRenderer = null;
    _summaryText = '';
    const id = _currentUploadId;
    // Abort any in-flight summarisation so server and DOM stop updating
    // before we delete the upload row that the request is referencing.
    if (_summaryAbortController) {
        try { _summaryAbortController.abort(); } catch (_) {}
        _summaryAbortController = null;
        taskUpdate('read-summary', {message: 'Read reset; generation cancelled.'});
        taskEnd('read-summary', 'cancelled');
    }
    // C5/#47: also abort an in-flight UPLOAD so its late resolve can't re-enable
    // Summarise / cache an upload_id after we've reset to the empty overlay.
    if (_uploadAbortController) {
        try { _uploadAbortController.abort(); } catch (_) {}
        _uploadAbortController = null;
        taskUpdate('read-upload', {message: 'Read reset; upload cancelled.'});
        taskEnd('read-upload', 'cancelled');
    }
    _currentUploadId = null;
    _currentFilename = 'summary';
    _lastSummaryMeta = null;
    _lastSummaryUsage = null;
    document.getElementById('upload-overlay').style.display = 'flex';
    document.getElementById('summary-view').style.display = 'none';
    document.getElementById('pdf-upload').value = '';
    const summaryBtn = document.getElementById('summarise-btn');
    summaryBtn.disabled = true;
    summaryBtn.textContent = 'Summarise';
    // Reset owns the empty pane now: the invalidated stream's finally must
    // stay inert, so it cannot clear these controls for us.
    const stopBtn = document.getElementById('summarise-stop-btn');
    if (stopBtn) { stopBtn.style.display = 'none'; stopBtn.textContent = 'Stop'; }
    const stageEl = document.getElementById('summarise-stage');
    if (stageEl) { stageEl.hidden = true; stageEl.textContent = ''; }
    announceStatus('Ready for a new paper.');
    document.getElementById('export-summary-btn').disabled = true;
    document.getElementById('export-summary-md-btn').disabled = true;
    if (id) {
        try {
            await secureFetch(`/api/upload/${encodeURIComponent(id)}`, { method: 'DELETE' });
        } catch (e) {
            console.error('Upload cleanup failed:', e);
        }
    }
}
