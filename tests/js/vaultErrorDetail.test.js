// 2026-07-29 audit 0.1 — the erased refusal.
//
// DEFECT PINNED: the status poll's `state === 'error'` branch blanked the
// status line (which had just received the server's ONE copy of the reason via
// the destructive `messages` drain) and rendered a generic "Indexing failed."
// with a Retry button. For the embed-backend gate — a REFUSAL whose entire
// value is its recovery instructions — the user saw no reason and a Retry that
// re-ran the refusing gate identically, forever.
//
// Invariants: (1) the boundary renders the server's persistent `error_detail`;
// (2) an `embed_backend_mismatch` error offers a confirm-gated "Rebuild from
// scratch" and NO Retry; (3) the confirmed rebuild POSTs `force_fresh: true`
// (and only the confirmed rebuild — a plain indexVault() sends false); (4) any
// other error code keeps the Retry affordance.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

function mountVaultDom() {
    document.body.innerHTML = `
      <div id="sr-status-announcer" role="status"></div>
      <div id="sr-error-announcer" role="alert"></div>
      <div id="obsidian-status-msg"></div>
      <div id="vault-error-boundary"></div>
      <input id="obsidian-input" value="">
      <div id="obsidian-chat"></div>
      <div id="vault-index-progress" hidden></div>
    `;
}

const REFUSAL =
    "ERROR: the existing index was embedded through 'ollama', but embeddings "
    + "would now come from 'lm_studio'. Refusing to touch the index.";

/**
 * Mock fetch: index POSTs are recorded (bodies parsed); the first status poll
 * answers `scanning`, the second with `statusPayload`, later polls park forever
 * so the test stays in control of the poll loop.
 *
 * The `scanning` poll is not scaffolding — it is what the real server returns
 * now that `POST /api/obsidian/index` publishes the run state before its 200
 * (2026-08-12). The client's run-started latch deliberately ignores a terminal
 * state seen before any running state, because that is the PREVIOUS run's
 * leftover; serving the error on poll #1 would exercise a shape the server can
 * no longer produce.
 */
function mockVaultFetch(state) {
    let statusPolls = 0;
    return vi.fn((url, opts = {}) => {
        const u = String(url);
        if (u.includes('/api/obsidian/index')) {
            state.indexPosts.push(JSON.parse(opts.body));
            return Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true }) });
        }
        if (u.includes('/api/obsidian/status')) {
            statusPolls += 1;
            if (statusPolls === 1) {
                return Promise.resolve({
                    ok: true, status: 200,
                    json: async () => ({ state: 'scanning', messages: [], warnings: [] }),
                });
            }
            if (statusPolls === 2) {
                return Promise.resolve({
                    ok: true, status: 200, json: async () => state.statusPayload,
                });
            }
            return new Promise(() => {});   // park later polls
        }
        return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
    });
}

describe('vault indexing error detail (2026-07-29)', () => {
    let state;
    let vault;
    beforeEach(async () => {
        mountVaultDom();
        vi.resetModules();
        vi.useFakeTimers();
        state = { indexPosts: [], statusPayload: {} };
        vi.stubGlobal('fetch', mockVaultFetch(state));
        vault = await import('../../static/js/vault.js');
    });
    afterEach(() => {
        vi.useRealTimers();
        vi.unstubAllGlobals();
    });

    async function runToError(statusPayload) {
        state.statusPayload = { messages: [], warnings: [], ...statusPayload };
        await vault.indexVault();          // poll #1 → scanning (run observed)
        // Poll #2 carries the payload, one steady-state poll interval later.
        // Advanced explicitly rather than waited out, so the file stays fast
        // and does not depend on the interval's exact value.
        await vi.advanceTimersByTimeAsync(2100);
        await vi.waitFor(() => {
            expect(document.getElementById('vault-error-boundary').textContent)
                .not.toBe('');
        });
    }

    it('renders the persistent error_detail, not a generic message', async () => {
        await runToError({ state: 'error', error_code: 'embed_backend_mismatch', error_detail: REFUSAL });
        const boundary = document.getElementById('vault-error-boundary');
        expect(boundary.textContent).toContain('embedded through');
        expect(boundary.textContent).toContain('lm_studio');
    });

    it('embed_backend_mismatch offers a confirm-gated Rebuild and no Retry', async () => {
        await runToError({ state: 'error', error_code: 'embed_backend_mismatch', error_detail: REFUSAL });
        const buttons = [...document.querySelectorAll('#vault-error-boundary button')];
        const labels = buttons.map((b) => b.textContent);
        expect(labels.some((l) => l.includes('Rebuild from scratch'))).toBe(true);
        expect(labels).not.toContain('Retry');

        // Click Rebuild → the confirmInline ceremony appears; nothing POSTed yet.
        buttons.find((b) => b.textContent.includes('Rebuild from scratch')).click();
        const strip = document.querySelector('.inline-confirm-strip');
        expect(strip).toBeTruthy();
        expect(state.indexPosts.length).toBe(1);   // only the original run

        // Confirm → indexVault(true) → POST carries force_fresh: true.
        const confirmBtn = [...strip.querySelectorAll('button')]
            .find((b) => b.textContent === 'Rebuild');
        expect(confirmBtn).toBeTruthy();
        confirmBtn.click();
        await vi.waitFor(() => expect(state.indexPosts.length).toBe(2));
        expect(state.indexPosts[1].force_fresh).toBe(true);
        // …and the plain first run did NOT (strict-boolean guard).
        expect(state.indexPosts[0].force_fresh).toBe(false);
    });

    it('any other error code keeps Retry and falls back to a generic message', async () => {
        await runToError({ state: 'error', error_code: 'index_failed', error_detail: 'ERROR: Indexing failed: boom' });
        const labels = [...document.querySelectorAll('#vault-error-boundary button')]
            .map((b) => b.textContent);
        expect(labels).toContain('Retry');
        expect(labels.some((l) => l.includes('Rebuild from scratch'))).toBe(false);
        expect(document.getElementById('vault-error-boundary').textContent)
            .toContain('boom');
    });

    it('a payload with no detail degrades to the generic message + Retry', async () => {
        await runToError({ state: 'error' });
        const boundary = document.getElementById('vault-error-boundary');
        expect(boundary.textContent).toContain('Indexing failed.');
        const labels = [...boundary.querySelectorAll('button')].map((b) => b.textContent);
        expect(labels).toContain('Retry');
    });
});
