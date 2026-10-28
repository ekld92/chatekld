// Pins the accession surfaces on the "PDFs Without Zotero" report (audit.js).
//
// Guarded classes:
//   1. THE BULK BOUNDARY — only resolved-identifier tiers (bib_doi / xmp_doi /
//      printed_doi) default CHECKED in the review lists; title_filename is
//      listed UNCHECKED, so bulk always means "an identifier resolved" and
//      never "a title looked right" (the plan's tier table, verbatim).
//   2. WRITE CEREMONY — every mapping write (bulk link, deck link, not-in-bib,
//      undo) goes through confirmInline; undo posts `clear` and is
//      peek-not-pop (a failed undo stays retryable).
//   3. EGRESS CEREMONY — the batch identify is confirm-gated and posts
//      confirm:true; the toggle persists accession_enabled via /api/config.
//   4. PDF TRANSPORT — blob URL via secureFetch, never an /api iframe src
//      (source-ratcheted).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import axe from 'axe-core';
import fs from 'node:fs';
import path from 'node:path';

// audit.js keeps per-snapshot module state (_acc.decided / undo / selections)
// that deliberately SURVIVES re-renders within one scan session — so each
// test must get a fresh module instance (vi.resetModules + dynamic import,
// the tagging.test.js harness pattern), or one test's decisions leak into
// the next test's deck.
let selectAuditReport;
const enc = new TextEncoder();

function jsonOk(payload) {
    return { ok: true, status: 200, json: async () => payload };
}

function sseOk(frames) {
    const payload = frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('')
        + 'data: [DONE]\n\n';
    let sent = false;
    return {
        ok: true,
        status: 200,
        body: {
            getReader: () => ({
                read: async () => sent
                    ? { value: undefined, done: true }
                    : ((sent = true), { value: enc.encode(payload), done: false }),
                releaseLock: () => {},
                cancel: async () => {},
            }),
        },
    };
}

const REPORT = {
    suggested_read_cutoff: 3,
    ambiguous_count: 0,
    confirmed_no_match_count: 0,
    skipped_count: 0,
    rows: [
        { pdf: { abs: '/v/b/a.pdf', rel: 'b/a.pdf', name: 'a.pdf' }, annotations: 5, error: null },
        { pdf: { abs: '/v/b/b.pdf', rel: 'b/b.pdf', name: 'b.pdf' }, annotations: 4, error: null },
        { pdf: { abs: '/v/b/c.pdf', rel: 'b/c.pdf', name: 'c.pdf' }, annotations: 3, error: null },
        { pdf: { abs: '/v/b/d.pdf', rel: 'b/d.pdf', name: 'd.pdf' }, annotations: 2, error: null },
        { pdf: { abs: '/v/b/e.pdf', rel: 'b/e.pdf', name: 'e.pdf' }, annotations: 1, error: null },
        { pdf: { abs: '/v/b/f.pdf', rel: 'b/f.pdf', name: 'f.pdf' }, annotations: 0, error: null },
    ],
};

const ACC_ROWS = {
    enabled: true,
    batch_max: 50,
    resolved_count: 5,
    rows: [
        { pdf: { rel: 'b/a.pdf', name: 'a.pdf' }, sha256: 'a1', resolution: {
            outcome: 'link', tier: 'bib_doi', citation_key: 'myran2025',
            doi: '10.1/a', query_kind: 'doi', evidence: 'meta',
            candidates: [], scores: [], notes: [], from_cache: true } },
        { pdf: { rel: 'b/b.pdf', name: 'b.pdf' }, sha256: 'b1', resolution: {
            outcome: 'candidates', tier: 'xmp_doi', citation_key: null,
            doi: '10.1/b', query_kind: 'doi', evidence: 'meta',
            candidates: [{ source: 'crossref', title: 'New <i>paper</i> B &amp; more:', authors: ['Smith, J'], year: 2024, doi: '10.1/b' }],
            scores: [1.0], notes: [], from_cache: true } },
        { pdf: { rel: 'b/c.pdf', name: 'c.pdf' }, sha256: 'c1', resolution: {
            outcome: 'candidates', tier: 'title_filename', citation_key: null,
            doi: '10.1/c', query_kind: 'title', evidence: 'text',
            candidates: [{ source: 'crossref', title: 'Corroborated paper C', authors: ['Doe, A'], year: 2023, doi: '10.1/c' }],
            scores: [0.93], notes: [], from_cache: true } },
        { pdf: { rel: 'b/d.pdf', name: 'd.pdf' }, sha256: 'd1', resolution: {
            outcome: 'candidates', tier: 'title_only', citation_key: null,
            doi: '10.1/d', query_kind: 'title', evidence: 'text',
            candidates: [{ source: 'europepmc', title: 'Uncorroborated paper D', authors: ['Roe, B'], year: 2020, doi: '10.1/d' }],
            scores: [0.7], notes: ['thesesfr answered HTTP 400'],
            consulted: ['crossref', 'europepmc'],
            queried_at: Date.now() / 1000 - 3 * 86400,
            from_cache: true } },
        { pdf: { rel: 'b/e.pdf', name: 'e.pdf' }, sha256: null, resolution: null },
        { pdf: { rel: 'b/f.pdf', name: 'f.pdf' }, sha256: 'f1', resolution: {
            outcome: 'candidates', tier: 'filename_only', citation_key: null,
            doi: null, query_kind: 'authoryear', evidence: 'filename',
            candidates: [
                { source: 'crossref', title: 'Common Insects of Texas and Surrounding States', authors: ['Abbot, J'], year: 2000, doi: '10.9/x' },
                { source: 'europepmc', title: 'Patent ductus arteriosus', authors: ['Abbot, J'], year: 2001, doi: '10.9/y' },
            ],
            scores: [0.0, 0.0], notes: [], from_cache: true } },
    ],
};

// The cached scan's records — what the manual link picker searches. It
// deliberately includes an entry whose surname is transliterated away from the
// filename (`von_kaenel.pdf` → `vonkanel2019`), the measured class of orphan
// that no ladder rung catches and that the picker exists to reach.
const INVENTORY = [
    { citation_key: 'myran2025', title: 'Alcohol use disorder', first_author: 'Myran, D', year: '2025', has_bib_entry: true },
    { citation_key: 'vonkanel2019', title: 'Sleep and inflammation', first_author: 'von Känel, R', year: '2019', has_bib_entry: true },
    { citation_key: 'eluard1926', title: 'Capitale de la douleur', first_author: 'Éluard, P', year: '1926', has_bib_entry: true },
    { citation_key: 'smith2020', title: 'Cardiac outcomes', first_author: 'Smith, J', year: '2020', has_bib_entry: true },
    { citation_key: 'orphan2024', title: 'Scan-only orphan', first_author: 'Nobody, N', year: '2024', has_bib_entry: false },
    { citation_key: 'unknown2024', title: 'Malformed inventory row', first_author: 'Nobody, N', year: '2024' },
];

