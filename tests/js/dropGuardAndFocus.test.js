// Batch E (2026-08-30) — two failure modes that live only in the browser, and
// one that the app's own house rules already forbade.
//
//   1. AN UNHANDLED FILE DROP NAVIGATES THE WINDOW. In PyWebView that replaces
//      the entire app with a PDF viewer, with no back button. plainchat.js's
//      own comment names the hazard, but it guarded ONE element — a drop that
//      missed the chat panel by a few pixels, or landed on the sidebar, still
//      destroyed the session.
//   2. A LIVE REGION MUST NOT CONTAIN CONTROLS. The attachment strip was
//      role="status" aria-live="polite" while holding a Remove button per chip:
//      every repaint re-announced the whole strip, and plainchat.js ALSO
//      announces each attach through the static announcer, so it was said twice.
//   3. A WIPE-AND-REBUILD MUST NOT EAT FOCUS. `_renderAttachmentStrip` clears
//      and rebuilds; a user tabbed onto a chip's × when the next upload landed
//      was dropped to <body>.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const HTML = readFileSync(join(ROOT, 'templates/index.html'), 'utf8');

const DOM = `
  <div id="chat-tab">
    <div id="plainchat-history"></div>
    <div id="plainchat-error-boundary"></div>
    <div id="plainchat-attachments"></div>
    <input type="file" id="plainchat-file" multiple>
    <button id="plainchat-attach-btn"></button>
    <input type="text" id="plainchat-input">
    <button id="plainchat-send-btn"></button>
  </div>
  <div id="elsewhere">not a drop zone</div>
  <div id="sr-status-announcer" role="status"></div>`;

/** A DragEvent jsdom will actually dispatch, carrying a Files type. */
function fileDrag(type) {
    const e = new Event(type, { bubbles: true, cancelable: true });
    Object.defineProperty(e, 'dataTransfer', {
        value: { types: ['Files'], files: [] }, configurable: true });
    return e;
}

describe('global drop guard', () => {
    beforeEach(() => {
        document.documentElement.removeAttribute('data-drop-guard');
        document.body.innerHTML = DOM;
        vi.resetModules();
    });
    afterEach(() => vi.unstubAllGlobals());

    it('prevents a file drop that lands outside any drop zone', async () => {
        const ui = await import('../../static/js/ui.js');
        ui.installGlobalDropGuard();
        const target = document.getElementById('elsewhere');
        for (const type of ['dragover', 'drop']) {
            const e = fileDrag(type);
            target.dispatchEvent(e);
            expect(e.defaultPrevented,
                   `${type} outside a drop zone must be swallowed`).toBe(true);
        }
    });

    it('leaves a drop a real zone already handled alone', async () => {
        const ui = await import('../../static/js/ui.js');
        ui.installGlobalDropGuard();
        const panel = document.getElementById('chat-tab');
        // A real zone prevents in its own handler, which runs first (deeper in
        // the tree, non-capturing listeners).
        let sawItFirst = false;
        panel.addEventListener('drop', (e) => {
            sawItFirst = !e.defaultPrevented;
            e.preventDefault();
        });
        panel.dispatchEvent(fileDrag('drop'));
        expect(sawItFirst, 'the zone must see the event before the guard').toBe(true);
    });

    it('ignores a non-file drag so text selection still works', async () => {
        const ui = await import('../../static/js/ui.js');
        ui.installGlobalDropGuard();
        const e = new Event('drop', { bubbles: true, cancelable: true });
        Object.defineProperty(e, 'dataTransfer', {
            value: { types: ['text/plain'] }, configurable: true });
        document.getElementById('elsewhere').dispatchEvent(e);
        expect(e.defaultPrevented).toBe(false);
    });

    it('is idempotent via a DOM marker, not a module flag', async () => {
        // A module boolean stays true across a re-import while the listeners it
        // claims to have installed may not exist (the audit search-bar class).
        const ui = await import('../../static/js/ui.js');
        ui.installGlobalDropGuard();
        ui.installGlobalDropGuard();
        expect(document.documentElement.dataset.dropGuard).toBe('1');
        const e = fileDrag('drop');
        document.getElementById('elsewhere').dispatchEvent(e);
        expect(e.defaultPrevented).toBe(true);
    });
});

describe('the attachment strip is a group of controls, not a live region', () => {
    it('declares role="group" and no aria-live in the markup', () => {
        const tag = HTML.match(/<div id="plainchat-attachments"[^>]*>/)[0];
        expect(tag).toContain('role="group"');
        expect(tag).toContain('aria-label=');
        // A live region containing focusable controls re-announces the WHOLE
        // strip on every repaint, and plainchat.js announces each attach
        // separately through the static announcer — so it was said twice.
        expect(tag).not.toContain('aria-live');
        expect(tag).not.toContain('role="status"');
    });

    it('keeps the static announcer as the one announcement channel', () => {
        expect(HTML).toContain('id="sr-status-announcer"');
    });
});

describe('focus survives the attachment strip repaint', () => {
    beforeEach(() => {
        document.body.innerHTML = DOM;
        vi.resetModules();
    });
    afterEach(() => vi.unstubAllGlobals());

    const uploaded = (over) => ({
        ok: true, status: 200, json: async () => ({
            attachment_id: 'att-1', filename: 'paper.pdf', kind: 'pdf',
            chars: 100, attached_chars: 100, truncated: false, ...over }),
    });

    async function attachOne(plain, id, name) {
        vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
            uploaded({ attachment_id: id, filename: name })));
        const input = document.getElementById('plainchat-file');
        Object.defineProperty(input, 'files', {
            value: [new File(['x'], name)], configurable: true });
        input.dispatchEvent(new Event('change'));
        await new Promise((r) => setTimeout(r, 15));
    }

    it('returns focus to the same chip after another upload repaints the strip', async () => {
        const plain = await import('../../static/js/plainchat.js');
        plain.initAttachments();
        await attachOne(plain, 'a1', 'first.pdf');

        const first = document.querySelector('.chip-remove[data-att-id="a1"]');
        expect(first).not.toBeNull();
        first.focus();
        expect(document.activeElement).toBe(first);

        await attachOne(plain, 'a2', 'second.pdf');
        // Same attachment, new node — matched by id, because the chip's
        // POSITION can change between renders while its identity does not.
        const again = document.querySelector('.chip-remove[data-att-id="a1"]');
        expect(again).not.toBeNull();
        expect(document.activeElement, 'focus must not fall to <body>').toBe(again);
    });

    it('moves focus to the composer when the focused chip is the one removed', async () => {
        const plain = await import('../../static/js/plainchat.js');
        plain.initAttachments();
        await attachOne(plain, 'a1', 'first.pdf');
        const rm = document.querySelector('.chip-remove[data-att-id="a1"]');
        rm.focus();
        vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }));
        rm.click();
        await new Promise((r) => setTimeout(r, 10));
        // Not a neighbour's × the user never aimed at.
        expect(document.activeElement.id).toBe('plainchat-input');
    });
});
