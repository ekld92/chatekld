import {test, expect} from '@playwright/test';
import {createRequire} from 'node:module';

const require = createRequire(import.meta.url);
const AXE = require.resolve('axe-core/axe.min.js');
const frame = (key, value) => `data: ${JSON.stringify({[key]: value})}\n\ndata: [DONE]\n\n`;
const review = {
    observation: {new: {new_note: ['n']}, open: []},
    renames: Array.from({length: 60}, (_, i) => ({
        unit: `u${i}`,
        name: `original_${i}_${'longname'.repeat(10)}.pdf`,
        rel: `old${i}.pdf`, proposal: `new${i}.pdf`, can_apply: true,
    })),
    tags: [],
    mirror: {plan_digest: 'd', to_create: 1},
    mirror_units: ['n'],
};

async function mount(page, body = review) {
    await page.route('**/api/tagging/zotero-available', r => r.fulfill({json: {writable: true, mode: 'auto'}}));
    await page.route('**/api/library/sessions', r => r.fulfill({json: {session: {id: 'fixture'}}}));
    await page.route('**/api/library/analyze', r => r.fulfill({
        contentType: 'text/event-stream', body: frame('library_analyze', body),
    }));
    await page.goto('/');
    await page.waitForFunction(() => !!window.selectAuditReport);
    await page.locator('#tab-audit').click();
    await page.locator('#audit-library-update-btn').click();
    await page.getByText('Scan, settings and recovery ?', {exact: true}).click();
    await page.getByRole('button', {name: 'Review using existing extraction'}).click();
}

test('mounted populated review supports narrow short keyboard use and axe', async ({page}) => {
    await page.setViewportSize({width: 1024, height: 540});
    const violations = [];
    await page.addInitScript(() => {
        window.csp = [];
        document.addEventListener('securitypolicyviolation', e => window.csp.push(e.violatedDirective));
    });
    await page.addInitScript({path: AXE});
    await mount(page);
    await page.locator('#library-run-analyse').click();
    await expect(page.locator('#library-run-review input[data-kind=rename]')).toHaveCount(60);
    await page.locator('#library-run-select-all').focus();
    await page.keyboard.press('Enter');
    await expect(page.locator('#library-run-review input:checked')).toHaveCount(61);
    await page.locator('#library-run-apply').focus();
    await page.keyboard.press('Enter');
    await expect(page.locator('.inline-confirm-strip')).toBeVisible();
    await page.screenshot({path: '/tmp/library-review-library.png'});
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const axe = await page.evaluate(async () =>
        (await window.axe.run(document.querySelector('#audit-report-body'))).violations
            .filter(v => ['serious', 'critical'].includes(v.impact)));
    expect(axe).toEqual(violations);
    expect(await page.evaluate(() => window.csp)).toEqual([]);
});

test('blocked analysis provides focus on its reason', async ({page}) => {
    await mount(page, {blocked: ['No completed scan. Run an audit scan, then analyse.']});
    await page.locator('#library-run-analyse').click();
    await expect(page.locator('#library-run-review')).toContainText('No completed scan');
    await expect(page.locator('#library-run-status')).toContainText('No completed scan');
});

test('departed section cannot announce a late analysis', async ({page}) => {
    await mount(page);
    let release;
    const gate = new Promise(r => release = r);
    await page.route('**/api/library/analyze', async r => {
        await gate;
        await r.fulfill({
            contentType: 'text/event-stream',
            body: 'data: {"info":"STALE OLD RUN"}\n\n' + frame('library_analyze', review),
        });
    });
    await page.locator('#library-run-analyse').click();
    await expect(page.locator('#library-run-status')).toContainText('Analysing');
    await page.getByRole('button', {name: 'Documents', exact: true}).click();
    release();
    await page.waitForTimeout(300);
    await expect(page.locator('#sr-status-announcer')).not.toContainText('STALE OLD RUN');
});

