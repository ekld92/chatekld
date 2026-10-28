import { expect, test } from '@playwright/test';

/**
 * Layout checks for the shared side-by-side diff (ui.js::renderSideBySideDiff).
 *
 * The committed jsdom suite (tests/js/sideBySideDiff.test.js) pins what the
 * renderer PRODUCES — alignment, losslessness, the word gate, the -/+ channel.
 * It structurally cannot pin what the renderer LOOKS like, because jsdom has no
 * layout engine, no media queries applied to boxes and no text wrapping. Two
 * grid columns nested inside a report table cell is exactly the shape that goes
 * wrong invisibly, and it is the shape the notesync preview injects.
 *
 * The diff normally appears only after an audit scan, which this hermetic shell
 * has not run — so the test drives the real exported primitive with synthetic
 * content and mounts it where audit.js mounts it: a `colspan` cell inside an
 * `.audit-table` in `.audit-table-scroll`. Content is synthetic; real note text
 * never enters the repo.
 */

// Deliberately long unbroken tokens: a DOI, a URL and a run-together citation
// key are what actually blow a two-column layout out sideways, and all three
// occur in the reference notes this view is built for.
const BEFORE = [
    '',
    'meta-epidemio n=492 RCTs (n=506 studies) de 1977 a 2024',
    '- "we only included OMIs for depression present in at least ten comparisons"',
    'https://example.org/a/very/long/url/that/will/not/break/on/its/own/at/all/ever',
    'doi:10.1016/j.jad.2025.120873 sprengerInconsistentOutcomeMeasurementIn2026',
].join('\n');
const AFTER = [
    '',
    'meta-epidemio n=492 RCTs (n=506 studies) de 1977 a 2025',
    '- "we only included OMIs for depression present in at least ten comparisons"',
    'https://example.org/a/very/long/url/that/will/not/break/on/its/own/at/all/ever',
    'doi:10.1016/j.jad.2025.120874 waltonClinicalOutcomeAssessmentsReport2015',
].join('\n');

/** Mount a real diff where audit.js mounts it, using the shipped module. */
async function mountDiff(page, before, after, opts = {}) {
    // `opts` lets a test choose the column labels; they carry the load-bearing
    // word counts, and the stacked-caption tests assert those survive.
    const labels = {
        beforeLabel: opts.beforeLabel || 'In the note now (82 word(s) not in Zotero)',
        afterLabel: opts.afterLabel || 'Would become (2 word(s) imported)',
    };
    await page.goto('/');
    await page.evaluate(async ({ before, after, labels }) => {
        const { renderSideBySideDiff } = await import('/static/js/ui.js');
        const host = document.createElement('div');
        host.className = 'audit-table-scroll';
        host.id = 'sxs-probe';
        // Its own full-window layer: <body> is a fixed flex shell (2026-09-19),
        // so a probe appended as a plain child became a squeezed flex item.
        host.style.cssText = 'position:fixed; inset:0; z-index:5000; overflow:auto; background:var(--card-bg);';
        host.innerHTML = '<table class="audit-table"><tbody>'
            + '<tr><td>key</td><td>state</td></tr>'
            + '<tr class="audit-notesync-preview"><td colspan="2"></td></tr>'
            + '</tbody></table>';
        document.body.appendChild(host);
        host.querySelector('.audit-notesync-preview td').appendChild(
            renderSideBySideDiff(before, after, labels));
    }, { before, after, labels });
    await page.waitForSelector('#sxs-probe .diff-sxs-row');
}

for (const width of [960, 1024, 1280]) {
    test(`the diff fits its own width at ${width}px`, async ({ page }) => {
        await page.setViewportSize({ width, height: 900 });
        await mountDiff(page, BEFORE, AFTER);
        const m = await page.evaluate(() => {
            const d = document.querySelector('#sxs-probe .diff-sxs');
            return {
                doc: document.documentElement.scrollWidth,
                docClient: document.documentElement.clientWidth,
                diff: d.scrollWidth,
                diffClient: d.clientWidth,
            };
        });
        // (a) The document does not scroll sideways — the app-wide rule.
        expect(m.doc).toBeLessThanOrEqual(m.docClient);
        // (b) And the diff fits inside its OWN box. This is the assertion that
        //     actually pins the wrapping decision: the host `.audit-table-scroll`
        //     has `overflow-x: auto`, so it would absorb an over-wide diff and
        //     leave (a) passing while `.diff-sxs` (overflow: hidden) silently
        //     CLIPPED the right-hand column. The long URL and DOI in the fixture
        //     are there to make an unwrapped layout genuinely too wide.
        expect(m.diff).toBeLessThanOrEqual(m.diffClient);
    });
}

