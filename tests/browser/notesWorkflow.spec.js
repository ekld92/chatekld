import {test, expect} from '@playwright/test';
import {createRequire} from 'node:module';

const axe = createRequire(import.meta.url).resolve('axe-core/axe.min.js');
const rows = Array.from({length: 60}, (_, i) => ({
    citation_key: `paper${i}`,
    state: i % 2 ? 'pull' : 'missing_obsidian',
    missing_words: 12,
    extra_words: 0,
    region_changed: false,
    zotero_changed: true,
    baselined: true,
    note_rel: `Z_Zotero_Notes/paper${i}.md`,
}));
const plan = {
    dry_run: true, ok: true, stopped: null,
    git_clean: true, git_detail: '', git_restore_commit: 'a'.repeat(40),
    plan_digest: 'reviewed-fixture', scanned_at: 1758000000,
    to_create: 30, create_total: 30, create_capped_at: null,
    create_keys: rows.filter((_, i) => i % 2 === 0).map(r => r.citation_key),
    to_refresh: 30,
    refresh_keys: rows.filter((_, i) => i % 2).map(r => r.citation_key),
    to_baseline: 60, unchanged: 0, lossy_markup: ['paper1'],
    listed: {}, stale_bases: 1, skipped: {create: {}},
    created: 0, refreshed: 0, bootstrapped: 0, bases_written: 0,
    failed: {}, aborted: false,
};

async function setup(page) {
    await page.addInitScript({path: axe});
    await page.route('**/api/notesync/**', route => {
        const p = new URL(route.request().url()).pathname;
        let json = {};
        if (p.endsWith('/status')) json = {with_reading_note: 60, ok: 0, missing: [], drifted: []};
        if (p.endsWith('/sync-status')) {
            json = {authoritative: true, total: 60, counts: {missing_obsidian: 30, pull: 30}, rows};
        }
        if (p.endsWith('/key-changes')) json = {count: 0, rows: []};
        if (p.endsWith('/create-attempts')) json = {rows: []};
        if (p.endsWith('/mirror')) {
            const body = route.request().postDataJSON();
            const result = body.confirm
                ? {...plan, dry_run: false, ok: false, stopped: 'create_failed', created: 1, error: 'fixture disk full'}
                : plan;
            return route.fulfill({
                contentType: 'text/event-stream',
                body: `data: ${JSON.stringify({mirror: result})}\n\ndata: [DONE]\n\n`,
            });
        }
        return route.fulfill({json});
    });
    await page.goto('/');
    await page.waitForFunction(() => !!window.selectAuditReport);
    await page.locator('#tab-audit').click();
    await page.evaluate(() => window.selectAuditReport('note_sync'));
    await expect(page.locator('[data-action="notesync-mirror"]')).toBeVisible();
}