for (const recover of [false, true]) {
    test(`partial and uncertain results persist and expose recovery (${recover})`, async ({page}) => {
        const result = {
            session: 'fixture',
            renames: [{unit: 'u0', status: 'partial', zotero: '1 of 2 Zotero links moved'}],
            tags: [{unit: 'u1', status: 'uncertain', error: 'Connection lost'}],
            mirror: {status: 'partial', created: 1, stopped: 'cancelled'},
            not_reached: [],
        };
        await mount(page, {...review, renames: review.renames.slice(0, 2)});
        await page.route('**/api/library/apply', r => r.fulfill({
            contentType: 'text/event-stream',
            body: recover ? 'data: [DONE]\n\n' : frame('library_apply', result),
        }));
        await page.route('**/api/library/sessions/fixture', r => r.fulfill({json: {
            session: {applies: [{at: Date.now() / 1000, result}]},
        }}));
        await page.locator('#library-run-analyse').click();
        await page.locator('#library-run-select-all').click();
        await page.locator('#library-run-apply').click();
        await page.locator('.inline-confirm-strip').getByRole('button', {name: 'Apply', exact: true}).click();
        await expect(page.locator('#library-run-results')).toContainText('uncertain');
        await expect(page.locator('#library-run-results')).toContainText('partial');
        await expect(page.locator('#library-run-results')).toContainText('1 of 2');
        await expect(page.locator('#library-run-apply')).toBeDisabled();
        await expect(page.locator('#library-run-results h4')).toBeFocused();
    });
}

