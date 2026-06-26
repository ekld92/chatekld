/**
 * Note Refactor tab (Phase 1.5 — read-only analyzer + central hub).
 *
 * Imports only ui.js + api.js (per the JS module hierarchy). All user-controlled
 * strings (note paths, image targets, descriptions, model output) are rendered
 * with textContent / createElement — never innerHTML. The ONE exception is the
 * rendered-markdown preview, which goes through marked + sanitiseHtml exactly
 * like vault.js::_renderAnswer (and falls back to textContent when marked is
 * unavailable).
 *
 * Layout: a streamed list of notes in a sidebar (#refactor-note-list) + a
 * single-note detail pane (#refactor-detail) with a Rendered/Diff view toggle.
 */
import { secureFetch, readSSE, logError } from './api.js';
import { setStatusA11y, taskBegin, taskEnd, sanitiseHtml, openModal, closeModal } from './ui.js';

let _running = false;

// Streamed note frames (in arrival order) + the currently-selected note + the
// detail view mode. Reset at the start of each run.
let _notes = [];
let _selectedRel = null;
let _viewMode = 'rendered'; // 'rendered' | 'diff'

// Phase 2 (vault writes). _approved holds rel_paths the user ticked for the
// callout-only batch apply; _scope is the scope the current plan ran against
// (echoed back to the write endpoints so they target the same sub-folder).
let _approved = new Set();
let _scope = '';

// Object URLs created for thumbnails this run; revoked on the next run so the
// blobs don't leak. _thumbCache de-dupes fetches across note re-selections.
let _objectUrls = [];
let _thumbCache = new Map();
// One shared IntersectionObserver lazy-loads thumbnails only when they scroll
// into view, so a detail pane with many images doesn't fire all fetches at once.
let _thumbObserver = null;

function $(id) { return document.getElementById(id); }

/**
 * The image endpoint requires the X-Requested-With header (origin_is_local),
 * which a plain <img src> CANNOT send — so we fetch the bytes with secureFetch
 * and hand the <img> a blob: URL instead. This keeps the server's CSRF/origin
 * check intact rather than weakening the endpoint to allow header-less GETs.
 */
async function _loadThumb(img, rel) {
    if (_thumbCache.has(rel)) { img.src = _thumbCache.get(rel); return; }
    try {
        const resp = await secureFetch('/api/refactor/image?rel=' + encodeURIComponent(rel));
        if (!resp.ok) { img.alt = 'image unavailable'; return; }
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        _objectUrls.push(url);
        _thumbCache.set(rel, url);
        img.src = url;
    } catch (e) {
        img.alt = 'image failed to load';
    }
}

function _ensureObserver() {
    if (_thumbObserver) return _thumbObserver;
    _thumbObserver = new IntersectionObserver((entries, obs) => {
        for (const e of entries) {
            if (!e.isIntersecting) continue;
            obs.unobserve(e.target);              // load once, then stop watching
            _loadThumb(e.target, e.target.dataset.rel);
        }
    }, { rootMargin: '150px' });                  // start loading just before visible
    return _thumbObserver;
}

function _resetThumbs() {
    // Drop watchers on now-removed <img>s and free last run's blob URLs.
    if (_thumbObserver) _thumbObserver.disconnect();
    _objectUrls.forEach((u) => URL.revokeObjectURL(u));
    _objectUrls = [];
    _thumbCache.clear();
}

function _status(msg, isError = false) {
    const el = $('refactor-status');
    if (!el) return;
    el.style.display = msg ? 'block' : 'none';
    setStatusA11y(el, msg, isError);
}

function _clear(el) { while (el && el.firstChild) el.removeChild(el.firstChild); }

function _activity(text, muted = false) {
    const log = $('refactor-activity');
    if (!log) return;
    const line = document.createElement('div');
    line.className = muted ? 'deck-activity-line muted' : 'deck-activity-line';
    line.textContent = text;
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
}

function _badge(text, cls) {
    const b = document.createElement('span');
    b.className = 'refactor-badge' + (cls ? ' ' + cls : '');
    b.textContent = text;
    return b;
}

// --- Folder picker ----------------------------------------------------------

/**
 * Open the native folder picker and fill the scope input with the chosen
 * sub-folder (server-side converted to a vault-relative path; rejected if it
 * is the vault root or outside the vault). Manual typing keeps working.
 */
export async function pickScopeFolder() {
    try {
        const r = await secureFetch('/api/refactor/native-pick-folder', { method: 'POST' });
        const d = await r.json();
        if (d && d.scope) { $('refactor-scope').value = d.scope; _status(''); }
        else if (d && d.error) { _status(d.error, true); }
        // d.cancelled → no-op
    } catch (e) {
        logError('Refactor scope pick failed', e);
        _status('Folder pick failed (see console).', true);
    }
}

