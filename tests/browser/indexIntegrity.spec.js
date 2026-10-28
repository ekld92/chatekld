import {test, expect} from '@playwright/test';

test('mounted run recovery and bounded picker are observational at small viewport', async ({page}, testInfo) => {
    await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.fallback() : route.abort());
    const writes = [];
    page.on('request', request => { if (request.method() === 'POST' && request.url().includes('/online-ocr/')) writes.push(request.url()); });
    await page.route('**/api/online-ocr/runs', route => route.fulfill({json: {runs: [{
        run_id: 'a'.repeat(32), state: 'index_incomplete', source_count: 500,
        promotion_barrier: true, index_continuation_available: true, retry_allowed: false
    }]}}));
    await page.route('**/api/online-ocr/status', route => route.fulfill({json: {state: 'idle'}}));
    await page.route('**/api/online-ocr/sources?*', route => route.fulfill({json: {
        sources: Array.from({length: 50}, (_, i) => `folder/${'long-name-'.repeat(30)}${i}.pdf`), next_offset: 50
    }}));
    await page.setViewportSize({width: 960, height: 540});
    await page.goto('/');
    await page.waitForFunction(() => !!window.chooseOnlineSources);
    await expect(page.getByRole('button', {name: 'Update Index', exact: true})).toBeVisible();
    await expect(page.locator('#vault-online-full-rebuild')).not.toBeVisible();
    await page.locator('#app-settings-btn').click();
    await page.locator('#index-settings-btn').click();
    await page.getByRole('button', {name:'History', exact:true}).click();
    await expect(page.locator('#online-run-history')).toContainText('Finish interrupted search update');
    await page.getByRole('button', {name:'Documents', exact:true}).click();
    await page.getByRole('button', {name: 'Browse sources', exact: true}).click();
    await expect(page.locator('#online-source-picker input')).toHaveCount(50);
    const first = page.locator('#online-source-picker input').first();
    const panel = page.locator('#online-ocr-panel');
    expect(await panel.evaluate(el => el.clientHeight)).toBeGreaterThanOrEqual(180);
    await first.focus();
    const visible = await first.evaluate(el => {
        const input = el.getBoundingClientRect();
        const panel = el.closest('details').getBoundingClientRect();
        return input.top >= panel.top && input.bottom <= panel.bottom && input.bottom <= innerHeight;
    });
    expect(visible).toBe(true);
    await page.screenshot({path: testInfo.outputPath("source-picker.png")});
    await page.keyboard.press('Space');
    await expect(first).toBeChecked();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    expect(overflow).toBe(false);
    expect(writes).toEqual([]);
    await page.reload();
    await expect(page.locator('#online-run-history')).toContainText('index_incomplete');
    expect(writes).toEqual([]);
});

test('confirmed historical retry follows its own run after a previous daily run', async ({page}, info) => {
    const oldId = 'a'.repeat(32), retryId = 'b'.repeat(32);
    await page.addInitScript(id => localStorage.setItem('papermind-extraction-run', id), oldId);
    let status = {state: 'idle'};
    await page.route('**/api/online-ocr/status', r => r.fulfill({json: status}));
    await page.route(`**/api/online-ocr/runs/${oldId}?*`, r => r.fulfill({json: {state: 'text_prepared', run_id: oldId}}));
    await page.route('**/api/online-ocr/runs', r => r.fulfill({json: {runs: [{
        run_id: retryId, state: 'extraction_incomplete', kind: 'session', routine: true,
        retry_allowed: true, source_count: 1, prepare_only: true, no_external: true
    }]}}));
    let starts = 0;
    await page.route('**/api/online-ocr/session/start', r => {
        starts++;
        expect(r.request().postDataJSON().resume_run_id).toBe(retryId);
        status = {state: 'text_prepared', run_id: retryId, prepare_only: true, source_count: 1, result_count: 1};
        return r.fulfill({json: {ok: true, run_id: retryId}});
    });
    await page.goto('/');
    await page.waitForFunction(() => !!window.chooseOnlineSources);
    await page.locator('#app-settings-btn').click();
    await page.locator('#index-settings-btn').click();
    await page.getByRole('button', {name:'History', exact:true}).click();
    await page.getByRole('button', {name: 'Retry unfinished extraction', exact: true}).click();
    await page.getByRole('button', {name: 'Retry this run', exact: true}).click();
    await expect.poll(() => starts).toBe(1);
    await expect(page.locator('#online-ocr-status')).toContainText(/Another operation|1\/1 documents/);
    await page.screenshot({path: info.outputPath('historical-retry.png')});
    await expect(page.locator('#online-ocr-status')).toContainText('Preparation complete');
    expect(await page.evaluate(() => localStorage.getItem('papermind-extraction-run'))).toBe(retryId);
});
