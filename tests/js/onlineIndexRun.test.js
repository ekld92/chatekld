import { describe, it, expect, vi, afterEach } from 'vitest';

describe('online index run admission', () => {
    afterEach(() => vi.unstubAllGlobals());

    it.each([true, false])('names the confirmed operation while its start is pending (prepareOnly=%s)', async (prepareOnly) => {
        vi.resetModules(); vi.useFakeTimers();
        document.body.innerHTML = '<button id="vault-index-btn">Review</button><p id="library-preparation-status"></p>';
        let finishStart;
        const pendingStart = new Promise(resolve => { finishStart = resolve; });
        vi.stubGlobal('fetch', vi.fn(async (url) => {
            const path = String(url);
            if (path.endsWith('/session/start')) return pendingStart;
            return {ok: true, json: async () => path.endsWith('/session/preview')
                ? {eligible: 0, source_count: 0, session_token: 'intent-v1:fixture'}
                : path.endsWith('/status') ? {state: 'indexed', run_index: true, bm25_status: 'ready'} : {ok: true}};
        }));
        try {
            const vault = await import('../../static/js/vault.js');
            await vault.previewLocalIndex(document.getElementById('vault-index-btn'), prepareOnly, null, false);
            const label = prepareOnly ? 'Prepare available text' : 'Update using available text';
            [...document.querySelectorAll('button')].find(b => b.textContent === label).click();
            await vi.advanceTimersByTimeAsync(10);
            expect(document.getElementById('library-preparation-status').textContent).toBe(prepareOnly
                ? 'Starting the confirmed preparation…' : 'Starting the confirmed search update…');
        } finally {
            finishStart({ok: true, json: async () => ({ok: true})});
            await vi.advanceTimersByTimeAsync(10);
            vi.clearAllTimers(); vi.useRealTimers();
        }
    });

    it('previews and confirms before starting one run-wide online job', async () => {
        document.body.innerHTML = `
            <button id="advanced-index-btn">Advanced</button>
            <input id="vault-online-visual" type="checkbox" checked>
            <input id="vault-online-full-rebuild" type="checkbox" checked>
            <details id="online-ocr-panel"></details>
            <div id="online-ocr-status"></div>
            <select id="online-ocr-provider"><option value="deepseek">DeepSeek</option></select>
            <select id="online-ocr-model"><option value="deepseek-flash">deepseek-flash</option></select>
            <select id="online-vision-provider"><option value="deepseek">DeepSeek</option></select>
            <select id="online-vision-model"><option value="deepseek-flash">deepseek-flash</option></select>
        `;
        const calls = [];
        vi.stubGlobal('fetch', vi.fn((url, options = {}) => {
            const path = String(url);
            calls.push({ path, body: options.body ? JSON.parse(options.body) : null });
            const body = path.includes('/session/preview')
                ? { eligible: 4, source_count: 2, session_token: 'bound-inventory' }
                : path.includes('/online-ocr/status') ? { state: 'indexed', run_index: true }
                    : { ok: true };
            return Promise.resolve({ ok: true, status: 200, json: async () => body });
        }));
        const vault = await import('../../static/js/vault.js');
        await vault.advancedOnlineIndex();
        expect(calls.some(call => call.path.includes('/session/preview'))).toBe(true);
        expect(calls.some(call => call.path.includes('/session/start'))).toBe(false);
        const confirm = [...document.querySelectorAll('.inline-confirm-strip button')]
            .find(button => button.textContent === 'Start full online rebuild');
        expect(confirm).toBeTruthy();
        confirm.click();
        await vi.waitFor(() => expect(calls.some(call => call.path.includes('/session/start'))).toBe(true));
        const start = calls.find(call => call.path.includes('/session/start')).body;
        expect(start).toMatchObject({ run_index: true, reextract: false,
            confirm_external: true, confirm_index: true, retry_failed: true,
            full_rebuild: true, confirm_full_rebuild: true,
            session_token: 'bound-inventory', provider: 'deepseek',
            model: 'deepseek-flash', vision_provider: 'deepseek',
            vision_model: 'deepseek-flash' });
        expect(calls.some(call => call.path.includes('/api/obsidian/index'))).toBe(false);
    });
    it('daily update ignores advanced rebuild, re-extraction and selected sources', async () => {
        document.body.innerHTML = `
            <button id="vault-index-btn">Update Index</button>
            <input id="vault-online-visual" type="checkbox" checked>
            <input id="vault-online-full-rebuild" type="checkbox" checked>
            <input id="online-ocr-reextract" type="checkbox" checked>
            <div id="online-source-picker"></div>
            <details id="online-ocr-panel"></details>`;
        const calls = [];
        vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
            const path = String(url);
            calls.push({path, body: options.body ? JSON.parse(options.body) : null});
            return {ok: true, status: 200, json: async () => path.includes('/sources?')
                ? {sources: ['one.pdf']} : path.includes('/session/preview')
                    ? {eligible: 0, source_count: 2, session_token: 'daily'}
                    : path.includes('/online-ocr/status') ? {state: 'indexed', run_index: true} : {ok: true}};
        }));
        const vault = await import('../../static/js/vault.js');
        await vault.chooseOnlineSources();
        document.querySelector('#online-source-picker input').click();
        await vault.indexVault();
        expect(calls.find(c => c.path.includes('/session/preview')).body).toEqual({run_index: true, reextract: false, operation_mode: 'update_search'});
        expect(calls.some(c => c.path.includes('/session/start'))).toBe(false);
        expect(document.getElementById('online-ocr-panel').open).toBe(false);
        [...document.querySelectorAll('.inline-confirm-strip button')]
            .find(b => b.textContent === 'Update using available text').click();
        await vi.waitFor(() => expect(calls.some(c => c.path.includes('/session/start'))).toBe(true));
        expect(calls.find(c => c.path.includes('/session/start')).body).toMatchObject({
            reextract: false, full_rebuild: false, confirm_full_rebuild: false, retry_failed: false, no_external: true, confirm_external: false});
        expect(calls.find(c => c.path.includes('/session/start')).body).not.toHaveProperty('paths');
    });

});

