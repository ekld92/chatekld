import {test, expect} from '@playwright/test';

// Chantier 5 (2026-09-16): every Library view in the real app shell, in Chromium,
// with stubbed payloads — no raw JSON block, no horizontal overflow at 1024 and
// 1280 px, keyboard-reachable disclosures, and a capture of each view under
// test-results/ for the human UI review. Chromium is not WKWebView.
const payloads = {
    overview: {freshness: {checked_at: 1758000000, scan_at: 1758003600, available: true}, populations: {references: 12, files: 9, attachments: 7}, coverage: {scan_only_entries: 2}},
    documents: {freshness: {checked_at: 1758000000, available: true}, populations: {references: 2, files: 2, attachments: 1}, documents: [
        {id: 'zotero:db:1:ABCD', kind: 'reference', title: 'Sleep outcomes after a brief behavioural intervention', citation_key: 'martin2026', files: ['martin_2026.pdf'], attachments: [{key: 'ATT1', link_mode: 'linked_file', storage: 'linked', path: '/v/biblio_articles/martin_2026.pdf'}]},
        {id: 'file:martin_2026.pdf', kind: 'file', title: 'martin_2026.pdf', references: ['zotero:db:1:ABCD'], row: {pdf: {rel: 'martin_2026.pdf', name: 'martin_2026.pdf'}, mapping: {keys: ['martin2026']}, state: 'Linked', physical: {sha256: 'a', size: 1234}, attachment_state: 'observed', parents: []}, versions: [{file: 'file:martin_2026_1.pdf', relation: 'different_bytes', preserve: true}], variant: {kind: null}},
        {id: 'file:martin_2026_1.pdf', kind: 'file', title: 'martin_2026_1.pdf', references: [], row: {pdf: {rel: 'martin_2026_1.pdf', name: 'martin_2026_1.pdf'}, mapping: {keys: []}, state: 'Needs identification', physical: {sha256: 'b', size: 999}, attachment_state: 'none_observed'}, variant: {kind: 'underscore', counter: 1, base_stem: 'martin_2026'}},
    ]},
    checks: {freshness: {checked_at: 1758000000, scan_at: 1758003600, available: true}, counts: {review: 1, relationship_checked: 1}, results: [
        {id: 'file:martin_2026.pdf', pdf: 'martin_2026.pdf', status: 'relationship_checked', method: 'deterministic evidence comparison', contradictions: [], uncertainties: [], sources: {mapping: {keys: ['martin2026']}, bibliography_records: [{citation_key: 'martin2026', doi: '10.1000/corpus.2026.001', title: 'Sleep outcomes'}]}},
        {id: 'file:martin_2026_1.pdf', pdf: 'martin_2026_1.pdf', status: 'review', method: 'deterministic evidence comparison', contradictions: [], uncertainties: ['No attachment observed for this physical file.', 'PDF identity not independently confirmed; title/filename are only clues.'], sources: {mapping: {keys: []}}},
    ]},
    storage: {goal: 'Zero PDFs in Zotero storage or cloud: every PDF a linked file under the bibliography folder.', destination: '/v/biblio_articles', population: {attachments: 7}, observation: {checked_at: 1758000000, available: true}, counts: {stored: 1, broken_path: 1, linked_ok: 5},
        findings: [{attachment_key: 'STORED01', parent_title: 'An imported paper', citation_key: 'imported2019', finding: 'stored', path: 'storage:paper.pdf', guidance: ['In Zotero, select the parent item and open the attachment\'s row.', 'Attanger → Move attachment.']},
                   {attachment_key: 'BROKEN01', parent_title: 'A moved paper', citation_key: 'moved2021', finding: 'broken_path', path: '/v/old/moved_2021.pdf', guidance: ['Find the file and re-point the attachment.']}], results: [], writes: false, applicable: false, conversion_offered: false},
    history: {freshness: {checked_at: 1758000000}, note_campaigns: [
        {id: 'c0ffee', kind: 'notes', created_at: 1758000000, scope: '/v', method: 'deterministic three-way merge', proposals: {martin2026: {}, hopper2024: {}}, outcomes: {martin2026: {ok: true, at: 1758000100, obsidian: {}, zotero: {}}, hopper2024: {status: 'dispatched_or_unknown', at: 1758000200}}},
        {id: 'b1b1b1', kind: 'bibliography', created_at: 1758000000, proposals: {input: {provider: 'ollama', model: 'fixture', max_tokens: 2048}}, outcomes: {
            pdf_authentication: {reports: {rec: {status: 'authenticated', doi: {status: 'front_matter'}, title: {found: true, page: 1}, authors: {found: ['Martin']}, pages_scanned: 3, page_count: 3, reason: 'The record\'s identifiers are printed in the document\'s front matter.'}}},
            generation: {status: 'proposed', proposals: [{id: 'rec', publication_existence: {status: 'unverified', evidence_status: 'located_for_review', model_assessment: {status: 'exists', reason: 'Catalogue record observed.'}, citations: [{source_id: 'pdf_front', excerpt: 'Sleep outcomes', excerpt_verified: true}], issues: []}, pdf_correspondence: {status: 'unverified', evidence_status: 'insufficient', issues: ['No located passage supplied.']}, citation_support: {status: 'unverified', evidence_status: 'insufficient', issues: ['An exact input claim and matching analyzed claim are required.']},
                fields: [{field: 'date', current: '2025', proposed: '2026', evidence: 'ev', excerpt_verified: true, excerpt: 'Journal of Fixtures 12(3) 2026', source: {locator: 'martin_2026.pdf page 1', kind: 'pdf_passage'}, uncertainties: ['Requires human source verification.']}]}]}}}],
        pdf_operations: [{id: 'op01', key: 'martin2026', status: 'complete', at: 1758000300, steps: [{rel: 'martin_2026_1.pdf', action: 'delete', status: 'complete', recovery: '/recovery/op01/0.pdf'}], plan: {survivor: {rel: 'martin_2026.pdf'}, acknowledge_annotations: true}}]},
    links: {freshness: {}, scope: 'known scan notes', syntax_scope: 'wikilinks and markdown links', inspected_notes: 1, known_notes: 1, complete: true, notes: [{note: 'martin2026.md', status: 'candidate', links: [{target: 'martin_2026.pdf', status: 'candidate', candidates: ['file:martin_2026.pdf'], basis: 'saved scan basename link'}]}]},
};

