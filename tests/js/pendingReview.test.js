import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { PreviewResource, createPagePreview } from '../../static/js/pdfPreview.js';
import { mountPendingReview } from '../../static/js/pendingReview.js';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');

const reply = value => ({ok:true,json:async()=>value});
const flush = async () => { for(let i=0;i<20;i++) await Promise.resolve(); };
const token = {id:'file1',sha256:'abc',revision:1,proposal_revision:'one'};
const approved = {status:'confirmed',export_eligible:true,token,chosen:{doi:'10.1234/one',title:'First'},identity_state:'verified'};
let fetch, cleanups;
beforeEach(()=>{
    document.body.innerHTML='<div id="host"></div>'; cleanups=[];
    fetch=vi.fn(); vi.stubGlobal('fetch',fetch);
    vi.stubGlobal('navigator',{clipboard:{writeText:vi.fn().mockResolvedValue()}});
    URL.createObjectURL=vi.fn().mockReturnValue('blob:preview');
    URL.revokeObjectURL=vi.fn();
});
afterEach(()=>{cleanups.forEach(fn=>fn());vi.useRealTimers();vi.unstubAllGlobals();vi.restoreAllMocks();});
const click = text => [...document.querySelectorAll('button')].find(b=>b.textContent===text).click();
const data = () => ({review_scope:Math.random().toString(),rows:[
    {pdf:{rel:'a.pdf',name:'Alpha.pdf'},exists:true,state:'pending',review:approved},
    {pdf:{rel:'b.pdf',name:'Beta.pdf'},exists:true,state:'pending',review:{...approved,token:{...token,id:'file2'}}},
    {pdf:{rel:'c.pdf',name:'Gamma.pdf'},exists:true,state:'pending',resolution:{outcome:'ambiguous',candidates:[{doi:'10.1234/guess'}]}},
]});

describe('pending PDF review',()=>{
    it('keeps hidden selections and counts unique confirmed DOIs', async () => {
        vi.useFakeTimers();
        cleanups.push(mountPendingReview(document.querySelector('#host'),data()));
        click('Select visible confirmed');
        const input=document.querySelector('.pending-filter'); input.value='Alpha';input.dispatchEvent(new Event('input'));
        await vi.advanceTimersByTimeAsync(250);
        expect(document.querySelector('.pending-count').textContent).toContain('2 PDFs selected · 1 unique confirmed DOIs');
        expect(document.querySelector('.pending-count').textContent).toContain('1 hidden selected');
        click('Clear selection');
        expect(document.querySelector('.pending-count').textContent).toContain('0 PDFs selected');
        vi.useRealTimers();
    });
    it('exports the explicit selection through the server and preserves it after clipboard failure',async()=>{
        const included=[{pdf:'a.pdf',title:'First',doi:'10.1234/one'},{pdf:'b.pdf',title:'First',doi:'10.1234/one'}];
        fetch.mockResolvedValue(reply({included,excluded:[],dois:['10.1234/one']}));
        navigator.clipboard.writeText.mockRejectedValue(new Error('denied'));
        cleanups.push(mountPendingReview(document.querySelector('#host'),data()));
        click('Select visible confirmed'); click('Copy confirmed DOIs'); await flush();
        const request=JSON.parse(fetch.mock.calls[0][1].body);
        expect(request.items.map(x=>x.pdf)).toEqual(['a.pdf','b.pdf']);
        click('Copy these DOIs');await flush();
        expect(navigator.clipboard.writeText).toHaveBeenCalledWith('10.1234/one');
        expect(document.querySelector('textarea').value).toBe('10.1234/one');
        expect(document.querySelectorAll('input[type="checkbox"]:checked').length).toBe(2);
    });
    it('clicking a row opens review without selecting it',async()=>{
        fetch.mockResolvedValue(reply({...approved,sha256:'abc',pages:2,candidate_ids:['cand'],proposal:{candidates:[approved.chosen]}}));
        cleanups.push(mountPendingReview(document.querySelector('#host'),data()));
        click('Alpha.pdf');await flush();
        expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({pdf:'a.pdf'});
        expect(document.querySelectorAll('input[type="checkbox"]:checked').length).toBe(0);
        expect(document.querySelector('.pending-details').textContent).toContain('First');
    });
});

describe('shared PDF preview lifetime',()=>{
    it('discards a delayed response after switching documents and revokes the active URL',async()=>{
        let finish;
        fetch.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve;}))
             .mockResolvedValueOnce({ok:true,blob:async()=>new Blob(['new'])});
        const resource=new PreviewResource(); cleanups.push(()=>resource.clear());
        const old=resource.load('/old');
        const fresh=await resource.load('/new');
        finish({ok:true,blob:async()=>new Blob(['old'])});
        expect(await old).toBeNull();expect(fresh).toBe('blob:preview');
        expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
        resource.clear();expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:preview');
    });
    it('page and zoom controls request only the selected page and clear failures',async()=>{
        fetch.mockResolvedValue({ok:true,blob:async()=>new Blob(['png'])});
        cleanups.push(createPagePreview(document.querySelector('#host'),'a.pdf',{sha256:'abc',pages:5}));
        await flush();click('Next page');await flush();
        expect(fetch.mock.calls.at(-1)[0]).toContain('page=2');
        const zoom=document.querySelector('select');zoom.value='2';zoom.dispatchEvent(new Event('change'));await flush();
        expect(fetch.mock.calls.at(-1)[0]).toContain('scale=2');
        expect(document.querySelector('img').style.width).toBe('200%');
        fetch.mockResolvedValue({ok:false,status:409,json:async()=>({error:'PDF changed'})});
        click('Next page');await flush();
        expect(document.querySelector('img')).toBeNull();
        expect(document.body.textContent).toContain('PDF changed');
    });
});

