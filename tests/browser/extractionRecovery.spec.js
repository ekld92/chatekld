import {test, expect} from '@playwright/test';

// Regressions reproduced during the 27 October audit, using isolated responses.
test('slow preparation announces its phase and blocks repeated observation through preview', async ({page}) => {
    let observeCalls = 0, previewCalls = 0;
    let releaseObserve, releasePreview;
    const observation = new Promise(resolve => { releaseObserve = resolve; });
    const preview = new Promise(resolve => { releasePreview = resolve; });
    await page.route('**/api/online-ocr/status', r => r.fulfill({json:{state:'idle'}}));
    await page.route('**/api/online-ocr/runs', r => r.fulfill({json:{runs:[]}}));
    await page.route('**/api/library/sessions', r => r.fulfill({json:{session:{id:'audit'}}}));
    await page.route('**/api/library/observe', async r => { observeCalls++; await observation; await r.fulfill({json:{initialized:true}}); });
    await page.route('**/api/online-ocr/session/preview', async r => { previewCalls++; await preview; await r.fulfill({json:{eligible:0,source_count:0,session_token:'audit'}}); });
    await page.goto('/');
    await page.waitForFunction(() => !!window.selectAuditReport);
    await page.locator('#tab-audit').click();
    const button = page.locator('#library-daily-prepare');
    await expect(button).toBeVisible();
    await button.click();
    await expect.poll(() => observeCalls).toBe(1);
    await expect(button).toBeDisabled();
    await expect(page.locator('#audit-report-body')).toContainText('Checking changes and associations');
    releaseObserve();
    await expect.poll(() => previewCalls).toBe(1);
    await expect(button).toBeDisabled();
    await expect(page.locator('#audit-report-body')).toContainText('Preparing the text preview');
    // Even a programmatic event cannot get past the controller's busy guard.
    await button.dispatchEvent('click');
    expect(observeCalls).toBe(1);
    releasePreview();
    await expect(page.locator('.inline-confirm-strip')).toBeVisible();
    await expect(button).toBeEnabled();
});

test('all-deferred preparation opens exceptions without claiming text prepared', async ({page}) => {
    await page.route('**/api/online-ocr/status', r => r.fulfill({json:{state:'text_prepared',prepare_only:true,
        source_count:12,result_count:12,deferred_count:12,recent_results:[{source:'moka_2026.pdf',status:'partial',page_statuses:['not_attempted']} ]}}));
    await page.route('**/api/online-ocr/runs', r => r.fulfill({json:{runs:[]}}));
    await page.goto('/');
    await page.waitForFunction(() => !!window.selectAuditReport);
    await page.locator('#tab-audit').click();
    await expect(page.locator('#library-run-analyse')).toBeHidden();
    await expect(page.locator('#library-preparation-status')).toContainText('No new source text prepared');
    await expect(page.locator('#library-extraction-exceptions')).not.toContainText('memory');
    await expect(page.locator('#library-extraction-exceptions')).toHaveAttribute('open', '');
});



test('targeted retry asks its own online choice and prepares only the selected source', async ({page}) => {
    const id = 'a'.repeat(32);
    let started = null;
    await page.route('**/api/online-ocr/status', r => r.fulfill({json:{state:'idle'}}));
    await page.route('**/api/online-ocr/runs', r => r.fulfill({json:{runs:[{run_id:id,state:'text_prepared',kind:'session',source_count:1}]}}));
    await page.route(`**/api/online-ocr/runs/${id}?*`, r => r.fulfill({json:{sources:[{source:'doc.pdf',source_sha256:'b'.repeat(64),status:'partial'}],source_count:1}}));
    await page.route('**/api/online-ocr/session/preview', r => {
        expect(r.request().postDataJSON()).toMatchObject({paths:['doc.pdf'],reextract:true});
        return r.fulfill({json:{eligible:1,external_eligible:0,source_count:1,session_token:'bound'}});
    });
    await page.route('**/api/online-ocr/session/start', r => {
        started = r.request().postDataJSON();
        return r.fulfill({json:{ok:true,run_id:id}});
    });
    await page.goto('/');
    await page.waitForFunction(() => !!window.chooseOnlineSources);
    // A hidden global preference must never authorize the retry.
    await page.locator('#app-settings-btn').click();
    await page.locator('#index-settings-btn').click();
    await page.getByRole('button',{name:'History',exact:true}).click();
    await page.getByRole('button',{name:'Review documents',exact:true}).click();
    await page.getByRole('button',{name:'Review targeted retry: doc.pdf',exact:true}).click();
    await expect(page.locator('.inline-confirm-strip')).toContainText('Optional online retry');
    expect(started).toBeNull();
    await page.getByRole('button',{name:'Authorize OCR and prepare text',exact:true}).click();
    await expect.poll(() => started).toMatchObject({paths:['doc.pdf'],reextract:true,prepare_only:true,
        retry_failed:true,confirm_external:true,no_external:false});
});

