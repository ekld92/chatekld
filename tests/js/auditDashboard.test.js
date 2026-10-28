// Pins the audit dashboard (audit.js, 2026-08-13).
//
// Three defect classes, all of which shipped before this test existed:
//
//   1. DENOMINATORLESS COVERAGE — a coverage figure rendered without the
//      population it is a fraction of is the same failure mode as Tag
//      Hygiene's floor-presented-as-a-total. Every coverage tile must carry
//      its own "of N" line, and the Finder floor/platform caveats must render.
//   2. TILES ONLY AFTER THE INVENTORY TAB — `_lastSummary` was populated only
//      on the inventory branch of `_loadReport`, so the dashboard describing
//      the whole library stayed blank for anyone who opened another tab first.
//      It now has its own feed (`/api/audit/summary`).
//   3. A COUNT READ OFF A FIELD THAT DOES NOT EXIST — the first-scan milestone
//      read `status.record_count || status.counts.records`; the status payload
//      has never carried either, so the strip always rendered its countless
//      fallback and its one-shot flag then made that permanent.
//
// `_lastSummary` is MODULE state, so every test re-imports audit.js through
// `load()` after `vi.resetModules()`. Without that, the first test's summary
// leaks into the next one and a test asserting a DIFFERENT payload silently
// checks the previous one — the same module-state trap tagging.js documents.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const SUMMARY = {
    record_count: 6141,
    bib_with_pdf: 4060,
    bib_with_obsidian_note: 1232,
    bib_with_zotero_parent: 6122,
    fully_triangulated: 1100,
    zotero_parents_with_child_note: 900,
    pdfs_unmapped: 1253,
    pdfs_ambiguous: 12,
    pdfs_skipped: 4,
    zotero_matched_by_key: 6122,
    zotero_matched_by_title: 0,
    zotero_title_refused: 3,
    mapping_orphans: 2,
    notes_without_record: 7,
    zotero_items_without_record: 72,
    zotero_error: null,
    tag_coverage: {
        records_total: 6141,
        writable_records: 6100,
        note_present: 1232,
        note_tagged: 300,
        finder_present: 4060,
        finder_tagged: 2000,
        zotero_present: 6122,
        zotero_tagged: 1500,
        any_tagged: 2500,
        none_tagged: 3600,
        bib_keywords_tagged: 10,
        zotero_note_tagged: 5,
        finder_is_floor: true,
        finder_platform: 'darwin',
    },
};

const DOM = `
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
  <span id="audit-paper-index-age"></span>
  <div id="sr-status-announcer" role="status"></div>
`;

const jsonOk = (payload) => ({ ok: true, status: 200, json: async () => payload });

/** Router over the endpoints the audit tab touches; `over` patches responses. */
function routes(over = {}) {
    return vi.fn(async (url) => {
        const u = String(url);
        for (const [frag, payload] of Object.entries(over)) {
            if (u.includes(frag)) return jsonOk(payload);
        }
        if (u.includes('/api/audit/summary')) return jsonOk({ summary: SUMMARY });
        if (u.includes('/api/audit/config')) return jsonOk({ config: {} });
        if (u.includes('/api/audit/scan')) return jsonOk({ started: true });
        if (u.includes('/api/audit/status')) {
            // Deliberately the REAL status shape — no record_count, no counts.
            return jsonOk({
                state: 'done', error: null, has_results: true,
                has_duplicates: false, has_near_duplicates: false,
                started_at: 1, finished_at: 2, messages: [],
            });
        }
        if (u.includes('/api/audit/inventory')) {
            return jsonOk({ summary: SUMMARY, records: [] });
        }
        return jsonOk({ name: 'zotero_no_pdf', rows: [] });
    });
}

function installStorage(seed = {}) {
    const store = new Map(Object.entries(seed));
    vi.stubGlobal('localStorage', {
        getItem: (k) => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => store.set(k, String(v)),
        removeItem: (k) => store.delete(k),
    });
    return store;
}

/** Fresh audit.js, so module-level `_lastSummary` never leaks between tests. */
async function load() {
    vi.resetModules();
    return import('../../static/js/audit.js');
}

const summaryText = () => document.getElementById('audit-summary').textContent;

beforeEach(() => {
    document.body.innerHTML = DOM;
    installStorage();
});

afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
});

