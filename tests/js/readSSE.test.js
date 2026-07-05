// Contract tests for api.js::readSSE (improvement plan 2026-07-04, Track 7.3).
//
// readSSE(response) is an async generator over an already-fetched Response —
// it never issues a network request itself. Track 1.1 (the dead Compile &
// Auto-Fix button) was exactly this signature being misused as
// readSSE(url, options, callback): the never-iterated generator was awaited as
// a plain value, so no request was ever sent and no error surfaced. These
// tests pin the real contract (parse/split/UTF-8/[DONE]) and pin that the
// misuse class can no longer fail silently once iterated.
import { describe, it, expect, vi } from 'vitest';
import { readSSE } from '../../static/js/api.js';

const enc = new TextEncoder();

/** Build a minimal fetch-Response stand-in whose body streams `chunks`. */
function sseResponse(chunks) {
    let i = 0;
    const reader = {
        read: async () =>
            i < chunks.length
                ? { value: enc.encode(chunks[i++]), done: false }
                : { value: undefined, done: true },
        releaseLock: vi.fn(),
        cancel: vi.fn(async () => {}),
    };
    return { body: { getReader: () => reader }, _reader: reader };
}

async function collect(response) {
    const out = [];
    for await (const evt of readSSE(response)) out.push(evt);
    return out;
}

describe('readSSE', () => {
    it('yields each data: frame as parsed JSON and stops at [DONE]', async () => {
        const resp = sseResponse([
            'data: {"info": "stage"}\n\ndata: {"token": "a"}\n\n',
            'data: [DONE]\n\ndata: {"token": "never"}\n\n',
        ]);
        expect(await collect(resp)).toEqual([{ info: 'stage' }, { token: 'a' }]);
    });

    it('buffers a frame split across two network chunks', async () => {
        const resp = sseResponse([
            'data: {"tok',
            'en": "b"}\n\ndata: [DONE]\n\n',
        ]);
        expect(await collect(resp)).toEqual([{ token: 'b' }]);
    });

    it('reassembles a multi-byte UTF-8 character split across chunks', async () => {
        // 'é' is 0xC3 0xA9 — split the two bytes across reads; a non-streaming
        // decoder would corrupt both halves into U+FFFD.
        const head = enc.encode('data: {"token": "é"}\n\ndata: [DONE]\n\n');
        const splitAt = 17; // inside the é sequence
        let i = 0;
        const parts = [head.slice(0, splitAt), head.slice(splitAt)];
        const reader = {
            read: async () =>
                i < parts.length
                    ? { value: parts[i++], done: false }
                    : { value: undefined, done: true },
            releaseLock: vi.fn(),
        };
        const out = await collect({ body: { getReader: () => reader } });
        expect(out).toEqual([{ token: 'é' }]);
    });

    it('skips a malformed frame instead of throwing', async () => {
        const resp = sseResponse([
            'data: {broken\n\ndata: {"token": "c"}\n\ndata: [DONE]\n\n',
        ]);
        expect(await collect(resp)).toEqual([{ token: 'c' }]);
    });

    it('flushes a trailing frame that arrived without a blank-line terminator', async () => {
        const resp = sseResponse(['data: {"token": "z"}']);
        expect(await collect(resp)).toEqual([{ token: 'z' }]);
    });

    it('releases the reader lock when iteration completes', async () => {
        const resp = sseResponse(['data: [DONE]\n\n']);
        await collect(resp);
        expect(resp._reader.releaseLock).toHaveBeenCalled();
    });

    it('cancels the reader on EVERY exit — including an early break (Track 3.5)', async () => {
        // Early exit: the consumer breaks after the first frame (the error-
        // frame pattern every SSE consumer uses). Pre-fix only the lock was
        // released — the HTTP connection stayed open, the server streaming
        // into a dead pipe until its own stall guard fired.
        const resp = sseResponse([
            'data: {"token": "a"}\n\ndata: {"token": "b"}\n\ndata: [DONE]\n\n',
        ]);
        for await (const evt of readSSE(resp)) {
            void evt;
            break;                       // early exit mid-stream
        }
        expect(resp._reader.cancel).toHaveBeenCalled();
        expect(resp._reader.releaseLock).toHaveBeenCalled();

        // Normal completion cancels too (a no-op on a finished stream).
        const resp2 = sseResponse(['data: [DONE]\n\n']);
        await collect(resp2);
        expect(resp2._reader.cancel).toHaveBeenCalled();
    });

    it('flushes a multi-byte character split at STREAM END (Track 3.5)', async () => {
        // The é's second byte never arrives in a data chunk followed by more
        // data — the stream just ends. decode(…, {stream:true}) was holding
        // the first byte back forever; the final argless decode() flushes it.
        const whole = enc.encode('data: {"token": "caf\u00e9"}');
        let i = 0;
        const parts = [whole.slice(0, whole.length - 2), whole.slice(whole.length - 2)];
        const reader = {
            read: async () =>
                i < parts.length
                    ? { value: parts[i++], done: false }
                    : { value: undefined, done: true },
            releaseLock: () => {},
            cancel: async () => {},
        };
        const out = [];
        for await (const evt of readSSE({ body: { getReader: () => reader } })) out.push(evt);
        expect(out).toEqual([{ token: 'café' }]);
    });

    it('rejects loudly when misused with a URL instead of a Response (Track 1.1 class)', async () => {
        // The 1.1 bug passed (url, options, cb) and never iterated — silent
        // no-op. Iterating the misuse must throw, so any consumer written with
        // `for await` fails fast instead of silently sending nothing.
        const gen = readSSE('/api/deck/compile-fix');
        await expect(gen.next()).rejects.toThrow();
    });
});
