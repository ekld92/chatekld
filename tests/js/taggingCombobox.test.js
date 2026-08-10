// 2026-07-22 audit Batch F pins: the tag type-ahead is a REAL ARIA combobox
// (F2), chip removal works from the keyboard (F1), and the deck's Space key
// is navigation-safe (F4). Pre-fix: the input carried no combobox semantics
// (a blind user got no signal suggestions opened), the suggestion "options"
// were Tab-reachable <button>s that did nothing on Enter, chip × buttons were
// mousedown-only (inert on Enter/Space), and Space silently DEFERRED papers
// when a keyboard user scrolled the deck.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

function okJson(body) {
    return { ok: true, status: 200, json: async () => body };
}

function makeLocalStorage(init = {}) {
    const store = { ...init };
    return {
        getItem: (k) => (k in store ? store[k] : null),
        setItem: (k, v) => { store[k] = String(v); },
        removeItem: (k) => { delete store[k]; },
        clear: () => { for (const k of Object.keys(store)) delete store[k]; },
    };
}

const SAMPLE = {
    rows: [{
        citation_key: 'smith2020', year: '2020', title: 'Paper',
        finder_tags: [], note: { present: true, tags: [] },
        zotero: { present: false, parent_tags: [] },
    }],
};
const VOCAB = { vocab: [
    { tag: 'depression', gloss: 'mood disorder' },
    { tag: 'dependance', gloss: 'addiction' },
    { tag: 'anxiety', gloss: '' },
] };

async function renderTable() {
    const body = document.getElementById('audit-report-body');
    const meta = document.getElementById('audit-report-meta');
    const { taggingRenderInto } = await import('../../static/js/tagging.js');
    await taggingRenderInto(body, meta, () => true);
    return body;
}

describe('tagging combobox + keyboard chips (2026-07-22 F1/F2)', () => {
    beforeEach(() => {
        document.body.innerHTML =
            '<div id="sr-status-announcer" role="status"></div>'
            + '<div id="audit-report-meta"></div><div id="audit-report-body"></div>';
        vi.resetModules();
        vi.stubGlobal('localStorage', makeLocalStorage({ tagging_view: 'table' }));
        vi.stubGlobal('fetch', vi.fn((url) => {
            const u = String(url);
            if (u.includes('/api/tagging/vocab')) return Promise.resolve(okJson(VOCAB));
            if (u.includes('/api/tagging/zotero-available')) return Promise.resolve(okJson({ available: false }));
            return Promise.resolve(okJson(SAMPLE));
        }));
    });
    afterEach(() => vi.unstubAllGlobals());

    it('the input is a combobox whose expanded state and activedescendant track the popup', async () => {
        const body = await renderTable();
        const input = body.querySelector('.tagging-tag-input');
        expect(input).toBeTruthy();
        expect(input.getAttribute('role')).toBe('combobox');
        expect(input.getAttribute('aria-expanded')).toBe('false');
        expect(input.getAttribute('aria-autocomplete')).toBe('list');
        const boxId = input.getAttribute('aria-controls');
        expect(boxId).toBeTruthy();

        input.value = 'dep';
        input.dispatchEvent(new Event('input', { bubbles: true }));
        expect(input.getAttribute('aria-expanded')).toBe('true');
        const box = document.getElementById(boxId);
        const options = box.querySelectorAll('[role="option"]');
        expect(options.length).toBe(2);   // depression + dependance
        // Options are NOT buttons (semantics clash + phantom tab stops).
        options.forEach((o) => expect(o.tagName).not.toBe('BUTTON'));
        // The highlight is exposed via activedescendant on the INPUT.
        expect(input.getAttribute('aria-activedescendant')).toBe(options[0].id);
        expect(options[0].getAttribute('aria-selected')).toBe('true');

        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
        expect(input.getAttribute('aria-activedescendant')).toBe(options[1].id);

        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        expect(input.getAttribute('aria-expanded')).toBe('false');
        expect(input.hasAttribute('aria-activedescendant')).toBe(false);
    });

    it('a MIDDLE chip is removable from the keyboard (click with detail 0)', async () => {
        const body = await renderTable();
        const input = body.querySelector('.tagging-tag-input');
        // Commit three chips by typing separators.
        input.value = 'a b c ';
        input.dispatchEvent(new Event('input', { bubbles: true }));
        expect(input._tags).toEqual(['a', 'b', 'c']);
        const xs = body.querySelectorAll('.tagging-entry-chip-x');
        expect(xs.length).toBe(3);
        // Keyboard activation of a <button> fires click with detail === 0.
        xs[1].dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 0 }));
        expect(input._tags).toEqual(['a', 'c']);
    });
});

describe('tagging deck Space key (2026-07-22 F4)', () => {
    beforeEach(() => {
        document.body.innerHTML =
            '<div id="sr-status-announcer" role="status"></div>'
            + '<div id="audit-report-meta"></div><div id="audit-report-body"></div>';
        vi.resetModules();
        vi.stubGlobal('localStorage', makeLocalStorage({ tagging_view: 'deck' }));
        vi.stubGlobal('fetch', vi.fn((url) => {
            const u = String(url);
            if (u.includes('/api/tagging/vocab')) return Promise.resolve(okJson(VOCAB));
            if (u.includes('/api/tagging/zotero-available')) return Promise.resolve(okJson({ available: false }));
            if (u.includes('/api/tagging/snippet')) return Promise.resolve(okJson({ snippet: '', chars: 0 }));
            return Promise.resolve(okJson(SAMPLE));
        }));
    });
    afterEach(() => vi.unstubAllGlobals());

    it('Space on the deck no longer defers; the container is a named group', async () => {
        const body = document.getElementById('audit-report-body');
        const meta = document.getElementById('audit-report-meta');
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        const deck = body.querySelector('.tagging-deck');
        expect(deck).toBeTruthy();
        // F4: aria-label on a role-less div is invalid ARIA — role=group names it.
        expect(deck.getAttribute('role')).toBe('group');

        const progress = document.getElementById('tagging-deck-progress');
        const before = progress.textContent;
        deck.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true }));
        // Pre-fix Space deferred the paper (progress text changed / card advanced).
        expect(progress.textContent).toBe(before);
        // M still defers.
        deck.dispatchEvent(new KeyboardEvent('keydown', { key: 'm', bubbles: true }));
        expect(document.getElementById('tagging-deck-card').textContent)
            .toContain('deferred paper(s) remain');
    });
});
