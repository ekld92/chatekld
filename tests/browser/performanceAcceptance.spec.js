import {test, expect} from '@playwright/test';

// Real shell and renderer, synthetic retrieval/provider frames: no live model.
test('vault successive answers, sources and interrupted answer remain visible', async ({page}) => {
    let calls = 0;
    await page.route('**/api/obsidian/chat', route => {
        calls++;
        const frames = [
            {retrieval: [{source: 'fixture-performance.md', score: 0.9}]},
            {token: `Réponse de contrôle ${calls}.`},
        ];
        if (calls === 3) frames.push({error: 'Fixture provider unavailable'});
        return route.fulfill({contentType: 'text/event-stream', body:
            frames.map(f => `data: ${JSON.stringify(f)}\n\n`).join('') + 'data: [DONE]\n\n'});
    });
    await page.goto('/');
    await page.waitForFunction(() => typeof window.chatWithVault === 'function');
    await expect(page.locator('#obsidian-chat')).toContainText('No vault yet');
    for (let turn = 1; turn <= 3; turn++) {
        await page.locator('#obsidian-input').fill(`Question fixture ${turn}`);
        await page.locator('#vault-send-btn').click();
        await expect(page.locator('#obsidian-chat')).toContainText(`Réponse de contrôle ${turn}.`);
        await expect(page.locator('#vault-send-btn')).toBeEnabled();
    }
    expect(calls).toBe(3);
    await expect(page.locator('#obsidian-chat')).toContainText('fixture-performance.md');
    await expect(page.locator('#obsidian-chat')).toContainText('Response interrupted');
    await expect(page.locator('#vault-error-boundary')).toContainText('Fixture provider unavailable');
    await expect(page.locator('#obsidian-chat .message-user')).toHaveCount(3);
});
