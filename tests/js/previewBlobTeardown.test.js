// 2026-09-02 — PDF preview blobs pinned for the process lifetime.
//
// DEFECT PINNED: deck.js and docgen.js revoked the previous preview's object
// URL only at the moment the NEXT one landed. Every path that clears the host
// without producing a new blob — a compile that fails, a Cancel, a PDF fetch
// that errors — therefore removed the <iframe> referencing it and left the
// multi-megabyte blob alive with nothing pointing at it, for the rest of the
// process. Likewise tagging.js's `V` panel: switching the triage deck to the
// table view destroys the panel markup through _renderView without ever
// calling _deckClosePdf.
//
// The invariant: a preview blob is revoked whenever the element referencing it
// is destroyed. Retention while the iframe is still mounted (a tab switch
// hides the panel but keeps it in the DOM) is NOT a leak — the memory backs
// something the user can come back to — and is deliberately left alone; see
// CLAUDE.md Known Issues.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

function jsonOk(payload) {
    return { ok: true, status: 200, json: async () => payload };
}
function pdfOk() {
    return {
        ok: true, status: 200,
        blob: async () => new Blob(['%PDF-1.4 fake'], { type: 'application/pdf' }),
        json: async () => ({}),
    };
}

describe('preview blob teardown (2026-09-02)', () => {
    let fetchMock;
    let revoked;
    let created;

    beforeEach(() => {
        document.body.innerHTML = '';
        fetchMock = vi.fn();
        vi.stubGlobal('fetch', fetchMock);
        revoked = [];
        created = 0;
        if (!URL.createObjectURL) URL.createObjectURL = () => {};
        if (!URL.revokeObjectURL) URL.revokeObjectURL = () => {};
        vi.spyOn(URL, 'createObjectURL').mockImplementation(() => {
            created += 1;
            return `blob:pdf-${created}`;
        });
        vi.spyOn(URL, 'revokeObjectURL').mockImplementation((u) => revoked.push(u));
    });
    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    for (const mod of ['deck', 'docgen']) {
        const entry = mod === 'deck' ? 'runDeckPreview' : 'runDocPreview';

        it(`${mod}.js releases the previous blob when a later compile fails`,
           async () => {
            vi.resetModules();
            const m = await import(`../../static/js/${mod}.js`);
            const btn = document.createElement('button');
            const host = document.createElement('div');
            document.body.appendChild(host);

            // 1. A preview lands.
            fetchMock
                .mockResolvedValueOnce(jsonOk({ success: true, has_pdf: true, log_excerpt: '' }))
                .mockResolvedValueOnce(pdfOk());
            await m[entry]('/tmp/d/d.tex', btn, host);
            expect(host.querySelector('iframe').src).toContain('blob:pdf-1');
            expect(revoked).toEqual([]);

            // 2. The next compile fails: the host is cleared, so nothing
            //    references blob:pdf-1 any more.
            fetchMock.mockResolvedValueOnce(
                jsonOk({ success: false, has_pdf: false, log_excerpt: '! error' }));
            await m[entry]('/tmp/d/d.tex', btn, host);

            expect(host.querySelector('iframe')).toBeNull();
            expect(revoked).toEqual(['blob:pdf-1']);
        });

        it(`${mod}.js releases the previous blob when the PDF fetch errors`,
           async () => {
            vi.resetModules();
            const m = await import(`../../static/js/${mod}.js`);
            const btn = document.createElement('button');
            const host = document.createElement('div');
            document.body.appendChild(host);

            fetchMock
                .mockResolvedValueOnce(jsonOk({ success: true, has_pdf: true, log_excerpt: '' }))
                .mockResolvedValueOnce(pdfOk());
            await m[entry]('/tmp/d/d.tex', btn, host);
            expect(created).toBe(1);

            fetchMock
                .mockResolvedValueOnce(jsonOk({ success: true, has_pdf: true, log_excerpt: '' }))
                .mockResolvedValueOnce({ ok: false, status: 500, json: async () => ({ error: 'boom' }) });
            await m[entry]('/tmp/d/d.tex', btn, host);

            expect(host.querySelector('iframe')).toBeNull();
            expect(revoked).toEqual(['blob:pdf-1']);
        });
    }

    it('source ratchet: tagging.js closes the PDF panel when the view changes', () => {
        const src = fs.readFileSync(
            path.resolve(__dirname, '../../static/js/tagging.js'), 'utf8');
        const body = src.slice(src.indexOf('function _renderView()'));
        const fnEnd = body.indexOf('\n}\n');
        expect(fnEnd).toBeGreaterThan(0);
        // Switching deck -> table destroys the panel markup; without this the
        // blob URL survived until the deck was next re-entered.
        expect(body.slice(0, fnEnd)).toContain('_deckClosePdf()');
    });

    it('source ratchet: deck.js and docgen.js have a named preview teardown', () => {
        for (const mod of ['deck', 'docgen']) {
            const src = fs.readFileSync(
                path.resolve(__dirname, `../../static/js/${mod}.js`), 'utf8');
            expect(src).toContain('function _previewTeardown()');
            // Cancel must release it too: the abort leaves the host cleared.
            expect(src).toMatch(/_abortCtrl\.abort\(\);[\s\S]{0,400}_previewTeardown\(\)/);
        }
    });
});
