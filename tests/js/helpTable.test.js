// Axis ii — declarative help table + install wiring (help.js).
//
// RATCHET (spirit of moduleHierarchy.test.js): every static vault-write action
// button in WRITE_ACTION_IDS must carry a writes:true popover help entry — a
// new write button without help fails the suite. Plus table integrity and the
// install-onto-DOM behaviour.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));

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
            <label for="docgen-md-subfolder">Scope</label>
            <input id="docgen-md-subfolder" />
          </div>
          <div class="control-group">
            <label for="set-reranker-model">Reranker model</label>
            <input id="set-reranker-model" />
          </div>
          <button id="docgen-md-generate-btn">Apply</button>
          <button id="unused-button">Fix formatting</button>
        `;
        vi.resetModules();
        help = await import('../../static/js/help.js');
    });

    it('attaches a tip badge onto the setting label and a popover badge onto the action', () => {
        const n = help.installHelp();
        expect(n).toBeGreaterThanOrEqual(3);
        // Tip badge sits right after the label.
        const lbl = document.querySelector('label[for="deck-max-sections"]');
        expect(lbl.nextElementSibling.classList.contains('help-badge')).toBe(true);
        // Action badge sits after the button and opens a dialog.
        const applyBadge = document.getElementById('docgen-md-generate-btn').nextElementSibling;
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
        const applyNext = document.getElementById('docgen-md-generate-btn').nextElementSibling;
        expect(applyNext.classList.contains('help-badge')).toBe(true);
        expect(applyNext.nextElementSibling && applyNext.nextElementSibling.classList.contains('help-badge')).toBeFalsy();
    });
});

// 2026-09-20 UX audit: three badges were visibly misplaced — a tab's "?" on
// its own line under the tab, an action's "?" floating between two buttons,
// and a label-wrapped checkbox's "?" between the box and its caption. Each
// case below is pinned on the DOM the panels actually use.
describe('installHelp() badge placement', () => {
    let help;
    beforeEach(async () => {
        document.body.innerHTML = `
          <div class="actions">
            <button id="maint-run-btn">Run selected</button>
            <button id="maint-stop-btn">Stop</button>
          </div>
          <div class="audit-tab-group">
            <button class="audit-report-tab" id="audit-rtab-note_sync">Zotero Notes Sync</button>
            <button class="audit-report-tab" id="audit-rtab-orphan_notes">Notes Without a Record</button>
          </div>
          <div class="control-group">
            <label class="checkbox-row"><input type="checkbox" id="vault-agent-enabled"> Strip the preamble</label>
          </div>
        `;
        vi.resetModules();
        help = await import('../../static/js/help.js');
        help.installHelp();
    });

    it('an action button and its badge travel as one unit, so the badge cannot read as the next button\'s', () => {
        const run = document.getElementById('maint-run-btn');
        const wrap = run.parentElement;
        expect(wrap.classList.contains('help-anchor')).toBe(true);
        expect(run.nextElementSibling.classList.contains('help-badge')).toBe(true);
        // Stop is outside the unit — it is not what the badge describes.
        expect(wrap.nextElementSibling).toBe(document.getElementById('maint-stop-btn'));
        expect(wrap.parentElement.classList.contains('actions')).toBe(true);
    });

    it('a report tab keeps its badge on its own line inside the column group', () => {
        const tab = document.getElementById('audit-rtab-note_sync');
        expect(tab.parentElement.classList.contains('help-anchor')).toBe(true);
        expect(tab.nextElementSibling.classList.contains('help-badge')).toBe(true);
        // The group still holds the tabs in order; the neighbour is untouched.
        const group = document.querySelector('.audit-tab-group');
        expect(group.children[0]).toBe(tab.parentElement);
        expect(group.children[1]).toBe(document.getElementById('audit-rtab-orphan_notes'));
        expect(group.querySelectorAll('.audit-report-tab').length).toBe(2);
    });

    it('a label-wrapped checkbox gets its badge after the caption, never between the box and its text', () => {
        const box = document.getElementById('vault-agent-enabled');
        expect(box.nextElementSibling).toBeNull();
        const lbl = box.closest('label');
        expect(lbl.nextElementSibling.classList.contains('help-badge')).toBe(true);
    });
});

// 2026-09-20 badge polish. Two small placement/duplication defects the audit
// found, each pinned on the DOM the panel actually uses.
describe('installHelp() badge polish', () => {
    let help;
    beforeEach(async () => {
        vi.resetModules();
        help = await import('../../static/js/help.js');
    });

    it('a label-less control leads its row instead of splitting it', () => {
        document.body.innerHTML = `
          <div class="input-bar">
            <input type="file" id="plainchat-file" class="sr-only">
            <button type="button" id="plainchat-attach-btn">Attach</button>
            <input type="text" id="plainchat-input" aria-label="Chat message">
            <button id="plainchat-send-btn">Send</button>
          </div>`;
        help.installHelp();
        const row = document.querySelector('.input-bar');
        expect(row.firstElementChild.classList.contains('help-badge')).toBe(true);
        // …and crucially it is no longer wedged between the field and Send,
        // where it read as help for Send.
        const input = document.getElementById('plainchat-input');
        expect(input.nextElementSibling).toBe(document.getElementById('plainchat-send-btn'));
    });

    // Falling back to the element id made a screen reader say
    // "Help: plainchat-input" — the internal name of the control.
    it('names a label-less control from the entry, never from its id', () => {
        document.body.innerHTML = '<div class="input-bar"><input type="text" id="plainchat-input"></div>';
        help.installHelp();
        const badge = document.querySelector('.help-badge');
        expect(badge.getAttribute('aria-label')).toBe('Help: Plain Chat');
    });

    // A native tooltip and a badge competing for the same hover, saying the
    // same thing, is worse than either alone.
    it('no report tab carries both a help badge and a rival title attribute', () => {
        const html = readFileSync(join(HERE, '../../templates/index.html'), 'utf8');
        const tabs = [...html.matchAll(/<button class="audit-report-tab"[^>]*>/g)].map((m) => m[0]);
        expect(tabs.length).toBeGreaterThan(15);
        const badged = tabs.filter((t) => {
            const id = (t.match(/ id="([^"]+)"/) || [])[1];
            return id && help.HELP_ENTRIES.some(([eid]) => eid === id);
        });
        // The two the 2026-09-20 pass converted; if a third is badged later it
        // is covered for free.
        expect(badged.length).toBeGreaterThanOrEqual(2);
        for (const t of badged) {
            expect(t, `${t.match(/ id="([^"]+)"/)[1]} has a title beside its help badge`)
                .not.toMatch(/\stitle="/);
        }
    });
});
