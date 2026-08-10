// Axis ii — declarative help table + install wiring (help.js).
//
// RATCHET (spirit of moduleHierarchy.test.js): every static vault-write action
// button in WRITE_ACTION_IDS must carry a writes:true popover help entry — a
// new write button without help fails the suite. Plus table integrity and the
// install-onto-DOM behaviour.
import { describe, it, expect, vi, beforeEach } from 'vitest';

describe('HELP_ENTRIES table integrity', () => {
    let help;
    beforeEach(async () => {
        vi.resetModules();
        help = await import('../../static/js/help.js');
    });

    it('ids are unique and kinds are well-formed', () => {
        const ids = help.HELP_ENTRIES.map(([id]) => id);
        expect(new Set(ids).size).toBe(ids.length);
        for (const [id, kind, payload] of help.HELP_ENTRIES) {
            expect(typeof id).toBe('string');
            expect(['tip', 'popover']).toContain(kind);
            if (kind === 'tip') {
                expect(typeof payload.text).toBe('string');
                expect(payload.text.length).toBeGreaterThan(0);
            } else {
                expect(typeof payload.title).toBe('string');
                expect(typeof payload.body).toBe('string');
                expect(payload.title.length).toBeGreaterThan(0);
                expect(payload.body.length).toBeGreaterThan(0);
            }
        }
    });

    // RATCHET (2026-07-26 UX audit): a tip is pointer-events:none, so the
    // pointer can never enter it — WCAG 1.4.13 "Hoverable" is unmeetable for
    // one long enough to need reading time or scrolling, and its text can't be
    // selected/copied. `set-reranker-model` had grown to 364 chars (2.2x the
    // next longest, ~11 lines at 260px) carrying a model id the user must copy;
    // it is now a popover. This cap keeps the next long entry honest: over the
    // bound, promote it to a popover rather than widening the tip.
    it('RATCHET: no tip is long enough to need scrolling or copying (promote to popover)', () => {
        const MAX_TIP_CHARS = 220;
        for (const [id, kind, payload] of help.HELP_ENTRIES) {
            if (kind !== 'tip') continue;
            expect(
                payload.text.length,
                `${id}: ${payload.text.length} chars — too long for a hover tooltip, make it a popover`,
            ).toBeLessThanOrEqual(MAX_TIP_CHARS);
        }
    });

    it('the reranker-model help is a label-anchored popover (copyable + hoverable)', () => {
        const entry = help.HELP_ENTRIES.find(([id]) => id === 'set-reranker-model');
        expect(entry).toBeTruthy();
        const [, kind, payload] = entry;
        expect(kind).toBe('popover');
        // Anchored on the LABEL so promoting it did not move the badge from
        // beside the field name to after the input box.
        expect(payload.anchor).toBe('label');
        // The operative instruction — the reason the old always-visible hint
        // was actively wrong — must survive any future rewording.
        expect(payload.body).toMatch(/RELAUNCH|restart/i);
    });

    it('RATCHET: every vault-write action button has a writes:true popover entry', () => {
        for (const id of help.WRITE_ACTION_IDS) {
            const entry = help.HELP_ENTRIES.find(([eid]) => eid === id);
            expect(entry, `missing help entry for write action ${id}`).toBeTruthy();
            const [, kind, payload] = entry;
            expect(kind, `${id} help must be a popover`).toBe('popover');
            expect(payload.writes, `${id} popover must set writes:true`).toBe(true);
        }
    });
});

describe('installHelp() wiring', () => {
    let help;
    beforeEach(async () => {
        // A DOM carrying a representative subset of the real control/button ids.
        document.body.innerHTML = `
          <div class="control-group">
            <label for="deck-max-sections">Max sections</label>
            <input id="deck-max-sections" />
          </div>
          <div class="control-group">
            <label for="refactor-scope">Scope</label>
            <input id="refactor-scope" />
          </div>
          <div class="control-group">
            <label for="set-reranker-model">Reranker model</label>
            <input id="set-reranker-model" />
          </div>
          <button id="refactor-apply-btn">Apply</button>
          <button id="refactor-normalize-btn">Fix formatting</button>
        `;
        vi.resetModules();
        help = await import('../../static/js/help.js');
    });

    it('attaches a tip badge onto the setting label and a popover badge onto the action', () => {
        const n = help.installHelp();
        expect(n).toBeGreaterThanOrEqual(4);
        // Tip badge sits right after the label.
        const lbl = document.querySelector('label[for="deck-max-sections"]');
        expect(lbl.nextElementSibling.classList.contains('help-badge')).toBe(true);
        // Action badge sits after the button and opens a dialog.
        const applyBadge = document.getElementById('refactor-apply-btn').nextElementSibling;
        expect(applyBadge.classList.contains('help-badge')).toBe(true);
        expect(applyBadge.getAttribute('aria-haspopup')).toBe('dialog');
    });

    // Promoting a SETTING from tip to popover must not relocate its badge from
    // beside the field name to after the input box (2026-07-26 UX audit).
    it('a label-anchored popover keeps its badge next to the label, not the input', () => {
        help.installHelp();
        const lbl = document.querySelector('label[for="set-reranker-model"]');
        const badge = lbl.nextElementSibling;
        expect(badge.classList.contains('help-badge')).toBe(true);
        expect(badge.getAttribute('aria-haspopup')).toBe('dialog');
        // Clicking opens a real dialog whose text is selectable/copyable — the
        // thing a pointer-events:none tip could never offer.
        badge.click();
        const pop = document.querySelector('.help-popover');
        expect(pop).toBeTruthy();
        expect(pop.getAttribute('role')).toBe('dialog');
        expect(pop.textContent).toContain('bge-reranker-v2-m3');
    });

    it('skips ids absent from the DOM and is idempotent', () => {
        const first = help.installHelp();
        const second = help.installHelp(); // badges already present → nothing new
        expect(second).toBe(0);
        expect(first).toBeGreaterThan(0);
        // Only one badge per anchor (the apply button's immediate next sibling).
        const applyNext = document.getElementById('refactor-apply-btn').nextElementSibling;
        expect(applyNext.classList.contains('help-badge')).toBe(true);
        expect(applyNext.nextElementSibling && applyNext.nextElementSibling.classList.contains('help-badge')).toBeFalsy();
    });
});