test('populated review at 200 percent CSS zoom keeps last controls reachable', async ({page}) => {
    await page.setViewportSize({width: 1280, height: 720});
    await mount(page);
    await page.locator('#library-run-analyse').click();
    await page.evaluate(() => document.documentElement.style.zoom = '2');
    await page.locator('#library-run-select-all').focus();
    await page.keyboard.press('Enter');
    await page.locator('#library-run-apply').focus();
    await page.keyboard.press('Enter');
    await expect(page.locator('.inline-confirm-strip')).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

async function applyFixture(page, result) {
    await mount(page, {...review, renames: review.renames.slice(0, 1)});
    await page.route('**/api/library/apply', r => r.fulfill({
        contentType: 'text/event-stream', body: frame('library_apply', result),
    }));
    await page.locator('#library-run-analyse').click();
    await page.locator('#library-run-select-all').click();
    await page.locator('#library-run-apply').click();
    await page.locator('.inline-confirm-strip').getByRole('button', {name: 'Apply', exact: true}).click();
    await expect(page.locator('#library-run-results h4')).toBeVisible();
}

test('cancelled mirror retains written counts and recovery commit', async ({page}) => {
    await applyFixture(page, {
        mirror: {status: 'partial', stopped: 'cancelled', created: 2, refreshed: 1, git_restore_commit: 'abc123'},
        tags: [], renames: [],
    });
    await expect(page.locator('#library-run-results')).toContainText('2 created, 1 refreshed');
    await expect(page.locator('#library-run-results')).toContainText('abc123');
    await expect(page.locator('#library-run-results')).not.toContainText('nothing written');
});

test('cancellation before dispatch is not announced as successful', async ({page}) => {
    await applyFixture(page, {cancelled: true, not_reached: ['u0'], renames: [], tags: []});
    await expect(page.locator('#library-run-status')).toContainText('Stopped');
    await expect(page.locator('#library-run-status')).not.toContainText('All selected items were applied');
});

test('tag receipts retain tracking uncertainty beside target receipts', async ({page}) => {
    await applyFixture(page, {
        tags: [{unit: 'u0', status: 'uncertain', targets: {finder: 'done'}, tracker: 'identity changed'}],
        renames: [],
    });
    await expect(page.locator('#library-run-results')).toContainText('finder: done');
    await expect(page.locator('#library-run-results')).toContainText('identity changed');
});

test('rename confirmation names Obsidian reference writes', async ({page}) => {
    await mount(page, {...review, mirror: null, renames: review.renames.slice(0, 1)});
    await page.locator('#library-run-analyse').click();
    await page.locator('#library-run-select-all').click();
    await page.locator('#library-run-apply').click();
    await expect(page.locator('.inline-confirm-strip')).toContainText('Obsidian');
});

test('first baseline distinguishes whole-library mirror from new-import proposals', async ({page}) => {
    await mount(page, {...review, observation: {initialized: true, new: {}}, renames: []});
    await page.locator('#library-run-analyse').click();
    await expect(page.locator('#library-run-review')).toContainText('existing notes');
    await expect(page.locator('#library-run-mirror')).toBeVisible();
});

test('analysis with no terminal reports recoverable failure', async ({page}) => {
    await mount(page);
    await page.route('**/api/library/analyze', r => r.fulfill({
        contentType: 'text/event-stream', body: 'data: [DONE]\n\n',
    }));
    await page.locator('#library-run-analyse').click();
    await expect(page.locator('#library-run-status')).toContainText('No completed analysis');
    await expect(page.locator('#library-run-analyse')).toBeEnabled();
});

test('saved run can be recovered after remount with its original absolute date', async ({page}) => {
    await applyFixture(page, {renames: [{unit: 'u0', status: 'done'}], tags: []});
    await page.route('**/api/library/sessions/fixture', r => r.fulfill({json: {session: {applies: [{
        at: 1700000000,
        result: {tags: [{unit: 'u0', status: 'partial', error: 'saved fixture'}]},
    }]}}}));
    await page.getByRole('button', {name: 'Documents', exact: true}).click();
    await page.locator('#audit-library-update-btn').click();
    await page.getByText('Scan, settings and recovery ?', {exact: true}).click();
    await page.getByRole('button', {name: 'Review using existing extraction'}).click();
    await page.getByRole('button', {name: 'Recover last run', exact: true}).click();
    // Audit remediation 2026-09-23 (U14): local time in the heading, the
    // original UTC instant in its tooltip.
    await expect(page.locator('#library-run-results h4')).toHaveAttribute('title', '2023-11-14 22:13:20 UTC');
    await expect(page.locator('#library-run-results h4')).not.toContainText('UTC');
    await expect(page.locator('#library-run-results')).toContainText('saved fixture');
});

test('changing the main tab suppresses late announcements and keeps analysis available', async ({page}) => {
    await mount(page);
    let release;
    const gate = new Promise(r => release = r);
    await page.route('**/api/library/analyze', async r => {
        await gate;
        await r.fulfill({
            contentType: 'text/event-stream',
            body: 'data: {"info":"HIDDEN TAB RUN"}\n\n' + frame('library_analyze', review),
        });
    });
    await page.locator('#library-run-analyse').click();
    await expect(page.locator('#library-run-status')).toContainText('Analysing');
    await page.locator('#tab-help').click();
    release();
    await page.waitForTimeout(300);
    await expect(page.locator('#sr-status-announcer')).not.toContainText('HIDDEN TAB RUN');
    await expect(page.locator('#sr-status-announcer')).not.toContainText('Analysis complete');
    await page.locator('#tab-audit').click();
    await expect(page.locator('#library-run-analyse')).toBeEnabled();
});

for (const width of [960, 1440]) {
    test(`populated review fits supported width ${width} in a short window`, async ({page}) => {
        await page.setViewportSize({width, height: 480});
        await mount(page);
        await page.locator('#library-run-analyse').click();
        await page.locator('#library-run-apply').focus();
        await expect(page.locator('#library-run-apply')).toBeInViewport();
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    });
}

test('online model disclosure states the estimate and the no-limit cost', async ({page}) => {
    await page.route('**/api/models**', r => r.fulfill({json: {models: ['gpt-4o-mini'], configured: 'gpt-4o-mini', provider: 'openai', error: null}}));
    await mount(page);
    await expect(page.locator('#library-run-model-block')).toBeHidden();
    await page.locator('#library-run-tags').check();
    await expect(page.locator('#library-run-model-block')).toBeVisible();
    await expect(page.locator('#library-run-cost')).toBeHidden();
    await page.locator('#library-run-provider').selectOption('openai');
    await expect(page.locator('#library-run-online-note')).toContainText(/estimate/i);
    await expect(page.locator('#library-run-online-note')).toContainText(/unpriced/i);
    await expect(page.locator('#library-run-cost')).toBeVisible();
});

test('long filenames keep proposed names inside the review scroll area', async ({page}) => {
    await page.setViewportSize({width: 960, height: 520});
    await mount(page);
    await page.locator('#library-run-analyse').click();
    await expect(page.locator('#library-run-review input[data-kind=rename]')).toHaveCount(60);
    const overflow = await page.locator('#audit-report-body').evaluate(n => n.scrollWidth - n.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
    const proposed = await page.locator('#library-run-review tr').nth(1).locator('td').last().boundingBox();
    const report = await page.locator('#audit-report-body').boundingBox();
    expect(proposed.x + proposed.width).toBeLessThanOrEqual(report.x + report.width);
});

test('section summary scopes approval to library content, not app tracking writes', async ({page}) => {
    await mount(page);
    // Plain words (audit remediation 2026-09-23, U16).
    await expect(page.locator('#audit-report-meta')).toContainText('until you tick it and confirm');
    await expect(page.locator('#audit-report-meta')).not.toContainText('app state');
});

test('late session creation from a departed mount cannot replace the recovery session', async ({page}) => {
    await mount(page);
    let release;
    let requested;
    const firstRequested = new Promise(resolve => { requested = resolve; });
    const gate = new Promise(resolve => { release = resolve; });
    let calls = 0;
    await page.route('**/api/library/sessions', async route => {
        const first = ++calls === 1;
        if (first) { requested(); await gate; }
        await route.fulfill({json: {session: {id: first ? 'departed' : 'current'}}});
    });
    await page.locator('#library-run-analyse').click();
    await firstRequested;
    await page.getByRole('button', {name: 'Documents', exact: true}).click();
    await page.locator('#audit-library-update-btn').click();
    await page.getByText('Scan, settings and recovery ?', {exact: true}).click();
    await page.getByRole('button', {name: 'Review using existing extraction'}).click();
    await page.locator('#library-run-analyse').click();
    await expect(page.locator('#library-run-select-all')).toBeVisible();
    const lateResponse = page.waitForResponse(response => response.url().endsWith('/api/library/sessions'));
    release();
    await lateResponse;
    await page.waitForTimeout(100);
    expect(await page.evaluate(() => localStorage.getItem('papermind-library-last-session'))).toBe('current');
});

// Lot 5 (2026-09-22): the whole section — Step 1 with the model block open,
// a populated review of every group, the results — at 1024 px, no overflow,
// no serious/critical axe violation, and the model list rendered as a real
// datalist (jsdom cannot judge any of that).
test('the update section with every group open passes axe and reflow', async ({page}) => {
    await page.setViewportSize({width: 1024, height: 700});
    await page.addInitScript({path: AXE});
    await page.route('**/api/models**', r => r.fulfill({json: {models: ['llama3.1:8b', 'mistral'], configured: 'llama3.1:8b', provider: 'ollama', error: null}}));
    const full = {...review, renames: review.renames.slice(0, 3),
        pdf_links: {counts: {}, rows: [
            {citation_key: 'a2020', state: 'absent', expected: ['a_2020.pdf'], listed: [], note: 'a2020.md', selectable: true},
            {citation_key: 'b2020', state: 'dirty', expected: ['b_2020.pdf'], listed: ['hand'], note: 'b2020.md', selectable: false, reason: 'a pdf: list this app did not write; edit it by hand'},
        ]},
        tags: [{unit: 'u0', citation_key: 'jones2026', source: 'pdf', sent: {online: false, provider: 'ollama'}, proposed: [{tag: 'dep'}, {tag: 'mood'}]}]};
    await mount(page, full);
    await page.route('**/api/library/apply', r => r.fulfill({contentType: 'text/event-stream', body: frame('library_apply', {
        mirror: {status: 'done', created: 1, refreshed: 0}, pdf_links: [{citation_key: 'a2020', status: 'done', changed: true}],
        tags: [{unit: 'u0', status: 'done', targets: {finder: 'done', zotero: 'done', note: 'skipped'}}],
        renames: [{unit: 'u0', from: 'old0.pdf', to: 'new0.pdf', status: 'done'}], notes_written: 2})}));
    await page.locator('#library-run-tags').check();
    await expect(page.locator('#library-run-model')).toHaveValue('llama3.1:8b');
    expect(await page.locator('#library-run-models option').count()).toBe(2);
    await page.locator('#library-run-analyse').click();
    await expect(page.locator('fieldset .library-run-group-head h4')).toHaveCount(4);
    const cells = await page.locator('.library-run-chips').evaluate(node => {
        const cell = node.closest('td');
        const source = cell.nextElementSibling;
        return {display: getComputedStyle(cell).display, chipTop: cell.getBoundingClientRect().top,
            sourceTop: source.getBoundingClientRect().top};
    });
    expect(cells.display).toBe('table-cell');
    expect(Math.abs(cells.chipTop - cells.sourceTop)).toBeLessThan(10);
    await page.getByText('Article overview · names, tags and extraction', {exact: true}).click();
    await page.screenshot({path: '/tmp/library-article-overview.png'});
    expect(await page.locator('.library-article-overview table').evaluate(table => table.scrollWidth <= table.clientWidth + 1)).toBe(true);
    await page.locator('#library-run-select-all').click();
    await page.locator('#library-run-apply').click();
    await expect(page.locator('.inline-confirm-strip')).toContainText('Zotero entry');
    await page.locator('.inline-confirm-strip').getByRole('button', {name: 'Apply', exact: true}).click();
    await expect(page.locator('#library-run-results h5')).toHaveCount(4);
    await expect(page.locator('#library-run-results')).toContainText('uncommitted');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const axe = await page.evaluate(async () =>
        (await window.axe.run(document.querySelector('#audit-report-body'))).violations
            .filter(v => ['serious', 'critical'].includes(v.impact)).map(v => v.id));
    expect(axe).toEqual([]);
    await page.screenshot({path: 'test-results/library-update-full-1024.png', fullPage: true});
});
