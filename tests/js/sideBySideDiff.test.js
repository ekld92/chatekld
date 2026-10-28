// The shared side-by-side diff (ui.js::renderSideBySideDiff).
//
// It exists so the app has ONE diff look across the two surfaces that ask
// "here is what would change to a file you own": the Zotero-region update
// preview (audit.js) and Note Refactor's detail pane (refactor.js). What this
// file pins is not the look but the four properties that make it safe to point
// at a user's vault:
//
//   1. LOSSLESSNESS. The word pass tokenises each changed line and reassembles
//      it from the tokens. If the tokenizer or the renderer dropped, reordered
//      or duplicated a character, the preview would show something other than
//      the bytes on disk — while the WYSIWYG sha256 guard downstream stayed
//      perfectly green, because it hashes the server's text, not the pixels the
//      user approved. That is a silent-wrong-preview class, so every test here
//      that builds a diff also re-derives both original texts from the DOM.
//   2. NEVER innerHTML. Both sides are vault / Zotero content.
//   3. THE WORD GATE IS THE MEASUREMENT. Over the live library's 16 actionable
//      region updates the similarity of positionally-paired replaced lines is
//      bimodal — 32 pairs >=0.9, 26 pairs <=0.1, 4 in between. Highlighting
//      words in the low mode would be confetti, so the gate must actually
//      discriminate, not merely exist.
//   4. COLOUR IS NOT THE ONLY CHANNEL (WCAG 1.4.1): every cell carries a
//      visible -/+ sign, so the state survives monochrome and reaches a screen
//      reader in the unified-diff vocabulary.
//
// Content here is SYNTHETIC but shaped like the target vault (bilingual FR/EN
// prose, markdown lists, quotes). Real note text never enters the repo — the
// same rule tests/eval/README.md states for golden sets.
import { describe, it, expect, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { renderSideBySideDiff } from '../../static/js/ui.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');

beforeEach(() => { document.body.innerHTML = ''; });

/** Render into the document so layout-free DOM queries work. */
function render(a, b, opts) {
    const el = renderSideBySideDiff(a, b, opts);
    document.body.appendChild(el);
    return el;
}

const rows = (el) => [...el.querySelectorAll('.diff-sxs-row')];
const cell = (row, side) => row.querySelector(`.diff-sxs-cell.side-${side}`);
const textOf = (row, side) => {
    const c = cell(row, side);
    const t = c && c.querySelector('.diff-sxs-text');
    return t ? t.textContent : null;
};

/** Re-derive one side's full text from the rendered DOM, in row order.
 *  This is the losslessness probe: it must equal the input exactly. */
function sideText(el, side) {
    return rows(el)
        .map((r) => textOf(r, side))
        .filter((t) => t !== null)
        .join('\n');
}