test('the two columns sit side by side above the stacking breakpoint', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await mountDiff(page, BEFORE, AFTER);
    const [a, b] = await page.evaluate(() => {
        const row = document.querySelector('#sxs-probe .diff-sxs-row.is-chg')
            || document.querySelector('#sxs-probe .diff-sxs-row');
        return ['a', 'b'].map((s) => {
            const r = row.querySelector(`.diff-sxs-cell.side-${s}`).getBoundingClientRect();
            return { left: r.left, top: r.top, width: r.width };
        });
    });
    // Genuinely adjacent, not merely two boxes: same top, right one further right.
    expect(b.left).toBeGreaterThan(a.left);
    expect(Math.abs(b.top - a.top)).toBeLessThan(2);
    expect(a.width).toBeGreaterThan(0);
});

test('the two columns stack below the breakpoint instead of scrolling', async ({ page }) => {
    await page.setViewportSize({ width: 700, height: 900 });
    await mountDiff(page, BEFORE, AFTER);
    const [a, b] = await page.evaluate(() => {
        const row = document.querySelector('#sxs-probe .diff-sxs-row.is-chg')
            || document.querySelector('#sxs-probe .diff-sxs-row');
        return ['a', 'b'].map((s) => {
            const r = row.querySelector(`.diff-sxs-cell.side-${s}`).getBoundingClientRect();
            return { left: r.left, top: r.top };
        });
    });
    expect(b.top).toBeGreaterThan(a.top);
    expect(Math.abs(b.left - a.left)).toBeLessThan(2);
});

test('stacked, an unchanged line is shown once and a changed one twice', async ({ page }) => {
    // Below the breakpoint the grid collapses to one column, so both cells of a
    // row sit one above the other. For an `equal` row the two cells hold the
    // SAME text, and showing both made the diff read as though the note
    // contained duplicated lines (2026-08-29). One column is the unified
    // reading; a CHANGED pair must still show both versions.
    await page.setViewportSize({ width: 700, height: 900 });
    await mountDiff(page, BEFORE, AFTER);
    const seen = await page.evaluate(() => {
        const out = [];
        for (const row of document.querySelectorAll('#sxs-probe .diff-sxs-row')) {
            const cells = [...row.querySelectorAll('.diff-sxs-cell')]
                .filter((c) => getComputedStyle(c).display !== 'none');
            out.push({
                kind: [...row.classList].find((c) => c.startsWith('is-')),
                texts: cells.map((c) => c.querySelector('.diff-sxs-text')?.textContent ?? ''),
            });
        }
        return out;
    });
    const equal = seen.filter((r) => r.kind === 'is-equal');
    expect(equal.length).toBeGreaterThan(0);
    for (const r of equal) expect(r.texts).toHaveLength(1);
    const changed = seen.filter((r) => r.kind === 'is-chg');
    expect(changed.length).toBeGreaterThan(0);
    for (const r of changed) expect(r.texts).toHaveLength(2);
    // No stacked row repeats its own text: that repetition, on the `equal`
    // rows, WAS the defect. (Deliberately per-row rather than over a flattened
    // list — a flat scan would need a carve-out for the changed pairs, and a
    // loop-invariant carve-out is how an assertion silently stops running.)
    for (const r of seen) {
        if (r.texts.length === 2) expect(r.texts[0]).not.toBe(r.texts[1]);
    }
});