const DOM = `
  <div id="audit-summary" style="display:none;"></div>
  <div id="audit-report-tabs" style="display:none;"></div>
  <div id="audit-search-bar" style="display:none;">
    <input id="audit-search-input"><button id="audit-search-clear"></button>
  </div>
  <div id="audit-report-meta"></div>
  <div id="audit-search-results" style="display:none;"></div>
  <div id="audit-report-body"></div>
  <div id="audit-empty-state"></div>
  <div id="sr-status-announcer" role="status"></div>
`;

function acceptConfirm() {
    const strip = document.querySelector('.inline-confirm-strip');
    expect(strip, 'no confirm strip appeared').toBeTruthy();
    const buttons = [...strip.querySelectorAll('button')];
    buttons[buttons.length - 1].click();   // Cancel is first in DOM order
}

function mappingCalls(fetchMock) {
    return fetchMock.mock.calls
        .filter(([url]) => String(url).includes('/api/audit/mapping'))
        .map(([, opts]) => JSON.parse(opts.body));
}

async function load(fetchMock, view = 'table') {
    const store = new Map([['audit_accession_view', view]]);
    vi.stubGlobal('localStorage', {
        getItem: (k) => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => store.set(k, String(v)),
        removeItem: (k) => store.delete(k),
    });
    vi.stubGlobal('fetch', fetchMock);
    vi.resetModules();
    ({ selectAuditReport } = await import('../../static/js/audit.js'));
    await selectAuditReport('read_unzoterod');
    await vi.waitFor(() => {
        expect(document.getElementById('audit-acc-panel')).toBeTruthy();
    });
    fetchMock.mockClear();
}

function mkFetch(overrides = {}) {
    return vi.fn(async (url, opts) => {
        const u = String(url);
        for (const [needle, resp] of Object.entries(overrides)) {
            if (u.includes(needle)) {
                return typeof resp === 'function' ? resp(u, opts) : resp;
            }
        }
        if (u.includes('/api/audit/reports/read_unzoterod')) return jsonOk(REPORT);
        if (u.includes('/api/accession/rows')) return jsonOk(ACC_ROWS);
        if (u.includes('/api/audit/inventory')) return jsonOk({ summary: {}, records: INVENTORY });
        return jsonOk({ ok: true });
    });
}

describe('accession review lists (table view)', () => {
    let fetchMock;
    beforeEach(async () => {
        document.body.innerHTML = DOM;
        fetchMock = mkFetch();
        await load(fetchMock, 'table');
    });
    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it('default-checks identifier tiers only — title_filename is listed unchecked', () => {
        const linkCb = document.querySelector('[data-acc-link-sel="b/a.pdf"]');
        expect(linkCb, 'bib_doi link row missing').toBeTruthy();
        expect(linkCb.checked).toBe(true);

        const xmpCb = document.querySelector('[data-acc-acc-sel="b/b.pdf"]');
        expect(xmpCb, 'xmp_doi accession row missing').toBeTruthy();
        expect(xmpCb.checked).toBe(true);

        const titleCb = document.querySelector('[data-acc-acc-sel="b/c.pdf"]');
        expect(titleCb, 'title_filename row must be LISTED').toBeTruthy();
        expect(titleCb.checked, 'title_filename must default UNCHECKED').toBe(false);

        // title_only never reaches the lists at all.
        expect(document.querySelector('[data-acc-acc-sel="b/d.pdf"]')).toBeNull();
    });

    it('bulk link is confirm-gated and posts one mapping write per selection', async () => {
        document.getElementById('audit-acc-link-btn').click();
        expect(mappingCalls(fetchMock)).toEqual([]);   // ceremony first

        acceptConfirm();
        await vi.waitFor(() => expect(mappingCalls(fetchMock).length).toBe(1));
        // The provenance annotation (2026-08-19): accession-made links say so,
        // so the next scan's pairings board can tell them from hand curation.
        expect(mappingCalls(fetchMock)).toEqual([
            { pdf: 'b/a.pdf', citation_key: 'myran2025', source: 'accession', confirm: true },
        ]);
    });

    it('copy identifiers joins the selected DOIs with newlines for Zotero', async () => {
        const writeText = vi.fn(async () => {});
        vi.stubGlobal('navigator', { clipboard: { writeText } });
        // Select the title_filename row too — an explicit tick is allowed.
        const titleCb = document.querySelector('[data-acc-acc-sel="b/c.pdf"]');
        titleCb.checked = true;
        titleCb.dispatchEvent(new Event('change', { bubbles: true }));
        document.getElementById('audit-acc-copy-btn').click();
        await vi.waitFor(() => expect(writeText).toHaveBeenCalled());
        expect(writeText.mock.calls[0][0].split('\n').sort())
            .toEqual(['10.1/b', '10.1/c']);
    });

    it('the toggle persists accession_enabled through /api/config', async () => {
        const toggle = document.getElementById('audit-acc-enabled');
        expect(toggle.checked).toBe(true);
        toggle.checked = false;
        toggle.dispatchEvent(new Event('change', { bubbles: true }));
        await vi.waitFor(() => {
            const call = fetchMock.mock.calls.find(
                ([url]) => String(url).includes('/api/config'));
            expect(call).toBeTruthy();
            expect(JSON.parse(call[1].body)).toEqual({ accession_enabled: false });
        });
    });

    it('the batch button is confirm-gated and posts confirm:true', async () => {
        document.getElementById('audit-acc-batch').click();
        expect(fetchMock.mock.calls.filter(
            ([u]) => String(u).includes('/api/accession/batch')).length).toBe(0);
        acceptConfirm();
        await vi.waitFor(() => {
            const call = fetchMock.mock.calls.find(
                ([u]) => String(u).includes('/api/accession/batch'));
            expect(call).toBeTruthy();
            expect(JSON.parse(call[1].body))
                .toEqual({ pdfs: ['b/e.pdf'], confirm: true });
        });
        // The run's final status must SURVIVE the completion re-render — the
        // panel is rebuilt in the finally, and a status line living only in
        // the replaced DOM vanished with it (fixed pre-ship). The mock is not
        // a real SSE stream, so this run ends in the error path — which
        // re-renders the same way; a non-empty line proves persistence.
        await vi.waitFor(() => {
            expect(document.getElementById('audit-acc-status').textContent)
                .not.toBe('');
        });
    });
});

