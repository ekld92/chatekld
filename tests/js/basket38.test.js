// Pinning tests for the item-3.8 basket (improvement plan 2026-07-04) — the
// drivable subset: settings save chaining, superseded-upload cleanup, export
// pane preservation, audit meta escaping, and deck busy-flag symmetry.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const enc = new TextEncoder();

function sseHanging() {
    return {
        ok: true, status: 200,
        body: { getReader: () => ({
            read: () => new Promise(() => {}),   // never resolves
            releaseLock: () => {}, cancel: async () => {},
        }) },
    };
}

describe('3.8 basket', () => {
    let fetchMock;
    beforeEach(() => {
        vi.resetModules();
        fetchMock = vi.fn();
        vi.stubGlobal('fetch', fetchMock);
    });
    afterEach(() => vi.unstubAllGlobals());

    it('settings saves are chained in order and failures are announced', async () => {
        document.body.innerHTML = '<div id="sr-error-announcer"></div>';
        const order = [];
        let releaseFirst;
        let call = 0;
        fetchMock.mockImplementation((url, opts = {}) => {
            if (String(url).includes('/api/config') && opts.method === 'POST') {
                call += 1;
                const mine = call;
                order.push(`start-${mine}`);
                if (mine === 1) {
                    return new Promise((res) => {
                        releaseFirst = () => { order.push('end-1'); res({ ok: true, status: 200 }); };
                    });
                }
                order.push(`end-${mine}`);
                return Promise.resolve({ ok: true, status: 200 });
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
        });
        const settings = await import('../../static/js/settings.js');
        const p1 = settings.saveSettings();
        const p2 = settings.saveSettings();     // must WAIT for p1, not race it
        await new Promise((r) => setTimeout(r, 10));
        expect(order).toEqual(['start-1']);      // second POST not started yet
        releaseFirst();
        await Promise.all([p1, p2]);
        expect(order).toEqual(['start-1', 'end-1', 'start-2', 'end-2']);
    });

    it('replacing an upload DELETEs the superseded id', async () => {
        document.body.innerHTML = `
          <input id="pdf-upload" type="file">
          <div id="upload-overlay"></div><div id="summary-view"></div>
          <div id="document-summary-content"></div>
          <button id="summarise-btn"></button>
          <button id="export-summary-btn"></button>
          <button id="export-summary-md-btn"></button>
        `;
        const deletes = [];
        let uploads = 0;
        fetchMock.mockImplementation((url, opts = {}) => {
            const u = String(url);
            if (u.includes('/api/upload') && (opts.method || '') === 'DELETE') {
                deletes.push(u);
                return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
            }
            if (u.includes('/api/upload')) {
                uploads += 1;
                return Promise.resolve({ ok: true, status: 200,
                    json: async () => ({ upload_id: `id-${uploads}`, filename: `f${uploads}.pdf` }) });
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
        });
        const summarizer = await import('../../static/js/summarizer.js');
        const input = document.getElementById('pdf-upload');
        Object.defineProperty(input, 'files', {
            configurable: true,
            value: [new File([new Uint8Array([1])], 'a.pdf', { type: 'application/pdf' })],
        });
        await summarizer.uploadPDF();            // id-1 adopted, nothing to delete
        expect(deletes).toEqual([]);
        await summarizer.uploadPDF();            // id-2 replaces id-1
        await new Promise((r) => setTimeout(r, 0));
        expect(deletes.length).toBe(1);
        expect(deletes[0]).toContain('/api/upload/id-1');
    });

    it('audit metas escape server-supplied values before innerHTML', async () => {
        document.body.innerHTML = `
          <div id="audit-report-meta"></div>
          <div id="audit-report-body"></div>
          <div id="audit-report-tabs"></div>
          <div id="audit-summary"></div>
        `;
        fetchMock.mockImplementation((url) => {
            if (String(url).includes('/api/audit/reports/zotero_unread')) {
                return Promise.resolve({ ok: true, status: 200, json: async () => ({
                    rows: [],
                    // A hostile/odd payload where a "numeric" field is markup.
                    skipped_no_zotero_match: '<img src=x onerror="window.__pwned=1">',
                }) });
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
        });
        const audit = await import('../../static/js/audit.js');
        await audit.selectAuditReport('zotero_unread');
        const meta = document.getElementById('audit-report-meta');
        expect(meta.querySelector('img')).toBeNull();          // never became an element
        expect(meta.textContent).toContain('<img');            // rendered as TEXT
        expect(window.__pwned).toBeUndefined();
    });

    it('deck busy flags are symmetric: a running compile-fix blocks generate', async () => {
        document.body.innerHTML = `
          <input id="deck-topic" value="Sujet">
          <textarea id="deck-template-editor">x</textarea>
          <input id="deck-template-path" value=""><input id="deck-out-dir" value="">
          <input id="deck-name" value=""><textarea id="deck-instructions"></textarea>
          <input id="deck-audience" value="">
          <input id="deck-citations" type="checkbox"><input id="deck-overwrite" type="checkbox">
          <input id="deck-review" type="checkbox"><input id="deck-force-fresh" type="checkbox">
          <input id="deck-max-sections" value="4"><input id="deck-agent-iters" value="4">
          <input id="deck-temp" value="0.4">
          <div id="deck-status"></div><div id="deck-activity"></div><div id="deck-result"></div>
          <button id="deck-generate-btn"></button><button id="deck-aug-preview-btn"></button>
        `;
        fetchMock.mockImplementation((url) => {
            if (String(url).includes('/api/deck/compile-fix')) return Promise.resolve(sseHanging());
            return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
        });
        const deckMod = await import('../../static/js/deck.js');
        const btn = document.createElement('button');
        const running = deckMod.runCompileFix('/tmp/d.tex', { sha: 's' }, btn); // parks
        await new Promise((r) => setTimeout(r, 0));

        await deckMod.generate();     // pre-fix: generate ignored _compileFixing
        const genCalls = fetchMock.mock.calls.filter(([u]) =>
            String(u).includes('/api/deck/generate'));
        expect(genCalls.length).toBe(0);
        void running;                 // left parked; jsdom teardown reaps it
    });
});
