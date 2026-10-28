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
import { confirmInline, announceStatus, announceError, showTaskError, installTableControls, statusLine, dailyCounter } from './ui.js';

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
    if (target === 'zotero' && !_ctx.zoteroOk) return _tags(tags);
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
/* Snippet cache: key -> {snippet, chars} (successful fetches ONLY — an error
 * must stay re-fetchable, never a sticky "no text"). Module-scoped so it
 * survives card re-renders AND the Table<->Deck toggle (_renderView).
 *
 * PERF (2026-09-02): bounded, and it now SURVIVES a scope flip.
 * Before: a plain object with no bound, holding a text excerpt per card for
 *   as long as the panel lived — and wiped wholesale on every
 *   taggingRenderInto, so flipping the with-PDF/all scope threw away every
 *   snippet the user had already paid a server extraction for and re-fetched
 *   them one card at a time.
 * Change: a Map with an LRU bound of _SNIPPET_CACHE_MAX (200) entries, kept
 *   across renders. The snippet is a pure function of the citation key and
 *   the PDF's own cache, so a re-render cannot invalidate it.
 * Safe because: only SUCCESSES are stored (unchanged), an eviction costs one
 *   re-fetch, and the deck reads through the same accessor either way.
 * Pinned by: tests/js/taggingSnippetCache.test.js. */
const _SNIPPET_CACHE_MAX = 200;
const _snippetCache = new Map();

function _snippetGet(key) {
    const hit = _snippetCache.get(key);
    if (hit === undefined) return null;
    _snippetCache.delete(key);
    _snippetCache.set(key, hit);       // most-recently-used
    return hit;
}

function _snippetPut(key, rec) {
    _snippetCache.delete(key);
    _snippetCache.set(key, rec);
    while (_snippetCache.size > _SNIPPET_CACHE_MAX) {
        _snippetCache.delete(_snippetCache.keys().next().value);
    }
}
// Single-flight guard for the next-card snippet prefetch: the server runs
// snippet extraction on the request thread, so never two in parallel.
let _prefetchInFlight = false;
// The deck PDF panel's blob URL singleton (revoked before each replacement
// and on close — the deck.js _previewObjectUrl pattern).
let _pdfObjectUrl = null;
let _view = 'deck';
try { _view = localStorage.getItem('tagging_view') || 'deck'; } catch (e) { _view = 'deck'; }

// Which inventory scope to request. `with_pdf` is the server default and the
// historical set; `all` also returns records with no local PDF — ~2 100 of
// them in the maintainer's library, every one writable via its note and its
// Zotero item. Only this projection ever hid them, so the toggle is the whole
// distance between "invisible" and "taggable". Opt-in rather than default
// because it multiplies the row count, which changes the deck's character.
let _scope = 'with_pdf';
// Cohort filter (2026-08-26): a VIEW filter over the rows already loaded, not
// a re-fetch — the depth is computed from fields every row already carries, so
// unlike `_scope` (which changes what the server returns) this only re-renders.
let _cohort = 'all';
try {
    _scope = localStorage.getItem('tagging_scope') === 'all' ? 'all' : 'with_pdf';
} catch (e) { _scope = 'with_pdf'; }
try {
    const c = localStorage.getItem('tagging_cohort');
    _cohort = ['all', 'untagged', 'under', 'done'].includes(c) ? c : 'all';
} catch (e) { _cohort = 'all'; }
// Reported by the last inventory response, for the toggle's own label.
let _pdflessCount = 0;

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
        resp = await secureFetch(`/api/tagging/inventory?scope=${encodeURIComponent(_scope)}`);
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
        body.innerHTML = '<div class="task-error-boundary"></div>';
        showTaskError(body.firstChild, msg, [
            { label: 'Retry', primary: true, onClick: () => taggingRenderInto(body, meta, isCurrent) },
        ]);
        return;
    }

    const rows = (data && Array.isArray(data.rows)) ? data.rows : [];
    _pdflessCount = (data && Number(data.pdfless_count)) || 0;
    // The picker selection belongs to ONE scan snapshot (its own comment's
    // rule, previously unimplemented — 2026-08-13 review): a new `scanned_at`
    // means new rows, and keys picked against the old snapshot would keep
    // counting toward "N selected" and ride the Copy-keys export unseen.
    // Scope/view toggles re-fetch the SAME snapshot and stay non-lossy.
    _noteInventorySnapshot((data && Number(data.scanned_at)) || 0);
    body.textContent = 'Loading vocabulary…';

    // Both are independent of the inventory payload and of each other, so
    // they run CONCURRENTLY (they were sequential — three round-trips of
    // frozen "Loading…", with the Zotero probe's 3 s timeout gating a render
    // it has nothing to do with). Both stay best-effort: a vocab failure just
    // means no autocomplete, a probe failure means the [→zotero] buttons
    // start disabled.
    const [vocabRows, zoteroState] = await Promise.all([
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
                    return zd || {};
                }
            } catch (e) {
                logError('tagging zotero probe failed: ' + String(e));
            }
            return {};
        })(),
    ]);
    if (!live()) return;

    // `isCurrent` rides the context so a scope change can re-fetch through the
    // SAME latest-wins gate the audit panel handed us — re-entering without it
    // would let a superseded inventory repaint over a newer report.
    _ctx = { rows, vocabRows, zoteroOk: zoteroState.writable === true, zoteroState, body, meta, isCurrent };
    // The snippet cache is deliberately NOT cleared here: a snippet is a pure
    // function of the citation key, so a re-fetch of the SAME snapshot (a
    // scope flip, a view toggle) describes the same PDFs and the extraction
    // the server already paid for still applies. A NEW scan does clear it —
    // see _noteInventorySnapshot, beside the selection it shares that rule
    // with.
    _renderView();
}

