// Pinning test for the refactor post-apply pane refresh (improvement plan
// 2026-07-04, item 3.4, refactor half). The defect: after Apply, the note
// objects kept their PRE-apply original/proposed bodies, so the detail pane
// went on showing a diff for a note whose on-disk content had just become the
// proposed body. Invariant: the selected applied note re-analyzes via
// /api/refactor/note immediately, and any other applied note re-analyzes
// (once) when it is next selected.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const enc = new TextEncoder();

function mountRefactorDom() {
    document.body.innerHTML = `
      <button id="refactor-run-btn"></button>
      <input id="refactor-scope" value="study_notes">
      <input id="refactor-strip-preamble" type="checkbox">
      <div id="refactor-status"></div>
      <div id="refactor-activity"></div>
      <div id="refactor-note-list"></div>
      <div id="refactor-detail"></div>
      <div id="refactor-discrepancies"></div>
      <div id="refactor-summary" style="display:none"></div>
      <button id="refactor-apply-btn"></button>
      <button id="refactor-normalize-btn"></button>
      <div id="refactor-apply-list"></div>
      <div id="refactor-apply-modal-status"></div>
      <button id="refactor-apply-confirm"></button>
      <div id="refactor-apply-modal"></div>
    `;
}

function noteFrame(rel) {
    return {
        rel_path: rel, changed: true,
        original: `# ${rel}\nold body`, proposed: `# ${rel}\nnew body`,
        diff: `--- ${rel}\n+++ ${rel}\n+new body`,
        content_sha256: `content-${rel}`, proposed_sha256: `proposed-${rel}`,
        normalize_changed: false, normalized: '', normalized_sha256: '',
        normalize_diff: '', images: [], hygiene_notes: [],
    };
}

function sseOnce(frames) {
    const payload =
        frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('') + 'data: [DONE]\n\n';
    let sent = false;
    return {
        ok: true,
        status: 200,
        body: {
            getReader: () => ({
                read: async () =>
                    sent
                        ? { value: undefined, done: true }
                        : ((sent = true), { value: enc.encode(payload), done: false }),
                releaseLock: () => {},
                cancel: async () => {},
            }),
        },
    };
}

describe('refactor post-apply pane refresh (Track 3.4)', () => {
    let fetchMock;
    beforeEach(() => {
        mountRefactorDom();
        vi.resetModules();
        fetchMock = vi.fn();
        vi.stubGlobal('fetch', fetchMock);
    });
    afterEach(() => vi.unstubAllGlobals());

    it('re-analyzes the selected note after apply, and others on selection', async () => {
        const reanalyzed = [];
        fetchMock.mockImplementation((url, opts = {}) => {
            const u = String(url);
            if (u.includes('/api/refactor/plan')) {
                return Promise.resolve(sseOnce([
                    { note: noteFrame('study_notes/a.md') },
                    { note: noteFrame('study_notes/b.md') },
                    { refactor: { counts: {}, discrepancies: [] } },
                ]));
            }
            if (u.includes('/api/refactor/apply')) {
                return Promise.resolve({
                    ok: true, status: 200,
                    json: async () => ({ ok: true, results: [
                        { rel: 'study_notes/a.md', status: 'applied' },
                        { rel: 'study_notes/b.md', status: 'applied' },
                    ] }),
                });
            }
            if (u.includes('/api/refactor/note')) {
                const body = JSON.parse(opts.body);
                reanalyzed.push(body.rel);
                const fresh = noteFrame(body.rel);
                fresh.changed = false;
                fresh.original = fresh.proposed;    // on-disk truth after apply
                return Promise.resolve({ ok: true, status: 200,
                                         json: async () => ({ ok: true, note: fresh }) });
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
        });

        const refactor = await import('../../static/js/refactor.js');
        await refactor.runPlan();

        const entries = [...document.querySelectorAll('.refactor-note-entry')];
        expect(entries.length).toBe(2);
        // Select note a, approve both.
        entries[0].click();
        await new Promise((r) => setTimeout(r, 0));
        document.querySelectorAll('.refactor-approve-cb').forEach((cb) => {
            if (!cb.checked) cb.click();
        });
        reanalyzed.length = 0;   // ignore any selection-triggered traffic so far

        await refactor.confirmApply();
        await new Promise((r) => setTimeout(r, 5));
        // Selected note (a) refreshed immediately.
        expect(reanalyzed).toContain('study_notes/a.md');
        expect(reanalyzed).not.toContain('study_notes/b.md');

        // Selecting the other applied note triggers its one lazy refresh…
        reanalyzed.length = 0;
        entries[1].click();
        await new Promise((r) => setTimeout(r, 5));
        expect(reanalyzed).toEqual(['study_notes/b.md']);

        // …exactly once: re-selecting it again does not re-fetch.
        reanalyzed.length = 0;
        entries[0].click();
        await new Promise((r) => setTimeout(r, 5));
        entries[1].click();
        await new Promise((r) => setTimeout(r, 5));
        expect(reanalyzed).toEqual([]);
    });
});
