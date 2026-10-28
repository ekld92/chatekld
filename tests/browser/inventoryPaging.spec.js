import {test, expect} from '@playwright/test';

async function inventory(page) {
    const records = Array.from({length:7300}, (_,i)=>({citation_key:`fixture${String(i).padStart(4,'0')}`,
        title:`Synthetic ${i} ${i===7299?'Éléphant '+ 'x'.repeat(100)+' distantword':''}`, year:String(1900+i%120),
        pdf_count:i%7, annotations_count_max:-1, pdf_paths:[], match_sources:[]}));
    records[0].pdf_paths = [{rel:'synthetic.pdf',abs:'/fixture/synthetic.pdf',source:'manual'}];
    await page.route('**/api/audit/status',r=>r.fulfill({json:{state:'done',has_results:true,messages:[]}}));
    await page.route('**/api/audit/summary',r=>r.fulfill({json:{summary:{record_count:7300}}}));
    await page.route('**/api/audit/inventory',r=>r.fulfill({json:{summary:{record_count:7300},records}}));
    // A stuck optional overlay must never block the table.
    await page.route('**/api/tagging/pdf-links-status',()=>{});
    await page.goto('/');
    await page.waitForFunction(()=>!!window.selectAuditReport);
    await page.locator('#tab-audit').click();
    await page.evaluate(()=>window.selectAuditReport('inventory'));
}

test('full inventory renders bounded pages and filters/sorts every record',async({page})=>{
    await inventory(page);
    const rows=page.locator('.audit-inv-table tbody tr[data-key]');
    await expect(rows).toHaveCount(100);
    await expect(page.locator('.table-filter-count')).toContainText('1–100 of 7300');
    await page.getByRole('button',{name:'Next page',exact:true}).click();
    await expect(rows.first()).toHaveAttribute('data-key','fixture0100');
    await page.getByRole('button',{name:'Title',exact:true}).click();
    await expect(page.locator('th[aria-sort=ascending]')).toHaveText(/Title/);
    await page.getByRole('button',{name:'PDFs',exact:true}).click();
    await page.getByRole('button',{name:'PDFs',exact:true}).click();
    await expect(rows.first().locator('td').nth(7)).toHaveAttribute('data-sort-value','6');
    const filter=page.getByRole('searchbox',{name:'Filter all inventory records'});
    await filter.fill('elephant distantword');
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toHaveAttribute('data-key','fixture7299');
    await filter.fill('never present');
    await expect(rows).toHaveCount(0);
    await expect(page.locator('.audit-inv-table')).toContainText('No records match');
    await filter.fill('');
    await expect(rows).toHaveCount(100);
    expect(await page.locator('#audit-report-body *').count()).toBeLessThan(1700);
});

test('paging retains a detail node and global search reveals an off-page row',async({page})=>{
    await inventory(page);
    await page.locator('[data-inv-detail=fixture0000]').click();
    await page.locator('.audit-inv-detail-row').evaluate(el=>el.dataset.proof='same-node');
    await page.getByRole('button',{name:'Next page',exact:true}).click();
    await expect(page.locator('.audit-inv-detail-row')).toHaveCount(0);
    await page.getByRole('button',{name:'Previous page',exact:true}).click();
    await expect(page.locator('.audit-inv-detail-row')).toHaveAttribute('data-proof','same-node');
    await page.route('**/api/audit/search?*',r=>r.fulfill({json:{q:'last',total:1,shown:1,
        groups:[{report:'inventory',count:1,rows:[{citation_key:'fixture7299',label:'Last record'}]}]}}));
    await page.locator('#audit-search-input').fill('last');
    await page.locator('.audit-search-hit').click();
    await expect(page.locator('tr[data-key=fixture7299]')).toBeVisible();
    await expect(page.locator('[data-inv-detail=fixture7299]')).toBeFocused();
});


test('a pending unmap confirmation survives a page change without submitting',async({page})=>{
    let writes=0;
    await page.route('**/api/audit/mapping',r=>{writes++;return r.abort();});
    await inventory(page);
    await page.locator('tr[data-key=fixture0000] [data-action=detach]').click();
    await expect(page.locator('.inline-confirm-strip')).toBeVisible();
    await page.locator('.inline-confirm-strip').evaluate(el=>el.dataset.proof='same-confirmation');
    await page.getByRole('button',{name:'Next page',exact:true}).click();
    await expect(page.locator('.inline-confirm-strip')).toHaveCount(0);
    await page.getByRole('button',{name:'Previous page',exact:true}).click();
    await expect(page.locator('tr[data-key=fixture0000] .inline-confirm-strip')).toHaveAttribute('data-proof','same-confirmation');
    await page.getByRole('button',{name:'Cancel',exact:true}).click();
    expect(writes).toBe(0);
});
