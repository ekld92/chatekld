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
import { secureFetch, logError } from './api.js';
import { setStatusA11y, closeModal, taskBegin, taskEnd, makeLatestGate } from './ui.js';

const _STATUS_POLL_MS = 1500;
let _statusPollTimer = null;
let _currentReport = 'inventory';
let _lastSummary = null;

const _REPORT_LABELS = {
    inventory: 'Inventory',
    note_tag_drift: 'Tag Drift',
    unread_unzoterod: 'Unread PDFs',
    zotero_unread: 'Zotero Queue',
    read_unzoterod: 'Read PDFs Missing Zotero',
    zotero_no_pdf: 'Bib Entries Without PDFs',
    duplicates: 'Duplicate PDFs',
};

/**
 * Initialise the Audit tab: load + display the audit config and the reused vault
 * path, then read status. If a prior in-memory scan left results, render them
 * WITHOUT polling — the scan itself only ever runs on an explicit Run Scan.
 */
export async function initAuditTab() {
    try {
        const cfg = await fetchAuditConfig();
        _populateAuditSettingsForm(cfg);
        _setVaultDisplay(cfg.obsidian_vault_path);
    } catch (e) {
        logError('audit init', e);
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
    ];
    for (const [domId, cfgKey] of fields) {
        const el = document.getElementById(domId);
        if (!el) continue;
        const v = cfg[cfgKey];
        el.value = v == null ? '' : String(v);
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
            body: JSON.stringify({ count_annotations: true, include_duplicates: true }),
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

/** Request cancellation of the running scan; polling notices the state change
 * and tears down the in-flight UI. */
export async function cancelAuditScan() {
    try {
        await secureFetch('/api/audit/cancel', { method: 'POST' });
    } catch (e) {
        logError('audit cancel', e);
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
    _renderStatusBanner(status);
    if (status.state !== 'scanning') {
        _stopStatusPolling();
        const btn = document.getElementById('audit-run-btn');
        const cancelBtn = document.getElementById('audit-cancel-btn');
        if (btn) btn.disabled = false;
        if (cancelBtn) cancelBtn.style.display = 'none';
        taskEnd('audit-scan');
        if (status.state === 'done' || (status.state === 'cancelled' && status.has_results)) {
            await _loadReport(_currentReport);
        }
    }
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

/** Switch the active report tab (updating ARIA selection state) and load it. */
export async function selectAuditReport(name) {
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
            if (tabs) tabs.style.display = '';
            if (summaryBox) {
                summaryBox.style.display = '';
                summaryBox.innerHTML = _summaryHtml(d.summary);
            }
            if (meta) {
                meta.style.display = '';
                meta.textContent = `${d.records.length} records (one per citation key).`;
            }
            body.innerHTML = _inventoryTable(d.records);
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
            if (tabs) tabs.style.display = '';
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
    if (tabs) { tabs.style.display = 'none'; }
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
            <td class="cell-title" title="${_esc(r.title || '')}">${_esc((r.title || '').slice(0, 90))}</td>
            <td>${r.has_bib_entry ? '✓' : ''}</td>
            <td>${r.has_zotero_item ? '✓' : ''}</td>
            <td>${r.has_obsidian_note ? '✓' : ''}</td>
            <td>${_esc(r.pdf_count)}${_pdfActions(r)}</td>
            <td>${r.annotations_count_max >= 0 ? r.annotations_count_max : '—'}</td>
            <td>${_esc((r.match_sources || []).join(','))}</td>
        </tr>`).join('');
    return `<table class="audit-table">${head}<tbody>${rows}</tbody></table>`;
}

function _pdfActions(r) {
    if (!r.pdf_paths || !r.pdf_paths.length) return '';
    return r.pdf_paths.map(p =>
        ` <button type="button" class="audit-link-btn" data-action="open" data-abs="${_attr(p.abs)}" title="Open ${_attr(p.rel)}">[open]</button>`
    ).join('');
}

function _renderReport(name, d) {
    if (name === 'note_tag_drift') {
        const meta = `${_esc(d.rows.length)} citation keys with Zotero note tags that the Obsidian YAML is missing.`;
        const head = '<thead><tr><th scope="col">Key</th><th scope="col">Author</th><th scope="col">Title</th><th scope="col">Zotero note tags</th><th scope="col">Obs YAML tags</th><th scope="col">Missing in Obs</th></tr></thead>';
        const rows = d.rows.map(r => `
            <tr>
                <td>${_esc(r.citation_key)}</td>
                <td>${_esc(r.author || '')}</td>
                <td class="cell-title" title="${_esc(r.title || '')}">${_esc((r.title || '').slice(0, 80))}</td>
                <td>${_esc(r.zotero_note_tags.join(', '))}</td>
                <td>${_esc(r.obs_tags.join(', '))}</td>
                <td><strong>${_esc(r.missing_in_obs.join(', '))}</strong></td>
            </tr>`).join('');
        return [meta, `<table class="audit-table">${head}<tbody>${rows}</tbody></table>`];
    }
    if (name === 'unread_unzoterod') {
        const meta = `${_esc(d.rows.length)} unmapped PDFs with fewer than ${_esc(d.threshold)} annotations · ${_esc(d.ambiguous_count)} ambiguous PDFs excluded.`;
        return [meta, _pdfReportTable(d.rows)];
    }
    if (name === 'read_unzoterod') {
        const meta = `${_esc(d.rows.length)} unmapped PDFs ranked by annotation count (suggested read cutoff: ${_esc(d.suggested_read_cutoff)}).`;
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
                <td class="cell-title" title="${_esc(r.title || '')}">${_esc((r.title || '').slice(0, 120))}</td>
            </tr>`).join('');
        return [meta, `<table class="audit-table">${head}<tbody>${rows}</tbody></table>`];
    }
    if (name === 'zotero_no_pdf') {
        const meta = `${_esc(d.rows.length)} bib entries with no resolved PDF.`;
        const head = '<thead><tr><th scope="col">Year</th><th scope="col">Key</th><th scope="col">Author</th><th scope="col">Title</th><th scope="col">Zotero match</th></tr></thead>';
        const rows = d.rows.map(r => `
            <tr>
                <td>${_esc(r.year || '')}</td>
                <td>${_esc(r.citation_key)}</td>
                <td>${_esc(r.author || '')}</td>
                <td class="cell-title" title="${_esc(r.title || '')}">${_esc((r.title || '').slice(0, 120))}</td>
                <td>${r.has_zotero_match ? '✓' : ''}</td>
            </tr>`).join('');
        return [meta, `<table class="audit-table">${head}<tbody>${rows}</tbody></table>`];
    }
    if (name === 'duplicates') {
        const meta = `${_esc(d.rows.length)} duplicate sets · ${_fmtBytes(d.total_wasted_bytes)} wasted.`;
        const head = '<thead><tr><th scope="col">Hash</th><th scope="col">Size</th><th scope="col">Wasted</th><th scope="col">Files</th></tr></thead>';
        const rows = d.rows.map(r => `
            <tr>
                <td><code>${_esc(r.content_hash.slice(0, 12))}</code></td>
                <td>${_fmtBytes(r.size_bytes)}</td>
                <td>${_fmtBytes(r.wasted_bytes)}</td>
                <td>${r.paths.map(p => `<div>${_esc(p.rel)} <button type="button" class="audit-link-btn" data-action="reveal" data-abs="${_attr(p.abs)}">[reveal]</button></div>`).join('')}</td>
            </tr>`).join('');
        return [meta, `<table class="audit-table">${head}<tbody>${rows}</tbody></table>`];
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
    return `<table class="audit-table">${head}<tbody>${body}</tbody></table>`;
}

function _wireRowActions() {
    const body = document.getElementById('audit-report-body');
    if (!body) return;
    body.querySelectorAll('.audit-link-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.preventDefault();
            const action = btn.getAttribute('data-action');
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