/** Render the currently-selected view (deck or table) into the stored body. */
function _renderView() {
    const { rows, vocabRows, zoteroOk, body, meta } = _ctx;
    if (!body) return;

    // PERF (2026-09-02): release the deck's PDF blob before the markup that
    // shows it is destroyed. Switching deck -> table re-renders the whole body,
    // so the panel element goes with it — but _deckClosePdf (which owns the
    // revoke) was only wired to the V toggle, the card change and
    // window.taggingTeardown, none of which fire on a view switch. The blob
    // therefore survived, unreferenced, until the deck was next re-entered.
    // Idempotent: it no-ops when no panel is open.
    // Pinned by tests/js/previewBlobTeardown.test.js
    _deckClosePdf();

    // Opt the report body out of its overflow clip while tagging is mounted:
    // its overflow-x:auto coerces overflow-y to auto (CSS spec), which was
    // cutting off the type-ahead dropdown near the panel's bottom edge.
    // taggingTeardown removes the class when another report takes the panel.
    body.classList.add('tagging-active');

    if (meta) {
        meta.style.display = '';
        // Say which scope produced this count. Under `all` the PDF-less share
        // is stated because those rows behave differently (no Finder target,
        // no excerpt, metadata-mode Propose); under `with_pdf` the omission is
        // named rather than left implicit.
        const scopeNote = _scope === 'all'
            ? ` (${_esc(_pdflessCount)} with no local PDF)`
            : ' with a local PDF';
        meta.innerHTML = `${_esc(rows.length)} paper(s) under <code>biblio_articles/</code>`
            + `${scopeNote}. `
            + '<strong>Propose</strong> suggests tags from the PDF; edit (type-ahead from '
            + `<code>_tags.md</code>, ${_esc(vocabRows.length)} tags) then apply to the note, `
            + `the PDF (Finder) or Zotero (<span class="tagging-access-readiness">${zoteroOk ? 'authorized' : 'not ready'}</span>). `
            + 'Every write is confirmed and audit-logged. <strong>Restore</strong> verifies the recorded target before undoing it.';
    }

    body.innerHTML = _toolbar()
        + `<div class="tagging-zotero-access"><p>${_esc(_ctx.zoteroState?.note || 'Refresh Zotero access before writing.')}</p><button type="button" class="btn btn-outline btn-sm" data-action="authorize-zotero">Authorize Zotero Desktop…</button><span class="tagging-access-status"></span></div>`
        + '<div id="tagging-view-host"></div>'
        + '<div id="tagging-restore-panel" style="display:none;"></div>';
    _wireToolbar(body);
    body.querySelector('[data-action="authorize-zotero"]').onclick = event => _authorizeZotero(event.currentTarget);

    const host = body.querySelector('#tagging-view-host');
    // The cohort filter narrows both views. It is applied HERE rather than
    // inside each view so the deck's triage order and the table's rows can
    // never disagree about which papers are in scope.
    const shown = rows.filter(_inCohort);
    if (_view === 'table') {
        host.innerHTML = _table(shown, zoteroOk);
        _wireTable(host);
    } else {
        _renderDeck(host, shown);
    }
}

/** Explicit transport selection and authorization; never replay a tag write. */
async function _authorizeZotero(button) {
    const context = _ctx;
    const live = () => _ctx === context && (!context.isCurrent || context.isCurrent());
    const status = context.body.querySelector('.tagging-access-status');
    try {
        const response = await secureFetch('/api/zotlink/local-status');
        const state = await response.json();
        if (!live()) return;
        if (!response.ok || !state.supports_writes) throw new Error(state.error || 'Open a Zotero version supporting local writes.');
        const switchTransport = context.zoteroState?.mode === 'web';
        confirmInline(button, {
            message: (switchTransport ? 'Switch Zotero transport from Web to local (a new audit scan will be needed), then request authorization? ' : 'Request Zotero Desktop authorization? Automatic transport will use Desktop for this session. ')
                + 'Allow permits one write; Always Allow permits writes for this session. No tag write will be replayed.',
            confirmLabel: 'Authorize Desktop',
            onConfirm: async () => {
                if (!live()) return;
                try {
                    if (switchTransport) {
                        const changed = await secureFetch('/api/config', {method: 'POST', body: JSON.stringify({tagging_zotero_transport: 'local'})});
                        if (!changed.ok) throw new Error((await changed.json()).error || 'Could not switch transport.');
                    }
                    const granted = await secureFetch('/api/zotlink/authorize-local', {method: 'POST', body: JSON.stringify({confirm: true, server_id: state.server_id})});
                    const result = await granted.json();
                    if (!granted.ok) throw new Error(result.error || 'Authorization failed.');
                    if (!live()) return;
                    if (switchTransport) {
                        _say('Desktop authorized. Run Library Audit again, then review and apply the tags.', status);
                    } else {
                        const probe = await secureFetch('/api/tagging/zotero-available');
                        const readiness = await probe.json();
                        if (!live()) return;
                        if (!probe.ok) throw new Error(readiness.error || 'Could not refresh Zotero access.');
                        _updateZoteroReadiness(readiness);
                        _say(readiness.writable ? 'Desktop authorized. Review your tags, then apply.' : (readiness.note || 'Zotero is not ready.'), status);
                    }
                } catch (error) { if (live()) _say(error.message, status); }
            },
        });
    } catch (error) { if (live()) _say(error.message, status); }
}

/** Change access controls in place: authorizing must not discard user drafts. */
function _updateZoteroReadiness(state) {
    _ctx.zoteroState = state;
    _ctx.zoteroOk = state.writable === true;
    const {body, meta, zoteroOk} = _ctx;
    const label = meta?.querySelector('.tagging-access-readiness');
    if (label) label.textContent = zoteroOk ? 'authorized' : 'not ready';
    const note = body.querySelector('.tagging-zotero-access p');
    if (note) note.textContent = state.note || 'Review your tags before writing.';
    body.querySelectorAll('[data-action="apply-zotero"]').forEach(button => {
        button.disabled = !zoteroOk;
        button.title = zoteroOk ? '' : 'Zotero not authorized; use Authorize Zotero Desktop';
    });
    body.querySelectorAll('tr[data-key]').forEach(tr => {
        const row = _ctx.rows.find(r => r.citation_key === tr.dataset.key);
        const cell = tr.querySelector('.tagging-zotero-cell');
        if (row && cell) cell.innerHTML = _tagsRemovable(row.zotero?.parent_tags || [], 'zotero');
    });
    const key = _deck?.order?.[_deck.idx];
    const row = _deck?.byKey?.[key];
    const checkbox = body.querySelector('.tagging-target-toggle input[data-target="zotero"]');
    if (checkbox) {
        checkbox.disabled = !row?.zotero?.present || !zoteroOk;
        const reason = !row?.zotero?.present ? 'No Zotero item' : (!zoteroOk ? 'Zotero not authorized; use Authorize Zotero Desktop' : '');
        checkbox.closest('label').classList.toggle('is-disabled', checkbox.disabled);
        checkbox.closest('label').title = reason;
        checkbox.setAttribute('aria-label', reason ? `Zotero — ${reason}` : 'Zotero');
    }
    const tags = body.querySelector('[data-current-target="zotero"]');
    if (tags && row) {
        tags.innerHTML = _tagsRemovable(row.zotero?.parent_tags || [], 'zotero');
        tags.querySelectorAll('.tagging-cur-x').forEach(button => {
            button.addEventListener('click', event => { event.preventDefault(); _deckRemoveTag(button, key); });
        });
    }
}