describe('daily progress projection', () => {
    it('describes a resource stop without blaming documents or claiming chat readiness', async () => {
        document.body.innerHTML = '<p id="daily-index-status"></p>';
        vi.stubGlobal('fetch', vi.fn(async () => ({ok: true, json: async () => ({
            state: 'extraction_incomplete', global_failure: true, result_count: 1,
            source_count: 19, not_attempted_count: 18,
            failure: {stop_reason: 'initial_memory_pressure', failure_scope: 'global',
                error_detail: 'macOS reported memory pressure.',
                next_step: 'Review resource details before retrying. Unattempted documents are not defective.'}
        })})));
        try {
            const vault = await import('../../static/js/vault.js');
            await vault.pollOnlineOcr();
            const text = document.getElementById('daily-index-status').textContent;
            expect(text).toContain('resource limit');
            expect(text).toContain('18 not attempted');
            expect(text).toContain('not defective');
            expect(text).not.toContain('review failed documents');
            expect(text).not.toContain('library is ready');
        } finally { vi.unstubAllGlobals(); }
    });
    it('keeps polling while lexical search is preparing and reports readiness only after persistence', async () => {
        vi.resetModules(); vi.useFakeTimers();
        document.body.innerHTML = '<p id="daily-index-status"></p>';
        const status = {state: 'indexed', run_id: 'bm25', bm25_status: 'preparing'};
        vi.stubGlobal('fetch', vi.fn(async () => ({ok: true, json: async () => ({...status})})));
        try {
            const vault = await import('../../static/js/vault.js');
            await vault.pollOnlineOcr();
            expect(document.getElementById('daily-index-status').textContent).toContain('Preparing and saving lexical search');
            status.bm25_status = 'ready';
            await vault.pollOnlineOcr();
            expect(document.getElementById('daily-index-status').textContent).toContain('Lexical search ready and saved');
        } finally { vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals(); }
    });
    it('does not present extraction 0/0 as index progress or claim lexical readiness', async () => {
        document.body.innerHTML = '<p id="daily-index-status"></p>';
        vi.stubGlobal('fetch', vi.fn(async () => ({ok: true, json: async () => ({
            state: 'indexed', run_id: 'cached', result_count: 0, source_count: 0
        })})));
        const vault = await import('../../static/js/vault.js');
        await vault.pollOnlineOcr();
        expect(document.getElementById('daily-index-status').textContent).not.toContain('0/0');
        expect(document.getElementById('daily-index-status').textContent).toContain('Lexical search readiness unknown');
        vi.unstubAllGlobals();
    });
    it('keeps receipt paths out of the daily summary and distinguishes connection loss', async () => {
        document.body.innerHTML = '<p id="daily-index-status"></p><div id="activity-bar"><span id="activity-label-text"></span></div>';
        vi.stubGlobal('fetch', vi.fn(async () => ({ok: true, json: async () => ({
            state: 'running', run_id: 'projection', source_count: 3, result_count: 1,
            current_source: 'paper.pdf', promotion_receipt: '/private/receipt.json',
            started_at: Date.now() / 1000 - 2, last_activity_at: Date.now() / 1000
        })})));
        const vault = await import('../../static/js/vault.js');
        await vault.pollOnlineOcr();
        expect(document.getElementById('daily-index-status').textContent).toContain('1/3 documents');
        expect(document.getElementById('daily-index-status').textContent).not.toContain('/private');
        vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('network'); }));
        await vault.pollOnlineOcr();
        expect(document.getElementById('activity-label-text').textContent).toContain('Status unknown');
        vi.stubGlobal('fetch', vi.fn(async () => ({ok: true, json: async () => ({state: 'indexed', run_id: 'projection'})})));
        await vault.pollOnlineOcr();
        vi.unstubAllGlobals();
    });
});

