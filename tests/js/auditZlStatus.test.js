// Re-point outcomes must survive `_loadReport('zotero_attachments')`
// (2026-09-19). The picker lived only in announceStatus; the report then
// rebuilt identical rows and the write looked like a no-op.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

let selectAuditReport;

function jsonOk(payload) {
    return { ok: true, status: 200, json: async () => payload };
}

const DOM = `
  <div id="audit-summary" style="display:none;"></div>
  <div id="audit-report-tabs" style="display:none;"></div>
  <div id="audit-search-bar" style="display:none;">
    <input id="audit-search-input"><button id="audit-search-clear"></button>
  </div>
  <div id="audit-status-banner" role="status" style="display:none;"></div>
  <div id="audit-report-meta"></div>
  <div id="audit-search-results" style="display:none;"></div>
  <div id="audit-report-body"></div>
  <div id="audit-empty-state"></div>
  <div id="sr-status-announcer" role="status"></div>
`;

const ATTACH_REPORT = {
    count: 1, linked: 1, missing: 1,
    rows: [{
        item_key: 'ATT1', parent_key: 'PAR1', parent_title: 'A paper',
        citation_key: 'paperA', filename: 'gone.pdf', path: '/v/gone.pdf',
        link_mode: 'linked_file', file_owned_by_zotero: false,
        annotation_count: 0, exists: false,
    }],
};

function attachStatus() {
    const el = document.getElementById('audit-zl-status');
    return el ? el.textContent : null;
}

function acceptConfirm() {
    const strip = document.querySelector('.inline-confirm-strip');
    expect(strip, 'no confirm strip appeared').toBeTruthy();
    const buttons = [...strip.querySelectorAll('button')];
    buttons[buttons.length - 1].click();
}

function mkFetch(overrides = {}) {
    return vi.fn(async (url, opts) => {
        const u = String(url);
        for (const [needle, resp] of Object.entries(overrides)) {
            if (u.includes(needle)) {
                return typeof resp === 'function' ? resp(u, opts) : resp;
            }
        }
        if (u.includes('/api/audit/summary')) return jsonOk({ summary: {} });
        if (u.includes('/api/audit/reports/zotero_attachments')) return jsonOk(ATTACH_REPORT);
        return jsonOk({ ok: true });
    });
}

async function load(fetchMock) {
    const store = new Map();
    vi.stubGlobal('localStorage', {
        getItem: (k) => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => store.set(k, String(v)),
        removeItem: (k) => store.delete(k),
    });
    vi.stubGlobal('fetch', fetchMock);
    vi.resetModules();
    ({ selectAuditReport } = await import('../../static/js/audit.js'));
    await selectAuditReport('zotero_attachments');
    await vi.waitFor(() => {
        expect(document.querySelector('[data-action="zl-repoint"]')).toBeTruthy();
    });
}

beforeEach(() => { document.body.innerHTML = DOM; });
afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = ''; });

describe('Zotero Attachments: a re-point outcome is visible after reload', () => {
    it('paints the tab status line and keeps it after the report re-renders', async () => {
        const fetchMock = mkFetch({
            '/api/zotlink/repoint-candidates': jsonOk({
                stored_path: '/v/gone.pdf', total: 1,
                candidates: [{ rel: 'biblio_articles/found.pdf' }],
            }),
            '/api/zotlink/repoint': jsonOk({
                ok: true, note: 'The next scan will pair from the new path.',
            }),
        });
        await load(fetchMock);
        document.querySelector('[data-action="zl-repoint"]').click();
        await vi.waitFor(() => {
            expect(attachStatus()).toMatch(/Re-point picker open/);
            expect(document.querySelector('.audit-zl-repoint-list button')).toBeTruthy();
        });
        document.querySelector('.audit-zl-repoint-list button').click();
        acceptConfirm();
        await vi.waitFor(() => {
            expect(attachStatus()).toContain('Zotero now points at biblio_articles/found.pdf');
        });
        expect(document.getElementById('audit-zl-status').textContent)
            .toContain('Zotero now points at biblio_articles/found.pdf');
    });
});

