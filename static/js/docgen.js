/**
 * Report / Article Generator tab (docgen). The prose sibling of deck.js: load a
 * user's LaTeX article template, generate a vault-grounded article section by
 * section over SSE (with cancel + retry), write a Markdown note into the vault,
 * augment an existing article, and preview/compile-fix the result.
 *
 * Imports only ui.js + api.js (JS Module Hierarchy).
 *
 * NAMING NOTE: the /api/docgen bodies and frames deliberately reuse the deck
 * route's field names (`deck_path`, `deck_sha256`, the `augment` frame shape)
 * even though the payload is an article — the two panels share the server
 * helpers, JS patterns and tests, and renaming the wire fields would be a
 * breaking API change with no functional gain. Do not "fix" the names.
 */
import { secureFetch, consumeSSE, logError, safeJson } from './api.js';
import { setStatusA11y, taskBegin, taskEnd, taskNote, copyToClipboard, confirmInline, showTaskError, showTab } from './ui.js';

const $ = (id) => document.getElementById(id);

let _generating = false;
let _compileFixing = false;
let _previewing = false;
let _compileAvailable = false;
let _previewObjectUrl = null;

/**
 * Release the current preview's blob URL.
 *
 * PERF (2026-09-02): a preview blob no longer outlives the iframe that shows it.
 *   Before:    the previous object URL was revoked only at the moment the NEXT
 *              one landed. Every path that clears the host WITHOUT producing a
 *              new blob — a compile that fails, a Cancel, a PDF fetch that
 *              errors — removed the <iframe> referencing it and left a
 *              multi-megabyte blob alive with nothing pointing at it, for the
 *              rest of the process. Repeat a failing compile and you leak one
 *              PDF per attempt.
 *   Change:    one named teardown, called wherever the host is cleared and from
 *              Cancel.
 *   Safe because:
 *              it only ever runs where the iframe has just been (or is about to
 *              be) removed by _clear(host); a live preview's URL is never
 *              revoked out from under it.
 *   Preserves: the bytes still arrive via secureFetch and render from a blob:
 *              URL — an /api iframe src is 403'd by the origin guard
 *              (tests/js/deckPreview.test.js's source ratchet).
 *   NOT done deliberately: a tab switch hides the panel but keeps the iframe in
 *              the DOM, so that blob backs something the user can come back to.
 *              Revoking there would be a UX regression, not a leak fix. Recorded
 *              in CLAUDE.md Known Issues.
 *   Pinned by: tests/js/previewBlobTeardown.test.js
 */
function _previewTeardown() {
    if (_previewObjectUrl) {
        URL.revokeObjectURL(_previewObjectUrl);
        _previewObjectUrl = null;
    }
}

// One AbortController per in-flight SSE op: Cancel aborts the fetch, the
// server sees the consumer disconnect, and the worker's cancel event stops
// generation at the next gate (all writes are cancel-gated server-side).
let _abortCtrl = null;

// Item 3.8 parity with deck.js: ONE busy predicate for all doc operations —
// the server serialises them behind _DOCGEN_OP_LOCK (409), so the client
// must too or the loser burns a round-trip on a confusing 409 (compile-fix
// and preview previously didn't join the set — audit #16).
function _docOpBusy() {
    return _generating || _compileFixing || _previewing;
}

/**
 * Render a one-shot Cancel button above the given activity log for the
 * current SSE op. Returns the button; the caller removes it in its finally.
 */
function _mkCancelButton(logEl) {
    if (!logEl || !logEl.parentElement) return null;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-outline btn-sm';
    btn.textContent = 'Cancel';
    btn.setAttribute('aria-label', 'Cancel the running generation');
    btn.addEventListener('click', () => {
        if (_abortCtrl) _abortCtrl.abort();
        // The abort leaves the host cleared, so the previous preview's
        // blob has nothing referencing it (2026-09-02).
        _previewTeardown();
        btn.disabled = true;
        btn.textContent = 'Cancelling…';
    });
    logEl.parentElement.insertBefore(btn, logEl);
    return btn;
}

/** True when the current op's failure is actually the user's own Cancel. */
function _wasCancelled() {
    return !!(_abortCtrl && _abortCtrl.signal.aborted);
}

/**
 * Recoverable error boundary (audit #17): a multi-minute generation that
 * fails must offer one-click Retry, not just a red status line. Renders a
 * task-error box into *root* whose Retry re-invokes *retryFn* (for vault
 * writes pass the confirm-ceremony entry point, never the raw runner).
 */
function _showRetryBoundary(root, message, retryFn) {
    if (!root) return;
    const box = document.createElement('div');
    box.className = 'task-error-boundary';
    root.appendChild(box);
    showTaskError(box, message, [
        { label: 'Retry', primary: true, onClick: () => { box.remove(); retryFn(); } },
    ]);
}

/** One-shot init: probe whether a LaTeX toolchain (latexmk) is installed. */
export async function initDocgen() {
    try {
        const r = await secureFetch('/api/docgen/compile-available');
        const d = await safeJson(r);
        _compileAvailable = !!(d && d.available);
    } catch (_e) { _compileAvailable = false; }
}

