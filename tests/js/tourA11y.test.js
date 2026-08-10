// A11y fixes (2026-07-23): the guided tour is a NON-modal dialog — no
// aria-modal claim (the overlay is pointer-events:none with no focus trap),
// focus moves to the labelled CARD per step so SR users hear the content,
// Escape works at document level even after focus wanders, focus returns to
// the pre-tour element on close, and the optional per-step onEnter hook fires
// (ui.js stays feature-blind: it just calls it).
import { describe, it, expect, vi } from 'vitest';
import { runTour } from '../../static/js/ui.js';

function docEscape() {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
}

describe('guided tour a11y', () => {
    it('declares a non-modal dialog and focuses the labelled card', () => {
        document.body.innerHTML = '<button id="launch"></button><div id="t1"></div>';
        document.getElementById('launch').focus();
        runTour([{ target: 't1', title: 'One', text: 'first' }]);
        const overlay = document.querySelector('.tour-overlay');
        expect(overlay.getAttribute('role')).toBe('dialog');
        // pointer-events:none + no focus trap → claiming aria-modal would tell
        // AT the background is inert when it isn't.
        expect(overlay.hasAttribute('aria-modal')).toBe(false);
        const card = document.querySelector('.tour-card');
        expect(document.activeElement).toBe(card);
        // The card (the focus target) is labelled by the step title/text.
        const title = document.getElementById(card.getAttribute('aria-labelledby'));
        const text = document.getElementById(card.getAttribute('aria-describedby'));
        expect(title.textContent).toContain('One');
        expect(text.textContent).toBe('first');
        docEscape();
    });

    it('Escape at document level closes even after focus wanders, and restores focus', () => {
        document.body.innerHTML = '<button id="launch"></button><button id="elsewhere"></button>';
        const launch = document.getElementById('launch');
        launch.focus();
        runTour([{ title: 'Only', text: 'step' }]);
        // Focus wanders out of the (pointer-events:none, non-modal) overlay…
        document.getElementById('elsewhere').focus();
        docEscape();
        expect(document.querySelector('.tour-overlay')).toBeNull();
        // …and close hands focus back to the element focused at tour START.
        expect(document.activeElement).toBe(launch);
    });

    it('calls the optional per-step onEnter hook, and a throwing hook cannot kill the tour', () => {
        document.body.innerHTML = '';
        const entered = vi.fn();
        runTour([
            { title: 'A', text: 'a', onEnter: entered },
            { title: 'B', text: 'b', onEnter: () => { throw new Error('boom'); } },
        ]);
        expect(entered).toHaveBeenCalledTimes(1);
        const next = [...document.querySelectorAll('.tour-actions button')].pop();
        next.click();   // enters step 2, whose hook throws
        expect(document.querySelector('.tour-card').textContent).toContain('2/2 — B');
        docEscape();
    });
});
