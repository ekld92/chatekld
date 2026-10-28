// The Not-in-Zotero tab reports its identify outcomes ON SCREEN (2026-09-07).
//
// The reported bug: clicking [re-identify] appeared to do nothing. It was
// doing everything — `_nizIdentifyOne` reported every outcome through
// `announceStatus()` alone, which writes to the `sr-only`
// #sr-status-announcer. `_nizApplyRow` repaints the row's cells, but ~90% of
// this bucket already holds a cached resolution, so a successful run that
// returned the SAME answer repainted identical cells and the only visible
// event was the button flickering to `…` and back. "Nothing happened" was
// literally true on screen while the pipeline worked.
//
// These tests are BEHAVIOURAL on purpose. A source ratchet on
// `announceStatus` would not have caught the original bug — the old code
// called it faithfully. The question is only ever "is there visible text
// afterwards", so that is what is asserted.
//
// Same defect class the accession deck (#audit-acc-status) and the tagging
// deck each fixed once; this is its third instance.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

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

/** A cached resolution — what most of this bucket actually holds, and the
 *  case where an identify legitimately changes nothing on the row. */
const CACHED = {
    outcome: 'candidates', tier: 'printed_doi', citation_key: null,
    doi: '10.1/a', query_kind: 'doi', evidence: 'text',
    candidates: [{ source: 'crossref', title: 'A cached paper', authors: ['Smith, J'], year: 2024, doi: '10.1/a' }],
    scores: [1.0], notes: [], consulted: [], from_cache: true,
};

const REPORT = {
    count: 2, pending_count: 2, no_match_count: 0,
    resolved_count: 1, with_doi_count: 1, missing_count: 0,
    rows: [
        { pdf: { abs: '/v/b/a.pdf', rel: 'b/a.pdf', name: 'a.pdf' },
          state: 'pending', exists: true, resolution: CACHED },
        { pdf: { abs: '/v/b/n.pdf', rel: 'b/n.pdf', name: 'n.pdf' },
          state: 'pending', exists: true, resolution: null },
    ],
};

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

function statusText() {
    const el = document.getElementById('audit-niz-status');
    if (!el) return null;
    // The MESSAGE, not the decoration: `ui.js::statusLine` puts the tone glyph
    // in its own aria-hidden span, so reading the whole host would prefix
    // every assertion with a character the user is never meant to read.
    const body = el.querySelector('.status-text');
    return body ? body.textContent : el.textContent;
}

function rowBtn(rel, action) {
    const tr = document.querySelector(`tr[data-rel="${rel}"]`);
    expect(tr, `no row for ${rel}`).toBeTruthy();
    return tr.querySelector(`[data-action="${action}"]`);
}

function acceptConfirm() {
    const strip = document.querySelector('.inline-confirm-strip');
    expect(strip, 'no confirm strip appeared').toBeTruthy();
    const buttons = [...strip.querySelectorAll('button')];
    buttons[buttons.length - 1].click();   // Cancel is first in DOM order
}

/** Resolve once the run has finished — never on the in-progress
 *  "Identifying …" line, which contains the PDF name too. */
async function waitForOutcome() {
    await vi.waitFor(() => {
        const t = statusText();
        expect(t && !t.startsWith('Identifying') && !t.startsWith('[0/'),
            `still in progress: ${t}`).toBe(true);
    });
    return statusText();
}

async function load(fetchMock) {
    const store = new Map();
    // jsdom ships no CSS.escape; `_nizApplyRow`/`_nizMarkRows` use it to
    // address a row by its rel. Both real engines have it — this is an
    // environment gap, not a product one.
    vi.stubGlobal('CSS', {
        escape: (v) => String(v).replace(/[^a-zA-Z0-9_-]/g, (c) => `\\${c}`),
    });
    vi.stubGlobal('localStorage', {
        getItem: (k) => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => store.set(k, String(v)),
        removeItem: (k) => store.delete(k),
    });
    vi.stubGlobal('fetch', fetchMock);
    // audit.js keeps module state across tests in one file (the _acc/_niz
    // trap) — a fresh instance per test, the accessionDeck.test.js pattern.
    vi.resetModules();
    ({ selectAuditReport } = await import('../../static/js/audit.js'));
    await selectAuditReport('not_in_zotero');
    await vi.waitFor(() => {
        expect(document.querySelector('tr[data-rel="b/a.pdf"]')).toBeTruthy();
    });
}

