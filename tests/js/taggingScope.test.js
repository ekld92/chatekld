// Pins the tagging view's PDF-less handling (tagging.js).
//
// `?scope=all` shipped server-side on 2026-08-11 but nothing in the UI ever
// requested it, so ~2 100 records that are perfectly writable through their
// note and their Zotero item stayed invisible. The toggle added 2026-08-12 is
// the whole distance between "invisible" and "taggable", and it makes the
// `has_pdf:false` degradations — five call sites that had ZERO coverage — the
// common case rather than an edge one.
//
// Guarded classes:
//   1. SCOPE WIRING — the request carries the persisted scope; the toggle
//      re-fetches (it changes DATA, unlike the view toggle) and persists.
//   2. DEGRADATION — a PDF-less row shows no Finder cell content and no
//      [→finder] verb rather than an empty cell that reads as "no tags yet".
//   3. ORDERING — `_taggedScore` is a FRACTION of the targets a row HAS, not a
//      count. This changed the DEFAULT scope's deck order too (a record tagged
//      on its only target moves from near-front to last), which is the
//      intended behaviour — a record with nothing left to do belongs at the
//      back — but it was unpinned, so it was drift rather than a decision.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { taggingRenderInto } from '../../static/js/tagging.js';

function jsonOk(payload) {
    return { ok: true, status: 200, json: async () => payload };
}

function row(key, { has_pdf = true, finder = [], note = null, zotero = null,
                    year = '2020' } = {}) {
    return {
        citation_key: key,
        year,
        title: `Title ${key}`,
        has_pdf,
        pdf_paths: has_pdf ? [{ abs: `/v/${key}.pdf`, rel: `${key}.pdf`, name: `${key}.pdf` }] : [],
        finder_tags: has_pdf ? finder : [],
        note: note ? { present: true, rel: `${key}.md`, tags: note } : { present: false, rel: null, tags: [] },
        zotero: zotero
            ? { present: true, item_key: `Z${key}`, parent_tags: zotero, child_note_tags: [] }
            : { present: false, item_key: null, parent_tags: [], child_note_tags: [] },
        bib_keywords: [],
    };
}

const DOM = `
  <div id="audit-report-meta"></div>
  <div id="audit-report-body"></div>
  <div id="sr-status-announcer" role="status"></div>
`;

let store;
let requested;

// tagging.js reads `_view` / `_scope` from localStorage ONCE at module load,
// so seeding storage from a test is inert — and worse, a test that flips one
// via the UI leaks that module state into every later test. Everything here
// therefore drives the real controls and asserts from a known starting point.
async function render(rows, { pdfless = 0 } = {}) {
    requested = [];
    vi.stubGlobal('fetch', vi.fn(async (url) => {
        const u = String(url);
        requested.push(u);
        if (u.includes('/api/tagging/inventory')) {
            return jsonOk({ rows, count: rows.length, scope: 'x', pdfless_count: pdfless });
        }
        if (u.includes('/api/tagging/vocab')) return jsonOk({ vocab: [] });
        if (u.includes('/api/tagging/zotero-available')) return jsonOk({ available: true, writable: true });
        return jsonOk({});
    }));
    await taggingRenderInto(
        document.getElementById('audit-report-body'),
        document.getElementById('audit-report-meta'),
    );
    // Let the concurrent vocab / probe promises settle into the render.
    await Promise.resolve();
}

/** Drain the microtask queue past a full re-fetch (fetch → json → Promise.all). */
async function flush() {
    for (let i = 0; i < 5; i += 1) {
        await new Promise((resolve) => { setTimeout(resolve, 0); });
    }
}

/** Flip a toolbar control and let its (re-fetching) handler settle. */
async function setScope(all) {
    const box = document.getElementById('tagging-scope-all');
    expect(box, 'scope toggle missing').toBeTruthy();
    if (box.checked === all) return;
    box.checked = all;
    box.dispatchEvent(new Event('change'));
    await flush();
}

async function useView(which) {
    const btn = document.querySelector(`[data-action="view-${which}"]`);
    expect(btn, `${which} toggle missing`).toBeTruthy();
    if (btn.getAttribute('aria-pressed') !== 'true') btn.click();
    await Promise.resolve();
}
const useTable = () => useView('table');
const useDeck = () => useView('deck');

