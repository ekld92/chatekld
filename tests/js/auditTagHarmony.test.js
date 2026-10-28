// Pins the Tag Harmony report's rendering (audit.js, 2026-08-13).
//
// The table's job is to state, per paper, what a harmonize would write. Three
// things must survive any future edit to it:
//
//   1. THREE DISTINCT CELL STATES. "the surface does not exist here", "it
//      exists and is in sync" and "it is missing these tags" are different
//      facts. Rendering the first two the same way (an empty cell) tells a
//      user with no Zotero item that there is nothing to do, which is true by
//      accident and false in general.
//   2. THE ID THAT WOULD BE WRITTEN, not the surface that was found. Writes
//      are gated on the literal `_tags.md` id while the report compares by a
//      spelling-insensitive fingerprint, so the two differ exactly when an
//      accent or separator is involved.
//   3. UNAPPLYABLE TAGS BADGED. A tag absent from `_tags.md` cannot be
//      written at all; listing it silently promises a write that never lands.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const jsonOk = (payload) => ({ ok: true, status: 200, json: async () => payload });

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
  <div id="audit-status-banner" style="display:none;"></div>
  <div id="sr-status-announcer" role="status"></div>
`;

function row(over = {}) {
    return {
        citation_key: 'smith2020',
        author: 'Smith, J',
        title: 'A paper',
        present: ['note', 'finder', 'zotero'],
        note_tags: [], finder_tags: [], zotero_tags: [],
        union: ['depression'],
        missing_in_note: [],
        missing_in_finder: ['depression'],
        missing_in_zotero: ['depression'],
        variants: [],
        vocab_ids: { depression: 'depression' },
        write_count: 2,
        applyable_count: 2,
        tagged_surface_count: 1,
        note_rel: 'Z_Zotero_Notes/smith2020.md',
        pdf_count: 1,
        ...over,
    };
}

function report(over = {}) {
    const rows = over.rows || [row()];
    return {
        name: 'tag_harmony',
        rows,
        count: rows.length,
        write_count: 2,
        applyable_count: 2,
        single_surface_count: 1,
        vocab_available: true,
        ...over,
    };
}

async function render(payload) {
    vi.resetModules();
    vi.stubGlobal('fetch', vi.fn(async (url) =>
        String(url).includes('/api/audit/summary')
            ? { ok: false, status: 404, json: async () => ({ error: 'none' }) }
            : String(url).includes('/api/tagging/zotero-available')
                ? jsonOk({ transport: 'local', writable: true, note: 'Desktop authorized.' })
            : jsonOk(payload)));
    const { selectAuditReport } = await import('../../static/js/audit.js');
    await selectAuditReport('tag_harmony');
}

const bodyText = () => document.getElementById('audit-report-body').textContent;
const metaText = () => document.getElementById('audit-report-meta').textContent;
const cells = () => [...document.querySelectorAll('#audit-report-body tbody td')];

beforeEach(() => {
    document.body.innerHTML = DOM;
    const store = new Map();
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

describe('Tag Harmony — cell states', () => {
    it('distinguishes absent, in-sync and missing', async () => {
        await render(report({
            rows: [row({
                present: ['note', 'finder'],       // no Zotero item at all
                missing_in_note: [],               // in sync
                missing_in_finder: ['depression'], // missing
                missing_in_zotero: [],
            })],
        }));
        // cells: [0] select, [1] key, [2] author, [3] note, [4] finder, [5] zotero.
        const [, , , note, finder, zotero] = cells();
        expect(note.textContent.trim()).toBe('✓');
        expect(finder.textContent).toContain('depression');
        expect(zotero.textContent.trim()).toBe('—');
        // The absent surface must not read as "in sync".
        expect(zotero.textContent.trim()).not.toBe('✓');
    });

    it('says the merge is additive, in the meta line', async () => {
        await render(report());
        expect(metaText()).toContain('additive');
        expect(metaText()).toContain('nothing is ever removed');
    });

    it('headers say "Add to …", never the "→" the 2026-08-19 review found misread', async () => {
        // "→ Finder" was read as "tags IN Finder" — the exact inversion of
        // what the cell holds (the tags a harmonize would ADD there).
        await render(report());
        const heads = [...document.querySelectorAll('#audit-report-body th')]
            .map(th => th.textContent.trim());
        expect(heads).toContain('Add to note');
        expect(heads).toContain('Add to Finder');
        expect(heads).toContain('Add to Zotero');
        expect(heads.join(' ')).not.toContain('→');
        expect(metaText()).toContain('missing');
    });

    it('every surface cell discloses its CURRENT tags in a tooltip', async () => {
        await render(report({
            rows: [row({
                present: ['note', 'finder', 'zotero'],
                note_tags: [], finder_tags: [], zotero_tags: ['depression'],
                missing_in_note: ['depression'],
                missing_in_finder: ['depression'],
                missing_in_zotero: [],
            })],
        }));
        const [, , , note, finder, zotero] = cells();
        // The missing cell says what the surface holds today (nothing) —
        // the reader can see the tag is NOT there yet.
        expect(finder.getAttribute('title')).toContain('currently on finder');
        expect(finder.getAttribute('title')).toContain('(no tags)');
        // The in-sync cell names what it carries.
        expect(zotero.getAttribute('title')).toContain('currently on zotero');
        expect(zotero.getAttribute('title')).toContain('depression');
        expect(note.getAttribute('title')).toContain('currently on note');
    });

    it('reports an empty result as done, not as an error', async () => {
        await render(report({ rows: [], count: 0, write_count: 0, applyable_count: 0 }));
        expect(metaText()).toContain('Nothing to harmonize');
        expect(document.querySelector('#audit-report-body table')).toBeNull();
    });

    it('caveats the Finder column off macOS, and stays quiet on it', async () => {
        // Off darwin, Finder xattrs read empty, so every union tag looks
        // "missing in Finder" — fabricated drift the meta line must disclose
        // (2026-08-18; tag_coverage's finder_platform precedent).
        await render(report({ finder_platform: 'linux' }));
        expect(metaText()).toContain('Finder tags read empty on this platform');
        await render(report({ finder_platform: 'darwin' }));
        expect(metaText()).not.toContain('Finder tags read empty');
    });

    it('scrolls the wide table in its own container, not the whole body', async () => {
        // The toolbar, status line and post-run undo button render ABOVE the
        // 7-column table; if the table rode #audit-report-body's horizontal
        // scroll they would scroll out of view with it (2026-08-13 review).
        await render(report());
        const table = document.querySelector('#audit-report-body table');
        expect(table.parentElement.classList.contains('audit-table-scroll')).toBe(true);
        const toolbar = document.querySelector('.audit-harmony-toolbar');
        expect(toolbar.closest('.audit-table-scroll')).toBeNull();
    });
});

describe('Tag Harmony — vocabulary gating', () => {
    it('renders the id that would be written, not the surface found', async () => {
        await render(report({
            rows: [row({
                union: ['Dépression'],
                missing_in_finder: ['Dépression'],
                missing_in_zotero: [],
                vocab_ids: { 'Dépression': 'depression' },
            })],
        }));
        const finder = cells()[4];
        expect(finder.textContent).toContain('depression');
        // …and keeps the found spelling reachable rather than hiding it.
        expect(finder.querySelector('.harmony-tag').getAttribute('title'))
            .toContain('Dépression');
    });

    it('badges a tag absent from the vocabulary as unapplyable', async () => {
        await render(report({
            rows: [row({
                union: ['mystery'],
                missing_in_finder: ['mystery'],
                missing_in_zotero: [],
                vocab_ids: { mystery: null },
                applyable_count: 0,
            })],
            applyable_count: 0,
        }));
        expect(document.querySelector('.harmony-tag-novocab')).not.toBeNull();
        expect(bodyText()).toContain('not in vocab');
    });

    it('states up front when fewer tags are applyable than listed', async () => {
        await render(report({ write_count: 12, applyable_count: 3 }));
        // "12 tags to propagate" must never turn out to mean 3 at write time.
        expect(metaText()).toContain('12');
        expect(metaText()).toContain('3 applyable');
    });

    it('does not badge anything when the vocabulary failed to load', async () => {
        await render(report({ vocab_available: false, rows: [row({ vocab_ids: {} })] }));
        expect(document.querySelector('.harmony-tag-novocab')).toBeNull();
    });
});

describe('Tag Harmony — escaping', () => {
    it('escapes tag text, which comes from filesystem xattrs and Zotero', async () => {
        await render(report({
            rows: [row({
                union: ['<img src=x onerror=alert(1)>'],
                missing_in_finder: ['<img src=x onerror=alert(1)>'],
                missing_in_zotero: [],
                vocab_ids: { '<img src=x onerror=alert(1)>': null },
            })],
        }));
        expect(document.querySelector('#audit-report-body img')).toBeNull();
    });
});

describe('Tag Harmony — selection and the confirmed bulk write', () => {
    const boxes = () => [...document.querySelectorAll('input[data-harmony-key]')];
    const applyBtn = () => document.getElementById('harmony-apply-btn');

    it('disables the action until something is selected', async () => {
        await render(report());
        expect(applyBtn().disabled).toBe(true);
        boxes()[0].checked = true;
        boxes()[0].dispatchEvent(new Event('change'));
        expect(applyBtn().disabled).toBe(false);
        expect(applyBtn().textContent).toContain('(1)');
    });

    it('refuses to select a row with nothing applyable', async () => {
        // Including it would inflate the count the confirm promises and then
        // write nothing for that paper.
        await render(report({
            rows: [row({ applyable_count: 0, vocab_ids: { depression: null } })],
        }));
        expect(boxes()[0].disabled).toBe(true);
        document.querySelector('[data-harmony-all="1"]').click();
        expect(applyBtn().disabled).toBe(true);
    });

    it('select all skips unapplyable rows', async () => {
        await render(report({
            rows: [
                row({ citation_key: 'ok2020' }),
                row({ citation_key: 'dead2019', applyable_count: 0,
                      vocab_ids: { depression: null } }),
            ],
        }));
        document.querySelector('[data-harmony-all="1"]').click();
        expect(applyBtn().textContent).toContain('(1)');
    });

    it('writes nothing until the confirm is accepted', async () => {
        await render(report());
        boxes()[0].checked = true;
        boxes()[0].dispatchEvent(new Event('change'));
        fetch.mockClear();
        applyBtn().click();               // opens the confirm strip only
        const posted = fetch.mock.calls.filter((c) =>
            String(c[0]).includes('/api/tagging/harmonize'));
        expect(posted).toHaveLength(0);
    });

    it('names the scope and the direction in the confirm', async () => {
        await render(report());
        boxes()[0].checked = true;
        boxes()[0].dispatchEvent(new Event('change'));
        applyBtn().click();
        await new Promise((r) => setTimeout(r, 0));
        const strip = document.body.textContent;
        expect(strip).toContain('Additive only');
        expect(strip).toContain('1 selected paper');
        expect(strip).toContain('undoable');
    });

    it('posts only citation keys — never the tags themselves', async () => {
        // The union, its vocabulary resolution and the writes are all
        // server-side; a client that could name the tags could widen the write.
        await render(report());
        boxes()[0].checked = true;
        boxes()[0].dispatchEvent(new Event('change'));
        applyBtn().click();
        await new Promise((r) => setTimeout(r, 0));
        const confirmBtn = [...document.querySelectorAll('button')]
            .find((b) => b.textContent === 'Harmonize' && b !== applyBtn());
        expect(confirmBtn).toBeTruthy();
        confirmBtn.click();
        await new Promise((r) => setTimeout(r, 0));
        const call = fetch.mock.calls.find((c) =>
            String(c[0]).includes('/api/tagging/harmonize'));
        expect(call).toBeTruthy();
        const body = JSON.parse(call[1].body);
        expect(body.keys).toEqual(['smith2020']);
        expect(body.confirm).toBe(true);
        expect(body.tags).toBeUndefined();
    });

    it('blocks a Desktop selection under Web before confirmation or any write', async () => {
        await render(report());
        fetch.mockImplementation(async (url) => String(url).includes('/api/tagging/zotero-available')
            ? jsonOk({ transport: 'web', writable: false, note: 'Desktop selection cannot use Web.' })
            : jsonOk(report()));
        boxes()[0].checked = true;
        boxes()[0].dispatchEvent(new Event('change'));
        applyBtn().click();
        await new Promise((r) => setTimeout(r, 0));
        expect(bodyText()).toContain('Desktop selection cannot use Web.');
        expect(bodyText()).toContain('authorize Zotero Desktop');
        expect(fetch.mock.calls.some(c => String(c[0]).includes('/api/tagging/harmonize'))).toBe(false);
        expect([...document.querySelectorAll('button')].some(b => b.textContent === 'Harmonize')).toBe(false);
    });

    it('drops the selection when a new report payload arrives', async () => {
        // Rows are a snapshot; carrying a selection across a rescan would
        // re-offer keys whose gaps may already be closed.
        await render(report());
        boxes()[0].checked = true;
        boxes()[0].dispatchEvent(new Event('change'));
        expect(applyBtn().textContent).toContain('(1)');
        await render(report());
        expect(applyBtn().disabled).toBe(true);
    });
});

// Sort/filter over a report that carries per-row SELECTION state (2026-08-19).
// This is the case that decided the primitive's design: harmony rows are
// selected into `_harmonySelected` and the "Harmonize selected (N)" button
// reads that set, so a sort that re-rendered the table would have had to
// rehydrate every checkbox. Reordering the existing nodes means there is
// nothing to rehydrate.
describe('tag_harmony — sort and filter over a selectable table', () => {
    beforeEach(async () => {
        document.body.innerHTML = DOM;
        const rows = ['aaa2001', 'zzz1999', 'Éluard2010', 'mmm2005',
            'bbb2002', 'ccc2003', 'ddd2004', 'eee2006', 'fff2007']
            .map((k) => row({ citation_key: k, author: k }));
        await render(report({ rows }));
    });
    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    const keys = () => [...document.querySelectorAll('#audit-report-body tbody tr')]
        .filter((tr) => !tr.hidden && !tr.classList.contains('table-filter-empty'))
        .map((tr) => tr.cells[1].textContent);

    it('the report table gets the shared controls, ABOVE its scrollport', () => {
        const strip = document.querySelector('#audit-report-body .table-controls');
        expect(strip).toBeTruthy();
        expect(document.querySelector('#audit-report-body th .th-sort')).toBeTruthy();
        // The wide tables scroll horizontally in their own container; a strip
        // inside it would ride out of view with the columns.
        expect(strip.nextElementSibling.className).toContain('audit-table-scroll');
    });

    it('A SELECTED ROW STAYS SELECTED — and stays in the count — after a sort', async () => {
        const cb = document.querySelector('[data-harmony-key="zzz1999"]');
        cb.checked = true;
        cb.dispatchEvent(new Event('change', { bubbles: true }));
        const applyBtn = document.getElementById('harmony-apply-btn');
        await vi.waitFor(() => expect(applyBtn.textContent).toContain('(1)'));

        const sortByKey = [...document.querySelectorAll('#audit-report-body th .th-sort')]
            .find((b) => b.textContent.includes('Citation key'));
        sortByKey.click();
        // Accent-folded: Éluard2010 sorts under E, between ddd and eee.
        expect(keys()[0]).toBe('aaa2001');
        expect(keys()[keys().length - 1]).toBe('zzz1999');
        // …and the selection is untouched, in the DOM and in the module set.
        expect(document.querySelector('[data-harmony-key="zzz1999"]').checked).toBe(true);
        expect(applyBtn.textContent).toContain('(1)');
    });

    it('filtering hides rows without touching the selection', async () => {
        const cb = document.querySelector('[data-harmony-key="mmm2005"]');
        cb.checked = true;
        cb.dispatchEvent(new Event('change', { bubbles: true }));
        const input = document.querySelector('#audit-report-body input.table-filter');
        input.value = 'eluard';
        input.dispatchEvent(new Event('input'));
        await vi.waitFor(() => expect(keys()).toEqual(['Éluard2010']));
        expect(document.getElementById('harmony-apply-btn').textContent).toContain('(1)');
        expect(document.querySelector('[data-harmony-key="mmm2005"]').checked).toBe(true);
    });
});
