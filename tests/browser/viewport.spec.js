import { createRequire } from 'node:module';

import { expect, test } from '@playwright/test';

const require = createRequire(import.meta.url);
const AXE_PATH = require.resolve('axe-core/axe.min.js');

/**
 * Layout and accessibility checks that need a real layout engine.
 *
 * jsdom has no layout: it cannot reflow, cannot apply a media query to a box,
 * and `getComputedStyle` returns declared values only. The committed jsdom
 * suites are therefore blind to horizontal overflow and to any contrast or
 * focus state that depends on rendering. 960 px is the product's supported
 * minimum desktop width (CLAUDE.md); above it the shell must reflow without
 * the DOCUMENT scrolling sideways. Individual wide tables keep their own
 * `overflow-x` scrollers by design, which is why the assertion is on the
 * document element and not on every descendant.
 */
const WIDTHS = [960, 1024, 1280, 1600];

for (const width of WIDTHS) {
    test(`the app shell reflows without document overflow at ${width}px`, async ({ page }) => {
        await page.setViewportSize({ width, height: 900 });
        await page.goto('/');
        const overflow = await page.evaluate(() => ({
            scrollWidth: document.documentElement.scrollWidth,
            clientWidth: document.documentElement.clientWidth,
        }));
        expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth);
    });
}

test('the app shell has no serious or critical axe violations', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    // addInitScript, not addScriptTag: the app ships an ENFORCED CSP with no
    // 'unsafe-inline', so injecting a <script> tag is (correctly) blocked. This
    // route goes through CDP before document scripts run, so the page keeps the
    // exact policy under test instead of the suite weakening it with
    // `bypassCSP` — which would also have hidden the blob-PDF regression.
    await page.addInitScript({ path: AXE_PATH });
    await page.goto('/');
    const violations = await page.evaluate(async () => {
        const results = await window.axe.run(document, {
            resultTypes: ['violations'],
            // Hidden tab panels are not reachable by any user in this state;
            // auditing them here would report on markup the browser is not
            // presenting and drown the real findings.
            rules: { 'color-contrast': { enabled: true } },
        });
        return results.violations
            .filter((v) => v.impact === 'serious' || v.impact === 'critical')
            .map((v) => `${v.id} (${v.nodes.length}): ${v.help}`);
    });
    expect(violations).toEqual([]);
});

/*
 * Per-TAB axe, added 2026-09-03 after the shell-only run above was found blind
 * to two real violations: `#plainchat-file`, the .sr-only file input behind
 * Plain Chat's Attach button, had no accessible name at all (CRITICAL
 * "label"), and `#help-guide-body`, an overflow-y:auto region, was not
 * focusable, so a keyboard-only user could not scroll the rendered guide
 * (SERIOUS "scrollable-region-focusable"). Neither is in the shell: a hidden
 * tab panel is not presented, so the shell run correctly ignores it — and
 * therefore never sees it. Each panel is audited once activated, which is the
 * only state in which its markup is real.
 *
 * Chromium is not WKWebView and axe cannot judge announcement QUALITY; the
 * manual VoiceOver pass recorded in CLAUDE.md is still owed before a build
 * ships. This closes the mechanical half.
 */
const TAB_PANELS = [
    'obsidian', 'document', 'chat', 'audit', 'deck',
    'docgen', 'prompts', 'help',
    // 'maintenance' is private-only and ships hidden; it is audited when the
    // probe reveals it (the loop skips a button that is not visible).
    'maintenance',
];

test('no tab panel has a serious or critical axe violation', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.addInitScript({ path: AXE_PATH });
    await page.goto('/');
    // The tab buttons are static markup, but the panels' own JS fills some of
    // them (the Help guide is fetched); give init a moment before auditing.
    await page.waitForFunction(() => !!window.showTab);
    const found = {};
    for (const id of TAB_PANELS) {
        const button = page.locator(`#tab-${id}`);
        if ((await button.count()) === 0 || !(await button.isVisible())) continue;
        await button.click();
        await page.waitForTimeout(400);
        found[id] = await page.evaluate(async (panelId) => {
            const root = document.getElementById(`${panelId}-tab`);
            if (!root) return ['(no panel)'];
            const results = await window.axe.run(root, { resultTypes: ['violations'] });
            return results.violations
                .filter((v) => v.impact === 'serious' || v.impact === 'critical')
                .map((v) => `${v.id} (${v.impact}) x${v.nodes.length}: ${v.help}`);
        }, id);
    }
    // One assertion over the whole map: a failure names the tab, not just the rule.
    expect(found).toEqual(Object.fromEntries(Object.keys(found).map((k) => [k, []])));
});

/*
 * 2026-09-19: the document itself never scrolls. With `body { height: 100vh }`
 * an absolutely positioned element with no positioned ancestor (the audit
 * search bar's .sr-only label sat ~1 300px down) made the document taller than
 * the window, and any scrollIntoView()/focus() then scrolled the ROOT: the top
 * bar disappeared and a grey band filled the bottom, unrecoverable because the
 * root is overflow:hidden. The test recreates that condition directly — a
 * stray absolute element far below the fold, then a scrollIntoView on it —
 * so it guards the shell against every future trigger, not just the one found.
 */
test('the app shell stays on the window when something scrolls into view', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto('/');
    const moved = await page.evaluate(() => {
        const stray = document.createElement('div');
        stray.style.cssText = 'position:absolute; top:2400px; left:0; width:10px; height:10px;';
        document.querySelector('.main').appendChild(stray);
        stray.scrollIntoView({ block: 'center' });
        const body = document.body.getBoundingClientRect();
        const header = document.querySelector('.main > .header').getBoundingClientRect();
        stray.remove();
        return { bodyTop: body.top, bodyBottom: body.bottom, headerTop: header.top, vh: innerHeight };
    });
    expect(moved.bodyTop).toBe(0);
    expect(moved.headerTop).toBeGreaterThanOrEqual(0);
    expect(moved.bodyBottom).toBe(moved.vh);
});

