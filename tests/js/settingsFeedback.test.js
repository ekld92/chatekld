// 2026-07-26 UX audit — silent config rejection (WCAG 3.3.1 Error
// Identification).
//
// DEFECT PINNED: api/routes/config.py::_validate_llm_config_keys POPS an
// out-of-range/malformed key and the route still answers 200 with no mention of
// it. settings.js surfaced only network/HTTP failures, and initSettings ran
// exactly once at boot with the modal never reloading — so a rejected value sat
// in the input for the whole session while the server kept the old one, with
// nothing visible, nothing announced, and no way to tell which of the ~85
// controls was ignored.
//
// Invariants: (1) every key named in the response's `dropped` array is flagged
// inline AND announced; (2) a later clean save clears the stale marker; (3)
// re-opening the modal re-reads /api/config so the field can never keep
// disagreeing with the server; (4) a refresh never fights an in-flight save.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

function modalMarkup() {
    return `
      <div id="sr-status-announcer" role="status"></div>
      <div id="sr-error-announcer" role="alert"></div>
      <div id="settings-modal" class="modal-overlay">
        <input type="search" id="settings-filter">
        <span id="settings-filter-count"></span>
        <details class="settings-section" open>
          <summary>Global</summary>
          <div class="control-group">
            <label for="set-online-timeout">Request timeout (seconds)</label>
            <input type="number" id="set-online-timeout">
          </div>
          <div class="control-group">
            <label for="set-vision-timeout">Vision timeout</label>
            <input type="number" id="set-vision-timeout">
          </div>
        </details>
      </div>`;
}

/** POST /api/config answers 200 with the given dropped list; GET answers cfg. */
function mockConfigFetch(state) {
    return vi.fn((url, opts = {}) => {
        if (String(url).includes('/api/config') && (opts.method || 'GET') === 'POST') {
            state.posts.push(JSON.parse(opts.body));
            return Promise.resolve({
                ok: true, status: 200,
                json: async () => ({ ok: true, dropped: state.dropped }),
            });
        }
        state.gets += 1;
        return Promise.resolve({ ok: true, status: 200, json: async () => state.cfg });
    });
}