function mkFetch(overrides = {}) {
    return vi.fn(async (url, opts) => {
        const u = String(url);
        for (const [needle, resp] of Object.entries(overrides)) {
            if (u.includes(needle)) {
                return typeof resp === 'function' ? resp(u, opts) : resp;
            }
        }
        if (u.includes('/api/audit/reports/not_in_zotero')) return jsonOk(REPORT);
        return jsonOk({ ok: true });
    });
}

beforeEach(() => { document.body.innerHTML = DOM; });
afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = ''; });

describe('Not in Zotero: an identify outcome is visible, not only announced', () => {
    it('paints a status line when re-identify returns the SAME cached answer', async () => {
        // The exact reported gesture: a row that already holds a resolution,
        // re-identified, answering identically. Every cell repaints to the
        // same text — so the status line is the ONLY visible evidence.
        const identify = sseOk([{ row: {
            pdf: { rel: 'b/a.pdf', name: 'a.pdf' }, sha256: 'a1',
            resolution: { ...CACHED, from_cache: false, consulted: ['crossref'] },
        } }]);
        await load(mkFetch({ '/api/accession/identify': identify }));

        expect(statusText()).toBe('');
        rowBtn('b/a.pdf', 'niz-identify').click();

        expect(await waitForOutcome()).toContain('a.pdf');
        expect(statusText()).toMatch(/crossref/i);
    });

    it('reports "no match found" as a result rather than a silence', async () => {
        const identify = sseOk([{ row: {
            pdf: { rel: 'b/n.pdf', name: 'n.pdf' }, sha256: 'n1',
            resolution: {
                outcome: 'none', tier: null, citation_key: null, doi: null,
                query_kind: 'title', evidence: 'text', candidates: [],
                scores: [], notes: [], consulted: ['crossref', 'europepmc'],
                from_cache: false,
            },
        } }]);
        await load(mkFetch({ '/api/accession/identify': identify }));

        rowBtn('b/n.pdf', 'niz-identify').click();

        expect(await waitForOutcome()).toContain('no match found');
        // ...and it names what was actually asked, which is the fact that
        // answers "did clicking this do anything at all?".
        expect(statusText()).toMatch(/asked .*Crossref/i);
    });

    it('distinguishes a cached answer from a live lookup', async () => {
        const identify = sseOk([{ row: {
            pdf: { rel: 'b/a.pdf', name: 'a.pdf' }, sha256: 'a1',
            resolution: { ...CACHED, from_cache: true },
        } }]);
        await load(mkFetch({ '/api/accession/identify': identify }));

        rowBtn('b/a.pdf', 'niz-identify').click();

        expect(await waitForOutcome()).toContain('cached');
        expect(statusText()).toContain('nothing was asked online');
    });

    it('names WHY a degraded lookup came up empty, not just that it did', async () => {
        // "no match found" reads identically whether nothing matched or every
        // source timed out at the configured accession_timeout_s (whose live
        // value is the 5 s floor of its range). The note text is the only
        // thing that tells those apart.
        const identify = sseOk([{ row: {
            pdf: { rel: 'b/n.pdf', name: 'n.pdf' }, sha256: 'n1',
            resolution: {
                outcome: 'none', tier: null, candidates: [], scores: [],
                notes: ['crossref timed out after 5s'],
                consulted: ['crossref'], from_cache: false,
            },
        } }]);
        await load(mkFetch({ '/api/accession/identify': identify }));

        rowBtn('b/n.pdf', 'niz-identify').click();

        expect(await waitForOutcome()).toContain('crossref timed out after 5s');
    });

    it('surfaces a server error on the tab, not only in the top banner', async () => {
        const identify = sseOk([{ error: 'An identification is already running.' }]);
        await load(mkFetch({ '/api/accession/identify': identify }));

        rowBtn('b/a.pdf', 'niz-identify').click();

        expect(await waitForOutcome()).toContain('already running');
        expect(document.getElementById('audit-niz-status').classList
            .contains('is-error')).toBe(true);
    });
});

