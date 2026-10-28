import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const json = data => ({ok: true, status: 200, json: async () => data});
let fetchMock, body, meta, granted;
beforeEach(() => {
    vi.resetModules();
    granted = false;
    document.body.innerHTML = '<div id="meta"></div><div id="body"></div>';
    body = document.getElementById('body'); meta = document.getElementById('meta');
    vi.stubGlobal('localStorage', {getItem: k => k === 'tagging_view' ? 'table' : null, setItem: vi.fn()});
    fetchMock = vi.fn(async url => {
        if (String(url).includes('/inventory')) return json({rows: [{citation_key: 'key', title: 'Paper', note: {present: true, tags: []}, finder_tags: [], zotero: {present: true, parent_tags: ['old']}}]});
        if (String(url).endsWith('/zotero-available')) return json({available: true, writable: granted, mode: 'auto', note: 'Authorize Desktop'});
        if (String(url).endsWith('/local-status')) return json({supports_writes: true, server_id: 'fixture'});
        if (String(url).endsWith('/authorize-local')) { granted = true; return json({authorized: true}); }
        return json({});
    });
    vi.stubGlobal('fetch', fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

it('does not confuse reachable with writable and authorizes without replaying tags', async () => {
    const {taggingRenderInto} = await import('../../static/js/tagging.js');
    await taggingRenderInto(body, meta, () => true);
    expect(body.querySelector('[data-action="apply-zotero"]').disabled).toBe(true);
    expect(body.querySelector('.tagging-zotero-cell .tagging-cur-x')).toBeNull();
    body.querySelector('[data-action="authorize-zotero"]').click();
    await vi.waitFor(() => expect(body.querySelector('.inline-confirm-strip')).not.toBeNull());
    expect(granted).toBe(false);
    body.querySelector('.inline-confirm-strip').querySelectorAll('button')[1].click();
    await vi.waitFor(() => expect(body.querySelector('[data-action="apply-zotero"]').disabled).toBe(false));
    const authorization = fetchMock.mock.calls.find(c => String(c[0]).endsWith('/authorize-local'));
    expect(JSON.parse(authorization[1].body)).toEqual({confirm: true, server_id: 'fixture'});
    expect(fetchMock.mock.calls.some(c => String(c[0]).includes('/apply-'))).toBe(false);
    expect(fetchMock.mock.calls.some(c => String(c[0]).endsWith('/api/config'))).toBe(false);
});

it('does not authorize after navigating away before status returns', async () => {
    let current = true;
    const {taggingRenderInto} = await import('../../static/js/tagging.js');
    await taggingRenderInto(body, meta, () => current);
    body.querySelector('[data-action="authorize-zotero"]').click(); current = false;
    await new Promise(r => setTimeout(r, 10));
    expect(body.querySelector('.inline-confirm-strip')).toBeNull();
    expect(granted).toBe(false);
});

it('switches explicit Web only after confirmation and asks for a new scan', async () => {
    const original = fetchMock.getMockImplementation();
    fetchMock.mockImplementation(async (url, options) => String(url).endsWith('/zotero-available')
        ? json({available: false, writable: false, mode: 'web', note: 'Desktop selection requires local'})
        : original(url, options));
    const {taggingRenderInto} = await import('../../static/js/tagging.js');
    await taggingRenderInto(body, meta, () => true);
    body.querySelector('[data-action="authorize-zotero"]').click();
    await vi.waitFor(() => expect(body.querySelector('.inline-confirm-strip')).not.toBeNull());
    expect(body.querySelector('.inline-confirm-strip').textContent).toContain('Switch Zotero transport from Web to local');
    expect(fetchMock.mock.calls.some(c => String(c[0]).endsWith('/api/config'))).toBe(false);
    body.querySelector('.inline-confirm-strip').querySelectorAll('button')[1].click();
    await vi.waitFor(() => expect(body.querySelector('.tagging-access-status').textContent).toContain('Run Library Audit again'));
    const changed = fetchMock.mock.calls.find(c => String(c[0]).endsWith('/api/config'));
    expect(JSON.parse(changed[1].body)).toEqual({tagging_zotero_transport: 'local'});
    expect(fetchMock.mock.calls.some(c => String(c[0]).includes('/apply-'))).toBe(false);
});

for (const view of ['table', 'deck']) {
    it(`preserves ${view} tag chips and unfinished input after authorization`, async () => {
        vi.stubGlobal('localStorage', {getItem: k => k === 'tagging_view' ? view : null, setItem: vi.fn()});
        const {taggingRenderInto} = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        const input = body.querySelector('.tagging-tag-input');
        input.value = 'dep';
        input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
        input.value = 'unfinished';
        const tags = [...input._tags];
        body.querySelector('[data-action="authorize-zotero"]').click();
        await vi.waitFor(() => expect(body.querySelector('.inline-confirm-strip')).not.toBeNull());
        body.querySelector('.inline-confirm-strip').querySelectorAll('button')[1].click();
        await vi.waitFor(() => expect(body.querySelector('.tagging-access-status').textContent).toContain('Desktop authorized'));
        expect(body.querySelector('.tagging-tag-input')).toBe(input);
        expect(input.value).toBe('unfinished');
        expect(input._tags).toEqual(tags);
        expect(fetchMock.mock.calls.filter(c => String(c[0]).includes('/inventory'))).toHaveLength(1);
        expect(fetchMock.mock.calls.some(c => String(c[0]).includes('/apply-'))).toBe(false);
    });
}

it('keeps drafts when authorization is refused', async () => {
    const original = fetchMock.getMockImplementation();
    fetchMock.mockImplementation((url, options) => String(url).endsWith('/authorize-local')
        ? {ok: false, json: async () => ({error: 'User refused'})} : original(url, options));
    const {taggingRenderInto} = await import('../../static/js/tagging.js');
    await taggingRenderInto(body, meta, () => true);
    const input = body.querySelector('.tagging-tag-input'); input.value = 'draft';
    body.querySelector('[data-action="authorize-zotero"]').click();
    await vi.waitFor(() => expect(body.querySelector('.inline-confirm-strip')).not.toBeNull());
    body.querySelector('.inline-confirm-strip').querySelectorAll('button')[1].click();
    await vi.waitFor(() => expect(body.querySelector('.tagging-access-status').textContent).toContain('User refused'));
    expect(body.querySelector('.tagging-tag-input')).toBe(input);
    expect(input.value).toBe('draft');
    expect(body.querySelector('[data-action="apply-zotero"]').disabled).toBe(true);
});
