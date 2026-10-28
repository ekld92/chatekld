import {beforeEach, afterEach, expect, it, vi} from 'vitest';
const {fetch, confirms} = vi.hoisted(() => ({fetch:vi.fn(), confirms:[]}));
vi.mock('../../static/js/api.js',()=>({secureFetch:fetch}));
vi.mock('../../static/js/ui.js',()=>({confirmInline:(button, options)=>confirms.push(options)}));
import {mountPdfManagement} from '../../static/js/pdfManagement.js';
const data = {attachments:[{key:'KEEP',rel:'keep.pdf',child_counts:{annotations:2}},{key:'COPY',rel:'keep.pdf',child_counts:{annotations:1}}],parent:{key:'PARENT01',fields:{title:'An entry'}},operations:[],rows:[
    {rel:'keep.pdf',physical:{size:10,sha256:'same'},attachments:[{key:'KEEP',mode:'linked_file'}]},
    {rel:'remove.pdf',physical:{size:10,sha256:'same'},attachments:[]},
]};
let dispose;
const tick = async()=>{ for(let i=0;i<10;i++) await Promise.resolve(); };
const ok = result => ({ok:true,json:async()=>result});
beforeEach(()=>{fetch.mockReset(); confirms.length=0; document.body.innerHTML='<div id="host"></div>'; fetch.mockResolvedValue(ok(data));});
afterEach(()=>{dispose?.();});
it('loading and selecting a survivor never writes',async()=>{
    dispose=mountPdfManagement(document.getElementById('host'),'Key'); await tick();
    document.querySelector('input[value="keep.pdf"]').click();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(document.body.textContent).toContain('Identical bytes');
    expect(confirms).toHaveLength(0);
});
it('requires a preview and explicit confirmation before cleanup',async()=>{
    dispose=mountPdfManagement(document.getElementById('host'),'Key'); await tick();
    document.querySelector('input[value="keep.pdf"]').click();
    document.querySelector('[data-pm-rel="remove.pdf"] select').value='delete';
    fetch.mockResolvedValueOnce(ok({plan:{parent:data.parent,survivor:{rel:'keep.pdf'},steps:[{rel:'remove.pdf',action:'delete',notes:[]}]},token:'reviewed',message:'Recoverable deletion',executable:true}));
    document.querySelector('[data-pm-cleanup]').click(); await tick();
    expect(fetch.mock.calls[1][0]).toContain('/preview');
    expect(JSON.parse(fetch.mock.calls[1][1].body).actions).toEqual({'remove.pdf':'delete'});
    document.querySelector('[data-pm-confirm]').click();
    expect(fetch).toHaveBeenCalledTimes(2);
    fetch.mockResolvedValueOnce(ok({operation:{status:'complete'}})).mockResolvedValueOnce(ok(data));
    await confirms[0].onConfirm();
    expect(fetch.mock.calls[2][0]).toContain('/apply');
    expect(JSON.parse(fetch.mock.calls[2][1].body)).toMatchObject({token:'reviewed',confirm:true,survivor:'keep.pdf'});
});
it('changed selection invalidates a previously confirmed preview',async()=>{
    dispose=mountPdfManagement(document.getElementById('host'),'Key'); await tick();
    fetch.mockResolvedValueOnce(ok({plan:{parent:data.parent,steps:[]},token:'old',message:'Unmap',executable:true}));
    document.querySelector('[data-pm-cleanup]').click(); await tick();
    document.querySelector('[data-pm-confirm]').click();
    document.querySelector('input[value="keep.pdf"]').click();
    await confirms[0].onConfirm();
    expect(fetch).toHaveBeenCalledTimes(2);
});
it('does not expose apply when deletion has shared-reference blockers',async()=>{
    dispose=mountPdfManagement(document.getElementById('host'),'Key'); await tick();
    fetch.mockResolvedValueOnce(ok({plan:{parent:data.parent,steps:[]},token:'x',message:'Delete',blockers:['Still linked in Zotero'],executable:false}));
    document.querySelector('[data-pm-cleanup]').click(); await tick();
    expect(document.querySelector('[data-pm-confirm]')).toBeNull();
    expect(document.body.textContent).toContain('Still linked in Zotero');
});
it('late responses cannot reopen a closed panel',async()=>{
    let resolve; fetch.mockReturnValue(new Promise(r=>{resolve=r;}));
    dispose=mountPdfManagement(document.getElementById('host'),'Key'); dispose();
    resolve(ok(data)); await tick();
    expect(document.querySelector('.pdf-management')).toBeNull();
});
it('shows comparison facts and sends the annotation acknowledgement only when ticked',async()=>{
    const annotated={...data, removal_rule:'Annotated copies are kept unless acknowledged.', rows:[
        {rel:'keep.pdf',physical:{size:10,sha256:'same'},attachments:[{key:'KEEP',mode:'linked_file'}],native_annotations:0,embedded_annotations:0,note_dependencies:[],other_keys:[],comparison:{identical_to:['remove_1.pdf'],different_from:[],variant:null}},
        {rel:'remove_1.pdf',physical:{size:10,sha256:'same'},attachments:[],native_annotations:0,embedded_annotations:3,note_dependencies:['notes/a.md'],other_keys:[],comparison:{identical_to:['keep.pdf'],different_from:[],variant:'underscore',counter:1,base_stem:'remove'}},
    ]};
    fetch.mockResolvedValue(ok(annotated));
    dispose=mountPdfManagement(document.getElementById('host'),'Key'); await tick();
    const text=document.body.textContent;
    expect(text).toContain('3 annotation(s) embedded in the PDF');
    expect(text).toContain('referenced by 1 note(s): notes/a.md');
    expect(text).toContain('Collision clue: underscore suffix 1 of remove');
    expect(text).toContain('Blocks removal until acknowledged.');
    document.querySelector('input[value="keep.pdf"]').click();
    document.querySelector('[data-pm-rel="remove_1.pdf"] select').value='delete';
    fetch.mockResolvedValueOnce(ok({plan:{parent:data.parent,survivor:{rel:'keep.pdf'},steps:[]},token:'t',message:'Blocked',blockers:['remove_1.pdf: carries 3 annotation(s)'],executable:false}));
    document.querySelector('[data-pm-cleanup]').click(); await tick();
    expect(JSON.parse(fetch.mock.calls[1][1].body).acknowledge_annotations).toBe(false);
    expect(document.querySelector('[data-pm-confirm]')).toBeNull();
    document.querySelector('[data-pm-ack-annotations]').checked=true;
    fetch.mockResolvedValueOnce(ok({plan:{parent:data.parent,survivor:{rel:'keep.pdf'},steps:[{rel:'remove_1.pdf',action:'delete',notes:[]}]},token:'ack',message:'Recoverable deletion',executable:true}));
    document.querySelector('[data-pm-cleanup]').click(); await tick();
    expect(JSON.parse(fetch.mock.calls[2][1].body).acknowledge_annotations).toBe(true);
    document.querySelector('[data-pm-confirm]').click();
    fetch.mockResolvedValueOnce(ok({operation:{status:'complete'}})).mockResolvedValueOnce(ok(annotated));
    await confirms[0].onConfirm();
    expect(JSON.parse(fetch.mock.calls[3][1].body)).toMatchObject({token:'ack',acknowledge_annotations:true,confirm:true});
});

