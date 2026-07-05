// Pinning tests for the per-overlay modal machinery (improvement plan
// 2026-07-04, item 3.7). The defect: one global trigger/keyHandler/
// overlayClick slot — opening a second modal overwrote the first's handlers,
// and ANY closeModal call (refactor.js's delayed ~1.1 s timers) stripped
// whatever modal was live, killing its Esc/Tab handling and focus restore.
import { describe, it, expect, beforeEach } from 'vitest';
import { openModal, closeModal } from '../../static/js/ui.js';

function mountModals() {
    document.body.innerHTML = `
      <div id="main-content"><button id="opener-a">A</button><button id="opener-b">B</button></div>
      <div id="app-sidebar"></div>
      <div id="modal-a"><div class="modal"><button id="a-btn">ok</button></div></div>
      <div id="modal-b"><div class="modal"><button id="b-btn">ok</button></div></div>
    `;
}

function pressEscape() {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
}

describe('modal machinery (Track 3.7)', () => {
    beforeEach(mountModals);

    it('a delayed close of one modal leaves the live modal fully armed', () => {
        openModal('modal-a');
        openModal('modal-b');
        // The refactor-style delayed timer fires for A while B is live.
        closeModal('modal-a');
        expect(document.getElementById('modal-b').classList.contains('open')).toBe(true);
        // B's Esc handling survived (pre-fix, A's close removed B's handler).
        pressEscape();
        expect(document.getElementById('modal-b').classList.contains('open')).toBe(false);
    });

    it('closeModal on a not-open overlay is a strict no-op', () => {
        openModal('modal-b');
        closeModal('modal-a');            // never opened
        closeModal('modal-a');            // twice, for good measure
        expect(document.getElementById('modal-b').classList.contains('open')).toBe(true);
        // Background stays inert while B is open.
        expect(document.getElementById('main-content').inert).toBe(true);
        pressEscape();
        expect(document.getElementById('modal-b').classList.contains('open')).toBe(false);
    });

    it('Escape peels only the TOPMOST of stacked modals', () => {
        openModal('modal-a');
        openModal('modal-b');
        pressEscape();
        expect(document.getElementById('modal-b').classList.contains('open')).toBe(false);
        expect(document.getElementById('modal-a').classList.contains('open')).toBe(true);
        pressEscape();
        expect(document.getElementById('modal-a').classList.contains('open')).toBe(false);
    });

    it('background inert clears only when the LAST modal closes; focus restores to the trigger', () => {
        const openerA = document.getElementById('opener-a');
        openerA.focus();
        openModal('modal-a');
        openModal('modal-b');
        expect(document.getElementById('main-content').inert).toBe(true);
        closeModal('modal-b');
        expect(document.getElementById('main-content').inert).toBe(true);   // A still open
        closeModal('modal-a');
        expect(document.getElementById('main-content').inert).toBe(false);
        expect(document.activeElement).toBe(openerA);                       // trigger restored
    });

    it('closing a lower modal does not steal focus from the top one', () => {
        openModal('modal-a');
        openModal('modal-b');
        document.getElementById('b-btn').focus();
        closeModal('modal-a');            // lower layer closes (delayed timer)
        expect(document.activeElement).toBe(document.getElementById('b-btn'));
    });
});