// --- Run the plan -----------------------------------------------------------

/**
 * Run the read-only analysis plan over the chosen scope (0 vision calls, 0 vault
 * writes server-side). Resets all per-run state (notes, selection, approvals,
 * thumbnail blob URLs/observer) before streaming the SSE result: `{info}`
 * activity, one `{note}` frame per analyzed note (fed to _addNote), and a
 * terminal `{refactor}` summary. The scope is captured into `_scope` so the
 * apply/archive write endpoints target the same sub-folder.
 */
export async function runPlan() {
    if (_running) return;
    _running = true;
    $('refactor-run-btn').disabled = true;
    taskBegin('refactor-plan');
    _status('');
    _resetThumbs();   // free prior run's blob URLs + observer before clearing DOM
    _notes = [];
    _selectedRel = null;
    _approved = new Set();
    _updateApplyButton();
    _clear($('refactor-activity'));
    _clear($('refactor-note-list'));
    _clear($('refactor-detail'));
    _clear($('refactor-discrepancies'));
    $('refactor-summary').style.display = 'none';
    _renderDetail(null);   // placeholder until the first note streams in

    const payload = {};
    const scope = ($('refactor-scope').value || '').trim();
    if (scope) payload.scope_subdir = scope;
    _scope = scope;   // the apply/archive endpoints target this same sub-folder

    try {
        const resp = await secureFetch('/api/refactor/plan', {
            method: 'POST',
            body: JSON.stringify(payload),
        });
        if (!resp.ok) {
            let msg = `Plan failed (HTTP ${resp.status}).`;
            try { const d = await resp.json(); if (d && d.error) msg = d.error; } catch (_) {}
            _status(msg, true);
            return;
        }
        for await (const evt of readSSE(resp)) {
            if (evt.info) _activity(evt.info);
            else if (evt.error) { _status(evt.error, true); _activity('ERROR: ' + evt.error); }
            else if (evt.note) _addNote(evt.note);
            else if (evt.refactor) _renderSummary(evt.refactor);
        }
    } catch (e) {
        logError('Refactor plan failed', e);
        _status('Refactor plan failed (see console).', true);
    } finally {
        _running = false;
        $('refactor-run-btn').disabled = false;
        taskEnd('refactor-plan');
    }
}

// --- Summary + discrepancies ------------------------------------------------

function _renderSummary(summary) {
    if (summary.scope_subdir) _scope = summary.scope_subdir;  // server-resolved scope
    const el = $('refactor-summary');
    _clear(el);
    el.style.display = 'block';
    const parts = [
        `${summary.note_count} note(s)`,
        `${summary.image_count} image embed(s)`,
        `${summary.changed_count} with proposed inlines`,
        `${summary.not_extracted_count} not yet extracted`,
        `${summary.likely_table_count} likely table(s)`,
    ];
    if (summary.handwritten_count) parts.push(`${summary.handwritten_count} handwritten`);
    if (summary.ignored_count) parts.push(`${summary.ignored_count} ignored`);
    el.textContent = parts.join(' · ');

    const dEl = $('refactor-discrepancies');
    _clear(dEl);
    const discs = summary.discrepancies || [];
    if (!discs.length) return;
    const h = document.createElement('div');
    h.className = 'refactor-section-title';
    h.textContent = `Advisory dose discrepancies (${discs.length}) — heuristic, verify manually`;
    dEl.appendChild(h);
    for (const d of discs) {
        const row = document.createElement('div');
        row.className = 'refactor-discrepancy';
        const subj = document.createElement('strong');
        subj.textContent = d.subject;
        row.appendChild(subj);
        const reason = document.createElement('span');
        reason.className = 'muted';
        reason.textContent = ' — ' + d.reason;
        row.appendChild(reason);
        const ul = document.createElement('ul');
        for (const o of d.occurrences || []) {
            const li = document.createElement('li');
            li.textContent = `${o.dose}  —  ${o.note}:${o.line}`;
            ul.appendChild(li);
        }
        row.appendChild(ul);
        dEl.appendChild(row);
    }
}

// --- Sidebar (note list) ----------------------------------------------------

/**
 * Handle one streamed `{note}` frame: keep it in `_notes` (the in-memory model
 * the detail pane reads from + mutates on classify/ignore), append a clickable
 * sidebar entry (path + proposed/image/hygiene badges, all via createElement so
 * the user's note path can't inject markup), and auto-select the very first
 * note so the detail pane is populated as soon as analysis starts.
 */
