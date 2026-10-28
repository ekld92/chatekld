import {test, expect} from '@playwright/test';

test('PDF workspace navigation, editable suggestions, draft retention and recovery in Chromium', async ({page})=>{
    const review={status:'confirmed',chosen:{title:'One',doi:'10.1234/one'},proposal:{candidates:[{title:'One',doi:'10.1234/one'}]},candidate_ids:['c'],rejected:[],token:{id:'fixture'},pages:1};
    const first={pdf:{rel:'b/one.pdf',name:'one.pdf'},exists:true,state:'Ready to register',can_create:true,review,associations:[]};
    const second={pdf:{rel:'b/two.pdf',name:'two.pdf'},exists:true,state:'Recovery',can_create:false,review,associations:[],attempt:{stage:'parent_unknown'}};
    const data={rows:[first,second],review_scope:'fixture-browser',freshness:{available:true,checked_at:100}};
    const writes=[];
    await page.route('**/api/accession/review/**',async route=>{
        const path=new URL(route.request().url()).pathname;
        let body=review;
        if(path.endsWith('/metadata')) body={journals:['Journal of Fixtures'],creators:[{firstName:'Ada',lastName:'Test',creatorType:'author'},{name:'Fixture Society',creatorType:'author'}]};
        if(path.endsWith('/refresh-zotero')) body=data;
        if(path.endsWith('/page')) return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'Fixture page unavailable'})});
        return route.fulfill({json:body});
    });
    await page.route('**/api/zotcreate/**',async route=>{
        if(route.request().url().endsWith('/recover')) {writes.push(route.request().postDataJSON());return route.fulfill({json:{ok:true,message:'Existing attachment verified. No POST sent.'}});}
        return route.fulfill({json:{physical:{sha256:'fixture'},pdf_sha256:'fixture',review_token:review.token,item_types:['journalArticle','book'],draft:{item_type:'journalArticle',fields:{title:'One'},creators:[]}}});
    });
    await page.goto('/');
    await page.evaluate(async data=>{
        document.body.innerHTML='<main id="fixture"></main>';
        const {mountPendingReview}=await import('/static/js/pendingReview.js');
        window.fixtureTeardown=mountPendingReview(document.getElementById('fixture'),data);
    },data);
    await page.getByRole('button',{name:'one.pdf',exact:true}).click();
    await page.getByRole('button',{name:'Register/link…',exact:true}).click();
    await expect(page.getByText('Automatic Desktop undo is unavailable. Review all edits before confirming.')).toBeVisible();
    await page.locator('[data-field="publicationTitle"]').fill('My edited journal');
    await expect(page.locator('#pending-journals option')).toHaveAttribute('value','Journal of Fixtures');
    await page.getByRole('button',{name:'Add creator',exact:true}).click();
    await page.locator('[data-suggest]').selectOption('0');
    await expect(page.locator('[data-name="firstName"]')).toHaveValue('Ada');
    await page.locator('[data-name="lastName"]').fill('Edited spelling');
    await page.getByRole('button',{name:'Add creator',exact:true}).click();
    await page.locator('[data-suggest]').nth(1).selectOption('1');
    await expect(page.locator('[data-name="name"]')).toHaveValue('Fixture Society');
    await page.locator('[data-role]').nth(1).fill('editor');
    await page.getByRole('button',{name:'two.pdf',exact:true}).click();
    await expect(page.getByText('Retained operation evidence. An absent search result does not authorize another POST.')).toBeVisible();
    await page.getByRole('button',{name:'Inspect / recover acknowledged attachment'}).click();
    await expect.poll(()=>writes.length).toBe(1);
    await page.getByRole('button',{name:'one.pdf',exact:true}).click();
    await page.getByRole('button',{name:'Register/link…',exact:true}).click();
    await expect(page.locator('[data-field="publicationTitle"]')).toHaveValue('My edited journal');
    await expect(page.locator('[data-name="lastName"]')).toHaveValue('Edited spelling');
    await expect(page.locator('[data-name="name"]')).toHaveValue('Fixture Society');
    await expect(page.locator('[data-role]').nth(1)).toHaveValue('editor');
    await page.getByRole('combobox',{name:'Status',exact:true}).selectOption('Recovery');
    await expect(page.locator('tbody tr')).toHaveCount(1);
    await page.screenshot({path:'test-results/pdf-workspace.png',fullPage:true});
    await page.evaluate(()=>window.fixtureTeardown());
});

