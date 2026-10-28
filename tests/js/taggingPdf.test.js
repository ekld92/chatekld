// Tagging deck PDF side panel (V). Same invariant class as deckPreview.test.js:
// the app-level origin guard requires the X-Requested-With header on EVERY
// /api route, and an iframe navigation cannot send custom headers — so the PDF
// bytes MUST be fetched via secureFetch and handed to the iframe as a blob:
// URL, never as a direct /api src. Ratcheted at source level for tagging.js.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

function okJson(body) {
    return { ok: true, status: 200, json: async () => body };
}
function errJson(status, body) {
    return { ok: false, status, json: async () => body };
}
function pdfOk() {
    return {
        ok: true,
        status: 200,
        blob: async () => new Blob(['%PDF-1.4 fake'], { type: 'application/pdf' }),
        json: async () => ({}),
    };
}

function makeLocalStorage(init = {}) {
    const store = { ...init };
    return {
        getItem: (k) => (k in store ? store[k] : null),
        setItem: (k, v) => { store[k] = String(v); },
        removeItem: (k) => { delete store[k]; },
        clear: () => { for (const k of Object.keys(store)) delete store[k]; },
    };
}

const DECK_SAMPLE = {
    rows: [{
        citation_key: 'aaa2021', year: '2021', title: 'Untagged with note',
        finder_tags: [],
        note: { present: true, tags: [] },
        zotero: { present: true, parent_tags: [] },
    }],
};

function deckFetch(overrides = {}) {
    return (url) => {
        const u = String(url);
        for (const [frag, resp] of Object.entries(overrides)) {
            if (u.includes(frag)) return Promise.resolve(resp);
        }
        if (u.includes('/api/tagging/inventory')) return Promise.resolve(okJson(DECK_SAMPLE));
        if (u.includes('/api/tagging/vocab')) return Promise.resolve(okJson({ vocab: [] }));
        if (u.includes('/api/tagging/zotero-available')) return Promise.resolve(okJson({ available: true, writable: true }));
        if (u.includes('/api/tagging/snippet')) return Promise.resolve(okJson({ snippet: 'excerpt', chars: 7 }));
        return Promise.resolve(okJson({}));
    };
}

const flush = () => new Promise((r) => setTimeout(r, 0));

describe('tagging deck PDF panel', () => {
    let fetchMock;
    let body;
    beforeEach(() => {
        document.body.innerHTML = '<div id="sr-status-announcer" role="status"></div>'
            + '<div id="audit-report-meta"></div><div id="audit-report-body"></div>';
        body = document.getElementById('audit-report-body');
        vi.resetModules();
        vi.stubGlobal('localStorage', makeLocalStorage({ tagging_view: 'deck' }));
        fetchMock = vi.fn();
        vi.stubGlobal('fetch', fetchMock);
        if (!URL.createObjectURL) URL.createObjectURL = () => {};
        vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:mock-tagging-pdf');
        if (!URL.revokeObjectURL) URL.revokeObjectURL = () => {};
        vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    });
    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    async function renderDeck(overrides) {
        fetchMock.mockImplementation(deckFetch(overrides));
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, document.getElementById('audit-report-meta'), () => true);
        await flush();
        return body.querySelector('.tagging-deck');
    }

    it('V fetches the PDF with the CSRF header and renders a blob: iframe', async () => {
        const deckEl = await renderDeck({ '/api/tagging/pdf?': pdfOk() });
        deckEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'v', bubbles: true }));
        await flush();

        const call = fetchMock.mock.calls.find((c) => String(c[0]).includes('/api/tagging/pdf?'));
        expect(call).toBeTruthy();
        expect(String(call[0])).toContain('citation_key=aaa2021');
        expect(call[1].headers['X-Requested-With']).toBe('ChatEKLD');

        const frame = body.querySelector('#tagging-deck-pdf iframe');
        expect(frame).toBeTruthy();
        expect(frame.src).toContain('blob:mock-tagging-pdf');
        expect(frame.src).not.toContain('/api/');
        expect(deckEl.classList.contains('with-pdf')).toBe(true);
    });

    it('V again closes the panel and revokes the blob URL', async () => {
        const deckEl = await renderDeck({ '/api/tagging/pdf?': pdfOk() });
        deckEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'v', bubbles: true }));
        await flush();
        deckEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'V', bubbles: true }));
        expect(body.querySelector('#tagging-deck-pdf').style.display).toBe('none');
        expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:mock-tagging-pdf');
        expect(deckEl.classList.contains('with-pdf')).toBe(false);
    });

    it('a 404 shows an inline error, not an iframe', async () => {
        const deckEl = await renderDeck({
            '/api/tagging/pdf?': errJson(404, { error: 'PDF not found.' }),
        });
        deckEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'v', bubbles: true }));
        await flush();
        const panel = body.querySelector('#tagging-deck-pdf');
        expect(panel.querySelector('iframe')).toBeNull();
        expect(panel.textContent).toContain('PDF unavailable');
        expect(panel.textContent).toContain('PDF not found.');
    });

    it('source ratchet: tagging.js never assigns an /api URL to a .src', () => {
        const src = fs.readFileSync(
            path.resolve(__dirname, '../../static/js/tagging.js'), 'utf8');
        expect(src).not.toMatch(/\.src\s*=\s*[`'"]\/api\//);
    });
});
