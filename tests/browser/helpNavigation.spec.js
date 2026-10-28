import {test, expect} from '@playwright/test';

test('How to opens the daily guide and its internal troubleshooting links work', async ({page}) => {
    await page.goto('/');
    await page.waitForFunction(() => typeof window.openHowTo === 'function');
    await page.evaluate(() => window.openHowTo());
    const guide = page.locator('#help-guide-body');
    const daily = guide.locator('a').filter({hasText: 'Daily vault and search update'}).first();
    await expect(daily).toBeVisible();
    await daily.click();
    await expect(page).toHaveURL(/#help-section-daily-vault-and-search-update$/);
    await expect(guide.locator('#help-section-daily-vault-and-search-update')).toBeInViewport();
    const troubleshooting = guide.locator('a').filter({hasText: 'When to stop, inspect or retry'}).first();
    await troubleshooting.click();
    await expect(page).toHaveURL(/#help-section-when-to-stop-inspect-or-retry$/);
    await expect(guide.locator('#help-section-when-to-stop-inspect-or-retry')).toBeInViewport();
    await expect(guide).toContainText('initial_memory_pressure');
    const broken = await guide.locator('a[href^="#"]').evaluateAll(links =>
        links.map(link => link.getAttribute('href')).filter(href => !document.getElementById(href.slice(1))));
    expect(broken).toEqual([]);
});
