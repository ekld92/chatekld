// Track 6e pinning — confirmation-ceremony parity for destructive writes.
//
// Defects pinned: the deck "Apply repaired version" / "Apply to the deck"
// buttons overwrote a user's .tex on a SINGLE click and refactor's
// "Revert all" rewrote every recorded vault change with no confirm — while
// each individual Note Refactor write was confirm-gated (same risk class).
// Also: the Reset modal auto-focused the destructive button (openModal
// focuses the first focusable element), so Enter-on-open reset app data.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');

describe('confirmInline primitive (Track 6e)', () => {
    let ui;
    beforeEach(async () => {
        document.body.innerHTML = '<div id="wrap"><button id="anchor">Do it</button></div>';
        vi.resetModules();
        ui = await import('../../static/js/ui.js');
    });

    it('renders the strip, focuses Cancel (never the destructive default)', () => {
        const anchor = document.getElementById('anchor');
        const strip = ui.confirmInline(anchor, {
            message: 'sure?', confirmLabel: 'Overwrite', onConfirm: () => {},
        });
        expect(strip).toBeTruthy();
        expect(document.querySelector('.inline-confirm-strip')).toBe(strip);
        const buttons = strip.querySelectorAll('button');
        expect(buttons[0].textContent).toBe('Cancel');       // safe default first
        expect(buttons[1].textContent).toBe('Overwrite');
        expect(document.activeElement).toBe(buttons[0]);
    });

    it('Cancel disarms without firing; Confirm fires exactly once and removes', () => {
        const anchor = document.getElementById('anchor');
        const onConfirm = vi.fn();
        let strip = ui.confirmInline(anchor, { message: 'm', confirmLabel: 'Go', onConfirm });
        strip.querySelectorAll('button')[0].click();          // Cancel
        expect(onConfirm).not.toHaveBeenCalled();
        expect(document.querySelector('.inline-confirm-strip')).toBeNull();
        expect(document.activeElement).toBe(anchor);          // focus returns

        strip = ui.confirmInline(anchor, { message: 'm', confirmLabel: 'Go', onConfirm });
        strip.querySelectorAll('button')[1].click();          // Confirm
        expect(onConfirm).toHaveBeenCalledTimes(1);
        expect(document.querySelector('.inline-confirm-strip')).toBeNull();
    });

    it('re-invoking replaces the open strip instead of stacking', () => {
        const anchor = document.getElementById('anchor');
        ui.confirmInline(anchor, { message: 'a', confirmLabel: 'A', onConfirm: () => {} });
        ui.confirmInline(anchor, { message: 'b', confirmLabel: 'B', onConfirm: () => {} });
        const strips = document.querySelectorAll('.inline-confirm-strip');
        expect(strips.length).toBe(1);
        expect(strips[0].textContent).toContain('b');
    });
});

describe('Revert all requires the ceremony (Track 6e)', () => {
    let fetchMock;
    beforeEach(() => {
        document.body.innerHTML = `
          <div id="sr-status-announcer" role="status"></div>
          <div id="sr-error-announcer" role="alert"></div>
          <div id="refactor-restore-modal"><div class="modal">
            <div id="refactor-restore-status" role="status"></div>
            <div id="refactor-restore-list"></div>
            <div><button id="refactor-restore-all">Revert all</button></div>
          </div></div>
        `;
        vi.resetModules();
        fetchMock = vi.fn();
        vi.stubGlobal('fetch', fetchMock);
    });
    afterEach(() => vi.unstubAllGlobals());

    it('first click arms only; the strip confirm actually reverts', async () => {
        fetchMock.mockResolvedValue({
            ok: true, status: 200,
            json: async () => ({ reverted: 2, ops: [] }),
        });
        const refactor = await import('../../static/js/refactor.js');

        refactor.revertAll();
        // No write yet — only the ceremony strip appeared.
        expect(fetchMock).not.toHaveBeenCalled();
        const strip = document.querySelector('.inline-confirm-strip');
        expect(strip).toBeTruthy();

        strip.querySelectorAll('button')[1].click();          // confirm
        await new Promise((r) => setTimeout(r, 20));
        const calls = fetchMock.mock.calls.filter(([u]) => String(u).includes('/api/refactor/restore'));
        expect(calls.length).toBe(1);
        expect(JSON.parse(calls[0][1].body)).toEqual({ all: true });
    });
});

describe('destructive-defaults markup + deck wiring ratchet (Track 6e)', () => {
    it('Reset modal lists Cancel BEFORE the destructive Reset button', () => {
        const html = readFileSync(resolve(root, 'templates/index.html'), 'utf-8');
        const modal = /<div id="reset-modal"[\s\S]*?<\/div>\s*<\/div>\s*<\/div>/.exec(html)[0];
        const cancelIdx = modal.indexOf("closeModal('reset-modal')");
        const resetIdx = modal.indexOf('resetAppData()');
        expect(cancelIdx).toBeGreaterThan(-1);
        expect(resetIdx).toBeGreaterThan(-1);
        // openModal focuses the FIRST focusable — it must be Cancel.
        expect(cancelIdx).toBeLessThan(resetIdx);
    });

    it('deck.js routes both file-overwriting applies through confirmInline', () => {
        // Source ratchet (same style as moduleHierarchy.test.js): the deck
        // apply handlers must never call the writers directly from a click.
        const src = readFileSync(resolve(root, 'static/js/deck.js'), 'utf-8');
        expect(src).toMatch(/confirmInline\(btn,[\s\S]{0,400}?_applyRepair\(deck, btn\)/);
        expect(src).toMatch(/confirmInline\(btn,[\s\S]{0,400}?_applyAugment\(aug, btn\)/);
        // No direct single-click binding of the writers remains.
        expect(src).not.toMatch(/addEventListener\('click',\s*\(\)\s*=>\s*_applyRepair/);
        expect(src).not.toMatch(/addEventListener\('click',\s*\(\)\s*=>\s*_applyAugment/);
    });
});
