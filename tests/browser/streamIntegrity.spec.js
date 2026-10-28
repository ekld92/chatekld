import {test, expect} from '@playwright/test';

const sse = frames => frames.map(f => `data: ${JSON.stringify(f)}\n\n`).join('');

test('EOF keeps short text, marks interruption, protects history and leaves Copy clear', async ({page}) => {
    await page.setViewportSize({width:960,height:700});
    let calls = 0;
    await page.route('**/api/plainchat', route => { calls++; return route.fulfill({contentType:'text/event-stream',body:sse([{token:'OK'}])}); });
    await page.goto('/');
    await page.waitForFunction(()=>!!window.chatPlain);
    await page.locator('#tab-chat').click();
    await page.locator('#plainchat-input').fill('x');
    await page.locator('#plainchat-send-btn').click();
    await expect(page.locator('#plainchat-history')).toContainText('Response interrupted');
    await expect(page.locator('#plainchat-error-boundary')).toContainText('completion marker');
    await expect(page.locator('#plainchat-stop-btn')).toBeHidden();
    expect(calls).toBe(1);
    for (const selector of ['.message-user','.message-bot']) {
        const separated = await page.locator(`#plainchat-history ${selector}`).evaluate(el => {
            const copy=el.querySelector('.copy-btn').getBoundingClientRect();
            const range=document.createRange();
            range.selectNodeContents(el.firstChild);
            const text=range.getBoundingClientRect();
            return text.top >= copy.bottom;
        });
        expect(separated).toBe(true);
    }
    await page.locator('#plainchat-new-btn').click();
    await expect(page.getByRole('button',{name:'Discard conversation',exact:true})).toBeVisible();
    await expect(page.locator('#plainchat-history .message-user')).toHaveCount(1);
    await page.screenshot({path:'test-results/chat-confirm.png',fullPage:true});
    await page.getByRole('button',{name:'Cancel',exact:true}).click();
    await expect(page.locator('#plainchat-history .message-user')).toHaveCount(1);
    await page.setViewportSize({width:960,height:1000});
    await page.screenshot({path:'test-results/stream-eof-short.png',fullPage:true});
    await page.locator('#plainchat-new-btn').click();
    await page.getByRole('button',{name:'Discard conversation',exact:true}).click();
    await expect(page.locator('#plainchat-history .message')).toHaveCount(0);
});

for (const kind of ['plain','vault']) test(`${kind} Stop preserves partial text and reports request without provider guarantee`, async ({page}) => {
    await page.goto('/');
    await page.waitForFunction(()=>!!window.chatPlain);
    // Real consumer and renderer; a deliberately blocked synthetic stream.
    await page.evaluate(kind => {
        const original=window.fetch;
        window.fetch=(url,opts)=> {
            if (url !== (kind==='plain'?'/api/plainchat':'/api/obsidian/chat')) return original(url,opts);
            let controller;
            const body=new ReadableStream({start(c){controller=c;c.enqueue(new TextEncoder().encode('data: {"token":"Partial fixture"}\n\n'));}});
            opts.signal.addEventListener('abort',()=>controller.error(new DOMException('cancelled','AbortError')),{once:true});
            return Promise.resolve(new Response(body,{headers:{'Content-Type':'text/event-stream'}}));
        };
    },kind);
    const plain=kind==='plain';
    if (plain) await page.locator('#tab-chat').click();
    await page.locator(plain?'#plainchat-input':'#obsidian-input').fill('Stop fixture');
    await page.locator(plain?'#plainchat-send-btn':'#vault-send-btn').click();
    const history=page.locator(plain?'#plainchat-history':'#obsidian-chat');
    await expect(history).toContainText('Partial fixture');
    await page.locator(plain?'#plainchat-stop-btn':'#vault-stop-btn').click();
    await expect(history).toContainText('Response interrupted');
    await expect(page.locator(plain?'#plainchat-stop-btn':'#vault-stop-btn')).toBeHidden();
    await expect(page.locator(plain?'#plainchat-send-btn':'#vault-send-btn')).toBeEnabled();
    await expect(page.locator(plain?'#plainchat-error-boundary':'#vault-error-boundary')).toContainText('provider may still be finishing');
    await page.screenshot({path:`test-results/${kind}-stop.png`,fullPage:true});
});

