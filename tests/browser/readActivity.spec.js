import {test, expect} from '@playwright/test';

for (const outcome of ['completed', 'failed', 'cancelled', 'reset']) {
    test(`Read Activity keeps partial text and owns its controls: ${outcome}`, async ({page}) => {
        let finish;
        const release = new Promise(resolve => { finish = resolve; });
        let calls = 0;
        await page.route('**/api/upload', r => r.fulfill({json: {upload_id:'fixture', filename:'fixture.pdf'}}));
        await page.route('**/api/upload/fixture', r => r.fulfill({json:{ok:true}}));
        await page.route('**/api/summarise', async r => {
            calls++;
            await release;
            await r.fulfill({contentType:'text/event-stream', body: 'data: {"info":"Summarising part 1/2…"}\n\ndata: {"token":"Kept text"}\n\n'
                + (outcome === 'failed' ? 'data: {"error":"Fixture failure"}\n\n' : '') + 'data: [DONE]\n\n'}).catch(() => {});
        });
        await page.goto('/');
        await page.waitForFunction(() => !!window.uploadPDF);
        await page.locator('#tab-document').click();
        await page.locator('#pdf-upload').setInputFiles({name:'fixture.pdf', mimeType:'application/pdf', buffer:Buffer.from('%PDF-fixture')});
        await expect(page.locator('#summarise-btn')).toBeEnabled();
        // Model discovery is disabled in this fixture; supply a disposable option.
        await page.locator('#model-select').evaluate(el => { el.add(new Option('fixture','fixture')); el.value='fixture'; });
        await page.locator('#summarise-btn').click();
        await expect.poll(() => calls).toBe(1);
        await expect(page.locator('#activity-label-text')).toContainText('Read');
        await page.locator('#summarise-btn').dispatchEvent('click');
        expect(calls).toBe(1);
        if (outcome === 'cancelled') await page.locator('#summarise-stop-btn').click();
        if (outcome === 'reset') await page.evaluate(() => window.resetUpload());
        finish();
        await page.locator('#activity-console-details').evaluate(el => { el.open = true; });
        await expect(page.locator('#activity-tasks')).toContainText(outcome === 'reset' || outcome === 'cancelled' ? 'Cancelled' : outcome === 'failed' ? 'Partly complete' : 'Completed');
        if (outcome === 'completed' || outcome === 'failed') {
            await expect(page.locator('#document-summary-content')).toHaveText('Kept text');
            await expect(page.locator('#export-summary-md-btn')).toBeEnabled();
        }
        if (outcome === 'reset') await expect(page.locator('#summarise-btn')).toBeDisabled();
    });
}
