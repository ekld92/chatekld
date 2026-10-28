// Biblio Tagging sub-view render (slice 1, read-only). Pins that
// taggingRenderInto: (1) renders a table of current tags from the inventory,
// (2) escapes user-controlled tags/titles, (3) shows a friendly notice on a 404
// (no audit scan yet), and (4) honours the latest-wins `isCurrent` gate so a
// stale fetch cannot clobber the panel.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

function okJson(body) {
    return { ok: true, status: 200, json: async () => body };
}
function errJson(status, body) {
    return { ok: false, status, json: async () => body };
}

// This vitest environment does not ship a working localStorage; provide a tiny
// in-memory one so the module's `tagging_view` read/write works under test.
function makeLocalStorage(init = {}) {
    const store = { ...init };
    return {
        getItem: (k) => (k in store ? store[k] : null),
        setItem: (k, v) => { store[k] = String(v); },
        removeItem: (k) => { delete store[k]; },
        clear: () => { for (const k of Object.keys(store)) delete store[k]; },
    };
}

const SAMPLE = {
    rows: [
        {
            citation_key: 'smith2020',
            year: '2020',
            title: 'A study of <x> & things',
            finder_tags: ['anxiety'],
            note: { present: true, tags: ['dep'] },
            zotero: { present: true, parent_tags: ['mood'] },
        },
        {
            citation_key: 'jones2019',
            year: '2019',
            title: 'No note paper',
            finder_tags: [],
            note: { present: false, tags: [] },
            zotero: { present: false, parent_tags: [] },
        },
    ],
};

describe('tagging sub-view render', () => {
    let fetchMock;
    let body;
    let meta;
    beforeEach(() => {
        document.body.innerHTML = '<div id="audit-report-meta"></div><div id="audit-report-body"></div>';
        body = document.getElementById('audit-report-body');
        meta = document.getElementById('audit-report-meta');
        vi.resetModules();
        // These suites assert the table grid; force the table view (deck is the
        // default). The module reads `tagging_view` from localStorage at import.
        vi.stubGlobal('localStorage', makeLocalStorage({ tagging_view: 'table' }));
        fetchMock = vi.fn();
        vi.stubGlobal('fetch', fetchMock);
    });
    afterEach(() => vi.unstubAllGlobals());

    it('renders one row per paper with tags per target', async () => {
        fetchMock.mockResolvedValue(okJson(SAMPLE));
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);

        const rows = body.querySelectorAll('table.audit-table tbody tr');
        expect(rows.length).toBe(2);
        const first = rows[0].textContent;
        expect(first).toContain('smith2020');
        expect(first).toContain('anxiety'); // finder
        expect(first).toContain('dep');     // note
        expect(first).toContain('mood');    // zotero parent
        expect(meta.textContent).toContain('2 paper(s)');
    });

    it('escapes user-controlled title/tags (no raw HTML injected)', async () => {
        fetchMock.mockResolvedValue(okJson(SAMPLE));
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        // The literal <x> in the title must not become a DOM element.
        expect(body.querySelector('x')).toBeNull();
        expect(body.innerHTML).toContain('&lt;x&gt;');
    });

    it('shows a "run a scan" notice on a 404', async () => {
        fetchMock.mockResolvedValue(errJson(404, { error: 'No scan results. Run an audit scan first.' }));
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        expect(body.querySelector('table')).toBeNull();
        expect(body.textContent).toContain('Run an audit scan first');
        expect(meta.style.display).toBe('none');
    });

    it('abandons a stale render when isCurrent() turns false', async () => {
        fetchMock.mockResolvedValue(okJson(SAMPLE));
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => false);
        // Superseded: nothing rendered beyond the initial "Loading…" clear.
        expect(body.querySelector('table')).toBeNull();
    });
});

const flush = () => new Promise((r) => setTimeout(r, 0));