test('empty completed answer announces no response',async({page})=>{
    await page.route('**/api/plainchat',route=>route.fulfill({contentType:'text/event-stream',body:'data: [DONE]\n\n'}));
    await page.goto('/');
    await page.waitForFunction(()=>!!window.chatPlain);
    await page.locator('#tab-chat').click();
    await page.locator('#plainchat-input').fill('Empty fixture');
    await page.locator('#plainchat-send-btn').click();
    await expect(page.locator('#plainchat-history')).toContainText('(No response from the model.)');
    await expect(page.locator('#sr-status-announcer')).toHaveText('No response from the model.');
});

for (const receipt of [false,true]) test(`Markdown cancel exposes journal and retains received result (${receipt})`,async({page})=>{
    await page.route('**/api/history/manifest',route=>route.fulfill({json:{ops:[]}}));
    await page.goto('/');
    await page.waitForFunction(()=>!!window.docgenGenerateMarkdown);
    await page.evaluate(receipt=>{
        const original=window.fetch;
        window.fetch=(url,opts)=>{
            if(url!=='/api/docgen/generate-md')return original(url,opts);
            let controller;
            const body=new ReadableStream({start(c){controller=c;if(receipt)c.enqueue(new TextEncoder().encode('data: {"doc_md":{"rel":"fixture/result.md","section_count":1}}\n\n'));}});
            opts.signal.addEventListener('abort',()=>controller.error(new DOMException('cancelled','AbortError')),{once:true});
            return Promise.resolve(new Response(body,{headers:{'Content-Type':'text/event-stream'}}));
        };
    },receipt);
    await page.locator('#tab-deck').click();
    await page.locator('#studio-kind').selectOption('report');
    await page.locator('#studio-format').selectOption('md');
    await page.locator('#docgen-topic').fill('Synthetic report');
    await page.locator('#docgen-md-generate-btn').click();
    await page.getByRole('button',{name:'Generate & write',exact:true}).click();
    if(receipt)await expect(page.locator('#docgen-md-result')).toContainText('fixture/result.md');
    await page.getByRole('button',{name:'Cancel the running generation',exact:true}).click();
    await expect(page.locator('#docgen-md-status')).toContainText('may already have been written');
    if(receipt)await expect(page.locator('#docgen-md-result')).toContainText('fixture/result.md');
    await page.getByRole('button',{name:'Open History and Restore',exact:true}).scrollIntoViewIfNeeded();
    await page.screenshot({path:`test-results/docgen-cancel-${receipt}.png`,fullPage:true});
    await page.getByRole('button',{name:'Open History and Restore',exact:true}).click();
    await expect(page.locator('#history-restore-modal')).toBeVisible();
    await expect(page.locator('#history-restore-status')).toContainText('No vault changes recorded yet');
});

test('vault retrieval sources survive EOF before any answer in display and export',async({page})=>{
    let exported='';
    await page.route('**/api/obsidian/chat',route=>route.fulfill({contentType:'text/event-stream',body:sse([{retrieval:[{source:'fixture-source.md',score:0.8}]}])}));
    await page.route('**/api/export-summary',route=>{exported=route.request().postDataJSON().content;return route.fulfill({json:{path:'fixture-export.md'}});});
    await page.goto('/');
    await page.waitForFunction(()=>!!window.chatWithVault);
    await page.locator('#obsidian-input').fill('Synthetic source');
    await page.locator('#vault-send-btn').click();
    await expect(page.locator('#vault-error-boundary')).toContainText('completion marker');
    await expect(page.locator('#obsidian-chat')).toContainText('fixture-source.md');
    await page.evaluate(()=>window.exportVaultConversation('md'));
    expect(exported).toContain('fixture-source.md');
    expect(exported).toContain('[interrupted]');
});