function _status(msg, isError = false) {
    taskNote('docgen-', msg, isError ? 'failed' : null);
    const el = $('docgen-status');
    if (!el) return;
    el.style.display = msg ? 'block' : 'none';
    setStatusA11y(el, msg, isError);
}

function _clear(el) { while (el && el.firstChild) el.removeChild(el.firstChild); }

function _addActivity(text, muted = false) {
    taskNote('docgen-', text);
    const log = $('docgen-activity');
    if (!log) return;
    const line = document.createElement('div');
    line.className = muted ? 'deck-activity-line muted' : 'deck-activity-line';
    line.textContent = text;
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
}

// --- Template loading -------------------------------------------------------

export async function pickTemplateFile() {
    try {
        const r = await secureFetch('/api/docgen/native-pick-file', { method: 'POST' });
        const d = await safeJson(r);
        if (!r.ok) throw new Error((d?.error || 'Native picker unavailable.') + ' Use the desktop window or enter the path manually.');
        if (d && d.path) {
            $('docgen-template-path').value = d.path;
            await loadTemplate();
        }
    } catch (e) { logError('Template pick failed', e); _status(e.message, true); }
}

export async function pickOutDir() {
    try {
        const r = await secureFetch('/api/docgen/native-pick-folder', { method: 'POST' });
        const d = await safeJson(r);
        if (!r.ok) throw new Error((d?.error || 'Native picker unavailable.') + ' Use the desktop window or enter the path manually.');
        if (d && d.path) $('docgen-out-dir').value = d.path;
    } catch (e) { logError('Output folder pick failed', e); _status(e.message, true); }
}

export async function loadTemplate() {
    const path = ($('docgen-template-path').value || '').trim();
    if (!path) { _status('Enter or browse to a template .tex/.sty first.', true); return; }
    _status('Loading template…');
    try {
        const r = await secureFetch('/api/docgen/load-template', {
            method: 'POST',
            body: JSON.stringify({ path }),
        });
        const d = await safeJson(r);
        if (!r.ok || d.error) { _status(d.error || 'Could not load template.', true); return; }

        $('docgen-template-editor').value = d.tex || '';
        if (d.suite_root && !($('docgen-out-dir').value || '').trim()) {
            $('docgen-out-dir').value = d.suite_root;
        }
        _renderTemplateHint(d);
        _status('Template loaded. Edit the preamble below if you like, then Generate.');
    } catch (e) {
        logError('Load template failed', e);
        _status('Could not load template (see console).', true);
    }
}

function _renderTemplateHint(d) {
    const hint = $('docgen-template-hint');
    if (!hint) return;
    _clear(hint);
    const parts = [];
    if (Array.isArray(d.macros) && d.macros.length) {
        parts.push(`${d.macros.length} custom macro(s) detected`);
    }
    if (typeof d.bib_keys_count === 'number' && d.bib_keys_count > 0) {
        parts.push(`${d.bib_keys_count} bibliography key(s)`);
    }
    if (d.suite_root) parts.push(`suite root: ${d.suite_root}`);
    if (parts.length) { hint.textContent = parts.join(' · '); }
}

// --- Generation -------------------------------------------------------------

export async function generate() {
    if (_docOpBusy()) return;
    const topic = ($('docgen-topic').value || '').trim();
    const templateTex = ($('docgen-template-editor').value || '').trim();
    if (!topic) { _status('Enter a topic first.', true); return; }
    if (!templateTex) { _status('Load (and optionally edit) a template first.', true); return; }

    const payload = {
        topic,
        template_tex: templateTex,
        template_path: ($('docgen-template-path').value || '').trim(),
        instructions: ($('docgen-instructions').value || '').trim(),
        audience: ($('docgen-audience').value || '').trim(),
        doc_name: ($('docgen-name').value || '').trim(),
        out_dir: ($('docgen-out-dir').value || '').trim(),
        doc_title: ($('docgen-title').value || '').trim(),
        doc_subtitle: ($('docgen-subtitle').value || '').trim(),
        citations_enabled: !!($('docgen-citations') && $('docgen-citations').checked),
        overwrite: !!($('docgen-overwrite') && $('docgen-overwrite').checked),
        force_fresh: !!($('docgen-force-fresh') && $('docgen-force-fresh').checked),
    };

    _generating = true;
    let outcome = 'unknown'; // Only an authoritative result can establish completion.
    taskBegin('docgen-generate', 'Generating document');
    _setBusy(true);
    _clear($('docgen-activity'));
    _clear($('docgen-result'));
    _status('Generating — a multi-section article can take a few minutes…');
    _abortCtrl = new AbortController();
    const cancelBtn = _mkCancelButton($('docgen-activity'));

    try {
        await consumeSSE('/api/docgen/generate', {
            method: 'POST',
            body: JSON.stringify(payload),
            signal: _abortCtrl.signal,
        }, {
            onInfo: (info) => _addActivity(info),
            onOther: (evt) => {
                if (evt.iteration) _addActivity(`· iteration ${evt.iteration}`, true);
                else if (evt.tool_call) _addActivity(`· ${evt.tool_call.name}()`, true);
                else if (evt.tool_result) _addActivity(`· tool result${evt.tool_result.is_error ? ' [error]' : ''}`, true);
                else if (evt.doc) {
                    outcome = evt.doc.scaffold_error ? 'partial' : evt.doc.tex_path ? 'completed' : 'unknown';
                    _renderDoc(evt.doc);
                }
            },
            onError: (err) => {
                outcome = _wasCancelled() ? 'cancelled' : 'failed';
                // A user Cancel surfaces through the same path (the aborted
                // fetch rejects inside consumeSSE) — not an error.
                if (_wasCancelled()) { _status('Cancellation requested. Inspect the output folder for published files before retrying. Saved sections can be resumed.'); return; }
                _status(err, true);
                _addActivity('ERROR: ' + err);
                _showRetryBoundary($('docgen-result'), err, generate);
            },
        });
    } catch (e) {
        outcome = _wasCancelled() ? 'cancelled' : 'failed';
        if (_wasCancelled()) { _status('Cancellation requested. Inspect the output folder for published files before retrying. Saved sections can be resumed.'); return; }
        logError('Document generation failed', e);
        _status('Document generation failed (see console).', true);
        _showRetryBoundary($('docgen-result'), 'Document generation failed.', generate);
    } finally {
        if (_wasCancelled() && outcome !== 'completed') outcome = 'unknown';
        if (cancelBtn) cancelBtn.remove();
        _abortCtrl = null;
        _generating = false;
        _setBusy(false);
        taskEnd('docgen-generate', outcome);
    }
}