function _addNote(note) {
    _notes.push(note);
    const list = $('refactor-note-list');
    const entry = document.createElement('div');
    entry.className = 'refactor-note-entry';
    entry.setAttribute('role', 'option');
    entry.dataset.rel = note.rel_path;
    entry.tabIndex = 0;

    // Only a note with a proposed callout (changed) can be applied; give it an
    // approve checkbox feeding the batch-apply selection.
    if (note.changed) {
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.className = 'refactor-approve-cb';
        cb.title = 'Approve this note for Apply';
        cb.setAttribute('aria-label', 'Approve ' + note.rel_path + ' for apply');
        cb.onclick = (e) => { e.stopPropagation(); _toggleApprove(note.rel_path, cb.checked); };
        entry.appendChild(cb);
    }

    const path = document.createElement('span');
    path.className = 'refactor-note-path';
    path.textContent = note.rel_path;
    entry.appendChild(path);

    const badges = document.createElement('span');
    badges.className = 'refactor-entry-badges';
    if (note.changed) badges.appendChild(_badge('proposed', 'badge-changed'));
    const imgCount = (note.images || []).length;
    if (imgCount) badges.appendChild(_badge(imgCount + ' img', 'badge-muted'));
    const hyg = (note.hygiene_notes || []).length;
    if (hyg) badges.appendChild(_badge('⚠ ' + hyg, 'badge-status'));
    entry.appendChild(badges);

    entry.onclick = () => _selectNote(note.rel_path);
    entry.onkeydown = (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _selectNote(note.rel_path); }
    };
    list.appendChild(entry);

    if (_selectedRel === null) _selectNote(note.rel_path);  // auto-select the first
}

/**
 * Make `rel` the active note: record it, sync the `.selected`/`aria-selected`
 * state across every sidebar entry (so styling + screen-reader state track the
 * selection), and render its detail pane. Falls back to the empty placeholder
 * if the note isn't in `_notes` (defensive — shouldn't happen).
 */
function _selectNote(rel) {
    _selectedRel = rel;
    $('refactor-note-list').querySelectorAll('.refactor-note-entry').forEach((el) => {
        const on = el.dataset.rel === rel;
        el.classList.toggle('selected', on);
        if (on) el.setAttribute('aria-selected', 'true');
        else el.removeAttribute('aria-selected');
    });
    _renderDetail(_notes.find((n) => n.rel_path === rel) || null);
}

// --- Detail pane ------------------------------------------------------------

function _renderMarkdown(container, text) {
    // marked is vendored locally (static/js/vendor/marked.min.js), but if it
    // failed to load we degrade to plain text rather than destroying the view —
    // mirrors vault.js::_renderAnswer. Output is sanitised before insertion.
    if (typeof marked !== 'undefined' && typeof marked.parse === 'function') {
        container.innerHTML = sanitiseHtml(marked.parse(text || ''));
    } else {
        container.textContent = text || '';
    }
}

function _renderDiff(diff) {
    // Render a unified diff with per-line colouring. Each line is its own span
    // with textContent (never innerHTML) so diff content — which contains the
    // user's own note text — can never inject markup.
    const pre = document.createElement('pre');
    pre.className = 'refactor-diff';
    if (!diff) {
        pre.textContent = '(no changes proposed)';
        return pre;
    }
    for (const line of diff.split('\n')) {
        const span = document.createElement('span');
        let cls = 'diff-ctx';
        if (line.startsWith('+') && !line.startsWith('+++')) cls = 'diff-add';
        else if (line.startsWith('-') && !line.startsWith('---')) cls = 'diff-del';
        else if (line.startsWith('@@')) cls = 'diff-hunk';
        span.className = cls;
        span.textContent = line + '\n';
        pre.appendChild(span);
    }
    return pre;
}

// One labelled column of the rendered ("Original" / "Proposed") side-by-side view.
function _mdColumn(label, text) {
    const col = document.createElement('div');
    col.className = 'refactor-md-col';
    const h = document.createElement('div');
    h.className = 'refactor-md-col-label';
    h.textContent = label;
    col.appendChild(h);
    const body = document.createElement('div');
    body.className = 'refactor-md-preview';
    _renderMarkdown(body, text);
    col.appendChild(body);
    return col;
}