describe('alignment', () => {
    it('says so, and renders no rows, when the two texts are identical', () => {
        const el = render('same\ntext', 'same\ntext', { emptyMessage: 'Nothing to import.' });
        expect(rows(el)).toHaveLength(0);
        expect(el.querySelector('.diff-sxs-empty').textContent).toBe('Nothing to import.');
    });

    it('puts an inserted line on its own row with an empty left side', () => {
        const el = render('a\nc', 'a\nb\nc');
        const ins = rows(el).filter((r) => r.classList.contains('is-ins'));
        expect(ins).toHaveLength(1);
        expect(textOf(ins[0], 'b')).toBe('b');
        expect(cell(ins[0], 'a').classList.contains('is-void')).toBe(true);
    });

    it('puts a deleted line on its own row with an empty right side', () => {
        const el = render('a\nb\nc', 'a\nc');
        const del = rows(el).filter((r) => r.classList.contains('is-del'));
        expect(del).toHaveLength(1);
        expect(textOf(del[0], 'a')).toBe('b');
        expect(cell(del[0], 'b').classList.contains('is-void')).toBe(true);
    });

    it('pairs a changed line onto ONE row, tinted on both sides', () => {
        const el = render('BDI, HAM-D et PHQ-9 sont les plus utilises',
                          'BDI, HAM-D et PHQ-9 sont les plus frequents');
        const chg = rows(el).filter((r) => r.classList.contains('is-chg'));
        expect(chg).toHaveLength(1);
        expect(cell(chg[0], 'a').classList.contains('is-del')).toBe(true);
        expect(cell(chg[0], 'b').classList.contains('is-add')).toBe(true);
    });

    it('keeps unchanged lines on one row, untinted', () => {
        const el = render('kept\nchanged one way', 'kept\nchanged another way');
        const eq = rows(el).filter((r) => r.classList.contains('is-equal'));
        expect(eq).toHaveLength(1);
        expect(cell(eq[0], 'a').classList.contains('is-del')).toBe(false);
        expect(cell(eq[0], 'b').classList.contains('is-add')).toBe(false);
    });

    it('renders a leading blank line rather than swallowing it', () => {
        // The rendered region begins with the blank line that follows the
        // template's `---` rule, so a renderer that trimmed would misreport the
        // very first byte of every region.
        const el = render('\nfirst', '\nsecond');
        expect(sideText(el, 'a')).toBe('\nfirst');
        expect(sideText(el, 'b')).toBe('\nsecond');
    });
});

describe('losslessness — the rendered text is the input, exactly', () => {
    const CASES = [
        ['prose reworded in place',
            'meta-epidemio n=492 RCTs de 1977 a 2024\n- "we only included OMIs present in at least ten comparisons"\n\nfont pas mal de trucs',
            'meta-epidemio n=492 RCTs de 1977 a 2025\n- "we only included OMIs present in at least ten comparisons"\n\nfont beaucoup de trucs'],
        ['markdown list edited',
            '- PROM\n- ClinROM\n- ObsROM',
            '- PROM\n- ClinROM\n- ObsROM\n- PerfOM'],
        ['whitespace and punctuation only',
            'un peu   de PROM, au cours du temps',
            'un peu de PROM ; au cours du temps'],
        ['unicode: accents, quotes, arrows, emoji',
            'depression → heterogeneite « utile » — 📚 café',
            'depression → heterogeneite « inutile » — 📖 café'],
        ['tabs and trailing spaces preserved',
            '\tindented line  \nplain',
            '\tindented row  \nplain'],
        ['one side empty',
            '',
            'brand new region\nwith two lines'],
    ];
    for (const [name, a, b] of CASES) {
        it(name, () => {
            const el = render(a, b);
            expect(sideText(el, 'a')).toBe(a);
            expect(sideText(el, 'b')).toBe(b);
        });
    }
});

describe('word-level highlighting', () => {
    it('marks only the words that changed in a lightly reworded line', () => {
        const el = render('les criteres inclusion sont larges',
                          'les criteres exclusion sont larges');
        const chg = rows(el).find((r) => r.classList.contains('is-chg'));
        const left = [...cell(chg, 'a').querySelectorAll('.diff-sxs-word')].map((n) => n.textContent);
        const right = [...cell(chg, 'b').querySelectorAll('.diff-sxs-word')].map((n) => n.textContent);
        expect(left.join('')).toBe('inclusion');
        expect(right.join('')).toBe('exclusion');
    });

    it('does NOT highlight words when the paired lines are unrelated', () => {
        // The low mode of the measured bimodal distribution: two lines that
        // share almost nothing but sit in the same replace block. Marking the
        // handful of incidental shared characters would be noise.
        const el = render('appendicitis criteria for inclusion',
                          'Lebrun 2019 methodologie qualitative');
        const chg = rows(el).find((r) => r.classList.contains('is-chg'));
        expect(chg.querySelectorAll('.diff-sxs-word')).toHaveLength(0);
        // The line is still shown as changed — the gate suppresses the word
        // pass, never the fact that the line differs.
        expect(cell(chg, 'a').classList.contains('is-del')).toBe(true);
        expect(cell(chg, 'b').classList.contains('is-add')).toBe(true);
    });

    it('treats re-indentation as a small change, not a rewrite', () => {
        // Similarity is weighted by non-whitespace characters, so leading
        // whitespace cannot dominate the ratio and suppress the word pass.
        const el = render('    - un point de detail assez long a comparer',
                          '\t\t- un point de detail assez long a comparer!');
        const chg = rows(el).find((r) => r.classList.contains('is-chg'));
        expect(chg.querySelectorAll('.diff-sxs-word').length).toBeGreaterThan(0);
    });

    it('marks nothing on either side of an unchanged line', () => {
        const el = render('stable\nx', 'stable\ny');
        const eq = rows(el).find((r) => r.classList.contains('is-equal'));
        expect(eq.querySelectorAll('.diff-sxs-word')).toHaveLength(0);
    });
});

