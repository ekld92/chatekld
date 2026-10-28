// Pins the Inventory pairings board (audit.js, 2026-08-19 review request v):
// per-pair PDF↔Zotero overview on the Inventory tab.
//
// Guarded classes:
//   1. PDF TRANSPORT — the [view] preview is a header-fetched blob rendered
//      into an iframe, never an /api iframe src (the origin guard 403s
//      those); toggling closes and revokes.
//   2. PAIR DETAIL — the drawer shows per-PDF match provenance (the bridge
//      rung), the Zotero side (item + reading-note count + zotero:// deep
//      link with GROUP routing), the note, and per-surface tags; it
//      collapses on the second click.
//   3. NAVIGATION IS READ-ONLY — [open]/[reveal]/[Zotero] all go through
//      POST /api/audit/reveal and change nothing.
//   4. THE UNMAP VERBS (2026-09-07) are the drawer's only writes, and are
//      confirm-gated. They live here because "Entries With Several PDFs"
//      lists records owning ≥2 files, so a record with ONE wrongly-matched
//      PDF had no unmap affordance anywhere in the app; the drawer is the
//      only surface that shows a pair at all.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const jsonOk = (payload) => ({ ok: true, status: 200, json: async () => payload });

/** Read one facet of the pair drawer's <dl> by its label prefix. */
function facet(drawer, label) {
    const dl = drawer.querySelector('dl.audit-inv-dl');
    expect(dl, 'the pair drawer must be a definition grid').toBeTruthy();
    const kids = [...dl.children];
    const i = kids.findIndex(
        (el) => el.tagName === 'DT' && el.textContent.trim().startsWith(label));
    expect(i, `no <dt> for ${label}`).toBeGreaterThan(-1);
    return kids[i + 1].textContent;
}

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

const RECORDS = [
    {
        citation_key: 'smith2020', year: '2020', first_author: 'Smith, J',
        title: 'A paper', entry_type: 'article',
        has_bib_entry: true, has_zotero_item: true, has_obsidian_note: true,
        has_zotero_child_note: true, pdf_count: 1,
        pdf_paths: [{ abs: '/v/b/smith.pdf', rel: 'b/smith.pdf', name: 'smith.pdf', source: 'doi' }],
        note: { abs: '/v/n/smith2020.md', rel: 'n/smith2020.md', name: 'smith2020.md' },
        zotero: {
            item_key: 'ZK1', item_type: 'journalArticle',
            library_type: 'group', group_id: 484650, child_note_count: 2,
        },
        annotations_count_max: 3,
        finder_tags: ['ftag'], match_sources: ['doi'], obs_tags: ['ntag'],
        zotero_note_tags: [], zotero_parent_tags: ['ztag'], bib_keywords: [],
    },
    {
        citation_key: 'bare1999', year: '1999', first_author: null,
        title: null, entry_type: null,
        has_bib_entry: false, has_zotero_item: false, has_obsidian_note: false,
        has_zotero_child_note: false, pdf_count: 0,
        pdf_paths: [], note: null, zotero: null,
        annotations_count_max: -1,
        finder_tags: [], match_sources: [], obs_tags: [],
        zotero_note_tags: [], zotero_parent_tags: [], bib_keywords: [],
    },
];

function mkFetch(overrides = {}) {
    return vi.fn(async (url, opts) => {
        const u = String(url);
        for (const [needle, resp] of Object.entries(overrides)) {
            if (u.includes(needle)) {
                return typeof resp === 'function' ? resp(u, opts) : resp;
            }
        }
        if (u.includes('/api/audit/inventory')) {
            return jsonOk({ summary: { record_count: 2 }, records: RECORDS });
        }
        return jsonOk({ ok: true });
    });
}

let selectAuditReport;

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
    await selectAuditReport('inventory');
    await vi.waitFor(() => {
        expect(document.querySelector('[data-inv-detail]')).toBeTruthy();
    });
    fetchMock.mockClear();
}

