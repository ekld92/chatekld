import {beforeEach, afterEach, it, expect, vi} from 'vitest';

beforeEach(() => { vi.resetModules(); document.body.innerHTML = `
    <button id="vault-index-btn"></button><div id="online-ocr-status"></div>
    <div id="online-run-history"></div><div id="online-source-picker"></div>
    <input id="online-source-search"><div id="vault-error-boundary"></div>`; });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });
function mock(handler) {
    const calls = [];
    vi.stubGlobal('fetch', vi.fn(async (url, opts = {}) => {
        calls.push([String(url), opts]);
        const body = handler(String(url), opts);
        return {ok: true, status: 200, json: async () => body};
    }));
    return calls;
}
it('reload inspects durable history without posting', async () => {
    const calls = mock(url => url.includes('/runs') ? {runs: [{run_id: 'a'.repeat(32), state: 'interrupted',
        source_count: 2, retry_allowed: true, routine: true}]} : {state: 'idle'});
    const vault = await import('../../static/js/vault.js');
    vault.applyOnlineVisualSettings({});
    await vi.waitFor(() => expect(document.getElementById('online-run-history').textContent).toContain('Retry unfinished'));
    expect(calls.every(([, opts]) => !opts.method || opts.method === 'GET')).toBe(true);
});
it('source picker bounds visible rows and preserves text as text', async () => {
    mock(() => ({sources: ['<img src=x onerror=alert(1)>.pdf', ...Array.from({length: 49}, (_, i) => `file${i}.pdf`)], next_offset: 50}));
    const vault = await import('../../static/js/vault.js');
    await vault.chooseOnlineSources();
    expect(document.querySelectorAll('#online-source-picker input')).toHaveLength(50);
    expect(document.querySelector('#online-source-picker img')).toBeNull();
    expect(document.getElementById('online-source-picker').textContent).toContain('Next 50');
});
it('local refusal offers a deliberate rebuild without starting indexing', async () => {
    const calls = mock(url => url.includes('index-preview') ? {ready: false, reason: 'interrupted_store'} : {});
    const vault = await import('../../static/js/vault.js');
    await vault.advancedLocalIndex();
    expect(document.getElementById('vault-error-boundary').textContent).toContain('Rebuild from scratch');
    expect(calls.some(([url]) => url.endsWith('/api/obsidian/index'))).toBe(false);
});
it('terminal status slows polling after reattachment', async () => {
    vi.useFakeTimers();
    let state = 'indexing';
    const calls = mock(url => url.endsWith('/status') ? {state, run_index: true} : {runs: []});
    const vault = await import('../../static/js/vault.js');
    await vault.pollOnlineOcr();
    state = 'indexed';
    await vi.advanceTimersByTimeAsync(1500);
    const count = calls.length;
    await vi.advanceTimersByTimeAsync(4900);
    expect(calls).toHaveLength(count);
    await vi.advanceTimersByTimeAsync(200);
    expect(calls).toHaveLength(count + 1);
});

it.each(['next_offset', 'promotion_next_offset'])('paginates old runs and %s without accumulating duplicate detail', async (nextField) => {
    const calls = mock(url => {
        if (url.endsWith('/runs')) return {runs: [{run_id: 'new', state: 'indexed'}], next_offset: 20};
        if (url.endsWith('/runs?offset=20')) return {runs: [{run_id: 'old', state: 'indexed'}]};
        if (url.includes('/runs/old?offset=50')) return {sources: [{source: 'later.pdf', export_url: '/export/later'}], promotion_evidence: [{source: 'later.pdf'}]};
        return {sources: [{source: 'first.pdf', export_url: '/export/first'}], [nextField]: 50};
    });
    const vault = await import('../../static/js/vault.js');
    const button = name => [...document.querySelectorAll('button')].find(b => b.textContent === name);
    await vault.refreshOnlineHistory();
    button('Older runs').click();
    await vi.waitFor(() => expect(button('Newer runs')).toBeTruthy());
    button('Review documents').click();
    await vi.waitFor(() => expect(button('Next evidence page')).toBeTruthy());
    button('Next evidence page').click();
    await vi.waitFor(() => expect(button('Review later.pdf')).toBeTruthy());
    expect(button('Review first.pdf')).toBeUndefined();
    expect(document.querySelectorAll('#online-run-history pre')).toHaveLength(1);
    button('Previous evidence page').click();
    await vi.waitFor(() => expect(button('Review first.pdf')).toBeTruthy());
    expect(calls.every(([, options]) => !options.method || options.method === 'GET')).toBe(true);
});

it('offers session promotion from bounded completion counts', async () => {
    document.body.insertAdjacentHTML('beforeend', '<button id="online-ocr-promote" hidden></button>');
    mock(() => ({state: 'done', scope: 'vault_session', completed_count: 5, result_count: 5, source_count: 5, promoted_count: 2}));
    const vault = await import('../../static/js/vault.js');
    await vault.pollOnlineOcr();
    expect(document.getElementById('online-ocr-promote').hidden).toBe(false);
    expect(document.getElementById('online-ocr-status').textContent).toContain('2 cache(s) replaced');
});


