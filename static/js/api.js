/**
 * Network / HTTP layer — a LEAF module in the JS module hierarchy: it imports
 * nothing from this project, so every other module may depend on it freely
 * without risking a circular import. Owns the CSRF-safe fetch wrapper, the SSE
 * frame parser shared by every streaming route, and the global error logger.
 */

/**
 * CSRF-safe fetch wrapper; adds the X-Requested-With header required
 * by the server's local origin check on every request.
 */
export async function secureFetch(url, options = {}) {
    const defaults = {
        headers: {
            'X-Requested-With': 'ChatEKLD',
            'Content-Type': 'application/json'
        }
    };
    const headers = { ...defaults.headers, ...(options.headers || {}) };
    return fetch(url, { ...options, headers });
}

/**
 * Parse a Server-Sent-Events response into a stream of decoded JSON payloads.
 *
 * Buffers across network-chunk boundaries — a single SSE frame can be split
 * across two `reader.read()` calls — and tolerates a malformed frame by
 * skipping it rather than throwing, so a partial/garbled frame can never
 * convert a healthy stream into a user-visible error.  Stops when the server
 * sends the `data: [DONE]` sentinel.
 *
 * Frames are `\n\n`-separated `data: <json>` events per the app's SSE
 * contract (see the streaming routes under api/routes/).
 *
 * @param {Response} response  a fetch() Response whose body is an SSE stream
 * @yields {object} the parsed payload of each `data:` frame
 */
export async function* readSSE(response) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    // An event block may carry one or more `data:` lines; join their payloads
    // per the SSE spec. Non-data lines (comments, blanks) are ignored.
    const payloadOf = (block) =>
        block
            .split('\n')
            .filter((l) => l.startsWith('data:'))
            .map((l) => l.slice(l.startsWith('data: ') ? 6 : 5))
            .join('\n');

    try {
        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const blocks = buffer.split('\n\n');
            buffer = blocks.pop();  // retain the trailing (possibly partial) frame
            for (const block of blocks) {
                const data = payloadOf(block);
                if (!data) continue;
                if (data === '[DONE]') return;
                try {
                    yield JSON.parse(data);
                } catch (e) {
                    console.warn('[ChatEKLD] Skipped malformed SSE frame:', data.slice(0, 120));
                }
            }
        }
        // Item 3.5: flush the decoder's internal buffer. decode(value,
        // {stream:true}) holds back a multi-byte UTF-8 sequence split at a
        // chunk boundary; if the stream ENDS on that split, the held bytes
        // were silently dropped from the tail frame below. The argless
        // decode() emits them (as U+FFFD if genuinely incomplete — visible,
        // not vanished).
        buffer += decoder.decode();
        // Flush a final frame that arrived without a trailing blank line.
        const tail = payloadOf(buffer);
        if (tail && tail !== '[DONE]') {
            try { yield JSON.parse(tail); } catch (e) { /* ignore trailing partial */ }
        }
    } finally {
        // Item 3.5: cancel BEFORE releasing the lock (cancel needs it). An
        // early exit — a consumer breaking out of `for await` on an {error}
        // frame, or an exception mid-loop — used to only release the lock,
        // leaving the HTTP connection open and the server streaming into a
        // dead pipe until its own stall guard fired. cancel() closes the
        // underlying stream; on a normally-finished stream it is a no-op.
        // Invariant (pinned by tests/js/readSSE.test.js): every exit path
        // cancels the reader.
        try { reader.cancel(); } catch (_) { /* already closed */ }
        try { reader.releaseLock(); } catch (_) { /* read in flight or already released */ }
    }
}

/**
 * Parse a fetch Response body as JSON without ever throwing.
 *
 * The action endpoints can answer with a non-JSON body — a Flask HTML error
 * page, a proxy 502, or an origin-reject — in which case a bare `resp.json()`
 * throws and the caller's catch block loses the real HTTP status behind a
 * generic "parse failed". This returns `{}` on any non-JSON body so the caller
 * can fall through to its `!resp.ok` branch and surface `HTTP <status>` (or the
 * server's structured `{error}` when the body IS json). Never rejects.
 *
 * @param {Response} resp a fetch() Response
 * @returns {Promise<object>} the parsed object, or `{}` if the body isn't JSON
 */
export async function safeJson(resp) {
    try {
        return await resp.json();
    } catch (_) {
        return {};
    }
}

/**
 * Global last-resort error logger.
 *
 * Logs to the console, then best-effort POSTs the message to /api/log so it
 * lands in chatekld.log (the server truncates the line to 500 chars). The POST
 * is fire-and-forget and swallows its own failure so logging an error can never
 * itself throw.
 */
export function logError(msg, error) {
    console.error(`[ChatEKLD] ${msg}`, error);
    try {
        fetch('/api/log', {
            method: 'POST',
            headers: { 'X-Requested-With': 'ChatEKLD', 'Content-Type': 'application/json' },
            body: JSON.stringify({ level: 'error', msg: `[JS] ${msg}: ${error?.message || String(error)}` }),
        }).catch(() => {});
    } catch (_) {}
}

/**
 * Shared consumer for SSE streams (improvement plan 2026-07-04, item 4.4).
 * Handles the secureFetch boilerplate, bad-response fallback, SSE stream draining,
 * and dispatching frames to the caller's handlers.
 *
 * @param {string} url - The URL to POST to.
 * @param {object} options - Fetch options (e.g. body string).
 * @param {object} handlers - Handlers for each frame type: { onInfo, onError, onToken, onOther, onDone }
 * @returns {Promise<void>} Resolves when the stream fully completes.
 */
export async function consumeSSE(url, options, handlers) {
    try {
        const resp = await secureFetch(url, options);
        if (!resp.ok) {
            const result = await safeJson(resp);
            const err = result.error || `HTTP ${resp.status} - failed to start stream`;
            handlers.onError?.(err);
            return;
        }

        for await (const frame of readSSE(resp)) {
            if (frame.error) {
                handlers.onError?.(frame.error);
                return; // Server {error} is terminal
            }
            if (frame.info) {
                handlers.onInfo?.(frame.info);
            } else if (frame.token !== undefined) {
                handlers.onToken?.(frame.token);
            } else {
                handlers.onOther?.(frame);
            }
        }
        handlers.onDone?.();
    } catch (exc) {
        handlers.onError?.(exc.message || String(exc));
    }
}
