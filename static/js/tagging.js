// Biblio Tagging sub-view (rendered inside the Library Audit panel).
//
// Two views over the same biblio-scoped inventory, switched by a toolbar toggle
// (persisted in localStorage `tagging_view`):
//
//   • Table  — the original per-row grid (overview + bulk scan). Every Propose /
//     Apply now writes a VISIBLE status line into its row (previously the result
//     went only to the screen-reader announcer, so a sighted user saw nothing).
//   • Deck   — a one-card-at-a-time triage flow (untagged-first): Propose tags
//     for the current paper, edit with the vocabulary type-ahead, then Accept to
//     fan the decision out to all three targets at once (note + Finder + Zotero)
//     via /api/tagging/apply-all. Keyboard-first: P propose, A accept, S/→ skip,
//     M defer, ⌫ undo, 1-9 toggle proposed chips, ← previous.
//
// Accept routes through the shared confirmInline ceremony (a vault write); Undo
// reverses the paper's ops through /api/tagging/restore. Every write stays
// confirm-gated, vocabulary-validated server-side, journalled and reversible.
//
// Module hierarchy: imports api.js + ui.js only (the feature allowance).
// audit.js delegates rendering here via window.taggingRenderInto (a call, not
// an import) and hands over its latest-wins `isCurrent` gate.

import { secureFetch, logError } from './api.js';
import { confirmInline, announceStatus, announceError, showTaskError } from './ui.js';

/** HTML-escape a value for safe interpolation (user paths/tags never raw). */
function _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}
/** Escape a value destined for a double-quoted HTML attribute. */
function _attr(s) { return _esc(s); }

/** A tag set rendered as escaped, comma-joined text; em-dash when empty. */
function _tags(tags) {
    if (!Array.isArray(tags) || tags.length === 0) return '—';
    return _esc(tags.join(', '));
}

/** Removable rendering of a store's current tags (tag deletion, 2026-07-22):
 *  each tag carries an × button targeting ONE store. `target` ∈ note|finder|
 *  zotero. Falls back to the plain rendering for an empty list. */
function _tagsRemovable(tags, target) {
    if (!Array.isArray(tags) || tags.length === 0) return '—';
    return tags.map((t) => `<span class="tagging-cur-tag" data-tag="${_attr(t)}">${_esc(t)}`
        + `<button type="button" class="tagging-cur-x" data-tag="${_attr(t)}" data-target="${_attr(target)}" `
        + `aria-label="Remove tag ${_attr(t)} from ${_attr(target)}">×</button></span>`).join(', ');
}

/** Current tags of a cell rendered by _tagsRemovable (data-driven, never a
 *  textContent split — the × glyphs would pollute it). */
function _cellTags(cell) {
    return Array.from(cell.querySelectorAll('.tagging-cur-tag'))
        .map((el) => el.getAttribute('data-tag')).filter(Boolean);
}

const _CELL_TARGET = { '.tagging-note-cell': 'note', '.tagging-finder-cell': 'finder', '.tagging-zotero-cell': 'zotero' };

function _renderCellTags(cell, tags, target) {
    cell.innerHTML = _tagsRemovable(tags, target);
}

/** Parse a free-text tag entry into a clean list.
 *  Commas, whitespace AND newlines all separate — vocabulary tag ids never
 *  contain spaces (they come from `#token`s in _tags.md), so "dep mood" is
 *  two tags and the user never has to type a comma by hand. */
