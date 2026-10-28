import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {beforeEach, afterEach, expect, it, vi} from 'vitest';

const shell = readFileSync(resolve(__dirname, '../../templates/index.html'), 'utf8');
const sse = frames => new Response(frames.map(frame => `data: ${JSON.stringify(frame)}\n\n`).join('') + 'data: [DONE]\n\n');

beforeEach(() => {
    vi.resetModules();
    document.body.innerHTML = new DOMParser().parseFromString(shell, 'text/html').body.innerHTML;
    vi.stubGlobal('fetch', vi.fn());
});
afterEach(() => vi.unstubAllGlobals());

for (const [module, resultKey, compile] of [
    ['deck', 'deck', 'runCompileFix'], ['docgen', 'doc', 'runDocCompileFix'],
]) {
    for (const [result, expected] of [
        [{tex_path:'/fixture/output.tex'}, 'Completed'],
        [{scaffold_error:'Destination refused'}, 'Partly complete'],
        [null, 'Status unknown'],
    ]) it(`${module} generation reports ${expected} from its result`, async () => {
        const mod = await import(`../../static/js/${module}.js`);
        document.getElementById(`${module}-topic`).value = 'Fixture topic';
        document.getElementById(`${module}-template-editor`).value = 'Fixture template';
        fetch.mockResolvedValue(sse(result ? [{[resultKey]:result}] : []));
        await mod.generate();
        expect(document.querySelector('#activity-tasks .operation-state').textContent).toBe(expected);
        expect(document.getElementById('activity-label-text').textContent.includes('completed this session')).toBe(expected === 'Completed');
    });

    for (const [frames, expected] of [
        [[{compile:{success:true,iterations:1}}], 'Completed'],
        [[{compile:{success:false,iterations:1}}], 'Needs attention'],
        [[{error:'Provider refused'}], 'Needs attention'],
        [[], 'Status unknown'],
    ]) it(`${module} compile reports ${expected} without inferring stream success`, async () => {
        const mod = await import(`../../static/js/${module}.js`);
        fetch.mockResolvedValue(sse(frames));
        await mod[compile]('/fixture/output.tex', {sha:'fixture'}, document.createElement('button'));
        expect(document.querySelector('#activity-tasks .operation-state').textContent).toBe(expected);
    });
}

for (const [result, expected] of [
    [{rel:'writing/report.md'}, 'Completed'],
    [{rel:'writing/report.md',publication_uncertain:true}, 'Status unknown'],
    [null, 'Status unknown'],
]) it(`Markdown writing reports ${expected} from publication evidence`, async () => {
    const mod = await import('../../static/js/docgen.js');
    document.getElementById('docgen-topic').value = 'Fixture topic';
    fetch.mockResolvedValue(sse(result ? [{doc_md:result}] : []));
    mod.generateMarkdown();
    document.querySelector('.inline-confirm-strip button:last-child').click();
    await vi.waitFor(() => expect(document.querySelector('#activity-tasks .operation-state')?.textContent).toBe(expected));
    expect(document.getElementById('activity-label-text').textContent.includes('completed this session')).toBe(expected === 'Completed');
});

for (const module of ['deck','docgen']) {
    for (const [proposal, expected] of [
        [{changed:false}, 'Completed'],
        [{changed:false,rejected_reason:'Unsafe change refused'}, 'Needs attention'],
        [null, 'Status unknown'],
    ]) it(`${module} augmentation reports ${expected} from its screened proposal`, async () => {
        const mod = await import(`../../static/js/${module}.js`);
        document.getElementById(`${module}-aug-path`).value = '/fixture/output.tex';
        document.getElementById(`${module}-aug-instruction`).value = 'Add limitations';
        if (module === 'deck') {
            fetch.mockResolvedValueOnce(new Response(JSON.stringify({deck_path:'/fixture/output.tex',deck_sha256:'fixture',sections:[]})));
            await mod.loadDeckSections();
        }
        fetch.mockResolvedValue(sse(proposal ? [{augment:proposal}] : []));
        await mod.augmentPreview();
        expect(document.querySelector('#activity-tasks .operation-state').textContent).toBe(expected);
    });
}

