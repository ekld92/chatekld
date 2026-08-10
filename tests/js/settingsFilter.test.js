// G4 (2026-07-22 audit): the LLM Settings modal's live filter — the light fix
// for the 84-control monolith. Pins: matching rows stay visible, non-matching
// sections hide, matching sections auto-open, and clearing restores each
// section's pre-filter open state.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

function modalMarkup() {
    return `
      <div id="sr-status-announcer" role="status"></div>
      <div id="settings-modal">
        <div class="settings-filter-row">
          <input type="search" id="settings-filter">
          <span id="settings-filter-count"></span>
        </div>
        <details class="settings-section" open>
          <summary>Global</summary>
          <div class="control-grid">
            <div class="control-group"><label>Request timeout (seconds)</label></div>
            <div class="control-group"><label>Max retries</label></div>
          </div>
        </details>
        <details class="settings-section">
          <summary>Vision</summary>
          <div class="control-grid">
            <div class="control-group"><label>Vision timeout</label></div>
          </div>
        </details>
        <details class="settings-section">
          <summary>Chat</summary>
          <div class="control-grid">
            <div class="control-group"><label>Plain chat temperature</label></div>
          </div>
        </details>
        <details class="settings-section">
          <summary>Vault</summary>
          <div class="control-grid">
            <div class="control-group"><label>Top K</label></div>
          </div>
          <!-- The real modal nests 17 of its 85 rows one level deeper, in a
               closed <details class="advanced-controls"> — including
               set-reranker-model, the documented discovery path. -->
          <details class="advanced-controls">
            <summary>Vault Chat · fine-tuning</summary>
            <div class="control-group">
              <label for="set-reranker-model">Reranker model</label>
              <input id="set-reranker-model">
            </div>
          </details>
        </details>
      </div>`;
}

describe('settings filter (2026-07-22 G4)', () => {
    let input;
    beforeEach(async () => {
        document.body.innerHTML = modalMarkup();
        vi.resetModules();
        vi.useFakeTimers();
        vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, json: async () => ({}) })));
        const settings = await import('../../static/js/settings.js');
        settings.initSettings({});
        input = document.getElementById('settings-filter');
    });
    afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

    function type(q) {
        input.value = q;
        input.dispatchEvent(new Event('input', { bubbles: true }));
        vi.advanceTimersByTime(200);
    }

    it('filters rows, hides empty sections, opens matching ones', () => {
        type('timeout');
        const sections = document.querySelectorAll('.settings-section');
        // Global (Request timeout) and Vision (Vision timeout) match; Chat hides.
        expect(sections[0].hidden).toBe(false);
        expect(sections[1].hidden).toBe(false);
        expect(sections[1].open).toBe(true);      // auto-opened
        expect(sections[2].hidden).toBe(true);
        // Non-matching row inside a matching section hides too.
        const globalRows = sections[0].querySelectorAll('.control-group');
        expect(globalRows[0].hidden).toBe(false);  // Request timeout
        expect(globalRows[1].hidden).toBe(true);   // Max retries
        expect(document.getElementById('settings-filter-count').textContent)
            .toContain('2');
    });

    it('clearing restores rows and each section pre-filter open state', () => {
        type('timeout');
        type('');
        const sections = document.querySelectorAll('.settings-section');
        sections.forEach((sec) => expect(sec.hidden).toBe(false));
        expect(sections[0].open).toBe(true);       // was open before filtering
        expect(sections[1].open).toBe(false);      // restored to closed
        document.querySelectorAll('.control-group')
            .forEach((row) => expect(row.hidden).toBe(false));
    });

    // RATCHET (2026-07-26 UX audit): the filter used to COUNT rows inside a
    // nested <details> (and announce the count to AT) while opening only the
    // top-level section — so searching "reranker" said "1 matching setting(s)"
    // and revealed nothing. Count and visibility must describe the same set.
    it('reveals a match nested inside an inner <details>, not just its section', () => {
        type('reranker');
        const inner = document.querySelector('.advanced-controls');
        const section = inner.closest('.settings-section');
        expect(section.hidden).toBe(false);
        expect(section.open).toBe(true);
        expect(inner.hidden).toBe(false);
        expect(inner.open, 'nested details must be opened, not just counted').toBe(true);
        const row = document.getElementById('set-reranker-model').closest('.control-group');
        expect(row.hidden).toBe(false);
        expect(document.getElementById('settings-filter-count').textContent).toContain('1');
    });

    it('hides a nested <details> whose rows all filtered out, and restores it on clear', () => {
        const inner = document.querySelector('.advanced-controls');
        type('timeout');
        expect(inner.hidden, 'an open-but-empty nested block reads as a broken filter').toBe(true);
        type('');
        expect(inner.hidden).toBe(false);
        expect(inner.open, 'nested open state must be restored, not left forced open').toBe(false);
    });
});