for (const kind of ['plain', 'vault']) test(`${kind} visible unanswered transcript still requires discard confirmation`, async ({page}) => {
    const plain = kind === 'plain';
    await page.route(plain ? '**/api/plainchat' : '**/api/obsidian/chat', route => route.fulfill({
        contentType: 'text/event-stream',
        body: plain ? sse([{error: 'Synthetic refusal before tokens'}]) + 'data: [DONE]\n\n' : 'data: [DONE]\n\n',
    }));
    await page.goto('/');
    await page.waitForFunction(() => !!window.chatPlain);
    if (plain) await page.locator('#tab-chat').click();
    await page.locator(plain ? '#plainchat-input' : '#obsidian-input').fill('Unanswered question');
    await page.locator(plain ? '#plainchat-send-btn' : '#vault-send-btn').click();
    await expect(page.locator(plain ? '#plainchat-send-btn' : '#vault-send-btn')).toBeEnabled();
    const history = page.locator(plain ? '#plainchat-history' : '#obsidian-chat');
    await expect(history).toContainText('Unanswered question');
    await page.locator(plain ? '#plainchat-new-btn' : '#vault-clear-chat-btn').click();
    await expect(page.getByRole('button', {name: 'Discard conversation', exact: true})).toBeVisible();
    await page.getByRole('button', {name: 'Cancel', exact: true}).click();
    await expect(history).toContainText('Unanswered question');
});

test('uncertain Markdown publication retains operation receipt without claiming success', async ({page}) => {
    await page.route('**/api/docgen/generate-md', route => route.fulfill({
        contentType: 'text/event-stream', body: sse([
            {doc_md: {rel: 'fixture/uncertain.md', op_id: 'fixture-op', publication_uncertain: true}},
            {error: 'Synthetic publication uncertainty'},
        ]) + 'data: [DONE]\n\n',
    }));
    await page.goto('/');
    await page.waitForFunction(() => !!window.docgenGenerateMarkdown);
    await page.locator('#tab-deck').click();
    await page.locator('#studio-kind').selectOption('report');
    await page.locator('#studio-format').selectOption('md');
    await page.locator('#docgen-topic').fill('Synthetic uncertain publication');
    await page.locator('#docgen-md-generate-btn').click();
    await page.getByRole('button', {name: 'Generate & write', exact: true}).click();
    const result = page.locator('#docgen-md-result');
    await expect(result).toContainText('Publication uncertain — fixture/uncertain.md (operation fixture-op)');
    await expect(result).not.toContainText('Wrote');
    await expect(result).not.toContainText('Reversible');
    await expect(result.getByRole('button', {name: 'Retry', exact: true})).toHaveCount(0);
    await expect(result.getByRole('button', {name: 'Open History and Restore', exact: true})).toBeVisible();
});

test('Markdown Start fresh is explicitly confirmed and sent only after approval', async ({page}) => {
    const payloads = [];
    await page.route('**/api/docgen/generate-md', route => {
        payloads.push(route.request().postDataJSON());
        return route.fulfill({contentType: 'text/event-stream', body: 'data: [DONE]\n\n'});
    });
    await page.goto('/');
    await page.waitForFunction(() => !!window.docgenGenerateMarkdown);
    await page.locator('#tab-deck').click();
    await page.locator('#studio-kind').selectOption('report');
    await page.locator('#studio-format').selectOption('md');
    await page.locator('#docgen-topic').fill('Synthetic checkpoint recovery');
    await page.locator('#docgen-force-fresh').check();
    await page.locator('#docgen-md-generate-btn').click();
    await expect(page.getByText(/This run will start without reusing saved progress/)).toBeVisible();
    expect(payloads).toHaveLength(0);
    await page.getByRole('button', {name: 'Cancel', exact: true}).click();
    expect(payloads).toHaveLength(0);
    await page.locator('#docgen-md-generate-btn').click();
    await page.getByRole('button', {name: 'Generate & write', exact: true}).click();
    await expect.poll(() => payloads.length).toBe(1);
    expect(payloads[0]).toMatchObject({force_fresh: true, confirm: true});
    await expect(page.locator('#docgen-md-generate-btn')).toBeEnabled();
    await page.locator('#docgen-force-fresh').uncheck();
    await page.locator('#docgen-md-generate-btn').click();
    await page.getByRole('button', {name: 'Generate & write', exact: true}).click();
    await expect.poll(() => payloads.length).toBe(2);
    expect(payloads[1]).toMatchObject({force_fresh: false, confirm: true});
});
