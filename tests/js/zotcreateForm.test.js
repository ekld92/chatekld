// Creating a Zotero item from an orphan PDF, from the Not-in-Zotero tab.
//
// Four properties, each silent if it goes wrong:
//
// 1. The write is confirm-gated, and the confirm names what will be made and
//    which file will be linked — "create an item" alone does not tell you
//    which of 6,000 PDFs is about to be attached to it.
// 2. The form panel is a ROW ATTACHMENT. `installTableControls` moves and
//    hides rows on sort/filter, so without the marker a sort strands a form
//    that aims a write beside an unrelated PDF.
// 3. A retained attempt disables the form instead of offering a second
//    create — the duplicate barrier has to be visible, not only enforced.
// 4. Every outcome reaches the VISIBLE status line, never the sr-only
//    announcer alone (the 2026-09-07 defect class, third instance).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

function jsonOk(payload, ok = true, status = 200) {
    return { ok, status, json: async () => payload };
}

const PREVIEW = {
    pdf_rel: 'b/a.pdf', pdf_sha256: 'a'.repeat(64), klass: 'browser_print',
    pages: 3, producer: 'Skia/PDF', source_url: 'https://example.org/page',
    doi: null, isbn: null, title: 'A printed web page',
    suggested_item_type: 'webpage', filename: 'a.pdf',
    item_types: ['journalArticle', 'webpage', 'book'],
    transport: 'web', can_write: true,
    already_in_bib: null, resolution: null,
    draft: {
        item_type: 'webpage', prefilled_from: 'pdf', tier: null, basis: null,
        suggestions: [],
        fields: { title: 'A printed web page', url: 'https://example.org/page' },
        creators: [],
    },
    attempt: { stage: 'fresh', retained: null, message: null },
};

const REPORT = {
    count: 1, pending_count: 1, no_match_count: 0, resolved_count: 0,
    with_doi_count: 0, missing_count: 0,
    rows: [{ pdf: { abs: '/v/b/a.pdf', rel: 'b/a.pdf', name: 'a.pdf' },
             state: 'pending', exists: true, resolution: null }],
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

const statusText = () => {
    const el = document.getElementById('audit-niz-status');
    return el ? el.textContent : null;
};
const panel = () => document.querySelector('[data-zc-panel]');
const rowBtn = (action) =>
    document.querySelector(`tr[data-rel="b/a.pdf"] [data-action="${action}"]`);

function acceptConfirm() {
    const strip = document.querySelector('.inline-confirm-strip');
    expect(strip, 'no confirm strip appeared').toBeTruthy();
    const buttons = [...strip.querySelectorAll('button')];
    buttons[buttons.length - 1].click();   // Cancel is first in DOM order
    return strip.textContent;
}

async function load(overrides = {}) {
    const store = new Map();
    vi.stubGlobal('CSS', {
        escape: (v) => String(v).replace(/[^a-zA-Z0-9_-]/g, (c) => `\\${c}`),
    });
    vi.stubGlobal('localStorage', {
        getItem: (k) => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => store.set(k, String(v)),
        removeItem: (k) => store.delete(k),
    });
    const calls = [];
    const fetchMock = vi.fn(async (url, opts) => {
        const u = String(url);
        calls.push({ url: u, body: opts && opts.body ? JSON.parse(opts.body) : null });
        for (const [needle, resp] of Object.entries(overrides)) {
            if (u.includes(needle)) return typeof resp === 'function' ? resp(u, opts) : resp;
        }
        if (u.includes('/api/zotcreate/preview')) return jsonOk(PREVIEW);
        if (u.includes('/api/audit/reports/not_in_zotero')) return jsonOk(REPORT);
        return jsonOk({ ok: true });
    });
    vi.stubGlobal('fetch', fetchMock);
    vi.resetModules();   // audit.js keeps module state across tests in one file
    const { selectAuditReport } = await import('../../static/js/audit.js');
    await selectAuditReport('not_in_zotero');
    await vi.waitFor(() => {
        expect(document.querySelector('tr[data-rel="b/a.pdf"]')).toBeTruthy();
    });
    return calls;
}

async function openForm() {
    rowBtn('zc-open').click();
    await vi.waitFor(() => expect(panel()).toBeTruthy());
}

beforeEach(() => { document.body.innerHTML = DOM; });
afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = ''; });

