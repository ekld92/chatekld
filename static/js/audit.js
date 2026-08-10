/**
 * Library Audit tab controller.
 *
 * Talks to the /api/audit/* blueprint.  Nothing here fires on page load
 * except a single config fetch — the actual scan only runs when the
 * user clicks "Run Scan".  Polling starts only while a scan is in
 * flight and stops as soon as the manager reports `done | error |
 * cancelled`.
 */

// Imports only ui.js + api.js (per the JS module hierarchy). All report tables
// interpolate server data into innerHTML, so every cell is run through the _esc /
// _attr HTML-escape helpers — there is no createElement path here.
import { secureFetch, consumeSSE, logError } from './api.js';
import { setStatusA11y, closeModal, taskBegin, taskEnd, makeLatestGate, confirmInline, announceStatus, announceError, showTaskError, clearTaskError } from './ui.js';

const _STATUS_POLL_MS = 1500;
let _statusPollTimer = null;
let _currentReport = 'inventory';
let _lastSummary = null;

const _REPORT_LABELS = {
    inventory: 'Inventory',
    note_tag_drift: 'Tag Drift',
    // read_unzoterod is the FULL set of un-Zotero'd PDFs; unread_unzoterod is a
    // filtered subset of it (annotations < threshold) — the old pair ("Read PDFs
    // Missing Zotero" / "Unread PDFs") read as a partition and hid the full list.
    // These labels are ALSO the group headings in the cross-report search panel,
    // where there is no adjacent tab to imply the containment, so the subset
    // spells its parent out rather than reusing the tablist's "↳" affordance
    // (which lives in index.html as an aria-hidden span — a bare arrow glyph is
    // noise to a screen reader and meaningless in a search result heading).
    read_unzoterod: 'PDFs Without Zotero',
    unread_unzoterod: 'PDFs Without Zotero · Unread Only',
    zotero_unread: 'Zotero Queue',
    zotero_no_pdf: 'Bib Entries Without PDFs',
    finder_tag_drift: 'Finder Tag Drift',
    tag_hygiene: 'Tag Hygiene',
    ambiguous_pdfs: 'Ambiguous PDFs',
    skipped_pdfs: 'Skipped PDFs',
    duplicates: 'Duplicate PDFs',
    near_duplicates: 'Possible Duplicates',
};

/**
 * Initialise the Audit tab: load + display the audit config and the reused vault
 * path, then read status. If a prior in-memory scan left results, render them
 * WITHOUT polling — the scan itself only ever runs on an explicit Run Scan.
 */
export async function initAuditTab() {
    _wireSearch();
    // E5 (2026-07-22 audit): a failed init fetch previously only logError'd —
    // the tab then rendered its default "set the vault path" copy with Run
    // Scan enabled, misleading a user whose vault IS configured. Surface a
    // visible error with a Retry instead.
    const boundary = document.getElementById('audit-init-error');
    if (boundary) clearTaskError(boundary);
    let initFailed = false;
    try {
        const cfg = await fetchAuditConfig();
        _populateAuditSettingsForm(cfg);
        _setVaultDisplay(cfg.obsidian_vault_path);
    } catch (e) {
        logError('audit init', e);
        initFailed = true;
    }
    try {
        const status = await _getStatus();
        _renderStatusBanner(status);
        if (status.has_results) {
            // A previous run left data in memory — show it without polling.
            await _loadReport(_currentReport);
        }
    } catch (e) {
        logError('audit status', e);
        initFailed = true;
    }
    if (initFailed && boundary) {
        showTaskError(boundary,
            'Could not load the audit configuration or status — the settings '
            + 'shown may be incomplete.', [
                { label: 'Retry', primary: true, onClick: () => initAuditTab() },
                { label: 'Dismiss', onClick: () => clearTaskError(boundary) },
            ]);
    }
}

async function fetchAuditConfig() {
    const r = await secureFetch('/api/audit/config');
    if (!r.ok) throw new Error(`config fetch failed: ${r.status}`);
    return r.json();
}

function _setVaultDisplay(path) {
    const el = document.getElementById('audit-vault-path-display');
    if (!el) return;
    if (path) {
        el.textContent = path;
        el.classList.remove('vault-path-missing');
    } else {
        el.textContent = 'Set the Obsidian vault path on the Obsidian Agent tab first.';
    }
}

function _populateAuditSettingsForm(cfg) {
    const fields = [
        ['audit-attachments-subdir', 'audit_attachments_subdir'],
        ['audit-biblio-articles-subdir', 'audit_biblio_articles_subdir'],
        ['audit-zotero-notes-subdir', 'audit_zotero_notes_subdir'],
        ['audit-master-bib-path', 'audit_master_bib_path'],
        ['audit-zotero-sqlite', 'audit_zotero_sqlite'],
        ['audit-zotero-storage', 'audit_zotero_storage'],
        ['audit-biblio-skip-prefix', 'audit_biblio_skip_prefix'],
        ['audit-annotations-read-threshold', 'audit_annotations_read_threshold'],
        ['audit-near-dup-ratio', 'audit_near_dup_ratio'],
    ];
    for (const [domId, cfgKey] of fields) {
        const el = document.getElementById(domId);
        if (!el) continue;
        const v = cfg[cfgKey];
        el.value = v == null ? '' : String(v);
    }
    const parentTags = document.getElementById('audit-drift-include-parent-tags');
    if (parentTags) parentTags.checked = !!cfg.audit_drift_include_parent_tags;
}

/** Persist the audit path/threshold settings to /api/audit/config (NOT the
 * generic /api/config, which strips audit_* keys), then close the modal. */
export async function saveAuditSettings() {
    const body = {
        audit_attachments_subdir: _readInput('audit-attachments-subdir'),
        audit_biblio_articles_subdir: _readInput('audit-biblio-articles-subdir'),
        audit_zotero_notes_subdir: _readInput('audit-zotero-notes-subdir'),
        audit_master_bib_path: _readInput('audit-master-bib-path'),
        audit_zotero_sqlite: _readInput('audit-zotero-sqlite'),
        audit_zotero_storage: _readInput('audit-zotero-storage'),
        audit_biblio_skip_prefix: _readInput('audit-biblio-skip-prefix'),
        audit_annotations_read_threshold: _readInt('audit-annotations-read-threshold'),
        audit_near_dup_ratio: _readInt('audit-near-dup-ratio') || 88,
        audit_drift_include_parent_tags: !!(document.getElementById('audit-drift-include-parent-tags') || {}).checked,
    };
    const status = document.getElementById('audit-settings-status');
    try {
        const r = await secureFetch('/api/audit/config', {
            method: 'POST',
            body: JSON.stringify(body),
        });
        const d = await r.json();
        if (!r.ok) {
            setStatusA11y(status, d.error || 'Save failed', true);
            return;
        }
        setStatusA11y(status, 'Saved.', false);
        setTimeout(() => closeModal('audit-settings-modal'), 400);
    } catch (e) {
        setStatusA11y(status, String(e), true);
    }
}

function _readInput(id) {
    const el = document.getElementById(id);
    return el ? el.value.trim() : '';
}

function _readInt(id) {
    const el = document.getElementById(id);
    if (!el) return 0;
    const n = parseInt(el.value, 10);
    return Number.isFinite(n) ? n : 0;
}

/**
 * Start a scan (the ONLY code path that triggers one) and begin status polling.
 * Re-enables the Run button immediately on a start failure; otherwise polling
 * owns the button state until the scan reaches a terminal state.
 */
export async function runAuditScan() {
    const btn = document.getElementById('audit-run-btn');
    const cancelBtn = document.getElementById('audit-cancel-btn');
    if (btn) btn.disabled = true;
    try {
        const r = await secureFetch('/api/audit/scan', {
            method: 'POST',
            body: JSON.stringify({ count_annotations: true, include_duplicates: true, include_near_duplicates: true }),
        });
        const d = await r.json();
        if (!r.ok || !d.started) {
            _showError(d.error || 'Scan could not start');
            if (btn) btn.disabled = false;
            return;
        }
        _hideEmptyState();
        if (cancelBtn) cancelBtn.style.display = '';
        taskBegin('audit-scan', 'Audit scanning...');
        _startStatusPolling();
    } catch (e) {
        _showError(String(e));
        if (btn) btn.disabled = false;
    }
}

/** "Refresh widget" (Lot 2, 2026-08-06): rewrite BASE_DIR/widget.json from
 * the cached scan. Regenerable app-data write, not a vault write — no
 * confirmInline. 409 = no completed scan cached yet. */
export async function refreshWidgetSnapshot() {
    const btn = document.getElementById('audit-widget-refresh-btn');
    if (btn) btn.disabled = true;
    try {
        const r = await secureFetch('/api/widget/refresh', {
            method: 'POST',
            body: JSON.stringify({}),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) {
            _showError(d.error || `Widget refresh failed (${r.status})`);
            return;
        }
        const title = (d.paper && d.paper.title) ? ` — paper of the day: ${d.paper.title}` : '';
        const banner = document.getElementById('audit-status-banner');
        if (banner) {
            banner.textContent = `Widget snapshot written${title}.`;
            banner.style.display = 'block';
        }
        announceStatus('Widget snapshot written.');
    } catch (e) {
        _showError(String(e));
    } finally {
        if (btn) btn.disabled = false;
    }
}

/** Request cancellation of the running scan; polling notices the state change
 * and tears down the in-flight UI. */
export async function cancelAuditScan() {
    // C5/#49 (2026-07-13): immediate feedback + surface a failed cancel (the
    // banner otherwise only updates when the 1.5 s poll notices the state).
    const cancelBtn = document.getElementById('audit-cancel-btn');
    const banner = document.getElementById('audit-status-banner');
    if (cancelBtn) cancelBtn.disabled = true;
    if (banner) { banner.textContent = 'Cancelling…'; banner.style.display = 'block'; }
    try {
        const resp = await secureFetch('/api/audit/cancel', { method: 'POST' });
        if (!resp.ok && banner) {
            banner.textContent = 'Cancel failed — the scan continues.';
            if (cancelBtn) cancelBtn.disabled = false;
        }
    } catch (e) {
        logError('audit cancel', e);
        if (banner) banner.textContent = 'Cancel failed — the scan continues (see console).';
        if (cancelBtn) cancelBtn.disabled = false;
    }
}

function _startStatusPolling() {
    _stopStatusPolling();
    _statusPollTimer = setInterval(_pollStatus, _STATUS_POLL_MS);
    _pollStatus();
}

function _stopStatusPolling() {
    if (_statusPollTimer) {
        clearInterval(_statusPollTimer);
        _statusPollTimer = null;
    }
}

// Item 3.3: the 1.5 s poll had no staleness discipline — a slow status
// response could render its banner (and even fire _loadReport) AFTER a newer
// poll already painted fresher state. Latest-wins per poll tick.
const _statusPollGate = makeLatestGate();

async function _pollStatus() {
    const isCurrent = _statusPollGate.enter();
    let status;
    try {
        status = await _getStatus();
    } catch (e) {
        if (!isCurrent()) return;
        _showError(String(e));
        return;
    }
    if (!isCurrent()) return;
    // Track the scan identity so the ambiguous deck can distinguish a fresh
    // scan (reset decisions) from a view toggle (keep them) — finding 3.
    if (status && status.finished_at) _lastScanFinishedAt = status.finished_at;
    _renderStatusBanner(status);
    if (status.state !== 'scanning') {
        _stopStatusPolling();
        const btn = document.getElementById('audit-run-btn');
        const cancelBtn = document.getElementById('audit-cancel-btn');
        if (btn) btn.disabled = false;
        if (cancelBtn) cancelBtn.style.display = 'none';
        taskEnd('audit-scan');
        if (status.state === 'done' || (status.state === 'cancelled' && status.has_results)) {
            if (status.state === 'done') _maybeFirstScanMilestone(status);
            await _loadReport(_currentReport);
        }
    }
}

// ── One-shot milestone (Card Catalog gamification, deliberately minimal) ──
// The FIRST completed scan gets a single acknowledgment strip; the flag makes
// it never reappear. No badges, no repetition — a fact, said once.
function _maybeFirstScanMilestone(status) {
    const KEY = 'audit_first_scan_done';
    try {
        if (localStorage.getItem(KEY)) return;
        localStorage.setItem(KEY, '1');
    } catch { return; }
    const banner = document.getElementById('audit-status-banner');
    if (!banner) return;
    const strip = document.createElement('div');
    strip.className = 'milestone-strip';
    strip.setAttribute('role', 'status');
    const body = document.createElement('span');
    const n = status.record_count || (status.counts && status.counts.records);
    const strong = document.createElement('strong');
    strong.textContent = 'First full scan complete';
    body.appendChild(strong);
    body.appendChild(document.createTextNode(
        (n ? ` — ${n} records reconciled.` : '.') + ' Shown once.'));
    strip.appendChild(body);
    banner.insertAdjacentElement('afterend', strip);
    announceStatus('First full library scan complete.');
}

async function _getStatus() {
    const r = await secureFetch('/api/audit/status');
    if (!r.ok) throw new Error(`status ${r.status}`);
    return r.json();
}

function _renderStatusBanner(status) {
    const banner = document.getElementById('audit-status-banner');
    if (!banner) return;
    const fragments = [];
    if (status.state === 'scanning') fragments.push('Scanning…');
    else if (status.state === 'done') fragments.push('Scan complete.');
    else if (status.state === 'error') fragments.push(`Error: ${status.error || 'unknown'}`);
    else if (status.state === 'cancelled') fragments.push('Cancelled.');
    if (Array.isArray(status.messages) && status.messages.length) {
        fragments.push(status.messages[status.messages.length - 1]);
    }
    if (fragments.length) {
        banner.textContent = fragments.join(' · ');
        banner.style.display = 'block';
    } else {
        banner.style.display = 'none';
    }
}

function _hideEmptyState() {
    const el = document.getElementById('audit-empty-state');
    if (el) el.style.display = 'none';
}

function _showError(msg) {
    const banner = document.getElementById('audit-status-banner');
    if (banner) {
        banner.textContent = msg;
        banner.style.display = 'block';
    }
}

// --- cross-report search ---------------------------------------------------
//
// The reports are views of ONE cached scan, so finding a paper otherwise
// means guessing which tab surfaced it. The search panel overlays the report
// body (it does not replace the active tab) so clearing the box restores
// exactly what the user was looking at.

/**
 * Show/hide the post-scan report chrome: the grouped tablist AND the search
 * bar, which are meaningless before a scan and must appear together.
 */
function _setTabsVisible(tabs, on) {
    if (tabs) tabs.style.display = on ? '' : 'none';
    const bar = document.getElementById('audit-search-bar');
    if (bar) bar.style.display = on ? '' : 'none';
    // Finding 6: only CLOSE the overlay here (keeping the typed query), never
    // erase it. This runs from _hideReport on a per-report 404 (e.g. the
    // Duplicates tab after a skip-duplicates scan); wiping the box there was the
    // exact a52c87b-class regression — the user's query vanished on a routine
    // "that report isn't in this scan". The bar is hidden anyway, so the
    // preserved text is invisible until the tablist returns. Explicit discard
    // stays with the Clear button / Escape, which call _clearSearch directly.
    if (!on) _hideSearchResults();
}

const _SEARCH_DEBOUNCE_MS = 220;
// Below the server's MIN_QUERY_LEN — checked client-side too so a single
// character never costs a round trip that can only 400.
const _SEARCH_MIN_LEN = 2;
let _searchDebounce = null;
// Same discipline as _reportLoadGate: a slow query must not paint over a
// newer one (or over a report the user switched to meanwhile).
const _searchGate = makeLatestGate();

/** Wire the search input. Called once from initAuditTab.
 *
 * Guarded against re-entry: this file already carries a documented
 * double-binding hazard (_wireRowActions), and a listener bound twice here
 * would fire two searches per keystroke.
 */
function _wireSearch() {
    const input = document.getElementById('audit-search-input');
    const clear = document.getElementById('audit-search-clear');
    if (!input) return;
    // Marker on the ELEMENT, not a module flag: a module-scoped boolean stays
    // true after the node is replaced, silently leaving the new input unwired.
    if (input.dataset.searchWired) return;
    input.dataset.searchWired = '1';
    input.addEventListener('input', () => {
        if (_searchDebounce) clearTimeout(_searchDebounce);
        _searchDebounce = setTimeout(() => _runSearch(input.value), _SEARCH_DEBOUNCE_MS);
    });
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') { _clearSearch(); return; }
        // Enter re-opens the results for a query that is still typed but whose
        // panel was closed by a jump to a report — no retype, no debounce wait.
        if (e.key === 'Enter') { e.preventDefault(); _runSearch(input.value); }
    });
    if (clear) clear.addEventListener('click', () => _clearSearch());
    const restore = document.getElementById('audit-search-restore');
    if (restore) restore.addEventListener('click', () => _runSearch(input.value));
    // L4: Close hides the overlay but keeps the query (unlike Clear).
    const close = document.getElementById('audit-search-close');
    if (close) close.addEventListener('click', () => _hideSearchResults());
}

