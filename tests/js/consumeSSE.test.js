// Contract tests for api.js::consumeSSE (2026-07-20 audit A2/A3).
//
// The consumer must call onError AT MOST ONCE (it is terminal), and it must
// preserve an aborted fetch's DOMException identity (name === 'AbortError')
// so the vault/plainchat callers' friendly-timeout branch is reachable —
// both were broken: a throwing onError was invoked twice, and an abort was
// flattened through onError(exc.message), losing the name.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { consumeSSE } from '../../static/js/api.js';

const enc = new TextEncoder();

/** Mock global fetch to return an SSE body streaming `chunks`. */
function mockSSEFetch(chunks, { ok = true, status = 200 } = {}) {
    let i = 0;
    const reader = {
        read: async () =>
            i < chunks.length
                ? { value: enc.encode(chunks[i++]), done: false }
                : { value: undefined, done: true },
        releaseLock: vi.fn(),
        cancel: vi.fn(async () => {}),
    };
    globalThis.fetch = vi.fn(async () => ({
        ok, status,
        body: { getReader: () => reader },
        json: async () => ({}),
    }));
}

afterEach(() => { vi.restoreAllMocks(); });

describe('consumeSSE — onError-once + abort identity', () => {
    it('calls a throwing onError exactly once on a server {error} frame', async () => {
        mockSSEFetch(['data: {"error": "boom"}\n\n']);
        const onError = vi.fn((msg) => { throw new Error(msg); });
        // The caller's throw propagates out of consumeSSE (vault relies on this).
        await expect(consumeSSE('/x', {}, { onError })).rejects.toThrow('boom');
        expect(onError).toHaveBeenCalledTimes(1);
    });

    it('re-throws an aborted fetch as the original AbortError DOMException', async () => {
        const abortErr = new DOMException('The operation was aborted.', 'AbortError');
        globalThis.fetch = vi.fn(async () => { throw abortErr; });
        const onError = vi.fn();
        let caught;
        try {
            await consumeSSE('/x', {}, { onError });
        } catch (e) { caught = e; }
        expect(caught).toBe(abortErr);
        expect(caught.name).toBe('AbortError');
        // The abort must NOT be flattened through onError.
        expect(onError).not.toHaveBeenCalled();
    });

    it('delivers a genuine network error through onError once', async () => {
        globalThis.fetch = vi.fn(async () => { throw new TypeError('network down'); });
        const onError = vi.fn();
        await consumeSSE('/x', {}, { onError });
        expect(onError).toHaveBeenCalledTimes(1);
        expect(onError).toHaveBeenCalledWith('network down');
    });

    it('dispatches tokens then onDone on a clean stream', async () => {
        mockSSEFetch(['data: {"token": "a"}\n\ndata: {"token": "b"}\n\ndata: [DONE]\n\n']);
        const onToken = vi.fn();
        const onDone = vi.fn();
        const onError = vi.fn();
        await consumeSSE('/x', {}, { onToken, onDone, onError });
        expect(onToken.mock.calls.map((c) => c[0])).toEqual(['a', 'b']);
        expect(onDone).toHaveBeenCalledTimes(1);
        expect(onError).not.toHaveBeenCalled();
    });
});