function _parseTags(raw) {
    return String(raw || '')
        .split(/[\s,\n]+/)
        .map((t) => t.trim().replace(/^#/, '').trim())
        .filter(Boolean);
}

/** Announce to the SR region AND (when given) a visible element, so both a
 *  screen-reader user and a sighted user learn the outcome. */
function _say(msg, visibleEl) {
    // F5 (2026-07-22): announceStatus (the STATIC #sr-status-announcer) is
    // the single AT channel; the visible element is deliberately NOT a live
    // region — minted-at-render live regions are dropped by WebKit (the
    // liveRegions.test.js hazard) and, where they worked, double-announced.
    announceStatus(msg);
    if (visibleEl) visibleEl.textContent = msg;
}

// Module-scoped render context, refreshed on every taggingRenderInto().
let _ctx = { rows: [], vocabRows: [], zoteroOk: false, body: null, meta: null };
// Snippet cache: key -> {snippet, chars} (successful fetches ONLY — an error
// must stay re-fetchable, never a sticky "no text"). Module-scoped so it
// survives card re-renders AND the Table↔Deck toggle (_renderView); cleared
// only when a fresh inventory arrives (taggingRenderInto).
let _snippetCache = {};
// Single-flight guard for the next-card snippet prefetch: the server runs
// snippet extraction on the request thread, so never two in parallel.
let _prefetchInFlight = false;
// The deck PDF panel's blob URL singleton (revoked before each replacement
// and on close — the deck.js _previewObjectUrl pattern).
let _pdfObjectUrl = null;
let _view = 'deck';
try { _view = localStorage.getItem('tagging_view') || 'deck'; } catch (e) { _view = 'deck'; }

/**
 * Fetch the biblio-scoped tagging inventory and render it into `body`.
 * @param {HTMLElement} body   the #audit-report-body panel
 * @param {HTMLElement} meta   the #audit-report-meta line (may be null)
 * @param {() => boolean} [isCurrent]  audit's latest-wins gate
 */
export async function taggingRenderInto(body, meta, isCurrent) {
    if (!body) return;
    const live = () => (typeof isCurrent !== 'function' || isCurrent());
    body.textContent = 'Loading inventory…';
    let resp;
    let data;
    try {
        resp = await secureFetch('/api/tagging/inventory');
        data = await resp.json();
    } catch (e) {
        if (!live()) return;
        logError('tagging inventory fetch failed: ' + String(e));
        if (meta) { meta.style.display = 'none'; meta.textContent = ''; }
        // A real error boundary with a Retry — the bare warning div left the
        // user reloading the whole app to recover from a transient failure.
        body.innerHTML = '<div class="task-error-boundary"></div>';
        showTaskError(body.firstChild, String(e), [
            { label: 'Retry', primary: true, onClick: () => taggingRenderInto(body, meta, isCurrent) },
        ]);
        return;
    }
    if (!live()) return;

    if (!resp.ok) {
        if (meta) { meta.style.display = 'none'; meta.textContent = ''; }
        const msg = (data && data.error)
            ? data.error
            : 'Tagging inventory unavailable. Run an audit scan first.';
        body.innerHTML = `<div class="modal-copy" style="padding:16px;">${_esc(msg)}</div>`;
        return;
    }

    const rows = (data && Array.isArray(data.rows)) ? data.rows : [];
    body.textContent = 'Loading vocabulary…';

    // Both are independent of the inventory payload and of each other, so
    // they run CONCURRENTLY (they were sequential — three round-trips of
    // frozen "Loading…", with the Zotero probe's 3 s timeout gating a render
    // it has nothing to do with). Both stay best-effort: a vocab failure just
    // means no autocomplete, a probe failure means the [→zotero] buttons
    // start disabled.
    const [vocabRows, zoteroOk] = await Promise.all([
        (async () => {
            try {
                const vr = await secureFetch('/api/tagging/vocab');
                if (vr.ok) {
                    const vd = await vr.json();
                    return (vd && Array.isArray(vd.vocab)) ? vd.vocab : [];
                }
            } catch (e) {
                logError('tagging vocab fetch failed: ' + String(e));
            }
            return [];
        })(),
        (async () => {
            try {
                const zr = await secureFetch('/api/tagging/zotero-available');
                if (zr.ok) {
                    const zd = await zr.json();
                    return !!(zd && zd.available);
                }
            } catch (e) {
                logError('tagging zotero probe failed: ' + String(e));
            }
            return false;
        })(),
    ]);
    if (!live()) return;

    _ctx = { rows, vocabRows, zoteroOk, body, meta };
    _snippetCache = {};
    _renderView();
}

/** Render the currently-selected view (deck or table) into the stored body. */
function _renderView() {
    const { rows, vocabRows, zoteroOk, body, meta } = _ctx;
    if (!body) return;

    // Opt the report body out of its overflow clip while tagging is mounted:
    // its overflow-x:auto coerces overflow-y to auto (CSS spec), which was
    // cutting off the type-ahead dropdown near the panel's bottom edge.
    // taggingTeardown removes the class when another report takes the panel.
    body.classList.add('tagging-active');

    if (meta) {
        meta.style.display = '';
        meta.innerHTML = `${_esc(rows.length)} paper(s) under <code>biblio_articles/</code>. `
            + '<strong>Propose</strong> suggests tags from the PDF; edit (type-ahead from '
            + `<code>_tags.md</code>, ${_esc(vocabRows.length)} tags) then apply to the note, `
            + `the PDF (Finder) or Zotero (${zoteroOk ? 'reachable' : 'not reachable'}). `
            + 'Every write is confirmed, audit-logged and reversible from <strong>Restore</strong>.';
    }

    body.innerHTML = _toolbar()
        + '<div id="tagging-view-host"></div>'
        + '<div id="tagging-restore-panel" style="display:none;"></div>';
    _wireToolbar(body);

    const host = body.querySelector('#tagging-view-host');
    if (_view === 'table') {
        host.innerHTML = _table(rows, zoteroOk);
        _wireTable(host);
    } else {
        _renderDeck(host);
    }
}

// ---------------------------------------------------------------------------
// Tag entry: a chips token-field + per-token type-ahead (table + deck).
//
// The first type-ahead cut kept a comma-joined text value; when Propose (or a
// finished manual tag) left the field ending WITHOUT a separator, the next
// keystroke glued onto the previous tag ("adhd" + "sti" → "adhdsti"), the
// token matched nothing, and the user was back to typing commas by hand. So:
// committed tags live as removable CHIPS and the <input> only ever holds the
// tag being typed — there are no separators at all. Typing a space/comma (or
// pasting a separated list) commits the token(s); picking a suggestion
// commits it; Enter commits a non-empty token and falls through to the deck's
// Accept only when the field is empty; Backspace in an empty input removes
// the last chip. Suggestions are accent-insensitive, match tag id AND gloss,
// and exclude tags already entered.
//
// State model: `input._tags` = committed chips; the effective tag list is
// always `_entryTags(input)` = chips + whatever is parsed from the live
// value, so code that sets `input.value` directly (tests, power-paste) keeps
// working.
// ---------------------------------------------------------------------------

// Unique-id counter for combobox listbox ids (aria-controls/activedescendant).
let _suggestSeq = 0;

/** CSS.escape with a fallback (jsdom lacks the CSS global). */
function _cssEscape(s) {
    if (typeof CSS !== 'undefined' && typeof CSS.escape === 'function') return CSS.escape(s);
    return String(s).replace(/["'\\\]]/g, '\\$&');
}

/** Accent/case-fold for matching (Dépression ⇒ depression). */
function _fold(s) {
    return String(s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
}

/** The effective tag list of an entry field: committed chips + typed text. */
function _entryTags(input) {
    if (!input) return [];
    const committed = Array.isArray(input._tags) ? input._tags : [];
    return committed.concat(_parseTags(input.value));
}

/** Replace the committed chip list (deduped, #-stripped) and re-render. */
function _entrySetTags(input, tags) {
    const out = [];
    const seen = new Set();
    (tags || []).forEach((t) => {
        const c = String(t || '').trim().replace(/^#/, '').trim();
        const key = c.toLowerCase();
        if (c && !seen.has(key)) { seen.add(key); out.push(c); }
    });
    input._tags = out;
    _renderEntryChips(input);
}

/** Clear chips + typed text (after a successful apply). */
function _entryClear(input) {
    if (!input) return;
    input._tags = [];
    input.value = '';
    _renderEntryChips(input);
}

function _renderEntryChips(input) {
    const host = input._chipsHost;
    if (!host) return;
    host.innerHTML = '';
    (input._tags || []).forEach((t) => {
        const chip = document.createElement('span');
        chip.className = 'tagging-entry-chip';
        chip.appendChild(document.createTextNode(t));
        const x = document.createElement('button');
        x.type = 'button';
        x.className = 'tagging-entry-chip-x';
        x.setAttribute('aria-label', `Remove ${t}`);
        x.textContent = '×';
        const removeChip = () => {
            input._tags = (input._tags || []).filter((v) => v !== t);
            _renderEntryChips(input);
            // Keep the proposed-chip highlight in sync (deck only; a miss is fine).
            const pc = document.querySelector(`.tagging-chip[data-chip="${_cssEscape(t)}"]`);
            if (pc) pc.classList.remove('is-on');
            input.focus();
        };
        // mousedown (not click) for the MOUSE path: it fires before the
        // input's blur. But keyboard activation of a <button> fires ONLY
        // click — the mousedown-only wiring made these Tab-reachable buttons
        // inert on Enter/Space (2026-07-22 audit F1, WCAG 2.1.1: a keyboard
        // user could not remove a MIDDLE chip at all, ⌫ only pops the last).
        // e.detail === 0 distinguishes the keyboard click; the mouse path's
        // re-render detaches the button before its click can fire, so the
        // two paths can never double-remove.
        x.addEventListener('mousedown', (e) => { e.preventDefault(); removeChip(); });
        x.addEventListener('click', (e) => { if (e.detail === 0) removeChip(); });
        chip.appendChild(x);
        host.appendChild(chip);
    });
}

function _wireTagEntry(input) {
    if (!input || input._taggingEntry) return;
    input._taggingEntry = true;
    input.removeAttribute('list'); // never double up with a native datalist
    input.setAttribute('autocomplete', 'off');

    // Wrap the input in a token field: [chip] [chip] [input…], one visual box.
    const field = document.createElement('div');
    field.className = 'tagging-tokenfield';
    input.parentNode.insertBefore(field, input);
    const chipsHost = document.createElement('span');
    chipsHost.className = 'tagging-entry-chips';
    field.appendChild(chipsHost);
    field.appendChild(input);
    input._chipsHost = chipsHost;
    input._tags = [];
    field.addEventListener('mousedown', (e) => {
        if (e.target === field || e.target === chipsHost) { e.preventDefault(); input.focus(); }
    });

    // Full ARIA combobox pattern (2026-07-22 audit F2 — the old markup had a
    // listbox role and option roles on <button>s and nothing else, so a blind
    // user typing got NO signal that suggestions opened, which was active, or
    // that Enter's meaning had changed from "commit token" to "pick").
    const boxId = `tagging-suggest-${++_suggestSeq}`;
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('aria-controls', boxId);
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-haspopup', 'listbox');

    const box = document.createElement('div');
    box.className = 'tagging-suggest';
    box.id = boxId;
    box.setAttribute('role', 'listbox');
    box.setAttribute('aria-label', 'Tag suggestions');
    box.style.display = 'none';
    field.appendChild(box);

    let items = [];
    let active = -1;

    const close = () => {
        box.style.display = 'none'; box.innerHTML = ''; items = []; active = -1;
        input.setAttribute('aria-expanded', 'false');
        input.removeAttribute('aria-activedescendant');
    };

    const markActive = () => {
        Array.from(box.children).forEach((c, i) => {
            c.classList.toggle('is-active', i === active);
            c.setAttribute('aria-selected', i === active ? 'true' : 'false');
        });
        // The arrow-key highlight is exposed to AT via activedescendant —
        // focus never leaves the input (the options are not tabbable).
        if (active >= 0 && box.children[active]) {
            input.setAttribute('aria-activedescendant', box.children[active].id);
        } else {
            input.removeAttribute('aria-activedescendant');
        }
    };

    // Any separator typed or pasted commits the completed token(s) as chips;
    // a trailing partial (no separator after it) stays in the input.
    const absorbSeparators = () => {
        const v = input.value;
        if (!/[\s,]/.test(v)) return;
        const endsWithSep = /[\s,]$/.test(v);
        const toks = _parseTags(v);
        const keep = endsWithSep ? '' : (toks.pop() || '');
        if (toks.length) _entrySetTags(input, (input._tags || []).concat(toks));
        input.value = keep;
    };

    const pick = (i) => {
        const v = items[i];
        if (!v) return;
        _entrySetTags(input, (input._tags || []).concat([v.tag]));
        input.value = '';
        close();
        const pc = document.querySelector(`.tagging-chip[data-chip="${_cssEscape(v.tag)}"]`);
        if (pc) pc.classList.add('is-on');
        input.focus();
    };

    const render = () => {
        const q = _fold(input.value.trim().replace(/^#/, ''));
        if (!q) { close(); return; }
        const have = new Set(_entryTags(input).map(_fold));
        const starts = [];
        const contains = [];
        (_ctx.vocabRows || []).forEach((v) => {
            const tag = String(v.tag || '');
            const l = _fold(tag);
            if (!tag || (have.has(l) && l !== q)) return; // entered (unless mid-edit)
            if (l.startsWith(q)) starts.push(v);
            else if (l.includes(q) || _fold(v.gloss).includes(q)) contains.push(v);
        });
        items = starts.concat(contains).slice(0, 8);
        if (!items.length) { close(); return; }
        active = 0;
        box.innerHTML = '';
        items.forEach((v, i) => {
            // Options are DIVs, not <button>s (F2): role="option" on a button
            // is a semantics clash, and the buttons were Tab stops that did
            // nothing on Enter (mousedown-only wiring). The listbox pattern
            // keeps focus in the input; mouse picks still work via mousedown
            // (fires before the input's blur).
            const b = document.createElement('div');
            b.className = 'tagging-suggest-item';
            b.id = `${boxId}-opt-${i}`;
            b.setAttribute('role', 'option');
            b.setAttribute('aria-selected', 'false');
            const strong = document.createElement('strong');
            strong.textContent = v.tag;
            b.appendChild(strong);
            if (v.gloss) {
                const g = document.createElement('span');
                g.className = 'tagging-suggest-gloss';
                g.textContent = ` — ${String(v.gloss).slice(0, 60)}`;
                b.appendChild(g);
            }
            b.addEventListener('mousedown', (e) => { e.preventDefault(); pick(i); });
            box.appendChild(b);
        });
        markActive();
        box.style.display = '';
        input.setAttribute('aria-expanded', 'true');
    };

    input.addEventListener('input', () => { absorbSeparators(); render(); });
    input.addEventListener('blur', () => { window.setTimeout(close, 120); });
    input.addEventListener('keydown', (e) => {
        const open = box.style.display !== 'none';
        if (open) {
            if (e.key === 'ArrowDown') {
                e.preventDefault(); e.stopPropagation();
                active = (active + 1) % items.length; markActive();
                return;
            }
            if (e.key === 'ArrowUp') {
                e.preventDefault(); e.stopPropagation();
                active = (active - 1 + items.length) % items.length; markActive();
                return;
            }
            if (e.key === 'Enter' || e.key === 'Tab') {
                // Enter picks the suggestion INSTEAD of accepting the card.
                e.preventDefault(); e.stopPropagation();
                pick(active);
                return;
            }
            if (e.key === 'Escape') { e.stopPropagation(); close(); return; }
        }
        if (e.key === 'Enter' && input.value.trim()) {
            // Commit the typed token; Accept only fires from an EMPTY field,
            // so one Enter finishes the tag and the next one accepts.
            e.preventDefault(); e.stopPropagation();
            _entrySetTags(input, (input._tags || []).concat(_parseTags(input.value)));
            input.value = '';
            close();
            return;
        }
        if (e.key === 'Backspace' && !input.value && (input._tags || []).length) {
            e.preventDefault(); e.stopPropagation();
            const removed = input._tags[input._tags.length - 1];
            input._tags = input._tags.slice(0, -1);
            _renderEntryChips(input);
            const pc = document.querySelector(`.tagging-chip[data-chip="${_cssEscape(removed)}"]`);
            if (pc) pc.classList.remove('is-on');
        }
    });
}

function _toolbar() {
    const seg = (id, label, on) =>
        `<button type="button" class="tagging-btn btn ${on ? 'btn-primary' : 'btn-outline'}" `
        + `data-action="view-${id}" aria-pressed="${on ? 'true' : 'false'}">${label}</button>`;
    return '<div class="tagging-toolbar" style="margin:8px 0;display:flex;gap:8px;align-items:center;">'
        + '<div class="tagging-viewtoggle" role="group" aria-label="Tagging view">'
        + seg('deck', 'Triage deck', _view === 'deck')
        + seg('table', 'Table', _view === 'table')
        + '</div>'
        + '<span style="flex:1;"></span>'
        + '<button type="button" class="tagging-btn btn btn-outline" data-action="show-restore">Restore…</button>'
        + '</div>';
}

function _wireToolbar(body) {
    body.querySelectorAll('.tagging-toolbar .tagging-btn').forEach((btn) => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            const action = btn.getAttribute('data-action');
            if (action === 'view-deck' || action === 'view-table') {
                const next = action === 'view-deck' ? 'deck' : 'table';
                if (next === _view) return;
                _view = next;
                try { localStorage.setItem('tagging_view', _view); } catch (err) { /* ignore */ }
                _renderView();
                return;
            }
            if (action === 'show-restore') { _toggleRestore(body, btn); return; }
        });
    });
}

// ---------------------------------------------------------------------------
// Shared write helpers (used by both views)
// ---------------------------------------------------------------------------

// Client-side hard timeouts, slightly above the matching server bounds
// (propose: 120 s server deadline). secureFetch has no timeout of its own, so
// a hung request would otherwise pin _deck.busy forever and permanently
// disable Propose/Accept/Undo — the deck's whole write surface.
const _PROPOSE_ABORT_MS = 130000;
const _APPLY_ABORT_MS = 60000;

/** fetch options with an aborting timeout (cleared when the response lands). */
function _abortable(ms) {
    const ctrl = new AbortController();
    const timer = window.setTimeout(() => ctrl.abort(), ms);
    return { signal: ctrl.signal, done: () => window.clearTimeout(timer) };
}

/** POST /api/tagging/propose for one key. Returns {tags:[], status, error}. */
async function _proposeCall(key) {
    const guard = _abortable(_PROPOSE_ABORT_MS);
    try {
        const r = await secureFetch('/api/tagging/propose', {
            method: 'POST', body: JSON.stringify({ citation_key: key }),
            signal: guard.signal,
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) return { tags: [], status: null, error: d.error || `HTTP ${r.status}` };
        return { tags: (d.proposed || []).map((p) => p.tag), status: d.status, proposed: d.proposed || [] };
    } catch (err) {
        logError('propose failed: ' + String(err));
        return { tags: [], status: null, error: 'network' };
    } finally {
        guard.done();
    }
}

/** A human sentence for a propose status/result. */
function _proposeSummary(res, key) {
    if (res.error) return `${key}: propose failed — ${res.error}.`;
    if (res.tags.length) return `${key}: proposed ${res.tags.join(', ')} (${res.status}).`;
    if (res.status === 'no_text') return `${key}: no proposal — the PDF has no extractable text (scanned/unreadable).`;
    if (res.status === 'empty') return `${key}: the model returned no tags for this paper.`;
    if (res.status === 'low_confidence') return `${key}: only low-confidence tags — none met the threshold.`;
    return `${key}: no tags proposed (${res.status || 'unknown'}).`;
}

// ---------------------------------------------------------------------------
// Table view
// ---------------------------------------------------------------------------

function _table(rows, zoteroOk) {
    if (rows.length === 0) {
        return '<div class="modal-copy" style="padding:16px;">'
            + 'No PDFs found under <code>biblio_articles/</code> in the last scan.</div>';
    }
    const head = `<thead><tr>
        <th scope="col">Key</th>
        <th scope="col">Year</th>
        <th scope="col">Title</th>
        <th scope="col">PDF Finder tags</th>
        <th scope="col">Note tags</th>
        <th scope="col">Zotero tags</th>
        <th scope="col">Add tags → note</th>
    </tr></thead>`;
    const trs = rows.map((r) => {
        const note = r.note || {};
        const zot = r.zotero || {};
        const key = _attr(r.citation_key);
        const proposeBtn = `<button type="button" class="tagging-btn audit-link-alt" data-action="propose" data-key="${key}" aria-label="Propose tags for ${key}">[propose]</button>`;
        const noteBtn = note.present
            ? `<button type="button" class="tagging-btn audit-link-alt" data-action="apply-note" data-key="${key}" aria-label="Apply tags to the Obsidian note">[→note]</button>`
            : '';
        const finderBtn = `<button type="button" class="tagging-btn audit-link-alt" data-action="apply-finder" data-key="${key}" aria-label="Apply tags to the PDF's Finder tags">[→finder]</button>`;
        const zotBtn = zot.present
            ? `<button type="button" class="tagging-btn audit-link-alt" data-action="apply-zotero" data-key="${key}" aria-label="Apply tags to the Zotero item"${zoteroOk ? '' : ' disabled title="Zotero not reachable"'}>[→zotero]</button>`
            : '';
        const writeCell = `<div class="tagging-write">
                   ${proposeBtn}
                   <input type="text" class="tagging-tag-input" data-key="${key}"
                          placeholder="dep mood" autocomplete="off"
                          aria-label="Tags to add to ${key}">
                   ${noteBtn}${finderBtn}${zotBtn}
               </div>
               <div class="tagging-status" data-key="${key}"></div>`;
        return `<tr data-key="${key}">
            <td>${_esc(r.citation_key)}</td>
            <td>${_esc(r.year || '')}</td>
            <td class="cell-title" title="${_attr(r.title || '')}" aria-label="${_attr(r.title || '')}">${_esc((r.title || '').slice(0, 70))}</td>
            <td class="tagging-finder-cell">${_tagsRemovable(r.finder_tags, 'finder')}</td>
            <td class="tagging-note-cell">${note.present ? _tagsRemovable(note.tags, 'note') : '<span title="no Obsidian note">—</span>'}</td>
            <td class="tagging-zotero-cell">${zot.present ? _tagsRemovable(zot.parent_tags, 'zotero') : '<span title="no Zotero item">—</span>'}</td>
            <td>${writeCell}</td>
        </tr>`;
    }).join('');
    // Own scroll wrapper: the report body's clip is disabled while tagging is
    // mounted (tagging-active), so a wide table must contain itself.
    return `<div class="tagging-table-wrap"><table class="audit-table">${head}<tbody>${trs}</tbody></table></div>`;
}

function _wireTable(host) {
    host.querySelectorAll('.tagging-tag-input').forEach(_wireTagEntry);
    host.querySelectorAll('.tagging-btn').forEach((btn) => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            const action = btn.getAttribute('data-action');
            if (action === 'propose') { _tablePropose(btn); return; }
            if (action === 'apply-note') { _tableApply(btn, 'note'); return; }
            if (action === 'apply-finder') { _tableApply(btn, 'finder'); return; }
            if (action === 'apply-zotero') { _tableApply(btn, 'zotero'); return; }
        });
    });
    // Tag deletion (2026-07-22): one delegate for every per-tag × button —
    // rows re-render their cells in place, so per-button wiring would leak.
    if (!host.dataset.taggingRemovalsWired) {
        host.dataset.taggingRemovalsWired = '1';
        host.addEventListener('click', (e) => {
            const btn = e.target.closest('.tagging-cur-x');
            if (!btn || !host.contains(btn)) return;
            e.preventDefault();
            _tableRemoveTag(btn);
        });
    }
}

async function _tablePropose(btn) {
    const key = btn.getAttribute('data-key');
    const row = btn.closest('tr');
    const input = row ? row.querySelector('.tagging-tag-input') : null;
    const status = row ? row.querySelector('.tagging-status') : null;
    const label = btn.textContent;
    btn.textContent = '[…]';
    btn.setAttribute('disabled', 'disabled');
    if (status) status.textContent = 'Proposing…';
    try {
        const res = await _proposeCall(key);
        if (input && res.tags.length) {
            // Proposed tags land as committed CHIPS; the typed draft (if any)
            // stays in the input untouched.
            const have = new Set(_entryTags(input).map((t) => t.toLowerCase()));
            const add = res.tags.filter((t) => !have.has(t.toLowerCase()));
            _entrySetTags(input, (input._tags || []).concat(add));
        }
        _say(_proposeSummary(res, key), status);
    } finally {
        btn.textContent = label;
        btn.removeAttribute('disabled');
    }
}

const _TARGETS = {
    note: { endpoint: '/api/tagging/apply-note-tags', cell: '.tagging-note-cell', label: 'note frontmatter' },
    finder: { endpoint: '/api/tagging/apply-finder-tags', cell: '.tagging-finder-cell', label: 'PDF Finder tags' },
    zotero: { endpoint: '/api/tagging/apply-zotero-tags', cell: '.tagging-zotero-cell', label: 'Zotero item tags' },
};

function _tableApply(btn, target) {
    const cfg = _TARGETS[target];
    const key = btn.getAttribute('data-key');
    const row = btn.closest('tr');
    const input = row ? row.querySelector('.tagging-tag-input') : null;
    const status = row ? row.querySelector('.tagging-status') : null;
    const tags = _entryTags(input);
    if (tags.length === 0) {
        _say('Enter at least one tag first.', status);
        if (input) input.focus();
        return;
    }
    confirmInline(btn, {
        message: `Add ${tags.length} tag(s) to ${key}'s ${cfg.label}?`,
        confirmLabel: 'Add tags',
        tone: 'confirm',
        onConfirm: async () => {
            try {
                const r = await secureFetch(cfg.endpoint, {
                    method: 'POST',
                    body: JSON.stringify({ citation_key: key, tags, confirm: true }),
                });
                const d = await r.json().catch(() => ({}));
                if (!r.ok) {
                    _say(`${cfg.label} write failed: ${d.error || r.status}`, status);
                    return;
                }
                _mergeCell(row, cfg.cell, d.added || []);
                if (input) _entryClear(input);
                const added = (d.added || []).join(', ') || 'none (already present)';
                const rejected = (d.rejected && d.rejected.length)
                    ? ` (rejected: ${d.rejected.join(', ')})` : '';
                _say(`${key} → ${cfg.label}: added ${added}${rejected}.`, status);
            } catch (err) {
                logError(`apply ${target} failed: ` + String(err));
                _say(`${cfg.label} write failed (network).`, status);
            }
        },
    });
}

/** Merge freshly-added tags into a row's tag cell in place (no rescan). */
function _mergeCell(row, selector, added) {
    if (!row || !added.length) return;
    const cell = row.querySelector(selector);
    if (!cell) return;
    // Data-driven read (2026-07-22): the cells render removable tag spans now,
    // so a textContent split would pick up the × glyphs.
    const have = _cellTags(cell);
    const lower = new Set(have.map((t) => t.toLowerCase()));
    added.forEach((t) => { if (!lower.has(String(t).toLowerCase())) have.push(t); });
    _renderCellTags(cell, have, _CELL_TARGET[selector] || 'note');
}

/** Confirm + POST a one-tag removal from one store (table view). */
function _tableRemoveTag(btn) {
    const row = btn.closest('tr');
    const key = row ? row.getAttribute('data-key') : null;
    const tag = btn.getAttribute('data-tag');
    const target = btn.getAttribute('data-target');
    const status = row ? row.querySelector('.tagging-status') : null;
    if (!key || !tag || !_TARGETS[target]) return;
    const label = _TARGETS[target].label;
    confirmInline(btn, {
        message: `Remove tag "${tag}" from ${key}'s ${label}?`,
        confirmLabel: 'Remove tag',
        onConfirm: async () => {
            try {
                const targets = { note: false, finder: false, zotero: false, [target]: true };
                const r = await secureFetch('/api/tagging/remove-tags', {
                    method: 'POST',
                    body: JSON.stringify({ citation_key: key, tags: [tag], targets, confirm: true }),
                });
                const d = await r.json().catch(() => ({}));
                const sub = (d.results || {})[target] || {};
                if (!r.ok || !sub.ok) {
                    _say(`${label}: removal failed — ${sub.error || d.error || r.status}`, status);
                    return;
                }
                const cell = row.querySelector(_TARGETS[target].cell);
                if (cell) {
                    const kept = _cellTags(cell).filter((t) => t.toLowerCase() !== tag.toLowerCase());
                    _renderCellTags(cell, kept, target);
                }
                const undoBit = sub.op_id ? ' Undo via Restore.' : '';
                _say(`${key} → ${label}: removed ${(sub.removed || []).join(', ') || tag}.${undoBit}`, status);
            } catch (err) {
                logError(`remove ${target} tag failed: ` + String(err));
                _say(`${label}: removal failed (network).`, status);
            }
        },
    });
}

// ---------------------------------------------------------------------------
// Deck (triage) view
// ---------------------------------------------------------------------------

const _deck = {
    order: [],        // citation keys in triage order (untagged first)
    idx: 0,
    deferred: new Set(),
    // Undo stack: [{key, ops:[{opId, target, added:[...]}]}], most-recent last.
    // `added` per target is what lets a successful undo un-patch `byKey` (the
    // in-memory row) precisely, so a revisit shows the reverted state.
    undo: [],
    byKey: {},        // key -> row
    targets: { note: true, finder: true, zotero: true },
    // The current card's proposed chips (for the 1-9 number keys). MUST be
    // cleared on every card render: it is per-card state, and a leftover list
    // from the previous paper would let a number key silently inject the
    // WRONG paper's tags into this card's input (the ghost-state bug class the
    // reset in _renderDeck guards against for every other field).
    proposed: [],
    // busy = a WRITE (or Propose) is in flight. It gates Accept, Propose and
    // Undo — never navigation: the user can keep reading/skipping while an
    // optimistic Accept's apply-all settles in the background.
    busy: false,
};

/** Count how many of the three targets already carry any tag. */
function _taggedScore(row) {
    const note = row.note || {};
    const zot = row.zotero || {};
    let n = 0;
    if (Array.isArray(row.finder_tags) && row.finder_tags.length) n += 1;
    if (Array.isArray(note.tags) && note.tags.length) n += 1;
    if (Array.isArray(zot.parent_tags) && zot.parent_tags.length) n += 1;
    return n;
}

/** Build the triage order: untagged first, then by year desc, then key. */
function _deckBuildOrder(rows) {
    const sorted = rows.slice().sort((a, b) => {
        const sa = _taggedScore(a);
        const sb = _taggedScore(b);
        if (sa !== sb) return sa - sb;
        const ya = parseInt(a.year, 10) || 0;
        const yb = parseInt(b.year, 10) || 0;
        if (ya !== yb) return yb - ya;
        return String(a.citation_key).localeCompare(String(b.citation_key));
    });
    return sorted.map((r) => r.citation_key);
}

function _renderDeck(host) {
    const rows = _ctx.rows;
    if (!rows.length) {
        host.innerHTML = '<div class="modal-copy" style="padding:16px;">'
            + 'No PDFs found under <code>biblio_articles/</code> in the last scan.</div>';
        return;
    }
    _deck.byKey = {};
    rows.forEach((r) => { _deck.byKey[r.citation_key] = r; });
    _deck.order = _deckBuildOrder(rows);
    _deck.idx = 0;
    _deck.deferred = new Set();
    _deck.undo = [];
    // busy is module-lifetime state: without this reset, a fetch that never
    // settled (before the abort guards existed) left the re-entered deck with
    // Propose/Accept/Undo permanently dead and no recovery short of a reload.
    _deck.busy = false;

    host.innerHTML = ''
        + '<div class="tagging-deck" tabindex="0" role="group" aria-label="Tagging triage deck">'  // F4 (2026-07-22): aria-label on a role-less div is invalid ARIA — role=group makes it a named region (parity with the ambig deck's M5 fix)
        + '  <div class="tagging-deck-progress" id="tagging-deck-progress"></div>'
        + '  <div class="tagging-deck-lastresult" id="tagging-deck-lastresult"></div>'
        + '  <div class="tagging-deck-card" id="tagging-deck-card"></div>'
        + '  <div class="tagging-deck-pdf" id="tagging-deck-pdf" style="display:none;"></div>'
        + '  <div class="tagging-deck-hint">'
        + '    <kbd>P</kbd> propose · <kbd>A</kbd>/<kbd>Enter</kbd> accept → all targets · '
        + '    <kbd>S</kbd>/<kbd>→</kbd> skip · <kbd>M</kbd> defer · <kbd>⌫</kbd> undo · '
        + '    <kbd>V</kbd> PDF · <kbd>1</kbd>–<kbd>9</kbd> toggle proposed · <kbd>←</kbd> back'
        + '  </div>'
        + '</div>';

    const deckEl = host.querySelector('.tagging-deck');
    deckEl.addEventListener('keydown', _deckKeydown);
    _deckRenderCard();
    // Focus the deck so keyboard verbs work immediately (not while typing tags).
    deckEl.focus();
}

/** Advance to the next un-deferred, in-range card (or the deferred pass). */
function _deckCurrentKey() {
    if (_deck.idx < 0 || _deck.idx >= _deck.order.length) return null;
    return _deck.order[_deck.idx];
}

function _deckProgress() {
    const el = document.getElementById('tagging-deck-progress');
    if (!el) return;
    const total = _deck.order.length;
    const pos = Math.min(_deck.idx + 1, total);
    const deferred = _deck.deferred.size;
    const today = _sessionCount();
    el.textContent = `Paper ${pos} of ${total}`
        + (deferred ? ` · ${deferred} deferred` : '')
        + (_deck.undo.length ? ` · ${_deck.undo.length} undoable` : '')
        + (today ? ` · ${today} triaged today` : '');
}

function _deckRenderCard() {
    const card = document.getElementById('tagging-deck-card');
    if (!card) return;
    // Per-card state resets: a fresh card must never inherit the previous
    // paper's proposed chips (1-9 keys) or its open PDF panel (whose blob
    // would show the WRONG paper beside the new card).
    _deck.proposed = [];
    _deckClosePdf();
    _deckProgress();

    const key = _deckCurrentKey();
    if (key == null) { _deckRenderDone(card); return; }
    const row = _deck.byKey[key];
    if (!row) { _deckSkip(); return; }

    const note = row.note || {};
    const zot = row.zotero || {};
    const zoteroOk = _ctx.zoteroOk;
    const t = _deck.targets;

    // Card Catalog: each routing target carries its lettered legend badge in
    // the target's own color (N gold / F blue / Z red — the reference site's
    // A/G/Q key device). The badge is aria-hidden; the text label carries it.
    const LEGEND = { Note: ['legend-note', 'N'], Finder: ['legend-finder', 'F'], Zotero: ['legend-zotero', 'Z'] };
    const TARGET_ID = { Note: 'note', Finder: 'finder', Zotero: 'zotero' };
    const chip = (label, tags, extra) => {
        const [cls, letter] = LEGEND[label] || ['', ''];
        const badge = cls ? `<span class="legend-badge ${cls}" aria-hidden="true">${letter}</span> ` : '';
        // Current tags are removable per store (tag deletion, 2026-07-22).
        return `<div class="tagging-card-target"><span class="tagging-card-target-label">${badge}${label}</span> `
            + `<span class="tagging-card-target-tags">${_tagsRemovable(tags, TARGET_ID[label] || 'note')}</span>${extra || ''}</div>`;
    };

    // The disabled reason rides in aria-label too — a `title` tooltip is
    // mouse-only (invisible to keyboard/touch, inconsistently read by AT).
    const toggle = (id, label, on, disabled, title) =>
        `<label class="tagging-target-toggle${disabled ? ' is-disabled' : ''}"${title ? ` title="${_attr(title)}"` : ''}>`
        + `<input type="checkbox" data-target="${id}"${on ? ' checked' : ''}${disabled ? ' disabled' : ''}`
        + `${title ? ` aria-label="${_attr(`${label} — ${title}`)}"` : ''}> ${label}</label>`;

    card.innerHTML = ''
        + `<div class="tagging-card-head">`
        + `<span class="tagging-card-key">${_esc(row.citation_key)}</span>`
        + `<span class="tagging-card-year">${_esc(row.year || '')}</span>`
        + `</div>`
        + `<div class="tagging-card-title">${_esc(row.title || '(untitled)')}</div>`
        + `<div class="tagging-card-current">`
        + chip('Note', note.present ? note.tags : null, note.present ? '' : ' <em>(no note)</em>')
        + chip('Finder', row.finder_tags)
        + chip('Zotero', zot.present ? zot.parent_tags : null, zot.present ? '' : ' <em>(no item)</em>')
        + `</div>`
        + `<div class="tagging-card-snippet" id="tagging-card-snippet">Loading excerpt…</div>`
        + `<div class="tagging-card-proposed" id="tagging-card-proposed"></div>`
        + `<div class="tagging-card-entry">`
        + `<input type="text" class="tagging-tag-input" id="tagging-card-input" autocomplete="off" `
        + `placeholder="dep mood" aria-label="Tags to apply to ${_attr(row.citation_key)}">`
        + `</div>`
        + `<div class="tagging-card-targets">`
        + toggle('note', 'Note', t.note && note.present, !note.present, note.present ? '' : 'No Obsidian note')
        + toggle('finder', 'Finder', t.finder, false, '')
        + toggle('zotero', 'Zotero', t.zotero && zot.present, !zot.present || !zoteroOk,
            !zot.present ? 'No Zotero item' : (!zoteroOk ? 'Zotero not reachable' : ''))
        + `</div>`
        + `<div class="tagging-card-actions">`
        + `<button type="button" class="tagging-btn btn btn-outline" data-deck="propose">Propose (P)</button>`
        + `<button type="button" class="tagging-btn btn btn-primary" data-deck="accept">Accept → all (A)</button>`
        + `<button type="button" class="tagging-btn btn btn-outline" data-deck="skip">Skip (S)</button>`
        + `<button type="button" class="tagging-btn btn btn-outline" data-deck="defer">Defer (M)</button>`
        + `<button type="button" class="tagging-btn btn btn-outline" data-deck="pdf">PDF (V)</button>`
        + `<button type="button" class="tagging-btn btn btn-outline" data-deck="undo"${_deck.undo.length ? '' : ' disabled'}>Undo (⌫)</button>`
        + `</div>`
        + `<div class="tagging-card-status" id="tagging-card-status"></div>`;

    card.querySelectorAll('[data-deck]').forEach((btn) => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            const a = btn.getAttribute('data-deck');
            if (a === 'propose') _deckPropose();
            else if (a === 'accept') _deckAccept(btn);
            else if (a === 'skip') _deckSkip();
            else if (a === 'defer') _deckDefer();
            else if (a === 'pdf') _deckTogglePdf();
            else if (a === 'undo') _deckUndo();
        });
    });
    card.querySelectorAll('.tagging-target-toggle input[data-target]').forEach((cb) => {
        cb.addEventListener('change', () => {
            _deck.targets[cb.getAttribute('data-target')] = cb.checked;
        });
    });
    // Tag deletion (2026-07-22): × on a current-tag chip removes it from that
    // ONE store (confirm-gated; the busy gate blocks it mid-apply like Undo).
    card.querySelectorAll('.tagging-cur-x').forEach((btn) => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            _deckRemoveTag(btn, key);
        });
    });
    _wireTagEntry(card.querySelector('#tagging-card-input'));

    // Announce the NEW paper, not just the counter: focus stays on the deck
    // container across skips/accepts, so without this a screen-reader user
    // heard "Paper 3 of 50" from the progress region and triaged blind.
    announceStatus(`Paper ${Math.min(_deck.idx + 1, _deck.order.length)} of `
        + `${_deck.order.length}: ${row.title || row.citation_key}`
        + (row.year ? ` (${row.year})` : ''));

    _deckLoadSnippet(key);
}

