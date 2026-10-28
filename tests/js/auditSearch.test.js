// Pins the audit cross-report search panel (audit.js).
//
// The search panel is an OVERLAY over the report body, not a mode: clearing
// the box must restore exactly the report the user was looking at, and a
// superseded in-flight query must never repaint over it. Both are the
// latest-wins / restore-state classes this file's sibling tests already guard
// elsewhere in the app.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { initAuditTab } from '../../static/js/audit.js';

function jsonOk(payload) {
    return { ok: true, status: 200, json: async () => payload };
}

const DOM = `
  <div id="audit-vault-path-display"></div>
  <div id="audit-status-banner"></div>
  <div id="audit-empty-state"></div>
  <div id="audit-summary" style="display:none;"></div>
  <div id="audit-report-tabs" style="display:none;"></div>
  <div id="audit-search-bar" style="display:none;">
    <input id="audit-search-input" type="search">
    <button id="audit-search-restore" style="display:none;">Show results</button>
    <button id="audit-search-clear" style="display:none;">Clear</button>
  </div>
  <div id="audit-report-meta" style="display:none;"></div>
  <div id="audit-search-results" style="display:none;"></div>
  <div id="audit-report-body"></div>
`;

/** Type into the search box and let the debounce fire. */
async function type(value) {
    const input = document.getElementById('audit-search-input');
    input.value = value;
    input.dispatchEvent(new Event('input'));
    await vi.advanceTimersByTimeAsync(400);
    await Promise.resolve();
}