describe('safety', () => {
    it('renders markup in the note as text, never as elements', () => {
        const nasty = '<img src=x onerror=alert(1)> & <b>bold</b>';
        const el = render(nasty, `${nasty} plus`);
        expect(el.querySelector('img')).toBeNull();
        expect(el.querySelector('b')).toBeNull();
        expect(sideText(el, 'a')).toBe(nasty);
    });

    it('renders markup as text on an UNPAIRED line too', () => {
        // A pure insertion takes the plain branch (`text.textContent = raw`),
        // not the word-diff branch, so it is a second place markup could be
        // interpolated. The two branches are tested separately on purpose: the
        // obvious single test only ever exercises one of them.
        const el = render('kept', 'kept\n<img src=x onerror=alert(1)>');
        expect(el.querySelector('img')).toBeNull();
        const ins = rows(el).find((r) => r.classList.contains('is-ins'));
        expect(textOf(ins, 'b')).toBe('<img src=x onerror=alert(1)>');
    });

    it('renders markup that lands inside a highlighted WORD run as text too', () => {
        // The word pass builds extra elements, so it is a second place markup
        // could be interpolated if it used innerHTML.
        const el = render('prefix <script>alert(1)</script> suffix stable tail',
                          'prefix <script>alert(2)</script> suffix stable tail');
        expect(el.querySelector('script')).toBeNull();
        expect(sideText(el, 'a')).toContain('<script>alert(1)</script>');
    });

    it('never uses innerHTML in the renderer source', () => {
        const src = readFileSync(join(ROOT, 'static/js/ui.js'), 'utf8');
        const start = src.indexOf('export function renderSideBySideDiff(');
        expect(start).toBeGreaterThan(-1);
        // The whole helper family, from the tokenizer to the exported entry.
        const family = src.slice(src.indexOf('const _DIFF_LINE_BUDGET'));
        expect(family).not.toContain('innerHTML');
        expect(family).not.toContain('insertAdjacentHTML');
    });
});

describe('accessibility', () => {
    it('carries a -/+ sign on every rendered side, so colour is not the only channel', () => {
        const el = render('gone\nkept\nold', 'kept\nnew');
        for (const row of rows(el)) {
            for (const side of ['a', 'b']) {
                const c = cell(row, side);
                if (c.classList.contains('is-void')) continue;
                const sign = c.querySelector('.diff-sxs-sign').textContent;
                const changed = c.classList.contains('is-del') || c.classList.contains('is-add');
                expect(changed ? sign.trim() : sign.trim()).toBe(
                    changed ? (side === 'a' ? '−' : '+') : '');
            }
        }
    });

    it('summarises the change count for a screen reader', () => {
        const el = render('a\nb\nc', 'a\nB\nc\nd');
        const label = el.getAttribute('aria-label');
        expect(label).toContain('1 line(s) removed');
        expect(label).toContain('2 line(s) added');
    });

    it('hides the decorative line numbers from assistive tech', () => {
        const el = render('a', 'b');
        for (const n of el.querySelectorAll('.diff-sxs-ln')) {
            expect(n.getAttribute('aria-hidden')).toBe('true');
        }
    });

    it('labels both columns', () => {
        const el = render('a', 'b', { beforeLabel: 'In the note now', afterLabel: 'Would become' });
        const labels = [...el.querySelectorAll('.diff-sxs-col-label')].map((n) => n.textContent);
        expect(labels).toEqual(['In the note now', 'Would become']);
    });
});