// Render just the body sub-container for the current `_viewMode`: the unified
// diff (Diff) or the rendered Original-vs-Proposed columns (Rendered). Called
// both on initial render and when the view toggle flips, so it must fully
// replace the container's contents each time.
function _renderDetailBody(note, content) {
    _clear(content);
    if (_viewMode === 'diff') {
        content.appendChild(_renderDiff(note.diff));
        return;
    }
    const cols = document.createElement('div');
    cols.className = 'refactor-md-cols';
    cols.appendChild(_mdColumn('Original', note.original || ''));
    cols.appendChild(_mdColumn('Proposed (preview)', note.proposed || ''));
    content.appendChild(cols);
}

/**
 * Render the detail pane for `note` (or an empty-state placeholder when null):
 * header (path + proposed badge) → Rendered/Diff view toggle (swaps only the
 * body sub-container) → hygiene advisories → per-image rows.
 *
 * Re-rendered on note switch AND in place after an ignore toggle, so it first
 * disconnects the thumbnail IntersectionObserver: _clear() detaches the previous
 * render's <img> nodes but the observer would otherwise keep strong refs to them
 * until the next run. The cached blob URLs (_thumbCache) survive — they're only
 * revoked on a fresh run (_resetThumbs) — so re-observing the new imgs is cheap.
 */
function _renderDetail(note) {
    const detail = $('refactor-detail');
    _clear(detail);
    if (_thumbObserver) _thumbObserver.disconnect();
    if (!note) {
        const ph = document.createElement('div');
        ph.className = 'refactor-detail-placeholder muted';
        ph.textContent = 'Run a plan, then pick a note from the list to review it.';
        detail.appendChild(ph);
        return;
    }

    const head = document.createElement('div');
    head.className = 'refactor-detail-head';
    const path = document.createElement('code');
    path.className = 'refactor-note-path';
    path.textContent = note.rel_path;
    head.appendChild(path);
    if (note.changed) head.appendChild(_badge('proposed', 'badge-changed'));
    detail.appendChild(head);

    // View toggle (Rendered / Diff) — swaps only the body sub-container.
    const toggle = document.createElement('div');
    toggle.className = 'refactor-view-toggle';
    const btnR = document.createElement('button');
    btnR.className = 'btn btn-sm';
    btnR.textContent = 'Rendered';
    const btnD = document.createElement('button');
    btnD.className = 'btn btn-sm';
    btnD.textContent = 'Diff';
    const content = document.createElement('div');
    content.className = 'refactor-detail-content';
    function applyMode() {
        btnR.classList.toggle('btn-primary', _viewMode === 'rendered');
        btnR.classList.toggle('btn-outline', _viewMode !== 'rendered');
        btnD.classList.toggle('btn-primary', _viewMode === 'diff');
        btnD.classList.toggle('btn-outline', _viewMode !== 'diff');
        _renderDetailBody(note, content);
    }
    btnR.onclick = () => { _viewMode = 'rendered'; applyMode(); };
    btnD.onclick = () => { _viewMode = 'diff'; applyMode(); };
    toggle.appendChild(btnR);
    toggle.appendChild(btnD);
    detail.appendChild(toggle);
    detail.appendChild(content);
    applyMode();

    // Hygiene advisories.
    for (const h of note.hygiene_notes || []) {
        const hn = document.createElement('div');
        hn.className = 'refactor-hygiene';
        hn.textContent = `⚠ ${h.message}` + (h.line ? ` (line ${h.line})` : '');
        detail.appendChild(hn);
    }

    // Per-image rows.
    const imgsWrap = document.createElement('div');
    imgsWrap.className = 'refactor-images';
    for (const im of note.images || []) imgsWrap.appendChild(_renderImage(im, note));
    detail.appendChild(imgsWrap);
}

function _setClassificationBadge(head, label) {
    if (!head) return;
    head.querySelectorAll('.badge-handwritten, .badge-classify').forEach((b) => b.remove());
    if (!label) return;
    const badge = label === 'handwritten'
        ? _badge('handwritten — can’t OCR', 'badge-handwritten')
        : _badge(label, 'badge-classify');
    head.appendChild(badge);
}

