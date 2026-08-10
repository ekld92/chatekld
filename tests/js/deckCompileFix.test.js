// Regression test for deck.js::runCompileFix (improvement plan 2026-07-04,
// item 1.1). The defect: readSSE was called as readSSE(url, options, cb) —
// but it is an async generator over an already-fetched Response, so NO
// network request was ever issued and the Compile & Auto-Fix button silently
// completed without doing anything. Invariant pinned here: clicking the
// button issues exactly one POST to /api/deck/compile-fix with the documented
// body, consumes the SSE stream, applies the terminal {compile} frame
// (sha holder sync + button state), and surfaces a non-SSE refusal (409).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { runCompileFix } from '../../static/js/deck.js';

const enc = new TextEncoder();

/** A fetch()-Response stand-in whose body streams the given SSE frames. */
function sseOk(frames) {
    const payload = frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('') + 'data: [DONE]\n\n';
    let sent = false;
    return {
        ok: true,
        status: 200,
        body: {
            getReader: () => ({
                read: async () =>
                    sent ? { value: undefined, done: true } : ((sent = true), { value: enc.encode(payload), done: false }),
                releaseLock: () => {},
            }),
        },
    };
}

describe('runCompileFix (Track 1.1)', () => {
    let fetchMock;
    beforeEach(() => {
        fetchMock = vi.fn();
        vi.stubGlobal('fetch', fetchMock);
    });
    afterEach(() => vi.unstubAllGlobals());

    it('actually issues the POST and applies the terminal compile frame', async () => {
        fetchMock.mockResolvedValue(sseOk([
            { info: 'latexmk pass 1' },
            { compile: { success: true, iterations: 1, changed: true, tex_sha256: 'newsha' } },
        ]));
        const btn = document.createElement('button');
        const shaRef = { sha: 'oldsha' };

        await runCompileFix('/tmp/deck/deck.tex', shaRef, btn);

        // The 1.1 bug: fetch was NEVER called. Pin that the request goes out,
        // once, to the right endpoint, with the documented body + CSRF header.
        expect(fetchMock).toHaveBeenCalledTimes(1);
        const [url, opts] = fetchMock.mock.calls[0];
        expect(url).toBe('/api/deck/compile-fix');
        expect(opts.method).toBe('POST');
        expect(opts.headers['X-Requested-With']).toBe('ChatEKLD');
        expect(JSON.parse(opts.body)).toEqual({
            deck_path: '/tmp/deck/deck.tex',
            base_sha256: 'oldsha',
            confirm: true,
        });
        // Terminal frame consumed: sha holder synced (re-run must not 409),
        // button settled into its success state and re-enabled.
        expect(shaRef.sha).toBe('newsha');
        expect(btn.textContent).toBe('Compiled ✓');
        expect(btn.disabled).toBe(false);
    });

    it('surfaces a non-SSE refusal (409) without touching the sha holder', async () => {
        fetchMock.mockResolvedValue({
            ok: false,
            status: 409,
            json: async () => ({ error: 'Another deck operation is already running.' }),
        });
        const btn = document.createElement('button');
        const shaRef = { sha: 'oldsha' };

        await runCompileFix('/tmp/deck/deck.tex', shaRef, btn);

        expect(fetchMock).toHaveBeenCalledTimes(1);
        expect(shaRef.sha).toBe('oldsha');
        expect(btn.disabled).toBe(false); // button recovers for a retry
    });

    it('recovers (button re-enabled, no throw) when fetch itself rejects', async () => {
        fetchMock.mockRejectedValue(new TypeError('network down'));
        const btn = document.createElement('button');

        await expect(runCompileFix('/tmp/d.tex', { sha: 's' }, btn)).resolves.toBeUndefined();
        expect(btn.disabled).toBe(false);
    });
});

// ---------------------------------------------------------------------------
// P6.2 (2026-07-30): client-side abort for the two LONGEST LaTeX operations.
// Generate/Augment had Cancel since their SSE wiring landed; compile-fix and
// preview did not, so a user watching a doomed repair loop could only wait or
// reload. Source ratchets (the functions are DOM+SSE heavy; the invariant is
// "the request carries an abort signal and the finally tears the button down").
// ---------------------------------------------------------------------------
describe('compile-fix / preview abort wiring (P6.2)', () => {
    const fs = require('fs');
    const path = require('path');
    const read = (f) => fs.readFileSync(
        path.join(__dirname, '..', '..', 'static', 'js', f), 'utf8');

    for (const [file, api] of [['deck.js', 'deck'], ['docgen.js', 'docgen']]) {
        it(`${file}: compile-fix POSTs with an abort signal`, () => {
            const src = read(file);
            // Anchor on the URL LITERAL, not the bare word — the surrounding
            // comments mention the endpoint names too.
            const call = src.slice(src.indexOf(`'/api/${api}/compile-fix'`));
            // The signal must be inside the SAME fetch options object as the body.
            const opts = call.slice(0, call.indexOf('}, {'));
            expect(opts).toContain('signal: _abortCtrl.signal');
        });

        it(`${file}: compile-preview POSTs with an abort signal`, () => {
            const src = read(file);
            const call = src.slice(src.indexOf(`'/api/${api}/compile-preview'`));
            expect(call.slice(0, 400)).toContain('signal: _abortCtrl.signal');
        });

        it(`${file}: both paths mint a controller and a Cancel button`, () => {
            const src = read(file);
            // 5 ops each now: generate(+md for docgen)/augment/compile-fix/preview.
            const ctrls = src.match(/_abortCtrl = new AbortController\(\)/g) || [];
            expect(ctrls.length).toBeGreaterThanOrEqual(4);
            const cancels = src.match(/_mkCancelButton\(/g) || [];
            // one definition + one per op
            expect(cancels.length).toBeGreaterThanOrEqual(5);
        });

        it(`${file}: a cancelled run reports cancellation, not a crash`, () => {
            const src = read(file);
            expect(src).toContain('Compile & auto-fix cancelled.');
            expect(src).toContain('the server compile finishes on its own');
        });
    }
});
