// Paper labels (2026-09-23): the `vault_paper_labels` switch is a config-only
// vault knob persisted by settings.js like `vault_prewarm_enabled` — loaded
// from /api/config into the checkbox, and sent back on save. It is never a
// chat-body override (pinned server-side in tests/test_paper_labels_chat.py).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

function markup() {
    return `
      <div id="sr-status-announcer" role="status"></div>
      <div id="sr-error-announcer" role="alert"></div>
      <div id="settings-status" role="status"></div>
      <div id="settings-modal" class="modal-overlay">
        <input type="search" id="settings-filter">
        <span id="settings-filter-count"></span>
        <details class="settings-section" open>
          <summary>Vault</summary>
          <div class="control-group">
            <label class="checkbox-row"><input type="checkbox" id="set-paper-labels-enabled" checked> Paper labels</label>
          </div>
        </details>
      </div>`;
}

describe('paper-labels setting persistence', () => {
    let posts;
    let settings;
    beforeEach(async () => {
        document.body.innerHTML = markup();
        vi.resetModules();
        posts = [];
        vi.stubGlobal('fetch', vi.fn((url, opts = {}) => {
            if ((opts.method || 'GET') === 'POST') {
                posts.push(JSON.parse(opts.body));
                return Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true, dropped: [] }) });
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({ vault_paper_labels: false }) });
        }));
        vi.stubGlobal('requestAnimationFrame', (cb) => { cb(); return 0; });
        settings = await import('../../static/js/settings.js');
    });
    afterEach(() => vi.unstubAllGlobals());

    it('loads the persisted value into the checkbox', () => {
        settings.initSettings({ vault_paper_labels: false });
        expect(document.getElementById('set-paper-labels-enabled').checked).toBe(false);
    });

    it('defaults to on when the key is absent', () => {
        settings.initSettings({});
        expect(document.getElementById('set-paper-labels-enabled').checked).toBe(true);
    });

    it('sends vault_paper_labels on save', async () => {
        settings.initSettings({ vault_paper_labels: true });
        document.getElementById('set-paper-labels-enabled').checked = false;
        await settings.saveSettings();
        expect(posts.length).toBe(1);
        expect(posts[0].vault_paper_labels).toBe(false);
    });
});