describe('Not in Zotero: force is carried in the DOM, not in the label', () => {
    it('sends force for a row that already holds a resolution', async () => {
        const fetchMock = mkFetch({
            '/api/accession/identify': () => sseOk([{ row: {
                pdf: { rel: 'b/a.pdf', name: 'a.pdf' }, sha256: 'a1',
                resolution: CACHED,
            } }]),
        });
        await load(fetchMock);

        const btn = rowBtn('b/a.pdf', 'niz-identify');
        expect(btn.textContent).toBe('[re-identify]');
        expect(btn.getAttribute('data-force')).toBe('1');
        // Relabel the button — the old code read `force` off this string, so
        // a cosmetic edit silently downgraded every re-identify to a
        // cache-consulting identify (the "nothing happened" symptom).
        btn.textContent = '[look it up again]';
        btn.click();

        expect(await waitForOutcome()).toContain('a.pdf');
        const body = JSON.parse(fetchMock.mock.calls
            .find(([u]) => String(u).includes('/api/accession/identify'))[1].body);
        expect(body.force).toBe(true);
    });

    it('does not force a row that has never been identified', async () => {
        const fetchMock = mkFetch({
            '/api/accession/identify': () => sseOk([{ row: {
                pdf: { rel: 'b/n.pdf', name: 'n.pdf' }, sha256: 'n1',
                resolution: { outcome: 'none', candidates: [], scores: [],
                              notes: [], consulted: [], from_cache: false },
            } }]),
        });
        await load(fetchMock);

        const btn = rowBtn('b/n.pdf', 'niz-identify');
        expect(btn.textContent).toBe('[identify]');
        expect(btn.getAttribute('data-force')).toBe('0');
        btn.click();

        expect(await waitForOutcome()).toContain('n.pdf');
        const body = JSON.parse(fetchMock.mock.calls
            .find(([u]) => String(u).includes('/api/accession/identify'))[1].body);
        expect(body.force).toBe(false);
    });

    it('flips a newly-resolved row to force so the next click is not a cache read', async () => {
        await load(mkFetch({
            '/api/accession/identify': () => sseOk([{ row: {
                pdf: { rel: 'b/n.pdf', name: 'n.pdf' }, sha256: 'n1',
                resolution: { ...CACHED, from_cache: false, consulted: ['crossref'] },
            } }]),
        }));

        const btn = rowBtn('b/n.pdf', 'niz-identify');
        expect(btn.getAttribute('data-force')).toBe('0');
        btn.click();

        expect(await waitForOutcome()).toContain('n.pdf');
        expect(btn.textContent).toBe('[re-identify]');
        expect(btn.getAttribute('data-force')).toBe('1');
    });
});

describe('Not in Zotero: the batch reports what it found', () => {
    it('summarises the outcome breakdown and how many were looked up online', async () => {
        const batch = sseOk([
            { row: { pdf: { rel: 'b/a.pdf', name: 'a.pdf' }, sha256: 'a1',
                     resolution: { ...CACHED, from_cache: false, consulted: ['crossref'] } } },
            { row: { pdf: { rel: 'b/n.pdf', name: 'n.pdf' }, sha256: 'n1',
                     resolution: { outcome: 'none', candidates: [], scores: [],
                                   notes: [], consulted: [], from_cache: true } } },
            { accession: { total: 2, done: 2, outcomes: {}, cancelled: false } },
        ]);
        await load(mkFetch({ '/api/accession/batch': batch }));

        document.querySelectorAll('input[data-niz-sel]').forEach((cb) => {
            cb.checked = true;
            cb.dispatchEvent(new Event('change', { bubbles: true }));
        });
        document.querySelector('[data-action="niz-bulk"][data-verb="reidentify"]').click();
        acceptConfirm();

        expect(await waitForOutcome()).toContain('Identified 2 of 2');
        // The breakdown is what makes "0 matched" legible as a RESULT.
        expect(statusText()).toContain('1 candidates');
        expect(statusText()).toContain('1 none');
        // ...and how much of it actually cost egress.
        expect(statusText()).toContain('1 looked up online');
    });
});
