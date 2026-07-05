// Pinning tests for vault.js's split in-flight flags (improvement plan
// 2026-07-04, item 3.1). The defect: ONE module flag served both the chat
// in-flight guard and the indexing lifecycle — a multi-hour index run blocked
// vault chat entirely (the server supports chat during indexing), and
// cancelVaultIndex force-cleared whichever guard was held, including a live
// chat's. Invariants pinned: an in-flight index run never refuses a chat, and
// cancelling an index run never clears the chat guard.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const enc = new TextEncoder();

/** Minimal DOM the vault chat/index paths touch. */
function mountVaultDom() {
    document.body.innerHTML = `
      <div id="obsidian-status-msg"></div>
      <div id="vault-error-boundary"></div>
      <input id="obsidian-input" value="">
      <div id="obsidian-chat"></div>
      <div id="vault-index-progress" hidden></div>
      <input id="vault-wikilink-enabled" type="checkbox">
      <input id="vault-thesaurus-enabled" type="checkbox">
      <input id="vault-primer-enabled" type="checkbox">
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

describe('vault in-flight flags (Track 3.1)', () => {
    let fetchMock;
    beforeEach(() => {
        mountVaultDom();
        fetchMock = vi.fn();
        vi.stubGlobal('fetch', fetchMock);
    });
    afterEach(() => vi.unstubAllGlobals());

    it('an in-flight index run does not refuse a chat', async () => {
        const vault = await import('../../static/js/vault.js');

        // Start an index run: /api/obsidian/index ok, then the status poll
        // parks on a pending promise (run "in flight" forever).
        fetchMock.mockImplementation((url) => {
            if (String(url).includes('/api/obsidian/index')) {
                return Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true }) });
            }
            if (String(url).includes('/api/obsidian/status')) {
                return new Promise(() => {});   // poll never resolves — stays "running"
            }
            if (String(url).includes('/api/obsidian/chat')) {
                return Promise.resolve(sseOk([{ token: 'bonjour' }]));
            }
            if (String(url).includes('/api/config')) {
                return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
        });

        await vault.indexVault();
        // The old shared flag would make chatWithVault bail here with
        // "Please wait for the current response to complete."
        document.getElementById('obsidian-input').value = 'ma question';
        await vault.chatWithVault();

        const chatCalls = fetchMock.mock.calls.filter(([u]) =>
            String(u).includes('/api/obsidian/chat'));
        expect(chatCalls.length).toBe(1);         // the chat request WENT OUT
        const chatEl = document.getElementById('obsidian-chat');
        expect(chatEl.textContent).toContain('bonjour');
        // Cleanup: settle the index machine so later tests start idle.
        await vault.cancelVaultIndex();
    });

    it('cancelling an index run does not clear the chat guard', async () => {
        const vault = await import('../../static/js/vault.js');

        // A chat parked mid-stream (fetch never resolves) → chat guard held.
        let resolveChat;
        fetchMock.mockImplementation((url) => {
            if (String(url).includes('/api/obsidian/chat')) {
                return new Promise((res) => { resolveChat = res; });
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
        });
        document.getElementById('obsidian-input').value = 'q1';
        const chatPromise = vault.chatWithVault();   // in flight, guard held
        await new Promise((r) => setTimeout(r, 0));

        await vault.cancelVaultIndex();              // must NOT touch the chat guard

        // Second send while the first is still streaming must be refused —
        // with the pre-fix shared flag, cancelVaultIndex had just cleared it.
        document.getElementById('obsidian-input').value = 'q2';
        await vault.chatWithVault();
        const chatCalls = fetchMock.mock.calls.filter(([u]) =>
            String(u).includes('/api/obsidian/chat'));
        expect(chatCalls.length).toBe(1);            // no double-send

        resolveChat(sseOk([{ token: 'fin' }]));      // let the first finish
        await chatPromise;
    });

    it('sends the three documented live overrides in the chat body (Track 3.6)', async () => {
        const vault = await import('../../static/js/vault.js');
        let sentBody = null;
        fetchMock.mockImplementation((url, opts = {}) => {
            if (String(url).includes('/api/obsidian/chat')) {
                sentBody = JSON.parse(opts.body);
                return Promise.resolve(sseOk([{ token: 'ok' }]));
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
        });
        // Flip all three live toggles ON — the body must carry the LIVE values
        // (pre-fix the server fell back to the persisted config, so a toggle
        // inside the save debounce ran with the old value).
        document.getElementById('vault-wikilink-enabled').checked = true;
        document.getElementById('vault-thesaurus-enabled').checked = true;
        document.getElementById('vault-primer-enabled').checked = true;
        document.getElementById('obsidian-input').value = 'q';
        await vault.chatWithVault();
        expect(sentBody).not.toBeNull();
        expect(sentBody.wikilink_expansion).toBe(true);
        expect(sentBody.thesaurus_expansion).toBe(true);
        expect(sentBody.primer_enabled).toBe(true);
    });
});