describe('audit cross-report search', () => {
    let fetchMock;

    beforeEach(async () => {
        vi.useFakeTimers();
        document.body.innerHTML = DOM;
        fetchMock = vi.fn(async (url) => {
            if (String(url).includes('/api/audit/config')) return jsonOk({});
            if (String(url).includes('/api/audit/status')) {
                return jsonOk({ state: 'idle', has_results: false });
            }
            return jsonOk({ q: 'x', total: 0, shown: 0, truncated: false, groups: [] });
        });
        vi.stubGlobal('fetch', fetchMock);
        await initAuditTab();
        fetchMock.mockClear();
    });

    afterEach(() => {
        vi.unstubAllGlobals();
        vi.useRealTimers();
    });

    it('queries /api/audit/search and overlays the report body', async () => {
        fetchMock.mockImplementation(async () => jsonOk({
            q: 'dupont', total: 1, shown: 1, truncated: false,
            groups: [{
                report: 'inventory', count: 1, truncated: false,
                rows: [{ label: 'Étude sur le sommeil', detail: 'Dupont · 2019',
                         citation_key: 'dupont2019', pdf_rel: null }],
            }],
        }));
        await type('dupont');

        const url = String(fetchMock.mock.calls[0][0]);
        expect(url).toContain('/api/audit/search?q=dupont');
        const results = document.getElementById('audit-search-results');
        expect(results.style.display).not.toBe('none');
        expect(results.textContent).toContain('Étude sur le sommeil');
        // The report body is hidden, not destroyed.
        expect(document.getElementById('audit-report-body').style.display).toBe('none');
    });

    it('opens a search identity in Documents without losing the historical report action', async () => {
        fetchMock.mockImplementation(async url => {
            if (String(url).includes('/api/audit/library/documents')) return jsonOk({freshness: {}, populations: {references: 1, files: 0}, documents: [
                {id: 'r', kind: 'reference', citation_key: 'dupont2019', title: 'Étude', attachments: []},
            ]});
            return jsonOk({q: 'dupont', total: 1, shown: 1, groups: [{report: 'inventory', count: 1, rows: [{label: 'Étude', citation_key: 'dupont2019'}]}]});
        });
        await type('dupont');
        expect(document.querySelector('.audit-search-hit')).toBeTruthy();
        document.querySelector('.audit-search-documents').click();
        await vi.advanceTimersByTimeAsync(50);
        expect(document.querySelector('#audit-report-body input').value).toBe('dupont2019');
        expect(document.querySelector('#audit-report-body').textContent).toContain('1 entries shown');
        expect(document.getElementById('audit-search-input').value).toBe('dupont');
        expect(document.activeElement.textContent).toBe('reference: Étude');
    });

    it('opens the exact native document despite a colliding citation key', async () => {
        fetchMock.mockImplementation(async url => {
            if (String(url).includes('/api/audit/library/documents')) return jsonOk({freshness: {}, populations: {references: 2, files: 0}, documents: [
                {id: 'native:one', kind: 'reference', citation_key: 'same', title: 'First', attachments: []},
                {id: 'native:two', kind: 'reference', citation_key: 'same', title: 'Second', attachments: []},
            ]});
            return jsonOk({q: 'same', total: 1, shown: 1, groups: [{report: 'library_documents', count: 1,
                rows: [{label: 'Second', citation_key: 'same', document_id: 'native:two'}]}]});
        });
        await type('same');
        document.querySelector('.audit-search-hit').click();
        await vi.advanceTimersByTimeAsync(50);
        expect(document.querySelector('.library-documents-list').textContent).toBe('reference: Second');
        [...document.querySelectorAll('button')].find(b => b.textContent === 'Clear document selection').click();
        expect(document.querySelector('.library-documents-list').textContent).toContain('reference: First');
    });

    it('does not query below the minimum length, but KEEPS what was typed', async () => {
        // Regression: the short-query path used to route through _clearSearch,
        // which empties the input — so pausing mid-word past the debounce
        // erased the letter the user had just typed.
        await type('d');
        expect(fetchMock).not.toHaveBeenCalled();
        expect(document.getElementById('audit-search-results').style.display).toBe('none');
        expect(document.getElementById('audit-search-input').value).toBe('d');
    });

    it('keeps the query when jumping to a hit, and offers it back', async () => {
        fetchMock.mockImplementation(async (url) => {
            if (String(url).includes('/api/audit/search')) {
                return jsonOk({
                    q: 'dupont', total: 1, shown: 1, truncated: false,
                    groups: [{
                        report: 'zotero_no_pdf', count: 1, truncated: false,
                        rows: [{ label: 'Étude', detail: '', citation_key: 'dupont2019' }],
                    }],
                });
            }
            return jsonOk({ name: 'zotero_no_pdf', rows: [] });
        });
        await type('dupont');
        document.querySelector('.audit-search-hit').click();
        await vi.advanceTimersByTimeAsync(50);

        // Followed the hit into its report — the query survives the jump.
        expect(document.getElementById('audit-search-input').value).toBe('dupont');
        expect(document.getElementById('audit-report-body').style.display).toBe('');
        const restore = document.getElementById('audit-search-restore');
        expect(restore.style.display).not.toBe('none');
        expect(document.getElementById('audit-search-clear').style.display).not.toBe('none');

        // …and comes back without a retype.
        restore.click();
        await vi.advanceTimersByTimeAsync(50);
        expect(document.getElementById('audit-search-results').textContent).toContain('Étude');
    });

    it('Enter re-opens the results for a still-typed query', async () => {
        await type('dupont');
        document.getElementById('audit-search-clear');
        // Close the overlay the way a tab switch does, then press Enter.
        fetchMock.mockClear();
        const input = document.getElementById('audit-search-input');
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
        await vi.advanceTimersByTimeAsync(50);
        expect(String(fetchMock.mock.calls[0][0])).toContain('q=dupont');
    });

    it('Clear restores the report body and empties the panel', async () => {
        await type('dupont');
        expect(document.getElementById('audit-report-body').style.display).toBe('none');

        document.getElementById('audit-search-clear').click();

        const results = document.getElementById('audit-search-results');
        expect(results.style.display).toBe('none');
        expect(results.innerHTML).toBe('');
        expect(document.getElementById('audit-report-body').style.display).toBe('');
        expect(document.getElementById('audit-search-input').value).toBe('');
    });

    it('Escape in the search box clears it', async () => {
        await type('dupont');
        const input = document.getElementById('audit-search-input');
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
        expect(input.value).toBe('');
        expect(document.getElementById('audit-report-body').style.display).toBe('');
    });

    it('a superseded slow query never repaints the panel', async () => {
        // Query A hangs; query B resolves first. A's late response must be
        // dropped by the latest-wins gate rather than clobbering B's results.
        let resolveA;
        fetchMock.mockImplementationOnce(() => new Promise(res => { resolveA = res; }));
        fetchMock.mockImplementationOnce(async () => jsonOk({
            q: 'beta', total: 1, shown: 1, truncated: false,
            groups: [{ report: 'inventory', count: 1, truncated: false,
                       rows: [{ label: 'BETA-RESULT', detail: '' }] }],
        }));

        await type('alpha');
        await type('beta');
        expect(document.getElementById('audit-search-results').textContent)
            .toContain('BETA-RESULT');

        resolveA(jsonOk({
            q: 'alpha', total: 1, shown: 1, truncated: false,
            groups: [{ report: 'inventory', count: 1, truncated: false,
                       rows: [{ label: 'STALE-ALPHA', detail: '' }] }],
        }));
        await vi.advanceTimersByTimeAsync(50);

        const text = document.getElementById('audit-search-results').textContent;
        expect(text).toContain('BETA-RESULT');
        expect(text).not.toContain('STALE-ALPHA');
    });

    it('escapes server-supplied row text rather than injecting HTML', async () => {
        fetchMock.mockImplementation(async () => jsonOk({
            q: 'x', total: 1, shown: 1, truncated: false,
            groups: [{
                report: 'inventory', count: 1, truncated: false,
                rows: [{ label: '<img src=x onerror=alert(1)>', detail: '' }],
            }],
        }));
        await type('xx');
        const results = document.getElementById('audit-search-results');
        expect(results.querySelector('img')).toBeNull();
        expect(results.textContent).toContain('<img src=x onerror=alert(1)>');
    });
});
