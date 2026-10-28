import {test, expect} from '@playwright/test';

test('direct daily search update requires confirmation and skips library analysis', async ({page}, info) => {
    const starts = [];
    const forbidden = [];
    let state = {state: 'idle'};
    for (const path of ['sessions', 'observe', 'analyze', 'apply']) {
        await page.route(`**/api/library/${path}`, r => {
            forbidden.push(path);
            return r.fulfill({status: 500, json: {error: 'Unexpected library operation'}});
        });
    }
    await page.route('**/api/online-ocr/status', r => r.fulfill({json: state}));
    await page.route('**/api/online-ocr/session/preview', r => r.fulfill({json: {
        eligible: 2, external_eligible: 2, source_count: 3, reused_count: 8,
        pending_count: 1, session_token: 'direct-daily', sources: []
    }}));
    await page.route('**/api/online-ocr/session/start', r => {
        starts.push(r.request().postDataJSON());
        state = {state: 'indexed', run_index: true, run_id: 'd'.repeat(32), bm25_status: 'ready'};
        return r.fulfill({json: {ok: true, run_id: state.run_id}});
    });
    await page.setViewportSize({width: 960, height: 640});
    await page.goto('/');
    await page.waitForFunction(() => !!window.selectAuditReport);
    for (const [id, label] of [['obsidian', 'Ask'], ['document', 'Read'], ['audit', 'Library'], ['deck', 'Create']]) {
        await expect(page.locator('#tab-' + id)).toHaveAccessibleName(label, {exact: true});
    }
    await page.locator('#tab-audit').click();
    await page.locator('summary').filter({hasText: 'Scan, settings and recovery'}).click();
    await page.locator('#library-daily-update').click();
    await expect(page.locator('.inline-confirm-strip')).toContainText('No external calls');
    expect(starts).toHaveLength(0);
    expect(forbidden).toEqual([]);
    await page.screenshot({path: info.outputPath('direct-daily-preview.png')});
    await page.getByRole('button', {name: 'Update using available text', exact: true}).click();
    await expect.poll(() => starts.length).toBe(1);
    expect(starts[0]).toMatchObject({initiating_action: 'search_only', prepare_only: false, no_external: true,
        confirm_external: false, confirm_index: true, full_rebuild: false,
        confirm_full_rebuild: false, reextract: false, retry_failed: false});
    expect(starts[0]).not.toHaveProperty('embed');
    expect(forbidden).toEqual([]);
    await expect(page.locator('#library-preparation-status')).toContainText('Lexical search ready and saved');
});

test('resource recovery checks memory without replaying preparation', async ({page}, info) => {
    let starts = 0;
    let checks = 0;
    await page.route('**/api/online-ocr/status', r => r.fulfill({json: {
        state: 'extraction_incomplete', global_failure: true, source_count: 19,
        result_count: 1, not_attempted_count: 18,
        failure: {stop_reason: 'initial_memory_pressure', failure_scope: 'global',
            error_detail: 'Document preparation stopped because macOS reported memory pressure before processing.',
            next_step: 'Review unfinished preparation. This is a resource stop, not a defective document.'}
    }}));
    await page.route('**/api/online-ocr/resources', r => {
        checks++;
        return r.fulfill({json: {state: 'warning', message: 'Warning pressure; bounded preparation is allowed.'}});
    });
    await page.route('**/api/online-ocr/session/start', r => { starts++; return r.abort(); });
    await page.goto('/');
    await expect(page.locator('#daily-resource-actions')).toBeVisible();
    await page.locator('#daily-resource-actions').getByRole('button', {name: 'Check memory'}).click();
    await expect(page.locator('#daily-memory-status')).toContainText('No operation was started');
    await expect(page.locator('#daily-index-status')).toContainText('18 not attempted');
    await expect(page.locator('#daily-index-status')).not.toContainText('review failed documents');
    expect(checks).toBe(1);
    expect(starts).toBe(0);
    await page.screenshot({path: info.outputPath('resource-recovery.png')});
});

