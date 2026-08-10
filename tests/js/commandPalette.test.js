// Batch H (2026-07-22): the ⌘K command palette — open/filter/run/escape —
// and the guided-tour primitive's open/close lifecycle.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { installCommandPalette, runTour } from '../../static/js/ui.js';

function pressCmdK() {
    document.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'k', metaKey: true, bubbles: true, cancelable: true,
    }));
}

describe('command palette (Batch H)', () => {
    // NOTE: the document-level ⌘K listener is wired ONCE per document (the
    // dataset guard); re-registering per test would stack listeners and make
    // one keypress open-then-toggle-close. installCommandPalette still swaps
    // the action registry on every call, which is what each test needs.
    beforeEach(() => {
        document.body.innerHTML = '';
        document.querySelectorAll('.cmdk-overlay').forEach((el) => el.remove());
    });

    it('⌘K opens, typing filters (accent-insensitive), Enter runs the action', () => {
        const ran = vi.fn();
        installCommandPalette([
            { label: 'Go to Résumé view', run: ran },
            { label: 'Open Settings', run: vi.fn() },
        ]);
        pressCmdK();
        const input = document.querySelector('.cmdk-input');
        expect(input).toBeTruthy();
        input.value = 'resume';
        input.dispatchEvent(new Event('input', { bubbles: true }));
        const items = document.querySelectorAll('.cmdk-item');
        expect(items).toHaveLength(1);
        expect(items[0].textContent).toContain('Résumé');
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
        expect(ran).toHaveBeenCalledTimes(1);
        expect(document.querySelector('.cmdk-overlay')).toBeNull();  // closed
    });

    it('Escape closes without running; a second ⌘K toggles', () => {
        const ran = vi.fn();
        installCommandPalette([{ label: 'Danger', run: ran }]);
        pressCmdK();
        const input = document.querySelector('.cmdk-input');
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
        expect(document.querySelector('.cmdk-overlay')).toBeNull();
        expect(ran).not.toHaveBeenCalled();
        pressCmdK();
        expect(document.querySelector('.cmdk-overlay')).toBeTruthy();
        pressCmdK();   // toggle closed
        expect(document.querySelector('.cmdk-overlay')).toBeNull();
    });

    it('no matching action renders the empty row, never throws', () => {
        installCommandPalette([{ label: 'Alpha', run: vi.fn() }]);
        pressCmdK();
        const input = document.querySelector('.cmdk-input');
        input.value = 'zzz';
        input.dispatchEvent(new Event('input', { bubbles: true }));
        expect(document.querySelector('.cmdk-empty')).toBeTruthy();
        // No options → the stale active-descendant id must be dropped too.
        expect(input.hasAttribute('aria-activedescendant')).toBe(false);
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
        expect(document.querySelector('.cmdk-overlay')).toBeTruthy();  // nothing ran, stays open
    });

    // A prior test can leave the palette open while beforeEach removed its
    // node — the module still thinks it is open, so the first ⌘K registers as
    // a toggle-close. Press again when that happens.
    function openPalette() {
        pressCmdK();
        if (!document.querySelector('.cmdk-overlay')) pressCmdK();
    }

    // A11y fixes (2026-07-23): the palette completes the combobox pattern —
    // focus stays on the input while aria-activedescendant names the selection.
    it('combobox wiring: roles + aria-activedescendant tracks the selection', () => {
        installCommandPalette([
            { label: 'Alpha', run: vi.fn() },
            { label: 'Beta', run: vi.fn() },
        ]);
        openPalette();
        const input = document.querySelector('.cmdk-input');
        expect(input.getAttribute('role')).toBe('combobox');
        expect(input.getAttribute('aria-expanded')).toBe('true');
        expect(input.getAttribute('aria-autocomplete')).toBe('list');
        expect(input.getAttribute('aria-controls')).toBe('cmdk-listbox');
        expect(document.getElementById('cmdk-listbox').getAttribute('role')).toBe('listbox');
        expect(input.getAttribute('aria-activedescendant')).toBe('cmdk-opt-0');
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true }));
        expect(input.getAttribute('aria-activedescendant')).toBe('cmdk-opt-1');
        expect(document.getElementById('cmdk-opt-1').getAttribute('aria-selected')).toBe('true');
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
        expect(document.querySelector('.cmdk-overlay')).toBeNull();
    });

    it('Tab is trapped in the input and Escape still closes afterwards (document-level)', () => {
        installCommandPalette([{ label: 'Alpha', run: vi.fn() }]);
        openPalette();
        const input = document.querySelector('.cmdk-input');
        const tab = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true });
        input.dispatchEvent(tab);
        expect(tab.defaultPrevented).toBe(true);   // the input is the only focusable — keep focus inside
        // Even if focus DID wander, Escape is bound at document level while open.
        document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
        expect(document.querySelector('.cmdk-overlay')).toBeNull();
    });

    it('restores focus to the pre-open element on close', () => {
        const btn = document.createElement('button');
        document.body.appendChild(btn);
        btn.focus();
        installCommandPalette([{ label: 'Alpha', run: vi.fn() }]);
        openPalette();
        expect(document.activeElement.className).toBe('cmdk-input');
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
        expect(document.activeElement).toBe(btn);
    });
});

describe('guided tour primitive (Batch H)', () => {
    it('renders steps, navigates, and Escape/Done close and unhighlight', () => {
        document.body.innerHTML = '<div id="x-target"></div>';
        runTour([
            { target: 'x-target', title: 'One', text: 'first' },
            { title: 'Two', text: 'second' },
        ]);
        expect(document.querySelector('.tour-card').textContent).toContain('1/2 — One');
        expect(document.getElementById('x-target').classList.contains('tour-highlight')).toBe(true);
        // Next → step 2 (previous highlight cleared).
        const next = [...document.querySelectorAll('.tour-actions button')].pop();
        next.click();
        expect(document.querySelector('.tour-card').textContent).toContain('2/2 — Two');
        expect(document.getElementById('x-target').classList.contains('tour-highlight')).toBe(false);
        // Done closes.
        [...document.querySelectorAll('.tour-actions button')].pop().click();
        expect(document.querySelector('.tour-overlay')).toBeNull();
    });
});