describe('audit dashboard — tag coverage tiles', () => {
    it('renders coverage per surface with its own denominator', async () => {
        vi.stubGlobal('fetch', routes());
        const { selectAuditReport } = await load();
        await selectAuditReport('zotero_no_pdf');

        const text = summaryText();
        expect(text).toContain('Reference note');
        expect(text).toContain('Finder (PDF)');
        expect(text).toContain('Zotero item');
        // The denominators — the half that makes the numerators mean anything.
        expect(text).toContain('of 1232');  // notes present
        expect(text).toContain('of 4060');  // PDFs present
        expect(text).toContain('of 6122');  // Zotero items present
    });

    it('states the Finder floor caveat beside the number', async () => {
        vi.stubGlobal('fetch', routes());
        const { selectAuditReport } = await load();
        await selectAuditReport('zotero_no_pdf');
        expect(summaryText()).toContain('Finder counts are a floor');
    });

    it('separates coverage from reconciliation and from the orphan buckets', async () => {
        vi.stubGlobal('fetch', routes());
        const { selectAuditReport } = await load();
        await selectAuditReport('zotero_no_pdf');

        const text = summaryText();
        expect(text).toContain('Reconciliation');
        expect(text).toContain('Tag coverage');
        expect(text).toContain('Outside the join');
        // Orphan counts are shipped AND rendered, not merely computed.
        expect(text).toContain('Notes w/o record');
        expect(text).toContain('72');
    });

    it('shows the tiles without the Inventory tab ever being loaded', async () => {
        // Class 2: the dashboard has its own feed. If it were still a
        // by-product of the inventory branch, this box would be empty here.
        const fetchMock = routes();
        vi.stubGlobal('fetch', fetchMock);
        const { selectAuditReport } = await load();
        await selectAuditReport('zotero_no_pdf');

        expect(document.getElementById('audit-summary').style.display).not.toBe('none');
        expect(summaryText()).toContain('6141');
        const urls = fetchMock.mock.calls.map((c) => String(c[0]));
        expect(urls.some((u) => u.includes('/api/audit/summary'))).toBe(true);
        expect(urls.some((u) => u.includes('/api/audit/inventory'))).toBe(false);
    });
});

describe('audit dashboard — platform and error disclosure', () => {
    it('explains a zero Finder count off macOS instead of implying no tags', async () => {
        const payload = {
            ...SUMMARY,
            tag_coverage: {
                ...SUMMARY.tag_coverage, finder_tagged: 0, finder_platform: 'linux',
            },
        };
        vi.stubGlobal('fetch', routes({ '/api/audit/summary': { summary: payload } }));
        const { selectAuditReport } = await load();
        await selectAuditReport('zotero_no_pdf');
        expect(summaryText()).toContain('macOS only');
    });

    it('still surfaces a Zotero read warning', async () => {
        const payload = { ...SUMMARY, zotero_error: 'DatabaseError: locked' };
        vi.stubGlobal('fetch', routes({ '/api/audit/summary': { summary: payload } }));
        const { selectAuditReport } = await load();
        await selectAuditReport('zotero_no_pdf');
        expect(summaryText()).toContain('DatabaseError: locked');
    });

    it('survives a summary feed that 404s — the report still renders', async () => {
        vi.stubGlobal('fetch', vi.fn(async (url) => {
            const u = String(url);
            if (u.includes('/api/audit/summary')) {
                return { ok: false, status: 404, json: async () => ({ error: 'no scan' }) };
            }
            return jsonOk({ name: 'zotero_no_pdf', rows: [] });
        }));
        const { selectAuditReport } = await load();
        await selectAuditReport('zotero_no_pdf');
        // The tiles are absent, but the tab is usable — a dashboard that fails
        // to load must not take the report down with it.
        expect(document.getElementById('audit-report-meta').textContent).not.toBe('');
    });
});

