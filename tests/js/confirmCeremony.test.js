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

    // 2026-07-19 keyboard-trap fix: consumers (the tagging deck) bind
    // container-level verb handlers that preventDefault Enter/Space, which
    // cancelled the focused strip button's native activation — the confirm
    // was mouse-only. The strip now stops those keys from propagating and
    // handles Escape itself.
    it('Enter/Space inside the strip never propagate to ancestor verb handlers', () => {
        const anchor = document.getElementById('anchor');
        const wrap = document.getElementById('wrap');
        const seen = vi.fn();
        wrap.addEventListener('keydown', seen);
        const strip = ui.confirmInline(anchor, { message: 'm', confirmLabel: 'Go', onConfirm: () => {} });
        const cancel = strip.querySelectorAll('button')[0];
        for (const key of ['Enter', ' ']) {
            cancel.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }));
        }
        expect(seen).not.toHaveBeenCalled();
        // An unrelated key still propagates (the strip only owns its own verbs).
        cancel.dispatchEvent(new KeyboardEvent('keydown', { key: 'x', bubbles: true }));
        expect(seen).toHaveBeenCalledTimes(1);
    });

    it('Escape dismisses the strip without firing and refocuses the anchor', () => {
        const anchor = document.getElementById('anchor');
        const onConfirm = vi.fn();
        const strip = ui.confirmInline(anchor, { message: 'm', confirmLabel: 'Go', onConfirm });
        strip.querySelectorAll('button')[0].dispatchEvent(
            new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
        expect(onConfirm).not.toHaveBeenCalled();
        expect(document.querySelector('.inline-confirm-strip')).toBeNull();
        expect(document.activeElement).toBe(anchor);
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
        // B8 (2026-07-13): restore is now server-side confirm-gated, so the
        // client sends confirm: true alongside the revert-all flag.
        expect(JSON.parse(calls[0][1].body)).toEqual({ all: true, confirm: true });
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

    it('docgen.js routes its Markdown vault write + article overwrite through confirmInline', () => {
        // The Markdown "Generate note" button writes a NEW note into the vault,
        // and "Apply to the article" overwrites a user .tex — both must arm the
        // Track 6e ceremony before writing.
        const src = readFileSync(resolve(root, 'static/js/docgen.js'), 'utf-8');
        expect(src).toMatch(/confirmInline\(btn,[\s\S]{0,400}?_runMarkdown\(topic, scope\)/);
        expect(src).not.toMatch(/onclick[\s\S]{0,40}_runMarkdown/);
        expect(src).toMatch(/confirmInline\(applyBtn,[\s\S]{0,400}?_applyAugment\(aug, applyBtn\)/);
        expect(src).not.toMatch(/addEventListener\('click',\s*\(\)\s*=>\s*_applyAugment/);
    });

    it('audit.js routes its vault write through confirmInline, never window.confirm', () => {
        // Review lot 3.1 ratchet: apply-tag-drift overwrites a user vault
        // note, so it must use the Track 6e ceremony. A bare window.confirm
        // anywhere in audit.js reintroduces the violation.
        const src = readFileSync(resolve(root, 'static/js/audit.js'), 'utf-8');
        expect(src).not.toMatch(/window\.confirm/);
        expect(src).toMatch(/confirmInline\(btn,[\s\S]{0,700}?apply-tag-drift/);
    });

    it('the finder-drift fix verbs all arm the ceremony (drift-fix 2026-08-06)', () => {
        // Three new vault/PDF/vocabulary-writing buttons in the finder drift
        // report: [→ note] / [→ Finder] (via _finderDriftApply), [+ vocab]
        // (via _finderVocabAdd), and the per-row [undo] (a restore rewrites
        // user data too). Each must route through confirmInline.
        const src = readFileSync(resolve(root, 'static/js/audit.js'), 'utf-8');
        expect(src).toMatch(/function _finderDriftApply[\s\S]{0,900}?confirmInline\(btn,/);
        expect(src).toMatch(/function _finderVocabAdd[\s\S]{0,900}?confirmInline\(btn,/);
        expect(src).toMatch(/function _finderDriftUndoBtn[\s\S]{0,900}?confirmInline\(undo,/);
        // The dispatch never calls the writers directly.
        expect(src).not.toMatch(/if \(action === 'finder-apply-note'\) \{ await secureFetch/);
    });

    it('the near-duplicate dismiss AND its undo both arm the ceremony (2026-08-09)', () => {
        // The [restore]/[undo] of a dismissal was the lone unconfirmed write
        // verb in the Possible Duplicates report while its sibling [not a
        // duplicate] and every other undo in the app were gated — asymmetric
        // ceremony inside one report teaches users the strip is noise.
        const src = readFileSync(resolve(root, 'static/js/audit.js'), 'utf-8');
        expect(src).toMatch(/function _dupDismiss[\s\S]{0,600}?confirmInline\(btn,/);
        expect(src).toMatch(/function _dupUndismiss[\s\S]{0,600}?confirmInline\(btn,/);
        // Neither dispatch path calls the writer directly.
        expect(src).not.toMatch(/if \(action === 'dup-undismiss'\) \{ await _dupDecision/);
        expect(src).not.toMatch(/addEventListener\('click',\s*\(\)\s*=>\s*_dupDecision/);
    });

    it('compile & auto-fix arms the ceremony in BOTH panels (2026-07-22 E10)', () => {
        // The compile-fix loop writes LLM-authored repairs into the user's
        // .tex — identical risk to apply-repair/apply-augment, which carry a
        // strip. An unceremonied sibling teaches users the strip is noise.
        const deckSrc = readFileSync(resolve(root, 'static/js/deck.js'), 'utf-8');
        expect(deckSrc).toMatch(/confirmInline\(compileBtn,[\s\S]{0,500}?runCompileFix\(/);
        expect(deckSrc).not.toMatch(/addEventListener\('click',\s*\(\)\s*=>\s*runCompileFix/);
        const docSrc = readFileSync(resolve(root, 'static/js/docgen.js'), 'utf-8');
        expect(docSrc).toMatch(/confirmInline\(fixBtn,[\s\S]{0,500}?runDocCompileFix\(/);
        expect(docSrc).not.toMatch(/addEventListener\('click',\s*\(\)\s*=>\s*runDocCompileFix/);
    });

    it('the ambiguous-PDF TABLE view confirm-gates its mapping writes (2026-07-22 E9)', () => {
        // The deck view was built precisely because the table assigned a key
        // on one unconfirmed click; the kept table must not retain the defect.
        const src = readFileSync(resolve(root, 'static/js/audit.js'), 'utf-8');
        expect(src).toMatch(/confirmInline\(btn,[\s\S]{0,400}?_assignAmbiguous\(btn\)/);
        expect(src).toMatch(/confirmInline\(btn,[\s\S]{0,400}?_markNoMatch\(btn\)/);
        expect(src).not.toMatch(/if \(action === 'pick-key'\) \{ await _assignAmbiguous/);
    });

    it('refactor.js gates Approve & apply and Archive through confirmInline (2026-07-22 E11)', () => {
        const src = readFileSync(resolve(root, 'static/js/refactor.js'), 'utf-8');
        expect(src).toMatch(/confirmInline\(apply,[\s\S]{0,400}?_applyStaged\(/);
        expect(src).toMatch(/confirmInline\(btn,[\s\S]{0,600}?_doArchive\(/);
        // The bespoke archive confirm box (its own DOM + ordering) is gone.
        expect(src).not.toMatch(/refactor-archive-confirm/);
    });
});