function _renderImage(im, note) {
    const row = document.createElement('div');
    row.className = 'refactor-image' + (im.ignored ? ' refactor-image-ignored' : '');

    const head = document.createElement('div');
    head.className = 'refactor-image-head';
    const t = document.createElement('code');
    t.textContent = im.target || im.rel_path || '(unresolved)';
    head.appendChild(t);
    head.appendChild(_badge(im.status, 'badge-status'));
    _setClassificationBadge(head, im.classification);
    if (im.likely_table) {
        const b = _badge('likely table', 'badge-table');
        if (im.likely_table_reason) b.title = im.likely_table_reason;
        head.appendChild(b);
    }
    if (im.has_table) head.appendChild(_badge('table cached', 'badge-muted'));
    if (im.ignored) head.appendChild(_badge('ignored', 'badge-muted'));
    row.appendChild(head);

    const body = document.createElement('div');
    body.className = 'refactor-image-body';

    // Thumbnail, only for status 'ok' images (bytes present + within the cap).
    // dataless/too_big/missing are skipped so rendering never triggers a
    // surprise iCloud download or a huge transfer. Bytes load lazily via the
    // IntersectionObserver once the row nears the viewport.
    if (im.status === 'ok' && im.rel_path) {
        const img = document.createElement('img');
        img.className = 'refactor-thumb';
        img.alt = im.target || im.rel_path;
        img.dataset.rel = im.rel_path;
        body.appendChild(img);
        _ensureObserver().observe(img);
    }

    const side = document.createElement('div');
    side.className = 'refactor-image-side';

    const desc = document.createElement('div');
    desc.className = 'refactor-desc';
    desc.textContent = im.description || (im.extracted ? '' : '(not yet extracted)');
    side.appendChild(desc);

    // Per-image vision actions — the ONLY vision-calling controls in the panel.
    // Gated on a resolvable, existing target ('unresolved'/'missing' have no
    // file to read). dataless/too_big DO get buttons (server returns a clean
    // error if it can't). All vision buttons disable together during a call and
    // the calls are serialized server-side via _VISION_LOCK.
    if (im.rel_path && im.status !== 'unresolved' && im.status !== 'missing') {
        const actions = document.createElement('div');
        actions.className = 'refactor-image-actions';
        const tableBtn = document.createElement('button');
        tableBtn.className = 'btn btn-outline btn-sm';
        tableBtn.textContent = 'Extract table';
        const descBtn = document.createElement('button');
        descBtn.className = 'btn btn-outline btn-sm';
        descBtn.textContent = 'Re-describe';
        const classifyBtn = document.createElement('button');
        classifyBtn.className = 'btn btn-outline btn-sm';
        classifyBtn.textContent = 'Classify';
        const out = document.createElement('div');
        out.className = 'refactor-extract-out';
        const visionBtns = [tableBtn, descBtn, classifyBtn];
        tableBtn.onclick = () => _extract(im.rel_path, 'table', out, visionBtns);
        descBtn.onclick = () => _extract(im.rel_path, 'describe', out, visionBtns);
        classifyBtn.onclick = () => _extract(im.rel_path, 'classify', out, visionBtns, { im, note, head });
        actions.appendChild(tableBtn);
        actions.appendChild(descBtn);
        actions.appendChild(classifyBtn);

        // Ignore toggle — does NOT call vision; persists to the sticky sidecar.
        const ignoreBtn = document.createElement('button');
        ignoreBtn.className = 'btn btn-outline btn-sm';
        ignoreBtn.textContent = im.ignored ? 'Un-ignore' : 'Ignore';
        ignoreBtn.onclick = () => _toggleIgnore(im, note, ignoreBtn);
        actions.appendChild(ignoreBtn);

        // Archive (vault WRITE) — only for 'ok' images whose bytes we can read +
        // move. Opens an inline confirm in `out` describing the move first.
        if (im.status === 'ok' && !im.archived) {
            const archiveBtn = document.createElement('button');
            archiveBtn.className = 'btn btn-outline btn-sm refactor-archive-btn';
            archiveBtn.textContent = 'Archive…';
            archiveBtn.onclick = () => _confirmArchive(im, note, out, archiveBtn);
            actions.appendChild(archiveBtn);
        }

        side.appendChild(actions);
        side.appendChild(out);
    }
    if (im.archived) {
        const done = document.createElement('div');
        done.className = 'refactor-archived-note muted';
        done.textContent = '✓ archived — original moved out of the vault (restore from the Restore… dialog)';
        side.appendChild(done);
    }

    body.appendChild(side);
    row.appendChild(body);
    return row;
}

/**
 * Trigger one server-side vision pass for a single image and render the result.
 * Modes: 'table' (markdown table, may report suspect cells), 'describe' (fresh
 * description), 'classify' (one of printed-table / figure-diagram / handwritten
 * / photo / other). Disables all of the image's vision buttons for the duration.
 * All output goes through textContent — model output is untrusted.
 */