describe('tagging write flow (slice 2)', () => {
    let fetchMock;
    let body;
    let meta;
    beforeEach(() => {
        document.body.innerHTML = '<div id="sr-status-announcer" role="status"></div>'
            + '<div id="audit-report-meta"></div><div id="audit-report-body"></div>';
        body = document.getElementById('audit-report-body');
        meta = document.getElementById('audit-report-meta');
        vi.resetModules();
        // These suites assert the table grid; force the table view (deck is the
        // default). The module reads `tagging_view` from localStorage at import.
        vi.stubGlobal('localStorage', makeLocalStorage({ tagging_view: 'table' }));
        fetchMock = vi.fn();
        vi.stubGlobal('fetch', fetchMock);
    });
    afterEach(() => vi.unstubAllGlobals());

    it('apply routes through confirmInline and POSTs the chosen tags, then updates the cell', async () => {
        fetchMock.mockImplementation((url) => {
            const u = String(url);
            if (u.includes('/api/tagging/inventory')) return Promise.resolve(okJson(SAMPLE));
            if (u.includes('/api/tagging/apply-note-tags')) {
                return Promise.resolve(okJson({ ok: true, added: ['mood'], rejected: [] }));
            }
            return Promise.resolve(okJson({}));
        });
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);

        const row = body.querySelector('tr[data-key="smith2020"]');
        row.querySelector('.tagging-tag-input').value = 'mood';
        row.querySelector('[data-action="apply-note"]').click();

        // The destructive-action ceremony appeared; nothing written yet.
        const strip = body.querySelector('.inline-confirm-strip');
        expect(strip).not.toBeNull();
        const applyCalls = () => fetchMock.mock.calls.filter(
            (c) => String(c[0]).includes('apply-note-tags'));
        expect(applyCalls().length).toBe(0);

        strip.querySelectorAll('button')[1].click(); // confirm
        await flush();

        const call = applyCalls()[0];
        expect(call).toBeTruthy();
        const sent = JSON.parse(call[1].body);
        expect(sent).toMatchObject({ citation_key: 'smith2020', tags: ['mood'], confirm: true });
        // Note cell now shows the merged set (original dep + new mood).
        expect(row.querySelector('.tagging-note-cell').textContent).toContain('mood');
        expect(row.querySelector('.tagging-note-cell').textContent).toContain('dep');
    });

    it('apply-finder POSTs to the finder endpoint and updates the finder cell', async () => {
        fetchMock.mockImplementation((url) => {
            const u = String(url);
            if (u.includes('/api/tagging/inventory')) return Promise.resolve(okJson(SAMPLE));
            if (u.includes('/api/tagging/apply-finder-tags')) {
                return Promise.resolve(okJson({ ok: true, added: ['dep'], rejected: [] }));
            }
            return Promise.resolve(okJson({}));
        });
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        const row = body.querySelector('tr[data-key="smith2020"]');
        row.querySelector('.tagging-tag-input').value = 'dep';
        row.querySelector('[data-action="apply-finder"]').click();
        body.querySelector('.inline-confirm-strip').querySelectorAll('button')[1].click();
        await flush();
        const call = fetchMock.mock.calls.find((c) => String(c[0]).includes('apply-finder-tags'));
        expect(call).toBeTruthy();
        // The finder cell (originally "anxiety") gains "dep".
        const finderCell = row.querySelector('.tagging-finder-cell').textContent;
        expect(finderCell).toContain('anxiety');
        expect(finderCell).toContain('dep');
    });

    it('apply-zotero is enabled when reachable, POSTs, and updates the zotero cell', async () => {
        fetchMock.mockImplementation((url) => {
            const u = String(url);
            if (u.includes('/api/tagging/inventory')) return Promise.resolve(okJson(SAMPLE));
            if (u.includes('/api/tagging/zotero-available')) return Promise.resolve(okJson({ available: true, writable: true }));
            if (u.includes('/api/tagging/apply-zotero-tags')) {
                return Promise.resolve(okJson({ ok: true, added: ['dep'], rejected: [] }));
            }
            return Promise.resolve(okJson({}));
        });
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        const row = body.querySelector('tr[data-key="smith2020"]');
        const zbtn = row.querySelector('[data-action="apply-zotero"]');
        expect(zbtn.hasAttribute('disabled')).toBe(false);
        row.querySelector('.tagging-tag-input').value = 'dep';
        zbtn.click();
        body.querySelector('.inline-confirm-strip').querySelectorAll('button')[1].click();
        await flush();
        expect(fetchMock.mock.calls.some((c) => String(c[0]).includes('apply-zotero-tags'))).toBe(true);
        const cell = row.querySelector('.tagging-zotero-cell').textContent;
        expect(cell).toContain('mood');
        expect(cell).toContain('dep');
    });

    it('apply-zotero is disabled when Zotero is not reachable', async () => {
        fetchMock.mockImplementation((url) => {
            const u = String(url);
            if (u.includes('/api/tagging/inventory')) return Promise.resolve(okJson(SAMPLE));
            if (u.includes('/api/tagging/zotero-available')) return Promise.resolve(okJson({ available: false }));
            return Promise.resolve(okJson({}));
        });
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        const zbtn = body.querySelector('tr[data-key="smith2020"] [data-action="apply-zotero"]');
        expect(zbtn.hasAttribute('disabled')).toBe(true);
    });

    it('apply with an empty tag box does not fire a write', async () => {
        fetchMock.mockResolvedValue(okJson(SAMPLE));
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        const row = body.querySelector('tr[data-key="smith2020"]');
        row.querySelector('[data-action="apply-note"]').click();
        expect(body.querySelector('.inline-confirm-strip')).toBeNull();
        expect(fetchMock.mock.calls.filter((c) => String(c[0]).includes('apply-note-tags')).length).toBe(0);
    });

    it('Propose fills the tag input from the LLM suggestion (merging a draft)', async () => {
        fetchMock.mockImplementation((url) => {
            const u = String(url);
            if (u.includes('/api/tagging/inventory')) return Promise.resolve(okJson(SAMPLE));
            if (u.includes('/api/tagging/vocab')) {
                return Promise.resolve(okJson({ vocab: [{ tag: 'anxiety', gloss: 'Anxiety' }] }));
            }
            if (u.includes('/api/tagging/propose')) {
                return Promise.resolve(okJson({
                    citation_key: 'smith2020',
                    proposed: [{ tag: 'anxiety', confidence: 0.9 }],
                    status: 'ok',
                }));
            }
            return Promise.resolve(okJson({}));
        });
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        const row = body.querySelector('tr[data-key="smith2020"]');
        const input = row.querySelector('.tagging-tag-input');
        input.value = 'dep'; // existing typed draft
        row.querySelector('[data-action="propose"]').click();
        await flush();
        // The proposal lands as a committed chip; the typed draft is untouched.
        const chips = Array.from(row.querySelectorAll('.tagging-entry-chip'))
            .map((c) => c.textContent.replace('×', ''));
        expect(chips).toContain('anxiety'); // proposed
        expect(input.value).toBe('dep');    // draft preserved
    });

    it('Restore toggle loads the manifest and lists revertible ops', async () => {
        fetchMock.mockImplementation((url) => {
            const u = String(url);
            if (u.includes('/api/tagging/inventory')) return Promise.resolve(okJson(SAMPLE));
            if (u.includes('/api/tagging/manifest')) {
                return Promise.resolve(okJson({
                    ops: [{ op_id: '1-0', kind: 'note_tag', citation_key: 'smith2020', added: ['dep'], ts: '2026-07-15T10:00:00', state: 'applied' }],
                    count: 1,
                }));
            }
            return Promise.resolve(okJson({}));
        });
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        body.querySelector('[data-action="show-restore"]').click();
        await flush();
        const panel = body.querySelector('#tagging-restore-panel');
        expect(panel.style.display).not.toBe('none');
        expect(panel.textContent).toContain('smith2020');
        expect(panel.querySelector('[data-action="revert-op"]')).not.toBeNull();
    });
});

