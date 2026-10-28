import {test, expect} from '@playwright/test';

test('Library navigation uses saved observations, keyboard filters and distinct populations', async ({page}) => {
    const calls = [];
    await page.route('**/api/audit/library/**', route => {
        calls.push(route.request().method());
        return route.fulfill({json: {freshness: {checked_at: 100, available: true}, populations: {references: 1, files: 1, attachments: 0}, documents: [
            {id: 'ref:1', kind: 'reference', title: 'Reference without a PDF', files: [], attachments: []},
            {id: 'file:1', kind: 'file', title: 'A physical PDF with no association', row: {mapping: {keys: []}, attachment_state: 'none_observed'}},
        ]}});
    });
    const scans = [];
    page.on('request', r => { if (r.url().endsWith('/api/audit/scan')) scans.push(r.url()); });
    await page.setViewportSize({width: 1024, height: 800});
    await page.goto('/');
    await page.waitForFunction(() => !!window.showTab && !!window.selectAuditReport);
    await page.locator('#tab-audit').click();
    await page.getByRole('button', {name: 'Documents', exact: true}).click();
    await expect(page.getByText('reference: Reference without a PDF', {exact: true})).toBeVisible();
    const population = page.getByLabel('Population', {exact: true});
    await population.focus();
    await population.selectOption('reference');
    await expect(page.getByText('file: A physical PDF with no association', {exact: true})).toHaveCount(0);
    await expect(page.getByText(/1 entries shown; 1 references and 1 physical files/)).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({path: '/tmp/papermind-library-maintenance-1024.png'});
    expect(calls).toEqual(['GET']);
    expect(scans).toEqual([]);
});