async function _extract(rel, mode, outEl, buttons, opts = {}) {
    buttons.forEach((b) => (b.disabled = true));
    _clear(outEl);
    const pending = document.createElement('div');
    pending.className = 'muted';
    pending.textContent = mode === 'table' ? 'Extracting table…'
        : mode === 'classify' ? 'Classifying…' : 'Re-describing…';
    outEl.appendChild(pending);
    try {
        const resp = await secureFetch('/api/refactor/extract-image', {
            method: 'POST',
            body: JSON.stringify({ rel, mode }),
        });
        const d = await resp.json();
        _clear(outEl);
        if (!resp.ok || d.error) {
            const err = document.createElement('div');
            err.className = 'refactor-extract-error';
            err.textContent = 'Error: ' + (d.error || `HTTP ${resp.status}`);
            outEl.appendChild(err);
            return;
        }
        if (mode === 'classify') {
            _renderClassifyResult(d, outEl, opts);
            return;
        }
        const suspect = d.suspect_cells || [];
        if (suspect.length) {
            const warn = document.createElement('div');
            warn.className = 'refactor-extract-warn';
            warn.textContent = `⚠ ${suspect.length} cell(s) differed between two reads — verify carefully.`;
            outEl.appendChild(warn);
        }
        const pre = document.createElement('pre');
        pre.className = 'refactor-extract-text';
        pre.textContent = d.text || '(empty result)';
        outEl.appendChild(pre);
        const meta = document.createElement('div');
        meta.className = 'muted';
        meta.textContent = (d.cached ? 'Cached to obsidian_cache.' : 'Not cached.') +
            (d.mode === 'table' ? ' (table)' : ' (description)');
        outEl.appendChild(meta);
    } catch (e) {
        logError('Refactor extract failed', e);
        _clear(outEl);
        const err = document.createElement('div');
        err.className = 'refactor-extract-error';
        err.textContent = 'Extraction failed (see console).';
        outEl.appendChild(err);
    } finally {
        buttons.forEach((b) => (b.disabled = false));
    }
}

function _renderClassifyResult(d, outEl, opts) {
    const label = d.label || 'other';
    if (opts.im) opts.im.classification = label;     // keep in-memory note in sync
    _setClassificationBadge(opts.head, label);        // live-update the row badge

    const line = document.createElement('div');
    line.className = 'refactor-classify-result';
    line.textContent = 'Classified as: ' + label + (d.cached ? ' (cached)' : '');
    outEl.appendChild(line);

    // A handwritten image can't be reliably OCR'd — offer a one-click ignore.
    if (label === 'handwritten' && opts.im && opts.note && !opts.im.ignored) {
        const ignoreBtn = document.createElement('button');
        ignoreBtn.className = 'btn btn-outline btn-sm';
        ignoreBtn.textContent = 'Ignore (handwritten)';
        ignoreBtn.onclick = () => _toggleIgnore(opts.im, opts.note, ignoreBtn);
        outEl.appendChild(ignoreBtn);
    }
}

/**
 * Add or remove the image from the sticky ignore-list (persisted to a sidecar
 * under obsidian_cache, NOT the vault). On success, flip the in-memory flag and
 * re-render the detail pane so the row greys out / un-greys and the Ignore
 * button label flips. The proposed body/diff are server-computed, so the
 * callout suppression for a newly-ignored image takes full effect on the next
 * Run Plan; the greying is immediate.
 */
async function _toggleIgnore(im, note, btn) {
    const action = im.ignored ? 'remove' : 'add';
    if (btn) btn.disabled = true;
    try {
        const resp = await secureFetch('/api/refactor/ignore', {
            method: 'POST',
            body: JSON.stringify({ rel: im.rel_path, action }),
        });
        const d = await resp.json();
        if (!resp.ok || d.error) {
            _status(d.error || `Ignore failed (HTTP ${resp.status}).`, true);
            return;
        }
        im.ignored = (action === 'add');
        if (note && note.rel_path === _selectedRel) _renderDetail(note);
    } catch (e) {
        logError('Refactor ignore toggle failed', e);
        _status('Ignore toggle failed (see console).', true);
    } finally {
        if (btn) btn.disabled = false;
    }
}

// --- Phase 2: apply (callout-only batch write) ------------------------------

function _toggleApprove(rel, checked) {
    if (checked) _approved.add(rel); else _approved.delete(rel);
    _updateApplyButton();
}

function _updateApplyButton() {
    const btn = $('refactor-apply-btn');
    if (!btn) return;
    const n = _approved.size;
    btn.textContent = `Apply approved (${n})`;
    btn.disabled = n === 0;
}

/** Open the apply confirmation modal listing the approved notes. */
export function openApply() {
    if (!_approved.size) return;
    const list = $('refactor-apply-list');
    _clear(list);
    for (const rel of _approved) {
        const li = document.createElement('li');
        li.textContent = rel;
        list.appendChild(li);
    }
    $('refactor-apply-modal-status').textContent = '';
    $('refactor-apply-confirm').disabled = false;
    openModal('refactor-apply-modal');
}