it('reopens the confirmed candidate and focuses the review heading',async()=>{
    const a={title:'Candidate A',doi:'10.1234/a'},b={title:'Candidate B',doi:'10.1234/b'};
    fetch.mockResolvedValue(reply({...approved,chosen:b,sha256:'abc',pages:2,candidate_ids:['a','b'],proposal:{candidates:[a,b]}}));
    cleanups.push(mountPendingReview(document.querySelector('#host'),data()));
    const button=[...document.querySelectorAll('button')].find(b=>b.textContent==='Alpha.pdf');
    button.focus();button.click();await flush();
    expect(document.querySelector('.pending-candidates').value).toBe('b');
    expect(document.activeElement).toBe(document.querySelector('.pending-details h3'));
    const select=document.querySelector('.pending-candidates');select.focus();select.value='a';select.dispatchEvent(new Event('change'));
    expect(document.querySelector('.pending-confirmed').textContent).toContain('Candidate B');
    expect(document.activeElement).toBe(document.querySelector('.pending-candidates'));
});


it('shows attachment journal warnings and sends the selected review token',async()=>{
    const review={...approved,sha256:'abc',pages:2,candidate_ids:['cand'],proposal:{candidates:[approved.chosen]}};
    fetch.mockResolvedValue(reply(review));
    const d=data();d.rows[0].mapped_key='smith2020';
    cleanups.push(mountPendingReview(document.querySelector('#host'),d));
    click('Alpha.pdf');await flush();
    let sent=false;
    fetch.mockResolvedValue({ok:true,status:200,body:{getReader:()=>({
        read:async()=> sent ? {done:true} : (sent=true,{done:false,value:new TextEncoder().encode('data: '+JSON.stringify({zotlink_attach:{attached:1,journal_warning:'No restore record was saved',results:{}}})+'\n\ndata: [DONE]\n\n')}),
        releaseLock:()=>{},cancel:async()=>{},
    })}});
    click('Attach this PDF in Zotero…');click('Attach in Zotero');await flush();
    const request=fetch.mock.calls.find(([url])=>url==='/api/zotlink/attach');
    expect(JSON.parse(request[1].body).review_items).toEqual({'a.pdf':token});
    expect(document.querySelector('.pending-export [role="alert"]').textContent).toContain('No restore record');
});

it('authorizes Desktop only after explicit confirmation and never on mount',async()=>{
    fetch.mockResolvedValue(reply({supports_writes:true,server_id:'fixture'}));
    cleanups.push(mountPendingReview(document.querySelector('#host'),data()));
    expect(fetch).not.toHaveBeenCalled();
    click('Authorize Zotero Desktop…');await flush();
    expect(fetch.mock.calls.map(([url])=>url)).toEqual(['/api/zotlink/local-status']);
    click('Authorize');await flush();
    expect(fetch.mock.calls.at(-1)[0]).toBe('/api/zotlink/authorize-local');
    expect(JSON.parse(fetch.mock.calls.at(-1)[1].body)).toEqual({confirm:true,server_id:'fixture'});
    expect(document.querySelector('.pending-status').textContent).toContain('authorized');
});


it('previews a rename without dispatching any rename write',async()=>{
    fetch.mockResolvedValue(reply({...approved,sha256:'abc',pages:2,candidate_ids:['cand'],proposal:{candidates:[approved.chosen]}}));
    cleanups.push(mountPendingReview(document.querySelector('#host'),data()));
    click('Alpha.pdf');await flush();
    fetch.mockResolvedValue(reply({proposal:{reason:'From bibliography'},physical_filename:{current:'Alpha.pdf',proposed:'smith_2025.pdf'},linked_paths:[],reference_notes:{references:['reference.md']},limitations:['Preview only']}));
    click('[preview rename]');await flush();
    expect(fetch.mock.calls.at(-1)[0]).toBe('/api/accession/review/rename-preview');
    expect(document.querySelector('.pending-export').textContent).toContain('smith_2025.pdf');
    expect(document.querySelector('.pending-export').textContent).toContain('reference.md');
    expect(fetch.mock.calls.some(([url])=>url==='/api/maintenance/rename')).toBe(false);
});

