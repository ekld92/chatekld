// Pins the finder-drift fix surface (audit.js, drift-fix 2026-08-06).
//
// Guarded classes:
//   1. WRITE CEREMONY — [→ note] / [→ Finder] / [+ vocab] / [undo] all mutate
//      user data (note frontmatter, PDF Finder tags, _tags.md, restores); each
//      must arm confirmInline before any fetch fires.
//   2. VOCAB-ID PAYLOADS — the buttons must POST the report's vocab_ids
//      mapping (exact _tags.md ids, deduped), never the raw drift surfaces:
//      the tagging writers' literal-id gate would 400 a surface whose
//      spelling differs, re-opening the unresolvable-row loop.
//   3. BADGING — an out-of-vocab surface renders "(not in vocab)" + [+ vocab]
//      and is excluded from the direction button; a direction with nothing
//      applyable renders no button at all.
//   4. UNDO — per-op restore over /api/tagging/restore, peek-not-pop (a
//      failed restore leaves the button retryable); the vocab-add chain
//      reverts the apply BEFORE the vocab entry.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { selectAuditReport } from '../../static/js/audit.js';

function jsonOk(payload) {
    return { ok: true, status: 200, json: async () => payload };
}

const REPORT = {
    name: 'finder_tag_drift',
    rows: [
        {
            citation_key: 'smith2020',
            author: 'Smith',
            title: 'Cardiac outcomes',
            finder_tags: ['sleep quality', 'Sommeil profond', 'weird-new'],
            obs_tags: ['cognition'],
            finder_only: ['Sommeil profond', 'sleep quality', 'weird-new'],
            note_only: ['cognition'],
            variants: [{ finder: 'Anxiety', note: 'anxiety', reason: 'spelling' }],
            note_rel: 'Z_Zotero_Notes/smith2020.md',
            vocab_ids: {
                'sleep quality': 'sleep-quality',
                'Sommeil profond': 'sleep-quality', // dedupes with the above
                'weird-new': null,                  // out of vocab → badge
                'cognition': 'cognition',
            },
            pdf_count: 2,
        },
        {
            citation_key: 'jones2019',
            author: 'Jones',
            title: 'Something',
            finder_tags: ['oddball', 'two words'],
            obs_tags: [],
            finder_only: ['oddball', 'two words'],
            note_only: [],
            variants: [],
            note_rel: 'Z_Zotero_Notes/jones2019.md',
            vocab_ids: { oddball: null, 'two words': null },
            pdf_count: 1,
        },
    ],
};

const DOM = `
  <div id="audit-summary" style="display:none;"></div>
  <div id="audit-report-tabs" style="display:none;"></div>
  <div id="audit-search-bar" style="display:none;">
    <input id="audit-search-input"><button id="audit-search-clear"></button>
  </div>
  <div id="audit-report-meta"></div>
  <div id="audit-search-results" style="display:none;"></div>
  <div id="audit-report-body"></div>
  <div id="audit-empty-state"></div>
  <div id="audit-status-banner" style="display:none;"></div>
  <div id="sr-status-announcer" role="status"></div>
`;

function acceptConfirm() {
    const strip = document.querySelector('.inline-confirm-strip');
    expect(strip, 'no confirm strip appeared').toBeTruthy();
    const buttons = [...strip.querySelectorAll('button')];
    buttons[buttons.length - 1].click();
}

function row(key) {
    return document.querySelector(`#audit-report-body tr[data-key="${key}"]`);
}

