// Pinning test for the deck post-write sha echo (improvement plan 2026-07-04,
// item 3.4, deck half). The defect: _applyRepair ignored the fresh tex_sha256
// the server returned after overwriting the deck, and the Compile & Fix
// button's sha holder was a render-time COPY — so applying a repair then
// clicking Compile & Fix sent the PRE-repair sha and 409'd on a perfectly
// legitimate deck. Invariant: after apply-repair succeeds, every follow-up
// action sends the sha of the deck as it now exists on disk.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const enc = new TextEncoder();

function mountDeckDom() {
    document.body.innerHTML = `
      <input id="deck-topic" value="Sujet">
      <textarea id="deck-template-editor">\\documentclass{beamer}</textarea>
      <input id="deck-template-path" value="">
      <input id="deck-out-dir" value="/tmp/out">
      <input id="deck-name" value="">
      <textarea id="deck-instructions"></textarea>
      <input id="deck-audience" value="">
      <input id="deck-citations" type="checkbox">
      <input id="deck-overwrite" type="checkbox">
      <input id="deck-review" type="checkbox" checked>
      <input id="deck-force-fresh" type="checkbox">
      <input id="deck-max-sections" value="4">
      <input id="deck-agent-iters" value="4">
      <input id="deck-temp" value="0.4">
      <div id="deck-status"></div>
      <div id="deck-activity"></div>
      <div id="deck-result"></div>
      <button id="deck-generate-btn"></button>
      <button id="deck-aug-preview-btn"></button>
    `;
}

function sseOnce(frames) {
    const payload =
        frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('') + 'data: [DONE]\n\n';
    let sent = false;
    return {
        ok: true,
        status: 200,
        body: {
            getReader: () => ({
                read: async () =>
                    sent
                        ? { value: undefined, done: true }
                        : ((sent = true), { value: enc.encode(payload), done: false }),
                releaseLock: () => {},
                cancel: async () => {},
            }),
        },
    };
}

describe('deck apply-repair sha echo (Track 3.4)', () => {
    let fetchMock;
    beforeEach(() => {
        mountDeckDom();
        vi.resetModules();
        fetchMock = vi.fn();
        vi.stubGlobal('fetch', fetchMock);
    });
    afterEach(() => vi.unstubAllGlobals());

    it('compile-fix after an applied repair sends the FRESH sha', async () => {
        const bodies = {};
        fetchMock.mockImplementation((url, opts = {}) => {
            const u = String(url);
            if (u.includes('/api/deck/compile-available')) {
                return Promise.resolve({ ok: true, status: 200, json: async () => ({ available: true }) });
            }
            if (u.includes('/api/deck/generate')) {
                return Promise.resolve(sseOnce([{
                    deck: {
                        tex: '\\documentclass{beamer}', warnings: [],
                        section_count: 1, placeholder_count: 0,
                        slug: 'my_deck', out_dir: '/tmp/out',
                        project_dir: '/tmp/out/my_deck',
                        tex_path: '/tmp/out/my_deck/my_deck.tex',
                        tex_sha256: 'sha-BEFORE',
                        review: { ran: true, issues: [], changed: true,
                                  repaired_tex: '\\documentclass{beamer}fixed',
                                  repaired_warnings: [], truncated: false,
                                  repair_truncated: false, error: '' },
                    },
                }]));
            }
            if (u.includes('/api/deck/apply-repair')) {
                bodies.applyRepair = JSON.parse(opts.body);
                return Promise.resolve({
                    ok: true, status: 200,
                    json: async () => ({ ok: true, tex_path: '/tmp/out/my_deck/my_deck.tex',
                                         tex_sha256: 'sha-AFTER', warnings: [] }),
                });
            }
            if (u.includes('/api/deck/compile-fix')) {
                bodies.compileFix = JSON.parse(opts.body);
                return Promise.resolve(sseOnce([{
                    compile: { success: true, iterations: 1, changed: false,
                               tex_sha256: 'sha-AFTER', log_excerpt: '' },
                }]));
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
        });

        const deckMod = await import('../../static/js/deck.js');
        await deckMod.initDeck({ deck_review_enabled: true });   // arms _compileAvailable
        await deckMod.generate();                                 // renders the deck frame

        const buttons = [...document.querySelectorAll('#deck-result button')];
        const applyBtn = buttons.find((b) => b.textContent.includes('Apply repaired'));
        const compileBtn = buttons.find((b) => b.textContent.includes('Compile'));
        expect(applyBtn).toBeTruthy();
        expect(compileBtn).toBeTruthy();

        // Track 6e: the first click only ARMS the confirmation ceremony —
        // the overwrite fires from the strip's explicit confirm button.
        applyBtn.click();
        expect(bodies.applyRepair).toBeUndefined();
        const strip = document.querySelector('.inline-confirm-strip');
        expect(strip).toBeTruthy();
        strip.querySelectorAll('button')[1].click();   // [0] is Cancel-first
        await new Promise((r) => setTimeout(r, 0));
        expect(bodies.applyRepair.base_sha256).toBe('sha-BEFORE'); // stale-diff token of the review

        compileBtn.click();
        await new Promise((r) => setTimeout(r, 10));
        // Pre-fix: the copy in shaRef still held sha-BEFORE → server 409.
        expect(bodies.compileFix.base_sha256).toBe('sha-AFTER');
    });
});
