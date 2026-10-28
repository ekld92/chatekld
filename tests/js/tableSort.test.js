// The shared table sort + filter primitive (ui.js::installTableControls).
//
// It exists because the maintainer wanted to reorder and narrow the audit
// report tables A→Z, and because building that per table would have forked
// the behaviour sixteen ways. What this file pins is not the feature so much
// as the two decisions that make it safe:
//
//   1. IT MOVES NODES, IT DOES NOT RE-RENDER. Every audit/tagging table is
//      painted from a rows array by a pure string builder, so the obvious
//      design is "sort the array, re-render". That would destroy per-row
//      state living only in the DOM — a ticked selection checkbox, and worse,
//      the half-typed tag in the tagging table's <input>. Reordering the
//      existing <tr> nodes preserves bindings, checkboxes and carets by
//      construction, so there is no rehydration step to get wrong.
//   2. FILTERING NEVER RE-RENDERS EITHER (rows are hidden), so it cannot
//      re-enter the owner's seeding/wiring passes on every keystroke.
//
//   Plus: accent-folded comparison on BOTH sides (bilingual FR/EN vault —
//   mirrors audit/search.py::fold), numeric-aware columns, AND-of-tokens
//   filtering with a disclosed count, aria-sort, and the wire-once dataset
//   marker (a module flag stays true after the node is replaced — the audit
//   search-bar defect class).
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { installTableControls, foldText } from '../../static/js/ui.js';

const HERE = dirname(fileURLToPath(import.meta.url));

const DOM = `
  <div id="sr-status-announcer" role="status"></div>
  <div id="host"></div>
`;

/** A table with an author column (accents), a year column (numeric) and a
 *  verb column whose <th> is empty — deliberately not sortable. */
function table(rows) {
    const trs = rows.map((r) => `
        <tr data-key="${r.key}">
            <td><input type="checkbox" class="pick" data-key="${r.key}"></td>
            <td>${r.author}</td>
            <td>${r.year}</td>
            <td><button type="button">[open]</button></td>
        </tr>`).join('');
    return `<table class="audit-table"><thead><tr>
            <th scope="col"><span class="sr-only">Select</span></th>
            <th scope="col">Author</th>
            <th scope="col">Year</th>
            <th scope="col"></th>
        </tr></thead><tbody>${trs}</tbody></table>`;
}

const ROWS = [
    { key: 'k1', author: 'Zola', year: 1885 },
    { key: 'k2', author: 'Éluard', year: 1926 },
    { key: 'k3', author: 'Duras', year: 1984 },
    { key: 'k4', author: 'Camus', year: 1942 },
    { key: 'k5', author: 'Beauvoir', year: 1949 },
    { key: 'k6', author: 'Aragon', year: 2001 },
    { key: 'k7', author: 'Perec', year: 1978 },
    { key: 'k8', author: 'Ernaux', year: 2008 },
    { key: 'k9', author: '', year: 1900 },
];

function mount(rows = ROWS, opts = {}) {
    document.body.innerHTML = DOM;
    document.getElementById('host').innerHTML = table(rows);
    const t = document.querySelector('table.audit-table');
    const ctl = installTableControls(t, opts);
    return { t, ctl };
}

const authorsInOrder = () => [...document.querySelectorAll('tbody tr')]
    .filter((tr) => !tr.hidden && !tr.classList.contains('table-filter-empty'))
    .map((tr) => tr.cells[1].textContent);

const sortBtn = (label) => [...document.querySelectorAll('th .th-sort')]
    .find((b) => b.textContent.includes(label));

describe('foldText', () => {
    it('folds accents and case, both sides of every comparison', () => {
        expect(foldText('Éluard')).toBe('eluard');
        expect(foldText('DÉPRESSION')).toBe('depression');
        expect(foldText(null)).toBe('');
    });
});