describe('finder-drift fix buttons', () => {
    let fetchMock;

    beforeEach(async () => {
        document.body.innerHTML = DOM;
        const store = new Map();
        vi.stubGlobal('localStorage', {
            getItem: (k) => (store.has(k) ? store.get(k) : null),
            setItem: (k, v) => store.set(k, String(v)),
            removeItem: (k) => store.delete(k),
        });
        fetchMock = vi.fn(async (url) => {
            if (String(url).includes('/api/audit/reports/finder_tag_drift')) {
                return jsonOk(REPORT);
            }
            return jsonOk({ ok: true, added: [], op_id: 'op-x' });
        });
        vi.stubGlobal('fetch', fetchMock);
        await selectAuditReport('finder_tag_drift');
        fetchMock.mockClear();
    });

    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it('renders deduped vocab-id buttons, badges out-of-vocab, hides empty directions', () => {
        const r1 = row('smith2020');
        const noteBtn = r1.querySelector('[data-action="finder-apply-note"]');
        // Two finder-only surfaces map to ONE vocab id → deduped payload.
        expect(JSON.parse(noteBtn.getAttribute('data-tags'))).toEqual(['sleep-quality']);
        const finderBtn = r1.querySelector('[data-action="finder-apply-finder"]');
        expect(JSON.parse(finderBtn.getAttribute('data-tags'))).toEqual(['cognition']);
        // Out-of-vocab surface: badge + [+ vocab], excluded from the payload.
        expect(r1.textContent).toContain('weird-new');
        expect(r1.textContent).toContain('not in vocab');
        expect(r1.querySelector('[data-action="finder-vocab-add"][data-tag="weird-new"]')).toBeTruthy();
        // Variants column renders the near-miss.
        expect(r1.textContent).toContain('Anxiety ~ anxiety');
        // Row 2: every surface out-of-vocab → no direction buttons at all.
        const r2 = row('jones2019');
        expect(r2.querySelector('[data-action="finder-apply-note"]')).toBeNull();
        expect(r2.querySelector('[data-action="finder-apply-finder"]')).toBeNull();
        expect(r2.querySelector('[data-action="finder-vocab-add"]')).toBeTruthy();
        // The meta line no longer claims the report is read-only.
        expect(document.getElementById('audit-report-meta').textContent)
            .not.toContain('Read-only');
    });

    it('[→ note] is confirm-gated, posts vocab ids, then shows ✓ + undo', async () => {
        fetchMock.mockImplementation(async () =>
            jsonOk({ ok: true, added: ['sleep-quality'], op_id: 'op-1' }));
        const btn = row('smith2020').querySelector('[data-action="finder-apply-note"]');
        btn.click();
        expect(fetchMock).not.toHaveBeenCalled();  // ceremony first
        acceptConfirm();
        await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
        const [url, opts] = fetchMock.mock.calls[0];
        expect(String(url)).toContain('/api/tagging/apply-note-tags');
        expect(JSON.parse(opts.body)).toEqual({
            citation_key: 'smith2020', tags: ['sleep-quality'], confirm: true,
        });
        await vi.waitFor(() => {
            const r1 = row('smith2020');
            expect(r1.textContent).toContain('✓ note: +1');
            expect([...r1.querySelectorAll('button')]
                .some(b => b.textContent === '[undo]')).toBe(true);
        });
        // Applied surfaces struck through in the direction cell.
        const struck = [...row('smith2020').querySelectorAll('.fdrift-surf[data-dir="note"]')]
            .filter(sp => sp.style.textDecoration === 'line-through');
        expect(struck.length).toBe(2); // both surfaces of the applied vocab id
    });

    it('[→ Finder] posts to apply-finder-tags', async () => {
        fetchMock.mockImplementation(async () =>
            jsonOk({ ok: true, added: ['cognition'], op_id: 'op-2' }));
        const btn = row('smith2020').querySelector('[data-action="finder-apply-finder"]');
        btn.click();
        acceptConfirm();
        await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
        const [url, opts] = fetchMock.mock.calls[0];
        expect(String(url)).toContain('/api/tagging/apply-finder-tags');
        expect(JSON.parse(opts.body)).toEqual({
            citation_key: 'smith2020', tags: ['cognition'], confirm: true,
        });
    });

    it('undo posts a confirm-gated restore and is retryable on failure', async () => {
        fetchMock.mockImplementation(async () =>
            jsonOk({ ok: true, added: ['sleep-quality'], op_id: 'op-3' }));
        const btn = row('smith2020').querySelector('[data-action="finder-apply-note"]');
        btn.click();
        acceptConfirm();
        await vi.waitFor(() => expect(row('smith2020').textContent).toContain('✓ note'));
        fetchMock.mockClear();

        // First restore attempt fails → the undo button stays retryable.
        fetchMock.mockImplementation(async () =>
            ({ ok: false, status: 503, json: async () => ({ error: 'busy' }) }));
        const undo = [...row('smith2020').querySelectorAll('button')]
            .find(b => b.textContent === '[undo]');
        expect(undo).toBeTruthy();
        undo.click();
        expect(fetchMock).not.toHaveBeenCalled();  // restore is ceremonied too
        acceptConfirm();
        await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
        expect(String(fetchMock.mock.calls[0][0])).toContain('/api/tagging/restore');
        expect(JSON.parse(fetchMock.mock.calls[0][1].body))
            .toEqual({ op_id: 'op-3', confirm: true });
        await vi.waitFor(() => expect(undo.disabled).toBe(false));
        expect(row('smith2020').contains(undo)).toBe(true);

        // Second attempt succeeds → reverted, undo removed.
        fetchMock.mockClear();
        fetchMock.mockImplementation(async () =>
            jsonOk({ ok: true, status: 'reverted' }));
        undo.click();
        acceptConfirm();
        await vi.waitFor(() =>
            expect(row('smith2020').textContent).toContain('↺ reverted'));
        expect(row('smith2020').contains(undo)).toBe(false);
    });

    it('[+ vocab] chains add-vocab-tag then the apply; undo reverts apply first', async () => {
        const calls = [];
        fetchMock.mockImplementation(async (url, opts) => {
            calls.push([String(url), JSON.parse(opts.body)]);
            if (String(url).includes('add-vocab-tag')) {
                return jsonOk({ ok: true, op_id: 'op-vocab', tag: 'weird-new' });
            }
            if (String(url).includes('apply-note-tags')) {
                return jsonOk({ ok: true, added: ['weird-new'], op_id: 'op-apply' });
            }
            return jsonOk({ ok: true, status: 'reverted' });
        });
        const btn = row('smith2020')
            .querySelector('[data-action="finder-vocab-add"][data-tag="weird-new"]');
        btn.click();
        expect(fetchMock).not.toHaveBeenCalled();
        acceptConfirm();
        await vi.waitFor(() => expect(calls.length).toBe(2));
        expect(calls[0][0]).toContain('/api/tagging/add-vocab-tag');
        expect(calls[0][1]).toEqual({ tag: 'weird-new', confirm: true });
        expect(calls[1][0]).toContain('/api/tagging/apply-note-tags');
        expect(calls[1][1]).toEqual({
            citation_key: 'smith2020', tags: ['weird-new'], confirm: true,
        });

        // Undo reverts the APPLY op before the vocab op.
        const undo = [...row('smith2020').querySelectorAll('button')]
            .find(b => b.textContent === '[undo]');
        expect(undo).toBeTruthy();
        calls.length = 0;
        undo.click();
        acceptConfirm();
        await vi.waitFor(() => expect(calls.length).toBe(2));
        expect(calls[0][1]).toEqual({ op_id: 'op-apply', confirm: true });
        expect(calls[1][1]).toEqual({ op_id: 'op-vocab', confirm: true });
    });

    it('[+ vocab] slugifies a multi-word surface to a single-token id', async () => {
        // _tags.md ids must be single tokens (the server 400s whitespace);
        // a spaced Finder tag must reach add-vocab-tag hyphenated — the
        // fingerprint folds separators, so the row still closes at next scan.
        const calls = [];
        fetchMock.mockImplementation(async (url, opts) => {
            calls.push([String(url), JSON.parse(opts.body)]);
            if (String(url).includes('add-vocab-tag')) {
                return jsonOk({ ok: true, op_id: 'op-v', tag: 'two-words' });
            }
            return jsonOk({ ok: true, added: ['two-words'], op_id: 'op-a' });
        });
        const btn = row('jones2019')
            .querySelector('[data-action="finder-vocab-add"][data-tag="two words"]');
        btn.click();
        const strip = document.querySelector('.inline-confirm-strip');
        expect(strip.textContent).toContain('#two-words');  // confirm names the id
        acceptConfirm();
        await vi.waitFor(() => expect(calls.length).toBe(2));
        expect(calls[0][1]).toEqual({ tag: 'two-words', confirm: true });
        expect(calls[1][1]).toEqual({
            citation_key: 'jones2019', tags: ['two-words'], confirm: true,
        });
    });

    it('a failed apply re-enables the button and surfaces the error', async () => {
        fetchMock.mockImplementation(async () =>
            ({ ok: false, status: 501, json: async () => ({ error: 'macOS only' }) }));
        const btn = row('smith2020').querySelector('[data-action="finder-apply-finder"]');
        btn.click();
        acceptConfirm();
        await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
        await vi.waitFor(() => expect(btn.disabled).toBe(false));
        expect(document.getElementById('audit-status-banner').textContent)
            .toContain('macOS only');
    });
});