test('mounted long notes table axe and short viewport confirmation', async ({page}) => {
    await page.setViewportSize({width: 960, height: 520});
    await setup(page);
    await page.locator('[data-action="notesync-mirror"]').click();
    await expect(page.getByRole('button', {name: 'Mirror 60', exact: true})).toBeVisible();
    await page.screenshot({path: '/tmp/library-review-notes.png'});
    const violations = await page.evaluate(async () =>
        (await window.axe.run(document.querySelector('#audit-tab'))).violations
            .filter(v => ['serious', 'critical'].includes(v.impact))
            .map(v => ({id: v.id, nodes: v.nodes.map(n => ({target: n.target, summary: n.failureSummary}))})));
    expect(violations).toEqual([]);
    await page.getByRole('button', {name: 'Mirror 60', exact: true}).focus();
    await page.keyboard.press('Enter');
    await expect(page.locator('#audit-ns-status')).toContainText('Created 1');
    await expect(page.locator('#audit-ns-status')).toContainText('fixture disk full');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('restore filled modal axe and extreme controls in short viewport', async ({page}) => {
    await page.setViewportSize({width: 960, height: 520});
    await page.addInitScript({path: axe});
    await page.route('**/api/history/manifest', r => r.fulfill({json: {
        ops: Array.from({length: 60}, (_, i) => ({
            id: `op${i}`, kind: i % 2 ? 'rename_note' : 'create_note', state: 'applied',
            note_rel: `Z_Zotero_Notes/paper${i}.md`, old_rel: `Z_Zotero_Notes/old${i}.md`,
            ts: '2026-09-21T10:00:00Z',
        })),
    }}));
    await page.goto('/');
    await page.waitForFunction(() => !!window.historyOpenRestore);
    await page.locator('#app-settings-btn').click();
    await page.locator('#settings-modal summary').filter({hasText: 'History and Restore'}).click();
    await page.getByRole('button', {name: 'History and Restore…', exact: true}).click();
    await expect(page.locator('.history-restore-op')).toHaveCount(60);
    const modal = page.locator('#history-restore-modal');
    const violations = await page.evaluate(async () =>
        (await window.axe.run(document.querySelector('#history-restore-modal'))).violations
            .filter(v => ['serious', 'critical'].includes(v.impact))
            .map(v => ({id: v.id, nodes: v.nodes.map(n => n.target)})));
    expect(violations).toEqual([]);
    const last = modal.locator('.history-restore-op button').last();
    await last.focus();
    await expect(last).toBeInViewport();
    await page.keyboard.press('Escape');
    await expect(modal).not.toBeVisible();
});

test('Restore preserves a refused-operation result after reloading the journal', async ({page}) => {
    await page.route('**/api/history/manifest', r => r.fulfill({json: {ops: [{
        id: 'op1', kind: 'create_note', state: 'applied',
        note_rel: 'Z_Zotero_Notes/edited.md', ts: '2026-09-21T10:00:00Z',
    }]}}));
    await page.route('**/api/history/restore', r => r.fulfill({json: {results: [{
        ok: false, status: 'skipped', message: 'The note changed since creation — left in place.',
    }]}}));
    await page.goto('/');
    await page.waitForFunction(() => !!window.showTab);
    await page.evaluate(async () => {
        const m = await import('/static/js/history.js');
        await m.openRestore();
    });
    await page.getByRole('button', {name: 'Delete Created note: Z_Zotero_Notes/edited.md', exact: true}).click();
    await page.locator('#history-restore-modal .inline-confirm-strip').getByRole('button', {name: 'Delete note', exact: true}).click();
    await expect(page.locator('#history-restore-status')).toContainText('left in place');
    await expect(page.locator('#history-restore-status')).toBeFocused();
});

test('Revert all confirmation discloses deletion of created mirror notes', async ({page}) => {
    await page.route('**/api/history/manifest', r => r.fulfill({json: {ops: [{
        id: 'op1', kind: 'create_note', state: 'applied',
        note_rel: 'Z_Zotero_Notes/new.md', ts: '2026-09-21T10:00:00Z',
    }]}}));
    await page.goto('/');
    await page.waitForFunction(() => !!window.showTab);
    await page.evaluate(async () => {
        const m = await import('/static/js/history.js');
        await m.openRestore();
    });
    await page.locator('#history-restore-all').click();
    await expect(page.locator('#history-restore-modal .inline-confirm-strip')).toContainText(/delet/i);
});

test('Notes Sync help stays readable when pointer moves onto tooltip', async ({page}) => {
    await setup(page);
    const badge = page.getByRole('button', {name: /Help: (audit-rtab-note_sync|Zotero Notes Sync)/, exact: true});
    await badge.hover();
    const tip = page.locator('#help-tip-shared');
    await expect(tip).toBeVisible();
    const rect = await tip.boundingBox();
    await page.mouse.move(rect.x + rect.width - 12, rect.y + rect.height - 12);
    await page.waitForTimeout(350);
    await expect(tip).toBeVisible();
    expect(await page.evaluate(
        ({x, y}) => document.elementFromPoint(x, y)?.closest('#help-tip-shared')?.id,
        {x: rect.x + rect.width - 12, y: rect.y + rect.height - 12},
    )).toBe('help-tip-shared');
    await page.keyboard.press('Escape');
    await expect(tip).not.toBeVisible();
    await badge.focus();
    await expect(tip).toBeVisible();
});
