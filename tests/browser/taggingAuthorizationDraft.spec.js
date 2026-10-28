import {test, expect} from '@playwright/test';

for (const view of ['table', 'deck']) {
    test(`Desktop authorization preserves ${view} drafts and focus`, async ({page}) => {
        let authorized = false;
        let writes = 0;
        let inventoryReads = 0;
        await page.addInitScript(value => localStorage.setItem('tagging_view', value), view);
        await page.route('**/api/tagging/**', route => {
            const name = new URL(route.request().url()).pathname.split('/').pop();
            if (name === 'inventory') inventoryReads += 1;
            if (name.startsWith('apply-')) writes += 1;
            const rows = ['K', 'L'].map(key => ({citation_key: key, title: 'Fixture', has_pdf: true,
                note: {present: true, tags: []}, finder_tags: [], zotero: {present: true, parent_tags: []}}));
            const payload = {
                inventory: {scanned_at: 123, rows},
                vocab: {vocab: [{id: 'dep', gloss: 'Depression'}]},
                'zotero-available': {available: true, writable: authorized, mode: 'auto', note: 'Fixture'},
                snippet: {snippet: 'Fixture text'},
            }[name] || {};
            return route.fulfill({json: payload});
        });
        await page.route('**/api/zotlink/local-status', r => r.fulfill({json: {supports_writes: true, server_id: 'fixture'}}));
        await page.route('**/api/zotlink/authorize-local', r => {
            authorized = true;
            return r.fulfill({json: {authorized: true}});
        });
        await page.goto('/');
        await page.waitForFunction(() => !!window.selectAuditReport);
        await page.locator('#tab-audit').click();
        await page.evaluate(() => window.selectAuditReport('tagging'));
        const inputs = page.locator('#audit-report-body .tagging-tag-input');
        await expect(inputs.first()).toBeVisible();
        for (const input of await inputs.all()) {
            await input.fill('dep');
            await input.press('Enter');
            await input.fill('unfinished');
        }
        const drafts = () => inputs.evaluateAll(elements => elements.map(el => ({draft: el.value, tags: el._tags})));
        const before = await drafts();
        const authorize = page.locator('[data-action="authorize-zotero"]');
        await authorize.focus();
        await page.keyboard.press('Enter');
        await page.getByRole('button', {name: 'Authorize Desktop', exact: true}).click();
        await expect(page.locator('.tagging-access-status')).toContainText('Desktop authorized');
        if (view === 'table') await expect(page.locator('[data-action="apply-zotero"]').first()).toBeEnabled();
        else await expect(page.locator('input[data-target="zotero"]')).toBeEnabled();
        expect(await drafts()).toEqual(before);
        expect(writes).toBe(0);
        expect(inventoryReads).toBe(1);
        await expect(authorize).toBeFocused();
        await page.screenshot({path: test.info().outputPath(`authorization-${view}.png`)});
    });
}

test('unavailable Zotero observation is an error, not an empty absence report', async ({page}) => {
    await page.route('**/api/audit/reports/read_unzoterod', r => r.fulfill({status: 503, json: {
        error: 'Zotero observation is unavailable; absence cannot be established.', reason: 'zotero_observation_unavailable',
    }}));
    await page.goto('/');
    await page.waitForFunction(() => !!window.selectAuditReport);
    await page.locator('#tab-audit').click();
    await page.evaluate(() => window.selectAuditReport('read_unzoterod'));
    await expect(page.locator('#audit-status-banner')).toContainText('absence cannot be established');
    await expect(page.locator('#audit-status-banner')).toBeVisible();
    await expect(page.locator('#audit-report-body tr')).toHaveCount(0);
});