describe('Create in Zotero', () => {
    it('prefills the form from the physical class and the page-1 text', async () => {
        await load();
        await openForm();
        const type = panel().querySelector('[data-zc="item_type"]');
        expect(type.value).toBe('webpage');          // browser_print ⇒ webpage
        const byKey = (k) => panel().querySelector(`[data-zc-field="${k}"]`).value;
        expect(byKey('title')).toBe('A printed web page');
        expect(byKey('url')).toBe('https://example.org/page');
        // The class is stated, because it is WHY the type was suggested.
        expect(panel().textContent).toMatch(/browser print/);
    });

    it('marks the form as belonging to its row, so a sort cannot strand it', async () => {
        await load();
        await openForm();
        expect(panel().hasAttribute('data-row-attachment')).toBe(true);
        expect(panel().previousElementSibling.getAttribute('data-rel')).toBe('b/a.pdf');
    });

    it('will not create without a confirm that names the item and the file', async () => {
        const calls = await load();
        await openForm();
        panel().querySelector('[data-zc="create"]').click();
        const text = acceptConfirm();
        expect(text).toMatch(/webpage/);
        expect(text).toMatch(/A printed web page/);
        expect(text).toMatch(/a\.pdf/);              // WHICH file gets linked
        await vi.waitFor(() => {
            expect(calls.some((c) => c.url.includes('/api/zotcreate/create'))).toBe(true);
        });
    });

    it('sends only non-empty fields, with parsed creators and confirm', async () => {
        const calls = await load({
            '/api/zotcreate/create': jsonOk({
                ok: true, item_key: 'PARENT01', item_type: 'webpage',
                attachment_key: 'ATTACH01', note: 'Zotero has it now.' }),
        });
        await openForm();
        panel().querySelector('[data-zc="creators"]').value =
            'Jardri, Renaud; World Health Organization';
        panel().querySelector('[data-zc="create"]').click();
        acceptConfirm();
        const post = await vi.waitFor(() => {
            const c = calls.find((x) => x.url.includes('/api/zotcreate/create'));
            expect(c).toBeTruthy();
            return c;
        });
        expect(post.body.confirm).toBe(true);
        expect(post.body.item_type).toBe('webpage');
        expect(post.body.fields.url).toBe('https://example.org/page');
        expect('DOI' in post.body.fields).toBe(false);   // empty, not sent blank
        // A name with no comma is an organisation, never split on a guess.
        expect(post.body.creators).toEqual([
            { creatorType: 'author', lastName: 'Jardri', firstName: 'Renaud' },
            { creatorType: 'author', name: 'World Health Organization' },
        ]);
    });

    it('reports the created keys in the VISIBLE status line', async () => {
        await load({
            '/api/zotcreate/create': jsonOk({
                ok: true, item_key: 'PARENT01', item_type: 'webpage',
                attachment_key: 'ATTACH01',
                note: 'This app will see it after a new scan.' }),
        });
        await openForm();
        panel().querySelector('[data-zc="create"]').click();
        acceptConfirm();
        await vi.waitFor(() => expect(statusText()).toMatch(/PARENT01/));
        expect(statusText()).toMatch(/new scan/);   // the sync-lag disclosure
        expect(panel()).toBeNull();                 // the form closes on success
    });

    it('does not promise an undo the journal has decided to refuse', async () => {
        await load({
            '/api/zotcreate/create': jsonOk({
                ok: true, undo: 'manual', item_key: 'PARENT01',
                item_type: 'webpage', attachment_key: 'ATTACH01', note: '' }),
        });
        await openForm();
        panel().querySelector('[data-zc="create"]').click();
        acceptConfirm();
        await vi.waitFor(() => expect(statusText()).toMatch(/PARENT01/));
        expect(statusText()).toMatch(/Undo is manual/);
        expect(statusText()).toMatch(/trash it in Zotero/);
    });

    it('creates nothing when the form changed after the confirm was offered', async () => {
        // The strip leaves the form editable, so the ceremony can name one
        // item while the boxes hold another — the single outcome a confirm
        // exists to prevent.
        const calls = await load();
        await openForm();
        panel().querySelector('[data-zc="create"]').click();
        panel().querySelector('[data-zc-field="title"]').value = 'Something else';
        acceptConfirm();
        await vi.waitFor(() => expect(statusText()).toMatch(/form changed/));
        expect(calls.some((c) => c.url.includes('/api/zotcreate/create'))).toBe(false);
    });

    it('shows a refusal rather than a silence', async () => {
        await load({
            '/api/zotcreate/create': jsonOk(
                { error: 'Zotero is set to rename linked files.' }, false, 409),
        });
        await openForm();
        panel().querySelector('[data-zc="create"]').click();
        acceptConfirm();
        await vi.waitFor(() => expect(statusText()).toMatch(/rename linked files/));
        expect(document.getElementById('audit-niz-status').className)
            .toMatch(/is-error/);
    });

    it('offers no form at all when an attempt is already retained', async () => {
        await load({
            '/api/zotcreate/preview': jsonOk({
                ...PREVIEW,
                attempt: { stage: 'complete',
                           retained: { parent_key: 'PARENT01' },
                           message: 'This PDF was already made into a Zotero item '
                                    + '(item PARENT01, attachment ATTACH01).' },
            }),
        });
        await openForm();
        expect(panel().querySelector('[data-zc="item_type"]')).toBeNull();
        expect(panel().textContent).toMatch(/already made into a Zotero item/);
        expect(statusText()).toMatch(/PARENT01/);
    });

    it('offers no form when Zotero is not writable', async () => {
        await load({
            '/api/zotcreate/preview': jsonOk({ ...PREVIEW, can_write: false }),
        });
        await openForm();
        expect(panel().querySelector('[data-zc="item_type"]')).toBeNull();
        expect(panel().textContent).toMatch(/ZOTERO_API_KEY|authorize/i);
    });

    it('says where a prefilled form came from, and how old that lookup is', async () => {
        await load({
            '/api/zotcreate/preview': jsonOk({
                ...PREVIEW,
                resolution: { outcome: 'candidates', tier: 'xmp_doi',
                              queried_at: Date.now() / 1000 - 3 * 86400,
                              consulted: [] },
                draft: {
                    item_type: 'journalArticle', prefilled_from: 'candidate',
                    tier: 'xmp_doi', basis: 'a DOI stored inside the file',
                    suggestions: [],
                    fields: { title: 'The real paper', DOI: '10.1/x',
                              publicationTitle: 'J. Things', date: '2024' },
                    creators: [{ creatorType: 'author', lastName: 'Jardri',
                                 firstName: 'Renaud' }],
                },
            }),
        });
        await openForm();
        expect(panel().querySelector('[data-zc="item_type"]').value)
            .toBe('journalArticle');
        expect(panel().querySelector('[data-zc-field="DOI"]').value).toBe('10.1/x');
        expect(panel().querySelector('[data-zc="creators"]').value)
            .toBe('Jardri, Renaud');
        expect(panel().textContent).toMatch(/DOI stored inside the file/);
        expect(panel().textContent).toMatch(/3 d ago/);
    });

    it('offers a weak match instead of filling the form with it', async () => {
        // The measured majority case: a Crossref search on the filename's
        // surname and year, which routinely names a DIFFERENT paper. Applied
        // silently it would create an item for that paper and link this PDF
        // to it, with a form that looks filled-in and plausible.
        await load({
            '/api/zotcreate/preview': jsonOk({
                ...PREVIEW,
                draft: {
                    item_type: 'journalArticle', prefilled_from: 'pdf',
                    tier: 'filename_only',
                    basis: "a search on the filename's surname and year — often "
                           + 'a different paper',
                    fields: { title: 'What page 1 says' }, creators: [],
                    suggestions: [{ source: 'crossref', title: 'Some other paper',
                                    authors: ['Abbot, Colleen'], year: 2001,
                                    doi: '10.9/z', container: 'J. Elsewhere',
                                    url: 'https://doi.org/10.9/z' }],
                },
            }),
        });
        await openForm();
        // The PDF's own title survives; the guess is NOT in the boxes.
        expect(panel().querySelector('[data-zc-field="title"]').value)
            .toBe('What page 1 says');
        expect(panel().querySelector('[data-zc-field="DOI"]').value).toBe('');
        expect(panel().textContent).toMatch(/often a different paper/);

        // …and picking it is an explicit act that then fills everything.
        panel().querySelector('.audit-zc-suggestions button').click();
        expect(panel().querySelector('[data-zc-field="DOI"]').value).toBe('10.9/z');
        expect(panel().querySelector('[data-zc-field="title"]').value)
            .toBe('Some other paper');
        expect(panel().querySelector('[data-zc="creators"]').value)
            .toBe('Abbot, Colleen');
        expect(statusText()).toMatch(/Nothing created yet/);
    });

    it('refuses to create a second item for a paper already in the bib', async () => {
        await load({
            '/api/zotcreate/preview': jsonOk({ ...PREVIEW,
                                               already_in_bib: 'smith2020' }),
        });
        await openForm();
        expect(panel().querySelector('[data-zc="item_type"]')).toBeNull();
        expect(panel().textContent).toMatch(/already in your bibliography as smith2020/);
    });

    it('shows the first page as text, collapsed, for a wrong extracted title', async () => {
        await load({
            '/api/zotcreate/preview': jsonOk({
                ...PREVIEW,
                page1_excerpt: 'ZOOM: ZWANGSSTÖRUNGEN – STATE OF THE ART\nEndres et al.',
            }),
        });
        await openForm();
        const det = panel().querySelector('details.audit-zc-excerpt');
        expect(det).toBeTruthy();
        expect(det.open).toBe(false);
        expect(det.querySelector('pre').textContent).toMatch(/ZWANGSSTÖRUNGEN/);
    });

    it('closes the form on a second click rather than stacking panels', async () => {
        await load();
        await openForm();
        rowBtn('zc-open').click();
        await vi.waitFor(() => expect(panel()).toBeNull());
    });
});