function _deckRenderDone(card) {
    // The done view keeps an Undo button + status line when accepts are still
    // undoable: without them, a ⌫ on the done screen had no ceremony anchor
    // and its outcome went to a status element that no longer existed
    // (feedback silently lost — the SR-only bug class in miniature).
    const undoBit = _deck.undo.length
        ? `<p><button type="button" class="tagging-btn btn btn-outline" data-deck="undo">Undo last accept (⌫)</button></p>`
        : '';
    card.innerHTML = '<div class="tagging-deck-done">'
        + '<p><strong>All papers triaged.</strong></p>'
        + (_deck.deferred.size
            ? `<p>${_deck.deferred.size} deferred paper(s) remain — <button type="button" class="tagging-btn btn btn-outline" data-deck="review-deferred">Review deferred</button></p>`
            : '<p>Nothing deferred. Switch to the Table view for a full overview.</p>')
        + undoBit
        + '<div class="tagging-card-status" id="tagging-card-status"></div>'
        + '</div>';
    const rb = card.querySelector('[data-deck="review-deferred"]');
    if (rb) {
        rb.addEventListener('click', (e) => {
            e.preventDefault();
            // Re-enter the deck over just the deferred keys.
            _deck.order = Array.from(_deck.deferred);
            _deck.deferred = new Set();
            _deck.idx = 0;
            _deckRenderCard();
            const deckEl = document.querySelector('.tagging-deck');
            if (deckEl) deckEl.focus();
        });
    }
    const ub = card.querySelector('[data-deck="undo"]');
    if (ub) ub.addEventListener('click', (e) => { e.preventDefault(); _deckUndo(); });
    _deckProgress();
}