describe('settings dropped-key feedback (2026-07-26)', () => {
    let state;
    let settings;
    beforeEach(async () => {
        document.body.innerHTML = modalMarkup();
        vi.resetModules();
        state = { posts: [], gets: 0, dropped: [], cfg: { online_timeout_s: 60 } };
        vi.stubGlobal('fetch', mockConfigFetch(state));
        vi.stubGlobal('requestAnimationFrame', (cb) => { cb(); return 0; });
        settings = await import('../../static/js/settings.js');
        settings.initSettings({ online_timeout_s: 60 });
    });
    afterEach(() => vi.unstubAllGlobals());

    it('flags the offending control inline and announces it', async () => {
        state.dropped = ['online_timeout_s'];
        await settings.saveSettings();

        const el = document.getElementById('set-online-timeout');
        expect(el.getAttribute('aria-invalid')).toBe('true');
        const note = el.closest('.control-group').querySelector('.settings-field-error');
        expect(note, 'no inline marker on the refused field').toBeTruthy();
        expect(note.textContent).toMatch(/not saved/i);
        // Announced through the STATIC region (never a live region minted at
        // write time — the liveRegions.test.js rule).
        const announcer = document.getElementById('sr-error-announcer');
        expect(announcer.textContent).toContain('Request timeout');
        // The untouched control is not implicated.
        expect(document.getElementById('set-vision-timeout').getAttribute('aria-invalid')).toBe(null);
    });

    it('a subsequent clean save clears the stale marker', async () => {
        state.dropped = ['online_timeout_s'];
        await settings.saveSettings();
        state.dropped = [];
        await settings.saveSettings();
        expect(document.querySelectorAll('.settings-field-error').length).toBe(0);
        expect(document.getElementById('set-online-timeout').getAttribute('aria-invalid')).toBe(null);
    });

    it('re-opening the modal re-reads /api/config so the field matches the server', async () => {
        const overlay = document.getElementById('settings-modal');
        state.cfg = { online_timeout_s: 45 };
        document.getElementById('set-online-timeout').value = '999999';   // rejected value left behind
        const before = state.gets;

        overlay.classList.add('open');
        // MutationObserver callbacks are microtask-scheduled.
        await new Promise((r) => setTimeout(r, 20));

        expect(state.gets).toBeGreaterThan(before);
        expect(document.getElementById('set-online-timeout').value).toBe('45');
    });

    it('a refresh never overwrites controls while a save is in flight', async () => {
        // Park the POST so the save stays in flight across the refresh attempt.
        let release;
        const pending = new Promise((r) => { release = r; });
        vi.stubGlobal('fetch', vi.fn((url, opts = {}) => {
            if ((opts.method || 'GET') === 'POST') {
                return pending.then(() => ({
                    ok: true, status: 200, json: async () => ({ ok: true, dropped: [] }),
                }));
            }
            state.gets += 1;
            return Promise.resolve({ ok: true, status: 200, json: async () => ({ online_timeout_s: 7 }) });
        }));
        const saving = settings.saveSettings();
        const before = state.gets;
        await settings.refreshSettings();
        expect(state.gets, 'refresh must not GET while a save is in flight').toBe(before);
        release();
        await saving;
    });

    // 2026-07-29 audit 1.3 — the guard must hold AFTER the await too: a
    // keystroke landing while the GET was on the wire arms the debounce, and a
    // stale response applied over it would revert the typed value, which the
    // pending debounce would then PERSIST.
    it('an edit landing during the refresh GET wins over the stale response', async () => {
        let releaseGet;
        const parkedGet = new Promise((r) => { releaseGet = r; });
        vi.stubGlobal('fetch', vi.fn((url, opts = {}) => {
            if ((opts.method || 'GET') === 'POST') {
                return Promise.resolve({
                    ok: true, status: 200, json: async () => ({ ok: true, dropped: [] }),
                });
            }
            // The GET resolves only when the test says so — after the edit.
            return parkedGet.then(() => ({
                ok: true, status: 200, json: async () => ({ online_timeout_s: 45 }),
            }));
        }));

        const el = document.getElementById('set-online-timeout');
        el.value = '60';
        const refreshing = settings.refreshSettings();

        // The user types while the GET is in flight: the input listener arms
        // the 400 ms save debounce (this is what sets _saveTimer).
        el.value = '77';
        el.dispatchEvent(new Event('input', { bubbles: true }));

        releaseGet();
        await refreshing;
        expect(el.value, 'stale GET must not clobber the in-flight edit').toBe('77');

        // Let the armed 400 ms debounce fire while fetch is still stubbed, so
        // its save cannot leak into the next test's teardown as noise.
        await new Promise((r) => setTimeout(r, 450));
    });

    // 2026-07-29 audit 8 (WCAG 3.3.1) — aria-invalid alone says "wrong", not
    // WHY: the reason must be programmatically associated with the control so
    // a screen-reader user tabbing back hears it, and the association must
    // never clobber (or be clobbered by) the ordinary control-hint id.
    it('the error note is in the control\'s aria-describedby, additively', async () => {
        const el = document.getElementById('set-online-timeout');
        el.setAttribute('aria-describedby', 'control-hint-42');   // wireControlHints' work

        state.dropped = ['online_timeout_s'];
        await settings.saveSettings();

        const ids = (el.getAttribute('aria-describedby') || '').split(/\s+/);
        expect(ids).toContain('control-hint-42');
        expect(ids).toContain('settings-field-error-online_timeout_s');
        const note = document.getElementById('settings-field-error-online_timeout_s');
        expect(note).toBeTruthy();
        expect(note.textContent).toMatch(/not saved/i);

        // A clean save strips OUR id and leaves the hint's untouched.
        state.dropped = [];
        await settings.saveSettings();
        expect(el.getAttribute('aria-describedby')).toBe('control-hint-42');
        expect(document.getElementById('settings-field-error-online_timeout_s')).toBe(null);
    });
});