describe('folding long unchanged runs', () => {
    const long = (n, tag) => Array.from({ length: n }, (_, i) => `line ${i} ${tag}`).join('\n');

    it('collapses a long middle run behind a real button', () => {
        const a = `first\n${long(40, 'x')}\nlast A`;
        const b = `first\n${long(40, 'x')}\nlast B`;
        const el = render(a, b);
        const fold = el.querySelector('button.diff-sxs-fold');
        expect(fold).not.toBeNull();
        expect(fold.textContent).toMatch(/Show \d+ unchanged line\(s\)/);
        // Context is kept on both sides of the fold.
        expect(rows(el).length).toBeLessThan(44);
    });

    it('expands to the exact hidden lines on click, restoring losslessness', () => {
        const a = `first\n${long(40, 'x')}\nlast A`;
        const b = `first\n${long(40, 'x')}\nlast B`;
        const el = render(a, b);
        el.querySelector('button.diff-sxs-fold').click();
        expect(sideText(el, 'a')).toBe(a);
        expect(sideText(el, 'b')).toBe(b);
    });

    it('collapses again — the fold is a toggle, not a one-way reveal', () => {
        // It used to `replaceWith` its rows, so on a long note an accidental
        // click buried the changes under hundreds of unchanged lines with no
        // way back short of re-rendering the whole diff (2026-08-30).
        const a = `first\n${long(40, 'x')}\nlast A`;
        const b = `first\n${long(40, 'x')}\nlast B`;
        const el = render(a, b);
        const fold = () => el.querySelector('button.diff-sxs-fold');
        const collapsedRows = el.querySelectorAll('.diff-sxs-row').length;

        fold().click();
        expect(fold().getAttribute('aria-expanded')).toBe('true');
        expect(fold().textContent).toMatch(/^Hide /);
        expect(el.querySelectorAll('.diff-sxs-row').length)
            .toBeGreaterThan(collapsedRows);

        fold().click();
        expect(fold().getAttribute('aria-expanded')).toBe('false');
        expect(fold().textContent).toMatch(/^Show /);
        // Back to exactly the collapsed rendering — no duplicated rows.
        expect(el.querySelectorAll('.diff-sxs-row').length).toBe(collapsedRows);

        // And re-expanding is still lossless.
        fold().click();
        expect(sideText(el, 'a')).toBe(a);
        expect(sideText(el, 'b')).toBe(b);
    });

    it('does not fold a short run', () => {
        const el = render('a\n1\n2\nb', 'A\n1\n2\nB');
        expect(el.querySelector('button.diff-sxs-fold')).toBeNull();
    });

    it('honours an explicit context of 0', () => {
        // `first` and the 20 filler lines are ONE contiguous unchanged run, so
        // at context 0 all 21 collapse and only the changed pair is left.
        const a = `first\n${long(20, 'x')}\nlast A`;
        const b = `first\n${long(20, 'x')}\nlast B`;
        const el = render(a, b, { context: 0 });
        const fold = el.querySelector('button.diff-sxs-fold');
        expect(fold.textContent).toContain('21');
        expect(rows(el)).toHaveLength(1);
        expect(rows(el)[0].classList.contains('is-chg')).toBe(true);
        fold.click();
        expect(sideText(el, 'a')).toBe(a);
    });
});