describe('accession review deck', () => {
    let fetchMock;
    beforeEach(async () => {
        document.body.innerHTML = DOM;
        fetchMock = mkFetch();
        if (!URL.createObjectURL) URL.createObjectURL = () => {};
        if (!URL.revokeObjectURL) URL.revokeObjectURL = () => {};
        vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:mock-acc-pdf');
        vi.spyOn(URL, 'revokeObjectURL');
        await load(fetchMock, 'deck');
    });
    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it('the deck reviews only sub-bulk rows (title_only first, then unresolved)', () => {
        const card = document.getElementById('audit-acc-card');
        expect(card.textContent).toContain('d.pdf');       // title_only
        expect(card.textContent).toContain('Uncorroborated paper D');
        // Bulk-tier rows never enter the deck.
        expect(card.textContent).not.toContain('a.pdf');
    });

    it('not-in-bib is confirm-gated and posts no_match', async () => {
        document.querySelector('[data-acc-deck="no-match"]').click();
        expect(mappingCalls(fetchMock)).toEqual([]);
        acceptConfirm();
        await vi.waitFor(() => expect(mappingCalls(fetchMock)).toEqual([
            { pdf: 'b/d.pdf', no_match: true, confirm: true },
        ]));
        // The decided card leaves the deck: e.pdf is next.
        await vi.waitFor(() => {
            expect(document.getElementById('audit-acc-card').textContent)
                .toContain('e.pdf');
        });
    });

    it('undo is confirm-gated, posts clear, and is peek-not-pop on failure', async () => {
        document.querySelector('[data-acc-deck="no-match"]').click();
        acceptConfirm();
        await vi.waitFor(() => expect(mappingCalls(fetchMock).length).toBe(1));
        // Wait for the WRITE to complete (card advanced ⇒ busy cleared) —
        // asserting on the fetch call alone races the async tail, and a
        // busy-gated undo click would silently no-op.
        await vi.waitFor(() => {
            expect(document.getElementById('audit-acc-card').textContent)
                .toContain('e.pdf');
        });

        // First undo attempt FAILS server-side — the entry must survive.
        fetchMock.mockClear();
        fetchMock.mockImplementationOnce(async () => (
            { ok: false, status: 500, json: async () => ({ error: 'boom' }) }));
        document.querySelector('[data-acc-deck="undo"]').click();
        acceptConfirm();
        await vi.waitFor(() => expect(mappingCalls(fetchMock).length).toBe(1));
        expect(mappingCalls(fetchMock)).toEqual([{ pdf: 'b/d.pdf', clear: true, confirm: true }]);
        // Wait for the failure to LAND (error text and the busy clear share
        // one synchronous continuation) before retrying.
        await vi.waitFor(() => {
            expect(document.getElementById('audit-acc-result').textContent)
                .toContain('boom');
        });

        // Retry succeeds with the SAME target (peek, not pop).
        fetchMock.mockClear();
        const undoBtn = document.querySelector('[data-acc-deck="undo"]');
        expect(undoBtn.disabled).toBe(false);
        undoBtn.click();
        acceptConfirm();
        await vi.waitFor(() => expect(mappingCalls(fetchMock)).toEqual([
            { pdf: 'b/d.pdf', clear: true, confirm: true },
        ]));
    });

    it('identify posts the current card and merges the result', async () => {
        // Move to the unresolved card (e.pdf).
        document.querySelector('[data-acc-deck="skip"]').click();
        await vi.waitFor(() => {
            expect(document.getElementById('audit-acc-card').textContent)
                .toContain('e.pdf');
        });
        fetchMock.mockImplementation(async (url) => {
            if (String(url).includes('/api/accession/identify')) {
                return sseOk([{ row: {
                    pdf: { rel: 'b/e.pdf', name: 'e.pdf' }, sha256: 'e1',
                    resolution: {
                        outcome: 'candidates', tier: 'title_only',
                        citation_key: null, doi: '10.1/e',
                        query_kind: 'title', evidence: 'text',
                        candidates: [{ source: 'crossref', title: 'Found E', authors: [], year: 2021, doi: '10.1/e' }],
                        scores: [0.8], notes: [],
                    },
                } }]);
            }
            return jsonOk({ ok: true });
        });
        document.querySelector('[data-acc-deck="identify"]').click();
        await vi.waitFor(() => {
            const call = fetchMock.mock.calls.find(
                ([u]) => String(u).includes('/api/accession/identify'));
            expect(call).toBeTruthy();
            expect(JSON.parse(call[1].body)).toEqual({ pdf: 'b/e.pdf' });
        });
        await vi.waitFor(() => {
            expect(document.getElementById('audit-acc-card').textContent)
                .toContain('Found E');
        });
    });

    it('the PDF panel is a header-fetched blob, never an /api iframe src', async () => {
        fetchMock.mockImplementation(async (url) => {
            if (String(url).includes('/api/audit/pdf')) {
                return { ok: true, status: 200, blob: async () => new Blob(['x']) };
            }
            return jsonOk({ ok: true });
        });
        document.querySelector('[data-acc-deck="pdf"]').click();
        await vi.waitFor(() => {
            const frame = document.querySelector('#audit-acc-pdf iframe');
            expect(frame).toBeTruthy();
            expect(frame.src).toContain('blob:');
        });
        const call = fetchMock.mock.calls.find(
            ([u]) => String(u).includes('/api/audit/pdf'));
        expect(call).toBeTruthy();
        expect(call[1].headers['X-Requested-With']).toBe('ChatEKLD');
    });
});

describe('batch cancel affordance', () => {
    let fetchMock;
    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it('a running batch offers [cancel], which aborts the SSE fetch and keeps partials', async () => {
        // The batch fetch hangs until its AbortController fires — the shape
        // of a long identification run.
        fetchMock = mkFetch({
            '/api/accession/batch': (_u, opts) => new Promise((_resolve, reject) => {
                opts.signal.addEventListener('abort', () =>
                    reject(new DOMException('aborted', 'AbortError')));
            }),
        });
        document.body.innerHTML = DOM;
        await load(fetchMock, 'table');

        document.getElementById('audit-acc-batch').click();
        acceptConfirm();
        // The panel repaints with the cancel affordance while in flight.
        let cancelBtn;
        await vi.waitFor(() => {
            cancelBtn = document.getElementById('audit-acc-batch-cancel');
            expect(cancelBtn).toBeTruthy();
        });
        cancelBtn.click();
        await vi.waitFor(() => {
            expect(document.getElementById('audit-acc-status').textContent)
                .toContain('cancelled');
        });
        // Not an error tone, and the affordance is gone with the run.
        expect(document.getElementById('audit-acc-status').classList
            .contains('is-error')).toBe(false);
        expect(document.getElementById('audit-acc-batch-cancel')).toBeNull();
    });

    it('renders a terminal failure tally persistently until dismissed', async () => {
        vi.stubGlobal('CSS', { escape: (value) => String(value) });
        const failed = {
            pdf: { rel: 'b/e.pdf', name: 'e.pdf' },
            sha256: null, resolution: null, error: 'catalog unavailable',
        };
        fetchMock = mkFetch({
            '/api/accession/batch': sseOk([
                { row: failed },
                {
                    error: 'Aborting after 5 consecutive failures — check the log.',
                    accession: {
                        total: 1, done: 1, outcomes: { error: 1 },
                        cancelled: false, failed_early: true,
                    },
                },
            ]),
        });
        document.body.innerHTML = DOM;
        await load(fetchMock, 'table');

        document.getElementById('audit-acc-batch').click();
        acceptConfirm();
        await vi.waitFor(() => expect(document.getElementById('audit-acc-status').textContent)
            .toContain('Identified 1 of 1 PDF(s) — 1 error'));
        const status = document.getElementById('audit-acc-status');
        expect(status.textContent).toContain('Stopped early');
        expect(status.classList.contains('is-error')).toBe(true);
        expect(document.querySelector('[data-acc-cell="b/e.pdf"]').textContent)
            .toContain('lookup failed: catalog unavailable');

        const dismiss = document.getElementById('audit-acc-status-dismiss');
        expect(dismiss).toBeTruthy();
        dismiss.click();
        expect(status.textContent).toBe('');
        expect(document.getElementById('audit-acc-status-dismiss')).toBeNull();
        expect(document.activeElement.id).toBe('audit-acc-batch');
    });
});