describe('tagging scope + PDF-less degradation', () => {
    beforeEach(() => {
        document.body.innerHTML = DOM;
        store = new Map();
        vi.stubGlobal('localStorage', {
            getItem: (k) => (store.has(k) ? store.get(k) : null),
            setItem: (k, v) => store.set(k, String(v)),
            removeItem: (k) => store.delete(k),
        });
    });

    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it('toggling scope persists and re-fetches with scope=all', async () => {
        await render([row('a2020')]);
        // Default: the historical set, never silently widened.
        expect(requested.find((u) => u.includes('/api/tagging/inventory')))
            .toContain('scope=with_pdf');

        await setScope(true);
        expect(store.get('tagging_scope')).toBe('all');
        expect(requested.some((u) => u.includes('scope=all'))).toBe(true);

        await setScope(false);
        expect(store.get('tagging_scope')).toBe('with_pdf');
    });

    it('a PDF-less row loses its Finder cell and its [→finder] verb', async () => {
        await render([row('withpdf', { finder: ['dep'] }),
                      row('nopdf', { has_pdf: false, note: ['dep'] })], { pdfless: 1 });
        await useTable();

        const body = document.getElementById('audit-report-body');
        const rows = [...body.querySelectorAll('tbody tr')];
        const cell = (tr) => tr.querySelector('.tagging-finder-cell');
        const withPdf = rows.find((tr) => tr.textContent.includes('withpdf'));
        const noPdf = rows.find((tr) => tr.textContent.includes('nopdf'));
        expect(withPdf && noPdf, 'both rows should render').toBeTruthy();

        expect(cell(withPdf).textContent).toContain('dep');
        expect(cell(noPdf).textContent.trim()).toBe('—');
        expect(withPdf.querySelector('[data-action="apply-finder"]')).toBeTruthy();
        expect(noPdf.querySelector('[data-action="apply-finder"]')).toBeNull();
    });

    it('the meta line discloses the PDF-less share under scope=all', async () => {
        await render([row('a2020', { has_pdf: false })], { pdfless: 1 });
        await setScope(true);
        expect(document.getElementById('audit-report-meta').textContent)
            .toContain('1 with no local PDF');
        await setScope(false);
        expect(document.getElementById('audit-report-meta').textContent)
            .toContain('with a local PDF');
    });

    // -- ordering -----------------------------------------------------------
    // The deck orders untagged-first. Under a COUNT, a record tagged on its
    // only available target scored 1 and sorted ahead of a record tagged on
    // two of three (score 2) — finished work at the front of the queue. Under
    // a FRACTION they are 1.0 and 0.67, so the finished one sorts last.

    it('orders by fraction tagged, so a fully-tagged row sorts last', async () => {
        await render([
            // Fully tagged, one target only. Count=1 (near front) -> 1.0 (last).
            row('done', { finder: ['dep'] }),
            // Tagged on 2 of 3. Count=2 (last) -> 0.67 (middle).
            row('partial', { finder: ['dep'], note: ['dep'], zotero: [] }),
            // Nothing tagged anywhere.
            row('fresh', { note: [], zotero: [] }),
        ]);
        await useDeck();
        const title = () => document.querySelector('.tagging-card-title');
        const skip = async () => {
            document.querySelector('.tagging-deck')
                .dispatchEvent(new KeyboardEvent('keydown', { key: 's', bubbles: true }));
            await flush();
        };
        expect(title(), 'deck card missing').toBeTruthy();
        expect(title().tagName, 'a visible card title must support heading navigation')
            .toBe('H4');

        // The discriminating assertion. Under the OLD count the order was
        // fresh(0), done(1), partial(2); under the fraction it is
        // fresh(0), partial(0.67), done(1.0). Asserting only that `fresh`
        // comes first would pass under both.
        expect(title().textContent).toContain('Title fresh');
        await skip();
        expect(title().textContent).toContain('Title partial');
        await skip();
        expect(title().textContent).toContain('Title done');
    });

    it('a row with no addressable target at all sorts last, not first', async () => {
        await render([
            row('orphan', { has_pdf: false }),           // available = 0 -> 1
            row('fresh', { note: [], zotero: [] }),      // available = 3 -> 0
        ], { pdfless: 1 });
        await useDeck();
        expect(document.querySelector('.tagging-card-title').textContent)
            .toContain('fresh');
    });
});

