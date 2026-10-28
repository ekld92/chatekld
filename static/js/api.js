/**
 * Network / HTTP layer — a LEAF module in the JS module hierarchy: it imports
 * nothing from this project, so every other module may depend on it freely
 * without risking a circular import. Owns the CSRF-safe fetch wrapper, the SSE
 * frame parser shared by every streaming route, and the global error logger.
 */

/**
 * Local-origin fetch wrapper; adds the X-Requested-With CSRF signal required
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
 * @param {object} options requireDone rejects premature EOF; readers with their
 * own business terminal may leave it false and validate that terminal themselves.
 * @yields {object} the parsed payload of each `data:` frame
 */
export async function* readSSE(response, { requireDone = false } = {}) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    // An event block may carry one or more `data:` lines; join their payloads
    // per the SSE spec. Non-data lines (comments, blanks) are ignored.
    const payloadOf = (block) =>
        block
            .split(/\r?\n/)
            .filter((l) => l.startsWith('data:'))
            .map((l) => l.slice(l.startsWith('data: ') ? 6 : 5))
            .join('\n');

    try {
        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const blocks = buffer.split(/\r?\n\r?\n/);
            buffer = blocks.pop();  // retain the trailing (possibly partial) frame
            for (const block of blocks) {
                const data = payloadOf(block);
                if (!data) continue;
                if (data === '[DONE]') return;
                try {
                    yield JSON.parse(data);
                } catch (e) {
                    console.warn('[CHERQAiN] Skipped malformed SSE frame:', data.slice(0, 120));
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
        if (tail === '[DONE]') return;
        if (tail) {
            try { yield JSON.parse(tail); } catch (e) { /* ignore trailing partial */ }
        }
        if (requireDone) throw new Error('Response interrupted: stream ended without its completion marker.');
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
    console.error(`[CHERQAiN] ${msg}`, error);
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
    // Contract (2026-07-20 audit A2/A3): onError fires AT MOST ONCE, and it is
    // terminal. Two prior bugs lived here — (1) a caller whose onError THROWS
    // (vault/plainchat do, to reach their own catch) had onError called again
    // from the catch with exc.message, i.e. twice; (2) an aborted fetch throws
    // a DOMException named 'AbortError', but routing it through
    // onError(exc.message) flattened it into a plain Error, so the callers'
    // `e.name === 'AbortError'` friendly-timeout branch was DEAD code.
    let onErrorCalled = false;
    // Opt-in for chat backstops. The server owns provider/retry policy; the
    // browser only adds a transport margin to its advertised silence window.
    // Transport completion is mandatory unless the caller explicitly validates
    // its business terminal (Library and Notes Sync preserve that contract).
    const { serverBudget, requireDone = true, ...fetchOptions } = options;
    const budgetController = serverBudget ? new AbortController() : null;
    let budgetTimer;
    let silenceMs = 360000; // bounded handshake / legacy-server fallback
    const rearm = () => {
        if (!budgetController) return;
        clearTimeout(budgetTimer);
        budgetTimer = setTimeout(() => budgetController.abort(), silenceMs);
    };
    const forwardCancel = () => budgetController?.abort();
    if (budgetController) {
        options.signal?.addEventListener('abort', forwardCancel, { once: true });
        if (options.signal?.aborted) forwardCancel();
        fetchOptions.signal = budgetController.signal;
        rearm();
    }
    try {
        const resp = await secureFetch(url, fetchOptions);
        if (budgetController) {
            const seconds = Number(resp.headers?.get?.('X-PaperMind-SSE-Silence-S'));
            if (Number.isFinite(seconds) && seconds > 0 && seconds <= 86400) {
                silenceMs = (seconds + 30) * 1000;
            }
            rearm();
        }
        if (!resp.ok) {
            const result = await safeJson(resp);
            const err = result.error || `HTTP ${resp.status} - failed to start stream`;
            onErrorCalled = true;
            // Second arg = the full error payload (additive, 2026-07-23):
            // structured fields like `code` let callers branch on machine
            // identity instead of regexing the human message.
            handlers.onError?.(err, result);   // may throw; propagates as the caller intends
            return;
        }
        // The server accepted the request and the stream is open (additive,
        // 2026-09-23): a caller can tell a request REFUSED before anything
        // ran (onError without onOpen) from one that failed mid-run.
        handlers.onOpen?.();

        for await (const frame of readSSE(resp, { requireDone })) {
            rearm();
            if (frame.error) {
                onErrorCalled = true;
                handlers.onError?.(frame.error, frame);
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
        // If onError already ran, THIS exception is its own throw (or a
        // downstream effect) — propagate it to the caller's catch untouched;
        // never invoke onError a second time.
        if (onErrorCalled) throw exc;
        // Preserve abort identity so the caller's `e.name === 'AbortError'`
        // branch is reachable — re-throw the original DOMException rather than
        // flatten it through onError.
        if (exc && exc.name === 'AbortError') throw exc;
        handlers.onError?.(exc.message || String(exc));
    } finally {
        clearTimeout(budgetTimer);
        options.signal?.removeEventListener('abort', forwardCancel);
    }
}
