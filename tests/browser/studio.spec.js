import { expect, test } from '@playwright/test';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);

test('Studio exposes all eight choices and bundled typography', async ({page}) => {
    await page.goto('/');
    await page.waitForFunction(() => document.getElementById('deck-tab')?.dataset.studioReady === '1');
    await page.locator('#tab-deck').click();
    for (const kind of ['deck', 'report']) for (const action of ['generate', 'augment']) for (const format of ['tex', 'md']) {
        await page.locator('#studio-kind').selectOption(kind);
        await page.locator('#studio-action').selectOption(action);
        await page.locator('#studio-format').selectOption(format);
        const selector = format === 'md' ? (action === 'generate' ? '#docgen-md-generate-btn' : '#studio-md-preview') : `#${kind === 'deck' ? 'deck' : 'docgen'}-${action === 'generate' ? 'generate-btn' : 'aug-preview-btn'}`;
        await expect(page.locator(selector)).toBeVisible();
    }
    await page.evaluate(() => document.fonts.ready);
    expect(await page.evaluate(() => document.fonts.check('16px "Source Sans 3"'))).toBe(true);
    expect(await page.locator('#studio-md-preview').evaluate(el => getComputedStyle(el).fontFamily)).toContain('Source Sans 3');
    await page.screenshot({path: '/tmp/papermind-studio.png'});
});

test('Index tools separates documents from operation history', async ({page}) => {
    await page.route('**/api/online-ocr/runs', route => route.fulfill({json:{runs:[
        {run_id:'a'.repeat(32), state:'extraction_incomplete', source_count:33, deferred_count:30, recorded_at:1793040000, retry_allowed:true},
        {run_id:'b'.repeat(32), state:'indexed', source_count:277, recorded_at:1793030000}
    ]}}));
    await page.setViewportSize({width: 960, height: 700});
    await page.goto('/');
    await page.waitForFunction(() => document.getElementById('deck-tab')?.dataset.studioReady === '1');
    await page.evaluate(() => window.openModal('index-settings-modal'));
    await page.getByRole('button', {name:'History', exact:true}).click();
    await expect(page.locator('#online-run-history')).toBeVisible();
    await expect(page.locator('#online-source-search')).toBeHidden();
    await page.screenshot({path:'/tmp/papermind-history.png'});
    await page.getByRole('button', {name:'Documents', exact:true}).click();
    await expect(page.locator('#online-source-search')).toBeVisible();
    await expect(page.locator('#online-run-history')).toBeHidden();
    await page.screenshot({path:'/tmp/papermind-index-tools.png'});
});

test('Markdown additions require a separate apply confirmation', async ({page}) => {
    let applied = 0;
    await page.route('**/api/docgen/markdown-preview', route => route.fulfill({
        contentType:'text/event-stream', body:'data: '+JSON.stringify({markdown_preview:{token:'a'.repeat(32),path:'report.md',addition:'## New section\nGrounded additions'}})+'\n\ndata: [DONE]\n\n'
    }));
    await page.route('**/api/docgen/markdown-apply', route => {
        expect(route.request().postDataJSON()).toEqual({token:'a'.repeat(32),confirm:true});
        applied++;
        return route.fulfill({json:{ok:true,path:'report.md'}});
    });
    await page.goto('/');
    await page.waitForFunction(() => document.getElementById('deck-tab')?.dataset.studioReady === '1');
    await page.locator('#tab-deck').click();
    await page.locator('#studio-action').selectOption('augment');
    await page.locator('#studio-format').selectOption('md');
    await page.locator('#studio-md-path').fill('report.md');
    await page.locator('#studio-md-instruction').fill('Add limitations');
    await page.locator('#studio-md-preview').click();
    await expect(page.locator('#studio-md-result')).toContainText('Grounded additions');
    expect(applied).toBe(0);
    await page.getByRole('button',{name:'Apply additions',exact:true}).click();
    expect(applied).toBe(0);
    await page.locator('.inline-confirm-strip button').last().click();
    await expect(page.locator('#studio-md-status')).toContainText('Saved report.md');
    expect(applied).toBe(1);
    await expect(page.locator('#activity-label-text')).toContainText('1 completed this session');
});