describe('installTableControls — sort', () => {
    beforeEach(() => { mount(); });

    it('sorts A→Z with accents folded ("Éluard" lands under E, not last)', () => {
        sortBtn('Author').click();
        expect(authorsInOrder()).toEqual([
            'Aragon', 'Beauvoir', 'Camus', 'Duras', 'Éluard', 'Ernaux',
            'Perec', 'Zola',
            '',   // blanks sink
        ]);
    });

    it('a second click reverses, and blanks still sink', () => {
        const b = sortBtn('Author');
        b.click();
        b.click();
        const order = authorsInOrder();
        expect(order[0]).toBe('Zola');
        // "no value" is not the smallest value: a blank must not float to the
        // top on a descending sort and bury the rows the user asked to see.
        expect(order[order.length - 1]).toBe('');
    });

    it('a numeric column sorts numerically, not lexically', () => {
        // Lexical order would put 1885 < 1900 < 1926 … which agrees here, so
        // use a column where the two orders genuinely differ.
        mount([
            { key: 'a', author: 'A', year: 9 },
            { key: 'b', author: 'B', year: 100 },
            { key: 'c', author: 'C', year: 25 },
            { key: 'd', author: 'D', year: 3 },
            { key: 'e', author: 'E', year: 40 },
            { key: 'f', author: 'F', year: 7 },
            { key: 'g', author: 'G', year: 88 },
            { key: 'h', author: 'H', year: 1 },
        ]);
        sortBtn('Year').click();
        const years = [...document.querySelectorAll('tbody tr')]
            .map((tr) => Number(tr.cells[2].textContent));
        expect(years).toEqual([1, 3, 7, 9, 25, 40, 88, 100]);
    });

    it('toggles aria-sort on the header, and only one header at a time', () => {
        const ths = [...document.querySelectorAll('th')];
        const author = ths[1];
        const year = ths[2];
        expect(author.getAttribute('aria-sort')).toBe('none');
        sortBtn('Author').click();
        expect(author.getAttribute('aria-sort')).toBe('ascending');
        sortBtn('Author').click();
        expect(author.getAttribute('aria-sort')).toBe('descending');
        sortBtn('Year').click();
        expect(year.getAttribute('aria-sort')).toBe('ascending');
        expect(author.getAttribute('aria-sort')).toBe('none');
    });

    it('a header with no visible text of its own is not sortable', () => {
        const ths = [...document.querySelectorAll('th')];
        // The select column carries only an .sr-only label; the verb column
        // is empty. Neither has anything to order by.
        expect(ths[0].querySelector('.th-sort')).toBeNull();
        expect(ths[3].querySelector('.th-sort')).toBeNull();
        expect(ths[0].hasAttribute('aria-sort')).toBe(false);
    });

    it('SELECTION SURVIVES A SORT — the nodes move, they are not rebuilt', () => {
        const cb = document.querySelector('.pick[data-key="k1"]');
        cb.checked = true;
        const marker = {};
        cb.addEventListener('click', () => { marker.clicked = true; });
        sortBtn('Author').click();
        const after = document.querySelector('.pick[data-key="k1"]');
        expect(after, 'the row must survive the sort').toBeTruthy();
        expect(after).toBe(cb);            // the SAME node, moved
        expect(after.checked).toBe(true);  // …so its state came with it
        after.click();
        expect(marker.clicked, 'listeners must survive too').toBe(true);
    });
});