// In-flight snippet fetches by key: rapid card navigation (skip-skip-skip)
// re-requests the same keys faster than the server extracts; deduping on the
// promise means each key costs at most ONE request/extraction, no matter how
// fast the user moves. Resolved records still land in _snippetCache.
const _snippetInflight = {};

/** DOM-free snippet fetch: caches successes, returns {snippet,chars}|{error}. */
function _fetchSnippet(key) {
    if (_snippetCache[key] != null) return Promise.resolve(_snippetCache[key]);
    if (_snippetInflight[key]) return _snippetInflight[key];
    const p = (async () => {
        try {
            const r = await secureFetch('/api/tagging/snippet', {
                method: 'POST', body: JSON.stringify({ citation_key: key }),
            });
            const d = await r.json().catch(() => ({}));
            if (!r.ok) {
                // An HTTP error is NOT "the PDF has no text": rendering it as
                // such (and caching it) turned any transient server hiccup
                // into a sticky, false "scanned/unreadable" claim users made
                // skip decisions on. Surface the failure, stay re-fetchable.
                return { error: d.error || `HTTP ${r.status}` };
            }
            const rec = { snippet: (d && d.snippet) || '', chars: (d && d.chars) || 0 };
            _snippetCache[key] = rec; // cache SUCCESSES only
            return rec;
        } catch (err) {
            logError('snippet fetch failed: ' + String(err));
            return { error: 'network' };
        } finally {
            delete _snippetInflight[key];
        }
    })();
    _snippetInflight[key] = p;
    return p;
}

