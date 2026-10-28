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

function sseOnce(frames, completion = Promise.resolve()) {
    const payload =
        frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('');
    let reads = 0;
    return {
        ok: true,
        status: 200,
        body: {
            getReader: () => ({
                read: async () => {
                    if (reads++ === 0) return { value: enc.encode(payload), done: false };
                    if (reads === 2) {
                        await completion;
                        return { value: enc.encode('data: [DONE]\n\n'), done: false };
                    }
                    return { value: undefined, done: true };
                },
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
        ['history-restore-status', 'role="status"'],
        // 2026-07-20 Card Catalog batch: the write-ceremony statuses that were
        // silent to AT (a VoiceOver user confirmed a vault write and heard
        // nothing) are now static live regions like their restore sibling.
        ['settings-status', 'role="status"'],
        ['audit-settings-status', 'role="status"'],
        // 2026-09-17 review, D5: Library status lines were roled at write time.
        ['library-note-status', 'role="status"'],
        ['library-campaign-plan-status', 'role="status"'],
        ['library-campaign-review-status', 'role="status"'],
        ['image-exts-status', 'role="status"'],
        // 2026-08-09 UI pass: the remaining load-bearing status surfaces were
        // static live regions in the markup but unpinned — removing any role
        // passed the suite. Pin them all.
        ['runtime-warning', 'role="status"'],
        ['vault-warning', 'role="status"'],
        ['audit-status-banner', 'role="status"'],
        ['deck-status', 'role="status"'],
        ['deck-aug-status', 'role="status"'],
        ['docgen-status', 'role="status"'],
        ['docgen-md-status', 'role="status"'],
        ['docgen-aug-status', 'role="status"'],
        ['prompts-status', 'role="status"'],
        ['reset-status', 'role="status"'],
        // 2026-08-18: the vault index status line — setStatusA11y used to
        // mint the role at write time (textContent first, the same-tick
        // WebKit hazard this file exists to prevent) and the region was
        // unpinned, so deleting the role passed the suite.
        ['obsidian-status-msg', 'role="status"'],
        // Maintenance tab (private-only feature whose MARKUP ships): the run
        // status line must be a static live region — the panel JS is stripped
        // publicly, so the role can only ever come from index.html.
        ['maintenance-status', 'role="status"'],
    ])('#%s ships with %s in the HTML', (id, role) => {
        // Element-agnostic: most regions are <div>s, #reset-status is a <p>.
        const tag = new RegExp(`<[a-z]+[^>]*id="${id}"[^>]*>`).exec(indexHtml);
        expect(tag, `#${id} missing from index.html`).toBeTruthy();
        expect(tag[0]).toContain(role);
    });

    it('#pull-status is deliberately NOT a live region (per-chunk progress would spam AT)', () => {
        const tag = new RegExp('<div[^>]*id="pull-status"[^>]*>').exec(indexHtml);
        expect(tag).toBeTruthy();
        expect(tag[0]).not.toContain('aria-live');
        expect(tag[0]).not.toContain('role=');
    });

    it.each([
        'deck-activity', 'deck-aug-activity', 'docgen-activity',
        'docgen-md-activity', 'docgen-aug-activity',
    ])('#%s (agent-trace log) is deliberately NOT a live region', (id) => {
        // 2026-08-18: a deck run appends ~50–100 per-iteration/tool_call
        // lines; aria-live here monopolized the SR queue for the whole run
        // (the audit search overlay's deliberate "no aria-live" precedent).
        // Milestones announce via announceStatus instead.
        const tag = new RegExp(`<div[^>]*id="${id}"[^>]*>`).exec(indexHtml);
        expect(tag, `#${id} missing from index.html`).toBeTruthy();
        expect(tag[0]).not.toContain('aria-live');
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

// 2026-07-26 UX audit — the SAME defect class as (2) above, still live in the
// vault chat's streamed {info} notices: vault.js created a <div>, set
// role="status" on it and wrote its text all in one tick, then inserted it. So
// the stage labels — "Loading cross-encoder reranker…", a provider-fallback
// notice — were very likely never announced, and a VoiceOver user heard
// "Generating response…" then silence through a multi-GB model load with the
// one message that EXPLAINS the wait inaudible. They now go through the static
// #sr-status-announcer region (announceStatus).
describe('vault chat {info} notices reach assistive tech (2026-07-26)', () => {
    let fetchMock;
    beforeEach(() => {
        document.body.innerHTML = `
          <div id="sr-status-announcer" role="status" aria-live="polite"></div>
          <div id="sr-error-announcer" role="alert" aria-live="assertive"></div>
          <div id="obsidian-status-msg"></div>
          <div id="vault-error-boundary"></div>
          <input id="obsidian-input" value="">
          <div id="obsidian-chat"></div>
          <div id="vault-index-progress" hidden></div>
        `;
        vi.resetModules();
        fetchMock = vi.fn();
        vi.stubGlobal('fetch', fetchMock);
    });
    afterEach(() => vi.unstubAllGlobals());

    it('announces and displays progress during streaming, then clears the completed stage', async () => {
        const announcer = document.getElementById('sr-status-announcer');
        const seen = [];
        const observer = new MutationObserver(() => {
            if (announcer.textContent) seen.push(announcer.textContent);
        });
        observer.observe(announcer, { childList: true, characterData: true, subtree: true });
        vi.stubGlobal('requestAnimationFrame', (cb) => { cb(); return 0; });

        let finish;
        const completion = new Promise(resolve => { finish = resolve; });
        fetchMock.mockImplementation((url) => {
            if (String(url).includes('/api/obsidian/chat')) {
                return Promise.resolve(sseOnce([
                    { info: 'Loading cross-encoder reranker…' },
                    { token: 'ok' },
                ], completion));
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
        });
        const vault = await import('../../static/js/vault.js');
        document.getElementById('obsidian-input').value = 'une question';
        const run = vault.chatWithVault();
        try {
            await vi.waitFor(() => {
                expect(seen.some((t) => t.includes('cross-encoder reranker'))).toBe(true);
                const bubble = document.querySelector('#obsidian-chat .message-info');
                expect(bubble).toBeTruthy();
                expect(bubble.textContent).toContain('cross-encoder reranker');
            });
        } finally {
            finish();
            await run;
            observer.disconnect();
        }
        expect(document.querySelector('#obsidian-chat .message-info')).toBeNull();
        expect(document.querySelector('#obsidian-chat .message-bot').textContent).toContain('ok');
    });

    it('RATCHET: the info bubble does not mint its own live-region role at write time', () => {
        const vaultSrc = readFileSync(
            resolve(dirname(fileURLToPath(import.meta.url)), '../../static/js/vault.js'),
            'utf-8',
        );
        const onInfo = /onInfo:\s*\(info\)\s*=>\s*\{[\s\S]*?\n {12}\},/.exec(vaultSrc);
        expect(onInfo, 'onInfo handler not found in vault.js').toBeTruthy();
        // A role/aria-live set on a node in the same tick it is populated can be
        // missed by WebKit — the whole reason this file exists. Announce through
        // the STATIC region instead.
        expect(onInfo[0]).not.toMatch(/setAttribute\(\s*['"](role|aria-live)['"]/);
        expect(onInfo[0]).toContain('announceStatus(');
    });

    it('RATCHET: plainchat onInfo announces via the static region too (2026-08-09)', () => {
        // Same defect class, plain-chat copy: its onInfo minted role="status"
        // in the write tick and never called announceStatus, so provider
        // fallback / stage notices were silent there while audible in vault chat.
        const src = readFileSync(
            resolve(dirname(fileURLToPath(import.meta.url)), '../../static/js/plainchat.js'),
            'utf-8',
        );
        const onInfo = /onInfo:\s*\(info\)\s*=>\s*\{[\s\S]*?\n {12}\},/.exec(src);
        expect(onInfo, 'onInfo handler not found in plainchat.js').toBeTruthy();
        expect(onInfo[0]).not.toMatch(/setAttribute\(\s*['"](role|aria-live)['"]/);
        expect(onInfo[0]).toContain('announceStatus(');
    });

});
