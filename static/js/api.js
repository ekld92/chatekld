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
        // Flush a final frame that arrived without a trailing blank line.
        const tail = payloadOf(buffer);
        if (tail && tail !== '[DONE]') {
            try { yield JSON.parse(tail); } catch (e) { /* ignore trailing partial */ }
        }
    } finally {
        try { reader.releaseLock(); } catch (_) { /* read in flight or already released */ }
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
