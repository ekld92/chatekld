// Pinning tests for partial-answer preservation + draft-safe retry
// (improvement plan 2026-07-04, item 3.5). Defect: a mid-stream {error}
// after real tokens removed the whole bot bubble — discarding an answer the
// user was already reading — and the Retry callback stuffed the failed
// question back into the input, clobbering any fresh draft typed after the
// failure. Invariants: streamed text survives a mid-stream error (marked
// interrupted, display-only), and Retry re-sends the original question
// without touching the input.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');

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

describe('plain chat partial answers + draft-safe retry (Track 3.5)', () => {
    let fetchMock;
    beforeEach(() => {
        document.body.innerHTML = `
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

    it('keeps streamed text visible after a mid-stream error', async () => {
        fetchMock.mockResolvedValue(sseOnce([
            { token: 'Début de réponse utile. ' },
            { error: 'provider fell over mid-stream' },
        ]));
        const plain = await import('../../static/js/plainchat.js');
        document.getElementById('plainchat-input').value = 'ma question';
        await plain.chatPlain();
        await new Promise((r) => setTimeout(r, 60));   // let the coalesced render land

        const historyEl = document.getElementById('plainchat-history');
        // Pre-fix the bubble was removed wholesale.
        expect(historyEl.textContent).toContain('Début de réponse utile.');
        expect(historyEl.textContent).toContain('interrupted');   // E13: UI standardized to English
    });

    it('Retry re-sends the failed question without clobbering a fresh draft', async () => {
        const sent = [];
        fetchMock.mockImplementation((url, opts = {}) => {
            sent.push(JSON.parse(opts.body));
            if (sent.length === 1) {
                return Promise.resolve(sseOnce([{ error: 'boom before any token' }]));
            }
            return Promise.resolve(sseOnce([{ token: 'ok cette fois' }]));
        });
        const plain = await import('../../static/js/plainchat.js');
        const input = document.getElementById('plainchat-input');
        input.value = 'question originale';
        await plain.chatPlain();

        // The user starts typing something NEW while the error boundary shows.
        input.value = 'brouillon tout neuf';

        // Click the boundary's Retry.
        const retryBtn = [...document.querySelectorAll('#plainchat-error-boundary button')]
            .find((b) => b.textContent === 'Retry');
        expect(retryBtn).toBeTruthy();
        retryBtn.click();
        await new Promise((r) => setTimeout(r, 60));

        // The retry re-sent the ORIGINAL question…
        const lastBody = sent[sent.length - 1];
        const lastUser = lastBody.messages[lastBody.messages.length - 1];
        expect(lastUser.content).toBe('question originale');
        // …and the fresh draft survived untouched.
        expect(input.value).toBe('brouillon tout neuf');
    });

    it('records a kept partial into history so a follow-up has context', async () => {
        // 2026-08-12 adjudicated #5: the partial stayed on screen but the user
        // turn was popped and no assistant turn pushed — a visible exchange
        // left NO trace in _history, so "développe ce point" was sent with
        // nothing for it to refer to (and the export omitted it).
        const sent = [];
        fetchMock.mockImplementation((url, opts = {}) => {
            sent.push(JSON.parse(opts.body));
            if (sent.length === 1) {
                return Promise.resolve(sseOnce([
                    { token: 'Réponse partielle utile. ' },
                    { error: 'provider fell over mid-stream' },
                ]));
            }
            return Promise.resolve(sseOnce([{ token: 'suite' }]));
        });
        const plain = await import('../../static/js/plainchat.js');
        const input = document.getElementById('plainchat-input');
        input.value = 'ma question';
        await plain.chatPlain();
        await new Promise((r) => setTimeout(r, 60));

        input.value = 'développe ce point';
        await plain.chatPlain();
        const followUp = sent[1].messages;
        const roles = followUp.map((m) => m.role);
        expect(roles).toContain('user');
        expect(followUp.some((m) => m.content === 'ma question')).toBe(true);
        expect(followUp.some((m) => m.role === 'assistant'
            && m.content.includes('Réponse partielle utile.')
            && m.content.includes('[interrupted]'))).toBe(true);
    });

    it('Retry after a kept partial does not double-render the question', async () => {
        const sent = [];
        fetchMock.mockImplementation((url, opts = {}) => {
            sent.push(JSON.parse(opts.body));
            if (sent.length === 1) {
                return Promise.resolve(sseOnce([
                    { token: 'Partiel. ' },
                    { error: 'boom mid-stream' },
                ]));
            }
            return Promise.resolve(sseOnce([{ token: 'réponse complète' }]));
        });
        const plain = await import('../../static/js/plainchat.js');
        document.getElementById('plainchat-input').value = 'ma question';
        await plain.chatPlain();
        await new Promise((r) => setTimeout(r, 60));

        const retryBtn = [...document.querySelectorAll('#plainchat-error-boundary button')]
            .find((b) => b.textContent === 'Retry');
        retryBtn.click();
        await new Promise((r) => setTimeout(r, 60));

        const bubbles = [...document.querySelectorAll('#plainchat-history .message-user')]
            .filter((b) => b.textContent.includes('ma question'));
        expect(bubbles.length).toBe(1);
        // The retry's wire copy still ends with the question so the model has
        // something to answer, with the recorded [interrupted] exchange ahead
        // of it for context.
        const lastBody = sent[sent.length - 1];
        expect(lastBody.messages[lastBody.messages.length - 1]).toEqual(
            { role: 'user', content: 'ma question' });
        expect(lastBody.messages.some((m) => m.role === 'assistant'
            && m.content.includes('[interrupted]'))).toBe(true);
    });
});

describe('single paper summary preserves partials (2026-08-12 adjudicated #2)', () => {
    // Source ratchet (summarisePDF needs an uploaded-paper module state that a
    // unit harness cannot reach cheaply): both failure branches used to
    // destroy already-streamed text — Stop overwrote the pane and a mid-stream
    // error blanked it — erasing a 90%-streamed map-reduce summary.
    const SUM = readFileSync(join(ROOT, 'static/js/summarizer.js'), 'utf8');
    const block = () => {
        const fn = SUM.indexOf('export async function summarisePDF');
        expect(fn).toBeGreaterThan(-1);
        const iCatch = SUM.indexOf('} catch (e) {', fn);
        expect(iCatch).toBeGreaterThan(-1);
        return SUM.slice(iCatch, iCatch + 3000);
    };

    it('the catch distinguishes a kept partial from an empty pane', () => {
        expect(block()).toMatch(/const partial = receivedText/);
    });

    it('Stop keeps streamed text and says so', () => {
        const b = block();
        expect(b).toContain('Summary stopped — the text above is partial');
        // The overwriting cancel message survives only for the empty case.
        const cancelIdx = b.indexOf("content.textContent = 'Summary cancelled");
        expect(cancelIdx).toBeGreaterThan(-1);
        expect(b.slice(0, cancelIdx)).toMatch(/if \(partial\)/);
    });

    it('a mid-stream error keeps streamed text beside the retry boundary', () => {
        const b = block();
        expect(b).toContain('Summary interrupted — the text above is partial');
        // The pane blank is confined to the no-partial branch.
        const blankIdx = b.indexOf("content.textContent = ''");
        expect(blankIdx).toBeGreaterThan(-1);
        expect(b.slice(0, blankIdx)).toMatch(/if \(partial\)/);
    });

    it('a kept partial is exportable', () => {
        expect(block()).toMatch(/exportBtn\.disabled = false/);
    });
});
