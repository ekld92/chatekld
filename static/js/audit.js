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
import { mountPendingReview } from './pendingReview.js';
import { PreviewResource } from './pdfPreview.js';
import { mountPdfManagement } from './pdfManagement.js';
import { mountLibraryRun } from './libraryRun.js';
let _managementTeardown = () => {};
let _pendingTeardown = () => {};
import { setStatusA11y, closeModal, taskBegin, taskEnd, makeLatestGate, confirmInline, announceStatus, announceError, showTaskError, clearTaskError, installTableControls, foldText, renderSideBySideDiff, helpTip, statusLine, dailyCounter, trendSeries, sparkline, milestoneOnce } from './ui.js';

const _STATUS_POLL_MS = 1500;
let _statusPollTimer = null;
let _currentReport = 'library_update';
let _lastSummary = null;
let _scanMessage = '';
let _scanStartedAt = null;

const _REPORT_LABELS = {
    library_documents: 'Documents',
    library_storage: 'Storage control',
    inventory: 'Inventory',
    // Both drift reports NAME THEIR TWO SIDES (2026-08-13). "Tag Drift" and
    // "Finder Tag Drift" did not say what was compared with what, and the
    // ambiguity was live: this one compares the ZOTERO side (child-note tags,
    // plus the item's own when the Audit-Settings toggle is on) against the
    // Obsidian reference note — but "Zotero note" reads just as naturally as
    // the Obsidian note named after a citation key, which is the other thing
    // the app calls a Zotero note.
    note_tag_drift: 'Zotero vs Note Tags',
    // Not a view over the per-citation-key records like its neighbours: it
    // compares each Zotero reading note against the Obsidian note exported
    // from it, which is the one relationship no other report looks at.
    note_sync: 'Zotero Notes Sync',
    // read_unzoterod is the FULL set of un-Zotero'd PDFs; unread_unzoterod is a
    // filtered subset of it (annotations < threshold) — the old pair ("Read PDFs
    // Missing Zotero" / "Unread PDFs") read as a partition and hid the full list.
    // These labels are ALSO the group headings in the cross-report search panel,
    // where there is no adjacent tab to imply the containment, so the subset
    // spells its parent out rather than reusing the tablist's "↳" affordance
    // (which lives in index.html as an aria-hidden span — a bare arrow glyph is
    // noise to a screen reader and meaningless in a search result heading).
    read_unzoterod: 'Unmapped PDFs',
    unread_unzoterod: 'Unmapped PDFs · Unread Only',
    zotero_unread: 'Zotero Queue',
    zotero_no_pdf: 'Bib Entries Without PDFs',
    finder_tag_drift: 'Finder vs Note Tags',
    tag_harmony: 'Note vs Finder vs Zotero Tags',
    tag_hygiene: 'Tag Spellings & Vocabulary',
    ambiguous_pdfs: 'Ambiguous PDFs',
    skipped_pdfs: 'Skipped PDFs',
    duplicates: 'Duplicate PDFs',
    near_duplicates: 'Possible Duplicates',
    orphan_notes: 'Notes Without a Record',
    orphan_zotero_items: 'Zotero Items Without a Record',
    // Not "Attachments": the tab is about what ZOTERO holds a file for, which
    // is a different set from the vault's own Z_attachments tree.
    zotero_attachments: 'Zotero Attachments',
    // 2026-09-06 harmonization reports (read-only views; the verbs come in
    // Phase 2 of docs/plan_2026-09-06_library_harmonization.md).
    multi_pdf: 'Entries With Several PDFs',
    not_in_zotero: 'PDF workspace',
    // Phase 2 Option A (2026-09-10): the identification workspace used to
    // silently replace Not in Zotero whenever review_scope rode the payload.
    // Its own tab keeps the curated queue as a queue, and "confirm" as a
    // review verb that never competes with "pending".
    pdf_review: 'PDF identification review',
    dead_curation: 'Dead Curation',
};

/** Human labels for the bridge's per-PDF match source (Inventory, the pair
 *  drawer, Entries With Several PDFs). Hoisted so a new source cannot print
 *  as a raw id on one surface and a sentence on another. */
const _PROVENANCE_LABELS = {
    zotero_link: 'linked in Zotero',
    manual: 'hand-curated',
    manual_hash: 'hand-curated (re-linked by hash)',
    accession: 'accession',
    accession_hash: 'accession (re-linked by hash)',
    fm_pointer: 'note frontmatter pointer',
    fm_authoryear: 'note frontmatter author+year',
    wikilink: 'note wikilink',
    doi: 'DOI in PDF metadata',
    doi_text: 'DOI printed on page 1',
    authoryear: 'filename author+year only',
};

function _provenanceLabel(source) {
    return _PROVENANCE_LABELS[source] || source || 'unknown';
}

/**
 * Initialise the Audit tab: load + display the audit config and the reused vault
 * path, then read status. If a prior in-memory scan left results, render them
 * WITHOUT polling — the scan itself only ever runs on an explicit Run Scan.
 */
export async function initAuditTab() {
    _wireSearch();
    _wireLibraryUpdateButton();
    _wireAuditSettingsModal();
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
        // A scan may have finished outside this page's poll loop (CLI/MCP);
        // drop summary state keyed to the previous scan before rendering.
        _noteScanIdentity(status);
        _renderStatusBanner(status);
        if (status.has_results) {
            /* PERF (2026-09-02): a previous run's report is rendered when the
             * user opens the Audit tab, not during launch.
             * Before: initAuditTab ran _loadReport at boot, so a cached scan
             *   made app.js's init sequence fetch and render the full
             *   inventory (3.71 MB, ~6 000 rows) BEFORE the refactor, deck,
             *   docgen and maintenance steps ran — on the vault tab, which is
             *   the one actually on screen.
             * Change: the load is armed and runs on the first
             *   'chatekld:tabshown' for the audit tab (and immediately when
             *   that tab is already active, e.g. a rescan finishing while the
             *   user is looking at it).
             * Safe because: nothing outside this tab reads the report body,
             *   the scan is NOT started or polled either way (the manual-only
             *   invariant is untouched), and every later path into the tab
             *   goes through selectAuditReport, which loads unconditionally.
             * The dashboard tiles still refresh here, through the summary
             * endpoint that exists for exactly that (/api/audit/summary,
             * cached on scan identity) — before this they were a by-product
             * of the inventory payload, which is the 3.71 MB fetch being
             * deferred.
             * Pinned by: tests/js/auditDeferredLoad.test.js. */
            await _ensureSummary();
        }
        _armDeferredReportLoad();
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
        el.textContent = 'Set the Obsidian vault path on the Ask tab first.';
    }
}

const _AUDIT_SETTINGS_FIELDS = [
    ['audit-attachments-subdir', 'audit_attachments_subdir'],
    ['audit-biblio-articles-subdir', 'audit_biblio_articles_subdir'],
    ['audit-zotero-notes-subdir', 'audit_zotero_notes_subdir'],
    ['audit-master-bib-path', 'audit_master_bib_path'],
    ['audit-zotero-sqlite', 'audit_zotero_sqlite'],
    ['audit-zotero-storage', 'audit_zotero_storage'],
    ['audit-biblio-skip-prefix', 'audit_biblio_skip_prefix'],
    ['audit-annotations-read-threshold', 'audit_annotations_read_threshold'],
    ['audit-near-dup-ratio', 'audit_near_dup_ratio'],
    ['audit-no-pdf-types', 'audit_no_pdf_types'],
];
let _auditSettingsDirty = false;
let _auditCfg = null;

function _populateAuditSettingsForm(cfg) {
    _auditCfg = cfg || {};
    for (const [domId, cfgKey] of _AUDIT_SETTINGS_FIELDS) {
        const el = document.getElementById(domId);
        if (!el) continue;
        const v = _auditCfg[cfgKey];
        el.value = v == null ? '' : String(v);
    }
    const parentTags = document.getElementById('audit-drift-include-parent-tags');
    if (parentTags) parentTags.checked = !!_auditCfg.audit_drift_include_parent_tags;
    _auditSettingsDirty = false;
    const status = document.getElementById('audit-settings-status');
    if (status) setStatusA11y(status, '', false);
}

function _markAuditSettingsDirty() {
    _auditSettingsDirty = true;
    const status = document.getElementById('audit-settings-status');
    if (status) setStatusA11y(status, 'Unsaved changes — click Save to apply.', false);
}

export async function pickAuditSettingsPath(inputId, kind = 'folder') {
    const input = document.getElementById(inputId);
    const status = document.getElementById('audit-settings-status');
    if (!input) return;
    const relative = ['audit-attachments-subdir', 'audit-biblio-articles-subdir', 'audit-zotero-notes-subdir', 'audit-master-bib-path'].includes(inputId);
    const endpoint = kind === 'file' ? '/api/config/native-pick-file' : '/api/config/native-pick-folder';
    try {
        const resp = await secureFetch(endpoint, { method: 'POST', body: JSON.stringify({relative_to_vault: relative, under_attachments: inputId === 'audit-biblio-articles-subdir', ...(inputId === 'audit-master-bib-path' ? {kind: 'bib'} : {})}) });
        const data = await resp.json().catch(() => null);
        if (!resp.ok) {
            const msg = (data && data.error) || `Could not open picker (HTTP ${resp.status}).`;
            if (status) setStatusA11y(status, msg, true);
            return;
        }
        if (data && data.path) {
            input.value = relative ? data.relative_path : data.path;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            if (status) setStatusA11y(status, `Selected: ${data.path}. Unsaved changes — click Save.`, false);
        }
    } catch (e) {
        logError('Audit settings picker failed', e);
        if (status) setStatusA11y(status, 'Could not open picker.', true);
    }
}

function _wireAuditSettingsModal() {
    const ids = [
        ..._AUDIT_SETTINGS_FIELDS.map(([id]) => id),
        'audit-drift-include-parent-tags',
    ];
    for (const id of ids) {
        const el = document.getElementById(id);
        if (!el || el.dataset.dirtyWired === '1') continue;
        el.dataset.dirtyWired = '1';
        el.addEventListener('input', _markAuditSettingsDirty);
        el.addEventListener('change', _markAuditSettingsDirty);
    }
    for (const id of ['audit-attachments-subdir', 'audit-biblio-articles-subdir', 'audit-zotero-notes-subdir', 'audit-master-bib-path']) {
        const button = document.getElementById(id + '-browse');
        if (button && !button.dataset.wired) {
            button.dataset.wired = '1';
            button.addEventListener('click', () => pickAuditSettingsPath(id, id === 'audit-master-bib-path' ? 'file' : 'folder'));
        }
    }
    const sqliteBtn = document.getElementById('audit-zotero-sqlite-browse');
    if (sqliteBtn && sqliteBtn.dataset.wired !== '1') {
        sqliteBtn.dataset.wired = '1';
        sqliteBtn.addEventListener('click', () => { pickAuditSettingsPath('audit-zotero-sqlite', 'file'); });
    }
    const storageBtn = document.getElementById('audit-zotero-storage-browse');
    if (storageBtn && storageBtn.dataset.wired !== '1') {
        storageBtn.dataset.wired = '1';
        storageBtn.addEventListener('click', () => { pickAuditSettingsPath('audit-zotero-storage', 'folder'); });
    }
    const overlay = document.getElementById('audit-settings-modal');
    if (overlay && overlay.dataset.refreshWired !== '1' && typeof MutationObserver === 'function') {
        overlay.dataset.refreshWired = '1';
        let wasOpen = overlay.classList.contains('open');
        new MutationObserver(() => {
            const isOpen = overlay.classList.contains('open');
            if (isOpen && !wasOpen) {
                if (_auditCfg) _populateAuditSettingsForm(_auditCfg);
                fetchAuditConfig().then((cfg) => _populateAuditSettingsForm(cfg)).catch(() => {
                    if (_auditCfg) _populateAuditSettingsForm(_auditCfg);
                });
            }
            wasOpen = isOpen;
        }).observe(overlay, { attributes: true, attributeFilter: ['class'] });
    }
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
        audit_no_pdf_types: _readInput('audit-no-pdf-types'),
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
        _auditCfg = { ...(_auditCfg || {}), ...body };
        _auditSettingsDirty = false;
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
        // Re-arm as well as re-show: cancelAuditScan disables the button and
        // only the failure paths re-enabled it, so after one successful cancel
        // every later scan ran with a dead, greyed Cancel (2026-08-18).
        if (cancelBtn) { cancelBtn.style.display = ''; cancelBtn.disabled = false; }
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
        // Phrased like vault.js's poll failure, not raw exception text —
        // "TypeError: Failed to fetch" reads as a crash; a transient poll
        // failure is retried on the next tick anyway (2026-08-18).
        logError('audit status poll', e);
        _showError('Could not reach the app to check scan status — retrying…');
        return;
    }
    if (!isCurrent()) return;
    _noteScanIdentity(status);
    _renderStatusBanner(status);
    if (status.state !== 'scanning') {
        _stopStatusPolling();
        const btn = document.getElementById('audit-run-btn');
        const cancelBtn = document.getElementById('audit-cancel-btn');
        if (btn) btn.disabled = false;
        if (cancelBtn) cancelBtn.style.display = 'none';
        taskEnd('audit-scan');
        if (status.state === 'done' || (status.state === 'cancelled' && status.has_results)) {
            // Seed the dashboard BEFORE the milestone and the report: the tiles
            // used to be a by-product of the Inventory tab (blank until it
            // loaded), and the milestone read a record count off the status
            // payload, which has never carried one.
            const summary = await _loadSummary();
            if (!isCurrent()) return;
            if (status.state === 'done') _maybeFirstScanMilestone(summary);
            await _loadReport(_currentReport);
        }
    }
}

/* ── Library health over the last ten scans ──────────────────────────────
 *
 * The dashboard says where the library stands TODAY; nothing said whether it
 * was getting better. A user who has spent a month closing orphans had no way
 * to see the month.
 *
 * "Needs attention" is deliberately a short, named list of the things a
 * cleanup actually CLOSES, not every warned tile on the dashboard:
 *
 *   • "Tagged nowhere" is a tagging-campaign metric in the hundreds. It would
 *     dominate the line and swamp every reconciliation fix.
 *   • "Entry, no local PDF" is ~2 000 on the maintainer's library and the
 *     `pdf_wanted` decisions that retire those rows are not in this payload,
 *     so it would never move however much work was done.
 *
 * Each of the five below goes to zero when the library is clean, which is the
 * property that makes a trend line mean anything. The composition is named on
 * screen, because an unexplained number going down is not information.
 */
const _HEALTH_FIELDS = [
    ['pdfs_unmapped', 'unmapped PDFs'],
    ['pdfs_ambiguous', 'ambiguous PDFs'],
    ['zotero_items_without_record', 'Zotero items with no record'],
    ['notes_without_record', 'notes with no record'],
    ['mapping_orphans', 'dead mapping entries'],
];
const _healthTrend = trendSeries('audit_health_v1', 10);

/** The "needs attention" total for one summary payload, or null when the
 *  payload carries none of the fields (an older server — report nothing
 *  rather than a confident zero). */
function _healthScore(s) {
    if (!s) return null;
    let seen = 0, total = 0;
    for (const [k] of _HEALTH_FIELDS) {
        if (s[k] === undefined || s[k] === null) continue;
        seen++;
        total += Number(s[k]) || 0;
    }
    return seen ? total : null;
}

/** Record this scan's score and paint the trend under the dashboard tiles.
 *  One sample per SCAN (keyed on finished_at), so re-opening the tab does not
 *  draw a flat line and call it a trend. */
function _paintHealthTrend(summary) {
    const host = document.getElementById('audit-summary');
    if (!host) return;
    host.querySelector('.audit-health')?.remove();
    const n = _healthScore(summary);
    if (n === null || !_lastScanFinishedAt) return;
    const series = _healthTrend.record(_lastScanFinishedAt, n);
    const nums = series.map((p) => p.n);
    const composition = _HEALTH_FIELDS.map(([, label]) => label).join(' + ');
    const wrap = document.createElement('div');
    wrap.className = 'audit-health';
    const chart = sparkline(nums, {
        label: `Needs attention over the last ${nums.length} scans: `
            + `${nums.join(', ')}. Now ${n}.`,
    });
    if (chart) wrap.appendChild(chart);
    const text = document.createElement('span');
    const strong = document.createElement('strong');
    strong.textContent = String(n);
    text.appendChild(strong);
    text.appendChild(document.createTextNode(' need attention'));
    if (nums.length > 1) {
        const first = nums[0];
        const delta = n - first;
        if (delta !== 0) {
            const d = document.createElement('span');
            d.className = delta < 0 ? 'trend-better' : 'trend-worse';
            // The WORD carries the direction; the colour is the second channel.
            d.textContent = ` — ${delta < 0 ? 'down' : 'up'} ${Math.abs(delta)} over ${nums.length} scans`;
            text.appendChild(d);
        }
    }
    wrap.appendChild(text);
    const note = document.createElement('span');
    note.className = 'audit-health-note';
    note.textContent = composition;
    wrap.appendChild(note);
    host.appendChild(wrap);
    _libraryMilestones(summary);
}

/** One-shot acknowledgements for states that are real achievements and that a
 *  tile reports as a bare "0" nobody would notice. Never repeated. */
function _libraryMilestones(s) {
    const banner = document.getElementById('audit-status-banner');
    if (!banner || !s) return;
    if (s.pdfs_unmapped === 0 && s.pdfs_ambiguous === 0) {
        milestoneOnce('audit_ms_zero_orphans', banner, {
            title: 'Every PDF is paired',
            detail: 'no unmapped and no ambiguous PDFs in this scan. Shown once.',
        });
    }
    if (s.mapping_orphans === 0 && s.notes_without_record === 0) {
        milestoneOnce('audit_ms_zero_dead_curation', banner, {
            title: 'No dead curation left',
            detail: 'every mapping entry and every reference note still points at a record. Shown once.',
        });
    }
}

// ── One-shot milestone (Card Catalog gamification, deliberately minimal) ──
// The FIRST completed scan gets a single acknowledgment strip; the flag makes
// it never reappear. No badges, no repetition — a fact, said once.
function _maybeFirstScanMilestone(summary) {
    const KEY = 'audit_first_scan_done';
    try {
        if (localStorage.getItem(KEY)) return;
    } catch { return; }
    // Burn the one-shot flag only once the count has actually rendered: on a
    // summary-fetch failure the countless fallback used to render AND spend
    // the flag, making the real count permanently unshowable — the precise
    // historical defect this strip's comment describes (2026-08-18). A run
    // with no count keeps the milestone for the next successful scan.
    const n = summary && summary.record_count;
    if (!n) return;
    const banner = document.getElementById('audit-status-banner');
    if (!banner) return;
    const strip = document.createElement('div');
    strip.className = 'milestone-strip';
    strip.setAttribute('role', 'status');
    const body = document.createElement('span');
    const strong = document.createElement('strong');
    strong.textContent = 'First full scan complete';
    body.appendChild(strong);
    body.appendChild(document.createTextNode(
        ` — ${n} records reconciled. Shown once.`));
    strip.appendChild(body);
    banner.insertAdjacentElement('afterend', strip);
    announceStatus('First full library scan complete.');
    try { localStorage.setItem(KEY, '1'); } catch { /* private mode: may repeat */ }
}

async function _getStatus() {
    const r = await secureFetch('/api/audit/status');
    if (!r.ok) throw new Error(`status ${r.status}`);
    return r.json();
}

/**
 * Fetch the headline counters and paint the dashboard above the tablist.
 *
 * Its own endpoint, not a slice of `/api/audit/inventory`: the tiles describe
 * the whole library, and requiring a thousands-row table fetch to see them is
 * what left them blank for any user who never opened the Inventory tab.
 * Never throws — a summary that fails to load must not take the report with it.
 *
 * @returns {Promise<object|null>} the summary, or null if unavailable.
 */
// Latest-wins for the summary fetch (the documented makeLatestGate
// discipline): a straddling fetch resolving after a scan-identity reset would
// otherwise repaint the PREVIOUS scan's tiles and pin them via the now-warm
// cache. _noteScanIdentity supersedes the gate on reset.
const _summaryGate = makeLatestGate();

async function _loadSummary() {
    const isCurrent = _summaryGate.enter();
    try {
        const r = await secureFetch('/api/audit/summary');
        if (!isCurrent()) return null;
        if (!r.ok) {
            // 404 = no scan yet — tiles are legitimately absent. Anything
            // else is a real failure and must be visible: silent-null read
            // as "no scan data" (2026-08-18).
            if (r.status !== 404) _summaryUnavailable();
            return null;
        }
        const d = await r.json();
        if (!isCurrent()) return null;
        _lastSummary = d.summary ? {...d.summary, pdf_reconciliation:d.pdf_reconciliation} : null;
        _paintPaperIndexAge(d.paper_index_built_at);
    } catch {
        if (isCurrent()) _summaryUnavailable();
        return null;
    }
    _paintSummary();
    const target=document.getElementById('audit-summary');
    if(target) {
        target.querySelector('.audit-reconciliation-status')?.remove();
        target.insertAdjacentHTML('beforeend',_pdfProjectionHtml(_lastSummary?.pdf_reconciliation));
    }
    return _lastSummary;
}

/** "Paper labels: index from 12 min ago" beside the Refresh-widget button
 *  (2026-09-23). The index is rewritten only when a scan completes, so a chat
 *  labels papers from the LAST scan — the `tagging batch` snapshot-age
 *  disclosure, for the same reason. `null` = no index yet. */
export function _paintPaperIndexAge(builtAt) {
    const el = document.getElementById('audit-paper-index-age');
    if (!el) return;
    const t = typeof builtAt === 'string' ? Date.parse(builtAt) : NaN;
    if (!Number.isFinite(t)) {
        el.textContent = 'Paper labels: no index yet — complete a scan to build it.';
        return;
    }
    const age = _accAge(t / 1000).trim() || 'just now';
    el.textContent = `Paper labels: index from the scan ${age}.`;
    el.title = `Built ${new Date(t).toLocaleString()} (UTC ${builtAt})`;
}

/** Visible dashboard-failure state — distinguishable from "no scan data". */
function _summaryUnavailable() {
    const box = document.getElementById('audit-summary');
    if (box) {
        box.style.display = '';
        box.innerHTML = '<p class="audit-search-empty">Dashboard summary '
            + 'unavailable (fetch failed) — the reports below are unaffected.</p>';
    }
}

/** Paint the cached summary, if there is one. */
function _paintSummary() {
    const box = document.getElementById('audit-summary');
    if (_lastSummary && box) {
        box.style.display = '';
        box.innerHTML = _summaryHtml(_lastSummary);
        _decorateDefs(box);
        _wireOverviewToggle(box);
        _paintHealthTrend(_lastSummary);
    }
}

/**
 * Paint the dashboard, fetching it first if this session has never loaded it.
 *
 * Every report branch goes through here, so the tiles no longer depend on the
 * user having opened the Inventory tab: that dependency is what left a
 * whole-library dashboard blank for anyone whose first click was any other tab.
 */
async function _ensureSummary() {
    if (_lastSummary) { _paintSummary(); return _lastSummary; }
    return _loadSummary();
}

function _renderStatusBanner(status) {
    const banner = document.getElementById('audit-status-banner');
    if (!banner) return;
    const fragments = [];
    if (status.started_at !== _scanStartedAt) {
        _scanStartedAt = status.started_at;
        _scanMessage = '';
    }
    if (status.state === 'scanning') {
        const elapsed = status.started_at ? Math.max(0, Math.floor(Date.now() / 1000 - status.started_at)) : null;
        fragments.push(['Scanning', elapsed == null ? '' : `${elapsed}s elapsed`].filter(Boolean).join(' · '));
    }
    else if (status.state === 'done') fragments.push('Scan complete.');
    else if (status.state === 'error') fragments.push(`Error: ${status.error || 'unknown'}`);
    else if (status.state === 'cancelled') fragments.push('Cancelled.');
    if (Array.isArray(status.messages) && status.messages.length) _scanMessage = status.messages[status.messages.length - 1];
    if (status.state === 'scanning' && _scanMessage) fragments.push(_scanMessage);
    // A finished or cancelled scan is news, not a warning — it used to wear
    // the yellow warning palette with a `banner-neutral` modifier bolted on to
    // say "ignore the colour". It is an OUTCOME, so it is a status line, and
    // the tone says which of the four it is rather than a modifier denying the
    // shape (2026-09-20 information-grammar pass).
    const tone = status.state === 'error' ? 'error'
        : status.state === 'cancelled' ? 'warn'
            : status.state === 'done' ? 'ok' : 'info';
    banner.classList.remove('warning-banner');
    if (fragments.length) {
        statusLine(banner, { tone, text: fragments.join(' · ') });
        banner.style.display = 'block';
    } else {
        statusLine(banner, { tone, text: '' });
        banner.style.display = 'none';
    }
    // While a scan runs — even one started elsewhere (CLI, another window) —
    // the "No scan yet" invitation and an enabled Run Scan contradicted the
    // "Scanning…" line right above them.
    if (status.state === 'scanning') {
        _hideEmptyState();
        const run = document.getElementById('audit-run-btn');
        const cancel = document.getElementById('audit-cancel-btn');
        if (run) run.disabled = true;
        if (cancel) cancel.style.display = '';
        if (!_statusPollTimer) _startStatusPolling();
    }
}

function _hideEmptyState() {
    const el = document.getElementById('audit-empty-state');
    if (el) el.style.display = 'none';
}

function _showError(msg) {
    const banner = document.getElementById('audit-status-banner');
    if (banner) {
        // An error keeps the alarm even right after an "ok"-toned
        // "Scan complete." (2026-09-19 review).
        banner.classList.remove('warning-banner');
        statusLine(banner, { tone: 'error', text: msg });
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
    const help = tabs && tabs.nextElementSibling;
    if (help && help.classList.contains('audit-report-help')) help.style.display = on ? '' : 'none';
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

// Bound the 202 "Preparing search index…" retry loop (2026-08-18): a build
// that wedges server-side otherwise had the client polling every 700 ms
// forever with no escalation. ~40 × 700 ms ≈ 30 s covers a slow legitimate
// build; past it the user gets an actionable message instead of a spinner.
const _SEARCH_BUILD_MAX_RETRIES = 40;

async function _runSearch(raw, buildRetries = 0) {
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
            if (buildRetries >= _SEARCH_BUILD_MAX_RETRIES) {
                results.innerHTML = '<p class="audit-search-empty">The search '
                    + 'index is taking unusually long to build. Press Enter to '
                    + 'retry the search; if this persists, re-run the scan.</p>';
                return;
            }
            results.innerHTML = `<p class="audit-search-empty">Preparing search index…</p>`;
            setTimeout(() => { if (isCurrent()) _runSearch(q, buildRetries + 1); }, 700);
            return;
        }
        if (!r.ok) {
            results.innerHTML = `<p class="audit-search-empty">${_esc(d.error || 'Search failed')}</p>`;
            return;
        }
        results.innerHTML = _pdfProjectionHtml(d.pdf_reconciliation) + _searchResultsHtml(d);
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
            const documentAttr = row.document_id ? ` data-document-id="${_attr(row.document_id)}"` : '';
            const documents = key || relAttr || documentAttr ? `<button type="button" class="audit-search-documents btn btn-outline"${key}${relAttr}${documentAttr}>Open in Documents</button>` : '';
            return `<li><button type="button" class="audit-search-hit"
                        data-report="${_attr(g.report)}"${key}${relAttr}${documentAttr}>
                        <span class="audit-search-label">${_esc(row.label)}</span>${detail}
                    </button>${documents}</li>`;
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
    results.querySelectorAll('.audit-search-documents').forEach(btn => {
        btn.addEventListener('click', async () => {
            _libraryDocumentState.filter = '';
            _libraryDocumentState.documentId = btn.getAttribute('data-document-id') || '';
            _libraryDocumentState.query = btn.getAttribute('data-rel') || btn.getAttribute('data-key') || '';
            await selectAuditReport('library_documents');
            document.querySelector('.library-documents-list button')?.focus();
        });
    });
    results.querySelectorAll('.audit-search-hit').forEach(btn => {
        btn.addEventListener('click', async () => {
            const report = btn.getAttribute('data-report');
            if (!report) return;
            if (report === 'library_documents') {
                _libraryDocumentState.documentId = btn.getAttribute('data-document-id') || '';
                _libraryDocumentState.query = ''; _libraryDocumentState.filter = '';
                await selectAuditReport(report);
                document.querySelector('.library-documents-list button')?.focus();
                return;
            }
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
    if (_invPager) _invPager.reveal(citationKey, pdfRel);
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
    // A live per-table filter may be hiding the very row the search hit names.
    // Scrolling to a hidden row is a silent no-op, so drop the filters first.
    if (rowEl.hidden) {
        _clearTableFilters();
    }
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

// The first report render is deferred to the first activation of the Audit
// tab; a pending arm is consumed exactly once.
let _pendingFirstLoad = false;

function _auditTabIsActive() {
    const panel = document.getElementById('audit-tab');
    return !!(panel && panel.classList.contains('active'));
}

function _armDeferredReportLoad() {
    if (_auditTabIsActive()) { selectAuditReport(_currentReport); return; }
    _pendingFirstLoad = true;
}

function _onTabShown(e) {
    if (!_pendingFirstLoad) return;
    if (!e || !e.detail || e.detail.tabId !== 'audit') return;
    _pendingFirstLoad = false;
    selectAuditReport(_currentReport);
}

// One hook per DOCUMENT, not per module evaluation: a re-imported audit.js
// (vi.resetModules in the JS suite, and any future dynamic re-import) would
// otherwise leave the previous instance listening with its own pending flag
// and load the report twice. The marker idiom the audit search bar uses.
if (document.__auditTabShownHook) {
    document.removeEventListener('chatekld:tabshown', document.__auditTabShownHook);
}
document.__auditTabShownHook = _onTabShown;
document.addEventListener('chatekld:tabshown', _onTabShown);

/** Switch the active report tab (updating ARIA selection state) and load it. */
const _libraryDocumentState = {filter: '', query: '', documentId: ''};

export async function selectAuditReport(name) {
    const requested = name;
    if (name.startsWith('library_documents:')) {
        _libraryDocumentState.filter = name.split(':')[1];
        _libraryDocumentState.query = '';
        _libraryDocumentState.documentId = '';
        name = 'library_documents';
    }
    // The search panel overlays the report body — switching tabs while it is
    // open would otherwise render the new report underneath an unrelated
    // result list. The query itself is KEPT (a "Show results" button brings it
    // back), so following a hit into its report costs nothing to undo.
    _hideSearchResults();
    _currentReport = name;
    document.getElementById('audit-tab')?.classList.toggle('audit-daily-active', name === 'library_update');
    // Exactly one tab is selected (2026-09-17 review, D1): a redirected tab
    // carries its full `library_documents:<filter>` name, so match the
    // requested name first and fall back to the first tab sharing its base.
    const tabs = [...document.querySelectorAll('.audit-report-tab')];
    const chosen = tabs.find(t => t.getAttribute('data-report') === requested)
        || tabs.find(t => t.getAttribute('data-report') === name)
        || tabs.find(t => t.id === 'audit-rtab-' + requested)
        || tabs.find(t => (t.getAttribute('data-report') || '').split(':')[0] === name)
        || null;
    tabs.forEach(t => {
        const selected = t === chosen;
        t.classList.toggle('active', selected);
        t.setAttribute('aria-selected', selected ? 'true' : 'false');
        t.setAttribute('tabindex', selected ? '0' : '-1');
    });
    // The "Library sections" buttons show which one is open (2026-09-19: they
    // had no active state, so after a click nothing said where you were).
    for (const btn of document.querySelectorAll('.audit-section-btn')) {
        btn.setAttribute('tabindex', '0');
        const on = btn.dataset.section === name;
        btn.classList.toggle('active', on);
        if (on) btn.setAttribute('aria-current', 'page'); else btn.removeAttribute('aria-current');
    }
    // Track 6c: keep the tabpanel's accessible name in sync with the active
    // tab (the static markup names the initial Inventory tab only).
    const panel = document.getElementById('audit-report-body');
    if (panel) {
        if (chosen && chosen.id) {
            panel.setAttribute('aria-labelledby', chosen.id); panel.removeAttribute('aria-label');
        } else {
            panel.removeAttribute('aria-labelledby'); panel.setAttribute('aria-label', name.replace('library_', 'Library '));
        }
    }
    await _loadReport(name);
}

/* The "Update my library" section button carries no inline handler (the CSP
 * migration direction: no new `on*=` attribute). Wire-once by a dataset
 * marker on the element itself, never a module flag (the search-bar lesson:
 * a flag stays true after the node is replaced). */
function _wireLibraryUpdateButton() {
    const btn = document.getElementById('audit-library-update-btn');
    if (!btn || btn.dataset.wired) return;
    btn.dataset.wired = '1';
    btn.addEventListener('click', () => selectAuditReport('library_update'));
}

// Item 3.3: rapid report-tab switches raced — the SLOWER response rendered
// last, leaving report A's table under report B's selected tab. Latest-wins:
// a superseded load abandons before touching the DOM.
const _reportLoadGate = makeLatestGate();

const _PDF_REPORTS = new Set(['duplicates', 'near_duplicates', 'multi_pdf', 'read_unzoterod',
    'unread_unzoterod', 'zotero_no_pdf', 'not_in_zotero', 'pdf_review', 'ambiguous_pdfs',
    'skipped_pdfs', 'dead_curation', 'zotero_attachments']);

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
    // …and the accession deck's (same lifetime rule, separate URL slot).
    auditAccTeardown();
    // …and the inventory pairings preview's.
    auditInvTeardown();
    _invPager = null;
    _invRecords.clear();
    _managementTeardown();
    _managementTeardown = () => {};
    _pendingTeardown();
    _pendingTeardown = () => {};
    /* PERF (2026-09-02): a report's cached payload does not outlive it.
     * Before: _acc.rows (one entry per orphan PDF) and _acc.report (the whole
     *   read_unzoterod payload) stayed in memory for the session after the
     *   user moved to another report, and the ambiguous payload added by the
     *   view-toggle fix would have done the same.
     * Safe because: each is re-populated by its own report's load — the
     *   accession body by _accessionAttach, the ambiguous payload by
     *   _renderReport — so dropping it when a DIFFERENT report takes the body
     *   costs nothing; the per-snapshot decided/undo/selection state is
     *   deliberately untouched (it is keyed to the scan token and must
     *   survive a trip to another tab).
     * Pinned by: tests/js/auditRowDelegate.test.js. */
    if (name !== 'ambiguous_pdfs') _ambigPayload = null;
    if (name !== 'read_unzoterod') { _acc.rows = new Map(); _acc.report = null; }
    _closeKeyPicker(false);
    body.innerHTML = _skeletonHtml(name);
    let accPayload = null;
    let pendingPayload = null;
    try {
        if (name === 'library_update') {
            // The maintenance run (library-workflow lot 6): a client-side
            // surface over /api/library/*, with no report payload to fetch.
            _hideEmptyState();
            if (meta) {
                meta.style.display = '';
                // Plain words (audit remediation 2026-09-23, U16): "app
                // state" and "the session" meant nothing to a reader.
                meta.textContent = 'Nothing in your library changes until you tick it and confirm. '
                    + 'Each run’s outcome is kept, so you can come back to it.';
            }
            body.innerHTML = '';
            mountLibraryRun(body);
            return;
        }
        if (name === 'library_maintenance') {
            _hideEmptyState();
            body.replaceChildren();
            if (meta) meta.textContent = 'Select checks below. Findings do not authorize changes.';
            if (typeof window.mountLibraryMaintenance === 'function') window.mountLibraryMaintenance(body);
            else body.textContent = 'Maintenance is unavailable in this build or its registry could not be loaded.';
            return;
        }
        if (name.startsWith('library_')) {
            const view = name.slice('library_'.length);
            const r = await secureFetch('/api/audit/library/' + encodeURIComponent(view));
            const d = await r.json();
            if (!isCurrent()) return;
            if (!r.ok) {
                _hideEmptyState();
                if (meta) { meta.style.display = 'none'; meta.textContent = ''; }
                body.innerHTML = '<p class="audit-search-empty" role="status">'
                    + _esc(d.error || 'Library observation unavailable')
                    + ' Opening this section does not start a scan or update the search index.</p>';
                if (view === 'checks' && typeof window.mountLibraryMaintenance === 'function') window.mountLibraryMaintenance(body);
                return;
            }
            _hideEmptyState();
            if (meta) { meta.style.display = ''; meta.textContent = d.freshness?.basis || 'Saved observations; actions revalidate.'; }
            body.innerHTML = '';
            _mountLibrary(body, view, d);
            return;
        } else if (name === 'inventory') {
            const r = await secureFetch('/api/audit/inventory');
            const d = await r.json();
            if (!isCurrent()) return;
            if (!r.ok) {
                _hideReport();
                _showError(d.error || 'Inventory unavailable');
                return;
            }
            _invPdfLinkStatus = {};
            _invPdfLinkLoaded = false;
            // The optional note-link overlay must not delay the inventory.
            const linksController = new AbortController();
            const linksTimer = setTimeout(() => linksController.abort(), 10000);
            secureFetch('/api/tagging/pdf-links-status', {signal: linksController.signal})
                .then(async response => {
                    if (!response.ok) return;
                    const ld = await response.json();
                    if (!isCurrent()) return;
                    _invPdfLinkLoaded = true;
                    for (const row of (ld.rows || [])) {
                        if (row.citation_key) _invPdfLinkStatus[row.citation_key] = row.state;
                    }
                }).catch(() => {}).finally(() => clearTimeout(linksTimer));
            if (!isCurrent()) return;
            _lastSummary = d.summary;
            _hideEmptyState();
            _setTabsVisible(tabs, true);
            if (summaryBox) {
                summaryBox.style.display = '';
                summaryBox.innerHTML = _summaryHtml(d.summary);
                _decorateDefs(summaryBox);
                _wireOverviewToggle(summaryBox);
                _paintHealthTrend(d.summary);
            }
            if (meta) {
                meta.style.display = '';
                meta.textContent = `${d.records.length} records (one per citation key).`;
            }
            body.innerHTML = _inventoryTable(d.records.length > 100 ? [] : d.records);
            if (d.records.length > 100) _mountPagedInventory(body, d.records);
        } else if (name === 'note_sync') {
            // Own endpoint (/api/notesync/status), not /api/audit/reports/*:
            // the write half lives outside the vendored audit tree so that
            // tree keeps its read-only posture and its single documented
            // vault-write exception. Same chrome, different source.
            _hideEmptyState();
            _setTabsVisible(tabs, true);
            await _ensureSummary();
            const r = await secureFetch('/api/notesync/status');
            const d = await r.json().catch(() => ({}));
            if (!isCurrent()) return;
            if (!r.ok) {
                if (meta) { meta.style.display = 'none'; meta.textContent = ''; }
                body.innerHTML = '<p class="audit-search-empty">'
                    + _esc(d.error || `Could not load (${r.status})`) + '</p>';
                return;
            }
            // The two-way state (Phase 3, 2026-09-04) reads Zotero FRESH and
            // is a second request by design: it can fail on its own (an
            // unreadable database is a 503) without taking the rest of the
            // tab with it, and the tab says so instead of showing nothing.
            try {
                const r2 = await secureFetch('/api/notesync/sync-status');
                const d2 = await r2.json().catch(() => ({}));
                if (!isCurrent()) return;
                d.sync = r2.ok ? d2 : { error: d2.error || `Could not load the two-way state (${r2.status})` };
            } catch (e) {
                logError('notesync sync-status failed: ' + String(e));
                d.sync = { error: String(e) };
            }
            try {
                const attempts = await secureFetch('/api/notesync/create-attempts');
                const payload = await attempts.json();
                d.attempts = attempts.ok ? payload : {error: payload.error || 'Retained attempts unavailable.'};
            } catch (e) { d.attempts = {error: String(e)}; }
            try {
                const kc = await secureFetch('/api/notesync/key-changes');
                const payload = await kc.json().catch(() => ({}));
                d.key_changes = kc.ok ? payload : { error: payload.error || 'Key-change list unavailable.' };
            } catch (e) { d.key_changes = { error: String(e) }; }
            if (!isCurrent()) return;
            const [m2, b2] = _renderNoteSync(d);
            if (meta) { meta.style.display = ''; meta.innerHTML = m2; }
            body.innerHTML = b2;
        } else if (name === 'tagging') {
            // The Tagging sub-view is owned by tagging.js. audit.js keeps the
            // shared chrome (tabs + summary) and delegates the body/meta render
            // through a window global (a call, not an import — no audit->tagging
            // edge), handing over this load's latest-wins gate so a stale
            // tagging fetch can't clobber a report the user switched to.
            _hideEmptyState();
            _setTabsVisible(tabs, true);
            await _ensureSummary();
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
                // A scan exists (we hold its summary) but did not compute
                // THIS report — a skip-duplicates scan 404s the Duplicates
                // tab as a matter of course. Hiding the whole tablist here
                // made every other servable report and the search bar
                // unreachable until a rescan (2026-08-12 adjudicated #4).
                // Keep the chrome; empty the body only. With no summary at
                // all there is no scan, and the full pre-scan hide is right.
                if (r.status === 404 && _lastSummary) {
                    _hideEmptyState();
                    _setTabsVisible(tabs, true);
                    await _ensureSummary();
                    if (meta) { meta.style.display = 'none'; meta.textContent = ''; }
                    body.innerHTML = '<p class="audit-search-empty">'
                        + _esc(d.error || 'This report is not part of the cached scan.')
                        + ' Pick another tab, or re-run the scan with this phase enabled.</p>';
                    return;
                }
                _hideReport();
                _showError(d.error || 'Report unavailable');
                return;
            }
            _hideEmptyState();
            _setTabsVisible(tabs, true);
            await _ensureSummary();
            if (name === 'read_unzoterod') accPayload = d;
            // Phase 2 Option A: the review workspace mounts on its OWN tab only.
            // not_in_zotero always keeps the curated-queue table.
            if ((name === 'pdf_review' || name === 'not_in_zotero') && d.review_scope) pendingPayload = d;
            const [metaHtml, tableHtml] = _renderReport(name, d);
            if (meta) {
                meta.style.display = '';
                // The PDF-reconciliation line only on reports ABOUT PDFs: on a
                // tag or note report it was a paragraph of unrelated counts
                // above the report's own explanation (2026-09-19 UI review).
                meta.innerHTML = (_PDF_REPORTS.has(name) ? _pdfProjectionHtml(d.pdf_reconciliation) : '') + metaHtml;
            }
            body.innerHTML = tableHtml;
        }
        if (pendingPayload) {
            // Mode title for the dedicated review tab (Phase 0 + Phase 2).
            if (meta) {
                meta.innerHTML = '<strong>PDF identification review</strong> — per-file workspace. '
                    + `${pendingPayload.rows.length} PDF(s) to review. `
                    + 'Confirming writes only the local review store; '
                    + 'mapping, curation and Zotero writes each ask separately. '
                    + _gotoReportBtn('not_in_zotero', '[Not in Zotero queue]')
                    + ' · '
                    + _gotoReportBtn('zotero_attachments', '[Zotero Attachments]');
                // Mount returns before _wireRowActions — still need meta jumps.
                _wireMetaGotoButtons(meta);
            }
            _pendingTeardown = mountPendingReview(body, pendingPayload);
            return;
        }
        if (name === 'pdf_review') {
            // No review_scope (no scan / enrich failed) — still show a clear empty.
            if (meta) {
                meta.innerHTML = '<strong>PDF identification review</strong> — run an audit scan first, '
                    + 'or open '
                    + _gotoReportBtn('not_in_zotero', '[Not in Zotero]')
                    + ' for the curated queue.';
                _wireMetaGotoButtons(meta);
            }
            body.innerHTML = '<p class="audit-search-empty">No review workspace for this scan.</p>';
            return;
        }
        _wireRowActions();
        if (name === 'read_unzoterod' && accPayload) {
            // AFTER the wire pass: _accessionAttach replaces the body
            // wholesale and re-runs _wireRowActions itself, so attaching
            // earlier would leave the outer call double-binding its DOM.
            await _accessionAttach(accPayload, isCurrent);
        }
    } catch (e) {
        if (!isCurrent()) return;
        body.textContent = '';
        _showError(String(e));
    }
}

/**
 * Count-aware loading placeholder for the report body.
 *
 * `body.textContent = 'Loading…'` collapsed a 72vh pane to one line and then
 * snapped back to a thousands-row table, so every tab switch moved the page
 * under the pointer. The row count is usually already known (the dashboard
 * summary is cached from the last scan), so the skeleton is sized from it
 * rather than fixed — the point is holding roughly the right height, not the
 * shimmer. `aria-busy` is what AT hears; the bars themselves are decoration.
 */
const _SKELETON_MAX_ROWS = 14;

function _skeletonHtml(name) {
    let n = 8;
    const s = _lastSummary;
    if (s) {
        if (name === 'inventory' && s.record_count) n = s.record_count;
        else if (name === 'ambiguous_pdfs' && s.pdfs_ambiguous) n = s.pdfs_ambiguous;
        else if (name === 'skipped_pdfs' && s.pdfs_skipped) n = s.pdfs_skipped;
        else if (name === 'read_unzoterod' && s.pdfs_unmapped) n = s.pdfs_unmapped;
    }
    n = Math.max(3, Math.min(n, _SKELETON_MAX_ROWS));
    const bars = new Array(n).fill('<div class="audit-skeleton-row" aria-hidden="true"></div>').join('');
    return `<div class="audit-skeleton" aria-busy="true" aria-label="Loading report">`
        + `<div class="audit-skeleton-row is-head" aria-hidden="true"></div>${bars}</div>`;
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

/**
 * One tag in a Tag Harmony cell, rendered as the id that would be written.
 *
 * Writes are gated on the literal `_tags.md` id, and the report compares by a
 * spelling-insensitive fingerprint — so the surface shown in the drift and the
 * string a write would use are not always the same text. Showing the resolved
 * id (with the original as a tooltip when they differ) means the cell states
 * what would actually land. A surface the vocabulary cannot resolve is
 * genuinely unapplyable and is badged rather than silently listed, which would
 * promise a write that never happens.
 *
 * @param {string} surface   the drift surface as the report found it
 * @param {object} vocabIds  the row's surface → `_tags.md` id map ({} if none)
 * @param {boolean} available whether the vocabulary loaded at all
 */
function _harmonyTagHtml(surface, vocabIds, available) {
    if (!available) return `<span class="harmony-tag">${_esc(surface)}</span>`;
    const id = vocabIds ? vocabIds[surface] : null;
    if (id) {
        const title = id === surface ? '' : ` title="${_attr(`found as “${surface}”`)}"`;
        return `<span class="harmony-tag"${title}>${_esc(id)}</span>`;
    }
    return `<span class="harmony-tag harmony-tag-novocab"`
        + ` title="${_attr('Not in _tags.md — tag writes are vocabulary-gated, so this one is skipped')}">`
        + `${_esc(surface)} <span class="harmony-badge">not in vocab</span></span>`;
}

/** Every dashboard label and heading, defined (2026-09-19).
 *
 * The user could not tell what "Triangulated" or "Outside the join" counted:
 * no tile, heading or tab carried an explanation. One table, keyed by the
 * label exactly as rendered, so a renamed label without a definition is
 * caught by tests/js/auditTerms.test.js rather than silently losing its "?".
 * The Help guide's "Library Audit glossary" says the same things at length;
 * keep the two in step. A RECORD is one citation key: the union of the
 * `_master.bib` keys and the keys a PDF was matched to. */
export const _AUDIT_TERMS = {
    // Section headings
    'Where every record stands': 'The main states of the library, side by side. Mixes counts of records and counts of PDFs, so it is not a partition.',
    'Reconciliation': 'How often each pair of sources joins up, per record (a record is one citation key).',
    'Tag coverage — where the tags actually are': 'How many records carry at least one tag on each surface you can write to: the reference note, the PDF in Finder, the Zotero item.',
    'Tag depth — how many tags each paper carries': 'Distinct tags per record, counted across note, Finder and Zotero item together (case and accents folded).',
    'Outside the join': 'Things the per-citation-key join cannot see, and how Zotero items were matched to records.',
    // Where every record stands
    'Triangulated': 'Complete records: a bib entry, at least one matched PDF, a reference note AND a Zotero item, all four at once. Reading notes and tags are not required.',
    'Entry, no local PDF': 'Records minus "Bib + PDF": almost all are bib entries with no matched PDF. It also counts entries you marked as needing no PDF, so it can exceed the "Bib Entries Without PDFs" report.',
    'Unmapped PDF': 'PDFs in the bibliography folder that no rule matched to any citation key. Ambiguous, skipped, "not for Zotero" and "add to Zotero later" PDFs are not counted here.',
    'PDF, confirmed not in bib': 'PDFs you marked "not a paper for Zotero" (the no_match decision in mapping.json).',
    'PDF, pending for Zotero': 'PDFs you marked "add to Zotero later" (the pending decision in mapping.json).',
    'Zotero item, no record': 'Zotero parent items no record claimed: no Better BibTeX key, the loser of a duplicated key, or a refused title match.',
    // Reconciliation
    'Records': 'Citation keys: every key in _master.bib plus any key a PDF was matched to.',
    'Bib + PDF': 'Records with a bib entry and at least one matched PDF.',
    'Bib + reference note': 'Records with a bib entry and an Obsidian note named <citation key>.md in the Zotero-notes folder.',
    'Bib + Zotero item': 'Records with a bib entry and a Zotero parent item (matched by Better BibTeX key, otherwise by a unique title).',
    'Zotero w/ child note': 'Records whose Zotero item has at least one child note (your Zotero reading note).',
    'PDFs ambiguous': 'PDFs for which several bib entries matched equally well, so none was chosen. Resolve them under Advanced decisions and historical reports → Ambiguous PDF decisions.',
    'PDFs skipped': 'PDFs whose filename starts with the skip prefix (default z_item). They are left out of matching and of every report except duplicate detection (Duplicate PDFs, Possible Duplicates).',
    // Tag coverage
    'Reference note': 'Records whose Obsidian note has at least one tag, out of the records that have a note.',
    'Finder (PDF)': 'Records with at least one matched PDF carrying a macOS Finder tag, out of the records that have a PDF.',
    'Zotero item': 'Records whose Zotero parent item (not its child note) has at least one tag, out of the records that have a Zotero item.',
    'Tagged somewhere': 'Records with at least one tag on any of the three writable surfaces, out of the records that have at least one of them.',
    'Tagged nowhere': 'Records with no tag on any writable surface. Bib keywords and Zotero note tags are not counted.',
    // Outside the join
    'Notes w/o record': 'Notes in the Zotero-notes folder whose filename matches no citation key.',
    'Zotero matched by key': 'Records joined to their Zotero item by the Better BibTeX citation key (the normal case).',
    'Matched by title': 'Records joined to Zotero by a unique normalised title because the key did not match.',
    'Title match refused': 'Records whose key did not match Zotero and whose title did not give one safe match either: no item with that title, several, or one already claimed.',
    'Dead mapping entries': 'Manual decisions in mapping.json that point at a PDF path which no longer exists. See the Dead Curation report.',
};

const _tile = (label, value, sub, warn) =>
    `<div class="audit-summary-tile${warn && Number(value) > 0 ? ' tile-warn' : ''}">` +
    `<div class="audit-summary-value">${_esc(String(value))}</div>` +
    `<div class="audit-summary-label"><span${_AUDIT_TERMS[label] ? ` data-def="${_attr(_AUDIT_TERMS[label])}"` : ''}>${_esc(label)}</span></div>` +
    (sub ? `<div class="audit-summary-sub">${_esc(sub)}</div>` : '') +
    '</div>';

/** Give every `[data-def]` label in *root* its "?" badge (keyboard-focusable,
 * aria-describedby — `helpTip`'s accessible tooltip, not a mouse-only title). */
function _decorateDefs(root) {
    if (!root) return;
    for (const el of root.querySelectorAll('[data-def]')) {
        helpTip(el, { text: el.dataset.def, labelText: el.textContent.trim() });
    }
}

/** `"of 5678 (22%)"` — the denominator a coverage count is meaningless without. */
function _ofDenom(n, total) {
    if (!total) return 'no records have this surface';
    return `of ${total} (${Math.round((n / total) * 100)}%)`;
}

function _summaryHtml(s) {
    const group = (label, tiles) =>
        `<div class="audit-summary-group-label"><span${_AUDIT_TERMS[label] ? ` data-def="${_attr(_AUDIT_TERMS[label])}"` : ''}>${_esc(label)}</span></div>` +
        `<div class="audit-summary-grid">${tiles.join('')}</div>`;

    // The five states a record can be in, together. Each already had a report;
    // none of them had a shared denominator, so "where does the library stand"
    // meant opening four tabs and doing the arithmetic yourself.
    let html = group('Where every record stands', [
        _tile('Triangulated', s.fully_triangulated),
        _tile('Entry, no local PDF', (s.record_count ?? 0) - (s.bib_with_pdf ?? 0), null, true),
        _tile('Unmapped PDF', s.pdfs_unmapped, null, true),
        _tile('PDF, confirmed not in bib', s.confirmed_no_match_count ?? 0),
        _tile('PDF, pending for Zotero', s.pending_count ?? 0),
        _tile('Zotero item, no record', s.zotero_items_without_record ?? 0, null, true),
    ]);

    html += group('Reconciliation', [
        _tile('Records', s.record_count),
        _tile('Bib + PDF', s.bib_with_pdf),
        _tile('Bib + reference note', s.bib_with_obsidian_note),
        _tile('Bib + Zotero item', s.bib_with_zotero_parent),
        _tile('Zotero w/ child note', s.zotero_parents_with_child_note),
        _tile('PDFs ambiguous', s.pdfs_ambiguous, null, true),
        _tile('PDFs skipped', s.pdfs_skipped),
    ]);

    // Tag coverage: every tile carries its own denominator, because "1 200
    // note-tagged" says nothing until you know how many records have a note.
    const c = s.tag_coverage;
    if (c) {
        html += group('Tag coverage — where the tags actually are', [
            _tile('Reference note', c.note_tagged, _ofDenom(c.note_tagged, c.note_present)),
            _tile('Finder (PDF)', c.finder_tagged, _ofDenom(c.finder_tagged, c.finder_present)),
            _tile('Zotero item', c.zotero_tagged, _ofDenom(c.zotero_tagged, c.zotero_present)),
            _tile('Tagged somewhere', c.any_tagged, _ofDenom(c.any_tagged, c.writable_records)),
            _tile('Tagged nowhere', c.none_tagged, _ofDenom(c.none_tagged, c.writable_records), true),
        ]);
        const notes = [];
        if (c.finder_is_floor) {
            notes.push('Finder counts are a floor: a tagged PDF the bridge never ' +
                       'mapped to a citation key has no record, so it is not counted here.');
        }
        if (c.finder_platform && c.finder_platform !== 'darwin') {
            notes.push('Finder tags read empty on this platform (macOS only).');
        }
        if (notes.length) {
            html += `<div class="audit-summary-note">${_esc(notes.join(' '))}</div>`;
        }
        // Depth, not just presence (2026-08-26). Coverage above answers "does
        // this paper have a tag"; this answers "how well is it described",
        // which is the question a re-tagging cohort is selected on. The union
        // row is the one the Tagging tab's cohort filter and the CLI's
        // --max-tags act on, so it is the one shown.
        const d = c.tag_depth && c.tag_depth.union;
        if (d) {
            const order = ['0', '1', '2', '3-4', '5+'];
            const total = order.reduce((n, k) => n + (d[k] || 0), 0);
            const under = (d['0'] || 0) + (d['1'] || 0) + (d['2'] || 0);
            const max = Math.max(1, ...order.map((k) => d[k] || 0));
            const bars = order.map((k) => {
                const n = d[k] || 0;
                const pct = Math.round((n / max) * 100);
                const low = k === '0' || k === '1' || k === '2';
                return `<div class="audit-depth-row${low ? ' low' : ''}">
                    <span class="audit-depth-label">${_esc(k)}</span>
                    <span class="audit-depth-track"><span class="audit-depth-fill" style="width:${pct}%"></span></span>
                    <span class="audit-depth-val">${_esc(String(n))}</span>
                </div>`;
            }).join('');
            html += `<div class="audit-summary-group-label"><span data-def="${_attr(_AUDIT_TERMS['Tag depth — how many tags each paper carries'])}">Tag depth — how many tags each paper carries</span></div>
                <div class="audit-depth" role="img" aria-label="${_attr(
                    order.map((k) => `${d[k] || 0} papers with ${k} tags`).join(', '))}">${bars}</div>
                <div class="audit-summary-note">Distinct tags across the surfaces each
                    paper has, over ${_esc(String(total))} records with somewhere to write.
                    <strong>${_esc(String(under))}</strong> carry two or fewer — the cohort the
                    Tagging tab's <em>Tags</em> filter and <code>papermind tagging batch
                    --max-tags 2</code> select.</div>`;
        }
    }

    // Orphans: things the vault holds that no record can see. Zero is a real
    // and welcome answer, so the group renders either way rather than
    // disappearing (an absent tile reads as "not checked", not as "none").
    if (s.notes_without_record !== undefined || s.zotero_items_without_record !== undefined) {
        html += group('Outside the join', [
            _tile('Notes w/o record', s.notes_without_record ?? 0, null, true),
            _tile('Zotero matched by key', s.zotero_matched_by_key ?? 0),
            _tile('Matched by title', s.zotero_matched_by_title ?? 0),
            _tile('Title match refused', s.zotero_title_refused ?? 0),
            _tile('Dead mapping entries', s.mapping_orphans ?? 0, null, true),
        ]);
    }

    if (s.zotero_error) {
        html += `<div class="audit-summary-warning">Zotero read warning: ${_esc(s.zotero_error)}</div>`;
    }
    // Collapsible (2026-09-19 UI review): open, the tiles pushed every report
    // about two screens down. The choice is remembered per viewer.
    let open = true;
    try { open = localStorage.getItem('audit_overview_open') !== '0'; } catch (_e) { /* storage unavailable */ }
    return `<details class="audit-summary-details"${open ? ' open' : ''}>`
        + `<summary>Library overview — ${_esc(String(s.record_count ?? 0))} records`
        + ` · ${_esc(String(s.fully_triangulated ?? 0))} triangulated</summary>${html}</details>`;
}

/** Remember the overview's open/closed state (wired once per render). */
function _wireOverviewToggle(root) {
    const det = root && root.querySelector('.audit-summary-details');
    if (!det || det.dataset.wired) return;
    det.dataset.wired = '1';
    det.addEventListener('toggle', () => {
        try { localStorage.setItem('audit_overview_open', det.open ? '1' : '0'); } catch (_e) { /* ignore */ }
    });
}

function _inventoryTable(records) {
    // The pairings-board state (2026-08-19 review, request v): the detail
    // drawer and the [view] preview read the record payloads back by key.
    _invRecords.clear();
    for (const r of records) _invRecords.set(r.citation_key, r);
    const head = `
        <thead><tr>
            <th scope="col">Citation key</th><th scope="col">Year</th><th scope="col">Author</th><th scope="col">Title</th>
            <th scope="col">Bib</th><th scope="col">Zot</th><th scope="col">Note</th><th scope="col">PDFs</th><th scope="col">Annot</th><th scope="col">Match</th>
            <th scope="col"><span class="sr-only">Pair detail</span></th>
        </tr></thead>`;
    // data-key is the cross-report search's landing anchor. _scrollToReportRow
    // looks for [data-key] or [data-pdf] and otherwise silently does nothing —
    // and this table had neither, so a hit in the `inventory` group (the
    // largest group the corpus produces) jumped to the tab and then stopped,
    // breaking the search's stated promise that "a hit always corresponds to a
    // row the user can go and look at". The per-PDF [unmap] verb happens to
    // carry both, but a record with NO pdf would still have no anchor.
    const rows = _inventoryRows(records);
    // Wrapped in a layout container so the [view] preview can be a SIDE panel
    // on a wide window instead of a block that pushes the whole table down
    // (see .audit-inv-layout.has-preview).
    // .audit-table-scroll is the house scrollport for a wide report table, and
    // this was the ONE table without it (2026-09-07 review). Two consequences,
    // both measured against not_in_zotero as the control: the filter strip
    // mounts at `table.closest('.audit-table-scroll')` and so, with no wrapper,
    // landed INSIDE the horizontally-scrolling region — scrolling right to
    // reach the row's own verbs carried it away (at 960px, from x=[283,917] to
    // [-676,-42], entirely off screen), which is the exact hazard the comment
    // at that mount point warns about. It also left the table sizing itself
    // against #audit-report-body rather than its own scrollport. The wrapper
    // goes INSIDE .audit-inv-main so the .has-preview grid column is untouched.
    return `<div class="audit-inv-layout"><div class="audit-inv-main">`
        + `<div class="audit-table-scroll">`
        + `<table class="audit-table audit-inv-table">${head}${_tbodyHtml(rows)}</table>`
        + `</div></div></div>`;
}

// Only this read-mostly inventory uses data-level paging. Other tables keep
// node-preserving controls for their editors/selections.
let _invPager = null;
function _inventoryRows(records) {
    return records.map(r => `
        <tr data-key="${_attr(r.citation_key)}">
            <td>${_esc(r.citation_key)}</td>
            <td>${_esc(r.year || '')}</td>
            <td>${_esc(r.first_author || '')}</td>
            <td class="cell-title" title="${_esc(r.title || '')}" aria-label="${_esc(r.title || '')}">${_esc((r.title || '').slice(0, 90))}</td>
            <td>${r.has_bib_entry ? '✓' : ''}</td>
            <td>${r.has_zotero_item ? '✓' : ''}</td>
            <td>${r.has_obsidian_note ? '✓' : ''}</td>
            <td data-sort-value="${_attr(r.pdf_count)}">${_esc(r.pdf_count)}${_pdfActions(r)}</td>
            <td data-sort-value="${_attr(r.annotations_count_max >= 0 ? r.annotations_count_max : '')}">${r.annotations_count_max >= 0 ? r.annotations_count_max : '—'}</td>
            <td>${_esc((r.match_sources || []).join(','))}</td>
            <td><button type="button" class="audit-link-btn" data-inv-detail="${_attr(r.citation_key)}"
                 aria-expanded="false" aria-label="Pair detail for ${_attr(r.citation_key)}">[detail]</button>${r.has_zotero_item ? `<button type="button" class="btn btn-outline btn-sm" data-manage-pdfs="${_attr(r.citation_key)}">Files &amp; attachments…</button>` : ''}</td>
        </tr>`).join('');
}

function _mountPagedInventory(body, records) {
    for (const record of records) _invRecords.set(record.citation_key, record);
    const table = body.querySelector('.audit-inv-table');
    table.dataset.inventoryPaged = '1';
    const tbody = table.tBodies[0];
    const stateKey = 'inventory-paged';
    const state = {query: '', sortKey: null, dir: 'asc', ..._tableViewState.get(stateKey)};
    let page = 0;
    let matches = [];
    // Keep only visible rows and rows with an open drawer/confirmation. This
    // preserves an action's exact node/binding without caching every visited page.
    const retained = new Map();
    const text = records.map(r => foldText([r.citation_key, r.year, r.first_author,
        r.title, r.has_bib_entry ? '✓' : '', r.has_zotero_item ? '✓' : '',
        r.has_obsidian_note ? '✓' : '', r.pdf_count, r.annotations_count_max,
        (r.match_sources || []).join(',')].join(' ')));
    const fields = ['citation_key', 'year', 'first_author', 'title', 'has_bib_entry',
        'has_zotero_item', 'has_obsidian_note', 'pdf_count', 'annotations_count_max', 'match_sources'];
    const numeric = new Set([1, 7, 8]);
    const wrap = document.createElement('div');
    wrap.className = 'table-controls inventory-controls';
    const input = document.createElement('input');
    input.type = 'search'; input.className = 'table-filter';
    input.setAttribute('aria-label', 'Filter all inventory records');
    input.placeholder = 'Filter all records…'; input.value = state.query;
    const count = document.createElement('span'); count.className = 'table-filter-count';
    const prev = document.createElement('button'); prev.type = 'button'; prev.className = 'btn btn-outline'; prev.textContent = 'Previous page';
    const next = document.createElement('button'); next.type = 'button'; next.className = 'btn btn-outline'; next.textContent = 'Next page';
    wrap.append(input, count, prev, next);
    table.closest('.audit-table-scroll').before(wrap);
    const paint = (announce = true) => {
        for (const row of [...tbody.rows]) {
            if (!row.dataset.key) continue;
            const nodes = [row];
            let sibling = row.nextElementSibling;
            while (sibling?.hasAttribute('data-row-attachment')) {
                nodes.push(sibling); sibling = sibling.nextElementSibling;
            }
            retained.set(row.dataset.key, nodes);
        }
        const visible = matches.slice(page * 100, (page + 1) * 100);
        const keys = new Set(visible.map(i => records[i].citation_key));
        for (const [key, nodes] of retained) {
            if (!keys.has(key) && nodes.length === 1 && !nodes[0].querySelector('.inline-confirm-strip, button:disabled')) retained.delete(key);
        }
        const fragment = document.createDocumentFragment();
        for (const i of visible) {
            const record = records[i];
            let nodes = retained.get(record.citation_key);
            if (!nodes) {
                const scratch = document.createElement('tbody');
                scratch.innerHTML = _inventoryRows([record]);
                nodes = [scratch.firstElementChild];
            }
            fragment.append(...nodes);
        }
        tbody.replaceChildren(fragment);
        if (!visible.length) tbody.innerHTML = '<tr><td colspan="11" class="muted">No records match this filter.</td></tr>';
        applyActionRoles(tbody);
        count.textContent = matches.length ? `${page * 100 + 1}–${Math.min((page + 1) * 100, matches.length)} of ${matches.length} matches · ${records.length} records` : `No matching records · ${records.length} records`;
        prev.disabled = page === 0; next.disabled = (page + 1) * 100 >= matches.length;
        if (announce) announceStatus(count.textContent);
    };
    const update = () => {
        const tokens = foldText(state.query).split(/\s+/).filter(Boolean);
        matches = records.map((_, i) => i).filter(i => tokens.every(t => text[i].includes(t)));
        const col = fields.indexOf(state.sortKey);
        if (col >= 0) {
            const values = records.map(r => {
                const v = r[state.sortKey];
                return v == null || v === '' || (col >= 4 && col <= 6 && !v) || (col === 8 && v < 0) ? null : numeric.has(col) ? Number(v) : foldText(Array.isArray(v) ? v.join(',') : String(v));
            });
            matches.sort((a, b) => {
                const x = values[a], y = values[b];
                if (x === null || y === null) return x === y ? a - b : x === null ? 1 : -1;
                const order = numeric.has(col) ? x - y : x.localeCompare(y);
                return (state.dir === 'asc' ? order : -order) || a - b;
            });
        }
        _tableViewState.set(stateKey, {...state});
        page = 0; paint();
    };
    input.addEventListener('input', () => { state.query = input.value; update(); });
    prev.addEventListener('click', () => { page--; paint(); if (prev.disabled) next.focus(); });
    next.addEventListener('click', () => { page++; paint(); if (next.disabled) prev.focus(); });
    [...table.tHead.rows[0].cells].forEach((th, i) => {
        if (!fields[i]) return;
        const button = document.createElement('button'); button.type = 'button';
        button.className = 'th-sort'; button.textContent = th.textContent;
        const arrow = document.createElement('span'); arrow.className = 'th-sort-arrow';
        arrow.setAttribute('aria-hidden', 'true'); arrow.textContent = '↕'; button.append(arrow);
        th.replaceChildren(button);
        button.addEventListener('click', () => {
            state.dir = state.sortKey === fields[i] && state.dir === 'asc' ? 'desc' : 'asc';
            state.sortKey = fields[i];
            for (const header of table.tHead.rows[0].cells) header.removeAttribute('aria-sort');
            th.setAttribute('aria-sort', state.dir === 'asc' ? 'ascending' : 'descending'); update();
        });
        if (state.sortKey === fields[i]) th.setAttribute('aria-sort', state.dir === 'asc' ? 'ascending' : 'descending');
    });
    _invPager = {reveal(key, rel) {
        const index = records.findIndex(r => key ? r.citation_key === key : r.pdf_paths?.some(p => p.rel === rel));
        if (index < 0) return;
        if (!matches.includes(index)) { state.query = ''; input.value = ''; update(); }
        page = Math.floor(matches.indexOf(index) / 100); paint();
    }};
    update();
}

/** Per-PDF verbs in the Inventory row's PDF cell.
 *
 * [unmap] is HERE and not only behind [detail] (2026-09-07 review): breaking
 * one wrong pair was a two-click disclosure on a ~6 000-row table, and the
 * drawer is otherwise a read-only detail view. The blanket [never auto-map]
 * deliberately stays in the drawer — it also DROPS any hand-curated match,
 * which is a decision that should be read before it is taken, and the drawer
 * is where that sentence lives. Both still post through _detachConfirm.
 *
 * Sort/filter safety: the cell carries data-sort-value="pdf_count", so the
 * column sorts on the number rather than on this markup, and
 * installTableControls excludes verb-button labels from the filter haystack.
 */
function _pdfActions(r) {
    if (!r.pdf_paths || !r.pdf_paths.length) return '';
    return r.pdf_paths.map(p => {
        const via = p.source ? ` — matched via ${_provenanceLabel(p.source)}` : '';
        return ` <button type="button" class="audit-link-btn" data-inv-view="${_attr(p.rel)}" title="Preview ${_attr(p.rel)}${_attr(via)}" aria-label="Preview ${_attr(p.rel)}">[view]</button>`
            + ` <button type="button" class="audit-link-btn" data-action="open" data-abs="${_attr(p.abs)}" title="Open ${_attr(p.rel)}" aria-label="Open ${_attr(p.rel)}">[open]</button>`
            + ` <button type="button" class="audit-link-btn" data-action="detach" data-pdf="${_attr(p.rel)}" data-key="${_attr(r.citation_key)}" data-source="${_attr(p.source || '')}" title="Unmap ${_attr(p.rel)} from ${_attr(r.citation_key)}" aria-label="Unmap ${_attr(p.rel)} from ${_attr(r.citation_key)}">[unmap]</button>`;
    }).join('');
}

// --- Inventory pairings board (2026-08-19 review, request v) ---------------
//
// The Inventory tab IS the per-pair board: each row joins one citation key's
// bib entry, Zotero item, note and PDFs. What was missing: an in-app PDF
// preview (the tagging deck's `V` affordance), the per-PDF match provenance
// (the bridge records it; the union column hid it), and a per-pair detail
// drawer for the Zotero/note side. Mostly a read surface, but NOT read-only
// since 2026-09-07: [unmap] / [never auto-map] write mapping.json (via
// _detachConfirm) and [link pdf: into note] writes one note's frontmatter
// (via _invPdfLink, journalled in Tagging → Restore). Keep this sentence in
// step with the drawer and _pdfActions — a "read-only" claim here previously
// outlived the verbs by two days (2026-09-10 Phase 0).

let _invPdfUrl = null;
const _invPdfResource = new PreviewResource();
const _invRecords = new Map();   // citation_key -> serialized record
let _invPdfLinkStatus = {};      // citation_key -> non-current pdf: list state
let _invPdfLinkLoaded = false;

const _PDF_LINK_STATE_LABELS = {
    current: 'up to date',
    absent: 'missing from the note',
    stale: 'names a different file',
    dirty: 'hand-written — the writer will refuse',
    unreadable: 'note unreadable',
    no_note: 'no reference note',
    no_pdf: 'no mapped PDF',
};

function _pdfLinkStateLine(key) {
    if (!_invPdfLinkLoaded) return '';
    const st = _invPdfLinkStatus[key];
    const label = st ? (_PDF_LINK_STATE_LABELS[st] || st) : 'up to date';
    return ` Current <code>pdf:</code> list: ${label}.`;
}

/** Release the inventory preview's blob URL + panel (report-switch hook). */
function auditInvTeardown() {
    _invPdfResource.clear();
    _invPdfUrl = null;
    const panel = document.getElementById('audit-inv-pdf');
    if (panel) panel.remove();
    document.querySelectorAll('.audit-inv-layout.has-preview')
        .forEach(l => l.classList.remove('has-preview'));
}

/** Blob-URL PDF preview above the table — same transport rule as every PDF
 *  surface: secureFetch with the header, blob: src, NEVER an /api iframe src
 *  (the origin guard 403s those). Click [view] again to close. */
async function _invViewPdf(rel) {
    const body = document.getElementById('audit-report-body');
    if (!body || !rel) return;
    const existing = document.getElementById('audit-inv-pdf');
    if (existing && existing.getAttribute('data-rel') === rel) {
        auditInvTeardown();   // toggle off
        return;
    }
    auditInvTeardown();
    const panel = document.createElement('div');
    panel.id = 'audit-inv-pdf';
    panel.className = 'audit-inv-pdf';
    panel.setAttribute('data-rel', rel);
    panel.textContent = `Loading ${rel}…`;
    // Second child of the layout (the table is the first) so the wide-window
    // grid puts the preview on the RIGHT and the row you clicked stays where
    // it was; below the breakpoint it stacks under the table as a block.
    const layout = body.querySelector('.audit-inv-layout');
    if (layout) {
        layout.classList.add('has-preview');
        layout.appendChild(panel);
    } else {
        body.insertBefore(panel, body.firstChild);
    }
    // Identity guard on every post-await write: the user may have switched
    // reports (teardown removed the panel) or opened another PDF meanwhile.
    const stillCurrent = () => document.getElementById('audit-inv-pdf') === panel;
    try {
        const url = await _invPdfResource.load(`/api/audit/pdf?rel=${encodeURIComponent(rel)}`);
        if (!stillCurrent() || !url) return;
        _invPdfUrl = url;
        panel.innerHTML = '';
        const bar = document.createElement('div');
        bar.className = 'audit-inv-pdf-bar';
        const label = document.createElement('span');
        label.className = 'muted';
        label.textContent = `${rel} `;
        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'audit-link-btn';
        close.textContent = '[close preview]';
        close.addEventListener('click', auditInvTeardown);
        bar.appendChild(label);
        bar.appendChild(close);
        panel.appendChild(bar);
        const frame = document.createElement('iframe');
        frame.className = 'audit-ambig-frame';
        frame.src = _invPdfUrl;
        frame.title = `Preview of ${rel}`;
        panel.appendChild(frame);
        if (typeof panel.scrollIntoView === 'function') {
            panel.scrollIntoView({ block: 'nearest' });
        }
    } catch (e) {
        if (stillCurrent()) panel.textContent = String(e);
    }
}

async function _postReveal(payload) {
    try {
        const r = await secureFetch('/api/audit/reveal', {
            method: 'POST', body: JSON.stringify(payload),
        });
        if (!r.ok) {
            const d = await r.json().catch(() => ({}));
            _showError(d.error || `Reveal failed (${r.status})`);
        }
    } catch (err) {
        _showError(String(err));
    }
}

/**
 * The pair drawer, as a two-column definition grid.
 *
 * It was a run of <p> paragraphs (2026-08-19 Batch 3), so the four facets —
 * PDFs / Zotero / Note / Tags — and their values reflowed into one wall with
 * no shared left edge. A <dl> gives every facet the same label column, which
 * is what makes a drawer scannable at a glance. The drawer carries write
 * verbs since 2026-09-07 — per-PDF [unmap], the blanket [never auto-map]
 * (both mapping.json, via _detachConfirm) and the pdf: frontmatter link
 * (one note, via _invPdfLink) — so it is no longer read-only.
 */
function _invDetailHtml(r) {
    const rows = [];
    const dd = (label, html) => rows.push(`<dt>${_esc(label)}</dt><dd>${html}</dd>`);

    // The unmap verbs live HERE and not only on Entries With Several PDFs
    // (2026-09-07). That report lists records owning ≥2 files, so a record
    // with ONE wrongly-matched PDF had no unmap affordance anywhere in the
    // app — the drawer is the only surface that shows a pair at all, which
    // makes it the place to break one.
    const pdfLines = (r.pdf_paths || []).map(p => `
        <li>${_esc(p.rel)} <span class="muted">· matched via ${_esc(_provenanceLabel(p.source))}</span>
            <button type="button" class="audit-link-btn" data-inv-view="${_attr(p.rel)}">[view]</button>
            <button type="button" class="audit-link-btn" data-inv-open="${_attr(p.abs)}">[open]</button>
            <button type="button" class="audit-link-btn" data-inv-reveal="${_attr(p.abs)}">[reveal]</button>
            <button type="button" class="audit-link-btn" data-action="detach"
                data-pdf="${_attr(p.rel)}" data-key="${_attr(r.citation_key)}"
                data-source="${_attr(p.source || '')}"
                aria-label="${_attr(`Unmap ${p.rel} from ${r.citation_key}`)}">[unmap]</button>
            <button type="button" class="audit-link-btn" data-action="detach"
                data-pdf="${_attr(p.rel)}" data-detach-all
                data-source="${_attr(p.source || '')}"
                aria-label="${_attr(`Never auto-map ${p.rel} to any entry`)}">[never auto-map]</button>
        </li>`).join('');
    dd(`PDFs (${(r.pdf_paths || []).length})`, pdfLines
        ? `<ul class="audit-inv-pairs">${pdfLines}</ul>`
          + (r.note ? '<p class="audit-summary-note">'
              + `<button type="button" class="audit-link-btn" data-inv-pdflink="${_attr(r.citation_key)}">`
              + '[write these into the note as a <code>pdf:</code> link]</button> '
              + '\u2014 adds a frontmatter list so the note links to its own file. Reversible from '
              + 'Tagging \u2192 Restore. A rename made in this app follows the link; any other '
              + 'reference to the file still makes a rename refuse.'
              + _pdfLinkStateLine(r.citation_key)
              + '</p>' : '')
          + '<p class="audit-summary-note"><strong>Unmap</strong> stops automatic matching of that '
          + 'file to THIS key; <strong>never auto-map</strong> stops it for every key and drops any '
          + 'hand-curated match, leaving the file listed as unmapped so you can still link it by '
          + 'hand. Both take effect at the next scan and are undone with [clear] on the Not in '
          + 'Zotero tab.</p>'
        : '<span class="muted">none — no file is paired to this key.</span>');

    if (r.zotero) {
        const zBtn = `<button type="button" class="audit-link-btn" data-inv-zotero="${_attr(r.zotero.item_key)}"${r.zotero.group_id ? ` data-inv-zgroup="${_attr(r.zotero.group_id)}"` : ''}>[open in Zotero]</button>`;
        dd('Zotero', `${_esc(r.zotero.item_type || 'item')} ${_esc(r.zotero.item_key)}
            · ${r.zotero.child_note_count} reading note(s) ${zBtn}`);
    } else {
        dd('Zotero', '<span class="muted">no item.</span>');
    }

    if (r.note) {
        dd('Note', `${_esc(r.note.rel)}
            <button type="button" class="audit-link-btn" data-inv-open="${_attr(r.note.abs)}">[open]</button>
            <button type="button" class="audit-link-btn" data-inv-reveal="${_attr(r.note.abs)}">[reveal]</button>`);
    } else {
        dd('Note', '<span class="muted">none.</span>');
    }

    const tagLine = (label, tags) =>
        `${label}: ${tags && tags.length ? _esc(tags.join(', ')) : '<span class="muted">(none)</span>'}`;
    dd('Tags', `${tagLine('note', r.obs_tags)} · ${tagLine('Finder', r.finder_tags)} · ${tagLine('Zotero', r.zotero_parent_tags)}`);

    return `<dl class="audit-inv-dl">${rows.join('')}</dl>`;
}

function _invToggleDetail(btn) {
    const key = btn.getAttribute('data-inv-detail');
    const tr = btn.closest('tr');
    if (!key || !tr) return;
    const next = tr.nextElementSibling;
    if (next && next.classList.contains('audit-inv-detail-row')) {
        next.remove();
        btn.setAttribute('aria-expanded', 'false');
        return;
    }
    const r = _invRecords.get(key);
    if (!r) return;
    const row = document.createElement('tr');
    row.className = 'audit-inv-detail-row';
    // The drawer belongs to the row above it. Without this marker
    // installTableControls treats it as an ordinary sibling: dataRows is a
    // snapshot taken at install time and cannot contain a row injected later,
    // so a sort re-appends every REAL row around the drawer and leaves it
    // behind — measured, a descending sort floated it to the top of the table,
    // adjacent to an unrelated pair whose [unmap] it then appeared to describe.
    // A filter matching nothing left it on screen for the same reason. This is
    // the 2026-08-30 notesync-panel defect on a surface that carries three
    // write verbs. Pinned by tests/js/auditInventoryBoard.test.js.
    row.setAttribute('data-row-attachment', '');
    const td = document.createElement('td');
    td.colSpan = 11;
    td.innerHTML = _invDetailHtml(r);
    row.appendChild(td);
    tr.after(row);
    btn.setAttribute('aria-expanded', 'true');
    // The drawer is created after _wireRowActions ran, so the verbs the
    // report body's delegate does NOT reach are wired here: it dispatches
    // [data-inv-view], [data-inv-detail] and every .audit-link-btn carrying a
    // data-action, and falls through harmlessly on a button with neither a
    // data-action nor a data-abs. So [data-inv-open] / [data-inv-reveal] /
    // [data-inv-pdflink] / [data-inv-zotero] need a direct listener and the
    // other two must NOT have one — binding them here as well fired both
    // handlers on one click. That was not cosmetic for [view]: _invViewPdf
    // TOGGLES, so the first call built the preview panel and the second tore
    // it down inside the same dispatch, leaving a dead button. The drawer can
    // only exist if the delegate is live (_invToggleDetail has exactly one
    // caller — the delegate — and it runs under _genericRowPass), so leaning
    // on it here is safe by construction rather than by convention.
    // Pinned by: tests/js/auditInventoryBoard.test.js.
    td.querySelectorAll('[data-inv-open]').forEach(b => {
        b.addEventListener('click', () => _postReveal({ open: b.getAttribute('data-inv-open') }));
    });
    td.querySelectorAll('[data-inv-reveal]').forEach(b => {
        b.addEventListener('click', () => _postReveal({ path: b.getAttribute('data-inv-reveal') }));
    });
    td.querySelectorAll('[data-inv-pdflink]').forEach(b => {
        b.addEventListener('click', () => _invPdfLink(b));
    });
    td.querySelectorAll('[data-inv-zotero]').forEach(b => {
        const payload = { zotero_key: b.getAttribute('data-inv-zotero') };
        const g = parseInt(b.getAttribute('data-inv-zgroup') || '', 10);
        if (Number.isFinite(g) && g > 0) payload.zotero_group_id = g;
        b.addEventListener('click', () => _postReveal(payload));
    });
}

// --- Manual bibliography link picker (2026-08-19) --------------------------
//
// Closes the registered deferral: "the deck has no 'assign to an arbitrary
// key' verb — a `none` row that IS in the bib under a spelling no ladder rung
// catches can only be linked by hand outside the deck." A transliterated
// surname (`von_kaenel` → `vonkanel`) is the measured example.
//
// NO NEW WRITE SURFACE: the link is the existing POST /api/audit/mapping
// {pdf, citation_key}, confirm-gated and undoable through the same `clear`
// verb as every other mapping decision. It deliberately does NOT carry
// `source: "accession"` — a human-picked key is hand curation, and the
// pairings board's provenance column would otherwise credit the resolver for
// a decision it never made.
//
// DATA SOURCE: the cached scan's records, over the EXISTING
// GET /api/audit/inventory, fetched once per scan and held in a module cache.
// A dedicated `GET /api/audit/keys?q=` would ship a smaller payload, but it
// would also be a new route with its own gating, its own 404-without-scan
// contract and its own tests — and it would put a server round-trip in the
// path of every keystroke, which is worse than one fetch for a type-ahead.
// The inventory route already exists, already serves exactly this snapshot,
// and the Inventory tab already pays for it on every visit.

const _keyIndex = {
    token: undefined,   // the scan identity these rows came from
    rows: null,
    loading: null,      // in-flight promise, so two opens share one fetch
};
let _keyPicker = null;   // {el, input, anchor, rows, sel, onPick, cleanup}
let _keyPickerSeq = 0;

async function _loadKeyIndex() {
    if (_keyIndex.rows && _keyIndex.token === _lastScanFinishedAt) return _keyIndex.rows;
    if (_keyIndex.loading) return _keyIndex.loading;
    _keyIndex.loading = (async () => {
        const r = await secureFetch('/api/audit/inventory');
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || `Inventory unavailable (${r.status})`);
        const rows = (d.records || []).filter(
            (rec) => rec.has_bib_entry === true).map((rec) => {
            const key = rec.citation_key || '';
            const title = rec.title || '';
            const author = rec.first_author || '';
            const year = rec.year || '';
            return {
                key,
                title,
                author,
                year,
                hay: foldText(`${key} ${title} ${author} ${year}`),
            };
        });
        rows.sort((a, b) => (a.key < b.key ? -1 : (a.key > b.key ? 1 : 0)));
        _keyIndex.rows = rows;
        _keyIndex.token = _lastScanFinishedAt;
        return rows;
    })().finally(() => { _keyIndex.loading = null; });
    return _keyIndex.loading;
}

const _KEY_PICKER_MAX = 20;

function _keyPickerMatches(rows, query) {
    const tokens = foldText(query).split(/\s+/).filter(Boolean);
    if (!tokens.length) return [];
    const out = [];
    let total = 0;
    for (const row of rows) {
        if (!tokens.every((t) => row.hay.includes(t))) continue;
        total += 1;
        if (out.length < _KEY_PICKER_MAX) out.push(row);
    }
    return Object.assign(out, { total });
}

/** Close the picker. `restore` hands focus back to whatever opened it — the
 *  dialog-dismissal rule the palette and the confirm strip already follow. */
function _closeKeyPicker(restore) {
    if (!_keyPicker) return;
    const { el, input, anchor, cleanup } = _keyPicker;
    _keyPicker = null;
    if (input) {
        input.setAttribute('aria-expanded', 'false');
        input.removeAttribute('aria-activedescendant');
    }
    if (typeof cleanup === 'function') cleanup();
    if (el && el.parentNode) el.remove();
    if (restore && anchor && anchor.isConnected && typeof anchor.focus === 'function') {
        anchor.focus();
    }
}

function _keyPickerRender() {
    if (!_keyPicker) return;
    const { el, input, rows, sel, optionIdPrefix } = _keyPicker;
    const list = el.querySelector('.acc-keypicker-list');
    const count = el.querySelector('.acc-keypicker-count');
    const clearActive = () => input.removeAttribute('aria-activedescendant');
    if (!rows) {
        list.innerHTML = '';
        clearActive();
        count.textContent = 'Loading the bibliography…';
        return;
    }
    const matches = _keyPicker.matches || [];
    if (!_keyPicker.query) {
        list.innerHTML = '';
        clearActive();
        count.textContent = `Type to search ${rows.length} entries — citation key, title, author or year.`;
        return;
    }
    if (!matches.length) {
        list.innerHTML = '';
        clearActive();
        count.textContent = 'No entry matches.';
        return;
    }
    count.textContent = matches.total > matches.length
        ? `${matches.total} matches — showing the first ${matches.length}.`
        : `${matches.total} match(es).`;
    list.innerHTML = matches.map((m, i) => `
        <li role="option" id="${optionIdPrefix}-${i}" aria-selected="${i === sel ? 'true' : 'false'}"
            class="acc-keypicker-opt${i === sel ? ' is-sel' : ''}" data-keypick="${i}">
            <span class="acc-keypicker-key">${_esc(m.key)}</span>
            <span class="acc-keypicker-meta">${_esc(m.author)}${m.year ? ` · ${_esc(m.year)}` : ''}</span>
            <span class="acc-keypicker-title">${_esc((m.title || '').slice(0, 90))}</span>
        </li>`).join('');
    input.setAttribute('aria-activedescendant', `${optionIdPrefix}-${sel}`);
    list.querySelectorAll('[data-keypick]').forEach((li) => {
        // Pointer-down preserves combobox focus; click is the primary pointer
        // activation path, while touchend covers older embedded WebViews that
        // do not synthesize Pointer Events reliably. A second synthesized
        // event is harmless because the first pick closes the picker.
        li.addEventListener('pointerdown', (e) => e.preventDefault());
        li.addEventListener('click', (e) => {
            e.preventDefault();
            _keyPickerPick(parseInt(li.getAttribute('data-keypick'), 10));
        });
        li.addEventListener('touchend', (e) => {
            e.preventDefault();
            _keyPickerPick(parseInt(li.getAttribute('data-keypick'), 10));
        });
    });
    const selected = list.querySelector('[aria-selected="true"]');
    if (selected && typeof selected.scrollIntoView === 'function') {
        selected.scrollIntoView({ block: 'nearest' });
    }
}

function _keyPickerPick(i) {
    if (!_keyPicker) return;
    const m = (_keyPicker.matches || [])[i];
    if (!m) return;
    const { anchor, onPick, pdfName } = _keyPicker;
    _closeKeyPicker(true);
    // The write ceremony is the SAME confirmInline every other mapping write
    // uses, anchored on the control that opened the picker, and it names both
    // sides plus the exact file it touches.
    if (!anchor || !anchor.isConnected) { onPick(m.key); return; }
    confirmInline(anchor, {
        message: `Link “${pdfName}” to ${m.key}? Writes the audit’s mapping.json `
            + 'only — nothing touches the vault or Zotero, and it stays undoable.',
        confirmLabel: 'Link',
        tone: 'confirm',
        onConfirm: () => onPick(m.key),
    });
}

/**
 * Open the type-ahead over the cached scan's citation keys.
 *
 * @param {HTMLElement} anchorEl the control that opened it (focus returns here)
 * @param {object} o  {pdfName, onPick(citationKey)}
 */
function _openKeyPicker(anchorEl, o) {
    if (!anchorEl) return;
    _closeKeyPicker(false);
    const el = document.createElement('div');
    const seq = ++_keyPickerSeq;
    const listId = `acc-keypicker-list-${seq}`;
    const hintId = `acc-keypicker-hint-${seq}`;
    const countId = `acc-keypicker-count-${seq}`;
    el.id = 'audit-key-picker';
    el.className = 'acc-keypicker';
    el.setAttribute('role', 'group');
    el.setAttribute('aria-label', `Link ${o.pdfName || 'this PDF'} to a bibliography entry`);
    el.innerHTML = `
        <input type="text" class="acc-keypicker-input" role="combobox"
               aria-expanded="true" aria-controls="${listId}"
               aria-describedby="${hintId}"
               aria-autocomplete="list" autocomplete="off"
               aria-label="Search the bibliography by citation key, title, author or year"
               placeholder="Search the bibliography…">
        <p id="${countId}" class="muted acc-keypicker-count" role="status"
           aria-live="polite" aria-atomic="true"></p>
        <ul id="${listId}" class="acc-keypicker-list" role="listbox"
            aria-label="Bibliography entries"></ul>
        <p id="${hintId}" class="muted acc-keypicker-hint">
            ↑↓ to choose · Enter to pick · Esc or Tab to close</p>`;
    anchorEl.insertAdjacentElement('afterend', el);
    const input = el.querySelector('.acc-keypicker-input');
    const onDocumentFocusIn = (event) => {
        if (!_keyPicker || _keyPicker.el !== el || el.contains(event.target)) return;
        _closeKeyPicker(false);
    };
    document.addEventListener('focusin', onDocumentFocusIn, true);
    _keyPicker = {
        el, input, anchor: anchorEl, rows: null, matches: [], sel: 0, query: '',
        optionIdPrefix: `acc-keyopt-${seq}`,
        cleanup: () => document.removeEventListener('focusin', onDocumentFocusIn, true),
        onPick: o.onPick, pdfName: o.pdfName || '',
    };
    _keyPickerRender();
    input.focus();

    input.addEventListener('input', () => {
        if (!_keyPicker) return;
        _keyPicker.query = input.value.trim();
        _keyPicker.sel = 0;
        if (_keyPicker.rows) {
            _keyPicker.matches = _keyPickerMatches(_keyPicker.rows, _keyPicker.query);
        }
        _keyPickerRender();
    });
    // The picker sits inside decks that bind container-level verb handlers
    // (`L`, `S`, `1`–`9` …). Every key typed here must stop there, or typing a
    // search term would drive the deck underneath it.
    el.addEventListener('keydown', (e) => {
        e.stopPropagation();
        if (!_keyPicker) return;
        const n = (_keyPicker.matches || []).length;
        if (e.key === 'Escape') {
            e.preventDefault();
            _closeKeyPicker(true);
            return;
        }
        if (e.key === 'Tab') {
            // Removing the focused input while the browser performs its native
            // Tab traversal drops focus to <body> in WebKit/Chromium. Resolve
            // the destination relative to the opener before closing: reverse
            // Tab returns to it, forward Tab advances to its next real control.
            // This changes no write/confirmation state; it only preserves the
            // document's existing focus order across the transient widget.
            e.preventDefault();
            const anchor = _keyPicker.anchor;
            _closeKeyPicker(false);
            const stops = [...document.querySelectorAll(
                'a[href], button:not([disabled]), input:not([disabled]), '
                + 'select:not([disabled]), textarea:not([disabled]), '
                + '[tabindex]:not([tabindex="-1"])')]
                .filter((node) => !node.hidden && !node.closest('[hidden]')
                    && node.getAttribute('aria-hidden') !== 'true');
            const anchorIndex = stops.indexOf(anchor);
            const target = e.shiftKey
                ? anchor
                : stops[anchorIndex >= 0 ? anchorIndex + 1 : 0];
            if (target && target.isConnected && typeof target.focus === 'function') {
                target.focus();
            } else if (anchor && anchor.isConnected) {
                anchor.focus();
            }
            return;
        }
        if (e.key === 'ArrowDown' && n) {
            e.preventDefault();
            _keyPicker.sel = (_keyPicker.sel + 1) % n;
            _keyPickerRender();
            return;
        }
        if (e.key === 'ArrowUp' && n) {
            e.preventDefault();
            _keyPicker.sel = (_keyPicker.sel - 1 + n) % n;
            _keyPickerRender();
            return;
        }
        if (e.key === 'Enter') {
            e.preventDefault();
            if (n) _keyPickerPick(_keyPicker.sel);
        }
    });

    _loadKeyIndex().then((rows) => {
        if (!_keyPicker || _keyPicker.el !== el) return;   // closed meanwhile
        _keyPicker.rows = rows;
        _keyPicker.matches = _keyPickerMatches(rows, _keyPicker.query);
        _keyPickerRender();
    }).catch((err) => {
        if (!_keyPicker || _keyPicker.el !== el) return;
        input.removeAttribute('aria-activedescendant');
        const count = el.querySelector('.acc-keypicker-count');
        if (count) count.textContent = `Could not load the bibliography: ${err}`;
    });
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
/** The last ambiguous_pdfs payload rendered, so the view toggle need not
 *  re-fetch a view of the same cached scan. Cleared with the deck state. */
let _ambigPayload = null;

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
// Set by _noteScanIdentity so the ambiguous deck can tell a view toggle
// (no new scan) from a fresh scan. `null` until a scan's status is seen.
let _lastScanFinishedAt = null;

/**
 * Record which scan the module state describes; drop caches keyed to an
 * older one. `_pollStatus` refetches the summary itself on a GUI-driven scan,
 * but a scan finished OUTSIDE this page's poll loop (papermind audit scan /
 * MCP) is only ever seen here — on tab re-entry via initAuditTab — and
 * without the reset `_ensureSummary` kept painting the previous scan's tiles
 * for the rest of the session (2026-08-13 review).
 */
function _noteScanIdentity(status) {
    if (!status || !status.finished_at) return;
    if (status.finished_at !== _lastScanFinishedAt) {
        _lastScanFinishedAt = status.finished_at;
        _lastSummary = null;
        // The two persisted status lines describe writes made against the
        // PREVIOUS snapshot. They survive a report switch on purpose (that is
        // the bug they fix), but a new scan makes them history: "Renamed
        // oldKey → newKey." re-painted on top of a fresh scan reads as
        // something this scan just did (2026-09-20).
        _ns.lastStatus = { msg: '', isError: false, at: null, tone: 'info' };
        _zlLastStatus = { msg: '', isError: false, at: null, tone: 'info' };
        // Supersede any in-flight summary fetch: without this, a straddling
        // response landing after the reset repainted the previous scan's
        // tiles and re-warmed the cache with them (2026-08-18).
        _summaryGate.enter();
    }
}
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
    /* PERF (2026-09-02): re-render from the payload this report already has.
     * Before: every deck<->table toggle re-ran _loadReport, i.e. a fresh
     *   GET /api/audit/reports/ambiguous_pdfs plus the skeleton paint, for a
     *   view of the SAME cached scan — the accession toggle (_accSetView ->
     *   _accRenderBody) never did this.
     * Safe because: a report is a view of ONE cached scan, so the payload
     *   cannot change between two toggles; the render path, the meta line and
     *   the wire pass are the same functions _loadReport would have called
     *   with the same `d`. Without a cached payload (a toggle before any
     *   load) it falls back to the fetch.
     * Preserves: decided/undo are keyed to the scan token, so a toggle still
     *   does not wipe the deck's only undo surface.
     * Pinned by: tests/js/auditAmbigDeck.test.js. */
    const body = document.getElementById('audit-report-body');
    const meta = document.getElementById('audit-report-meta');
    if (!_ambigPayload || !body) { _loadReport('ambiguous_pdfs'); return; }
    auditAmbigTeardown();
    const [metaHtml, tableHtml] = _renderReport('ambiguous_pdfs', _ambigPayload);
    if (meta) { meta.style.display = ''; meta.innerHTML = metaHtml; }
    body.innerHTML = tableHtml;
    _wireRowActions();
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
            <kbd>L</kbd> link to a bib entry you pick ·
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

/* The tagging triage deck has shown "N triaged today" since the Card Catalog
 * pass; the other two decks are the same work and showed nothing. A day-scoped
 * fact, never a streak — skipping a day must not become a penalty. */
const _ambigToday = dailyCounter('audit_ambig_triage_daily');
const _accToday = dailyCounter('audit_acc_triage_daily');

function _ambigProgress() {
    const el = document.getElementById('audit-ambig-progress');
    if (!el) return;
    const total = _ambig.rows.length;
    const done = _ambig.decided.size;
    const bits = [`PDF ${Math.min(_ambig.idx + 1, total)} of ${total}`, `${done} resolved`];
    if (_ambig.undo.length) bits.push(`${_ambig.undo.length} undoable`);
    const today = _ambigToday.count();
    if (today) bits.push(`${today} decided today`);
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
    _closeKeyPicker(false);   // it is anchored to a button this render destroys
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
    // aria-pressed mirrors the visual is-sel state (2026-08-18): selection
    // was class-only, so AT could not tell which candidate 1–9 had picked.
    const candHtml = cands.map((c, i) => `
        <li>
            <button type="button" class="audit-ambig-cand${i === _ambig.sel ? ' is-sel' : ''}"
                    data-ambig="pick" data-i="${i}"
                    aria-pressed="${i === _ambig.sel ? 'true' : 'false'}">
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
            <button type="button" class="audit-link-btn" data-ambig="link-to" aria-label="Search the bibliography and assign an entry that is not among the candidates">[link to…]</button>
            <button type="button" class="audit-link-btn" data-ambig="no-match" aria-label="Mark as not a paper for Zotero">[not in bib]</button>
            <button type="button" class="audit-link-btn" data-ambig="pending" aria-label="Mark as to add to Zotero later">[pending]</button>
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
    applyActionRoles(card);
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

/** Select candidate *i* on the current card and SAY which one (2026-08-18 —
 *  pressing 1–9 used to announce nothing about what it picked; the polite
 *  region's last-write-wins means this lands after the card re-announce). */
function _ambigSelect(i) {
    _ambig.sel = i;
    _ambigRenderCard();
    const row = _ambig.rows[_ambig.idx];
    const c = row && (row.candidates || [])[i];
    if (c) announceStatus(`Candidate ${i + 1} selected: ${c.citation_key}.`);
}

function _ambigVerb(verb, btn) {
    switch (verb) {
        case 'pick': {
            const i = parseInt(btn.getAttribute('data-i'), 10);
            if (Number.isFinite(i)) _ambigSelect(i);
            break;
        }
        case 'assign': _ambigConfirmWrite(btn, 'match'); break;
        case 'link-to': {
            // Same picker as the accession deck. It is worth having here too:
            // the bridge parked this PDF because author+year matched several
            // keys, and the right key may be none of them.
            const row = _ambigCurrent();
            if (!row || _ambig.busy) break;
            _openKeyPicker(btn, {
                pdfName: row.pdf.name || row.pdf.rel,
                onPick: (key) => _ambigWrite('match', key),
            });
            break;
        }
        case 'no-match': _ambigConfirmWrite(btn, 'no_match'); break;
        case 'pending': _ambigConfirmWrite(btn, 'pending'); break;
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

// ── Accession: identify orphan PDFs against keyless bibliographic sources ──
//
// Phase 3 of the Accession Deck plan — extends the "Unmapped PDFs"
// report, no new tab. Three surfaces over /api/accession/*:
//   1. a per-row Identification column on the existing ranked table;
//   2. a bulk REVIEW LIST for resolved-identifier tiers — Link rows
//      (already in _master.bib) and Accession rows (new papers, copy their
//      DOIs into Zotero's "Add Item by Identifier") — where only identifier
//      tiers (bib_doi / xmp_doi / printed_doi) default CHECKED and
//      title_filename is listed UNCHECKED, so bulk always means "an
//      identifier resolved" and never "a title looked right";
//   3. a card deck for everything below those tiers, following the
//      ambiguous-PDF deck's idioms (confirmInline on every write,
//      peek-not-pop undo via mapping `clear`, blob-URL PDF panel with
//      identity guards, static-announcer a11y).
// Linking reuses POST /api/audit/mapping unchanged — no new write surface.

const _ACC_VIEW_KEY = 'audit_accession_view';
// The bulk default-check boundary (the plan's tier table): identifier
// evidence only. title_filename is deliberately absent — listed, unchecked.
// `isbn` joined 2026-08-26 (Batch D): a valid check digit is publisher-
// asserted identity, the same grade as an embedded DOI, so it sits above
// the bulk-selection boundary. Bulk still means "an identifier resolved",
// never "a title looked right".
const _ACC_PRECHECK_TIERS = ['bib_doi', 'xmp_doi', 'printed_doi', 'isbn'];
const _ACC_LIST_TIERS = ['bib_doi', 'xmp_doi', 'printed_doi', 'isbn', 'title_filename'];

const _acc = {
    enabled: false,
    batchMax: 50,
    rows: new Map(),      // rel -> {pdf, sha256, resolution} from /rows
    report: null,         // the read_unzoterod payload (row order = rank)
    linkSel: new Set(),   // review-list selections (rels)
    accSel: new Set(),
    seeded: false,        // default-check pass done for this snapshot
    busy: false,          // an identify/batch/link run in flight (writes only)
    abort: null,          // the running batch's AbortController (⇒ [cancel])
    decided: new Map(),   // rel -> {action, label} — this session's writes
    undo: [],             // [{pdf, label}] most recent last (mapping writes)
    sources: null,        // the server's CONFIGURED source subset (/rows)
    emailSet: false,      // a contact email rides the Crossref User-Agent
    idx: 0,
    sel: 0,
    lastStatus: { msg: '', isError: false, at: null, tone: 'info' },   // survives body re-renders
    scanToken: undefined, // decided/undo/selections are per scan snapshot
    focusAfter: null,     // selector list to re-focus after the next re-render
};
let _accPdfUrl = null;

/** Release the accession deck's blob URL (mirrors auditAmbigTeardown). */
function auditAccTeardown() {
    if (_accPdfUrl) {
        URL.revokeObjectURL(_accPdfUrl);
        _accPdfUrl = null;
    }
}

/** Escape a value for use inside a double-quoted attribute selector.
 *  (CSS.escape escapes IDENTIFIERS; an attribute value only needs its
 *  backslashes and quotes escaped, and rels legitimately carry `/` and `.`.) */
function _selQ(v) {
    return String(v == null ? '' : v).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

/**
 * Focus survival across a body re-render (2026-08-19 UX pass).
 *
 * Every accession write ends in `_accRenderBody()`, which replaces the whole
 * report body — so the control the user had just activated was destroyed with
 * its DOM and focus fell to <body>. For a keyboard user that meant the next
 * Tab restarted from the top of the page after every single link/undo. The
 * caller registers an ORDERED list of selectors; the first one that resolves
 * to an enabled control after the render wins, so a verb that no longer
 * exists (the row's [link] became [undo]) still lands somewhere sensible in
 * the same row rather than nowhere.
 */
function _accFocusAfter(selectors) {
    _acc.focusAfter = selectors && selectors.length ? selectors : null;
}

/** Selector chain for "the control that acts on this PDF row". */
function _accRowFocus(rel) {
    const q = _selQ(rel);
    return [
        `[data-acc-verb][data-rel="${q}"]`,
        `[data-acc-cell="${q}"] button`,
        `[data-acc-link-sel="${q}"]`,
        `[data-acc-acc-sel="${q}"]`,
        '#audit-acc-batch',
    ];
}

function _accRestoreFocus() {
    const chain = _acc.focusAfter;
    _acc.focusAfter = null;
    if (!chain) return;
    for (const sel of chain) {
        const el = document.querySelector(sel);
        if (el && !el.disabled && typeof el.focus === 'function') {
            el.focus();
            return;
        }
    }
}

function _accViewMode() {
    try {
        return localStorage.getItem(_ACC_VIEW_KEY) === 'deck' ? 'deck' : 'table';
    } catch (e) { return 'table'; }
}

function _accSetView(mode) {
    try { localStorage.setItem(_ACC_VIEW_KEY, mode); } catch (e) { /* private mode */ }
    _accRenderBody();
}

/** Fetch /api/accession/rows and take over the report body. Called from
 *  _loadReport AFTER the plain table painted and was wired — replacing
 *  innerHTML discards those bindings with their nodes, so nothing
 *  double-fires. A fetch failure leaves the plain table standing. */
async function _accessionAttach(d, isCurrent) {
    try {
        const r = await secureFetch('/api/accession/rows');
        if (!isCurrent()) return;
        if (!r.ok) return;
        const a = await r.json();
        if (!isCurrent()) return;
        if (_acc.scanToken !== _lastScanFinishedAt) {
            _acc.scanToken = _lastScanFinishedAt;
            _acc.decided = new Map();
            _acc.undo = [];
            _acc.linkSel = new Set();
            _acc.accSel = new Set();
            _acc.seeded = false;
            _acc.idx = 0;
            _acc.sel = 0;
        }
        _acc.enabled = !!a.enabled;
        _acc.batchMax = a.batch_max || 50;
        _acc.sources = Array.isArray(a.sources) ? a.sources : null;
        _acc.emailSet = !!a.contact_email_set;
        _acc.rows = new Map((a.rows || []).map(row => [row.pdf.rel, row]));
        _acc.report = d;
        _accSeedSelections();
        _accRenderBody();
    } catch (e) { logError(e); }
}

/** Default-check NEW bulk-tier rows exactly once per appearance — a user's
 *  untick must survive re-renders, so seeding never re-adds a rel that was
 *  seen before (seeded flag per snapshot; identify results seed as they
 *  arrive via _accApplyRow). */
function _accSeedSelections() {
    if (_acc.seeded) return;
    for (const row of _acc.rows.values()) {
        _accSeedRow(row);
    }
    _acc.seeded = true;
}

function _accSeedRow(row) {
    const res = row.resolution;
    if (!res || _acc.decided.has(row.pdf.rel)) return;
    if (!_ACC_PRECHECK_TIERS.includes(res.tier)) return;
    if (res.outcome === 'link' && res.citation_key) {
        // Never pre-check a key this PDF was unmapped from — bulk Link would
        // otherwise silently re-lift the exclude (2026-09-11).
        if (_accKeyIsExcluded(row, res.citation_key)) return;
        _acc.linkSel.add(row.pdf.rel);
    } else if (res.outcome === 'candidates' && res.doi) {
        _acc.accSel.add(row.pdf.rel);
    }
}

/** True when linking *key* would rematch a prior detach exclusion. */
function _accKeyIsExcluded(row, key) {
    const ex = (row && Array.isArray(row.excluded_keys)) ? row.excluded_keys : [];
    return ex.includes('*') || (!!key && ex.includes(key));
}

function _accRes(rel) {
    const row = _acc.rows.get(rel);
    return row ? row.resolution : null;
}

/** Rows for the two review lists: [linkRows, accessionRows]. */
function _accListRows() {
    const links = [];
    const accs = [];
    for (const r of (_acc.report ? _acc.report.rows : [])) {
        const rel = r.pdf.rel;
        if (_acc.decided.has(rel)) continue;
        const res = _accRes(rel);
        if (!res || !_ACC_LIST_TIERS.includes(res.tier)) continue;
        if (res.outcome === 'link' && res.citation_key) links.push(r);
        else if (res.outcome === 'candidates' && res.doi) accs.push(r);
    }
    return [links, accs];
}

/** Rows the deck reviews: everything below the bulk lists — unresolved,
 *  none, ambiguous, and title_only / filename_only candidates. Decided rows
 *  drop out (their outcome shows in the table view). */
function _accDeckRows() {
    const out = [];
    for (const r of (_acc.report ? _acc.report.rows : [])) {
        const rel = r.pdf.rel;
        if (_acc.decided.has(rel)) continue;
        const res = _accRes(rel);
        if (res && _ACC_LIST_TIERS.includes(res.tier)
            && (res.citation_key || res.doi)) continue;
        out.push(r);
    }
    return out;
}

function _accTierLabel(tier) {
    return ({
        bib_doi: 'DOI — in your bib',
        xmp_doi: 'embedded DOI',
        printed_doi: 'page-1 DOI',
        title_filename: 'title + filename',
        title_only: 'title guess — verify',
        filename_only: 'surname search',
        ambiguous: 'ambiguous',
    })[tier] || (tier || '');
}

const _ACC_SOURCE_LABELS = {
    crossref: 'Crossref', europepmc: 'Europe PMC', arxiv: 'arXiv',
    hal: 'HAL', thesesfr: 'theses.fr',
};

function _accSourceLabel(s) {
    return _ACC_SOURCE_LABELS[s] || s || '';
}

/** Catalog titles arrive raw — JATS `<i>` markup, HTML entities, ALL-CAPS,
 *  Crossref records whose subtitle was never fetched ("Title:" stubs) — and
 *  the old `slice()` cut mid-word with no ellipsis. Cleaned for DISPLAY only,
 *  never for matching; running client-side means every already-cached
 *  resolution benefits immediately (2026-08-19 review, titles lane). The
 *  result is still `_esc`'d at every render site. */
function _accCleanTitle(t, max) {
    let s = String(t || '');
    s = s.replace(/<[^>]*>/g, ' ');
    s = s.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
        .replace(/&quot;/g, '"').replace(/&#(\d+);/g,
            (_, n) => String.fromCharCode(Number(n)));
    s = s.replace(/\s+/g, ' ').trim().replace(/[\s:;,–-]+$/, '');
    if (max && s.length > max) s = `${s.slice(0, max - 1).trimEnd()}…`;
    return s;
}

/** Provenance the payload has always carried and the UI used to drop
 *  (2026-08-19 review): which source answered, the title-match score, the
 *  offline-bib zero-egress case, and whether the answer was re-served from
 *  the accession cache. Returned as display bits for the tier line. */
function _accProvenanceBits(res) {
    const bits = [];
    const win = (res.candidates || [])[0];
    if (win && win.source) bits.push(`via ${_accSourceLabel(win.source)}`);
    const score = (res.scores || [])[0];
    if ((res.tier === 'title_filename' || res.tier === 'title_only')
        && typeof score === 'number' && score > 0) {
        bits.push(`match ${score.toFixed(2)}`);
    }
    // The offline bib fast path consults no source and is never cached —
    // the one resolution shape that provably cost zero egress. (`consulted`
    // is additive 2026-08-19; older cache entries lack it, hence the
    // candidates fallback.)
    if (res.tier === 'bib_doi' && !res.from_cache
        && !(res.consulted || []).length && !(res.candidates || []).length) {
        bits.push('resolved offline from your bib');
    }
    if (res.from_cache) {
        bits.push(`cached${_accAge(res.queried_at)}`);
    }
    return bits;
}

/** " · 3 d ago" from the cache entry's queried_at epoch (absent on entries
 *  older than the field, and on fresh identify responses). */
function _accAge(queriedAt) {
    if (typeof queriedAt !== 'number' || !Number.isFinite(queriedAt)) return '';
    const s = Math.max(0, Date.now() / 1000 - queriedAt);
    if (s < 3600) return ` ${Math.max(1, Math.round(s / 60))} min ago`;
    if (s < 86400) return ` ${Math.round(s / 3600)} h ago`;
    return ` ${Math.round(s / 86400)} d ago`;
}

/** "asked Crossref, Europe PMC" — the consulted set the engine records
 *  (additive 2026-08-19; absent on older cache entries). */
function _accConsultedText(res) {
    const c = (res && res.consulted) || [];
    if (!c.length) return '';
    return `asked ${c.map(_accSourceLabel).join(', ')}`;
}

/** "⚠ N source note(s)" badge — a degraded resolution (a source 400'd or was
 *  skipped on budget) must be visually distinct from a genuine no-match,
 *  because degraded answers are cached and only [re-identify] retries them. */
function _accNotesBadge(res) {
    const notes = (res && res.notes) || [];
    if (!notes.length) return '';
    return ` <span class="audit-error-tag" title="${_attr(notes.join('; '))}">⚠ ${notes.length} source note(s)</span>`;
}

/** Tiers whose evidence is an IDENTIFIER — the bulk-select boundary. The
 *  chip is tinted for exactly these, so "an identifier resolved this" is
 *  legible per row instead of only stated once in the rule line. */
const _ACC_ID_TIERS = new Set(['bib_doi', 'xmp_doi', 'printed_doi', 'isbn']);

/** Evidence grade as a CHIP, provenance detail as the muted run after it.
 *  Before the 2026-08-19 UX pass both were one joined muted string, in which
 *  the grade (what a bulk decision hangs on) carried no more weight than the
 *  cache age. The consulted-source tooltip stays on the muted run when there
 *  is one — it is provenance, not grade — and falls back to the chip when a
 *  resolution has no provenance bits at all. */
function _accTierLine(res) {
    const label = _accTierLabel(res.tier);
    const bits = _accProvenanceBits(res).filter(Boolean);
    const consulted = _accConsultedText(res);
    const tone = _ACC_ID_TIERS.has(res.tier) ? ' chip-id'
        : (res.tier === 'bib_doi' ? '' : ' chip-weak');
    const chipTitle = (!bits.length && consulted) ? ` title="${_attr(consulted)}"` : '';
    const chip = label
        ? `<span class="audit-chip${tone}"${chipTitle}>${_esc(label)}</span>`
        : '';
    if (!bits.length) return chip;
    return `${chip} <span class="muted"${consulted ? ` title="${_attr(consulted)}"` : ''}>${_esc(bits.join(' · '))}</span>`;
}

function _accRenderBody() {
    const body = document.getElementById('audit-report-body');
    if (!body || _currentReport !== 'read_unzoterod' || !_acc.report) return;
    _closeKeyPicker(false);
    auditAccTeardown();
    const view = _accViewMode();
    body.innerHTML = _accPanelHtml(view)
        + _accReviewListsHtml()
        + (view === 'deck' ? _accDeckShell() : _accTableHtml());
    _wireRowActions();
    // The deck mounts and focuses itself (_accMount); only the table view's
    // focus has to be put back by hand.
    if (view !== 'deck') _accRestoreFocus();
}

function _accPanelHtml(view) {
    const total = _acc.report.rows.length;
    let resolved = 0;
    for (const row of _acc.rows.values()) if (row.resolution) resolved += 1;
    const unresolved = _accUnresolvedRels();
    const n = Math.min(unresolved.length, _acc.batchMax);
    // "looked up" (a query ran) vs "linked / marked" (a mapping decision was
    // written): the old identified/resolved pair read as synonyms.
    const batchLabel = unresolved.length > n
        ? `Identify next ${n} of ${unresolved.length} unresolved`
        : `Identify ${n} unresolved PDF(s)`;
    // The egress disclosure names the CONFIGURED source subset the server
    // reports, never a hard-coded five-source string (accession_sources can
    // narrow the real set), and discloses the Crossref contact email.
    const srcNames = (_acc.sources && _acc.sources.length
        ? _acc.sources : Object.keys(_ACC_SOURCE_LABELS))
        .map(_accSourceLabel).join(', ');
    const emailNote = _acc.emailSet
        ? ' Your configured contact email rides the Crossref requests (polite pool).'
        : '';
    // The egress statement is a DISCLOSURE, not a paragraph: the sentence that
    // actually bounds the risk stays on one line, and the reference detail
    // (which catalogs, the polite-pool email, the bib-first rule) collapses
    // behind a <details>. Nothing was cut — before the 2026-08-19 UX pass all
    // five sentences sat in one muted run above the buttons, which is why the
    // panel read as a wall on every visit.
    return `<div id="audit-acc-panel" class="audit-acc-panel">
        <div class="audit-acc-head">
            <label><input type="checkbox" id="audit-acc-enabled"${_acc.enabled ? ' checked' : ''}>
                Identify sources online</label>
            <span class="muted audit-acc-egress">Only a DOI, or a title and one
                surname, ever leaves this machine — never the PDF or its text.</span>
            <details class="audit-acc-disclosure">
                <summary>What it sends, and where</summary>
                <p class="muted">Looks orphans up in ${_esc(srcNames)} (all keyless).
                    Runs only when you press Identify, never during a scan.${_esc(emailNote)}
                    Your own bibliography is always checked first, offline — a hit never touches the network.</p>
            </details>
        </div>
        <div class="audit-acc-actions">
            <button type="button" class="audit-btn-primary" id="audit-acc-batch"${(!_acc.enabled || !n || _acc.busy) ? ' disabled' : ''}>
                ${batchLabel}</button>
            ${(_acc.busy && _acc.abort)
        ? '<button type="button" class="audit-link-btn" id="audit-acc-batch-cancel">[cancel]</button>'
        : ''}
            <button type="button" class="audit-link-btn" id="audit-acc-view">[${view === 'deck' ? 'ranked table' : 'review deck'}]</button>
            <span class="muted">${resolved} of ${total} looked up · ${_acc.decided.size} linked / marked this session</span>
        </div>
        <div class="audit-acc-status-row">
            <p id="audit-acc-status" class="muted"></p>
            ${(_acc.lastStatus.isError && _acc.lastStatus.msg)
        ? '<button type="button" class="audit-link-btn" id="audit-acc-status-dismiss">[dismiss error]</button>'
        : ''}
        </div>
    </div>`;
}

function _accUnresolvedRels() {
    const out = [];
    for (const r of (_acc.report ? _acc.report.rows : [])) {
        const row = _acc.rows.get(r.pdf.rel);
        if (row && !row.resolution && !_acc.decided.has(r.pdf.rel)) out.push(r.pdf.rel);
    }
    return out;
}

function _accReviewListsHtml() {
    const [links, accs] = _accListRows();
    if (!links.length && !accs.length) return '';
    let html = '<div id="audit-acc-review">';
    // The default-check rule is the feature's central safety promise; until
    // the 2026-08-19 review it lived only in a code comment, so a
    // mixed-checked list read as random.
    const rule = '<p class="muted audit-acc-rule">Rows backed by a DOI are '
        + 'pre-checked. Title-based matches are listed unchecked — confirm '
        + 'them by eye before ticking.</p>';
    if (links.length) {
        const rows = links.map(r => {
            const rel = r.pdf.rel;
            const res = _accRes(rel);
            const row = _acc.rows.get(rel);
            const excluded = _accKeyIsExcluded(row, res && res.citation_key);
            const win = (res.candidates || [])[0] || {};
            const matched = win.title
                ? `${_esc(_accCleanTitle(win.title, 60))}${win.year ? ` (${_esc(win.year)})` : ''}`
                : '<span class="muted">offline DOI match — no external record fetched</span>';
            const badge = excluded
                ? ' <span class="audit-chip" title="Unmapped from this key — will not bulk-link">unmapped</span>'
                : '';
            const checked = !excluded && _acc.linkSel.has(rel);
            const disabled = excluded ? ' disabled' : '';
            const title = excluded
                ? ' title="Unmapped from this key — use Link anyway on the row to rematch"'
                : '';
            return `<tr${excluded ? ' class="audit-acc-excluded"' : ''}>
                <td><input type="checkbox" data-acc-link-sel="${_attr(rel)}"${checked ? ' checked' : ''}${disabled}${title}
                     aria-label="Link ${_attr(r.pdf.name)} to ${_attr(res.citation_key)}"></td>
                <td>${_esc(r.pdf.name)}</td>
                <td>→ ${_esc(res.citation_key)}${badge}${excluded
                    ? ` <button type="button" class="audit-link-btn" data-acc-verb="link-anyway" data-rel="${_attr(rel)}">[link anyway]</button>`
                    : ''}</td>
                <td class="cell-title" title="${_attr(win.title || '')}">${matched}</td>
                <td class="muted">${_accTierLine(res)}${_accNotesBadge(res)}</td>
            </tr>`;
        }).join('');
        html += `<div class="audit-acc-list-head">
            <h4>Re-links <span class="audit-chip">${links.length}</span></h4>
            <span class="muted">already in your bib, file unlinked</span>
            <span>
                <button type="button" class="audit-link-btn" data-acc-selall="link:1">[all]</button>
                <button type="button" class="audit-link-btn" data-acc-selall="link:0">[none]</button>
            </span></div>
            ${rule}
            <table class="audit-table"><thead><tr><th scope="col"></th><th scope="col">PDF</th>
            <th scope="col">Bib entry</th><th scope="col">Matched title</th><th scope="col">Evidence</th></tr></thead>${_tbodyHtml(rows)}</table>
            <div class="audit-acc-actions">
                <button type="button" class="audit-btn-primary" id="audit-acc-link-btn"${(_acc.busy || !_acc.linkSel.size) ? ' disabled' : ''}>
                    Link selected (${_acc.linkSel.size})</button>
                <span class="muted">writes mapping.json only — each row stays undoable from the table afterwards</span>
            </div>`;
    }
    if (accs.length) {
        const rows = accs.map(r => {
            const rel = r.pdf.rel;
            const res = _accRes(rel);
            const win = (res.candidates || [])[0] || {};
            return `<tr>
                <td><input type="checkbox" data-acc-acc-sel="${_attr(rel)}"${_acc.accSel.has(rel) ? ' checked' : ''}
                     aria-label="Copy the identifier of ${_attr(r.pdf.name)}"></td>
                <td>${_esc(r.pdf.name)}</td>
                <td class="cell-title" title="${_attr(win.title || '')}">${_esc(_accCleanTitle(win.title, 80))}${win.year ? ` (${_esc(win.year)})` : ''}</td>
                <td class="mono">${_esc(res.doi || '')}</td>
                <td class="muted">${_accTierLine(res)}${_accNotesBadge(res)}</td>
            </tr>`;
        }).join('');
        html += `<div class="audit-acc-list-head">
            <h4>Accessions <span class="audit-chip">${accs.length}</span></h4>
            <span class="muted">not in your bib; add via Zotero’s “Add Item by Identifier”</span>
            <span>
                <button type="button" class="audit-link-btn" data-acc-selall="acc:1">[all]</button>
                <button type="button" class="audit-link-btn" data-acc-selall="acc:0">[none]</button>
            </span></div>
            ${rule}
            <table class="audit-table"><thead><tr><th scope="col"></th><th scope="col">PDF</th>
            <th scope="col">Best match</th><th scope="col">DOI</th><th scope="col">Evidence</th></tr></thead>${_tbodyHtml(rows)}</table>
            <div class="audit-acc-actions">
                <button type="button" class="audit-btn-primary" id="audit-acc-copy-btn"${_acc.accSel.size ? '' : ' disabled'}>
                    Copy ${_acc.accSel.size} identifier(s)</button>
                <span class="muted">copies to the clipboard only — nothing is written</span>
            </div>`;
    }
    return html + '</div>';
}

function _accTableHtml() {
    const head = '<thead><tr><th scope="col">Annot</th><th scope="col">Path</th>'
        + '<th scope="col">Identification</th><th scope="col"></th></tr></thead>';
    const rows = _acc.report.rows.map(r => `
        <tr>
            <td>${r.annotations}${r.error ? ` <span class="audit-error-tag">(${_esc(r.error)})</span>` : ''}</td>
            <td>${_esc(r.pdf.rel)}</td>
            <td data-acc-cell="${_attr(r.pdf.rel)}">${_accCellHtml(r.pdf.rel)}</td>
            <td>
                <button type="button" class="audit-link-btn" data-action="open" data-abs="${_attr(r.pdf.abs)}">[open]</button>
                <button type="button" class="audit-link-btn" data-action="reveal" data-abs="${_attr(r.pdf.abs)}">[reveal]</button>
            </td>
        </tr>`).join('');
    return `<table class="audit-table">${head}${_tbodyHtml(rows)}</table>`;
}

function _accCellHtml(rel) {
    const prior = _acc.decided.get(rel);
    if (prior) {
        const undoBtn = ` <button type="button" class="audit-link-btn" data-acc-verb="undo-one" data-rel="${_attr(rel)}">[undo]</button>`;
        return (prior.action === 'no_match'
            ? 'not in bib (this session — the row leaves at the next scan)'
            : `linked → ${_esc(prior.label)} (this session)`) + undoBtn;
    }
    const row = _acc.rows.get(rel);
    const res = _accRes(rel);
    // A resolved row's verb is a FORCED re-identify: degraded resolutions
    // (a flaky source mid-batch) are cached like any other, so a plain
    // re-run would return the cached failure forever.
    const idBtn = _acc.enabled
        ? ` <button type="button" class="audit-link-btn" data-acc-verb="identify"${res ? ' data-acc-force="1"' : ''} data-rel="${_attr(rel)}">[${res ? 're-identify' : 'identify'}]</button>`
        : '';
    // The manual escape hatch, offered on every row the ladder did NOT resolve
    // to a bib key: an entry can be in the bibliography under a spelling no
    // rung catches (a transliterated surname), and until this verb existed the
    // only way to link it was to leave the app.
    const pickBtn = ` <button type="button" class="audit-link-btn" data-acc-verb="link-to" data-rel="${_attr(rel)}" aria-label="${_attr(`Search the bibliography and link ${rel} to an entry`)}">[link to…]</button>`;
    if (!res) {
        // A batch per-PDF crash carries a row-level `error` with no
        // resolution — distinct from never-attempted (2026-08-19 review).
        if (row && row.error) {
            return `<span class="audit-error-tag">lookup failed: ${_esc(row.error)}</span>${idBtn}${pickBtn}`;
        }
        return _acc.enabled
            ? `<span class="muted">—</span>${idBtn}${pickBtn}`
            : `<span class="muted">— enable “Identify sources online” above to look this up</span>${pickBtn}`;
    }
    const tier = _accTierLine(res);
    const notes = _accNotesBadge(res);
    if (res.outcome === 'link' && res.citation_key) {
        return `→ ${_esc(res.citation_key)} · ${tier}${notes}
            <button type="button" class="audit-link-btn" data-acc-verb="link" data-rel="${_attr(rel)}">[link]</button>`;
    }
    if (res.outcome === 'candidates' && res.candidates && res.candidates.length) {
        const doiBtn = res.doi
            ? ` <button type="button" class="audit-link-btn" data-acc-verb="copy" data-rel="${_attr(rel)}">[copy DOI]</button>`
            : '';
        // A surname search asserts NO winner (scores are 0.0) — leading with
        // an arbitrary same-surname paper's title made 48% of the cached
        // rows read as confident wrong identifications (2026-08-19 review).
        if (res.tier === 'filename_only') {
            return `<span class="muted">${res.candidates.length} same-surname candidate(s) — review in the deck</span> · ${tier}${notes}${idBtn}${pickBtn}`;
        }
        const win = res.candidates[0];
        const maybe = res.tier === 'title_only' ? 'maybe: ' : '';
        return `${maybe}${_esc(_accCleanTitle(win.title, 70))}${win.year ? ` (${_esc(win.year)})` : ''} · ${tier}${notes}${doiBtn}${pickBtn}`;
    }
    if (res.outcome === 'ambiguous') {
        return `${(res.candidates || []).length} close matches — use the review deck · ${tier}${notes}${idBtn}${pickBtn}`;
    }
    return `<span class="muted">no match found</span>${notes}${idBtn}${pickBtn}`;
}

function _accUpdateTableCell(rel) {
    const cell = document.querySelector(`[data-acc-cell="${CSS.escape(rel)}"]`);
    if (cell) {
        cell.innerHTML = _accCellHtml(rel);
        applyActionRoles(cell);
        cell.querySelectorAll('[data-acc-verb]').forEach(btn => {
            btn.addEventListener('click', () => _accVerbClick(btn));
        });
    }
}

function _accSay(msg, isError, tone) {
    // Recorded in state as well as painted: most callers re-render the whole
    // body right after their final say (their finally), and a status line
    // that only lived in the replaced DOM vanished with it. The stamp rides
    // the record so a repaint reports when the outcome HAPPENED, not when the
    // body was last rebuilt.
    _acc.lastStatus = { msg: msg || '', isError: !!isError, at: msg ? Date.now() : null,
        tone: tone || (isError ? 'error' : 'info') };
    _accRepaintStatus();
    if (isError && msg) announceError(msg);
}

/** Repaint the persisted accession status from state. Called by `_accSay` and
 *  again after every render (`_wireRowActions`), because the panel template
 *  mints an EMPTY host — one painter, so the stamp cannot disagree with it. */
function _accRepaintStatus() {
    const el = document.getElementById('audit-acc-status');
    if (!el) return;
    const st = _acc.lastStatus;
    statusLine(el, { tone: st.tone || (st.isError ? 'error' : 'info'), text: st.msg, at: st.at });
}

/** Panel + review-list + table-verb wiring. Called from _wireRowActions when
 *  the panel is in the DOM (fresh innerHTML each render — no double binds). */
function _accWirePanel() {
    const toggle = document.getElementById('audit-acc-enabled');
    if (toggle) {
        toggle.addEventListener('change', () => _accToggleEnabled(toggle));
    }
    const view = document.getElementById('audit-acc-view');
    if (view) {
        view.addEventListener('click', () => {
            _accFocusAfter(['#audit-acc-view']);
            _accSetView(_accViewMode() === 'deck' ? 'table' : 'deck');
        });
    }
    const batch = document.getElementById('audit-acc-batch');
    if (batch) {
        batch.addEventListener('click', () => {
            const rels = _accUnresolvedRels().slice(0, _acc.batchMax);
            if (!rels.length || _acc.busy) return;
            confirmInline(batch, {
                message: `Look up ${rels.length} PDF(s) online now? Sends one or `
                    + 'a few short DOI / title+surname queries per PDF to the '
                    + 'keyless sources (paced politely — expect a few seconds each).',
                confirmLabel: 'Identify',
                onConfirm: () => {
                    _accFocusAfter(['#audit-acc-batch-cancel', '#audit-acc-batch']);
                    _accRunBatch(rels);
                },
            });
        });
    }
    const cancelBtn = document.getElementById('audit-acc-batch-cancel');
    if (cancelBtn) {
        // Aborting the SSE fetch closes the stream; the server's worker sees
        // its cancel event between PDFs and flushes the memos, so everything
        // already identified stays cached. No confirm: cancelling stops
        // egress, it never writes.
        cancelBtn.addEventListener('click', () => {
            if (_acc.abort) _acc.abort.abort();
        });
    }
    const dismissError = document.getElementById('audit-acc-status-dismiss');
    if (dismissError) {
        dismissError.addEventListener('click', () => {
            _acc.lastStatus = { msg: '', isError: false, at: null, tone: 'info' };
            _accRepaintStatus();
            dismissError.remove();
            const next = document.getElementById('audit-acc-batch')
                || document.getElementById('audit-acc-view');
            if (next) next.focus();
        });
    }
    const linkBtn = document.getElementById('audit-acc-link-btn');
    if (linkBtn) {
        linkBtn.addEventListener('click', () => {
            const n = _acc.linkSel.size;
            if (!n || _acc.busy) return;
            // Bulk Link never rematches an excluded key — drop any that slipped
            // in (e.g. a stale selection) and refuse if nothing remains.
            const blocked = [..._acc.linkSel].filter(rel => {
                const row = _acc.rows.get(rel);
                const res = _accRes(rel);
                return _accKeyIsExcluded(row, res && res.citation_key);
            });
            for (const rel of blocked) _acc.linkSel.delete(rel);
            if (blocked.length) _accRefreshListButtons();
            const remain = _acc.linkSel.size;
            if (!remain) {
                _accSay('Those re-links were previously unmapped from that key. '
                    + 'Use [link anyway] on a row to rematch deliberately.', true);
                return;
            }
            confirmInline(linkBtn, {
                message: `Link ${remain} PDF(s) to their bib entries? Writes the `
                    + 'audit’s mapping.json only — nothing touches the vault or '
                    + 'Zotero, and each row stays undoable from the table afterwards.'
                    + (blocked.length
                        ? ` (${blocked.length} previously-unmapped row(s) skipped.)`
                        : ''),
                confirmLabel: 'Link',
                onConfirm: () => {
                    _accFocusAfter(['#audit-acc-link-btn', '#audit-acc-batch']);
                    _accLinkSelected();
                },
            });
        });
    }
    const copyBtn = document.getElementById('audit-acc-copy-btn');
    if (copyBtn) {
        copyBtn.addEventListener('click', () => _accCopySelected());
    }
    document.querySelectorAll('[data-acc-link-sel]').forEach(cb => {
        cb.addEventListener('change', () => {
            const rel = cb.getAttribute('data-acc-link-sel');
            if (cb.checked) _acc.linkSel.add(rel); else _acc.linkSel.delete(rel);
            _accRefreshListButtons();
        });
    });
    document.querySelectorAll('[data-acc-acc-sel]').forEach(cb => {
        cb.addEventListener('change', () => {
            const rel = cb.getAttribute('data-acc-acc-sel');
            if (cb.checked) _acc.accSel.add(rel); else _acc.accSel.delete(rel);
            _accRefreshListButtons();
        });
    });
    document.querySelectorAll('[data-acc-verb]').forEach(btn => {
        btn.addEventListener('click', () => _accVerbClick(btn));
    });
    document.querySelectorAll('[data-acc-selall]').forEach(btn => {
        btn.addEventListener('click', () => {
            const raw = btn.getAttribute('data-acc-selall');
            const [which, on] = raw.split(':');
            _accFocusAfter([`[data-acc-selall="${_selQ(raw)}"]`]);
            _accSelectAll(which, on === '1');
        });
    });
}

/** Select-all/none over one review list (the plan's "review list with
 *  select-all"). Operates on the LISTED rows only — [all] can tick
 *  title_filename rows too, which is an explicit user act, unlike the
 *  identifier-only default seeding. */
function _accSelectAll(which, on) {
    const [links, accs] = _accListRows();
    const rows = which === 'link' ? links : accs;
    const set = which === 'link' ? _acc.linkSel : _acc.accSel;
    for (const r of rows) {
        if (which === 'link') {
            const row = _acc.rows.get(r.pdf.rel);
            const res = _accRes(r.pdf.rel);
            if (_accKeyIsExcluded(row, res && res.citation_key)) {
                set.delete(r.pdf.rel);
                continue;
            }
        }
        if (on) set.add(r.pdf.rel); else set.delete(r.pdf.rel);
    }
    _accRenderBody();
}

function _accRefreshListButtons() {
    const linkBtn = document.getElementById('audit-acc-link-btn');
    if (linkBtn) {
        linkBtn.disabled = _acc.busy || !_acc.linkSel.size;
        linkBtn.textContent = `Link selected (${_acc.linkSel.size})`;
    }
    const copyBtn = document.getElementById('audit-acc-copy-btn');
    if (copyBtn) {
        copyBtn.disabled = !_acc.accSel.size;
        copyBtn.textContent = `Copy ${_acc.accSel.size} identifier(s)`;
    }
}

async function _accToggleEnabled(toggle) {
    const want = !!toggle.checked;
    try {
        const r = await secureFetch('/api/config', {
            method: 'POST', body: JSON.stringify({ accession_enabled: want }),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        _acc.enabled = want;
        _accFocusAfter(['#audit-acc-enabled']);
        _accRenderBody();
        announceStatus(want
            ? 'Online identification enabled.'
            : 'Online identification disabled.');
    } catch (e) {
        toggle.checked = !want;   // revert — the server kept the old value
        _accSay(`Could not save the setting: ${e}`, true);
    }
}

function _accVerbClick(btn) {
    const rel = btn.getAttribute('data-rel');
    const verb = btn.getAttribute('data-acc-verb');
    if (!rel || !verb) return;
    if (verb === 'identify') {
        _accFocusAfter(_accRowFocus(rel));
        _accIdentifyOne(rel, btn.getAttribute('data-acc-force') === '1');
        return;
    }
    if (verb === 'copy') { _accCopyDois([rel]); return; }
    if (verb === 'link-to') {
        if (_acc.busy) return;
        const row = _acc.rows.get(rel);
        _openKeyPicker(btn, {
            pdfName: (row && row.pdf && row.pdf.name) || rel,
            onPick: (key) => {
                _accFocusAfter(_accRowFocus(rel));
                _accMappingWrite(rel, 'match', key, true);
            },
        });
        return;
    }
    if (verb === 'undo-one') {
        const prior = _acc.decided.get(rel);
        if (!prior || _acc.busy) return;
        confirmInline(btn, {
            message: `Undo “${prior.label}” for ${rel}?`,
            confirmLabel: 'Undo',
            onConfirm: () => { _accFocusAfter(_accRowFocus(rel)); _accUndoOne(rel); },
        });
        return;
    }
    if (verb === 'link') {
        const res = _accRes(rel);
        if (!res || !res.citation_key) return;
        const row = _acc.rows.get(rel);
        const excluded = _accKeyIsExcluded(row, res.citation_key);
        confirmInline(btn, {
            message: excluded
                ? `This PDF was unmapped from ${res.citation_key}. Link it again anyway?`
                : `Link this PDF to ${res.citation_key}?`,
            confirmLabel: excluded ? 'Link anyway' : 'Link',
            onConfirm: () => {
                _accFocusAfter(_accRowFocus(rel));
                _accMappingWrite(rel, 'match', res.citation_key, false, excluded);
            },
        });
        return;
    }
    if (verb === 'link-anyway') {
        const res = _accRes(rel);
        if (!res || !res.citation_key) return;
        confirmInline(btn, {
            message: `This PDF was unmapped from ${res.citation_key}. Link it again anyway? `
                + 'Writes mapping.json only; undoes the detach exclusion for that key.',
            confirmLabel: 'Link anyway',
            onConfirm: () => {
                _accFocusAfter(_accRowFocus(rel));
                _accMappingWrite(rel, 'match', res.citation_key, false, true);
            },
        });
    }
}

/** One identify — the table [identify] verb and the deck's I key. */
async function _accIdentifyStream(rel, force, onInfo) {
    let row = null;
    await consumeSSE('/api/accession/identify', {
        method: 'POST',
        body: JSON.stringify(force ? { pdf: rel, force: true } : { pdf: rel }),
        signal: _acc.abort.signal,
    }, {
        onInfo,
        onOther: (frame) => { if (frame && frame.row) row = frame.row; },
        onError: (err) => { throw new Error(err); },
    });
    if (!row) throw new Error('Identification ended without a result.');
    return row;
}

async function _accIdentifyOne(rel, force) {
    if (_acc.busy || !_acc.enabled) return;
    _acc.busy = true;
    _acc.abort = new AbortController();
    _accSay(`Identifying ${rel}…`);
    try {
        const d = await _accIdentifyStream(rel, force, (m) => _accSay(m));
        _accApplyRow(d);
        const line = _accOutcomeLine(d);
        _accSay(line);
        announceStatus(line);   // errors announce via _accSay; outcomes must too
    } catch (e) {
        if (e && e.name === 'AbortError') _accSay('Identification cancelled.');
        else _accSay(String(e), true);
    } finally {
        _acc.busy = false;
        _acc.abort = null;
        _accRenderBody();
    }
}

function _accOutcomeLine(row) {
    const res = row.resolution || {};
    const name = row.pdf ? row.pdf.name : '';
    const notes = (res.notes || []).length
        ? ` (${res.notes.length} source note(s))` : '';
    if (res.outcome === 'link') return `${name}: already in your bib → ${res.citation_key}.${notes}`;
    if (res.outcome === 'candidates') {
        if (res.tier === 'filename_only') {
            return `${name}: ${(res.candidates || []).length} same-surname candidate(s) — review in the deck.${notes}`;
        }
        const win = (res.candidates || [])[0] || {};
        const via = win.source ? ` — via ${_accSourceLabel(win.source)}` : '';
        return `${name}: ${_accTierLabel(res.tier)} — ${_accCleanTitle(win.title, 60)}${via}${notes}`;
    }
    if (res.outcome === 'ambiguous') return `${name}: two close matches — refused to guess.${notes}`;
    return `${name}: no match found.${notes}`;
}

/** Merge one resolved row into state (identify + batch frames). */
function _accApplyRow(row) {
    if (!row || !row.pdf || !row.pdf.rel) return;
    const prev = _acc.rows.get(row.pdf.rel);
    // Identify/batch frames may omit excluded_keys; keep the /rows value so
    // a re-identify cannot silently make an unmapped key look pre-approved.
    if (prev && Array.isArray(prev.excluded_keys)
        && !Array.isArray(row.excluded_keys)) {
        row.excluded_keys = prev.excluded_keys;
    }
    _acc.rows.set(row.pdf.rel, row);
    _accSeedRow(row);
}

async function _accRunBatch(rels) {
    if (_acc.busy) return;
    _acc.busy = true;
    _acc.abort = new AbortController();
    _accRenderBody();   // repaint: batch button disabled, [cancel] offered
    let done = 0;
    let terminal = null;
    let terminalError = null;
    try {
        await consumeSSE('/api/accession/batch', {
            method: 'POST',
            body: JSON.stringify({ pdfs: rels, confirm: true }),
            signal: _acc.abort.signal,
        }, {
            onInfo: (m) => _accSay(m),
            onOther: (f) => {
                if (f && f.row) {
                    done += 1;
                    _accApplyRow(f.row);
                    _accUpdateTableCell(f.row.pdf.rel);
                    _accSay(`Identifying ${done}/${rels.length}: ${f.row.pdf.name}`);
                }
                if (f && f.accession) terminal = f.accession;
            },
            onError: (err, frame) => {
                if (frame && frame.accession) {
                    terminal = frame.accession;
                    terminalError = err;
                    return;
                }
                throw new Error(err);
            },
        });
        const o = (terminal && terminal.outcomes) || {};
        const bits = Object.keys(o).map(k => `${o[k]} ${k}`).join(' · ');
        const msg = `Identified ${done} of ${rels.length} PDF(s)${bits ? ` — ${bits}` : ''}.`
            + (terminal && terminal.cancelled ? ' (cancelled early)' : '')
            + (terminalError ? ` Stopped early: ${terminalError}` : '');
        _accSay(msg, !!terminalError);
        if (!terminalError) announceStatus(msg);
    } catch (e) {
        if (e && e.name === 'AbortError') {
            // User cancel, not a failure: the server stops between PDFs and
            // flushes its memos, so everything identified so far is kept.
            const msg = `Batch cancelled — ${done} of ${rels.length} `
                + 'identified; finished lookups stay cached.';
            _accSay(msg);
            announceStatus(msg);
        } else {
            _accSay(String(e), true);
        }
    } finally {
        _acc.busy = false;
        _acc.abort = null;
        _accRenderBody();
    }
}

/** Sequential mapping writes for the Link review list. */
async function _accLinkSelected() {
    if (_acc.busy) return;
    _acc.busy = true;
    const rels = [..._acc.linkSel];
    let ok = 0;
    const failures = [];
    try {
        for (let i = 0; i < rels.length; i++) {
            const rel = rels[i];
            const res = _accRes(rel);
            const row = _acc.rows.get(rel);
            if (!res || !res.citation_key) continue;
            // Bulk never sends accept_excluded — skip excluded keys (server
            // would 409 anyway).
            if (_accKeyIsExcluded(row, res.citation_key)) {
                _acc.linkSel.delete(rel);
                continue;
            }
            _accSay(`Linking ${i + 1}/${rels.length}…`);
            try {
                const r = await secureFetch('/api/audit/mapping', {
                    method: 'POST',
                    body: JSON.stringify({
                        pdf: rel, citation_key: res.citation_key,
                        source: 'accession', confirm: true,
                    }),
                });
                if (!r.ok) {
                    const d = await r.json().catch(() => ({}));
                    failures.push(`${rel}: ${d.error || r.status}`);
                    continue;
                }
                ok += 1;
                _acc.decided.set(rel, { action: 'match', label: res.citation_key });
                _acc.undo.push({ pdf: rel, label: res.citation_key });
                _acc.linkSel.delete(rel);
            } catch (e) {
                failures.push(`${rel}: ${e}`);
            }
        }
        let msg = `Linked ${ok} of ${rels.length} PDF(s). Rows refresh fully on the next scan.`;
        if (failures.length) {
            msg += ` Failures: ${failures.slice(0, 3).join('; ')}`
                + (failures.length > 3 ? '…' : '');
        }
        _accSay(msg, !!failures.length);
        announceStatus(msg);
    } finally {
        _acc.busy = false;
        _accRenderBody();   // lastStatus re-paints the final message
    }
}

function _accCopySelected() {
    _accCopyDois([..._acc.accSel]);
}

/** Clipboard-refusal fallback (the tagging `_pickCopy` pattern): a sandboxed
 *  webview can refuse `navigator.clipboard`, and the Accessions workflow IS
 *  "copy these identifiers" — an error message with no way to get them
 *  dead-ends the feature. Shows the text selected in a read-only textarea
 *  under the panel instead. Returns false when the panel is absent. */
function _accShowCopyFallback(text) {
    const panel = document.getElementById('audit-acc-panel');
    if (!panel) return false;
    let box = document.getElementById('audit-acc-copy-export');
    if (!box) {
        box = document.createElement('textarea');
        box.id = 'audit-acc-copy-export';
        box.readOnly = true;
        box.rows = 4;
        box.style.width = '100%';
        box.setAttribute('aria-label', 'Identifiers — copy manually');
        panel.appendChild(box);
    }
    box.value = text;
    box.focus();
    box.select();
    return true;
}

async function _accCopyDois(rels) {
    const dois = [];
    for (const rel of rels) {
        const res = _accRes(rel);
        if (res && res.doi) dois.push(res.doi);
    }
    if (!dois.length) return;
    try {
        await navigator.clipboard.writeText(dois.join('\n'));
        const msg = `${dois.length} identifier(s) copied — paste into Zotero → `
            + 'File → Add Item by Identifier (the magic-wand button). They '
            + 'appear here as re-links after a Better BibTeX re-export and a rescan.';
        _accSay(msg);
        announceStatus(msg);
    } catch (e) {
        if (_accShowCopyFallback(dois.join('\n'))) {
            _accSay('Clipboard unavailable — the identifiers are selected '
                + 'below; copy them manually.', true);
        } else {
            _accSay(`Could not copy to the clipboard: ${e}`, true);
        }
    }
}

/** Body of a mapping write for a curated STATE (no_match | pending) or a
 *  link. `manual` omits the `source: "accession"` annotation on a link.
 *  `acceptExcluded` is the explicit rematch ack for a previously-unmapped key
 *  — bulk Link never sets it. */
function _mappingStateBody(rel, action, citationKey, manual, acceptExcluded) {
    if (action === 'no_match') return { pdf: rel, no_match: true, confirm: true };
    if (action === 'pending') return { pdf: rel, pending: true, confirm: true };
    const body = manual
        ? { pdf: rel, citation_key: citationKey, confirm: true }
        : { pdf: rel, citation_key: citationKey, source: 'accession', confirm: true };
    if (acceptExcluded) body.accept_excluded = true;
    return body;
}

function _mappingStateLabel(action, citationKey) {
    if (action === 'no_match') return 'not in bib';
    if (action === 'pending') return 'pending';
    return citationKey;
}

function _mappingStateDone(action, citationKey) {
    if (action === 'no_match') return 'Recorded as not a paper for Zotero — the row leaves this report at the next scan (listed under Not in Zotero).';
    if (action === 'pending') return 'Recorded as to add to Zotero later — the row leaves this report at the next scan (listed under Not in Zotero).';
    return `Linked to ${citationKey}. The row refreshes fully at the next scan.`;
}

/** The confirmed mapping write shared by list rows, table verbs and deck.
 *
 *  `manual` omits the `source: "accession"` annotation: a key the USER picked
 *  from the bibliography is hand curation, and crediting the resolver for it
 *  would make the pairings board's provenance column lie at the next scan. */
async function _accMappingWrite(rel, action, citationKey, manual, acceptExcluded) {
    if (_acc.busy) return;
    _acc.busy = true;
    _accSay('Applying…');
    const body = _mappingStateBody(rel, action, citationKey, manual, acceptExcluded);
    try {
        const r = await secureFetch('/api/audit/mapping', {
            method: 'POST', body: JSON.stringify(body),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { _accSay(d.error || `Mapping failed (${r.status})`, true); return; }
        _acc.decided.set(rel, { action, label: _mappingStateLabel(action, citationKey) });
        _accToday.bump();
        _acc.undo.push({ pdf: rel, label: _mappingStateLabel(action, citationKey) });
        _acc.linkSel.delete(rel);
        _acc.accSel.delete(rel);
        const msg = action !== 'match'
            ? _mappingStateDone(action, citationKey)
            : `Linked to ${citationKey}. The row refreshes fully at the next scan.`;
        _accSay(msg);
        announceStatus(msg);
    } catch (e) {
        _accSay(String(e), true);
    } finally {
        _acc.busy = false;
        _accRenderBody();
    }
}

/** Table-row undo — clears ANY decided rel, not just the deck stack's last
 *  (the bulk confirm promises "undoable per PDF"; until the 2026-08-19
 *  review the only undo lived in the deck and was LIFO). */
async function _accUndoOne(rel) {
    if (_acc.busy) return;
    _acc.busy = true;
    _accSay('Undoing…');
    try {
        const r = await secureFetch('/api/audit/mapping', {
            method: 'POST', body: JSON.stringify({ pdf: rel, clear: true, confirm: true }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { _accSay(d.error || `Undo failed (${r.status})`, true); return; }
        _acc.decided.delete(rel);
        _acc.undo = _acc.undo.filter(u => u.pdf !== rel);
        // An identifier-grade row re-enters its review list CHECKED — the
        // system removed that tick on write, so the system restores it.
        const row = _acc.rows.get(rel);
        if (row) _accSeedRow(row);
        const msg = `Undone — “${rel}” is un-curated again.`;
        _accSay(msg);
        announceStatus(msg);
    } catch (e) {
        _accSay(String(e), true);
    } finally {
        _acc.busy = false;
        _accRenderBody();
    }
}

// ── the accession review deck ──────────────────────────────────────────────

function _accDeckShell() {
    const n = _accDeckRows().length;
    return `<div id="audit-acc-deck" class="audit-ambig-deck" tabindex="0"
                 role="group" aria-label="Accession review deck">
        <p id="audit-acc-progress" class="audit-ambig-progress"></p>
        <p id="audit-acc-result" class="audit-ambig-result"></p>
        <div id="audit-acc-card" class="audit-ambig-card"></div>
        <div id="audit-acc-pdf" class="audit-ambig-pdf" style="display:none;"></div>
        <p class="audit-ambig-keys">${n} to review one-by-one (bulk-tier rows stay in the lists above) ·
            <kbd>I</kbd> identify · <kbd>1</kbd>–<kbd>9</kbd> select the candidate ·
            <kbd>A</kbd> link / copy its DOI · <kbd>L</kbd> link to a bib entry you pick ·
            <kbd>N</kbd> not in bib ·
            <kbd>S</kbd> skip · <kbd>←</kbd> back · <kbd>V</kbd> PDF · <kbd>⌫</kbd> undo
        </p>
    </div>`;
}

function _accMount() {
    const deck = document.getElementById('audit-acc-deck');
    if (!deck) return;
    deck.addEventListener('keydown', _accKeydown);
    _accRenderCard();
    deck.focus();
}

function _accDeckCurrent() {
    return _accDeckRows()[_acc.idx] || null;
}

function _accProgress() {
    const el = document.getElementById('audit-acc-progress');
    if (!el) return;
    const total = _accDeckRows().length;
    const bits = [`PDF ${Math.min(_acc.idx + 1, Math.max(total, 1))} of ${total}`,
        `${_acc.decided.size} resolved this session`];
    if (_acc.undo.length) bits.push(`${_acc.undo.length} undoable`);
    const today = _accToday.count();
    if (today) bits.push(`${today} decided today`);
    el.textContent = bits.join(' · ');
}

function _accDeckSay(msg, isError) {
    const el = document.getElementById('audit-acc-result');
    if (!el) return;
    el.textContent = msg || '';
    el.classList.toggle('is-error', !!isError);
    if (isError && msg) announceError(msg);
}

function _accRenderCard() {
    const card = document.getElementById('audit-acc-card');
    if (!card) return;
    _closeKeyPicker(false);   // it is anchored to a button this render destroys
    _accClosePdf();
    _accProgress();
    const row = _accDeckCurrent();
    if (!row) {
        const total = _accDeckRows().length;
        card.innerHTML = total === 0
            ? '<p class="audit-ambig-done">Nothing left to review one-by-one — every row is unresolved-with-no-text, bulk-listed above, or already decided.</p>'
            : `<p class="audit-ambig-done">All ${total} reviewed.</p>
               <div class="audit-ambig-actions">
                   <button type="button" class="audit-link-btn" data-acc-deck="restart">[review again]</button>
               </div>`;
        _accWireCard(card);
        announceStatus('Accession deck: nothing left to review.');
        return;
    }
    const rel = row.pdf.rel;
    const res = _accRes(rel);
    const cands = (res && res.candidates) || [];
    _acc.sel = Math.min(_acc.sel, Math.max(cands.length - 1, 0));
    let status;
    let statusTitle = '';
    if (!res) {
        status = _acc.enabled ? 'Not identified yet — press I to look it up.'
            : 'Not identified yet — enable “Identify sources online” above to look it up.';
    } else {
        const bits = [_accTierLabel(res.tier) || res.outcome,
            ..._accProvenanceBits(res)];
        const consulted = _accConsultedText(res);
        if (consulted) bits.push(consulted);
        if (res.tier === 'filename_only') {
            bits.push('no title evidence — same-surname candidates only');
        }
        if (res.outcome === 'ambiguous') {
            bits.push('two candidates scored within the margin; pick with your eyes');
        }
        if ((res.notes || []).length) {
            bits.push(`⚠ ${res.notes.length} source note(s) — hover for detail`);
            statusTitle = res.notes.join('; ');
        }
        status = bits.join(' · ');
    }
    const candHtml = cands.map((c, i) => `
        <li>
            <button type="button" class="audit-ambig-cand${i === _acc.sel ? ' is-sel' : ''}"
                    data-acc-deck="pick" data-i="${i}"
                    aria-pressed="${i === _acc.sel ? 'true' : 'false'}">
                <span class="audit-ambig-num">${i + 1}</span>
                <span class="audit-ambig-title">${_esc(_accCleanTitle(c.title, 110))}</span>
                <span class="muted">${_esc((c.authors || []).slice(0, 3).join('; '))}${c.year ? ` · ${_esc(c.year)}` : ''}${c.container ? ` · ${_esc(c.container)}` : ''}${c.doi ? ` · ${_esc(c.doi)}` : ''} · ${_esc(_accSourceLabel(c.source))}</span>
            </button>
        </li>`).join('');
    const primary = res && res.outcome === 'link' && res.citation_key
        ? `<button type="button" class="audit-btn-primary" data-acc-deck="link">Link to ${_esc(res.citation_key)}</button>`
        : (cands[_acc.sel] && cands[_acc.sel].doi
            ? '<button type="button" class="audit-btn-primary" data-acc-deck="copy">Copy DOI for Zotero</button>'
            : '');
    card.innerHTML = `
        <h4 class="audit-ambig-name">${_esc(row.pdf.name || rel)}</h4>
        <p class="audit-ambig-path" title="${_attr(rel)}">${_esc(rel)} · ${row.annotations} annotation(s)</p>
        <p class="muted"${statusTitle ? ` title="${_attr(statusTitle)}"` : ''}>${_esc(status)}</p>
        <ul class="audit-ambig-cands">${candHtml}</ul>
        <div class="audit-ambig-actions">
            ${primary}
            <button type="button" class="audit-link-btn" data-acc-deck="identify"${_acc.enabled ? '' : ' disabled'}>[identify]</button>
            <button type="button" class="audit-link-btn" data-acc-deck="link-to" aria-label="Search the bibliography and link this PDF to an entry">[link to…]</button>
            <button type="button" class="audit-link-btn" data-acc-deck="no-match" aria-label="Mark as not a paper for Zotero">[not in bib]</button>
            <button type="button" class="audit-link-btn" data-acc-deck="pending" aria-label="Mark as to add to Zotero later">[pending]</button>
            <button type="button" class="audit-link-btn" data-acc-deck="skip">[skip]</button>
            <button type="button" class="audit-link-btn" data-acc-deck="prev">[back]</button>
            <button type="button" class="audit-link-btn" data-acc-deck="pdf" aria-label="View the PDF">[view PDF]</button>
            <button type="button" class="audit-link-btn" data-action="reveal" data-abs="${_attr(row.pdf.abs)}" aria-label="Reveal the PDF in Finder">[reveal]</button>
            <button type="button" class="audit-link-btn" data-acc-deck="undo"${_acc.undo.length ? '' : ' disabled'}>[undo]</button>
        </div>`;
    _accWireCard(card);
    const total = _accDeckRows().length;
    announceStatus(`PDF ${Math.min(_acc.idx + 1, total)} of ${total}: ${row.pdf.name || rel}`);
    _accRefocusDeck();
}

function _accRefocusDeck() {
    const active = document.activeElement;
    if (active && active.closest && active.closest('.inline-confirm-strip')) return;
    const deck = document.getElementById('audit-acc-deck');
    if (deck) deck.focus();
}

function _accWireCard(card) {
    applyActionRoles(card);
    card.querySelectorAll('[data-acc-deck]').forEach(btn => {
        btn.addEventListener('click', () => _accDeckVerb(btn.getAttribute('data-acc-deck'), btn));
    });
    card.querySelectorAll('[data-action="reveal"]').forEach(btn => {
        btn.addEventListener('click', (e) => { e.preventDefault(); _ambigReveal(btn); });
    });
}

function _accDeckVerb(verb, btn) {
    const row = _accDeckCurrent();
    switch (verb) {
        case 'pick': {
            const i = parseInt(btn.getAttribute('data-i'), 10);
            if (Number.isFinite(i)) { _acc.sel = i; _accRenderCard(); }
            break;
        }
        case 'link': {
            if (!row) break;
            const res = _accRes(row.pdf.rel);
            if (!res || !res.citation_key) break;
            const excluded = _accKeyIsExcluded(row, res.citation_key);
            confirmInline(btn, {
                message: excluded
                    ? `“${row.pdf.name}” was unmapped from ${res.citation_key}. Link it again anyway?`
                    : `Link “${row.pdf.name}” to ${res.citation_key}? Writes the audit’s `
                      + 'mapping.json only — nothing touches the vault or Zotero. Undo: ⌫ on this deck.',
                confirmLabel: excluded ? 'Link anyway' : 'Link',
                onConfirm: () => _accDeckWrite('match', res.citation_key, excluded),
            });
            break;
        }
        case 'copy': {
            if (!row) break;
            const res = _accRes(row.pdf.rel);
            const cand = res && (res.candidates || [])[_acc.sel];
            const doi = (cand && cand.doi) || (res && res.doi);
            if (doi) {
                _accCopyOneDoi(doi);
            }
            break;
        }
        case 'identify': {
            if (row) _accDeckIdentify(row.pdf.rel);
            break;
        }
        case 'link-to': {
            if (!row || _acc.busy) break;
            _openKeyPicker(btn, {
                pdfName: row.pdf.name || row.pdf.rel,
                onPick: (key) => {
                    const accept = _accKeyIsExcluded(row, key);
                    if (accept) {
                        confirmInline(btn, {
                            message: `“${row.pdf.name}” was unmapped from ${key}. Link it again anyway?`,
                            confirmLabel: 'Link anyway',
                            onConfirm: () => _accMappingWriteFromDeck(
                                row.pdf.rel, 'match', key, _acc.idx, true, true),
                        });
                    } else {
                        _accMappingWriteFromDeck(
                            row.pdf.rel, 'match', key, _acc.idx, true);
                    }
                },
            });
            break;
        }
        case 'no-match': {
            if (!row) break;
            confirmInline(btn, {
                message: `Record “${row.pdf.name}” as not a paper for Zotero? Writes the audit’s `
                    + 'mapping.json curated queue; it is listed under Not in Zotero. '
                    + 'Undo: ⌫ on this deck, or [clear] there.',
                confirmLabel: 'Not in bib',
                onConfirm: () => _accDeckWrite('no_match', null),
            });
            break;
        }
        case 'pending': {
            // The second curated state (2026-09-06): "add to Zotero later".
            // Same exit from the queue as no_match, different intent, and
            // listed under Not in Zotero with the intent preserved.
            if (!row) break;
            confirmInline(btn, {
                message: `Record “${row.pdf.name}” as to add to Zotero later? Writes the audit’s `
                    + 'mapping.json curated queue (“pending”); it is listed under Not in Zotero. '
                    + 'Undo: ⌫ on this deck, or [clear] there.',
                confirmLabel: 'Pending',
                onConfirm: () => _accDeckWrite('pending', null),
            });
            break;
        }
        case 'skip': _accMove(1); break;
        case 'prev': _accMove(-1); break;
        case 'pdf': _accTogglePdf(); break;
        case 'undo': _accDeckUndo(btn); break;
        case 'restart': _acc.idx = 0; _accRenderCard(); break;
        default: break;
    }
}

async function _accCopyOneDoi(doi) {
    try {
        await navigator.clipboard.writeText(doi);
        _accDeckSay(`${doi} copied — paste into Zotero's Add Item by Identifier.`);
        announceStatus('Identifier copied.');
    } catch (e) {
        if (_accShowCopyFallback(doi)) {
            _accDeckSay('Clipboard unavailable — the identifier is selected '
                + 'in the panel above; copy it manually.', true);
        } else {
            _accDeckSay(`Could not copy: ${e}`, true);
        }
    }
}

async function _accDeckIdentify(rel) {
    if (_acc.busy) return;
    if (!_acc.enabled) {
        _accDeckSay('Enable “Identify sources online” in the panel above first.', true);
        return;
    }
    _acc.busy = true;
    _acc.abort = new AbortController();
    _accDeckSay('Identifying…');
    try {
        const force = !!_accRes(rel);   // resolved card ⇒ genuine re-look
        const d = await _accIdentifyStream(rel, force, (m) => _accDeckSay(m));
        _accApplyRow(d);
        const line = _accOutcomeLine(d);
        _accDeckSay(line);
        announceStatus(line);   // the card re-announce names the PDF, not the outcome
    } catch (e) {
        if (e && e.name === 'AbortError') _accDeckSay('Identification cancelled.');
        else _accDeckSay(String(e), true);
    } finally {
        _acc.busy = false;
        _acc.abort = null;
        // The row may have LEFT the deck (bulk tier) — clamp and re-render.
        _acc.idx = Math.min(_acc.idx, Math.max(_accDeckRows().length - 1, 0));
        _accRenderCard();
    }
}

async function _accDeckWrite(action, citationKey, acceptExcluded) {
    const row = _accDeckCurrent();
    if (!row || _acc.busy) return;
    const writtenIdx = _acc.idx;
    await _accMappingWriteFromDeck(
        row.pdf.rel, action, citationKey, writtenIdx, false, acceptExcluded);
}

/** Deck variant of the mapping write: advances only when the user is still
 *  parked on the card that was written (the ambig captured-index fix). */
async function _accMappingWriteFromDeck(rel, action, citationKey, writtenIdx, manual, acceptExcluded) {
    if (_acc.busy) return;
    _acc.busy = true;
    _accDeckSay('Applying…');
    const body = _mappingStateBody(rel, action, citationKey, manual, acceptExcluded);
    try {
        const r = await secureFetch('/api/audit/mapping', {
            method: 'POST', body: JSON.stringify(body),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { _accDeckSay(d.error || `Mapping failed (${r.status})`, true); return; }
        _acc.decided.set(rel, { action, label: _mappingStateLabel(action, citationKey) });
        _accToday.bump();
        _acc.undo.push({ pdf: rel, label: _mappingStateLabel(action, citationKey) });
        _accDeckSay(_mappingStateDone(action, citationKey));
        announceStatus(`Resolved ${rel}`);
        // The decided row leaves _accDeckRows(), so staying at writtenIdx
        // already shows the next card; only clamp when at the tail.
        if (_acc.idx === writtenIdx) _acc.sel = 0;
        _acc.idx = Math.min(_acc.idx, Math.max(_accDeckRows().length - 1, 0));
        _accRenderCard();
    } catch (e) {
        _accDeckSay(String(e), true);
    } finally {
        _acc.busy = false;
        _accProgress();
    }
}

function _accDeckUndo(btn) {
    if (_acc.busy || !_acc.undo.length) return;
    const last = _acc.undo[_acc.undo.length - 1];   // peek, not pop
    confirmInline(btn, {
        message: `Undo “${last.label}” for ${last.pdf}?`,
        confirmLabel: 'Undo',
        onConfirm: () => _accPerformUndo(),
    });
}

async function _accPerformUndo() {
    if (_acc.busy || !_acc.undo.length) return;
    const last = _acc.undo[_acc.undo.length - 1];   // peek-not-pop: a failed
    _acc.busy = true;                               // undo stays retryable
    _accDeckSay('Undoing…');
    try {
        const r = await secureFetch('/api/audit/mapping', {
            method: 'POST', body: JSON.stringify({ pdf: last.pdf, clear: true, confirm: true }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { _accDeckSay(d.error || `Undo failed (${r.status})`, true); return; }
        _acc.undo.pop();
        _acc.decided.delete(last.pdf);
        // Re-seed: an identifier-grade row re-enters its review list CHECKED
        // (the system removed that tick on write, so the system restores it).
        const undoneRow = _acc.rows.get(last.pdf);
        if (undoneRow) _accSeedRow(undoneRow);
        _accDeckSay(`Undone — “${last.pdf}” is un-curated again.`);
        announceStatus('Undone.');
        _accRenderCard();
    } catch (e) {
        _accDeckSay(String(e), true);
    } finally {
        _acc.busy = false;
        _accProgress();
    }
}

function _accMove(delta) {
    const total = _accDeckRows().length;
    const next = _acc.idx + delta;
    if (next < 0 || next > total) return;
    _acc.idx = next;
    _acc.sel = 0;
    _accRenderCard();
}

function _accClosePdf() {
    const panel = document.getElementById('audit-acc-pdf');
    const deck = document.getElementById('audit-acc-deck');
    if (panel) { panel.style.display = 'none'; panel.innerHTML = ''; }
    if (deck) deck.classList.remove('with-pdf');
    auditAccTeardown();
}

async function _accTogglePdf() {
    const panel = document.getElementById('audit-acc-pdf');
    const deck = document.getElementById('audit-acc-deck');
    if (!panel) return;
    if (panel.style.display !== 'none') { _accClosePdf(); return; }
    const row = _accDeckCurrent();
    if (!row) return;
    const rel = row.pdf.rel;
    panel.style.display = '';
    if (deck) deck.classList.add('with-pdf');
    panel.textContent = 'Loading PDF…';
    // Identity guard on every post-await write (the ambig finding 5): the
    // user may have navigated or closed the panel while bytes were in flight.
    const _stillCurrent = () => {
        const cur = _accDeckCurrent();
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
        auditAccTeardown();
        _accPdfUrl = URL.createObjectURL(blob);
        panel.innerHTML = '';
        const frame = document.createElement('iframe');
        frame.className = 'audit-ambig-frame';
        frame.src = _accPdfUrl;
        frame.title = `Preview of ${rel}`;
        panel.appendChild(frame);
        const open = document.createElement('button');
        open.type = 'button';
        open.className = 'audit-link-btn';
        open.textContent = '[open in PDF viewer]';
        open.addEventListener('click', async () => {
            try {
                await secureFetch('/api/audit/reveal', {
                    method: 'POST', body: JSON.stringify({ open: row.pdf.abs }),
                });
            } catch (e) { _accDeckSay(String(e), true); }
        });
        panel.appendChild(open);
    } catch (e) {
        if (_stillCurrent()) panel.textContent = String(e);
    }
}

function _accKeydown(e) {
    if (e.target.closest && e.target.closest('.inline-confirm-strip')) return;
    if (e.target.tagName === 'BUTTON' && (e.key === 'Enter' || e.key === ' ')) return;
    if (e.key === 'Escape') {
        const panel = document.getElementById('audit-acc-pdf');
        if (panel && panel.style.display !== 'none') {
            _accClosePdf();
            _accRefocusDeck();
            e.preventDefault();
        }
        return;
    }
    const row = _accDeckCurrent();
    const cands = (row && _accRes(row.pdf.rel)
        && _accRes(row.pdf.rel).candidates) || [];
    if (e.key >= '1' && e.key <= '9') {
        const i = parseInt(e.key, 10) - 1;
        if (i < cands.length) { _acc.sel = i; _accRenderCard(); e.preventDefault(); }
        return;
    }
    const card = document.getElementById('audit-acc-card');
    const btn = (v) => card && card.querySelector(`[data-acc-deck="${v}"]`);
    switch (e.key) {
        case 'i': case 'I': {
            const b = btn('identify');
            if (b && !b.disabled) { _accDeckVerb('identify', b); e.preventDefault(); }
            break;
        }
        case 'a': case 'A': case 'Enter': {
            const link = btn('link');
            const copy = btn('copy');
            if (link) { _accDeckVerb('link', link); e.preventDefault(); }
            else if (copy) { _accDeckVerb('copy', copy); e.preventDefault(); }
            break;
        }
        case 'l': case 'L': {
            const b = btn('link-to');
            if (b) { _accDeckVerb('link-to', b); e.preventDefault(); }
            break;
        }
        case 'n': case 'N': {
            const b = btn('no-match');
            if (b) { _accDeckVerb('no-match', b); e.preventDefault(); }
            break;
        }
        case 'p': case 'P': {
            const b = btn('pending');
            if (b) { _accDeckVerb('pending', b); e.preventDefault(); }
            break;
        }
        case 's': case 'S': case 'ArrowRight':
            _accMove(1); e.preventDefault(); break;
        case 'ArrowLeft':
            _accMove(-1); e.preventDefault(); break;
        case 'v': case 'V':
            _accTogglePdf(); e.preventDefault(); break;
        case 'Backspace': {
            const b = btn('undo');
            if (b && !b.disabled) { _accDeckUndo(b); e.preventDefault(); }
            break;
        }
        default: break;
    }
}


/**
 * Batch G (2026-07-22): retire one tag from every store, from the Tag Hygiene
 * report. Streams per-record progress into a status line above the report
 * table; the terminal frame offers a one-click batch Undo (confirm-gated —
 * it rewrites user data across all three stores).
 */
function _retireStatusHost() {
    // Report-identity guard (2026-08-18): a retire run is minutes long, and
    // lazily re-creating the host inside whatever #audit-report-body happens
    // to hold sprayed status + the undo button into whichever report the
    // user switched to. Only Tag Hygiene — the report that owns [retire] —
    // may host it; frames landing elsewhere still reach the static announcer
    // through _retireSay.
    if (_currentReport !== 'tag_hygiene') return null;
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

// Announce batch progress at MILESTONES only (2026-08-18): a 500-op retire /
// harmonize run's per-op {info} frames made an unbroken announcement stream
// that monopolized the SR queue for minutes. The VISIBLE text still updates
// on every frame; AT hears every 25th op, the final one, and every message
// that is not a "[n/total]" progress line.
function _announceBatchProgress(msg, isErr) {
    if (isErr) { announceError(msg); return; }
    const m = /^(?:Undo: )?\[(\d+)\/(\d+)\]/.exec(msg);
    if (m) {
        const n = Number(m[1]);
        if (n !== Number(m[2]) && n % 25 !== 0) return;
    }
    announceStatus(msg);
}

// Announcements go through the STATIC announcer regions (announceStatus /
// announceError), never a minted live region on the host — WebKit drops
// dynamically-minted live regions (documented project hazard).
function _retireSay(msg, isErr, tone) {
    // Re-resolve the host on every write: a report re-render between SSE
    // frames would leave a node captured at run start disconnected.
    const host = _retireStatusHost();
    // The TEXT span, never the host: an undo button is appended beside it and
    // painting the host would take the undo away with the message.
    if (host) statusLine(host.querySelector('.audit-retire-status-text'),
        { tone: tone || (isErr ? 'error' : 'info'), text: msg || '' });
    _announceBatchProgress(msg, isErr);
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

// ── Tag Harmony: selection + the confirmed bulk write ──────────────────────
//
// The selection model is refactor.js's (a Set of ids, a live-count action
// button disabled at zero, a confirm that names the scope), not a new one.
// Selection is deliberately NOT persisted: it belongs to one snapshot of one
// report, and restoring it across a rescan would re-offer keys whose gaps may
// already be closed.

let _harmonySelected = new Set();
let _harmonyZoteroKeys = new Set();
// In-flight gate (2026-08-18): _harmonyApply used to disable only the button,
// but any checkbox change re-ran _harmonyUpdateButton, which recomputed
// `disabled` purely from selection size — so ticking a row mid-stream
// re-armed the button and a second confirm launched a concurrent run
// (interleaved status lines, two undo buttons). Retire solved this with
// _retireSetAllDisabled; this is its harmonize twin.
let _harmonyBusy = false;
let _harmonyPreflightBusy = false;

/** Reset the selection — a new report payload is a different snapshot. */
function harmonyResetSelection() {
    _harmonySelected = new Set();
    _harmonyZoteroKeys = new Set();
}

function _harmonyUpdateButton() {
    const btn = document.getElementById('harmony-apply-btn');
    if (!btn) return;
    const n = _harmonySelected.size;
    btn.disabled = _harmonyBusy || _harmonyPreflightBusy || n === 0;
    btn.textContent = n ? `Harmonize selected (${n})` : 'Harmonize selected';
}

/** Freeze/unfreeze the whole harmonize surface while a run is in flight. */
function _harmonySetAllDisabled(on) {
    _harmonyBusy = on;
    const body = document.getElementById('audit-report-body');
    if (body) {
        body.querySelectorAll('input[data-harmony-key]').forEach((cb) => {
            // Dead rows (nothing applyable) stay disabled either way.
            cb.disabled = on || cb.hasAttribute('data-harmony-dead');
        });
        body.querySelectorAll('[data-harmony-all]').forEach((b) => { b.disabled = on; });
    }
    _harmonyUpdateButton();
}

function _harmonyToggle(key, on) {
    if (on) _harmonySelected.add(key); else _harmonySelected.delete(key);
    _harmonyUpdateButton();
}

function _harmonySelectAll(on) {
    const body = document.getElementById('audit-report-body');
    if (!body) return;
    body.querySelectorAll('input[data-harmony-key]').forEach((cb) => {
        // Only rows that CAN be written are selectable: a row whose every tag
        // is out of vocabulary has nothing to apply, and including it in
        // "select all" would inflate the count the confirm dialog promises.
        if (cb.disabled) return;
        cb.checked = on;
        _harmonyToggle(cb.getAttribute('data-harmony-key'), on);
    });
}

function _harmonyStatusHost() {
    // Same report-identity guard as _retireStatusHost: only Tag Harmony may
    // host harmonize status and its undo button (2026-08-18 — a mid-stream
    // tab switch used to spray both into a foreign report).
    if (_currentReport !== 'tag_harmony') return null;
    const body = document.getElementById('audit-report-body');
    if (!body) return null;
    let host = body.querySelector('#audit-harmony-status');
    if (!host) {
        host = document.createElement('div');
        host.id = 'audit-harmony-status';
        host.className = 'muted';
        const text = document.createElement('span');
        text.className = 'audit-harmony-status-text';
        host.appendChild(text);
        body.insertBefore(host, body.firstChild);
    }
    return host;
}

function _harmonySay(msg, isErr, tone) {
    const host = _harmonyStatusHost();
    // The TEXT span, never the host: an undo button is appended beside it and
    // painting the host would take the undo away with the message.
    if (host) statusLine(host.querySelector('.audit-harmony-status-text'),
        { tone: tone || (isErr ? 'error' : 'info'), text: msg || '' });
    _announceBatchProgress(msg, isErr);
}

/**
 * Run the confirmed bulk harmonize over the current selection.
 *
 * Sends KEYS only — the union, its vocabulary resolution and the writes are
 * all server-side, so the client cannot widen what gets written.
 */
async function _harmonyApply(btn, targets) {
    const keys = [..._harmonySelected];
    if (!keys.length || _harmonyBusy) return;
    _harmonySetAllDisabled(true);
    let terminal = null;
    try {
        await consumeSSE('/api/tagging/harmonize', {
            method: 'POST',
            body: JSON.stringify({ keys, targets, confirm: true }),
        }, {
            onInfo: (info) => _harmonySay(info),
            onOther: (frame) => { if (frame && frame.harmonize) terminal = frame.harmonize; },
            onError: (err) => { throw new Error(err); },
        });
        if (!terminal) { _harmonySay('Harmonize produced no result.', true); return; }
        const c = terminal.results || {};
        const bits = ['note', 'finder', 'zotero'].map((t) =>
            `${t}: ${(c[t] || {}).written || 0} written`
            + ((c[t] || {}).skipped ? ` / ${(c[t] || {}).skipped} skipped` : '')
            + ((c[t] || {}).failed ? ` / ${(c[t] || {}).failed} FAILED` : ''));
        let msg = `Harmonized ${terminal.records} paper(s) — ${bits.join(' · ')}.`;
        if (terminal.out_of_vocab && terminal.out_of_vocab.length) {
            msg += ` ${terminal.out_of_vocab.length} tag(s) skipped as absent from _tags.md.`;
        }
        if (terminal.failures && terminal.failures.length) {
            msg += ` Failures: ${terminal.failures.slice(0, 3).join('; ')}`
                + (terminal.failures.length > 3 ? '…' : '');
        }
        _harmonySay(msg + ' (Snapshot view: rows refresh on the next scan.)');
        // A successful run spends the selection: the rows just written have
        // nothing more to apply in this snapshot, and a still-armed button
        // would re-issue every Zotero read to write nothing. Failed rows can
        // be re-ticked from the still-rendered table.
        _harmonySelected = new Set();
        const bodyEl = document.getElementById('audit-report-body');
        if (bodyEl) {
            bodyEl.querySelectorAll('input[data-harmony-key]').forEach((cb) => {
                cb.checked = false;
            });
        }
        const host = _harmonyStatusHost();
        if (terminal.batch_id && host) {
            const undo = document.createElement('button');
            undo.type = 'button';
            undo.className = 'audit-link-btn';
            // Paper-framed copy (2026-08-18): "[undo harmonize (23)]" counted
            // JOURNAL OPS (one per target per paper) two lines under
            // "Harmonized 12 paper(s)" — journal vocabulary inviting doubt.
            undo.textContent = `[undo harmonize (${terminal.records} paper(s))]`;
            undo.addEventListener('click', () => confirmInline(undo, {
                message: `Remove the tags this harmonize just added — `
                    + `${terminal.op_count} write(s) across ${terminal.records} paper(s)?`,
                confirmLabel: 'Undo harmonize',
                onConfirm: async () => {
                    undo.disabled = true;
                    // Cancellable like the retire undo: a batch revert is
                    // minutes of Zotero reads + reverts, and abandoning the
                    // WAIT must not look like abandoning the work.
                    const ctrl = new AbortController();
                    const cancel = _mkUndoCancel(host, ctrl, 'harmonize');
                    let done = null;
                    try {
                        await consumeSSE('/api/tagging/restore', {
                            method: 'POST',
                            body: JSON.stringify({ batch_id: terminal.batch_id, confirm: true }),
                            signal: ctrl.signal,
                        }, {
                            onInfo: (info) => _harmonySay(`Undo: ${info}`),
                            onOther: (f) => { if (f && f.restore) done = f.restore; },
                            onError: (err) => { throw new Error(err); },
                        });
                        if (!done) { _harmonySay('Undo produced no result.', true); undo.disabled = false; return; }
                        if (done.ok === false && done.reverted >= done.total) {
                            _harmonySay('Undo is not fully recorded or was interrupted. Inspect Restore before retrying.', true);
                            undo.disabled = false;
                            return;
                        }
                        if (done.reverted < done.total) {
                            // The server keeps failed/skipped ops `applied`
                            // (journal conservatism), so the button stays so a
                            // re-run picks up exactly the remainder.
                            const left = done.total - done.reverted;
                            _harmonySay(`Undo: ${done.reverted}/${done.total} reverted — ${left} remain and stay retryable.`, true);
                            undo.textContent = `[retry undo harmonize (${left})]`;
                            undo.disabled = false;
                            return;
                        }
                        _harmonySay(`Undo: ${done.reverted}/${done.total} operation(s) reverted.`);
                        undo.remove();
                    } catch (err) {
                        if (err && err.name === 'AbortError') {
                            // UI-only cancel: already-reverted ops stay
                            // reverted; the rest are still retryable.
                            _harmonySay('Undo cancelled — operations already reverted stay '
                                + 'reverted; use the undo button again for the rest.');
                            undo.disabled = false;
                            return;
                        }
                        logError('harmonize undo failed: ' + String(err));
                        _harmonySay(`Undo failed: ${err.message || 'network'}`, true);
                        undo.disabled = false;
                    } finally {
                        if (cancel) cancel.remove();
                    }
                },
            }));
            host.appendChild(document.createTextNode(' '));
            host.appendChild(undo);
        }
    } catch (err) {
        _harmonySay(`Harmonize failed: ${err}`, true);
    } finally {
        _harmonySetAllDisabled(false);
    }
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
                        if (done.ok === false && done.reverted >= done.total) {
                            say('Undo is not fully recorded or was interrupted. Inspect Restore before retrying.', true);
                            undo.disabled = false;
                            return;
                        }
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
        ? `Record “${row.pdf.name}” as not a paper for Zotero?`
        : (action === 'pending'
            ? `Record “${row.pdf.name}” as to add to Zotero later?`
            : `Assign “${row.pdf.name}” to ${cand.citation_key}?`);
    confirmInline(btn, {
        message,
        confirmLabel: action === 'no_match' ? 'Not in bib' : (action === 'pending' ? 'Pending' : 'Assign'),
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
    const body = _mappingStateBody(rel, action, citationKey, true);
    _ambigSay('Applying…');   // L5: in-flight feedback (writes were silent)
    try {
        const r = await secureFetch('/api/audit/mapping', {
            method: 'POST', body: JSON.stringify(body),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { _ambigSay(d.error || `Mapping failed (${r.status})`, true); return; }
        _ambig.decided.set(rel, { action, citation_key: citationKey });
        _ambigToday.bump();
        _ambig.undo.push({
            pdf: rel,
            label: _mappingStateLabel(action, citationKey),
        });
        _ambigSay(_mappingStateDone(action, citationKey));
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
            method: 'POST', body: JSON.stringify({ pdf: last.pdf, clear: true, confirm: true }),
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
        if (i < cands.length) { _ambigSelect(i); e.preventDefault(); }
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
        case 'l': case 'L': {
            const b = btn('link-to');
            if (b) { _ambigVerb('link-to', b); e.preventDefault(); }
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
                ? `<button type="button" class="audit-link-btn" data-action="apply-tags" data-key="${_attr(r.citation_key)}" data-tags="${_attr(JSON.stringify(missing))}" aria-label="${_attr(`Apply the missing tags to the Obsidian note of ${r.citation_key}`)}">[apply to Obsidian]</button>`
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
        return [meta, `<div class="audit-table-scroll"><table class="audit-table">${head}${_tbodyHtml(rows)}</table></div>`];
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
                + ` <button type="button" class="audit-link-btn" data-action="finder-vocab-add" data-key="${_attr(r.citation_key)}" data-tag="${_attr(s)}" data-dir="${_attr(dir)}" aria-label="${_attr(`Add ${s} to the vocabulary, then apply it to ${r.citation_key}`)}">[+ vocab]</button>`;
        };
        const rows = d.rows.map(r => {
            const vocabIds = r.vocab_ids || {};
            const noteIds = [...new Set((r.finder_only || []).map(s => vocabIds[s]).filter(Boolean))];
            const finderIds = [...new Set((r.note_only || []).map(s => vocabIds[s]).filter(Boolean))];
            const variants = (r.variants || [])
                .map(v => `${v.finder} ~ ${v.note}${v.reason === 'synonym' ? ' (syn)' : ''}`)
                .join(', ');
            const applyNote = (noteIds.length && r.note_rel)
                ? `<button type="button" class="audit-link-btn" data-action="finder-apply-note" data-key="${_attr(r.citation_key)}" data-tags="${_attr(JSON.stringify(noteIds))}" data-note="${_attr(r.note_rel)}" aria-label="${_attr(`Write the Finder-only tags into the note of ${r.citation_key}`)}">[→ note (${noteIds.length})]</button>`
                : '';
            const applyFinder = finderIds.length
                ? `<button type="button" class="audit-link-btn" data-action="finder-apply-finder" data-key="${_attr(r.citation_key)}" data-tags="${_attr(JSON.stringify(finderIds))}" data-pdfs="${_attr(r.pdf_count)}" aria-label="${_attr(`Write the note-only tags onto the PDF Finder tags of ${r.citation_key}`)}">[→ Finder (${finderIds.length})]</button>`
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
        return [meta, `<div class="audit-table-scroll"><table class="audit-table">${head}${_tbodyHtml(rows)}</table></div>`];
    }
    if (name === 'ambiguous_pdfs') {
        // PERF (2026-09-02): kept so the deck<->table toggle re-renders from
        // module state instead of re-fetching the whole report (the
        // accession toggle's pattern, _accSetView -> _accRenderBody).
        _ambigPayload = d;
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
            // Row-context accessible names (2026-08-18): the deck view of the
            // SAME report labels every verb; the table's select had no name
            // at all (WCAG 4.1.2) and Tab/forms-mode heard "[assign]" N times.
            const name_ = r.pdf.name || r.pdf.rel;
            return `
            <tr>
                <td>${_esc(r.pdf.rel)}</td>
                <td><select class="audit-ambig-select" data-pdf="${_attr(r.pdf.rel)}"
                     aria-label="${_attr(`Candidate citation key for ${name_}`)}">${opts}</select></td>
                <td>
                    <button type="button" class="audit-link-btn" data-action="pick-key" data-pdf="${_attr(r.pdf.rel)}"
                        aria-label="${_attr(`Assign the selected key to ${name_}`)}">[assign]</button>
                    <button type="button" class="audit-link-btn" data-action="no-match" data-pdf="${_attr(r.pdf.rel)}"
                        aria-label="${_attr(`Mark ${name_} as not a paper for Zotero`)}">[not in bib]</button>
                    <button type="button" class="audit-link-btn" data-action="pending" data-pdf="${_attr(r.pdf.rel)}"
                        aria-label="${_attr(`Mark ${name_} as to add to Zotero later`)}">[pending]</button>
                    <button type="button" class="audit-link-btn" data-action="reveal" data-abs="${_attr(r.pdf.abs)}"
                        aria-label="${_attr(`Reveal ${name_} in Finder`)}">[reveal]</button>
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
    if (name === 'tag_harmony') {
        harmonyResetSelection();
        _harmonyZoteroKeys = new Set(d.rows.filter(r => r.present.includes('zotero')
            && r.missing_in_zotero.length && r.vocab_ids
            && r.missing_in_zotero.some(tag => r.vocab_ids[tag])).map(r => r.citation_key));
        const meta = d.count
            ? `${_esc(d.count)} paper${d.count === 1 ? "" : "s"} whose note / Finder / Zotero tags disagree · `
              + `${_esc(d.write_count)} tag(s) to propagate`
              + (d.applyable_count !== d.write_count
                  ? ` · <strong>${_esc(d.applyable_count)} applyable</strong> (the rest are absent from <code>_tags.md</code>)`
                  : '')
              + (d.single_surface_count
                  ? ` · ${_esc(d.single_surface_count)} carry tags on one surface only`
                  : '')
              + '. Merging is additive — nothing is ever removed. Each “Add to …” '
              + 'cell lists the tags that surface is <strong>missing</strong> '
              + '(they exist on another surface); hover a cell for what that '
              + 'surface carries today.'
              + (d.finder_platform && d.finder_platform !== 'darwin'
                  ? ' <strong>Finder tags read empty on this platform</strong> — the Add-to-Finder column is not real drift.'
                  : '')
            : 'Every paper carries the same tags on every surface it has. Nothing to harmonize.';
        // Header wording (2026-08-19 review): "→ Finder" read as "tags IN
        // Finder" — the exact inversion of what the cell holds (tags a write
        // would ADD there, i.e. today's Zotero/note-only tags).
        const head = '<thead><tr>'
            + '<th scope="col"><span class="sr-only">Select</span></th>'
            + '<th scope="col">Citation key</th><th scope="col">Author</th>'
            + '<th scope="col">Add to note</th><th scope="col">Add to Finder</th><th scope="col">Add to Zotero</th>'
            + '<th scope="col">Union</th></tr></thead>';
        // A surface absent from the record renders as "—", never as an empty
        // cell: empty reads as "nothing to do", which is the opposite of
        // "there is no note here at all". Every cell's tooltip states the
        // surface's CURRENT tags (serialized all along, displayed nowhere
        // until the 2026-08-19 review) so "what IS where" and "what WILL be
        // written" are both visible.
        const cell = (surface, tags, r) => {
            const curTags = ({ note: r.note_tags, finder: r.finder_tags,
                zotero: r.zotero_tags })[surface] || [];
            const now = `currently on ${surface}: ${curTags.length ? curTags.join(', ') : '(no tags)'}`;
            if (!r.present.includes(surface)) {
                return `<td class="cell-muted" title="${_attr(`this record has no ${surface} surface`)}">—</td>`;
            }
            if (!tags.length) return `<td title="${_attr(`up to date — ${now}`)}">✓</td>`;
            return `<td title="${_attr(now)}">${tags.map(t =>
                _harmonyTagHtml(t, r.vocab_ids, d.vocab_available)).join(' ')}</td>`;
        };
        const rows = d.rows.map(r => {
            // A row with nothing applyable is shown (so the gap is visible)
            // but not selectable: including it would inflate the count the
            // confirm dialog promises and then write nothing for it.
            const dead = !r.applyable_count;
            return `
            <tr>
                <td><input type="checkbox" data-harmony-key="${_attr(r.citation_key)}"
                     ${dead ? 'disabled data-harmony-dead="1"' : ''}
                     aria-label="${_attr(`Select ${r.citation_key}`)}"
                     title="${_attr(dead ? 'Nothing applyable — every missing tag is absent from _tags.md'
                                          : `${r.applyable_count} tag write(s)`)}"></td>
                <td>${_esc(r.citation_key)}</td>
                <td class="cell-title" title="${_attr(r.title || '')}">${_esc(r.author || '')}</td>
                ${cell('note', r.missing_in_note, r)}
                ${cell('finder', r.missing_in_finder, r)}
                ${cell('zotero', r.missing_in_zotero, r)}
                <td class="cell-muted">${_esc(r.union.join(', '))}</td>
            </tr>`;
        }).join('');
        const toolbar = d.count ? `
            <div class="audit-harmony-toolbar">
                <button type="button" class="audit-link-btn" data-harmony-all="1">[select all]</button>
                <button type="button" class="audit-link-btn" data-harmony-all="0">[clear]</button>
                <button type="button" class="btn btn-outline" id="harmony-apply-btn" disabled>Harmonize selected</button>
                <button type="button" class="audit-link-btn" data-harmony-tagging="1">[Zotero Desktop access in Tagging]</button>
            </div>` : '';
        // The 7-column table gets its OWN scroll container (tagging-table-wrap
        // precedent) so the toolbar, status line and post-run undo button —
        // all rendered above it in #audit-report-body — stay visible instead
        // of riding the body's horizontal scroll out of view.
        return [meta, d.count
            ? `${toolbar}<div class="audit-table-scroll"><table class="audit-table">${head}${_tbodyHtml(rows)}</table></div>`
            : ''];
    }
    if (name === 'orphan_notes') {
        const meta = d.count
            ? `${_esc(d.count)} reference note(s) whose filename matches no citation key — `
              + 'not in <code>_master.bib</code> and not reached by the PDF bridge. '
              + 'They produce no record, so their tags are invisible to every drift report.'
            : 'Every reference note matches a record. Nothing outside the join.';
        const head = '<thead><tr><th scope="col">Note</th><th scope="col"></th></tr></thead>';
        const rows = d.rows.map(r => `
            <tr>
                <td>${_esc(r.rel)}</td>
                <td>
                    <button type="button" class="audit-link-btn" data-action="open" data-abs="${_attr(r.abs)}"
                        aria-label="${_attr(`Open ${r.name || r.rel}`)}">[open]</button>
                    <button type="button" class="audit-link-btn" data-action="reveal" data-abs="${_attr(r.abs)}"
                        aria-label="${_attr(`Reveal ${r.name || r.rel} in Finder`)}">[reveal]</button>
                </td>
            </tr>`).join('');
        return [meta, d.count ? `<table class="audit-table">${head}${_tbodyHtml(rows)}</table>` : ''];
    }
    if (name === 'orphan_zotero_items') {
        const meta = d.count
            ? `${_esc(d.count)} Zotero item(s) no record claimed`
              + (d.tagged ? `, ${_esc(d.tagged)} of them carrying tags` : '')
              + ' — items with no Better BibTeX key, the losers of a duplicated key, '
              + 'those whose title fallback was refused, and whole group libraries '
              + 'whose items carry no keys. Their tags reach no report.'
            : 'Every Zotero item is claimed by a record. Nothing outside the join.';
        const head = '<thead><tr><th scope="col">Title</th><th scope="col">Type</th>'
            + '<th scope="col">Citation key</th><th scope="col">Tags</th><th scope="col"></th></tr></thead>';
        const rows = d.rows.map(r => `
            <tr>
                <td class="cell-title" title="${_attr(r.title || '')}">${_esc((r.title || '(untitled)').slice(0, 90))}</td>
                <td>${_esc(r.item_type || '')}</td>
                <td>${_esc(r.citation_key || '—')}</td>
                <td>${_esc((r.tags || []).join(', '))}</td>
                <td><button type="button" class="audit-link-btn" data-action="zotero"
                     data-key="${_attr(r.item_key)}"${r.group_id ? ` data-group="${_attr(String(r.group_id))}"` : ''}
                     aria-label="${_attr(`Open ${r.title || r.item_key} in Zotero`)}">[Zotero]</button></td>
            </tr>`).join('');
        return [meta, d.count ? `<table class="audit-table">${head}${_tbodyHtml(rows)}</table>` : ''];
    }
    if (name === 'multi_pdf') {
        // One citation key owning several byte-different PDFs, with the rung
        // that claimed each file. Read-only in Phase 1 of the 2026-09-06
        // plan; the detach verb arrives with Phase 2.
        const srcLabel = _PROVENANCE_LABELS;
        const meta = d.count
            ? `${_esc(d.count)} citation key(s) own ${_esc(d.file_count)} PDFs between them. `
              + `<strong>${_esc(d.mixed_count)}</strong> mix an identifier- or hand-verified file with a `
              + 'filename-only match — the shape of a second paper absorbed by its neighbour’s key, '
              + 'so review those first'
              + (d.all_filename_count ? `; ${_esc(d.all_filename_count)} rest on filename matches alone` : '')
              + (d.dismissed_count ? `; ${_esc(d.dismissed_count)} were dismissed as “not a duplicate” in Possible Duplicates` : '')
              + '. <strong>[unmap]</strong> records that no automatic match may re-claim that file for that key; it returns to Unmapped PDFs at the next scan (undo: [clear] on Not in Zotero).'
            : 'Every citation key owns at most one PDF.';
        if (!d.count) return [meta, ''];
        const head = '<thead><tr><th scope="col">Citation key</th><th scope="col">Author</th>'
            + '<th scope="col">Files</th><th scope="col">Matched by</th>'
            + '<th scope="col">Shape</th><th scope="col">Possible Duplicates</th></tr></thead>';
        const rows = d.rows.map(r => {
            const files = (r.files || []).map(f => `<div>${_esc(f.name)} `
                + `<button type="button" class="audit-link-btn" data-action="open" data-abs="${_attr(f.abs)}" aria-label="${_attr(`Open ${f.name}`)}">[open]</button> `
                + `<button type="button" class="audit-link-btn" data-action="reveal" data-abs="${_attr(f.abs)}" aria-label="${_attr(`Reveal ${f.name} in Finder`)}">[reveal]</button> `
                + `<button type="button" class="audit-link-btn" data-action="detach" data-pdf="${_attr(f.rel)}" data-key="${_attr(r.citation_key)}" aria-label="${_attr(`Unmap ${f.name} from ${r.citation_key}`)}">[unmap]</button></div>`).join('');
            const by = (r.files || []).map(f => `<div>${_esc(srcLabel[f.source] || f.source)}</div>`).join('');
            const shape = r.mixed ? 'mixed' : (r.all_filename ? 'filename only' : 'verified');
            const dis = r.dismissed === true ? 'dismissed' : (r.dismissed === false ? 'open' : '—');
            return `
            <tr data-key="${_attr(r.citation_key)}">
                <td class="cell-title" title="${_attr(r.title || '')}">${_esc(r.citation_key)}</td>
                <td>${_esc(r.author || '')}${r.year ? ` · ${_esc(r.year)}` : ''}</td>
                <td data-sort-value="${_attr(String((r.files || []).length))}">${files}</td>
                <td>${by}</td>
                <td data-sort-value="${_attr(r.mixed ? '0' : (r.all_filename ? '1' : '2'))}">${r.mixed ? '<strong>mixed</strong>' : _esc(shape)}</td>
                <td>${_esc(dis)}</td>
            </tr>`;
        }).join('');
        return [meta, `<div class="audit-table-scroll"><table class="audit-table">${head}${_tbodyHtml(rows)}</table></div>`];
    }
    if (name === 'pdf_review' || (name === 'not_in_zotero' && d.review_scope)) {
        // Body/meta replaced by the dedicated mount path in `_loadReport`.
        return ['', ''];
    }
    if (name === 'not_in_zotero') {
        // The PDFs curated "not in bib". Each row carries what the accession
        // resolver already knows about the file (cache only — no egress on a
        // report load), because most of this bucket turned out to be papers
        // meant for Zotero LATER rather than never.
        //
        // Phase 2 Option A: this tab is ONLY the curated queue. The per-file
        // identification workspace lives on pdf_review — do not mount it here.
        const meta = d.count
            ? `${_esc(d.count)} curated PDF(s): <strong>${_esc(d.pending_count || 0)}</strong> to add to Zotero later, `
              + `${_esc(d.no_match_count || 0)} not a paper for Zotero. They leave every other queue at the next scan, `
              + 'so this is the only place they are listed. '
              + `${_esc(d.resolved_count)} have a cached identification, <strong>${_esc(d.with_doi_count)}</strong> of those with a DOI`
              + (d.missing_count ? `; ${_esc(d.missing_count)} no longer exist on disk (see Dead Curation)` : '')
              // Not listed: Zotero links it, the rung could not pair it. A
              // curated "add later" row for a file already in Zotero would
              // have proposed a SECOND record (2026-09-20).
              + (d.zotero_unclaimed_count
                  ? `; ${_esc(d.zotero_unclaimed_count)} not listed because Zotero already links them `
                    + _gotoReportBtn('dead_curation', '[why]')
                  : '')
              + '. Each verb writes mapping.json once (a selection is one atomic write); [clear] returns a PDF to Unmapped PDFs at the next scan. '
              + 'For per-file confirmation and DOI export, open '
              + _gotoReportBtn('pdf_review', '[PDF identification review]') + '.'
            : 'No PDF is marked “not in bib” or “pending”. '
              + _gotoReportBtn('pdf_review', '[PDF identification review]')
              + ' is the per-file workspace when a scan has review data.';
        if (!d.count) return [meta, ''];
        const withDoi = d.rows.filter(r => _nizDoi(r)).length;
        const toolbar = `<div class="audit-harmony-toolbar">`
            + (withDoi
                ? `<button type="button" class="audit-link-btn" id="audit-niz-copy" data-count="${_attr(String(withDoi))}">[copy selected confirmed DOIs]</button> `
                  + '<span class="muted">for Zotero → Add Item by Identifier</span> · '
                : '')
            + '<button type="button" class="audit-link-btn" data-niz-all="1">[select all]</button> '
            + '<button type="button" class="audit-link-btn" data-niz-all="0">[none]</button> '
            + '<button type="button" class="audit-link-btn" data-action="niz-bulk" data-verb="pending" disabled>[selected → pending]</button> '
            + '<button type="button" class="audit-link-btn" data-action="niz-bulk" data-verb="no_match" disabled>[selected → not in bib]</button> '
            + '<button type="button" class="audit-link-btn" data-action="niz-bulk" data-verb="clear" data-role="destructive" disabled>[clear selected]</button> '
            + '<button type="button" class="audit-link-btn" data-action="niz-bulk" data-verb="identify" disabled title="Online lookup of the selected pending PDFs not yet identified (needs Identify sources online on Unmapped PDFs)">[identify selected]</button> '
            + '<button type="button" class="audit-link-btn" data-action="niz-bulk" data-verb="reidentify" disabled title="Online lookup of ALL selected pending PDFs, ignoring cached results">[re-identify selected]</button>'
            + '</div>'
            // Every identify outcome lands HERE, visibly (2026-09-07). It used
            // to go to announceStatus() alone, i.e. the sr-only announcer, and
            // most rows already hold a cached resolution — so a successful
            // re-identify that returned the same answer repainted identical
            // cells and a sighted user saw the button flicker and nothing
            // else. "Nothing happened" was literally true on screen while the
            // pipeline was working. Same defect the accession deck
            // (#audit-acc-status) and the tagging deck each fixed once.
            + '<p id="audit-niz-status" class="muted audit-niz-status"></p>';
        const head = '<thead><tr><th scope="col" data-sort="off"><span class="sr-only">Select</span></th>'
            + '<th scope="col">PDF</th><th scope="col">State</th><th scope="col">Identification</th>'
            + '<th scope="col">Best candidate</th><th scope="col">DOI</th><th scope="col" data-sort="off"></th></tr></thead>';
        const rows = d.rows.map(r => {
            const res = r.resolution;
            const doi = _nizDoi(r);
            const cand = res && res.candidates && res.candidates[0];
            const ident = _nizIdentHtml(r);
            const candTxt = cand
                ? `${_esc((cand.title || '').slice(0, 80))}${cand.year ? ` (${_esc(cand.year)})` : ''}`
                : '';
            const st = r.state || 'no_match';
            const setBtn = (state, label) => `<button type="button" class="audit-link-btn" data-action="niz-set" data-pdf="${_attr(r.pdf.rel)}" data-state="${state}"${st === state ? ' hidden' : ''} aria-label="${_attr(`Mark ${r.pdf.name} as ${label}`)}">[→ ${_esc(label)}]</button>`;
            return `
            <tr data-rel="${_attr(r.pdf.rel)}" data-review-token="${_attr(JSON.stringify(r.review?.token || null))}" data-doi="${_attr(doi || '')}" data-state="${_attr(st)}">
                <td><input type="checkbox" data-niz-sel="1"${r.exists ? '' : ' disabled'} aria-label="${_attr(`Select ${r.pdf.name}`)}"></td>
                <td class="cell-title" title="${_attr(r.pdf.rel)}">${_esc(r.pdf.name)}</td>
                <td data-niz-state="1">${st === 'pending' ? 'pending' : 'not in bib'}</td>
                <td data-niz-ident="1">${ident}</td>
                <td class="cell-title" data-niz-cand="1" title="${_attr(cand ? (cand.title || '') : '')}">${candTxt}</td>
                <td data-niz-doi="1">${doi ? `<code>${_esc(doi)}</code>` : ''}</td>
                <td>${r.exists
                    ? setBtn('pending', 'Add later') + ' ' + setBtn('no_match', 'Exclude from registration') + ' '
                      + `<button type="button" class="audit-link-btn" data-action="niz-clear" data-pdf="${_attr(r.pdf.rel)}" aria-label="${_attr(`Clear the decision on ${r.pdf.name}`)}">[clear]</button> `
                      + `<button type="button" class="audit-link-btn" data-action="niz-identify" data-pdf="${_attr(r.pdf.rel)}" data-force="${res ? '1' : '0'}"${st === 'pending' ? '' : ' hidden'} aria-label="${_attr(`Identify ${r.pdf.name} online`)}">[${res ? 're-identify' : 'identify'}]</button> `
                      + `<button type="button" class="audit-link-btn" data-action="zc-open" data-pdf="${_attr(r.pdf.rel)}" aria-expanded="false"${st === 'pending' ? '' : ' hidden'} aria-label="${_attr(`Create a Zotero item for ${r.pdf.name}`)}">[create in Zotero…]</button> `
                      + `<button type="button" class="audit-link-btn" data-action="open" data-abs="${_attr(r.pdf.abs)}" aria-label="${_attr(`Open ${r.pdf.name}`)}">[open]</button> `
                      + `<button type="button" class="audit-link-btn" data-action="reveal" data-abs="${_attr(r.pdf.abs)}" aria-label="${_attr(`Reveal ${r.pdf.name} in Finder`)}">[reveal]</button>`
                    : `<span class="muted">missing</span> <button type="button" class="audit-link-btn" data-action="niz-clear" data-pdf="${_attr(r.pdf.rel)}" aria-label="${_attr(`Clear the decision on ${r.pdf.name}`)}">[clear]</button>`}</td>
            </tr>`;
        }).join('');
        return [meta, `${toolbar}<div class="audit-table-scroll"><table class="audit-table">${head}${_tbodyHtml(rows)}</table></div>`];
    }
    if (name === 'dead_curation') {
        const kindLabel = {
            match_missing_file: 'curated match, file gone',
            no_match_missing_file: '“not in bib”, file gone',
            pending_missing_file: '“add to Zotero later”, file gone',
            match_dead_key: 'curated match, key no longer in bib or Zotero',
            overridden_by_zotero: 'overridden by the Zotero link',
            zotero_unexported: 'Zotero links it, but Better BibTeX has not exported the key',
            zotero_multi_links: 'two Zotero items link the same file',
        };
        const curationLabel = {
            match: 'hand-paired with another key', no_match: '“not for Zotero”',
            pending: '“add to Zotero later”', exclude: 'excluded from this entry',
        };
        const meta = d.count
            ? `${_esc(d.count)} mapping.json entr${d.count === 1 ? 'y' : 'ies'} point at nothing `
              + `(of ${_esc(d.curated_matches)} curated matches, ${_esc(d.curated_no_match)} “not in bib” `
              + `and ${_esc(d.curated_pending)} “add to Zotero later” decisions)`
              + (d.recovered_count ? `; ${_esc(d.recovered_count)} other file(s) were re-linked by content hash this scan` : '')
              + '. A stale decision is still the record of a hand choice, so nothing is pruned automatically. '
              + 'For a row overridden by the Zotero link, <strong>[clear decision]</strong> drops the unused mapping.json entry — the Zotero pairing stays.'
            : 'Every mapping.json entry points at a file on disk and a citation key that exists, '
              + 'and none disagrees with the file Zotero links.';
        if (!d.count) return [meta, ''];
        const head = '<thead><tr><th scope="col">Kind</th><th scope="col">Path</th><th scope="col">Citation key</th><th scope="col"></th></tr></thead>';
        const rows = d.rows.map(r => `
            <tr>
                <td>${_esc(kindLabel[r.kind] || r.kind)}${r.kind === 'overridden_by_zotero'
                    ? `<br><span class="muted">${_esc(curationLabel[r.curation] || r.curation || '')}; Zotero links it under ${_esc(r.zotero_key || '—')}</span>`
                    : ''}</td>
                <td class="cell-title" title="${_attr(r.pdf.rel)}">${_esc(r.pdf.rel)}</td>
                <td>${_esc(r.citation_key || '—')}</td>
                <td>${r.kind === 'overridden_by_zotero'
                    ? `<button type="button" class="audit-link-btn" data-action="dc-clear" data-pdf="${_attr(r.pdf.rel)}" aria-label="${_attr(`Clear the stale decision on ${r.pdf.rel}`)}">[clear decision]</button>`
                    : r.kind === 'match_dead_key'
                    ? `<button type="button" class="audit-link-btn" data-action="open" data-abs="${_attr(r.pdf.abs)}" aria-label="${_attr(`Open ${r.pdf.name}`)}">[open]</button> `
                      + `<button type="button" class="audit-link-btn" data-action="reveal" data-abs="${_attr(r.pdf.abs)}" aria-label="${_attr(`Reveal ${r.pdf.name} in Finder`)}">[reveal]</button>`
                    : ''}</td>
            </tr>`).join('');
        return [meta, `<table class="audit-table">${head}${_tbodyHtml(rows)}</table>`];
    }
    if (name === 'zotero_attachments') {
        zotlinkResetSelection();
        // Cleared FIRST: the `!d.count` early return below skips the
        // recompute, and a stale list that no longer describes this report is
        // how a keys-only verb later writes to the wrong attachments.
        _zlRetitleKeys = [];
        // What Zotero itself holds a file for. Unrelated to the vault's own
        // Z_attachments tree. Since 2026-09-19 a linked file IS the first
        // pairing rung, so trashing one hands that PDF back to the rest of
        // the ladder at the next scan; mapping.json itself is never touched.
        const meta = d.count
            ? `${_esc(d.count)} attachment(s) on Zotero items`
              + (d.pdfs ? ` · ${_esc(d.pdfs)} PDF` : '')
              + (d.owned ? ` · ${_esc(d.owned)} whose file Zotero owns` : '')
              + (d.with_annotations
                  ? ` · ${_esc(d.with_annotations)} carrying annotations` : '')
              + (d.missing
                  ? ` \u00b7 <strong>${_esc(d.missing)} broken</strong>` : '')
              + '. Removing one moves it to Zotero\u2019s trash \u2014 the file is '
              + 'never deleted and mapping.json is untouched, but a linked file is how the '
              + 'scan pairs a PDF with its entry: after removing one, the next scan pairs that '
              + 'PDF by its name, DOI or your hand decisions instead.'
              + (d.missing
                  ? ' A <strong>broken</strong> link is one whose file is no longer at the '
                    + 'path Zotero stored \u2014 a linked attachment is only a pointer, so '
                    + 'anything that renames or moves the file orphans it silently. '
                    + '[re-point\u2026] aims it at the right file again.'
                  : (d.linked ? ` All ${_esc(d.linked)} linked file(s) still resolve.` : ''))
              // Counted by the scan since 2026-09-19 and rendered nowhere
              // until 2026-09-20: a relative link resolves to nothing while
              // the Linked Attachment Base Directory is unset, so the pairing
              // rung skips it and the file looks un-linked everywhere else.
              + (_lastSummary && _lastSummary.zotero_relative_links
                  ? ` ${_esc(_lastSummary.zotero_relative_links)} link(s) are stored `
                    + 'relative to Zotero\u2019s Linked Attachment Base Directory, which is '
                    + 'unset \u2014 they resolve to nothing, so the scan cannot pair them. '
                    + 'Set that directory in Zotero, or re-link those files by absolute path.'
                  : '')
            : 'Zotero holds no attachments. Nothing to de-link.';
        // Phase 4 (2026-09-06): the forward verb — link the vault's PDFs INTO
        // Zotero as linked files (a path, no bytes, no cloud storage). Lazily
        // loaded: the candidate list is every record with a Zotero item and a
        // PDF, thousands of rows.
        //
        // Phase 2 IA (2026-09-10): two labelled sections on ONE tab — remove
        // vs link — so the destructive and constructive verbs cannot be
        // mistaken for each other.
        const attachSection = `
            <section class="audit-zl-attach" aria-labelledby="zl-attach-h">
                <h3 id="zl-attach-h">Link vault PDFs into Zotero</h3>
                <p class="muted">Creates a <em>linked file</em> on the Zotero item for each mapped PDF — Zotero stores the path only, so it costs no cloud storage and syncs down in seconds. Absolute paths (this Mac); personal library only; a PDF already linked is skipped. Undo: the remove verb above, or Tagging → Restore. After identifying a pending PDF, map it first, then return here.</p>
                <div class="audit-harmony-toolbar">
                    <button type="button" class="audit-link-btn" data-action="zl-load">[load candidates]</button>
                </div>
                <div id="zl-attach-body"></div>
            </section>`;
        // The tab's one status line: every zotlink verb (attach, re-point,
        // retitle, bulk remove) reports here, and it survives the re-render
        // because `_zlLastStatus` seeds it.
        // Minted empty and painted by `_zlRepaintStatus`; no `role="status"`
        // for the reason `#audit-ns-status` carries.
        const zlStatus = '<p id="audit-zl-status" class="audit-status-line"></p>';
        if (!d.count) return [meta, `${zlStatus}${attachSection}`];
        // The selection column carries no header text, so `installTableControls`
        // correctly treats it as unsortable without needing `data-sort="off"`;
        // it is marked anyway so the intent survives a future header label.
        const head = '<thead><tr><th scope="col" data-sort="off">'
            + '<span class="sr-only">Select</span></th>'
            + '<th scope="col">Parent</th>'
            + '<th scope="col">Citation key</th><th scope="col">File</th>'
            + '<th scope="col">Link mode</th><th scope="col">Annotations</th>'
            + '<th scope="col" data-sort="off"></th></tr></thead>';
        const rows = d.rows.map(r => {
            const file = r.filename || r.path || '';
            // The link mode is the whole safety story in one cell, so it says
            // which side owns the bytes rather than leaving the reader to know
            // Zotero's vocabulary.
            const mode = (r.link_mode || 'unknown')
                + (r.file_owned_by_zotero ? ' (Zotero holds the file)' : ' (a pointer only)');
            const ann = r.annotation_count || 0;
            // `exists` is null for an imported attachment (Zotero owns the
            // bytes and its location is not the user's business) and boolean
            // only for a linked one — so a missing pointer is stated, never
            // inferred from a blank.
            const broken = r.exists === false;
            return `
            <tr data-item-key="${_attr(r.item_key)}"${broken ? ' class="audit-zl-broken"' : ''}>
                <td><input type="checkbox" data-zotlink-key="${_attr(r.item_key)}"
                     data-annotations="${_attr(String(ann))}"
                     aria-label="${_attr(`Select ${file || r.item_key} for removal from Zotero`)}"
                     title="${_attr(ann ? `${ann} annotation(s) would go to the trash with it`
                                        : 'No annotations on this attachment')}"></td>
                <td class="cell-title" title="${_attr(r.parent_title || '')}">${_esc((r.parent_title || '(untitled)').slice(0, 70))}</td>
                <td>${_esc(r.citation_key || '\u2014')}</td>
                <td class="cell-title" title="${_attr(file)}">${_esc(file.slice(0, 60))}</td>
                <td>${_esc(mode)}${broken ? ' <strong>\u00b7 file missing</strong>' : ''}</td>
                <td data-sort-value="${_attr(String(r.annotation_count || 0))}">${_esc(r.annotation_count || 0)}</td>
                <td>
                    <button type="button" class="audit-link-btn" data-action="zotero"
                        data-key="${_attr(r.parent_key || r.item_key)}"${r.group_id ? ` data-group="${_attr(String(r.group_id))}"` : ''}
                        aria-label="${_attr(`Open ${r.parent_title || r.item_key} in Zotero`)}">[Zotero]</button>
                    <button type="button" class="audit-link-btn" data-action="zotlink-trash"
                        data-key="${_attr(r.item_key)}"
                        data-annotations="${_attr(String(r.annotation_count || 0))}"
                        data-file="${_attr(file)}">[remove from Zotero]</button>
                    ${broken ? `<button type="button" class="audit-link-btn" data-action="zl-repoint"
                        data-key="${_attr(r.item_key)}" data-file="${_attr(file)}"
                        aria-expanded="false"
                        aria-label="${_attr(`Re-point ${file || r.item_key} at another file`)}">[re-point\u2026]</button>` : ''}
                </td>
            </tr>`;
        }).join('');
        // Bulk de-link. The route has taken up to `_MAX_KEYS` item keys since
        // it shipped — with SSE progress, the annotation acknowledgement and
        // one batch_id for the undo — but the report only offered a per-row
        // verb, so clearing a folder's worth of attachments meant N confirms.
        const toolbar = `
            <div class="audit-harmony-toolbar">
                <button type="button" class="audit-link-btn" data-zotlink-all="1">[select all]</button>
                <button type="button" class="audit-link-btn" data-zotlink-all="0">[clear]</button>
                <button type="button" class="btn btn-outline" id="zotlink-apply-btn" disabled>Remove selected from Zotero</button>
                <span id="zotlink-cap-note" class="muted"></span>
            </div>`;
        // Title := filename (annual cleanup T4). The route has existed since
        // 2026-09-18 with a CLI caller only, so 653 connector-saved "Full Text
        // PDF" titles were unreachable from the app (2026-09-20). Selection is
        // by MISMATCH, not by checkbox: the new title is the basename Zotero
        // already holds, so there is nothing for the user to choose — and
        // keys-only on the wire means the client cannot name a title at all.
        const mismatched = d.rows.filter(
            r => r.link_mode === 'linked_file' && r.filename
                 && (r.title || '') !== r.filename);
        const retitleSection = mismatched.length ? `
            <section class="audit-zl-retitle" aria-labelledby="zl-retitle-h">
                <h3 id="zl-retitle-h">Title = filename</h3>
                <p class="muted">${_esc(mismatched.length)} linked attachment(s) are titled
                    something other than their file — usually <em>Full Text PDF</em> from a
                    connector save. The new title is the basename Zotero already stores, so
                    nothing here can rename a file or move a pointer. One batch, undoable from
                    Tagging \u2192 Restore.${mismatched.length > _ZOTLINK_MAX_RETITLE
                        ? ` Only the first ${_esc(_ZOTLINK_MAX_RETITLE)} are sent per run.` : ''}</p>
                <div class="audit-harmony-toolbar">
                    <button type="button" class="audit-link-btn" data-action="zl-retitle-dry" data-role="read">[preview]</button>
                    <button type="button" class="btn btn-outline" id="zl-retitle-btn">Set ${
                        _esc(Math.min(mismatched.length, _ZOTLINK_MAX_RETITLE))} title(s) to the filename</button>
                </div>
            </section>` : '';
        const removeSection = `
            <section class="audit-zl-remove" aria-labelledby="zl-remove-h">
                <h3 id="zl-remove-h">Remove from Zotero</h3>
                <p class="muted">Moves the attachment to Zotero\u2019s trash. The file itself is never deleted and mapping.json is untouched; the next scan pairs this PDF by its name, DOI or your hand decisions instead of this link. Undo from Tagging \u2192 Restore.</p>
                ${toolbar}
                <div class="audit-table-scroll"><table class="audit-table">${head}${_tbodyHtml(rows)}</table></div>
            </section>`;
        _zlRetitleKeys = mismatched.slice(0, _ZOTLINK_MAX_RETITLE).map(r => r.item_key);
        return [meta, `${zlStatus}${removeSection}${retitleSection}${attachSection}`];
    }
    if (name === 'unread_unzoterod') {
        // Say the containment out loud AND give a one-click way up to the full
        // list: landing here and wanting "all of them" is the exact journey the
        // old tab names broke.
        const meta = `${_esc(d.rows.length)} of the PDFs without Zotero have fewer than `
            + `${_esc(d.threshold)} annotations — a subset of `
            + `${_gotoReportBtn('read_unzoterod', '[Unmapped PDFs]')}, not the rest of it`
            + ` · ${_esc(d.ambiguous_count)} ambiguous PDFs excluded`
            + (d.zotero_unclaimed_count
                ? ` · ${_esc(d.zotero_unclaimed_count)} that Zotero links but the scan could not pair `
                  + _gotoReportBtn('dead_curation', '[why]')
                : '')
            + '.';
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
            excl.push(`${_esc(d.confirmed_no_match_count)} you confirmed are not in the bib `
                + _gotoReportBtn('not_in_zotero', '[view]'));
        }
        if (d.pending_count) {
            excl.push(`${_esc(d.pending_count)} pending for Zotero `
                + _gotoReportBtn('not_in_zotero', '[view]'));
        }
        if (d.skipped_count) {
            excl.push(`${_esc(d.skipped_count)} skipped by prefix `
                + _gotoReportBtn('skipped_pdfs', '[view]'));
        }
        // In Zotero, just not paired — an unexported Better BibTeX key, or two
        // items linking one file. Listing these here said "no Zotero entry"
        // about a file Zotero demonstrably holds (2026-09-20).
        if (d.zotero_unclaimed_count) {
            excl.push(`${_esc(d.zotero_unclaimed_count)} that Zotero links but the scan `
                + `could not pair `
                + _gotoReportBtn('dead_curation', '[why]'));
        }
        const meta = `${_esc(d.rows.length)} PDFs in biblio_articles/ without a verified association — `
            + `read and unread, ranked by annotation count (suggested read cutoff: `
            + `${_esc(d.suggested_read_cutoff)}). `
            + (excl.length
                ? `Not listed: ${excl.join(' · ')}.`
                : 'Nothing else under biblio_articles/ is excluded.')
            + ' Per-file confirmation and DOI export: '
            + _gotoReportBtn('pdf_review', '[PDF identification review]') + '.';
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
        // The dismissed count is disclosed, never silently subtracted: a
        // filtered list that reads as the whole truth is the Tag Hygiene
        // under-reporting defect, one report over.
        const dismissed = d.dismissed_count || 0;
        const meta = `${_esc(d.rows.length)} bib entries with no resolved PDF.`;
        // The controls live in the BODY, not the meta line: the verb dispatcher
        // wires `.audit-link-btn` under #audit-report-body only, so a button
        // rendered into the meta element is inert (caught by the test that
        // clicked one and found nothing had been sent).
        let controls = '';
        if (dismissed) {
            controls += `<div class="audit-dismissed-note">${_esc(dismissed)} entr${dismissed === 1 ? 'y is' : 'ies are'}
                hidden as “no PDF needed”. <button type="button" class="audit-link-btn"
                data-action="nopdf-restore-all">[show them again]</button></div>`;
        }
        // Bulk dismissal by item type. A videoRecording or a webpage has no
        // PDF by nature, and scrolling past those forever is what kept this an
        // overview instead of a queue.
        const byType = d.by_item_type || {};
        // Server-provided from the configurable `audit_no_pdf_types` key —
        // one source of truth, and the user can edit it in Audit Settings.
        const NEVER_PDF = Array.isArray(d.never_pdf_types) ? d.never_pdf_types : [];
        const bulk = Object.entries(byType)
            .filter(([t, n]) => n > 0 && NEVER_PDF.includes(t))
            .map(([t, n]) => `<button type="button" class="audit-link-btn"
                    data-action="nopdf-dismiss-type" data-type="${_attr(t)}">
                    [${_esc(t)} · ${_esc(n)}]</button>`).join(' ');
        if (bulk) {
            controls += `<div class="audit-nopdf-bulk">These item types rarely have a
                local PDF — dismiss a whole class: ${bulk}</div>`;
        }
        const head = '<thead><tr><th scope="col">Year</th><th scope="col">Key</th><th scope="col">Author</th><th scope="col">Title</th><th scope="col">Type</th><th scope="col">Zotero match</th><th scope="col" data-sort="off"></th></tr></thead>';
        const rows = d.rows.map(r => `
            <tr data-key="${_attr(r.citation_key)}">
                <td>${_esc(r.year || '')}</td>
                <td>${_esc(r.citation_key)}</td>
                <td>${_esc(r.author || '')}</td>
                <td class="cell-title" title="${_esc(r.title || '')}" aria-label="${_esc(r.title || '')}">${_esc((r.title || '').slice(0, 120))}</td>
                <td>${_esc(r.item_type || '')}</td>
                <td>${r.has_zotero_match ? '✓' : ''}</td>
                <td><button type="button" class="audit-link-btn"
                     data-action="nopdf-dismiss" data-key="${_attr(r.citation_key)}"
                     aria-label="${_attr(`Mark ${r.citation_key} as needing no local PDF`)}">[no PDF needed]</button></td>
            </tr>`).join('');
        return [meta, controls + `<table class="audit-table">${head}${_tbodyHtml(rows)}</table>`];
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
        // Finder tags on biblio PDFs the bridge never mapped to a citation key.
        // A SEPARATE table on purpose: the counts above are per paper and come
        // from the citation-key join, these are per PDF and come from a direct
        // xattr sweep, so folding them in would move every figure at once and
        // silently change what audit_library.py's --max-hygiene bar means.
        // Rendering them at all is the point — the server has computed this
        // bucket since 2026-08-11 but nothing displayed it, so the headline
        // "outside the vocabulary" number stayed a floor presented as a total.
        const unmappedRows = Array.isArray(d.unmapped_rows) ? d.unmapped_rows : [];
        let unmapped = '';
        if (d.unmapped_pdf_count) {
            const unmappedOov = unmappedRows.filter(r => !r.in_vocab).length;
            const body = unmappedRows.map(r => {
                const counts = `<td>${_esc((r.counts && r.counts.finder) || 0)}</td>`;
                const vocabCell = r.in_vocab
                    ? `✓ ${_esc(r.vocab_tag || '')}`
                    : (r.near_vocab ? `≈ ${_esc(r.near_vocab)}` : '—');
                const retireBtns = r.variants.map(v =>
                    `<button type="button" class="audit-link-btn" data-action="retire-tag" `
                    + `data-tag="${_attr(v)}" data-invocab="${r.in_vocab ? '1' : ''}" `
                    + `data-total="${_attr(r.total)}" `
                    + `aria-label="Retire tag ${_attr(v)} everywhere">[retire ${_esc(v)}]</button>`
                ).join(' ');
                return `<tr><td>${_esc(r.variants.join(', '))}`
                    + `${r.variants.length > 1 ? ' ⚠' : ''} ${retireBtns}</td>`
                    + `${counts}<td><strong>${_esc(r.total)}</strong></td>`
                    + `<td>${vocabCell}</td></tr>`;
            }).join('');
            unmapped = '<h4 style="margin-top:16px;">Finder tags on PDFs with no citation key</h4>'
                + `<p class="audit-meta">${_esc(d.unmapped_tagged_count)} of `
                + `${_esc(d.unmapped_pdf_count)} unmapped / ambiguous / not-in-bib PDF(s) carry `
                + `Finder tags — ${_esc(unmappedRows.length)} distinct tag(s), `
                + `${_esc(unmappedOov)} outside the vocabulary. These PDFs join to no `
                + `<code>Record</code>, so they are counted per PDF here and are NOT `
                + `included in the totals above.</p>`
                + '<table class="audit-table"><thead><tr><th scope="col">Tag (spellings seen)</th>'
                + '<th scope="col">PDFs</th><th scope="col">Total</th>'
                + '<th scope="col">Vocabulary</th></tr></thead>'
                + _tbodyHtml(body) + '</table>';
        }
        const cooc = (d.cooccurrence && d.cooccurrence.length)
            ? '<h4 style="margin-top:16px;">Frequent tag pairs (papers sharing both)</h4>'
              + '<table class="audit-table"><thead><tr><th scope="col">Tag A</th><th scope="col">Tag B</th><th scope="col">Papers</th></tr></thead><tbody>'
              + d.cooccurrence.map(p => `<tr><td>${_esc(p.a)}</td><td>${_esc(p.b)}</td><td>${_esc(p.count)}</td></tr>`).join('')
              + '</tbody></table>'
            : '';
        return [meta, `<div class="audit-table-scroll"><table class="audit-table">${head}${_tbodyHtml(rows)}</table></div><div class="audit-table-scroll">${unmapped}</div><div class="audit-table-scroll">${cooc}</div>`];
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
                <td data-sort-value="${_attr(r.size_bytes)}">${_fmtBytes(r.size_bytes)}</td>
                <td data-sort-value="${_attr(r.wasted_bytes)}">${_fmtBytes(r.wasted_bytes)}</td>
                <td>${r.paths.map(p => `<div>${p.rel === r.suggested_keep ? '<span title="Cleanest filename — the copy a user would keep" aria-label="Suggested keep">★</span> ' : ''}${_esc(p.rel)} <button type="button" class="audit-link-btn" data-action="reveal" data-abs="${_attr(p.abs)}">[reveal]</button></div>`).join('')}</td>
            </tr>`).join('');
        return [meta, `<table class="audit-table">${head}${_tbodyHtml(rows)}</table>`];
    }
    return ['', ''];
}

/** Human labels for the physical classes (audit/core/pdf_provenance.py).
 *
 * The label says what the file IS and, where it matters, what that implies —
 * a printed web page has no DOI to find, so telling the user that is the whole
 * point of the column. `article` gets no badge: it is the ladder's home ground
 * and badging the majority class would be noise. */
const _PROV_LABEL = {
    browser_print: ['printed web page', 'no DOI will ever exist'],
    book: ['book-length', 'try an ISBN rather than a DOI'],
    office_export: ['office export', 'a guideline, report or thesis — often grey literature'],
    scan: ['scan', 'no text layer to read an identifier from'],
    publisher: ['publisher PDF', ''],
};

function _provBadge(r) {
    const spec = _PROV_LABEL[r.klass];
    if (!spec) return '';
    const [label, hint] = spec;
    const pages = Number.isFinite(r.pages) && r.pages ? ` · ${r.pages}p` : '';
    // The "add it by URL in Zotero" half of the hint is only true when a URL
    // was actually recovered — the row shows a [copy URL] button in that case.
    // Asserting it unconditionally told the user to use something that was
    // not there for most of the class.
    const remedy = r.klass === 'browser_print'
        ? (r.source_url ? ' — add it by URL in Zotero (the URL is on this row)'
                        : ' — and no source URL could be recovered; open it and check the header')
        : '';
    return `<span class="audit-prov audit-prov-${_attr(r.klass)}"
        title="${_attr((hint || label) + remedy)}">${_esc(label)}${_esc(pages)}</span>`;
}

//: Mirror of `api/routes/notesync.py::_MAX_CREATE` (half the refactor
//: journal's evictable window). Chunking to get past it is deliberately NOT
//: done: a second chunk's prune makes the first chunk's ops evictable again,
//: which silently reintroduces the very undo loss the cap exists to prevent.
//: One bounded batch, then the user runs it again.
const _NOTESYNC_MAX_CREATE = 100;

//: Mirrors `api/routes/notesync.py::_PUSH_MAX_KEYS` / `_PULL_MAX_KEYS`.
const _NOTESYNC_MAX_PUSH = 50;
const _NOTESYNC_MAX_PULL = 100;

/** Last notesync outcome. `_loadReport('note_sync')` rebuilds the body, so a
 *  status that only lived in the replaced DOM vanished with it — the 4th
 *  instance of the class `#audit-niz-status` / `_acc.lastStatus` already
 *  closed. Painted into `#audit-ns-status` after every render. */
const _ns = { lastStatus: { msg: '', isError: false, at: null, tone: 'info' } };

function _nsSay(msg, isError, tone) {
    _ns.lastStatus = { msg: msg || '', isError: !!isError, at: msg ? Date.now() : null,
        tone: tone || (isError ? 'error' : 'info') };
    _nsRepaintStatus();
    if (isError && msg) announceError(msg);
    else if (msg) announceStatus(msg);
}

/** One painter for `#audit-ns-status`, used by `_nsSay` and again after every
 *  render — the template mints an EMPTY host, so the stamp and the text can
 *  never come from two places. */
function _nsRepaintStatus() {
    const el = document.getElementById('audit-ns-status');
    if (!el) return;
    const st = _ns.lastStatus;
    statusLine(el, { tone: st.tone || (st.isError ? 'error' : 'info'), text: st.msg, at: st.at });
}

const _KEYCHANGE_LABELS = {
    ready: 'ready to rename',
    no_item_key: 'note has no zotero-key',
    no_item: 'no Zotero item for that key',
    target_exists: 'a note already exists under the new key',
    linked_from: 'other notes still link to the old name',
    links_unverified: 'some vault files could not be checked for links',
};

function _renderKeyChanges(kc) {
    if (!kc) return '';
    if (kc.error) return `<p class="audit-summary-note">${_esc(kc.error)}</p>`;
    const rows = kc.rows || [];
    if (!rows.length) return '';
    const n = rows.length;
    let html = `<div class="audit-summary-group-label">Key changed in Zotero (${_esc(n)})</div>`
        + '<p class="audit-summary-note">Zotero renamed the citation key; the Obsidian note is still '
        + 'under the old filename. Rename adds the old key as an alias and moves the file. Dry-run '
        + 'first. Undo: Settings → History and Restore.</p>';
    const trs = rows.map(r => `
        <tr data-old-key="${_attr(r.old_key)}" data-state="${_attr(r.state)}">
            <td>${_esc(r.old_key)}</td>
            <td>${_esc(r.new_key || '—')}</td>
            <td>${_esc(_KEYCHANGE_LABELS[r.state] || r.state)}</td>
            <td>${r.state === 'ready'
                ? `<button type="button" class="audit-link-btn" data-action="notesync-rename-note" data-key="${_attr(r.old_key)}">[rename note]</button>`
                : `<span class="muted">${_esc((r.linked_from || r.unverified || []).join(', ') || '')}</span>`}</td>
        </tr>`).join('');
    html += `<table class="audit-table notesync-keychanges"><thead><tr>`
        + `<th scope="col">Old key</th><th scope="col">New key</th><th scope="col">State</th>`
        + `<th scope="col" data-sort="off"></th></tr></thead>${_tbodyHtml(trs)}</table>`;
    return html;
}

async function _noteSyncRenameNote(btn) {
    const key = btn.getAttribute('data-key');
    if (!key) return;
    let plan;
    try {
        const r = await secureFetch('/api/notesync/rename-note', {
            method: 'POST', body: JSON.stringify({ citation_key: key, dry_run: true }),
        });
        plan = await r.json().catch(() => ({}));
        if (!r.ok) { _nsSay(plan.error || `Dry run failed (${r.status})`, true); return; }
    } catch (e) {
        _nsSay(String(e), true); return;
    }
    const p = plan.plan || {};
    const steps = (p.changes || []).join('; ') || 'rename the note to the new citation key';
    confirmInline(btn, {
        message: `Rename ${key} → ${p.new_key || 'the new key'}? ${steps}. Reversible from Settings → History and Restore.`,
        confirmLabel: 'Rename note',
        onConfirm: async () => {
            try {
                const r = await secureFetch('/api/notesync/rename-note', {
                    method: 'POST', body: JSON.stringify({ citation_key: key, confirm: true }),
                });
                const d = await r.json().catch(() => ({}));
                if (!r.ok) { _nsSay(d.error || `Rename failed (${r.status})`, true); return; }
                const done = (d.plan || p);
                // A 200 can still carry a warning (2026-09-21 review): both files
                // left in place, or a version kept in a hidden file. Show it.
                _nsSay(`Renamed ${key} → ${done.new_key || 'new key'}.`
                    + (d.warning ? ` Warning: ${d.warning}.` : ''), Boolean(d.warning));
                await _loadReport('note_sync');
            } catch (e) {
                _nsSay(String(e), true);
            }
        },
    });
}

/** The one sync table — /api/notesync/sync-status.
 *
 * Three buttons on every comparable row (2026-09-07): take Zotero's / keep
 * mine / merge. Before this the verb was chosen FOR the user from the row's
 * classification, so a row the app had summarised as "you edited this" could
 * not be told "throw mine away, take Zotero's" — the one thing a person most
 * wants to say about exactly that row. The classification still earns its
 * place: it orders the table and decides which direction needs the second
 * acknowledgement, which is where the real risk is.
 *
 * `_nsNeedsAccept` mirrors the server's `_needs_accept` exactly. It is a
 * COURTESY, not the gate: the route re-derives it, so a client that got it
 * wrong is refused rather than obeyed.
 *
 * Bulk verbs still select by STATE from the visible rows, so a hidden
 * (filtered) row is never swept in and no bulk request ever carries
 * `accept_conflict`. */
function _nsNeedsAccept(r, direction) {
    if (r.baselined) return !!(direction === 'pull' ? r.region_changed : r.zotero_changed);
    return !!(direction === 'pull' ? r.extra_words : r.missing_words);
}

const _NS_COMPARABLE = new Set(['in_sync', 'pull', 'push', 'conflict', 'unbaselined']);

function _renderCreateAttempts(attempts) {
    if (!attempts) return '';
    if (attempts.error) return `<p class="audit-summary-note">${_esc(attempts.error)}</p>`;
    const rows = attempts.rows || [];
    if (!rows.length) return '';
    // Collapsed until needed (Phase 2 IA): recovery is rare and must not
    // compete with the everyday sync verbs above.
    return `<details class="notesync-attempts" aria-label="Retained note creation attempts">`
        + `<summary>Retained creation attempts (${_esc(rows.length)}) — recovery only</summary>`
        + '<p class="muted">These records block duplicate creation. '
        + '<strong>[inspect]</strong> makes no changes; a matching note does not authorize retry or undo.</p>'
        + rows.map(r => `<div class="notesync-attempt"><p><strong>${_esc(r.citation_key)}</strong> — `
            + `${r.status === 'acknowledged' ? 'creation acknowledged' : 'outcome uncertain'}`
            + (r.disposition ? ` · ${_esc(r.disposition)}` : '')
            + (r.automatic_retry_allowed ? ' · one retry allowed after trash' : '')
            + `; original parent ${_esc(r.parent)}; note ${_esc(r.note_key || 'key unknown')}. `
            + (r.in_inventory ? `<button type="button" class="audit-link-btn" data-action="notesync-inspect-create" data-key="${_attr(r.citation_key)}">[inspect]</button>`
                : 'Not in the cached inventory; manual review required.')
            + (r.resolution ? ` Last check: ${_esc(r.resolution.checked_at)}.`
                + (r.automatic_retry_allowed
                    ? ' Use [create in Zotero] on the missing-note row for the one replacement.'
                    : ' Creation remains blocked.')
                : '')
            + `</p><p class="notesync-attempt-result" role="status"></p>`
            + `<button type="button" hidden class="audit-link-btn notesync-attempt-repair" data-action="notesync-repair-create" data-key="${_attr(r.citation_key)}">[repair journal]</button> `
            + `<button type="button" hidden class="audit-link-btn notesync-attempt-keys" data-action="notesync-repair-keys" data-key="${_attr(r.citation_key)}">[repair keys]</button> `
            + `<button type="button" hidden class="audit-link-btn notesync-attempt-resolve" data-action="notesync-resolve-create" data-key="${_attr(r.citation_key)}">[bookkeeping]</button> `
            + `<button type="button" hidden class="audit-link-btn notesync-attempt-satisfied" data-action="notesync-mark-satisfied" data-key="${_attr(r.citation_key)}">[mark satisfied]</button> `
            + `<button type="button" hidden class="audit-link-btn notesync-attempt-uncertain" data-action="notesync-mark-uncertain" data-key="${_attr(r.citation_key)}">[mark uncertain]</button> `
            + `<button type="button" hidden class="audit-link-btn notesync-attempt-retry" data-action="notesync-allow-retry" data-key="${_attr(r.citation_key)}">[allow one retry after trash…]</button> `
            + `<button type="button" hidden class="audit-link-btn notesync-attempt-adopt" data-action="notesync-adopt-key" data-key="${_attr(r.citation_key)}">[adopt key…]</button></div>`).join('')
        + `<p class="muted">${_esc(attempts.legacy_scope || '')}</p></details>`;
}

/** What each note-sync word means — shown collapsed under the counts. Same
 * wording as the Help guide's glossary. */
const _NOTESYNC_LEGEND = '<details class="audit-legend"><summary>What these states mean</summary><dl>'
    + '<dt>Reference note / Zotero note</dt><dd>Your Obsidian note named after the citation key, and the reading note (child note) under the item in Zotero. Only the text between <code>&lt;!-- zotero-note:begin --&gt;</code> and <code>&lt;!-- zotero-note:end --&gt;</code> (the <em>marked region</em>) is compared and ever written; frontmatter, tags, the header and anything below the end marker are left alone.</dd>'
    + '<dt>Sync point</dt><dd>The app’s record of both texts, the Zotero version and the converter at the last time they were in sync. It is stored in the app’s data folder, never in the vault. With one, a difference can be traced to the side that changed; without one, the state is a guess from word counts.</dd>'
    + '<dt>In sync</dt><dd>A sync point exists and both sides say the same thing once the Zotero HTML is converted to Markdown.</dd>'
    + '<dt>Changed in Zotero / changed by you</dt><dd>Since the sync point, only the Zotero note changed (take Zotero’s to import it) or only your region changed (keep mine to send it to Zotero).</dd>'
    + '<dt>Conflict</dt><dd>Both sides changed since the sync point, or neither changed but they still differ. Use merge.</dd>'
    + '<dt>No sync point</dt><dd>Never recorded, or recorded by an older converter. “Record a sync point” fixes it for notes that are already identical.</dd>'
    + '<dt>Needs content review</dt><dd>The note has no recognisable marked region, or the region holds something the HTML ⇄ Markdown conversion cannot carry without loss: images, Obsidian wikilinks, or Zotero-only formatting. Nothing is offered until you fix it by hand; the words themselves may well be identical.</dd>'
    + '<dt>Several Zotero notes / no Zotero note / no reference note</dt><dd>The item has more than one child note, none, or there is no Obsidian note for it: there is nothing single to compare.</dd>'
    + '<dt>Words only Zotero has / words only you have</dt><dd>What a pull would bring in, and what it would overwrite. A write that would delete any of your words asks a second time.</dd>'
    + '</dl></details>';

function _renderTwoWaySync(sync) {
    if (!sync) return '';
    if (sync.error) {
        return `<div class="audit-summary-group-label">Sync</div>`
            + `<p class="audit-summary-note">${_esc(sync.error)}</p>`;
    }
    const counts = sync.counts || {};
    const rows = sync.rows || [];
    const n = (k) => counts[k] || 0;
    // Only ever announced from the tab that computed it, and only once: a
    // library with every note baselined is the end state the whole note-sync
    // subsystem exists to reach, and the tile for it is a "0" nobody notices.
    if (rows.length && n('unbaselined') === 0) {
        milestoneOnce('audit_ms_all_baselined', document.getElementById('audit-report-meta'), {
            title: 'Every note has a sync point',
            detail: `all ${rows.length} comparable notes are baselined, so a pull or a `
                + 'push can say what changed rather than guess. Shown once.',
        });
    }
    // Zotero-authoritative mode (maintainer decision 2026-09-19): the reading
    // notes are edited only in Zotero and this folder is a read-only mirror,
    // so the Obsidian → Zotero verbs are hidden and ONE sweep replaces the
    // per-row decisions. A note whose reading note vanished in Zotero is
    // LISTED, never offered a [create in Zotero] that would push the mirror
    // back, and never deleted.
    const auth = !!sync.authoritative;
    // The server decides readiness (normalised-text equality, and for a base
    // written by the previous converter, its stored text still matching).
    const bootstrappable = rows.filter(r => r.state === 'unbaselined'
        && (r.bootstrap_ready !== undefined ? r.bootstrap_ready : (!r.missing_words && !r.extra_words)));
    const legacyReady = bootstrappable.filter(r => r.legacy_base).length;
    // In step AND baselined is the only "nothing to say" row. An in-step row
    // with no sync point still needs one, so it stays visible until baselined.
    //
    // `missing_obsidian` USED to be excluded here (2026-09-20): the parts line
    // counted "N no reference note" and then the table held no row for any of
    // them — they were listed only in the separate "Missing" section, which is
    // the CACHED scan's Zotero side rather than this table's fresh read, so the
    // two could disagree with nothing saying so. The fresh read owns them now;
    // `_renderNoteSync` narrows that section to whatever the two disagree on.
    const actionable = rows.filter(r => r.state !== 'in_sync'
        && !bootstrappable.includes(r));
    const pullN = Math.min(n('pull'), _NOTESYNC_MAX_PULL);
    const pushN = Math.min(n('push'), _NOTESYNC_MAX_PUSH);
    const createN = Math.min(n('missing_obsidian'), _NOTESYNC_MAX_CREATE);
    let html = '';

    // Baselining comes FIRST while any note lacks a sync point: without one
    // the state is guessed from a word count rather than measured against
    // what both sides looked like last time, and the merge view has no
    // "before" to show. It writes only the app's own records.
    if (auth) {
        html += '<div class="audit-summary-group-label">Zotero is authoritative</div>'
            + '<p class="audit-summary-note">Reading notes are edited only in Zotero; this folder is a '
            + 'read-only mirror. <button type="button" class="audit-link-btn" data-action="notesync-mirror">'
            + '[mirror from Zotero]</button> is the whole job in one confirmed action: it '
            + '<strong>creates</strong> the notes Zotero has and this folder lacks, '
            + '<strong>rewrites</strong> every region that differs from today’s Zotero note, and '
            + '<strong>records a sync point</strong> for everything left without one — including the '
            + 'notes it just created, which nothing else in the app can baseline until the next scan. '
            + 'It shows you the plan before it writes.</p>'
            + '<p class="audit-summary-note">Narrower: <button type="button" class="audit-link-btn" '
            + 'data-action="notesync-refresh-all">[refresh all from Zotero]</button> rewrites every note’s '
            + 'marked region from today’s Zotero note, whatever it held — frontmatter, tags and header '
            + 'stay byte-for-byte — but never creates and never baselines a new note. Rewrites are not '
            + 'journalled: every note they replace must be committed in the vault’s git, which is their '
            + 'undo (the result names the commit); a run that only creates notes needs no git — those '
            + 'creations are undone from Settings → History and Restore. '
            + 'Notes whose reading note or item is gone from Zotero are listed below, never removed.</p>';
    } else {
        html += '<p class="audit-summary-note">To treat Zotero as the only editor of reading notes, turn on '
            + '<strong>Zotero is authoritative</strong> in Settings → Biblio Tagging → Zotero → '
            + 'Obsidian note sync. The tab then offers [mirror from Zotero] (create, refresh and baseline '
            + 'in one confirmed run) instead of per-note keep-mine / merge / push, with [refresh all '
            + 'from Zotero] kept as the narrower rewrite-only sweep.</p>';
    }
    // Rendered in BOTH modes since 2026-09-20. It was hidden under `!auth`,
    // which is the mode the maintainer runs — so a note with no sync point
    // could not be baselined from the app at all, whatever created it. It
    // writes only the app's own records, so hiding it was over-conservative.
    if (bootstrappable.length) {
        html += `<div class="audit-summary-group-label">${auth ? 'Sync points' : 'Start here — record a sync point'}</div>`
            + `<p class="audit-summary-note"><strong>${_esc(bootstrappable.length)} note(s) `
            + 'have no sync point yet</strong>, so their state is inferred from a word count '
            + 'rather than measured against what both sides looked like at the last sync, and '
            + 'the merge view has no "before" to show you. Recording one writes '
            + '<strong>nothing</strong> to your vault or to Zotero — only the app’s own '
            + 'records. '
            + (legacyReady ? `${_esc(legacyReady)} of them carry a sync point from the previous converter, `
                + 'which is carried over unchanged. ' : '')
            + `<button type="button" class="audit-link-btn" data-action="notesync-bootstrap">`
            + `[record today’s text as the sync point for ${_esc(bootstrappable.length)} matching note(s)]</button></p>`;
    }

    // Every state is named, so the parts add up to the total (2026-09-19: the
    // line used to omit "needs content review", "several Zotero notes" and
    // "no reference note", and 1 080 + 0 + 0 + 0 + 0 + 0 read as a count of
    // 1 237 that did not add up). Zero states stay silent.
    const parts = [
        ['in_sync', 'in sync'], ['pull', 'changed in Zotero'],
        ['push', auth ? 'will be overwritten' : 'changed by you'],
        ['conflict', auth ? 'will be overwritten' : 'conflict'], ['unbaselined', 'no sync point'],
        ['unparsed', 'needs content review'], ['multiple_notes', 'several Zotero notes'],
        ['missing_zotero', auth ? 'deleted in Zotero' : 'no Zotero note'], ['missing_obsidian', 'no reference note'],
    ].filter(([k]) => n(k) || k === 'in_sync')
        .map(([k, l]) => `${_esc(n(k))} ${l}`);
    html += `<div class="audit-summary-group-label">Sync — ${_esc(sync.total ?? rows.length)} notes: `
        + parts.join(' · ')
        + (n('pull') && !auth ? ` <button type="button" class="audit-link-btn" data-action="notesync-pull-all">[take Zotero’s for ${_esc(pullN)}]</button>` : '')
        + (n('push') && !auth ? ` <button type="button" class="audit-link-btn" data-action="notesync-push-all">[keep mine for ${_esc(pushN)}]</button>` : '')
        + (n('missing_obsidian') && !auth ? ` <button type="button" class="audit-link-btn" data-action="notesync-create-missing">[create ${_esc(createN)}${createN < n('missing_obsidian') ? ' of ' + _esc(n('missing_obsidian')) : ''}]</button>` : '')
        + '</div>'
        + (auth ? '' : _NOTESYNC_LEGEND
        + '<p class="audit-summary-note"><strong>Take Zotero’s</strong> writes the Zotero note '
        + 'into the marked region of your note (undo: Settings → History and Restore). '
        + '<strong>Keep mine</strong> writes that region to the Zotero note through the web API '
        + '(undo: Tagging → Restore). <strong>Merge</strong> shows both changes and lets you '
        + 'write the approved result to both notes. Only the marked region is ever touched — '
        + 'frontmatter, your tags and the header are left byte-for-byte alone. A direction that '
        + 'would delete something asks a second time before it runs.</p>');

    if (!actionable.length) {
        html += '<p class="audit-search-empty">Nothing to decide: every comparable note is in sync '
            + 'with a recorded sync point.</p>';
        return html;
    }

    const verbs = (r) => {
        const k = _attr(r.citation_key);
        if (_NS_COMPARABLE.has(r.state)) {
            if (auth) return `<button type="button" class="audit-link-btn" data-action="notesync-pull" data-key="${k}">[take Zotero’s]</button>`;
            return `<button type="button" class="audit-link-btn" data-action="notesync-pull" data-key="${k}">[take Zotero’s]</button>
                    <button type="button" class="audit-link-btn" data-action="notesync-push" data-key="${k}">[keep mine]</button>
                    <button type="button" class="audit-link-btn" data-action="notesync-merge" data-key="${k}" aria-expanded="false">[merge…]</button>`;
        }
        if (r.state === 'missing_obsidian') {
            // Fresh-read rows: Zotero has a reading note, this folder has no
            // note. One confirm per row; the bulk verb sits on the group label.
            return `<button type="button" class="audit-link-btn" data-action="notesync-preview" data-key="${k}">[preview]</button>
                    <button type="button" class="audit-link-btn" data-action="notesync-create" data-key="${k}">[create]</button>`;
        }
        if (r.state === 'missing_zotero') {
            if (auth) return '';
            // No [inspect prior attempt] here: these cells have no result host,
            // so `_noteSyncInspectCreate`'s `closest('.notesync-attempt')` lookups
            // all miss and a successful inspection renders NOTHING (it only
            // reaches the screen-reader announcer). The "Retained note creation
            // attempts" section above lists every key that actually has one, with
            // a working Inspect button and the three repair verbs; `create-zotero`
            // itself 409s with `reconciliation_required` pointing there.
            return `<button type="button" class="audit-link-btn" data-action="notesync-create-zotero" data-key="${k}">[create in Zotero]</button>`;
        }
        return '';
    };
    const label = { missing_obsidian: 'no reference note',
                    pull: 'changed in Zotero',
                    push: auth ? 'will be overwritten' : 'changed by you',
                    conflict: auth ? 'will be overwritten' : 'conflict', unbaselined: 'no sync point',
                    in_sync: 'in sync',
                    missing_zotero: auth ? 'deleted in Zotero' : 'no Zotero note', unparsed: 'needs content review',
                    multiple_notes: 'several Zotero notes' };
    // Worst first: most at stake, then most to import.
    const order = { conflict: 0, push: 1, pull: 2, missing_obsidian: 3,
                    unbaselined: 4, in_sync: 5,
                    missing_zotero: 6, multiple_notes: 7, unparsed: 8 };
    const sorted = actionable.slice().sort((a, b) =>
        (order[a.state] ?? 9) - (order[b.state] ?? 9)
        || (b.extra_words + b.missing_words) - (a.extra_words + a.missing_words)
        || String(a.citation_key).localeCompare(String(b.citation_key)));
    const trs = sorted.map(r => `
            <tr data-key="${_attr(r.citation_key)}" data-state="${_attr(r.state)}"
                data-accept-pull="${_nsNeedsAccept(r, 'pull') ? '1' : ''}"
                data-accept-push="${_nsNeedsAccept(r, 'push') ? '1' : ''}">
                <td>${_esc(r.citation_key)}</td>
                <td data-sort-value="${_attr(r.state)}">${_esc((label[r.state] || r.state) + (r.content_issues?.length ? ': ' + r.content_issues.join(' ') : ''))}</td>
                <td data-sort-value="${_attr(String(r.missing_words))}">${_esc(r.missing_words)}</td>
                <td data-sort-value="${_attr(String(r.extra_words))}">${_esc(r.extra_words)}</td>
                <td>${verbs(r)}
                    <button type="button" class="audit-link-btn" data-action="open" data-rel="${_attr(r.note_rel || '')}">[open note]</button>
                </td>
            </tr>`).join('');
    html += `<table class="audit-table notesync-twoway"><thead><tr><th scope="col">Key</th>`
        + `<th scope="col">State</th><th scope="col">Words only Zotero has</th><th scope="col">Words only you have</th>`
        + `<th scope="col" data-sort="off"></th></tr></thead>${_tbodyHtml(trs)}</table>`
        + '<p class="audit-summary-note">A note that <em>needs content review</em> or has '
        + '<em>several Zotero notes</em> is never written either way — it is listed here because '
        + 'saying nothing would read as “in sync”.</p>';
    return html;
}

/** Keys of the VISIBLE two-way rows in one state (bulk verbs). Filtered-out
 * rows are hidden, not removed, and a bulk verb must never sweep in a row the
 * user just narrowed away. */
function _noteSyncVisibleKeys(state, cap) {
    return [...document.querySelectorAll('#audit-report-body .notesync-twoway tbody tr[data-key]')]
        .filter(tr => tr.getAttribute('data-state') === state && !tr.hidden && tr.style.display !== 'none')
        .map(tr => tr.getAttribute('data-key'))
        .filter((k, i, all) => k && all.indexOf(k) === i)
        .slice(0, cap);
}

function _noteSyncBulk(btn, direction, keys, { accept = false } = {}) {
    const clean = (keys || []).filter(Boolean);
    if (!clean.length) return;
    const one = clean.length === 1;
    const subject = one ? clean[0] : clean.length + ' notes';
    const what = direction === 'pull'
        ? `Take Zotero’s version for ${subject}? `
          + (accept ? 'This DELETES the words you wrote in that region. ' : '')
          + 'Only the marked region changes. Reversible from Settings → History and Restore.'
        : `Keep your version for ${subject} and write it to Zotero? `
          + (accept ? 'This DELETES the edit made in Zotero. ' : '')
          + 'Reversible from Tagging → Restore.';
    confirmInline(btn, {
        message: what,
        tone: accept ? 'danger' : 'default',
        confirmLabel: (direction === 'pull' ? 'Take Zotero’s' : 'Keep mine')
            + (one ? '' : ` for ${clean.length}`),
        onConfirm: async () => {
            try {
                const body = { citation_keys: clean, confirm: true };
                if (accept) body.accept_conflict = true;
                const r = await secureFetch(`/api/notesync/${direction}`, { method: 'POST', body: JSON.stringify(body) });
                const d = await r.json().catch(() => ({}));
                if (!r.ok) { _nsSay(d.error || `${direction} failed (${r.status})`, true); return; }
                const done = direction === 'pull' ? d.pulled : d.pushed;
                const failed = Object.entries(d.results || {}).filter(([, v]) => !v.ok);
                _nsSay(`${direction === 'pull' ? 'Took Zotero’s for' : 'Kept yours for'} ${done} note(s)`
                    + (failed.length ? `; ${failed.length} not written (${failed.map(([k, v]) => `${k}: ${v.reason || v.status || v.message}`).slice(0, 5).join(', ')})` : '')
                    + (direction === 'push' && d.note ? ` ${d.note}` : '') + '.');
                await _loadReport('note_sync');
            } catch (e) {
                logError(`notesync ${direction} failed: ` + String(e));
                _nsSay(String(e), true);
            }
        },
    });
}

function _noteSyncBootstrap(btn) {
    const keys = _noteSyncVisibleKeys('unbaselined', 100000);
    confirmInline(btn, {
        tone: 'default',
        message: 'Record today’s text as the sync point for every note that is in step? '
            + 'Writes only the app’s own base records — nothing in the vault or Zotero.',
        confirmLabel: 'Baseline',
        onConfirm: async () => {
            try {
                const r = await secureFetch('/api/notesync/bootstrap', {
                    method: 'POST', body: JSON.stringify({ all: true, confirm: true }) });
                const d = await r.json().catch(() => ({}));
                if (!r.ok) { _nsSay(d.error || `Baseline failed (${r.status})`, true); return; }
                const nd = (d.skipped && d.skipped.needs_decision) ? d.skipped.needs_decision.length : 0;
                const rb = (d.rebaselined || []).length;
                _nsSay(`Baselined ${d.written} note(s)` + (rb ? ` (${rb} carried over from the previous converter)` : '')
                    + (nd ? `; ${nd} still need a decision` : '') + '.');
                await _loadReport('note_sync');
            } catch (e) {
                logError('notesync bootstrap failed: ' + String(e));
                _nsSay(String(e), true);
            }
        },
    });
    void keys;
}

async function _noteSyncInspectCreate(btn) {
    const key = btn.getAttribute('data-key');
    if (!key) return;
    btn.disabled = true;
    try {
        const response = await secureFetch('/api/notesync/inspect-create-attempt', {
            method: 'POST', body: JSON.stringify({citation_key: key})});
        const d = await response.json();
        if (!response.ok) { _showError(d.error || d.message || 'Inspection unavailable.'); return; }
        const states = {
            target_changed: 'The configured target differs from the original target.',
            unknown_outcome: 'No creation key was acknowledged; the outcome remains unknown.',
            absent_unresolved: 'The acknowledged key was not found; the outcome still needs review.',
            unavailable_or_database_changed: 'The original database could not be verified.',
            trashed: 'The acknowledged note is in the trash.',
            type_or_parent_changed: 'The acknowledged item changed type or parent.',
            creation_baseline_unknown: 'The note exists, but creation-time evidence is incomplete.',
            changed: 'The note differs from its creation acknowledgement.',
            matches_acknowledgement: 'The note matches its creation acknowledgement.'
        };
        // The frontmatter verdict decides which repair verbs appear, so when it
        // hides both of them it is the ONLY thing that explains why. `key_conflict`
        // especially: `set_frontmatter_keys` calls a note pointing at another
        // Zotero item "a fact to surface, not to overwrite" -- and it was computed,
        // returned, and then dropped one line short of the screen.
        const frontmatter = {
            already_keyed: 'The note already carries both Zotero keys.',
            missing_keys: 'The note is missing its Zotero keys.',
            key_conflict: 'The note frontmatter names a different Zotero item; it is left untouched.',
            ambiguous_frontmatter_keys: 'The frontmatter repeats or empties a Zotero key; it cannot be repaired safely.',
            frontmatter_unparseable: 'The frontmatter could not be parsed.',
            no_frontmatter: 'The note has no frontmatter block.',
            invalid_key: 'A recorded key is not a valid Zotero key.'
        };
        const message = `${states[d.remote] || 'Inspection requires manual review'} `
            + (d.target_changed_fields?.length ? `Changed: ${d.target_changed_fields.join(', ')}. ` : '')
            + `Source: ${d.source}. Original parent: ${d.target.parent}. `
            + (d.note_key ? `Note key: ${d.note_key}. ` : '')
            + (d.disposition ? `Disposition: ${d.disposition}. ` : '')
            + (d.frontmatter ? `${frontmatter[d.frontmatter] || `Frontmatter: ${d.frontmatter}.`} ` : '')
            + (d.automatic_retry_allowed
                ? 'One replacement create is allowed after trash confirmation.'
                : 'No changes made. Automatic retry and undo remain blocked.');
        const output = btn.closest?.('.notesync-attempt')?.querySelector('.notesync-attempt-result');
        if (output) output.textContent = message;
        const host = btn.closest?.('.notesync-attempt');
        const repair = host?.querySelector('.notesync-attempt-repair');
        if (repair) {
            repair.hidden = d.remote !== 'matches_acknowledgement' || d.source !== 'unchanged';
            repair.dataset.token = d.evidence_token || '';
        }
        const keys = host?.querySelector('.notesync-attempt-keys');
        if (keys) {
            keys.hidden = d.remote !== 'matches_acknowledgement' || d.source !== 'unchanged' || d.frontmatter !== 'missing_keys';
            keys.dataset.token = d.evidence_token || '';
            keys.dataset.sha = d.source_sha256 || '';
            keys.dataset.parent = d.target.parent;
            keys.dataset.note = d.note_key || '';
        }
        const resolve = host?.querySelector('.notesync-attempt-resolve');
        if (resolve) {
            resolve.hidden = d.remote !== 'matches_acknowledgement' || d.source !== 'unchanged' || d.frontmatter !== 'already_keyed';
            resolve.dataset.token = d.evidence_token || '';
            resolve.dataset.sha = d.source_sha256 || '';
        }
        const satisfied = host?.querySelector('.notesync-attempt-satisfied');
        if (satisfied) {
            const ok = ['matches_acknowledgement', 'changed', 'creation_baseline_unknown',
                'type_or_parent_changed', 'trashed'].includes(d.remote) && !!d.note_key;
            satisfied.hidden = !ok;
            satisfied.dataset.token = d.evidence_token || '';
        }
        const uncertain = host?.querySelector('.notesync-attempt-uncertain');
        if (uncertain) {
            const ok = ['unknown_outcome', 'absent_unresolved', 'unavailable_or_database_changed',
                'target_changed'].includes(d.remote);
            uncertain.hidden = !ok;
            uncertain.dataset.token = d.evidence_token || '';
        }
        const retry = host?.querySelector('.notesync-attempt-retry');
        if (retry) {
            retry.hidden = d.remote !== 'trashed' || !d.note_key;
            retry.dataset.token = d.evidence_token || '';
        }
        const adopt = host?.querySelector('.notesync-attempt-adopt');
        if (adopt) {
            adopt.hidden = d.remote !== 'unknown_outcome';
            adopt.dataset.token = d.evidence_token || '';
        }
        _nsSay(message);
    } catch (e) { _showError(String(e)); }
    finally { btn.disabled = false; }
}

function _noteSyncResolveCreate(btn) {
    confirmInline(btn, {
        message: 'Record that the acknowledged note, source keys and one applied journal record agree? This saves a dated bookkeeping check (mark bookkeeping done). It does not release the creation block or grant undo permission.',
        confirmLabel: 'Mark bookkeeping done',
        onConfirm: async () => {
            try {
                const response = await secureFetch('/api/notesync/resolve-create-attempt', {
                    method: 'POST', body: JSON.stringify({citation_key: btn.getAttribute('data-key'),
                        evidence_token: btn.dataset.token, source_sha256: btn.dataset.sha, confirm: true})});
                const result = await response.json();
                if (!response.ok) { _showError(result.error || 'Reconciliation refused.'); return; }
                _nsSay('Bookkeeping check saved. Duplicate creation remains blocked.');
                await _loadReport('note_sync');
            } catch (e) { _showError(String(e)); }
        }
    });
}

function _noteSyncMarkSatisfied(btn) {
    confirmInline(btn, {
        message: 'Record that this creation succeeded and stays blocked because the Zotero note exists? Does not release retry or grant undo.',
        confirmLabel: 'Mark satisfied',
        onConfirm: async () => {
            try {
                const response = await secureFetch('/api/notesync/mark-create-satisfied', {
                    method: 'POST', body: JSON.stringify({citation_key: btn.getAttribute('data-key'),
                        evidence_token: btn.dataset.token, confirm: true})});
                const result = await response.json();
                if (!response.ok) { _showError(result.error || 'Could not mark satisfied.'); return; }
                _nsSay('Marked satisfied. Creation stays blocked because the note exists.');
                await _loadReport('note_sync');
            } catch (e) { _showError(String(e)); }
        }
    });
}

function _noteSyncMarkUncertain(btn) {
    confirmInline(btn, {
        message: 'Record that this outcome is permanently undecidable by the app? Creation stays blocked. Prefer Adopt key… when you can identify the note in Zotero.',
        confirmLabel: 'Mark uncertain',
        onConfirm: async () => {
            try {
                const response = await secureFetch('/api/notesync/mark-create-uncertain', {
                    method: 'POST', body: JSON.stringify({citation_key: btn.getAttribute('data-key'),
                        evidence_token: btn.dataset.token, confirm: true})});
                const result = await response.json();
                if (!response.ok) { _showError(result.error || 'Could not mark uncertain.'); return; }
                _nsSay('Marked uncertain. Automatic retry remains blocked.');
                await _loadReport('note_sync');
            } catch (e) { _showError(String(e)); }
        }
    });
}

function _noteSyncAllowRetry(btn) {
    confirmInline(btn, {
        message: 'Allow exactly one new Zotero note create because the acknowledged note is in Zotero’s trash? The old attempt is kept as history. A 404 alone never unlocks retry.',
        confirmLabel: 'Allow one retry',
        onConfirm: async () => {
            try {
                const response = await secureFetch('/api/notesync/allow-create-retry', {
                    method: 'POST', body: JSON.stringify({citation_key: btn.getAttribute('data-key'),
                        evidence_token: btn.dataset.token, confirm: true})});
                const result = await response.json();
                if (!response.ok) { _showError(result.error || 'Retry not allowed.'); return; }
                _nsSay('One replacement create is allowed. Use [create in Zotero] on the missing-note row.');
                await _loadReport('note_sync');
            } catch (e) { _showError(String(e)); }
        }
    });
}

function _noteSyncAdoptKey(btn) {
    const key = window.prompt('Paste the Zotero note key that was created for this attempt:');
    if (!key) return;
    confirmInline(btn, {
        message: `Adopt note key ${key}? Verifies parent and HTML against the retained payload. Never grants automatic trash/undo authority.`,
        confirmLabel: 'Adopt key',
        onConfirm: async () => {
            try {
                const response = await secureFetch('/api/notesync/adopt-create-key', {
                    method: 'POST', body: JSON.stringify({citation_key: btn.getAttribute('data-key'),
                        note_key: key, evidence_token: btn.dataset.token, confirm: true})});
                const result = await response.json();
                if (!response.ok) { _showError(result.error || 'Adopt refused.'); return; }
                _nsSay(`Adopted note key ${result.note_key}. Repair journal/keys if needed; undo stays manual.`);
                await _loadReport('note_sync');
            } catch (e) { _showError(String(e)); }
        }
    });
}

function _noteSyncRepairKeys(btn) {
    confirmInline(btn, {
        message: `Add missing frontmatter keys for parent ${btn.dataset.parent} and note ${btn.dataset.note}? Existing keys and note content are preserved. This is journalled in Note Refactor; it does not update sync baselines or release the creation block.`,
        confirmLabel: 'Restore note keys',
        onConfirm: async () => {
            try {
                const response = await secureFetch('/api/notesync/repair-create-keys', {
                    method: 'POST', body: JSON.stringify({citation_key: btn.getAttribute('data-key'),
                        evidence_token: btn.dataset.token, source_sha256: btn.dataset.sha, confirm: true})});
                const result = await response.json();
                if (!response.ok) { _showError(result.error || 'Note key repair refused.'); return; }
                _nsSay(`Note key repair: ${result.status}. Sync baseline and creation block unchanged.`);
                await _loadReport('note_sync');
            } catch (e) { _showError(String(e)); }
        }
    });
}

function _noteSyncRepairCreate(btn) {
    confirmInline(btn, {
        message: 'Restore a missing CHERQAiN journal record for this acknowledged note? Existing records are preserved. This does not repair note keys, enable automatic undo, or release the creation block.',
        confirmLabel: 'Restore journal record',
        onConfirm: async () => {
            try {
                const response = await secureFetch('/api/notesync/repair-create-journal', {
                    method: 'POST', body: JSON.stringify({citation_key: btn.getAttribute('data-key'),
                        evidence_token: btn.dataset.token, confirm: true})});
                const result = await response.json();
                if (!response.ok) { _showError(result.error || 'Journal repair unavailable.'); return; }
                _nsSay(`Journal record ${result.op_id}: ${result.status}. Note contents and retry block unchanged.`);
                await _loadReport('note_sync');
            } catch (e) { _showError(String(e)); }
        }
    });
}

function _noteSyncCreateZotero(btn) {
    const key = btn.getAttribute('data-key');
    if (!key) return;
    confirmInline(btn, {
        message: `Create a reading note in Zotero for ${key} from this note’s region? `
            + 'The only verb that creates a Zotero item. Undo = trash it (Tagging → Restore).',
        confirmLabel: 'Create in Zotero',
        onConfirm: async () => {
            try {
                const r = await secureFetch('/api/notesync/create-zotero-note', {
                    method: 'POST', body: JSON.stringify({ citation_key: key, confirm: true }) });
                const d = await r.json().catch(() => ({}));
                if (!r.ok) {
                    const detail = d.error || d.message || `Create failed (${r.status})`;
                    _showError(detail + (d.note_key ? ` Retained Zotero note key: ${d.note_key}.` : ''));
                    return;
                }
                _nsSay(d.verification_pending ? d.message
                    : `Created Zotero note ${d.note_key} for ${key}.`);
                await _loadReport('note_sync');
            } catch (e) {
                logError('notesync create-zotero failed: ' + String(e));
                _showError(String(e));
            }
        },
    });
}

/** The merge view for one conflicting note: two side-by-side diffs (what
 * you changed, what Zotero changed — both against the last sync point) and an
 * editor pre-filled with the three-way merge. The user picks where the
 * result lands; whatever is not written is accepted as it is. */
async function _noteSyncMerge(btn) {
    const key = btn.getAttribute('data-key');
    if (!key) return;
    const row = btn.closest('tr');
    if (!row) return;
    const existing = row.nextElementSibling;
    if (existing && existing.classList.contains('audit-notesync-merge')) {
        existing.remove();
        btn.setAttribute('aria-expanded', 'false');
        _nsSay(`Merge view for ${key} closed.`);
        return;
    }
    if (btn.dataset.busy === '1') return;
    btn.dataset.busy = '1';
    btn.disabled = true;
    try {
        const r = await secureFetch('/api/notesync/merge-preview', {
            method: 'POST', body: JSON.stringify({ citation_key: key }) });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { _showError(d.error || `Merge preview failed (${r.status})`); return; }
        const tr = document.createElement('tr');
        tr.className = 'audit-notesync-merge';
        tr.setAttribute('data-row-attachment', '');
        const td = document.createElement('td');
        td.colSpan = row.children.length;
        const note = document.createElement('p');
        note.className = 'audit-summary-note';
        note.textContent = d.baselined
            ? `Both sides changed since the last sync. ${d.conflicts} overlapping change(s) are marked in the editor.`
            : 'No sync point is recorded for this note, so both versions are shown whole; edit the result by hand.';
        td.appendChild(note);
        td.appendChild(renderSideBySideDiff(d.base, d.ours, {
            beforeLabel: 'Last sync point', afterLabel: 'In the note now (yours)',
            emptyMessage: 'You did not change the region.' }));
        td.appendChild(renderSideBySideDiff(d.base, d.theirs, {
            beforeLabel: 'Last sync point', afterLabel: 'In Zotero now',
            emptyMessage: 'Zotero did not change.' }));
        const ta = document.createElement('textarea');
        ta.className = 'audit-notesync-merge-editor';
        ta.rows = Math.min(30, Math.max(6, String(d.merged || '').split('\n').length + 2));
        ta.value = d.merged || '';
        ta.setAttribute('aria-label', `Merge result for ${key}`);
        ta.setAttribute('lang', 'fr');
        td.appendChild(ta);
        const bar = document.createElement('div');
        bar.className = 'audit-notesync-merge-bar';
        const mk = (label, targets) => {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'audit-link-btn';
            b.textContent = label;
            b.addEventListener('click', () => _noteSyncMergeWrite(b, key, ta, targets, d.content_sha256, d.evidence));
            return b;
        };
        bar.appendChild(mk('[write to both]', { obsidian: true, zotero: true }));
        td.appendChild(bar);
        tr.appendChild(td);
        row.after(tr);
        btn.setAttribute('aria-expanded', 'true');
        _nsSay(`Merge view for ${key} shown: ${d.conflicts} conflict(s) marked.`);
    } catch (e) {
        logError('notesync merge preview failed: ' + String(e));
        _showError(String(e));
    } finally {
        delete btn.dataset.busy;
        btn.disabled = false;
    }
}

function _noteSyncMergeWrite(btn, key, ta, targets, contentSha, evidence) {
    const result = ta.value;
    if (/^(<<<<<<< |=======$|>>>>>>> )/m.test(result)) {
        _showError('The result still contains conflict markers; resolve them first.');
        return;
    }
    const where = targets.obsidian && targets.zotero ? 'the note AND Zotero'
        : targets.obsidian ? 'the note (Zotero is accepted as it is)' : 'Zotero (the note is accepted as it is)';
    confirmInline(btn, {
        message: `Write the merge result for ${key} to ${where}? Results are recorded separately for each destination. Inspect an incomplete result before resuming.`,
        confirmLabel: 'Write',
        onConfirm: async () => {
            try {
                const r = await secureFetch('/api/notesync/merge', {
                    method: 'POST', body: JSON.stringify({
                        citation_key: key, result, targets, content_sha256: contentSha, evidence, confirm: true }) });
                const d = await r.json().catch(() => ({}));
                if (!r.ok) {
                    _showError(d.error || (d.zotero && d.zotero.message) || `Merge failed (${r.status})`);
                    return;
                }
                _nsSay(`Merged ${key}` + (targets.zotero ? ' (Zotero written)' : '') + '.');
                await _loadReport('note_sync');
            } catch (e) {
                logError('notesync merge failed: ' + String(e));
                _showError(String(e));
            }
        },
    });
}

/** "Zotero Notes Sync" — /api/notesync/status.
 *
 * ONE table (2026-09-07). Until now this tab stacked six sections, two of
 * which drove the older one-directional `update` route: the same note could
 * appear as `[pull]` in the sync table and again as `[update]` or
 * `[overwrite region]` below it — three names for two writes, with the
 * destructive one reachable from a list that looked like the safe one. The
 * legacy `updatable` / `conflicts` / `drifted` / `unparsed` sections are
 * retired; every comparable note now lives in `_renderTwoWaySync` with the
 * same three buttons. `missing` keeps its own section because creating a
 * note that does not exist is not a direction choice.
 *
 * The `/api/notesync/update` route itself is unchanged and still serves the
 * CLI and `update-preview`; only this surface stopped offering it. */
function _renderNoteSync(d) {
    const missing = d.missing || [];
    const sync = d.sync || {};
    const counts = sync.counts || {};
    const decided = (counts.in_sync || 0);
    // ONE source per count (2026-09-19 review): every number here comes from
    // the fresh sync read. The "Missing" list below is the last scan's and
    // says so, rather than a second "no Obsidian note" figure beside this one.
    const meta = `${_esc(decided)} of ${_esc(sync.total ?? d.with_reading_note)} notes are in sync `
        + '(records with a reference note or a Zotero reading note).';

    // No `role="status"`: this <p> is minted on every render (WebKit drops a
    // dynamically-minted live region) and `_nsSay` already announces through
    // the static #sr-status-announcer — the pair was an unreliable duplicate.
    let html = '<p id="audit-ns-status" class="audit-status-line"></p>';
    // Layout (Phase 2, 2026-09-04): the marker pair around the Zotero region.
    // Offered as a dry run first; the write is confirm-gated and refused by
    // the server unless the notes folder is committed in the vault's git.
    const layout = d.layout || {};
    if (layout.unmarked > 0 || layout.marked > 0) {
        const unmarked = layout.unmarked || 0, marked = layout.marked || 0;
        html += `<p class="audit-summary-note notesync-layout">Note layout: ${_esc(marked)} marked with the `
            + '<code>&lt;!-- zotero-note:begin/end --&gt;</code> pair · '
            + `${_esc(unmarked)} unmarked (single <code>---</code> rule). `
            + (unmarked > 0
                ? '<button type="button" class="audit-link-btn" data-action="notesync-migrate">[migrate layout]</button> '
                : '')
            + (marked > 0
                ? '<button type="button" class="audit-link-btn" data-action="notesync-unmigrate">[undo layout migration]</button>'
                : '')
            + '</p>';
    }
    html += _renderCreateAttempts(d.attempts);
    html += _renderKeyChanges(d.key_changes);
    html += _renderTwoWaySync(d.sync);

    // ONE list per fact (2026-09-20). The sync table above owns the missing
    // mirrors, from today's Zotero read; this section is the CACHED scan's
    // view of the same question, so it now shows only what the two DISAGREE
    // on — records the scan called missing that today's Zotero read no longer
    // reports a reading note for. Showing both lists in full is what made the
    // tab claim two different numbers for one thing.
    const syncRows = (d.sync && !d.sync.error) ? (d.sync.rows || []) : null;
    const freshMissing = syncRows
        ? new Set(syncRows.filter(r => r.state === 'missing_obsidian').map(r => r.citation_key))
        : null;
    const missingShown = freshMissing
        ? missing.filter(m => !freshMissing.has(m.citation_key))
        : missing;
    if (missingShown.length) {
        const rows = missingShown.map(r => `
            <tr data-key="${_attr(r.citation_key)}">
                <td>${_esc(r.citation_key)}</td>
                <td>${_esc(r.note_rel || '')}</td>
                <td data-sort-value="${_attr(String(r.zotero_chars))}">${_esc(r.zotero_chars)}</td>
                <td>
                    <button type="button" class="audit-link-btn" data-action="notesync-preview"
                        data-key="${_attr(r.citation_key)}">[preview]</button>
                    <button type="button" class="audit-link-btn" data-action="notesync-create"
                        data-key="${_attr(r.citation_key)}">[create]</button>
                </td>
            </tr>`).join('');
        // The bulk verb is capped at the server's `_MAX_CREATE`, which is
        // itself half the refactor journal's retention window: a larger batch
        // could not keep the undo the confirm promises. Say the real number
        // rather than offering a button whose request would 400.
        const bulkN = Math.min(missingShown.length, _NOTESYNC_MAX_CREATE);
        html += `<div class="audit-summary-group-label">${freshMissing
            ? 'Missing according to the last scan only — today’s Zotero read does not report a reading note for these'
            : 'Missing — a reading note with no Obsidian note (as of the last scan)'}
            <button type="button" class="audit-link-btn" data-action="notesync-create-all">[create ${_esc(bulkN)}${bulkN < missingShown.length ? ' of ' + _esc(missingShown.length) : ' all'}]</button></div>`
            + `<table class="audit-table notesync-missing"><thead><tr><th scope="col">Key</th>`
            + `<th scope="col">Would create</th><th scope="col">Zotero note (chars)</th>`
            + `<th scope="col" data-sort="off"></th></tr></thead>${_tbodyHtml(rows)}</table>`;
    }
    return [meta, html];
}

function _pdfReportTable(rows) {
    // The class column only appears when the payload carries one, so a scan
    // that predates the provenance pass (or was cancelled during it) renders
    // exactly as before rather than growing an empty column.
    const hasProv = rows.some(r => r.klass);
    const head = '<thead><tr><th scope="col">Annot</th><th scope="col">Path</th>'
        + (hasProv ? '<th scope="col">What it is</th>' : '')
        + '<th scope="col" data-sort="off"></th></tr></thead>';
    const body = rows.map(r => `
        <tr>
            <td>${r.annotations}${r.error ? ` <span class="audit-error-tag">(${_esc(r.error)})</span>` : ''}</td>
            <td>${_esc(r.pdf.rel)}</td>
            ${hasProv ? `<td data-sort-value="${_attr(r.klass || '')}">${_provBadge(r)}</td>` : ''}
            <td>
                <button type="button" class="audit-link-btn" data-action="open" data-abs="${_attr(r.pdf.abs)}">[open]</button>
                <button type="button" class="audit-link-btn" data-action="reveal" data-abs="${_attr(r.pdf.abs)}">[reveal]</button>
                ${r.source_url ? `<button type="button" class="audit-link-btn"
                    data-action="copy-source-url" data-url="${_attr(r.source_url)}"
                    aria-label="${_attr(`Copy the source URL of ${r.pdf.rel}`)}">[copy URL]</button>` : ''}
            </td>
        </tr>`).join('');
    return `<table class="audit-table">${head}${_tbodyHtml(body)}</table>`;
}

// --- per-table sort + filter (2026-08-19) ---------------------------------
//
// The shared primitive lives in ui.js; audit.js only says WHICH tables get it
// and remembers each one's sort/filter across the owning module's re-renders
// (the accession body repaints on every write, so without this a sort would
// be silently undone by the very action the user took while sorted).
//
// Keyed by report + the table's ordinal within the body: several reports
// render more than one table (Tag Hygiene's three, the accession lists), and
// they are independent views.
const _tableViewState = new Map();
let _tableCtls = [];   // this DOM generation's controllers

function _installReportTableControls() {
    const body = document.getElementById('audit-report-body');
    if (!body) return;
    // One call per DOM generation (every render path ends in _wireRowActions),
    // so the previous generation's controllers are detached and dropped here.
    _tableCtls = [];
    body.querySelectorAll('table.audit-table').forEach((table, i) => {
        if (table.dataset.inventoryPaged === '1') return;
        const key = `${_currentReport}#${i}`;
        const ctl = installTableControls(table, {
            label: 'Filter the rows shown in this table',
            // Above the scrollport, not inside it — the wide tables scroll
            // horizontally and the strip would ride out of view.
            mountBefore: table.closest('.audit-table-scroll'),
            initial: _tableViewState.get(key),
            onState: (st) => {
                if (st.sortKey || st.query) _tableViewState.set(key, st);
                else _tableViewState.delete(key);
            },
        });
        if (ctl) _tableCtls.push(ctl);
    });
}

/** Drop every per-table filter in the report body. Used when a cross-report
 *  search hit lands on a row a live filter is hiding — the jump must show
 *  the row, not silently do nothing. */
function _clearTableFilters() {
    for (const ctl of _tableCtls) ctl.clearFilter();
}

// False while a deck view owns its own card wiring — the delegate must then
// stay out of the way exactly as the old early return did.
let _genericRowPass = true;


/** The DOI a "Not in Zotero" row can hand to Zotero: the resolution's own,
 *  else its best candidate's. Null when the cache knows nothing. */
function _nizDoi(r) {
    const res = r && r.resolution;
    if (!res) return null;
    if (res.doi) return res.doi;
    const c = res.candidates && res.candidates[0];
    return (c && c.doi) || null;
}

/** Copy every DOI the visible "Not in Zotero" rows carry, newline-joined for
 *  Zotero's Add Item by Identifier. Rows a table filter is hiding are skipped
 *  (the zotlink select-all rule); a clipboard refusal falls back to a
 *  selected read-only textarea, the accession pattern. */
async function _nizCopyDois(btn) {
    const body = document.getElementById('audit-report-body');
    if (!body) return;
    const items = [];
    body.querySelectorAll('tr[data-rel]').forEach(tr => {
        if (tr.hidden || tr.style.display === 'none') return;
        if (!tr.querySelector('input[data-niz-sel]:checked')) return;
        let token = null;
        try { token = JSON.parse(tr.getAttribute('data-review-token') || 'null'); } catch (_) { /* Missing review is excluded by the server. */ }
        items.push({pdf:tr.getAttribute('data-rel'),token});
    });
    if (!items.length) { announceStatus('Select confirmed PDFs to export.'); return; }
    let dois;
    try {
        const response = await secureFetch('/api/accession/review/export',{method:'POST',body:JSON.stringify({items})});
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'DOI validation failed.');
        dois = result.dois;
        _nizSay(`${result.included.length} PDFs included; ${dois.length} unique DOIs; ${result.excluded.length} excluded.`);
    } catch (error) { _nizSay(String(error),true); return; }
    if (!dois.length) { announceStatus('Confirm current selected PDFs in the workspace before exporting.'); return; }
    const text = dois.join('\n');
    try {
        await navigator.clipboard.writeText(text);
        announceStatus(`${dois.length} DOI(s) copied — paste into Zotero → Add Item by Identifier.`);
        btn.textContent = `[copied ${dois.length}]`;
    } catch (_e) {
        let box = document.getElementById('audit-niz-copy-export');
        if (!box) {
            box = document.createElement('textarea');
            box.id = 'audit-niz-copy-export';
            box.readOnly = true;
            box.rows = 4;
            box.style.width = '100%';
            box.setAttribute('aria-label', 'DOIs — copy manually');
            btn.parentElement.appendChild(box);
        }
        box.value = text;
        box.focus();
        box.select();
    }
}

/** Title := filename, over every linked attachment whose title differs.
 *
 * KEYS ONLY on the wire (harmonize's posture): the server reads each
 * attachment and uses the basename of the path IT holds, so no client can
 * write an arbitrary string into Zotero through this. The file is never
 * renamed and no pointer moves — this changes a display label — but it is
 * still a Zotero write, so it is confirm-gated and carries one `batch_id`.
 */
async function _zlRetitle(btn, dry) {
    if (_zlBusy) return;
    const keys = _zlRetitleKeys.slice(0, _ZOTLINK_MAX_RETITLE);
    if (!keys.length) { _zlSay('No attachment is titled differently from its file.'); return; }
    const run = async () => {
        // Re-checked HERE, not only at click time: `confirmInline` can sit
        // open for seconds, and another zotlink op may have started since
        // (`_zlRun` guards the same way for the same reason).
        if (_zlBusy) { _zlSay('Another Zotero link operation is running.', true); return; }
        _zlBusy = true;
        _zlUpdateButtons();
        let terminal = null;
        let streamErr = null;
        try {
            await consumeSSE('/api/zotlink/retitle', {
                method: 'POST',
                body: JSON.stringify({ item_keys: keys, ...(dry ? { dry_run: true } : { confirm: true }) }),
            }, {
                onInfo: (m) => _zlSay(m),
                onOther: (f) => { if (f && f.zotlink_retitle) terminal = f.zotlink_retitle; },
                onError: (m) => { streamErr = m; },
            });
            if (streamErr) { _zlSay(streamErr, true); return; }
            if (!terminal) { _zlSay('The run ended without reporting an outcome.', true); return; }
            const results = terminal.results || {};
            // Read the outcome PER KEY: the route answers 200 with per-key
            // skips, so a terminal count alone would hide a partial run.
            const failed = keys.filter(k => results[k] && results[k].ok === false).length;
            let msg = dry
                ? `Preview: ${terminal.would_retitle || 0} title(s) would change. Nothing was written.`
                : `${terminal.retitled || 0} title(s) set to the filename.`;
            if (terminal.cancelled) msg += ' Run cancelled \u2014 the rest were not attempted.';
            if (failed) msg += ` ${failed} failed.`;
            if (terminal.failures && terminal.failures.length) msg += ` ${terminal.failures.slice(0, 3).join('; ')}`;
            if (!dry && terminal.batch_id) {
                msg += ` Undo the whole batch from Tagging \u2192 Restore (batch ${terminal.batch_id}).`;
            }
            if (!dry && terminal.note) msg += ` ${terminal.note}`;
            _zlSay(msg, failed > 0);
            if (!dry) {
                // Outside the outer catch: the write LANDED, and a failed
                // reload must not replace the only message carrying the
                // batch id the undo needs.
                try {
                    await _loadReport('zotero_attachments');
                    _zlSay(msg, failed > 0);
                } catch (reloadErr) {
                    logError('zotlink retitle reload failed: ' + String(reloadErr));
                }
            }
        } catch (e) {
            logError('zotlink retitle failed: ' + String(e));
            _zlSay(String(e), true);
        } finally {
            _zlBusy = false;
            _zlUpdateButtons();
        }
    };
    if (dry) { await run(); return; }
    confirmInline(btn, {
        message: `Set ${keys.length} Zotero attachment title(s) to their filename? `
            + 'The files are not renamed and no pointer moves \u2014 only the label Zotero '
            + 'shows. Undoable in one batch from Tagging \u2192 Restore.',
        confirmLabel: 'Set titles',
        onConfirm: run,
    });
}


/* ---- Link vault PDFs into Zotero (Phase 4, 2026-09-06) ------------------ */

let _zlBusy = false;
let _zlRows = [];
let _zlLastStatus = { msg: '', isError: false, at: null, tone: 'info' };
//: Mirrors `api/routes/zotlink.py::_RETITLE_MAX_KEYS`. The server is the
//: authority; refusing here just avoids spending a confirm on a 400.
const _ZOTLINK_MAX_RETITLE = 1000;
//: Keys of the linked attachments whose title differs from their filename,
//: recomputed on every render of this report (they are one scan's snapshot).
let _zlRetitleKeys = [];

function _zlSay(msg, isError, tone) {
    _zlLastStatus = { msg: msg || '', isError: !!isError, at: msg ? Date.now() : null,
        tone: tone || (isError ? 'error' : 'info') };
    _zlRepaintStatus();
    if (msg) { if (isError) announceError(msg); else announceStatus(msg); }
}

/** ONE host for the tab, above both sections. It used to live inside "Link
 *  vault PDFs into Zotero", so a re-point — a verb on the REMOVE table —
 *  reported its outcome under the attach heading, reading as output of a run
 *  the user had not started (2026-09-20). Repainted after every render as
 *  well, since the template mints it empty. */
function _zlRepaintStatus() {
    const el = document.getElementById('audit-zl-status');
    if (!el) return;
    const st = _zlLastStatus;
    el.classList.toggle('error-text', !!st.isError);
    statusLine(el, { tone: st.tone || (st.isError ? 'error' : 'info'), text: st.msg, at: st.at });
}

async function _zlLoad(btn) {
    if (_zlBusy) return;
    btn.disabled = true;
    _zlSay('Loading candidates…');
    try {
        const r = await secureFetch('/api/zotlink/attach-candidates');
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { _zlSay(d.error || `Failed (${r.status})`, true); btn.disabled = false; return; }
        _zlRows = (d.rows || []).filter(x => x.to_link > 0);
        _zlRenderTable(d);
        _zlSay(`${_zlRows.length} record(s) with ${d.to_link} PDF(s) not yet linked`
            + (d.group_count ? ` · ${d.group_count} group-library record(s) can never be linked` : '')
            + (d.can_write ? '' : ' · the Zotero transport cannot write (set ZOTERO_API_KEY)'));
    } catch (e) {
        _zlSay(String(e), true);
    } finally {
        btn.disabled = false;
    }
}

function _zlRenderTable(d) {
    const host = document.getElementById('zl-attach-body');
    if (!host) return;
    const max = d.batch_max || 200;
    const head = '<thead><tr><th scope="col" data-sort="off"><span class="sr-only">Select</span></th>'
        + '<th scope="col">Citation key</th><th scope="col">Title</th><th scope="col">Library</th>'
        + '<th scope="col">PDFs to link</th><th scope="col">Result</th></tr></thead>';
    const rows = _zlRows.map(r => {
        const group = r.library_type === 'group';
        const files = r.pdfs.filter(p => !p.linked).map(p => `<div>${_esc(p.name)}</div>`).join('');
        return `
            <tr data-zl-key="${_attr(r.citation_key)}">
                <td><input type="checkbox" data-zl-sel="1"${group ? ' disabled' : ''} aria-label="${_attr(`Select ${r.citation_key}`)}"></td>
                <td>${_esc(r.citation_key)}</td>
                <td class="cell-title" title="${_attr(r.title || '')}">${_esc((r.title || '').slice(0, 70))}</td>
                <td>${group ? '<strong>group</strong> (cannot link)' : 'personal'}</td>
                <td data-sort-value="${_attr(String(r.to_link))}">${files}</td>
                <td data-zl-result="1"></td>
            </tr>`;
    }).join('');
    host.innerHTML = `
        <div class="audit-harmony-toolbar">
            <button type="button" class="audit-link-btn" data-action="zl-all" data-on="1">[select all visible]</button>
            <button type="button" class="audit-link-btn" data-action="zl-all" data-on="0">[none]</button>
            <button type="button" class="audit-link-btn" data-action="zl-run" data-dry="1" data-role="read" disabled>[dry run]</button>
            <button type="button" class="btn btn-outline" data-action="zl-run" data-dry="0" disabled>Link selected into Zotero</button>
            <span id="zl-attach-cap" class="muted">at most ${_esc(max)} records per run</span>
            <label class="muted" title="Attanger, ZotFile and ZotMoov rename or move files when a link is added. Disable their auto-rename/move first."><input type="checkbox" id="zl-ack-plugins"> I have disabled attachment-renaming plugins</label>
        </div>
        <div class="audit-table-scroll"><table class="audit-table">${head}${_tbodyHtml(rows)}</table></div>`;
    host.querySelectorAll('input[data-zl-sel]').forEach(cb => cb.addEventListener('change', _zlUpdateButtons));
    // The primary button is a `.btn`, not an `.audit-link-btn`, so the report
    // body's row-action delegate never sees it (that delegate matches
    // `.audit-link-btn` only). Bind it directly, like `zotlink-apply-btn`.
    host.querySelectorAll('[data-action="zl-run"]').forEach(b => {
        if (b.dataset.zlWired) return;
        b.dataset.zlWired = '1';
        b.addEventListener('click', () => _zlConfirm(b, b.getAttribute('data-dry') === '1'));
    });
    host.dataset.batchMax = String(max);
    applyActionRoles(host);
    _installReportTableControls();
    _zlUpdateButtons();
}

function _zlSelected() {
    const host = document.getElementById('zl-attach-body');
    const out = [];
    if (!host) return out;
    host.querySelectorAll('tr[data-zl-key]').forEach(tr => {
        if (tr.hidden || tr.style.display === 'none') return;
        const cb = tr.querySelector('input[data-zl-sel]');
        if (cb && cb.checked && !cb.disabled) out.push(tr.getAttribute('data-zl-key'));
    });
    return out;
}

function _zlSelectAll(on) {
    const host = document.getElementById('zl-attach-body');
    if (!host) return;
    host.querySelectorAll('tr[data-zl-key]').forEach(tr => {
        if (tr.hidden || tr.style.display === 'none') return;
        const cb = tr.querySelector('input[data-zl-sel]');
        if (cb && !cb.disabled) cb.checked = on;
    });
    _zlUpdateButtons();
}

function _zlUpdateButtons() {
    // The retitle verbs live outside `#zl-attach-body` and must still freeze
    // while any zotlink run is in flight — the early return below fires
    // whenever the attach candidates have not been loaded, which is the
    // normal state of the tab.
    document.querySelectorAll('#zl-retitle-btn, [data-action="zl-retitle-dry"]')
        .forEach((b) => { b.disabled = _zlBusy; });
    const host = document.getElementById('zl-attach-body');
    if (!host) return;
    const n = _zlSelected().length;
    const max = Number(host.dataset.batchMax || 200);
    host.querySelectorAll('[data-action="zl-run"]').forEach(b => {
        b.disabled = !n || n > max || _zlBusy;
        if (b.getAttribute('data-dry') === '0') b.textContent = `Link selected into Zotero (${n})`;
    });
    const cap = document.getElementById('zl-attach-cap');
    if (cap) cap.textContent = n > max ? `${n} selected — at most ${max} per run; clear some first.` : `at most ${max} records per run`;
}

function _zlConfirm(btn, dry) {
    const keys = _zlSelected();
    if (!keys.length) return;
    if (dry) { _zlRun(keys, true); return; }
    confirmInline(btn, {
        message: `Create linked-file attachments in Zotero for ${keys.length} record(s)? Absolute paths on this Mac, no bytes uploaded, no cloud storage used. Journalled — undo from Tagging → Restore (each also becomes removable from this tab).`,
        confirmLabel: `Link ${keys.length} into Zotero`,
        onConfirm: () => _zlRun(keys, false),
    });
}

async function _zlRun(keys, dry) {
    if (_zlBusy) return;
    _zlBusy = true;
    _zlUpdateButtons();
    let terminal = null;
    let streamErr = null;
    try {
        await consumeSSE('/api/zotlink/attach', {
            method: 'POST',
            body: JSON.stringify({
                citation_keys: keys, confirm: true,
                ...(dry ? { dry_run: true } : {}),
                ...(document.getElementById('zl-ack-plugins')?.checked ? { acknowledge_plugins: true } : {}),
            }),
        }, {
            onInfo: (m) => _zlSay(m),
            onOther: (f) => { if (f && f.zotlink_attach) terminal = f.zotlink_attach; },
            onError: (m) => { streamErr = m; },
        });
        if (streamErr) { _zlSay(streamErr, true); return; }
        if (!terminal) { _zlSay('The run ended without reporting an outcome.', true); return; }
        const results = terminal.results || {};
        let failed = 0;
        for (const key of keys) {
            const res = results[key] || {};
            const tr = document.querySelector(`#zl-attach-body tr[data-zl-key="${CSS.escape(key)}"]`);
            if (!tr) continue;
            const cell = tr.querySelector('[data-zl-result]');
            const label = res.status === 'attached' ? 'linked'
                : res.status === 'planned' ? 'would link'
                : res.status === 'skipped' ? `skipped (${res.reason || res.message || ''})`
                : `${res.status || 'failed'}${res.message ? `: ${res.message}` : ''}`;
            if (cell) cell.textContent = label;
            const cb = tr.querySelector('input[data-zl-sel]');
            if (res.status === 'attached' || res.status === 'skipped') { if (cb) { cb.checked = false; cb.disabled = !dry; } }
            else if (!dry) failed += 1;
        }
        let msg = dry
            ? `Dry run: ${Object.values(results).filter(r => r.status === 'planned').length} record(s) would get linked files. Nothing was written.`
            : `${terminal.attached} linked file(s) created in Zotero.`;
        if (terminal.cancelled) msg += ' Run cancelled — the rest were not attempted.';
        if (failed) msg += ` ${failed} failed and stay selected for a retry.`;
        if (terminal.failures && terminal.failures.length) msg += ` ${terminal.failures.slice(0, 3).join('; ')}`;
        if (terminal.journal_warning) msg += ` ${terminal.journal_warning}`;
        if (!dry && terminal.note) msg += ` ${terminal.note}`;
        _zlSay(msg, failed > 0);
    } catch (e) {
        _zlSay(String(e), true);
    } finally {
        _zlBusy = false;
        _zlUpdateButtons();
    }
}

/* ── "Since your last visit" ─────────────────────────────────────────────
 *
 * A report is a view of ONE cached scan, so its row count is a fact about the
 * library at that moment. Remembering the count this browser last saw turns
 * every tab into a progress report for free: "18 rows — 6 fewer than when you
 * last looked". A user closing orphans over a week sees the week.
 *
 * Honest about what it compares: the number the BROWSER last displayed, not a
 * server-side history. Two scans without opening the tab collapse into one
 * delta, which is why the line says "since you last looked" and not "since the
 * last scan". A deck view has no table and gets nothing rather than a
 * fabricated zero.
 */
const _reportSeenKey = 'audit_report_seen_v1';

function _reportSeen() {
    try { return JSON.parse(localStorage.getItem(_reportSeenKey) || '{}') || {}; }
    catch { return {}; }
}

function _paintReportDelta(body) {
    const meta = document.getElementById('audit-report-meta');
    if (!meta || !_currentReport) return;
    meta.querySelector('.audit-report-delta')?.remove();
    // Attachment rows belong to the row above them, and hidden rows are hidden
    // by the filter, not absent — count what the report HOLDS.
    const rows = body.querySelectorAll('table.audit-table tbody tr:not([data-row-attachment])');
    if (!rows.length) return;
    const n = rows.length;
    const seen = _reportSeen();
    const prev = seen[_currentReport];
    try {
        seen[_currentReport] = n;
        localStorage.setItem(_reportSeenKey, JSON.stringify(seen));
    } catch { /* private mode: the delta simply never appears */ }
    if (typeof prev !== 'number' || prev === n) return;
    const delta = n - prev;
    const span = document.createElement('span');
    span.className = `audit-report-delta ${delta < 0 ? 'trend-better' : 'trend-worse'}`;
    // The WORDS carry it; the colour is the second channel.
    span.textContent = ` · ${Math.abs(delta)} ${delta < 0 ? 'fewer' : 'more'} than when you last looked`;
    meta.appendChild(span);
}

function _wireRowActions() {
    const body = document.getElementById('audit-report-body');
    if (!body) return;
    // Draw the verb grammar before anything else touches the body: every
    // render path that wires row actions goes through here.
    applyActionRoles(body);
    _paintReportDelta(body);
    // The three persisted status hosts are minted EMPTY by their templates,
    // so one painter fills them — the stamp has to be computed at paint time
    // or an outcome from earlier in the scan reads as if it just happened.
    _accRepaintStatus();
    _nsRepaintStatus();
    _zlRepaintStatus();
    body.querySelectorAll('[data-manage-pdfs]').forEach(button => {
        button.addEventListener('click', () => {
            _managementTeardown();
            const panel = document.createElement('div');
            body.prepend(panel);
            _managementTeardown = mountPdfManagement(panel, button.dataset.managePdfs);
            panel.scrollIntoView?.({block:'start'});
        });
    });
    _genericRowPass = true;
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
        _wireMetaGotoButtons(meta);
    }
    // Tag Harmony's selection controls. Wired here (not delegated through
    // .audit-link-btn) because the apply button is the one control that must
    // carry the confirm ceremony and the live selection count.
    body.querySelectorAll('input[data-harmony-key]').forEach((cb) => {
        cb.addEventListener('change', () =>
            _harmonyToggle(cb.getAttribute('data-harmony-key'), cb.checked));
    });
    body.querySelectorAll('[data-harmony-all]').forEach((b) => {
        b.addEventListener('click', () =>
            _harmonySelectAll(b.getAttribute('data-harmony-all') === '1'));
    });
    const harmonyApply = document.getElementById('harmony-apply-btn');
    body.querySelector('[data-harmony-tagging]')?.addEventListener('click', () => selectAuditReport('tagging'));
    if (harmonyApply) {
        harmonyApply.addEventListener('click', async () => {
            const n = _harmonySelected.size;
            if (!n || _harmonyBusy || _harmonyPreflightBusy) return;
            const selection = [..._harmonySelected].sort().join('\u0000');
            if ([..._harmonySelected].some(key => _harmonyZoteroKeys.has(key))) {
                _harmonyPreflightBusy = true;
                _harmonyUpdateButton();
                try {
                    const response = await secureFetch('/api/tagging/zotero-available');
                    const readiness = await response.json();
                    if (_currentReport !== 'tag_harmony' || !document.contains(harmonyApply)) return;
                    if (!response.ok || readiness.writable !== true || readiness.transport !== 'local') {
                        _harmonySay((readiness.note || 'Zotero Desktop is not ready.')
                            + ' Open Tagging and authorize Zotero Desktop, then review the selection again.', true);
                        return;
                    }
                } catch (error) {
                    _harmonySay('Could not check Zotero Desktop access. Review access in Tagging before harmonizing.', true);
                    return;
                } finally { _harmonyPreflightBusy = false; _harmonyUpdateButton(); }
            }
            if (_currentReport !== 'tag_harmony' || !document.contains(harmonyApply)) return;
            if ([..._harmonySelected].sort().join('\u0000') !== selection) {
                _harmonySay('Selection changed while checking Zotero access. Review it and try again.', true);
                return;
            }
            confirmInline(harmonyApply, {
                // The confirm names the exact scope AND the direction: this is
                // the one place a user decides to write to three stores at once.
                message: `Propagate each paper's existing tags to whichever of its `
                    + `note / Finder / Zotero surfaces are missing them, for `
                    + `${n} selected paper(s)? Additive only — nothing is removed. `
                    + `Journalled and undoable in one click (also Tagging → Restore).`,
                confirmLabel: 'Harmonize',
                onConfirm: () => _harmonyApply(harmonyApply, {
                    note: true, finder: true, zotero: true,
                }),
            });
        });
    }
    // Zotero attachments: the same selection controls, wired here rather than
    // delegated for the same reason — the apply button carries the confirm
    // ceremony and the live count.
    body.querySelectorAll('input[data-zotlink-key]').forEach((cb) => {
        cb.addEventListener('change', () =>
            _zotlinkToggle(cb.getAttribute('data-zotlink-key'), cb.checked));
    });
    body.querySelectorAll('[data-zotlink-all]').forEach((b) => {
        b.addEventListener('click', () =>
            _zotlinkSelectAll(b.getAttribute('data-zotlink-all') === '1'));
    });
    const retitleBtn = document.getElementById('zl-retitle-btn');
    if (retitleBtn) {
        retitleBtn.addEventListener('click', () => _zlRetitle(retitleBtn, false));
    }
    const zotlinkApply = document.getElementById('zotlink-apply-btn');
    if (zotlinkApply) {
        zotlinkApply.addEventListener('click', () => {
            const keys = [..._zotlinkSelected];
            if (!keys.length) return;
            const { rows: annRows, annotations } = _zotlinkAnnotationTotal();
            // The confirm names the SUMMED annotation loss across the
            // selection, because that is the decision the server's second gate
            // is about and the per-row copy cannot answer it for a batch. A
            // selection with no annotated row never mentions annotations, and
            // never sends the acknowledgement.
            const base = `Move ${keys.length} attachment(s) to Zotero\u2019s trash? `
                + 'The files themselves are not deleted, and your PDF\u2194bib '
                + 'mapping is unaffected. Journalled and undoable in one click (also Tagging → Restore).';
            confirmInline(zotlinkApply, {
                message: annotations
                    ? `${base} ${annRows} of them carry ${annotations} Zotero `
                      + 'annotation(s) in total, which go to the trash with them.'
                    : base,
                confirmLabel: annotations
                    ? `Remove ${keys.length} (loses ${annotations} annotation${annotations === 1 ? '' : 's'})`
                    : `Remove ${keys.length} from Zotero`,
                onConfirm: () => _zotlinkBulk(zotlinkApply, keys, annotations > 0),
            });
        });
    }
    // The triage deck owns its own wiring (it re-renders per card); mount it
    // once its shell is in the DOM, and skip the generic pass so its buttons
    // are not double-bound.
    if (document.getElementById('audit-ambig-deck')) {
        _genericRowPass = false;
        _installRowActionDelegate(body);
        _ambigMount();
        return;
    }
    // Accession surfaces (read_unzoterod): the deck wires its own cards
    // (skip the generic pass, ambig precedent); the table view wires its
    // panel/lists/verbs and FALLS THROUGH so [open]/[reveal] still bind.
    if (document.getElementById('audit-acc-deck')) {
        _genericRowPass = false;
        _installRowActionDelegate(body);
        _accWirePanel();
        _accMount();
        return;
    }
    if (document.getElementById('audit-acc-panel')) { _accWirePanel(); }
    const nizCopy = document.getElementById('audit-niz-copy');
    if (nizCopy) nizCopy.addEventListener('click', () => _nizCopyDois(nizCopy));
    body.querySelectorAll('[data-niz-all]').forEach(b => {
        b.addEventListener('click', () => _nizSelectAll(b.getAttribute('data-niz-all') === '1'));
    });
    body.querySelectorAll('input[data-niz-sel]').forEach(cb => {
        cb.addEventListener('change', _nizUpdateCount);
    });
    // Inventory pairings-board verbs (their buttons carry no data-action /
    // data-abs, so the generic .audit-link-btn pass below no-ops on them).
    _installRowActionDelegate(body);
    _installReportTableControls();
}

/* PERF (2026-09-02): one delegated click handler for the report body's row
 * verbs, instead of one closure per button.
 * Before: every render bound a listener to each .audit-link-btn plus each
 *   inventory [data-inv-view] / [data-inv-detail] — 12 000 to 20 000 closures
 *   for the 6 000-row inventory, re-created on every re-render (and every
 *   accession write re-renders the whole body).
 * Change: a single listener on #audit-report-body, attached once per element
 *   via a dataset MARKER (the audit search bar's lesson: a module flag stays
 *   true after the node is replaced and would leave the new body unwired).
 * Safe because: the body element persists across innerHTML swaps, so the
 *   delegate survives exactly the re-renders the per-button binds were
 *   re-created for, and every branch below is the code the per-button handler
 *   ran, reached through closest(). The DECK views' early return is preserved
 *   by _genericRowPass — the ambiguous and accession cards wire their own
 *   [data-action="reveal"] buttons, which also carry .audit-link-btn, so a
 *   delegate ignoring the flag would fire twice.
 * Lock/thread contract: n/a (browser).
 * Pinned by: tests/js/auditRowDelegate.test.js. */
/* ── Action roles: one grammar for every inline verb ─────────────────────
 *
 * 54 distinct `data-action` values are emitted from this file across ~159
 * buttons, and they all looked the same: `[open]`, `[unmap]` and
 * `[retire everywhere]` were one undifferentiated blue. A reader could not
 * tell, before clicking, which of them only looks at something, which writes,
 * and which takes something away.
 *
 * This table is the ONE place that decides. Three roles, by what the click
 * costs the user — never by which endpoint it happens to call:
 *
 *   read        nothing the user can lose changes. Opening, revealing,
 *               previewing, copying, a dry run, a selection toggle. An online
 *               identification is `read`: it writes only regenerable memos.
 *   write       persistent, ADDS or SETS a value (confirm-gated, journalled).
 *   destructive persistent, REMOVES / CLEARS / DETACHES / TRASHES, or
 *               OVERWRITES text the user may have written. Carries the ⚠
 *               glyph, and — by the rule this table exists to keep honest —
 *               only ever sits on a verb `confirmInline` gates.
 *
 * Pull and push are both `destructive`: each overwrites the OTHER side's
 * region, which is exactly why the route demands `accept_conflict` on top of
 * `confirm`. Calling the outbound one `write` would say the safer thing about
 * the more dangerous pair.
 *
 * One action may serve several verbs (`zl-run` is both a dry run and the real
 * one; `niz-bulk` carries five `data-verb`s). Those buttons set `data-role`
 * at the emit site and the stamp leaves them alone — the table gives the
 * DEFAULT, never the last word.
 *
 * Pinned by tests/js/actionRoles.test.js, whose shape is WRITE_ACTION_IDS':
 * a new `data-action` without a row here fails the suite, and so does a row
 * here for an action the file no longer emits. */
export const ACTION_ROLES = {
    // ── read ──────────────────────────────────────────────────────────────
    'open': 'read',
    'reveal': 'read',
    'zotero': 'read',                    // zotero:// deep link
    'copy-source-url': 'read',
    'zl-load': 'read',
    'zl-all': 'read',                    // selection toggle
    'zl-retitle-dry': 'read',            // dry run
    'zc-open': 'read',                   // opens the create form; creates nothing
    'niz-identify': 'read',              // online lookup → regenerable memos only
    'notesync-preview': 'read',          // the exact bytes a create would write
    'notesync-inspect-create': 'read',   // observational, never repairs
    'notesync-merge': 'read',            // opens the merge VIEW; the write is separate

    // ── write ─────────────────────────────────────────────────────────────
    'apply-tags': 'write',
    'finder-apply-note': 'write',
    'finder-apply-finder': 'write',
    'finder-vocab-add': 'write',
    'pick-key': 'write',
    'pending': 'write',
    'no-match': 'write',
    'niz-set': 'write',
    'niz-bulk': 'write',                 // data-role="destructive" on [clear selected]
    'dup-dismiss': 'write',
    'nopdf-dismiss': 'write',
    'nopdf-dismiss-type': 'write',
    'zl-run': 'write',                   // data-role="read" on the dry run
    'zl-repoint': 'write',               // repairs a broken pointer
    'notesync-create': 'write',
    'notesync-create-all': 'write',
    'notesync-create-missing': 'write',
    'notesync-create-zotero': 'write',
    'notesync-bootstrap': 'write',
    'notesync-mirror': 'write',
    'notesync-migrate': 'write',
    'notesync-rename-note': 'write',
    'notesync-repair-create': 'write',
    'notesync-repair-keys': 'write',
    'notesync-resolve-create': 'write',
    'notesync-mark-satisfied': 'write',
    'notesync-mark-uncertain': 'write',
    'notesync-allow-retry': 'write',
    'notesync-adopt-key': 'write',

    // ── destructive ───────────────────────────────────────────────────────
    'detach': 'destructive',
    'dc-clear': 'destructive',
    'niz-clear': 'destructive',
    'dup-undismiss': 'destructive',      // withdraws a recorded decision
    'nopdf-restore-all': 'destructive',  // clears every "needs no PDF" decision
    'retire-tag': 'destructive',
    'zotlink-trash': 'destructive',
    'notesync-pull': 'destructive',      // overwrites the note's region
    'notesync-pull-all': 'destructive',
    'notesync-push': 'destructive',      // overwrites the Zotero note
    'notesync-push-all': 'destructive',
    'notesync-refresh-all': 'destructive',
    'notesync-unmigrate': 'destructive', // removes the markers
};

/** The five roles the whole app's buttons are drawn from. `primary`,
 *  `secondary` and `navigation` are class conventions in the markup
 *  (`btn-primary` / `btn-outline` / `.audit-section-btn`); the two below are
 *  what an INLINE verb can be, plus `read` for the ones that cost nothing. */
export const BUTTON_ROLES = ['primary', 'secondary', 'navigation', 'destructive', 'write', 'read'];

/**
 * Stamp `data-role` onto every inline verb under *root* that does not already
 * carry one, so the CSS can draw the grammar.
 *
 * Central rather than per-emit-site: there are ~159 emit sites and a new one
 * that forgot its class would be silently undifferentiated. A button that
 * already declares `data-role` is left alone — that is how one action serves
 * two verbs. A missing stamp is cosmetic (no glyph), never a correctness bug,
 * which is why the ratchet is on the TABLE and not on the DOM.
 */
export function applyActionRoles(root) {
    if (!root || typeof root.querySelectorAll !== 'function') return 0;
    let n = 0;
    for (const btn of root.querySelectorAll('[data-action]')) {
        if (btn.dataset.role) { n++; continue; }
        const role = ACTION_ROLES[btn.getAttribute('data-action')];
        if (!role) continue;
        btn.dataset.role = role;
        n++;
    }
    return n;
}

function _installRowActionDelegate(body) {
    if (body.dataset.rowDelegate === '1') return;
    body.dataset.rowDelegate = '1';
    body.addEventListener('click', async (e) => {
        const t = e.target;
        if (!t || typeof t.closest !== 'function') return;
        const invView = t.closest('[data-inv-view]');
        if (invView && body.contains(invView)) {
            _invViewPdf(invView.getAttribute('data-inv-view'));
            return;
        }
        const invDetail = t.closest('[data-inv-detail]');
        if (invDetail && body.contains(invDetail)) {
            _invToggleDetail(invDetail);
            return;
        }
        const btn = t.closest('.audit-link-btn');
        if (!btn || !body.contains(btn) || !_genericRowPass) return;
        {
            e.preventDefault();
            const action = btn.getAttribute('data-action');
            if (action === 'apply-tags') { await _applyTagDrift(btn); return; }
            if (action === 'finder-apply-note') { _finderDriftApply(btn, 'note'); return; }
            if (action === 'finder-apply-finder') { _finderDriftApply(btn, 'finder'); return; }
            if (action === 'finder-vocab-add') { _finderVocabAdd(btn); return; }
            if (action === 'dup-dismiss') { _dupDismiss(btn); return; }
            if (action === 'copy-source-url') { _copySourceUrl(btn); return; }
            if (action === 'zotlink-trash') { _zotlinkTrash(btn); return; }
            if (action === 'zl-repoint') { _zlRepointPicker(btn); return; }
            if (action === 'dc-clear') { _deadCurationClear(btn); return; }
            if (action === 'zl-load') { _zlLoad(btn); return; }
            if (action === 'zl-retitle-dry') { _zlRetitle(btn, true); return; }
            if (action === 'zl-all') { _zlSelectAll(btn.getAttribute('data-on') === '1'); return; }
            if (action === 'notesync-preview') { _noteSyncPreview(btn); return; }
            if (action === 'notesync-create') { _noteSyncCreate(btn, [btn.getAttribute('data-key')]); return; }
            if (action === 'notesync-migrate') { _noteSyncLayout(btn, 'migrate'); return; }
            if (action === 'notesync-refresh-all') { _noteSyncRefreshAll(btn); return; }
            if (action === 'notesync-mirror') { _noteSyncMirror(btn); return; }
            if (action === 'notesync-create-missing') {
                _noteSyncCreate(btn, _noteSyncVisibleKeys('missing_obsidian', _NOTESYNC_MAX_CREATE), true);
                return;
            }
            if (action === 'notesync-rename-note') { _noteSyncRenameNote(btn); return; }
            if (action === 'notesync-pull') { _noteSyncBulk(btn, 'pull', [btn.getAttribute('data-key')], { accept: btn.closest('tr')?.getAttribute('data-accept-pull') === '1' }); return; }
            if (action === 'notesync-push') { _noteSyncBulk(btn, 'push', [btn.getAttribute('data-key')], { accept: btn.closest('tr')?.getAttribute('data-accept-push') === '1' }); return; }
            if (action === 'notesync-pull-all') { _noteSyncBulk(btn, 'pull', _noteSyncVisibleKeys('pull', _NOTESYNC_MAX_PULL)); return; }
            if (action === 'notesync-push-all') { _noteSyncBulk(btn, 'push', _noteSyncVisibleKeys('push', _NOTESYNC_MAX_PUSH)); return; }
            if (action === 'notesync-bootstrap') { _noteSyncBootstrap(btn); return; }
            if (action === 'notesync-merge') { _noteSyncMerge(btn); return; }
            if (action === 'notesync-create-zotero') { _noteSyncCreateZotero(btn); return; }
            if (action === 'notesync-inspect-create') { _noteSyncInspectCreate(btn); return; }
            if (action === 'notesync-repair-create') { _noteSyncRepairCreate(btn); return; }
            if (action === 'notesync-repair-keys') { _noteSyncRepairKeys(btn); return; }
            if (action === 'notesync-resolve-create') { _noteSyncResolveCreate(btn); return; }
            if (action === 'notesync-mark-satisfied') { _noteSyncMarkSatisfied(btn); return; }
            if (action === 'notesync-mark-uncertain') { _noteSyncMarkUncertain(btn); return; }
            if (action === 'notesync-allow-retry') { _noteSyncAllowRetry(btn); return; }
            if (action === 'notesync-adopt-key') { _noteSyncAdoptKey(btn); return; }
            if (action === 'notesync-unmigrate') { _noteSyncLayout(btn, 'unmigrate'); return; }
            if (action === 'notesync-create-all') {
                // Scoped to the MISSING table: `tr[data-key]` also matches the
                // drifted rows, and a drifted note must never be posted to a
                // create (the server skips it, but offering it is the wrong
                // shape). Capped at the server's own per-request limit.
                const keys = [...document.querySelectorAll(
                    '#audit-report-body .notesync-missing tbody tr[data-key]')]
                    .map((tr) => tr.getAttribute('data-key'))
                    .filter((k, i, all) => k && all.indexOf(k) === i)
                    .slice(0, _NOTESYNC_MAX_CREATE);
                _noteSyncCreate(btn, keys, true); return;
            }
            if (action === 'nopdf-dismiss') { _noPdfDismiss(btn); return; }
            if (action === 'nopdf-dismiss-type') { _noPdfDismissType(btn); return; }
            if (action === 'nopdf-restore-all') { _noPdfRestoreAll(btn); return; }
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
                    message: `Assign ${key0} to this PDF? Writes the audit’s mapping.json only. `
                        + 'Undo: ⌫ in the deck view, or [unmap] on the Inventory pair.',
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
                    message: `Remove tag "${tag}" from every note / PDF / Zotero item that carries it (~${total} occurrences in the last scan; Zotero child-note tags and .bib keywords are not touched)${vocabBit}? Journalled — undoable in one click after (also Tagging → Restore).`,
                    confirmLabel: 'Retire everywhere',
                    onConfirm: () => _retireTag(btn, tag),
                });
                return;
            }
            if (action === 'no-match' || action === 'pending') {
                const state = action === 'pending' ? 'pending' : 'no_match';
                confirmInline(btn, {
                    message: (state === 'pending'
                        ? 'Mark this PDF as to add to Zotero later? '
                        : 'Mark this PDF as not a paper for Zotero? ')
                        + 'Writes the audit’s mapping.json curated queue; it is listed under '
                        + 'Not in Zotero. Undo: [clear] there.',
                    confirmLabel: 'Mark',
                    onConfirm: () => _markState(btn, state),
                });
                return;
            }
            if (action === 'niz-set') { _nizSetState(btn, [btn.getAttribute('data-pdf')], btn.getAttribute('data-state')); return; }
            if (action === 'niz-clear') { _nizClear(btn, [btn.getAttribute('data-pdf')]); return; }
            if (action === 'niz-bulk') { _nizBulk(btn); return; }
            if (action === 'niz-identify') { _nizIdentifyOne(btn, btn.getAttribute('data-pdf')); return; }
            if (action === 'zc-open') { _zcOpen(btn, btn.getAttribute('data-pdf')); return; }
            if (action === 'detach') { _detachConfirm(btn); return; }
            // An orphan Zotero item has no file to reveal — it is reachable
            // only through the zotero:// handler, which /api/audit/reveal
            // already speaks as its third body shape.
            if (action === 'zotero') {
                const zkey = btn.getAttribute('data-key');
                if (!zkey) return;
                // A group-library item needs the groups/<id> deep-link form —
                // the personal-library form silently selects nothing for it.
                const zgroup = parseInt(btn.getAttribute('data-group') || '', 10);
                const body = { zotero_key: zkey };
                if (Number.isFinite(zgroup) && zgroup > 0) body.zotero_group_id = zgroup;
                try {
                    const r = await secureFetch('/api/audit/reveal', {
                        method: 'POST',
                        body: JSON.stringify(body),
                    });
                    if (!r.ok) {
                        const d = await r.json().catch(() => ({}));
                        _showError(d.error || `Could not open in Zotero (${r.status})`);
                    }
                } catch (err) {
                    _showError(String(err));
                }
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
        }
    });
}

// --- Axis-ii write/curation actions ---------------------------------------

/** "Not a duplicate": confirmInline ceremony, then record the dismissal and
 * mark the row in place (snapshot semantics — the row itself stays until the
 * next scan, showing its outcome + an undo button, the mapping-deck pattern). */
/** Show the exact bytes a create would write, before writing them. */
async function _noteSyncPreview(btn) {
    const key = btn.getAttribute('data-key');
    if (!key) return;
    // Same in-flight guard as _noteSyncMerge: two clicks during a slow request
    // inserted two panels, and the toggle keys on nextElementSibling so
    // neither could be closed afterwards.
    if (btn.dataset.busy === '1') return;
    btn.dataset.busy = '1';
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    try {
        const r = await secureFetch('/api/notesync/preview', {
            method: 'POST', body: JSON.stringify({ citation_key: key }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { _showError(d.error || `Preview failed (${r.status})`); return; }
        const row = btn.closest('tr');
        if (!row) return;
        // Inline, under the row: a modal for a 300-byte note would be heavier
        // than the thing it shows.
        const existing = row.nextElementSibling;
        if (existing && existing.classList.contains('audit-notesync-preview')) {
            existing.remove();
            btn.setAttribute('aria-expanded', 'false');
            _nsSay(`Preview for ${key} closed.`);
            return;
        }
        const tr = document.createElement('tr');
        tr.className = 'audit-notesync-preview';
        // Marks this row as belonging to the one above it, so the report's
        // sort/filter (ui.js::installTableControls) moves and hides it WITH
        // its parent. Without it, sorting appendChild-moved every DATA row to
        // the end of the tbody and left this panel behind — so it floated to
        // the top, stranded under an unrelated note whose [update] button it
        // then appeared to describe.
        tr.setAttribute('data-row-attachment', '');
        const td = document.createElement('td');
        td.colSpan = row.children.length;
        const pre = document.createElement('pre');
        pre.textContent = d.text || '';        // never innerHTML — vault content
        td.appendChild(pre);
        tr.appendChild(td);
        row.after(tr);
        btn.setAttribute('aria-expanded', 'true');
        _nsSay(`Showing the note that would be created for ${key}.`);
    } catch (e) {
        logError('notesync preview failed: ' + String(e));
        _showError(String(e));
    } finally {
        delete btn.dataset.busy;
        btn.disabled = false;
        btn.removeAttribute('aria-busy');
    }
}

/** Move one Zotero attachment to Zotero's trash. Confirm-gated.
 *
 * Trash, never delete: the file stays on disk (for an imported attachment,
 * until the user empties Zotero's trash) and the op is exactly reversible from
 * the Tagging sub-view's Restore panel. Annotations ride the attachment, so a
 * row that has them takes a second, differently-worded acknowledgement — the
 * server enforces that independently (409 `would_trash_annotations`), and this
 * only sends the flag for a row that actually needs it. */
//: Mirror of `api/routes/zotlink.py::_MAX_KEYS`. The bound is not about
//: request size: every trashed attachment leaves a PERMANENTLY non-evictable
//: op in the tagging journal (its restore mapping lives inline, with no
//: snapshot behind it), so the batch is capped for the same reason
//: `_HARMONIZE_MAX_KEYS` is. Say the real number rather than offering a button
//: whose request would 400.
const _ZOTLINK_MAX_KEYS = 200;

// ── Zotero attachments: selection + the confirmed bulk de-link ─────────────
//
// The route has accepted `item_keys: [...]` up to 200 since it shipped — with
// SSE progress, the annotation acknowledgement and one `batch_id` for the whole
// run — but the report only ever offered a per-row verb, so de-linking a
// scanned folder's worth of attachments meant N confirms. This is the missing
// client half; no route changes.
//
// Selection model and lifetime are Tag Harmony's verbatim: a Set of ids, a
// live-count button disabled at zero, a busy flag that freezes the whole
// surface (a checkbox change must not re-arm the button mid-stream and let a
// second confirm launch a concurrent run), and NO persistence — the rows are
// one scan's snapshot.
let _zotlinkSelected = new Set();
let _zotlinkBusy = false;

/** Reset the selection — a new report payload is a different snapshot. */
function zotlinkResetSelection() {
    _zotlinkSelected = new Set();
}

/** Annotations carried by the current selection, from the rendered rows.
 *
 *  Summed over the SELECTION rather than over the table, and read from each
 *  checkbox's own `data-annotations`, so the figure the confirm promises is the
 *  figure the server will compute for the same keys. The Set is deduplicated by
 *  construction, which matters: the route deduplicates before deriving any
 *  count precisely because summing per LISTED key once claimed twice the
 *  highlights at risk. */
function _zotlinkAnnotationTotal() {
    const body = document.getElementById('audit-report-body');
    if (!body) return { rows: 0, annotations: 0 };
    let rows = 0;
    let annotations = 0;
    body.querySelectorAll('input[data-zotlink-key]').forEach((cb) => {
        if (!_zotlinkSelected.has(cb.getAttribute('data-zotlink-key'))) return;
        const n = parseInt(cb.getAttribute('data-annotations') || '0', 10) || 0;
        if (n) { rows += 1; annotations += n; }
    });
    return { rows, annotations };
}

function _zotlinkUpdateButton() {
    const btn = document.getElementById('zotlink-apply-btn');
    if (!btn) return;
    const n = _zotlinkSelected.size;
    btn.disabled = _zotlinkBusy || n === 0;
    btn.textContent = n ? `Remove selected from Zotero (${n})`
                        : 'Remove selected from Zotero';
    const over = document.getElementById('zotlink-cap-note');
    if (over) {
        over.textContent = n > _ZOTLINK_MAX_KEYS
            ? `${n} selected — at most ${_ZOTLINK_MAX_KEYS} per run; clear some first.`
            : '';
    }
    // Over the cap the server would 400 the whole batch. Refuse client-side so
    // the user is told before spending a confirm, not after.
    if (n > _ZOTLINK_MAX_KEYS) btn.disabled = true;
}

/** Freeze/unfreeze the whole de-link surface while a run is in flight. */
function _zotlinkSetAllDisabled(on) {
    _zotlinkBusy = on;
    const body = document.getElementById('audit-report-body');
    if (body) {
        body.querySelectorAll('input[data-zotlink-key]').forEach((cb) => {
            // A row already trashed in this session stays disabled either way.
            cb.disabled = on || cb.hasAttribute('data-zotlink-done');
        });
        body.querySelectorAll('[data-zotlink-all]').forEach((b) => { b.disabled = on; });
        // The per-row verbs would otherwise race the batch for the gate.
        body.querySelectorAll('[data-action="zotlink-trash"]').forEach((b) => {
            if (!b.hasAttribute('data-zotlink-done')) b.disabled = on;
        });
    }
    _zotlinkUpdateButton();
}

function _zotlinkToggle(key, on) {
    if (on) _zotlinkSelected.add(key); else _zotlinkSelected.delete(key);
    _zotlinkUpdateButton();
}

function _zotlinkSelectAll(on) {
    const body = document.getElementById('audit-report-body');
    if (!body) return;
    body.querySelectorAll('input[data-zotlink-key]').forEach((cb) => {
        if (cb.disabled) return;
        // "Select all" respects the filter: `installTableControls` hides a
        // non-matching row rather than removing it, so without this a filtered
        // "select all" would silently pick up every row the user had just
        // narrowed away — and this batch is destructive.
        const row = cb.closest('tr');
        if (on && row && row.hidden) return;
        cb.checked = on;
        _zotlinkToggle(cb.getAttribute('data-zotlink-key'), on);
    });
}

function _zotlinkStatusHost() {
    // Report-identity guard, as for harmonize/retire: only this report may host
    // de-link status and its undo button, or a mid-stream tab switch sprays
    // both into a foreign report.
    if (_currentReport !== 'zotero_attachments') return null;
    const body = document.getElementById('audit-report-body');
    if (!body) return null;
    let host = body.querySelector('#audit-zotlink-status');
    if (!host) {
        host = document.createElement('div');
        host.id = 'audit-zotlink-status';
        host.className = 'muted';
        const text = document.createElement('span');
        text.className = 'audit-zotlink-status-text';
        host.appendChild(text);
        body.insertBefore(host, body.firstChild);
    }
    return host;
}

function _zotlinkSay(msg, isErr, tone) {
    const host = _zotlinkStatusHost();
    // The TEXT span, never the host: an undo button is appended beside it and
    // painting the host would take the undo away with the message.
    if (host) statusLine(host.querySelector('.audit-zotlink-status-text'),
        { tone: tone || (isErr ? 'error' : 'info'), text: msg || '' });
    _announceBatchProgress(msg, isErr);
}

/** Mark one row's outcome in place. The row is never removed: this report is a
 *  view of ONE cached scan, and deleting a row would imply the snapshot moved. */
function _zotlinkMarkRow(key, label, ok) {
    const body = document.getElementById('audit-report-body');
    if (!body) return;
    const cb = body.querySelector(`input[data-zotlink-key="${CSS.escape(key)}"]`);
    const row = cb ? cb.closest('tr') : null;
    if (!row) return;
    if (ok) {
        cb.checked = false;
        cb.disabled = true;
        cb.setAttribute('data-zotlink-done', '1');
        row.classList.add('audit-row-decided');
    }
    const verb = row.querySelector('[data-action="zotlink-trash"]');
    if (verb) {
        verb.textContent = label;
        verb.disabled = ok;
        if (ok) verb.setAttribute('data-zotlink-done', '1');
    }
}

/**
 * Run the confirmed bulk de-link over the current selection.
 *
 * Sends KEYS only: the route re-derives every attachment from the last scan's
 * index and 404s a key it did not see, so the client cannot name an arbitrary
 * Zotero item however the request is shaped.
 */
async function _zotlinkBulk(btn, keys, acceptAnnotationLoss) {
    if (!keys.length || _zotlinkBusy) return;
    _zotlinkSetAllDisabled(true);
    let terminal = null;
    let streamErr = null;
    try {
        await consumeSSE('/api/zotlink/trash', {
            method: 'POST',
            body: JSON.stringify({
                item_keys: keys, confirm: true,
                // Only when the selection actually contains annotated rows: a
                // blanket true would disarm the server's second gate for every
                // batch, which is the whole reason it is a separate gate.
                ...(acceptAnnotationLoss ? { accept_annotation_loss: true } : {}),
            }),
        }, {
            onInfo: (info) => _zotlinkSay(info),
            onOther: (frame) => { if (frame && frame.zotlink) terminal = frame.zotlink; },
            onError: (msg) => { streamErr = msg; },
        });
        if (streamErr) { _zotlinkSay(streamErr, true); return; }
        if (!terminal) {
            _zotlinkSay('The de-link ended without reporting an outcome.', true);
            return;
        }
        // Per-key truth, exactly as the route reports it: a partial batch must
        // read as partial. Failed keys keep their checkbox so a retry picks up
        // the remainder rather than re-sending the whole selection.
        const results = terminal.results || {};
        let ok = 0;
        const failed = [];
        for (const key of keys) {
            const res = results[key] || {};
            if (res.ok) { ok += 1; _zotlinkMarkRow(key, '[trashed]', true); }
            else {
                failed.push(key);
                _zotlinkMarkRow(key, '[failed]', false);
                _zotlinkSelected.add(key);
            }
        }
        let msg = `${ok} of ${keys.length} attachment(s) moved to Zotero\u2019s trash.`;
        if (terminal.cancelled) msg += ' Run cancelled — the rest were not attempted.';
        if (failed.length) {
            msg += ` ${failed.length} failed and stay selected for a retry.`;
            if (terminal.failures && terminal.failures.length) {
                msg += ` ${terminal.failures.slice(0, 3).join('; ')}`
                    + (terminal.failures.length > 3 ? '…' : '');
            }
        }
        if (terminal.journal_warning) msg += ` ${terminal.journal_warning}`;
        if (terminal.note) msg += ` ${terminal.note}`;
        _zotlinkSay(msg, failed.length > 0);
        _zotlinkUpdateButton();

        const host = _zotlinkStatusHost();
        if (terminal.batch_id && ok && host) {
            const undo = document.createElement('button');
            undo.type = 'button';
            undo.className = 'audit-link-btn';
            undo.textContent = `[undo de-link (${ok} attachment(s))]`;
            undo.addEventListener('click', () => confirmInline(undo, {
                message: `Take ${ok} attachment(s) back out of Zotero\u2019s trash?`,
                confirmLabel: 'Undo de-link',
                onConfirm: async () => {
                    undo.disabled = true;
                    // Cancellable like the harmonize/retire undos: a batch
                    // revert is a Zotero round-trip per op, and abandoning the
                    // WAIT must not look like abandoning the work.
                    const ctrl = new AbortController();
                    const cancel = _mkUndoCancel(host, ctrl, 'de-link');
                    let done = null;
                    try {
                        await consumeSSE('/api/tagging/restore', {
                            method: 'POST',
                            body: JSON.stringify({ batch_id: terminal.batch_id, confirm: true }),
                            signal: ctrl.signal,
                        }, {
                            onInfo: (info) => _zotlinkSay(`Undo: ${info}`),
                            onOther: (f) => { if (f && f.restore) done = f.restore; },
                            onError: (err) => { throw new Error(err); },
                        });
                        if (!done) {
                            _zotlinkSay('Undo produced no result.', true);
                            undo.disabled = false; return;
                        }
                        if (done.ok === false && done.reverted >= done.total) {
                            _zotlinkSay('Undo is not fully recorded or was interrupted. Inspect Restore before retrying.', true);
                            undo.disabled = false;
                            return;
                        }
                        if (done.reverted < done.total) {
                            // The server keeps a failed op `applied` (journal
                            // conservatism), so the button stays and a re-run
                            // picks up exactly the remainder.
                            const left = done.total - done.reverted;
                            _zotlinkSay(`Undo: ${done.reverted}/${done.total} reverted — `
                                + `${left} remain and stay retryable.`, true);
                            undo.textContent = `[retry undo de-link (${left})]`;
                            undo.disabled = false;
                            return;
                        }
                        _zotlinkSay(`Undo: ${done.reverted}/${done.total} attachment(s) `
                            + 'restored. They reappear here after Zotero syncs and you rescan.');
                        undo.remove();
                    } catch (err) {
                        if (err && err.name === 'AbortError') {
                            _zotlinkSay('Undo cancelled — attachments already restored stay '
                                + 'restored; use the undo button again for the rest.');
                        } else {
                            logError('zotlink batch undo failed: ' + String(err));
                            _zotlinkSay(`Undo failed: ${String(err)}`, true);
                        }
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
        logError('zotlink bulk trash failed: ' + String(e));
        _zotlinkSay(String(e), true);
    } finally {
        _zotlinkSetAllDisabled(false);
    }
}

function _zotlinkTrash(btn) {
    const key = btn.getAttribute('data-key');
    if (!key) return;
    const ann = parseInt(btn.getAttribute('data-annotations') || '0', 10) || 0;
    const file = btn.getAttribute('data-file') || 'this attachment';
    confirmInline(btn, {
        message: ann
            ? `Move ${file} to Zotero\u2019s trash? It carries ${ann} Zotero `
              + 'annotation(s), which go to the trash with it. The file itself is '
              + 'not deleted, and your PDF\u2194bib mapping is unaffected. '
              + 'Reversible from Tagging \u2192 Restore.'
            : `Move ${file} to Zotero\u2019s trash? The file itself is not deleted, `
              + 'and your PDF\u2194bib mapping is unaffected. Reversible from '
              + 'Tagging \u2192 Restore.',
        confirmLabel: ann ? `Trash (loses ${ann} annotation${ann === 1 ? '' : 's'})`
            : 'Move to trash',
        onConfirm: async () => {
            try {
                // SSE since 2026-08-30: the route's network phase no longer
                // runs under the vault op-lock, and no longer occupies a
                // Waitress worker for its whole budget. Pre-stream refusals
                // (400/404/409/429/503) still arrive as real status codes with
                // a JSON body, which consumeSSE surfaces through onError.
                let d = null;
                let streamErr = null;
                btn.disabled = true;
                await consumeSSE('/api/zotlink/trash', {
                    method: 'POST',
                    body: JSON.stringify({
                        item_keys: [key], confirm: true,
                        // Only for a row that has annotations: a blanket true
                        // would disarm the server's second gate everywhere.
                        ...(ann ? { accept_annotation_loss: true } : {}),
                    }),
                }, {
                    onInfo: (info) => announceStatus(info),
                    onOther: (frame) => { if (frame && frame.zotlink) d = frame.zotlink; },
                    onError: (msg) => { streamErr = msg; },
                });
                if (streamErr) { btn.disabled = false; _showError(streamErr); return; }
                if (!d) {
                    btn.disabled = false;
                    _showError('The de-link ended without reporting an outcome.');
                    return;
                }
                const res = (d.results || {})[key] || {};
                if (!res.ok) {
                    btn.disabled = false;
                    _showError(res.message || 'Zotero refused the change.');
                    return;
                }
                announceStatus(`${file} moved to Zotero\u2019s trash. `
                    + (d.note || '')
                    + ' Undo it from the Tagging tab\u2019s Restore panel.');
                // The row itself stays: this report is a view of ONE cached
                // scan, and quietly removing a row would imply the snapshot
                // changed. Mark the outcome in place instead (the mapping
                // deck's pattern).
                const row = btn.closest('tr');
                if (row) {
                    btn.textContent = '[trashed]';       // already disabled above
                    row.classList.add('audit-row-decided');
                }
            } catch (e) {
                btn.disabled = false;
                logError('zotlink trash failed: ' + String(e));
                _showError(String(e));
            }
        },
    });
}

/** Create the missing reference note(s). Confirm-gated: it writes vault files.
 *
 * Only ever CREATES — the server refuses a key whose note exists, whatever its
 * drift state, and says so per key. */
function _noteSyncCreate(btn, keys, bulk = false) {
    const clean = (keys || []).filter(Boolean);
    if (!clean.length) return;
    confirmInline(btn, {
        message: bulk
            ? `Create ${clean.length} missing reference note${clean.length === 1 ? '' : 's'} `
              + 'in the vault? Reversible from Settings → History and Restore.'
            : `Create the reference note for ${clean[0]}? `
              + 'Reversible from Settings → History and Restore.',
        confirmLabel: bulk ? `Create ${clean.length}` : 'Create',
        onConfirm: async () => {
            try {
                const r = await secureFetch('/api/notesync/create', {
                    method: 'POST',
                    body: JSON.stringify({ citation_keys: clean, confirm: true }),
                });
                const d = await r.json().catch(() => ({}));
                if (!r.ok) { _nsSay(d.error || `Create failed (${r.status})`, true); return; }
                const skipped = Object.entries(d.results || {})
                    .filter(([, v]) => !v.ok);
                _nsSay(`Created ${d.created} note(s)`
                    + (skipped.length ? `; ${skipped.length} skipped` : '')
                    + '. Re-run the scan to refresh the join.');
                await _loadReport('note_sync');
            } catch (e) {
                logError('notesync create failed: ' + String(e));
                _nsSay(String(e), true);
            }
        },
    });
}

/** Layout migration (Phase 2, 2026-09-04): dry run, then a confirmed write.
 *
 * Two requests by design. The dry run is what the confirm text quotes (how
 * many notes, how many refused and why); the write recomputes its own plan
 * server-side from the bytes on disk, so a note edited between the two is
 * skipped as `changed_on_disk` rather than written from a stale plan. The
 * server refuses the write unless the notes folder is committed in the
 * vault's git — that, plus the exact-inverse [undo], is the whole undo story,
 * because 1,235 notes cannot ride the refactor journal's 200-op window. */
function _nsRefreshPlanDetails(btn, plan) {
    const existing = document.getElementById('ns-refresh-plan');
    if (existing) existing.remove();
    const det = document.createElement('details');
    det.id = 'ns-refresh-plan';
    det.className = 'audit-legend';
    det.open = true;
    const sum = document.createElement('summary');
    sum.textContent = `Dry run: ${plan.planned || 0} note(s) would be rewritten`;
    det.appendChild(sum);
    const addList = (title, items) => {
        if (!items || !items.length) return;
        const p = document.createElement('p');
        p.className = 'audit-summary-note';
        p.textContent = `${title} (${items.length}):`;
        det.appendChild(p);
        const ul = document.createElement('ul');
        for (const k of items.slice(0, 40)) {
            const li = document.createElement('li');
            li.textContent = k;
            ul.appendChild(li);
        }
        if (items.length > 40) {
            const li = document.createElement('li');
            li.textContent = `…and ${items.length - 40} more`;
            ul.appendChild(li);
        }
        det.appendChild(ul);
    };
    addList('Would be rewritten', plan.planned_keys || []);
    addList('Lossy markup (images or formatting the converter flattens)', plan.lossy_markup || []);
    const listed = plan.listed || {};
    addList('Deleted in Zotero (listed, not touched)', listed.deleted_in_zotero || []);
    addList('No record (listed, not touched)', listed.no_record || []);
    btn.insertAdjacentElement('afterend', det);
}

/** How old the cached scan is, in words, plus the remedy when it is stale.
 *  The create half of a mirror is planned against a FRESH Zotero read, but
 *  the citation-key ↔ record JOIN is the scan's, so a record that appeared
 *  since the scan is invisible whatever Zotero holds (the `tagging batch`
 *  disclosure, same reason). */
function _nsScanAge(scannedAt) {
    if (!scannedAt) return ' The scan time is unknown, so this plan may be built on a stale snapshot.';
    const ageMin = Math.max(0, Math.round((Date.now() / 1000 - scannedAt) / 60));
    const age = ageMin < 120 ? `${ageMin} min ago` : `${Math.floor(ageMin / 60)} h ago`;
    return ` Planned against the audit scan from ${age}.`
        + (ageMin >= 30 ? ' Records added since it are invisible — run `papermind audit scan` (or Run Scan) first.' : '');
}

/** The mirror dry run, rendered as an expandable plan beside the button. */
function _nsMirrorPlanDetails(btn, plan) {
    const existing = document.getElementById('ns-mirror-plan');
    if (existing) existing.remove();
    const det = document.createElement('details');
    det.id = 'ns-mirror-plan';
    det.className = 'audit-legend';
    det.open = true;
    const sum = document.createElement('summary');
    sum.textContent = `Dry run: create ${plan.to_create || 0}, refresh ${plan.to_refresh || 0}, `
        + `baseline ${plan.to_baseline || 0}`;
    det.appendChild(sum);
    const addList = (title, items) => {
        if (!items || !items.length) return;
        const p = document.createElement('p');
        p.className = 'audit-summary-note';
        p.textContent = `${title} (${items.length}):`;
        det.appendChild(p);
        const ul = document.createElement('ul');
        for (const k of items.slice(0, 40)) {
            const li = document.createElement('li');
            li.textContent = k;
            ul.appendChild(li);
        }
        if (items.length > 40) {
            const li = document.createElement('li');
            li.textContent = `…and ${items.length - 40} more`;
            ul.appendChild(li);
        }
        det.appendChild(ul);
    };
    addList('Would be created', plan.create_keys || []);
    addList('Would be rewritten from Zotero', plan.refresh_keys || []);
    addList('Lossy markup (images or formatting the converter flattens)', plan.lossy_markup || []);
    const listed = plan.listed || {};
    for (const [reason, keys] of Object.entries(listed)) {
        addList(`${reason} (listed, not touched)`, keys);
    }
    const skippedCreate = (plan.skipped || {}).create || {};
    for (const [reason, keys] of Object.entries(skippedCreate)) {
        addList(`Not created — ${reason}`, keys);
    }
    btn.insertAdjacentElement('afterend', det);
}

/**
 * What the confirm says about git, from the dry run's per-file check
 * (2026-09-21, library-workflow lot 1). Nothing to rewrite → nothing to say.
 * A failed check is quoted so the user can commit first instead of learning
 * it from a refusal; the server re-checks on the confirm either way.
 */
function _nsGitNote(rewrites, ok, detail, commit) {
    if (!rewrites) return '';
    if (!ok) return ` Git cannot undo this yet — ${detail || 'the notes are not committed.'} The confirm will be refused.`;
    return commit ? ` Undo point: git commit ${String(commit).slice(0, 12)}.` : '';
}

/**
 * "Mirror from Zotero" — create + refresh + baseline in ONE confirmed action.
 *
 * Two requests by design, like `_noteSyncRefreshAll`: the dry run is what
 * the confirm quotes, and the confirm sends back its `plan_digest`. The
 * server recomputes the plan and refuses (`plan_changed`, nothing written)
 * unless it is the one the user reviewed (2026-09-21).
 *
 * The confirm has to name THREE different undo stories, because the phases
 * have three: creates are journalled (Settings → History and Restore), region
 * rewrites are not (the vault's git is their undo), and sync points touch
 * nothing in the vault or Zotero at all.
 */
async function _noteSyncMirror(btn) {
    if (btn.dataset.busy === '1') return;
    btn.dataset.busy = '1';
    let plan = null;
    let planErr = null;
    try {
        await consumeSSE('/api/notesync/mirror', { requireDone: false,
            method: 'POST', body: JSON.stringify({ dry_run: true }),
        }, {
            onInfo: (info) => _nsSay(info),
            onOther: (frame) => { if (frame && frame.mirror) plan = frame.mirror; },
            onError: (msg) => { planErr = msg; },
        });
    } catch (e) {
        logError('notesync mirror dry run failed: ' + String(e));
        planErr = String(e);
    } finally {
        btn.dataset.busy = '';
    }
    if (planErr) { _nsSay(planErr, true); return; }
    if (!plan) { _nsSay('The dry run ended without reporting a plan.', true); return; }

    const listed = Object.entries(plan.listed || {});
    const listedText = listed.length
        ? ` Listed, not touched: ${listed.map(([r, k]) => `${r} (${k.length})`).join(', ')}.` : '';
    const age = _nsScanAge(plan.scanned_at);
    _nsMirrorPlanDetails(btn, plan);
    if (!plan.to_create && !plan.to_refresh && !plan.to_baseline && !plan.stale_bases) {
        _nsSay(`Nothing to mirror: ${plan.unchanged} note(s) already match Zotero and are baselined.`
            + `${listedText}${age}`);
        return;
    }
    // The cap is the refactor journal's, never chunked past — say "100 of N"
    // rather than implying the rest were not there.
    const capped = plan.create_capped_at
        ? ` Only ${plan.to_create} of ${plan.create_total} missing note(s) fit one undoable batch — re-run for the rest.`
        : '';
    confirmInline(btn, {
        tone: 'danger',
        message: `Create ${plan.to_create} note(s), rewrite the Zotero region of ${plan.to_refresh} note(s), `
            + `and record ${plan.to_baseline} sync point(s)?${capped}`
            + (plan.lossy_markup?.length ? ` ${plan.lossy_markup.length} carry images or markup the converter flattens.` : '')
            + (plan.stale_bases ? ` ${plan.stale_bases} stale sync record(s) will be removed.` : '')
            + ' Creates are reversible from Settings → History and Restore. The region rewrites are NOT journalled:'
            + ' git must hold every note they replace (checked file by file), which is their only undo, and they'
            + ' replace whatever the region held. Sync points write nothing to your vault or to Zotero.'
            + _nsGitNote(plan.to_refresh, plan.git_clean, plan.git_detail, plan.git_restore_commit)
            + `${listedText}${age}`,
        confirmLabel: `Mirror ${plan.to_create + plan.to_refresh}`,
        onConfirm: async () => {
            let done = null;
            let streamErr = null;
            try {
                // Bound to the plan quoted above (`plan_changed` otherwise).
                await consumeSSE('/api/notesync/mirror', { requireDone: false,
                    method: 'POST', body: JSON.stringify({ confirm: true, plan_digest: plan.plan_digest }),
                }, {
                    onInfo: (info) => _nsSay(info),
                    onOther: (frame) => { if (frame && frame.mirror) done = frame.mirror; },
                    onError: (msg) => { streamErr = msg; },
                });
            } catch (e) {
                logError('notesync mirror failed: ' + String(e));
                streamErr = String(e);
            }
            if (streamErr) { _nsSay(streamErr, true); return; }
            if (!done) { _nsSay('The mirror ended without reporting an outcome.', true); return; }
            const failedN = Object.keys(done.failed || {}).length;
            // What each stop means in words (2026-09-21 review: "Stopped after
            // the plan changed step" named a step that never ran).
            const STOP_WORDS = {
                plan_changed: 'Nothing was written: the notes or Zotero changed since the plan you reviewed. Run it again to review the new plan.',
                git_not_clean: 'Nothing was written: git cannot undo the rewrites.',
                cancelled: 'Stopped: cancelled partway.',
                create_failed: 'Stopped: the notes could not be created.',
                indexing: 'Stopped: indexing is in progress.',
            };
            const stopped = done.stopped
                ? ` ${STOP_WORDS[done.stopped] || `Stopped (${done.stopped}).`}${done.error ? ` ${done.error}` : ''}`
                : '';
            _nsSay(`Created ${done.created}, refreshed ${done.refreshed}, baselined ${done.bootstrapped} note(s).`
                + (done.stale_bases_removed ? ` ${done.stale_bases_removed} stale sync record(s) removed.` : '')
                + (failedN ? ` ${failedN} failed: ${Object.entries(done.failed).slice(0, 3)
                    .map(([k, v]) => `${k} (${v})`).join(', ')}${failedN > 3 ? '…' : ''}.` : '')
                + (done.aborted ? ' Aborted — the indexing lock was lost partway.' : '')
                + stopped
                + (done.created ? ' Re-run the scan to refresh the join.' : ''),
                !done.ok);
            await _loadReport('note_sync');
        },
    });
}

/** Zotero-authoritative sweep: dry run → confirm → write. Mirrors
 * `_noteSyncLayout`: the plan shown in the confirm is a dry run, and the
 * server recomputes it on the write. Deleted-in-Zotero and no-record notes
 * come back in `listed` and are named, never touched. */
async function _noteSyncRefreshAll(btn) {
    if (btn.dataset.busy === '1') return;
    btn.dataset.busy = '1';
    let plan;
    try {
        const r = await secureFetch('/api/notesync/refresh-all', {
            method: 'POST', body: JSON.stringify({ dry_run: true }),
        });
        plan = await r.json().catch(() => ({}));
        if (!r.ok) { _nsSay(plan.error || `Dry run failed (${r.status})`, true); return; }
    } catch (e) {
        logError('notesync refresh-all dry run failed: ' + String(e));
        _nsSay(String(e), true); return;
    } finally {
        btn.dataset.busy = '';
    }
    const listed = Object.entries(plan.listed || {});
    const listedText = listed.length
        ? ` Listed, not touched: ${listed.map(([r, k]) => `${r} (${k.length})`).join(', ')}.` : '';
    if (!plan.planned && !plan.stale_bases) {
        _nsSay(`Every note already matches Zotero (${plan.unchanged} checked).${listedText}`);
        _nsRefreshPlanDetails(btn, plan);
        return;
    }
    _nsRefreshPlanDetails(btn, plan);
    confirmInline(btn, {
        message: `Rewrite the Zotero region of ${plan.planned} note(s) from Zotero, whatever they hold`
            + (plan.lossy_markup?.length ? ` (${plan.lossy_markup.length} carry images or markup the converter flattens)` : '')
            + (plan.stale_bases ? `; remove ${plan.stale_bases} stale sync record(s)` : '')
            + `.${listedText} Not journalled: git must hold every note it replaces (checked file by file), which is the undo.`
            + _nsGitNote(plan.planned, plan.git_restorable, plan.git_detail, plan.git_restore_commit),
        confirmLabel: `Refresh ${plan.planned}`,
        tone: 'danger',
        onConfirm: async () => {
            try {
                // Bound to the plan shown above: the server refuses
                // (`plan_changed`) if the notes or Zotero moved since.
                const r = await secureFetch('/api/notesync/refresh-all', {
                    method: 'POST', body: JSON.stringify({ confirm: true, plan_digest: plan.plan_digest }),
                });
                const d = await r.json().catch(() => ({}));
                if (!r.ok) { _nsSay(d.error || `refresh failed (${r.status})`, true); return; }
                // `failed` holds every failure, prefixed by effect; count the
                // note writes apart from the sync-record ones (2026-09-21 review:
                // a base failure read as a note that failed to refresh).
                const failed = Object.entries(d.failed || {});
                const noteFailedN = failed.filter(([, v]) => !/^(base|stale_base):/.test(String(v))).length;
                const baseFailedN = Object.keys(d.base_failed || {}).length;
                const staleFailedN = Object.keys(d.stale_base_failed || {}).length;
                const staleKeptN = Object.keys(d.stale_bases_kept || {}).length;
                _nsSay(`Refreshed ${d.written} note(s) from Zotero`
                    + (noteFailedN ? `; ${noteFailedN} could not be refreshed` : '')
                    + (d.aborted ? '; aborted (lock lost)' : '') + (d.cancelled ? '; cancelled partway' : '')
                    + `; ${d.bases_written} sync record(s) written`
                    + (baseFailedN ? ` (${baseFailedN} could not be saved)` : '')
                    + (d.stale_bases_removed ? `; ${d.stale_bases_removed} stale record(s) removed` : '')
                    + (staleFailedN ? ` (${staleFailedN} could not be removed)` : '')
                    + (staleKeptN ? `; ${staleKeptN} kept because they changed since the plan` : '')
                    + (d.git_restore_commit ? `. Undo: restore from git commit ${String(d.git_restore_commit).slice(0, 12)}` : '')
                    + `.${listedText}`, failed.length > 0 || d.aborted || d.cancelled);
                await _loadReport('note_sync');
            } catch (e) {
                logError('notesync refresh-all failed: ' + String(e));
                _nsSay(String(e), true);
            }
        },
    });
}

async function _noteSyncLayout(btn, direction) {
    if (btn.dataset.busy === '1') return;
    btn.dataset.busy = '1';
    let plan;
    try {
        const r = await secureFetch(`/api/notesync/${direction}`, {
            method: 'POST', body: JSON.stringify({ dry_run: true }),
        });
        plan = await r.json().catch(() => ({}));
        if (!r.ok) { _nsSay(plan.error || `Dry run failed (${r.status})`, true); return; }
    } catch (e) {
        logError('notesync layout dry run failed: ' + String(e));
        _nsSay(String(e), true); return;
    } finally {
        btn.dataset.busy = '';
    }
    const skipped = Object.entries(plan.skipped || {});
    const skippedN = skipped.reduce((n, [, keys]) => n + keys.length, 0);
    if (!plan.planned) {
        _nsSay(`Nothing to ${direction}.`
            + (skippedN ? ` ${skippedN} note(s) skipped: ${skipped.map(([r, k]) => `${r} (${k.length})`).join(', ')}.` : ''));
        return;
    }
    const verb = direction === 'migrate' ? 'Wrap' : 'Unwrap';
    confirmInline(btn, {
        message: `${verb} the Zotero region of ${plan.planned} note(s)`
            + (direction === 'migrate' ? ' in the marker pair and add the Zotero keys to their frontmatter' : '')
            + (skippedN ? `; ${skippedN} skipped (${skipped.map(([r, k]) => `${r}: ${k.length}`).join(', ')})` : '')
            + '. Not journalled: the notes folder must be committed in the vault\u2019s git, which is the undo.',
        confirmLabel: `${verb} ${plan.planned}`,
        onConfirm: async () => {
            try {
                const r = await secureFetch(`/api/notesync/${direction}`, {
                    method: 'POST', body: JSON.stringify({ confirm: true }),
                });
                const d = await r.json().catch(() => ({}));
                if (!r.ok) { _nsSay(d.error || `${direction} failed (${r.status})`, true); return; }
                const failedN = Object.keys(d.failed || {}).length;
                _nsSay(`${direction === 'migrate' ? 'Migrated' : 'Unmigrated'} ${d.written} note(s)`
                    + (failedN ? `; ${failedN} failed` : '') + (d.aborted ? '; aborted (lock lost)' : '')
                    + (direction === 'migrate'
                        ? '. New imports: add the marker lines around {{note.note}} in the Zotero Integration template (see the console for the exact lines).'
                        : '.'));
                if (direction === 'migrate' && d.template_hint) console.info('Zotero Integration template lines:\n' + d.template_hint);
                await _loadReport('note_sync');
            } catch (e) {
                logError('notesync layout change failed: ' + String(e));
                _nsSay(String(e), true);
            }
        },
    });
}

/** Copy a printed web page's source URL for Zotero's "add by URL".
 *
 * For the browser_print class this is the entire remedy: no catalogue holds a
 * printed page, so the URL is the only identifier that will ever exist. Falls
 * back to a selectable field when the clipboard is unavailable — the
 * `_pickCopy` pattern, because a dead [copy] button is worse than no button. */
async function _copySourceUrl(btn) {
    const url = btn.getAttribute('data-url');
    if (!url) return;
    try {
        await navigator.clipboard.writeText(url);
        announceStatus('Source URL copied — paste it into Zotero’s “Add Item by Identifier”.');
        const prev = btn.textContent;
        btn.textContent = '[copied]';
        setTimeout(() => { btn.textContent = prev; }, 1500);
        return;
    } catch (e) {
        logError('copy source url failed: ' + String(e));
    }
    const field = document.createElement('input');
    field.type = 'text';
    field.value = url;
    field.readOnly = true;
    field.className = 'audit-url-fallback';
    btn.replaceWith(field);
    field.select();
    announceStatus('Clipboard unavailable — the URL is selected; copy it manually.');
}

/** "This entry needs no local PDF" — one row. Confirm-gated like every other
 *  decision write, and undoable from the report's own [show them again]. */
function _noPdfDismiss(btn) {
    const key = btn.getAttribute('data-key');
    if (!key) return;
    confirmInline(btn, {
        message: `Mark ${key} as needing no local PDF? It leaves this list (undoable).`,
        confirmLabel: 'No PDF needed',
        onConfirm: () => _noPdfDecision([key], { dismiss: true },
                                        `${key} — no PDF needed`),
    });
}

/** Bulk dismissal of a whole item type. The count goes INTO the confirm text:
 *  "dismiss 40 webpages" is a very different decision from "dismiss one". */
function _noPdfDismissType(btn) {
    const type = btn.getAttribute('data-type');
    if (!type) return;
    const keys = [...document.querySelectorAll('#audit-report-body tbody tr')]
        .filter((tr) => {
            const cells = tr.querySelectorAll('td');
            return cells.length > 4 && cells[4].textContent.trim() === type;
        })
        .map((tr) => tr.getAttribute('data-key'))
        .filter(Boolean);
    if (!keys.length) return;
    confirmInline(btn, {
        message: `Mark all ${keys.length} ${type} entr${keys.length === 1 ? 'y' : 'ies'} `
            + 'as needing no local PDF? They leave this list (undoable in one click).',
        confirmLabel: `Dismiss ${keys.length}`,
        onConfirm: () => _noPdfDecision(keys, { dismiss: true },
                                        `${keys.length} ${type} entries dismissed`),
    });
}

/** The undo. Restores EVERY dismissal, because the report only knows the
 *  count of what it hid, not the keys — offering a partial undo it cannot
 *  actually scope would be worse than an honest all-or-nothing one. */
function _noPdfRestoreAll(btn) {
    confirmInline(btn, {
        message: 'Show every entry marked "no PDF needed" again? This clears all '
            + 'those decisions.',
        confirmLabel: 'Show them again',
        // The server owns the "everything currently dismissed" set: the report
        // ships the COUNT of what it hid, not the keys, so the client cannot
        // enumerate them and an explicit clear_all verb is the honest shape.
        onConfirm: () => _noPdfDecision(null, { clear_all: true },
                                        'restored — every entry shown again'),
    });
}

async function _noPdfDecision(keys, verb, doneText) {
    const status = document.getElementById('audit-report-meta');
    try {
        const body = keys ? { citation_keys: keys, ...verb } : { ...verb };
        if (body.citation_keys && !body.citation_keys.length) return;
        const r = await secureFetch('/api/audit/pdf-decision', {
            method: 'POST',
            body: JSON.stringify(body),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { _showError(d.error || `Decision failed (${r.status})`); return; }
        announceStatus(doneText);
        // The decision changes which rows the report returns, and it applies on
        // the next LOAD (not the next scan) — so reload it.
        await _loadReport('zotero_no_pdf');
    } catch (e) {
        logError('no-pdf decision failed: ' + String(e));
        _showError(String(e));
    }
}

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
                    undo.dataset.role = ACTION_ROLES['dup-undismiss'];
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

/**
 * Focus after a table-row write whose own button is about to be disabled
 * (2026-08-19 UX pass, the registry's paired paper cut).
 *
 * Disabling the focused element drops focus to <body>, so assigning a key in
 * the ambiguous TABLE — the bulk surface, where a user works down the rows —
 * restarted every subsequent Tab from the top of the page. Move to the next
 * row's primary verb (the natural next decision), else to a still-live verb
 * in this row.
 */
function _focusAfterRowWrite(btn, verb) {
    const row = btn.closest && btn.closest('tr');
    if (!row) return;
    let next = row.nextElementSibling;
    while (next) {
        const cand = next.querySelector(`[data-action="${verb}"]:not([disabled])`);
        if (cand) { cand.focus(); return; }
        next = next.nextElementSibling;
    }
    const here = row.querySelector('[data-action="reveal"]');
    if (here && typeof here.focus === 'function') here.focus();
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
            body: JSON.stringify({ pdf, citation_key: key, confirm: true }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { _showError(d.error || `Mapping failed (${r.status})`); btn.disabled = false; return; }
        btn.textContent = `✓ ${key}`;
        announceStatus(`Assigned ${key}.`);
        _focusAfterRowWrite(btn, 'pick-key');
    } catch (err) {
        _showError(String(err));
        btn.disabled = false;
    }
}

async function _markState(btn, state) {
    const pdf = btn.getAttribute('data-pdf');
    if (!pdf) return;
    btn.disabled = true;
    try {
        const r = await secureFetch('/api/audit/mapping', {
            method: 'POST',
            body: JSON.stringify(_mappingStateBody(pdf, state, null, true)),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { _showError(d.error || `Mapping failed (${r.status})`); btn.disabled = false; return; }
        btn.textContent = state === 'pending' ? '✓ pending' : '✓ not in bib';
        announceStatus(_mappingStateDone(state, null));
        _focusAfterRowWrite(btn, state === 'pending' ? 'pending' : 'no-match');
    } catch (err) {
        _showError(String(err));
        btn.disabled = false;
    }
}

/* ---- Not in Zotero verbs (Phase 2, 2026-09-06) ------------------------- */

let _nizBusy = false;

/** Rows of the Not-in-Zotero table currently ticked, skipping any the table
 *  filter is hiding (the zotlink select-all rule). */
function _nizSelectedRels() {
    const body = document.getElementById('audit-report-body');
    const out = [];
    if (!body) return out;
    body.querySelectorAll('tr[data-rel]').forEach(tr => {
        if (tr.hidden || tr.style.display === 'none') return;
        const cb = tr.querySelector('input[data-niz-sel]');
        if (cb && cb.checked) out.push(tr.getAttribute('data-rel'));
    });
    return out;
}

function _nizSelectAll(on) {
    const body = document.getElementById('audit-report-body');
    if (!body) return;
    body.querySelectorAll('tr[data-rel]').forEach(tr => {
        if (tr.hidden || tr.style.display === 'none') return;
        const cb = tr.querySelector('input[data-niz-sel]');
        if (cb && !cb.disabled) cb.checked = on;
    });
    _nizUpdateCount();
}

function _nizUpdateCount() {
    const n = _nizSelectedRels().length;
    document.querySelectorAll('[data-action="niz-bulk"]').forEach(b => {
        b.disabled = !n || _nizBusy;
        const base = b.getAttribute('data-label') || b.textContent;
        b.setAttribute('data-label', base);
        b.textContent = `${base} (${n})`;
    });
}

/** Mark the rows in place after a successful write; the report itself is a
 *  view of ONE cached scan, so nothing is removed until the next scan. */
function _nizMarkRows(rels, state) {
    const body = document.getElementById('audit-report-body');
    if (!body) return;
    for (const rel of rels) {
        const tr = body.querySelector(`tr[data-rel="${CSS.escape(rel)}"]`);
        if (!tr) continue;
        tr.setAttribute('data-state', state);
        const cell = tr.querySelector('[data-niz-state]');
        if (cell) cell.textContent = state === 'pending' ? 'pending' : (state === 'no_match' ? 'not in bib' : 'cleared (orphan at next scan)');
        tr.querySelectorAll('[data-action="niz-set"]').forEach(b => {
            b.hidden = b.getAttribute('data-state') === state || state === 'cleared';
        });
        const clr = tr.querySelector('[data-action="niz-clear"]');
        if (clr) clr.hidden = state === 'cleared';
        if (state === 'cleared') {
            tr.querySelectorAll('[data-action="niz-set"]').forEach(b => { b.hidden = false; });
        }
        const cb = tr.querySelector('input[data-niz-sel]');
        if (cb) cb.checked = false;
    }
    _nizUpdateCount();
}

async function _nizWrite(btn, body, rels, stateAfter, doneMsg) {
    if (_nizBusy || !rels.length) return;
    _nizBusy = true;
    const prev = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Applying…';
    try {
        const r = await secureFetch('/api/audit/mapping', {
            method: 'POST', body: JSON.stringify(body),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) {
            const m = d.error || `Mapping failed (${r.status})`;
            _nizSay(m, true);
            _showError(m);
            return;
        }
        _nizMarkRows(rels, stateAfter);
        _nizSay(doneMsg);
    } catch (err) {
        _nizSay(String(err), true);
        _showError(String(err));
    } finally {
        _nizBusy = false;
        btn.disabled = false;
        btn.textContent = prev;
        _nizUpdateCount();
    }
}

function _nizSetState(btn, rels, state) {
    if (!rels.length || !rels[0]) return;
    const many = rels.length > 1;
    const what = state === 'pending' ? 'to add to Zotero later' : 'not a paper for Zotero';
    confirmInline(btn, {
        message: many
            ? `Mark ${rels.length} PDF(s) as ${what}? One atomic write to the audit’s mapping.json; undo: [clear] on this tab.`
            : `Mark this PDF as ${what}? Writes the audit’s mapping.json; undo: [clear] on this tab.`,
        confirmLabel: state === 'pending' ? 'Pending' : 'Not in bib',
        onConfirm: () => _nizWrite(btn,
            many ? { pdfs: rels, [state]: true, confirm: true }
                 : _mappingStateBody(rels[0], state, null, true),
            rels, state,
            `${rels.length} PDF(s) marked ${what}.`),
    });
}

function _nizClear(btn, rels) {
    if (!rels.length || !rels[0]) return;
    confirmInline(btn, {
        message: rels.length > 1
            ? `Clear the decision on ${rels.length} PDF(s)? They return to Unmapped PDFs at the next scan.`
            : 'Clear the decision on this PDF? It returns to Unmapped PDFs at the next scan.',
        confirmLabel: 'Clear',
        onConfirm: () => _nizWrite(btn,
            rels.length > 1 ? { pdfs: rels, clear: true, confirm: true }
                            : { pdf: rels[0], clear: true, confirm: true },
            rels, 'cleared', `${rels.length} decision(s) cleared.`),
    });
}

function _nizBulk(btn) {
    const rels = _nizSelectedRels();
    if (!rels.length) return;
    const verb = btn.getAttribute('data-verb');
    if (verb === 'clear') _nizClear(btn, rels);
    else if (verb === 'identify' || verb === 'reidentify') _nizIdentifyMany(btn, rels, verb === 'reidentify');
    else _nizSetState(btn, rels, verb);
}

/* ---- identify from Not in Zotero (2026-09-06) ------------------------- */

/** The visible status host for the Not-in-Zotero tab.
 *
 * Re-resolved on EVERY write, never captured at run start: a report
 * re-render between SSE frames would leave a captured node disconnected
 * (the _retireSay/_harmonySay rule). Report-identity guarded so a mid-batch
 * tab switch drops the line instead of spraying it into another report.
 *
 * Announcements go through the STATIC #sr-status-announcer via
 * _announceBatchProgress — this <p> is deliberately NOT a live region
 * (WebKit drops dynamically-minted ones, and a 366-row batch would announce
 * every frame; _announceBatchProgress throttles the SR channel).
 */
function _nizStatusHost() {
    if (_currentReport !== 'not_in_zotero') return null;
    return document.getElementById('audit-niz-status');
}

function _nizSay(msg, isErr, tone) {
    const host = _nizStatusHost();
    // No stamp: unlike `#audit-ns-status` / `#audit-zl-status` this line is
    // not persisted across a render, so it cannot outlive the moment it
    // describes — the defect a stamp exists to catch.
    if (host) statusLine(host, { tone: tone || (isErr ? 'error' : 'info'), text: msg || '' });
    _announceBatchProgress(msg, isErr);
}

/** Where a resolution actually came from — the fact that answers "did
 *  clicking this do anything?".
 *
 * Three genuinely different states the outcome alone cannot distinguish:
 * a CACHED answer (no egress, and what a non-force [identify] returns for
 * the ~90% of rows that already hold one), an answer resolved offline
 * against the bib (`consulted` empty — provably zero egress), and a live
 * lookup naming the sources it asked.
 */
function _nizProvenance(res) {
    if (!res) return '';
    if (res.from_cache) return 'cached result — nothing was asked online';
    const consulted = _accConsultedText(res);
    if (consulted) return consulted;
    return 'resolved offline against your bibliography — nothing left this machine';
}

/** One row's outcome, with its provenance. `_accOutcomeLine` already reads
 *  "no match found." for an empty result, which is a RESULT and not a
 *  silence — that is exactly what had to become visible. */
function _nizOutcomeLine(row) {
    const res = (row && row.resolution) || {};
    const prov = _nizProvenance(res);
    // The note TEXT, not just its count. `_accOutcomeLine` renders
    // "(2 source note(s))" and the deck keeps the text in a tooltip — but a
    // degraded answer is exactly the case a user cannot diagnose from the
    // outcome ("no match found" reads identically whether nothing matched or
    // every source timed out at the configured accession_timeout_s). Capped:
    // this is one line under a table, not a log.
    const notes = (res.notes || []).slice(0, 3).join('; ');
    return `${_accOutcomeLine(row)}${prov ? ` (${prov})` : ''}`
        + (notes ? ` — ${notes}` : '');
}

/** The Identification cell's text for a row payload (also used to repaint a
 *  row in place after an identify frame). */
function _nizIdentHtml(r) {
    const res = r.resolution;
    if (res) return `${_esc(res.outcome)}${res.tier ? ` · ${_esc(res.tier.replace(/_/g, ' '))}` : ''}`;
    return r.exists === false ? 'file missing' : 'never identified';
}

/** Repaint one Not-in-Zotero row from an identify/batch `{row}` frame. The
 *  report is a view of one scan, so only the resolution cells change. */
function _nizApplyRow(frameRow) {
    if (!frameRow || !frameRow.pdf || !frameRow.pdf.rel) return;
    const body = document.getElementById('audit-report-body');
    if (!body) return;
    const tr = body.querySelector(`tr[data-rel="${CSS.escape(frameRow.pdf.rel)}"]`);
    if (!tr) return;
    const r = { resolution: frameRow.resolution, exists: true };
    const doi = _nizDoi(r);
    const cand = r.resolution && r.resolution.candidates && r.resolution.candidates[0];
    tr.setAttribute('data-doi', doi || '');
    const ident = tr.querySelector('[data-niz-ident]');
    if (ident) { ident.innerHTML = _nizIdentHtml(r); applyActionRoles(ident); }
    const cc = tr.querySelector('[data-niz-cand]');
    if (cc) {
        cc.textContent = cand ? `${(cand.title || '').slice(0, 80)}${cand.year ? ` (${cand.year})` : ''}` : '';
        cc.setAttribute('title', cand ? (cand.title || '') : '');
    }
    const dc = tr.querySelector('[data-niz-doi]');
    if (dc) { dc.textContent = ''; if (doi) { const code = document.createElement('code'); code.textContent = doi; dc.appendChild(code); } }
    const ib = tr.querySelector('[data-action="niz-identify"]');
    // Label AND intent: a row that now holds a resolution must re-identify
    // with force, or the next click just re-reads the cache it just wrote.
    if (ib) { ib.textContent = '[re-identify]'; ib.setAttribute('data-force', '1'); }
}

// --- create a Zotero item from an orphan PDF (2026-09-11) -----------------
//
// The forward verb: `pending` means "add to Zotero later", so this is that
// queue's exit. Everything the form offers comes from ONE `/preview` call —
// the physical class picks the item type, the page-1 text the title — and
// the user edits before anything is created. The write is confirm-gated and
// undone from Tagging → Restore like every other external write.

/** Fields offered on the create form. Generic across item types on purpose:
 *  Zotero keeps the ones its type recognises and the server drops the empty
 *  ones, so a per-type field map would be a second copy of Zotero's schema
 *  for no gain. `key` is the Zotero field name sent verbatim. */
const _ZC_FIELDS = [
    { key: 'title', label: 'Title', required: true },
    { key: 'date', label: 'Date' },
    { key: 'publicationTitle', label: 'Publication / journal' },
    { key: 'publisher', label: 'Publisher' },
    { key: 'DOI', label: 'DOI' },
    { key: 'url', label: 'URL' },
];

/** "Surname, First; Surname" → Zotero creators. A single name with no comma
 *  is sent as a one-field `name` (an organisation), never split on a guess. */
function _zcCreators(raw) {
    return String(raw || '').split(';').map(s => s.trim()).filter(Boolean).map(part => {
        const i = part.indexOf(',');
        if (i < 0) return { creatorType: 'author', name: part };
        return {
            creatorType: 'author',
            lastName: part.slice(0, i).trim(),
            firstName: part.slice(i + 1).trim(),
        };
    });
}

function _zcRemovePanel(tr) {
    const next = tr && tr.nextElementSibling;
    if (next && next.hasAttribute('data-zc-panel')) next.remove();
}

/** Open (or close) the create form under one Not-in-Zotero row. */
async function _zcOpen(btn, rel) {
    const tr = btn.closest('tr');
    if (!tr) return;
    if (btn.getAttribute('aria-expanded') === 'true') {
        _zcRemovePanel(tr);
        btn.setAttribute('aria-expanded', 'false');
        return;
    }
    btn.disabled = true;
    const prev = btn.textContent;
    btn.textContent = '…';
    try {
        const r = await secureFetch('/api/zotcreate/preview', {
            method: 'POST', body: JSON.stringify({ pdf: rel }),
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
        _zcRemovePanel(tr);
        tr.after(_zcPanel(tr, rel, d));
        btn.setAttribute('aria-expanded', 'true');
        _nizSay(d.attempt && d.attempt.message
            ? d.attempt.message
            : `Review the item to create for ${d.filename}.`);
    } catch (e) {
        logError('zotcreate preview failed: ' + String(e));
        _nizSay(String(e), true);
    } finally {
        btn.disabled = false;
        btn.textContent = prev;
    }
}

function _zcPanel(tr, rel, d) {
    const row = document.createElement('tr');
    row.className = 'audit-notesync-preview';
    // Belongs to the row above it: the report's sort/filter moves and hides
    // it with its parent. Without this a sort strands an aim-a-write form
    // beside an unrelated PDF.
    row.setAttribute('data-row-attachment', '');
    row.setAttribute('data-zc-panel', '');
    const td = document.createElement('td');
    td.colSpan = tr.children.length;

    const blocked = d.attempt && d.attempt.stage && d.attempt.stage !== 'fresh'
        && d.attempt.stage !== 'parent_done';
    // A paper the bibliography already holds is not something to create: a
    // second Zotero item for it is a duplicate, and the remedy is the link
    // verb on the accession table.
    const refusal = blocked
        ? d.attempt.message
        : d.already_in_bib
            ? `This paper is already in your bibliography as ${d.already_in_bib}. `
              + 'Link the PDF to it from Unmapped PDFs instead of creating '
              + 'a second item.'
            : !d.can_write
                ? 'Zotero is not writable from this app yet — authorize Zotero 10, '
                  + 'or set ZOTERO_API_KEY for the web transport.'
                : null;
    if (refusal) {
        const p = document.createElement('p');
        p.className = 'muted';
        p.textContent = refusal;
        td.appendChild(p);
        row.appendChild(td);
        return row;
    }
    // Degrade to what the PDF itself yielded rather than to a blank form,
    // so an older/partial payload still opens something usable.
    const draft = d.draft || {
        item_type: d.suggested_item_type, creators: [], suggestions: [],
        prefilled_from: 'pdf',
        fields: { title: d.title || '', DOI: d.doi || '', url: d.source_url || '' },
    };

    const form = document.createElement('div');
    form.className = 'audit-zc-form';

    const typeWrap = document.createElement('label');
    typeWrap.textContent = 'Item type ';
    const sel = document.createElement('select');
    sel.setAttribute('data-zc', 'item_type');
    (d.item_types || []).forEach(t => {
        const o = document.createElement('option');
        o.value = t;
        o.textContent = t;
        if (t === draft.item_type) o.selected = true;
        sel.appendChild(o);
    });
    typeWrap.appendChild(sel);
    form.appendChild(typeWrap);

    const prefill = Object.assign({}, draft.fields || {});
    _ZC_FIELDS.forEach(f => {
        const wrap = document.createElement('label');
        wrap.textContent = f.label + ' ';
        const input = document.createElement('input');
        input.type = 'text';
        input.setAttribute('data-zc-field', f.key);
        input.value = prefill[f.key] || '';
        if (f.required) input.required = true;
        wrap.appendChild(input);
        form.appendChild(wrap);
    });

    const cw = document.createElement('label');
    cw.textContent = 'Authors ';
    const ci = document.createElement('input');
    ci.type = 'text';
    ci.setAttribute('data-zc', 'creators');
    ci.placeholder = 'Surname, First; Surname, First';
    ci.value = (draft.creators || []).map(
        c => (c.name ? c.name : `${c.lastName}, ${c.firstName || ''}`.replace(/, $/, ''))
    ).join('; ');
    cw.appendChild(ci);
    form.appendChild(cw);

    // WHERE the form's contents came from. A record fetched for this file and
    // a search on its filename read identically once they are in the boxes,
    // so the difference has to be said out loud.
    if (draft.prefilled_from === 'candidate') {
        const src = document.createElement('p');
        src.className = 'muted';
        src.textContent = `Filled in from a record matched by ${draft.basis}`
            + `${_zcAge(d)}. Check it before creating.`;
        form.appendChild(src);
    } else if ((draft.suggestions || []).length) {
        form.appendChild(_zcSuggestions(draft, form));
    }
    if (draft.from_filename) {
        const fn = document.createElement('p');
        fn.className = 'muted';
        // Worth saying: a surname read off a filename is the user's own
        // earlier decision, not a record anyone looked up — and a
        // `first_second_year` name only ever yields its first author.
        fn.textContent = 'Author and year come from this file\u2019s name, so '
            + 'only the first author is filled in.';
        form.appendChild(fn);
    }

    const why = document.createElement('p');
    why.className = 'muted';
    // The class is WHY the type was suggested, and the page count is what
    // separates a book from an article when the producer says nothing.
    why.textContent = `${d.klass.replace(/_/g, ' ')}`
        + (d.pages ? `, ${d.pages} page${d.pages === 1 ? '' : 's'}` : '')
        + (d.source_url ? ' — printed from a web page, so its URL is the only '
                          + 'identifier it will ever have' : '')
        + (d.isbn ? ` — ISBN ${d.isbn} printed inside` : '');
    form.appendChild(why);

    if (d.page1_excerpt) {
        // The extractor gets the title wrong often enough that the remedy
        // has to be in the form: on a sample of 40 pending PDFs it produced a
        // title for 29, and roughly 40% of those were a running header or an
        // acknowledgement line. Collapsed, so it does not become a reader.
        const det = document.createElement('details');
        det.className = 'audit-zc-excerpt';
        const sum = document.createElement('summary');
        sum.textContent = 'First page, as text';
        det.appendChild(sum);
        const pre = document.createElement('pre');
        pre.textContent = d.page1_excerpt;   // never innerHTML — vault content
        det.appendChild(pre);
        form.appendChild(det);
    }

    const go = document.createElement('button');
    go.type = 'button';
    go.className = 'audit-link-btn';
    go.setAttribute('data-zc', 'create');
    go.textContent = '[create it]';
    go.addEventListener('click', () => _zcConfirm(go, rel, d, form));
    form.appendChild(go);

    td.appendChild(form);
    row.appendChild(td);
    return row;
}

/** "cached 3 days ago", or nothing when we have no stamp. */
function _zcAge(d) {
    const t = d.resolution && d.resolution.queried_at;
    if (!t) return '';
    const days = Math.floor((Date.now() / 1000 - t) / 86400);
    return days <= 0 ? ' (looked up today)' : ` (looked up ${days} d ago)`;
}

/** Candidates a tier was NOT confident enough to fill the form with.
 *
 *  Offered, never applied: on the live library the commonest resolution is a
 *  Crossref search on the filename's surname and year, which routinely names
 *  a different paper by a different author who shares a surname. Picking one
 *  has to be a decision, so each is a button that says what it would write. */
function _zcSuggestions(draft, form) {
    const wrap = document.createElement('div');
    wrap.className = 'audit-zc-suggestions';
    const h = document.createElement('p');
    h.className = 'muted';
    h.textContent = `Not filled in: the match rests on ${draft.basis}. `
        + `${draft.suggestions.length === 1 ? 'One record was' : `${draft.suggestions.length} records were`} `
        + 'found — check one against the PDF before using it.';
    wrap.appendChild(h);
    draft.suggestions.forEach((c, i) => {
        const line = document.createElement('div');
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'audit-link-btn';
        b.textContent = `[use ${i + 1}]`;
        b.addEventListener('click', () => _zcApplySuggestion(form, c));
        line.appendChild(b);
        const txt = document.createElement('span');
        txt.textContent = ' ' + [c.title, (c.authors || [])[0], c.year,
                                 c.container, c.doi].filter(Boolean).join(' · ');
        line.appendChild(txt);
        wrap.appendChild(line);
    });
    return wrap;
}

/** Lay one picked record over the form. Same mapping the prefill uses — the
 *  difference between the two is who chose it, not how it is written. */
function _zcApplySuggestion(form, c) {
    const set = (k, v) => {
        const el = form.querySelector(`[data-zc-field="${k}"]`);
        if (el) el.value = v == null ? '' : String(v);
    };
    set('title', c.title);
    set('date', c.year);
    set('publicationTitle', c.container);
    set('DOI', c.doi);
    set('url', c.url);
    form.querySelector('[data-zc="creators"]').value = (c.authors || []).join('; ');
    if (c.container) form.querySelector('[data-zc="item_type"]').value = 'journalArticle';
    _nizSay(`Filled the form from “${(c.title || '').slice(0, 60)}”. Nothing created yet.`);
}

function _zcConfirm(go, rel, d, form) {
    const body = _zcBody(rel, form);
    if (!body) { _nizSay('A title is required.', true); return; }
    confirmInline(go, {
        message: `Create a ${body.item_type} in Zotero titled “${body.fields.title}” `
                 + `and link ${d.filename} to it? Automatic Desktop undo is unavailable; corrections require review in Zotero.`,
        confirmLabel: 'Create',
        // A gated write, not a destructive one — the undo is a trash. Red is
        // reserved for actions that actually destroy something (ui.js).
        tone: 'confirm',
        onConfirm: () => {
            // The form stays editable while the strip is open, so re-read it:
            // creating something other than what the confirm named is the one
            // outcome this ceremony exists to prevent.
            const live = _zcBody(rel, form);
            if (!live) { _nizSay('A title is required.', true); return; }
            if (live.item_type !== body.item_type
                    || live.fields.title !== body.fields.title) {
                _nizSay('The form changed after the confirmation — nothing was '
                        + 'created. Check it and confirm again.', true);
                return;
            }
            _zcCreate(go, rel, {...live, physical:d.physical, review_token:d.review_token, target:d.target});
        },
    });
}

function _zcBody(rel, form) {
    const fields = {};
    form.querySelectorAll('[data-zc-field]').forEach(i => {
        const v = i.value.trim();
        if (v) fields[i.getAttribute('data-zc-field')] = v;
    });
    if (!fields.title) return null;
    return {
        pdf: rel,
        item_type: form.querySelector('[data-zc="item_type"]').value,
        fields,
        creators: _zcCreators(form.querySelector('[data-zc="creators"]').value),
        confirm: true,
    };
}

async function _zcCreate(go, rel, body) {
    go.disabled = true;
    _nizSay('Creating the Zotero item…');
    try {
        const r = await secureFetch('/api/zotcreate/create', {
            method: 'POST', body: JSON.stringify(body),
        });
        const d = await r.json();
        if (!r.ok || !d.ok) throw new Error(d.message || d.error || `HTTP ${r.status}`);
        const tr = go.closest('tr') && go.closest('tr').previousElementSibling;
        if (tr) _zcRemovePanel(tr);
        // `undo: "manual"` means the item was created but its creation could
        // not be verified, so the journalled revert will refuse. Saying
        // "undo from Tagging → Restore" there would be a promise the journal
        // has already decided not to keep.
        const undo = d.journal_warning
            ? ` (no undo record: ${d.journal_warning})`
            : d.undo === 'manual'
                ? ' Undo is manual for this one: its creation could not be '
                  + 'verified, so Restore will refuse — trash it in Zotero if '
                  + 'it was a mistake.'
                : '';
        _nizSay(`Created ${d.item_type} ${d.item_key} and linked the PDF. ${d.note}${undo}`);
    } catch (e) {
        logError('zotcreate create failed: ' + String(e));
        _nizSay(String(e), true);
    } finally {
        go.disabled = false;
    }
}

async function _nizIdentifyOne(btn, rel) {
    if (_nizBusy || !rel) return;
    _nizBusy = true;
    btn.disabled = true;
    const prev = btn.textContent;
    // Intent carried in the DOM, never inferred from the button's own label
    // (2026-09-07). `force: prev === '[re-identify]'` happened to work, but
    // any edit to that rendered string would have silently turned every
    // re-identify into a cache-consulting identify — which returns the
    // cached answer unchanged, i.e. the very "nothing happened" symptom.
    const force = btn.getAttribute('data-force') === '1';
    let applied = false;
    btn.textContent = '…';
    _nizSay(`Identifying ${rel}…`);
    try {
        let row = null;
        await consumeSSE('/api/accession/identify', {
            method: 'POST', body: JSON.stringify({ pdf: rel, force }),
        }, {
            onInfo: (m) => _nizSay(m),
            onOther: (f) => { if (f && f.row) row = f.row; },
            // Throwing here reaches the catch below with the server's own
            // message: consumeSSE sets onErrorCalled BEFORE invoking the
            // handler and re-throws an already-reported exception untouched.
            onError: (err) => { throw new Error(err); },
        });
        if (!row) throw new Error('Identification ended without a result.');
        _nizApplyRow(row);   // relabels to [re-identify] and sets data-force
        applied = true;
        _nizSay(_nizOutcomeLine(row));
    } catch (e) {
        _nizSay(String(e), true);
        _showError(String(e));
    } finally {
        _nizBusy = false;
        btn.disabled = false;
        // Restore the old label ONLY on failure. On success _nizApplyRow has
        // just relabelled this very button to [re-identify]; restoring `prev`
        // unconditionally undid that (2026-09-07), so a row identified for
        // the first time stayed [identify] and the NEXT click re-read the
        // cache it had itself just written — the reported "nothing happens",
        // by a second route.
        if (!applied) btn.textContent = prev;
        _nizUpdateCount();
    }
}

function _nizIdentifyMany(btn, rels, force) {
    if (!rels.length) return;
    confirmInline(btn, {
        message: force
            ? `Re-identify ${rels.length} pending PDF(s) online, ignoring cached results (the configured accession sources; DOIs and titles leave this machine)? Nothing is written to the mapping.`
            : `Look up ${rels.length} pending PDF(s) online (the configured accession sources; DOIs and titles leave this machine)? Cached results are reused; nothing is written to the mapping.`,
        confirmLabel: force ? `Re-identify ${rels.length}` : `Identify ${rels.length}`,
        onConfirm: () => _nizRunBatch(btn, rels, force),
    });
}

async function _nizRunBatch(btn, rels, force) {
    if (_nizBusy) return;
    _nizBusy = true;
    _nizUpdateCount();
    let done = 0;
    let terminalError = null;
    // Outcome tally, so the summary says what the run FOUND and not merely
    // that it ran: a batch over already-cached rows legitimately finishes
    // with "0 matched", which is a result and must not read as a silence.
    const tally = new Map();
    let asked = 0;
    _nizSay(`[0/${rels.length}] Identifying…`);
    try {
        await consumeSSE('/api/accession/batch', {
            method: 'POST', body: JSON.stringify({ pdfs: rels, confirm: true, ...(force ? { force: true } : {}) }),
        }, {
            onInfo: (m) => _nizSay(m),
            onOther: (f) => {
                if (!(f && f.row)) return;
                done += 1;
                _nizApplyRow(f.row);
                const res = f.row.resolution || {};
                const outcome = res.outcome || 'none';
                tally.set(outcome, (tally.get(outcome) || 0) + 1);
                if (!res.from_cache && (res.consulted || []).length) asked += 1;
                // _announceBatchProgress throttles the SR channel on this
                // exact "[n/total]" shape; the visible line updates per row.
                _nizSay(`[${done}/${rels.length}] ${f.row.pdf ? f.row.pdf.name : ''}`);
            },
            onError: (err, frame) => {
                if (frame && frame.accession) { terminalError = err; return; }
                throw new Error(err);
            },
        });
        const breakdown = [...tally.entries()]
            .sort((a, b) => b[1] - a[1])
            .map(([k, n]) => `${n} ${k.replace(/_/g, ' ')}`)
            .join(', ');
        const msg = `Identified ${done} of ${rels.length} pending PDF(s)`
            + (breakdown ? ` — ${breakdown}` : '')
            + `. ${asked} looked up online, ${done - asked} answered from cache or your bibliography.`
            + (terminalError ? ` Stopped early: ${terminalError}` : '');
        if (terminalError) { _nizSay(msg, true); _showError(msg); } else _nizSay(msg);
    } catch (e) {
        _nizSay(String(e), true);
        _showError(String(e));
    } finally {
        _nizBusy = false;
        _nizUpdateCount();
    }
}

/* ---- detach (Entries With Several PDFs) --------------------------------- */

/** Unmap one PDF from one key, or from every key (2026-09-07).
 *
 * A `data-key` names the one key; `data-detach-all` is the blanket form,
 * which posts `detach_all` rather than a wildcard in `detach_from` — the
 * server refuses the wildcard on that field precisely so a body cannot
 * smuggle a blanket refusal in where the confirm text promised one key.
 *
 * THE BLANKET FORM IS OPT-IN, not the absence of a key (2026-09-07 review).
 * It used to be `key ? one : all`, so a new call site that forgot `data-key`
 * — the easiest possible slip, since every other row verb takes one — would
 * silently offer "never auto-map" where it meant "unmap from this key", and
 * the confirm text would have read correctly for a decision the user never
 * asked for. A button carrying neither attribute now does nothing at all,
 * which is the failure a developer notices immediately.
 *
 * Why the blanket form exists: per-key exclusion is whack-a-mole, because
 * the ladder simply finds the next candidate. Measured on the live library,
 * 2 of 127 exclusions had already needed a second round with no upper bound
 * in sight. Undo for both is [clear], which is also what a later hand-link
 * implies (the server lifts the exclusion the link contradicts). */
function _detachConfirm(btn) {
    const pdf = btn.getAttribute('data-pdf');
    const key = btn.getAttribute('data-key');
    const blanket = btn.hasAttribute('data-detach-all');
    if (!pdf) return;
    if (!key && !blanket) {
        _showError('This unmap button names no citation key. It has not been '
            + 'run — report it rather than retrying.');
        return;
    }
    const name = pdf.split('/').pop();
    const zoteroWins = btn.getAttribute('data-source') === 'zotero_link'
        ? ' Zotero currently links this file, so changing mapping.json does not change that pairing '
          + '(Zotero wins, reported). Trash the linked attachment if you want the ladder to reclaim it.'
        : '';
    confirmInline(btn, {
        message: key
            ? `Unmap ${name} from ${key}? No automatic match will re-claim it for that key; `
              + 'it returns to Unmapped PDFs at the next scan. Undo: [clear] on the Not in Zotero tab. '
              + 'Zotero trash undo (if checked below): Tagging → Restore.'
              + zoteroWins
            : `Never auto-map ${name} to any entry? No automatic match will claim it for ANY key, `
              + 'and any hand-curated match is removed. It stays listed as an unmapped PDF, so you can '
              + 'still link it by hand — which lifts this. Undo: [clear] on the Not in Zotero tab. '
              + 'Zotero trash undo (if checked below): Tagging → Restore.'
              + zoteroWins,
        confirmLabel: key ? 'Unmap' : 'Never auto-map',
        checkbox: {
            label: 'Also trash Zotero linked attachment(s) of this file on that entry',
            defaultChecked: true,
        },
        onConfirm: (opts) => {
            const trash = !opts || opts.checkboxChecked !== false;
            _detach(btn, pdf, key, trash);
        },
    });
}

async function _detach(btn, pdf, key, trashZoteroLinks) {
    btn.disabled = true;
    const name = pdf.split('/').pop();
    try {
        const body = key
            ? { pdf, detach_from: key, confirm: true,
                trash_zotero_links: !!trashZoteroLinks }
            : { pdf, detach_all: true, confirm: true,
                trash_zotero_links: !!trashZoteroLinks };
        const r = await secureFetch('/api/audit/mapping', {
            method: 'POST',
            body: JSON.stringify(body),
        });
        const d = await r.json().catch(() => ({}));
        // Pre-detach refuses are true failures (409). Post-detach trash
        // problems are 200 + zotero_error — never treat those as "unmap failed".
        if (!r.ok) {
            _showError(d.error || `Unmap failed (${r.status})`);
            btn.disabled = false;
            return;
        }
        const nTrash = Array.isArray(d.trashed) ? d.trashed.length : 0;
        btn.textContent = key ? '✓ unmapped' : '✓ never auto-mapped';
        let msg = key
            ? `Unmapped ${name} from ${key}; it is an orphan at the next scan.`
            : `${name} will never be auto-mapped; it is an orphan at the next scan.`;
        if (nTrash) {
            msg += ` Moved ${nTrash} Zotero linked attachment(s) to Zotero trash`
                + (d.batch_id ? ` (restore batch ${d.batch_id}).` : '.');
        }
        if (d.warning) {
            msg += ` ${d.warning}`;
        }
        if (d.zotero_error) {
            msg += ` Zotero trash incomplete: ${d.zotero_error}`;
            _showError(msg);
        } else {
            announceStatus(msg);
        }
    } catch (err) {
        _showError(String(err));
        btn.disabled = false;
    }
}

function _deadCurationClear(btn) {
    const pdf = btn.getAttribute('data-pdf');
    if (!pdf) return;
    confirmInline(btn, {
        message: `Clear the stale mapping.json decision for ${pdf}? Zotero already links this file. Writes mapping.json only; the Zotero link stays. Re-curate from Inventory if you need a different pairing.`,
        confirmLabel: 'Clear decision',
        onConfirm: async () => {
            try {
                const r = await secureFetch('/api/audit/mapping', {
                    method: 'POST', body: JSON.stringify({ pdf, clear: true }),
                });
                const d = await r.json().catch(() => ({}));
                if (!r.ok) { _showError(d.error || `Clear failed (${r.status})`); return; }
                announceStatus(`Cleared the stale decision on ${pdf}.`);
                await _loadReport('dead_curation');
            } catch (e) {
                logError('dead curation clear failed: ' + String(e));
                _showError(String(e));
            }
        },
    });
}

/** Write a record's mapped PDFs into its note as a `pdf:` frontmatter list.
 *
 * KEYS ONLY on the wire: the filenames come from the cached scan's PDF↔bib
 * join, so a client cannot point a note at a file the record does not own.
 *
 * The confirm names the rename interaction because it is the non-obvious
 * cost: this makes those PDFs "referenced from a note", and the renamer
 * refuses a referenced PDF for every shape but this one. */
function _invPdfLink(btn) {
    const key = btn.getAttribute('data-inv-pdflink');
    if (!key) return;
    confirmInline(btn, {
        message: `Write ${key}'s PDF(s) into its note as a pdf: frontmatter list? `
            + 'Only that field changes. Reversible from Tagging → Restore.',
        confirmLabel: 'Write the link',
        onConfirm: async () => {
            try {
                const r = await secureFetch('/api/tagging/apply-pdf-links', {
                    method: 'POST',
                    body: JSON.stringify({ citation_keys: [key], confirm: true }),
                });
                const d = await r.json().catch(() => ({}));
                if (!r.ok) { _showError(d.error || `Write failed (${r.status})`); return; }
                const res = (d.results || {})[key] || {};
                if (!res.ok) {
                    _showError(res.error || `Not written: ${res.reason || 'unknown'}`);
                    return;
                }
                announceStatus(res.changed
                    ? `${key}: pdf link written (${(res.links || []).join(', ')}).`
                    : `${key}: already linked.`);
                btn.textContent = res.changed ? '\u2713 linked' : '\u2713 already linked';
                btn.disabled = true;
            } catch (e) {
                logError('pdf link write failed: ' + String(e));
                _showError(String(e));
            }
        },
    });
}

/* ---- re-point a broken Zotero link (2026-09-07) -------------------------- */

/** A picker over biblio PDFs for one broken linked attachment.
 *
 * A linked attachment is only a path, so anything that renames or moves the
 * file orphans it silently — Zotero's own rename preference did that to five
 * vault files and the Attanger plugin to four more, and all nine were
 * repaired by hand. The server ranks candidates against the filename Zotero
 * remembers, which after a rename is usually the best clue to the new one.
 *
 * Opened as a row attachment (`data-row-attachment`) so `installTableControls`
 * moves and hides it WITH its row: a sort would otherwise strand it beside an
 * unrelated attachment, on a surface whose whole job is aiming a write. */
async function _zlRepointPicker(btn) {
    const key = btn.getAttribute('data-key');
    const row = btn.closest('tr');
    if (!key || !row) return;
    const existing = row.nextElementSibling;
    if (existing && existing.classList.contains('audit-zl-repoint')) {
        existing.remove();
        btn.setAttribute('aria-expanded', 'false');
        _zlSay('Re-point picker closed.');
        return;
    }
    if (btn.dataset.busy === '1') return;
    btn.dataset.busy = '1';
    btn.disabled = true;
    try {
        const d = await _zlCandidates(key, '');
        if (!d) return;
        const tr = document.createElement('tr');
        tr.className = 'audit-zl-repoint';
        tr.setAttribute('data-row-attachment', '');
        const td = document.createElement('td');
        td.colSpan = row.children.length;
        const note = document.createElement('p');
        note.className = 'audit-summary-note';
        note.textContent = `Zotero points at ${d.stored_path || '(no path)'}, which is not there. `
            + `Pick the file it should point at (${d.total} biblio PDF(s) searched).`;
        td.appendChild(note);
        const input = document.createElement('input');
        input.type = 'search';
        input.className = 'audit-zl-repoint-q';
        input.setAttribute('aria-label', 'Filter biblio PDFs');
        input.placeholder = 'Filter by file name…';
        td.appendChild(input);
        const list = document.createElement('div');
        list.className = 'audit-zl-repoint-list';
        td.appendChild(list);
        const paint = (cands) => {
            list.innerHTML = '';
            if (!cands.length) {
                const p = document.createElement('p');
                p.className = 'muted';
                p.textContent = 'No biblio PDF matches that filter.';
                list.appendChild(p);
                return;
            }
            for (const c of cands) {
                const b = document.createElement('button');
                b.type = 'button';
                b.className = 'audit-link-btn';
                b.textContent = c.rel;
                b.addEventListener('click', () => _zlRepoint(b, key, c.rel));
                const line = document.createElement('div');
                line.appendChild(b);
                list.appendChild(line);
            }
        };
        paint(d.candidates || []);
        let timer = null;
        input.addEventListener('input', () => {
            clearTimeout(timer);
            timer = setTimeout(async () => {
                const next = await _zlCandidates(key, input.value);
                if (next) paint(next.candidates || []);
            }, 220);
        });
        tr.appendChild(td);
        row.after(tr);
        btn.setAttribute('aria-expanded', 'true');
        _zlSay(`Re-point picker open: ${(d.candidates || []).length} suggestion(s).`);
    } catch (e) {
        logError('zotlink repoint picker failed: ' + String(e));
        _zlSay(String(e), true);
    } finally {
        delete btn.dataset.busy;
        btn.disabled = false;
    }
}

async function _zlCandidates(key, q) {
    const url = `/api/zotlink/repoint-candidates?item_key=${encodeURIComponent(key)}`
        + (q ? `&q=${encodeURIComponent(q)}` : '');
    const r = await secureFetch(url);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { _zlSay(d.error || `Could not list candidates (${r.status})`, true); return null; }
    return d;
}

function _zlRepoint(btn, key, rel) {
    confirmInline(btn, {
        message: `Point this Zotero attachment at ${rel}? Only the stored path changes — `
            + 'the file is not moved and Zotero\u2019s display name is left alone. '
            + 'Reversible from Tagging → Restore.',
        confirmLabel: 'Re-point',
        onConfirm: async () => {
            try {
                const r = await secureFetch('/api/zotlink/repoint', {
                    method: 'POST',
                    body: JSON.stringify({ item_key: key, rel, confirm: true }),
                });
                const d = await r.json().catch(() => ({}));
                if (!r.ok || d.ok === false) {
                    _zlSay(d.error || d.message || `Re-point failed (${r.status})`, true);
                    return;
                }
                _zlSay(`Zotero now points at ${rel}. ${d.note || ''}`);
                await _loadReport('zotero_attachments');
            } catch (e) {
                logError('zotlink repoint failed: ' + String(e));
                _zlSay(String(e), true);
            }
        },
    });
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
    // An ARRAY is joined here rather than coerced by the template literal.
    // `Array.prototype.toString` joins on "," and the browser then
    // FOSTER-PARENTS those stray text nodes out of the table, dropping them
    // immediately before it — so a caller that forgot `.join('')` printed a
    // run of commas above its own table, one per row gap. Two of the twelve
    // call sites had (the notesync update/conflict tables), and the defect is
    // invisible at 0 or 1 row, which is why it survived. Call sites still join
    // explicitly; this is the backstop that stops the class returning.
    if (Array.isArray(rowsHtml)) rowsHtml = rowsHtml.join('');
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

/** Wire meta-line jumps. Shared by `_wireRowActions` and the pdf_review early mount. */
function _wireMetaGotoButtons(meta) {
    if (!meta) return;
    meta.querySelectorAll('[data-goto-report]').forEach(btn => {
        btn.addEventListener('click', () => selectAuditReport(btn.getAttribute('data-goto-report')));
    });
}


function _pdfProjectionHtml(projection) {
    if (!projection) return '';
    const freshness=projection.freshness || {};
    const counts=Object.entries(projection.counts || {}).map(([state,n])=>`${_esc(projection.labels?.[state] || state)}: ${Number(n)}`).join(' · ');
    return `<p class="audit-reconciliation-status">PDF reconciliation (${freshness.fresh ? 'recent observation' : 'observation needs refresh'}): ${counts || _esc(freshness.error || 'unavailable')}. Other report totals describe the saved scan. ${_gotoReportBtn('pdf_review','[Open PDF workspace]')}</p>`;
}


/** Phase 6 reduced (2026-09-16): the Zotero storage CONTROL. Read-only; every
 *  row names the manual step to take in Zotero or Attanger. Never a conversion. */
const _STORAGE_FINDING_LABELS = {
    stored: 'Stored in Zotero (bytes in Zotero storage / cloud)',
    outside_biblio: 'Linked outside the bibliography folder',
    broken_path: 'Linked path does not resolve',
    relative_link: 'Relative to the Linked Attachment Base Directory',
    duplicate_path: 'Same file linked more than once',
    remote: 'Linked URL (not a file)',
    linked_ok: 'Linked under the bibliography folder',
    unknown_mode: 'Unknown link mode',
};
function _mountStorageControl(host, data, text, jump) {
    text('p', `${data.goal || ''} Destination: ${data.destination || 'unknown'}. Nothing here converts, moves or writes: correct in Zotero or Attanger, then rescan.`);
    const population = data.population || {};
    const observation = data.observation || {};
    text('p', `${population.attachments || 0} attachment records in the saved Zotero observation${observation.checked_at ? ' of ' + new Date(observation.checked_at * 1000).toLocaleString() : ''}; ${(data.findings || []).length} to correct by hand. ${observation.available === false ? 'No observation is saved yet — refresh the PDF workspace first; an absent observation is not a clean library.' : ''}`);
    const counts = Object.entries(data.counts || {}).map(([finding, n]) => `${_STORAGE_FINDING_LABELS[finding] || finding}: ${Number(n)}`).join(' · ');
    if (counts) text('p', counts);
    const excluded = data.excluded || {};
    const excludedParts = [];
    if (excluded.embedded_image) excludedParts.push(`${excluded.embedded_image} image(s) embedded in Zotero notes`);
    if (excluded.note_child) excludedParts.push(`${excluded.note_child} other attachment(s) whose parent is a note`);
    if (excluded.web_snapshot) excludedParts.push(`${excluded.web_snapshot} web snapshot(s)`);
    const leftover = Object.entries(excluded)
        .filter(([k, n]) => n && !['embedded_image', 'note_child', 'web_snapshot'].includes(k));
    for (const [k, n] of leftover) excludedParts.push(`${n} ${k}`);
    if (excludedParts.length) text('p', `Excluded, not in this control: ${excludedParts.join('; ')}.`);
    const rows = data.findings || [];
    if (!rows.length) { text('p', 'Every attachment is a linked file under the bibliography folder.'); jump('Checks & campaigns', 'library_checks'); return; }
    const wrap = document.createElement('div'); wrap.className = 'audit-table-scroll'; host.appendChild(wrap);
    const table = document.createElement('table'); table.className = 'audit-table'; wrap.appendChild(table);
    const thead = table.createTHead(); const hr = thead.insertRow();
    for (const h of ['Finding', 'Citation key', 'Parent', 'Attachment', 'Path', 'What to do']) { const th = document.createElement('th'); th.textContent = h; hr.appendChild(th); }
    const tbody = table.createTBody();
    for (const r of rows) {
        const tr = tbody.insertRow();
        const cells = [_STORAGE_FINDING_LABELS[r.finding] || r.finding, r.citation_key || '—', r.parent_title || r.parent_key || '—', r.attachment_key, r.path || (r.link_mode || '—')];
        for (const value of cells) { const td = tr.insertCell(); td.textContent = value; }
        const guidance = tr.insertCell();
        const list = document.createElement('ol'); guidance.appendChild(list);
        for (const step of r.guidance || []) { const li = document.createElement('li'); li.textContent = step; list.appendChild(li); }
        if (r.duplicate_of?.length) { const p = document.createElement('p'); p.textContent = 'Also linked as ' + r.duplicate_of.join(', ') + '.'; guidance.appendChild(p); }
    }
    installTableControls(table, {label: 'Storage findings', noun: 'findings'});
    jump('Zotero attachments (advanced, re-point verb)', 'zotero_attachments');
    jump('Checks & campaigns', 'library_checks');
}


/** Contractual Library vocabulary (plan §5; mirrors accession/vocabulary.py
 *  STATE_LABELS, pinned by tests/test_library_vocabulary.py). Internal ids stay
 *  as they are persisted; only what the user reads changes. */
const _LIBRARY_STATE_LABELS = {
    'Needs identification': 'Identity to review',
    'Needs review': 'Relationship to review',
    'Ready to register': 'PDF identity confirmed — check Zotero reference',
    'Awaiting bibliography': 'Awaiting bibliography export',
    'Linked': 'Associated in CHERQAiN; attachment observed',
    'Discrepancies': 'Relationship differs — review required',
    'Accepted differences': 'Difference accepted by you',
    'Excluded': 'Excluded from registration or automatic association',
    'Recovery': 'Observation or operation requires inspection',
    'Retired': 'File removed with recovery',
    curated: 'Saved decision', manual: 'Saved decision (yours)',
    unmapped: 'No CHERQAiN association', mapped: 'Associated in CHERQAiN',
    linked_file: 'Linked-file attachment in Zotero', linked: 'Linked-file attachment in Zotero',
    pending: 'Deferred by you', no_match: 'Excluded from registration',
    confirmed: 'PDF identity confirmed', registered: 'Zotero reference found', observed: 'Zotero reference found',
    none_observed: 'No attachment observed', skipped: 'Not applied', reconciled: 'Relationship checked',
    in_sync: 'In sync', conflict: 'Conflict', push: 'Changed by you', pull: 'Changed in Zotero',
    unbaselined: 'No sync point', relationship_checked: 'Relationship checked', review: 'Review required',
    contradiction: 'Contradiction found', dispatched_or_unknown: 'Outcome unknown — inspect',
    recovery_dispatched_or_unknown: 'Recovery outcome unknown — inspect', settled_after_interruption: 'Completed (settled after interruption)',
    undone: 'Undone on both notes', undo_incomplete: 'Undo incomplete — inspect', completed: 'Completed',
    concordant_observed: 'Both notes match the approved result', incomplete: 'One destination matches — incomplete',
    concurrent_change: 'Edited since the preview — preserve and review', not_observed: 'Neither destination matches — inspect',
    base_changed: 'Sync base changed — settlement verdict below', unavailable: 'Observation unavailable',
    content_blocked: 'Content requires review (unsupported or out of region)', identity_review_required: 'Identity or converter changed — review',
    not_dispatched: 'Not applied yet', stale: 'Proposal out of date — preview again',
    proposed: 'Proposals ready for review', generation_pending: 'Generation in progress or interrupted', interrupted: 'Generation interrupted',
    decisions_recorded: 'Decisions recorded (no metadata written)', applied: 'Applied', partial: 'Applied in part — inspect',
    needs_inspection: 'Outcome needs inspection', complete: 'Complete', restored: 'Restored', prepared: 'Prepared', moved: 'Moved to recoverable storage',
};
function _libraryLabel(value) {
    if (value === null || value === undefined || value === '') return '—';
    return _LIBRARY_STATE_LABELS[value] || String(value);
}
function _libraryKeyLabel(key) {
    return String(key).replace(/_/g, ' ').replace(/^./, c => c.toUpperCase());
}
function _libraryDate(value) {
    if (typeof value === 'number' && value > 1e9) return new Date(value * 1000).toLocaleString();
    if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T/.test(value)) { const d = new Date(value); return Number.isNaN(d.getTime()) ? value : d.toLocaleString(); }
    return null;
}
/** Structured rendering of a JSON-able value: definition lists, bullet lists
 *  and nested disclosures, textContent only, bounded in depth and width. This
 *  is what every Library view uses where a raw JSON dump used to be — the
 *  2026-09-16 decision: no view renders JSON.stringify. */
const _FACTS_MAX_DEPTH = 4, _FACTS_MAX_ITEMS = 40, _FACTS_HIDDEN = new Set(['ctx', 'raw', 'html', 'body_html', 'base_html']);
/** Keys whose values are state ids shown through the contractual vocabulary,
 *  and keys whose numbers are epoch seconds (2026-09-17 review, D2): any other
 *  scalar prints verbatim — a title "Recovery" is not a state and a 2 GB byte
 *  count is not a date. */
const _FACTS_STATE_KEYS = new Set(['state', 'parent_state', 'attachment_state', 'bibliography_state', 'identity_state', 'outcome', 'verdict', 'status', 'kind', 'finding', 'category']);
function _factsIsStateKey(key) {
    return key !== null && (_FACTS_STATE_KEYS.has(key) || /_state$|_status$/.test(key));
}
function _factsIsDateKey(key) {
    return key !== null && /(^|_)(at|ts|date)$/.test(key);
}
function _libraryFacts(parent, value, depth = 0, key = null) {
    const mk = (tag, text, host = parent) => { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; host.appendChild(el); return el; };
    if (value === null || value === undefined) return mk('span', '—');
    if (typeof value !== 'object') {
        const date = _factsIsDateKey(key) && (typeof value === 'number' || typeof value === 'string') ? _libraryDate(value) : null;
        if (date) return mk('span', date);
        if (typeof value === 'boolean') return mk('span', value ? 'yes' : 'no');
        return mk('span', _factsIsStateKey(key) ? _libraryLabel(value) : (value === '' ? '—' : String(value)));
    }
    if (Array.isArray(value) && value.length && value.every(v => v && typeof v === 'object' && !Array.isArray(v) && ('lastName' in v || 'firstName' in v || 'name' in v))) {
        // Creators read as names, never as nested fact lists.
        return mk('span', value.slice(0, _FACTS_MAX_ITEMS).map(v => v.name || [v.firstName, v.lastName].filter(Boolean).join(' ')).join('; ')
            + (value.length > _FACTS_MAX_ITEMS ? ` … (${value.length} total)` : ''));
    }
    if (depth >= _FACTS_MAX_DEPTH) return mk('span', Array.isArray(value) ? `${value.length} items (open the report for detail)` : `${Object.keys(value).length} fields (open the report for detail)`);
    if (Array.isArray(value)) {
        if (!value.length) return mk('span', 'none');
        if (value.every(v => v === null || typeof v !== 'object')) return mk('span', value.slice(0, _FACTS_MAX_ITEMS).map(v => _factsIsStateKey(key) ? _libraryLabel(v) : (v === null ? '—' : String(v))).join(', ') + (value.length > _FACTS_MAX_ITEMS ? ` … (${value.length} total)` : ''));
        const ol = mk('ol');
        value.slice(0, _FACTS_MAX_ITEMS).forEach((item, index) => { const li = mk('li', undefined, ol); if (item && typeof item === 'object' && !Array.isArray(item)) { const summary = item.rel || item.id || item.key || item.citation_key || item.label || item.field || item.source_id || item.action || `item ${index + 1}`; const box = mk('details', undefined, li); mk('summary', String(summary), box); _libraryFacts(box, item, depth + 1); } else _libraryFacts(li, item, depth + 1, key); });
        if (value.length > _FACTS_MAX_ITEMS) mk('p', `… ${value.length - _FACTS_MAX_ITEMS} more`);
        return ol;
    }
    const dl = mk('dl'); dl.className = 'library-facts';
    const entries = Object.entries(value).filter(([k]) => !_FACTS_HIDDEN.has(k));
    for (const [key, item] of entries.slice(0, _FACTS_MAX_ITEMS)) {
        mk('dt', _libraryKeyLabel(key), dl);
        const dd = mk('dd', undefined, dl);
        _libraryFacts(dd, item, depth + 1, key);
    }
    if (entries.length > _FACTS_MAX_ITEMS) mk('p', `… ${entries.length - _FACTS_MAX_ITEMS} more fields`, dl);
    return dl;
}
function _libraryList(parent, items, emptyText) {
    if (!items || !items.length) { const p = document.createElement('p'); p.textContent = emptyText; parent.appendChild(p); return; }
    const ul = document.createElement('ul'); parent.appendChild(ul);
    for (const item of items) { const li = document.createElement('li'); if (typeof item === 'string') li.textContent = item; else _libraryFacts(li, item); ul.appendChild(li); }
}
function _libraryTable(parent, columns, rows) {
    const wrap = document.createElement('div'); wrap.className = 'audit-table-scroll'; parent.appendChild(wrap);
    const table = document.createElement('table'); table.className = 'audit-table'; wrap.appendChild(table);
    const hr = table.createTHead().insertRow();
    for (const [label] of columns) { const th = document.createElement('th'); th.textContent = label; hr.appendChild(th); }
    const body = table.createTBody();
    for (const row of rows) { const tr = body.insertRow(); for (const [, pick] of columns) { const td = tr.insertCell(); const v = pick(row); if (v instanceof Node) td.appendChild(v); else td.textContent = v === null || v === undefined ? '—' : String(v); } }
    return table;
}


/** Mirror a visible, re-rendered status line into its STATIC live region in
 *  templates/index.html (2026-09-17 review, D5): WebKit can miss a region
 *  created and populated in the same tick, so the role lives in the markup. */
function _mirrorLibraryStatus(visible, regionId) {
    const region = document.getElementById(regionId);
    if (!region) return visible;
    const sync = () => { if (visible.isConnected) region.textContent = visible.textContent; };
    new MutationObserver(sync).observe(visible, {childList: true, characterData: true, subtree: true});
    return visible;
}

/** Shared Library entry points; old report identifiers remain accessible. */
function _mountLibrary(host, view, data) {
    const text = (tag, value, parent = host) => {
        const el = document.createElement(tag); el.textContent = value; parent.appendChild(el); return el;
    };
    const jump = (label, report, parent = host) => {
        const b = text('button', label, parent); b.type = 'button'; b.className = 'btn btn-outline';
        b.addEventListener('click', () => selectAuditReport(report));
    };
    text('h3', ({documents: 'Documents', overview: 'Overview', checks: 'Checks & campaigns', links: 'Current dependent PDF links', history: 'History & recovery', storage: 'Storage control'})[view]);
    // The storage control carries its observation under `observation`; every other view under `freshness`.
    const f = data.freshness || (data.observation ? {checked_at: data.observation.checked_at, available: data.observation.available} : {});
    text('p', `Zotero observation: ${f.checked_at ? new Date(f.checked_at * 1000).toLocaleString() : 'unavailable'}. ${f.available === false ? 'Observation unavailable; last saved evidence is retained. ' : ''}${f.error || ''} A recent observation cannot exclude a later external edit.`);
    if (view === 'overview') {
        const scanAt = _libraryDate(f.scan_at), observedAt = _libraryDate(f.checked_at);
        const nouns = {references: 'references (Zotero items and scan records)', files: 'physical PDF files in the bibliography folder', attachments: 'Zotero attachment records'};
        for (const [kind, count] of Object.entries(data.populations || {})) text('p', `${count} ${nouns[kind] || kind} — ${kind === 'files' ? (scanAt ? 'scan of ' + scanAt : 'saved scan') : (observedAt ? 'Zotero observation of ' + observedAt : 'last saved observation')}`);
        if (data.coverage?.scan_only_entries) text('p', `${data.coverage.scan_only_entries} saved scan entries without current file evidence are listed separately in Documents.`);
        jump('Browse documents', 'library_documents'); jump('Storage control', 'library_storage'); jump('Tagging', 'tagging');
        return;
    }
    if (view === 'links') {
        text('p', `${data.scope} ${data.syntax_scope}`);
        text('p', `${data.inspected_notes} of ${data.known_notes} known notes inspected. ${data.complete ? 'Known-note reads completed.' : 'Coverage incomplete; unavailable reads do not establish missing links.'}`);
        for (const note of data.notes || []) {
            const box = document.createElement('details'); host.appendChild(box);
            text('summary', `${note.note}: ${_libraryLabel(note.status)}`, box);
            if (Array.isArray(note.links) && note.links.length) _libraryTable(box, [['Link target', l => l.target], ['Status', l => _libraryLabel(l.status)], ['Resolved to', l => l.resolved || (l.candidates || []).join(', ')], ['Basis', l => l.basis]], note.links);
            _libraryFacts(box, Object.fromEntries(Object.entries(note).filter(([k]) => k !== 'links')));
        }
        jump('Checks & campaigns', 'library_checks'); return;
    }
    if (view === 'storage') {
        _mountStorageControl(host, data, text, jump);
        return;
    }
    if (view === 'checks') {
        text('p', 'Deterministic checks produce observations, never approvals. Bibliography field decisions never modify metadata.');
        jump('Storage control: stored, misplaced, broken or duplicate attachment paths', 'library_storage');
        _mountBibliographyCampaigns(host);
        jump('Inspect current PDF links in known notes', 'library_links');
        text('p', 'Prepare with library bibliography-plan using an explicit provider, model, budget and source corpus (a record naming its pdf is authenticated against its own pages first). Review with library review; record exact fields with library bibliography-decide. The metadata executor (library bibliography-apply) is closed in production pending its separate validation.');
        const counts = Object.entries(data.counts || {}).map(([state, n]) => `${_libraryLabel(state)}: ${Number(n)}`).join(' · ');
        text('p', `${(data.results || []).length} checked entries${counts ? ' — ' + counts : ''}. Observation: ${_libraryDate(f.checked_at) || 'unavailable'}; scan: ${_libraryDate(f.scan_at) || 'saved scan'}.`);
        for (const r of data.results || []) {
            const box = document.createElement('details'); host.appendChild(box);
            text('summary', `${r.pdf || r.citation_key || r.id}: ${_libraryLabel(r.status)}`, box);
            text('h5', 'Contradictions', box); _libraryList(box, r.contradictions, 'None found.');
            text('h5', 'Uncertainties', box); _libraryList(box, r.uncertainties, 'None recorded.');
            text('p', `Method: ${r.method || 'deterministic'}. No action is applied from a check.`, box);
            const evidence = document.createElement('details'); box.appendChild(evidence);
            text('summary', 'Sources and evidence', evidence);
            _libraryFacts(evidence, r.sources || {});
        }
        if (typeof window.mountLibraryMaintenance === 'function') window.mountLibraryMaintenance(host);
        jump('Tagging', 'tagging');
        jump('Notes', 'note_sync'); jump('Possible duplicate references', 'near_duplicates');
        jump('Zotero attachments (advanced)', 'zotero_attachments');
        return;
    }
    if (view === 'history') {
        text('p', 'Incomplete and unknown outcomes require inspection before recovery. Stored campaigns never replay automatically.');
        for (const c of data.note_campaigns || []) {
            const box = document.createElement('details'); host.appendChild(box);
            text('summary', `${c.kind === 'bibliography' ? 'Bibliography' : 'Notes'} campaign ${c.id}`, box);
            if (c.kind === 'bibliography') _mountBibliographyReview(box, c);
            else {
                const keys = Object.keys(c.proposals || {});
                text('p', `Notes campaign prepared ${_libraryDate(c.created_at) || 'at an unknown time'} — ${c.method || 'deterministic three-way merge'}; ${keys.length} note(s); vault ${c.scope || 'unknown'}.`, box);
                _libraryTable(box, [['Citation key', k => k], ['Outcome', k => _libraryLabel((c.outcomes || {})[k]?.status || ((c.outcomes || {})[k]?.ok === true ? 'completed' : (c.outcomes || {})[k] ? 'dispatched_or_unknown' : 'not_dispatched'))], ['Recorded', k => _libraryDate((c.outcomes || {})[k]?.at) || '—']], keys);
                const inspect = text('button', 'Inspect current destinations', box); inspect.type = 'button';
                const result = document.createElement('div'); box.appendChild(result);
                inspect.addEventListener('click', async () => {
                    inspect.disabled = true; result.replaceChildren();
                    try {
                        const response = await secureFetch(`/api/notesync/campaign/${encodeURIComponent(c.id)}/inspect`, {method: 'POST', body: '{}'});
                        const payload = await response.json();
                        if (!response.ok) throw new Error(payload.error || 'Inspection unavailable');
                        text('p', payload.note || 'Fresh inspection never replays writes, settles a journal or advances a sync base.', result);
                        const rows = Object.entries(payload.inspections || {});
                        _libraryTable(result, [['Citation key', ([k]) => k], ['State', ([, i]) => _libraryLabel(i.status)], ['Obsidian', ([, i]) => _libraryLabel(i.destinations?.obsidian)], ['Zotero', ([, i]) => _libraryLabel(i.destinations?.zotero)], ['Next action', ([, i]) => i.next_action], ['Undo', ([, i]) => i.undo ? (i.undo.applicable ? 'admissible' : 'not admissible: ' + (i.undo.reasons || []).join(', ')) : '—'], ['Settlement', ([, i]) => i.settlement ? (i.settlement.applicable ? 'settleable' : 'blocked: ' + (i.settlement.reasons || []).join(', ')) : '—']], rows);
                    } catch (error) { text('p', `Inspection unavailable: ${error.message}`, result); }
                    finally { inspect.disabled = false; }
                });
            }
        }
        for (const op of data.pdf_operations || []) {
            const box = document.createElement('details'); host.appendChild(box);
            text('summary', `Files & attachments operation ${op.id}: ${_libraryLabel(op.kind || 'cleanup')} — ${_libraryLabel(op.status)}`, box);
            text('p', `${op.key ? 'Entry ' + op.key + '. ' : ''}${_libraryDate(op.at) ? 'Recorded ' + _libraryDate(op.at) + '. ' : ''}${op.error ? 'Error: ' + op.error : ''}`, box);
            if (Array.isArray(op.steps) && op.steps.length) _libraryTable(box, [['File', st => st.rel], ['Action', st => st.action], ['Result', st => _libraryLabel(st.status)], ['Recovery copy', st => st.recovery || '—']], op.steps);
            if (op.plan) { const plan = document.createElement('details'); box.appendChild(plan); text('summary', 'Plan as reviewed', plan); _libraryFacts(plan, op.plan); }
        }
        jump('Files & attachments recovery', 'inventory');
        return;
    }
    const label = text('label', 'Filter documents ');
    const search = document.createElement('input'); search.type = 'search'; search.value = _libraryDocumentState.query; label.appendChild(search);
    const scopeLabel = text('label', 'Population ');
    const scope = document.createElement('select'); scope.setAttribute('aria-label', 'Population'); scopeLabel.appendChild(scope);
    for (const [value, name] of [['', 'All'], ['reference', 'References'], ['file', 'Files'], ['attachment', 'Standalone attachments'], ['scan_file', 'Saved files without current evidence'], ['note', 'Unjoined scan notes'], ['no_attachment', 'No attachment observed'], ['stored', 'Zotero storage'], ['no_association', 'No CHERQAiN association'], ['identity', 'Identity to review'], ['changed', 'Zotero relationship changed'], ['export', 'Awaiting bibliography export'], ['recovery', 'Recovery required'], ['excluded', 'Excluded or deferred'], ['versions', 'Document version candidates'], ['links', 'Dependent links to review'], ['ambiguous', 'Ambiguous associations'], ['note_conflict', 'Notes in conflict'], ['note_blocked', 'Notes needing content review']]) {
        const opt = text('option', name, scope); opt.value = value;
    }
    scope.value = _libraryDocumentState.filter;
    if (data.coverage?.scan_only_entries) text('p', `${data.coverage.scan_only_entries} additional saved scan entries remain visible without current file evidence. They are not counted as current physical files.`);
    let noteRows = null; let notesLoading = false;
    const noteStatus = _mirrorLibraryStatus(text('p', ''), 'library-note-status');
    const noteRowsFor = d => (noteRows || []).filter(row => {
        const keys = d.kind === 'file' ? d.row.mapping.keys : (d.scan_keys || d.citation_keys || [d.citation_key]);
        if (!keys.includes(row.citation_key)) return false;
        if (d.database && row.native_scope) return d.database === row.native_scope.database && d.library === row.native_scope.library && d.item_key === row.native_scope.item;
        return true;
    });
    const count = text('p', ''); const list = document.createElement('div'); list.className = 'library-documents-list'; host.appendChild(list);
    const detail = document.createElement('section'); detail.setAttribute('aria-label', 'Document details'); host.appendChild(detail);
    const render = () => {
        _libraryDocumentState.query = search.value; _libraryDocumentState.filter = scope.value;
        list.replaceChildren();
        const visible = data.documents.filter(d => {
            if (_libraryDocumentState.documentId && d.id !== _libraryDocumentState.documentId) return false;
            const fold = value => value.normalize('NFD').replace(/\p{M}/gu, '').toLowerCase();
            const tokens = fold(search.value).split(/\s+/).filter(Boolean);
            // The filter matches what the view shows (2026-09-17 review, F14),
            // mirroring the server's search haystack — never hidden keys.
            const pdf = d.row?.pdf || {};
            const hay = fold([d.id, d.item_key, d.title, d.citation_key, ...(d.citation_keys || []), ...(d.scan_keys || []),
                d.kind, d.path, pdf.rel, pdf.name, d.row?.state, _libraryLabel(d.row?.state), d.metadata?.DOI, ...(d.files || [])]
                .filter(v => v !== null && v !== undefined && v !== '').join('\n'));
            if (!tokens.every(token => hay.includes(token))) return false;
            const states = {identity: ['Needs identification'], changed: ['Discrepancies', 'Accepted differences'], export: ['Awaiting bibliography'], recovery: ['Recovery', 'Retired'], excluded: ['Excluded']};
            if (states[scope.value]) return d.kind === 'file' && (states[scope.value].includes(d.row.state) || (scope.value === 'excluded' && d.row.mapping.pending));
            if (scope.value === 'note_conflict') return noteRowsFor(d).some(r => r.state === 'conflict');
            if (scope.value === 'note_blocked') return noteRowsFor(d).some(r => r.state === 'unparsed' || r.content_issues?.length);
            if (scope.value === 'versions') return !!d.versions?.length;
            if (scope.value === 'links') return d.dependent_links?.some(l => l.status !== 'resolved');
            if (scope.value === 'ambiguous') return d.kind === 'file' && (d.row.mapping.keys.length > 1 || d.row.parent_state === 'ambiguous');
            if (scope.value === 'no_attachment') return d.kind === 'file' ? d.row.attachment_state === 'none_observed' : d.kind === 'reference' && data.freshness?.available === true && !d.attachments?.length;
            if (scope.value === 'no_association') return d.kind === 'file' && !d.row.mapping.keys.length;
            if (scope.value === 'stored') return d.attachments?.some(a => a.storage === 'Zotero storage');
            return !scope.value || d.kind === scope.value;
        });
        count.textContent = `${visible.length} entries shown; ${data.populations.references} references and ${data.populations.files} physical files in the full view. ${data.populations.attachments ?? 0} observed attachments; parented attachments appear within references. A file may relate to a reference; these are separate populations.`;
        for (const d of visible) {
            const b = text('button', `${d.kind}: ${d.title || d.citation_key || d.id}${d.row?.state ? ' — ' + _libraryLabel(d.row.state) : ''}`, list); b.type = 'button'; b.className = 'btn btn-outline';
            b.addEventListener('click', () => {
                _pendingTeardown(); _managementTeardown(); detail.replaceChildren();
                text('h4', d.title || d.id, detail);
                if (noteRowsFor(d).length) {
                    const notes = text('details', '', detail); text('summary', 'Current note comparison', notes);
                    _libraryTable(notes, [['Citation key', r => r.citation_key], ['State', r => _libraryLabel(r.state)], ['Sync point', r => r.baselined ? 'yes' : 'none'], ['Words to import', r => r.missing_words], ['Your words at risk', r => r.extra_words]], noteRowsFor(d));
                    jump('Review notes and resolve conflicts', 'note_sync', notes);
                }
                const evidence = document.createElement('details'); detail.appendChild(evidence);
                text('summary', 'Evidence and saved relationships', evidence);
                if (d.row?.state) text('p', `State: ${_libraryLabel(d.row.state)}${d.row.reason ? ' — ' + d.row.reason : ''}`, evidence);
                _libraryFacts(evidence, d);
                if (d.kind === 'file') {
                    const panel = document.createElement('div'); detail.appendChild(panel);
                    // Full scope preserves hidden selections and all old decisions.
                    _pendingTeardown = mountPendingReview(panel, data.pdf_workspace, {initialPdf: d.row.pdf.rel});
                    if (d.row.mapped_key) {
                        text('h4', 'Files & attachments', detail);
                        const filesPanel = document.createElement('div'); detail.appendChild(filesPanel);
                        _managementTeardown = mountPdfManagement(filesPanel, d.row.mapped_key);
                    }
                } else if (d.citation_key && (!d.citation_keys || d.citation_keys.length === 1)) {
                    text('h4', 'Files & attachments', detail);
                    const panel = document.createElement('div'); detail.appendChild(panel);
                    _managementTeardown = mountPdfManagement(panel, d.citation_keys?.[0] || d.citation_key);
                } else if (d.kind === 'scan_file' || d.kind === 'note') {
                    text('p', 'Saved scan evidence only. Review the owning report; this entry cannot authorize a file or note write.', detail);
                    for (const report of d.owning_reports || []) jump(_REPORT_LABELS[report] || report, report, detail);
                } else if (d.kind === 'attachment') {
                    jump('Inspect attachment in advanced report', 'zotero_attachments', detail);
                } else if (d.citation_keys?.length > 1) {
                    text('p', 'Multiple scan keys refer to this identity. Review the exact key in Inventory before managing files.', detail);
                    jump('Review scan records', 'inventory', detail);
                }
                detail.scrollIntoView?.({block: 'start'});
            });
        }
    };
    const loadNotes = async () => {
        if (notesLoading) return;
        notesLoading = true; noteStatus.textContent = 'Reading current note comparison…';
        try {
            const response = await secureFetch('/api/notesync/sync-status'); const result = await response.json();
            if (!host.isConnected) return;
            if (!response.ok || !Array.isArray(result.rows)) throw new Error(result.error || 'Note comparison unavailable');
            noteRows = result.rows;
            noteStatus.textContent = `Note comparison read at ${new Date().toLocaleTimeString()}. A later edit requires a fresh review.`;
        } catch (error) {
            if (!host.isConnected) return;
            noteRows = null; noteStatus.textContent = `Note comparison unavailable: ${error.message}. Empty results do not establish absence of conflicts.`;
        } finally { notesLoading = false; if (host.isConnected) render(); }
    };
    const filterChanged = () => { render(); if (scope.value.startsWith('note_') && !noteRows) loadNotes(); };
    search.addEventListener('input', render); scope.addEventListener('change', filterChanged); filterChanged();
    const showAll = text('button', 'Clear document selection'); showAll.type = 'button';
    showAll.addEventListener('click', () => { _libraryDocumentState.documentId = ''; search.value = ''; scope.value = ''; render(); });
    const refreshNotes = text('button', 'Read current note comparison'); refreshNotes.type = 'button';
    refreshNotes.addEventListener('click', loadNotes);
    text('h4', 'Advanced decisions and historical reports');
    jump('PDF decisions and exclusions', 'pdf_review'); jump('Ambiguous PDF decisions', 'ambiguous_pdfs');
    jump('Unmapped PDF identification', 'read_unzoterod'); jump('Saved exclusions and deferrals', 'not_in_zotero');
    jump('Inventory (advanced)', 'inventory'); jump('Possible duplicate decisions', 'near_duplicates');
}


function _campaignElement(parent, tag, value) {
    const el = document.createElement(tag); el.textContent = value;
    if (tag === 'button') { el.type = 'button'; el.className = 'btn btn-outline'; }
    parent.appendChild(el); return el;
}

function _mountBibliographyCampaigns(host) {
    const box = _campaignElement(host, 'details', ''); box.classList.add('library-campaign');
    _campaignElement(box, 'summary', 'Bibliography campaigns — prepare or review');
    const input = (label, type = 'text') => {
        const holder = _campaignElement(box, 'label', label + ' ');
        const el = _campaignElement(holder, 'input', ''); el.type = type; return el;
    };
    const provider = input('Provider'); const model = input('Model');
    const acquire = input('Resolve up to five explicit DOI values through Crossref before analysis', 'checkbox');
    const budget = input('Output token budget', 'number'); budget.min = '128'; budget.max = '8192';
    const label = _campaignElement(box, 'label', 'Exact source corpus (JSON array, 1–20 records, 60,000 characters maximum)');
    const corpus = _campaignElement(label, 'textarea', ''); corpus.rows = 6;
    _campaignElement(box, 'p', 'Each record needs id, current fields and sources with id, locator and text. Include page, retrieval date and claim context when known. The supplied corpus and any explicitly requested catalogue metadata are sent to the chosen model. Catalogue evidence does not verify the PDF or a claim.');
    const prepare = _campaignElement(box, 'button', 'Review generation request'); prepare.type = 'button';
    const id = input('Existing campaign ID');
    const load = _campaignElement(box, 'button', 'Load saved campaign'); load.type = 'button';
    const status = _mirrorLibraryStatus(_campaignElement(box, 'p', ''), 'library-campaign-plan-status');
    const review = _campaignElement(box, 'section', '');
    prepare.addEventListener('click', () => {
        let data;
        try {
            data = {provider: provider.value.trim(), model: model.value.trim(), max_tokens: Number(budget.value), resolve_dois: acquire.checked, corpus: JSON.parse(corpus.value)};
            if (!data.provider || !data.model || !Array.isArray(data.corpus) || !data.corpus.length || !Number.isInteger(data.max_tokens) || data.max_tokens < 128 || data.max_tokens > 8192) throw new Error('Choose a provider, model, budget and source corpus.');
        } catch (error) { status.textContent = error.message; return; }
        confirmInline(prepare, {
            message: `Send ${data.corpus.length} record(s), ${JSON.stringify(data.corpus).length} characters to ${data.provider} / ${data.model}, at most ${data.max_tokens} output tokens, one model request? ${data.resolve_dois ? 'Also send the selected DOI values to Crossref and include the acquired catalogue metadata, within the same input cap. ' : ''}This creates proposals only.`,
            confirmLabel: 'Generate proposals',
            onConfirm: async () => {
                prepare.disabled = true; status.textContent = 'Generating proposals…';
                try {
                    const r = await secureFetch('/api/audit/library/bibliography/plan', {method: 'POST', body: JSON.stringify(data)});
                    const c = await r.json(); if (!r.ok) throw new Error(c.error || 'Generation unavailable');
                    id.value = c.id; review.replaceChildren(); _mountBibliographyReview(review, c); status.textContent = 'Campaign saved.';
                } catch (error) { status.textContent = `Generation outcome unavailable; inspect History before repeating: ${error.message}`; }
                finally { prepare.disabled = false; }
            },
        });
    });
    load.addEventListener('click', async () => {
        load.disabled = true;
        try {
            const r = await secureFetch('/api/notesync/campaign/' + encodeURIComponent(id.value.trim()));
            const c = await r.json(); if (!r.ok) throw new Error(c.error || 'Campaign unavailable');
            if (c.kind !== 'bibliography') throw new Error('Choose a bibliography campaign.');
            review.replaceChildren(); _mountBibliographyReview(review, c); status.textContent = 'Saved campaign loaded.';
        } catch (error) { status.textContent = error.message; }
        finally { load.disabled = false; }
    });
}

function _mountBibliographyReview(host, campaign) {
    host.classList.add('library-campaign');
    const text = (tag, value, parent = host) => _campaignElement(parent, tag, value);
    text('h4', `Bibliography campaign ${campaign.id}`);
    const request = campaign.proposals?.input || {};
    text('p', `Model: ${request.provider || 'unknown'} / ${request.model || 'unknown'}; budget: ${request.max_tokens || 'unknown'} tokens; prepared ${_libraryDate(campaign.created_at) || 'at an unknown time'}. Metadata application is closed in production pending its separate validation; approved fields can be written with papermind library bibliography-apply once it is opened.`);
    const acquisition = campaign.outcomes?.source_acquisition;
    if (acquisition) { const sources = text('details', ''); text('summary', 'Acquired catalogue observations — not PDF or claim verification', sources); _libraryTable(sources, [['Record', o => o.record_id], ['DOI', o => o.doi], ['Result', o => _libraryLabel(o.status)], ['Existence', o => _libraryLabel(o.publication_existence)], ['Retrieved', o => _libraryDate(o.retrieved_at) || '—'], ['Note', o => o.reason || '']], acquisition.observations || []); }
    const authentication = campaign.outcomes?.pdf_authentication;
    if (authentication) { const box = text('details', ''); text('summary', 'PDF authentication — independent of any catalogue', box); _libraryTable(box, [['Record', ([k]) => k], ['Document', ([, r]) => _libraryLabel(r.status)], ['DOI verdict', ([, r]) => _libraryLabel(r.doi?.status)], ['Title', ([, r]) => r.title?.found ? `page ${r.title.page}` : 'not found'], ['Authors found', ([, r]) => (r.authors?.found || []).join(', ') || '—'], ['Pages read', ([, r]) => `${r.pages_scanned ?? '—'} of ${r.page_count ?? '—'}${r.truncated ? ' (truncated)' : ''}`], ['Reason', ([, r]) => r.reason]], Object.entries(authentication.reports || {})); }
    const application = campaign.outcomes?.application;
    if (application) { const box = text('details', ''); text('summary', `Metadata application — ${_libraryLabel(application.status)}`, box); _libraryTable(box, [['Record', ([k]) => k], ['Result', ([, r]) => _libraryLabel(r.status)], ['Message', ([, r]) => r.message || ''], ['Journal op', ([, r]) => r.op_id || '—']], Object.entries(application.results || {})); }
    const generation = campaign.outcomes?.generation || {};
    text('p', `Generation: ${_libraryLabel(generation.status || 'unknown')}. ${generation.error || ''}`);
    if (generation.missing_records?.length) text('p', `No proposal returned for: ${generation.missing_records.join(', ')}`);
    const selected = [];
    const saved = campaign.outcomes?.review?.decisions || [];
    for (const proposal of generation.proposals || []) {
        text('h5', proposal.id);
        for (const question of ['publication_existence', 'pdf_correspondence', 'citation_support']) {
            const judgment = proposal[question] || {};
            const evidence = text('details', ''); text('summary', `${question.replaceAll('_', ' ')}: ${judgment.status || 'unknown'} — evidence ${judgment.evidence_status || 'unknown'}`, evidence);
            const assessment = judgment.model_assessment || {};
            text('p', `Model assessment (unverified): ${assessment.status || 'none'}${assessment.reason ? ' — ' + assessment.reason : ''}`, evidence);
            if (judgment.citations?.length) _libraryTable(evidence, [['Source', c => c.source_id], ['Excerpt', c => c.excerpt], ['Located in source', c => c.excerpt_verified ? 'yes' : 'no'], ['Locator', c => c.locator || '—']], judgment.citations);
            _libraryList(evidence, judgment.issues, 'No issues recorded.');
        }
        for (const field of proposal.fields || []) {
            const panel = text('fieldset', ''); text('legend', field.field, panel);
            for (const [caption, v] of [['Current', field.current], ['Proposed', field.proposed]]) {
                const line = text('p', `${caption}: `, panel);
                if (typeof v === 'string') line.append(v); else _libraryFacts(line, v, 1, field.field);
            }
            text('p', `Excerpt located: ${(field.excerpt_verified ?? field.source_supported) ? 'yes' : 'no'}. Semantic support: unverified. A located excerpt does not justify a correction by itself.`, panel);
            text('p', `Source: ${field.source?.locator || 'none'}${field.source?.kind ? ' (' + field.source.kind + ')' : ''}. Excerpt: ${field.excerpt || '—'}${field.excerpt_offset != null ? ' at offset ' + field.excerpt_offset : ''}. ${field.source_authentication || ''}`, panel);
            if (field.contradictions?.length) { text('p', 'Contradictions:', panel); _libraryList(panel, field.contradictions, 'None.'); }
            _libraryList(panel, field.uncertainties, 'No uncertainty recorded.');
            const previous = saved.find(d => d.record_id === proposal.id && d.field === field.field);
            if (previous) text('p', `Saved decision: ${previous.approve ? 'approved for review' : 'rejected'} — ${previous.reason || ''}`, panel);
            const label = text('label', 'Decision ', panel); const choice = text('select', '', label); choice.setAttribute('aria-label', 'Decision');
            for (const [value, caption] of [['', 'Leave unchanged'], ['approve', 'Approve reviewed value'], ['reject', 'Reject proposal']]) { const option = text('option', caption, choice); option.value = value; }
            const originalValue = previous && Object.hasOwn(previous, 'reviewed_value') ? previous.reviewed_value : field.proposed;
            const stringValue = typeof originalValue === 'string';
            const valueLabel = text('label', stringValue ? 'Reviewed value ' : 'Reviewed structured value (JSON) ', panel);
            const value = text('textarea', stringValue ? originalValue : JSON.stringify(originalValue), valueLabel);
            value.setAttribute('aria-label', stringValue ? 'Reviewed value' : 'Reviewed structured value (JSON)');
            const reasonLabel = text('label', 'Explain whether the source supports this exact field and value ', panel);
            const reason = text('textarea', '', reasonLabel);
            selected.push({proposal, field, choice, reason, value, stringValue});
        }
    }
    const save = text('button', 'Review selected field decisions'); save.type = 'button';
    const status = _mirrorLibraryStatus(text('p', ''), 'library-campaign-review-status');
    save.addEventListener('click', () => {
        let decisions;
        try {
            decisions = selected.filter(s => s.choice.value).map(s => ({record_id: s.proposal.id, field: s.field.field, evidence: s.field.evidence,
                approve: s.choice.value === 'approve', support_assessment: s.choice.value === 'approve' ? 'supported' : 'not_supported', reason: s.reason.value.trim(), reviewed_value: s.stringValue ? s.value.value : JSON.parse(s.value.value)}));
            if (!decisions.length) throw new Error('Select at least one exact field decision.');
            if (decisions.some(d => d.approve && !d.reason)) throw new Error('Explain support for every approved field.');
        } catch (error) { status.textContent = error.message; return; }
        confirmInline(save, {
            message: `Record these decisions: ${decisions.map(d => `${d.record_id} / ${d.field}: ${d.approve ? 'approve' : 'reject'} ${JSON.stringify(d.reviewed_value)}`).join('; ')}? This does not modify metadata.`,
            confirmLabel: 'Record decisions',
            onConfirm: async () => {
                save.disabled = true;
                try {
                    const r = await secureFetch(`/api/audit/library/bibliography/${encodeURIComponent(campaign.id)}/decisions`, {method: 'POST', body: JSON.stringify({confirm: true, decisions})});
                    const data = await r.json(); if (!r.ok) throw new Error(data.error || 'Decision recording unavailable');
                    selected.forEach(s => { s.choice.value = ''; }); status.textContent = 'Decisions recorded. Other field decisions and previous review history are preserved. No metadata was changed.';
                } catch (error) { status.textContent = `Inspect saved decisions before retrying: ${error.message}`; }
                finally { save.disabled = false; }
            },
        });
    });
}