function _setBusy(busy) {
    // One doc operation at a time (server enforces 409); reflect it in the UI so
    // every doc-op entry button disables together (incl. compile-fix/preview,
    // whose own buttons are created dynamically and guard via _docOpBusy()).
    for (const id of ['docgen-generate-btn', 'docgen-md-generate-btn', 'docgen-aug-preview-btn']) {
        const el = $(id);
        if (el) el.disabled = busy;
    }
}

// --- Markdown note (writes into the vault) ----------------------------------

function _mdStatus(msg, isError = false) {
    taskNote('docgen-md', msg, isError ? 'failed' : null);
    const el = $('docgen-md-status');
    if (!el) return;
    el.style.display = msg ? 'block' : 'none';
    setStatusA11y(el, msg, isError);
}

function _addMdActivity(text, muted = false) {
    const log = $('docgen-md-activity');
    if (!log) return;
    const line = document.createElement('div');
    line.className = muted ? 'deck-activity-line muted' : 'deck-activity-line';
    line.textContent = text;
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
}

/**
 * Generate a long-form Markdown note and write it into the vault. Reuses the
 * Topic / Instructions / Audience / Title inputs from the article card. Writing
 * to the vault is a confirmed action → route through the shared confirm ceremony.
 */
export function generateMarkdown() {
    if (_docOpBusy()) return;
    const topic = ($('docgen-topic').value || '').trim();
    if (!topic) { _mdStatus('Enter a topic first.', true); return; }

    const btn = $('docgen-md-generate-btn');
    const scope = ($('docgen-md-scope').value || '').trim();
    const forceFresh = !!$('docgen-force-fresh')?.checked;
    const message = `Generate this Markdown document and write it into your vault${scope ? ` under “${scope}”` : ''}? ` +
        'It never overwrites an existing note, and you can undo it from Settings → History and Restore.' +
        (forceFresh ? ' This run will start without reusing saved progress.' : '');
    confirmInline(btn, {
        message,
        confirmLabel: 'Generate & write',
        onConfirm: () => _runMarkdown(topic, scope, forceFresh),
    });
}

async function _runMarkdown(topic, scope, forceFresh) {
    // C3/#15 (2026-07-13): re-check the busy gate HERE. generateMarkdown()
    // checks _docOpBusy() before showing the confirm strip, but the strip's
    // "Generate & write" fires later — by then an article generation may have
    // started, and without this guard two client ops would run: the shared
    // _abortCtrl gets overwritten and the first op's finally clears _generating
    // mid-second-op. The server 409s the second stream, but the client must not
    // start it.
    if (_docOpBusy()) { _mdStatus('Another document operation is in progress.', true); return; }
    const payload = {
        topic,
        instructions: ($('docgen-instructions').value || '').trim(),
        audience: ($('docgen-audience').value || '').trim(),
        doc_title: ($('docgen-title').value || '').trim() || topic,
        doc_name: ($('docgen-md-name').value || '').trim(),
        scope_subdir: scope,
        output_kind: document.getElementById('studio-kind')?.value || 'report',
        force_fresh: forceFresh,
        confirm: true,
    };

    _generating = true;
    let outcome = 'unknown'; // Only an authoritative result can establish completion.
    taskBegin('docgen-md', 'Generating note');
    _setBusy(true);
    _clear($('docgen-md-activity'));
    _clear($('docgen-md-result'));
    _mdStatus('Generating — writing into your vault when done…');
    _abortCtrl = new AbortController();
    const cancelBtn = _mkCancelButton($('docgen-md-activity'));

    let publicationUncertain = false;
    try {
        await consumeSSE('/api/docgen/generate-md', {
            method: 'POST',
            body: JSON.stringify(payload),
            signal: _abortCtrl.signal,
        }, {
            onInfo: (info) => _addMdActivity(info),
            onOther: (evt) => {
                if (evt.iteration) _addMdActivity(`· iteration ${evt.iteration}`, true);
                else if (evt.tool_call) _addMdActivity(`· ${evt.tool_call.name}()`, true);
                else if (evt.tool_result) _addMdActivity(`· tool result${evt.tool_result.is_error ? ' [error]' : ''}`, true);
                else if (evt.doc_md) {
                    publicationUncertain = !!evt.doc_md.publication_uncertain;
                    outcome = publicationUncertain ? 'unknown' : evt.doc_md.rel ? 'completed' : 'unknown';
                    _renderMd(evt.doc_md);
                }
            },
            onError: (err) => {
                outcome = _wasCancelled() ? 'cancelled' : 'failed';
                if (_wasCancelled()) { _showMdUncertain(); return; }
                if (publicationUncertain) { outcome = 'unknown'; _showMdUncertain(err); return; }
                _mdStatus(err, true);
                _addMdActivity('ERROR: ' + err);
                // Retry goes through generateMarkdown (the confirm ceremony),
                // never the raw runner — it's a vault write.
                _showRetryBoundary($('docgen-md-result'), err, generateMarkdown);
            },
        });
    } catch (e) {
        outcome = _wasCancelled() ? 'cancelled' : 'failed';
        if (_wasCancelled()) { _showMdUncertain(); return; }
        if (publicationUncertain) { outcome = 'unknown'; _showMdUncertain(e.message); return; }
        logError('Note generation failed', e);
        _mdStatus('Note generation failed (see console).', true);
        _showRetryBoundary($('docgen-md-result'), 'Note generation failed.', generateMarkdown);
    } finally {
        if (_wasCancelled() && outcome !== 'completed') outcome = 'unknown';
        if (cancelBtn) cancelBtn.remove();
        _abortCtrl = null;
        _generating = false;
        _setBusy(false);
        taskEnd('docgen-md', outcome);
    }
}