/** Reset to the active report (the search panel is an overlay, not a mode). */
/**
 * Close the results overlay but KEEP what the user typed.
 *
 * The query text is theirs, not the panel's: wiping it on every close made the
 * box erase itself mid-word (any pause past the debounce below the minimum
 * length re-entered the close path) and made every jump-to-report cost a
 * retype. Only _clearSearch — the explicit Clear button / Escape — empties it.
 */
function _hideSearchResults() {
    if (_searchDebounce) { clearTimeout(_searchDebounce); _searchDebounce = null; }
    // Supersede any in-flight query so its response can't repaint the panel
    // after we've closed it.
    _searchGate.enter();
    _showSearchPanel(false);
}

/** Explicitly discard the query and its results (Clear button / Escape). */
function _clearSearch() {
    const input = document.getElementById('audit-search-input');
    if (input) input.value = '';
    _hideSearchResults();
}

/** Toggle between the search results panel and the active report's body. */
function _showSearchPanel(on) {
    const results = document.getElementById('audit-search-results');
    const body = document.getElementById('audit-report-body');
    const meta = document.getElementById('audit-report-meta');
    const input = document.getElementById('audit-search-input');
    const clear = document.getElementById('audit-search-clear');
    const restore = document.getElementById('audit-search-restore');
    // Two thresholds (finding 9): ANY typed text is discardable, so Clear
    // shows for it (even a single character, which the min-length path fought
    // to preserve); only a searchable-length query can be re-run, so "Show
    // results" keys on the min length.
    const hasAnyText = !!(input && input.value.trim().length > 0);
    const hasText = !!(input && input.value.trim().length >= _SEARCH_MIN_LEN);
    if (results) results.style.display = on ? '' : 'none';
    if (body) body.style.display = on ? 'none' : '';
    // The meta line describes the active report, which is hidden while
    // searching; remember its prior state so restoring doesn't reveal a meta
    // line for a report that never had one.
    if (meta) {
        if (on) {
            if (meta.dataset.preSearchDisplay === undefined) {
                meta.dataset.preSearchDisplay = meta.style.display;
            }
            meta.style.display = 'none';
        } else if (meta.dataset.preSearchDisplay !== undefined) {
            meta.style.display = meta.dataset.preSearchDisplay;
            delete meta.dataset.preSearchDisplay;
        }
    }
    // Clear stays available whenever there is a query to discard — including
    // while the panel is closed, which is now a state the query survives.
    if (clear) clear.style.display = hasAnyText ? '' : 'none';
    // …and when a query is closed but still typed, offer the way back rather
    // than making the user retype it.
    if (restore) restore.style.display = (!on && hasText) ? '' : 'none';
    // Close (keep-query) is offered only while the overlay is actually open.
    const close = document.getElementById('audit-search-close');
    if (close) close.style.display = on ? '' : 'none';
    if (!on && results) results.innerHTML = '';
}

async function _runSearch(raw) {
    const q = String(raw || '').trim();
    // Too short to search — close the overlay but leave the partial word alone;
    // the user is very likely still typing it.
    if (q.length < _SEARCH_MIN_LEN) { _hideSearchResults(); return; }
    const isCurrent = _searchGate.enter();
    const results = document.getElementById('audit-search-results');
    if (!results) return;
    _showSearchPanel(true);
    results.textContent = 'Searching…';
    try {
        const r = await secureFetch(`/api/audit/search?q=${encodeURIComponent(q)}`);
        const d = await r.json();
        if (!isCurrent()) return;
        // 202: the search corpus is being built off the request thread (a
        // count_annotations=off scan can make that take a while). Show a
        // preparing state and retry — never block a Waitress worker on the
        // build. Bounded by the latest-wins gate: a newer query supersedes.
        if (r.status === 202 || d.building) {
            results.innerHTML = `<p class="audit-search-empty">Preparing search index…</p>`;
            setTimeout(() => { if (isCurrent()) _runSearch(q); }, 700);
            return;
        }
        if (!r.ok) {
            results.innerHTML = `<p class="audit-search-empty">${_esc(d.error || 'Search failed')}</p>`;
            return;
        }
        results.innerHTML = _searchResultsHtml(d);
        _wireSearchResults();
        // M4: announce the COUNT only, via the static announcer. The results
        // container is no longer an aria-live region (it re-announced up to 300
        // rows on every debounced keystroke), so the useful signal is spoken
        // once here.
        announceStatus(d.total
            ? `${d.total} match${d.total === 1 ? '' : 'es'} for ${q}.`
            : `No matches for ${q}.`);
    } catch (e) {
        if (!isCurrent()) return;
        results.innerHTML = `<p class="audit-search-empty">${_esc(String(e))}</p>`;
    }
}

function _searchResultsHtml(d) {
    if (!d.total) {
        return `<p class="audit-search-empty">No matches for “${_esc(d.q)}”.</p>`;
    }
    const head = d.truncated
        ? `<p class="audit-search-count">${d.shown} of ${d.total} matches for “${_esc(d.q)}”.</p>`
        : `<p class="audit-search-count">${d.total} match${d.total === 1 ? '' : 'es'} for “${_esc(d.q)}”.</p>`;
    const groups = (d.groups || []).map(g => {
        const label = _REPORT_LABELS[g.report] || g.report;
        const more = g.truncated
            ? ` <span class="audit-search-more">showing ${g.rows.length} of ${g.count}</span>`
            : '';
        // Finding 7: a group fully spent by the cross-group cap arrives with
        // count>0 but zero rows. Rendering a dead heading + empty list looked
        // broken; tell the user how to reach those matches instead.
        if (g.count && !(g.rows || []).length) {
            return `<section class="audit-search-group">
                        <h4>${_esc(label)} <span class="audit-search-n">${g.count}</span></h4>
                        <p class="audit-search-empty">Narrow your query to see these
                        ${g.count} match${g.count === 1 ? '' : 'es'}.</p>
                    </section>`;
        }
        const rows = (g.rows || []).map(row => {
            const detail = row.detail
                ? `<span class="audit-search-detail">${_esc(row.detail)}</span>` : '';
            // Finding 8 / M3: carry the row's identity so the jump lands ON the
            // row, not just the top of a table of hundreds. The server already
            // sends citation_key/pdf_rel per row; the old code discarded them.
            const key = row.citation_key ? ` data-key="${_attr(row.citation_key)}"` : '';
            const relAttr = row.pdf_rel ? ` data-rel="${_attr(row.pdf_rel)}"` : '';
            return `<li><button type="button" class="audit-search-hit"
                        data-report="${_attr(g.report)}"${key}${relAttr}>
                        <span class="audit-search-label">${_esc(row.label)}</span>${detail}
                    </button></li>`;
        }).join('');
        return `<section class="audit-search-group">
                    <h4>${_esc(label)} <span class="audit-search-n">${g.count}</span>${more}</h4>
                    <ul>${rows}</ul>
                </section>`;
    }).join('');
    return head + groups;
}

function _wireSearchResults() {
    const results = document.getElementById('audit-search-results');
    if (!results) return;
    results.querySelectorAll('.audit-search-hit').forEach(btn => {
        btn.addEventListener('click', async () => {
            const report = btn.getAttribute('data-report');
            if (!report) return;
            const key = btn.getAttribute('data-key');
            const rel = btn.getAttribute('data-rel');
            // Jump to the owning tab so the user lands on the real,
            // fully-featured report (with its row actions). selectAuditReport
            // drops the overlay, then we scroll to + focus the specific row so
            // the search's promise ("a hit names a row you can go look at") is
            // actually delivered (M3 / finding 8).
            await selectAuditReport(report);
            _scrollToReportRow(key, rel);
        });
    });
}

/** CSS.escape with a fallback (jsdom / very old engines lack window.CSS). */
function _cssEscape(s) {
    if (typeof CSS !== 'undefined' && CSS.escape) return CSS.escape(s);
    // Conservative fallback: escape everything that isn't a safe identifier char.
    return String(s).replace(/[^\w-]/g, (ch) => '\\' + ch);
}

/** Best-effort scroll-to + highlight + focus of the row a search hit names. */
function _scrollToReportRow(citationKey, pdfRel) {
    const body = document.getElementById('audit-report-body');
    if (!body) return;
    let target = null;
    if (citationKey) {
        target = body.querySelector(`[data-key="${_cssEscape(citationKey)}"]`);
    }
    if (!target && pdfRel) {
        target = body.querySelector(`[data-pdf="${_cssEscape(pdfRel)}"]`);
        // Fall back to a cell whose text is exactly the rel (skipped/ambiguous
        // tables render the rel as plain text, not a data attribute).
        if (!target) {
            target = Array.from(body.querySelectorAll('td'))
                .find(td => td.textContent.trim() === pdfRel) || null;
        }
    }
    if (!target) return;
    const rowEl = target.closest('tr') || target;
    rowEl.scrollIntoView({ block: 'center' });
    rowEl.classList.add('audit-row-highlight');
    // Remove the highlight after a moment so it reads as a transient "here it
    // is", not a permanent selection.
    setTimeout(() => rowEl.classList.remove('audit-row-highlight'), 2400);
    // Move focus to the row's first actionable control (or the row) so keyboard
    // + screen-reader users land there too, not just sighted ones.
    const focusable = rowEl.querySelector('button, a, select, input');
    if (focusable) focusable.focus();
    else if (rowEl.tabIndex >= 0) rowEl.focus();
}

