import {test, expect} from '@playwright/test';

test.beforeEach(async ({page}) => {
    await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1'
        ? route.fallback() : route.abort());
});

test('saved source counts remain distinct from live coverage and disappear on refresh failure', async ({page}, info) => {
    let reads = 0;
    const writes = [];
    page.on('request', r => {
        if (r.method() === 'POST' && /online-ocr|obsidian\/index|audit\/scan|api\/reset/.test(r.url())) writes.push(r.url());
    });
    await page.route('**/api/obsidian/readiness', route => {
        reads++;
        return reads === 1 ? route.fulfill({json: {
            state: 'partial', recorded_sources: 12, manifest_at: '2026-10-25',
            indexed_at: '2026-10-26', observed_at: '2026-10-27',
            message: 'Saved index metadata records an incomplete update. Review unfinished preparation and index history.',
        }}) : route.fulfill({status: 503, json: {error: 'unavailable'}});
    });
    await page.setViewportSize({width: 700, height: 600});
    await page.goto('/');
    await page.waitForFunction(() => !!window.selectAuditReport);
    await page.locator('#tab-audit').click();
    const evidence = page.locator('details[aria-label="Search status"]');
    await evidence.locator('summary').first().click();
    await expect(evidence).toContainText('12 distinct sources with chunks');
    await expect(evidence).toContainText('current index: unknown');
    await expect(evidence).toContainText('pending or changed: unknown');
    await expect(evidence).toContainText('incomplete update');
    await evidence.scrollIntoViewIfNeeded();
    await page.screenshot({path: info.outputPath('saved-evidence.png')});
    await evidence.getByRole('button', {name: 'Refresh status'}).focus();
    await page.keyboard.press('Enter');
    await expect(evidence).toContainText('Search evidence unavailable');
    await expect(evidence).not.toContainText('12 distinct');
    expect(writes).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('settings layers advanced tools correctly and reset retains a cancel default', async ({page}, info) => {
    let resets = 0;
    await page.route('**/api/reset', r => { resets++; return r.fulfill({json: {ok: true}}); });
    await page.setViewportSize({width: 700, height: 600});
    await page.goto('/');
    await page.waitForFunction(() => !!window.openModal);
    await expect(page.getByRole('button', {name: 'Reset App Data', exact: true})).toBeHidden();
    await page.locator('#app-settings-btn').focus();
    await page.keyboard.press('Enter');
    await expect(page.locator('#index-settings-btn')).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(page.locator('#index-settings-modal')).toBeVisible();
    await expect(page.locator('#settings-modal')).toHaveAttribute('inert', '');
    // Hit-testing verifies actual visual layering, not merely both open classes.
    const onTop = await page.locator('#index-settings-title').evaluate(el => {
        const box = el.getBoundingClientRect();
        return el.contains(document.elementFromPoint(box.x + 5, box.y + 5));
    });
    expect(onTop).toBe(true);
    await page.screenshot({path: info.outputPath('settings-index-tools.png')});
    await page.keyboard.press('Escape');
    await expect(page.locator('#index-settings-btn')).toBeFocused();
    await page.getByRole('button', {name: 'Library paths and audit settings'}).click();
    await expect(page.locator('#audit-settings-modal')).toBeVisible();
    await page.keyboard.press('Escape');
    await page.locator('#settings-modal summary').filter({hasText: 'Advanced data controls'}).click();
    await page.getByRole('button', {name: 'Reset App Data', exact: true}).focus();
    await page.keyboard.press('Enter');
    await expect(page.locator('#reset-modal').getByRole('button', {name: 'Cancel', exact: true})).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(page.getByRole('button', {name: 'Reset App Data', exact: true})).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(page.locator('#app-settings-btn')).toBeFocused();
    expect(resets).toBe(0);
});

test('reopen and out-of-order close preserve keyboard focus and the return chain', async ({page}) => {
    await page.goto('/');
    await page.waitForFunction(() => !!window.openModal);
    await page.locator('#app-settings-btn').click();
    await page.locator('#index-settings-btn').click();
    await page.evaluate(() => window.closeModal('settings-modal'));
    await page.keyboard.press('Escape');
    await expect(page.locator('#app-settings-btn')).toBeFocused();
    await page.locator('#app-settings-btn').click();
    await page.locator('#index-settings-btn').click();
    await page.evaluate(() => window.openModal('settings-modal'));
    await expect(page.locator('#index-settings-modal')).toHaveAttribute('inert', '');
    await expect(page.locator('#index-settings-btn')).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(page.locator('#index-settings-modal')).not.toHaveAttribute('inert');
    expect(await page.locator('#index-settings-modal').evaluate(el => el.contains(document.activeElement))).toBe(true);
    await page.keyboard.press('Escape');
    await expect(page.locator('#app-settings-btn')).toBeFocused();
});

test('modal tab order includes details and traps empty dialogs', async ({page}) => {
    await page.goto('/');
    await page.waitForFunction(() => !!window.openModal);
    await page.locator('#app-settings-btn').click();
    await page.locator('#index-settings-btn').focus();
    await page.keyboard.press('Tab');
    await page.keyboard.press('Tab');
    await expect(page.locator('#settings-modal summary').filter({hasText: 'Advanced data controls'})).toBeFocused();
    await page.keyboard.press('Escape');
    await page.evaluate(() => {
        const overlay = document.createElement('div');
        overlay.id = 'empty-fixture-modal'; overlay.className = 'modal-overlay';
        overlay.innerHTML = '<div class="modal"><p>Loading…</p></div>';
        document.body.appendChild(overlay);
        window.openModal(overlay.id);
    });
    await expect(page.locator('#empty-fixture-modal')).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(page.locator('#empty-fixture-modal')).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(page.locator('#app-settings-btn')).toBeFocused();
});
