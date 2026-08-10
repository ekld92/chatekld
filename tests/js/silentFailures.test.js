// C1 (2026-07-13 review): the silent-failure basket. Several handlers used to
// swallow a failed fetch (only console.error) or claim success on a failed POST,
// leaving the user with no visible signal. These pins assert the error branch
// now renders a visible message / does not falsely report success.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

function okJson(body) {
    return { ok: true, status: 200, json: async () => body };
}
function errJson(status, body) {
    return { ok: false, status, json: async () => body };
}

describe('C1 silent-failure surfacing (vault.js)', () => {
    let fetchMock;
    beforeEach(() => {
        document.body.innerHTML = `
          <div id="vault-path-display"></div>
          <div id="obsidian-status-msg"></div>
          <div id="excl-status"></div>
          <button id="vault-index-btn"></button>
          <button id="vault-pause-btn"></button>
          <button id="vault-resume-btn"></button>
          <button id="vault-cancel-btn"></button>
          <div id="vault-index-progress"></div>
        `;
        vi.resetModules();
        fetchMock = vi.fn();
        vi.stubGlobal('fetch', fetchMock);
    });
    afterEach(() => vi.unstubAllGlobals());

    it('pickVaultFolder surfaces a rejected vault path instead of closing silently', async () => {
        fetchMock.mockImplementation((url) => {
            if (String(url).includes('native-pick-folder')) return Promise.resolve(okJson({ path: '/Users/me/Documents' }));
            if (String(url).includes('/api/config')) return Promise.resolve(errJson(400, { error: 'Vault path is too broad.' }));
            return Promise.resolve(okJson({}));
        });
        const vault = await import('../../static/js/vault.js');
        await vault.pickVaultFolder();
        expect(document.getElementById('obsidian-status-msg').textContent).toContain('too broad');
        // The display was NOT updated to the rejected path.
        expect(document.getElementById('vault-path-display').textContent).toBe('');
    });

    it('cancelVaultIndex does NOT claim "cancelled" when the POST fails', async () => {
        fetchMock.mockResolvedValue(errJson(500, { error: 'server down' }));
        const vault = await import('../../static/js/vault.js');
        await vault.cancelVaultIndex();
        const msg = document.getElementById('obsidian-status-msg').textContent;
        expect(msg).not.toContain('cancelled');
        expect(msg.toLowerCase()).toContain('continues');
    });

    it('removeExclusion surfaces a failed save', async () => {
        fetchMock.mockImplementation((url, opts = {}) => {
            if (!opts.method) return Promise.resolve(okJson({ vault_exclude_dirs: ['a/b'] }));
            return Promise.resolve(errJson(400, { error: 'could not save exclusions' }));
        });
        const vault = await import('../../static/js/vault.js');
        await vault.removeExclusion('a/b');
        expect(document.getElementById('excl-status').textContent).toContain('could not save');
    });
});
