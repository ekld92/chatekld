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
import { secureFetch, consumeSSE, logError, safeJson } from './api.js';
import { setStatusA11y, taskBegin, taskEnd, sanitiseHtml, openModal, closeModal, copyToClipboard, renderExampleChips, confirmInline } from './ui.js';

let _running = false;

// Streamed note frames (in arrival order) + the currently-selected note + the
// detail view mode. Reset at the start of each run.
let _notes = [];
let _selectedRel = null;
let _viewMode = 'rendered'; // 'rendered' | 'diff'

// Phase 2 (vault writes). _approved holds rel_paths the user ticked for the
// callout-only batch apply; _normApproved is the parallel opt-in set for the
// deterministic formatting fix (per-note, NOT select-all); _scope is the scope
// the current plan ran against (echoed back to the write endpoints so they
// target the same sub-folder).
let _approved = new Set();
let _normApproved = new Set();
let _scope = '';

// LLM-action state (requests b/c/e/f). _sectionsCache: rel -> [section] (lazy via
// /sections); _selectedSection: rel -> section_index string ('' = whole note),
// preserved across detail re-renders of the same note.
let _sectionsCache = new Map();
let _selectedSection = new Map();
// Free-prompt instruction text, preserved per note across detail re-renders.
let _customInstruction = new Map();
// Per-image OCR-inclusion panel: debounce timer for the single-note re-analyze,
// and the panel's open/closed state preserved across re-renders.
let _reanalyzeTimer = null;
// Stale-response guards for the single-note re-analyze (/api/refactor/note). The
// debounce only bounds *scheduling*, not *concurrency*: two quick toggles could
// leave two fetches in flight, and an older one resolving last would clobber the
// note's fresh proposed/hashes (last-writer-wins). `_reanalyzeSeq` is a monotonic
// token — a response whose token is stale is ignored; `_reanalyzeAbort` cancels
// the prior in-flight request outright.
let _reanalyzeSeq = 0;
let _reanalyzeAbort = null;
let _inclPanelOpen = true;

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
const _thumbInflight = new Map();   // rel -> Promise<objectURL|null>

async function _loadThumb(img, rel) {
    if (_thumbCache.has(rel)) { img.src = _thumbCache.get(rel); return; }
    // Item 3.8: share ONE in-flight fetch per rel. The same image renders in
    // several rows (inclusion panel + image list), and each visible <img>
    // fired its own full-size fetch + object URL before the first resolved —
    // megabytes of duplicate reads per note render. All callers now await the
    // same promise; the winner populates the cache once.
    let p = _thumbInflight.get(rel);
    if (!p) {
        p = (async () => {
            const resp = await secureFetch('/api/refactor/image?rel=' + encodeURIComponent(rel));
            if (!resp.ok) return null;
            const blob = await resp.blob();
            const url = URL.createObjectURL(blob);
            _objectUrls.push(url);
            _thumbCache.set(rel, url);
            return url;
        })().finally(() => _thumbInflight.delete(rel));
        _thumbInflight.set(rel, p);
    }
    try {
        const url = await p;
        if (url) img.src = url;
        else img.alt = 'image unavailable';
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
        const d = await safeJson(r);
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
    // Lock the scope-wide strip toggle for the duration: it re-runs the plan on
    // change, and a toggle landing mid-run would be silently dropped by the
    // `_running` guard above, leaving the checkbox disagreeing with the previews.
    { const sp = $('refactor-strip-preamble'); if (sp) sp.disabled = true; }
    taskBegin('refactor-plan');
    _status('');
    _resetThumbs();   // free prior run's blob URLs + observer before clearing DOM
    _notes = [];
    _selectedRel = null;
    _approved = new Set();
    _normApproved = new Set();
    _sectionsCache = new Map();
    _selectedSection = new Map();
    _customInstruction = new Map();
    if (_reanalyzeTimer) { clearTimeout(_reanalyzeTimer); _reanalyzeTimer = null; }
    _updateApplyButton();
    _updateNormalizeButton();
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
        await consumeSSE('/api/refactor/plan', {
            method: 'POST',
            body: JSON.stringify(payload),
        }, {
            onInfo: (info) => _activity(info),
            onOther: (evt) => {
                if (evt.note) _addNote(evt.note);
                else if (evt.refactor) _renderSummary(evt.refactor);
            },
            onError: (err) => {
                _status(err, true);
                _activity('ERROR: ' + err);
            }
        });

    } catch (e) {
        logError('Refactor plan failed', e);
        _status('Refactor plan failed (see console).', true);
    } finally {
        _running = false;
        $('refactor-run-btn').disabled = false;
        { const sp = $('refactor-strip-preamble'); if (sp) sp.disabled = false; }
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
        `${summary.normalize_changed_count} with formatting fixes`,
        `${summary.not_extracted_count} not yet extracted`,
        `${summary.likely_table_count} likely table(s)`,
    ];
    if (summary.handwritten_hidden_count) parts.push(`${summary.handwritten_hidden_count} handwritten OCR hidden`);
    else if (summary.handwritten_count) parts.push(`${summary.handwritten_count} handwritten`);
    if (summary.stripped_count) parts.push(`${summary.stripped_count} stripped`);
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
    // Track 6c roving tabindex: only the SELECTED option is a tabstop —
    // per-row tabstops made Tab walk every analyzed note (hundreds on a big
    // scope) before reaching the detail pane. Arrow keys (below) move within
    // the listbox; _selectNote keeps exactly one row at tabindex 0.
    entry.tabIndex = -1;

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
    if (note.normalize_changed) badges.appendChild(_badge('fmt', 'badge-muted'));
    const imgCount = (note.images || []).length;
    if (imgCount) badges.appendChild(_badge(imgCount + ' img', 'badge-muted'));
    const hyg = (note.hygiene_notes || []).length;
    if (hyg) badges.appendChild(_badge('⚠ ' + hyg, 'badge-status'));
    entry.appendChild(badges);

    entry.onclick = () => _selectNote(note.rel_path);
    entry.onkeydown = (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _selectNote(note.rel_path); return; }
        // Listbox arrow-key navigation: move focus + selection between options.
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'Home' || e.key === 'End') {
            const items = Array.from($('refactor-note-list').querySelectorAll('.refactor-note-entry'));
            const i = items.indexOf(entry);
            let n;
            if (e.key === 'ArrowDown') n = (i + 1) % items.length;
            else if (e.key === 'ArrowUp') n = (i - 1 + items.length) % items.length;
            else if (e.key === 'Home') n = 0;
            else n = items.length - 1;
            e.preventDefault();
            const t = items[n];
            if (t) { t.focus(); _selectNote(t.dataset.rel); }
        }
    };
    list.appendChild(entry);
    _updateNormalizeButton();

    if (_selectedRel === null) _selectNote(note.rel_path);  // auto-select the first
}

