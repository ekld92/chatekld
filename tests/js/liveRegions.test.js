// Track 6b pinning — streamed answers announce to assistive tech, and the
// status surfaces that AT must hear are STATIC live regions in the markup.
//
// Defects pinned: (1) streamed chat/summary output rendered silently for
// screen-reader users (no generating/ready lifecycle); (2) status elements
// relied on roles minted by JS at write time — WebKit can miss a live region
// created (or roled) the same tick it is populated, so the load-bearing
// regions must ship in templates/index.html.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const enc = new TextEncoder();

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

const indexHtml = readFileSync(
    resolve(dirname(fileURLToPath(import.meta.url)), '../../templates/index.html'),
    'utf-8',
);

describe('static live regions in markup (Track 6b)', () => {
    it.each([
        ['sr-status-announcer', 'role="status"'],
        ['sr-error-announcer', 'role="alert"'],
        ['prewarm-banner', 'role="status"'],
        ['excl-status', 'role="status"'],
        ['refactor-restore-status', 'role="status"'],
    ])('#%s ships with %s in the HTML', (id, role) => {
        const tag = new RegExp(`<div[^>]*id="${id}"[^>]*>`).exec(indexHtml);
        expect(tag, `#${id} missing from index.html`).toBeTruthy();
        expect(tag[0]).toContain(role);
    });

    it('#pull-status is deliberately NOT a live region (per-chunk progress would spam AT)', () => {
        const tag = new RegExp('<div[^>]*id="pull-status"[^>]*>').exec(indexHtml);
        expect(tag).toBeTruthy();
        expect(tag[0]).not.toContain('aria-live');
        expect(tag[0]).not.toContain('role=');
    });
});

describe('plain chat announces the generation lifecycle (Track 6b)', () => {
    let fetchMock;
    beforeEach(() => {
        document.body.innerHTML = `
          <div id="sr-status-announcer" role="status" aria-live="polite"></div>
          <textarea id="plainchat-input"></textarea>
          <div id="plainchat-history"></div>
          <div id="plainchat-error-boundary"></div>
          <button id="plainchat-send-btn"></button>
        `;
        vi.resetModules();
        fetchMock = vi.fn();
        vi.stubGlobal('fetch', fetchMock);
    });
    afterEach(() => vi.unstubAllGlobals());

    it('announces Generating… then Response ready. through #sr-status-announcer', async () => {
        // announceStatus clears then rewrites inside requestAnimationFrame —
        // record every write so the transient 'Generating' text is observable.
        const announcer = document.getElementById('sr-status-announcer');
        const seen = [];
        const observer = new MutationObserver(() => {
            if (announcer.textContent) seen.push(announcer.textContent);
        });
        observer.observe(announcer, { childList: true, characterData: true, subtree: true });
        vi.stubGlobal('requestAnimationFrame', (cb) => { cb(); return 0; });

        fetchMock.mockResolvedValue(sseOnce([{ token: 'Bonjour.' }]));
        const plain = await import('../../static/js/plainchat.js');
        document.getElementById('plainchat-input').value = 'salut';
        await plain.chatPlain();
        await new Promise((r) => setTimeout(r, 60));
        observer.takeRecords();
        observer.disconnect();

        expect(seen.some((t) => t.includes('Generating'))).toBe(true);
        expect(announcer.textContent).toBe('Response ready.');
    });
});