/** Rows in a cohort, for the selector's labels. Exact for the active scope. */
function _cohortCount(which) {
    const rows = (_ctx && _ctx.rows) || [];
    if (which === 'all') return rows.length;
    const prev = _cohort;
    _cohort = which;
    try {
        return rows.reduce((n, r) => n + (_inCohort(r) ? 1 : 0), 0);
    } finally {
        _cohort = prev;
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
        // Chip commits/removals are visual-only otherwise (2026-08-18).
        announceStatus(`Tag ${v.tag} added.`);
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
            const committed = _parseTags(input.value);
            _entrySetTags(input, (input._tags || []).concat(committed));
            input.value = '';
            close();
            if (committed.length) announceStatus(`Tag ${committed.join(', ')} added.`);
            return;
        }
        if (e.key === 'Backspace' && !input.value && (input._tags || []).length) {
            e.preventDefault(); e.stopPropagation();
            const removed = input._tags[input._tags.length - 1];
            input._tags = input._tags.slice(0, -1);
            _renderEntryChips(input);
            announceStatus(`Tag ${removed} removed.`);
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
        // Scope. Labelled with the count only once it is known (i.e. while
        // scope=all is active), because under with_pdf the server reports
        // pdfless_count as 0 by construction — printing "0 hidden" there
        // would assert the opposite of the truth.
        + '<label class="tagging-scope" style="display:flex;gap:6px;align-items:center;">'
        + `<input type="checkbox" id="tagging-scope-all" data-action="scope"${_scope === 'all' ? ' checked' : ''}>`
        + '<span>Include entries with no local PDF'
        + (_scope === 'all' && _pdflessCount ? ` (${_esc(_pdflessCount)})` : '')
        + '</span></label>'
        // Cohort. Counts come from the loaded rows, so they are exact for the
        // active scope and update with it — never a stale figure from a scan.
        + '<label class="tagging-scope" style="display:flex;gap:6px;align-items:center;">'
        + '<span>Tags:</span>'
        + `<select id="tagging-cohort" data-action="cohort" aria-label="Filter by how many tags a paper carries">
             <option value="all"${_cohort === 'all' ? ' selected' : ''}>any (${_cohortCount('all')})</option>
             <option value="untagged"${_cohort === 'untagged' ? ' selected' : ''}>none (${_cohortCount('untagged')})</option>
             <option value="under"${_cohort === 'under' ? ' selected' : ''}>2 or fewer (${_cohortCount('under')})</option>
             <option value="done"${_cohort === 'done' ? ' selected' : ''}>3 or more (${_cohortCount('done')})</option>
           </select>`
        + '</label>'
        + '<span style="flex:1;"></span>'
        + (_view === 'table' ? _pickToolbar() : '')
        + '<button type="button" class="tagging-btn btn btn-outline" data-action="show-restore">Restore…</button>'
        + '</div>';
}

// ── Hand-picking a subset (2026-08-13) ────────────────────────────────────
//
// `papermind tagging batch` had no subset selection: only --limit, a head
// slice of a list sorted by (year, key). Choosing WHICH papers to tag is a
// judgement call best made looking at titles and current tags — i.e. here —
// so the table picks and the CLI consumes the list through --keys-file.
//
// Selection is module state, deliberately not persisted: it belongs to one
// scan snapshot, and restoring it later would re-offer papers that may have
// been tagged since. It survives a re-render within the session (the
// checkbox reads `_picked`), which is what makes scope-switching non-lossy —
// and it is CLEARED when the inventory reports a new `scanned_at`
// (_noteInventorySnapshot), because "belongs to one snapshot" was previously
// only a comment: keys picked against an older scan silently outlived it.
const _picked = new Set();
let _pickedScanStamp = null;

/** Drop the selection when the inventory comes from a NEWER scan. */
function _noteInventorySnapshot(scannedAt) {
    if (_pickedScanStamp !== null && scannedAt !== _pickedScanStamp) {
        _picked.clear();
        // Same rule for the snippet excerpts (2026-09-02): they survive a
        // scope flip or a re-render of the SAME snapshot — that is the point
        // of caching them — but a new scan is a new set of papers, so the
        // excerpts go with the selection rather than outliving their rows.
        _snippetCache.clear();
    }
    _pickedScanStamp = scannedAt;
}

function _pickToolbar() {
    const n = _picked.size;
    return '<span class="tagging-pick-count">' + (n ? `${n} selected` : '') + '</span>'
        // A separate span from the count: overwriting the count with a message
        // would destroy the number the buttons act on. Not a live region —
        // `_say` announces through the static announcer (the WebKit hazard).
        + '<span class="tagging-pick-status muted"></span>'
        + '<button type="button" class="tagging-btn btn btn-outline" data-action="pick-all">Select all</button>'
        + `<button type="button" class="tagging-btn btn btn-outline" data-action="pick-none"${n ? '' : ' disabled'}>Clear</button>`
        + `<button type="button" class="tagging-btn btn btn-outline" data-action="pick-copy"${n ? '' : ' disabled'}>Copy keys</button>`;
}

function _refreshPickToolbar(body) {
    const host = body.querySelector('.tagging-toolbar');
    if (!host) return;
    const count = host.querySelector('.tagging-pick-count');
    if (count) count.textContent = _picked.size ? `${_picked.size} selected` : '';
    ['pick-none', 'pick-copy'].forEach((a) => {
        const b = host.querySelector(`[data-action="${a}"]`);
        if (b) b.disabled = _picked.size === 0;
    });
}

/**
 * Hand the selection to the CLI as a `--keys-file` body.
 *
 * Clipboard-first with a visible textarea fallback: a sandboxed webview can
 * refuse `navigator.clipboard`, and a "Copied!" toast that copied nothing is
 * worse than no button. The comment header makes the pasted block
 * self-describing — the file format ignores `#` lines.
 */
async function _pickCopy(btn, body) {
    const keys = [..._picked].sort();
    const text = `# papermind tagging batch --keys-file <this file>\n`
        + `# ${keys.length} paper(s) selected in CHERQAiN\n`
        + keys.join('\n') + '\n';
    const status = body.querySelector('.tagging-pick-status');
    try {
        await navigator.clipboard.writeText(text);
        _say(`${keys.length} key(s) copied — pass them with --keys-file.`, status);
        return;
    } catch (err) {
        void err;
    }
    let box = body.querySelector('#tagging-pick-export');
    if (!box) {
        box = document.createElement('textarea');
        box.id = 'tagging-pick-export';
        box.readOnly = true;
        box.rows = 6;
        box.style.width = '100%';
        box.setAttribute('aria-label', 'Selected citation keys — copy manually');
        btn.closest('.tagging-toolbar').insertAdjacentElement('afterend', box);
    }
    box.value = text;
    box.focus();
    box.select();
    _say('Clipboard unavailable — the keys are selected below; copy them manually.',
         status);
}

function _wireToolbar(body) {
    // Scope is a checkbox, not a .tagging-btn, and unlike the view toggle it
    // changes the DATA rather than the rendering — so it re-fetches instead of
    // calling _renderView().
    // Cohort is a pure VIEW filter over rows already loaded, so it re-renders
    // rather than re-fetching (unlike scope, which changes what the server
    // returns).
    const cohortSel = body.querySelector('#tagging-cohort');
    if (cohortSel) {
        cohortSel.addEventListener('change', () => {
            _cohort = cohortSel.value;
            try { localStorage.setItem('tagging_cohort', _cohort); } catch (e) { /* private mode */ }
            _renderView();
        });
    }
    const scopeBox = body.querySelector('#tagging-scope-all');
    if (scopeBox) {
        scopeBox.addEventListener('change', () => {
            _scope = scopeBox.checked ? 'all' : 'with_pdf';
            try { localStorage.setItem('tagging_scope', _scope); } catch (err) { /* ignore */ }
            taggingRenderInto(_ctx.body, _ctx.meta, _ctx.isCurrent);
        });
    }
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
            if (action === 'pick-all' || action === 'pick-none') {
                const on = action === 'pick-all';
                body.querySelectorAll('.tagging-pick').forEach((cb) => {
                    cb.checked = on;
                    if (on) _picked.add(cb.getAttribute('data-key'));
                    else _picked.delete(cb.getAttribute('data-key'));
                });
                if (!on) _picked.clear();
                _refreshPickToolbar(body);
                return;
            }
            if (action === 'pick-copy') { _pickCopy(btn, body); return; }
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
        // `source`/`has_pdf` ride along because the evidence behind a proposal
        // changes how much weight it deserves: since metadata mode, a paper
        // with no PDF is classified from its abstract alone. The server sends
        // both fields precisely so the user can see that before accepting.
        return {
            tags: (d.proposed || []).map((p) => p.tag),
            status: d.status, proposed: d.proposed || [],
            source: d.source || null, has_pdf: d.has_pdf,
        };
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
    // Name the evidence when it is NOT the paper's text: a metadata-mode
    // proposal comes from the abstract and bibliographic fields alone, and
    // deserves a closer look before it is written to three stores.
    const from = res.source === 'metadata' ? ', from metadata only — no PDF text' : '';
    if (res.tags.length) return `${key}: proposed ${res.tags.join(', ')} (${res.status}${from}).`;
    if (res.status === 'no_text') {
        return res.has_pdf === false
            ? `${key}: no proposal — no local PDF, and too little metadata (no abstract or keywords) to classify from.`
            : `${key}: no proposal — the PDF has no extractable text (scanned/unreadable).`;
    }
    if (res.status === 'empty') return `${key}: the model returned no tags for this paper${from}.`;
    if (res.status === 'low_confidence') return `${key}: only low-confidence tags — none met the threshold${from}.`;
    return `${key}: no tags proposed (${res.status || 'unknown'}).`;
}

// ---------------------------------------------------------------------------
// Table view
// ---------------------------------------------------------------------------

function _table(rows, zoteroOk) {
    if (rows.length === 0) {
        return '<div class="modal-copy" style="padding:16px;">'
            + (_cohort !== 'all' && (_ctx.rows || []).length > 0
                ? 'No papers in this tag cohort. Choose <strong>any</strong> above to see them all.</div>'
                : _scope === 'all'
                    ? 'No biblio records in the last scan.</div>'
                    : 'No PDFs found under <code>biblio_articles/</code> in the last scan. Tick "include entries with no PDF" to see records that have no local file.</div>');
    }
    const head = `<thead><tr>
        <th scope="col"><span class="sr-only">Select</span></th>
        <th scope="col">Key</th>
        <th scope="col">Year</th>
        <th scope="col">Title</th>
        <th scope="col" title="Distinct tags across the surfaces this paper has">#</th>
        <th scope="col">PDF Finder tags</th>
        <th scope="col">Note tags</th>
        <th scope="col">Zotero tags</th>
        <th scope="col" data-sort="off">Add tags → note <span class="tagging-entry-hint">(space/comma separates — e.g. dep mood)</span></th>
    </tr></thead>`;
    const trs = rows.map((r) => {
        const note = r.note || {};
        const zot = r.zotero || {};
        const key = _attr(r.citation_key);
        const proposeBtn = `<button type="button" class="tagging-btn audit-link-alt" data-action="propose" data-key="${key}" aria-label="Propose tags for ${key}">[propose]</button>`;
        const noteBtn = note.present
            ? `<button type="button" class="tagging-btn audit-link-alt" data-action="apply-note" data-key="${key}" aria-label="${_attr(`Apply tags to the Obsidian note of ${key}`)}">[→note]</button>`
            : '';
        // No local PDF => no file to carry Finder tags. Omitted like the note
        // and Zotero buttons already are when their target is absent, rather
        // than offered and failing.
        const finderBtn = r.has_pdf === false
            ? ''
            : `<button type="button" class="tagging-btn audit-link-alt" data-action="apply-finder" data-key="${key}" aria-label="${_attr(`Apply tags to the PDF Finder tags of ${key}`)}">[→finder]</button>`;
        const zotBtn = zot.present
            ? `<button type="button" class="tagging-btn audit-link-alt" data-action="apply-zotero" data-key="${key}" aria-label="${_attr(`Apply tags to the Zotero item of ${key}`)}"${zoteroOk ? '' : ' disabled title="Zotero not authorized; use Authorize Zotero Desktop"'}>[→zotero]</button>`
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
            <td><input type="checkbox" class="tagging-pick" data-key="${key}"
                 ${_picked.has(r.citation_key) ? 'checked' : ''}
                 aria-label="Select ${key}"></td>
            <td>${_esc(r.citation_key)}</td>
            <td>${_esc(r.year || '')}</td>
            <td class="cell-title" title="${_attr(r.title || '')}" aria-label="${_attr(r.title || '')}">${_esc((r.title || '').slice(0, 70))}</td>
            <td class="tagging-depth" data-sort-value="${_rowDepth(r)}">${_rowDepth(r)}</td>
            <td class="tagging-finder-cell">${r.has_pdf === false
                ? '<span title="no local PDF — Finder tags need a file to carry them">—</span>'
                : _tagsRemovable(r.finder_tags, 'finder')}</td>
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
    // A→Z sort + per-table filter (2026-08-19), the shared ui.js primitive.
    // It reorders the EXISTING <tr> nodes, which is what makes it safe here:
    // every row carries a live tag <input> the user may be mid-token in, and a
    // sort that re-rendered the table would discard it. Sort/filter state is
    // deliberately NOT persisted — the table is one scan's snapshot and the
    // view is re-selected from scratch each time.
    host.querySelectorAll('table.audit-table').forEach((table) => {
        installTableControls(table, {
            noun: 'papers',
            label: 'Filter the papers shown in this table',
            mountBefore: table.closest('.tagging-table-wrap'),
        });
    });
    host.querySelectorAll('.tagging-pick').forEach((cb) => {
        cb.addEventListener('change', () => {
            const key = cb.getAttribute('data-key');
            if (cb.checked) _picked.add(key); else _picked.delete(key);
            // The toolbar lives outside the table host, so refresh from the
            // document root rather than `host`.
            _refreshPickToolbar(document);
        });
    });
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
        message: `Add ${tags.length} tag(s) to ${key}'s ${cfg.label}? Journalled — undo from this panel's Restore.`,
        confirmLabel: 'Add tags',
        tone: 'confirm',
        onConfirm: async () => {
            // In-flight feedback (2026-08-18): the table applies used to show
            // nothing between confirm and settlement — the deck path's
            // "applying…" parity.
            _say(`${key} → ${cfg.label}: applying…`, status);
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
        message: `Remove tag "${tag}" from ${key}'s ${label}? Journalled — undo from this panel's Restore (re-adds it).`,
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

/** Distinct tags a row carries across the surfaces it HAS (2026-08-26).
 *
 * The union, case-folded, mirroring `tagging/batch.py::tag_depth`, which in
 * turn calls `audit/engine/normalize.py::fold_tag` — the batch, the table and
 * the dashboard histogram must agree on what "a paper with 2 tags" means, or
 * the cohort you select here is not the cohort `--max-tags 2` processes. A surface the
 * record does not have contributes nothing (it is not a zero-tag note; there
 * is no note).
 */
function _rowDepth(row) {
    const note = row.note || {};
    const zot = row.zotero || {};
    const seen = new Set();
    const add = (list) => {
        (Array.isArray(list) ? list : []).forEach((t) => {
            // Mirrors audit/engine/normalize.py::fold_tag exactly: NFC, then
            // strip EVERY leading '#' (the Python side uses lstrip, so a
            // '##tag' must not survive as '#tag' here), then case-fold.
            // macOS stores Finder tags in NFD, so without normalize('NFC')
            // the same accented tag counts twice across surfaces.
            seen.add(String(t).normalize('NFC').replace(/^#+/, '').toLowerCase());
        });
    };
    if (row.has_pdf !== false) add(row.finder_tags);
    if (note.present) add(note.tags);
    if (zot.present) add(zot.parent_tags);
    return seen.size;
}

/** Whether a row passes the active cohort filter (`_cohort`). */
function _inCohort(row) {
    if (_cohort === 'all') return true;
    const d = _rowDepth(row);
    if (_cohort === 'untagged') return d === 0;
    if (_cohort === 'under') return d <= 2;
    if (_cohort === 'done') return d >= 3;
    return true;
}

/** How tagged a row is, as a 0..1 fraction of the targets it actually HAS.
 *
 * Deliberately a fraction, not a count. A count ranks by how many targets
 * exist as much as by how tagged they are: a record with no PDF can never
 * score above 2, so a fully-tagged one sorts ahead of a barely-tagged record
 * that happens to have all three — and the deck's untagged-first order puts
 * finished work at the front. That was already true for records with no note
 * or no Zotero item; the 2026-08-11 scope widening (`scope=all` surfaces
 * 2 081 PDF-less records) would have made it the common case.
 *
 * A row with no addressable target at all scores 1 (nothing to do), so it
 * sorts last rather than first.
 */
function _taggedScore(row) {
    const note = row.note || {};
    const zot = row.zotero || {};
    let available = 0;
    let tagged = 0;
    if (row.has_pdf !== false) {
        available += 1;
        if (Array.isArray(row.finder_tags) && row.finder_tags.length) tagged += 1;
    }
    if (note.present) {
        available += 1;
        if (Array.isArray(note.tags) && note.tags.length) tagged += 1;
    }
    if (zot.present) {
        available += 1;
        if (Array.isArray(zot.parent_tags) && zot.parent_tags.length) tagged += 1;
    }
    return available ? tagged / available : 1;
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

function _renderDeck(host, visibleRows) {
    const rows = visibleRows || _ctx.rows;
    if (!rows.length) {
        // Distinguish "the scan found nothing" from "your filter hides
        // everything" — the same empty box for both reads as missing data.
        const emptyByFilter = _cohort !== 'all' && (_ctx.rows || []).length > 0;
        host.innerHTML = '<div class="modal-copy" style="padding:16px;">'
            + (emptyByFilter
                ? 'No papers in this tag cohort. Choose <strong>any</strong> above to see them all.</div>'
                : _scope === 'all'
                    ? 'No biblio records in the last scan.</div>'
                    : 'No PDFs found under <code>biblio_articles/</code> in the last scan. Tick "include entries with no PDF" to see records that have no local file.</div>');
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
            + `<span class="tagging-card-target-tags" data-current-target="${TARGET_ID[label] || 'note'}">${_tagsRemovable(tags, TARGET_ID[label] || 'note')}</span>${extra || ''}</div>`;
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
        + `<h4 class="tagging-card-title">${_esc(row.title || '(untitled)')}</h4>`
        + `<div class="tagging-card-current">`
        + chip('Note', note.present ? note.tags : null, note.present ? '' : ' <em>(no note)</em>')
        + chip('Finder', row.has_pdf === false ? null : row.finder_tags,
               row.has_pdf === false ? ' <em>(no PDF)</em>' : '')
        + chip('Zotero', zot.present ? zot.parent_tags : null, zot.present ? '' : ' <em>(no item)</em>')
        + `</div>`
        + `<div class="tagging-card-snippet" id="tagging-card-snippet" tabindex="0" role="region" aria-label="Paper excerpt">Loading excerpt…</div>`
        + `<div class="tagging-card-proposed" id="tagging-card-proposed"></div>`
        + `<div class="tagging-card-entry">`
        + `<input type="text" class="tagging-tag-input" id="tagging-card-input" autocomplete="off" `
        + `placeholder="dep mood" aria-label="Tags to apply to ${_attr(row.citation_key)}">`
        + `<div class="tagging-entry-hint">Type to search the vocabulary · space/comma commits a tag</div>`
        + `</div>`
        + `<div class="tagging-card-targets">`
        + toggle('note', 'Note', t.note && note.present, !note.present, note.present ? '' : 'No Obsidian note')
        // A row with no local PDF has no file to carry Finder tags, exactly as
        // a row with no note has no note target. Leaving it enabled made the
        // Accept confirm promise "→ note + finder + zotero" for a write the
        // server then answered `finder skipped (no_pdf)` — the table view and
        // the CLI both suppress it already. Disabling is enough to fix the
        // request too: _deckAccept collects `checked && !disabled`.
        + toggle('finder', 'Finder', t.finder && row.has_pdf !== false,
            row.has_pdf === false, row.has_pdf === false ? 'No local PDF' : '')
        + toggle('zotero', 'Zotero', t.zotero && zot.present, !zot.present || !zoteroOk,
            !zot.present ? 'No Zotero item' : (!zoteroOk ? 'Zotero not authorized; use Authorize Zotero Desktop' : ''))
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
    const cached = _snippetGet(key);
    if (cached != null) return Promise.resolve(cached);
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
            _snippetPut(key, rec); // cache SUCCESSES only
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
    if (next == null || _snippetGet(next) != null
        || _snippetInflight[next] || _prefetchInFlight) return;
    // Same reason the visible-card path skips: a PDF-less row has nothing to
    // extract, so the request only spends a round-trip on an empty answer.
    const nextRow = _deck.byKey && _deck.byKey[next];
    if (nextRow && nextRow.has_pdf === false) return;
    _prefetchInFlight = true;
    _fetchSnippet(next).finally(() => { _prefetchInFlight = false; });
}

async function _deckLoadSnippet(key) {
    const el = document.getElementById('tagging-card-snippet');
    if (!el) return;
    // No local PDF => nothing to extract. Say so instead of asking the server
    // for an excerpt it will spend a request deciding it cannot produce.
    const row = _deck.byKey && _deck.byKey[key];
    if (row && row.has_pdf === false) {
        el.textContent = '(no local PDF — classify from the title and metadata)';
        // Still warm the next card: returning early here broke the prefetch
        // chain, so every card after a PDF-less one paid a cold fetch.
        _deckPrefetchNext();
        return;
    }
    const hit = _snippetGet(key);
    if (hit != null) {
        _deckFillSnippet(el, hit);
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
        // "Propose will return no tags" stopped being true with metadata mode:
        // a scanned PDF still gets classified from its abstract + bib fields
        // when those are substantive enough.
        el.innerHTML = '<em>No extractable text (scanned / unreadable PDF) — '
            + 'Propose will fall back to the abstract and metadata.</em>';
        return;
    }
    el.textContent = rec.snippet.slice(0, 900) + (rec.chars > 900 ? ' …' : '');
}

async function _deckPropose() {
    // Message the busy window instead of a silent no-op (2026-08-18): on a
    // keyboard-first surface a swallowed P reads as a dead key — the
    // optimistic apply window can last up to 60 s (_deckRemoveTag precedent).
    if (_deck.busy) { _sayDeck('Wait for the current apply to finish.', 'warn'); return; }
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
    // Messaged, not silent — see _deckPropose (2026-08-18).
    if (_deck.busy) { _sayDeck('Wait for the current apply to finish.', 'warn'); return; }
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
        message: `Add ${tags.length} tag(s) to ${key} → ${chosen.join(' + ')}? Journalled — undo with ⌫, or from this panel's Restore.`,
        confirmLabel: 'Accept',
        tone: 'confirm',
        onConfirm: () => _deckApplyAll(key, tags, targets),
    });
}

// ── Session counter (Card Catalog gamification, deliberately minimal) ──
// "N triaged today" — a day-scoped local count of landed accepts, a fact about
// today and deliberately not a goal. This deck grew the counter first; the
// ambiguous and accession decks are the same work and now share the ONE
// primitive rather than a third copy of the date-keyed record.
const _session = dailyCounter('tagging_triage_daily');
const _sessionCount = () => _session.count();
const _sessionBump = () => _session.bump();

/** Announce into the deck-level result line (survives card changes) + SR.
 *
 * `tone` is the app's outcome grammar (ui.js::statusLine): 'ok' for a landed
 * write, 'info' for "applying…" and the navigational notes, 'warn' for a
 * refusal that is not a failure. The deck's ERROR channel is the separate
 * `_sayDeckFailure`, which also offers the retry affordance. */
function _sayDeck(msg, tone = 'info') {
    announceStatus(msg);
    // Re-queried at write time: a node captured before an optimistic advance
    // would be detached by the card re-render and the feedback silently lost.
    statusLine(document.getElementById('tagging-deck-lastresult'), { tone, text: msg });
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
    statusLine(el, { tone: 'error', text: `${msg} ` });
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
        const incomplete = Object.entries(targets).some(([tg, requested]) => requested
            && (!results[tg] || !results[tg].ok || results[tg].partial));
        _sayDeck(`${key}: ${parts.join(' · ') || 'no destination receipts'}.`
            + (incomplete ? ' Not all destinations are complete. Inspect the paper and Restore records before retrying; an unknown outcome may already have been applied.' : ''),
        incomplete ? 'warn' : 'ok');
        // Reflect the write into the in-memory row so a revisit shows it.
        _deckPatchRow(key, targets, results);
        _deckProgress();
        const undoBtn = document.querySelector('[data-deck="undo"]');
        if (undoBtn && _deck.undo.length) undoBtn.removeAttribute('disabled');
    } catch (err) {
        logError('apply-all failed: ' + String(err));
        // CLI-parity honesty (2026-08-18): the 60 s client abort fires while
        // the server may still be mid-fan-out — its driver twin carries a
        // 900 s timeout and a documented "write MAY have landed" caveat, so
        // asserting flat failure here misdirected the retry (and the landed
        // ops' undo ids never reached the stack).
        const timedOut = err && err.name === 'AbortError';
        _sayDeckFailure(key, tags, timedOut
            ? `${key}: apply timed out — the write MAY have landed. Check the `
              + 'paper (or the Restore panel) before retrying.'
            : `${key}: apply FAILED (network).`);
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
    if (_deck.busy) { _sayDeck('Wait for the current apply to finish.', 'warn'); return; }
    const tag = btn.getAttribute('data-tag');
    const target = btn.getAttribute('data-target');
    if (!tag || !_TARGETS[target]) return;
    const label = _TARGETS[target].label;
    confirmInline(btn, {
        message: `Remove tag "${tag}" from ${key}'s ${label}? Journalled — undo from this panel's Restore (re-adds it).`,
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
                    _sayDeck(`${key}: removal failed — ${sub.error || d.error || r.status}.`, 'error');
                    return;
                }
                _deckUnpatchRow(key, target, sub.removed || [tag]);
                _deckRenderCard();
                _sayDeck(`${key} → ${label}: removed ${(sub.removed || []).join(', ') || tag}.`
                    + (sub.op_id ? ' Undo via Restore.' : ''), 'ok');
            } catch (err) {
                logError('deck remove tag failed: ' + String(err));
                _sayDeck(`${key}: removal failed (network).`, 'error');
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
    const row = _deck.byKey && _deck.byKey[key];
    if (row && row.has_pdf === false) {
        _sayDeck('No local PDF for this entry.', 'warn');
        return;
    }
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
    // Messaged, not silent — see _deckPropose (2026-08-18). The busy gate
    // itself is load-bearing: the undo stack fills post-response, so a ⌫
    // during an in-flight apply-all would revert the WRONG accept.
    if (_deck.busy) { _sayDeck('Wait for the current apply to finish.', 'warn'); return; }
    if (!_deck.undo.length) return;
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

// Paging cursor for the Restore list. Module-scoped like `_view`/`_scope`;
// reset every time the panel is opened so a stale cursor from a previous
// session can never make page 2 look like page 1.
let _restoreCursor = null;
let _restoreOps = [];

/** Human-readable byte size — the journal reaches tens of MB. */
function _bytes(n) {
    if (!Number.isFinite(n) || n <= 0) return '0 B';
    const u = ['B', 'KB', 'MB', 'GB'];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i += 1; }
    return `${v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)} ${u[i]}`;
}

/** The journal size/composition header — see /api/tagging/journal-stats. */
function _journalStatsHtml(st) {
    if (!st || !st.ops) return '';
    const days = (a, b) => {
        if (!a || !b) return null;
        const d = (new Date(b) - new Date(a)) / 86400000;
        return Number.isFinite(d) ? Math.max(0, Math.round(d)) : null;
    };
    const span = days(st.oldest_ts, st.newest_ts);
    // `protected` is the population that is never evicted (applied Finder and
    // Zotero ops carry their prior tags inline, with no snapshot file behind
    // them). Naming it is the entire point of this header: it is the growth.
    return `<div class="tagging-journal-stats">
        <span><b>${st.ops.toLocaleString()}</b> operations</span>
        <span><b>${_esc(_bytes(st.bytes))}</b> on disk</span>
        <span><b>${(st.protected || 0).toLocaleString()}</b> permanent
            <span class="tagging-entry-hint">(Finder/Zotero — undo is stored inline, so these are never trimmed automatically)</span></span>
        ${span === null ? '' : `<span>spanning <b>${span}</b> day${span === 1 ? '' : 's'}</span>`}
        <button type="button" class="tagging-btn audit-link-alt" data-action="compact-journal">Compact…</button>
    </div>`;
}

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
    _restoreCursor = null;
    _restoreOps = [];
    try {
        // Stats and the first page in parallel: the stats query parses no
        // payloads, so it is cheap even when the history is five figures.
        const [rs, rm] = await Promise.all([
            secureFetch('/api/tagging/journal-stats').catch(() => null),
            secureFetch('/api/tagging/manifest'),
        ]);
        const d = await rm.json().catch(() => ({}));
        if (!rm.ok) { panel.innerHTML = `<div class="audit-summary-warning">${_esc(d.error || 'Manifest unavailable')}</div>`; return; }
        const st = rs && rs.ok ? await rs.json().catch(() => null) : null;
        _restoreOps = d.ops || [];
        _restoreCursor = d.next_before || null;
        // The status line is VISIBLE (not just the SR announcer): revert
        // failures previously went only to the sr-only region, the exact
        // silent-feedback class the deck/table status lines were added to fix.
        panel.innerHTML = _journalStatsHtml(st)
            + `<div id="tagging-restore-list">${_restoreList(_restoreOps, d.total)}</div>`
            + '<div class="tagging-status" id="tagging-restore-status"></div>';
        _wireRestore(panel);
    } catch (e) {
        logError('tagging manifest failed: ' + String(e));
        panel.innerHTML = `<div class="audit-summary-warning">${_esc(String(e))}</div>`;
    }
}

/** Append the next page in place — never a re-render, so an open confirm
 *  strip or a focused [revert] button on an already-listed row survives.
 *
 *  This is why the footer is its own element: rewriting the whole list to
 *  update "showing N of TOTAL" would destroy every row node, and with them a
 *  half-finished confirm and the caret/focus position — silently, since the
 *  rebuilt rows look identical. New rows are appended to the existing
 *  `<tbody>` and only the footer is swapped. */
async function _restoreMore(panel, btn) {
    if (!_restoreCursor) return;
    btn.disabled = true;
    btn.textContent = 'Loading…';
    try {
        const r = await secureFetch(
            `/api/tagging/manifest?before=${encodeURIComponent(_restoreCursor)}`);
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { _say(d.error || 'Could not load more.', document.getElementById('tagging-restore-status')); return; }
        const fresh = d.ops || [];
        _restoreOps = _restoreOps.concat(fresh);
        _restoreCursor = d.next_before || null;
        const body = panel.querySelector('#tagging-restore-list tbody');
        const footer = panel.querySelector('#tagging-restore-footer');
        if (body && fresh.length) body.insertAdjacentHTML('beforeend', _restoreRowsHtml(fresh));
        // The footer carries [Load more], so it is replaced last and re-wired
        // below; `_wireRestore` binds only nodes that are not already wired.
        if (footer) footer.innerHTML = _restoreFooter(_restoreOps.length, d.total);
        _wireRestore(panel);
    } catch (e) {
        logError('tagging manifest page failed: ' + String(e));
    } finally {
        btn.disabled = false;
    }
}

function _wireRestore(panel) {
    // Wire-once markers, not a module flag: paging calls this again after
    // appending rows, and re-binding an already-wired [revert] would fire the
    // confirm twice per click (the audit search-bar defect class).
    panel.querySelectorAll('[data-action="revert-op"]').forEach((btn) => {
        if (btn.dataset.wired) return;
        btn.dataset.wired = '1';
        btn.addEventListener('click', (e) => { e.preventDefault(); _revert(btn); });
    });
    const more = panel.querySelector('[data-action="restore-more"]');
    if (more && !more.dataset.wired) {
        more.dataset.wired = '1';
        more.addEventListener('click', (e) => { e.preventDefault(); _restoreMore(panel, more); });
    }
    const compact = panel.querySelector('[data-action="compact-journal"]');
    if (compact && !compact.dataset.wired) {
        compact.dataset.wired = '1';
        compact.addEventListener('click', (e) => { e.preventDefault(); _compactJournal(compact); });
    }
}

/** Compact the journal: always previews the count first, then confirms.
 *
 * Two steps by design — this is the only action in the subsystem that
 * deliberately destroys restore mappings, so the number is shown BEFORE the
 * confirm rather than described in the abstract. */
async function _compactJournal(btn) {
    const status = document.getElementById('tagging-restore-status');
    const policy = 'keep_newest';
    const value = 5000;
    _say('Checking what would be dropped…', status);
    let preview;
    try {
        const r = await secureFetch('/api/tagging/compact-journal', {
            method: 'POST',
            body: JSON.stringify({ policy, value, dry_run: true }),
        });
        preview = await r.json().catch(() => ({}));
        if (!r.ok) { _say(`Compact unavailable: ${preview.error || r.status}`, status); return; }
    } catch (e) {
        logError('tagging compact preview failed: ' + String(e));
        _say(`Compact unavailable: ${String(e)}`, status);
        return;
    }
    if (!preview.would_drop) {
        _say(`Nothing to compact — keeping the newest ${value.toLocaleString()} `
             + `Finder/Zotero operations already covers all ${preview.candidates.toLocaleString()}.`, status);
        return;
    }
    confirmInline(btn, {
        message: `Permanently drop ${preview.would_drop.toLocaleString()} of `
            + `${preview.candidates.toLocaleString()} Finder/Zotero operations, keeping the newest `
            + `${value.toLocaleString()}? Their undo is stored inline, so this cannot be reversed. `
            + `Note history is untouched.`,
        confirmLabel: 'Compact',
        onConfirm: async () => {
            try {
                const r = await secureFetch('/api/tagging/compact-journal', {
                    method: 'POST',
                    body: JSON.stringify({ policy, value, dry_run: false, confirm: true }),
                });
                const d = await r.json().catch(() => ({}));
                if (!r.ok) { _say(`Compact failed: ${d.error || r.status}`, status); return; }
                _say(`Compacted: dropped ${(d.dropped || 0).toLocaleString()} operation(s); `
                     + `${(d.after || 0).toLocaleString()} remain. Reopen Restore to refresh.`, status);
            } catch (e) {
                logError('tagging compact failed: ' + String(e));
                _say(`Compact failed: ${String(e)}`, status);
            }
        },
    });
}

/** One `<tr>` of markup for a restore-journal op. */
function _restoreRowHtml(op) {
    const added = Array.isArray(op.added) ? op.added.join(', ') : '';
    // outcome_unknown (a note write whose response was lost) is revertible
    // too: the server reads Zotero first and reverts only its own text.
    const unknown = op.state === 'outcome_unknown';
    const action = op.state === 'applied' || unknown
        ? `<button type="button" class="tagging-btn audit-link-alt" data-action="revert-op" data-op-id="${_attr(op.op_id)}"
             aria-label="${_attr(`Revert the ${op.kind || 'tag'} write on ${op.citation_key || op.op_id}`)}">[revert]</button>`
        : '';
    return `<tr data-op-id="${_attr(op.op_id)}">
        <td>${_esc((op.ts || '').replace('T', ' ').slice(0, 19))}</td>
        <td>${_esc(op.citation_key || '')}</td>
        <td>${_esc(op.kind || '')}</td>
        <td>${_esc(added)}</td>
        <td>${_esc(unknown ? 'outcome unknown (checked live before revert)' : (op.state || ''))}</td>
        <td>${action}</td>
    </tr>`;
}

function _restoreList(ops, total) {
    if (ops.length === 0) {
        return '<div class="modal-copy" style="padding:12px;">No tag writes to restore yet.</div>';
    }
    const head = '<thead><tr><th scope="col">When</th><th scope="col">Key</th>'
        + '<th scope="col">Kind</th><th scope="col">Added</th><th scope="col">State</th><th scope="col"></th></tr></thead>';
    return `<div class="tagging-table-wrap"><table class="audit-table">${head}`
        + `<tbody>${_restoreRowsHtml(ops)}</tbody></table></div>`
        + `<div id="tagging-restore-footer">${_restoreFooter(ops.length, total)}</div>`;
}

/** The count-vs-total line plus [Load more], as its own replaceable element.
 *
 * Split out of `_restoreList` so paging can append rows and swap ONLY this,
 * instead of rewriting the whole list (see `_restoreMore`).
 *
 * Never presents a page as the whole history — the count-vs-total honesty
 * rule the Tag Hygiene under-reporting incident established. The [Load more]
 * control is gated on the CURSOR, not on `total > shown`: those two can
 * disagree (a concurrent write moves `total`, and the server is the only
 * thing that knows whether another page exists), and a button whose click
 * cannot issue a request is worse than no button. */
function _restoreFooter(shownCount, total) {
    const more = _restoreCursor
        ? ` <button type="button" class="tagging-btn audit-link-alt" data-action="restore-more">Load more</button>`
        : '';
    if (!(Number.isFinite(total) && total > shownCount)) return more;
    return `<div class="tagging-entry-hint" style="padding:6px 2px;">Showing the
        ${shownCount.toLocaleString()} most recent of ${total.toLocaleString()} operations.${more}</div>`;
}

/** One `<tr>` of markup per op — the rows half of `_restoreList`. */
function _restoreRowsHtml(ops) {
    return ops.map(_restoreRowHtml).join('');
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
                        const recorded = _restoreOps.find(op => op.op_id === opId)?.state || 'unresolved';
                        stateCell.textContent = spent ? 'reverted' : `${recorded} (${d.status || 'unknown'})`;
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