describe('bounds', () => {
    it('degrades to a whole replacement, and says so, past the alignment budget', () => {
        // >1e6 aligning cells after the common prefix/suffix strip. A note with
        // this many DIFFERING lines is a full rewrite, which is what the note
        // then tells the user — coarser, never wrong.
        const a = Array.from({ length: 1100 }, (_, i) => `left ${i}`).join('\n');
        const b = Array.from({ length: 1100 }, (_, i) => `right ${i}`).join('\n');
        const el = render(a, b);
        expect(el.querySelector('.diff-sxs-note').textContent).toContain('Too many lines differ');
        // Still lossless, and still every line accounted for.
        expect(sideText(el, 'a')).toBe(a);
        expect(sideText(el, 'b')).toBe(b);
        // And NO word marks: the pairing inside a degraded block is positional
        // and therefore arbitrary, so highlighting "the words that changed"
        // would assert a relationship the aligner failed to establish.
        expect(el.querySelectorAll('.diff-sxs-word')).toHaveLength(0);
    });

    it('ignores a nonsensical context rather than dropping rows', () => {
        const a = `first\n${'x\n'.repeat(30)}last A`;
        const b = `first\n${'x\n'.repeat(30)}last B`;
        for (const context of [-5, 1.5, 'three', null]) {
            const el = render(a, b, { context });
            expect(sideText(el, 'a')).not.toBe('');
            el.querySelectorAll('button.diff-sxs-fold').forEach((f) => f.click());
            expect(sideText(el, 'a')).toBe(a);
        }
    });

    it('does not degrade a big note whose edits are few', () => {
        // The prefix/suffix strip is what makes this the common case: a 3,000
        // line note with one changed line aligns in a handful of cells.
        const body = Array.from({ length: 3000 }, (_, i) => `para ${i}`).join('\n');
        const el = render(`${body}\ntail one`, `${body}\ntail two`);
        expect(el.querySelector('.diff-sxs-note')).toBeNull();
        expect(rows(el).filter((r) => r.classList.contains('is-chg'))).toHaveLength(1);
    });

    it('skips the word pass on a line too long to tokenise cheaply', () => {
        const a = `${'mot '.repeat(400)}fin`;
        const b = `${'mot '.repeat(400)}FIN`;
        const el = render(a, b);
        const chg = rows(el).find((r) => r.classList.contains('is-chg'));
        expect(chg.querySelectorAll('.diff-sxs-word')).toHaveLength(0);
        expect(sideText(el, 'a')).toBe(a);
    });
});

describe('one diff look — both callers route through the shared primitive', () => {
    it('audit.js renders the notesync preview with it', () => {
        const src = readFileSync(join(ROOT, 'static/js/audit.js'), 'utf8');
        expect(src).toMatch(/renderSideBySideDiff[^}]*\} from '\.\/ui\.js'/);
        expect(src).toContain('renderSideBySideDiff(');
    });

});

describe('the RENDER is bounded, not only the alignment', () => {
    // `_DIFF_LINE_BUDGET` caps the alignment DP and nothing else: past it the
    // core is reported as one wholesale `replace`, which then expands into
    // `min(aCore, bCore)` ROWS — still thousands. Note Refactor renders
    // whole-note diffs and the live vault runs to 3,399 lines, so a heavily
    // reworded long note built ~20k elements synchronously on the main thread.
    const manyChanged = (n, tag) =>
        Array.from({ length: n }, (_, i) => `line ${i} ${tag}`).join('\n');

    it('truncates a diff too long to draw and says exactly what it withheld', () => {
        const el = render(manyChanged(2000, 'A'), manyChanged(2000, 'B'));
        // `.is-clipped`, not the bare class: a very long diff is BOTH
        // degraded and clipped, and the two notes say different things.
        const note = el.querySelector('.diff-sxs-note.is-clipped');
        expect(note, 'a clipped diff must disclose it').not.toBeNull();
        expect(note.textContent).toMatch(/showing the first 1500 of \d+ blocks/i);
        expect(rows(el).length).toBeLessThanOrEqual(1500);
    });

    it('does NOT clip a long note whose edits are few', () => {
        // The case the cap must not punish: folding collapses the unchanged run
        // to a single button, so this costs nothing to draw. Capping the
        // ALIGNED rows instead of the RENDERED blocks clipped it and claimed
        // lines were withheld when they were not.
        const filler = Array.from({ length: 3000 }, (_, i) => `l${i}`).join('\n');
        const el = render(`${filler}\ntail A`, `${filler}\ntail B`);
        expect(el.querySelector('.diff-sxs-note')).toBeNull();
    });

    it('still reports the true totals when clipped', () => {
        const el = render(manyChanged(2000, 'A'), manyChanged(2000, 'B'));
        // The aria summary counts the whole diff, not the drawn part.
        const label = el.getAttribute('aria-label');
        expect(label).toMatch(/2000 line\(s\) removed/);
        expect(label).toMatch(/2000 line\(s\) added/);
    });
});