/** Warm the NEXT card's snippet (single-flight, cache-aware, DOM-free). */
function _deckPrefetchNext() {
    const next = _deck.order[_deck.idx + 1];
    if (next == null || _snippetCache[next] != null
        || _snippetInflight[next] || _prefetchInFlight) return;
    _prefetchInFlight = true;
    _fetchSnippet(next).finally(() => { _prefetchInFlight = false; });
}

async function _deckLoadSnippet(key) {
    const el = document.getElementById('tagging-card-snippet');
    if (!el) return;
    if (_snippetCache[key] != null) {
        _deckFillSnippet(el, _snippetCache[key]);
        _deckPrefetchNext();
        return;
    }
    const rec = await _fetchSnippet(key);
    // The card may have re-rendered (or moved on) while fetching: re-query
    // the element and only paint when this is still the current card.
    const live = document.getElementById('tagging-card-snippet');
    if (live && _deckCurrentKey() === key) {
        if (rec.error) live.textContent = `(excerpt unavailable — ${rec.error})`;
        else _deckFillSnippet(live, rec);
    }
    // Prefetch AFTER the current card's fetch settled — the server extracts on
    // the request thread, so the deck never runs two extractions in parallel.
    _deckPrefetchNext();
}

function _deckFillSnippet(el, rec) {
    if (!rec.snippet) {
        el.innerHTML = '<em>No extractable text (scanned / unreadable PDF — Propose will return no tags).</em>';
        return;
    }
    el.textContent = rec.snippet.slice(0, 900) + (rec.chars > 900 ? ' …' : '');
}