function _showMdUncertain(message = 'Stop requested — a note may already have been written. Check the result below or Settings → History and Restore before generating again.') {
    _mdStatus(message, true);
    const root = $('docgen-md-result');
    if (!root || root.querySelector('[data-md-recovery]')) return;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-outline';
    button.dataset.mdRecovery = '';
    button.textContent = 'Open History and Restore';
    button.addEventListener('click', () => {
        window.historyOpenRestore?.();
    });
    root.appendChild(button);
}

function _renderMd(doc) {
    const root = $('docgen-md-result');
    _clear(root);
    const ok = document.createElement('div');
    ok.className = 'deck-summary';
    const n = doc.section_count || 0;
    const ph = doc.placeholder_count || 0;
    if (doc.publication_uncertain) {
        ok.textContent = `Publication uncertain — ${doc.rel}${doc.op_id ? ` (operation ${doc.op_id})` : ''}.`;
        root.appendChild(ok);
        _showMdUncertain('Publication uncertain — check Settings → History and Restore and the target note before generating again.');
        return;
    }
    ok.textContent = `Wrote ${doc.rel} — ${n} section(s)${ph ? `, ${ph} placeholder` : ''}.`;
    root.appendChild(ok);
    const hint = document.createElement('div');
    hint.className = 'muted';
    hint.textContent = 'Reversible: undo it from Settings → History and Restore.';
    root.appendChild(hint);
    _mdStatus(`Done — wrote ${doc.rel}.`);
}

// --- Augment an existing article --------------------------------------------

function _augStatus(msg, isError = false) {
    const el = $('docgen-aug-status');
    if (!el) return;
    el.style.display = msg ? 'block' : 'none';
    setStatusA11y(el, msg, isError);
}

function _addAugActivity(text, muted = false) {
    const log = $('docgen-aug-activity');
    if (!log) return;
    const line = document.createElement('div');
    line.className = muted ? 'deck-activity-line muted' : 'deck-activity-line';
    line.textContent = text;
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
}

export async function pickAugmentDoc() {
    try {
        const r = await secureFetch('/api/docgen/native-pick-file', { method: 'POST' });
        const d = await safeJson(r);
        if (!r.ok) throw new Error((d?.error || 'Native picker unavailable.') + ' Use the desktop window or enter the path manually.');
        if (d && d.path) { $('docgen-aug-path').value = d.path; await loadDocSections(); }
    } catch (e) { logError('Article pick failed', e); _augStatus(e.message, true); }
}

export async function loadDocSections() {
    const path = ($('docgen-aug-path').value || '').trim();
    if (!path) { _augStatus('Enter or browse to an article .tex first.', true); return; }
    _augStatus('Loading article sections…');
    try {
        const r = await secureFetch('/api/docgen/doc-sections', {
            method: 'POST', body: JSON.stringify({ deck_path: path }),
        });
        const d = await safeJson(r);
        if (!r.ok || d.error) { _augStatus(d.error || 'Could not read the article.', true); return; }
        const sel = $('docgen-aug-section');
        _clear(sel);
        (d.sections || []).forEach((s) => {
            const opt = document.createElement('option');
            opt.value = String(s.index);
            opt.textContent = `${s.index + 1}. ${s.title}`;
            sel.appendChild(opt);
        });
        $('docgen-aug-hint').textContent = `${(d.sections || []).length} section(s).`;
        augScopeChanged();
        _augStatus('Loaded. Choose an operation/scope and preview a change.');
    } catch (e) {
        logError('Load article sections failed', e);
        _augStatus('Could not load the article (see console).', true);
    }
}