test('cancelled Markdown preview is not an attention failure', async ({page}) => {
    await page.route('**/api/docgen/markdown-preview', () => {});
    await page.goto('/');
    await page.waitForFunction(() => document.getElementById('deck-tab')?.dataset.studioReady === '1');
    await page.locator('#tab-deck').click();
    await page.locator('#studio-action').selectOption('augment');
    await page.locator('#studio-format').selectOption('md');
    await page.locator('#studio-md-path').fill('report.md');
    await page.locator('#studio-md-instruction').fill('Add limitations');
    await page.locator('#studio-md-preview').click();
    await page.locator('#studio-md-cancel').click();
    await expect(page.locator('#studio-md-status')).toHaveText('Preview cancelled.');
    await page.locator('#activity-console-details summary').click();
    await expect(page.locator('#activity-tasks .operation-state')).toHaveText('Cancelled');
    await expect(page.locator('#activity-label-text')).not.toContainText('need attention');
    await expect(page.locator('#activity-label-text')).not.toContainText('completed this session');
});

test('populated operation history can be focused and scrolled with the keyboard', async ({page}) => {
    await page.addInitScript({path:require.resolve('axe-core/axe.min.js')});
    await page.setViewportSize({width:960, height:600});
    await page.goto('/');
    await page.waitForFunction(() => document.getElementById('deck-tab')?.dataset.studioReady === '1');
    await page.evaluate(async () => {
        const ui = await import('/static/js/ui.js');
        for (let n=0; n<25; n++) {
            ui.taskBegin(`fixture-${n}`, `Operation ${n}`);
            ui.taskEnd(`fixture-${n}`, 'completed');
        }
    });
    await page.locator('#activity-console-details summary').click();
    const operations = page.getByRole('region', {name:'Operations', exact:true});
    await page.locator('#activity-console-details summary').focus();
    await page.keyboard.press('Tab');
    await expect(operations).toBeFocused();
    await expect(operations).toHaveCSS('outline-style', 'solid');
    expect(await page.evaluate(async () => (await axe.run(document.getElementById('activity-bar'))).violations.filter(v => ['serious','critical'].includes(v.impact)).map(v => v.id))).toEqual([]);
    await page.keyboard.press('PageDown');
    await expect.poll(() => operations.evaluate(el => el.scrollTop)).toBeGreaterThan(0);
});

for (const theme of ['light', 'dark']) test(`Index view selection keeps contrast on hover in ${theme} theme`, async ({page}) => {
    await page.goto('/');
    await page.waitForFunction(() => document.getElementById('deck-tab')?.dataset.studioReady === '1');
    await page.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
    await page.evaluate(() => window.openModal('index-settings-modal'));
    const selected = page.getByRole('button', {name:'Documents', exact:true});
    await selected.click();
    await page.mouse.move(0, 0);
    const colors = el => {
        const style = getComputedStyle(el);
        return {fg:style.color, bg:style.backgroundColor, accent:style.getPropertyValue('--accent').trim()};
    };
    const resting = await selected.evaluate(colors);
    await selected.hover();
    const hovered = await selected.evaluate(colors);
    expect(hovered.fg).toBe(resting.fg);
    expect(hovered.bg).toBe(resting.bg);
    // Opaque selected colors must meet ordinary text contrast, including hover.
    const contrast = await selected.evaluate(el => {
        const s = getComputedStyle(el);
        const luminance = value => value.match(/[\d.]+/g).slice(0,3).map(Number)
            .map(v => v / 255).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4)
            .reduce((sum,v,i) => sum + v * [.2126,.7152,.0722][i], 0);
        const a = luminance(s.color), b = luminance(s.backgroundColor);
        return (Math.max(a,b) + .05) / (Math.min(a,b) + .05);
    });
    expect(contrast).toBeGreaterThanOrEqual(4.5);
    const unselected = page.getByRole('button', {name:'Update options', exact:true});
    await unselected.hover();
    expect(await unselected.getAttribute('aria-pressed')).toBe('false');
    expect((await unselected.evaluate(colors)).fg).not.toBe((await unselected.evaluate(colors)).bg);
});
