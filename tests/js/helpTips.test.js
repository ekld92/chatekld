// Axis ii — contextual help primitives (ui.js::helpTip / helpPopover).
//
// Pins: the "?" badge is a real, keyboard-focusable button with an accessible
// name; the tooltip is associated via aria-describedby and set with textContent
// (no HTML injection); the popover is a role="dialog", dismisses on Escape /
// outside-click / re-click (no stacking), and restores focus to its badge.
import { describe, it, expect, vi, beforeEach } from 'vitest';

describe('helpTip (hover/focus tooltip)', () => {
    let ui;
    beforeEach(async () => {
        document.body.innerHTML = '<div id="wrap"><label id="lbl">Max iterations</label></div>';
        vi.resetModules();
        ui = await import('../../static/js/ui.js');
    });

    it('injects a focusable button badge with an accessible name', () => {
        const badge = ui.helpTip(document.getElementById('lbl'), { text: 'hello', labelText: 'Max iterations' });
        expect(badge).toBeTruthy();
        expect(badge.tagName).toBe('BUTTON');
        expect(badge.type).toBe('button');
        expect(badge.getAttribute('aria-label')).toBe('Help: Max iterations');
        // Injected right after the anchor.
        expect(document.getElementById('lbl').nextElementSibling).toBe(badge);
    });

    it('is idempotent per anchor', () => {
        const lbl = document.getElementById('lbl');
        expect(ui.helpTip(lbl, { text: 'a' })).toBeTruthy();
        expect(ui.helpTip(lbl, { text: 'b' })).toBeNull();
        expect(document.querySelectorAll('.help-badge').length).toBe(1);
    });

    it('shows a role="tooltip" referenced by aria-describedby on focus, hides on Escape/blur', () => {
        const badge = ui.helpTip(document.getElementById('lbl'), { text: 'the hint' });
        badge.focus();
        const tip = document.querySelector('.help-tip');
        expect(tip).toBeTruthy();
        expect(tip.getAttribute('role')).toBe('tooltip');
        expect(tip.hidden).toBe(false);
        expect(badge.getAttribute('aria-describedby')).toBe(tip.id);
        expect(tip.textContent).toBe('the hint');

        badge.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        expect(tip.hidden).toBe(true);
        expect(badge.getAttribute('aria-describedby')).toBeNull();
    });

    it('sets tooltip text via textContent — no HTML injection', () => {
        const badge = ui.helpTip(document.getElementById('lbl'), { text: '<img src=x onerror=alert(1)>' });
        badge.focus();
        const tip = document.querySelector('.help-tip');
        expect(tip.querySelector('img')).toBeNull();
        expect(tip.textContent).toContain('<img');
    });
});

describe('helpPopover (click popover)', () => {
    let ui;
    beforeEach(async () => {
        document.body.innerHTML = '<div id="wrap"><button id="act">Apply</button></div>';
        vi.resetModules();
        ui = await import('../../static/js/ui.js');
    });

    it('toggles a role="dialog" labelled by the title, with a writes notice', () => {
        const badge = ui.helpPopover(document.getElementById('act'), {
            title: 'Apply approved', body: 'writes callouts', writes: true,
        });
        expect(badge.getAttribute('aria-haspopup')).toBe('dialog');
        expect(badge.getAttribute('aria-expanded')).toBe('false');

        badge.click();
        const pop = document.querySelector('.help-popover');
        expect(pop).toBeTruthy();
        expect(pop.getAttribute('role')).toBe('dialog');
        expect(pop.getAttribute('aria-label')).toBe('Apply approved');
        expect(pop.querySelector('.help-popover-writes')).toBeTruthy();
        expect(badge.getAttribute('aria-expanded')).toBe('true');

        badge.click(); // re-click closes
        expect(document.querySelector('.help-popover')).toBeNull();
        expect(badge.getAttribute('aria-expanded')).toBe('false');
    });

    it('omits the writes notice when writes is false', () => {
        const badge = ui.helpPopover(document.getElementById('act'), { title: 'Generate', body: 'emit-only' });
        badge.click();
        expect(document.querySelector('.help-popover-writes')).toBeNull();
    });

    it('Escape dismisses and restores focus to the badge', () => {
        const badge = ui.helpPopover(document.getElementById('act'), { title: 'X', body: 'y' });
        badge.click();
        expect(document.querySelector('.help-popover')).toBeTruthy();
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
        expect(document.querySelector('.help-popover')).toBeNull();
        expect(document.activeElement).toBe(badge);
    });

    it('outside mousedown dismisses; a second popover never stacks', () => {
        const b1 = ui.helpPopover(document.getElementById('act'), { title: 'A', body: 'a' });
        b1.click();
        document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
        expect(document.querySelector('.help-popover')).toBeNull();

        // Two badges, open in turn — only one popover ever exists.
        document.getElementById('wrap').insertAdjacentHTML('beforeend', '<button id="act2">Two</button>');
        const b2 = ui.helpPopover(document.getElementById('act2'), { title: 'B', body: 'b' });
        b1.click();
        b2.click();
        expect(document.querySelectorAll('.help-popover').length).toBe(1);
        expect(document.querySelector('.help-popover').getAttribute('aria-label')).toBe('B');
    });

    it('sets body text via textContent — no HTML injection', () => {
        const badge = ui.helpPopover(document.getElementById('act'), { title: 'T', body: '<b>x</b>' });
        badge.click();
        const body = document.querySelector('.help-popover-body');
        expect(body.querySelector('b')).toBeNull();
        expect(body.textContent).toBe('<b>x</b>');
    });

    it('tab-out (focusout to an outside element) dismisses; focusout to a non-focusable target does not', () => {
        // Audit #18: non-modal popover — focus leaving it must close it rather
        // than leave an open dialog floating behind the focus; but a null
        // relatedTarget (click-select inside the body) must NOT close it.
        document.getElementById('wrap').insertAdjacentHTML('beforeend', '<button id="after">After</button>');
        const badge = ui.helpPopover(document.getElementById('act'), { title: 'X', body: 'y' });
        badge.click();
        const pop = document.querySelector('.help-popover');
        expect(pop).toBeTruthy();

        // Focus moving to a non-focusable target (relatedTarget null) keeps it open.
        pop.dispatchEvent(new FocusEvent('focusout', { bubbles: true, relatedTarget: null }));
        expect(document.querySelector('.help-popover')).toBeTruthy();

        // Focus moving to an outside element closes it.
        pop.dispatchEvent(new FocusEvent('focusout', {
            bubbles: true, relatedTarget: document.getElementById('after'),
        }));
        expect(document.querySelector('.help-popover')).toBeNull();
    });
});

describe('helpTip Escape-on-hover (WCAG 1.4.13)', () => {
    let ui;
    beforeEach(async () => {
        document.body.innerHTML = '<div id="wrap"><label id="lbl">Knob</label></div>';
        vi.resetModules();
        ui = await import('../../static/js/ui.js');
    });

    it('Escape dismisses a HOVERED (unfocused) tooltip via the document listener', () => {
        // Audit #19: the badge's own keydown only fires when the badge is
        // focused; a hover-shown tip needs the document-level Escape.
        const badge = ui.helpTip(document.getElementById('lbl'), { text: 'hint' });
        badge.dispatchEvent(new MouseEvent('mouseenter'));
        const tip = document.querySelector('.help-tip');
        expect(tip.hidden).toBe(false);
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
        expect(tip.hidden).toBe(true);
        expect(badge.getAttribute('aria-describedby')).toBeNull();
    });
});