export function augScopeChanged() {
    const scope = $('docgen-aug-scope').value;
    const sel = $('docgen-aug-section');
    if (sel) sel.disabled = scope !== 'section' || sel.options.length === 0;
}

export async function augmentPreview(piecewise) {
    if (_docOpBusy()) return;
    const path = ($('docgen-aug-path').value || '').trim();
    const instruction = ($('docgen-aug-instruction').value || '').trim();
    if (!path) { _augStatus('Load an article .tex first.', true); return; }
    if (!instruction) { _augStatus('Describe what to change first.', true); return; }
    const scope = $('docgen-aug-scope').value;
    const payload = {
        deck_path: path,
        instruction,
        operation: $('docgen-aug-op').value,
        scope,
        section_index: scope === 'section' ? parseInt($('docgen-aug-section').value, 10) || 0 : 0,
        audience: ($('docgen-audience').value || '').trim(),
        topic: ($('docgen-topic').value || '').trim(),
    };
    // Batch E (2026-07-22): offer-on-refusal piecewise re-run (mirrors deck.js).
    if (piecewise === true) payload.piecewise = true;

    _generating = true;
    let outcome = 'unknown'; // Only an authoritative result can establish completion.
    taskBegin('docgen-augment', 'Augmenting article');
    _setBusy(true);
    _clear($('docgen-aug-activity'));
    _clear($('docgen-aug-result'));
    _augStatus('Previewing the change — nothing is written yet…');
    _abortCtrl = new AbortController();
    const cancelBtn = _mkCancelButton($('docgen-aug-activity'));

    try {
        await consumeSSE('/api/docgen/augment', {
            method: 'POST', body: JSON.stringify(payload),
            signal: _abortCtrl.signal,
        }, {
            onInfo: (info) => _addAugActivity(info),
            onOther: (evt) => {
                if (evt.iteration) _addAugActivity(`· iteration ${evt.iteration}`, true);
                else if (evt.tool_call) _addAugActivity(`· ${evt.tool_call.name}()`, true);
                else if (evt.augment) { outcome = evt.augment.rejected_reason ? 'failed' : 'completed'; _renderAugment(evt.augment); }
            },
            onError: (err, frame) => {
                outcome = _wasCancelled() ? 'cancelled' : 'failed';
                if (_wasCancelled()) { _augStatus('Cancelled — nothing was staged or written.'); return; }
                _augStatus(err, true);
                _addAugActivity('ERROR: ' + err);
                // Keyed on the structured `code` (2026-07-23 — a copy edit to
                // the server prose used to silently kill the offer); regex
                // kept as a fallback. Whole scope only.
                const sizeCap = (frame && frame.code === 'size_cap')
                    || /too large to revise in one pass/i.test(err);
                if (sizeCap && scope === 'whole') {
                    _offerPiecewiseAugment();
                }
            },
        });
    } catch (e) {
        outcome = _wasCancelled() ? 'cancelled' : 'failed';
        if (_wasCancelled()) { _augStatus('Cancelled — nothing was staged or written.'); return; }
        logError('Article augment failed', e);
        _augStatus('Augment failed (see console).', true);
    } finally {
        if (_wasCancelled() && outcome !== 'completed') outcome = 'cancelled';
        if (cancelBtn) cancelBtn.remove();
        _abortCtrl = null;
        _generating = false;
        _setBusy(false);
        taskEnd('docgen-augment', outcome);
    }
}

/** Render the 'Revise section by section' offer after a size-cap refusal. */
function _offerPiecewiseAugment() {
    const root = $('docgen-aug-result');
    if (!root || root.querySelector('.docgen-piecewise-offer')) return;
    // Button + caveat share one wrapper (carrying the dedupe class) so the
    // click removes them together — a click on the bare button used to leave
    // an orphaned note behind.
    const wrap = document.createElement('div');
    wrap.className = 'docgen-piecewise-offer';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-outline';
    btn.textContent = 'Revise section by section';
    btn.title = 'Runs one vault-grounded pass per \\section (over-cap sections are left unchanged), then previews one combined proposal.';
    btn.addEventListener('click', () => { wrap.remove(); augmentPreview(true); });
    wrap.appendChild(btn);
    // Visible caveat (title tooltips are invisible to keyboard/touch/SR users).
    const note = document.createElement('div');
    note.className = 'muted';
    note.textContent = 'Over-cap or failed sections are skipped and left unchanged.';
    wrap.appendChild(note);
    root.appendChild(wrap);
}

