// Review lot 1.1 regression pin for deck.js::runDeckPreview. The defect: the
// first cut set `iframe.src = '/api/deck/preview-pdf?...'` directly — but the
// app-level origin guard requires the X-Requested-With header on EVERY /api
// route, and an iframe navigation request cannot send custom headers, so the
// preview 403'd in the real app (the exact constraint refactor.js::_loadThumb
// documents for <img src>). Invariant pinned here: the PDF bytes are fetched
// with the header via secureFetch and handed to the iframe as a blob: URL —
// never as a direct /api src.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { runDeckPreview } from '../../static/js/deck.js';
import fs from 'node:fs';
import path from 'node:path';

function jsonOk(payload) {
    return { ok: true, status: 200, json: async () => payload };
}

function pdfOk() {
    return {
        ok: true,
        status: 200,
        blob: async () => new Blob(['%PDF-1.4 fake'], { type: 'application/pdf' }),
        json: async () => ({}),
    };
}

describe('runDeckPreview (review lot 1.1)', () => {
    let fetchMock;
    beforeEach(() => {
        fetchMock = vi.fn();
        vi.stubGlobal('fetch', fetchMock);
        // jsdom lacks createObjectURL; the assertion is that the iframe gets
        // THIS value, proving the bytes came from the header-authenticated fetch.
        if (!URL.createObjectURL) URL.createObjectURL = () => {};
        vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:mock-pdf-url');
        if (!URL.revokeObjectURL) URL.revokeObjectURL = () => {};
    });
    afterEach(() => vi.unstubAllGlobals());

    it('fetches the PDF with the CSRF header and renders a blob: iframe', async () => {
        fetchMock
            .mockResolvedValueOnce(jsonOk({ success: true, has_pdf: true, log_excerpt: '' }))
            .mockResolvedValueOnce(pdfOk());
        const btn = document.createElement('button');
        const host = document.createElement('div');
        document.body.appendChild(host);

        await runDeckPreview('/tmp/deck/deck.tex', btn, host);

        expect(fetchMock).toHaveBeenCalledTimes(2);
        const [compileUrl, compileOpts] = fetchMock.mock.calls[0];
        expect(compileUrl).toBe('/api/deck/compile-preview');
        expect(compileOpts.headers['X-Requested-With']).toBe('ChatEKLD');
        const [pdfUrl, pdfOpts] = fetchMock.mock.calls[1];
        expect(pdfUrl).toContain('/api/deck/preview-pdf?deck_path=');
        expect(pdfOpts.headers['X-Requested-With']).toBe('ChatEKLD');

        const frame = host.querySelector('iframe');
        expect(frame).toBeTruthy();
        // The load-bearing assertion: blob URL, never a direct /api src.
        expect(frame.src).toContain('blob:mock-pdf-url');
        expect(frame.src).not.toContain('/api/');
    });

    it('renders the log excerpt (no iframe) on a compile failure', async () => {
        fetchMock.mockResolvedValueOnce(
            jsonOk({ success: false, has_pdf: false, log_excerpt: '! Undefined control sequence.' }));
        const btn = document.createElement('button');
        const host = document.createElement('div');
        document.body.appendChild(host);

        await runDeckPreview('/tmp/deck/deck.tex', btn, host);

        expect(fetchMock).toHaveBeenCalledTimes(1);
        expect(host.querySelector('iframe')).toBeNull();
        expect(host.querySelector('pre').textContent).toContain('Undefined control sequence');
    });

    it('source ratchet: deck.js never assigns an /api URL to an iframe src', () => {
        const src = fs.readFileSync(
            path.resolve(__dirname, '../../static/js/deck.js'), 'utf8');
        // Any future `frame.src = \`/api/...\`` reintroduces the 403 class.
        expect(src).not.toMatch(/\.src\s*=\s*[`'"]\/api\//);
    });

    // 2026-08-04 audit: the preview Cancel is UI-only (synchronous route) —
    // that honesty lived in `title` alone, invisible to keyboard/SR users
    // while the inherited aria-label still claimed a server-side cancel; and
    // the follow-up PDF blob fetch carried no signal, so Cancel during the
    // download was inert. Both pinned in deck.js AND docgen.js.
    for (const mod of ['deck', 'docgen']) {
        const src = () => fs.readFileSync(
            path.resolve(__dirname, `../../static/js/${mod}.js`), 'utf8');

        it(`source ratchet: ${mod}.js preview cancel overrides aria-label with the honest title`, () => {
            const preview = src().match(
                /Stop waiting for this preview[\s\S]{0,400}/);
            expect(preview, 'preview cancel title text missing').toBeTruthy();
            expect(preview[0]).toContain(
                "cancelBtn.setAttribute('aria-label', cancelBtn.title)");
        });

        it(`source ratchet: ${mod}.js preview-pdf fetch carries the abort signal`, () => {
            expect(src()).toMatch(
                /preview-pdf\?deck_path=[\s\S]{0,120}signal:\s*_abortCtrl\.signal/);
        });
    }
});