test('the audit search label is contained by its search bar', async ({ page }) => {
    // The label is .sr-only (position:absolute). With no positioned ancestor it
    // was laid out relative to the initial containing block, ~1 300px down the
    // document once a report had rendered — the trigger of the grey band. The
    // hermetic app has no report to push it down, so assert the containment
    // itself rather than a height that only real data would produce.
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto('/');
    // The tabs' inline handlers call functions app.js defines once its module
    // has loaded; clicking earlier is a silent no-op (a flake under load).
    await page.waitForFunction(() => typeof window.showTab === 'function');
    await page.click('.tab[aria-controls="audit-tab"]');
    // Daily update deliberately hides audit search, even with inline display.
    // Enter the Documents surface before checking report-search containment.
    await page.getByRole('button', {name: 'Documents', exact: true}).click();
    const contained = await page.evaluate(() => {
        const bar = document.getElementById('audit-search-bar');
        bar.style.display = 'flex';
        const label = bar.querySelector('label.sr-only');
        return !!label && label.offsetParent === bar;
    });
    expect(contained).toBe(true);
});

test('the Help guide tables have visible cell borders and padding', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto('/');
    // The tabs' inline handlers call functions app.js defines once its module
    // has loaded; clicking earlier is a silent no-op (a flake under load).
    await page.waitForFunction(() => typeof window.showTab === 'function');
    await page.click('.tab[aria-controls="help-tab"]');
    await page.waitForSelector('#help-guide-body table', { state: 'attached' });
    const cell = await page.evaluate(() => {
        const td = document.querySelector('#help-guide-body td');
        const cs = getComputedStyle(td);
        return { border: cs.borderTopStyle, pad: parseFloat(cs.paddingLeft),
                 wrapped: !!td.closest('.help-table-wrap') };
    });
    expect(cell.border).toBe('solid');
    expect(cell.pad).toBeGreaterThanOrEqual(8);
    expect(cell.wrapped).toBe(true);
});

// With the shell on `overflow: clip` (2026-09-19) the DOCUMENT can no longer
// overflow, so the test above cannot see a column that has grown past the
// window: clip keeps the flex automatic minimum size, and without an explicit
// min-width:0 .main widened to its widest content and ran off the right edge.
for (const width of [960, 1440]) {
    test(`the main column fits the window at ${width}px`, async ({ page }) => {
        await page.setViewportSize({ width, height: 900 });
        await page.goto('/');
        await page.waitForFunction(() => typeof window.showTab === 'function');
        for (const tab of ['audit-tab', 'deck-tab', 'help-tab']) {
            await page.click(`.tab[aria-controls="${tab}"]`);
            const right = await page.evaluate(() => document.querySelector('.main').getBoundingClientRect().right);
            expect(right, tab).toBeLessThanOrEqual(width + 1);
        }
    });
}

// 2026-09-19 review, two regressions of the one-scroller change, each measured
// before it was fixed.
test('a long unbreakable line on the Deck tab never slides the column sideways', async ({ page }) => {
    await page.setViewportSize({ width: 960, height: 640 });
    await page.goto('/');
    await page.waitForFunction(() => typeof window.showTab === 'function');
    await page.click('.tab[aria-controls="deck-tab"]');
    const r = await page.evaluate(() => {
        const body = document.querySelector('#deck-tab .deck-body');
        const code = document.createElement('code');
        code.textContent = 'cd /Users/someone/Library/Mobile_Documents/' + 'x'.repeat(260) + ' && make view';
        const pre = document.createElement('div');
        pre.style.whiteSpace = 'nowrap';
        pre.appendChild(code);
        body.appendChild(pre);
        code.scrollIntoView({ inline: 'end' });
        const pane = document.querySelector('#deck-tab .pane-view');
        const out = { paneLeft: pane.scrollLeft, bodyScrolls: body.scrollWidth > body.clientWidth };
        pre.remove();
        return out;
    });
    expect(r.paneLeft).toBe(0);          // the column itself never moved
    expect(r.bodyScrolls).toBe(true);    // the wide line has its own scroller
});

test('the Inventory detail drawer wraps its text', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto('/');
    const r = await page.evaluate(() => {
        const host = document.createElement('div');
        host.className = 'audit-table-scroll';
        host.style.width = '900px';
        host.innerHTML = '<table class="audit-table audit-inv-table"><tbody>'
            + '<tr><td>smithKey2020</td><td>1 <button class="audit-link-btn">[view]</button></td></tr>'
            + '<tr class="audit-inv-detail-row" data-row-attachment><td colspan="2"><p>'
            + 'A long explanatory drawer sentence about provenance and the Zotero side. '.repeat(6)
            + '</p><button class="audit-link-btn">[reveal]</button></td></tr></tbody></table>';
        document.querySelector('.main').appendChild(host);
        const drawer = host.querySelector('.audit-inv-detail-row td');
        const key = host.querySelector('tr td');
        const out = { drawerWidth: drawer.getBoundingClientRect().width,
                      keyWs: getComputedStyle(key).whiteSpace };
        host.remove();
        return out;
    });
    expect(r.drawerWidth).toBeLessThanOrEqual(900);
    expect(r.keyWs).toBe('nowrap');      // the data rows keep their nowrap
});