describe('installTableControls — filter', () => {
    beforeEach(() => { mount(); });

    it('narrows accent-insensitively and discloses the count', async () => {
        const input = document.querySelector('input.table-filter');
        input.value = 'eluard';
        input.dispatchEvent(new Event('input'));
        await vi.waitFor(() => {
            expect(authorsInOrder()).toEqual(['Éluard']);
        });
        expect(document.querySelector('.table-filter-count').textContent)
            .toBe('1 of 9 rows');
    });

    it('ANDs its tokens', async () => {
        const input = document.querySelector('input.table-filter');
        input.value = 'camus 1942';
        input.dispatchEvent(new Event('input'));
        await vi.waitFor(() => expect(authorsInOrder()).toEqual(['Camus']));
        input.value = 'camus 1949';
        input.dispatchEvent(new Event('input'));
        await vi.waitFor(() => expect(authorsInOrder()).toEqual([]));
    });

    it('says so when nothing matches, instead of showing a bare header', async () => {
        const input = document.querySelector('input.table-filter');
        input.value = 'zzzz';
        input.dispatchEvent(new Event('input'));
        await vi.waitFor(() => {
            expect(document.querySelector('.table-filter-empty')).toBeTruthy();
        });
        expect(document.querySelector('.table-filter-empty').textContent)
            .toContain('No rows match');
    });

    it('does not match a row on its VERB labels', async () => {
        // Every row carries an [open] button; matching its text would make
        // "open" select the whole table — the filter searches data.
        const input = document.querySelector('input.table-filter');
        input.value = 'open';
        input.dispatchEvent(new Event('input'));
        await vi.waitFor(() => expect(authorsInOrder()).toEqual([]));
    });

    it('searches the FULL value of a truncated .cell-title, not just what fits', async () => {
        // The house pattern for a clipped cell is a visible slice plus the
        // whole value in `title`. Matching only the visible text answers "no
        // match" for a word that IS in the row — silently wrong, not merely
        // missing.
        document.body.innerHTML = DOM;
        document.getElementById('host').innerHTML = `
            <table class="audit-table"><thead><tr>
                <th scope="col">Key</th><th scope="col">Title</th>
            </tr></thead><tbody>
            ${ROWS.map((r, i) => `<tr>
                <td>${r.key}</td>
                <td class="cell-title" title="A very long title whose ${i === 0 ? 'sesquipedalian' : 'ordinary'} tail is clipped">A very long title whose…</td>
            </tr>`).join('')}
            </tbody></table>`;
        installTableControls(document.querySelector('table.audit-table'));
        const input = document.querySelector('input.table-filter');
        input.value = 'sesquipedalian';
        input.dispatchEvent(new Event('input'));
        await vi.waitFor(() => {
            const shown = [...document.querySelectorAll('tbody tr')]
                .filter((tr) => !tr.hidden && !tr.classList.contains('table-filter-empty'));
            expect(shown.length).toBe(1);
            expect(shown[0].cells[0].textContent).toBe('k1');
        });
    });

    it('an empty query restores every row and drops the count', async () => {
        const input = document.querySelector('input.table-filter');
        input.value = 'camus';
        input.dispatchEvent(new Event('input'));
        await vi.waitFor(() => expect(authorsInOrder().length).toBe(1));
        input.value = '';
        input.dispatchEvent(new Event('input'));
        await vi.waitFor(() => expect(authorsInOrder().length).toBe(9));
        expect(document.querySelector('.table-filter-count').textContent).toBe('');
    });

    it('a filtered-out row keeps its selection (it is hidden, not dropped)', async () => {
        const cb = document.querySelector('.pick[data-key="k4"]');   // Camus
        cb.checked = true;
        const input = document.querySelector('input.table-filter');
        input.value = 'zola';
        input.dispatchEvent(new Event('input'));
        await vi.waitFor(() => expect(authorsInOrder()).toEqual(['Zola']));
        expect(document.querySelector('.pick[data-key="k4"]').checked).toBe(true);
    });
});

describe('installTableControls — installation discipline', () => {
    it('is wire-once via a DATASET MARKER on the table, not a module flag', () => {
        const { t } = mount();
        expect(t.dataset.tableControls).toBe('1');
        expect(installTableControls(t)).toBeNull();
        expect(document.querySelectorAll('.table-controls').length).toBe(1);
        // A replaced node is a NEW table and must wire again — the exact case
        // a module-scoped "already wired" boolean gets wrong.
        document.getElementById('host').innerHTML = table(ROWS);
        const fresh = document.querySelector('table.audit-table');
        expect(installTableControls(fresh)).not.toBeNull();
    });

    it('skips a table too small to be worth narrowing', () => {
        const { ctl } = mount(ROWS.slice(0, 3));
        expect(ctl).toBeNull();
        expect(document.querySelector('.table-controls')).toBeNull();
    });

    it('restores a caller-held sort/filter state after the owner re-renders', async () => {
        const seen = [];
        mount(ROWS, { onState: (st) => seen.push(st) });
        sortBtn('Author').click();
        expect(seen[seen.length - 1]).toMatchObject({ dir: 'asc' });
        // The owner repaints (an accession write does this on every click) and
        // hands the state back.
        const carried = seen[seen.length - 1];
        document.getElementById('host').innerHTML = table(ROWS);
        installTableControls(document.querySelector('table.audit-table'),
            { initial: carried });
        expect(authorsInOrder()[0]).toBe('Aragon');
        expect(document.querySelectorAll('th')[1].getAttribute('aria-sort'))
            .toBe('ascending');
    });
});