function _renderAugment(aug) {
    const root = $('docgen-aug-result');
    _clear(root);

    const before = aug.counts ? aug.counts.sections_before : 0;
    const after = aug.counts ? aug.counts.sections_after : 0;
    if (aug.changed && after < before) {
        const warn = document.createElement('div');
        warn.className = 'warning-banner';
        warn.style.display = 'block';
        warn.textContent = `Heads up: the section count drops ${before} → ${after}. Review before applying.`;
        root.appendChild(warn);
    }

    if (!aug.changed) {
        // A genuinely rejected proposal is an error; "the model produced no
        // change" is a benign outcome — deck.js renders it as muted guidance
        // and styling it red here misread it as a failure (2026-08-18).
        if (aug.rejected_reason) {
            _augStatus(aug.rejected_reason, true);
        } else {
            _augStatus('No change was produced — the model returned content '
                + 'identical to the article. Try rephrasing the instruction.',
                false);
        }
        return;
    }

    (aug.warnings || []).forEach((w) => {
        const li = document.createElement('div');
        li.className = 'muted';
        li.textContent = '• ' + w;
        root.appendChild(li);
    });

    const ta = document.createElement('textarea');
    ta.className = 'deck-tex-output';
    ta.rows = 16;
    ta.readOnly = true;
    ta.spellcheck = false;
    ta.value = aug.proposed_tex;
    root.appendChild(ta);

    // Apply overwrites the user's .tex → route through the confirm ceremony.
    const applyBtn = document.createElement('button');
    applyBtn.className = 'btn btn-primary btn-sm';
    applyBtn.style.marginTop = '8px';
    applyBtn.textContent = 'Apply to the article';
    applyBtn.addEventListener('click', () => {
        confirmInline(applyBtn, {
            message: `Overwrite ${aug.deck_path} with this change? A .tex.bak backup is made first.`,
            confirmLabel: 'Apply & overwrite',
            onConfirm: () => _applyAugment(aug, applyBtn),
        });
    });
    root.appendChild(applyBtn);
    _augStatus('Previewed. Review, then Apply to write it.');
}

async function _applyAugment(aug, btn) {
    btn.disabled = true;
    _augStatus('Applying…');
    try {
        const r = await secureFetch('/api/docgen/apply-augment', {
            method: 'POST',
            // base_sha256 = stale-diff token; proposed_sha256 = preview-binding
            // token (409 if a newer preview replaced the staging slot).
            body: JSON.stringify({
                deck_path: aug.deck_path,
                base_sha256: aug.deck_sha256,
                proposed_sha256: aug.proposed_sha256 || '',
                confirm: true,
            }),
        });
        const d = await safeJson(r);
        if (!r.ok || d.error) { _augStatus(d.error || `Apply failed (${r.status})`, true); btn.disabled = false; return; }
        _augStatus(`Applied — wrote ${d.tex_path}. Backup: ${d.backup_path}.`);
        btn.textContent = 'Applied ✓';
    } catch (e) {
        logError('Apply augment failed', e);
        _augStatus('Apply request failed (see console).', true);
        btn.disabled = false;
    }
}

function _renderDoc(doc) {
    const root = $('docgen-result');
    _clear(root);

    if (doc.resumed && doc.reused_sections > 0) {
        const banner = document.createElement('div');
        banner.className = 'muted';
        banner.textContent = `Resumed: ${doc.reused_sections} section(s) reused from a ` +
            'previous interrupted run. Tick “Start fresh” to regenerate from scratch.';
        root.appendChild(banner);
    }

    if (doc.scaffold_error) {
        const err = document.createElement('div');
        err.className = 'warning-banner';
        err.style.display = 'block';
        err.textContent = 'Not written to disk: ' + doc.scaffold_error +
            ' — the generated .tex is still shown below; use Copy to grab it.';
        root.appendChild(err);
        _status('Generated, but not written: ' + doc.scaffold_error, true);
    } else if (doc.tex_path) {
        const ok = document.createElement('div');
        ok.className = 'deck-summary';
        const n = doc.section_count || 0;
        const ph = doc.placeholder_count || 0;
        ok.textContent = `Wrote ${doc.tex_path} — ${n} section(s)${ph ? `, ${ph} placeholder` : ''}.`;
        root.appendChild(ok);
        if (doc.make_hint) {
            const hint = document.createElement('div');
            hint.className = 'muted';
            hint.appendChild(document.createTextNode('Compile it: '));
            const code = document.createElement('code');
            code.textContent = doc.make_hint;
            hint.appendChild(code);
            root.appendChild(hint);
        }
        _status(`Done — ${n} section(s) written.`);
    }

    if (Array.isArray(doc.warnings) && doc.warnings.length) {
        const wrap = document.createElement('div');
        wrap.className = 'deck-warnings';
        const h = document.createElement('div');
        h.className = 'muted';
        h.textContent = 'Warnings — review before compiling:';
        wrap.appendChild(h);
        const ul = document.createElement('ul');
        for (const w of doc.warnings) {
            const li = document.createElement('li');
            li.textContent = w;
            ul.appendChild(li);
        }
        wrap.appendChild(ul);
        root.appendChild(wrap);
    }

    if (doc.tex) {
        const ta = document.createElement('textarea');
        ta.className = 'deck-tex-output';
        ta.rows = 16;
        ta.readOnly = true;
        ta.spellcheck = false;
        ta.value = doc.tex;
        root.appendChild(ta);

        const copyBtn = document.createElement('button');
        copyBtn.className = 'btn btn-outline btn-sm';
        copyBtn.style.marginTop = '8px';
        copyBtn.textContent = 'Copy .tex';
        copyBtn.addEventListener('click', () => copyToClipboard(doc.tex, copyBtn));
        root.appendChild(copyBtn);

        // Preview PDF + Compile & Auto-Fix — only when a LaTeX toolchain is
        // present and the doc was scaffolded to disk.
        if (_compileAvailable && doc.tex_path) {
            // Mutable sha holder: a compile-fix repair rewrites the .tex and the
            // terminal frame carries the new sha — a second action must send THAT,
            // not the generate-time sha (which would 409 as stale).
            const shaRef = {
                get sha() { return doc.tex_sha256; },
                set sha(v) { doc.tex_sha256 = v; },
            };
            const fixBtn = document.createElement('button');
            fixBtn.className = 'btn btn-outline btn-sm';
            fixBtn.style.marginTop = '8px';
            fixBtn.style.marginLeft = '8px';
            fixBtn.textContent = 'Compile & Auto-Fix';
            // E10 (2026-07-22 audit): same ceremony as the deck — this loop
            // rewrites the user's .tex with screened LLM repairs.
            fixBtn.addEventListener('click', () => confirmInline(fixBtn, {
                message: 'Compile and auto-repair may rewrite the article .tex '
                    + '(a .bak backup is kept). Continue?',
                confirmLabel: 'Compile & fix',
                onConfirm: () => runDocCompileFix(doc.tex_path, shaRef, fixBtn),
            }));
            root.appendChild(fixBtn);

            const previewBtn = document.createElement('button');
            previewBtn.className = 'btn btn-outline btn-sm';
            previewBtn.style.marginTop = '8px';
            previewBtn.style.marginLeft = '8px';
            previewBtn.textContent = 'Preview PDF';
            const host = document.createElement('div');
            host.className = 'deck-preview-host';
            previewBtn.addEventListener('click', () => runDocPreview(doc.tex_path, previewBtn, host));
            root.appendChild(previewBtn);
            root.appendChild(host);
        } else if (doc.tex_path) {
            // Say WHY the compile actions are absent (deck.js's explicit
            // "latexmk: not available" precedent, 2026-08-18): silently
            // hiding them is indistinguishable from a bug, and the probe
            // runs once at boot so the state is stable.
            const m = document.createElement('div');
            m.className = 'muted';
            m.textContent = 'LaTeX compiler (latexmk): not available on PATH — '
                + 'Preview PDF and Compile & Auto-Fix are disabled. '
                + 'Compile with `make` instead.';
            root.appendChild(m);
        }
    }
}