// A deck-oriented fixture: the first (untagged) paper HAS a note + a Zotero
// item (both empty), so the note/Zotero targets are enabled on its card.
const DECK_SAMPLE = {
    rows: [
        {
            citation_key: 'aaa2021', year: '2021', title: 'Untagged with note',
            finder_tags: [],
            note: { present: true, tags: [] },
            zotero: { present: true, parent_tags: [] },
        },
        {
            citation_key: 'zzz2018', year: '2018', title: 'Already tagged',
            finder_tags: ['x'],
            note: { present: true, tags: ['dep'] },
            zotero: { present: true, parent_tags: ['mood'] },
        },
    ],
};

function deckFetch(overrides = {}) {
    return (url) => {
        const u = String(url);
        // Overrides FIRST so a test can replace a defaulted endpoint (vocab,
        // snippet) — not only add new ones.
        for (const [frag, resp] of Object.entries(overrides)) {
            if (u.includes(frag)) return Promise.resolve(resp);
        }
        if (u.includes('/api/tagging/inventory')) return Promise.resolve(okJson(DECK_SAMPLE));
        if (u.includes('/api/tagging/vocab')) return Promise.resolve(okJson({ vocab: [{ tag: 'dep', gloss: 'Dépression' }] }));
        if (u.includes('/api/tagging/zotero-available')) return Promise.resolve(okJson({ available: true, writable: true }));
        if (u.includes('/api/tagging/snippet')) return Promise.resolve(okJson({ snippet: 'excerpt text', chars: 12 }));
        return Promise.resolve(okJson({}));
    };
}