describe('first-scan milestone', () => {
    it('names the real record count, which no status field ever carried', async () => {
        vi.stubGlobal('fetch', routes());
        const { runAuditScan } = await load();
        await runAuditScan();
        // _startStatusPolling fires its first poll synchronously; let the
        // status → summary → report chain settle.
        await new Promise((r) => setTimeout(r, 0));

        const strip = document.querySelector('.milestone-strip');
        expect(strip).not.toBeNull();
        expect(strip.textContent).toContain('6141 records reconciled');
    });

    it('shows once — the flag survives a second completed scan', async () => {
        installStorage({ audit_first_scan_done: '1' });
        vi.stubGlobal('fetch', routes());
        const { runAuditScan } = await load();
        await runAuditScan();
        await new Promise((r) => setTimeout(r, 0));

        expect(document.querySelector('.milestone-strip')).toBeNull();
    });

    it('keeps the one-shot flag when the summary has no count (2026-08-18)', async () => {
        // On a summary-fetch failure the countless fallback used to render AND
        // burn the flag, so the real count became permanently unshowable. The
        // flag must be set only after a successful count render.
        const store = installStorage();
        vi.stubGlobal('fetch', vi.fn(async (url) => {
            const u = String(url);
            if (u.includes('/api/audit/summary')) {
                return { ok: false, status: 500, json: async () => ({ error: 'boom' }) };
            }
            if (u.includes('/api/audit/scan')) return jsonOk({ started: true });
            if (u.includes('/api/audit/status')) {
                return jsonOk({ state: 'done', error: null, has_results: true,
                                started_at: 1, finished_at: 2, messages: [] });
            }
            if (u.includes('/api/audit/inventory')) {
                return jsonOk({ summary: SUMMARY, records: [] });
            }
            return jsonOk({ name: 'zotero_no_pdf', rows: [] });
        }));
        const { runAuditScan } = await load();
        await runAuditScan();
        await new Promise((r) => setTimeout(r, 0));
        expect(document.querySelector('.milestone-strip')).toBeNull();
        expect(store.get('audit_first_scan_done')).toBeUndefined();
    });
});

describe('audit scan controls (2026-08-18)', () => {
    it('re-arms the Cancel button on a new scan after a successful cancel', async () => {
        vi.stubGlobal('fetch', routes());
        const { runAuditScan } = await load();
        const cancelBtn = document.getElementById('audit-cancel-btn');
        // Simulate the state a successful cancel leaves: disabled + hidden by
        // the poll teardown. Pre-fix, runAuditScan re-showed the button but
        // never cleared `disabled`, so every later scan ran with a dead grey
        // Cancel. (Only `disabled` is asserted after settling: the immediate
        // 'done' status legitimately re-hides the button.)
        cancelBtn.disabled = true;
        cancelBtn.style.display = 'none';
        await runAuditScan();
        await new Promise((r) => setTimeout(r, 0));
        expect(cancelBtn.disabled).toBe(false);
    });
});

describe('per-report 404 keeps the chrome (2026-08-12 adjudicated #4)', () => {
    it('a missing report empties the body but keeps tablist + search bar', async () => {
        vi.stubGlobal('fetch', vi.fn(async (url) => {
            const u = String(url);
            if (u.includes('/api/audit/summary')) return jsonOk({ summary: SUMMARY });
            if (u.includes('/api/audit/reports/near_duplicates')) {
                return { ok: false, status: 404,
                         json: async () => ({ error: 'Near-duplicates were skipped for this scan.' }) };
            }
            return jsonOk({ name: 'zotero_no_pdf', rows: [] });
        }));
        const { selectAuditReport } = await load();
        // A first successful report seeds _lastSummary (a scan exists).
        await selectAuditReport('zotero_no_pdf');
        await selectAuditReport('near_duplicates');
        const tabs = document.getElementById('audit-report-tabs');
        const bar = document.getElementById('audit-search-bar');
        expect(tabs.style.display).not.toBe('none');
        expect(bar.style.display).not.toBe('none');
        const body = document.getElementById('audit-report-body');
        expect(body.textContent).toContain('skipped for this scan');
        expect(body.textContent).toContain('Pick another tab');
    });

    it('with no scan at all, the pre-scan hide still applies', async () => {
        vi.stubGlobal('fetch', vi.fn(async (url) => {
            const u = String(url);
            if (u.includes('/api/audit/summary')) {
                return { ok: false, status: 404, json: async () => ({ error: 'no scan' }) };
            }
            return { ok: false, status: 404,
                     json: async () => ({ error: 'No scan results. Run a scan first.' }) };
        }));
        const { selectAuditReport } = await load();
        await selectAuditReport('near_duplicates');
        expect(document.getElementById('audit-report-tabs').style.display).toBe('none');
    });
});

describe('dashboard failure is visible (2026-08-18)', () => {
    it('a non-404 summary failure renders an unavailable notice', async () => {
        vi.stubGlobal('fetch', vi.fn(async (url) => {
            const u = String(url);
            if (u.includes('/api/audit/summary')) {
                return { ok: false, status: 500, json: async () => ({ error: 'boom' }) };
            }
            return jsonOk({ name: 'zotero_no_pdf', rows: [] });
        }));
        const { selectAuditReport } = await load();
        await selectAuditReport('zotero_no_pdf');
        expect(summaryText()).toContain('unavailable');
    });
});