describe('proposal evidence + PDF-less target degradation (2026-08-12 review)', () => {
    beforeEach(() => {
        document.body.innerHTML = DOM;
        store = new Map();
        vi.stubGlobal('localStorage', {
            getItem: (k) => (store.has(k) ? store.get(k) : null),
            setItem: (k, v) => store.set(k, String(v)),
            removeItem: (k) => store.delete(k),
        });
    });

    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    /** Keys asked for via POST /api/tagging/snippet (the key rides the body). */
    let snippetKeys;

    /** Render with a scriptable /propose + /snippet responder. */
    let _scanStamp = 0;

    async function renderWithPropose(rows, proposePayload, { pdfless = 0 } = {}) {
        requested = [];
        snippetKeys = [];
        vi.stubGlobal('fetch', vi.fn(async (url, opts) => {
            const u = String(url);
            requested.push(u);
            if (u.includes('/api/tagging/snippet')) {
                try { snippetKeys.push(JSON.parse(opts.body).citation_key); }
                catch { snippetKeys.push('(unparsed)'); }
            }
            if (u.includes('/api/tagging/inventory')) {
                // A distinct scanned_at per render: tagging.js keeps module
                // state across tests in this file (it reads _view/_scope once
                // at load, which is why this file drives the real controls),
                // and since 2026-09-02 the snippet cache is module state too —
                // it survives a scope flip on purpose and is dropped only when
                // a NEW scan arrives. Without a distinct stamp, one test's
                // warmed excerpts would satisfy the next test's fetch.
                return jsonOk({ rows, count: rows.length, scope: 'x',
                                pdfless_count: pdfless, scanned_at: ++_scanStamp });
            }
            if (u.includes('/api/tagging/vocab')) return jsonOk({ vocab: [] });
            if (u.includes('/api/tagging/zotero-available')) return jsonOk({ available: true, writable: true });
            if (u.includes('/api/tagging/propose')) return jsonOk(proposePayload);
            if (u.includes('/api/tagging/snippet')) return jsonOk({ snippet: '', chars: 0 });
            return jsonOk({});
        }));
        await taggingRenderInto(
            document.getElementById('audit-report-body'),
            document.getElementById('audit-report-meta'),
        );
        await Promise.resolve();
    }

    // The deck's Propose reports on the per-CARD line; Accept/Undo use the
    // deck-level one that survives a card change.
    const status = () => document.getElementById('tagging-card-status')
        || document.getElementById('tagging-deck-lastresult');

    it('says a proposal came from metadata alone, before it can be accepted', async () => {
        // The route documents `source` as the cue that lets a user weigh a
        // metadata-only proposal differently; the client dropped the field, so
        // an abstract-only guess read exactly like one from the full text.
        await renderWithPropose(
            [row('meta2020', { has_pdf: false, note: [], zotero: [] })],
            { proposed: [{ tag: 'dep', confidence: 0.9 }], status: 'ok',
              source: 'metadata', has_pdf: false },
            { pdfless: 1 },
        );
        await useDeck();
        document.querySelector('[data-deck="propose"]').click();
        await flush();
        expect(status().textContent).toContain('from metadata only');
    });

    it('does not claim a missing PDF is an unreadable one', async () => {
        await renderWithPropose(
            [row('bare2020', { has_pdf: false, note: [], zotero: [] })],
            { proposed: [], status: 'no_text', source: 'none', has_pdf: false },
            { pdfless: 1 },
        );
        await useDeck();
        document.querySelector('[data-deck="propose"]').click();
        await flush();
        const txt = status().textContent;
        expect(txt).toContain('no local PDF');
        expect(txt).not.toContain('scanned/unreadable');
    });

    it('keeps the scanned-PDF wording when a PDF really is there', async () => {
        await renderWithPropose(
            [row('scan2020', { note: [], zotero: [] })],
            { proposed: [], status: 'no_text', source: 'none', has_pdf: true },
        );
        await useDeck();
        document.querySelector('[data-deck="propose"]').click();
        await flush();
        expect(status().textContent).toContain('scanned/unreadable');
    });

    it('disables the Finder toggle on a PDF-less card, with the reason', async () => {
        // Enabled + checked, it made the Accept confirm promise a Finder write
        // the server answers `skipped (no_pdf)`. _deckAccept collects
        // `checked && !disabled`, so disabling also fixes the POSTed targets.
        await render([row('meta2020', { has_pdf: false, note: [], zotero: [] })],
                     { pdfless: 1 });
        await useDeck();
        const cb = document.querySelector('.tagging-card-targets input[data-target="finder"]');
        expect(cb.disabled).toBe(true);
        expect(cb.checked).toBe(false);
        expect(cb.getAttribute('aria-label')).toContain('No local PDF');
    });

    it('leaves the Finder toggle usable when the row has a PDF', async () => {
        await render([row('withpdf2020', { note: [], zotero: [] })]);
        await useDeck();
        const cb = document.querySelector('.tagging-card-targets input[data-target="finder"]');
        expect(cb.disabled).toBe(false);
        expect(cb.checked).toBe(true);
    });

    it('does not prefetch an excerpt for a PDF-less NEXT card', async () => {
        // The visible-card path already skipped; _deckPrefetchNext did not, so
        // the deck still spent a round-trip warming a card that has nothing to
        // extract. Ordering is untagged-first and both rows are untagged, so
        // the PDF-bearing row leads and the PDF-less one is "next".
        await renderWithPropose(
            [row('withpdf2020', { note: [], zotero: [] }),
             row('meta2021', { has_pdf: false, note: [], zotero: [] })],
            {}, { pdfless: 1 });
        await useDeck();
        await flush();
        expect(snippetKeys).toEqual(['withpdf2020']);   // the visible card only
    });

    it('keeps warming the chain across a PDF-less card', async () => {
        // The PDF-less early return skipped _deckPrefetchNext entirely, so
        // every card following one paid a cold fetch.
        await renderWithPropose(
            [row('meta2020', { has_pdf: false, note: [], zotero: [] }),
             row('withpdf2021', { note: [], zotero: [] })],
            {}, { pdfless: 1 });
        await useDeck();
        await flush();
        // Card 1 has no excerpt to fetch, but card 2's must still be warmed.
        expect(snippetKeys).toEqual(['withpdf2021']);
    });
});