describe('tagging triage deck', () => {
    let fetchMock;
    let body;
    let meta;
    beforeEach(() => {
        document.body.innerHTML = '<div id="sr-status-announcer" role="status"></div>'
            + '<div id="audit-report-meta"></div><div id="audit-report-body"></div>';
        body = document.getElementById('audit-report-body');
        meta = document.getElementById('audit-report-meta');
        vi.resetModules();
        vi.stubGlobal('localStorage', makeLocalStorage({ tagging_view: 'deck' }));
        fetchMock = vi.fn();
        vi.stubGlobal('fetch', fetchMock);
    });
    afterEach(() => vi.unstubAllGlobals());

    it('renders one card, untagged-first, and loads the PDF excerpt', async () => {
        fetchMock.mockImplementation(deckFetch());
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        await flush();
        // No table; a single card for the untagged paper leads.
        expect(body.querySelector('table.audit-table')).toBeNull();
        const card = body.querySelector('#tagging-deck-card');
        expect(card.textContent).toContain('aaa2021'); // untagged (score 0) first
        expect(body.querySelector('#tagging-card-snippet').textContent).toContain('excerpt text');
    });

    it('toggles to the table view and persists the choice', async () => {
        fetchMock.mockImplementation(deckFetch());
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        body.querySelector('[data-action="view-table"]').click();
        expect(body.querySelector('table.audit-table')).not.toBeNull();
        expect(localStorage.getItem('tagging_view')).toBe('table');
    });

    it('P proposes; A opens the confirm ceremony (no write yet); confirm POSTs apply-all', async () => {
        fetchMock.mockImplementation(deckFetch({
            '/api/tagging/propose': okJson({ proposed: [{ tag: 'dep', confidence: 0.9 }], status: 'ok' }),
            '/api/tagging/apply-all': okJson({
                citation_key: 'aaa2021',
                results: {
                    note: { ok: true, added: ['dep'], op_id: 'op1' },
                    finder: { ok: false, skipped: 'not_macos' },
                    zotero: { ok: true, added: ['dep'], op_id: 'op2' },
                },
            }),
        }));
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        await flush();
        const deckEl = body.querySelector('.tagging-deck');

        // P → propose fills the entry field (as committed chips).
        deckEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'P', bubbles: true }));
        await flush();
        expect(Array.from(body.querySelectorAll('.tagging-entry-chip'))
            .map((c) => c.textContent.replace('×', ''))).toContain('dep');

        // A → ceremony strip appears, nothing written yet.
        deckEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'A', bubbles: true }));
        const strip = body.querySelector('.inline-confirm-strip');
        expect(strip).not.toBeNull();
        expect(fetchMock.mock.calls.some((c) => String(c[0]).includes('apply-all'))).toBe(false);

        // Confirm → apply-all POST with the chosen targets.
        strip.querySelectorAll('button')[1].click();
        await flush();
        const call = fetchMock.mock.calls.find((c) => String(c[0]).includes('apply-all'));
        expect(call).toBeTruthy();
        const sent = JSON.parse(call[1].body);
        expect(sent).toMatchObject({ citation_key: 'aaa2021', confirm: true });
        expect(sent.tags).toContain('dep');
        expect(sent.targets.note).toBe(true);
    });

    it('after an accept, Backspace opens a confirm ceremony, then undoes via /restore', async () => {
        fetchMock.mockImplementation(deckFetch({
            '/api/tagging/propose': okJson({ proposed: [{ tag: 'dep', confidence: 0.9 }], status: 'ok' }),
            '/api/tagging/apply-all': okJson({
                citation_key: 'aaa2021',
                results: { note: { ok: true, added: ['dep'], op_id: 'op1' } },
            }),
            '/api/tagging/restore': okJson({ status: 'reverted' }),
        }));
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        await flush();
        const deckEl = body.querySelector('.tagging-deck');
        body.querySelector('#tagging-card-input').value = 'dep';
        deckEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }));
        body.querySelector('.inline-confirm-strip').querySelectorAll('button')[1].click();
        await flush();
        // Backspace = undo, but it REWRITES vault data, so it must ceremony
        // first (Track 6e) — no /restore call before the confirm.
        deckEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'Backspace', bubbles: true }));
        const restoreCalls = () => fetchMock.mock.calls.filter(
            (c) => String(c[0]).includes('/api/tagging/restore'));
        expect(restoreCalls().length).toBe(0);
        const strip = body.querySelector('.inline-confirm-strip');
        expect(strip).not.toBeNull();
        strip.querySelectorAll('button')[1].click();
        await flush();
        const restore = restoreCalls()[0];
        expect(restore).toBeTruthy();
        expect(JSON.parse(restore[1].body)).toMatchObject({ op_id: 'op1', confirm: true });
    });

    it('a partial-failure response still records the landed op for undo', async () => {
        // 2026-07-19: even when one target errors (or the HTTP status is not
        // 200 — defensive), any per-target results the server sent must be
        // processed, or the landed note write's op_id never reaches the undo
        // stack and ⌫ silently loses its target.
        fetchMock.mockImplementation(deckFetch({
            '/api/tagging/apply-all': errJson(500, {
                error: 'zotero exploded',
                results: {
                    note: { ok: true, added: ['dep'], op_id: 'op-note-1' },
                    zotero: { ok: false, error: 'boom' },
                },
            }),
            '/api/tagging/restore': okJson({ status: 'reverted' }),
        }));
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        await flush();
        const deckEl = body.querySelector('.tagging-deck');
        body.querySelector('#tagging-card-input').value = 'dep';
        deckEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }));
        body.querySelector('.inline-confirm-strip').querySelectorAll('button')[1].click();
        await flush();
        // The outcome line reports per-target truth, not a blanket failure.
        const lastResult = body.querySelector('#tagging-deck-lastresult').textContent;
        expect(lastResult).toContain('note ✓');
        expect(lastResult).toContain('zotero error');
        // The landed note op is undoable.
        deckEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'Backspace', bubbles: true }));
        const strip = body.querySelector('.inline-confirm-strip');
        expect(strip).not.toBeNull();
        strip.querySelectorAll('button')[1].click();
        await flush();
        const restore = fetchMock.mock.calls.find(
            (c) => String(c[0]).includes('/api/tagging/restore'));
        expect(restore).toBeTruthy();
        expect(JSON.parse(restore[1].body)).toMatchObject({ op_id: 'op-note-1', confirm: true });
    });

    it('number keys map to the CURRENT card\'s proposals only (cleared on advance)', async () => {
        fetchMock.mockImplementation(deckFetch({
            '/api/tagging/propose': okJson({ proposed: [{ tag: 'dep', confidence: 0.9 }], status: 'ok' }),
        }));
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        await flush();
        const deckEl = body.querySelector('.tagging-deck');
        // Propose on card 1 → the tag lands as an entry chip.
        deckEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'P', bubbles: true }));
        await flush();
        const entryChips = () => Array.from(body.querySelectorAll('.tagging-entry-chip'))
            .map((c) => c.textContent.replace('×', ''));
        expect(entryChips()).toContain('dep');
        // Skip to card 2: the previous card's proposals must NOT leak — '1'
        // would otherwise inject the WRONG paper's tag into this card.
        deckEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'S', bubbles: true }));
        deckEl.dispatchEvent(new KeyboardEvent('keydown', { key: '1', bubbles: true }));
        expect(entryChips()).toEqual([]);
        expect(body.querySelector('#tagging-card-input').value).toBe('');
    });

    it('snippet HTTP errors are shown as errors, not cached as "no text"', async () => {
        fetchMock.mockImplementation((url) => {
            const u = String(url);
            if (u.includes('/api/tagging/snippet')) {
                return Promise.resolve(errJson(500, { error: 'boom' }));
            }
            return deckFetch()(url);
        });
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        await flush();
        const snip = body.querySelector('#tagging-card-snippet').textContent;
        expect(snip).toContain('excerpt unavailable');
        // Must NOT claim the PDF is scanned/unreadable on a transport failure.
        expect(snip).not.toContain('No extractable text');
    });

    it('prefetches the NEXT card snippet once and serves the skip from cache', async () => {
        fetchMock.mockImplementation(deckFetch());
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        await flush();
        await flush(); // let the sequential prefetch settle too
        const snippetCalls = () => fetchMock.mock.calls.filter(
            (c) => String(c[0]).includes('/api/tagging/snippet'));
        // Current card + one prefetch for the next card — no more, no dupes.
        expect(snippetCalls().length).toBe(2);
        const keys = snippetCalls().map((c) => JSON.parse(c[1].body).citation_key);
        expect(keys).toEqual(['aaa2021', 'zzz2018']);
        // Skip: the next card's excerpt is served from cache, no third fetch.
        const deckEl = body.querySelector('.tagging-deck');
        deckEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'S', bubbles: true }));
        await flush();
        expect(snippetCalls().length).toBe(2);
        expect(body.querySelector('#tagging-card-snippet').textContent).toContain('excerpt text');
    });

    it('snippet cache survives the Deck↔Table toggle (no refetch)', async () => {
        fetchMock.mockImplementation(deckFetch());
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        await flush();
        await flush();
        const snippetCalls = () => fetchMock.mock.calls.filter(
            (c) => String(c[0]).includes('/api/tagging/snippet'));
        const before = snippetCalls().length;
        body.querySelector('[data-action="view-table"]').click();
        body.querySelector('[data-action="view-deck"]').click();
        await flush();
        await flush();
        // Both cards were already cached at module scope: zero new fetches.
        expect(snippetCalls().length).toBe(before);
        expect(body.querySelector('#tagging-card-snippet').textContent).toContain('excerpt text');
    });

    it('Accept advances immediately; the result reconciles into the deck-level line', async () => {
        let resolveApply;
        fetchMock.mockImplementation((url) => {
            if (String(url).includes('/api/tagging/apply-all')) {
                return new Promise((res) => {
                    resolveApply = () => res(okJson({
                        citation_key: 'aaa2021',
                        results: { note: { ok: true, added: ['dep'], op_id: 'op1' } },
                    }));
                });
            }
            return deckFetch()(url);
        });
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        await flush();
        const deckEl = body.querySelector('.tagging-deck');
        body.querySelector('#tagging-card-input').value = 'dep';
        deckEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }));
        body.querySelector('.inline-confirm-strip').querySelectorAll('button')[1].click();
        // Advanced BEFORE the server answered — the write settles in background.
        expect(body.querySelector('#tagging-deck-card').textContent).toContain('zzz2018');
        expect(document.getElementById('tagging-deck-lastresult').textContent).toContain('applying');
        resolveApply();
        await flush();
        // The outcome lands in the deck-level line, which survived the advance.
        const line = document.getElementById('tagging-deck-lastresult').textContent;
        expect(line).toContain('aaa2021');
        expect(line).toContain('note ✓ dep');
    });

    it('a Zotero web write is labelled "syncing" — a bare ✓ would read as a lie', async () => {
        // The web transport writes to zotero.org; the desktop app only sees it
        // on its next sync. A user who spot-checks Zotero immediately must not
        // conclude the write failed.
        fetchMock.mockImplementation((url) => {
            if (String(url).includes('/api/tagging/apply-all')) {
                return Promise.resolve(okJson({
                    citation_key: 'aaa2021',
                    results: {
                        note: { ok: true, added: ['dep'], op_id: 'op1' },
                        zotero: { ok: true, added: ['dep'], op_id: 'op2', sync_pending: true },
                    },
                }));
            }
            return deckFetch()(url);
        });
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        await flush();
        const deckEl = body.querySelector('.tagging-deck');
        body.querySelector('#tagging-card-input').value = 'dep';
        deckEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }));
        body.querySelector('.inline-confirm-strip').querySelectorAll('button')[1].click();
        await flush();
        const line = document.getElementById('tagging-deck-lastresult').textContent;
        expect(line).toContain('zotero ✓ dep (syncing to Zotero)');
        // The local-transport targets stay unqualified — no false alarm.
        expect(line).toContain('note ✓ dep');
        expect(line).not.toContain('note ✓ dep (syncing');
    });

    it('a failed apply-all surfaces in the deck-level line (not lost with the old card)', async () => {
        fetchMock.mockImplementation((url) => {
            if (String(url).includes('/api/tagging/apply-all')) {
                return Promise.resolve(errJson(500, { error: 'boom' }));
            }
            return deckFetch()(url);
        });
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        await flush();
        const deckEl = body.querySelector('.tagging-deck');
        body.querySelector('#tagging-card-input').value = 'dep';
        deckEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }));
        body.querySelector('.inline-confirm-strip').querySelectorAll('button')[1].click();
        await flush();
        const line = document.getElementById('tagging-deck-lastresult').textContent;
        expect(line).toContain('FAILED');
        expect(line).toContain('boom');
    });

    it('Backspace is inert while an apply-all is in flight (undo stays busy-gated)', async () => {
        let resolveSecond;
        let applyCount = 0;
        fetchMock.mockImplementation((url) => {
            if (String(url).includes('/api/tagging/apply-all')) {
                applyCount += 1;
                if (applyCount === 1) {
                    return Promise.resolve(okJson({
                        citation_key: 'aaa2021',
                        results: { note: { ok: true, added: ['dep'], op_id: 'op1' } },
                    }));
                }
                return new Promise((res) => {
                    resolveSecond = () => res(okJson({
                        citation_key: 'zzz2018',
                        results: { note: { ok: true, added: ['dep'], op_id: 'op2' } },
                    }));
                });
            }
            return deckFetch()(url);
        });
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        await flush();
        const deckEl = body.querySelector('.tagging-deck');
        // First accept resolves fast → one undoable entry.
        body.querySelector('#tagging-card-input').value = 'dep';
        deckEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }));
        body.querySelector('.inline-confirm-strip').querySelectorAll('button')[1].click();
        await flush();
        // Second accept hangs → busy. Backspace must NOT open the undo
        // ceremony despite the undoable first accept: the stack fills
        // post-response, so an undo now would target the WRONG accept.
        body.querySelector('#tagging-card-input').value = 'dep';
        deckEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }));
        body.querySelector('.inline-confirm-strip').querySelectorAll('button')[1].click();
        deckEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'Backspace', bubbles: true }));
        expect(body.querySelector('.inline-confirm-strip')).toBeNull();
        resolveSecond();
        await flush();
        deckEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'Backspace', bubbles: true }));
        expect(body.querySelector('.inline-confirm-strip')).not.toBeNull();
    });

    it('type-ahead suggests for the token at the caret, not just the first tag', async () => {
        fetchMock.mockImplementation(deckFetch({
            '/api/tagging/vocab': okJson({ vocab: [
                { tag: 'dep', gloss: 'Dépression' },
                { tag: 'mood', gloss: 'Humeur' },
                { tag: 'dementia', gloss: 'Démence' },
            ] }),
        }));
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        await flush();
        const input = body.querySelector('#tagging-card-input');
        // SECOND token: the native <datalist> went silent here — pasting a
        // separated list commits the finished tags as chips, the trailing
        // partial keeps its suggestions.
        input.value = 'dep, mo';
        input.setSelectionRange(input.value.length, input.value.length);
        input.dispatchEvent(new Event('input', { bubbles: true }));
        expect(input.value).toBe('mo'); // 'dep' absorbed into a chip
        const boxItems = body.querySelectorAll('.tagging-suggest-item');
        expect(boxItems.length).toBe(1);
        expect(boxItems[0].textContent).toContain('mood');
        // Picking commits the tag as a chip — no separator ever typed.
        boxItems[0].dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
        const chips = Array.from(body.querySelectorAll('.tagging-entry-chip'))
            .map((c) => c.textContent.replace('×', ''));
        expect(chips).toEqual(['dep', 'mood']);
        expect(input.value).toBe('');
    });

    it('Enter picks the open suggestion instead of accepting the card', async () => {
        fetchMock.mockImplementation(deckFetch({
            '/api/tagging/vocab': okJson({ vocab: [
                { tag: 'dep', gloss: 'Dépression' },
                { tag: 'dementia', gloss: 'Démence' },
            ] }),
        }));
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        await flush();
        const input = body.querySelector('#tagging-card-input');
        input.value = 'deme';
        input.setSelectionRange(4, 4);
        input.dispatchEvent(new Event('input', { bubbles: true }));
        expect(body.querySelectorAll('.tagging-suggest-item').length).toBe(1);
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
        const chips = Array.from(body.querySelectorAll('.tagging-entry-chip'))
            .map((c) => c.textContent.replace('×', ''));
        expect(chips).toEqual(['dementia']);
        expect(input.value).toBe('');
        // The Enter was consumed by the pick — no Accept ceremony, no write.
        expect(body.querySelector('.inline-confirm-strip')).toBeNull();
        expect(fetchMock.mock.calls.some((c) => String(c[0]).includes('apply-all'))).toBe(false);
        // Field now empty and dropdown closed: Enter falls through to Accept.
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
        expect(body.querySelector('.inline-confirm-strip')).not.toBeNull();
    });

    it('type-ahead excludes tags already entered', async () => {
        fetchMock.mockImplementation(deckFetch({
            '/api/tagging/vocab': okJson({ vocab: [
                { tag: 'dep', gloss: 'Dépression' },
                { tag: 'dementia', gloss: 'Démence' },
            ] }),
        }));
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        await flush();
        const input = body.querySelector('#tagging-card-input');
        input.value = 'dep, de';
        input.setSelectionRange(input.value.length, input.value.length);
        input.dispatchEvent(new Event('input', { bubbles: true }));
        const labels = Array.from(body.querySelectorAll('.tagging-suggest-item strong'))
            .map((s) => s.textContent);
        expect(labels).toEqual(['dementia']); // 'dep' already in the field
    });

    it('Backspace in an empty input removes the last chip; × removes any chip', async () => {
        fetchMock.mockImplementation(deckFetch({
            '/api/tagging/vocab': okJson({ vocab: [
                { tag: 'dep', gloss: 'Dépression' },
                { tag: 'mood', gloss: 'Humeur' },
            ] }),
        }));
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        await flush();
        const input = body.querySelector('#tagging-card-input');
        input.value = 'dep mood '; // trailing separator commits both
        input.dispatchEvent(new Event('input', { bubbles: true }));
        const chips = () => Array.from(body.querySelectorAll('.tagging-entry-chip'))
            .map((c) => c.textContent.replace('×', ''));
        expect(chips()).toEqual(['dep', 'mood']);
        // Backspace on the EMPTY input pops the last chip (and must not
        // bubble into the deck's undo verb — the input is a typing target).
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Backspace', bubbles: true }));
        expect(chips()).toEqual(['dep']);
        expect(body.querySelector('.inline-confirm-strip')).toBeNull();
        // The × button removes a specific chip.
        body.querySelector('.tagging-entry-chip-x')
            .dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
        expect(chips()).toEqual([]);
    });

    it('suggestions are accent-insensitive (typing "dép" finds "dep")', async () => {
        fetchMock.mockImplementation(deckFetch({
            '/api/tagging/vocab': okJson({ vocab: [
                { tag: 'dep', gloss: 'Dépression, EDC' },
                { tag: 'scz', gloss: 'Schizophrénie' },
            ] }),
        }));
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        await flush();
        const input = body.querySelector('#tagging-card-input');
        input.value = 'dép';
        input.setSelectionRange(3, 3);
        input.dispatchEvent(new Event('input', { bubbles: true }));
        const labels = Array.from(body.querySelectorAll('.tagging-suggest-item strong'))
            .map((s) => s.textContent);
        expect(labels).toEqual(['dep']);
    });

    it('space-separated tags parse without commas', async () => {
        fetchMock.mockImplementation(deckFetch());
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        await flush();
        const deckEl = body.querySelector('.tagging-deck');
        const input = body.querySelector('#tagging-card-input');
        input.value = 'dep mood';
        deckEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }));
        const strip = body.querySelector('.inline-confirm-strip');
        expect(strip).not.toBeNull();
        expect(strip.textContent).toContain('Add 2 tag(s)');
    });

    it('keyboard verbs still work while a target checkbox has focus', async () => {
        fetchMock.mockImplementation(deckFetch());
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        await flush();
        const deckEl = body.querySelector('.tagging-deck');
        const checkbox = body.querySelector('.tagging-card-targets input[data-target]');
        // Simulate the verb keydown with the checkbox as the event target (as
        // after clicking a toggle): S must still advance the card.
        const ev = new KeyboardEvent('keydown', { key: 'S', bubbles: true });
        Object.defineProperty(ev, 'target', { value: checkbox });
        deckEl.dispatchEvent(ev);
        expect(body.querySelector('#tagging-deck-card').textContent).toContain('zzz2018');
    });

    // 2026-07-19 keyboard-trap fix: with the Accept confirm strip open, Enter
    // used to bubble to the deck's verb handler, whose preventDefault cancelled
    // the focused button's native activation and re-fired Accept — the strip
    // re-opened forever and the confirm was mouse-only.
    it('Enter inside the open confirm strip never re-fires Accept', async () => {
        fetchMock.mockImplementation(deckFetch());
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        await flush();
        const deckEl = body.querySelector('.tagging-deck');
        const input = body.querySelector('#tagging-card-input');
        input.value = 'dep';
        deckEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }));
        const strip = body.querySelector('.inline-confirm-strip');
        expect(strip).not.toBeNull();
        const cancel = strip.querySelectorAll('button')[0];
        const ev = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
        Object.defineProperty(ev, 'target', { value: cancel });
        deckEl.dispatchEvent(ev);
        // Still exactly one strip (not replaced/re-opened) and no write fired.
        expect(body.querySelectorAll('.inline-confirm-strip').length).toBe(1);
        const applyCalls = fetchMock.mock.calls.filter(
            (c) => String(c[0]).includes('apply-all'));
        expect(applyCalls.length).toBe(0);
    });

    it('Space on a focused deck button activates it natively, never Defers', async () => {
        fetchMock.mockImplementation(deckFetch());
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        await flush();
        const deckEl = body.querySelector('.tagging-deck');
        const firstCard = body.querySelector('#tagging-deck-card').textContent;
        const skipBtn = body.querySelector('[data-deck="skip"]');
        const ev = new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true });
        Object.defineProperty(ev, 'target', { value: skipBtn });
        deckEl.dispatchEvent(ev);
        // The verb handler must not have consumed the key (native activation
        // would follow in a real browser) — no Defer, card unchanged.
        expect(ev.defaultPrevented).toBe(false);
        expect(body.querySelector('#tagging-deck-card').textContent).toBe(firstCard);
        expect(body.querySelector('#tagging-deck-progress').textContent).not.toContain('deferred');
    });
});

