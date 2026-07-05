// Pinning tests for partial-answer preservation + draft-safe retry
// (improvement plan 2026-07-04, item 3.5). Defect: a mid-stream {error}
// after real tokens removed the whole bot bubble — discarding an answer the
// user was already reading — and the Retry callback stuffed the failed
// question back into the input, clobbering any fresh draft typed after the
// failure. Invariants: streamed text survives a mid-stream error (marked
// interrupted, display-only), and Retry re-sends the original question
// without touching the input.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

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
        expect(historyEl.textContent).toContain('interrompue');
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
});