/**
 * Compile-and-fix loop over the generated article: compile → (fail) → LLM repair
 * → screen → write → compile again. Writes the .tex (a .tex.bak is made first);
 * each repair is verified by the next compile. Mirrors deck.js::runCompileFix.
 */
export async function runDocCompileFix(docPath, shaRef, btn) {
    // Join the shared busy set (audit #16): compile-fix holds the server's
    // _DOCGEN_OP_LOCK, so Generate/Augment must grey out for its duration —
    // checking _generating alone left them clickable into a 409.
    if (_docOpBusy()) return;
    _compileFixing = true;
    _setBusy(true);
    btn.disabled = true;
    const act = $('docgen-activity');
    if (act) { _clear(act); act.style.display = 'block'; }
    _status('Starting LaTeX compilation & repair loop…');
    _addActivity('Initializing latexmk…');
    let outcome = 'unknown'; // Only an authoritative result can establish completion.
    taskBegin('docgen-compile-fix', 'Compiling article');
    // Client-side abort (P6.2, 2026-07-30) — the article mirror of deck.js.
    // compile-fix is the longest doc operation and had no Cancel; aborting
    // closes the SSE stream, and the server's disconnect path sets the
    // worker's cancel event, TERM->KILLs latexmk's process group on its own
    // timeout, and releases the op lock via _SSEOpGuard once the worker exits.
    _abortCtrl = new AbortController();
    const cancelBtn = _mkCancelButton($('docgen-activity'));
    try {
        await consumeSSE('/api/docgen/compile-fix', {
            method: 'POST',
            body: JSON.stringify({ deck_path: docPath, base_sha256: shaRef.sha, confirm: true }),
            signal: _abortCtrl.signal,
        }, {
            onInfo: (info) => _addActivity(info),
            onOther: (evt) => {
                if (!evt.compile) return;
                const c = evt.compile;
                outcome = c.success ? 'completed' : 'failed';
                if (c.tex_sha256) shaRef.sha = c.tex_sha256;
                if (c.success) {
                    _status('Compilation succeeded!', false);
                    _addActivity(`Success! Compiled in ${c.iterations} pass(es).`);
                    btn.textContent = 'Compiled ✓';
                    if (c.changed) _addActivity('The corrected .tex was written back to the file.');
                } else {
                    _status('Compilation failed.', true);
                    _addActivity(`Failed after ${c.iterations} pass(es).`);
                    (c.log_excerpt || '').split('\n').forEach((l) => _addActivity(l));
                }
            },
            onError: (err) => {
                outcome = _wasCancelled() ? 'cancelled' : 'failed';
                _status(err, true); _addActivity('Error: ' + err);
            },
        });
    } catch (e) {
        outcome = _wasCancelled() ? 'cancelled' : 'failed';
        if (_wasCancelled()) {
            _status('Compile & auto-fix cancelled.');
            _addActivity('Cancelled by user.', true);
        } else {
            logError('Compile-fix failed', e);
            _status('Compile-fix request failed (see console).', true);
        }
    } finally {
        if (_wasCancelled() && outcome !== 'completed') outcome = 'cancelled';
        _compileFixing = false;
        _setBusy(false);
        btn.disabled = false;
        _abortCtrl = null;
        if (cancelBtn) cancelBtn.remove();
        taskEnd('docgen-compile-fix', outcome);
    }
}

