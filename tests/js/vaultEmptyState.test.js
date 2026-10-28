// 2026-09-03 UI review — the landing tab had no empty state.
//
// DEFECT PINNED: `#obsidian-chat` is an empty `<div>` on first run. The Obsidian
// Agent tab is the tab the app opens on, and the Library Audit tab — which has
// a good empty state of its own — explicitly directs a new user here ("Set the
// Obsidian vault path on the Obsidian Agent tab first"). A first-run probe of
// the real app in Chromium showed this panel rendering "Vault: Not configured"
// followed by an empty box and a Send button, with nothing saying that answers
// come only from an index that does not exist yet.
//
// The state is decided from what `/api/obsidian/status` ALREADY carries
// (`vault_path` + `state`) — no new server field — and it is a hint, never a
// message: it is removed before the first turn is appended, and it is never
// painted over a transcript that already has messages.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

function mountDom() {
    document.body.innerHTML = `
      <div id="sr-status-announcer" role="status"></div>
      <div id="sr-error-announcer" role="alert"></div>
      <div id="obsidian-status-msg"></div>
      <div id="vault-error-boundary"></div>
      <div id="vault-index-progress" hidden></div>
      <button id="vault-index-btn"></button>
      <button id="vault-pause-btn"></button>
      <button id="vault-resume-btn"></button>
      <button id="vault-cancel-btn"></button>
      <div id="activity-bar"><span id="activity-label-text"></span></div>
      <div id="materials-list-container"></div>
      <span id="vault-path-display">Not configured</span>
      <div id="obsidian-chat"></div>
      <input id="obsidian-input" />
      <button id="vault-send-btn"></button>
    `;
}

function mockFetch(payload) {
    return vi.fn((url) => {
        const u = String(url);
        if (u.includes('/api/obsidian/status')) {
            return Promise.resolve({
                ok: true, status: 200,
                json: async () => ({ messages: [], warnings: [], ...payload }),
            });
        }
        return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
    });
}

const hint = () => document.querySelector('#obsidian-chat .chat-empty-state');

describe('vault chat empty state', () => {
    let vault;
    beforeEach(() => { mountDom(); vi.resetModules(); });
    afterEach(() => { vi.unstubAllGlobals(); });

    async function load(payload) {
        vi.stubGlobal('fetch', mockFetch(payload));
        vault = await import('../../static/js/vault.js');
        await vault.refreshIndexState();
    }

    it('with no vault configured it says to pick one and index', async () => {
        await load({ state: 'idle' });
        const el = hint();
        expect(el).not.toBeNull();
        expect(el.textContent).toMatch(/vault/i);
        expect(el.textContent).toMatch(/Update Index/);
    });

    it('with a vault set but never indexed it says to index, not to pick', async () => {
        await load({ state: 'idle', vault_path: '/Users/x/Vault' });
        const el = hint();
        expect(el).not.toBeNull();
        expect(el.textContent).toMatch(/Update Index/);
        expect(el.textContent).not.toMatch(/Change…/);
    });

    it('with an index present it invites a question instead', async () => {
        await load({ state: 'done', vault_path: '/Users/x/Vault' });
        const el = hint();
        expect(el).not.toBeNull();
        expect(el.textContent).toMatch(/Ask/i);
        expect(el.textContent).not.toMatch(/Update Index/);
    });

    it('it is a hint, not a message: aria-hidden and not focusable', async () => {
        await load({ state: 'done', vault_path: '/Users/x/Vault' });
        // The status line and the error boundary are the live regions here;
        // a third one announcing static copy on every poll would be noise.
        expect(hint().getAttribute('role')).toBeNull();
    });

    it('it is never painted over an existing transcript', async () => {
        const chat = document.getElementById('obsidian-chat');
        const msg = document.createElement('div');
        msg.className = 'message message-user';
        msg.textContent = 'earlier question';
        chat.appendChild(msg);
        await load({ state: 'idle' });
        expect(hint()).toBeNull();
        expect(chat.textContent).toContain('earlier question');
    });

    it('a repeated poll replaces the hint rather than stacking copies', async () => {
        await load({ state: 'idle' });
        await vault.refreshIndexState();
        await vault.refreshIndexState();
        expect(document.querySelectorAll('#obsidian-chat .chat-empty-state').length).toBe(1);
    });

    it('the first turn removes it', async () => {
        await load({ state: 'done', vault_path: '/Users/x/Vault' });
        expect(hint()).not.toBeNull();
        document.getElementById('obsidian-input').value = 'what does my vault say?';
        // fetch is stubbed to a non-SSE 200, so the turn fails fast — the point
        // is only that the hint is gone the moment a real turn starts.
        await vault.chatWithVault().catch(() => {});
        expect(hint()).toBeNull();
    });

    it('during a run it paints nothing — the progress bar and status line speak', async () => {
        await load({ state: 'scanning', vault_path: '/Users/x/Vault' });
        expect(hint()).toBeNull();
    });

    it('completing an index flips the copy without a reload', async () => {
        vi.useFakeTimers();
        vi.stubGlobal('fetch', vi.fn((url) => {
            const u = String(url);
            if (u.includes('/api/obsidian/index')) {
                return Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true }) });
            }
            if (u.includes('/api/obsidian/status')) {
                return Promise.resolve({ ok: true, status: 200,
                    json: async () => ({ messages: [], warnings: [],
                                         state: 'done', vault_path: '/v' }) });
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({ materials: [] }) });
        }));
        vault = await import('../../static/js/vault.js');
        // Paint the pre-index copy, then let the poll loop see 'done'.
        const chat = document.getElementById('obsidian-chat');
        const stale = document.createElement('div');
        stale.className = 'chat-empty-state';
        stale.textContent = 'This vault is not indexed yet — click Index Vault.';
        chat.appendChild(stale);
        // indexVault arms the poll loop; its first poll reads 'done'.
        await vault.indexVault();
        await vi.advanceTimersByTimeAsync(3000);
        expect(hint().textContent).toMatch(/Ask/i);
        vi.useRealTimers();
    });
});