/** Switch the active report tab (updating ARIA selection state) and load it. */
export async function selectAuditReport(name) {
    // The search panel overlays the report body — switching tabs while it is
    // open would otherwise render the new report underneath an unrelated
    // result list. The query itself is KEPT (a "Show results" button brings it
    // back), so following a hit into its report costs nothing to undo.
    _hideSearchResults();
    _currentReport = name;
    document.querySelectorAll('.audit-report-tab').forEach(t => {
        const selected = t.getAttribute('data-report') === name;
        t.classList.toggle('active', selected);
        t.setAttribute('aria-selected', selected ? 'true' : 'false');
        t.setAttribute('tabindex', selected ? '0' : '-1');
    });
    // Track 6c: keep the tabpanel's accessible name in sync with the active
    // tab (the static markup names the initial Inventory tab only).
    const panel = document.getElementById('audit-report-body');
    if (panel) panel.setAttribute('aria-labelledby', 'audit-rtab-' + name);
    await _loadReport(name);
}

// Item 3.3: rapid report-tab switches raced — the SLOWER response rendered
// last, leaving report A's table under report B's selected tab. Latest-wins:
// a superseded load abandons before touching the DOM.
const _reportLoadGate = makeLatestGate();

async function _loadReport(name) {
    const isCurrent = _reportLoadGate.enter();
    const meta = document.getElementById('audit-report-meta');
    const body = document.getElementById('audit-report-body');
    const tabs = document.getElementById('audit-report-tabs');
    const summaryBox = document.getElementById('audit-summary');
    if (!body) return;
    // Leaving the tagging sub-view: release its blob URL + overflow opt-out
    // (a window-global call, not an import — no audit→tagging edge).
    if (name !== 'tagging' && typeof window.taggingTeardown === 'function') {
        window.taggingTeardown();
    }
    // Same discipline for the ambiguous-PDF deck's blob URL — leaving the
    // report (or re-rendering it) must not leak the object URL.
    auditAmbigTeardown();
    body.textContent = 'Loading…';
    try {
        if (name === 'inventory') {
            const r = await secureFetch('/api/audit/inventory');
            const d = await r.json();
            if (!isCurrent()) return;
            if (!r.ok) {
                _hideReport();
                _showError(d.error || 'Inventory unavailable');
                return;
            }
            _lastSummary = d.summary;
            _hideEmptyState();
            _setTabsVisible(tabs, true);
            if (summaryBox) {
                summaryBox.style.display = '';
                summaryBox.innerHTML = _summaryHtml(d.summary);
            }
            if (meta) {
                meta.style.display = '';
                meta.textContent = `${d.records.length} records (one per citation key).`;
            }
            body.innerHTML = _inventoryTable(d.records);
        } else if (name === 'tagging') {
            // The Tagging sub-view is owned by tagging.js. audit.js keeps the
            // shared chrome (tabs + summary) and delegates the body/meta render
            // through a window global (a call, not an import — no audit->tagging
            // edge), handing over this load's latest-wins gate so a stale
            // tagging fetch can't clobber a report the user switched to.
            _hideEmptyState();
            _setTabsVisible(tabs, true);
            if (summaryBox && _lastSummary) {
                summaryBox.style.display = '';
                summaryBox.innerHTML = _summaryHtml(_lastSummary);
            }
            if (typeof window.taggingRenderInto === 'function') {
                await window.taggingRenderInto(body, meta, isCurrent);
                // taggingRenderInto swallows its own staleness (returns void),
                // so a superseded load reaches this line looking successful.
                // Without the guard, its continuation ran _wireRowActions()
                // over the NEXT report's already-wired DOM — double-binding
                // every .audit-link-btn (a reveal fired two `open -R`).
                if (!isCurrent()) return;
            } else {
                if (meta) { meta.style.display = 'none'; meta.textContent = ''; }
                body.textContent = 'Tagging module not loaded.';
            }
        } else {
            const r = await secureFetch(`/api/audit/reports/${encodeURIComponent(name)}`);
            const d = await r.json();
            if (!isCurrent()) return;
            if (!r.ok) {
                _hideReport();
                _showError(d.error || 'Report unavailable');
                return;
            }
            _hideEmptyState();
            _setTabsVisible(tabs, true);
            if (summaryBox && _lastSummary) {
                summaryBox.style.display = '';
                summaryBox.innerHTML = _summaryHtml(_lastSummary);
            }
            const [metaHtml, tableHtml] = _renderReport(name, d);
            if (meta) {
                meta.style.display = '';
                meta.innerHTML = metaHtml;
            }
            body.innerHTML = tableHtml;
        }
        _wireRowActions();
    } catch (e) {
        if (!isCurrent()) return;
        body.textContent = '';
        _showError(String(e));
    }
}

function _hideReport() {
    const meta = document.getElementById('audit-report-meta');
    const body = document.getElementById('audit-report-body');
    const tabs = document.getElementById('audit-report-tabs');
    const summaryBox = document.getElementById('audit-summary');
    if (meta) { meta.style.display = 'none'; meta.textContent = ''; }
    if (body) { body.innerHTML = ''; }
    _setTabsVisible(tabs, false);
    if (summaryBox) { summaryBox.style.display = 'none'; summaryBox.innerHTML = ''; }
}

function _summaryHtml(s) {
    const tile = (label, value) =>
        `<div class="audit-summary-tile"><div class="audit-summary-value">${_esc(String(value))}</div><div class="audit-summary-label">${_esc(label)}</div></div>`;
    const tiles = [
        tile('Records', s.record_count),
        tile('Bib + PDF', s.bib_with_pdf),
        tile('Bib + Obsidian note', s.bib_with_obsidian_note),
        tile('Bib + Zotero parent', s.bib_with_zotero_parent),
        tile('Triangulated', s.fully_triangulated),
        tile('Zotero w/ child note', s.zotero_parents_with_child_note),
        tile('PDFs unmapped', s.pdfs_unmapped),
        tile('PDFs ambiguous', s.pdfs_ambiguous),
        tile('PDFs skipped', s.pdfs_skipped),
    ];
    let html = `<div class="audit-summary-grid">${tiles.join('')}</div>`;
    if (s.zotero_error) {
        html += `<div class="audit-summary-warning">Zotero read warning: ${_esc(s.zotero_error)}</div>`;
    }
    return html;
}

function _inventoryTable(records) {
    const head = `
        <thead><tr>
            <th scope="col">Citation key</th><th scope="col">Year</th><th scope="col">Author</th><th scope="col">Title</th>
            <th scope="col">Bib</th><th scope="col">Zot</th><th scope="col">Note</th><th scope="col">PDFs</th><th scope="col">Annot</th><th scope="col">Match</th>
        </tr></thead>`;
    const rows = records.map(r => `
        <tr>
            <td>${_esc(r.citation_key)}</td>
            <td>${_esc(r.year || '')}</td>
            <td>${_esc(r.first_author || '')}</td>
            <td class="cell-title" title="${_esc(r.title || '')}" aria-label="${_esc(r.title || '')}">${_esc((r.title || '').slice(0, 90))}</td>
            <td>${r.has_bib_entry ? '✓' : ''}</td>
            <td>${r.has_zotero_item ? '✓' : ''}</td>
            <td>${r.has_obsidian_note ? '✓' : ''}</td>
            <td>${_esc(r.pdf_count)}${_pdfActions(r)}</td>
            <td>${r.annotations_count_max >= 0 ? r.annotations_count_max : '—'}</td>
            <td>${_esc((r.match_sources || []).join(','))}</td>
        </tr>`).join('');
    return `<table class="audit-table">${head}${_tbodyHtml(rows)}</table>`;
}

function _pdfActions(r) {
    if (!r.pdf_paths || !r.pdf_paths.length) return '';
    return r.pdf_paths.map(p =>
        ` <button type="button" class="audit-link-btn" data-action="open" data-abs="${_attr(p.abs)}" title="Open ${_attr(p.rel)}" aria-label="Open ${_attr(p.rel)}">[open]</button>`
    ).join('');
}

// --- Ambiguous-PDF triage deck --------------------------------------------
//
// A Covidence-style one-card-at-a-time surface over the ambiguous_pdfs report,
// modelled on tagging.js's deck. The table view is kept (bulk overview); the
// toggle persists in localStorage.
//
// Every write goes through /api/audit/mapping, and every write is undoable via
// its `clear` action — the deck is the reason bridge.clear_curation exists.

const _AMBIG_VIEW_KEY = 'audit_ambig_view';
const _ambig = {
    rows: [],            // the report's rows, as served
    idx: 0,
    sel: 0,              // highlighted candidate index on the current card
    // pdf rel -> {action:'match'|'no_match', citation_key?} for cards decided
    // this session. The report is a snapshot of the last scan, so decided rows
    // do not disappear until a rescan — this is what lets a card show its own
    // outcome and what the progress line counts.
    decided: new Map(),
    undo: [],            // [{pdf, label}] most recent last
    busy: false,         // gates writes only, never navigation
    // Scan-identity token (finding 3, 2026-07-20 audit): the last scan's
    // `finished_at`. decided/undo are keyed to a specific scan snapshot, so
    // they must survive a deck↔table view toggle (same scan, same rows, same
    // valid undo targets) but reset when a genuinely NEW scan replaces the
    // rows. Comparing this token — not "any payload arrival" — is what keeps a
    // resolved-then-toggled session's undo stack alive.
    scanToken: undefined,
};
// Set by _pollStatus/_loadReport so the ambiguous deck can tell a view toggle
// (no new scan) from a fresh scan. `null` until a scan's status is seen.
let _lastScanFinishedAt = null;
let _ambigPdfUrl = null;

/** Release the deck's blob URL. Mirrors window.taggingTeardown, but stays
 *  module-private: audit.js owns every path that leaves this report. */
function auditAmbigTeardown() {
    if (_ambigPdfUrl) {
        URL.revokeObjectURL(_ambigPdfUrl);
        _ambigPdfUrl = null;
    }
}

function _ambigViewMode() {
    try {
        return localStorage.getItem(_AMBIG_VIEW_KEY) === 'table' ? 'table' : 'deck';
    } catch (e) { return 'deck'; }
}

function _ambigSetView(mode) {
    try { localStorage.setItem(_AMBIG_VIEW_KEY, mode); } catch (e) { /* private mode */ }
    _loadReport('ambiguous_pdfs');
}

/** The deck shell — filled by _ambigMount once it is in the DOM. */
function _ambigDeckShell() {
    // M5: the deck is a keyboard-operated group, so it needs a role + name —
    // otherwise Tab lands on an anonymous focusable blob and VoiceOver says
    // nothing about the arrow/letter verbs that are now live.
    // M1: the progress/result <p>s are NOT role="status" — a live region minted
    // and populated in the same tick can be dropped by WKWebView (the renderer
    // this app ships in; see tests/js/liveRegions.test.js). They stay purely
    // VISUAL; every AT-relevant announcement is routed through the STATIC
    // #sr-status-announcer via announceStatus (card change, resolve, undo).
    return `<div id="audit-ambig-deck" class="audit-ambig-deck" tabindex="0"
                 role="group" aria-label="Ambiguous PDF triage deck">
        <p id="audit-ambig-progress" class="audit-ambig-progress"></p>
        <p id="audit-ambig-result" class="audit-ambig-result"></p>
        <div id="audit-ambig-card" class="audit-ambig-card"></div>
        <div id="audit-ambig-pdf" class="audit-ambig-pdf" style="display:none;"></div>
        <p class="audit-ambig-keys">
            <kbd>1</kbd>–<kbd>9</kbd> pick candidate · <kbd>A</kbd> assign ·
            <kbd>N</kbd> not in bib · <kbd>S</kbd> skip · <kbd>←</kbd> back ·
            <kbd>V</kbd> PDF · <kbd>⌫</kbd> undo
        </p>
    </div>`;
}

/** Wire the deck after its shell is in the DOM (called from _wireRowActions). */
function _ambigMount() {
    const deck = document.getElementById('audit-ambig-deck');
    if (!deck) return;
    _ambig.idx = 0;
    _ambig.sel = 0;
    _ambig.busy = false;
    // decided/undo deliberately survive a re-render so an undo stays available
    // after switching views, but a NEW report payload resets them (see the
    // ambiguous branch of _renderReport).
    deck.addEventListener('keydown', _ambigKeydown);
    _ambigRenderCard();
    deck.focus();
}

function _ambigCurrent() {
    return _ambig.rows[_ambig.idx] || null;
}

function _ambigProgress() {
    const el = document.getElementById('audit-ambig-progress');
    if (!el) return;
    const total = _ambig.rows.length;
    const done = _ambig.decided.size;
    const bits = [`PDF ${Math.min(_ambig.idx + 1, total)} of ${total}`, `${done} resolved`];
    if (_ambig.undo.length) bits.push(`${_ambig.undo.length} undoable`);
    el.textContent = bits.join(' · ');
}

