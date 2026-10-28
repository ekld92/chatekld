// G4 (2026-07-22 audit): the LLM Settings modal's live filter — the light fix
// for the 84-control monolith. Pins: matching rows stay visible, non-matching
// sections hide, matching sections auto-open, and clearing restores each
// section's pre-filter open state.
import { readFileSync } from 'node:fs';

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

function modalMarkup() {
    return `
      <div id="sr-status-announcer" role="status"></div>
      <div id="settings-modal">
        <div class="settings-filter-row">
          <input type="search" id="settings-filter">
          <span id="settings-filter-count"></span>
        </div>
        <p id="settings-filter-empty" hidden>No setting here matches. …
           <strong>Library Audit &rarr; Settings</strong>…</p>
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
        <!-- Mirrors #maintenance-settings-section: a capability-gated section
             that ships hidden (private-only subsystem). The filter must
             neither reveal nor count it until its owner module drops the
             data-gated marker. -->
        <details class="settings-section" id="gated-section" hidden data-gated="1">
          <summary>Vault maintenance</summary>
          <div class="control-grid">
            <div class="control-group"><label>Max drift timeout rows</label></div>
          </div>
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
        const sections = document.querySelectorAll('.settings-section:not([data-gated])');
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

    // RATCHET (2026-08-21, Maintenance Phase 1): a data-gated section (the
    // private-only Maintenance block, hidden on the public build) must never
    // be revealed OR counted by the filter — before this rule, the clear-path
    // `d.hidden = false` sweep unhid it after any filter use, exposing a
    // dead settings section on the public mirror.
    it('a data-gated hidden section survives filter + clear unrevealed and uncounted', () => {
        const gated = document.getElementById('gated-section');
        type('timeout');   // matches the gated row's text too — must not count it
        expect(gated.hidden, 'filter must not reveal a gated section').toBe(true);
        expect(document.getElementById('settings-filter-count').textContent)
            .toContain('2'); // Global + Vision only — the gated row is not counted
        type('');
        expect(gated.hidden, 'filter clear must not reveal a gated section').toBe(true);
        // Once the owner module drops the marker (probe succeeded), it
        // participates like any other section.
        gated.hidden = false;
        delete gated.dataset.gated;
        type('drift');
        expect(gated.hidden).toBe(false);
        expect(gated.open).toBe(true);
    });

    /* 2026-09-03 UI review. A query matching nothing emptied the modal and left
     * "0 match(es)" — a dead end on the one surface a user searches when they
     * cannot find a setting. The app has THREE settings surfaces (this modal,
     * the Library Audit's own Settings modal for the `audit_*` paths and
     * thresholds, and per-panel toggles), and the recorded complaint is exactly
     * that "where does the near-duplicate cutoff live?" has no single answer.
     * A zero-result query is where that answer belongs. */
    it('a query matching nothing points at the other settings surfaces', () => {
        type('zzzznothingmatchesthis');
        const hint = document.getElementById('settings-filter-empty');
        expect(hint).not.toBeNull();
        expect(hint.hidden).toBe(false);
        expect(hint.textContent).toMatch(/Library Audit/);
        expect(document.getElementById('settings-filter-count').textContent)
            .toContain('0');
    });

    it('the hint is hidden again as soon as something matches, and when cleared', () => {
        type('zzzznothingmatchesthis');
        expect(document.getElementById('settings-filter-empty').hidden).toBe(false);
        type('timeout');
        expect(document.getElementById('settings-filter-empty').hidden).toBe(true);
        type('');
        expect(document.getElementById('settings-filter-empty').hidden).toBe(true);
    });

    /* Source ratchet: the hint element is STATIC markup, per the liveRegions
     * rule that anything a user may need to hear exists at markup time rather
     * than being minted by JS. A test fixture that declares it while
     * index.html does not would pass while the shipped modal kept its dead
     * end. */
    it('index.html ships the hint element, not just the fixture', () => {
        const html = readFileSync('templates/index.html', 'utf8');
        expect(html).toContain('id="settings-filter-empty"');
        expect(html).toMatch(/settings-filter-empty[^>]*hidden/);
        expect(html).toContain('Library Audit');
    });
});
