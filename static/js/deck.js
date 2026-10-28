/**
 * Deck Generator tab: turn the vault into a Beamer .tex deck from a
 * user-supplied (and in-app editable) template.
 *
 * Imports only ui.js + api.js (per the JS module hierarchy). User-controlled
 * strings are rendered with textContent / createElement, never innerHTML.
 */
import { secureFetch, consumeSSE, logError, safeJson } from './api.js';
import { setStatusA11y, taskBegin, taskEnd, taskNote, copyToClipboard, confirmInline, showTaskError } from './ui.js';

let _generating = false;
let _previewing = false;
let _compileAvailable = false;
let _compileFixing = false;

function $(id) { return document.getElementById(id); }

/**
 * One-shot init from the bootstrap config: default the integrity-review panel
 * checkbox to the persisted `deck_review_enabled` knob (the LLM Settings
 * default). The checkbox still wins per run; this only seeds its initial state.
 */
export async function initDeck(cfg) {
    const el = $('deck-review');
    if (el) el.checked = !!(cfg && cfg.deck_review_enabled);

    try {
        const r = await secureFetch('/api/deck/compile-available');
        const d = await safeJson(r);
        _compileAvailable = !!(d && d.available);
    } catch (e) {
        console.error('Failed to check compile availability:', e);
    }
}

/** One-shot Retry boundary under the given root (docgen.js's pattern — the
 * checkpoint makes a retry cheap, which made the missing button costly: a
 * failed multi-minute run used to end with only a red status line). */
function _showRetryBoundary(root, message, retryFn) {
    if (!root) return;
    const box = document.createElement('div');
    box.className = 'task-error-boundary';
    root.appendChild(box);
    showTaskError(box, message, [
        { label: 'Retry', primary: true, onClick: () => { box.remove(); retryFn(); } },
    ]);
}

function _status(msg, isError = false) {
    taskNote('deck-', msg, isError ? 'failed' : null);
    const el = $('deck-status');
    if (!el) return;
    el.style.display = msg ? 'block' : 'none';
    setStatusA11y(el, msg, isError);
}

function _clear(el) { while (el && el.firstChild) el.removeChild(el.firstChild); }

// C2/#14 (2026-07-13): cancel affordance for the multi-minute deck SSE ops,
// mirroring docgen.js. Cancel aborts the fetch; the server sees the consumer
// disconnect and its cancel-gated worker stops at the next section (the
// per-section checkpoint makes the run resumable). One controller per op.
let _abortCtrl = null;