describe('clipboard fallback', () => {
    let fetchMock;
    beforeEach(async () => {
        document.body.innerHTML = DOM;
        fetchMock = mkFetch();
        await load(fetchMock, 'table');
    });
    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it('a refused clipboard falls back to a selectable textarea (never a dead end)', async () => {
        // No navigator.clipboard at all — the sandboxed-webview shape the
        // tagging _pickCopy fallback documents.
        vi.stubGlobal('navigator', {});
        document.getElementById('audit-acc-copy-btn').click();
        let box;
        await vi.waitFor(() => {
            box = document.getElementById('audit-acc-copy-export');
            expect(box).toBeTruthy();
        });
        expect(box.value.split('\n')).toContain('10.1/b');
        expect(document.getElementById('audit-acc-status').textContent)
            .toContain('copy them manually');
    });
});

describe('source ratchets', () => {
    const src = fs.readFileSync(
        path.join(__dirname, '../../static/js/audit.js'), 'utf8');

    it('the with-pdf grid names the accession deck ids, not only the ambig ones', () => {
        // The accession deck reuses .audit-ambig-deck but its children carry
        // #audit-acc-* ids; without their own grid-area selectors they
        // auto-place (progress/result side by side) instead of filling the
        // named areas when the PDF panel splits the deck.
        const css = fs.readFileSync(
            path.join(__dirname, '../../static/css/styles.css'), 'utf8');
        for (const id of ['#audit-acc-progress', '#audit-acc-result',
                          '#audit-acc-card', '#audit-acc-pdf']) {
            expect(css, `styles.css must grid-assign ${id}`)
                .toContain(`.audit-ambig-deck.with-pdf > ${id}`);
        }
    });

    it('no /api URL is ever assigned to an element .src directly', () => {
        // A property assignment like `frame.src = '/api/...'` would 403 (the
        // origin guard needs the header); blob URLs are the only legal src.
        expect(src).not.toMatch(/\.src\s*=\s*["'`]\/api\//);
    });

    it('every accession endpoint call goes through secureFetch/consumeSSE', () => {
        const lines = src.split('\n').filter(l => l.includes("'/api/accession"));
        expect(lines.length).toBeGreaterThanOrEqual(3);
        for (const line of lines) {
            expect(
                line.includes('secureFetch(') || line.includes('consumeSSE('),
                `bare fetch to an accession endpoint: ${line.trim()}`,
            ).toBe(true);
        }
    });
});

describe('provenance & title honesty (2026-08-19 review)', () => {
    let fetchMock;
    beforeEach(async () => {
        document.body.innerHTML = DOM;
        fetchMock = mkFetch();
        await load(fetchMock, 'table');
    });
    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    function cellFor(rel) {
        return document.querySelector(`[data-acc-cell="${rel}"]`);
    }

    it('a filename_only row never leads with a candidate title — it states the count', () => {
        const cell = cellFor('b/f.pdf');
        expect(cell.textContent).toContain('2 same-surname candidate(s)');
        expect(cell.textContent).not.toContain('Common Insects');
        expect(cell.textContent).toContain('surname search');
    });

    it('a title_only row is marked as a guess and carries its provenance', () => {
        const cell = cellFor('b/d.pdf');
        expect(cell.textContent).toContain('maybe:');
        expect(cell.textContent).toContain('title guess — verify');
        expect(cell.textContent).toContain('via Europe PMC');
        expect(cell.textContent).toContain('match 0.70');
        expect(cell.textContent).toContain('cached 3 d ago');
        // The consulted set rides the tier line's tooltip.
        const tier = cell.querySelector('span.muted[title]');
        expect(tier, 'tier line must carry the consulted tooltip').toBeTruthy();
        expect(tier.getAttribute('title')).toBe('asked Crossref, Europe PMC');
    });

    it('the panel egress label names the SERVER-configured sources', async () => {
        document.body.innerHTML = DOM;
        const narrowed = { ...ACC_ROWS, sources: ['crossref'], contact_email_set: true };
        fetchMock = mkFetch({ '/api/accession/rows': jsonOk(narrowed) });
        await load(fetchMock, 'table');
        const panel = document.getElementById('audit-acc-panel');
        // Sentence-cased since the 2026-08-19 UX pass: the source list moved
        // into the collapsed <details>, where it now OPENS the paragraph.
        expect(panel.textContent).toContain('Looks orphans up in Crossref (all keyless)');
        expect(panel.textContent).not.toContain('arXiv');
        expect(panel.textContent).toContain('contact email rides the Crossref requests');
        expect(panel.textContent).toContain('bibliography is always checked first');
    });

    it('a degraded resolution shows its source notes, never a clean face', () => {
        const badge = cellFor('b/d.pdf').querySelector('.audit-error-tag');
        expect(badge, 'notes badge missing').toBeTruthy();
        expect(badge.textContent).toContain('1 source note');
        expect(badge.getAttribute('title')).toContain('thesesfr answered HTTP 400');
    });

    it('raw catalog markup/entities are cleaned for display', () => {
        // The xmp_doi row's candidate title carries JATS <i> markup, an
        // &amp; entity and a trailing subtitle colon — the Accessions list
        // must render it cleaned (and never as literal markup).
        const body = document.getElementById('audit-report-body');
        expect(body.textContent).toContain('New paper B & more');
        expect(body.textContent).not.toContain('<i>');
        expect(body.textContent).not.toContain('&amp; more');
        expect(body.textContent).not.toContain('B & more:');
    });

    it('the default-check rule is stated in the DOM, not only in a comment', () => {
        const rules = document.querySelectorAll('.audit-acc-rule');
        expect(rules.length).toBeGreaterThanOrEqual(2);   // both lists
        expect(rules[0].textContent).toContain('pre-checked');
        expect(rules[0].textContent).toContain('unchecked');
    });

    it('the Re-links list shows the matched title so a link can be verified by eye', () => {
        // a.pdf is an offline bib_doi link with no fetched record — the cell
        // must say so rather than sit empty.
        const body = document.getElementById('audit-report-body');
        expect(body.textContent).toContain('offline DOI match');
    });

    it('a decided table row offers [undo], which posts clear and restores the pre-check', async () => {
        // Bulk-link a.pdf (default-checked bib_doi row).
        document.getElementById('audit-acc-link-btn').click();
        acceptConfirm();
        await vi.waitFor(() => expect(mappingCalls(fetchMock).length).toBe(1));
        let undoBtn;
        await vi.waitFor(() => {
            undoBtn = document.querySelector('[data-acc-verb="undo-one"][data-rel="b/a.pdf"]');
            expect(undoBtn, 'decided cell must offer [undo]').toBeTruthy();
        });
        fetchMock.mockClear();
        undoBtn.click();
        acceptConfirm();
        await vi.waitFor(() => expect(mappingCalls(fetchMock)).toEqual([
            { pdf: 'b/a.pdf', clear: true, confirm: true },
        ]));
        // The identifier-grade row re-enters its review list CHECKED.
        await vi.waitFor(() => {
            const cb = document.querySelector('[data-acc-link-sel="b/a.pdf"]');
            expect(cb, 'undone row must re-enter the Re-links list').toBeTruthy();
            expect(cb.checked).toBe(true);
        });
    });

    it('with the feature disabled, an unresolved cell explains itself instead of a dead dash', async () => {
        document.body.innerHTML = DOM;
        const disabled = {
            ...ACC_ROWS,
            enabled: false,
            rows: ACC_ROWS.rows.map(r => ({ ...r })),
        };
        fetchMock = mkFetch({ '/api/accession/rows': jsonOk(disabled) });
        await load(fetchMock, 'table');
        const cell = document.querySelector('[data-acc-cell="b/e.pdf"]');
        expect(cell.textContent).toContain('enable “Identify sources online”');
    });

    it('an excluded rematch key is never pre-checked and Link selected skips it', async () => {
        // The trap: unmap writes exclude[rel]; identify still proposes the
        // same key; bulk Link used to post citation_key and lift the exclude.
        document.body.innerHTML = DOM;
        const excludedRows = {
            ...ACC_ROWS,
            rows: ACC_ROWS.rows.map((r) => {
                if (r.pdf.rel !== 'b/a.pdf') return { ...r };
                return {
                    ...r,
                    excluded_keys: ['myran2025'],
                    resolution: { ...r.resolution },
                };
            }),
        };
        fetchMock = mkFetch({ '/api/accession/rows': jsonOk(excludedRows) });
        await load(fetchMock, 'table');

        const cb = document.querySelector('[data-acc-link-sel="b/a.pdf"]');
        expect(cb, 'excluded re-link must still be listed').toBeTruthy();
        expect(cb.checked, 'must not seed into Link selected').toBe(false);
        expect(cb.disabled, 'checkbox must stay inert without ack').toBe(true);
        expect(document.body.textContent).toMatch(/unmapped/i);
        const linkBtn = document.getElementById('audit-acc-link-btn');
        expect(linkBtn.disabled || /Link selected \(0\)/.test(linkBtn.textContent),
            'bulk Link must not count the excluded row').toBe(true);

        // Explicit rematch is the only path that may send accept_excluded.
        const anyway = document.querySelector(
            '[data-acc-verb="link-anyway"][data-rel="b/a.pdf"]');
        expect(anyway).toBeTruthy();
        anyway.click();
        acceptConfirm();
        await vi.waitFor(() => expect(mappingCalls(fetchMock).length).toBe(1));
        expect(mappingCalls(fetchMock)[0]).toEqual({
            pdf: 'b/a.pdf',
            citation_key: 'myran2025',
            source: 'accession',
            confirm: true,
            accept_excluded: true,
        });
    });

    it('bulk Link selected never sends accept_excluded (source ratchet)', () => {
        const src = fs.readFileSync(
            path.join(__dirname, '../../static/js/audit.js'), 'utf8');
        const start = src.indexOf('async function _accLinkSelected');
        const end = src.indexOf('\nasync function ', start + 1);
        const fn = src.slice(start, end > start ? end : start + 2000);
        // The POST body must stay accession-only — never the rematch ack.
        expect(fn).toMatch(
            /body:\s*JSON\.stringify\(\{\s*pdf:\s*rel,\s*citation_key:\s*res\.citation_key,\s*source:\s*'accession',\s*confirm:\s*true,\s*\}\)/);
        expect(fn).not.toMatch(/accept_excluded:\s*true/);
    });
});

// ---------------------------------------------------------------------------
// UX pass (2026-08-19): hierarchy over prose, and focus that survives a write.
//
// DEFECT CLASSES pinned here:
//   1. The evidence GRADE — the thing the bulk boundary is drawn on — was one
//      element of a joined muted run ("title guess — verify · via Europe PMC ·
//      match 0.70 · cached 3 d ago"), so it carried no more weight than the
//      cache age. It is now a chip, toned by whether an IDENTIFIER resolved
//      the row.
//   2. The egress statement was five sentences in one paragraph, re-read on
//      every visit. The risk-bounding sentence stays on the surface; the rest
//      collapses behind a <details> — collapsed by DEFAULT, or the pass buys
//      nothing.
//   3. Every accession write ends in a full body re-render, which destroyed
//      the control the user had just activated and dropped focus to <body> —
//      so a keyboard user's next Tab restarted from the top of the page after
//      every link/undo.
describe('UX pass — evidence chips, collapsed disclosure, focus survival', () => {
    let fetchMock;
    beforeEach(async () => {
        document.body.innerHTML = DOM;
        fetchMock = mkFetch();
        await load(fetchMock, 'table');
    });
    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it('the evidence grade is a chip, toned by whether an identifier resolved it', () => {
        const idChip = document.querySelector('[data-acc-cell="b/a.pdf"] .audit-chip');
        expect(idChip, 'the bib_doi row must carry an evidence chip').toBeTruthy();
        expect(idChip.className).toContain('chip-id');
        expect(idChip.textContent).toContain('DOI — in your bib');

        const weakChip = document.querySelector('[data-acc-cell="b/d.pdf"] .audit-chip');
        expect(weakChip, 'the title_only row must carry an evidence chip').toBeTruthy();
        // A title guess must NOT wear the identifier tone — that tone is the
        // per-row rendering of the bulk-select boundary.
        expect(weakChip.className).toContain('chip-weak');
        expect(weakChip.className).not.toContain('chip-id');
        expect(weakChip.textContent).toContain('title guess — verify');
        // The provenance detail stays beside it, not inside it.
        expect(weakChip.textContent).not.toContain('via Europe PMC');
    });

    it('the egress disclosure is COLLAPSED by default, risk sentence on the surface', () => {
        const details = document.querySelector('.audit-acc-disclosure');
        expect(details, 'the egress detail must be a disclosure').toBeTruthy();
        expect(details.tagName).toBe('DETAILS');
        expect(details.open, 'it must start collapsed').toBe(false);
        expect(details.querySelector('summary')).toBeTruthy();
        // The sentence that bounds the risk is never hidden behind the click.
        const surfaced = document.querySelector('.audit-acc-egress').textContent;
        expect(surfaced).toContain('never the PDF or its text');
    });

    it('focus lands back in the row after a table-view undo, not on <body>', async () => {
        document.getElementById('audit-acc-link-btn').click();
        acceptConfirm();
        let undoBtn;
        await vi.waitFor(() => {
            undoBtn = document.querySelector('[data-acc-verb="undo-one"][data-rel="b/a.pdf"]');
            expect(undoBtn).toBeTruthy();
        });
        undoBtn.click();
        acceptConfirm();
        await vi.waitFor(() => {
            expect(document.querySelector('[data-acc-verb="undo-one"][data-rel="b/a.pdf"]'))
                .toBeNull();   // the write landed and the body re-rendered
        });
        const active = document.activeElement;
        expect(active, 'focus must not fall to <body>').toBeTruthy();
        expect(active.tagName).not.toBe('BODY');
        expect(active.getAttribute('data-rel')).toBe('b/a.pdf');
    });

    it('focus survives the bulk link too (its own button may be gone)', async () => {
        const linkBtn = document.getElementById('audit-acc-link-btn');
        linkBtn.click();
        acceptConfirm();
        await vi.waitFor(() => {
            expect(document.querySelector('[data-acc-verb="undo-one"][data-rel="b/a.pdf"]'))
                .toBeTruthy();
        });
        const active = document.activeElement;
        expect(active.tagName).not.toBe('BODY');
        // Either the (now empty) list's button survived, or the panel's
        // primary action took the focus — never nothing.
        expect(['audit-acc-link-btn', 'audit-acc-batch']).toContain(active.id);
    });
});

// Sort/filter over the accession surfaces (2026-08-19). Two things are
// specific to this report and cannot be pinned generically:
//
//   * the bulk lists' default-check pass (`_acc.seeded`) runs ONCE per
//     snapshot, and a user's untick must outlive any view change. Because the
//     shared primitive hides/moves nodes instead of re-rendering, seeding is
//     never re-entered at all — this pins that it stays that way.
//   * the accession body repaints on EVERY write, so a sort the user set has
//     to be restored afterwards or it is silently undone by the very action
//     they took while sorted.
describe('accession table — sort, filter and per-row state', () => {
    let fetchMock;

    // The shared controls only install on a table worth narrowing (>= 8
    // rows), so this describe uses a wider fixture than the others.
    const MANY = ['aaa', 'zzz', 'elu', 'mmm', 'bbb', 'ccc', 'ddd', 'eee',
        'fff', 'ggg'];
    const bigReport = {
        ...REPORT,
        rows: MANY.map((n, i) => ({
            pdf: { abs: `/v/b/${n}.pdf`, rel: `b/${n}.pdf`, name: `${n}.pdf` },
            annotations: 10 - i, error: null,
        })),
    };
    const bigRows = {
        ...ACC_ROWS,
        rows: MANY.map((n, i) => ({
            pdf: { rel: `b/${n}.pdf`, name: `${n}.pdf` }, sha256: n,
            resolution: {
                outcome: 'link', tier: 'bib_doi',
                citation_key: n === 'elu' ? 'Éluard2010' : `${n}${2000 + i}`,
                doi: `10.1/${n}`, query_kind: 'doi', evidence: 'meta',
                candidates: [], scores: [], notes: [], from_cache: true,
            },
        })),
    };

    beforeEach(async () => {
        document.body.innerHTML = DOM;
        fetchMock = mkFetch({
            '/api/audit/reports/read_unzoterod': jsonOk(bigReport),
            '/api/accession/rows': jsonOk(bigRows),
        });
        await load(fetchMock, 'table');
    });
    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    const rankedTable = () => [...document.querySelectorAll('#audit-report-body table')]
        .find((t) => t.querySelector('[data-acc-cell]'));
    const paths = () => [...rankedTable().querySelectorAll('tbody tr')]
        .filter((tr) => !tr.hidden && !tr.classList.contains('table-filter-empty'))
        .map((tr) => tr.cells[1].textContent);

    it('AN UNTICKED BULK ROW STAYS UNTICKED THROUGH A SORT (no re-seed)', () => {
        const cb = document.querySelector('[data-acc-link-sel="b/zzz.pdf"]');
        expect(cb.checked, 'a bib_doi row seeds checked').toBe(true);
        cb.checked = false;
        cb.dispatchEvent(new Event('change', { bubbles: true }));

        const linkTable = document.querySelector('#audit-acc-review table');
        const sortBtn = [...linkTable.querySelectorAll('th .th-sort')]
            .find((b) => b.textContent.includes('PDF'));
        expect(sortBtn, 'the Re-links list must be sortable').toBeTruthy();
        sortBtn.click();

        // The seeding pass would re-add it; it must not have run.
        expect(document.querySelector('[data-acc-link-sel="b/zzz.pdf"]').checked)
            .toBe(false);
        expect(document.getElementById('audit-acc-link-btn').textContent)
            .toContain('(9)');   // 10 rows, one deliberately unticked
    });

    it('the ranked table sorts by path, accents folded', () => {
        const sortBtn = [...rankedTable().querySelectorAll('th .th-sort')]
            .find((b) => b.textContent.includes('Path'));
        sortBtn.click();
        expect(paths()[0]).toBe('b/aaa.pdf');
        expect(paths()[paths().length - 1]).toBe('b/zzz.pdf');
    });

    it('filtering the ranked table narrows it and discloses the count', async () => {
        const input = rankedTable().previousElementSibling
            .querySelector('input.table-filter');
        input.value = 'mmm';
        input.dispatchEvent(new Event('input'));
        await vi.waitFor(() => expect(paths()).toEqual(['b/mmm.pdf']));
        expect(rankedTable().previousElementSibling
            .querySelector('.table-filter-count').textContent)
            .toBe('1 of 10 rows');
    });

    it('A SORT SURVIVES THE WRITE THAT REPAINTS THE BODY', async () => {
        const sortBtn = [...rankedTable().querySelectorAll('th .th-sort')]
            .find((b) => b.textContent.includes('Path'));
        sortBtn.click();
        sortBtn.click();   // descending — the opposite of the served order
        expect(paths()[0]).toBe('b/zzz.pdf');

        // A per-row link write: the whole body re-renders in its finally.
        const linkBtn = document.querySelector('[data-acc-verb="link"][data-rel="b/aaa.pdf"]');
        linkBtn.click();
        acceptConfirm();
        await vi.waitFor(() => {
            expect(document.querySelector('[data-acc-verb="undo-one"][data-rel="b/aaa.pdf"]'))
                .toBeTruthy();
        });
        expect(paths()[0], 'the sort must be restored after the repaint')
            .toBe('b/zzz.pdf');
    });
});

// ---------------------------------------------------------------------------
// The manual link picker (2026-08-19) — closing the registered deferral
// "the deck has no 'assign to an arbitrary key' verb".
//
// Guarded classes:
//   1. PROVENANCE HONESTY — a human-picked key must NOT be annotated
//      `source: "accession"`. The pairings board reads that label to say who
//      made a link; crediting the resolver for hand curation makes the column
//      lie at the next scan, permanently and invisibly.
//   2. CEREMONY + REVERSIBILITY — same confirmInline as every other mapping
//      write, and the same `clear` undo bookkeeping afterwards.
//   3. KEYBOARD CONTRACT — arrows + Enter, Escape closes and RESTORES focus
//      to the control that opened it, and the picker's keys never leak to the
//      deck underneath (typing "smith" would otherwise drive S = skip).
describe('manual bibliography link picker', () => {
    let fetchMock;

    async function openFromDeckAndType(text) {
        const btn = document.querySelector('[data-acc-deck="link-to"]');
        expect(btn, 'the deck card must offer [link to…]').toBeTruthy();
        btn.click();
        const input = await vi.waitFor(() => {
            const el = document.querySelector('.acc-keypicker-input');
            expect(el).toBeTruthy();
            return el;
        });
        await vi.waitFor(() => {
            expect(document.querySelector('.acc-keypicker-count').textContent)
                .toContain('Type to search');
        });
        input.value = text;
        input.dispatchEvent(new Event('input'));
        return { btn, input };
    }

    const options = () => [...document.querySelectorAll('.acc-keypicker-opt')]
        .map((li) => li.querySelector('.acc-keypicker-key').textContent);

    beforeEach(async () => {
        document.body.innerHTML = DOM;
        fetchMock = mkFetch();
        await load(fetchMock, 'deck');
    });
    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it('opens from the deck key L, searches, and narrows accent-insensitively', async () => {
        // Drive the real keyboard verb, not the button.
        document.getElementById('audit-acc-deck')
            .dispatchEvent(new KeyboardEvent('keydown', { key: 'l', bubbles: true }));
        const input = await vi.waitFor(() => {
            const el = document.querySelector('.acc-keypicker-input');
            expect(el).toBeTruthy();
            return el;
        });
        await vi.waitFor(() => {
            expect(document.querySelector('.acc-keypicker-count').textContent)
                .toContain('Type to search');
        });
        // "eluard" must find "Éluard" — the vault is bilingual FR/EN.
        input.value = 'eluard';
        input.dispatchEvent(new Event('input'));
        expect(options()).toEqual(['eluard1926']);
        // …and the surname the ladder could not transliterate is reachable.
        input.value = 'kanel';
        input.dispatchEvent(new Event('input'));
        expect(options()).toEqual(['vonkanel2019']);
    });

    it('ANDs its tokens across key, title, author and year', async () => {
        await openFromDeckAndType('sleep 2019');
        expect(options()).toEqual(['vonkanel2019']);
        const input = document.querySelector('.acc-keypicker-input');
        input.value = 'sleep 2025';
        input.dispatchEvent(new Event('input'));
        expect(options()).toEqual([]);
        expect(document.querySelector('.acc-keypicker-count').textContent)
            .toContain('No entry matches');
    });

    it('never offers a scan record that has no authoritative bib entry', async () => {
        await openFromDeckAndType('orphan');
        expect(options()).toEqual([]);
        expect(document.querySelector('.acc-keypicker-count').textContent)
            .toContain('No entry matches');

        const input = document.querySelector('.acc-keypicker-input');
        input.value = 'unknown';
        input.dispatchEvent(new Event('input'));
        expect(options(), 'a missing authority flag must fail closed too').toEqual([]);
    });

    it('exposes a named live result count and described keyboard instructions', async () => {
        const { input } = await openFromDeckAndType('smith');
        const hint = document.getElementById(input.getAttribute('aria-describedby'));
        const count = document.querySelector('.acc-keypicker-count');
        expect(hint.textContent).toContain('Enter to pick');
        expect(hint.textContent).toContain('Tab to close');
        expect(count.getAttribute('role')).toBe('status');
        expect(count.getAttribute('aria-live')).toBe('polite');
        expect(count.getAttribute('aria-atomic')).toBe('true');
        expect(input.getAttribute('aria-controls')).toBe(
            document.querySelector('.acc-keypicker-list').id);
    });

    it('has no serious or critical axe violations while open with results', async () => {
        await openFromDeckAndType('smith');
        const result = await axe.run(document.getElementById('audit-key-picker'), {
            rules: { 'color-contrast': { enabled: false } },
        });
        expect(result.violations.filter(
            (violation) => ['serious', 'critical'].includes(violation.impact)))
            .toEqual([]);
    });

    it('removes aria-activedescendant whenever no option exists', async () => {
        const { input } = await openFromDeckAndType('smith');
        expect(document.getElementById(input.getAttribute('aria-activedescendant')))
            .toBeTruthy();
        input.value = 'does-not-exist';
        input.dispatchEvent(new Event('input'));
        expect(input.hasAttribute('aria-activedescendant')).toBe(false);
        input.value = '';
        input.dispatchEvent(new Event('input'));
        expect(input.hasAttribute('aria-activedescendant')).toBe(false);
    });

    it('Enter picks, confirms, and posts WITHOUT source: "accession"', async () => {
        await openFromDeckAndType('vonkanel');
        const input = document.querySelector('.acc-keypicker-input');
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
        // Ceremony first: nothing is written on the pick alone.
        expect(mappingCalls(fetchMock)).toEqual([]);
        acceptConfirm();
        await vi.waitFor(() => expect(mappingCalls(fetchMock).length).toBe(1));
        const body = mappingCalls(fetchMock)[0];
        expect(body).toEqual({ pdf: 'b/d.pdf', citation_key: 'vonkanel2019', confirm: true });
        expect(body.source, 'hand curation must never be credited to the resolver')
            .toBeUndefined();
    });

    it('arrow keys move the selection and Enter takes the highlighted row', async () => {
        await openFromDeckAndType('20');   // several matches
        expect(options().length).toBeGreaterThan(1);
        const input = document.querySelector('.acc-keypicker-input');
        const first = options()[0];
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
        const second = options()[1];
        expect(document.querySelectorAll('.acc-keypicker-opt')[1]
            .getAttribute('aria-selected')).toBe('true');
        expect(input.getAttribute('aria-activedescendant')).toBe(
            document.querySelectorAll('.acc-keypicker-opt')[1].id);
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
        acceptConfirm();
        await vi.waitFor(() => expect(mappingCalls(fetchMock).length).toBe(1));
        expect(mappingCalls(fetchMock)[0].citation_key).toBe(second);
        expect(second).not.toBe(first);
    });

    it('Escape closes and returns focus to the control that opened it', async () => {
        const { btn, input } = await openFromDeckAndType('smith');
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        expect(document.getElementById('audit-key-picker')).toBeNull();
        expect(document.activeElement).toBe(btn);
        expect(mappingCalls(fetchMock)).toEqual([]);
    });

    it('Tab and outside focus close without forcing focus back to the opener', async () => {
        const { btn, input } = await openFromDeckAndType('smith');
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }));
        await vi.waitFor(() => expect(document.getElementById('audit-key-picker')).toBeNull());
        expect(document.activeElement).not.toBe(btn);
        expect(document.activeElement.tagName).not.toBe('BODY');

        btn.click();
        const reopened = await vi.waitFor(() => document.querySelector('.acc-keypicker-input'));
        await vi.waitFor(() => expect(document.querySelector('.acc-keypicker-count').textContent)
            .toContain('Type to search'));
        const outside = document.createElement('button');
        document.body.appendChild(outside);
        outside.focus();
        expect(document.getElementById('audit-key-picker')).toBeNull();
        expect(document.activeElement).toBe(outside);
        expect(reopened.getAttribute('aria-expanded')).toBe('false');
    });

    it('Shift+Tab closes and returns to the opener', async () => {
        const { btn, input } = await openFromDeckAndType('smith');
        input.dispatchEvent(new KeyboardEvent('keydown', {
            key: 'Tab', shiftKey: true, bubbles: true,
        }));
        expect(document.getElementById('audit-key-picker')).toBeNull();
        expect(document.activeElement).toBe(btn);
    });

    it('scrolls the selected option and accepts click and touch activation', async () => {
        const previous = HTMLElement.prototype.scrollIntoView;
        const scroll = vi.fn();
        Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
            configurable: true, writable: true, value: scroll,
        });
        try {
            const { input } = await openFromDeckAndType('20');
            expect(scroll).toHaveBeenCalledWith({ block: 'nearest' });
            input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
            expect(scroll.mock.calls.length).toBeGreaterThan(1);
            document.querySelector('.acc-keypicker-opt').dispatchEvent(
                new MouseEvent('click', { bubbles: true }));
            let strip = document.querySelector('.inline-confirm-strip');
            expect(strip).toBeTruthy();
            strip.querySelector('button').click();

            await openFromDeckAndType('smith');
            document.querySelector('.acc-keypicker-opt').dispatchEvent(
                new Event('touchend', { bubbles: true, cancelable: true }));
            strip = document.querySelector('.inline-confirm-strip');
            expect(strip).toBeTruthy();
        } finally {
            if (previous === undefined) delete HTMLElement.prototype.scrollIntoView;
            else HTMLElement.prototype.scrollIntoView = previous;
        }
    });

    it('its keystrokes never reach the deck underneath', async () => {
        // Compare the card's NAME, not its whole text: the picker mounts
        // inside the card (anchored to the verb that opened it), so its own
        // copy is legitimately part of card.textContent.
        const name = () => document.querySelector('.audit-ambig-name').textContent;
        const before = name();
        const { input } = await openFromDeckAndType('smith');
        // "s" is the deck's skip verb; typed into the picker it must search
        // and nothing else.
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 's', bubbles: true }));
        expect(name()).toBe(before);
        expect(document.getElementById('audit-key-picker'),
            'and the picker must still be open').toBeTruthy();
    });

    it('the link is undoable exactly like every other mapping decision', async () => {
        await openFromDeckAndType('smith');
        document.querySelector('.acc-keypicker-input')
            .dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
        acceptConfirm();
        await vi.waitFor(() => expect(mappingCalls(fetchMock).length).toBe(1));
        // Await a DOM signal, never the fetch-call count: the response tail
        // (state update + re-render) runs after the mock records the call.
        const undo = await vi.waitFor(() => {
            const b = document.querySelector('[data-acc-deck="undo"]');
            expect(b).toBeTruthy();
            expect(b.disabled).toBe(false);
            return b;
        });
        fetchMock.mockClear();
        undo.click();
        acceptConfirm();
        await vi.waitFor(() => expect(mappingCalls(fetchMock)).toEqual([
            { pdf: 'b/d.pdf', clear: true, confirm: true },
        ]));
    });

    it('the table view offers it on every row the ladder did not resolve', async () => {
        document.body.innerHTML = DOM;
        fetchMock = mkFetch();
        await load(fetchMock, 'table');
        // never-identified, no-match-grade and candidate rows all get it…
        for (const rel of ['b/e.pdf', 'b/d.pdf', 'b/f.pdf']) {
            expect(document.querySelector(`[data-acc-verb="link-to"][data-rel="${rel}"]`),
                `${rel} must offer the manual picker`).toBeTruthy();
        }
        // …the row the ladder DID resolve to a bib key already has [link].
        expect(document.querySelector('[data-acc-verb="link-to"][data-rel="b/a.pdf"]'))
            .toBeNull();
    });

    it('the table verb writes the same un-annotated body', async () => {
        document.body.innerHTML = DOM;
        fetchMock = mkFetch();
        await load(fetchMock, 'table');
        document.querySelector('[data-acc-verb="link-to"][data-rel="b/e.pdf"]').click();
        const input = await vi.waitFor(() => {
            const el = document.querySelector('.acc-keypicker-input');
            expect(el).toBeTruthy();
            return el;
        });
        await vi.waitFor(() => {
            expect(document.querySelector('.acc-keypicker-count').textContent)
                .toContain('Type to search');
        });
        input.value = 'myran';
        input.dispatchEvent(new Event('input'));
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
        acceptConfirm();
        await vi.waitFor(() => expect(mappingCalls(fetchMock)).toEqual([
            { pdf: 'b/e.pdf', citation_key: 'myran2025', confirm: true },
        ]));
    });
});

describe('review-list select-all', () => {
    let fetchMock;
    beforeEach(async () => {
        document.body.innerHTML = DOM;
        fetchMock = mkFetch();
        await load(fetchMock, 'table');
    });
    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it('[all] ticks every listed row — explicitly including title_filename', async () => {
        document.querySelector('[data-acc-selall="acc:1"]').click();
        await vi.waitFor(() => {
            expect(document.querySelector('[data-acc-acc-sel="b/c.pdf"]').checked)
                .toBe(true);   // title_filename: never default, but [all] may
        });
        document.querySelector('[data-acc-selall="acc:0"]').click();
        await vi.waitFor(() => {
            expect(document.querySelector('[data-acc-acc-sel="b/b.pdf"]').checked)
                .toBe(false);
        });
    });
});
