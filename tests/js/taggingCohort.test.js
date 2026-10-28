// Pins the tagging view's tag-depth column and cohort filter (tagging.js).
//
// The load-bearing property is AGREEMENT: the number in the "#" column, the
// cohort the "Tags" selector narrows to, the deck's contents, and what
// `papermind tagging batch --max-tags N` will process must all be the same
// set. `tests/test_tag_depth_cohorts.py` pins the Python half against the
// dashboard buckets; this pins the JS half against the same definition —
// union across the surfaces a record HAS, case-folded, `#` stripped.
//
// Also guarded: the filter is a VIEW filter (re-render, never re-fetch —
// unlike `scope`, which changes what the server returns), and an empty result
// says which of the two possible causes applies.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { taggingRenderInto } from '../../static/js/tagging.js';

function jsonOk(payload) {
    return { ok: true, status: 200, json: async () => payload };
}

function row(key, { finder = [], note = null, zotero = null,
                    has_pdf = true, year = '2020' } = {}) {
    return {
        citation_key: key,
        year,
        title: `Title ${key}`,
        has_pdf,
        pdf_paths: has_pdf ? [{ abs: `/v/${key}.pdf`, rel: `${key}.pdf`, name: `${key}.pdf` }] : [],
        finder_tags: has_pdf ? finder : [],
        note: note ? { present: true, rel: `${key}.md`, tags: note }
                   : { present: false, rel: null, tags: [] },
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
let calls;

async function flush() {
    for (let i = 0; i < 5; i += 1) {
        await new Promise((resolve) => { setTimeout(resolve, 0); });
    }
}

async function render(rows) {
    calls = [];
    vi.stubGlobal('fetch', vi.fn(async (url) => {
        const u = String(url);
        calls.push(u);
        if (u.includes('/api/tagging/inventory')) {
            return jsonOk({ rows, count: rows.length, scope: 'with_pdf', pdfless_count: 0 });
        }
        if (u.includes('/api/tagging/vocab')) return jsonOk({ vocab: [] });
        if (u.includes('/api/tagging/zotero-available')) return jsonOk({ available: true, writable: true });
        return jsonOk({});
    }));
    await taggingRenderInto(
        document.getElementById('audit-report-body'),
        document.getElementById('audit-report-meta'),
    );
    await flush();
    // tagging.js reads `_view`/`_scope`/`_cohort` from localStorage ONCE at
    // module load and then keeps them in module state, so a test that flips
    // one through the UI leaks it into every later test in this file (the
    // documented taggingScope trap). Drive the control back to a known
    // starting point rather than seeding storage, which would be inert.
    const sel = document.getElementById('tagging-cohort');
    if (sel && sel.value !== 'all') {
        sel.value = 'all';
        sel.dispatchEvent(new Event('change'));
        await flush();
    }
}

async function useTable() {
    const btn = document.querySelector('[data-action="view-table"]');
    if (btn.getAttribute('aria-pressed') !== 'true') btn.click();
    await flush();
}

async function setCohort(value) {
    const sel = document.getElementById('tagging-cohort');
    expect(sel, 'cohort selector missing').toBeTruthy();
    sel.value = value;
    sel.dispatchEvent(new Event('change'));
    await flush();
}

function shownKeys() {
    return [...document.querySelectorAll('#tagging-view-host tbody tr')]
        .map((tr) => tr.getAttribute('data-key'));
}

function depthCells() {
    return [...document.querySelectorAll('#tagging-view-host .tagging-depth')]
        .map((td) => td.textContent.trim());
}

// 0, 1, 2 and 4 tags respectively — the four cohorts in one corpus.
const CORPUS = [
    row('none2020'),
    row('one2021', { note: ['dep'] }),
    row('two2022', { note: ['dep'], zotero: ['scz'] }),
    row('many2023', { note: ['a', 'b'], zotero: ['c', 'd'] }),
];

describe('tagging tag-depth column and cohort filter', () => {
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

    it('counts the union across the surfaces a record has', async () => {
        await render(CORPUS);
        await useTable();
        expect(depthCells()).toEqual(['0', '1', '2', '4']);
    });

    it('counts a tag shared by two surfaces once, case-folded', async () => {
        await render([row('a2020', { finder: ['#Dep'], note: ['dep'], zotero: ['DEP'] })]);
        await useTable();
        expect(depthCells()).toEqual(['1']);
    });

    it('does not count a surface the record does not have', async () => {
        // No note and no Zotero item: only the one Finder tag counts, and the
        // absent surfaces must not read as "zero tags there".
        await render([row('a2020', { finder: ['dep'] })]);
        await useTable();
        expect(depthCells()).toEqual(['1']);
    });

    it('ignores Finder tags on a row with no PDF', async () => {
        await render([row('a2020', { has_pdf: false, finder: ['dep'], note: ['scz'] })]);
        await useTable();
        expect(depthCells()).toEqual(['1']);
    });

    it('folds NFD against NFC, and every leading hash', async () => {
        // Mirrors audit/engine/normalize.py::fold_tag. macOS stores Finder
        // tags decomposed while the note YAML is composed, so without
        // normalize('NFC') the same accented tag counts twice; and the Python
        // side uses lstrip('#'), so '##dep' must not survive here as '#dep'.
        const nfd = 'de\u0301p';          // d + e + combining acute
        const nfc = 'd\u00e9p';           // d + e-acute
        expect(nfd).not.toBe(nfc);
        await render([row('a2020', { finder: [nfd], note: [nfc], zotero: ['##' + nfc] })]);
        await useTable();
        expect(depthCells()).toEqual(['1']);
    });

    it('the "2 or fewer" cohort is exactly buckets 0, 1 and 2', async () => {
        await render(CORPUS);
        await useTable();
        await setCohort('under');
        expect(shownKeys()).toEqual(['none2020', 'one2021', 'two2022']);
    });

    it('the "none" cohort is exactly the zero-tag papers', async () => {
        await render(CORPUS);
        await useTable();
        await setCohort('untagged');
        expect(shownKeys()).toEqual(['none2020']);
    });

    it('filters without re-fetching, unlike the scope toggle', async () => {
        await render(CORPUS);
        await useTable();
        calls.length = 0;
        await setCohort('under');
        expect(calls.filter((u) => u.includes('/api/tagging/inventory'))).toHaveLength(0);
        expect(shownKeys()).toHaveLength(3);
    });

    it('persists the cohort choice', async () => {
        await render(CORPUS);
        await useTable();
        await setCohort('done');
        expect(store.get('tagging_cohort')).toBe('done');
    });

    it('sorts by the true count, not the rendered text', async () => {
        await render(CORPUS);
        await useTable();
        const cells = [...document.querySelectorAll('#tagging-view-host .tagging-depth')];
        // data-sort-value is what installTableControls sorts on; without it a
        // 10-tag paper would sort between 1 and 2.
        expect(cells.map((c) => c.getAttribute('data-sort-value')))
            .toEqual(['0', '1', '2', '4']);
    });

    it('the deck honours the same cohort as the table', async () => {
        await render(CORPUS);
        await setCohort('untagged');
        // Deck view: one card at a time, and it must be drawn from the same
        // filtered set — a deck showing papers the table hides would make the
        // filter a lie.
        const host = document.getElementById('tagging-view-host');
        expect(host.textContent).toContain('none2020');
        expect(host.textContent).not.toContain('many2023');
    });

    it('an empty cohort says the filter caused it, not the scan', async () => {
        await render([row('many2023', { note: ['a', 'b', 'c'] })]);
        await useTable();
        await setCohort('untagged');
        const host = document.getElementById('tagging-view-host');
        expect(host.textContent).toContain('tag cohort');
        expect(host.textContent).not.toContain('last scan');
    });
});