describe('tagging load UX (2026-07-19)', () => {
    let fetchMock;
    let body;
    let meta;
    beforeEach(() => {
        document.body.innerHTML = '<div id="sr-status-announcer" role="status"></div>'
            + '<div id="sr-error-announcer" role="alert"></div>'
            + '<div id="audit-report-meta"></div><div id="audit-report-body"></div>';
        body = document.getElementById('audit-report-body');
        meta = document.getElementById('audit-report-meta');
        vi.resetModules();
        vi.stubGlobal('localStorage', makeLocalStorage({ tagging_view: 'deck' }));
        fetchMock = vi.fn();
        vi.stubGlobal('fetch', fetchMock);
    });
    afterEach(() => vi.unstubAllGlobals());

    it('an inventory network failure renders an error boundary whose Retry re-fetches', async () => {
        let fail = true;
        fetchMock.mockImplementation((url) => {
            const u = String(url);
            if (u.includes('/api/tagging/inventory')) {
                if (fail) return Promise.reject(new Error('net down'));
                return Promise.resolve(okJson(DECK_SAMPLE));
            }
            return Promise.resolve(okJson({ vocab: [], available: false }));
        });
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        const boundary = body.querySelector('.task-error-boundary');
        expect(boundary).not.toBeNull();
        const retry = Array.from(boundary.querySelectorAll('button'))
            .find((b) => b.textContent === 'Retry');
        expect(retry).toBeTruthy();
        fail = false;
        retry.click();
        await flush();
        await flush();
        expect(body.querySelector('.tagging-deck')).not.toBeNull();
    });

    it('vocab and zotero probes are fired concurrently after the inventory', async () => {
        const order = [];
        fetchMock.mockImplementation((url) => {
            const u = String(url);
            if (u.includes('/api/tagging/inventory')) {
                order.push('inventory');
                return Promise.resolve(okJson(DECK_SAMPLE));
            }
            if (u.includes('/api/tagging/vocab')) {
                order.push('vocab-start');
                return Promise.resolve(okJson({ vocab: [] }));
            }
            if (u.includes('/api/tagging/zotero-available')) {
                // If this were sequential, vocab would have RESOLVED before
                // this request even started; concurrent = both requests are
                // issued in the same tick.
                order.push('zotero-start');
                return Promise.resolve(okJson({ available: true, writable: true }));
            }
            return Promise.resolve(okJson({}));
        });
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        expect(order.slice(0, 3)).toEqual(['inventory', 'vocab-start', 'zotero-start']);
    });

    it('a disabled target toggle carries its reason as an aria-label', async () => {
        fetchMock.mockImplementation(deckFetch({
            '/api/tagging/zotero-available': okJson({ available: false }),
        }));
        const { taggingRenderInto } = await import('../../static/js/tagging.js');
        await taggingRenderInto(body, meta, () => true);
        await flush();
        const zotCb = body.querySelector('input[data-target="zotero"]');
        expect(zotCb.disabled).toBe(true);
        expect(zotCb.getAttribute('aria-label')).toContain('not authorized');
    });
});

describe('audit.js ↔ tagging.js integration ratchets', () => {
    it('audit.js re-checks isCurrent after the taggingRenderInto await', () => {
        // 2026-07-19: taggingRenderInto swallows its own staleness, so a
        // superseded tagging load's continuation reached _wireRowActions()
        // and double-bound the NEXT report's .audit-link-btn handlers (a
        // reveal fired two `open -R`). The guard after the await is load-
        // bearing; this ratchet keeps it from being refactored away.
        const { readFileSync } = require('node:fs');
        const { resolve } = require('node:path');
        const src = readFileSync(
            resolve(__dirname, '../../static/js/audit.js'), 'utf-8');
        expect(src).toMatch(
            /await window\.taggingRenderInto\(body, meta, isCurrent\);[\s\S]{0,600}?if \(!isCurrent\(\)\) return;/);
    });
});
