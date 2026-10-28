// 2026-09-02 — the deck's snippet cache was unbounded AND wiped by a scope flip.
//
// DEFECT PINNED: `_snippetCache` was a plain object holding one extracted PDF
// excerpt per card for as long as the panel lived, and taggingRenderInto reset
// it wholesale — so flipping the with-PDF/all scope threw away every excerpt
// the user had already paid a server-side extraction for and re-fetched them
// one card at a time.
//
// The invariants: only SUCCESSES are cached (an error stays re-fetchable), and
// a NEW scan still drops the excerpts with the selection they belong beside.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const jsonOk = (p) => ({ ok: true, status: 200, json: async () => p });

const DOM = `
  <div id="audit-report-meta"></div>
  <div id="audit-report-body"></div>
  <div id="sr-status-announcer" role="status"></div>
`;

function row(key) {
    return {
        citation_key: key, year: '2020', title: `Title ${key}`, has_pdf: true,
        pdf_paths: [{ abs: `/v/${key}.pdf`, rel: `${key}.pdf`, name: `${key}.pdf` }],
        finder_tags: [],
        note: { present: true, rel: `${key}.md`, tags: [] },
        zotero: { present: true, item_key: `Z${key}`, parent_tags: [], child_note_tags: [] },
        bib_keywords: [],
    };
}

let snippetKeys;

function install(rows, scannedAt) {
    vi.stubGlobal('fetch', vi.fn(async (url, opts) => {
        const u = String(url);
        if (u.includes('/api/tagging/snippet')) {
            try { snippetKeys.push(JSON.parse(opts.body).citation_key); }
            catch { snippetKeys.push('(unparsed)'); }
            return jsonOk({ snippet: 'excerpt', chars: 7 });
        }
        if (u.includes('/api/tagging/inventory')) {
            return jsonOk({ rows, count: rows.length, scope: 'x',
                            pdfless_count: 0, scanned_at: scannedAt });
        }
        if (u.includes('/api/tagging/vocab')) return jsonOk({ vocab: [] });
        if (u.includes('/api/tagging/zotero-available')) return jsonOk({ available: true, writable: true });
        return jsonOk({});
    }));
}

const flush = async () => {
    for (let i = 0; i < 5; i += 1) {
        await new Promise((r) => { setTimeout(r, 0); });
    }
};

beforeEach(() => {
    document.body.innerHTML = DOM;
    snippetKeys = [];
    const store = new Map();
    vi.stubGlobal('localStorage', {
        getItem: (k) => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => store.set(k, String(v)),
        removeItem: (k) => store.delete(k),
    });
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

async function loadModule() {
    vi.resetModules();
    return import('../../static/js/tagging.js');
}

async function render(mod, rows, scannedAt) {
    install(rows, scannedAt);
    await mod.taggingRenderInto(
        document.getElementById('audit-report-body'),
        document.getElementById('audit-report-meta'),
    );
    await flush();
}

describe('tagging snippet cache', () => {
    it('survives a re-fetch of the same snapshot', async () => {
        const mod = await loadModule();
        const rows = [row('a2020')];
        await render(mod, rows, 111);
        expect(snippetKeys).toEqual(['a2020']);

        // The scope toggle re-fetches the SAME scan: the excerpt must be
        // served from the cache, not extracted again server-side.
        await render(mod, rows, 111);
        expect(snippetKeys).toEqual(['a2020']);
    });

    it('is dropped when a NEW scan arrives', async () => {
        const mod = await loadModule();
        const rows = [row('a2020')];
        await render(mod, rows, 111);
        await render(mod, rows, 222);
        expect(snippetKeys).toEqual(['a2020', 'a2020']);
    });

    // The 200-entry LRU bound itself is not asserted here: the module exports
    // only taggingRenderInto, and adding a test-only export to reach the Map
    // would pin the harness rather than the behaviour. The eviction loop is
    // three lines beside _snippetPut, and the two cases above pin the part
    // that was actually wrong — the lifetime.
});