describe('inventory pairings board', () => {
    let fetchMock;
    beforeEach(async () => {
        document.body.innerHTML = DOM;
        if (!URL.createObjectURL) URL.createObjectURL = () => {};
        if (!URL.revokeObjectURL) URL.revokeObjectURL = () => {};
        vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:mock-inv-pdf');
        vi.spyOn(URL, 'revokeObjectURL');
        fetchMock = mkFetch();
        await load(fetchMock);
    });
    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it('[view] fetches the PDF with the header and renders a blob iframe; a second click closes and revokes', async () => {
        fetchMock.mockImplementation(async (url) => {
            if (String(url).includes('/api/audit/pdf')) {
                return { ok: true, status: 200, blob: async () => new Blob(['x']) };
            }
            return jsonOk({ ok: true });
        });
        const viewBtn = document.querySelector('[data-inv-view="b/smith.pdf"]');
        expect(viewBtn, 'per-PDF [view] verb missing').toBeTruthy();
        viewBtn.click();
        await vi.waitFor(() => {
            const frame = document.querySelector('#audit-inv-pdf iframe');
            expect(frame).toBeTruthy();
            expect(frame.src).toContain('blob:');
        });
        const call = fetchMock.mock.calls.find(
            ([u]) => String(u).includes('/api/audit/pdf'));
        expect(call[1].headers['X-Requested-With']).toBe('ChatEKLD');
        // Toggle off: same rel click removes the panel and revokes the URL.
        viewBtn.click();
        await vi.waitFor(() => {
            expect(document.getElementById('audit-inv-pdf')).toBeNull();
        });
        expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:mock-inv-pdf');
    });

    it('the detail drawer shows provenance, the Zotero side, the note and per-surface tags — and collapses', async () => {
        const btn = document.querySelector('[data-inv-detail="smith2020"]');
        btn.click();
        const drawer = document.querySelector('.audit-inv-detail-row');
        expect(drawer, 'drawer row missing').toBeTruthy();
        expect(btn.getAttribute('aria-expanded')).toBe('true');
        const text = drawer.textContent;
        expect(text).toContain('matched via DOI in PDF metadata');
        expect(text).toContain('Current pdf: list: up to date');
        expect(text).toContain('journalArticle ZK1');    // Zotero item
        expect(text).toContain('2 reading note(s)');
        expect(text).toContain('n/smith2020.md');        // the note side
        expect(text).toContain('ftag');                  // Finder tags
        expect(text).toContain('ztag');                  // Zotero tags
        btn.click();
        expect(document.querySelector('.audit-inv-detail-row')).toBeNull();
        expect(btn.getAttribute('aria-expanded')).toBe('false');
    });

    it('the drawer offers both unmap verbs per PDF, each confirm-gated', async () => {
        document.querySelector('[data-inv-detail="smith2020"]').click();
        const drawer = document.querySelector('.audit-inv-detail-row');
        const perKey = drawer.querySelector('[data-action="detach"][data-key]');
        const blanket = drawer.querySelector('[data-action="detach"]:not([data-key])');
        expect(perKey, '[unmap] missing from the pair drawer').toBeTruthy();
        expect(blanket, '[never auto-map] missing from the pair drawer').toBeTruthy();
        expect(perKey.getAttribute('data-pdf')).toBe('b/smith.pdf');
        expect(perKey.getAttribute('data-key')).toBe('smith2020');
        expect(blanket.getAttribute('data-pdf')).toBe('b/smith.pdf');

        // The report body's delegate matches rows it rendered; the drawer is
        // injected afterwards, so a verb wired only through the delegate is
        // a dead button (the Phase-4 attach lesson).
        const before = fetchMock.mock.calls.length;
        blanket.click();
        expect(fetchMock.mock.calls.length,
               'a write must not fire before the confirm').toBe(before);
        const confirm = [...document.querySelectorAll('button')]
            .find((b) => /never auto-map/i.test(b.textContent) && b !== blanket);
        expect(confirm, 'the confirm strip never appeared — the verb is unwired').toBeTruthy();
        confirm.click();
        await vi.waitFor(() => {
            const call = fetchMock.mock.calls.find(
                ([u]) => String(u).includes('/api/audit/mapping'));
            expect(call, 'no mapping write reached the server').toBeTruthy();
            const body = JSON.parse(call[1].body);
            // The blanket form has its own field: the server refuses the
            // wildcard on detach_from precisely so a body cannot smuggle a
            // blanket refusal in where the confirm promised one key.
            expect(body).toEqual({
                pdf: 'b/smith.pdf', detach_all: true, confirm: true,
                trash_zotero_links: true,
            });
        });
    });

    // The report body's delegate dispatches [data-inv-view] and every
    // .audit-link-btn carrying a data-action. The drawer is INSIDE that body,
    // so a second, direct listener on those verbs fires both handlers on one
    // click. The three tests below are the discriminating ones the pair above
    // is not: they pass either way, because confirmInline is replace-not-stack
    // and the strip they look for exists after one fire or two.
    it('[view] in the drawer survives its own click', async () => {
        // _invViewPdf TOGGLES on a second call for the same rel. Bound twice,
        // call 1 built the preview and call 2 tore it down inside the same
        // dispatch — a button that did nothing at all, every time.
        document.querySelector('[data-inv-detail="smith2020"]').click();
        const view = document.querySelector('.audit-inv-detail-row [data-inv-view]');
        expect(view, 'the drawer offers no preview verb').toBeTruthy();
        view.click();
        expect(document.getElementById('audit-inv-pdf'),
               '[view] destroyed its own panel — the verb is bound twice').toBeTruthy();
    });

    it('an unmap verb runs its confirm ceremony exactly once', async () => {
        document.querySelector('[data-inv-detail="smith2020"]').click();
        const perKey = document.querySelector(
            '.audit-inv-detail-row [data-action="detach"][data-key]');
        let inserted = 0;
        const obs = new MutationObserver((recs) => {
            for (const r of recs) for (const n of r.addedNodes) {
                if (n.nodeType === 1 && n.classList
                    && n.classList.contains('inline-confirm-strip')) inserted += 1;
            }
        });
        obs.observe(document.body, { childList: true, subtree: true });
        perKey.click();
        await new Promise((r) => setTimeout(r, 0));
        obs.disconnect();
        expect(inserted, 'the confirm ceremony ran twice — the verb is bound twice')
            .toBe(1);
    });

    it('a detach button naming neither a key nor the blanket form writes nothing', async () => {
        // The blanket form is opt-in via data-detach-all, not the ABSENCE of
        // data-key: a new call site that forgot the key would otherwise offer
        // "never auto-map" while meaning "unmap from this key", with confirm
        // text that read correctly for a decision nobody asked for.
        document.querySelector('[data-inv-detail="smith2020"]').click();
        const rogue = document.querySelector(
            '.audit-inv-detail-row [data-action="detach"][data-key]');
        rogue.removeAttribute('data-key');
        const before = fetchMock.mock.calls.length;
        rogue.click();
        const strip = document.querySelector('.inline-confirm-strip');
        expect(strip, 'a keyless verb offered a confirm — it would post detach_all')
            .toBeNull();
        expect(fetchMock.mock.calls.length).toBe(before);
    });

    it('the per-key verb sends detach_from, never the wildcard', async () => {
        document.querySelector('[data-inv-detail="smith2020"]').click();
        const perKey = document.querySelector(
            '.audit-inv-detail-row [data-action="detach"][data-key]');
        perKey.click();
        const confirm = [...document.querySelectorAll('button')]
            .find((b) => b.textContent.trim() === 'Unmap');
        expect(confirm).toBeTruthy();
        confirm.click();
        await vi.waitFor(() => {
            const call = fetchMock.mock.calls.find(
                ([u]) => String(u).includes('/api/audit/mapping'));
            expect(call).toBeTruthy();
            expect(JSON.parse(call[1].body)).toEqual({
                pdf: 'b/smith.pdf', detach_from: 'smith2020', confirm: true,
                trash_zotero_links: true,
            });
        });
    });

    it('unmap confirm offers a Zotero-trash checkbox defaulting on', async () => {
        document.querySelector('[data-inv-detail="smith2020"]').click();
        const perKey = document.querySelector(
            '.audit-inv-detail-row [data-action="detach"][data-key]');
        perKey.click();
        const strip = document.querySelector('.inline-confirm-strip');
        expect(strip).toBeTruthy();
        const cb = strip.querySelector('input[type="checkbox"]');
        expect(cb, 'unmap confirm must offer the trash checkbox').toBeTruthy();
        expect(cb.checked).toBe(true);
        cb.checked = false;
        const confirm = [...strip.querySelectorAll('button')]
            .find((b) => b.textContent.trim() === 'Unmap');
        confirm.click();
        await vi.waitFor(() => {
            const call = fetchMock.mock.calls.find(
                ([u]) => String(u).includes('/api/audit/mapping'));
            expect(call).toBeTruthy();
            expect(JSON.parse(call[1].body)).toEqual({
                pdf: 'b/smith.pdf', detach_from: 'smith2020', confirm: true,
                trash_zotero_links: false,
            });
        });
    });

    it('every row carries the cross-report search\'s landing anchor', () => {
        // _scrollToReportRow looks for [data-key] or [data-pdf] and otherwise
        // silently does nothing. This table had neither, so a hit in the
        // `inventory` group — the largest the corpus produces — jumped to the
        // tab and then stopped, breaking the search's stated promise that a
        // hit always names a row you can go and look at. A record with NO pdf
        // is the discriminating case: the per-PDF verbs cannot anchor it.
        const body = document.getElementById('audit-report-body');
        expect(body.querySelector('[data-key="smith2020"]'),
               'a record WITH a pdf has no search anchor').toBeTruthy();
        expect(body.querySelector('tr[data-key="bare1999"]'),
               'a PDF-less record has no search anchor — the jump is silent')
            .toBeTruthy();
    });

    it('the table sits in the house scrollport, like every other report', () => {
        // installTableControls mounts its filter strip at
        // `table.closest('.audit-table-scroll')`. With no wrapper that resolves
        // to null and the strip lands INSIDE the horizontally-scrolling region,
        // so scrolling right to reach the row's own verbs carries it away —
        // measured at 960px, from x=[283,917] to [-676,-42], entirely off
        // screen, against not_in_zotero as the control where it did not move.
        // The wrapper must stay INSIDE .audit-inv-main or the .has-preview
        // grid column (the [view] side panel) breaks.
        const table = document.querySelector('#audit-report-body table.audit-table');
        const scroll = table.closest('.audit-table-scroll');
        expect(scroll, 'the inventory table has no .audit-table-scroll').toBeTruthy();
        expect(scroll.closest('.audit-inv-main'),
               'the scrollport must nest inside .audit-inv-main').toBeTruthy();
    });

    it('the row offers [unmap] without opening the drawer', () => {
        // The placement complaint (2026-09-07): breaking one wrong pair was a
        // two-click disclosure on a ~6 000-row table. The blanket verb stays
        // in the drawer deliberately — it also drops hand-curated matches.
        const row = document.querySelector('tr[data-key="smith2020"]');
        const unmap = row.querySelector('[data-action="detach"][data-key]');
        expect(unmap, '[unmap] is not on the row').toBeTruthy();
        expect(unmap.getAttribute('data-pdf')).toBe('b/smith.pdf');
        expect(row.querySelector('[data-detach-all]'),
               'the blanket verb must stay behind [detail]').toBeNull();
        // Sort safety: the cell sorts on the count, not on this markup.
        expect(unmap.closest('td').getAttribute('data-sort-value')).toBe('1');
    });

    it('the pdf-link verb sends KEYS ONLY, never a filename', async () => {
        // The filenames come from the cached scan's PDF↔bib join server-side.
        // A client that could name the file could point a note at any file;
        // the request body is what makes that impossible.
        document.querySelector('[data-inv-detail="smith2020"]').click();
        const btn = document.querySelector('.audit-inv-detail-row [data-inv-pdflink]');
        expect(btn, '[write these into the note] missing').toBeTruthy();
        btn.click();
        const confirm = [...document.querySelectorAll('button')]
            .find((b) => b.textContent.trim() === 'Write the link');
        expect(confirm, 'the confirm strip never appeared — the verb is unwired').toBeTruthy();
        confirm.click();
        await vi.waitFor(() => {
            const call = fetchMock.mock.calls.find(
                ([u]) => String(u).includes('/api/tagging/apply-pdf-links'));
            expect(call).toBeTruthy();
            expect(JSON.parse(call[1].body)).toEqual(
                { citation_keys: ['smith2020'], confirm: true });
        });
    });

    it('offers no pdf-link verb for a record with no note', async () => {
        document.querySelector('[data-inv-detail="bare1999"]').click();
        expect(document.querySelector('.audit-inv-detail-row [data-inv-pdflink]'))
            .toBeNull();
    });

    it('an absent-surface record reads "none", never an empty cell', async () => {
        document.querySelector('[data-inv-detail="bare1999"]').click();
        const drawer = document.querySelector('.audit-inv-detail-row');
        // The drawer is a definition GRID since the 2026-08-19 UX pass (it was
        // a run of <p>s): assert per facet, which also pins that every facet
        // still has a label — an unlabelled value is the wall it replaced.
        expect(facet(drawer, 'PDFs')).toContain('none');
        expect(facet(drawer, 'Zotero')).toContain('no item');
        expect(facet(drawer, 'Note')).toContain('none');
    });

    // UX pass (2026-08-19): the preview used to be inserted as the report
    // body's FIRST child — a 65vh block that pushed every row off screen, so
    // "look at this pair" and "keep my place in the list" were mutually
    // exclusive. It is now the second child of a layout container, which the
    // wide-window grid lays out as a side panel (and narrow windows stack, as
    // before). Pinned structurally: the CSS side is visual QA, the DOM
    // ORDER + the has-preview hook are what the stylesheet keys on.
    it('the preview mounts beside the table (layout hook), never above it', async () => {
        fetchMock.mockImplementation(async (url) => {
            if (String(url).includes('/api/audit/pdf')) {
                return { ok: true, status: 200, blob: async () => new Blob(['x']) };
            }
            return jsonOk({ ok: true });
        });
        const viewBtn = document.querySelector('[data-inv-view="b/smith.pdf"]');
        viewBtn.click();
        await vi.waitFor(() => {
            expect(document.querySelector('#audit-inv-pdf iframe')).toBeTruthy();
        });
        const layout = document.querySelector('.audit-inv-layout');
        expect(layout, 'the table must sit in a layout container').toBeTruthy();
        expect(layout.classList.contains('has-preview')).toBe(true);
        const kids = [...layout.children];
        expect(kids[0].className).toContain('audit-inv-main');
        expect(kids[1].id).toBe('audit-inv-pdf');
        // …and the close bar is the sticky one, so it stays reachable.
        expect(document.querySelector('#audit-inv-pdf .audit-inv-pdf-bar')).toBeTruthy();
        viewBtn.click();
        await vi.waitFor(() => {
            expect(document.getElementById('audit-inv-pdf')).toBeNull();
        });
        expect(document.querySelector('.audit-inv-layout').classList
            .contains('has-preview')).toBe(false);
    });

    it('[open in Zotero] deep-links with the GROUP routing', async () => {
        document.querySelector('[data-inv-detail="smith2020"]').click();
        const z = document.querySelector('[data-inv-zotero="ZK1"]');
        expect(z).toBeTruthy();
        z.click();
        await vi.waitFor(() => {
            const call = fetchMock.mock.calls.find(
                ([u]) => String(u).includes('/api/audit/reveal'));
            expect(call).toBeTruthy();
            expect(JSON.parse(call[1].body)).toEqual({
                zotero_key: 'ZK1', zotero_group_id: 484650,
            });
        });
    });
});

