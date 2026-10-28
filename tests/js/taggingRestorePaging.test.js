// Pins the Restore panel's journal header, paging and compaction (tagging.js).
//
// Why this exists: the panel used to fetch `/api/tagging/manifest` unpaged and
// render one table row per operation. On the maintainer's library that is
// 11,651 rows behind one click, from an 8 MB response the server produced by
// JSON-parsing its entire history. Paging fixes both ends, but it introduces
// three ways to mislead the user, and those are what is guarded here:
//
//   1. HONESTY — a page must never be presented as the whole history. The
//      count-vs-total rule from the Tag Hygiene under-reporting incident: show
//      "N of TOTAL", never a bare N.
//   2. APPEND, NEVER RE-FETCH-AND-REPLACE — "Load more" must extend the list
//      with the next page's cursor, not re-request page 1.
//   3. COMPACTION IS TWO STEPS — the destructive call must be preceded by a
//      dry run whose real count goes into the confirm text, and it must never
//      fire without a confirm. This is the only action in the subsystem that
//      deliberately destroys restore mappings.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { taggingRenderInto } from '../../static/js/tagging.js';

function jsonOk(payload) {
    return { ok: true, status: 200, json: async () => payload };
}

function op(id, kind = 'zotero_tag') {
    return {
        op_id: id, kind, state: 'applied', citation_key: `key-${id}`,
        added: ['dep'], removed: null, batch_id: null,
        ts: '2026-08-25T12:00:00+00:00', note_rel: null, pdf_rel: null,
    };
}

const DOM = `
  <div id="audit-report-meta"></div>
  <div id="audit-report-body"></div>
  <div id="sr-status-announcer" role="status"></div>
`;

const STATS = {
    ops: 11651, bytes: 10661888, protected: 11451, evictable: 200,
    oldest_ts: '2026-07-18T20:19:45+00:00',
    newest_ts: '2026-08-25T12:22:00+00:00',
    by_kind_state: { 'zotero_tag/applied': 7080 }, tombstones: 0,
    retention: { enabled: false, policy: 'age_days', days: 365, keep: 5000 },
};

let store;
let calls;
let posted;
let compactPreview;
let pageOne = null;

async function flush() {
    for (let i = 0; i < 6; i += 1) {
        await new Promise((resolve) => { setTimeout(resolve, 0); });
    }
}

async function render() {
    calls = [];
    posted = [];
    vi.stubGlobal('fetch', vi.fn(async (url, init) => {
        const u = String(url);
        calls.push(u);
        if (init && init.method === 'POST') {
            posted.push({ url: u, body: JSON.parse(init.body || '{}') });
        }
        if (u.includes('/api/tagging/inventory')) {
            return jsonOk({ rows: [], count: 0, scope: 'with_pdf', pdfless_count: 0 });
        }
        if (u.includes('/api/tagging/vocab')) return jsonOk({ vocab: [] });
        if (u.includes('/api/tagging/zotero-available')) return jsonOk({ available: true, writable: true });
        if (u.includes('/api/tagging/journal-stats')) return jsonOk(STATS);
        if (u.includes('/api/tagging/compact-journal')) {
            const body = JSON.parse((init && init.body) || '{}');
            if (body.dry_run) return jsonOk(compactPreview);
            return jsonOk({ dropped: 6451, after: 5200, before: 11651 });
        }
        if (u.includes('/api/tagging/manifest')) {
            // Page 2 when a cursor rides the URL, page 1 otherwise.
            if (u.includes('before=')) {
                return jsonOk({
                    ops: [op('p2a'), op('p2b')], count: 2, total: 11651,
                    next_before: null, limit: 100,
                });
            }
            return jsonOk({
                ops: pageOne || [op('p1a'), op('p1b')], count: 2, total: 11651,
                next_before: 11552, limit: 100,
            });
        }
        return jsonOk({});
    }));
    await taggingRenderInto(
        document.getElementById('audit-report-body'),
        document.getElementById('audit-report-meta'),
    );
    await flush();
}

async function openRestore() {
    const btn = document.querySelector('[data-action="show-restore"]');
    expect(btn, 'Restore button missing').toBeTruthy();
    btn.click();
    await flush();
    return document.getElementById('tagging-restore-panel');
}