test('a four-digit line number does not double the row height', async ({ page }) => {
    // The line-number track is fixed, and the cell's `overflow-wrap: anywhere`
    // applies to it: at 3ch a number of 1000+ wrapped and made its row twice as
    // tall as its neighbours (measured 36px against 18px). Note Refactor diffs
    // whole notes, which run to ~3,400 lines in the live vault.
    //
    // Two CSS rules fix it and EITHER alone suffices (a 4ch track, so the
    // number fits; `white-space: nowrap`, so it cannot break) — so no single
    // mutation to styles.css turns this test red. It goes red when both are
    // reverted, which is the state that actually shipped.
    await page.setViewportSize({ width: 1280, height: 900 });
    const n = 1005;
    const src = Array.from({ length: n }, (_, i) => `line ${i}`);
    // TWO changes: one near the top so a 1-digit row is actually rendered, one
    // past 999 for the 4-digit row. With a single late change the fold keeps no
    // head context (there is nothing above the first change to orient against),
    // so nothing short would be on screen to compare against.
    const after = [...src];
    after[2] = 'line two';
    after[1000] = 'line one thousand';
    await mountDiff(page, src.join('\n'), after.join('\n'));
    const m = await page.evaluate(() => {
        const digits = (r) => (r.querySelector('.diff-sxs-cell.side-a .diff-sxs-ln')
            ?.textContent || '');
        const rows = [...document.querySelectorAll('#sxs-probe .diff-sxs-row')];
        const pick = (want) => {
            const r = rows.find((x) => /^\d+$/.test(digits(x)) && digits(x).length === want);
            return r && { label: digits(r), h: r.getBoundingClientRect().height };
        };
        return { short: pick(1), long: pick(4) };
    });
    // Both rows must genuinely be on screen, or this compares nothing.
    expect(m.short, 'no 1-digit line number rendered').toBeTruthy();
    expect(m.long, 'no 4-digit line number rendered').toBeTruthy();
    expect(m.long.label).toMatch(/^\d{4}$/);
    expect(m.long.h).toBeCloseTo(m.short.h, 0);
});

test('a changed line is not conveyed by colour alone', async ({ page }) => {
    // The rendered proof of the WCAG 1.4.1 decision: with colour removed the
    // -/+ signs still distinguish the two sides.
    await page.setViewportSize({ width: 1280, height: 900 });
    await mountDiff(page, BEFORE, AFTER);
    const signs = await page.evaluate(() => {
        const row = document.querySelector('#sxs-probe .diff-sxs-row.is-chg');
        return ['a', 'b'].map((s) => row
            .querySelector(`.diff-sxs-cell.side-${s} .diff-sxs-sign`).textContent.trim());
    });
    expect(signs).toEqual(['−', '+']);
});

test('stacked, one caption replaces the two column headings', async ({ page }) => {
    // Below the breakpoint the body is ONE interleaved column, so a PAIR of
    // headings labels nothing — and the callers put the load-bearing counts in
    // them ("N word(s) not in Zotero" / "N word(s) imported"), which a reader
    // would then take as describing whichever half sat underneath. jsdom has no
    // media queries applied to boxes, so only a real engine can check this.
    await page.setViewportSize({ width: 700, height: 900 });
    await mountDiff(page, BEFORE, AFTER, {
        beforeLabel: 'In the note now (7 words not in Zotero)',
        afterLabel: 'Would become (3 words imported)',
    });
    const shown = await page.evaluate(() => {
        const vis = (sel) => {
            const el = document.querySelector(`#sxs-probe ${sel}`);
            return el ? getComputedStyle(el).display !== 'none' : null;
        };
        return {
            head: vis('.diff-sxs-head'),
            caption: vis('.diff-sxs-caption'),
            captionText: document.querySelector('#sxs-probe .diff-sxs-caption')?.textContent,
        };
    });
    expect(shown.head, 'two headings over one column label nothing').toBe(false);
    expect(shown.caption).toBe(true);
    // Both counts survive into the single line — they are the whole point.
    expect(shown.captionText).toContain('7 words not in Zotero');
    expect(shown.captionText).toContain('3 words imported');
});

test('above the breakpoint the headings stay and the caption is hidden', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await mountDiff(page, BEFORE, AFTER);
    const shown = await page.evaluate(() => {
        const vis = (sel) => {
            const el = document.querySelector(`#sxs-probe ${sel}`);
            return el ? getComputedStyle(el).display !== 'none' : null;
        };
        return { head: vis('.diff-sxs-head'), caption: vis('.diff-sxs-caption') };
    });
    expect(shown.head).toBe(true);
    expect(shown.caption).toBe(false);
});