function _ambigSay(msg, isError) {
    const el = document.getElementById('audit-ambig-result');
    if (!el) return;
    el.textContent = msg || '';
    el.classList.toggle('is-error', !!isError);
    // E4 (2026-07-22 audit): the visual line is deliberately NOT a live
    // region (M1 — every AT announcement routes through the static
    // announcer), and the success paths already announceStatus — but no
    // FAILURE path announced anything, so a VoiceOver user who assigned a
    // key heard silence on a 500 and moved on believing it landed.
    if (isError && msg) announceError(msg);
}

function _ambigRenderCard() {
    const card = document.getElementById('audit-ambig-card');
    if (!card) return;
    _ambigClosePdf();
    _ambigProgress();
    const row = _ambigCurrent();
    if (!row) { _ambigRenderDone(card); return; }

    const rel = row.pdf.rel;
    const prior = _ambig.decided.get(rel);
    const cands = row.candidates || [];
    _ambig.sel = Math.min(_ambig.sel, Math.max(cands.length - 1, 0));

    const priorHtml = prior
        ? `<p class="audit-ambig-prior">Already resolved this session: ${
            prior.action === 'no_match'
                ? 'not in bibliography'
                : 'assigned to ' + _esc(prior.citation_key)}. Deciding again replaces it.</p>`
        : '';
    const candHtml = cands.map((c, i) => `
        <li>
            <button type="button" class="audit-ambig-cand${i === _ambig.sel ? ' is-sel' : ''}"
                    data-ambig="pick" data-i="${i}">
                <span class="audit-ambig-num">${i + 1}</span>
                <span class="audit-ambig-key">${_esc(c.citation_key)}</span>
                <span class="audit-ambig-title">${_esc(c.title || '')}</span>
            </button>
        </li>`).join('');

    card.innerHTML = `
        <h4 class="audit-ambig-name">${_esc(row.pdf.name || rel)}</h4>
        <p class="audit-ambig-path" title="${_attr(rel)}">${_esc(rel)}</p>
        ${priorHtml}
        <ul class="audit-ambig-cands">${candHtml}</ul>
        <div class="audit-ambig-actions">
            <button type="button" class="audit-btn-primary" data-ambig="assign">Assign selected</button>
            <button type="button" class="audit-link-btn" data-ambig="no-match" aria-label="Mark as not in the bibliography">[not in bib]</button>
            <button type="button" class="audit-link-btn" data-ambig="skip">[skip]</button>
            <button type="button" class="audit-link-btn" data-ambig="prev">[back]</button>
            <button type="button" class="audit-link-btn" data-ambig="pdf" aria-label="View the PDF">[view PDF]</button>
            <button type="button" class="audit-link-btn" data-action="reveal" data-abs="${_attr(row.pdf.abs)}" aria-label="Reveal the PDF in Finder">[reveal]</button>
            <button type="button" class="audit-link-btn" data-ambig="undo"${_ambig.undo.length ? '' : ' disabled'}>[undo]</button>
        </div>`;
    _ambigWireCard(card);
    // H2: announce the card so a screen-reader user isn't triaging blind — the
    // visible progress line is silent to AT (M1). Routed through the STATIC
    // announcer, not a render-time live region.
    const total = _ambig.rows.length;
    announceStatus(`PDF ${Math.min(_ambig.idx + 1, total)} of ${total}: ${row.pdf.name || rel}`);
    // H1: keep focus on the deck so the keyboard verbs keep working after a
    // mouse pick / a confirmed write re-render destroyed the previously-focused
    // button (focus would otherwise fall to <body> and every verb goes dead).
    // Never steal focus from an open confirm strip.
    _ambigRefocusDeck();
}

/** Return focus to the deck unless a confirm strip currently owns it. */
function _ambigRefocusDeck() {
    const active = document.activeElement;
    if (active && active.closest && active.closest('.inline-confirm-strip')) return;
    const deck = document.getElementById('audit-ambig-deck');
    if (deck) deck.focus();
}

function _ambigRenderDone(card) {
    // L6: distinguish "there was nothing to triage" from "you finished". The
    // old copy read "All 0 ambiguous PDFs reviewed — 0 resolved" on an empty
    // report, which is both awkward and misleading.
    const total = _ambig.rows.length;
    const body = total === 0
        ? `<p class="audit-ambig-done">No ambiguous PDFs — every scanned PDF matched
           a single bib key (or none). Nothing to triage here.</p>`
        : `<p class="audit-ambig-done">All ${total} ambiguous PDFs reviewed —
           ${_ambig.decided.size} resolved. Run a new scan to refresh the list.</p>`;
    const actions = total === 0 ? '' : `
        <div class="audit-ambig-actions">
            <button type="button" class="audit-link-btn" data-ambig="restart">[review again]</button>
            <button type="button" class="audit-link-btn" data-ambig="undo"${_ambig.undo.length ? '' : ' disabled'}>[undo last]</button>
        </div>`;
    card.innerHTML = body + actions;
    _ambigWireCard(card);
    announceStatus(total === 0
        ? 'No ambiguous PDFs to triage.'
        : `All ${total} ambiguous PDFs reviewed, ${_ambig.decided.size} resolved.`);
    _ambigRefocusDeck();
}

function _ambigWireCard(card) {
    card.querySelectorAll('[data-ambig]').forEach(btn => {
        btn.addEventListener('click', () => _ambigVerb(btn.getAttribute('data-ambig'), btn));
    });
    // The reveal button reuses the shared row-action handler.
    card.querySelectorAll('[data-action="reveal"]').forEach(btn => {
        btn.addEventListener('click', (e) => { e.preventDefault(); _ambigReveal(btn); });
    });
}

async function _ambigReveal(btn) {
    const abs = btn.getAttribute('data-abs');
    if (!abs) return;
    try {
        const r = await secureFetch('/api/audit/reveal', {
            method: 'POST', body: JSON.stringify({ path: abs }),
        });
        if (!r.ok) {
            const d = await r.json().catch(() => ({}));
            _ambigSay(d.error || `Reveal failed (${r.status})`, true);
        }
    } catch (e) { _ambigSay(String(e), true); }
}

function _ambigVerb(verb, btn) {
    switch (verb) {
        case 'pick': {
            const i = parseInt(btn.getAttribute('data-i'), 10);
            if (Number.isFinite(i)) { _ambig.sel = i; _ambigRenderCard(); }
            break;
        }
        case 'assign': _ambigConfirmWrite(btn, 'match'); break;
        case 'no-match': _ambigConfirmWrite(btn, 'no_match'); break;
        case 'skip': _ambigMove(1); break;
        case 'prev': _ambigMove(-1); break;
        case 'pdf': _ambigTogglePdf(); break;
        case 'undo': _ambigUndo(btn); break;
        case 'restart': _ambig.idx = 0; _ambigRenderCard(); break;
        default: break;
    }
}

function _ambigMove(delta) {
    const next = _ambig.idx + delta;
    if (next < 0 || next > _ambig.rows.length) return;
    _ambig.idx = next;
    _ambig.sel = 0;
    _ambigRenderCard();
}

/**
 * Batch G (2026-07-22): retire one tag from every store, from the Tag Hygiene
 * report. Streams per-record progress into a status line above the report
 * table; the terminal frame offers a one-click batch Undo (confirm-gated —
 * it rewrites user data across all three stores).
 */
function _retireStatusHost() {
    const body = document.getElementById('audit-report-body');
    if (!body) return null;
    let host = body.querySelector('#audit-retire-status');
    if (!host) {
        host = document.createElement('div');
        host.id = 'audit-retire-status';
        host.className = 'muted';
        body.insertBefore(host, body.firstChild);
    }
    // Dedicated text child: say() updates ONLY this span, and each batch's
    // Undo button is appended as a SIBLING after it — so a later retire's
    // status updates never destroy an earlier batch's Undo button (the old
    // host.textContent = msg wiped the whole host, buttons included).
    let text = host.querySelector('.audit-retire-status-text');
    if (!text) {
        text = document.createElement('span');
        text.className = 'audit-retire-status-text';
        host.insertBefore(text, host.firstChild);
    }
    return host;
}

// Announcements go through the STATIC announcer regions (announceStatus /
// announceError), never a minted live region on the host — WebKit drops
// dynamically-minted live regions (documented project hazard).
function _retireSay(msg, isErr) {
    // Re-resolve the host on every write: a report re-render between SSE
    // frames would leave a node captured at run start disconnected.
    const host = _retireStatusHost();
    if (host) host.querySelector('.audit-retire-status-text').textContent = msg;
    if (isErr) announceError(msg); else announceStatus(msg);
}

// Cancel affordance for the batch-undo stream (2026-08-04). Honest scope:
// aborting disconnects THIS client — operations already reverted stay
// reverted, the server worker stops once it notices the consumer is gone,
// and whatever remains is still `applied` in the journal, so the kept undo
// button retries exactly the remainder.
function _mkUndoCancel(host, ctrl, tag) {
    if (!host) return null;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'audit-link-btn';
    btn.textContent = '[cancel undo]';
    const scope = 'Stop waiting for this undo. Operations already reverted stay '
        + 'reverted; the rest remain retryable from the undo button.';
    btn.title = scope;
    btn.setAttribute('aria-label', `Cancel the undo of "${tag}". ${scope}`);
    btn.addEventListener('click', () => {
        ctrl.abort();
        btn.disabled = true;
        btn.textContent = '[cancelling…]';
    });
    host.appendChild(document.createTextNode(' '));
    host.appendChild(btn);
    return btn;
}

// Disable/enable EVERY [retire] button in the report while one run is in
// flight — a second concurrent retire would interleave status lines and
// journal batches.
function _retireSetAllDisabled(on) {
    const body = document.getElementById('audit-report-body');
    if (!body) return;
    body.querySelectorAll('button[data-action="retire-tag"]').forEach((b) => { b.disabled = on; });
}

async function _retireTag(btn, tag) {
    const say = _retireSay;
    _retireSetAllDisabled(true);
    let terminal = null;
    try {
        await consumeSSE('/api/tagging/retire-tag', {
            method: 'POST',
            body: JSON.stringify({
                tag,
                include_vocab: btn.getAttribute('data-invocab') === '1',
                confirm: true,
            }),
        }, {
            onInfo: (info) => say(info),
            onOther: (frame) => { if (frame && frame.retire) terminal = frame.retire; },
            onError: (err) => { throw new Error(err); },
        });
        if (!terminal) { say(`Retire of "${tag}" produced no result.`, true); return; }
        const c = terminal.results || {};
        const bits = ['note', 'finder', 'zotero'].map((t) =>
            `${t}: ${(c[t] || {}).removed || 0} removed / ${(c[t] || {}).skipped || 0} skipped`
            + ((c[t] || {}).failed ? ` / ${(c[t] || {}).failed} FAILED` : ''));
        if (terminal.vocab_rows) bits.push(`_tags.md: ${terminal.vocab_rows} row(s) edited`);
        say(`Retired "${tag}" — ${bits.join(' · ')}.`
            + (terminal.failures && terminal.failures.length
                ? ` Failures: ${terminal.failures.slice(0, 3).join('; ')}${terminal.failures.length > 3 ? '…' : ''}` : '')
            + ' (Snapshot view: counts refresh on the next scan.)');
        // Each batch appends its OWN labelled undo button (no single-button
        // guard): retiring a second tag must not eat the first batch's undo.
        const host = _retireStatusHost();
        if (terminal.batch_id && host) {
            const undo = document.createElement('button');
            undo.type = 'button';
            undo.className = 'audit-link-btn';
            undo.textContent = `[undo retire ${tag}]`;
            undo.addEventListener('click', () => confirmInline(undo, {
                message: `Re-add "${tag}" everywhere it was just removed (${terminal.op_count} operations)?`,
                confirmLabel: 'Undo retire',
                onConfirm: async () => {
                    undo.disabled = true;
                    // SSE since 2026-07-30: a 150-op undo is minutes of work,
                    // and as a plain JSON request it reported nothing until it
                    // finished. Per-op {info} frames drive the same status
                    // line the retire itself uses; the terminal {restore}
                    // frame carries what the JSON body used to.
                    const ctrl = new AbortController();
                    const cancel = _mkUndoCancel(host, ctrl, tag);
                    let done = null;
                    try {
                        await consumeSSE('/api/tagging/restore', {
                            method: 'POST',
                            body: JSON.stringify({ batch_id: terminal.batch_id, confirm: true }),
                            signal: ctrl.signal,
                        }, {
                            onInfo: (info) => say(`Undo: ${info}`),
                            onOther: (frame) => { if (frame && frame.restore) done = frame.restore; },
                            onError: (err) => { throw new Error(err); },
                        });
                        if (!done) { say('Undo produced no result.', true); undo.disabled = false; return; }
                        if (done.reverted < done.total) {
                            // Partial revert. The server deliberately keeps
                            // failed/skipped ops `applied` (journal
                            // conservatism) so a re-run picks up exactly the
                            // remainder — removing this button here stranded
                            // them with no one-click path (2026-08-04 audit).
                            const left = done.total - done.reverted;
                            say(`Undo: ${done.reverted}/${done.total} operation(s) reverted — `
                                + `${left} remain and stay retryable.`, true);
                            undo.textContent = `[retry undo retire ${tag} (${left} remaining)]`;
                            undo.disabled = false;
                            return;
                        }
                        say(`Undo: ${done.reverted}/${done.total} operation(s) reverted.`);
                        undo.remove();
                    } catch (err) {
                        if (err && err.name === 'AbortError') {
                            // UI-only cancel: already-reverted ops stay
                            // reverted; the rest are still retryable.
                            say(`Undo of "${tag}" cancelled — operations already reverted stay `
                                + 'reverted; use the undo button again for the rest.');
                            undo.disabled = false;
                            return;
                        }
                        logError('retire undo failed: ' + String(err));
                        say(`Undo failed: ${err.message || 'network'}`, true);
                        undo.disabled = false;
                    } finally {
                        if (cancel) cancel.remove();
                    }
                },
            }));
            host.appendChild(document.createTextNode(' '));
            host.appendChild(undo);
        }
    } catch (e) {
        logError('retire-tag failed: ' + String(e));
        say(`Retire of "${tag}" failed: ${e.message}`, true);
    } finally {
        _retireSetAllDisabled(false);
    }
}