async function _deckPropose() {
    if (_deck.busy) return;
    const key = _deckCurrentKey();
    if (key == null) return;
    const status = document.getElementById('tagging-card-status');
    const input = document.getElementById('tagging-card-input');
    _deck.busy = true;
    _say('Proposing…', status);
    try {
        const res = await _proposeCall(key);
        if (_deckCurrentKey() !== key) return;
        if (input && res.tags.length) {
            // Proposed tags become committed CHIPS (never glued to the typed
            // draft, which stays in the input as-is).
            const have = new Set(_entryTags(input).map((t) => t.toLowerCase()));
            const add = res.tags.filter((t) => !have.has(t.toLowerCase()));
            _entrySetTags(input, (input._tags || []).concat(add));
        }
        _deckRenderProposedChips(res.proposed || []);
        _say(_proposeSummary(res, key), status);
    } finally {
        _deck.busy = false;
    }
}

/** Render clickable / number-keyed chips for the proposed tags. */
function _deckRenderProposedChips(proposed) {
    const host = document.getElementById('tagging-card-proposed');
    if (!host) return;
    // Keep the number-key list in sync with what is DISPLAYED (cleared on an
    // empty proposal too — a leftover list would map 1-9 to invisible tags).
    _deck.proposed = proposed.map((p) => p.tag);
    if (!proposed.length) { host.innerHTML = ''; return; }
    // Chips render as `is-on` when their tag is already entered — which is
    // the normal case right after Propose (the tags were just auto-merged).
    // Without this, a chip started visually "off" while its tag was active, so
    // the first click removed the tag while the chip appeared to turn on.
    const input = document.getElementById('tagging-card-input');
    const present = new Set(_entryTags(input).map((t) => t.toLowerCase()));
    host.innerHTML = '<span class="tagging-proposed-label">Proposed:</span> '
        + proposed.map((p, i) => {
            const n = i + 1;
            const on = present.has(String(p.tag).toLowerCase()) ? ' is-on' : '';
            const conf = (typeof p.confidence === 'number') ? ` ${Math.round(p.confidence * 100)}%` : '';
            return `<button type="button" class="tagging-chip${on}" data-chip="${_attr(p.tag)}">`
                + `${n <= 9 ? `<span class="tagging-chip-num">${n}</span>` : ''}`
                + `${_esc(p.tag)}<span class="tagging-chip-conf">${_esc(conf)}</span></button>`;
        }).join(' ');
    host.querySelectorAll('.tagging-chip').forEach((chip) => {
        chip.addEventListener('click', (e) => {
            e.preventDefault();
            _deckToggleChip(chip.getAttribute('data-chip'));
        });
    });
}

/** Toggle a proposed tag in/out of the entry field (committed chips). */
function _deckToggleChip(tag) {
    const input = document.getElementById('tagging-card-input');
    if (!input) return;
    const lower = String(tag).toLowerCase();
    const present = _entryTags(input).some((t) => t.toLowerCase() === lower);
    if (present) {
        _entrySetTags(input, (input._tags || []).filter((t) => t.toLowerCase() !== lower));
        // Also drop it from the typed draft, in case it lived there.
        input.value = _parseTags(input.value)
            .filter((t) => t.toLowerCase() !== lower).join(' ');
    } else {
        _entrySetTags(input, (input._tags || []).concat([tag]));
    }
    // reflect selected state on the proposed chip
    const chip = document.querySelector(`.tagging-chip[data-chip="${_cssEscape(tag)}"]`);
    if (chip) chip.classList.toggle('is-on', !present);
}

function _deckAccept(anchorBtn) {
    if (_deck.busy) return;
    const key = _deckCurrentKey();
    if (key == null) return;
    const input = document.getElementById('tagging-card-input');
    const status = document.getElementById('tagging-card-status');
    const tags = _entryTags(input);
    if (tags.length === 0) {
        _say('Enter or propose at least one tag first.', status);
        if (input) input.focus();
        return;
    }
    // Effective targets = the currently-checked, enabled toggles (a paper with
    // no note / no reachable Zotero item never sends that target).
    const targets = { note: false, finder: false, zotero: false };
    document.querySelectorAll('.tagging-card-targets input[data-target]').forEach((cb) => {
        targets[cb.getAttribute('data-target')] = cb.checked && !cb.disabled;
    });
    const chosen = Object.keys(targets).filter((k) => targets[k]);
    if (chosen.length === 0) {
        _say('Enable at least one target (note / Finder / Zotero).', status);
        return;
    }
    const anchor = anchorBtn || document.querySelector('[data-deck="accept"]');
    confirmInline(anchor, {
        message: `Add ${tags.length} tag(s) to ${key} → ${chosen.join(' + ')}?`,
        confirmLabel: 'Accept',
        tone: 'confirm',
        onConfirm: () => _deckApplyAll(key, tags, targets),
    });
}

// ── Session counter (Card Catalog gamification, deliberately minimal) ──
// "N triaged today" — a day-scoped local count of landed accepts. No streaks,
// no goals: a fact about today's session, shown in the progress line.
const _SESSION_KEY = 'tagging_triage_daily';
function _sessionToday() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
function _sessionCount() {
    try {
        const rec = JSON.parse(localStorage.getItem(_SESSION_KEY) || 'null');
        return (rec && rec.d === _sessionToday()) ? (rec.n | 0) : 0;
    } catch { return 0; }
}
function _sessionBump() {
    try {
        localStorage.setItem(_SESSION_KEY,
            JSON.stringify({ d: _sessionToday(), n: _sessionCount() + 1 }));
    } catch { /* private-mode etc. — the counter is decoration, never load-bearing */ }
}

/** Announce into the deck-level result line (survives card changes) + SR. */
function _sayDeck(msg) {
    announceStatus(msg);
    // Re-queried at write time: a node captured before an optimistic advance
    // would be detached by the card re-render and the feedback silently lost.
    const el = document.getElementById('tagging-deck-lastresult');
    if (el) el.textContent = msg;
}

/** Failure variant with a recovery path (2026-07-22 audit E3).
 *
 * The optimistic advance destroys the failed card's typed/proposed tags (the
 * card re-render deliberately clears per-card state), so a failed apply-all
 * previously left the user to remember WHICH paper failed, arrow back to it,
 * and retype everything. This renders the failure with a "Go back & retry"
 * button that returns to the card and restores the exact tag set as chips —
 * and it announces via the ERROR channel, not the polite one.
 */
function _sayDeckFailure(key, tags, msg) {
    announceError(msg);
    const el = document.getElementById('tagging-deck-lastresult');
    if (!el) return;
    el.textContent = '';
    const span = document.createElement('span');
    span.textContent = msg + ' ';
    el.appendChild(span);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-outline btn-sm';
    btn.textContent = 'Go back & retry';
    btn.addEventListener('click', () => {
        const at = _deck.order.indexOf(key);
        if (at === -1) { _sayDeck(`${key} is no longer in the deck.`); return; }
        _deck.idx = at;
        _deckRenderCard();
        // AFTER the render (which clears per-card state): restore the failed
        // tag set as committed chips so retry is one Accept away.
        const input = document.getElementById('tagging-card-input');
        if (input) {
            input._tags = tags.slice();
            _renderEntryChips(input);
            input.focus();
        }
        _sayDeck(`${key}: tags restored — review and Accept to retry.`);
    });
    el.appendChild(btn);
}