/**
 * Compile the article as-is and render the PDF inline (write-free w.r.t. the
 * .tex). The PDF bytes are fetched with secureFetch and handed to the <iframe>
 * as a blob: URL — never a direct /api src (the origin guard 403s an iframe
 * navigation with no X-Requested-With header). Mirrors deck.js::runDeckPreview.
 */
export async function runDocPreview(docPath, btn, host) {
    // Same busy-set membership as compile-fix (audit #16): the preview holds
    // the server op lock for the whole latexmk pass.
    if (_docOpBusy()) return;
    _previewing = true;
    _setBusy(true);
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = 'Compiling…';
    _status('Compiling article for preview…');
    let outcome = 'unknown'; // Only an authoritative result can establish completion.
    taskBegin('docgen-preview', 'Compiling article');
    // Abort frees the UI only — compile-preview is a SYNCHRONOUS route, so the
    // server's latexmk pass runs out its own cap and then releases the op lock.
    // Write-free w.r.t. the .tex, so an abandoned pass leaves only regenerable
    // build artifacts (see deck.js::runDeckPreview for the same note).
    _abortCtrl = new AbortController();
    const cancelBtn = _mkCancelButton(host);
    if (cancelBtn) {
        cancelBtn.title = 'Stop waiting for this preview. The compile already '
            + 'running on the server finishes on its own (build artifacts only).';
        // The default aria-label claims a server-side cancel this synchronous
        // route can't deliver, and title is hover-only — AT users must hear
        // the same honest scope (2026-08-04 audit).
        cancelBtn.setAttribute('aria-label', cancelBtn.title);
    }
    try {
        const r = await secureFetch('/api/docgen/compile-preview', {
            method: 'POST',
            body: JSON.stringify({ deck_path: docPath }),
            signal: _abortCtrl.signal,
        });
        const d = await safeJson(r);
        if (!r.ok) { outcome = 'failed'; _status(d.error || `Preview failed (${r.status})`, true); return; }
        _clear(host);
        // The iframe just went; release whatever blob it was showing before
        // any early return below can strand it (2026-09-02).
        _previewTeardown();
        if (d.success && d.has_pdf) {
            // Same signal as the compile call: without it, Cancel during the
            // (potentially large) PDF download was inert — the blob completed
            // and the iframe rendered anyway (2026-08-04 audit).
            const pdfResp = await secureFetch(
                `/api/docgen/preview-pdf?deck_path=${encodeURIComponent(docPath)}`,
                { signal: _abortCtrl.signal });
            if (!pdfResp.ok) {
                outcome = 'failed';
                const pd = await safeJson(pdfResp);
                _status(pd.error || `PDF fetch failed (${pdfResp.status})`, true);
                return;
            }
            const blob = await pdfResp.blob();
            _previewObjectUrl = URL.createObjectURL(blob);
            outcome = 'completed';
            _status('Preview compiled.', false);
            const frame = document.createElement('iframe');
            frame.className = 'deck-preview-frame';
            frame.title = 'Article PDF preview';
            frame.style.width = '100%';
            frame.style.height = '70vh';
            frame.style.border = '1px solid var(--border, #ccc)';
            frame.style.marginTop = '8px';
            frame.src = _previewObjectUrl;
            host.appendChild(frame);
            const openBtn = document.createElement('button');
            openBtn.className = 'btn btn-outline btn-sm';
            openBtn.style.marginTop = '4px';
            openBtn.textContent = 'Open in PDF viewer';
            openBtn.addEventListener('click', async () => {
                try {
                    const or = await secureFetch('/api/docgen/open-pdf', {
                        method: 'POST',
                        body: JSON.stringify({ deck_path: docPath }),
                    });
                    if (!or.ok) {
                        const od = await safeJson(or);
                        _status(od.error || `Open failed (${or.status})`, true);
                    }
                } catch (e) { logError('Open PDF failed', e); }
            });
            host.appendChild(openBtn);
        } else {
            outcome = 'failed';
            _status('Compilation failed — see the log below.', true);
            const pre = document.createElement('pre');
            pre.className = 'deck-preview-log';
            pre.style.marginTop = '8px';
            pre.style.maxHeight = '40vh';
            pre.style.overflow = 'auto';
            pre.textContent = d.log_excerpt || 'No log produced.';
            host.appendChild(pre);
        }
    } catch (e) {
        outcome = _wasCancelled() ? 'unknown' : 'failed';
        if (_wasCancelled()) {
            _status('Stopped waiting for the preview (the server compile finishes on its own).');
        } else {
            logError('Article preview failed', e);
            _status('Preview request failed (see console).', true);
        }
    } finally {
        if (_wasCancelled() && outcome !== 'completed') outcome = 'unknown';
        _previewing = false;
        _setBusy(false);
        btn.disabled = false;
        btn.textContent = original;
        _abortCtrl = null;
        if (cancelBtn) cancelBtn.remove();
        taskEnd('docgen-preview', outcome);
    }
}