function _mkCancelButton(logEl) {
    if (!logEl || !logEl.parentElement) return null;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-outline btn-sm';
    btn.textContent = 'Cancel';
    btn.setAttribute('aria-label', 'Cancel the running deck operation');
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

function _wasCancelled() {
    return !!(_abortCtrl && _abortCtrl.signal.aborted);
}

function _addActivity(text, muted = false) {
    taskNote('deck-', text);
    const log = $('deck-activity');
    if (!log) return;
    const line = document.createElement('div');
    line.className = muted ? 'deck-activity-line muted' : 'deck-activity-line';
    line.textContent = text;
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
}

// --- Template loading -------------------------------------------------------

/** Native-pick a template .tex/.sty, fill the path input, and load it. */
export async function pickTemplateFile() {
    try {
        const r = await secureFetch('/api/deck/native-pick-file', { method: 'POST' });
        const d = await safeJson(r);
        if (!r.ok) throw new Error((d?.error || 'Native picker unavailable.') + ' Use the desktop window or enter the path manually.');
        if (d && d.path) {
            $('deck-template-path').value = d.path;
            await loadTemplate();
        }
    } catch (e) { logError('Template pick failed', e); _status(e.message, true); }
}

/** Native-pick the output folder for the scaffolded deck. */
export async function pickOutDir() {
    try {
        const r = await secureFetch('/api/deck/native-pick-folder', { method: 'POST' });
        const d = await safeJson(r);
        if (!r.ok) throw new Error((d?.error || 'Native picker unavailable.') + ' Use the desktop window or enter the path manually.');
        if (d && d.path) $('deck-out-dir').value = d.path;
    } catch (e) { logError('Output folder pick failed', e); _status(e.message, true); }
}

/**
 * Validate + scan the template at the path input (/api/deck/load-template),
 * load its .tex into the editor, default the output dir to the detected suite
 * root, and render the macro/bibliography hint.
 */
export async function loadTemplate() {
    const path = ($('deck-template-path').value || '').trim();
    if (!path) { _status('Enter or browse to a template .tex/.sty first.', true); return; }
    _status('Loading template…');
    try {
        const r = await secureFetch('/api/deck/load-template', {
            method: 'POST',
            body: JSON.stringify({ path }),
        });
        const d = await safeJson(r);
        if (!r.ok || d.error) { _status(d.error || 'Could not load template.', true); return; }

        $('deck-template-editor').value = d.tex || '';
        if (d.suite_root && !($('deck-out-dir').value || '').trim()) {
            $('deck-out-dir').value = d.suite_root;
        }
        _renderTemplateHint(d);
        _status('Template loaded. Edit the preamble below if you like, then Generate.');
    } catch (e) {
        logError('Load template failed', e);
        _status('Could not load template (see console).', true);
    }
}

function _renderTemplateHint(d) {
    const hint = $('deck-template-hint');
    if (!hint) return;
    _clear(hint);

    const macros = Array.isArray(d.macros) ? d.macros : [];
    const macroLine = document.createElement('div');
    if (macros.length) {
        macroLine.appendChild(document.createTextNode('Custom macros the model will use: '));
        macros.forEach((m, i) => {
            const code = document.createElement('code');
            code.textContent = m.signature;
            if (m.description) code.title = m.description;
            macroLine.appendChild(code);
            if (i < macros.length - 1) macroLine.appendChild(document.createTextNode(', '));
        });
    } else {
        macroLine.textContent = 'No custom macros detected in this template.';
    }
    hint.appendChild(macroLine);

    const bibLine = document.createElement('div');
    bibLine.className = 'muted';
    const n = d.bib_keys_count || 0;
    bibLine.textContent = n
        ? `${n} bibliography key(s) available — \\citefoot{key} citations enabled.`
        : 'No bibliography resolved (no active \\addbibresource) — citations will be plain-prose.';
    hint.appendChild(bibLine);

    const compilerLine = document.createElement('div');
    compilerLine.className = 'muted';
    compilerLine.textContent = _compileAvailable
        ? 'LaTeX compiler (latexmk): available.'
        : 'LaTeX compiler (latexmk): not available on PATH (PDF compilation disabled).';
    hint.appendChild(compilerLine);
}

// --- Generation -------------------------------------------------------------

/**
 * Generate the deck (emit-only). POSTs the topic + edited template + knobs and
 * streams the SSE trace: `{info}` activity, the agent frames
 * (iteration/tool_call/tool_result) shown as muted lines, and the terminal
 * `{deck}` frame rendered by _renderDeck. `thought` frames are deliberately
 * suppressed as noise; a non-200 (pre-stream reject) is surfaced and aborted so
 * the UI never hangs on "Generating…".
 */
// Item 3.8: ONE busy predicate for all deck operations. The guards were
// asymmetric — generate/augment ignored an in-flight compile-fix and
// compile-fix ignored an in-flight augment — so two deck operations could
// race client-side and the loser burned a round-trip on the server's 409.
// _previewing joins the set (review lot 5.1): a preview holds the server's
// _DECK_OP_LOCK for the whole latexmk pass, so a Generate click during it
// would 409 confusingly without this client-side gate.
function _deckOpBusy() {
    return _generating || _augmenting || _compileFixing || _previewing;
}

export async function generate() {
    // Only one deck operation at a time (the server enforces this too with a 409).
    if (_deckOpBusy()) return;
    const topic = ($('deck-topic').value || '').trim();
    const templateTex = $('deck-template-editor').value || '';
    if (!topic) { _status('A topic is required.', true); return; }
    if (!templateTex.trim()) { _status('Load (and optionally edit) a template first.', true); return; }

    const payload = {
        topic,
        template_tex: templateTex,
        template_path: ($('deck-template-path').value || '').trim(),
        out_dir: ($('deck-out-dir').value || '').trim(),
        deck_name: ($('deck-name').value || '').trim(),
        // The generated deck should carry the user's title, not the template's
        // placeholder — default to the topic when the field is left empty.
        deck_title: (($('deck-title') && $('deck-title').value) || '').trim() || topic,
        deck_subtitle: (($('deck-subtitle') && $('deck-subtitle').value) || '').trim(),
        instructions: $('deck-instructions').value || '',
        audience: ($('deck-audience').value || '').trim(),
        citations_enabled: $('deck-citations').checked,
        overwrite: $('deck-overwrite').checked,
        review_enabled: !!($('deck-review') && $('deck-review').checked),
        force_fresh: !!($('deck-force-fresh') && $('deck-force-fresh').checked),
        max_sections: parseInt($('deck-max-sections').value, 10) || 8,
        agent_max_iterations: parseInt($('deck-agent-iters').value, 10) || 6,
        temperature: parseFloat($('deck-temp').value),
    };

    _generating = true;
    let outcome = 'unknown'; // Only an authoritative result can establish completion.
    taskBegin('deck-generate', 'Generating deck');
    _setDeckBusy(true);
    _clear($('deck-activity'));
    _clear($('deck-result'));
    _status('Generating — this can take a few minutes for a multi-section deck…');
    _abortCtrl = new AbortController();
    const cancelBtn = _mkCancelButton($('deck-activity'));

    try {
        await consumeSSE('/api/deck/generate', {
            method: 'POST',
            body: JSON.stringify(payload),
            signal: _abortCtrl.signal,
        }, {
            onInfo: (info) => _addActivity(info),
            onOther: (evt) => {
                if (evt.iteration) _addActivity(`· iteration ${evt.iteration}`, true);
                else if (evt.tool_call) _addActivity(`· ${evt.tool_call.name}()`, true);
                else if (evt.tool_result) _addActivity(`· tool result${evt.tool_result.is_error ? ' [error]' : ''}`, true);
                else if (evt.deck) {
                    outcome = evt.deck.scaffold_error ? 'partial' : evt.deck.tex_path ? 'completed' : 'unknown';
                    _renderDeck(evt.deck);
                }
            },
            onError: (err) => {
                outcome = _wasCancelled() ? 'cancelled' : 'failed';
                _status(err, true);
                _addActivity('ERROR: ' + err);
                _showRetryBoundary($('deck-result'), err, generate);
            }
        });

    } catch (e) {
        outcome = _wasCancelled() ? 'cancelled' : 'failed';
        // C2/#14: distinguish the user's own Cancel from a real failure. The
        // per-section checkpoint means a cancelled run resumes on the next click.
        if (_wasCancelled()) {
            _status('Cancellation requested. Inspect the output folder for published files before retrying. Saved sections can be resumed.', false);
            _addActivity('Cancelled by user.', true);
        } else {
            logError('Deck generation failed', e);
            _status('Deck generation failed (see console).', true);
            // The checkpoint makes a retry resume from the last completed
            // section — offer it instead of ending on a red banner.
            _showRetryBoundary($('deck-result'), 'Deck generation failed.', generate);
        }
    } finally {
        if (_wasCancelled() && outcome !== 'completed') outcome = 'unknown';
        _generating = false;
        _abortCtrl = null;
        if (cancelBtn) cancelBtn.remove();
        _setDeckBusy(false);
        taskEnd('deck-generate', outcome);
    }
}

/** Enable/disable the deck-operation buttons together (one op at a time).
 * C2/#13 (2026-07-13): also covers the augment card's Preview-PDF button, and
 * preview/compile-fix now route through this (was: they disabled only their own
 * button, so Generate/Augment looked live but silently early-returned). */
function _setDeckBusy(busy) {
    for (const id of ['deck-generate-btn', 'deck-aug-preview-btn', 'deck-aug-pdf-btn']) {
        const el = $(id);
        if (el) el.disabled = busy;
    }
}

function _renderDeck(deck) {
    const root = $('deck-result');
    _clear(root);

    if (deck.resumed && deck.reused_sections > 0) {
        const banner = document.createElement('div');
        banner.className = 'muted';
        banner.textContent = `Resumed: ${deck.reused_sections} section(s) reused ` +
            'from a previous interrupted run. Tick “Start fresh” to regenerate from scratch.';
        root.appendChild(banner);
    }

    if (deck.scaffold_error) {
        const err = document.createElement('div');
        err.className = 'warning-banner';
        err.style.display = 'block';
        err.textContent = 'Not written to disk: ' + deck.scaffold_error +
            ' — the generated .tex is still shown below; use Copy to grab it.';
        root.appendChild(err);
        _status('Generated, but not written: ' + deck.scaffold_error, true);
    } else if (deck.tex_path) {
        const ok = document.createElement('div');
        ok.className = 'deck-summary';
        const n = deck.section_count || 0;
        const ph = deck.placeholder_count || 0;
        ok.textContent = `Wrote ${deck.tex_path}` +
            ` — ${n} section(s)${ph ? `, ${ph} placeholder` : ''}.`;
        root.appendChild(ok);
        if (deck.make_hint) {
            const hint = document.createElement('div');
            hint.className = 'muted';
            hint.appendChild(document.createTextNode('Compile it: '));
            const code = document.createElement('code');
            code.textContent = deck.make_hint;
            hint.appendChild(code);
            root.appendChild(hint);
        }
        if (_compileAvailable) {
            const compileBtn = document.createElement('button');
            compileBtn.className = 'btn btn-outline btn-sm';
            compileBtn.style.marginTop = '8px';
            compileBtn.textContent = 'Compile & Auto-Fix';
            // Mutable sha holder: a repair pass rewrites the .tex and the
            // terminal frame carries the new sha — a second click must send
            // THAT, not the generate-time sha (which would 409 as stale).
            // Item 3.4: accessor pair, NOT a copy — deck.tex_sha256 is the
            // single stale-diff source of truth. A plain {sha: …} snapshot
            // went stale the moment apply-repair rewrote the deck (its fresh
            // tex_sha256 echo landed on `deck`), so the next Compile & Fix
            // click sent the pre-repair sha and 409'd on a legitimate deck.
            const shaRef = {
                get sha() { return deck.tex_sha256; },
                set sha(v) { deck.tex_sha256 = v; },
            };
            // E10 (2026-07-22 audit): this loop WRITES LLM-authored repairs
            // into the user's .tex (with .bak/.orig) — identical risk to
            // apply-repair/apply-augment, which carry a strip. Route it
            // through the same Track-6e ceremony so the confirm strip never
            // reads as noise.
            compileBtn.addEventListener('click', () => confirmInline(compileBtn, {
                message: 'Compile and auto-repair may rewrite the deck .tex '
                    + '(a .bak backup is kept). Continue?',
                confirmLabel: 'Compile & fix',
                onConfirm: () => runCompileFix(deck.tex_path, shaRef, compileBtn),
            }));
            root.appendChild(compileBtn);

            const previewBtn = document.createElement('button');
            previewBtn.className = 'btn btn-outline btn-sm';
            previewBtn.style.marginTop = '8px';
            previewBtn.style.marginLeft = '8px';
            previewBtn.textContent = 'Preview PDF';
            const previewHost = document.createElement('div');
            previewHost.className = 'deck-preview-host';
            previewBtn.addEventListener('click', () => runDeckPreview(deck.tex_path, previewBtn, previewHost));
            root.appendChild(previewBtn);
            root.appendChild(previewHost);
        }
        _status(`Deck written (${n} section(s)). ` +
            (ph ? `${ph} section(s) had no content — review those.` : 'Done.'),
            ph > 0);
    }

    const warnings = Array.isArray(deck.warnings) ? deck.warnings : [];
    if (warnings.length) {
        const wrap = document.createElement('div');
        wrap.className = 'deck-warnings';
        const h = document.createElement('strong');
        h.textContent = 'Review before compiling:';
        wrap.appendChild(h);
        const ul = document.createElement('ul');
        warnings.forEach((w) => {
            const li = document.createElement('li');
            li.textContent = w;
            ul.appendChild(li);
        });
        wrap.appendChild(ul);
        root.appendChild(wrap);
    }

    if (deck.review && deck.review.ran) _renderReview(root, deck);

    root.appendChild(_texOutput('Generated .tex', 'deck-tex', deck.tex || '', 18));
}

/**
 * A labelled read-only .tex viewer with a Copy button — the generated deck and
 * the proposed repair both use it. The textarea stays natively selectable; the
 * button is the discoverable affordance (and announces success to AT).
 */
function _texOutput(labelText, id, value, rows) {
    const wrap = document.createElement('div');
    wrap.className = 'deck-tex-block';

    const head = document.createElement('div');
    head.className = 'deck-tex-head';
    const label = document.createElement('label');
    label.textContent = labelText;
    if (id) label.setAttribute('for', id);
    head.appendChild(label);
    const copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'btn btn-outline btn-sm';
    copy.textContent = 'Copy';
    copy.setAttribute('aria-label', 'Copy ' + labelText + ' to the clipboard');
    copy.addEventListener('click', () => copyToClipboard(value, copy));
    head.appendChild(copy);
    wrap.appendChild(head);

    const ta = document.createElement('textarea');
    if (id) ta.id = id;
    ta.readOnly = true;
    ta.rows = rows;
    ta.className = 'deck-tex-output';
    ta.value = value;
    wrap.appendChild(ta);
    return wrap;
}

/**
 * Render the opt-in LLM .tex integrity review: the flagged issues and, when the
 * model returned a screened repair, a preview of the corrected .tex plus an
 * Apply button that overwrites the on-disk deck via /api/deck/apply-repair.
 * All model/user text is rendered with textContent (never innerHTML).
 */
function _renderReview(root, deck) {
    const review = deck.review || {};
    const wrap = document.createElement('div');
    wrap.className = 'deck-warnings';

    const h = document.createElement('strong');
    h.textContent = 'LLM .tex integrity review';
    wrap.appendChild(h);

    if (review.error) {
        const err = document.createElement('div');
        err.className = 'muted';
        err.textContent = 'Review did not complete: ' + review.error;
        wrap.appendChild(err);
        root.appendChild(wrap);
        return;
    }

    const issues = Array.isArray(review.issues) ? review.issues : [];
    if (issues.length) {
        const ul = document.createElement('ul');
        issues.forEach((it) => {
            const li = document.createElement('li');
            li.textContent = it;
            ul.appendChild(li);
        });
        wrap.appendChild(ul);
    } else {
        const ok = document.createElement('div');
        ok.className = 'muted';
        ok.textContent = review.truncated
            ? 'Deck too large to review in full — only the start was checked.'
            : 'No compile-blocking issues flagged.';
        wrap.appendChild(ok);
    }

    (Array.isArray(review.repaired_warnings) ? review.repaired_warnings : [])
        .forEach((w) => {
            const note = document.createElement('div');
            note.className = 'muted';
            note.textContent = w;
            wrap.appendChild(note);
        });

    // The repair was cut off by the token cap (and the route did not already add a
    // size-budget warning, e.g. because no issue bullets preceded the repair) —
    // tell the user it was truncated rather than not attempted.
    if (review.repair_truncated && !review.changed
            && !(review.repaired_warnings || []).some((w) => /budget|too large/i.test(w))) {
        const note = document.createElement('div');
        note.className = 'muted';
        note.textContent = 'The auto-repair was cut off before completing — raise '
            + 'deck_review_max_tokens to let the model return the full corrected deck.';
        wrap.appendChild(note);
    }

    // Batch F (2026-07-22, offer-on-withheld-repair): the deck was too big for
    // a one-pass repair (input over the review cap OR output over the token
    // budget) but issues WERE found — offer the section-by-section repair.
    if (!review.changed && issues.length
            && (review.truncated || review.repair_truncated)
            && deck.tex_path) {
        const pw = document.createElement('button');
        pw.className = 'btn btn-outline';
        pw.textContent = 'Try section-by-section repair';
        pw.title = 'Reviews each \\section on its own (each fits the review budget), then previews one combined repaired deck — applied via the usual confirmed Apply.';
        pw.addEventListener('click', () => _runPiecewiseRepair(deck, pw));
        wrap.appendChild(pw);
        // Visible caveat (title tooltips are invisible to keyboard/touch/SR
        // users, and the skip behaviour matters before clicking).
        const pwNote = document.createElement('div');
        pwNote.className = 'muted';
        pwNote.textContent = 'Over-cap or failed sections are skipped and left unchanged.';
        wrap.appendChild(pwNote);
    }

    if (review.changed && review.repaired_tex) {
        wrap.appendChild(_texOutput('Proposed repaired .tex', '', review.repaired_tex, 14));

        const btn = document.createElement('button');
        btn.className = 'btn btn-outline';
        btn.textContent = 'Apply repaired version (overwrite file)';
        // Track 6e: overwriting a user's .tex is the same risk class Note
        // Refactor confirm-gates — never a single-click write.
        btn.addEventListener('click', () => confirmInline(btn, {
            message: 'Overwrite the deck .tex on disk with the repaired version? '
                + 'A .bak of the current file is written first.',
            confirmLabel: 'Overwrite file',
            onConfirm: () => _applyRepair(deck, btn),
        }));
        wrap.appendChild(btn);
    }

    root.appendChild(wrap);
}

/**
 * Run the section-by-section repair (Batch F) and pipe its terminal
 * `{augment}` frame into the standard augment renderer — the proposal is
 * staged server-side and applied through the usual confirmed apply-augment.
 */
async function _runPiecewiseRepair(deck, btn) {
    if (_deckOpBusy()) { _status('Another deck operation is running — wait for it.', true); return; }
    btn.disabled = true;
    _setDeckBusy(true);
    let outcome = 'unknown'; // Only an authoritative result can establish completion.
    taskBegin('deck-repair-pw', 'Piecewise repair');
    _clear($('deck-aug-activity'));
    _clear($('deck-aug-result'));
    _augStatus('Section-by-section repair — one review pass per section…');
    let sawAugment = false;
    try {
        await consumeSSE('/api/deck/repair-piecewise', {
            method: 'POST',
            body: JSON.stringify({ deck_path: deck.tex_path, base_sha256: deck.tex_sha256 || '' }),
        }, {
            onInfo: (info) => _augActivity(info),
            onOther: (evt) => {
                if (evt.augment) { sawAugment = true; outcome = evt.augment.rejected_reason ? 'failed' : 'completed'; _renderAugment(evt.augment); }
            },
            onError: (err) => {
                outcome = _wasCancelled() ? 'cancelled' : 'failed';
                _augStatus(err, true);
                _augActivity('ERROR: ' + err);
            },
        });
        if (sawAugment) {
            _augStatus('Piecewise repair preview ready below (Augment panel) — nothing written yet.');
        }
    } catch (e) {
        outcome = _wasCancelled() ? 'cancelled' : 'failed';
        logError('Piecewise repair failed', e);
        _augStatus('Piecewise repair failed (see console).', true);
    } finally {
        if (_wasCancelled() && outcome !== 'completed') outcome = 'cancelled';
        btn.disabled = false;
        _setDeckBusy(false);
        taskEnd('deck-repair-pw', outcome);
    }
}

/** Confirm + write a screened repair over the on-disk deck. */
async function _applyRepair(deck, btn) {
    const review = deck.review || {};
    if (!review.repaired_tex) return;
    btn.disabled = true;
    _status('Applying repaired .tex…');
    try {
        const r = await secureFetch('/api/deck/apply-repair', {
            method: 'POST',
            body: JSON.stringify({
                out_dir: deck.out_dir || '',
                deck_name: deck.slug || '',
                tex: review.repaired_tex,
                // Stale-diff token from the generate frame: the server refuses
                // (409) if the on-disk deck changed since this review.
                base_sha256: deck.tex_sha256 || '',
                confirm: true,
            }),
        });
        const d = await safeJson(r);
        if (!r.ok || d.error) {
            _status(d.error || 'Could not apply the repair.', true);
            btn.disabled = false;
            return;
        }
        btn.textContent = 'Applied ✓';
        // Item 3.4: consume the post-write echo. The server returns the
        // raw-byte sha of the deck it JUST wrote; without adopting it every
        // follow-up action (Compile & Fix reads deck.tex_sha256 through its
        // shaRef accessor) carried the pre-repair sha and 409'd.
        if (d.tex_sha256) deck.tex_sha256 = d.tex_sha256;
        _status('Repaired .tex written to ' + (d.tex_path || 'disk') + '.');
    } catch (e) {
        logError('Apply repair failed', e);
        _status('Could not apply the repair (see console).', true);
        btn.disabled = false;
    }
}

// --- Augment an existing deck ----------------------------------------------

let _augmenting = false;
// Loaded deck state: { deckPath, deckSha256, sections:[{index,title}] }.
let _augment = null;

function _augStatus(msg, isError = false) {
    const el = $('deck-aug-status');
    if (!el) return;
    el.style.display = msg ? 'block' : 'none';
    setStatusA11y(el, msg, isError);
}

function _augActivity(text, muted = false) {
    const log = $('deck-aug-activity');
    if (!log) return;
    const line = document.createElement('div');
    line.className = muted ? 'deck-activity-line muted' : 'deck-activity-line';
    line.textContent = text;
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
}

/**
 * Preview the AUGMENT panel's deck as a PDF (review lot 5.1). This is the
 * common case the generate-panel button misses: previewing an existing —
 * possibly hand-written — deck the user just picked, before or after an
 * augment. Same write-free compile+blob pipeline as runDeckPreview; the path
 * comes from the panel's input and is validated server-side.
 */
export async function augPreviewPdf() {
    const path = ($('deck-aug-path').value || '').trim();
    if (!path) { _augStatus('Choose a deck .tex first.', true); return; }
    const btn = $('deck-aug-pdf-btn');
    const host = $('deck-aug-pdf-host');
    if (!btn || !host) return;
    // C2/#44: route status into the augment card's own banner, not the generate card.
    await runDeckPreview(path, btn, host, _augStatus);
}

/** Native-pick an existing deck .tex and load its sections. */
export async function pickAugmentDeck() {
    try {
        const r = await secureFetch('/api/deck/native-pick-file', { method: 'POST' });
        const d = await safeJson(r);
        if (!r.ok) throw new Error((d?.error || 'Native picker unavailable.') + ' Use the desktop window or enter the path manually.');
        if (d && d.path) {
            $('deck-aug-path').value = d.path;
            await loadDeckSections();
        }
    } catch (e) { logError('Augment deck pick failed', e); _augStatus(e.message, true); }
}

/** Parse the deck at the path input (/api/deck/deck-sections) and fill the scope picker. */
export async function loadDeckSections() {
    const path = ($('deck-aug-path').value || '').trim();
    if (!path) { _augStatus('Choose a deck .tex first.', true); return; }
    _augStatus('Loading deck…');
    try {
        const r = await secureFetch('/api/deck/deck-sections', {
            method: 'POST',
            body: JSON.stringify({ deck_path: path }),
        });
        const d = await safeJson(r);
        if (!r.ok || d.error) { _augStatus(d.error || 'Could not load deck.', true); return; }
        _augment = {
            deckPath: d.deck_path,
            deckSha256: d.deck_sha256,
            sections: Array.isArray(d.sections) ? d.sections : [],
        };
        _fillSectionSelect(_augment.sections);
        augScopeChanged();
        const n = _augment.sections.length;
        const hint = $('deck-aug-hint');
        if (hint) hint.textContent = n
            ? `${n} section(s) detected.`
            : 'No \\section found — only whole-deck / new-section edits will apply.';
        _augStatus(`Deck loaded. Choose an operation and describe the change, then Preview.`);
    } catch (e) {
        logError('Load deck sections failed', e);
        _augStatus('Could not load the deck (see console).', true);
    }
}

function _fillSectionSelect(sections) {
    const sel = $('deck-aug-section');
    if (!sel) return;
    _clear(sel);
    sections.forEach((s) => {
        const opt = document.createElement('option');
        opt.value = String(s.index);
        // Titles are user-controlled — set via textContent, never innerHTML.
        opt.textContent = `${s.index + 1}. ${s.title}`;
        sel.appendChild(opt);
    });
}

/** Enable the section selector only when it is meaningful (section scope, or
 *  "after a section" for a new-section insert). */
export function augScopeChanged() {
    const sel = $('deck-aug-section');
    if (!sel) return;
    const scope = ($('deck-aug-scope').value || 'whole');
    const hasSections = !!(_augment && _augment.sections.length);
    sel.disabled = !(scope === 'section' && hasSections);
}

/**
 * Preview a free-text augmentation of the loaded deck. Streams the same SSE
 * trace as generate; the terminal `{augment}` frame is rendered by _renderAugment.
 * Writes nothing — the user confirms via Apply.
 */
export async function augmentPreview(piecewise) {
    if (_deckOpBusy()) return;   // one deck op at a time (all three ops)
    if (!_augment) { _augStatus('Load a deck first.', true); return; }
    const instruction = ($('deck-aug-instruction').value || '').trim();
    if (!instruction) { _augStatus('Describe the change you want.', true); return; }

    const scope = ($('deck-aug-scope').value || 'whole');
    const payload = {
        deck_path: _augment.deckPath,
        instruction,
        operation: ($('deck-aug-op').value || 'deepen'),
        scope,
        section_index: parseInt($('deck-aug-section').value, 10) || 0,
        audience: ($('deck-audience').value || '').trim(),
        citations_enabled: $('deck-citations').checked,
    };
    // Batch E (2026-07-22): the offer-on-refusal re-run — one agent turn per
    // \section when the whole-deck pass refused on the size cap.
    if (piecewise === true) payload.piecewise = true;

    _augmenting = true;
    let outcome = 'unknown'; // Only an authoritative result can establish completion.
    taskBegin('deck-augment', 'Augmenting deck');
    _setDeckBusy(true);
    _clear($('deck-aug-activity'));
    _clear($('deck-aug-result'));
    _augStatus('Augmenting — one vault-grounded pass; this can take a moment…');
    _abortCtrl = new AbortController();
    const cancelBtn = _mkCancelButton($('deck-aug-activity'));

    try {
        await consumeSSE('/api/deck/augment', {
            method: 'POST',
            body: JSON.stringify(payload),
            signal: _abortCtrl.signal,
        }, {
            onInfo: (info) => _augActivity(info),
            onOther: (evt) => {
                if (evt.iteration) _augActivity(`· iteration ${evt.iteration}`, true);
                else if (evt.tool_call) _augActivity(`· ${evt.tool_call.name}()`, true);
                else if (evt.tool_result) _augActivity(`· tool result${evt.tool_result.is_error ? ' [error]' : ''}`, true);
                else if (evt.augment) { outcome = evt.augment.rejected_reason ? 'failed' : 'completed'; _renderAugment(evt.augment); }
            },
            onError: (err, frame) => {
                outcome = _wasCancelled() ? 'cancelled' : 'failed';
                _augStatus(err, true);
                _augActivity('ERROR: ' + err);
                // Offer the piecewise run when the refusal is the size cap
                // (whole scope only — a single over-cap section has no
                // smaller unit to fall back to). Keyed on the structured
                // `code` (2026-07-23 — a copy edit to the server prose used
                // to silently kill the offer); the regex stays as a fallback
                // for an older server without the field.
                const sizeCap = (frame && frame.code === 'size_cap')
                    || /too large to revise in one pass/i.test(err);
                if (sizeCap && scope === 'whole') {
                    _offerPiecewiseAugment();
                }
            }
        });

    } catch (e) {
        outcome = _wasCancelled() ? 'cancelled' : 'failed';
        if (_wasCancelled()) {
            _augStatus('Augmentation cancelled — nothing was written.', false);
            _augActivity('Cancelled by user.', true);
        } else {
            logError('Deck augmentation failed', e);
            _augStatus('Augmentation failed (see console).', true);
            _showRetryBoundary($('deck-aug-result'), 'Augmentation failed.',
                () => augmentPreview(piecewise));
        }
    } finally {
        if (_wasCancelled() && outcome !== 'completed') outcome = 'cancelled';
        _augmenting = false;
        _abortCtrl = null;
        if (cancelBtn) cancelBtn.remove();
        _setDeckBusy(false);
        taskEnd('deck-augment', outcome);
    }
}

/** Render the 'Revise section by section' offer after a size-cap refusal. */
function _offerPiecewiseAugment() {
    const root = $('deck-aug-result');
    if (!root || root.querySelector('.deck-piecewise-offer')) return;
    // Button + caveat share one wrapper (carrying the dedupe class) so the
    // click removes them together — a click on the bare button used to leave
    // an orphaned note behind.
    const wrap = document.createElement('div');
    wrap.className = 'deck-piecewise-offer';
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
    const root = $('deck-aug-result');
    _clear(root);

    // Change summary FIRST: a drop in the frame/section count is the cheap signal
    // that an augmentation silently lost slides. Render it prominently and flag a
    // decrease (the server de-duplicates warnings, so we no longer concat lists).
    const c = aug.counts || {};
    if (Number.isFinite(c.frames_before) && Number.isFinite(c.frames_after)) {
        const lostFrames = c.frames_after < c.frames_before;
        const lostSecs = c.sections_after < c.sections_before;
        const sum = document.createElement('div');
        sum.className = (lostFrames || lostSecs) ? 'warning-banner' : 'deck-summary';
        if (lostFrames || lostSecs) sum.style.display = 'block';
        sum.textContent =
            `Frames: ${c.frames_before} → ${c.frames_after}` +
            `   ·   Sections: ${c.sections_before} → ${c.sections_after}` +
            ((lostFrames || lostSecs)
                ? '  — fewer than before; check nothing was dropped before applying.'
                : '');
        root.appendChild(sum);
    }

    const warnings = (Array.isArray(aug.warnings) ? aug.warnings : []).slice();
    if (aug.rejected_reason) warnings.push(aug.rejected_reason);
    if (warnings.length) {
        const wrap = document.createElement('div');
        wrap.className = 'deck-warnings';
        const h = document.createElement('strong');
        h.textContent = 'Review before applying:';
        wrap.appendChild(h);
        const ul = document.createElement('ul');
        warnings.forEach((w) => {
            const li = document.createElement('li');
            li.textContent = w;
            ul.appendChild(li);
        });
        wrap.appendChild(ul);
        root.appendChild(wrap);
    }

    if (!aug.changed || !aug.proposed_tex) {
        const m = document.createElement('div');
        m.className = 'muted';
        m.textContent = warnings.length
            ? 'No applicable change was produced (see above).'
            : 'No change was produced — the model returned content identical to the deck. Try rephrasing the instruction.';
        root.appendChild(m);
        _augStatus('No change to apply.', false);
        return;
    }

    root.appendChild(_texOutput('Proposed augmented .tex', 'deck-aug-tex', aug.proposed_tex, 18));

    const btn = document.createElement('button');
    btn.className = 'btn btn-outline';
    btn.textContent = 'Apply to the deck (overwrite file)';
    // Track 6e: same ceremony as apply-repair — this overwrites a user file.
    btn.addEventListener('click', () => confirmInline(btn, {
        message: 'Overwrite ' + (aug.deck_path || 'the deck') + ' with the augmented '
            + 'version? A .bak of the current file is written first.',
        confirmLabel: 'Overwrite file',
        onConfirm: () => _applyAugment(aug, btn),
    }));
    root.appendChild(btn);
    _augStatus('Preview ready — review the proposed .tex, then Apply to write it.');
}

/** Confirm + write the server-staged augmentation over the on-disk deck. */
async function _applyAugment(aug, btn) {
    if (!aug.proposed_tex) return;
    btn.disabled = true;
    _augStatus('Applying the augmentation…');
    try {
        const r = await secureFetch('/api/deck/apply-augment', {
            method: 'POST',
            body: JSON.stringify({
                deck_path: aug.deck_path || '',
                // The proposed .tex is read from server-side staging, never sent
                // from here; we pass only the stale-diff token (the server 409s if
                // the on-disk deck changed since the preview) and the
                // preview-binding token (409 if a newer preview replaced the
                // single staging slot after this one was shown).
                base_sha256: aug.deck_sha256 || '',
                proposed_sha256: aug.proposed_sha256 || '',
                confirm: true,
            }),
        });
        const d = await safeJson(r);
        if (!r.ok || d.error) {
            _augStatus(d.error || 'Could not apply the augmentation.', true);
            btn.disabled = false;
            return;
        }
        btn.textContent = 'Applied ✓';
        _augStatus('Augmented deck written to ' + (d.tex_path || 'disk') +
            (d.backup_path ? ' (backup: ' + d.backup_path + ').' : '.'));
        // The deck on disk changed — refresh the section list so a follow-up edit
        // previews against the new content (and picks up an inserted section).
        loadDeckSections();
    } catch (e) {
        logError('Apply augment failed', e);
        _augStatus('Could not apply the augmentation (see console).', true);
        btn.disabled = false;
    }
}

/**
 * Trigger the compile-and-fix SSE loop on the specified deck.
 *
 * `shaRef` is a mutable `{sha}` holder shared with the caller's button: the
 * loop rewrites the .tex on a successful repair, and the terminal frame's
 * `tex_sha256` must replace the stored value or the next click sends a stale
 * sha and 409s.
 */
export async function runCompileFix(deckPath, shaRef, btn) {
    if (_deckOpBusy()) return;
    _compileFixing = true;
    _setDeckBusy(true);   // C2/#13: block Generate/Augment during compile-fix
    btn.disabled = true;

    const act = $('deck-activity');
    if (act) {
        _clear(act);
        act.style.display = 'block';
    }
    _status('Starting LaTeX compilation & repair loop…');
    _addActivity('Initializing latexmk subprocess...');

    let outcome = 'unknown'; // Only an authoritative result can establish completion.
    taskBegin('deck-compile-fix', 'Compiling deck');
    // Client-side abort (P6.2, 2026-07-30). Generate and Augment have had a
    // Cancel button since their SSE wiring landed; compile-fix — the LONGEST
    // deck operation (up to (2*iters+1) compile passes plus LLM repair turns)
    // — had none, so a user watching a doomed repair loop could only wait or
    // reload. Aborting the fetch closes the SSE stream, which the server's
    // consumer-disconnect path already handles: the worker's cancel event is
    // set, latexmk's process group is TERM->KILLed on its own timeout, and
    // _SSEOpGuard releases the op lock once the worker exits.
    _abortCtrl = new AbortController();
    const cancelBtn = _mkCancelButton($('deck-activity'));
    try {
        // Dead-button fix (improvement plan 2026-07-04, item 1.1). This used to
        // call readSSE('/api/deck/compile-fix', {…}, callback) — but readSSE is
        // an async generator over an already-fetched Response, so NO request was
        // ever issued: the never-iterated generator was awaited as a plain value
        // and the button logged "Starting…" then silently completed. The whole
        // Compile & Auto-Fix feature was inert client-side. Fix: fetch first via
        // secureFetch, then `for await` the frames — the exact shape every other
        // SSE consumer in this module uses (generate/augment). Safe: client-only;
        // the request body ({deck_path, base_sha256, confirm}) and the
        // {info}/{error}/{compile} frame dispatch are byte-identical to what the
        // server route already documents — only the transport misuse is fixed.
        // Invariant (pinned by tests/js/deckCompileFix.test.js): clicking the
        // button issues exactly one POST to /api/deck/compile-fix and consumes
        // its SSE stream to the terminal {compile} frame.
        await consumeSSE('/api/deck/compile-fix', {
            method: 'POST',
            body: JSON.stringify({
                deck_path: deckPath,
                base_sha256: shaRef.sha,
                confirm: true
            }),
            signal: _abortCtrl.signal,
        }, {
            onInfo: (info) => _addActivity(info),
            onOther: (evt) => {
                if (evt.compile) {
                    const c = evt.compile;
                    outcome = c.success ? 'completed' : 'failed';
                    if (c.tex_sha256) shaRef.sha = c.tex_sha256;
                    if (c.success) {
                        _status('Compilation succeeded!', false);
                        _addActivity(`Success! Compiled in ${c.iterations} pass(es).`);
                        btn.textContent = 'Compiled ✓';
                        if (c.changed) {
                            _addActivity('The corrected .tex has been written back to the file.');
                        }
                    } else {
                        _status('Compilation failed.', true);
                        _addActivity(`Failed after ${c.iterations} pass(es).`);
                        if (c.log_excerpt) {
                            _addActivity('\n--- Log Excerpt ---');
                            c.log_excerpt.split('\n').forEach(line => _addActivity(line));
                        }
                    }
                }
            },
            onError: (err) => {
                outcome = _wasCancelled() ? 'cancelled' : 'failed';
                _status(err, true);
                _addActivity('Error: ' + err);
            }
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
        _setDeckBusy(false);
        btn.disabled = false;
        _abortCtrl = null;
        if (cancelBtn) cancelBtn.remove();
        taskEnd('deck-compile-fix', outcome);
    }
}

// The one live preview object URL. Revoked before each new preview so repeated
// compiles don't leak a blob per click (same discipline as refactor.js's
// _objectUrls, but a single slot: only one preview frame exists at a time).
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


/**
 * Compile the deck as-is and render the resulting PDF inline (axis i preview).
 * Write-free w.r.t. the .tex — the server runs one latexmk pass (build
 * artifacts only) and serves <base>.pdf. On a compile failure the log excerpt
 * is shown instead so the user can see why.
 *
 * The PDF bytes are fetched with secureFetch and handed to the <iframe> as a
 * blob: URL — NEVER as a direct `/api/...` src. Every /api route requires the
 * X-Requested-With header (app-level origin guard), which an iframe navigation
 * request cannot send — a direct src would 403. Same constraint and same fix
 * as refactor.js::_loadThumb; keeping the fetch header-authenticated preserves
 * the server's origin check instead of weakening the endpoint.
 */
export async function runDeckPreview(deckPath, btn, host, statusFn = _status) {
    // C2/#44 (2026-07-13): statusFn lets the augment card route compile
    // progress/errors into ITS OWN status banner instead of the generate card's.
    if (_deckOpBusy()) return;
    _previewing = true;
    _setDeckBusy(true);   // C2/#13: block Generate/Augment while a preview runs
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = 'Compiling…';
    statusFn('Compiling deck for preview…');
    let outcome = 'unknown'; // Only an authoritative result can establish completion.
    taskBegin('deck-preview', 'Compiling deck');
    // Client-side abort (P6.2, 2026-07-30). compile-preview is a SYNCHRONOUS
    // route, so aborting frees the UI (and this fetch) — it does NOT stop the
    // server's latexmk pass, which runs out its own min(deck_compile_timeout_s,
    // 300) cap and then releases the op lock. Honest affordance: the button
    // says as much, and the preview is write-free w.r.t. the .tex, so an
    // abandoned pass can only leave regenerable build artifacts.
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
        const r = await secureFetch('/api/deck/compile-preview', {
            method: 'POST',
            body: JSON.stringify({ deck_path: deckPath }),
            signal: _abortCtrl.signal,
        });
        const d = await safeJson(r);
        if (!r.ok) {
            outcome = 'failed';
            statusFn(d.error || `Preview failed (${r.status})`, true);
            return;
        }
        _clear(host);
        // The iframe just went; release whatever blob it was showing before
        // any early return below can strand it (2026-09-02).
        _previewTeardown();
        if (d.success && d.has_pdf) {
            // Same signal as the compile call: without it, Cancel during the
            // (potentially large) PDF download was inert — the blob completed
            // and the iframe rendered anyway (2026-08-04 audit).
            const pdfResp = await secureFetch(
                `/api/deck/preview-pdf?deck_path=${encodeURIComponent(deckPath)}`,
                { signal: _abortCtrl.signal });
            if (!pdfResp.ok) {
                outcome = 'failed';
                const pd = await safeJson(pdfResp);
                statusFn(pd.error || `PDF fetch failed (${pdfResp.status})`, true);
                return;
            }
            const blob = await pdfResp.blob();
            _previewObjectUrl = URL.createObjectURL(blob);
            outcome = 'completed';
            statusFn('Preview compiled.', false);
            const frame = document.createElement('iframe');
            frame.className = 'deck-preview-frame';
            frame.title = 'Deck PDF preview';
            frame.style.width = '100%';
            frame.style.height = '70vh';
            frame.style.border = '1px solid var(--border, #ccc)';
            frame.style.marginTop = '8px';
            frame.src = _previewObjectUrl;
            host.appendChild(frame);
            // Fallback affordance: WKWebView builds that refuse to render a
            // blob: PDF inline still get a one-click path to the compiled file
            // (server-side `open`, same validated deck path — no new surface).
            const openBtn = document.createElement('button');
            openBtn.className = 'btn btn-outline btn-sm';
            openBtn.style.marginTop = '4px';
            openBtn.textContent = 'Open in PDF viewer';
            openBtn.addEventListener('click', async () => {
                try {
                    const or = await secureFetch('/api/deck/open-pdf', {
                        method: 'POST',
                        body: JSON.stringify({ deck_path: deckPath }),
                    });
                    if (!or.ok) {
                        const od = await safeJson(or);
                        statusFn(od.error || `Open failed (${or.status})`, true);
                    }
                } catch (e) { logError('Open PDF failed', e); }
            });
            host.appendChild(openBtn);
        } else {
            outcome = 'failed';
            statusFn('Compilation failed — see the log below.', true);
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
            statusFn('Stopped waiting for the preview (the server compile finishes on its own).');
        } else {
            logError('Deck preview failed', e);
            statusFn('Preview request failed (see console).', true);
        }
    } finally {
        if (_wasCancelled() && outcome !== 'completed') outcome = 'unknown';
        _previewing = false;
        _setDeckBusy(false);
        btn.disabled = false;
        btn.textContent = original;
        _abortCtrl = null;
        if (cancelBtn) cancelBtn.remove();
        taskEnd('deck-preview', outcome);
    }
}