describe('word-level changes are not signalled by colour alone (WCAG 1.4.1)', () => {
    // The -/+ signs cover the LINE. Which WORDS changed inside a changed line
    // was distinguished only by a 20-28% red vs green tint — indistinguishable
    // under deuteranopia or protanopia (~8% of men), on the one view whose
    // whole job is "is any of that mine?".
    const CSS = readFileSync(join(ROOT, 'static/css/styles.css'), 'utf8');

    it('strikes removed words and underlines added ones', () => {
        const del = CSS.match(/\.diff-sxs-cell\.is-del \.diff-sxs-word \{[^}]*\}/g).join('');
        const add = CSS.match(/\.diff-sxs-cell\.is-add \.diff-sxs-word \{[^}]*\}/g).join('');
        expect(del).toMatch(/text-decoration:\s*line-through/);
        expect(add).toMatch(/text-decoration:\s*underline/);
    });

    it('keeps the line-level sign channel too', () => {
        const el = render('les criteres inclusion sont larges',
                          'les criteres exclusion sont larges');
        const chg = rows(el).find((r) => r.classList.contains('is-chg'));
        expect(cell(chg, 'a').querySelector('.diff-sxs-sign').textContent.trim()).toBeTruthy();
        expect(cell(chg, 'b').querySelector('.diff-sxs-sign').textContent.trim()).toBeTruthy();
    });
});

describe('the stacked layout gets a caption instead of two column headings', () => {
    // Below the breakpoint the body is ONE interleaved column, so a PAIR of
    // headings labels nothing — and they carry the load-bearing counts the
    // callers pass ("N word(s) not in Zotero" / "N word(s) imported"), which a
    // reader would then take as describing whichever half sat under them.
    it('builds both, so the text has one source', () => {
        const el = render('a', 'b', { beforeLabel: 'In the note now (3 words)',
                                      afterLabel: 'Would become (5 imported)' });
        expect(el.querySelector('.diff-sxs-head')).not.toBeNull();
        const cap = el.querySelector('.diff-sxs-caption');
        expect(cap).not.toBeNull();
        expect(cap.textContent).toContain('In the note now (3 words)');
        expect(cap.textContent).toContain('Would become (5 imported)');
    });

    it('hides the head and shows the caption below the breakpoint', () => {
        const CSS = readFileSync(join(ROOT, 'static/css/styles.css'), 'utf8');
        // Anchored on THIS media query, not the first 900px one in the file —
        // there are several, and slicing from the wrong one silently asserts
        // against unrelated rules.
        const at = CSS.indexOf('.diff-sxs-row.is-equal .diff-sxs-cell.side-b');
        expect(at, 'the stacked diff rules moved — update this ratchet')
            .toBeGreaterThan(-1);
        const block = CSS.slice(CSS.lastIndexOf('@media', at),
                                CSS.indexOf('\n}\n', at) + 3);
        expect(block).toContain('max-width: 900px');
        expect(block).toMatch(/\.diff-sxs-head \{ display: none/);
        expect(block).toMatch(/\.diff-sxs-caption \{ display: block/);
    });
});
