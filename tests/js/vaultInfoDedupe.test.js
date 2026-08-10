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

function sseOk(frames) {
    const payload =
        frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('') + 'data: [DONE]\n\n';
    let sent = false;
    return {
        ok: true,
        status: 200,
        body: {
            getReader: () => ({
                read: async () =>
                    sent
                        ? { value: undefined, done: true }
                        : ((sent = true), { value: enc.encode(payload), done: false }),
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
    afterEach(() => vi.unstubAllGlobals());

    it('progressKey is digit-blind and nothing else', () => {
        const a = vault.progressKey("Still waiting for the reranker 'm' (30s) — downloading…");
        const b = vault.progressKey("Still waiting for the reranker 'm' (60s) — downloading…");
        const c = vault.progressKey('Retrieving context…');
        expect(a).toBe(b);
        expect(a).not.toBe(c);
        expect(vault.progressKey('')).toBe('');
    });

    it('repeated heartbeats update one bubble; new stages append', async () => {
        vi.stubGlobal('fetch', vi.fn((url) => {
            if (String(url).includes('/api/obsidian/chat')) {
                return Promise.resolve(sseOk([
                    { info: 'Still waiting for the reranker (30s)…' },
                    { info: 'Still waiting for the reranker (60s)…' },
                    { info: 'Still waiting for the reranker (90s)…' },
                    { info: 'Retrieving context…' },
                    { token: 'bonjour' },
                ]));
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
        }));

        await vault.chatWithVault();

        const infos = [...document.querySelectorAll('#obsidian-chat .message-info')];
        expect(infos.length, 'three heartbeats must collapse into one bubble').toBe(2);
        // The surviving heartbeat bubble carries the LATEST figure.
        expect(infos[0].textContent).toContain('(90s)');
        expect(infos[1].textContent).toContain('Retrieving context');
    });

    it('an A/B/A sequence keeps three bubbles (no history rewriting)', async () => {
        vi.stubGlobal('fetch', vi.fn((url) => {
            if (String(url).includes('/api/obsidian/chat')) {
                return Promise.resolve(sseOk([
                    { info: 'Building lexical index (10s)…' },
                    { info: 'Retrieving context…' },
                    { info: 'Building lexical index (40s)…' },
                    { token: 'ok' },
                ]));
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
        }));

        await vault.chatWithVault();

        const infos = [...document.querySelectorAll('#obsidian-chat .message-info')];
        expect(infos.length).toBe(3);
    });
});
