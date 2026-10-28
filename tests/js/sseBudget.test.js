import { afterEach, describe, expect, it, vi } from 'vitest';
import { consumeSSE } from '../../static/js/api.js';

const encoder = new TextEncoder();
function pendingStream(seconds) {
    let deliver;
    let signal;
    const reader = {
        read: () => new Promise((resolve, reject) => {
            deliver = (frame) => resolve({ value: encoder.encode(`data: ${JSON.stringify(frame)}\n\n`), done: false });
            signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true });
        }),
        releaseLock: vi.fn(), cancel: vi.fn(async () => {}),
    };
    globalThis.fetch = vi.fn(async (_url, options) => {
        signal = options.signal;
        return { ok: true, headers: new Headers({ 'X-PaperMind-SSE-Silence-S': String(seconds) }),
            body: { getReader: () => reader } };
    });
    return { signal: () => signal, deliver: (frame) => deliver(frame), reader };
}
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

describe('server-owned SSE silence budget', () => {
    it.each([330, 512])('outlasts the %s second server guard and preserves AbortError', async (seconds) => {
        vi.useFakeTimers();
        const stream = pendingStream(seconds);
        const work = consumeSSE('/x', { serverBudget: true }, {}).catch((e) => e);
        await vi.advanceTimersByTimeAsync(0);
        await vi.advanceTimersByTimeAsync(seconds * 1000 + 29999);
        expect(stream.signal().aborted).toBe(false);
        await vi.advanceTimersByTimeAsync(1);
        expect((await work).name).toBe('AbortError');
        expect(stream.reader.cancel).toHaveBeenCalled();
        expect(vi.getTimerCount()).toBe(0);
    });

    it('renews on progress; a partial answer followed by an error is never replayed', async () => {
        vi.useFakeTimers();
        const stream = pendingStream(512);
        const tokens = vi.fn(); const errors = vi.fn();
        const work = consumeSSE('/x', { serverBudget: true }, { onToken: tokens, onError: errors });
        await vi.advanceTimersByTimeAsync(400000);
        stream.deliver({ token: 'partial' });
        await vi.advanceTimersByTimeAsync(400000);
        expect(stream.signal().aborted).toBe(false);
        stream.deliver({ error: 'provider failed' });
        await work;
        expect(tokens).toHaveBeenCalledOnce();
        expect(errors).toHaveBeenCalledOnce();
        expect(fetch).toHaveBeenCalledOnce();
        expect(vi.getTimerCount()).toBe(0);
    });

    it('forwards explicit cancellation while waiting for token one', async () => {
        vi.useFakeTimers();
        const stream = pendingStream(512);
        const cancel = new AbortController();
        const work = consumeSSE('/x', { serverBudget: true, signal: cancel.signal }, {}).catch((e) => e);
        await vi.advanceTimersByTimeAsync(0);
        cancel.abort();
        expect((await work).name).toBe('AbortError');
        expect(stream.signal().aborted).toBe(true);
        expect(vi.getTimerCount()).toBe(0);
    });
});