// --- 2026-09-17 review, E2: the Zotero-side flows ---------------------------
it('removing an attachment requires the annotation-scope tick and sends it with the apply',async()=>{
    dispose=mountPdfManagement(document.getElementById('host'),'Key'); await tick();
    document.querySelector('input[value="keep.pdf"]').click();
    fetch.mockResolvedValueOnce(ok({plan:{parent:data.parent,attachment:'KEEP',before:{title:'Keep',path:'keep.pdf'}},token:'trash-t',message:'Move to Zotero trash',executable:true}));
    document.querySelector('[data-pm-trash="KEEP"]').click(); await tick();
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toMatchObject({action:'trash',attachment:'KEEP',survivor:'keep.pdf'});
    document.querySelector('[data-pm-confirm]').click();
    expect(confirms).toHaveLength(0);
    expect(document.body.textContent).toContain('Confirm the annotation and child-note scope first');
    document.querySelector('[data-pm-loss]').checked=true;
    document.querySelector('[data-pm-confirm]').click();
    fetch.mockResolvedValueOnce(ok({operation:{status:'complete'}})).mockResolvedValueOnce(ok(data));
    await confirms[0].onConfirm();
    expect(JSON.parse(fetch.mock.calls[2][1].body)).toMatchObject({action:'trash',token:'trash-t',confirm:true,accept_annotation_loss:true});
});
it('linking searches candidates, previews, and carries the plugin acknowledgement',async()=>{
    dispose=mountPdfManagement(document.getElementById('host'),'Key'); await tick();
    document.querySelector('[data-pm-query]').value='new';
    fetch.mockResolvedValueOnce(ok({paths:['biblio/new.pdf'],total:1}));
    document.querySelector('[data-pm-search]').click(); await tick();
    expect(fetch.mock.calls[1][0]).toContain('/candidates?q=new');
    fetch.mockResolvedValueOnce(ok({plan:{parent:data.parent,rel:'biblio/new.pdf'},token:'link-t',message:'Link',executable:true}));
    document.querySelector('[data-pm-link]').click(); await tick();
    expect(JSON.parse(fetch.mock.calls[2][1].body)).toMatchObject({action:'link',pdf:'biblio/new.pdf'});
    document.querySelector('[data-pm-plugins]').checked=true;
    document.querySelector('[data-pm-confirm]').click();
    fetch.mockResolvedValueOnce(ok({operation:{status:'failed'}})).mockResolvedValueOnce(ok(data));
    await confirms[0].onConfirm();
    expect(JSON.parse(fetch.mock.calls[3][1].body)).toMatchObject({action:'link',token:'link-t',confirm:true,acknowledge_plugins:true});
    expect(document.body.textContent).toContain('Operation failed.');
});
it('restores a cleanup and reverts a failed link, and inspects only uncertain Zotero operations',async()=>{
    const ops={...data,operations:[
        {id:'cleanup1',status:'complete',steps:[{action:'delete',rel:'remove.pdf',status:'complete'}]},
        {id:'link1',kind:'link',status:'failed',plan:{rel:'x.pdf'}},
        {id:'trash1',kind:'trash',status:'needs_inspection',plan:{before:{title:'Copy'},attachment:'COPY'}},
    ]};
    fetch.mockResolvedValue(ok(ops));
    dispose=mountPdfManagement(document.getElementById('host'),'Key'); await tick();
    expect(document.querySelector('[data-pm-restore="link1"]')).toBeTruthy();
    expect(document.querySelector('[data-pm-inspect="link1"]')).toBeNull();
    expect(document.querySelector('[data-pm-inspect="trash1"]')).toBeTruthy();
    expect(document.querySelector('[data-pm-inspect="cleanup1"]')).toBeNull();
    document.querySelector('[data-pm-restore="cleanup1"]').click();
    fetch.mockResolvedValueOnce(ok({operation:{status:'restored'}})).mockResolvedValueOnce(ok(ops));
    await confirms[0].onConfirm();
    expect(fetch.mock.calls[1][0]).toContain('/restore');
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({operation:'cleanup1',confirm:true});
    fetch.mockResolvedValueOnce(ok({operation:'trash1',completable:true,token:'seen',reason:'The intended outcome is currently visible.'}));
    document.querySelector('[data-pm-inspect="trash1"]').click(); await tick();
    expect(document.body.textContent).toContain('The intended outcome is currently visible.');
    document.querySelector('[data-pm-complete]').click();
    fetch.mockResolvedValueOnce(ok({operation:{status:'complete'}})).mockResolvedValueOnce(ok(ops));
    await confirms[1].onConfirm();
    const complete = fetch.mock.calls.find(([url])=>String(url).includes('/complete'));
    expect(JSON.parse(complete[1].body)).toEqual({operation:'trash1',token:'seen',confirm:true});
});