test('daily entry avoids a synthetic 7300-record inventory', async ({page}, info) => {
    const records = Array.from({length: 7300}, (_, i) => ({citation_key: `fixture${i}`,
        title: `Synthetic title ${i}`, pdf_count: 0, annotations_count_max: -1}));
    let inventoryRequests = 0;
    await page.route('**/api/audit/status', r => r.fulfill({json: {state: 'done', has_results: true, messages: []}}));
    await page.route('**/api/audit/summary', r => r.fulfill({json: {summary: {record_count: records.length}}}));
    await page.route('**/api/audit/inventory', r => { inventoryRequests++; return r.fulfill({json: {summary: {record_count: records.length}, records}}); });
    await page.setViewportSize({width: 960, height: 540});
    await page.goto('/');
    await page.waitForFunction(() => !!window.selectAuditReport);
    await page.locator('#tab-audit').click();
    await expect(page.locator('#library-daily-prepare')).toBeVisible();
    expect(inventoryRequests).toBe(0);
    const dailyNodes = await page.locator('#audit-report-body *').count();
    await page.screenshot({path: info.outputPath('daily-entry.png')});
    await page.evaluate(() => window.selectAuditReport('inventory'));
    await expect(page.locator('#audit-report-body tbody tr')).toHaveCount(100);
    const inventoryNodes = await page.locator('#audit-report-body *').count();
    console.log(`Synthetic inventory: ${inventoryRequests} request, ${inventoryNodes} report nodes; daily entry: ${dailyNodes} report nodes`);
    expect(inventoryNodes).toBeLessThan(1600);
    await page.locator('#audit-library-update-btn').click();
    await expect(page.locator('#audit-report-tabs')).toBeHidden();
    await expect(page.locator('#audit-search-bar')).toBeHidden();
    await expect(page.locator('#audit-summary')).toBeHidden();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('daily controls are reachable by keyboard at a narrow viewport', async ({page}, info) => {
    await page.setViewportSize({width: 700, height: 520});
    await page.goto('/');
    await page.waitForFunction(() => !!window.selectAuditReport);
    await page.locator('#tab-audit').focus();
    await page.keyboard.press('Enter');
    const prepare = page.locator('#library-daily-prepare');
    await expect(prepare).toBeVisible();
    await page.locator('#library-online-yes').focus();
    await page.keyboard.press('Space');
    await expect(page.locator('#library-online-yes')).toBeChecked();
    await page.keyboard.press('Tab');
    await expect(prepare).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(page.locator('summary').filter({hasText: 'Extraction exceptions and scope'})).toBeFocused();
    await prepare.focus();
    await prepare.scrollIntoViewIfNeeded();
    await page.screenshot({path: info.outputPath('daily-keyboard.png')});
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('central extraction settings and offline daily confirmation at narrow size', async ({page}, info) => {
    await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.fallback() : route.abort());
    await page.route('**/api/online-ocr/status', r => r.fulfill({json: {state: 'idle'}}));
    await page.route('**/api/online-ocr/runs', r => r.fulfill({json: {runs: []}}));
    await page.route('**/api/online-ocr/session/preview', r => r.fulfill({json: {
        eligible: 2, source_count: 3, reused_count: 8, pending_count: 1, maximum_attempts: 4,
        session_token: 'fixture', sources: [], selection_counts: {changed: 1}
    }}));
    let start;
    await page.route('**/api/online-ocr/session/start', r => { start = r.request().postDataJSON(); return r.fulfill({json: {ok: true}}); });
    await page.setViewportSize({width: 960, height: 540});
    await page.goto('/');
    await page.waitForFunction(() => !!window.indexVault);
    await page.locator('#app-settings-btn').click();
    await page.locator('summary').filter({hasText: 'Global · Extraction and indexing'}).click();
    await expect(page.locator('#settings-modal #online-ocr-provider')).toBeVisible();
    await expect(page.locator('#settings-modal #embed-select')).toBeVisible();
    await expect(page.locator('#index-settings-modal #online-ocr-provider')).toHaveCount(0);
    await page.locator('#vault-online-visual').check();
    await page.locator('#online-vision-model').scrollIntoViewIfNeeded();
    await page.screenshot({path: info.outputPath('extraction-settings.png')});
    await page.locator('#settings-modal').getByRole('button', {name: 'Done', exact: true}).click();
    await page.locator('#vault-index-btn').click();
    await expect(page.locator('.inline-confirm-strip')).toContainText('1 unchanged failures deferred');
    await expect(page.getByRole('button', {name: 'Continue without online calls'})).toBeVisible();
    await page.screenshot({path: info.outputPath('daily-choice.png')});
    await page.getByRole('button', {name: 'Continue without online calls'}).click();
    await expect.poll(() => start).toMatchObject({no_external: true, confirm_external: false, retry_failed: false});
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('daily preparation observes first, refuses implicit egress and recovers progress', async ({page}, info) => {
    const order = [];
    let state = {state: 'idle'};
    await page.route('**/api/library/sessions', r => { order.push('session'); return r.fulfill({json: {session: {id: 'daily'}}}); });
    await page.route('**/api/library/observe', r => { order.push('observe'); return r.fulfill({json: {initialized: true}}); });
    await page.route('**/api/online-ocr/runs', r => r.fulfill({json: {runs: []}}));
    await page.route('**/api/online-ocr/status', r => r.fulfill({json: state}));
    await page.route('**/api/online-ocr/session/preview', r => {
        order.push('preview');
        return r.fulfill({json: {eligible: 2, source_count: 2, reused_count: 6,
            session_token: 'bound', sources: [{source: 'bad.pdf', known_failure: {reason: 'reported_bad'}}]}});
    });
    await page.route('**/api/online-ocr/session/start', r => {
        const body = r.request().postDataJSON();
        expect(body).toMatchObject({initiating_action: 'library_review', operation_mode: 'prepare_text', prepare_only: true, confirm_index: false, no_external: true, confirm_external: false, retry_failed: false});
        order.push('start');
        state = {state: 'running', run_id: 'a'.repeat(32), prepare_only: true,
            current_source: 'new.pdf', result_count: 1, source_count: 2,
            started_at: Date.now() / 1000 - 3, last_activity_at: Date.now() / 1000};
        return r.fulfill({json: {ok: true, run_id: state.run_id}});
    });
    await page.goto('/');
    await page.waitForFunction(() => !!window.selectAuditReport);
    await page.locator('#tab-audit').click();
    await page.locator('#audit-library-update-btn').click();
    await expect(page.locator('#library-run-analyse')).toBeHidden();
    await page.getByRole('button', {name: 'Review library update', exact: true}).click();
    await expect(page.locator('.inline-confirm-strip')).toContainText('No external calls');
    expect(order).toEqual(['session', 'observe', 'preview']);
    await page.getByRole('button', {name: 'Prepare available text', exact: true}).click();
    await expect(page.locator('#activity-label-text')).toContainText('new.pdf');
    await page.reload();
    await page.waitForFunction(() => !!window.selectAuditReport);
    await page.locator('#tab-audit').click();
    await page.locator('#audit-library-update-btn').click();
    await expect(page.locator('#library-preparation-status')).toContainText('1/2 documents examined');
    state = {...state, state: 'text_prepared', deferred_count: 1,
        recent_results: [{source: 'bad.pdf', status: 'deferred_known_failure'}]};
    await expect(page.locator('#library-run-analyse')).toBeVisible();
    await expect(page.locator('#library-preparation-status')).toContainText('1 deferred');
    await page.getByRole('button', {name: 'Skip proposals and review search update', exact: true}).click();
    await expect(page.getByRole('button', {name: 'Update using available text', exact: true})).toBeVisible();
    expect(order.filter(value => value === 'start')).toHaveLength(1);
    // A restarted server has no live job. Saved history must not replace active status.
    const saved = {...state, sources: state.recent_results};
    await page.route('**/api/online-ocr/runs/' + 'a'.repeat(32) + '?limit=50', r => r.fulfill({json: saved}));
    state = {state: 'idle'};
    await page.reload();
    await page.waitForFunction(() => !!window.selectAuditReport);
    await page.locator('#tab-audit').click();
    await page.locator('#audit-library-update-btn').click();
    await expect(page.locator('#library-preparation-status')).toContainText('No extraction active');
    await expect(page.locator('#library-run-analyse')).toBeHidden();
    await page.screenshot({path: info.outputPath('daily-prepared.png')});
    expect(order.filter(value => value === 'start')).toHaveLength(1);
});

test('failed preparation names the provider error and opens actionable details', async ({page}, info) => {
    const status = {
        state:'extraction_incomplete',global_failure:true,
        failure:{source:'figure.png',status:'authentication_failed',failure_scope:'global',provider:'deepseek',model:'deepseek-flash',
            http_status:401,provider_code:'invalid_api_key',error_detail:'The provider rejected the API key.',
            next_step:'Check the configured provider key, then explicitly retry unfinished documents.'}
    };
    await page.route('**/api/online-ocr/status', r => r.fulfill({json:status}));
    await page.route('**/api/online-ocr/runs', r => r.fulfill({json:{runs:[]}}));
    await page.goto('/');
    await page.waitForFunction(() => !!window.selectAuditReport && document.getElementById('deck-tab')?.dataset.studioReady);
    await page.locator('#tab-audit').click();
    await page.locator('#audit-library-update-btn').click();
    const detail = page.locator('#library-extraction-exceptions');
    await expect(detail).toHaveAttribute('open','');
    await expect(detail).toContainText('HTTP 401');
    await expect(detail).toContainText('Next: Check the configured provider key');
    await expect(detail.getByRole('button',{name:'Review extraction settings'})).toBeVisible();
    await detail.scrollIntoViewIfNeeded();
    await page.screenshot({path:info.outputPath('provider-error.png')});
    await page.getByRole('button',{name:'All extraction outcomes and targeted retries'}).click();
    await expect(page.getByRole('button',{name:'History',exact:true})).toHaveAttribute('aria-pressed','true');
});

test('visibility and reconnection follow server work instead of browser history', async ({page}) => {
    let state = {state: 'idle'};
    let unavailable = false;
    let starts = 0;
    await page.addInitScript(() => localStorage.setItem('papermind-extraction-run', 'b'.repeat(32)));
    await page.route('**/api/online-ocr/status', r => unavailable
        ? r.abort('connectionfailed') : r.fulfill({json: state}));
    await page.route('**/api/online-ocr/session/start', r => { starts++; return r.fulfill({status: 500, json: {error: 'Unexpected start'}}); });
    await page.goto('/');
    await page.waitForFunction(() => !!window.selectAuditReport);
    await page.locator('#tab-audit').click();
    await expect(page.locator('#library-preparation-status')).toContainText('No extraction active');
    state = {state: 'running', run_id: 'c'.repeat(32), current_source: 'external.pdf', source_count: 4, result_count: 2};
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await expect(page.locator('#library-preparation-status')).toContainText('external.pdf');
    await expect(page.locator('#activity-label-text')).toContainText('external.pdf');
    unavailable = true;
    await page.evaluate(() => window.dispatchEvent(new Event('online')));
    await expect(page.locator('#library-preparation-status')).toContainText('Status unknown');
    await expect(page.locator('#activity-label-text')).toContainText('Status unknown');
    unavailable = false;
    state = {...state, state: 'indexed', bm25_status: 'preparing'};
    await page.evaluate(() => window.dispatchEvent(new Event('online')));
    await expect(page.locator('#library-preparation-status')).toContainText('Preparing and saving lexical');
    await expect(page.locator('#library-daily-prepare')).toBeDisabled();
    state = {...state, bm25_status: 'ready'};
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await expect(page.locator('#library-preparation-status')).toContainText('Lexical search ready and saved');
    await expect(page.locator('#library-daily-prepare')).toBeEnabled();
    expect(starts).toBe(0);
});
