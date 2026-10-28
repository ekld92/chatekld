// 2026-09-02 — the audit tab rendered its whole report at LAUNCH.
//
// DEFECT PINNED: initAuditTab ran _loadReport as part of app.js's init
// sequence, so a cached scan made the browser fetch and render the full
// inventory (3.71 MB, ~6 000 rows) before the refactor, deck, docgen and
// maintenance init steps ran — while the vault tab was the one on screen.
//
// The invariants it must not break: the tiles still refresh (through
// /api/audit/summary, the dashboard's own feed), the scan is never started or
// polled by this path, and opening the tab still shows the report.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const DOM = `
  <div class="content-area active" id="obsidian-tab"></div>
  <div class="content-area" id="audit-tab">
    <button id="audit-run-btn"></button>
    <button id="audit-cancel-btn" style="display:none;"></button>
    <div id="audit-summary" style="display:none;"></div>
    <div id="audit-report-tabs" style="display:none;"></div>
    <div id="audit-search-bar" style="display:none;">
      <input id="audit-search-input"><button id="audit-search-clear"></button>
    </div>
    <div id="audit-report-meta"></div>
    <div id="audit-search-results" style="display:none;"></div>
    <div id="audit-report-body"></div>
    <div id="audit-empty-state"></div>
    <div id="audit-init-error"></div>
    <div id="audit-status-banner" style="display:none;"></div>
  </div>
  <div id="sr-status-announcer" role="status"></div>
`;

const SUMMARY = { record_count: 6141, tag_coverage: null };
const jsonOk = (p) => ({ ok: true, status: 200, json: async () => p });

function routes() {
    return vi.fn(async (url) => {
        const u = String(url);
        if (u.includes('/api/audit/summary')) return jsonOk({ summary: SUMMARY });
        if (u.includes('/api/audit/config')) return jsonOk({ config: {} });
        if (u.includes('/api/audit/status')) {
            return jsonOk({
                state: 'done', error: null, has_results: true,
                has_duplicates: false, has_near_duplicates: false,
                started_at: 1, finished_at: 2, messages: [],
            });
        }
        if (u.includes('/api/audit/inventory')) {
            return jsonOk({ summary: SUMMARY, records: [] });
        }
        return jsonOk({ name: 'inventory', rows: [] });
    });
}

const calls = (mock, frag) =>
    mock.mock.calls.filter((c) => String(c[0]).includes(frag)).length;

beforeEach(() => {
    document.body.innerHTML = DOM;
    vi.stubGlobal('localStorage', {
        getItem: () => null, setItem: () => {}, removeItem: () => {},
    });
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

async function load() {
    vi.resetModules();
    return import('../../static/js/audit.js');
}

describe('audit first report load is deferred to tab activation', () => {
    it('launch does not fetch the inventory', async () => {
        const fetchMock = routes();
        vi.stubGlobal('fetch', fetchMock);
        const { initAuditTab } = await load();

        await initAuditTab();

        expect(calls(fetchMock, '/api/audit/inventory')).toBe(0);
        // …but the dashboard tiles are refreshed, from their own endpoint.
        expect(calls(fetchMock, '/api/audit/summary')).toBe(1);
        // …and no scan was started or polled by this path.
        expect(calls(fetchMock, '/api/audit/scan')).toBe(0);
    });

    it('opens the daily workflow without fetching the inventory', async () => {
        const fetchMock = routes();
        vi.stubGlobal('fetch', fetchMock);
        const { initAuditTab } = await load();
        const UI = await import('../../static/js/ui.js');
        window.requestAnimationFrame = (fn) => { fn(); return 1; };

        await initAuditTab();
        UI.showTab('audit');
        await new Promise((r) => setTimeout(r, 0));
        expect(calls(fetchMock, '/api/audit/inventory')).toBe(0);
        expect(document.getElementById('audit-report-body').textContent).toContain('Review library update');

        // A second activation must not re-arm the deferred load.
        UI.showTab('obsidian');
        UI.showTab('audit');
        await new Promise((r) => setTimeout(r, 0));
        expect(calls(fetchMock, '/api/audit/inventory')).toBe(0);
    });

    it('loads immediately when the audit tab is already the active one', async () => {
        document.getElementById('obsidian-tab').classList.remove('active');
        document.getElementById('audit-tab').classList.add('active');
        const fetchMock = routes();
        vi.stubGlobal('fetch', fetchMock);
        const { initAuditTab } = await load();

        await initAuditTab();
        await new Promise((r) => setTimeout(r, 0));
        expect(calls(fetchMock, '/api/audit/inventory')).toBe(0);
    });
});

it('replaces the Library loading placeholder on no-scan and server errors', async () => {
    const fetchMock = vi.fn(async () => ({ok: false, status: 404,
        json: async () => ({error: 'No scan results. Run a scan explicitly first.'})}));
    vi.stubGlobal('fetch', fetchMock);
    const {selectAuditReport} = await load();
    await selectAuditReport('library_documents');
    const body = document.getElementById('audit-report-body');
    expect(body.textContent).toContain('No scan results');
    expect(body.querySelector('[aria-busy]')).toBeNull();
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(['/api/audit/library/documents']);
});