describe('Zotero Attachments: title = filename', () => {
    // The route shipped 2026-09-18 with a CLI caller only, so 653 connector
    // "Full Text PDF" titles were unreachable from the app until 2026-09-20.
    // Behavioural: render, click, assert the request and the visible line.
    const MIXED = {
        count: 3, linked: 3, missing: 0,
        rows: [
            {item_key: 'A1', parent_key: 'P1', parent_title: 'One', citation_key: 'a2020',
             filename: 'a_2020.pdf', title: 'Full Text PDF', path: '/v/a_2020.pdf',
             link_mode: 'linked_file', file_owned_by_zotero: false,
             annotation_count: 0, exists: true},
            {item_key: 'A2', parent_key: 'P2', parent_title: 'Two', citation_key: 'b2021',
             filename: 'b_2021.pdf', title: 'b_2021.pdf', path: '/v/b_2021.pdf',
             link_mode: 'linked_file', file_owned_by_zotero: false,
             annotation_count: 0, exists: true},
            // Zotero owns these bytes: there is no vault filename to adopt.
            {item_key: 'A3', parent_key: 'P3', parent_title: 'Three', citation_key: 'c2022',
             filename: 'c_2022.pdf', title: 'Snapshot', path: null,
             link_mode: 'imported_url', file_owned_by_zotero: true,
             annotation_count: 0, exists: null},
        ],
    };

    async function loadMixed(overrides = {}, {expectVerb = true} = {}) {
        const fetchMock = mkFetch({
            '/api/audit/reports/zotero_attachments': jsonOk(MIXED),
            ...overrides,
        });
        const store = new Map();
        vi.stubGlobal('localStorage', {
            getItem: (k) => (store.has(k) ? store.get(k) : null),
            setItem: (k, v) => store.set(k, String(v)),
            removeItem: (k) => store.delete(k),
        });
        vi.stubGlobal('fetch', fetchMock);
        vi.resetModules();
        ({ selectAuditReport } = await import('../../static/js/audit.js'));
        await selectAuditReport('zotero_attachments');
        await vi.waitFor(() => {
            expect(document.getElementById('audit-zl-status')).toBeTruthy();
        });
        if (expectVerb) {
            await vi.waitFor(() => {
                expect(document.getElementById('zl-retitle-btn')).toBeTruthy();
            });
        }
        return fetchMock;
    }

    it('offers the verb only for linked rows whose title differs, and sends keys only', async () => {
        let sent = null;
        await loadMixed({
            '/api/zotlink/retitle': (u, opts) => {
                sent = JSON.parse(opts.body);
                return {
                    ok: true, status: 200,
                    body: new ReadableStream({
                        start(c) {
                            c.enqueue(new TextEncoder().encode(
                                'data: {"zotlink_retitle":{"ok":true,"retitled":1,'
                                + '"batch_id":"BATCH7","results":{"A1":{"ok":true,'
                                + '"status":"retitled"}},"failures":[]}}\n\n'
                                + 'data: [DONE]\n\n'));
                            c.close();
                        },
                    }),
                };
            },
        });
        // A2 already matches; A3 is an imported copy whose bytes Zotero owns.
        expect(document.getElementById('zl-retitle-btn').textContent).toContain('1 title(s)');
        document.getElementById('zl-retitle-btn').click();
        acceptConfirm();
        await vi.waitFor(() => {
            expect(sent, 'no retitle request was sent').toBeTruthy();
        });
        expect(sent.item_keys).toEqual(['A1']);
        expect(sent.confirm).toBe(true);
        // Keys only on the wire: the client must not be able to name a title.
        expect(JSON.stringify(sent)).not.toContain('Full Text PDF');
        await vi.waitFor(() => {
            expect(attachStatus()).toContain('1 title(s) set');
        });
        // The batch id is the ONLY handle on the undo, and the write has
        // already landed — a reload after it must not replace this line.
        expect(attachStatus()).toContain('BATCH7');
        await vi.waitFor(() => {
            expect(document.getElementById('audit-zl-status').textContent)
                .toContain('BATCH7');
        });
    });

    it('is not offered at all when every title already matches', async () => {
        await loadMixed({
            '/api/audit/reports/zotero_attachments': jsonOk({
                count: 1, linked: 1, missing: 0,
                rows: [{item_key: 'A2', parent_key: 'P2', parent_title: 'Two',
                        citation_key: 'b2021', filename: 'b_2021.pdf', title: 'b_2021.pdf',
                        path: '/v/b_2021.pdf', link_mode: 'linked_file',
                        file_owned_by_zotero: false, annotation_count: 0, exists: true}],
            }),
        }, {expectVerb: false});
        expect(document.getElementById('zl-retitle-btn')).toBeNull();
        expect(document.querySelector('[data-action="zl-retitle-dry"]')).toBeNull();
    });

    it('discloses the attachments:-relative links the pairing rung skips', async () => {
        // The scan has counted these since 2026-09-19 and rendered them
        // nowhere until 2026-09-20. A relative link resolves to nothing while
        // Zotero's base directory is unset, so the file looks un-linked on
        // every other surface and nothing said why.
        await loadMixed({
            '/api/audit/summary': jsonOk({ summary: { zotero_relative_links: 4 } }),
        });
        const meta = document.getElementById('audit-report-meta').textContent;
        expect(meta).toContain('4 link(s) are stored');
        expect(meta).toContain('Linked Attachment Base Directory');
    });

    it('writes nothing on [preview] and asks for no confirmation', async () => {
        let sent = null;
        await loadMixed({
            '/api/zotlink/retitle': (u, opts) => {
                sent = JSON.parse(opts.body);
                return {
                    ok: true, status: 200,
                    body: new ReadableStream({
                        start(c) {
                            c.enqueue(new TextEncoder().encode(
                                'data: {"zotlink_retitle":{"ok":true,"dry_run":true,'
                                + '"would_retitle":1,"results":{},"failures":[]}}\n\n'
                                + 'data: [DONE]\n\n'));
                            c.close();
                        },
                    }),
                };
            },
        });
        document.querySelector('[data-action="zl-retitle-dry"]').click();
        await vi.waitFor(() => { expect(sent).toBeTruthy(); });
        expect(sent.dry_run).toBe(true);
        expect(sent.confirm).toBeUndefined();
        expect(document.querySelector('.inline-confirm-strip')).toBeNull();
        await vi.waitFor(() => {
            expect(attachStatus()).toContain('Nothing was written');
        });
    });
});
