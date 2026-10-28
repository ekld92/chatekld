// 2026-09-02 — the vault panel drained its own one-shot status feed twice.
//
// DEFECT PINNED: `GET /api/obsidian/status` destructively drains
// `_status_messages` — the server hands each message to exactly one reader.
// `pollVaultStatus` (2 s) and `pollPrewarmStatus` (1.5 s) both GET it, and the
// prewarm loop reads `prewarm_status`/`prewarm_message` and throws the rest of
// the payload away. While prewarm and indexing overlap — which is the normal
// case on a cold launch, since prewarm is what indexing competes with for the
// index load — roughly half the progress lines and any warning raised in that
// window went to the prewarm loop and were never rendered anywhere.
//
// The SwiftBar plugin carries a hard rule never to touch this endpoint for
// exactly this reason (CLAUDE.md, `widget/` bullet). The app's own panel broke
// it against itself.
//
// The invariant: never two concurrent readers of a draining endpoint. While a
// run is in flight the vault poller is the single reader and paints the prewarm
// banner from its own payload; the prewarm loop suspends and resumes when the
// run ends.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

function mountVaultDom() {
    document.body.innerHTML = `
      <div id="sr-status-announcer" role="status"></div>
      <div id="sr-error-announcer" role="alert"></div>
      <div id="obsidian-status-msg"></div>
      <div id="vault-error-boundary"></div>
      <div id="vault-index-progress" hidden></div>
      <div id="prewarm-banner" hidden></div>
      <button id="vault-send-btn"></button>
      <button id="vault-index-btn"></button>
      <button id="vault-pause-btn"></button>
      <button id="vault-resume-btn"></button>
      <button id="vault-cancel-btn"></button>
      <div id="activity-bar"><span id="activity-label-text"></span></div>
      <div id="materials-list-container"></div>
    `;
}

/**
 * A status endpoint that behaves like the real one: each GET DRAINS the pending
 * message list, so a message handed to one reader is gone for every other.
 */
function mockDrainingStatus(state) {
    return vi.fn((url, opts = {}) => {
        const u = String(url);
        if (u.includes('/api/obsidian/index')) {
            return Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true }) });
        }
        if (u.includes('/api/obsidian/status')) {
            state.statusPolls += 1;
            const drained = state.pending.splice(0);
            state.served.push(...drained);
            state.batches.push(drained);
            const next = state.payloads.length > 1
                ? state.payloads.shift() : state.payloads[0];
            return Promise.resolve({
                ok: true, status: 200,
                json: async () => ({ warnings: [], ...next, messages: drained }),
            });
        }
        if (u.includes('/api/obsidian/materials')) {
            return Promise.resolve({ ok: true, status: 200, json: async () => ({ materials: [] }) });
        }
        return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
    });
}

describe('one reader for the one-shot vault status feed (2026-09-02)', () => {
    let state;
    let vault;

    beforeEach(async () => {
        mountVaultDom();
        vi.resetModules();
        vi.useFakeTimers();
        state = { statusPolls: 0, pending: [], served: [], batches: [], payloads: [{}] };
        vi.stubGlobal('fetch', mockDrainingStatus(state));
        vault = await import('../../static/js/vault.js');
    });
    afterEach(() => {
        vi.useRealTimers();
        vi.unstubAllGlobals();
    });

    const warming = () => ({
        state: 'scanning',
        prewarm_status: 'loading_index',
        prewarm_message: 'Loading vault index…',
    });

    it('keeps Ask gated while the note-link graph is warming', async () => {
        state.payloads = [{ state: 'done', prewarm_status: 'building_wikilinks' }];
        vault.pollPrewarmStatus();
        await vi.advanceTimersByTimeAsync(0);
        const banner = document.getElementById('prewarm-banner');
        expect(banner.hidden).toBe(false);
        expect(banner.textContent).toContain('Preparing note-link graph');
        expect(document.getElementById('vault-send-btn').disabled).toBe(true);
        state.payloads = [{ state: 'done', prewarm_status: 'ready' }];
        await vi.advanceTimersByTimeAsync(1500);
        expect(banner.hidden).toBe(true);
        expect(document.getElementById('vault-send-btn').disabled).toBe(false);
    });

    it('while a run is in flight the prewarm loop adds no second GET', async () => {
        state.payloads = [warming()];
        await vault.indexVault();            // one immediate poll
        expect(state.statusPolls).toBe(1);

        vault.pollPrewarmStatus();           // prewarm overlaps the run
        await vi.advanceTimersByTimeAsync(6000);

        // The vault poller alone: t=0 (above) plus 2 s, 4 s, 6 s.
        expect(state.statusPolls).toBe(4);
    });

    it('no drained batch is lost to a reader that discards it', async () => {
        state.payloads = [warming()];
        await vault.indexVault();

        vault.pollPrewarmStatus();
        const statusEl = document.getElementById('obsidian-status-msg');

        // One message becomes available per second. The status line shows the
        // LAST of whatever batch a poll drained (by design — it is one line),
        // so the invariant is not "every message is rendered" but "every
        // non-empty batch went to the reader that renders one". Pre-fix the
        // prewarm loop's 1.5 s cadence took batches it then threw away.
        let renders = 0;
        let last = statusEl.textContent;
        for (let i = 1; i <= 6; i += 1) {
            state.pending.push(`Indexed note ${i}`);
            await vi.advanceTimersByTimeAsync(1000);
            if (statusEl.textContent !== last) {
                renders += 1;
                last = statusEl.textContent;
            }
        }
        const nonEmpty = state.batches.filter((b) => b.length > 0).length;
        expect(state.served).toEqual([1, 2, 3, 4, 5, 6].map((n) => `Indexed note ${n}`));
        expect(nonEmpty).toBeGreaterThan(0);
        expect(renders).toBe(nonEmpty);
        // …and the newest line is the one on screen.
        expect(last).toBe('Indexed note 6');
    });

    it('the prewarm banner is still painted while the prewarm loop is suspended',
       async () => {
        state.payloads = [warming()];
        await vault.indexVault();
        vault.pollPrewarmStatus();
        await vi.advanceTimersByTimeAsync(2100);

        const banner = document.getElementById('prewarm-banner');
        expect(banner.hidden).toBe(false);
        expect(banner.textContent).toContain('Loading vault index…');
        // Send stays gated while the index is still loading.
        expect(document.getElementById('vault-send-btn').disabled).toBe(true);
    });

    it('the prewarm loop resumes and settles once the run ends', async () => {
        state.payloads = [warming(), { state: 'done', prewarm_status: 'ready' }];
        await vault.indexVault();
        vault.pollPrewarmStatus();

        await vi.advanceTimersByTimeAsync(2100);   // run reaches `done`
        const pollsAtRunEnd = state.statusPolls;

        await vi.advanceTimersByTimeAsync(2000);   // prewarm loop takes over
        expect(state.statusPolls).toBeGreaterThan(pollsAtRunEnd);
        expect(document.getElementById('prewarm-banner').hidden).toBe(true);
        expect(document.getElementById('vault-send-btn').disabled).toBe(false);

        // …and stops, having seen a terminal prewarm status.
        const settled = state.statusPolls;
        await vi.advanceTimersByTimeAsync(5000);
        expect(state.statusPolls).toBe(settled);
    });
});
