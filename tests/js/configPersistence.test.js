// Pinning tests for config.js model persistence (improvement plan 2026-07-04,
// item 3.2). Defects pinned: (a) loadModels unconditionally ended with
// saveSelectedModels(), so booting against a degraded/offline backend
// silently ADOPTED and PERSISTED the first listed model over the user's saved
// one; (b) the embed fallback offered the whole (chat) model list as
// embedding candidates and the load-time save persisted a chat model as
// `embed`; (c) two rapid provider switches interleaved the 4-step async
// sequence and the stale switch's list could land last. Invariants: loads
// never save (only explicit user change events persist), no chat model is
// ever offered as an embed candidate, and only the latest switch's list
// survives into the DOM.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

function mountConfigDom() {
    document.body.innerHTML = `
      <select id="provider-select"><option value="ollama">ollama</option><option value="lm_studio">lm_studio</option></select>
      <select id="model-select"></select>
      <select id="embed-select"></select>
      <button id="pull-model-btn"></button>
      <div id="provider-badge"></div>
    `;
}

/** Route the config module's fetches; records POSTs to /api/config. */
function routeFetch(fetchMock, { models, config = {}, onConfigPost } = {}) {
    fetchMock.mockImplementation((url, opts = {}) => {
        const u = String(url);
        if (u.includes('/api/config') && (opts.method || 'GET') === 'POST') {
            if (onConfigPost) onConfigPost(JSON.parse(opts.body));
            return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
        }
        if (u.includes('/api/config')) {
            return Promise.resolve({ ok: true, status: 200, json: async () => config });
        }
        if (u.includes('/api/models')) {
            const m = typeof models === 'function' ? models() : models;
            return Promise.resolve({ ok: true, status: 200, json: async () => ({ models: m }) });
        }
        if (u.includes('/api/vision-models')) {
            return Promise.resolve({ ok: true, status: 200, json: async () => ({ models: [] }) });
        }
        return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
    });
}

describe('config.js model persistence (Track 3.2)', () => {
    let fetchMock;
    beforeEach(() => {
        mountConfigDom();
        vi.resetModules();          // fresh module state (gen counters, caches)
        fetchMock = vi.fn();
        vi.stubGlobal('fetch', fetchMock);
    });
    afterEach(() => vi.unstubAllGlobals());

    it('loadModels never POSTs config — even when the saved model is missing', async () => {
        const posts = [];
        routeFetch(fetchMock, {
            models: ['llama3', 'qwen'],        // saved model NOT in the list
            config: { provider: 'ollama', llm: 'my-saved-model', embed: 'nomic-embed-text' },
            onConfigPost: (b) => posts.push(b),
        });
        const config = await import('../../static/js/config.js');
        await config.loadConfig();             // primes _configModel = my-saved-model
        posts.length = 0;                      // ignore anything loadConfig-adjacent
        await config.loadModels();
        expect(posts).toEqual([]);             // the boot rewrite is gone
        // The DOM shows an option, but nothing was adopted or persisted.
        expect(document.getElementById('model-select').value).toBe('llama3');
    });

    it('restores the saved model when present, still without saving', async () => {
        const posts = [];
        routeFetch(fetchMock, {
            models: ['llama3', 'my-saved-model'],
            config: { provider: 'ollama', llm: 'my-saved-model' },
            onConfigPost: (b) => posts.push(b),
        });
        const config = await import('../../static/js/config.js');
        await config.loadConfig();
        posts.length = 0;
        await config.loadModels();
        expect(document.getElementById('model-select').value).toBe('my-saved-model');
        expect(posts).toEqual([]);
    });

    it('an explicit user change event persists the selection', async () => {
        const posts = [];
        routeFetch(fetchMock, {
            models: ['llama3', 'qwen'],
            config: { provider: 'ollama', llm: 'llama3' },
            onConfigPost: (b) => posts.push(b),
        });
        const config = await import('../../static/js/config.js');
        await config.loadConfig();
        await config.loadModels();
        posts.length = 0;
        const sel = document.getElementById('model-select');
        sel.value = 'qwen';
        sel.onchange();                        // the change handler IS the persist path
        await new Promise((r) => setTimeout(r, 0));
        expect(posts.some((p) => p.llm === 'qwen')).toBe(true);
    });

    it('never offers chat models as embedding candidates (fallback killed)', async () => {
        routeFetch(fetchMock, {
            models: ['llama3', 'qwen'],        // nothing embed-shaped
            config: { provider: 'ollama', llm: 'llama3' },
        });
        const config = await import('../../static/js/config.js');
        await config.loadConfig();
        await config.loadModels();
        const embed = document.getElementById('embed-select');
        expect(embed.disabled).toBe(true);     // honest empty state
        // The only option is the disabled placeholder — no selectable chat
        // model smuggled in (an <option> without a value attr reports its
        // text as .value, so assert on selectability, not value).
        const selectable = [...embed.options].filter((o) => !o.disabled);
        expect(selectable).toEqual([]);
    });

    it('only the latest of two racing provider switches lands in the DOM', async () => {
        // First switch's /api/models hangs until after the second finishes.
        let releaseFirstModels;
        let modelCall = 0;
        fetchMock.mockImplementation((url, opts = {}) => {
            const u = String(url);
            if (u.includes('/api/models')) {
                modelCall += 1;
                if (modelCall === 1) {
                    return new Promise((res) => {
                        releaseFirstModels = () =>
                            res({ ok: true, status: 200, json: async () => ({ models: ['STALE-A'] }) });
                    });
                }
                return Promise.resolve({ ok: true, status: 200, json: async () => ({ models: ['fresh-b'] }) });
            }
            if (u.includes('/api/vision-models')) {
                return Promise.resolve({ ok: true, status: 200, json: async () => ({ models: [] }) });
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
        });
        const config = await import('../../static/js/config.js');
        const sel = document.getElementById('provider-select');

        sel.value = 'ollama';
        const first = config.onProviderChange();     // parks on /api/models
        await new Promise((r) => setTimeout(r, 0));
        sel.value = 'lm_studio';
        await config.onProviderChange();             // completes fully
        releaseFirstModels();                        // stale switch resumes late
        await first;

        const values = [...document.getElementById('model-select').options].map((o) => o.value);
        expect(values).toEqual(['fresh-b']);          // stale list never landed
    });
});