describe('Phase 1 house chrome', () => {
    it('batch bar and queue use house button classes, not bare buttons', () => {
        cleanups.push(mountPendingReview(document.querySelector('#host'), data()));
        const batch = document.querySelector('.pending-batch');
        expect(batch.classList.contains('pending-batch')).toBe(true);
        for (const btn of batch.querySelectorAll('button')) {
            expect(btn.className).toMatch(/\bbtn\b/);
            expect(btn.type).toBe('button');
        }
        const open = document.querySelector('.pending-open');
        expect(open.classList.contains('audit-link-btn')).toBe(true);
    });

    it('review verbs use bracket labels and house classes', async () => {
        fetch.mockResolvedValue(reply({...approved, sha256:'abc', pages:2, candidate_ids:['cand'], proposal:{candidates:[approved.chosen]}}));
        cleanups.push(mountPendingReview(document.querySelector('#host'), data()));
        click('Alpha.pdf'); await flush();
        expect(document.querySelector('[data-review="confirm"]').textContent).toBe('[confirm & next]');
        expect(document.querySelector('[data-review="confirm"]').className).toMatch(/\bbtn-primary\b/);
        expect(document.querySelector('[data-review="reject"]').textContent).toBe('[wrong match]');
        expect(document.querySelector('[data-review="reject"]').classList.contains('audit-link-btn')).toBe(true);
        expect(document.querySelector('[data-review="defer"]').textContent).toBe('[skip]');
        expect(document.querySelector('[data-nav="-1"]').textContent).toBe('[previous]');
    });

    it('C / W / S / arrows drive review verbs when not typing in the filter', async () => {
        fetch.mockResolvedValue(reply({...approved, sha256:'abc', pages:2, candidate_ids:['cand'], proposal:{candidates:[approved.chosen]}, token}));
        cleanups.push(mountPendingReview(document.querySelector('#host'), data()));
        click('Alpha.pdf'); await flush();
        fetch.mockResolvedValue(reply({status:'confirmed', export_eligible:true, token, chosen:approved.chosen}));
        const root = document.querySelector('.pending-review');
        // Skip stays one-click (navigation), matching sibling decks.
        root.dispatchEvent(new KeyboardEvent('keydown', {key:'s', bubbles:true}));
        await flush();
        expect(fetch.mock.calls.some(([url]) => String(url).includes('decision'))).toBe(true);
        // Filter focus must not steal letters into a review verb.
        const filter = document.querySelector('.pending-filter');
        filter.focus();
        const before = fetch.mock.calls.length;
        filter.dispatchEvent(new KeyboardEvent('keydown', {key:'c', bubbles:true}));
        expect(fetch.mock.calls.length).toBe(before);
    });

    it('confirm / reject / undo require confirmInline before posting a decision', async () => {
        fetch.mockResolvedValue(reply({...approved, sha256:'abc', pages:2, candidate_ids:['cand'], proposal:{candidates:[approved.chosen]}, token}));
        cleanups.push(mountPendingReview(document.querySelector('#host'), data()));
        click('Alpha.pdf'); await flush();
        const before = fetch.mock.calls.length;
        click('[confirm & next]');
        expect(document.querySelector('.inline-confirm-strip')).toBeTruthy();
        expect(fetch.mock.calls.length).toBe(before);
        fetch.mockResolvedValue(reply({status:'confirmed', export_eligible:true, token, chosen:approved.chosen}));
        click('Confirm');
        await flush();
        const decision = fetch.mock.calls.find(([url]) => String(url).includes('/decision'));
        expect(decision).toBeTruthy();
        expect(JSON.parse(decision[1].body).action).toBe('confirm');
    });

    it('queue headers carry scope=col and auto-hide matches the 900px CSS breakpoint', async () => {
        const matchMedia = vi.fn((q) => ({ matches: q.includes('900px'), media: q, addEventListener() {}, removeEventListener() {} }));
        vi.stubGlobal('matchMedia', matchMedia);
        fetch
            .mockResolvedValueOnce(reply({...approved, sha256:'abc', pages:2, candidate_ids:['cand'], proposal:{candidates:[approved.chosen]}}))
            .mockResolvedValue({ok:true, blob:async()=>new Blob(['png'])});
        cleanups.push(mountPendingReview(document.querySelector('#host'), data()));
        for (const th of document.querySelectorAll('.pending-queue th')) {
            expect(th.getAttribute('scope')).toBe('col');
        }
        click('Alpha.pdf'); await flush();
        expect(matchMedia).toHaveBeenCalledWith('(max-width: 900px)');
        expect(document.querySelector('.pending-review').classList.contains('pending-queue-hidden')).toBe(true);
        const panelStatus = document.querySelector('.pending-status');
        expect(panelStatus.getAttribute('role')).toBe('status');
        expect(panelStatus.getAttribute('aria-live')).toBe('polite');
    });

    it('open loading uses the panel status line, not a minted reader live region', () => {
        const src = readFileSync(resolve(root, 'static/js/pendingReview.js'), 'utf8');
        expect(src).toContain("say('Loading review…')");
        expect(src).not.toMatch(/reader\.innerHTML = '<p role="status">Loading review/);
        expect(src).toContain("QUEUE_AUTO_HIDE_MQ = '(max-width: 900px)'");
    });
});