/** Write the approved callout-only proposals to the vault. */
export async function confirmApply() {
    const notes = [];
    for (const rel of _approved) {
        const n = _notes.find((x) => x.rel_path === rel);
        if (n) notes.push({ rel, content_sha256: n.content_sha256, proposed_sha256: n.proposed_sha256 });
    }
    if (!notes.length) { closeModal('refactor-apply-modal'); return; }
    const status = $('refactor-apply-modal-status');
    const btn = $('refactor-apply-confirm');
    btn.disabled = true;
    status.textContent = 'Writing to the vault…';
    try {
        const resp = await secureFetch('/api/refactor/apply', {
            method: 'POST',
            body: JSON.stringify({ scope_subdir: _scope, confirm: true, notes }),
        });
        const d = await resp.json();
        if (!resp.ok || d.error) {
            status.textContent = 'Error: ' + (d.error || `HTTP ${resp.status}`);
            btn.disabled = false;
            return;
        }
        const results = d.results || [];
        let applied = 0;
        for (const r of results) {
            if (r.status === 'applied') { applied++; _markApplied(r.rel); }
            _activity(`apply ${r.rel}: ${r.status}` + (r.message ? ' — ' + r.message : ''),
                      r.status !== 'applied');
        }
        status.textContent = `Applied ${applied} of ${results.length}.`;
        setTimeout(() => closeModal('refactor-apply-modal'), 1100);
    } catch (e) {
        logError('Refactor apply failed', e);
        status.textContent = 'Apply failed (see console).';
        btn.disabled = false;
    }
}

/** After a note is written, sync its in-memory state so a follow-up archive on
 * the same note passes the stale-diff guard (on-disk now == proposed). */
function _markApplied(rel) {
    const n = _notes.find((x) => x.rel_path === rel);
    if (n) { n.applied = true; n.content_sha256 = n.proposed_sha256; }
    _approved.delete(rel);
    _updateApplyButton();
    let entry = null;
    $('refactor-note-list').querySelectorAll('.refactor-note-entry').forEach((el) => {
        if (el.dataset.rel === rel) entry = el;
    });
    if (entry) {
        const cb = entry.querySelector('.refactor-approve-cb');
        if (cb) { cb.checked = false; cb.disabled = true; }
        const badges = entry.querySelector('.refactor-entry-badges');
        if (badges && !badges.querySelector('.badge-applied')) {
            const b = _badge('applied', 'badge-changed');
            b.classList.add('badge-applied');
            badges.appendChild(b);
        }
    }
}

// --- Phase 2: per-image archive ---------------------------------------------

/** Render an inline confirm for archiving one image (a vault write). */
function _confirmArchive(im, note, out, btn) {
    _clear(out);
    const box = document.createElement('div');
    box.className = 'refactor-archive-confirm';
    const msg = document.createElement('div');
    msg.textContent = 'Move the full-res original OUT of the vault to the archive folder and '
        + 'leave a ~384px thumbnail here? The embed will point at the thumbnail. '
        + 'Reversible from Restore.';
    box.appendChild(msg);
    const row = document.createElement('div');
    row.className = 'refactor-image-actions';
    const yes = document.createElement('button');
    yes.className = 'btn btn-primary btn-sm';
    yes.textContent = 'Archive (write)';
    yes.onclick = () => _doArchive(im, note, out);
    const no = document.createElement('button');
    no.className = 'btn btn-outline btn-sm';
    no.textContent = 'Cancel';
    no.onclick = () => _clear(out);
    row.appendChild(yes);
    row.appendChild(no);
    box.appendChild(row);
    out.appendChild(box);
}

async function _doArchive(im, note, out) {
    _clear(out);
    const pending = document.createElement('div');
    pending.className = 'muted';
    pending.textContent = 'Archiving…';
    out.appendChild(pending);
    try {
        const resp = await secureFetch('/api/refactor/archive', {
            method: 'POST',
            body: JSON.stringify({
                scope_subdir: _scope, confirm: true,
                note_rel: note.rel_path, image_rel: im.rel_path,
                content_sha256: note.content_sha256,
            }),
        });
        const d = await resp.json();
        _clear(out);
        if (!resp.ok || d.error || !d.ok) {
            const err = document.createElement('div');
            err.className = 'refactor-extract-error';
            err.textContent = (d.shared ? 'Shared image: ' : 'Error: ')
                + (d.message || d.error || `HTTP ${resp.status}`);
            out.appendChild(err);
            return;
        }
        im.archived = true;
        if (d.note_hash_after) note.content_sha256 = d.note_hash_after;
        _activity(`archived ${im.rel_path} → ${d.archive_rel}`);
        if (d.warning) _activity('WARNING: ' + d.warning, true);
        if (note.rel_path === _selectedRel) _renderDetail(note);
    } catch (e) {
        logError('Refactor archive failed', e);
        _clear(out);
        const err = document.createElement('div');
        err.className = 'refactor-extract-error';
        err.textContent = 'Archive failed (see console).';
        out.appendChild(err);
    }
}