// The pair drawer is injected UNDER its row after installTableControls has
// snapshotted dataRows, so it is invisible to the sort unless it is marked as
// a row attachment. Measured before the fix: a descending sort left the drawer
// at the TOP of the table, adjacent to an unrelated pair — while carrying
// [unmap], [never auto-map] and the pdf:-link verb, all of which act on the
// pair it came from. Same class as the 2026-08-30 notesync panels.
//
// TEST TRAPS, both of which made an earlier version of this file vacuously
// green: installTableControls has minRows = 8 (a smaller fixture installs no
// controls at all), and the sort is bound to a BUTTON inside the <th>, never
// to the <th> itself.
describe('audit.js — the pair drawer travels with its row', () => {
    const mk = (k, y) => ({
        citation_key: k, year: y, first_author: 'A', title: 'T',
        entry_type: 'article', has_bib_entry: true, has_zotero_item: false,
        has_obsidian_note: false, has_zotero_child_note: false, pdf_count: 1,
        pdf_paths: [{ abs: `/v/${k}.pdf`, rel: `b/${k}.pdf`, name: `${k}.pdf`, source: 'doi' }],
        note: null, zotero: null, annotations_count_max: -1, finder_tags: [],
        match_sources: ['doi'], obs_tags: [], zotero_note_tags: [],
        zotero_parent_tags: [], bib_keywords: [],
    });
    const MANY = Array.from({ length: 10 }, (_, i) =>
        mk(`k${String(i).padStart(2, '0')}x`, String(1990 + i)));

    beforeEach(async () => {
        vi.resetModules();
        document.body.innerHTML = DOM;
        global.URL.createObjectURL = vi.fn(() => 'blob:x');
        global.URL.revokeObjectURL = vi.fn();
        global.fetch = vi.fn(async (u) => (String(u).includes('/api/audit/inventory')
            ? jsonOk({ records: MANY, summary: {} }) : jsonOk({ ok: true })));
        const m = await import('../../static/js/audit.js');
        await m.selectAuditReport('inventory');
        await vi.waitFor(() => expect(document.querySelector('[data-inv-detail]')).toBeTruthy());
    });
    afterEach(() => { document.body.innerHTML = ''; vi.restoreAllMocks(); });

    const rowLabels = () => [...document.querySelectorAll('#audit-report-body tbody tr')]
        .map((r) => (r.classList.contains('audit-inv-detail-row')
            ? '>>DRAWER' : r.cells[0].textContent));

    it('a sort keeps the drawer directly under the pair it describes', () => {
        document.querySelector('[data-inv-detail="k00x"]').click();
        const sortBtn = document.querySelector('#audit-report-body th button');
        expect(sortBtn, 'the sort is bound to a button INSIDE the th').toBeTruthy();
        sortBtn.click();          // asc
        sortBtn.click();          // desc
        const labels = rowLabels();
        const i = labels.indexOf('>>DRAWER');
        expect(i, 'the drawer left the table').toBeGreaterThan(0);
        expect(labels[i - 1],
               'the drawer was stranded beside an unrelated pair').toBe('k00x');
    });

    it('a filter matching nothing hides the drawer with its row', async () => {
        document.querySelector('[data-inv-detail="k00x"]').click();
        const input = document.querySelector('#audit-report-body input.table-filter');
        expect(input, 'no filter box — is the fixture above minRows?').toBeTruthy();
        input.value = 'zzzznomatch';
        input.dispatchEvent(new Event('input', { bubbles: true }));
        // installTableControls hides with the `hidden` PROPERTY, not a style.
        await vi.waitFor(() => {
            const drawer = document.querySelector('.audit-inv-detail-row');
            expect(drawer && drawer.hidden,
                   'a stranded drawer stayed on screen with write verbs in it')
                .toBe(true);
        });
    });
});