test('lexical preparation keeps the daily prepare button unavailable', async ({page}) => {
    await page.route('**/api/online-ocr/status', r => r.fulfill({json:{state:'indexed',bm25_status:'preparing'}}));
    await page.route('**/api/online-ocr/runs', r => r.fulfill({json:{runs:[]}}));
    await page.goto('/');
    await page.waitForFunction(() => !!window.selectAuditReport);
    await page.locator('#tab-audit').click();
    await expect(page.locator('#library-daily-prepare')).toBeDisabled();
});

test('a stalled preview times out and releases the preparation control without starting extraction', async ({page}) => {
    let previews = 0, starts = 0;
    await page.clock.install();
    await page.route('**/api/online-ocr/status', r => r.fulfill({json:{state:'idle'}}));
    await page.route('**/api/online-ocr/runs', r => r.fulfill({json:{runs:[]}}));
    await page.route('**/api/library/sessions', r => r.fulfill({json:{session:{id:'slow'}}}));
    await page.route('**/api/library/observe', r => r.fulfill({json:{initialized:true}}));
    await page.route('**/api/online-ocr/session/preview', () => { previews++; });
    await page.route('**/api/online-ocr/session/start', r => { starts++; return r.abort(); });
    await page.goto('/');
    await page.waitForFunction(() => !!window.selectAuditReport);
    await page.locator('#tab-audit').click();
    const prepare = page.locator('#library-daily-prepare');
    await prepare.click();
    await expect.poll(() => previews).toBe(1);
    await expect(prepare).toBeDisabled();
    await page.clock.fastForward(130001);
    await expect(prepare).toBeEnabled();
    await expect(page.locator('#library-operation-notice')).toContainText('Preview timed out');
    expect(starts).toBe(0);
});

test('a stalled status read releases its polling guard and reconnects', async ({page}) => {
    let polls = 0;
    await page.clock.install();
    await page.route('**/api/online-ocr/status', r => {
        polls++;
        if (polls > 1) return r.fulfill({json:{state:'indexed',bm25_status:'ready'}});
    });
    await page.route('**/api/online-ocr/runs', r => r.fulfill({json:{runs:[]}}));
    await page.goto('/');
    await expect.poll(() => polls).toBe(1);
    await page.clock.fastForward(30001);
    await page.clock.fastForward(1501);
    await expect.poll(() => polls).toBeGreaterThan(1);
    await page.waitForFunction(() => !!window.selectAuditReport);
    await page.locator('#tab-audit').click();
    await expect(page.locator('#library-preparation-status')).toContainText('Lexical search ready');
    await expect(page.locator('#library-daily-prepare')).toBeEnabled();
});

test('an uncertain start reconnects without submitting the extraction twice', async ({page}) => {
    let starts = 0;
    let state = 'idle';
    await page.clock.install();
    await page.route('**/api/online-ocr/status', r => r.fulfill({json:{state,run_id: state === 'idle' ? undefined : 'b'.repeat(32),prepare_only:true}}));
    await page.route('**/api/online-ocr/runs', r => r.fulfill({json:{runs:[]}}));
    await page.route('**/api/library/sessions', r => r.fulfill({json:{session:{id:'uncertain'}}}));
    await page.route('**/api/library/observe', r => r.fulfill({json:{initialized:true}}));
    await page.route('**/api/online-ocr/session/preview', r => r.fulfill({json:{eligible:0,source_count:1,session_token:'bound'}}));
    await page.route('**/api/online-ocr/session/start', r => { starts++; state='running'; return r.abort(); });
    await page.goto('/');
    await page.waitForFunction(() => !!window.selectAuditReport);
    await page.locator('#tab-audit').click();
    const prepare = page.locator('#library-daily-prepare');
    await prepare.click();
    await page.getByRole('button',{name:'Prepare available text',exact:true}).click();
    await expect(page.locator('#library-operation-notice')).toContainText('operation may have started');
    await expect(prepare).toBeDisabled();
    await page.clock.fastForward(1501);
    await expect(page.locator('#library-preparation-status')).toContainText('Preparing text and figures');
    state='text_prepared';
    await page.clock.fastForward(1501);
    await expect(prepare).toBeEnabled();
    expect(starts).toBe(1);
});
