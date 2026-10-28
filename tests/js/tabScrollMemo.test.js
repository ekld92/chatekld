// 2026-09-02 — leaving a tab walked every descendant of the outgoing panel.
//
// DEFECT PINNED: ui.js::showTab ran prev.querySelectorAll('*') and read
// scrollTop on every node just to find the handful that had scrolled. With
// the audit inventory rendered that is tens of thousands of nodes and layout
// reads on every tab switch.
//
// The invariant this must not break: returning to a tab restores the scroll
// position of the elements the user had scrolled — the whole reason the memo
// exists (WebKit resets scrollTop under display:none).
import { describe, it, expect, beforeEach, vi } from 'vitest';

async function loadUI() {
    vi.resetModules();
    return import('../../static/js/ui.js');
}

function buildTabs(rowCount) {
    document.body.innerHTML = `
      <div class="tab" id="tab-audit"></div>
      <div class="tab" id="tab-chat"></div>
      <div class="content-area active" id="audit-tab">
        <div id="scroller" class="audit-table-scroll"></div>
      </div>
      <div class="content-area" id="chat-tab"></div>`;
    const scroller = document.getElementById('scroller');
    scroller.innerHTML = Array.from({ length: rowCount },
        (_, i) => `<div class="row"><span>cell ${i}</span></div>`).join('');
    return scroller;
}

describe('tab scroll memo', () => {
    beforeEach(() => {
        window.requestAnimationFrame = (fn) => { fn(); return 1; };
    });

    it('does not walk every descendant of the outgoing panel', async () => {
        const UI = await loadUI();
        buildTabs(200);
        const panel = document.getElementById('audit-tab');
        const seen = [];
        const real = panel.querySelectorAll.bind(panel);
        panel.querySelectorAll = (sel) => { seen.push(sel); return real(sel); };

        UI.showTab('chat');

        expect(seen).not.toContain('*');
    });

    it('still restores the scroll position of a scrolled element', async () => {
        const UI = await loadUI();
        const scroller = buildTabs(20);
        // jsdom has no layout, so scrollTop is a plain property: set it and
        // announce it the way a real scroll would.
        scroller.scrollTop = 340;
        scroller.dispatchEvent(new Event('scroll', { bubbles: false }));

        UI.showTab('chat');
        scroller.scrollTop = 0;          // WebKit's display:none reset
        UI.showTab('audit');

        expect(scroller.scrollTop).toBe(340);
    });

    it('ignores an element that was removed while the tab was hidden', async () => {
        const UI = await loadUI();
        const scroller = buildTabs(5);
        scroller.scrollTop = 120;
        scroller.dispatchEvent(new Event('scroll'));
        UI.showTab('chat');
        scroller.remove();
        expect(() => UI.showTab('audit')).not.toThrow();
    });
});