for (const [failure, reply, expected, retry] of [
    ['lost response', () => Promise.reject(new TypeError('Connection closed after request')), 'Status unknown', false],
    ['malformed success', () => Promise.resolve(new Response('not JSON')), 'Status unknown', false],
    ['missing confirmation', () => Promise.resolve(new Response(JSON.stringify({path:'report.md'}))), 'Status unknown', false],
    ['mismatched confirmation', () => Promise.resolve(new Response(JSON.stringify({ok:true,path:'other.md'}))), 'Status unknown', false],
    ['uncertain server result', () => Promise.resolve(new Response(JSON.stringify({error:'Inspect recovery',publication_outcome:'unknown'}), {status:500})), 'Status unknown', false],
    ['replayed attempt', () => Promise.resolve(new Response(JSON.stringify({error:'Inspect recovery',publication_outcome:'unknown'}), {status:409})), 'Status unknown', false],
    ['legacy rejection', () => Promise.resolve(new Response(JSON.stringify({error:'Apply failed'}), {status:409})), 'Status unknown', false],
    ['snapshot rejection', () => Promise.resolve(new Response(JSON.stringify({error:'snapshot write failed (OSError)',publication_outcome:'not_written',retry_allowed:false}), {status:409})), 'Needs attention', false],
    ['stale preview', () => Promise.resolve(new Response(JSON.stringify({error:'Document changed',publication_outcome:'not_written',retry_allowed:false}), {status:409})), 'Needs attention', false],
    ['legacy definite rejection', () => Promise.resolve(new Response(JSON.stringify({error:'Document changed',publication_outcome:'not_written'}), {status:409})), 'Needs attention', false],
    ['non-boolean retry flag', () => Promise.resolve(new Response(JSON.stringify({error:'Rejected',publication_outcome:'not_written',retry_allowed:'true'}), {status:409})), 'Needs attention', false],
    ['uncertain retry flag', () => Promise.resolve(new Response(JSON.stringify({error:'Inspect recovery',publication_outcome:'unknown',retry_allowed:true}), {status:409})), 'Status unknown', false],
    ['busy rejection', () => Promise.resolve(new Response(JSON.stringify({error:'Vault busy',publication_outcome:'not_written',retry_allowed:true}), {status:409})), 'Needs attention', true],
    ['confirmed success', () => Promise.resolve(new Response(JSON.stringify({ok:true,path:'report.md'}))), 'Completed', false],
]) it(`Markdown Apply handles ${failure} without guessing publication`, async () => {
    const {initStudio} = await import('../../static/js/studio.js');
    initStudio();
    document.getElementById('studio-md-path').value = 'report.md';
    document.getElementById('studio-md-instruction').value = 'Add limitations';
    fetch.mockResolvedValueOnce(sse([{markdown_preview:{token:'a'.repeat(32),path:'report.md',addition:'## New section'}}]));
    document.getElementById('studio-md-preview').click();
    await vi.waitFor(() => expect(document.querySelector('#studio-md-result button')).not.toBeNull());
    fetch.mockImplementationOnce(reply);
    const apply = document.querySelector('#studio-md-result button');
    apply.click();
    document.querySelector('.inline-confirm-strip button:last-child').click();
    await vi.waitFor(() => expect(document.querySelector('#activity-tasks .operation-state').textContent).toBe(expected));
    expect(apply.disabled).toBe(!retry);
    expect(fetch).toHaveBeenCalledTimes(2);
    const status = document.getElementById('studio-md-status').textContent;
    if (expected === 'Status unknown') {
        expect(status).toContain('History and Restore before trying again');
        expect(document.getElementById('activity-label-text').textContent).not.toContain('completed this session');
    }
    if (retry) expect(status).toBe('Vault busy');
    if (expected === 'Needs attention' && !retry) expect(status).toContain('Generate a fresh preview');
});

for (const module of ['deck','docgen']) it(`${module} cancellation asks for output inspection until publication is known`, async () => {
    const mod = await import(`../../static/js/${module}.js`);
    document.getElementById(`${module}-topic`).value = 'Fixture';
    document.getElementById(`${module}-template-editor`).value = 'Fixture template';
    fetch.mockImplementationOnce((_url, options) => new Promise((_resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(new DOMException('Cancelled', 'AbortError')));
    }));
    const operation = mod.generate();
    document.querySelector(`#${module}-activity`).previousElementSibling.click();
    await operation;
    expect(document.querySelector('#activity-tasks .operation-state').textContent).toBe('Status unknown');
    expect(document.getElementById(`${module}-status`).textContent).toContain('Inspect the output folder');
    expect(document.getElementById('activity-label-text').textContent).not.toContain('completed this session');
});

it('Markdown Apply retries a busy unattempted token only after another confirmation', async () => {
    const {initStudio} = await import('../../static/js/studio.js');
    initStudio();
    document.getElementById('studio-md-path').value = 'report.md';
    document.getElementById('studio-md-instruction').value = 'Add limitations';
    fetch.mockResolvedValueOnce(sse([{markdown_preview:{token:'a'.repeat(32),path:'report.md',addition:'## New section'}}]));
    document.getElementById('studio-md-preview').click();
    await vi.waitFor(() => expect(document.querySelector('#studio-md-result button')).not.toBeNull());
    const apply = document.querySelector('#studio-md-result button');
    fetch.mockResolvedValueOnce(new Response(JSON.stringify({error:'Vault busy',publication_outcome:'not_written',retry_allowed:true}), {status:409}));
    apply.click();
    document.querySelector('.inline-confirm-strip button:last-child').click();
    await vi.waitFor(() => expect(document.getElementById('studio-md-status').textContent).toBe('Vault busy'));
    expect(apply.disabled).toBe(false);
    expect(fetch).toHaveBeenCalledTimes(2);
    fetch.mockResolvedValueOnce(new Response(JSON.stringify({ok:true,path:'report.md'})));
    apply.click();
    expect(fetch).toHaveBeenCalledTimes(2); // No automatic retry or confirmation bypass.
    document.querySelector('.inline-confirm-strip button:last-child').click();
    await vi.waitFor(() => expect(document.getElementById('studio-md-status').textContent).toContain('Saved report.md'));
    expect(JSON.parse(fetch.mock.calls[2][1].body)).toEqual({token:'a'.repeat(32),confirm:true});
    expect(document.querySelector('#activity-tasks .operation-state').textContent).toBe('Completed');
});
