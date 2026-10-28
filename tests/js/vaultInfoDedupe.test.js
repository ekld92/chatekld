// 2026-07-29 audit 11 — heartbeat frames must not multiply.
//
// DEFECT PINNED: every `{info}` SSE frame appended a new .message-info bubble
// and fired a screen-reader announcement. The reranker/BM25 load heartbeats
// emit the SAME sentence every 30 s with only the seconds figure changing, so
// a 20-minute first-time model download produced ~40 near-identical bubbles
// and ~40 VoiceOver announcements. Invariants: (1) a frame differing from the
// previous one only by digits UPDATES that bubble in place; (2) a genuinely
// different stage label still appends a new bubble; (3) an A/B/A sequence
// renders three bubbles (only the immediately-preceding frame is compared —
// history is never rewritten).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const enc = new TextEncoder();

function mountVaultDom() {
    document.body.innerHTML = `
      <div id="sr-status-announcer" role="status"></div>
      <div id="sr-error-announcer" role="alert"></div>
      <div id="obsidian-status-msg"></div>
      <div id="vault-error-boundary"></div>
      <input id="obsidian-input" value="ma question">
      <div id="obsidian-chat"></div>
      <div id="vault-index-progress" hidden></div>
    `;
}

function sseOk(frames, completion = Promise.resolve()) {
    const payload =
        frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('');
    let sent = false;
    return {
        ok: true,
        status: 200,
        body: {
            getReader: () => ({
                read: async () => {
                    if (!sent) { sent = true; return {value: enc.encode(payload), done: false}; }
                    await completion;
                    return {value: enc.encode('data: [DONE]\n\n'), done: false};
                },
                releaseLock: () => {},
                cancel: async () => {},
            }),
        },
    };
}

describe('vault chat info-frame dedupe (2026-07-29)', () => {
    let vault;
    beforeEach(async () => {
        mountVaultDom();
        vi.resetModules();
        vault = await import('../../static/js/vault.js');
    });
    afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

    it('progressKey is digit-blind and nothing else', () => {
        const a = vault.progressKey("Still waiting for the reranker 'm' (30s) — downloading…");
        const b = vault.progressKey("Still waiting for the reranker 'm' (60s) — downloading…");
        const c = vault.progressKey('Retrieving context…');
        expect(a).toBe(b);
        expect(a).not.toBe(c);
        expect(vault.progressKey('')).toBe('');
    });

    it('repeated heartbeats update one bubble; new stages append', async () => {
        let finish;
        const completion = new Promise(resolve => { finish = resolve; });
        vi.stubGlobal('fetch', vi.fn((url) => {
            if (String(url).includes('/api/obsidian/chat')) {
                return Promise.resolve(sseOk([
                    { info: 'Still waiting for the reranker (30s)…' },
                    { info: 'Still waiting for the reranker (60s)…' },
                    { info: 'Still waiting for the reranker (90s)…' },
                    { info: 'Retrieving context…' },
                    { token: 'bonjour' },
                ], completion));
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
        }));

        const run = vault.chatWithVault();
        await vi.waitFor(() => expect(document.querySelectorAll('#obsidian-chat .message-info').length).toBeGreaterThan(0));

        const infos = [...document.querySelectorAll('#obsidian-chat .message-info')];
        expect(infos.length, 'three heartbeats must collapse into one bubble').toBe(2);
        // The surviving heartbeat bubble carries the LATEST figure.
        expect(infos[0].textContent).toContain('(90s)');
        expect(infos[1].textContent).toContain('Retrieving context');
        finish();
        await run;
        expect(document.querySelectorAll('#obsidian-chat .message-info')).toHaveLength(0);
    });

    it('an A/B/A sequence keeps three bubbles (no history rewriting)', async () => {
        let finish;
        const completion = new Promise(resolve => { finish = resolve; });
        vi.stubGlobal('fetch', vi.fn((url) => {
            if (String(url).includes('/api/obsidian/chat')) {
                return Promise.resolve(sseOk([
                    { info: 'Building lexical index (10s)…' },
                    { info: 'Retrieving context…' },
                    { info: 'Building lexical index (40s)…' },
                    { token: 'ok' },
                ], completion));
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
        }));

        const run = vault.chatWithVault();
        await vi.waitFor(() => expect(document.querySelectorAll('#obsidian-chat .message-info').length).toBeGreaterThan(0));

        const infos = [...document.querySelectorAll('#obsidian-chat .message-info')];
        expect(infos.length).toBe(3);
        finish();
        await run;
        expect(document.querySelectorAll('#obsidian-chat .message-info')).toHaveLength(0);
    });
    it('clears waiting stages on error while preserving durable notices and partial text', async () => {
        vi.stubGlobal('fetch', vi.fn(url => Promise.resolve(String(url).includes('/api/obsidian/chat')
            ? sseOk([{info: 'Waiting for model response…'}, {info: 'Coverage is limited.'},
                {token: 'Kept answer'}, {error: 'Disconnected'}])
            : {ok: true, status: 200, json: async () => ({})})));
        await vault.chatWithVault();
        const chat = document.getElementById('obsidian-chat');
        expect(chat.textContent).not.toContain('Waiting for model response');
        expect(chat.textContent).toContain('Coverage is limited.');
        expect(chat.textContent).toContain('Kept answer');
        expect(chat.textContent).toContain('interrupted');
    });

    it('cleans progress on Stop and ignores callbacks from the finished turn', async () => {
        vi.stubGlobal('fetch', vi.fn(async () => ({ok: true, json: async () => ({})})));
        const api = await import('../../static/js/api.js');
        let handlers;
        vi.spyOn(api, 'consumeSSE').mockImplementation((_url, options, callbacks) => {
            handlers = callbacks;
            callbacks.onInfo('Waiting for model response…');
            callbacks.onToken('Partial answer');
            return new Promise((_resolve, reject) => options.signal.addEventListener('abort', () =>
                reject(Object.assign(new Error('Stopped'), {name: 'AbortError'}))));
        });
        const run = vault.chatWithVault();
        await vi.waitFor(() => expect(handlers).toBeDefined());
        vault.stopChat();
        await run;
        const chat = document.getElementById('obsidian-chat');
        expect(chat.textContent).not.toContain('Waiting for model response');
        expect(chat.textContent).toContain('Partial answer');
        const terminal = chat.innerHTML;
        handlers.onInfo('Late progress…');
        handlers.onToken('Late answer');
        handlers.onOther({retrieval: [{source: 'late.pdf'}]});
        expect(chat.innerHTML).toBe(terminal);
    });

});
