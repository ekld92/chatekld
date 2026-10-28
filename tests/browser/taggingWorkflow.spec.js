import {test, expect} from '@playwright/test';
import {createRequire} from 'node:module';

const require = createRequire(import.meta.url);
const rows = Array.from({length: 60}, (_, i) => ({
    citation_key: `fixture${String(i).padStart(2, '0')}`,
    title: 'Long clinical research title '.repeat(7),
    year: '2026',
    has_pdf: true,
    pdf_rels: [`paper${i}.pdf`],
    finder_tags: ['existing'],
    note: {present: true, tags: ['existing'], rel: `paper${i}.md`},
    zotero: {present: true, parent_tags: ['existing']},
}));

async function mount(page, {inventoryError = false, zoteroAccess = {available: true, writable: true, note: "Zotero Desktop authorized."}} = {}) {
    await page.addInitScript({path: require.resolve('axe-core/axe.min.js')});
    await page.route('**/api/tagging/**', async route => {
        const name = new URL(route.request().url()).pathname.split('/').pop();
        const payload = {
            inventory: {rows, scanned_at: 1789900000},
            vocab: {vocab: [{id: 'dep', gloss: 'Depression'}, {id: 'existing', gloss: 'Existing'}]},
            'zotero-available': zoteroAccess,
            snippet: {snippet: 'Fixture evidence '.repeat(80), chars: 1200},
            propose: {proposed: [{tag: 'dep', confidence: .9}], status: 'ok', source: 'pdf'},
            'apply-all': {results: {
                note: {ok: true, changed: true, op_id: 'fixture-note'},
                finder: {ok: false, error: 'Fixture Finder failure'},
                zotero: {ok: false, error: 'Result unknown'},
            }},
        };
        if (name === 'inventory' && inventoryError) {
            return route.fulfill({status: 503, json: {error: 'Fixture temporary inventory outage'}});
        }
        return route.fulfill({json: payload[name] || {}});
    });
    await page.goto('/');
    await page.waitForFunction(() => !!window.selectAuditReport);
    await page.locator('#tab-audit').click();
    await page.evaluate(() => window.selectAuditReport('tagging'));
}

test('mounted deck has no serious axe errors at short viewport', async ({page}) => {
    await page.setViewportSize({width: 960, height: 500});
    await mount(page);
    await expect(page.locator('.tagging-deck')).toBeVisible();
    await page.screenshot({path: '/tmp/library-review-tags.png'});
    const violations = await page.evaluate(async () =>
        (await axe.run(document.querySelector('#audit-report-body'))).violations
            .filter(x => ['serious', 'critical'].includes(x.impact))
            .map(x => ({id: x.id, nodes: x.nodes.map(n => n.target)})));
    expect(violations).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const snippet = page.locator('#tagging-card-snippet');
    await snippet.focus();
    await expect(snippet).toBeFocused();
    await page.keyboard.press('End');
    await expect.poll(() => snippet.evaluate(n => n.scrollTop)).toBeGreaterThan(0);
});

test('keyboard confirmation names all destinations and partial failure remains recoverable', async ({page}) => {
    await page.setViewportSize({width: 960, height: 500});
    await mount(page);
    await page.locator('[data-deck=propose]').click();
    await expect(page.locator('#tagging-card-input')).toHaveJSProperty('_tags', ['dep']);
    await page.locator('[data-deck=accept]').focus();
    await page.keyboard.press('Enter');
    const strip = page.locator('.inline-confirm-strip');
    await expect(strip).toBeVisible();
    await expect(strip).toContainText('note + finder + zotero');
    await strip.getByRole('button', {name: 'Accept', exact: true}).focus();
    await page.keyboard.press('Enter');
    await expect(page.locator('#tagging-deck-lastresult')).toContainText('Fixture Finder failure');
    await expect(page.locator('#tagging-deck-lastresult')).not.toHaveClass(/tone-ok/);
    expect(await page.evaluate(() => document.activeElement === document.body)).toBe(false);
});

test('temporary HTTP inventory failure offers Retry', async ({page}) => {
    await mount(page, {inventoryError: true});
    await expect(page.locator('#audit-report-body')).toContainText('Fixture temporary inventory outage');
    await expect(page.locator('#audit-report-body').getByRole('button', {name: 'Retry'})).toBeVisible();
    await page.route('**/api/tagging/inventory?*', r => r.fulfill({json: {rows, scanned_at: 1789900000}}));
    await page.getByRole('button', {name: 'Retry', exact: true}).click();
    await expect(page.locator('.tagging-deck')).toBeVisible();
});

test('long table mounted short viewport remains accessible', async ({page}) => {
    await page.setViewportSize({width: 960, height: 500});
    await mount(page);
    await page.getByRole('button', {name: 'Table', exact: true}).click();
    await expect(page.locator('#audit-report-body tbody tr')).toHaveCount(60);
    const violations = await page.evaluate(async () =>
        (await axe.run(document.querySelector('#audit-report-body'))).violations
            .filter(x => ['serious', 'critical'].includes(x.impact))
            .map(x => ({id: x.id, nodes: x.nodes.map(n => n.target)})));
    expect(violations).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});


test('Desktop authorization is reachable from Tagging and never replays tags', async ({page}) => {
    const access = {available: true, writable: false, mode: 'auto', note: 'Authorize CHERQAiN in Zotero Desktop before applying tags.'};
    let writes = 0;
    await page.route('**/api/zotlink/local-status', route => route.fulfill({json: {supports_writes: true, server_id: 'fixture'}}));
    await page.route('**/api/zotlink/authorize-local', route => {
        writes++;
        expect(route.request().postDataJSON()).toEqual({confirm: true, server_id: 'fixture'});
        access.writable = true;
        access.note = 'Zotero Desktop authorized.';
        return route.fulfill({json: {authorized: true}});
    });
    await page.setViewportSize({width: 960, height: 700});
    await mount(page, {zoteroAccess: access});
    const button = page.getByRole('button', {name: 'Authorize Zotero Desktop…', exact: true});
    await page.locator('.tagging-zotero-access').scrollIntoViewIfNeeded();
    await page.screenshot({path: '/tmp/library-review-zotero-access.png'});
    await expect(page.locator('input[data-target="zotero"]')).toBeDisabled();
    await button.click();
    await expect(page.locator('.inline-confirm-strip')).toBeVisible();
    expect(writes).toBe(0);
    await page.getByRole('button', {name: 'Authorize Desktop', exact: true}).click();
    await expect(page.locator('input[data-target="zotero"]')).toBeEnabled();
    expect(writes).toBe(1);
});