/**
 * Make `rel` the active note: record it, sync the `.selected`/`aria-selected`
 * state across every sidebar entry (so styling + screen-reader state track the
 * selection), and render its detail pane. Falls back to the empty placeholder
 * if the note isn't in `_notes` (defensive — shouldn't happen).
 */
function _selectNote(rel) {
    // Drop any pending/in-flight re-analyze for the previously-selected note so a
    // late response can't mutate it after the user moved on.
    if (_reanalyzeTimer) { clearTimeout(_reanalyzeTimer); _reanalyzeTimer = null; }
    if (_reanalyzeAbort) { _reanalyzeAbort.abort(); _reanalyzeAbort = null; }
    _reanalyzeSeq++;   // invalidate any token already issued
    _selectedRel = rel;
    $('refactor-note-list').querySelectorAll('.refactor-note-entry').forEach((el) => {
        const on = el.dataset.rel === rel;
        el.classList.toggle('selected', on);
        el.tabIndex = on ? 0 : -1;   // roving tabindex (6c)
        if (on) el.setAttribute('aria-selected', 'true');
        else el.removeAttribute('aria-selected');
    });
    const selected = _notes.find((n) => n.rel_path === rel) || null;
    _renderDetail(selected);
    // Item 3.4: a note applied earlier in this session still holds its
    // PRE-apply original/proposed bodies — refresh its frame once from
    // /api/refactor/note so the panes show on-disk truth, not a stale diff.
    if (selected && selected._needsPostApplyRefresh) {
        selected._needsPostApplyRefresh = false;
        _reanalyzeNote(selected);
    }
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
    if (_viewMode === 'normalize') {
        content.appendChild(_renderDiff(note.normalize_diff));
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

    // A note without a callout diff but with a formatting fix shouldn't open in
    // the (empty) callout-diff view; a 'normalize' mode is only valid when the
    // note actually has a formatting fix.
    if (_viewMode === 'normalize' && !note.normalize_changed) _viewMode = 'rendered';

    // View toggle (Rendered / Diff / Formatting fix) — swaps only the body
    // sub-container. The Formatting-fix button is shown only when the
    // deterministic normalizer would change this note.
    const toggle = document.createElement('div');
    toggle.className = 'refactor-view-toggle';
    const btnR = document.createElement('button');
    btnR.className = 'btn btn-sm';
    btnR.textContent = 'Rendered';
    const btnD = document.createElement('button');
    btnD.className = 'btn btn-sm';
    btnD.textContent = 'Diff';
    let btnN = null;
    if (note.normalize_changed) {
        btnN = document.createElement('button');
        btnN.className = 'btn btn-sm';
        btnN.textContent = 'Formatting fix';
        btnN.title = 'Deterministic blank-line / whitespace normalization preview';
    }
    const content = document.createElement('div');
    content.className = 'refactor-detail-content';
    function applyMode() {
        btnR.classList.toggle('btn-primary', _viewMode === 'rendered');
        btnR.classList.toggle('btn-outline', _viewMode !== 'rendered');
        btnD.classList.toggle('btn-primary', _viewMode === 'diff');
        btnD.classList.toggle('btn-outline', _viewMode !== 'diff');
        if (btnN) {
            btnN.classList.toggle('btn-primary', _viewMode === 'normalize');
            btnN.classList.toggle('btn-outline', _viewMode !== 'normalize');
        }
        _renderDetailBody(note, content);
    }
    btnR.onclick = () => { _viewMode = 'rendered'; applyMode(); };
    btnD.onclick = () => { _viewMode = 'diff'; applyMode(); };
    if (btnN) btnN.onclick = () => { _viewMode = 'normalize'; applyMode(); };
    toggle.appendChild(btnR);
    toggle.appendChild(btnD);
    if (btnN) toggle.appendChild(btnN);
    // `content` is populated here but toggle+content are appended LOWER (below the
    // action controls) so the approve checkbox / Review / LLM actions stay visible
    // without scrolling past the tall ORIGINAL/PROPOSED preview.
    applyMode();

    // (a) Per-note opt-in for the deterministic formatting fix (default OFF),
    // mirroring Apply's per-note approval. Only shown when the normalizer would
    // change this note; ticking it adds the note to the "Fix formatting" batch.
    if (note.normalize_changed) {
        const fmtWrap = document.createElement('div');
        fmtWrap.className = 'refactor-norm-approve';
        const lbl = document.createElement('label');
        lbl.className = 'checkbox-row';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = _normApproved.has(note.rel_path);
        cb.onchange = () => _toggleNormApprove(note.rel_path, cb.checked);
        lbl.appendChild(cb);
        lbl.appendChild(document.createTextNode(' Approve deterministic formatting fix for this note'));
        fmtWrap.appendChild(lbl);
        detail.appendChild(fmtWrap);
    }

    // Opt-in LLM prose/formatting review — one advisory call per note. Output is
    // model text (untrusted) rendered through marked + sanitiseHtml.
    const reviewWrap = document.createElement('div');
    reviewWrap.className = 'refactor-review';
    const reviewBtn = document.createElement('button');
    reviewBtn.className = 'btn btn-outline btn-sm';
    reviewBtn.textContent = 'Review prose (LLM)';
    reviewBtn.title = 'Run one advisory LLM pass for formatting / unclear-sentence suggestions (uses your configured model)';
    const reviewOut = document.createElement('div');
    reviewOut.className = 'refactor-review-out';
    reviewBtn.onclick = () => _reviewNote(note, reviewBtn, reviewOut);
    reviewWrap.appendChild(reviewBtn);
    reviewWrap.appendChild(reviewOut);
    detail.appendChild(reviewWrap);

    // On-demand LLM actions (requests b/c/e) with optional section scope (f).
    _renderLlmActions(note, detail);

    // Per-image OCR-inclusion panel (request d) — choose which attached images'
    // OCR is inlined into the note; sits above the preview so it's discoverable.
    _renderImageInclusionPanel(note, detail);

    // The ORIGINAL/PROPOSED preview (tall) is appended AFTER the action controls
    // above, so Apply-approve / Fix-formatting-approve / Review / LLM actions are
    // visible immediately and the long preview scrolls within its own bounded box.
    detail.appendChild(toggle);
    detail.appendChild(content);

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

// --- Per-image OCR-inclusion panel (request d) ------------------------------

// "Included" = this image's OCR callout will be inlined by the planner: it has
// cached OCR text, isn't ignored, and isn't auto-hidden as handwritten.
function _imgIncluded(im) {
    return !!im.description && !im.ignored && !im.handwritten_hidden;
}
// Eligible to toggle: there is OCR text to include and a real resolved file.
function _imgInclEligible(im) {
    return !!im.description && !!im.rel_path
        && im.status !== 'unresolved' && im.status !== 'missing';
}

async function _postIgnore(rel, action) {
    try {
        const r = await secureFetch('/api/refactor/ignore', {
            method: 'POST', body: JSON.stringify({ rel, action }) });
        const d = await safeJson(r);
        return r.ok && !d.error;
    } catch (e) { logError('Refactor ignore post failed', e); return false; }
}

async function _postFlag(rel, flag, action) {
    try {
        const r = await secureFetch('/api/refactor/flag', {
            method: 'POST', body: JSON.stringify({ rel, flag, action }) });
        const d = await safeJson(r);
        return r.ok && !d.error;
    } catch (e) { logError('Refactor flag post failed', e); return false; }
}

function _updateInclSummary(note, summary) {
    const imgs = (note.images || []).filter((im) => im.rel_path);
    const inc = imgs.filter(_imgIncluded).length;
    const elig = imgs.filter(_imgInclEligible).length;
    summary.firstChild.textContent =
        `Images — inclure l’OCR (${inc}/${elig} incluses, ${imgs.length} au total)`;
}

/**
 * Persist one image's OCR inclusion (exclude ⇒ ignore-list; include ⇒ un-ignore,
 * and force-keep if handwritten), update local state optimistically, then
 * schedule a debounced single-note re-analyze so the preview + hashes refresh.
 */
async function _setImageInclusion(im, note, included, els) {
    try {
        if (!included) {
            if (!im.ignored) { if (!await _postIgnore(im.rel_path, 'add')) throw 0; im.ignored = true; }
        } else {
            if (im.ignored) { if (!await _postIgnore(im.rel_path, 'remove')) throw 0; im.ignored = false; }
            if (im.handwritten && !im.kept_handwritten) {
                if (!await _postFlag(im.rel_path, 'keep_handwritten', 'add')) throw 0;
                im.kept_handwritten = true;
            }
        }
        im.handwritten_hidden = im.handwritten && !im.kept_handwritten;
        if (els) {
            els.row.classList.toggle('refactor-incl-excluded', !_imgIncluded(im));
            els.cb.checked = _imgIncluded(im);
            _updateInclSummary(note, els.summary);
        }
        _scheduleReanalyze(note, els && els.status);
    } catch (e) {
        // Item 3.8: NO blanket rollback. Each flag above advances only after
        // its own POST succeeded, so on a partial failure the in-memory flags
        // already reflect exactly what the server accepted — the old rollback
        // reset flags whose server change HAD applied, silently desyncing UI
        // from server until the next full plan. Resync the visible state from
        // the (accurate) flags and say so.
        im.handwritten_hidden = im.handwritten && !im.kept_handwritten;
        if (els) {
            els.cb.checked = _imgIncluded(im);
            els.row.classList.toggle('refactor-incl-excluded', !_imgIncluded(im));
            _updateInclSummary(note, els.summary);
        }
        _status('Could not fully update OCR inclusion — the shown state reflects what was saved.', true);
    }
}

let _bulkInclusionBusy = false;

async function _bulkInclusion(note, included, status) {
    // Item 3.8: reentry guard + failure accounting. The unguarded version let
    // a double-click run two interleaved bulk sweeps over the same flags, and
    // a failed POST was silently skipped with no user-visible trace.
    if (_bulkInclusionBusy) return;
    _bulkInclusionBusy = true;
    let failures = 0;
    try {
        const targets = (note.images || [])
            .filter(_imgInclEligible).filter((im) => _imgIncluded(im) !== included);
        if (!targets.length) return;
        for (const im of targets) {
            if (!included) {
                if (!im.ignored) {
                    if (await _postIgnore(im.rel_path, 'add')) im.ignored = true;
                    else failures++;
                }
            } else {
                if (im.ignored) {
                    if (await _postIgnore(im.rel_path, 'remove')) im.ignored = false;
                    else failures++;
                }
                if (im.handwritten && !im.kept_handwritten) {
                    if (await _postFlag(im.rel_path, 'keep_handwritten', 'add')) im.kept_handwritten = true;
                    else failures++;
                }
            }
            im.handwritten_hidden = im.handwritten && !im.kept_handwritten;
        }
        if (failures) {
            _status(`${failures} image(s) n'ont pas pu être mises à jour — état affiché = état sauvegardé.`, true);
        }
        _scheduleReanalyze(note, status);
    } catch (e) {
        logError('Refactor bulk inclusion failed', e);
        _status('Mise à jour groupée interrompue — état affiché = état sauvegardé.', true);
    } finally {
        _bulkInclusionBusy = false;
    }
}

function _scheduleReanalyze(note, statusEl) {
    if (_reanalyzeTimer) clearTimeout(_reanalyzeTimer);
    if (statusEl) statusEl.textContent = ' mise à jour de l’aperçu…';
    _reanalyzeTimer = setTimeout(() => { _reanalyzeTimer = null; _reanalyzeNote(note); }, 400);
}

/** Re-analyze ONE note server-side and refresh its in-memory state + the UI. */
async function _reanalyzeNote(note) {
    // Cancel any prior in-flight re-analyze and claim a fresh token, so only the
    // latest request can apply its result (see the _reanalyzeSeq/_reanalyzeAbort
    // comment) — otherwise a slower older response could overwrite newer state.
    if (_reanalyzeAbort) _reanalyzeAbort.abort();
    _reanalyzeAbort = new AbortController();
    const mySeq = ++_reanalyzeSeq;
    const signal = _reanalyzeAbort.signal;
    try {
        const r = await secureFetch('/api/refactor/note', {
            method: 'POST', signal,
            body: JSON.stringify({ rel: note.rel_path, scope_subdir: _scope }) });
        const d = await safeJson(r);
        if (mySeq !== _reanalyzeSeq) return;      // a newer re-analyze superseded this one
        if (!r.ok || d.error || !d.note) { _status(d.error || 'Re-analyze failed.', true); return; }
        Object.assign(note, d.note);              // fresh proposed / hashes / images
        _refreshSidebarEntry(note);
        _updateApplyButton();
        _updateNormalizeButton();
        if (_selectedRel === note.rel_path) _renderDetail(note);
    } catch (e) {
        if (e && e.name === 'AbortError') return; // superseded/cancelled — not an error
        logError('Refactor note re-analyze failed', e);
        _status('Re-analyze failed (see console).', true);
    }
}

/** Sync a note's sidebar entry (badges + approve checkbox) after a re-analyze. */
function _refreshSidebarEntry(note) {
    let entry = null;
    $('refactor-note-list').querySelectorAll('.refactor-note-entry').forEach((el) => {
        if (el.dataset.rel === note.rel_path) entry = el;
    });
    if (!entry) return;
    const badges = entry.querySelector('.refactor-entry-badges');
    if (badges) {
        _clear(badges);
        if (note.changed) badges.appendChild(_badge('proposed', 'badge-changed'));
        if (note.normalize_changed) badges.appendChild(_badge('fmt', 'badge-muted'));
        const imgCount = (note.images || []).length;
        if (imgCount) badges.appendChild(_badge(imgCount + ' img', 'badge-muted'));
        const hyg = (note.hygiene_notes || []).length;
        if (hyg) badges.appendChild(_badge('⚠ ' + hyg, 'badge-status'));
    }
    // The approve checkbox exists only for a changed note — add/remove to match.
    let cb = entry.querySelector('.refactor-approve-cb');
    if (note.changed && !cb) {
        cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.className = 'refactor-approve-cb';
        cb.title = 'Approve this note for Apply';
        cb.setAttribute('aria-label', 'Approve ' + note.rel_path + ' for apply');
        cb.onclick = (e) => { e.stopPropagation(); _toggleApprove(note.rel_path, cb.checked); };
        entry.insertBefore(cb, entry.firstChild);
    } else if (!note.changed && cb) {
        cb.remove();
        _approved.delete(note.rel_path);
        _updateApplyButton();
    } else if (cb) {
        cb.checked = _approved.has(note.rel_path);
    }
}

function _renderInclRow(im, note, summary, status) {
    const row = document.createElement('label');
    row.className = 'refactor-incl-row' + (_imgIncluded(im) ? '' : ' refactor-incl-excluded');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'refactor-incl-cb';
    cb.checked = _imgIncluded(im);
    cb.disabled = !_imgInclEligible(im);
    cb.onchange = () => _setImageInclusion(im, note, cb.checked, { row, cb, summary, status });
    row.appendChild(cb);

    if (im.status === 'ok' && im.rel_path) {
        const img = document.createElement('img');
        img.className = 'refactor-incl-thumb';
        img.alt = '';
        img.dataset.rel = im.rel_path;
        row.appendChild(img);
        _ensureObserver().observe(img);
    }

    const txt = document.createElement('div');
    txt.className = 'refactor-incl-text';
    const top = document.createElement('div');
    top.className = 'refactor-incl-top';
    const name = document.createElement('code');
    name.textContent = im.target || im.rel_path;
    top.appendChild(name);
    if (!im.description) top.appendChild(_badge('not extracted', 'badge-status'));
    if (im.handwritten) top.appendChild(_badge('manuscrit — OCR peu fiable', 'badge-handwritten'));
    if (im.likely_table) top.appendChild(_badge('likely table', 'badge-table'));
    txt.appendChild(top);
    const snip = document.createElement('div');
    snip.className = 'refactor-incl-snippet muted';
    snip.textContent = im.description
        ? (im.description.length > 160 ? im.description.slice(0, 160) + '…' : im.description)
        : '(pas encore extrait — utilise Extract / Re-describe dans la liste d’images en bas)';
    txt.appendChild(snip);
    row.appendChild(txt);
    return row;
}

/**
 * Collapsible panel listing every attached image of the note with an
 * include-OCR checkbox (opt-out: checked = inlined), plus Tout inclure / Tout
 * exclure. Toggling persists to the ignore-list / keep_handwritten flag and
 * debounced-re-analyzes this one note so the preview + Apply hashes refresh.
 */
function _renderImageInclusionPanel(note, detail) {
    const imgs = (note.images || []).filter((im) => im.rel_path);
    if (!imgs.length) return;
    const panel = document.createElement('details');
    panel.className = 'refactor-incl-panel';
    panel.open = _inclPanelOpen;
    panel.ontoggle = () => { _inclPanelOpen = panel.open; };

    const summary = document.createElement('summary');
    summary.appendChild(document.createTextNode(''));   // text set by _updateInclSummary
    _updateInclSummary(note, summary);
    panel.appendChild(summary);

    const bar = document.createElement('div');
    bar.className = 'refactor-image-actions';
    const status = document.createElement('span');
    status.className = 'muted refactor-incl-status';
    const allIn = _mkBtn('Tout inclure');
    const allOut = _mkBtn('Tout exclure');
    allIn.onclick = () => _bulkInclusion(note, true, status);
    allOut.onclick = () => _bulkInclusion(note, false, status);
    bar.appendChild(allIn);
    bar.appendChild(allOut);
    bar.appendChild(status);
    panel.appendChild(bar);

    const list = document.createElement('div');
    list.className = 'refactor-incl-list';
    for (const im of imgs) list.appendChild(_renderInclRow(im, note, summary, status));
    panel.appendChild(list);
    detail.appendChild(panel);
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
    // Zero-vision handwritten auto-hide: when the heuristic (or a cached label)
    // marks the image handwritten and it isn't force-kept, its OCR callout is
    // suppressed — surface that distinctly from a manual 'ignored'.
    if (im.handwritten_hidden) {
        const b = _badge('OCR hidden (handwritten)', 'badge-handwritten');
        if (im.likely_handwritten_reason) b.title = im.likely_handwritten_reason;
        head.appendChild(b);
    } else if (im.likely_handwritten && im.kept_handwritten) {
        head.appendChild(_badge('handwritten — kept', 'badge-muted'));
    }
    if (im.metadata_stripped) head.appendChild(_badge('metadata stripped', 'badge-muted'));
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

    // (Per-image OCR inclusion now lives in the dedicated inclusion panel above —
    // _renderImageInclusionPanel — not here, to give a single overview/select UI.)

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

        // Strip-metadata toggle — only meaningful when there's a cached
        // description to trim. Persists the per-image 'strip' flag (no vision);
        // the stripped callout body materializes on the next Run Plan.
        if (im.description) {
            const stripBtn = document.createElement('button');
            stripBtn.className = 'btn btn-outline btn-sm';
            stripBtn.textContent = im.metadata_stripped ? 'Keep metadata' : 'Strip metadata';
            stripBtn.title = 'Drop the "This image is…/Transcribed Text:" preamble from this image’s callout';
            stripBtn.onclick = () => _toggleFlag(im, note, stripBtn, 'strip');
            actions.appendChild(stripBtn);
        }

        // Keep-anyway override — only when the image looks handwritten. Forces
        // the OCR callout back in despite the zero-vision auto-hide.
        if (im.handwritten) {
            const keepBtn = document.createElement('button');
            keepBtn.className = 'btn btn-outline btn-sm';
            keepBtn.textContent = im.kept_handwritten ? 'Re-hide OCR' : 'Keep anyway';
            keepBtn.title = 'Force this handwritten image’s OCR callout to be inlined';
            keepBtn.onclick = () => _toggleFlag(im, note, keepBtn, 'keep_handwritten');
            actions.appendChild(keepBtn);
        }

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
        const d = await safeJson(resp);
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

/**
 * Run one opt-in, advisory LLM prose/formatting review of the whole note.
 * One server-side call (scope-locked, writes nothing). The suggestions are
 * model output (untrusted) and are rendered through marked + sanitiseHtml with
 * a textContent fallback, like the note preview.
 */
async function _reviewNote(note, btn, outEl) {
    btn.disabled = true;
    _clear(outEl);
    const pending = document.createElement('div');
    pending.className = 'muted';
    pending.textContent = 'Reviewing… (one LLM pass)';
    outEl.appendChild(pending);
    try {
        const resp = await secureFetch('/api/refactor/review-note', {
            method: 'POST',
            body: JSON.stringify({ rel: note.rel_path, scope_subdir: _scope }),
        });
        const d = await safeJson(resp);
        _clear(outEl);
        if (!resp.ok || d.error) {
            const err = document.createElement('div');
            err.className = 'refactor-extract-error';
            err.textContent = 'Review error: ' + (d.error || `HTTP ${resp.status}`);
            outEl.appendChild(err);
            return;
        }
        const meta = document.createElement('div');
        meta.className = 'muted';
        meta.textContent = `Advisory — review manually. Model: ${d.model || '?'} (${d.provider || '?'})`
            + (d.truncated ? ' · note truncated for review' : '');
        outEl.appendChild(meta);
        const body = document.createElement('div');
        body.className = 'refactor-review-body';
        _renderMarkdown(body, d.suggestions || '(no suggestions returned)');
        outEl.appendChild(body);
    } catch (e) {
        logError('Refactor review failed', e);
        _clear(outEl);
        const err = document.createElement('div');
        err.className = 'refactor-extract-error';
        err.textContent = 'Review failed (see console).';
        outEl.appendChild(err);
    } finally {
        btn.disabled = false;
    }
}

// --- On-demand LLM actions (requests b/c/e) + section scope (f) --------------

function _mkBtn(label, title) {
    const b = document.createElement('button');
    b.className = 'btn btn-outline btn-sm';
    b.textContent = label;
    if (title) b.title = title;
    return b;
}

function _pending(out, text) {
    const p = document.createElement('div');
    p.className = 'muted';
    p.textContent = text;
    out.appendChild(p);
}

function _llmErrorMsg(out, msg) {
    const e = document.createElement('div');
    e.className = 'refactor-extract-error';
    e.textContent = msg;
    out.appendChild(e);
}

function _llmError(out, d, resp) {
    _llmErrorMsg(out, 'Error: ' + (d.error || `HTTP ${resp.status}`));
}

function _fillSectionOptions(sel, sections) {
    while (sel.options.length > 1) sel.remove(1);   // keep "Whole note"
    for (const s of sections) {
        const o = document.createElement('option');
        o.value = String(s.index);
        const indent = s.level > 1 ? ' '.repeat((s.level - 1) * 2) : '';
        o.textContent = s.is_intro ? s.title : (indent + '#'.repeat(s.level) + ' ' + s.title);
        sel.appendChild(o);
    }
}

const _sectionsInflight = new Set();

async function _ensureSections(note, sel) {
    if (_sectionsCache.has(note.rel_path)) return;
    // Item 3.8: in-flight guard replaces the old {once:true} listeners — a
    // TRANSIENT fetch failure used to consume both one-shot triggers, leaving
    // the section selector permanently unfillable until a full re-render.
    if (_sectionsInflight.has(note.rel_path)) return;
    _sectionsInflight.add(note.rel_path);
    try {
        const resp = await secureFetch('/api/refactor/sections', {
            method: 'POST',
            body: JSON.stringify({ rel: note.rel_path, scope_subdir: _scope }),
        });
        const d = await safeJson(resp);
        if (!resp.ok || d.error) return;
        _sectionsCache.set(note.rel_path, d.sections || []);
        const prev = sel.value;
        _fillSectionOptions(sel, d.sections || []);
        sel.value = prev;
    } catch (e) {
        logError('Refactor sections load failed', e);
    } finally {
        _sectionsInflight.delete(note.rel_path);
    }
}

/**
 * Build the "LLM actions" panel: a section-scope selector (Whole note / each
 * heading section, lazily loaded) plus the three on-demand actions — Improve
 * formatting (b, applyable), Summarize a PDF (c, applyable), Generate diagram
 * (e, advisory display only). Results render into one shared output area.
 */
function _renderLlmActions(note, detail) {
    const panel = document.createElement('div');
    panel.className = 'refactor-llm-panel';
    const title = document.createElement('div');
    title.className = 'refactor-section-title';
    title.textContent = 'LLM actions';
    panel.appendChild(title);

    const scopeRow = document.createElement('div');
    scopeRow.className = 'refactor-llm-scope';
    const scopeLbl = document.createElement('label');
    scopeLbl.textContent = 'Scope: ';
    const sel = document.createElement('select');
    sel.className = 'refactor-section-select';
    const whole = document.createElement('option');
    whole.value = '';
    whole.textContent = 'Whole note';
    sel.appendChild(whole);
    const cachedSecs = _sectionsCache.get(note.rel_path);
    if (cachedSecs) _fillSectionOptions(sel, cachedSecs);
    const savedSel = _selectedSection.get(note.rel_path);
    if (savedSel != null) sel.value = savedSel;
    sel.onchange = () => _selectedSection.set(note.rel_path, sel.value);
    // Load the section list on first interaction (avoids a request per note);
    // mousedown covers click, focus covers keyboard. _ensureSections is
    // idempotent (cache-guarded), so firing both is harmless.
    // Persistent triggers (item 3.8): _ensureSections self-guards on cache +
    // in-flight, so repeated events are cheap no-ops — and a transient failure
    // gets retried on the NEXT interaction instead of never.
    sel.addEventListener('mousedown', () => _ensureSections(note, sel));
    sel.addEventListener('focus', () => _ensureSections(note, sel));
    scopeLbl.appendChild(sel);
    scopeRow.appendChild(scopeLbl);
    const hint = document.createElement('small');
    hint.className = 'control-hint';
    hint.textContent = ' Sub-part to act on (defaults to the whole note).';
    scopeRow.appendChild(hint);
    panel.appendChild(scopeRow);

    const actions = document.createElement('div');
    actions.className = 'refactor-image-actions';
    const fmtBtn = _mkBtn('Improve formatting (LLM)',
        'Reformat the selected scope with the LLM, then review & apply');
    const chartBtn = _mkBtn('Generate diagram (Mermaid)',
        'Advisory Mermaid diagram for the selected scope (display only)');
    const pdfBtn = _mkBtn('Summarize a PDF…',
        'Summarize an attached PDF into bullets inlined as a callout');
    actions.appendChild(fmtBtn);
    actions.appendChild(pdfBtn);
    actions.appendChild(chartBtn);
    panel.appendChild(actions);

    // Free-prompt edit: type an instruction, get an applyable proposal (uses the
    // selected scope above). Content changes are allowed — the preview + Approve
    // & apply + Restore are the safety net.
    const customWrap = document.createElement('div');
    customWrap.className = 'refactor-custom';
    const ta = document.createElement('textarea');
    ta.className = 'refactor-custom-instruction';
    ta.rows = 2;
    ta.lang = 'fr';  // French free-prompt field in an en document (6c)
    ta.placeholder = 'Instruction libre — ex. : reformule en plus clair, corrige la ponctuation, transforme en tableau…';
    ta.value = _customInstruction.get(note.rel_path) || '';
    ta.oninput = () => _customInstruction.set(note.rel_path, ta.value);
    customWrap.appendChild(ta);
    // Click-to-insert example instructions (French; the chip dispatches `input`
    // so the oninput above records it into _customInstruction).
    renderExampleChips(ta, [
        { label: 'Titres clairs',
          text: "Reformate cette note en titres et sous-titres clairs, sans changer le contenu clinique." },
        { label: 'Posologies → tableau',
          text: "Convertis les listes de posologies en un tableau markdown : molécule, dose, indication, effets indésirables." },
        { label: 'Synthèse par section',
          text: "Ajoute une phrase de synthèse en tête de chaque section, en français." },
    ], { title: 'Examples:', lang: 'fr' });
    const customRow = document.createElement('div');
    customRow.className = 'refactor-image-actions';
    const customBtn = _mkBtn('Appliquer l’instruction (LLM)',
        'Run your free-form instruction over the selected scope, then review & apply');
    customRow.appendChild(customBtn);
    customWrap.appendChild(customRow);
    panel.appendChild(customWrap);

    const out = document.createElement('div');
    out.className = 'refactor-llm-out';
    panel.appendChild(out);

    const allBtns = [fmtBtn, chartBtn, pdfBtn, customBtn];
    fmtBtn.onclick = () => _llmRewrite(note, sel, out, allBtns);
    chartBtn.onclick = () => _llmChart(note, sel, out, allBtns);
    pdfBtn.onclick = () => _llmPdfRefs(note, out, allBtns);
    customBtn.onclick = () => _llmCustom(note, sel, ta, out, allBtns);

    detail.appendChild(panel);
}

function _sectionBody(sel) {
    return sel.value !== '' ? { section_index: parseInt(sel.value, 10) } : {};
}

/** Render a staged-proposal preview (diff + Approve & apply / Discard). */
function _renderStagedPreview(note, out, d, action) {
    const meta = document.createElement('div');
    meta.className = 'muted';
    meta.textContent = `Preview — review before applying. Model: ${d.model || '?'} (${d.provider || '?'})`
        + (d.truncated ? ' · note truncated for the LLM' : '');
    out.appendChild(meta);
    out.appendChild(_renderDiff(d.diff));
    const row = document.createElement('div');
    row.className = 'refactor-image-actions';
    const apply = document.createElement('button');
    apply.className = 'btn btn-primary btn-sm';
    apply.textContent = 'Approve & apply (write)';
    apply.onclick = () => _applyStaged(note, action, d, apply, out);
    const cancel = _mkBtn('Discard');
    cancel.onclick = () => _clear(out);
    row.appendChild(apply);
    row.appendChild(cancel);
    out.appendChild(row);
}

async function _llmRewrite(note, sel, out, btns) {
    btns.forEach((b) => (b.disabled = true));
    _clear(out);
    _pending(out, 'Reformatting… (one LLM pass)');
    try {
        const body = { rel: note.rel_path, scope_subdir: _scope, ..._sectionBody(sel) };
        const resp = await secureFetch('/api/refactor/rewrite', { method: 'POST', body: JSON.stringify(body) });
        const d = await safeJson(resp);
        _clear(out);
        if (!resp.ok || d.error) { _llmError(out, d, resp); return; }
        _renderStagedPreview(note, out, d, 'rewrite');
    } catch (e) {
        logError('Refactor rewrite failed', e);
        _clear(out); _llmErrorMsg(out, 'Rewrite failed (see console).');
    } finally {
        btns.forEach((b) => (b.disabled = false));
    }
}

async function _llmCustom(note, sel, ta, out, btns) {
    const instruction = (ta.value || '').trim();
    if (!instruction) {
        _clear(out);
        _llmErrorMsg(out, 'Type an instruction first.');
        return;
    }
    btns.forEach((b) => (b.disabled = true));
    _clear(out);
    _pending(out, 'Editing… (one LLM pass)');
    try {
        const body = { rel: note.rel_path, scope_subdir: _scope, instruction, ..._sectionBody(sel) };
        const resp = await secureFetch('/api/refactor/custom-edit', { method: 'POST', body: JSON.stringify(body) });
        const d = await safeJson(resp);
        _clear(out);
        if (!resp.ok || d.error) { _llmError(out, d, resp); return; }
        _renderStagedPreview(note, out, d, 'custom');
    } catch (e) {
        logError('Refactor custom-edit failed', e);
        _clear(out); _llmErrorMsg(out, 'Edit failed (see console).');
    } finally {
        btns.forEach((b) => (b.disabled = false));
    }
}

async function _llmChart(note, sel, out, btns) {
    btns.forEach((b) => (b.disabled = true));
    _clear(out);
    _pending(out, 'Generating diagram… (one LLM pass)');
    try {
        const body = { rel: note.rel_path, scope_subdir: _scope, ..._sectionBody(sel) };
        const resp = await secureFetch('/api/refactor/chart', { method: 'POST', body: JSON.stringify(body) });
        const d = await safeJson(resp);
        _clear(out);
        if (!resp.ok || d.error) { _llmError(out, d, resp); return; }
        const meta = document.createElement('div');
        meta.className = 'muted';
        meta.textContent = `Advisory — copy into your note (Obsidian renders Mermaid). Model: ${d.model || '?'} (${d.provider || '?'})`;
        out.appendChild(meta);
        const pre = document.createElement('pre');
        pre.className = 'refactor-extract-text';
        pre.textContent = d.mermaid || '(empty)';
        out.appendChild(pre);
        const copy = _mkBtn('Copy');
        // Use the shared helper so a clipboard failure (denied permission / non-
        // secure context) is reported honestly and announced for a11y, instead of
        // flipping to "Copied" on a fire-and-forget write that may have rejected.
        copy.onclick = () => copyToClipboard(d.mermaid || '', copy);
        out.appendChild(copy);
    } catch (e) {
        logError('Refactor chart failed', e);
        _clear(out); _llmErrorMsg(out, 'Diagram generation failed (see console).');
    } finally {
        btns.forEach((b) => (b.disabled = false));
    }
}

async function _llmPdfRefs(note, out, btns) {
    btns.forEach((b) => (b.disabled = true));
    _clear(out);
    _pending(out, 'Finding attached PDFs…');
    try {
        const resp = await secureFetch('/api/refactor/pdf-refs', {
            method: 'POST', body: JSON.stringify({ rel: note.rel_path, scope_subdir: _scope }),
        });
        const d = await safeJson(resp);
        _clear(out);
        if (!resp.ok || d.error) { _llmError(out, d, resp); return; }
        const pdfs = d.pdfs || [];
        if (!pdfs.length) {
            _pending(out, 'No resolvable PDF embeds found in this note.');
            return;
        }
        const row = document.createElement('div');
        row.className = 'refactor-llm-scope';
        const sel = document.createElement('select');
        sel.className = 'refactor-section-select';
        for (const p of pdfs) {
            const o = document.createElement('option');
            o.value = p.rel_path;
            o.textContent = (p.target || p.rel_path) + (p.cached ? '' : ' (not cached — may extract)');
            sel.appendChild(o);
        }
        row.appendChild(sel);
        const go = _mkBtn('Summarize');
        go.onclick = () => _llmSummarizePdf(note, sel.value, out, btns);
        row.appendChild(go);
        out.appendChild(row);
    } catch (e) {
        logError('Refactor pdf-refs failed', e);
        _clear(out); _llmErrorMsg(out, 'PDF lookup failed (see console).');
    } finally {
        btns.forEach((b) => (b.disabled = false));
    }
}

async function _llmSummarizePdf(note, pdfRel, out, btns) {
    btns.forEach((b) => (b.disabled = true));
    _clear(out);
    _pending(out, 'Summarizing PDF… (one LLM pass)');
    try {
        const resp = await secureFetch('/api/refactor/summarize-pdf', {
            method: 'POST',
            body: JSON.stringify({ rel: note.rel_path, scope_subdir: _scope, pdf_rel: pdfRel }),
        });
        const d = await safeJson(resp);
        _clear(out);
        if (!resp.ok || d.error) { _llmError(out, d, resp); return; }
        _renderStagedPreview(note, out, d, 'summarize_pdf');
    } catch (e) {
        logError('Refactor summarize-pdf failed', e);
        _clear(out); _llmErrorMsg(out, 'PDF summary failed (see console).');
    } finally {
        btns.forEach((b) => (b.disabled = false));
    }
}

/** Apply a staged LLM proposal (rewrite / PDF summary) to the note. */
async function _applyStaged(note, action, d, btn, out) {
    btn.disabled = true;
    try {
        const resp = await secureFetch('/api/refactor/apply-staged', {
            method: 'POST',
            body: JSON.stringify({
                rel: note.rel_path, scope_subdir: _scope, action,
                content_sha256: d.content_sha256, proposed_sha256: d.proposed_sha256,
                confirm: true,
            }),
        });
        const dd = await safeJson(resp);
        if (!resp.ok || dd.error || !dd.ok) {
            const r = dd.result || {};
            _clear(out);
            _llmErrorMsg(out, 'Apply: ' + (r.message || dd.error || `HTTP ${resp.status}`));
            btn.disabled = false;
            return;
        }
        _activity(`${action} applied to ${note.rel_path}`);
        _markStagedApplied(note, d.proposed_sha256);
        // Item 3.4: the pane is open on this very note — refresh its frame
        // now so Original/Proposed show the just-written on-disk body.
        _reanalyzeNote(note);
    } catch (e) {
        logError('Refactor apply-staged failed', e);
        _llmErrorMsg(out, 'Apply failed (see console).');
        btn.disabled = false;
    }
}

/** After a staged LLM apply, the whole note body changed — retire every sibling
 * preview (callout-apply, formatting fix, cached sections) until a re-plan. */
function _markStagedApplied(note, newHash) {
    note.content_sha256 = newHash || note.content_sha256;
    note.changed = false;
    note.normalize_changed = false;
    _approved.delete(note.rel_path);
    _normApproved.delete(note.rel_path);
    _sectionsCache.delete(note.rel_path);
    _selectedSection.delete(note.rel_path);
    _customInstruction.delete(note.rel_path);
    _updateApplyButton();
    _updateNormalizeButton();
    $('refactor-note-list').querySelectorAll('.refactor-note-entry').forEach((el) => {
        if (el.dataset.rel !== note.rel_path) return;
        const cb = el.querySelector('.refactor-approve-cb');
        if (cb) { cb.checked = false; cb.disabled = true; }
        const badges = el.querySelector('.refactor-entry-badges');
        if (badges && !badges.querySelector('.badge-applied')) {
            const b = _badge('edited', 'badge-changed');
            b.classList.add('badge-applied');
            badges.appendChild(b);
        }
    });
    if (_selectedRel === note.rel_path) _renderDetail(note);
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
        const d = await safeJson(resp);
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

/**
 * Toggle a per-image flag ('strip' | 'keep_handwritten') on the sticky flag
 * sidecar (under obsidian_cache, NOT the vault). On success flip the matching
 * in-memory field and re-render so the badge/label update immediately; the
 * actual proposed-callout change is server-computed, so it takes full effect on
 * the next Run Plan (mirrors the ignore-list's semantics).
 */
async function _toggleFlag(im, note, btn, flag) {
    const field = flag === 'strip' ? 'metadata_stripped' : 'kept_handwritten';
    const action = im[field] ? 'remove' : 'add';
    if (btn) btn.disabled = true;
    try {
        const resp = await secureFetch('/api/refactor/flag', {
            method: 'POST',
            body: JSON.stringify({ rel: im.rel_path, flag, action }),
        });
        const d = await safeJson(resp);
        if (!resp.ok || d.error) {
            _status(d.error || `Flag failed (HTTP ${resp.status}).`, true);
            return;
        }
        im[field] = (action === 'add');
        if (note && note.rel_path === _selectedRel) _renderDetail(note);
    } catch (e) {
        logError('Refactor flag toggle failed', e);
        _status('Flag toggle failed (see console).', true);
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
        const d = await safeJson(resp);
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
        // Item 3.4: the hand-synced note objects still carry the PRE-apply
        // original/proposed bodies, so the detail pane kept showing a diff
        // for a note whose on-disk content is now the proposed body. Refresh
        // the selected note's frame from the server immediately; other
        // applied notes refresh lazily on selection (_needsPostApplyRefresh
        // in _selectNote) — one request per viewed note, not per batch row.
        const sel = _notes.find((x) => x.rel_path === _selectedRel);
        if (sel && sel.applied) {
            sel._needsPostApplyRefresh = false;   // refreshing NOW — don't re-fetch on next select
            _reanalyzeNote(sel);
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
 * the same note passes the stale-diff guard (on-disk now == proposed).
 *
 * Crucially this also RETIRES the note's sibling formatting-fix: applying the
 * callout changed the on-disk body, so the previously-previewed `normalized` /
 * `normalized_sha256` (both computed from the pre-apply body) are now stale.
 * Offering "Fix formatting" on this note would send a stale hash the server
 * rejects as drift — confusing though safe. We drop it from the normalize
 * targets until the user re-runs the plan (which recomputes every hash). */
function _markApplied(rel) {
    const n = _notes.find((x) => x.rel_path === rel);
    if (n) {
        n.applied = true;
        n.content_sha256 = n.proposed_sha256;  // on-disk == applied body now
        n.normalize_changed = false;           // sibling fix is stale → retire it
        n._needsPostApplyRefresh = true;       // item 3.4: panes refresh on view
    }
    _approved.delete(rel);
    _normApproved.delete(rel);                  // sibling fix retired → drop opt-in
    _updateApplyButton();
    _updateNormalizeButton();
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

// --- Phase 2: deterministic formatting fix (batch write) --------------------

// Like Apply, the formatting fix is now per-note opt-in: the user ticks each
// note's "Approve formatting fix" checkbox in the detail pane (default OFF), and
// only approved notes are written. _normApproved holds those rel_paths; a note
// that stops being a normalize target (applied/normalized/re-planned) drops out.
function _normalizeTargets() {
    return _notes.filter((n) => n.normalize_changed && _normApproved.has(n.rel_path));
}

function _toggleNormApprove(rel, checked) {
    if (checked) _normApproved.add(rel); else _normApproved.delete(rel);
    _updateNormalizeButton();
}

function _updateNormalizeButton() {
    const btn = $('refactor-normalize-btn');
    if (!btn) return;
    const n = _normalizeTargets().length;
    btn.textContent = `Fix formatting (${n})`;
    btn.disabled = n === 0;
}

/** Open the formatting-fix confirmation modal listing the affected notes. */
export function openNormalize() {
    const targets = _normalizeTargets();
    if (!targets.length) return;
    const list = $('refactor-normalize-list');
    _clear(list);
    for (const n of targets) {
        const li = document.createElement('li');
        li.textContent = n.rel_path;
        list.appendChild(li);
    }
    $('refactor-normalize-modal-status').textContent = '';
    $('refactor-normalize-confirm').disabled = false;
    openModal('refactor-normalize-modal');
}

/** Write the deterministic formatting fixes to the vault (reversible). */
export async function confirmNormalize() {
    const notes = _normalizeTargets().map((n) => ({
        rel: n.rel_path, content_sha256: n.content_sha256, normalized_sha256: n.normalized_sha256,
    }));
    if (!notes.length) { closeModal('refactor-normalize-modal'); return; }
    const status = $('refactor-normalize-modal-status');
    const btn = $('refactor-normalize-confirm');
    btn.disabled = true;
    status.textContent = 'Writing to the vault…';
    try {
        const resp = await secureFetch('/api/refactor/normalize', {
            method: 'POST',
            body: JSON.stringify({ scope_subdir: _scope, confirm: true, notes }),
        });
        const d = await safeJson(resp);
        if (!resp.ok || d.error) {
            status.textContent = 'Error: ' + (d.error || `HTTP ${resp.status}`);
            btn.disabled = false;
            return;
        }
        const results = d.results || [];
        let applied = 0;
        for (const r of results) {
            if (r.status === 'applied') { applied++; _markNormalized(r.rel); }
            _activity(`format ${r.rel}: ${r.status}` + (r.message ? ' — ' + r.message : ''),
                      r.status !== 'applied');
        }
        status.textContent = `Fixed ${applied} of ${results.length}.`;
        setTimeout(() => closeModal('refactor-normalize-modal'), 1100);
    } catch (e) {
        logError('Refactor format-fix failed', e);
        status.textContent = 'Fix formatting failed (see console).';
        btn.disabled = false;
    }
}

/** After a formatting fix is written, sync in-memory state: the note is no
 * longer a normalize target and its on-disk hash is now the normalized one.
 *
 * Symmetric to _markApplied: the formatting fix changed the on-disk body, so
 * the sibling callout-apply's previewed `proposed` / `proposed_sha256` are now
 * stale. We retire the callout-apply for this note (clear `changed`, drop it
 * from the approve set, uncheck+disable its sidebar checkbox) until a re-plan
 * recomputes the hashes — otherwise Apply would send a stale hash the server
 * rejects as drift. */
function _markNormalized(rel) {
    const n = _notes.find((x) => x.rel_path === rel);
    if (n) {
        n.normalize_changed = false;
        n.content_sha256 = n.normalized_sha256;  // on-disk == normalized now
        n.changed = false;                        // sibling callout-apply is stale
    }
    _approved.delete(rel);
    _normApproved.delete(rel);
    _updateNormalizeButton();
    _updateApplyButton();
    // Uncheck + disable the sidebar approve checkbox so the now-stale callout
    // can't be re-submitted before a re-plan (mirrors _markApplied's entry work).
    $('refactor-note-list').querySelectorAll('.refactor-note-entry').forEach((el) => {
        if (el.dataset.rel !== rel) return;
        const cb = el.querySelector('.refactor-approve-cb');
        if (cb) { cb.checked = false; cb.disabled = true; }
    });
    if (_selectedRel === rel) _selectNote(rel);  // refresh the detail toggle
}

/**
 * Persist the scope-wide "strip OCR preamble" default, then re-run the plan so
 * the new callout bodies (and their hashes) materialize. Config is the single
 * source of truth read by both the plan and the apply writer, so the WYSIWYG
 * guard stays honest.
 */
export async function toggleStripDefault() {
    const cb = $('refactor-strip-preamble');
    if (!cb) return;
    try {
        const resp = await secureFetch('/api/config', {
            method: 'POST',
            body: JSON.stringify({ refactor_strip_preamble_default: cb.checked }),
        });
        if (!resp.ok) {
            _status('Could not save the strip-preamble setting.', true);
            cb.checked = !cb.checked;  // roll back the visual toggle
            return;
        }
        // Re-analyze with the new default. Awaited (not fire-and-forget) so an
        // error here is caught below, and guarded on !_running: the checkbox is
        // disabled during a run (see runPlan), so this is belt-and-braces only.
        if (_notes.length && !_running) await runPlan();
    } catch (e) {
        logError('Refactor strip-default toggle failed', e);
        cb.checked = !cb.checked;
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
        const d = await safeJson(resp);
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
        const d = await safeJson(resp);
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
        const d = await safeJson(resp);
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
export function revertAll() {
    // Track 6e: this rewrites every recorded vault change in one click —
    // the single most destructive button in the app had NO ceremony while
    // each individual write it undoes was confirm-gated.
    const btn = $('refactor-restore-all');
    if (!btn) return _doRevertAll();
    confirmInline(btn, {
        message: 'Revert EVERY recorded vault change (newest first)? '
            + 'Notes are restored from their journal snapshots.',
        confirmLabel: 'Revert all changes',
        onConfirm: _doRevertAll,
    });
}

async function _doRevertAll() {
    const status = $('refactor-restore-status');
    const btn = $('refactor-restore-all');
    if (btn) btn.disabled = true;
    status.textContent = 'Reverting all…';
    try {
        const resp = await secureFetch('/api/refactor/restore', {
            method: 'POST', body: JSON.stringify({ all: true }),
        });
        const d = await safeJson(resp);
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

// Pre-fill the scope input + strip-preamble toggle from saved config on init.
export function initRefactorTab(config) {
    const el = $('refactor-scope');
    if (el && config && config.refactor_scope_subdir && !el.value) {
        el.value = config.refactor_scope_subdir;
    }
    const strip = $('refactor-strip-preamble');
    if (strip && config) strip.checked = !!config.refactor_strip_preamble_default;
}