describe('audit dashboard — scan identity (2026-08-13 review)', () => {
    it('drops the cached tiles when tab re-entry sees a NEWER scan', async () => {
        // A scan can finish outside this page's poll loop (papermind audit
        // scan / MCP). The only place that scan is ever noticed is the status
        // read on tab re-entry — without the identity reset, _ensureSummary
        // kept painting the previous scan's tiles for the whole session.
        let finishedAt = 100;
        let current = SUMMARY;
        vi.stubGlobal('fetch', vi.fn(async (url) => {
            const u = String(url);
            if (u.includes('/api/audit/summary')) return jsonOk({ summary: current });
            if (u.includes('/api/audit/config')) return jsonOk({ config: {} });
            if (u.includes('/api/audit/status')) {
                return jsonOk({
                    state: 'done', error: null, has_results: true,
                    has_duplicates: false, has_near_duplicates: false,
                    started_at: 1, finished_at: finishedAt, messages: [],
                });
            }
            return jsonOk({ name: 'zotero_no_pdf', rows: [] });
        }));
        const { initAuditTab, selectAuditReport } = await load();

        // A non-inventory report is current (the inventory branch carries its
        // own summary; every other branch goes through _ensureSummary's cache).
        await selectAuditReport('zotero_no_pdf');
        expect(summaryText()).toContain('6141');

        // A CLI-driven rescan finishes; the user re-enters the audit tab.
        finishedAt = 200;
        current = { ...SUMMARY, record_count: 9999 };
        await initAuditTab();
        expect(summaryText()).toContain('9999');
    });

    it('keeps the cached tiles when re-entry sees the SAME scan', async () => {
        const fetchMock = vi.fn(async (url) => {
            const u = String(url);
            if (u.includes('/api/audit/summary')) return jsonOk({ summary: SUMMARY });
            if (u.includes('/api/audit/config')) return jsonOk({ config: {} });
            if (u.includes('/api/audit/status')) {
                return jsonOk({
                    state: 'done', error: null, has_results: true,
                    has_duplicates: false, has_near_duplicates: false,
                    started_at: 1, finished_at: 100, messages: [],
                });
            }
            if (u.includes('/api/audit/inventory')) {
                return jsonOk({ summary: SUMMARY, records: [] });
            }
            return jsonOk({ name: 'zotero_no_pdf', rows: [] });
        });
        vi.stubGlobal('fetch', fetchMock);
        const { initAuditTab, selectAuditReport } = await load();

        // Realistic order: tab activation notes the scan identity first and
        // seeds the tiles. Since 2026-09-02 the first report render is
        // deferred to the first activation of the Audit tab (perf plan Phase
        // 3.6), so the seed comes from /api/audit/summary — the dashboard's
        // own feed — rather than as a by-product of the 3.71 MB inventory
        // payload. What this test pins is unchanged: the SAME scan is never
        // re-fetched, so the count below is one seed, not one per entry.
        await initAuditTab();
        await selectAuditReport('zotero_no_pdf');
        await initAuditTab(); // re-entry, same finished_at → cache holds
        const summaryFetches = fetchMock.mock.calls
            .filter((c) => String(c[0]).includes('/api/audit/summary'));
        expect(summaryFetches.length).toBe(1); // seeded once, then cached
        expect(summaryText()).toContain('6141');
    });
});

describe('audit dashboard — paper-label index age (2026-09-23)', () => {
    const ageText = () => document.getElementById('audit-paper-index-age').textContent;

    it('prints the index age from the summary feed', async () => {
        const builtAt = new Date(Date.now() - 12 * 60 * 1000).toISOString();
        vi.stubGlobal('fetch', routes({
            '/api/audit/summary': { summary: SUMMARY, paper_index_built_at: builtAt },
        }));
        const { selectAuditReport } = await load();
        await selectAuditReport('zotero_no_pdf');
        expect(ageText()).toMatch(/Paper labels: index from the scan 12 min ago/);
    });

    it('says there is no index when the field is null', async () => {
        vi.stubGlobal('fetch', routes({
            '/api/audit/summary': { summary: SUMMARY, paper_index_built_at: null },
        }));
        const { selectAuditReport } = await load();
        await selectAuditReport('zotero_no_pdf');
        expect(ageText()).toContain('no index yet');
    });
});