it.each(['network', 'http', 'json'])('keeps the newer history page when an older %s failure arrives last', async (failure) => {
    let resolveOld, rejectOld;
    const oldResponse = new Promise((resolve, reject) => { resolveOld = resolve; rejectOld = reject; });
    const fetch = vi.fn()
        .mockReturnValueOnce(oldResponse)
        .mockResolvedValueOnce({ok: true, status: 200, json: async () => ({
            runs: [{run_id: 'newer-page', state: 'indexed', source_count: 1}]
        })});
    vi.stubGlobal('fetch', fetch);
    const {refreshOnlineHistory} = await import('../../static/js/vault.js');
    const older = refreshOnlineHistory();
    await refreshOnlineHistory(20, true);
    const host = document.getElementById('online-run-history');
    const newerRow = host.firstElementChild;
    expect(host.textContent).toContain('newer-page');
    expect(document.activeElement).toBe(host);
    if (failure === 'network') rejectOld(new Error('stale network failure'));
    else resolveOld({ok: failure !== 'http', status: failure === 'http' ? 500 : 200,
        json: async () => {
            if (failure === 'json') throw new Error('stale JSON failure');
            return {error: 'stale server failure'};
        }});
    await older;
    expect(host.firstElementChild).toBe(newerRow);
    expect(host.textContent).toContain('newer-page');
    expect(host.textContent).not.toContain('stale');
    expect(document.activeElement).toBe(host);
    expect(fetch).toHaveBeenCalledTimes(2);
});

it('still displays a failure from the current history request', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('current history failure')));
    const {refreshOnlineHistory} = await import('../../static/js/vault.js');
    await refreshOnlineHistory();
    expect(document.getElementById('online-run-history').textContent).toBe('current history failure');
});

it('shows missing legacy final status without claiming interruption', async () => {
    mock(() => ({runs: [{run_id: 'old-run', state: 'status_unrecorded', source_count: 10,
        status_note: 'The current search index has a separate status.', promotion_barrier: true}]}));
    const vault = await import('../../static/js/vault.js');
    await vault.refreshOnlineHistory();
    const text = document.getElementById('online-run-history').textContent;
    expect(text).toContain('Final outcome unknown');
    expect(text).toContain('separate status');
    expect(text).not.toContain('interrupted');
});

it('history shows the saved provider error and recovery instructions as text', async () => {
    mock(url => url.endsWith('/runs') ? {runs:[{run_id:'bad',state:'extraction_incomplete'}]} : {
        sources:[{source:'figure.png',status:'image_rejected',provider:'deepseek',model:'deepseek-flash',
            http_status:400,provider_code:'invalid_image',error_detail:'Invalid image <img src=x>',next_step:'Replace this image, then explicitly retry.'}]
    });
    const vault = await import('../../static/js/vault.js');
    await vault.refreshOnlineHistory();
    [...document.querySelectorAll('button')].find(b => b.textContent === 'Review documents').click();
    await vi.waitFor(() => expect(document.querySelector('.source-result')?.textContent).toContain('HTTP 400'));
    expect(document.querySelector('.source-result').textContent).toContain('Replace this image');
    expect(document.querySelector('.source-result img')).toBeNull();
});

it('reports interrupted promotion as partial and slows polling', async () => {
    vi.useFakeTimers();
    document.body.insertAdjacentHTML('beforeend', '<aside id="activity-bar"><span id="activity-label-text"></span><div id="activity-tasks"></div><div id="activity-events"></div></aside>');
    let state = 'promoting';
    const calls = mock(url => url.endsWith('/status') ? {state, run_id: 'audit', run_index: true} : {runs: []});
    const vault = await import('../../static/js/vault.js');
    await vault.pollOnlineOcr();
    state = 'promotion_or_index_incomplete';
    await vi.advanceTimersByTimeAsync(1500);
    expect(document.getElementById('online-ocr-status').textContent).toContain('inspect recovery evidence');
    expect(document.getElementById('activity-tasks').textContent).toContain('Partly complete');
    const count = calls.length;
    await vi.advanceTimersByTimeAsync(4900);
    expect(calls).toHaveLength(count);
    await vi.advanceTimersByTimeAsync(200);
    expect(calls).toHaveLength(count + 1);
});

it('explains that cancellation of promotion does not undo cache replacements', async () => {
    mock(() => ({ok: true, phase: 'promotion'}));
    const vault = await import('../../static/js/vault.js');
    await vault.cancelOnlineOcr();
    expect(document.getElementById('online-ocr-status').textContent).toContain('Completed replacements remain recorded');
});