describe('phase-specific activity progress', () => {
    it('uses indexing progress and reconciles final counts and filename', async () => {
        vi.resetModules();
        vi.useFakeTimers();
        document.body.innerHTML = '<p id="daily-index-status"></p><div id="activity-bar"><span id="activity-label-text"></span><div id="activity-tasks"></div></div>';
        const payload = {state: 'running', run_id: 'phase-test', result_count: 3, source_count: 32, current_source: 'old.pdf'};
        vi.stubGlobal('fetch', vi.fn(async () => ({ok: true, json: async () => ({...payload})})));
        try {
            const vault = await import('../../static/js/vault.js');
            await vault.pollOnlineOcr();
            expect(document.querySelector('progress').value).toBe(3);
            Object.assign(payload, {state: 'indexing', result_count: 32, index_progress: {phase: 'embedding', message: '537 embedded, 533951 reused'}});
            await vault.pollOnlineOcr();
            expect(document.querySelector('progress').hasAttribute('value')).toBe(false);
            expect(document.getElementById('activity-tasks').textContent).toContain('537 embedded');
            expect(document.querySelector('.activity-task strong').textContent).not.toContain('old.pdf');
            Object.assign(payload, {state: 'completed_with_omissions', deferred_count: 32, bm25_status: 'ready'});
            await vault.pollOnlineOcr();
            const row = document.getElementById('activity-tasks');
            expect(row.textContent).toContain('32/32 documents examined for extraction');
            expect(row.textContent).toContain('Partly complete');
            expect(row.textContent).not.toContain('old.pdf');
        } finally {
            vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals();
        }
    });
});

describe('authoritative operation monitoring', () => {
    it('discovers external work after idle and ignores remembered history', async () => {
        vi.resetModules(); vi.useFakeTimers();
        const memory = new Map([['papermind-extraction-run', 'old-browser-run']]);
        vi.stubGlobal('localStorage', {getItem: k => memory.get(k), setItem: (k, v) => memory.set(k, v), removeItem: k => memory.delete(k)});
        document.body.innerHTML = '<p id="library-preparation-status"></p><div id="activity-bar"><span id="activity-label-text"></span><div id="activity-tasks"></div></div>';
        let status = {state: 'idle'};
        const fetch = vi.fn(async url => {
            expect(String(url)).toBe('/api/online-ocr/status');
            return {ok: true, json: async () => ({...status})};
        });
        vi.stubGlobal('fetch', fetch);
        try {
            const vault = await import('../../static/js/vault.js');
            await vault.pollOnlineOcr();
            status = {state: 'running', run_id: 'external', current_source: 'actual.pdf', result_count: 2, source_count: 4};
            await vi.advanceTimersByTimeAsync(5000);
            expect(document.getElementById('library-preparation-status').textContent).toContain('actual.pdf');
            expect(document.getElementById('activity-label-text').textContent).toContain('actual.pdf');
            expect(localStorage.getItem('papermind-extraction-run')).toBe('old-browser-run');
        } finally { vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals(); }
    });

    it('follows a busy rejection without replaying the launch', async () => {
        vi.resetModules(); vi.useFakeTimers();
        document.body.innerHTML = '<button id="vault-index-btn">Prepare</button><p id="library-preparation-status"></p><div id="activity-bar"><span id="activity-label-text"></span></div>';
        const starts = [];
        vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
            const path = String(url);
            if (path.endsWith('/session/start')) {
                starts.push(JSON.parse(options.body));
                return {ok: false, status: 409, json: async () => ({error: 'An OCR or index job is running'})};
            }
            return {ok: true, json: async () => path.endsWith('/session/preview')
                ? {eligible: 0, source_count: 2, session_token: 'intent-v1:fixture'}
                : path.endsWith('/status') ? {state: 'running', run_id: 'other', current_source: 'active.pdf'} : {ok: true}};
        }));
        try {
            const vault = await import('../../static/js/vault.js');
            await vault.previewLocalIndex(document.getElementById('vault-index-btn'), true, null, false);
            [...document.querySelectorAll('button')].find(b => b.textContent === 'Prepare available text').click();
            await vi.advanceTimersByTimeAsync(10);
            expect(starts).toHaveLength(1);
            expect(starts[0]).toMatchObject({operation_mode: 'prepare_text', confirm_index: false});
            expect(document.getElementById('library-preparation-status').textContent).toContain('active.pdf');
            expect(document.getElementById('activity-label-text').textContent).toContain('active.pdf');
        } finally { vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals(); }
    });
});

it('malformed status cannot unlock controls or claim idle', async () => {
    vi.resetModules(); vi.useFakeTimers();
    document.body.innerHTML = '<p id="library-preparation-status"></p><div id="activity-bar"><span id="activity-label-text"></span><div id="activity-tasks"></div></div>';
    vi.stubGlobal('fetch', vi.fn(async () => ({ok: true, json: async () => ({state: 'nonsense'})})));
    try {
        const vault = await import('../../static/js/vault.js');
        await vault.pollOnlineOcr();
        expect(document.getElementById('library-preparation-status').textContent).toContain('Status unknown');
        expect(document.getElementById('activity-label-text').textContent).toContain('Status unknown');
    } finally { vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals(); }
});
