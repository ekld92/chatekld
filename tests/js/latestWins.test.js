// Pinning tests for the latest-wins request discipline (improvement plan
// 2026-07-04, item 3.3). One helper (ui.js::makeLatestGate) fixes four races
// that shared a root cause: overlapping async UI flows with no staleness
// discipline. Pinned here: the gate's contract, the audit report switcher
// (slower response must NOT render last), and the summarizer's
// abort-controller identity (an old run's finally must not clobber the new
// run's controller — else a third run can no longer abort the second).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

describe('makeLatestGate (Track 3.3)', () => {
    it('only the newest entrant stays current', async () => {
        const { makeLatestGate } = await import('../../static/js/ui.js');
        const gate = makeLatestGate();
        const a = gate.enter();
        expect(a()).toBe(true);
        const b = gate.enter();
        expect(a()).toBe(false);      // superseded
        expect(b()).toBe(true);
        const c = gate.enter();
        expect(b()).toBe(false);
        expect(c()).toBe(true);
    });
});

describe('audit report switcher race (Track 3.3)', () => {
    beforeEach(() => {
        document.body.innerHTML = `
          <div id="audit-report-meta"></div>
          <div id="audit-report-body"></div>
          <div id="audit-report-tabs"></div>
          <div id="audit-summary"></div>
        `;
        vi.resetModules();
    });
    afterEach(() => vi.unstubAllGlobals());

    it('the slower superseded report never overwrites the newer one', async () => {
        let releaseSlow;
        const fetchMock = vi.fn((url) => {
            const u = String(url);
            if (u.includes('/api/audit/reports/note_tag_drift')) {
                // Slow: resolves only after the second report already rendered.
                return new Promise((res) => {
                    releaseSlow = () => res({
                        ok: true, status: 200,
                        json: async () => ({ rows: [] }),
                    });
                });
            }
            if (u.includes('/api/audit/reports/zotero_unread')) {
                return Promise.resolve({
                    ok: true, status: 200,
                    json: async () => ({ rows: [], skipped_no_zotero_match: 0 }),
                });
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
        });
        vi.stubGlobal('fetch', fetchMock);
        const audit = await import('../../static/js/audit.js');

        const slow = audit.selectAuditReport('note_tag_drift'); // parks
        await new Promise((r) => setTimeout(r, 0));
        await audit.selectAuditReport('zotero_unread');          // completes
        const after = document.getElementById('audit-report-meta').textContent;
        expect(after).toContain('bib entries');                  // fresh report

        releaseSlow();                                           // stale resumes late
        await slow;
        // Pre-fix, the slow response rendered last: meta flipped back to the
        // note_tag_drift text under the zotero_unread tab.
        expect(document.getElementById('audit-report-meta').textContent)
            .toContain('bib entries');
        expect(document.getElementById('audit-report-meta').textContent)
            .not.toContain('citation keys');
    });
});

describe('summarizer abort-controller identity (Track 3.3)', () => {
    beforeEach(() => {
        document.body.innerHTML = `
          <input id="pdf-upload" type="file">
          <div id="upload-overlay"></div>
          <div id="summary-view"></div>
          <div id="document-summary-content"></div>
          <div id="doc-error-boundary"></div>
          <button id="summarise-btn"></button>
          <button id="export-summary-btn"></button>
          <button id="export-summary-md-btn"></button>
          <select id="model-select"><option value="m">m</option></select>
          <select id="preset-select"><option value="p">p</option></select>
          <select id="report-type-select"><option value="r">r</option></select>
          <select id="audience-select"><option value="a">a</option></select>
          <select id="language-select"><option value="fr">fr</option></select>
          <input id="doc-focus-question" value="">
          <input id="doc-temp" value="0.3">
          <input id="doc-predict" value="4096">
          <input id="doc-ctx" value="32768">
          <input id="doc-top-p" value="0.9">
          <input id="doc-repeat-penalty" value="1.1">
          <textarea id="doc-system-prompt"></textarea>
        `;
        vi.resetModules();
    });
    afterEach(() => vi.unstubAllGlobals());

    it("an old run's finally does not clobber the new run's controller", async () => {
        const signals = [];   // one entry per /api/summarise call
        const fetchMock = vi.fn((url, opts = {}) => {
            const u = String(url);
            if (u.includes('/api/upload')) {
                return Promise.resolve({
                    ok: true, status: 200,
                    json: async () => ({ upload_id: 'u1', filename: 'f.pdf' }),
                });
            }
            if (u.includes('/api/summarise')) {
                signals.push(opts.signal);
                // Hang until aborted; reject with AbortError like real fetch.
                return new Promise((_res, rej) => {
                    opts.signal.addEventListener('abort', () =>
                        rej(Object.assign(new Error('aborted'), { name: 'AbortError' })));
                });
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
        });
        vi.stubGlobal('fetch', fetchMock);
        const summarizer = await import('../../static/js/summarizer.js');

        // Seed the upload id through the real upload path.
        const input = document.getElementById('pdf-upload');
        Object.defineProperty(input, 'files', {
            value: [new File([new Uint8Array([1])], 'f.pdf', { type: 'application/pdf' })],
        });
        await summarizer.uploadPDF();

        const runA = summarizer.summarisePDF();   // parks on its fetch
        await new Promise((r) => setTimeout(r, 0));
        const runB = summarizer.summarisePDF();   // aborts A, parks
        await new Promise((r) => setTimeout(r, 0));
        await runA;                               // A's finally has now run
        expect(signals[0].aborted).toBe(true);

        const runC = summarizer.summarisePDF();   // must be able to abort B
        await new Promise((r) => setTimeout(r, 0));
        // Pre-fix, A's finally nulled the module field, so C found no
        // controller to abort and B kept streaming alongside C.
        expect(signals[1].aborted).toBe(true);

        // Cleanup: end C.
        const summariseCalls = fetchMock.mock.calls.filter(([u]) =>
            String(u).includes('/api/summarise'));
        expect(summariseCalls.length).toBe(3);
        signals[2].dispatchEvent(new Event('abort'));   // settle C's promise
        await runB;
        await runC.catch(() => {});
    });
});
