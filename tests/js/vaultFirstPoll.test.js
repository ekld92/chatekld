// 2026-08-12 — "the vault index needs two clicks".
//
// DEFECT PINNED: `POST /api/obsidian/index` returned 200 before the run's state
// transition (which lived on the spawned daemon thread), and the client polls
// `/api/obsidian/status` with ZERO delay on that 200 — every later poll is on a
// 2 s timer, so the first one is the only racy one. That poll therefore read the
// PREVIOUS run's terminal state, served instantly because `get_status`'s lazy
// recovery materialises "done" into `_index_state`. The client flashed
// "Indexing complete.", stopped polling, restored the idle buttons — and the run
// it had just started carried on invisibly. Clicking Index again 503'd and
// `refreshIndexState` re-attached, which is why starting an index appeared to
// need two clicks.
//
// The server-side half (the route publishes the state before its 200) is pinned
// by smoke_test.py::test_30d. This file pins the client's run-started latch:
// a terminal state seen before any running state is the previous run's
// leftover, so it must not end this run's lifecycle — bounded, so a run that
// really did fail before the first poll still terminates.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

function mountVaultDom() {
    document.body.innerHTML = `
      <div id="sr-status-announcer" role="status"></div>
      <div id="sr-error-announcer" role="alert"></div>
      <div id="obsidian-status-msg"></div>
      <div id="vault-error-boundary"></div>
      <div id="vault-index-progress" hidden></div>
      <button id="vault-index-btn"></button>
      <button id="vault-pause-btn"></button>
      <button id="vault-resume-btn"></button>
      <button id="vault-cancel-btn"></button>
      <div id="activity-bar"><span id="activity-label-text"></span></div>
      <div id="materials-list-container"></div>
    `;
}

/**
 * Mock fetch driven by a queue of status payloads: each poll shifts one, and
 * the last is repeated once the queue drains. Terminal handling is observed
 * through the `done` branch's side effects — it fetches /api/obsidian/materials
 * and flashes the sidebar — never through internal flags.
 */
function mockVaultFetch(state) {
    return vi.fn((url, opts = {}) => {
        const u = String(url);
        if (u.includes('/api/obsidian/index')) {
            state.indexPosts.push(JSON.parse(opts.body));
            return Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true }) });
        }
        if (u.includes('/api/obsidian/status')) {
            state.statusPolls += 1;
            const next = state.payloads.length > 1
                ? state.payloads.shift() : state.payloads[0];
            return Promise.resolve({
                ok: true, status: 200,
                json: async () => ({ messages: [], warnings: [], ...next }),
            });
        }
        if (u.includes('/api/obsidian/materials')) {
            state.materialsFetched += 1;
            return Promise.resolve({ ok: true, status: 200, json: async () => ({ materials: [] }) });
        }
        return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
    });
}

describe('vault index first-poll latch (2026-08-12)', () => {
    let state;
    let vault;

    beforeEach(async () => {
        mountVaultDom();
        vi.resetModules();
        vi.useFakeTimers();
        state = { indexPosts: [], payloads: [{}], statusPolls: 0, materialsFetched: 0 };
        vi.stubGlobal('fetch', mockVaultFetch(state));
        vault = await import('../../static/js/vault.js');
    });
    afterEach(() => {
        vi.useRealTimers();
        vi.unstubAllGlobals();
    });

    const completed = () => state.materialsFetched > 0
        || document.getElementById('activity-label-text').textContent
            .includes('Indexing complete');

    it('a stale "done" on the first poll does not end the run', async () => {
        // The leftover terminal state, then the run this click actually started.
        state.payloads = [{ state: 'done' }, { state: 'scanning' }];
        await vault.indexVault();

        expect(state.statusPolls).toBe(1);
        expect(completed()).toBe(false);
        expect(document.getElementById('vault-index-progress').hidden).toBe(false);

        // It keeps polling, and the real state arrives.
        await vi.advanceTimersByTimeAsync(500);
        expect(state.statusPolls).toBe(2);
        expect(completed()).toBe(false);
        // Running ⇒ Cancel offered, Index hidden (the pre-fix UI restored idle
        // buttons here, leaving a live run with no way to cancel it).
        expect(document.getElementById('vault-cancel-btn').style.display).toBe('');
        expect(document.getElementById('vault-index-btn').style.display).toBe('none');
    });

    it('a "done" that follows an observed run does terminate normally', async () => {
        state.payloads = [{ state: 'scanning' }, { state: 'done' }];
        await vault.indexVault();
        expect(completed()).toBe(false);

        await vi.advanceTimersByTimeAsync(2100);   // steady-state poll interval
        await vi.waitFor(() => expect(completed()).toBe(true));
        expect(document.getElementById('vault-index-progress').hidden).toBe(true);
    });

    it('a stale "error" on the first poll does not render the error boundary', async () => {
        state.payloads = [
            { state: 'error', error_code: 'index_failed', error_detail: 'ERROR: last run blew up' },
            { state: 'scanning' },
        ];
        await vault.indexVault();

        expect(document.getElementById('vault-error-boundary').textContent).toBe('');
        await vi.advanceTimersByTimeAsync(500);
        expect(document.getElementById('vault-error-boundary').textContent).toBe('');
        expect(state.statusPolls).toBe(2);
    });

    it('the allowance is bounded — a persistent terminal state still terminates', async () => {
        // Nothing ever reports running (a run that really did fail before the
        // first poll, or a server whose state never advances).
        state.payloads = [{ state: 'done' }];
        await vault.indexVault();
        expect(completed()).toBe(false);

        await vi.advanceTimersByTimeAsync(5000);
        await vi.waitFor(() => expect(completed()).toBe(true));
        // 5 tolerated pre-start polls + the one that falls through.
        expect(state.statusPolls).toBe(6);
    });

    it('re-attaching to a run in flight does not spend the allowance', async () => {
        // The 503 path (a second click while the first run is live) hands off to
        // refreshIndexState, which arms the poll loop on a state it has already
        // seen running — so the very next terminal state is this run's outcome.
        // refreshIndexState's own GET sees `scanning` and arms the loop, whose
        // first poll is immediate — so `done` lands on the very next poll and
        // must be honoured at once. Without the latch being pre-set in that
        // re-arm branch it would be deferred as a suspected leftover, and
        // completed() would stay false here.
        state.payloads = [{ state: 'scanning' }, { state: 'done' }];
        await vault.refreshIndexState();
        expect(state.statusPolls).toBe(2);

        await vi.waitFor(() => expect(completed()).toBe(true));
    });
});