test('Bibliography field review keeps source claims separate and records only selected decisions', async ({page}) => {
    const campaign = {id: 'fixture', kind: 'bibliography', proposals: {input: {provider: 'ollama', model: 'fixture', max_tokens: 128}}, outcomes: {generation: {
        status: 'proposed', proposals: [{id: 'article', publication_existence: {status: 'unknown'}, pdf_correspondence: {status: 'unknown'}, citation_support: {status: 'unknown'},
            fields: [{field: 'title', current: 'Old title', proposed: 'Reviewed title', evidence: 'fixture-evidence', excerpt_verified: true, source: {locator: 'fixture:page1', text: 'Reviewed title'}, excerpt: 'Reviewed title'}]}],
    }}};
    const decisions = [];
    await page.route('**/api/audit/library/**', async route => {
        if (route.request().method() === 'POST') {
            decisions.push(route.request().postDataJSON());
            return route.fulfill({json: campaign});
        }
        return route.fulfill({json: {freshness: {}, note_campaigns: [campaign]}});
    });
    await page.setViewportSize({width: 1024, height: 800});
    await page.goto('/');
    await page.waitForFunction(() => !!window.showTab && !!window.selectAuditReport);
    await page.locator('#tab-audit').click();
    await page.getByRole('button', {name: 'History & recovery', exact: true}).click();
    await page.getByText('Bibliography campaign fixture', {exact: true}).first().click();
    await expect(page.getByText(/Semantic support: unverified/)).toBeVisible();
    await page.getByLabel('Decision', {exact: true}).selectOption('approve');
    await page.getByLabel('Explain whether the source supports this exact field and value').fill('Verified the title against the source in context.');
    await page.getByRole('button', {name: 'Review selected field decisions'}).click();
    expect(decisions).toEqual([]);
    await page.getByRole('button', {name: 'Record decisions', exact: true}).click();
    await expect(page.locator('#audit-report-body').getByText(/Decisions recorded. Other field decisions/)).toBeVisible();
    expect(decisions[0].decisions).toEqual([{record_id: 'article', field: 'title', evidence: 'fixture-evidence', approve: true, support_assessment: 'supported', reason: 'Verified the title against the source in context.', reviewed_value: 'Reviewed title'}]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({path: '/tmp/papermind-library-bibliography-review-1024.png'});
});

test('Documents reads note conflicts on demand and shows unavailable evidence explicitly', async ({page}) => {
    let noteCalls = 0;
    await page.route('**/api/audit/library/documents', route => route.fulfill({json: {
        freshness: {}, populations: {references: 1, files: 0}, documents: [{id: 'r', kind: 'reference', citation_key: 'key', title: 'Conflicting notes', attachments: []}],
    }}));
    await page.route('**/api/notesync/sync-status', route => {
        noteCalls++;
        return noteCalls === 1 ? route.fulfill({json: {rows: [{citation_key: 'key', state: 'conflict'}]}})
            : route.fulfill({status: 503, json: {error: 'Fixture source unavailable'}});
    });
    await page.goto('/');
    await page.waitForFunction(() => !!window.showTab && !!window.selectAuditReport);
    await page.locator('#tab-audit').click();
    await page.getByRole('button', {name: 'Documents', exact: true}).click();
    expect(noteCalls).toBe(0);
    await page.getByLabel('Population', {exact: true}).selectOption('note_conflict');
    await expect(page.locator('#audit-report-body').getByText(/Note comparison read at/)).toBeVisible();
    await expect(page.getByText(/1 entries shown/)).toBeVisible();
    await page.getByRole('button', {name: 'Read current note comparison', exact: true}).click();
    await expect(page.locator('#audit-report-body').getByText(/Empty results do not establish absence of conflicts/)).toBeVisible();
    expect(noteCalls).toBe(2);
});

test('Saved-only Documents entries retain their owning report and secondary actions', async ({page}) => {
    const mutations = [];
    page.on('request', request => {
        // Shell configuration persistence is separate from Library data operations.
        if (/\/api\/(audit|accession|notesync|zotlink|tagging)(\/|$)/.test(request.url()) && request.method() !== 'GET') mutations.push(request.url());
    });
    await page.route('**/api/audit/library/documents', route => route.fulfill({json: {
        freshness: {}, populations: {references: 0, files: 0, attachments: 0}, coverage: {scan_only_entries: 1},
        documents: [{id: 'file:fixture_skipped.pdf', kind: 'scan_file', title: 'fixture_skipped.pdf',
            path: 'fixture_skipped.pdf', scan_keys: [], owning_reports: ['skipped_pdfs'], applicable: false}],
    }}));
    await page.route('**/api/audit/reports/skipped_pdfs', route => route.fulfill({json: {
        count: 1, skip_prefix: 'fixture_', rows: [{pdf: {rel: 'fixture_skipped.pdf', abs: '/tmp/fixture_skipped.pdf'}}],
    }}));
    await page.goto('/');
    await page.waitForFunction(() => !!window.showTab && !!window.selectAuditReport);
    await page.locator('#tab-audit').click();
    await page.getByRole('button', {name: 'Documents', exact: true}).click();
    await page.getByLabel('Population', {exact: true}).selectOption('scan_file');
    const entry = page.getByRole('button', {name: 'scan_file: fixture_skipped.pdf', exact: true});
    await entry.focus();
    await page.keyboard.press('Enter');
    const details = page.getByRole('region', {name: 'Document details'});
    await expect(details.getByText(/Saved scan evidence only/)).toBeVisible();
    await details.getByRole('button', {name: 'Skipped PDFs', exact: true}).click();
    await expect(page.getByRole('cell', {name: 'fixture_skipped.pdf', exact: true})).toBeVisible();
    await expect(page.getByRole('button', {name: '[open]', exact: true})).toBeVisible();
    await expect(page.getByRole('button', {name: '[reveal]', exact: true})).toBeVisible();
    await page.getByRole('button', {name: 'Documents', exact: true}).click();
    await expect(page.getByLabel('Population', {exact: true})).toHaveValue('scan_file');
    await expect(entry).toBeVisible();
    expect(mutations).toEqual([]);
});
