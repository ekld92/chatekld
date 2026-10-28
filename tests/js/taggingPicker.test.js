// Pins the tagging table's hand-picker (tagging.js, 2026-08-13).
//
// `papermind tagging batch` had no subset selection — only `--limit`, a head
// slice of a list sorted by (year, key), so "tag these twelve" was impossible
// and repeated limited runs re-proposed the same oldest papers. Choosing WHICH
// papers to tag is a judgement made looking at titles and current tags, i.e.
// in this table; the CLI consumes the result through `--keys-file`.
//
// What must not regress:
//   1. The export is a valid --keys-file body (one key per line, `#` comments).
//   2. Selection survives a re-render inside the session — scope-switching
//      re-fetches and rebuilds the table, and losing the picks there would
//      make the feature useless for exactly the browsing it exists to support.
//   3. The clipboard has a visible fallback: a sandboxed webview can refuse
//      navigator.clipboard, and a "Copied!" that copied nothing is worse than
//      no button at all.
//
// `_view`/`_scope` are read from localStorage ONCE at module load, so every
// test re-imports after seeding storage and drives the real controls.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const jsonOk = (payload) => ({ ok: true, status: 200, json: async () => payload });

function invRow(key, over = {}) {
    return {
        citation_key: key, title: `Title ${key}`, year: '2020',
        first_author: 'Smith, J', has_pdf: true,
        pdf_paths: [{ abs: `/v/${key}.pdf`, rel: `${key}.pdf`, name: `${key}.pdf` }],
        finder_tags: [],
        note: { present: true, rel: `Z/${key}.md`, tags: [] },
        zotero: { present: true, item_key: 'IT', parent_tags: [], child_note_tags: [] },
        bib_keywords: [],
        ...over,
    };
}

const ROWS = [invRow('a2020'), invRow('b2021'), invRow('c2022')];

const DOM = `
  <div id="audit-report-meta"></div>
  <div id="audit-report-body"></div>
  <div id="sr-status-announcer" role="status"></div>
`;

function routes(rows = ROWS) {
    return vi.fn(async (url) => {
        const u = String(url);
        if (u.includes('/api/tagging/vocab')) {
            return jsonOk({ vocab: [{ tag: 'dep', gloss: 'depression' }], count: 1 });
        }
        if (u.includes('/api/tagging/zotero-available')) {
            return jsonOk({ available: true, writable: true });
        }
        if (u.includes('/api/tagging/inventory')) {
            return jsonOk({ rows, count: rows.length, scope: 'with_pdf',
                            pdfless_count: 0, scanned_at: 0 });
        }
        return jsonOk({});
    });
}

async function mount(rows = ROWS) {
    vi.resetModules();
    const store = new Map([['tagging_view', 'table']]);
    vi.stubGlobal('localStorage', {
        getItem: (k) => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => store.set(k, String(v)),
        removeItem: (k) => store.delete(k),
    });
    vi.stubGlobal('fetch', routes(rows));
    const mod = await import('../../static/js/tagging.js');
    await mod.taggingRenderInto(
        document.getElementById('audit-report-body'),
        document.getElementById('audit-report-meta'),
        () => true);
    return mod;
}

const picks = () => [...document.querySelectorAll('input.tagging-pick')];
const btn = (action) => document.querySelector(`[data-action="${action}"]`);
const countText = () => document.querySelector('.tagging-pick-count').textContent;

beforeEach(() => { document.body.innerHTML = DOM; });
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('tagging picker — selection', () => {
    it('renders one checkbox per row and starts empty', async () => {
        await mount();
        expect(picks()).toHaveLength(3);
        expect(picks().every((cb) => !cb.checked)).toBe(true);
        expect(countText()).toBe('');
        expect(btn('pick-copy').disabled).toBe(true);
    });

    it('counts the selection and enables the actions', async () => {
        await mount();
        picks()[0].checked = true;
        picks()[0].dispatchEvent(new Event('change'));
        expect(countText()).toContain('1 selected');
        expect(btn('pick-copy').disabled).toBe(false);
        expect(btn('pick-none').disabled).toBe(false);
    });

    it('select all / clear move every row', async () => {
        await mount();
        btn('pick-all').click();
        expect(countText()).toContain('3 selected');
        expect(picks().every((cb) => cb.checked)).toBe(true);
        btn('pick-none').click();
        expect(countText()).toBe('');
        expect(picks().every((cb) => !cb.checked)).toBe(true);
    });
});