async function _deckApplyAll(key, tags, targets) {
    _deck.busy = true;
    // Optimistic advance: the decision is made and every write is journalled
    // and reversible, so move on immediately — the outcome reconciles into the
    // deck-level status line below (never the card-scoped node, which the
    // advance just detached). busy stays true only to serialize writes; the
    // user can keep navigating/reading meanwhile.
    _deckSkip();
    _sayDeck(`${key}: applying…`);
    const guard = _abortable(_APPLY_ABORT_MS);
    try {
        const r = await secureFetch('/api/tagging/apply-all', {
            method: 'POST',
            body: JSON.stringify({ citation_key: key, tags, targets, confirm: true }),
            signal: guard.signal,
        });
        const d = await r.json().catch(() => ({}));
        // Process per-target results whenever the server sent them — even on a
        // non-200 (defensive; the server isolates target crashes into results
        // now). Bailing on !r.ok when results existed dropped the note op's id
        // from the undo stack even though that write had landed and been
        // journalled, silently breaking the deck's ⌫ contract.
        if (!r.ok && !d.results) {
            _sayDeckFailure(key, tags, `${key}: apply FAILED — ${d.error || r.status}.`);
            return;
        }
        const results = d.results || {};
        const ops = [];
        const parts = [];
        ['note', 'finder', 'zotero'].forEach((tg) => {
            const res = results[tg];
            if (!res) return;
            if (res.skipped) { parts.push(`${tg} skipped (${res.skipped})`); return; }
            if (!res.ok) { parts.push(`${tg} error (${res.error || res.status})`); return; }
            // Record per-target op id + what it added: the id drives the
            // /restore call on undo, the added-list lets undo un-patch the
            // cached row precisely (not just "some tags changed").
            if (res.op_id) ops.push({ opId: res.op_id, target: tg, added: res.added || [] });
            const added = (res.added || []).join(', ');
            // A partial Finder write (some PDFs failed) is NOT a clean ✓ —
            // res.added already reflects only what landed; name the failures.
            if (res.partial) {
                const nFailed = (res.failed || []).length;
                parts.push(`${tg} PARTIAL — ${added || 'nothing'} written, ${nFailed} PDF(s) failed`);
                return;
            }
            // A Zotero web-API write lands on zotero.org, not in the desktop
            // app — that only sees it on its next sync. Saying a bare "✓"
            // makes a user who spot-checks Zotero right away think it failed.
            const pending = res.sync_pending ? ' (syncing to Zotero)' : '';
            parts.push(added ? `${tg} ✓ ${added}${pending}`
                : `${tg} ✓ (already present)`);
        });
        if (ops.length) { _deck.undo.push({ key, ops }); _sessionBump(); }
        _sayDeck(`${key}: ${parts.join(' · ') || 'nothing to write'}.`);
        // Reflect the write into the in-memory row so a revisit shows it.
        _deckPatchRow(key, targets, results);
        _deckProgress();
        const undoBtn = document.querySelector('[data-deck="undo"]');
        if (undoBtn && _deck.undo.length) undoBtn.removeAttribute('disabled');
    } catch (err) {
        logError('apply-all failed: ' + String(err));
        _sayDeckFailure(key, tags, `${key}: apply FAILED (network).`);
    } finally {
        guard.done();
        _deck.busy = false;
    }
}

/** Merge applied tags into the cached row so a revisit reflects the write. */
function _deckPatchRow(key, targets, results) {
    const row = _deck.byKey[key];
    if (!row) return;
    const merge = (arr, added) => {
        const have = Array.isArray(arr) ? arr.slice() : [];
        const lower = new Set(have.map((t) => String(t).toLowerCase()));
        (added || []).forEach((t) => { if (!lower.has(String(t).toLowerCase())) have.push(t); });
        return have;
    };
    if (targets.note && results.note && results.note.ok && row.note) {
        row.note.tags = merge(row.note.tags, results.note.added);
    }
    if (targets.finder && results.finder && results.finder.ok) {
        row.finder_tags = merge(row.finder_tags, results.finder.added);
    }
    if (targets.zotero && results.zotero && results.zotero.ok && row.zotero) {
        row.zotero.parent_tags = merge(row.zotero.parent_tags, results.zotero.added);
    }
}

/** Confirm + POST a one-tag removal from one store (deck view), patch the
 *  cached row, and re-render the card so the chip disappears in place. */
function _deckRemoveTag(btn, key) {
    if (_deck.busy) { _sayDeck('Wait for the current apply to finish.'); return; }
    const tag = btn.getAttribute('data-tag');
    const target = btn.getAttribute('data-target');
    if (!tag || !_TARGETS[target]) return;
    const label = _TARGETS[target].label;
    confirmInline(btn, {
        message: `Remove tag "${tag}" from ${key}'s ${label}?`,
        confirmLabel: 'Remove tag',
        onConfirm: async () => {
            try {
                const targets = { note: false, finder: false, zotero: false, [target]: true };
                const r = await secureFetch('/api/tagging/remove-tags', {
                    method: 'POST',
                    body: JSON.stringify({ citation_key: key, tags: [tag], targets, confirm: true }),
                });
                const d = await r.json().catch(() => ({}));
                const sub = (d.results || {})[target] || {};
                if (!r.ok || !sub.ok) {
                    _sayDeck(`${key}: removal failed — ${sub.error || d.error || r.status}.`);
                    return;
                }
                _deckUnpatchRow(key, target, sub.removed || [tag]);
                _deckRenderCard();
                _sayDeck(`${key} → ${label}: removed ${(sub.removed || []).join(', ') || tag}.`
                    + (sub.op_id ? ' Undo via Restore.' : ''));
            } catch (err) {
                logError('deck remove tag failed: ' + String(err));
                _sayDeck(`${key}: removal failed (network).`);
            }
        },
    });
}

/** Remove the tags an undone op had added from the cached row (undo's inverse
 *  of _deckPatchRow) so revisiting the paper shows the reverted state. */
function _deckUnpatchRow(key, target, added) {
    const row = _deck.byKey[key];
    if (!row || !added.length) return;
    const strip = (arr) => {
        const drop = new Set(added.map((t) => String(t).toLowerCase()));
        return (Array.isArray(arr) ? arr : []).filter((t) => !drop.has(String(t).toLowerCase()));
    };
    if (target === 'note' && row.note) row.note.tags = strip(row.note.tags);
    else if (target === 'finder') row.finder_tags = strip(row.finder_tags);
    else if (target === 'zotero' && row.zotero) row.zotero.parent_tags = strip(row.zotero.parent_tags);
}

/**
 * Teardown when another audit report takes over the shared panel.
 *
 * audit.js calls this (window global — a call, not an import, keeping the
 * module hierarchy) before rendering any non-tagging report. Without it the
 * open PDF panel's blob URL leaked until the deck was next re-entered, and
 * the body kept the tagging-active overflow opt-out under a report that
 * needs its scroll container back.
 */
window.taggingTeardown = function taggingTeardown() {
    _deckClosePdf();
    if (_ctx.body) _ctx.body.classList.remove('tagging-active');
};

/** Close the PDF side panel and release its blob URL. */
function _deckClosePdf() {
    const panel = document.getElementById('tagging-deck-pdf');
    const deckEl = document.querySelector('.tagging-deck');
    if (panel) { panel.style.display = 'none'; panel.innerHTML = ''; }
    if (deckEl) deckEl.classList.remove('with-pdf');
    if (_pdfObjectUrl) { URL.revokeObjectURL(_pdfObjectUrl); _pdfObjectUrl = null; }
}

/** Toggle the current card's full-PDF side panel (V). On-demand only —
 *  never auto-loaded per card, so triage speed is unaffected. */
async function _deckTogglePdf() {
    const panel = document.getElementById('tagging-deck-pdf');
    const deckEl = document.querySelector('.tagging-deck');
    if (!panel || !deckEl) return;
    if (panel.style.display !== 'none') { _deckClosePdf(); return; }
    const key = _deckCurrentKey();
    if (key == null) return;
    panel.style.display = '';
    deckEl.classList.add('with-pdf');
    panel.textContent = 'Loading PDF…';
    try {
        // The PDF bytes MUST come from a header-authenticated fetch rendered
        // via a blob: URL — an iframe src pointing at /api is 403'd by the
        // origin guard (the deck preview-pdf constraint; source-ratcheted).
        const r = await secureFetch(`/api/tagging/pdf?citation_key=${encodeURIComponent(key)}`);
        if (_deckCurrentKey() !== key) return; // moved on: render closed the panel
        if (!r.ok) {
            const d = await r.json().catch(() => ({}));
            panel.textContent = `(PDF unavailable — ${d.error || r.status})`;
            return;
        }
        const blob = await r.blob();
        if (_deckCurrentKey() !== key) return;
        if (_pdfObjectUrl) URL.revokeObjectURL(_pdfObjectUrl);
        _pdfObjectUrl = URL.createObjectURL(blob);
        panel.innerHTML = '';
        const frame = document.createElement('iframe');
        frame.className = 'tagging-pdf-frame';
        frame.title = `PDF — ${key}`;
        frame.src = _pdfObjectUrl;
        panel.appendChild(frame);
        // WKWebView builds that refuse inline blob PDFs get the system viewer.
        const openBtn = document.createElement('button');
        openBtn.type = 'button';
        openBtn.className = 'tagging-btn btn btn-outline';
        openBtn.textContent = 'Open in PDF viewer';
        openBtn.addEventListener('click', async (e) => {
            e.preventDefault();
            try {
                await secureFetch('/api/tagging/open-pdf', {
                    method: 'POST', body: JSON.stringify({ citation_key: key }),
                });
            } catch (err) { logError('tagging open-pdf failed: ' + String(err)); }
        });
        panel.appendChild(openBtn);
    } catch (err) {
        logError('tagging pdf fetch failed: ' + String(err));
        panel.textContent = '(PDF unavailable — network)';
    }
}

function _deckSkip() {
    if (_deck.idx < _deck.order.length) _deck.idx += 1;
    _deckRenderCard();
    const deckEl = document.querySelector('.tagging-deck');
    if (deckEl) deckEl.focus();
}

function _deckPrev() {
    if (_deck.idx > 0) { _deck.idx -= 1; _deckRenderCard(); }
    const deckEl = document.querySelector('.tagging-deck');
    if (deckEl) deckEl.focus();
}

function _deckDefer() {
    const key = _deckCurrentKey();
    if (key != null) _deck.deferred.add(key);
    _deckSkip();
}