describe('tagging Restore: journal header, paging and compaction', () => {
    beforeEach(() => {
        document.body.innerHTML = DOM;
        store = new Map();
        compactPreview = {
            policy: 'keep_newest', value: 5000, dry_run: true,
            candidates: 11451, would_drop: 6451, dropped: 0,
            by_kind: { zotero_tag: 3765, finder_tag: 2666 },
            before: 11651, after: 11651,
        };
        vi.stubGlobal('localStorage', {
            getItem: (k) => (store.has(k) ? store.get(k) : null),
            setItem: (k, v) => store.set(k, String(v)),
            removeItem: (k) => store.delete(k),
        });
    });

    afterEach(() => {
        pageOne = null;
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it('names an unknown note-write outcome and still offers its live-checked revert', async () => {
        // 2026-09-17 review, B2: an outcome_unknown op had no exit anywhere in
        // the UI. Its revert reads Zotero first and only PATCHes when the note
        // holds exactly the text we sent.
        pageOne = [{ ...op('u1', 'zotero_note_write'), state: 'outcome_unknown' }, op('p1b')];
        await render();
        const panel = await openRestore();
        const row = panel.querySelector('tr[data-op-id="u1"]');
        expect(row.textContent).toContain('outcome unknown');
        expect(row.querySelector('[data-action="revert-op"]')).toBeTruthy();
    });

    it('shows the journal size and names the never-trimmed population', async () => {
        await render();
        const panel = await openRestore();
        const header = panel.querySelector('.tagging-journal-stats');
        expect(header, 'journal stats header missing').toBeTruthy();
        expect(header.textContent).toContain('11,651');
        // The whole point of the header: 11,451 of those are never evicted.
        expect(header.textContent).toContain('11,451');
        expect(header.textContent).toMatch(/10(\.\d)? MB/);
    });

    it('never presents one page as the whole history', async () => {
        await render();
        const panel = await openRestore();
        expect(panel.querySelectorAll('tbody tr')).toHaveLength(2);
        // Count-vs-total honesty: the total must be visible beside the count.
        expect(panel.textContent).toContain('11,651');
        expect(panel.querySelector('[data-action="restore-more"]')).toBeTruthy();
    });

    it('Load more appends the next page using the cursor', async () => {
        await render();
        const panel = await openRestore();
        calls.length = 0;
        panel.querySelector('[data-action="restore-more"]').click();
        await flush();

        const paged = calls.filter((u) => u.includes('/api/tagging/manifest'));
        expect(paged).toHaveLength(1);
        // The cursor must ride the request — re-fetching page 1 would loop.
        expect(paged[0]).toContain('before=11552');

        const ids = [...panel.querySelectorAll('tbody tr')]
            .map((tr) => tr.getAttribute('data-op-id'));
        expect(ids).toEqual(['p1a', 'p1b', 'p2a', 'p2b']);
    });

    it('Load more preserves rows already on screen, node identity included', async () => {
        // The list is re-wired after each page; an in-place append is what
        // keeps an open confirm strip and the focused control alive. A full
        // innerHTML rewrite destroys both — silently, because the row text
        // comes back looking identical.
        await render();
        const panel = await openRestore();
        const firstRowBefore = panel.querySelector('tbody tr');
        const revert = firstRowBefore.querySelector('[data-action="revert-op"]');
        revert.click();                       // opens the inline confirm strip
        await flush();
        expect(panel.querySelector('.inline-confirm-msg'), 'confirm strip missing')
            .toBeTruthy();

        panel.querySelector('[data-action="restore-more"]').click();
        await flush();

        expect(panel.querySelector('tbody tr'), 'row 1 was re-created')
            .toBe(firstRowBefore);
        expect(panel.querySelector('.inline-confirm-msg'),
               'paging destroyed an open confirm strip').toBeTruthy();
    });

    it('the last page stops offering Load more', async () => {
        await render();
        const panel = await openRestore();
        panel.querySelector('[data-action="restore-more"]').click();
        await flush();
        // Page 2 came back with next_before: null and total === rows shown so
        // far is still less than 11,651 — but there is no cursor left, so the
        // control must go rather than request a page that cannot exist.
        expect(panel.querySelector('[data-action="restore-more"]')).toBeFalsy();
    });

    it('compaction previews the real count before asking to confirm', async () => {
        await render();
        const panel = await openRestore();
        posted.length = 0;
        panel.querySelector('[data-action="compact-journal"]').click();
        await flush();

        // Exactly one call so far, and it is a dry run.
        expect(posted).toHaveLength(1);
        expect(posted[0].body.dry_run).toBe(true);
        expect(posted[0].body.confirm).toBeUndefined();

        // The measured number, not a description, goes into the confirm.
        const strip = document.body.textContent;
        expect(strip).toContain('6,451');
        expect(strip).toContain('11,451');
    });

    it('does not destroy anything until the confirm is pressed', async () => {
        await render();
        const panel = await openRestore();
        panel.querySelector('[data-action="compact-journal"]').click();
        await flush();
        posted.length = 0;

        const confirmBtn = [...document.querySelectorAll('button')]
            .find((b) => /^compact$/i.test(b.textContent.trim()));
        expect(confirmBtn, 'confirm control missing').toBeTruthy();
        // Nothing destructive has been sent while the strip is merely open.
        expect(posted).toHaveLength(0);

        confirmBtn.click();
        await flush();
        expect(posted).toHaveLength(1);
        expect(posted[0].body).toMatchObject({ dry_run: false, confirm: true });
    });

    it('says so instead of confirming when there is nothing to drop', async () => {
        compactPreview = { ...compactPreview, would_drop: 0, candidates: 42 };
        await render();
        const panel = await openRestore();
        posted.length = 0;
        panel.querySelector('[data-action="compact-journal"]').click();
        await flush();

        expect(posted).toHaveLength(1);          // the dry run only
        expect(document.body.textContent).toContain('Nothing to compact');
        const confirmBtn = [...document.querySelectorAll('button')]
            .find((b) => /^compact$/i.test(b.textContent.trim()));
        expect(confirmBtn).toBeFalsy();
    });
});