for (const width of [1024, 1280]) {
    test(`Library views render structured content at ${width}px`, async ({page}) => {
        await page.route('**/api/audit/library/**', route => {
            const view = new URL(route.request().url()).pathname.split('/library/')[1];
            return route.fulfill({json: payloads[view] || {}});
        });
        await page.route('**/api/notesync/sync-status', route => route.fulfill({json: {rows: []}}));
        await page.setViewportSize({width, height: 900});
        await page.goto('/');
        await page.waitForFunction(() => !!window.showTab && !!window.selectAuditReport);
        await page.locator('#tab-audit').click();
        for (const view of ['overview', 'documents', 'checks', 'storage', 'history', 'links']) {
            await page.evaluate(v => window.selectAuditReport('library_' + v), view);
            const body = page.locator('#audit-report-body');
            await expect(body).not.toBeEmpty();
            expect(await body.locator('pre').count(), view).toBe(0);
            expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), view).toBe(true);
            // Every disclosure is a real <details>/<summary>, reachable by keyboard.
            const summaries = body.locator('summary');
            if (await summaries.count()) { await summaries.first().focus(); await page.keyboard.press('Enter'); }
            await page.screenshot({path: `test-results/library-${view}-${width}.png`, fullPage: true});
        }
        await page.evaluate(() => window.selectAuditReport('library_storage'));
        await expect(page.getByText('Stored in Zotero (bytes in Zotero storage / cloud)').first()).toBeVisible();
        await expect(page.getByText('imported2019').first()).toBeVisible();
        await page.evaluate(() => window.selectAuditReport('library_documents'));
        await expect(page.getByRole('button', {name: /martin_2026_1\.pdf — Identity to review/})).toBeVisible();
        await page.evaluate(() => window.selectAuditReport('library_history'));
        await page.getByText('Bibliography campaign b1b1b1', {exact: true}).first().click();
        await expect(page.getByText('PDF authentication — independent of any catalogue')).toBeVisible();
    });
}