/**
 * Confirm-gate then perform a mapping write.
 *
 * Routed through ui.js::confirmInline like every other write in this app that
 * mutates a user's curation file — the pre-deck table assigned on a single
 * click with no ceremony and no way back.
 */
function _ambigConfirmWrite(btn, action) {
    if (_ambig.busy) return;
    const row = _ambigCurrent();
    if (!row) return;
    const cand = (row.candidates || [])[_ambig.sel];
    if (action === 'match' && !cand) { _ambigSay('No candidate selected.', true); return; }
    const message = action === 'no_match'
        ? `Record “${row.pdf.name}” as not in the bibliography?`
        : `Assign “${row.pdf.name}” to ${cand.citation_key}?`;
    confirmInline(btn, {
        message,
        confirmLabel: action === 'no_match' ? 'Not in bib' : 'Assign',
        onConfirm: () => _ambigWrite(action, cand ? cand.citation_key : null),
    });
}

async function _ambigWrite(action, citationKey) {
    const row = _ambigCurrent();
    if (!row || _ambig.busy) return;
    _ambig.busy = true;
    const rel = row.pdf.rel;
    // Capture the card index BEFORE the await. Navigation (skip/back) is
    // deliberately not busy-gated, so the user can move while the POST is in
    // flight; if they do, we must NOT blindly `idx += 1` on the response (that
    // advanced an extra card and silently skipped one — finding 1). We advance
    // only when the user is still parked on the card we just wrote.
    const writtenIdx = _ambig.idx;
    const body = action === 'no_match'
        ? { pdf: rel, no_match: true }
        : { pdf: rel, citation_key: citationKey };
    _ambigSay('Applying…');   // L5: in-flight feedback (writes were silent)
    try {
        const r = await secureFetch('/api/audit/mapping', {
            method: 'POST', body: JSON.stringify(body),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { _ambigSay(d.error || `Mapping failed (${r.status})`, true); return; }
        _ambig.decided.set(rel, { action, citation_key: citationKey });
        _ambig.undo.push({
            pdf: rel,
            label: action === 'no_match' ? 'not in bib' : citationKey,
        });
        _ambigSay(action === 'no_match'
            ? `“${row.pdf.name}” recorded as not in the bibliography.`
            : `“${row.pdf.name}” assigned to ${citationKey}.`);
        announceStatus(`Resolved ${row.pdf.name}`);
        // Advance past the written card only if the user hasn't navigated since.
        if (_ambig.idx === writtenIdx) {
            _ambig.idx += 1;
            _ambig.sel = 0;
        }
        _ambigRenderCard();
    } catch (e) {
        _ambigSay(String(e), true);
    } finally {
        // Cleared before the re-render's own state so a failed write leaves the
        // card interactive and retryable.
        _ambig.busy = false;
        _ambigProgress();
    }
}

/** Undo the most recent decision — confirm-gated, since it rewrites the
 *  curation file just as the forward action did. */
function _ambigUndo(btn) {
    if (_ambig.busy || !_ambig.undo.length) return;
    const last = _ambig.undo[_ambig.undo.length - 1];   // peek, not pop
    confirmInline(btn, {
        message: `Undo “${last.label}” for this PDF?`,
        confirmLabel: 'Undo',
        onConfirm: () => _ambigPerformUndo(),
    });
}

async function _ambigPerformUndo() {
    if (_ambig.busy || !_ambig.undo.length) return;
    // Peek-not-pop: a failed undo must stay retryable.
    const last = _ambig.undo[_ambig.undo.length - 1];
    _ambig.busy = true;
    _ambigSay('Undoing…');   // L5: undo was silent while in flight
    try {
        const r = await secureFetch('/api/audit/mapping', {
            method: 'POST', body: JSON.stringify({ pdf: last.pdf, clear: true }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { _ambigSay(d.error || `Undo failed (${r.status})`, true); return; }
        _ambig.undo.pop();
        _ambig.decided.delete(last.pdf);
        // Land back on the card that was undone.
        const i = _ambig.rows.findIndex(row => row.pdf.rel === last.pdf);
        if (i >= 0) _ambig.idx = i;
        _ambigSay(`Undone — “${last.pdf}” is un-curated again.`);
        _ambigRenderCard();
    } catch (e) {
        _ambigSay(String(e), true);
    } finally {
        _ambig.busy = false;
        _ambigProgress();
    }
}

/** On-demand PDF side panel. The origin guard 403s an <iframe src="/api/…">,
 *  so the bytes are fetched with the header and rendered from a blob: URL. */
function _ambigClosePdf() {
    const panel = document.getElementById('audit-ambig-pdf');
    const deck = document.getElementById('audit-ambig-deck');
    if (panel) { panel.style.display = 'none'; panel.innerHTML = ''; }
    if (deck) deck.classList.remove('with-pdf');
    auditAmbigTeardown();
}

async function _ambigTogglePdf() {
    const panel = document.getElementById('audit-ambig-pdf');
    const deck = document.getElementById('audit-ambig-deck');
    if (!panel) return;
    if (panel.style.display !== 'none') { _ambigClosePdf(); return; }
    const row = _ambigCurrent();
    if (!row) return;
    const rel = row.pdf.rel;
    panel.style.display = '';
    if (deck) deck.classList.add('with-pdf');
    panel.textContent = 'Loading PDF…';
    // Identity guard shared by every post-await write into the panel (finding
    // 5): the user may have navigated, or closed the panel, while bytes were in
    // flight. Writing this card's error/PDF into a panel that now belongs to a
    // different card (or is closed) is worse than showing nothing.
    const _stillCurrent = () => {
        const cur = _ambigCurrent();
        return cur && cur.pdf.rel === rel && panel.style.display !== 'none';
    };
    try {
        const r = await secureFetch(`/api/audit/pdf?rel=${encodeURIComponent(rel)}`);
        if (!_stillCurrent()) return;
        if (!r.ok) {
            const d = await r.json().catch(() => ({}));
            if (!_stillCurrent()) return;
            panel.textContent = d.error || `Could not load PDF (${r.status})`;
            return;
        }
        const blob = await r.blob();
        if (!_stillCurrent()) return;
        auditAmbigTeardown();
        _ambigPdfUrl = URL.createObjectURL(blob);
        panel.innerHTML = '';
        const frame = document.createElement('iframe');
        frame.className = 'audit-ambig-frame';
        frame.src = _ambigPdfUrl;
        frame.title = `Preview of ${rel}`;
        panel.appendChild(frame);
        // WKWebView builds can refuse inline blob PDFs — same fallback the
        // tagging deck offers.
        const open = document.createElement('button');
        open.type = 'button';
        open.className = 'audit-link-btn';
        open.textContent = '[open in PDF viewer]';
        open.addEventListener('click', async () => {
            try {
                await secureFetch('/api/audit/reveal', {
                    method: 'POST', body: JSON.stringify({ open: row.pdf.abs }),
                });
            } catch (e) { _ambigSay(String(e), true); }
        });
        panel.appendChild(open);
    } catch (e) {
        // Same identity guard on the failure path — a stale fetch rejection must
        // not clobber the panel content of whatever card is showing now.
        if (_stillCurrent()) panel.textContent = String(e);
    }
}

function _ambigKeydown(e) {
    // Never steal keys from the confirm strip (its buttons need native
    // activation).
    if (e.target.closest && e.target.closest('.inline-confirm-strip')) return;
    // L2: only Enter/Space are the button's own (native activation); the verb
    // keys (S/V/←/N/⌫/1-9) must still work when a button holds focus — after a
    // confirm's Cancel returns focus to the anchor button, blanket-returning on
    // every BUTTON left the whole keyboard interface dead until Tab-out.
    if (e.target.tagName === 'BUTTON' && (e.key === 'Enter' || e.key === ' ')) return;
    // L3: Escape backs out of the PDF panel first, matching "Escape closes the
    // thing that's open" learned from the search overlay and confirm strip.
    if (e.key === 'Escape') {
        const panel = document.getElementById('audit-ambig-pdf');
        if (panel && panel.style.display !== 'none') {
            _ambigClosePdf();
            _ambigRefocusDeck();
            e.preventDefault();
        }
        return;
    }
    const cands = (_ambigCurrent() || {}).candidates || [];
    if (e.key >= '1' && e.key <= '9') {
        const i = parseInt(e.key, 10) - 1;
        if (i < cands.length) { _ambig.sel = i; _ambigRenderCard(); e.preventDefault(); }
        return;
    }
    const card = document.getElementById('audit-ambig-card');
    const btn = (v) => card && card.querySelector(`[data-ambig="${v}"]`);
    switch (e.key) {
        case 'a': case 'A': case 'Enter': {
            const b = btn('assign');
            if (b) { _ambigConfirmWrite(b, 'match'); e.preventDefault(); }
            break;
        }
        case 'n': case 'N': {
            const b = btn('no-match');
            if (b) { _ambigConfirmWrite(b, 'no_match'); e.preventDefault(); }
            break;
        }
        case 's': case 'S': case 'ArrowRight': _ambigMove(1); e.preventDefault(); break;
        case 'ArrowLeft': _ambigMove(-1); e.preventDefault(); break;
        case 'v': case 'V': _ambigTogglePdf(); e.preventDefault(); break;
        case 'Backspace': {
            const b = btn('undo');
            if (b && !b.disabled) { _ambigUndo(b); e.preventDefault(); }
            break;
        }
        default: break;
    }
}

function _renderReport(name, d) {
    if (name === 'note_tag_drift') {
        // "Missing" = absent from the note in ANY spelling (fingerprint
        // comparison, plan 2026-08-05/2); "variants" = present under another
        // spelling — informational only, so no Apply is offered for them
        // (applying one would no-op in the writer's dedup: the old
        // unresolvable-row loop).
        const missingCount = d.rows.filter(r => (r.missing_in_obs || []).length).length;
        const infoOnly = d.rows.length - missingCount;
        const meta = `${_esc(missingCount)} citation keys with Zotero tags genuinely missing from the Obsidian YAML`
            + (infoOnly ? ` (+${_esc(infoOnly)} with variants / note-only tags only)` : '')
            + (d.include_parent_tags ? ' Zotero item tags included (see Audit Settings); “(item)” marks tags only the item carries.' : '')
            + `. <strong>Apply</strong> writes the missing tags into the note's frontmatter (a <code>.bak</code> is kept).`;
        const head = '<thead><tr><th scope="col">Key</th><th scope="col">Author</th><th scope="col">Title</th><th scope="col">Zotero tags</th><th scope="col">Obs YAML tags</th><th scope="col">Missing in Obs</th><th scope="col">Variants (present, other spelling)</th><th scope="col">Note-only</th><th scope="col"></th></tr></thead>';
        const rows = d.rows.map(r => {
            const missing = r.missing_in_obs || [];
            const itemOnly = new Set(r.item_only_missing || []);
            const missingTxt = missing
                .map(t => itemOnly.has(t) ? `${t} (item)` : t).join(', ');
            const variants = (r.variants || [])
                .map(v => `${v.zotero} ~ ${v.note}${v.reason === 'synonym' ? ' (syn)' : ''}`)
                .join(', ');
            const applyBtn = missing.length
                ? `<button type="button" class="audit-link-btn" data-action="apply-tags" data-key="${_attr(r.citation_key)}" data-tags="${_attr(JSON.stringify(missing))}" aria-label="Apply the missing tags to the Obsidian note">[apply to Obsidian]</button>`
                : '';
            return `
            <tr data-key="${_attr(r.citation_key)}">
                <td>${_esc(r.citation_key)}</td>
                <td>${_esc(r.author || '')}</td>
                <td class="cell-title" title="${_esc(r.title || '')}" aria-label="${_esc(r.title || '')}">${_esc((r.title || '').slice(0, 80))}</td>
                <td>${_esc(r.zotero_note_tags.join(', '))}</td>
                <td>${_esc(r.obs_tags.join(', '))}</td>
                <td><strong>${_esc(missingTxt)}</strong></td>
                <td title="Present in both stores under different spellings (or as a _tags.md synonym) — a hygiene signal, not drift">${_esc(variants)}</td>
                <td title="Note tags absent from the Zotero side — read-only; write back via the Tagging view if wanted">${_esc((r.note_only || []).join(', '))}</td>
                <td>${applyBtn}</td>
            </tr>`;
        }).join('');
        return [meta, `<table class="audit-table">${head}${_tbodyHtml(rows)}</table>`];
    }
    if (name === 'finder_tag_drift') {
        // Correctable since 2026-08-06: the per-row verbs POST to the tagging
        // subsystem's journalled writers (apply-note-tags / apply-finder-tags)
        // — never a new audit write path. The payload's vocab_ids maps each
        // drift surface to the exact _tags.md id the writers' vocabulary gate
        // accepts (or null = out of vocab → badged, [+ vocab] to admit it).
        const meta = `${_esc(d.rows.length)} papers whose PDF Finder tags and note YAML tags disagree `
            + `(only papers carrying at least one Finder tag are compared). `
            + `<strong>[→ note]</strong> / <strong>[→ Finder]</strong> write the gap through the `
            + `Tagging writers — confirmed, journalled, undoable per row. A tag marked `
            + `<em>not in vocab</em> is absent from _tags.md (the writers refuse it); `
            + `[+ vocab] adds it to the vocabulary, then applies it.`;
        const head = '<thead><tr><th scope="col">Key</th><th scope="col">Author</th><th scope="col">Title</th><th scope="col">Finder tags</th><th scope="col">Obs YAML tags</th><th scope="col">Finder-only</th><th scope="col">Note-only</th><th scope="col">Variants (present, other spelling)</th><th scope="col">PDFs</th><th scope="col"></th></tr></thead>';
        const surfSpan = (r, s, dir) => {
            const vid = (r.vocab_ids || {})[s];
            if (vid) {
                // Show the mapped vocab id when it differs — that id, not the
                // drift surface, is what the writer will store.
                const mapped = vid !== s ? ` → ${_esc(vid)}` : '';
                return `<span class="fdrift-surf" data-dir="${_attr(dir)}" data-vid="${_attr(vid)}">${_esc(s)}${mapped}</span>`;
            }
            return `<span class="fdrift-surf" data-dir="${_attr(dir)}" title="Not in the _tags.md controlled vocabulary — the tagging writers refuse it. [+ vocab] adds it first, then applies.">${_esc(s)} <em>(not in vocab)</em></span>`
                + ` <button type="button" class="audit-link-btn" data-action="finder-vocab-add" data-key="${_attr(r.citation_key)}" data-tag="${_attr(s)}" data-dir="${_attr(dir)}" aria-label="Add ${_attr(s)} to the vocabulary, then apply it">[+ vocab]</button>`;
        };
        const rows = d.rows.map(r => {
            const vocabIds = r.vocab_ids || {};
            const noteIds = [...new Set((r.finder_only || []).map(s => vocabIds[s]).filter(Boolean))];
            const finderIds = [...new Set((r.note_only || []).map(s => vocabIds[s]).filter(Boolean))];
            const variants = (r.variants || [])
                .map(v => `${v.finder} ~ ${v.note}${v.reason === 'synonym' ? ' (syn)' : ''}`)
                .join(', ');
            const applyNote = (noteIds.length && r.note_rel)
                ? `<button type="button" class="audit-link-btn" data-action="finder-apply-note" data-key="${_attr(r.citation_key)}" data-tags="${_attr(JSON.stringify(noteIds))}" data-note="${_attr(r.note_rel)}" aria-label="Write the Finder-only tags into the Obsidian note">[→ note (${noteIds.length})]</button>`
                : '';
            const applyFinder = finderIds.length
                ? `<button type="button" class="audit-link-btn" data-action="finder-apply-finder" data-key="${_attr(r.citation_key)}" data-tags="${_attr(JSON.stringify(finderIds))}" data-pdfs="${_attr(r.pdf_count)}" aria-label="Write the note-only tags onto the PDF Finder tags">[→ Finder (${finderIds.length})]</button>`
                : '';
            return `
            <tr data-key="${_attr(r.citation_key)}">
                <td>${_esc(r.citation_key)}</td>
                <td>${_esc(r.author || '')}</td>
                <td class="cell-title" title="${_esc(r.title || '')}" aria-label="${_esc(r.title || '')}">${_esc((r.title || '').slice(0, 80))}</td>
                <td>${_esc((r.finder_tags || []).join(', '))}</td>
                <td>${_esc((r.obs_tags || []).join(', '))}</td>
                <td><strong>${(r.finder_only || []).map(s => surfSpan(r, s, 'note')).join(', ')}</strong></td>
                <td>${(r.note_only || []).map(s => surfSpan(r, s, 'finder')).join(', ')}</td>
                <td title="Present in both stores under different spellings (or as a _tags.md synonym) — a hygiene signal, not drift">${_esc(variants)}</td>
                <td>${_esc(r.pdf_count)}</td>
                <td>${applyNote} ${applyFinder}</td>
            </tr>`;
        }).join('');
        return [meta, `<table class="audit-table">${head}${_tbodyHtml(rows)}</table>`];
    }
    if (name === 'ambiguous_pdfs') {
        const mode = _ambigViewMode();
        const toggle = `<span class="audit-view-toggle">`
            + `<button type="button" class="audit-link-btn${mode === 'deck' ? ' is-active' : ''}" data-ambig-view="deck">[triage deck]</button>`
            + `<button type="button" class="audit-link-btn${mode === 'table' ? ' is-active' : ''}" data-ambig-view="table">[table]</button>`
            + `</span>`;
        const meta = `${_esc(d.count)} PDFs the bridge could not disambiguate (author+year matched more than one bib key). Pick the right key to record a manual mapping. ${toggle}`;
        if (mode === 'deck') {
            // Rows always refresh from the payload (the DOM row set). But
            // decided/undo are keyed to a scan SNAPSHOT — reset them only when a
            // genuinely new scan replaced the rows, NOT on a deck↔table toggle
            // (which re-fetches the same scan). Keying on scan identity instead
            // of "any payload arrival" is the finding-3 fix: the old code wiped
            // the only undo surface every time the user toggled to the table to
            // review and back.
            _ambig.rows = d.rows || [];
            if (_ambig.scanToken !== _lastScanFinishedAt) {
                _ambig.decided = new Map();
                _ambig.undo = [];
                _ambig.scanToken = _lastScanFinishedAt;
            }
            return [meta, _ambigDeckShell()];
        }
        const head = '<thead><tr><th scope="col">PDF</th><th scope="col">Candidate keys</th><th scope="col"></th></tr></thead>';
        const rows = d.rows.map(r => {
            const opts = r.candidates.map(c =>
                `<option value="${_attr(c.citation_key)}">${_esc(c.citation_key)}${c.title ? ' — ' + _esc(c.title.slice(0, 50)) : ''}</option>`).join('');
            return `
            <tr>
                <td>${_esc(r.pdf.rel)}</td>
                <td><select class="audit-ambig-select" data-pdf="${_attr(r.pdf.rel)}">${opts}</select></td>
                <td>
                    <button type="button" class="audit-link-btn" data-action="pick-key" data-pdf="${_attr(r.pdf.rel)}">[assign]</button>
                    <button type="button" class="audit-link-btn" data-action="no-match" data-pdf="${_attr(r.pdf.rel)}">[not in bib]</button>
                    <button type="button" class="audit-link-btn" data-action="reveal" data-abs="${_attr(r.pdf.abs)}">[reveal]</button>
                </td>
            </tr>`;
        }).join('');
        return [meta, `<table class="audit-table">${head}${_tbodyHtml(rows)}</table>`];
    }
    if (name === 'skipped_pdfs') {
        const meta = `${_esc(d.count)} PDFs skipped by the "<code>${_esc(d.skip_prefix || '')}</code>" prefix (excluded from every report except duplicates).`;
        const head = '<thead><tr><th scope="col">PDF</th><th scope="col"></th></tr></thead>';
        const rows = d.rows.map(r => `
            <tr>
                <td>${_esc(r.pdf.rel)}</td>
                <td>
                    <button type="button" class="audit-link-btn" data-action="open" data-abs="${_attr(r.pdf.abs)}">[open]</button>
                    <button type="button" class="audit-link-btn" data-action="reveal" data-abs="${_attr(r.pdf.abs)}">[reveal]</button>
                </td>
            </tr>`).join('');
        return [meta, `<table class="audit-table">${head}${_tbodyHtml(rows)}</table>`];
    }
    if (name === 'unread_unzoterod') {
        // Say the containment out loud AND give a one-click way up to the full
        // list: landing here and wanting "all of them" is the exact journey the
        // old tab names broke.
        const meta = `${_esc(d.rows.length)} of the PDFs without Zotero have fewer than `
            + `${_esc(d.threshold)} annotations — a subset of `
            + `${_gotoReportBtn('read_unzoterod', '[PDFs Without Zotero]')}, not the rest of it`
            + ` · ${_esc(d.ambiguous_count)} ambiguous PDFs excluded.`;
        return [meta, _pdfReportTable(d.rows)];
    }
    if (name === 'read_unzoterod') {
        // The complete un-Zotero'd set (read AND unread), so the meta line must
        // also name what it leaves out — silently omitting the ambiguous /
        // confirmed-no-match / skip-prefix buckets is what made this tab look
        // like it only held the "read" half. Zero counts are DROPPED rather than
        // printed as "0 ambiguous · 0 …": an omission that didn't happen is not
        // information, and the noise would bury the ones that did.
        const excl = [];
        if (d.ambiguous_count) {
            excl.push(`${_esc(d.ambiguous_count)} ambiguous `
                + _gotoReportBtn('ambiguous_pdfs', '[resolve]'));
        }
        if (d.confirmed_no_match_count) {
            excl.push(`${_esc(d.confirmed_no_match_count)} you confirmed are not in the bib`);
        }
        if (d.skipped_count) {
            excl.push(`${_esc(d.skipped_count)} skipped by prefix `
                + _gotoReportBtn('skipped_pdfs', '[view]'));
        }
        const meta = `${_esc(d.rows.length)} PDFs in biblio_articles/ with no Zotero entry — `
            + `read and unread, ranked by annotation count (suggested read cutoff: `
            + `${_esc(d.suggested_read_cutoff)}). `
            + (excl.length
                ? `Not listed: ${excl.join(' · ')}.`
                : 'Nothing else under biblio_articles/ is excluded.');
        return [meta, _pdfReportTable(d.rows)];
    }
    if (name === 'zotero_unread') {
        const meta = `${_esc(d.rows.length)} bib entries with a Zotero parent but no child note · ${_esc(d.skipped_no_zotero_match)} skipped (no Zotero title match).`;
        const head = '<thead><tr><th scope="col">Year</th><th scope="col">Key</th><th scope="col">Author</th><th scope="col">Title</th></tr></thead>';
        const rows = d.rows.map(r => `
            <tr>
                <td>${_esc(r.year || '')}</td>
                <td>${_esc(r.citation_key)}</td>
                <td>${_esc(r.author || '')}</td>
                <td class="cell-title" title="${_esc(r.title || '')}" aria-label="${_esc(r.title || '')}">${_esc((r.title || '').slice(0, 120))}</td>
            </tr>`).join('');
        return [meta, `<table class="audit-table">${head}${_tbodyHtml(rows)}</table>`];
    }
    if (name === 'zotero_no_pdf') {
        const meta = `${_esc(d.rows.length)} bib entries with no resolved PDF.`;
        const head = '<thead><tr><th scope="col">Year</th><th scope="col">Key</th><th scope="col">Author</th><th scope="col">Title</th><th scope="col">Zotero match</th></tr></thead>';
        const rows = d.rows.map(r => `
            <tr>
                <td>${_esc(r.year || '')}</td>
                <td>${_esc(r.citation_key)}</td>
                <td>${_esc(r.author || '')}</td>
                <td class="cell-title" title="${_esc(r.title || '')}" aria-label="${_esc(r.title || '')}">${_esc((r.title || '').slice(0, 120))}</td>
                <td>${r.has_zotero_match ? '✓' : ''}</td>
            </tr>`).join('');
        return [meta, `<table class="audit-table">${head}${_tbodyHtml(rows)}</table>`];
    }
    if (name === 'tag_hygiene') {
        const outOfVocab = d.rows.filter(r => !r.in_vocab).length;
        const review = d.rows.filter(r => r.variants.length > 1 || (!r.in_vocab && r.near_vocab));
        let meta = `${_esc(d.rows.length)} distinct tags across Finder / note YAML / Zotero / Zotero notes / bib keywords · `
            + `${_esc(outOfVocab)} outside the <code>_tags.md</code> vocabulary (${_esc(d.vocab_size)} tags) · `
            + `${_esc(review.length)} cluster(s) to examine (⚠ = several spellings, ≈ = near a vocabulary tag). [retire] removes a tag from every store (confirmed, journalled, undoable).`;
        // Library-health trend (Card Catalog gamification): the delta of
        // "clusters to examine" vs the last time this report was viewed —
        // visible improvement of the library over time, client-side only.
        try {
            const KEY = 'audit_hygiene_clusters';
            const prevRec = JSON.parse(localStorage.getItem(KEY) || 'null');
            const prev = prevRec ? prevRec.n : null;
            localStorage.setItem(KEY, JSON.stringify({ n: review.length }));
            if (prev !== null && prev !== review.length) {
                const better = review.length < prev;
                meta = `<div class="hygiene-trend"><span class="trend-n">${_esc(prev)}</span>`
                    + `<span class="${better ? 'trend-better' : 'trend-worse'}" aria-hidden="true">→</span>`
                    + `<span class="trend-n">${_esc(review.length)}</span>`
                    + `<span>cluster(s) to examine since last check${better ? ' — improving' : ''}</span></div>`
                    + meta;
            }
        } catch { /* decoration only */ }
        const head = '<thead><tr><th scope="col">Tag (spellings seen)</th><th scope="col">Finder</th><th scope="col">Note</th><th scope="col">Zotero</th><th scope="col">Z-notes</th><th scope="col">Bib</th><th scope="col">Total</th><th scope="col">Vocabulary</th></tr></thead>';
        const rows = d.rows.map(r => {
            const counts = ['finder', 'note', 'zotero', 'zotero_notes', 'bib']
                .map(s => `<td>${_esc(r.counts[s] || 0)}</td>`).join('');
            const vocabCell = r.in_vocab
                ? `✓ ${_esc(r.vocab_tag || '')}`
                : (r.near_vocab ? `≈ ${_esc(r.near_vocab)}` : '—');
            const retireBtns = r.variants.map(v =>
                `<button type="button" class="audit-link-btn" data-action="retire-tag" `
                + `data-tag="${_attr(v)}" data-invocab="${r.in_vocab ? '1' : ''}" `
                + `data-total="${_attr(r.total)}" `
                + `aria-label="Retire tag ${_attr(v)} everywhere">[retire ${_esc(v)}]</button>`
            ).join(' ');
            return `
            <tr>
                <td>${_esc(r.variants.join(', '))}${r.variants.length > 1 ? ' ⚠' : ''} ${retireBtns}</td>
                ${counts}
                <td><strong>${_esc(r.total)}</strong></td>
                <td>${vocabCell}</td>
            </tr>`;
        }).join('');
        const cooc = (d.cooccurrence && d.cooccurrence.length)
            ? '<h4 style="margin-top:16px;">Frequent tag pairs (papers sharing both)</h4>'
              + '<table class="audit-table"><thead><tr><th scope="col">Tag A</th><th scope="col">Tag B</th><th scope="col">Papers</th></tr></thead><tbody>'
              + d.cooccurrence.map(p => `<tr><td>${_esc(p.a)}</td><td>${_esc(p.b)}</td><td>${_esc(p.count)}</td></tr>`).join('')
              + '</tbody></table>'
            : '';
        return [meta, `<table class="audit-table">${head}${_tbodyHtml(rows)}</table>${cooc}`];
    }
    if (name === 'near_duplicates') {
        // Evidence-tier candidates (plan 2026-08-05/5): byte-DIFFERENT files
        // (or records) that look like the same paper, each with its stated
        // reason. Read-only advice + a persisted per-group dismiss with undo.
        const kindLabels = {
            page1_text: 'same first-page text',
            identifier: 'same DOI / arXiv id',
            bib_doi: 'same DOI in .bib',
            multi_pdf: 'one key, several PDFs',
            record_title: 'same title (confirmed)',
            title_only: 'same title (unconfirmed)',
            content_similarity: 'similar content',
        };
        const notCovered = [];
        if (d.no_text_count) notCovered.push(`${_esc(d.no_text_count)} PDF(s) had no readable first-page text (text tiers skipped for them)`);
        if (d.dismissed_count) notCovered.push(`${_esc(d.dismissed_count)} group(s) hidden by your “not a duplicate” decisions`);
        if (d.similarity_available === false) notCovered.push('similarity scoring unavailable (rapidfuzz not installed)');
        const meta = `${_esc(d.rows.length)} possible-duplicate groups — byte-different files the exact scan cannot see, each with its evidence. `
            + `<strong>[not a duplicate]</strong> hides a group from future scans (undoable).`
            + (notCovered.length ? ` ${notCovered.join(' · ')}.` : '');
        const head = '<thead><tr><th scope="col">Kind</th><th scope="col">Evidence</th><th scope="col">Members</th><th scope="col"></th></tr></thead>';
        const rows = d.rows.map(r => {
            const members = (r.paths || []).map(p => `<div>${_esc(p.rel)} <button type="button" class="audit-link-btn" data-action="reveal" data-abs="${_attr(p.abs)}">[reveal]</button></div>`).join('')
                + (r.citation_keys || []).map(k => `<div><code>${_esc(k)}</code></div>`).join('');
            // Paperless-ngx-calibrated confidence ladder for the % badge.
            const simLabel = (v) => (v >= 97 ? 'near-certain' : v >= 92 ? 'very likely' : v >= 88 ? 'likely' : 'weak');
            const sim = (r.similarity != null)
                ? ` · ${_esc(Math.round(r.similarity))}% (${simLabel(r.similarity)})` : '';
            return `
            <tr data-pair="${_attr(r.pair_id)}">
                <td>${_esc(kindLabels[r.kind] || r.kind)}${sim}</td>
                <td>${_esc(r.evidence)}</td>
                <td>${members}</td>
                <td><button type="button" class="audit-link-btn" data-action="dup-dismiss" data-pair="${_attr(r.pair_id)}" aria-label="Mark this group as not a duplicate">[not a duplicate]</button></td>
            </tr>`;
        }).join('');
        return [meta, `<table class="audit-table">${head}${_tbodyHtml(rows)}</table>`];
    }
    if (name === 'duplicates') {
        const meta = `${_esc(d.rows.length)} duplicate sets · ${_fmtBytes(d.total_wasted_bytes)} wasted. `
            + `★ marks the cleanest filename of each set — the copy a user would keep (advice only; nothing is deleted).`;
        const head = '<thead><tr><th scope="col">Hash</th><th scope="col">Size</th><th scope="col">Wasted</th><th scope="col">Files</th></tr></thead>';
        const rows = d.rows.map(r => `
            <tr>
                <td><code>${_esc(r.content_hash.slice(0, 12))}</code></td>
                <td>${_fmtBytes(r.size_bytes)}</td>
                <td>${_fmtBytes(r.wasted_bytes)}</td>
                <td>${r.paths.map(p => `<div>${p.rel === r.suggested_keep ? '<span title="Cleanest filename — the copy a user would keep" aria-label="Suggested keep">★</span> ' : ''}${_esc(p.rel)} <button type="button" class="audit-link-btn" data-action="reveal" data-abs="${_attr(p.abs)}">[reveal]</button></div>`).join('')}</td>
            </tr>`).join('');
        return [meta, `<table class="audit-table">${head}${_tbodyHtml(rows)}</table>`];
    }
    return ['', ''];
}

function _pdfReportTable(rows) {
    const head = '<thead><tr><th scope="col">Annot</th><th scope="col">Path</th><th scope="col"></th></tr></thead>';
    const body = rows.map(r => `
        <tr>
            <td>${r.annotations}${r.error ? ` <span class="audit-error-tag">(${_esc(r.error)})</span>` : ''}</td>
            <td>${_esc(r.pdf.rel)}</td>
            <td>
                <button type="button" class="audit-link-btn" data-action="open" data-abs="${_attr(r.pdf.abs)}">[open]</button>
                <button type="button" class="audit-link-btn" data-action="reveal" data-abs="${_attr(r.pdf.abs)}">[reveal]</button>
            </td>
        </tr>`).join('');
    return `<table class="audit-table">${head}${_tbodyHtml(body)}</table>`;
}

function _wireRowActions() {
    const body = document.getElementById('audit-report-body');
    if (!body) return;
    // The ambiguous-PDF view toggle lives in the meta line, not the body.
    const meta = document.getElementById('audit-report-meta');
    if (meta) {
        meta.querySelectorAll('[data-ambig-view]').forEach(btn => {
            btn.addEventListener('click', () => _ambigSetView(btn.getAttribute('data-ambig-view')));
        });
        // Cross-references between related reports (a meta line naming another
        // tab's contents should be able to take you there). Wired here, not in
        // the body pass below, because that pass early-returns for the triage
        // deck — and this block runs before it.
        meta.querySelectorAll('[data-goto-report]').forEach(btn => {
            btn.addEventListener('click', () => selectAuditReport(btn.getAttribute('data-goto-report')));
        });
    }
    // The triage deck owns its own wiring (it re-renders per card); mount it
    // once its shell is in the DOM, and skip the generic pass so its buttons
    // are not double-bound.
    if (document.getElementById('audit-ambig-deck')) { _ambigMount(); return; }
    body.querySelectorAll('.audit-link-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.preventDefault();
            const action = btn.getAttribute('data-action');
            if (action === 'apply-tags') { await _applyTagDrift(btn); return; }
            if (action === 'finder-apply-note') { _finderDriftApply(btn, 'note'); return; }
            if (action === 'finder-apply-finder') { _finderDriftApply(btn, 'finder'); return; }
            if (action === 'finder-vocab-add') { _finderVocabAdd(btn); return; }
            if (action === 'dup-dismiss') { _dupDismiss(btn); return; }
            if (action === 'dup-undismiss') { _dupUndismiss(btn); return; }
            // E9 (2026-07-22 audit): the TABLE view of this report wrote
            // mapping.json on one unconfirmed click — the exact defect the
            // deck view was built to fix ("assigned a citation key on one
            // unconfirmed click with no way back"). Same ceremony now: a
            // confirmInline strip on both write verbs. (Undo remains a
            // deck-view affordance; the table row shows its outcome.)
            if (action === 'pick-key') {
                const row0 = btn.closest('tr');
                const sel0 = row0 ? row0.querySelector('.audit-ambig-select') : null;
                const key0 = sel0 ? sel0.value : '';
                if (!key0) return;
                confirmInline(btn, {
                    message: `Assign ${key0} to this PDF?`,
                    confirmLabel: 'Assign',
                    onConfirm: () => _assignAmbiguous(btn),
                });
                return;
            }
            if (action === 'retire-tag') {
                const tag = btn.getAttribute('data-tag');
                const total = btn.getAttribute('data-total') || '?';
                // An in-vocab tag also has its _tags.md row edited
                // (include_vocab:true in _retireTag) — the confirm must state
                // that extra write, not just the per-store removals.
                const vocabBit = btn.getAttribute('data-invocab') === '1'
                    ? ', and delete its row entry from _tags.md (vocabulary)' : '';
                confirmInline(btn, {
                    // "item tags only": the hygiene row's cross-store total also
                    // counts Zotero child-note tags, .bib keywords and PDFs with
                    // no citation key — stores retire does NOT touch. Say so, or
                    // the count over-promises the reach.
                    message: `Remove tag "${tag}" from every note / PDF / Zotero item that carries it (~${total} occurrences in the last scan; Zotero child-note tags and .bib keywords are not touched)${vocabBit}? Journalled — undoable in one click after.`,
                    confirmLabel: 'Retire everywhere',
                    onConfirm: () => _retireTag(btn, tag),
                });
                return;
            }
            if (action === 'no-match') {
                confirmInline(btn, {
                    message: 'Mark this PDF as not in the bibliography?',
                    confirmLabel: 'Mark',
                    onConfirm: () => _markNoMatch(btn),
                });
                return;
            }
            const abs = btn.getAttribute('data-abs');
            if (!abs) return;
            const payload = action === 'reveal' ? { path: abs } : { open: abs };
            try {
                const r = await secureFetch('/api/audit/reveal', {
                    method: 'POST',
                    body: JSON.stringify(payload),
                });
                if (!r.ok) {
                    const d = await r.json().catch(() => ({}));
                    _showError(d.error || `Reveal failed (${r.status})`);
                }
            } catch (err) {
                _showError(String(err));
            }
        });
    });
}

// --- Axis-ii write/curation actions ---------------------------------------

/** "Not a duplicate": confirmInline ceremony, then record the dismissal and
 * mark the row in place (snapshot semantics — the row itself stays until the
 * next scan, showing its outcome + an undo button, the mapping-deck pattern). */
function _dupDismiss(btn) {
    confirmInline(btn, {
        message: 'Mark this group as not a duplicate? It will be hidden from future scans (undoable).',
        confirmLabel: 'Not a duplicate',
        onConfirm: () => _dupDecision(btn, { dismiss: true }, 'dismissed — hidden from future scans'),
    });
}

/** Undo of a dismissal — same ceremony as every other undo in the app (the
 * finder-drift and retire undos are confirmInline-gated; this one was the
 * lone unconfirmed write verb in the report). */
function _dupUndismiss(btn) {
    confirmInline(btn, {
        message: 'Restore this group? It will reappear on the next scan.',
        confirmLabel: 'Revert',
        onConfirm: () => _dupDecision(btn, { clear: true }, 'restored — will reappear on the next scan'),
    });
}

async function _dupDecision(btn, verb, doneText) {
    const pairId = btn.getAttribute('data-pair');
    if (!pairId) return;
    btn.disabled = true;
    try {
        const r = await secureFetch('/api/audit/dup-decision', {
            method: 'POST',
            body: JSON.stringify({ pair_id: pairId, ...verb }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { _showError(d.error || `Decision failed (${r.status})`); btn.disabled = false; return; }
        const row = btn.closest('tr');
        if (row) {
            const cell = btn.closest('td');
            if (cell) {
                cell.textContent = '';
                const status = document.createElement('span');
                status.textContent = `✓ ${doneText} `;
                cell.appendChild(status);
                if (verb.dismiss) {
                    // Peek-not-pop undo: the decision is server-persisted, so a
                    // failed undo leaves this button retryable.
                    const undo = document.createElement('button');
                    undo.type = 'button';
                    undo.className = 'audit-link-btn';
                    undo.setAttribute('data-action', 'dup-undismiss');
                    undo.setAttribute('data-pair', pairId);
                    undo.textContent = '[undo]';
                    undo.addEventListener('click', () => _dupUndismiss(undo));
                    cell.appendChild(undo);
                }
            }
            row.style.opacity = verb.dismiss ? '0.55' : '';
        }
    } catch (err) {
        _showError(String(err));
        btn.disabled = false;
    }
}

function _applyTagDrift(btn) {
    const key = btn.getAttribute('data-key');
    // Tags ride a JSON-encoded data attribute (entity-escaped by _attr at
    // render time; getAttribute un-escapes) because tag strings can contain
    // commas/colons that a naive join/split delimiter would corrupt.
    let tags = [];
    try { tags = JSON.parse(btn.getAttribute('data-tags') || '[]'); } catch { tags = []; }
    if (!key || !tags.length) return;
    // Track 6e ceremony: this button overwrites a user vault file, so it MUST
    // route through confirmInline (Cancel-first inline strip) — a bare native
    // confirm dialog is banned by tests/js/confirmCeremony.test.js's ratchet.
    confirmInline(btn, {
        message: `Write ${tags.length} missing tag(s) into ${key}'s note? (${tags.join(', ')}) — a .bak of the original is kept.`,
        confirmLabel: 'Write tags',
        onConfirm: async () => {
            btn.disabled = true;
            try {
                const r = await secureFetch('/api/audit/apply-tag-drift', {
                    method: 'POST',
                    body: JSON.stringify({ citation_key: key, tags, confirm: true }),
                });
                const d = await r.json().catch(() => ({}));
                if (!r.ok) { _showError(d.error || `Tag write failed (${r.status})`); btn.disabled = false; return; }
                btn.textContent = `✓ added ${(d.added || []).length}`;
                // Update the row in place from the server's post-write drift
                // (still_missing) — no rescan; the cached inventory stays
                // authoritative-stale until the user runs the next scan.
                const row = btn.closest('tr');
                if (row) {
                    const missingCell = row.querySelector('td:nth-child(6) strong');
                    if (missingCell) missingCell.textContent = (d.still_missing || []).join(', ');
                    row.style.opacity = '0.55';
                }
            } catch (err) {
                _showError(String(err));
                btn.disabled = false;
            }
        },
    });
}

/** Finder-drift fix (2026-08-06): write one direction's gap through the
 * tagging writers. `dir` is the WRITE target — 'note' applies the finder-only
 * tags to the note's frontmatter, 'finder' applies the note-only tags to the
 * PDF Finder tags. Tags in `data-tags` are exact _tags.md ids (the report's
 * vocab_ids mapping), so the writers' vocabulary gate accepts them. Track 6e
 * ceremony: both verbs overwrite user data → confirmInline, Cancel-first. */
function _finderDriftApply(btn, dir) {
    const key = btn.getAttribute('data-key');
    let tags = [];
    try { tags = JSON.parse(btn.getAttribute('data-tags') || '[]'); } catch { tags = []; }
    if (!key || !tags.length) return;
    const endpoint = dir === 'note'
        ? '/api/tagging/apply-note-tags' : '/api/tagging/apply-finder-tags';
    const target = dir === 'note'
        ? `${btn.getAttribute('data-note') || key + "'s note"} (frontmatter)`
        : `the Finder tags of all ${btn.getAttribute('data-pdfs') || '?'} biblio PDF(s) of ${key}`;
    confirmInline(btn, {
        message: `Write ${tags.length} tag(s) (${tags.join(', ')}) into ${target}? Journalled — undoable.`,
        confirmLabel: dir === 'note' ? 'Write to note' : 'Write to Finder',
        onConfirm: async () => {
            btn.disabled = true;
            try {
                const r = await secureFetch(endpoint, {
                    method: 'POST',
                    body: JSON.stringify({ citation_key: key, tags, confirm: true }),
                });
                const d = await r.json().catch(() => ({}));
                if (!r.ok) { _showError(d.error || `Tag write failed (${r.status})`); btn.disabled = false; return; }
                _finderDriftMarkApplied(btn, dir, d);
            } catch (err) {
                _showError(String(err));
                btn.disabled = false;
            }
        },
    });
}

/** In-place outcome (snapshot semantics — the full truth returns at the next
 * scan): strike the applied surfaces in the direction's cell, swap the button
 * for a ✓ + per-op [undo] (the tagging journal makes the write reversible —
 * strictly better than apply-tag-drift's .bak-only recovery). */
function _finderDriftMarkApplied(btn, dir, d) {
    const row = btn.closest('tr');
    const added = new Set(d.added || []);
    if (row) {
        row.querySelectorAll(`.fdrift-surf[data-dir="${dir}"]`).forEach(sp => {
            if (added.has(sp.getAttribute('data-vid'))) sp.style.textDecoration = 'line-through';
        });
    }
    const cell = btn.closest('td');
    btn.remove();
    if (!cell) return;
    const status = document.createElement('span');
    status.textContent = `✓ ${dir}: +${(d.added || []).length} `;
    cell.appendChild(status);
    if (d.op_id) cell.appendChild(_finderDriftUndoBtn([d.op_id], status));
}

/** Per-row undo over /api/tagging/restore. `opIds` revert in order (apply
 * first, then a chained vocab_add, so _tags.md is never left referencing a
 * write that came back). Peek-not-pop: a failed restore leaves the button
 * enabled and retryable. Restore rewrites user data → confirmInline. */
function _finderDriftUndoBtn(opIds, statusEl) {
    const undo = document.createElement('button');
    undo.type = 'button';
    undo.className = 'audit-link-btn';
    undo.textContent = '[undo]';
    undo.addEventListener('click', () => {
        confirmInline(undo, {
            message: 'Revert this tag write (restore the previous state)?',
            confirmLabel: 'Revert',
            onConfirm: async () => {
                undo.disabled = true;
                try {
                    for (const opId of opIds) {
                        const r = await secureFetch('/api/tagging/restore', {
                            method: 'POST',
                            body: JSON.stringify({ op_id: opId, confirm: true }),
                        });
                        const d = await r.json().catch(() => ({}));
                        if (!r.ok) { _showError(d.error || `Restore failed (${r.status})`); undo.disabled = false; return; }
                    }
                    statusEl.textContent = '↺ reverted ';
                    undo.remove();
                } catch (err) {
                    _showError(String(err));
                    undo.disabled = false;
                }
            },
        });
    });
    return undo;
}

/** Out-of-vocab drift surface: add the tag to _tags.md (journalled, kind
 * vocab_add), then chain the single-tag apply for the direction. One confirm
 * covers the pair; both ops ride the same [undo]. A multi-word surface (a
 * Finder tag like "sleep quality") is slugified to a single-token id (spaces
 * → "-") because _tags.md ids must be single tokens (the parser skips
 * space-ids); the fingerprint folds separators, so the hyphenated id still
 * closes the drift row at the next scan — the confirm names the exact id. */
function _finderVocabAdd(btn) {
    const key = btn.getAttribute('data-key');
    const surface = btn.getAttribute('data-tag');
    const dir = btn.getAttribute('data-dir');
    if (!key || !surface || !dir) return;
    const tag = surface.trim().replace(/\s+/g, '-');
    const fromBit = tag !== surface.trim() ? ` (from "${surface}")` : '';
    const targetBit = dir === 'note' ? "the note's frontmatter" : 'the PDF Finder tags';
    confirmInline(btn, {
        message: `Add "#${tag}"${fromBit} to _tags.md (controlled vocabulary), then write it into ${targetBit}? Both writes are journalled — undoable.`,
        confirmLabel: 'Add & apply',
        onConfirm: async () => {
            btn.disabled = true;
            try {
                const rv = await secureFetch('/api/tagging/add-vocab-tag', {
                    method: 'POST',
                    body: JSON.stringify({ tag, confirm: true }),
                });
                const dv = await rv.json().catch(() => ({}));
                if (!rv.ok) { _showError(dv.error || `Vocabulary edit failed (${rv.status})`); btn.disabled = false; return; }
                const endpoint = dir === 'note'
                    ? '/api/tagging/apply-note-tags' : '/api/tagging/apply-finder-tags';
                const ra = await secureFetch(endpoint, {
                    method: 'POST',
                    body: JSON.stringify({ citation_key: key, tags: [dv.tag || tag], confirm: true }),
                });
                const da = await ra.json().catch(() => ({}));
                if (!ra.ok) {
                    _showError((da.error || `Tag write failed (${ra.status})`)
                        + ' — the vocabulary entry was still added (undo it from the Tagging manifest if unwanted).');
                    btn.disabled = false;
                    return;
                }
                const cell = btn.closest('td');
                // Revert order: the apply first, then the vocab_add.
                const opIds = [da.op_id, dv.op_id].filter(Boolean);
                btn.remove();
                if (cell) {
                    const status = document.createElement('span');
                    status.textContent = ` ✓ #${tag} → ${dir} `;
                    cell.appendChild(status);
                    if (opIds.length) cell.appendChild(_finderDriftUndoBtn(opIds, status));
                }
            } catch (err) {
                _showError(String(err));
                btn.disabled = false;
            }
        },
    });
}

async function _assignAmbiguous(btn) {
    const pdf = btn.getAttribute('data-pdf');
    const row = btn.closest('tr');
    const sel = row ? row.querySelector('.audit-ambig-select') : null;
    const key = sel ? sel.value : '';
    if (!pdf || !key) return;
    btn.disabled = true;
    try {
        const r = await secureFetch('/api/audit/mapping', {
            method: 'POST',
            body: JSON.stringify({ pdf, citation_key: key }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { _showError(d.error || `Mapping failed (${r.status})`); btn.disabled = false; return; }
        btn.textContent = `✓ ${key}`;
    } catch (err) {
        _showError(String(err));
        btn.disabled = false;
    }
}

async function _markNoMatch(btn) {
    const pdf = btn.getAttribute('data-pdf');
    if (!pdf) return;
    btn.disabled = true;
    try {
        const r = await secureFetch('/api/audit/mapping', {
            method: 'POST',
            body: JSON.stringify({ pdf, no_match: true }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { _showError(d.error || `Mapping failed (${r.status})`); btn.disabled = false; return; }
        btn.textContent = '✓ not in bib';
    } catch (err) {
        _showError(String(err));
        btn.disabled = false;
    }
}

function _fmtBytes(n) {
    if (!n || n < 1024) return `${n || 0} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let v = n / 1024;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
        v /= 1024;
        i += 1;
    }
    return `${v.toFixed(1)} ${units[i]}`;
}

/** Wrap pre-built row HTML in a tbody, substituting a visible empty-state row
 * when there are no rows — a bare header row over nothing read as a broken
 * report, and the tagging tables already showed a message (2026-08-09). */
function _tbodyHtml(rowsHtml) {
    if (rowsHtml) return `<tbody>${rowsHtml}</tbody>`;
    return '<tbody><tr><td colspan="99" class="muted audit-empty-row">'
        + 'No rows in this report for the last scan.</td></tr></tbody>';
}

function _esc(s) {
    // Item 3.8 (carried 07-02 4.3): single quotes escaped too — an attribute
    // interpolation inside single-quoted HTML could otherwise break out.
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function _attr(s) {
    return _esc(s);
}

/**
 * An inline "jump to that report" control for a meta line that mentions
 * another tab's contents. Rendered as a real <button> (not an <a href="#">)
 * so it is keyboard-operable and never navigates the PyWebView window; the
 * click is bound by `_wireRowActions` via the `data-goto-report` hook, since
 * meta HTML is injected as a string and must carry no inline handler.
 *
 * `report` is a trusted literal from the call site, `label` is trusted UI
 * text; both still go through the escapers so this stays safe if either ever
 * becomes data-driven.
 */
function _gotoReportBtn(report, label) {
    return `<button type="button" class="audit-link-btn" data-goto-report="${_attr(report)}">`
        + `${_esc(label)}</button>`;
}