// --- Phase 2: restore -------------------------------------------------------

/** Open the restore modal and load the manifest. */
export async function openRestore() {
    _clear($('refactor-restore-list'));
    $('refactor-restore-status').textContent = 'Loading…';
    openModal('refactor-restore-modal');
    await _loadManifest();
}

async function _loadManifest() {
    const list = $('refactor-restore-list');
    _clear(list);
    const status = $('refactor-restore-status');
    try {
        const resp = await secureFetch('/api/refactor/manifest');
        const d = await resp.json();
        if (!resp.ok || d.error) {
            status.textContent = 'Error: ' + (d.error || `HTTP ${resp.status}`);
            return;
        }
        const ops = (d.ops || []).slice().reverse();   // newest first
        status.textContent = ops.length ? '' : 'No vault changes recorded yet.';
        for (const op of ops) list.appendChild(_renderOp(op));
    } catch (e) {
        logError('Refactor manifest load failed', e);
        status.textContent = 'Failed to load manifest (see console).';
    }
}

function _renderOp(op) {
    const row = document.createElement('div');
    row.className = 'refactor-restore-op';
    const main = document.createElement('div');
    const strong = document.createElement('strong');
    strong.textContent = op.kind === 'archive_image' ? 'Archived image' : 'Applied note';
    main.appendChild(strong);
    const path = document.createElement('span');
    path.className = 'muted';
    path.textContent = ' ' + (op.note_rel || '') + (op.image_rel ? ' · ' + op.image_rel : '');
    main.appendChild(path);
    row.appendChild(main);
    const meta = document.createElement('div');
    meta.className = 'muted';
    meta.textContent = (op.ts || '') + ' · ' + (op.state || '');
    row.appendChild(meta);
    const btn = document.createElement('button');
    btn.className = 'btn btn-outline btn-sm';
    if (op.state === 'reverted') { btn.textContent = 'reverted'; btn.disabled = true; }
    else { btn.textContent = 'Revert'; btn.onclick = () => _revertOp(op.id, btn); }
    row.appendChild(btn);
    return row;
}

async function _revertOp(opId, btn) {
    if (btn) btn.disabled = true;
    const status = $('refactor-restore-status');
    status.textContent = 'Reverting…';
    try {
        const resp = await secureFetch('/api/refactor/restore', {
            method: 'POST', body: JSON.stringify({ op_id: opId }),
        });
        const d = await resp.json();
        if (!resp.ok || d.error) {
            status.textContent = 'Error: ' + (d.error || `HTTP ${resp.status}`);
            if (btn) btn.disabled = false;
            return;
        }
        const r = (d.results || [])[0] || {};
        status.textContent = `${r.status || 'done'}${r.message ? ': ' + r.message : ''}`;
        await _loadManifest();
    } catch (e) {
        logError('Refactor revert failed', e);
        status.textContent = 'Revert failed (see console).';
        if (btn) btn.disabled = false;
    }
}

/** Revert every non-reverted op (newest first, server-side). */
export async function revertAll() {
    const status = $('refactor-restore-status');
    const btn = $('refactor-restore-all');
    if (btn) btn.disabled = true;
    status.textContent = 'Reverting all…';
    try {
        const resp = await secureFetch('/api/refactor/restore', {
            method: 'POST', body: JSON.stringify({ all: true }),
        });
        const d = await resp.json();
        if (!resp.ok || d.error) {
            status.textContent = 'Error: ' + (d.error || `HTTP ${resp.status}`);
            return;
        }
        status.textContent = `Reverted ${d.reverted || 0} change(s).`;
        await _loadManifest();
    } catch (e) {
        logError('Refactor revert-all failed', e);
        status.textContent = 'Revert all failed (see console).';
    } finally {
        if (btn) btn.disabled = false;
    }
}

// Pre-fill the scope input from saved config on first tab init.
export function initRefactorTab(config) {
    const el = $('refactor-scope');
    if (el && config && config.refactor_scope_subdir && !el.value) {
        el.value = config.refactor_scope_subdir;
    }
}