describe('tagging picker — export', () => {
    it('copies a valid --keys-file body', async () => {
        const written = [];
        vi.stubGlobal('navigator', {
            clipboard: { writeText: async (t) => { written.push(t); } },
        });
        await mount();
        picks()[0].checked = true;
        picks()[0].dispatchEvent(new Event('change'));
        picks()[2].checked = true;
        picks()[2].dispatchEvent(new Event('change'));
        btn('pick-copy').click();
        await new Promise((r) => setTimeout(r, 0));

        expect(written).toHaveLength(1);
        const lines = written[0].split('\n').filter(Boolean);
        const keys = lines.filter((l) => !l.startsWith('#'));
        // One key per line, sorted, and nothing else — this text is pasted
        // straight into a file the CLI reads.
        expect(keys).toEqual(['a2020', 'c2022']);
        // The header is comment-only, which --keys-file ignores.
        expect(lines.filter((l) => l.startsWith('#')).length).toBeGreaterThan(0);
        expect(written[0]).toContain('--keys-file');
    });

    it('falls back to a visible textarea when the clipboard refuses', async () => {
        // A "Copied!" toast that copied nothing is worse than no button.
        vi.stubGlobal('navigator', {
            clipboard: { writeText: async () => { throw new Error('denied'); } },
        });
        await mount();
        btn('pick-all').click();
        btn('pick-copy').click();
        await new Promise((r) => setTimeout(r, 0));

        const box = document.getElementById('tagging-pick-export');
        expect(box).not.toBeNull();
        expect(box.value).toContain('a2020');
        expect(document.querySelector('.tagging-pick-status').textContent)
            .toContain('Clipboard unavailable');
    });
});

describe('tagging picker — lifetime', () => {
    it('keeps the selection across a re-render of the table', async () => {
        // Scope switching re-fetches and rebuilds; losing picks there would
        // defeat the browsing the picker exists to support.
        const mod = await mount();
        picks()[1].checked = true;
        picks()[1].dispatchEvent(new Event('change'));
        expect(countText()).toContain('1 selected');

        await mod.taggingRenderInto(
            document.getElementById('audit-report-body'),
            document.getElementById('audit-report-meta'),
            () => true);

        const still = picks().find((cb) => cb.getAttribute('data-key') === 'b2021');
        expect(still.checked).toBe(true);
        expect(countText()).toContain('1 selected');
    });
});

describe('tagging picker — snapshot identity (2026-08-13 review)', () => {
    // A variable router so the same module instance can see two scans.
    function stampedRoutes(getStamp) {
        return vi.fn(async (url) => {
            const u = String(url);
            if (u.includes('/api/tagging/vocab')) {
                return jsonOk({ vocab: [], count: 0 });
            }
            if (u.includes('/api/tagging/zotero-available')) {
                return jsonOk({ available: true, writable: true });
            }
            if (u.includes('/api/tagging/inventory')) {
                return jsonOk({ rows: ROWS, count: ROWS.length,
                                scope: 'with_pdf', pdfless_count: 0,
                                scanned_at: getStamp() });
            }
            return jsonOk({});
        });
    }

    async function mountStamped(getStamp) {
        vi.resetModules();
        const store = new Map([['tagging_view', 'table']]);
        vi.stubGlobal('localStorage', {
            getItem: (k) => (store.has(k) ? store.get(k) : null),
            setItem: (k, v) => store.set(k, String(v)),
            removeItem: (k) => store.delete(k),
        });
        vi.stubGlobal('fetch', stampedRoutes(getStamp));
        const mod = await import('../../static/js/tagging.js');
        await mod.taggingRenderInto(
            document.getElementById('audit-report-body'),
            document.getElementById('audit-report-meta'),
            () => true);
        return mod;
    }

    const rerender = (mod) => mod.taggingRenderInto(
        document.getElementById('audit-report-body'),
        document.getElementById('audit-report-meta'),
        () => true);

    it('keeps the selection when a re-fetch returns the SAME scan', async () => {
        let stamp = 100;
        const mod = await mountStamped(() => stamp);
        picks()[0].checked = true;
        picks()[0].dispatchEvent(new Event('change'));
        expect(countText()).toContain('1 selected');

        await rerender(mod); // same scanned_at — scope/view style re-fetch
        expect(countText()).toContain('1 selected');
    });

    it('drops the selection when a re-fetch returns a NEWER scan', async () => {
        // "The selection belongs to one scan snapshot" was previously only a
        // comment: keys picked against the old scan kept counting toward
        // "N selected" and rode the Copy-keys export unseen.
        let stamp = 100;
        const mod = await mountStamped(() => stamp);
        picks()[0].checked = true;
        picks()[0].dispatchEvent(new Event('change'));
        expect(countText()).toContain('1 selected');

        stamp = 200; // an audit rescan happened
        await rerender(mod);
        expect(picks().every((cb) => !cb.checked)).toBe(true);
        expect(countText()).toBe('');
    });
});