/** Undo entry point (⌫ / the Undo button): confirm, then revert.
 *
 * Routed through the confirmInline ceremony because undo REWRITES user data
 * (note bytes / Finder xattrs / Zotero items via /restore) — the Track-6e rule
 * covers the inverse of a write as much as the write, and a bare Backspace
 * (with key auto-repeat: several) must not silently rewrite the vault. Accept
 * pays the same one-extra-keystroke cost for the same reason.
 */
function _deckUndo() {
    if (_deck.busy || !_deck.undo.length) return;
    const last = _deck.undo[_deck.undo.length - 1];
    const anchor = document.querySelector('[data-deck="undo"]');
    if (!anchor) return; // no card/done UI mounted — nothing to anchor to
    confirmInline(anchor, {
        message: `Undo the last accept (${last.key}: revert ${last.ops.length} write(s))?`,
        confirmLabel: 'Undo',
        onConfirm: () => _deckPerformUndo(),
    });
}

async function _deckPerformUndo() {
    if (_deck.busy || !_deck.undo.length) return;
    // Peek, don't pop: if every restore call fails (network, 503 while
    // indexing), the entry must survive so Backspace can retry — popping
    // upfront permanently lost the op ids and stranded the user in the
    // Restore panel hunting for them by hand.
    const last = _deck.undo[_deck.undo.length - 1];
    const status = document.getElementById('tagging-card-status');
    _deck.busy = true;
    _say(`Undoing ${last.key}…`, status);
    let reverted = 0;
    let skipped = 0;
    let failed = 0;
    try {
        for (const op of last.ops) {
            try {
                const r = await secureFetch('/api/tagging/restore', {
                    method: 'POST', body: JSON.stringify({ op_id: op.opId, confirm: true }),
                });
                const d = await r.json().catch(() => ({}));
                if (r.ok && d.status !== 'skipped') {
                    reverted += 1;
                    _deckUnpatchRow(last.key, op.target, op.added);
                } else if (r.ok) {
                    skipped += 1; // target changed after our write — server refused, correctly
                } else {
                    failed += 1;
                }
            } catch (err) {
                logError('deck undo failed: ' + String(err));
                failed += 1;
            }
        }
        // Consume the entry unless NOTHING got through (total failure ⇒ keep
        // it retryable). Partial success consumes too: the reverted ops are
        // spent server-side, so a retry would only re-fail the failed ones —
        // the Restore panel is the per-op recovery surface at that point.
        if (reverted || skipped) _deck.undo.pop();
        const detail = [`reverted ${reverted}`, skipped ? `skipped ${skipped}` : '', failed ? `FAILED ${failed}` : '']
            .filter(Boolean).join(', ');
        _say(`Undo ${last.key}: ${detail} of ${last.ops.length} op(s)${failed ? ' — see Restore… for the rest' : ''}.`, status);
        _deckProgress();
        const undoBtn = document.querySelector('[data-deck="undo"]');
        if (undoBtn && !_deck.undo.length) undoBtn.setAttribute('disabled', 'disabled');
    } finally {
        _deck.busy = false;
    }
}

function _deckKeydown(e) {
    const tag = (e.target && e.target.tagName) || '';
    const type = (e.target && e.target.type) || '';
    // An open confirm strip owns its keys entirely (it also stops propagation
    // itself — this is defence in depth): without this, Enter on the focused
    // Confirm button re-fired Accept and re-opened the strip forever.
    if (e.target && e.target.closest && e.target.closest('.inline-confirm-strip')) return;
    // A focused button gets native activation: swallowing Space/Enter here
    // turned "press Space on the Skip button" into Defer (WCAG 2.1.1).
    if (tag === 'BUTTON') return;
    // "Typing" = a TEXT-entry control only. A focused checkbox (the target
    // toggles) is an INPUT too, but swallowing the verbs there made the whole
    // keyboard go dead after clicking a toggle — only text fields need their
    // keystrokes back.
    const typing = tag === 'TEXTAREA' || (tag === 'INPUT' && type !== 'checkbox' && type !== 'radio');
    // While typing tags, only intercept Enter (accept) — let everything else type.
    if (typing) {
        if (e.key === 'Enter') { e.preventDefault(); _deckAccept(); }
        return;
    }
    switch (e.key) {
        case 'p': case 'P': e.preventDefault(); _deckPropose(); break;
        case 'a': case 'A': case 'Enter': e.preventDefault(); _deckAccept(); break;
        case 's': case 'S': case 'ArrowRight': e.preventDefault(); _deckSkip(); break;
        // Space is deliberately NOT a verb (2026-07-22 audit F4): a deck
        // taller than the viewport (PDF panel open) scrolls with Space, and
        // binding it to Defer silently deferred papers on every scroll press.
        case 'm': case 'M': e.preventDefault(); _deckDefer(); break;
        case 'v': case 'V': e.preventDefault(); _deckTogglePdf(); break;
        // Escape closes the PDF panel (parity with the ambig deck's L3 fix).
        case 'Escape': {
            const pdfPanel = document.getElementById('tagging-deck-pdf');
            if (pdfPanel && pdfPanel.style.display !== 'none') {
                e.preventDefault(); _deckClosePdf();
            }
            break;
        }
        case 'ArrowLeft': e.preventDefault(); _deckPrev(); break;
        case 'Backspace': e.preventDefault(); _deckUndo(); break;
        default:
            if (/^[1-9]$/.test(e.key)) {
                const t = _deck.proposed[parseInt(e.key, 10) - 1];
                if (t) { e.preventDefault(); _deckToggleChip(t); }
            }
    }
}

// ---------------------------------------------------------------------------
// Restore panel (shared by both views)
// ---------------------------------------------------------------------------

async function _toggleRestore(body, btn) {
    const panel = body.querySelector('#tagging-restore-panel');
    if (!panel) return;
    if (panel.style.display !== 'none') {
        panel.style.display = 'none';
        btn.textContent = 'Restore…';
        return;
    }
    btn.textContent = 'Hide restore';
    panel.style.display = '';
    panel.textContent = 'Loading history…';
    try {
        const r = await secureFetch('/api/tagging/manifest');
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { panel.innerHTML = `<div class="audit-summary-warning">${_esc(d.error || 'Manifest unavailable')}</div>`; return; }
        // The status line is VISIBLE (not just the SR announcer): revert
        // failures previously went only to the sr-only region, the exact
        // silent-feedback class the deck/table status lines were added to fix.
        panel.innerHTML = _restoreList(d.ops || [])
            + '<div class="tagging-status" id="tagging-restore-status"></div>';
        _wireRestore(panel);
    } catch (e) {
        logError('tagging manifest failed: ' + String(e));
        panel.innerHTML = `<div class="audit-summary-warning">${_esc(String(e))}</div>`;
    }
}

function _wireRestore(panel) {
    panel.querySelectorAll('[data-action="revert-op"]').forEach((btn) => {
        btn.addEventListener('click', (e) => { e.preventDefault(); _revert(btn); });
    });
}

function _restoreList(ops) {
    if (ops.length === 0) {
        return '<div class="modal-copy" style="padding:12px;">No tag writes to restore yet.</div>';
    }
    const head = '<thead><tr><th scope="col">When</th><th scope="col">Key</th>'
        + '<th scope="col">Kind</th><th scope="col">Added</th><th scope="col">State</th><th scope="col"></th></tr></thead>';
    const rows = ops.map((op) => {
        const added = Array.isArray(op.added) ? op.added.join(', ') : '';
        const revertible = op.state === 'applied';
        const action = revertible
            ? `<button type="button" class="tagging-btn audit-link-alt" data-action="revert-op" data-op-id="${_attr(op.op_id)}">[revert]</button>`
            : '';
        return `<tr data-op-id="${_attr(op.op_id)}">
            <td>${_esc((op.ts || '').replace('T', ' ').slice(0, 19))}</td>
            <td>${_esc(op.citation_key || '')}</td>
            <td>${_esc(op.kind || '')}</td>
            <td>${_esc(added)}</td>
            <td>${_esc(op.state || '')}</td>
            <td>${action}</td>
        </tr>`;
    }).join('');
    return `<div class="tagging-table-wrap"><table class="audit-table">${head}<tbody>${rows}</tbody></table></div>`;
}

function _revert(btn) {
    const opId = btn.getAttribute('data-op-id');
    const status = document.getElementById('tagging-restore-status');
    confirmInline(btn, {
        message: 'Revert this tag write (restore the note’s prior frontmatter)?',
        confirmLabel: 'Revert',
        onConfirm: async () => {
            try {
                const r = await secureFetch('/api/tagging/restore', {
                    method: 'POST',
                    body: JSON.stringify({ op_id: opId, confirm: true }),
                });
                const d = await r.json().catch(() => ({}));
                if (!r.ok && !d.status) { _say(`Restore failed: ${d.error || r.status}`, status); return; }
                _say(`Restore: ${d.message || d.status || 'done'}.`, status);
                const row = btn.closest('tr');
                if (row) {
                    // Only a clean full revert spends the op server-side; a
                    // skipped/partial/error op stays `applied` and retryable,
                    // so its [revert] button must survive.
                    const spent = d.status === 'reverted' || d.status === 'already_reverted';
                    const stateCell = row.children[4];
                    if (stateCell) {
                        stateCell.textContent = spent ? 'reverted' : `applied (${d.status || 'unknown'})`;
                    }
                    if (spent) btn.remove();
                }
            } catch (err) {
                logError('tagging restore failed: ' + String(err));
                _say('Restore failed (network).', status);
            }
        },
    });
}