test('parent repair displays preview before a separate explicit apply', async ({page})=>{
    const review={status:'unreviewed',proposal:{candidates:[]},candidate_ids:[],rejected:[],token:{id:'repair-fixture'},pages:1};
    const row={pdf:{rel:'b/repair.pdf',name:'repair.pdf'},exists:true,state:'Discrepancies',review,associations:[{parent:'OLD'}],evidence:'observed'};
    const data={rows:[row],review_scope:'repair-fixture',freshness:{available:true}};
    const applies=[];
    await page.route('**/api/accession/review/**',async route=>{
        const path=new URL(route.request().url()).pathname;
        if(path.endsWith('/page')) return route.fulfill({status:503,json:{error:'No fixture page'}});
        if(path.endsWith('/discrepancy')) return route.fulfill({json:{preview:{token:'approved-preview',command:{old_parent:'OLD',desired_parent:'NEW',parents:[{title:'Original'},{title:'Desired'}]},message:'Review both parent records. Automatic undo is unavailable.'}}});
        if(path.endsWith('/repair')) {applies.push(route.request().postDataJSON());return route.fulfill({json:{ok:true,message:'Parent change acknowledged.'}});}
        if(path.endsWith('/refresh-zotero')) return route.fulfill({json:data});
        return route.fulfill({json:review});
    });
    await page.goto('/');
    await page.evaluate(async data=>{
        document.body.innerHTML='<main id="fixture"></main>';
        const {mountPendingReview}=await import('/static/js/pendingReview.js');
        window.fixtureTeardown=mountPendingReview(document.getElementById('fixture'),data);
    },data);
    await page.getByRole('button',{name:'repair.pdf',exact:true}).click();
    await page.getByRole('button',{name:'Preview repair of Zotero',exact:true}).click();
    await page.getByRole('button',{name:'Continue',exact:true}).click();
    await expect(page.locator('.pending-repair')).toContainText('Desired');
    expect(applies).toHaveLength(0);
    await page.getByRole('button',{name:'Confirm displayed parent change',exact:true}).click();
    await expect.poll(()=>applies.length).toBe(1);
    expect(applies[0]).toEqual({pdf:'b/repair.pdf',token:'approved-preview',confirm:true});
    await page.evaluate(()=>window.fixtureTeardown());
});


test('retained repair inspection requires a separate bookkeeping confirmation', async ({page})=>{
    const review={status:'unreviewed',proposal:{candidates:[]},candidate_ids:[],rejected:[],token:{id:'r'},pages:1};
    const row={pdf:{rel:'b/repair.pdf',name:'repair.pdf'},exists:true,state:'Recovery',review,associations:[],external_repairs:[{id:'original',status:'sent_or_unknown'}]};
    const data={rows:[row],review_scope:'inspection-fixture',freshness:{available:true}};
    const writes=[];
    await page.route('**/api/accession/review/**',async route=>{
        const path=new URL(route.request().url()).pathname;
        if(path.endsWith('/page')) return route.fulfill({status:503,json:{error:'Unavailable'}});
        if(path.endsWith('/repair-inspect')) return route.fulfill({json:{eligible:true,token:'inspected',evidence:{current:[{parentItem:'NEW'}]},message:'Inspection does not authorize undo.'}});
        if(path.endsWith('/repair-complete')) {writes.push(route.request().postDataJSON());return route.fulfill({json:{ok:true}});}
        if(path.endsWith('/refresh-zotero')) return route.fulfill({json:data});
        return route.fulfill({json:review});
    });
    await page.goto('/');
    await page.evaluate(async data=>{
        document.body.innerHTML='<main id="fixture"></main>';
        const {mountPendingReview}=await import('/static/js/pendingReview.js');
        window.fixtureTeardown=mountPendingReview(document.getElementById('fixture'),data);
    },data);
    await page.getByRole('button',{name:'repair.pdf',exact:true}).click();
    await page.getByText('Retained parent-repair operations',{exact:true}).click();
    await page.getByRole('button',{name:'Inspect retained repair',exact:true}).click();
    await page.getByRole('button',{name:'Record inspected completion',exact:true}).click();
    expect(writes).toHaveLength(0);
    await page.getByRole('button',{name:'Confirm bookkeeping completion',exact:true}).click();
    await expect.poll(()=>writes.length).toBe(1);
    expect(writes[0]).toEqual({operation:'original',token:'inspected',confirm:true});
    await page.evaluate(()=>window.fixtureTeardown());
});