describe('source ratchet', () => {
    const read = (f) => readFileSync(resolve(HERE, '../../static/js', f), 'utf8');

    it('the primitive lives in ui.js — consumers only call it', () => {
        expect(read('ui.js')).toContain('export function installTableControls');
        for (const f of ['audit.js', 'tagging.js']) {
            const src = read(f);
            expect(src, `${f} must import the primitive`)
                .toMatch(/import \{[^}]*installTableControls[^}]*\} from '\.\/ui\.js'/s);
            expect(src, `${f} must not fork its own sort`)
                .not.toContain('function installTableControls');
        }
    });

    it('folds the same way audit/search.py does (NFD, drop marks, casefold)', () => {
        const py = readFileSync(resolve(HERE, '../../audit/search.py'), 'utf8');
        expect(py).toContain('NFD');
        expect(read('ui.js')).toContain("normalize('NFD')");
    });
});

describe('installTableControls — injected detail rows (data-row-attachment)', () => {
    // Regression, 2026-08-29. `dataRows` is snapshotted at install time, so a
    // row injected LATER (audit.js's Zotero-region diff and create preview)
    // was invisible to both sort and filter. Sorting appendChild-moves every
    // data row to the END of the tbody; the panel stayed put and floated to
    // the top, detached from its own note and sitting under an unrelated row
    // whose [update] button it then appeared to describe. Filtering never hid
    // it, so a query matching nothing still left a stranded diff on screen.

    /** Attach a panel under the row for `key`, as audit.js does. */
    const attach = (key, text) => {
        const parent = document.querySelector(`tbody tr[data-key="${key}"]`);
        const tr = document.createElement('tr');
        tr.setAttribute('data-row-attachment', '');
        const td = document.createElement('td');
        td.colSpan = 4;
        td.textContent = text;
        tr.appendChild(td);
        parent.after(tr);
        return { parent, tr };
    };
    const filterInput = () => document.querySelector('.table-filter');
    const countText = () => document.querySelector('.table-filter-count').textContent;
    const type = async (value) => {
        const input = filterInput();
        input.value = value;
        input.dispatchEvent(new Event('input'));
        await new Promise((r) => setTimeout(r, 260));   // past the debounce
    };

    it('stays immediately under its own row after a sort', () => {
        mount();
        const { parent, tr } = attach('k1', 'diff for Zola');
        sortBtn('Author').click();
        expect(parent.nextElementSibling).toBe(tr);
        // Not vacuous: the sort really did reorder the table. Asserted on the
        // DATA rows only — `authorsInOrder` maps cells[1] over every tbody row
        // and the panel has a single colspan cell.
        const dataKeys = [...document.querySelectorAll('tbody tr[data-key]')]
            .map((tr) => tr.getAttribute('data-key'));
        expect(dataKeys[0]).toBe('k6');          // Aragon leads A→Z
        expect(dataKeys[dataKeys.length - 1]).toBe('k9');   // blank sinks last
    });

    it('survives a second sort in the other direction', () => {
        mount();
        const { parent, tr } = attach('k3', 'diff for Duras');
        sortBtn('Author').click();
        sortBtn('Author').click();
        expect(parent.nextElementSibling).toBe(tr);
    });

    it('is hidden and shown with its parent by the filter', async () => {
        mount();
        const { parent, tr } = attach('k1', 'diff for Zola');
        await type('Camus');
        expect(parent.hidden).toBe(true);
        expect(tr.hidden).toBe(true);
        await type('');
        expect(parent.hidden).toBe(false);
        expect(tr.hidden).toBe(false);
    });

    it('never matches the filter on its own content', async () => {
        // The panel holds a whole diff; matching its text would surface a row
        // whose own data does not match the query at all.
        mount();
        attach('k1', 'supercalifragilistic');
        await type('supercalifragilistic');
        expect(countText()).toMatch(/^0 of 9\b/);
    });

    it('is not counted as a data row when present at install time', async () => {
        mount();
        // Re-install on a table that already carries a panel.
        const t = document.querySelector('table.audit-table');
        t.removeAttribute('data-table-controls');
        document.querySelector('.table-controls').remove();
        attach('k1', 'pre-existing panel');
        installTableControls(t, {});
        await type('zzzzzz');
        expect(countText()).toMatch(/of 9\b/);          // 9 data rows, not 10
    });
});
