/**
 * Single Paper tab: upload a PDF, stream a tunable summary, and export it.
 *
 * Per the JS module hierarchy this imports only ui.js + api.js (and config.js for
 * getActiveProvider — config is itself a ui.js/api.js-only module). The five
 * generation knobs (temperature/max_tokens/num_ctx/top_p/repeat_penalty) are read
 * by id from the controls settings.js owns (paper_* persistence) and sent
 * per-request; the server re-resolves them body → persisted paper_* → default.
 */
import { secureFetch, readSSE } from './api.js';
import { showTaskError, clearTaskError } from './ui.js';
import { getActiveProvider } from './config.js';

let _currentUploadId = null;
let _currentFilename = 'summary';
let _summaryAbortController = null;

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
            const content = document.getElementById('document-summary-content');
            if (content) {
                document.getElementById('upload-overlay').style.display = 'none';
                document.getElementById('summary-view').style.display = 'flex';
                content.textContent = 'Only .pdf files can be uploaded.';
            }
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
    if (!file) return;

    const content = document.getElementById('document-summary-content');
    content.innerHTML = '<div class="upload-spinner"></div><div>Extracting content…</div>';
    document.getElementById('upload-overlay').style.display = 'none';
    document.getElementById('summary-view').style.display = 'flex';

    const formData = new FormData();
    formData.append('file', file);

    try {
        const resp = await fetch('/api/upload', {
            method: 'POST',
            headers: { 'X-Requested-With': 'ChatEKLD' },
            body: formData
        });
        const data = await resp.json();
        if (data.upload_id) {
            _currentUploadId = data.upload_id;
            _currentFilename = data.filename || file.name || 'summary';
            content.textContent = 'Paper processed. Ready to summarise.';
            document.getElementById('summarise-btn').disabled = false;
            document.getElementById('export-summary-btn').disabled = true;
            document.getElementById('export-summary-md-btn').disabled = true;
        } else {
            content.textContent = 'Upload failed: ' + data.error;
        }
    } catch (e) {
        content.textContent = 'Upload error: ' + e.message;
    }
}

/**
 * Stream a summary of the uploaded PDF. Sends the content/instruction inputs plus
 * the generation knobs (read by id), then consumes the SSE `{token}`/`{error}`
 * stream into the summary pane. An in-flight summarisation is aborted first
 * (single AbortController) so re-clicking Summarise can't interleave two streams.
 * Enables the export buttons only once non-empty text has actually arrived.
 */
export async function summarisePDF() {
    if (!_currentUploadId) return;
    
    const content = document.getElementById('document-summary-content');
    clearTaskError(document.getElementById('doc-error-boundary'));
    // Show a waiting indicator until the first token arrives — a large doc or
    // a cold model can take many seconds, and a blank pane reads as "stuck".
    content.innerHTML = '<span class="typing-indicator"><span></span></span>';
    const btn = document.getElementById('summarise-btn');
    const exportBtn = document.getElementById('export-summary-btn');
    const exportMdBtn = document.getElementById('export-summary-md-btn');
    btn.disabled = true;
    btn.textContent = 'Generating...';
    exportBtn.disabled = true;
    exportMdBtn.disabled = true;

    if (_summaryAbortController) _summaryAbortController.abort();
    _summaryAbortController = new AbortController();

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

    try {
        const resp = await secureFetch('/api/summarise', {
            method: 'POST',
            body: JSON.stringify(payload),
            signal: _summaryAbortController.signal
        });
        if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            throw new Error(data.error || 'Summary request failed.');
        }

        let receivedText = false;

        for await (const d of readSSE(resp)) {
            if (d.error) throw new Error(d.error);
            if (d.token) {
                if (!receivedText) content.textContent = '';  // clear the waiting indicator
                content.textContent += d.token;
                receivedText = true;
            }
        }
        if (!receivedText) content.textContent = 'No summary was generated. Please try again.';
        const canExport = !(!receivedText || !content.textContent.trim());
        exportBtn.disabled = !canExport;
        exportMdBtn.disabled = !canExport;
    } catch (e) {
        if (e.name !== 'AbortError') {
            content.textContent = '';
            const el = document.getElementById('doc-error-boundary');
            showTaskError(el, 'Error: ' + e.message, [
                { label: 'Retry', primary: true, onClick: () => { clearTaskError(el); summarisePDF(); } },
                { label: 'Dismiss', onClick: () => clearTaskError(el) },
            ]);
        }
    } finally {
        btn.disabled = false;
        btn.textContent = 'Summarise';
        _summaryAbortController = null;
    }
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
    const content = document.getElementById('document-summary-content');
    const text = content.textContent.trim();
    if (!text) return;

    const btn = document.getElementById(format === 'md' ? 'export-summary-md-btn' : 'export-summary-btn');
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Exporting...';

    try {
        const resp = await secureFetch('/api/export-summary', {
            method: 'POST',
            body: JSON.stringify({ filename: _currentFilename, content: text, format })
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.error || 'Export failed.');
        btn.textContent = 'Exported';
        setTimeout(() => { btn.textContent = original; }, 1600);
    } catch (e) {
        content.textContent = 'Export error: ' + e.message;
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
    const id = _currentUploadId;
    // Abort any in-flight summarisation so server and DOM stop updating
    // before we delete the upload row that the request is referencing.
    if (_summaryAbortController) {
        try { _summaryAbortController.abort(); } catch (_) {}
        _summaryAbortController = null;
    }
    _currentUploadId = null;
    _currentFilename = 'summary';
    document.getElementById('upload-overlay').style.display = 'flex';
    document.getElementById('summary-view').style.display = 'none';
    document.getElementById('pdf-upload').value = '';
    document.getElementById('summarise-btn').disabled = true;
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
