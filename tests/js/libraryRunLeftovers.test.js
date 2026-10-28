// "Update my library": the leftover Zotero attachments group (CLI proof run
// 2026-09-23, finding 4, decision D-c). Behaviour: unticked by default, left
// out of the global "Select all", annotated rows are listed without a box,
// the confirm names the count and the Tagging → Restore undo, and the
// approvals carry the ticked UNITS only.
import { beforeEach, expect, it, vi } from 'vitest';

const { fetch, sse, confirms } = vi.hoisted(() => ({ fetch: vi.fn(), sse: vi.fn(), confirms: [] }));
vi.mock('../../static/js/api.js', () => ({ secureFetch: fetch, consumeSSE: sse }));
vi.mock('../../static/js/ui.js', () => ({
    taskBegin: vi.fn(), taskEnd: vi.fn(), openModal: vi.fn(),
    confirmInline: (btn, opts) => confirms.push(opts),
    announceStatus: () => {},
    statusLine: (host, { text }) => { host.textContent = text; return host; },
    relativeStamp: () => 'just now',
}));
import { mountLibraryRun } from '../../static/js/libraryRun.js';

const tick = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const ok = (body) => ({ ok: true, json: async () => body });

const LEFT = { unit: 'attachment:1:MZHLRTT9', item_key: 'MZHLRTT9', parent: 'entry:1:X',
    citation_key: 'martiCarvajal2026', stored_path: 'storage:martí-carvajal_2026.pdf',
    linked_copy_rel: 'Z/biblio/marti_carvajal_2026.pdf', annotations: 0, selectable: true };
const REVIEW = {
    observation: { initialized: false, new: {} }, blocked: [],
    renames: [{ unit: 'attachment:1:A', rel: 'a.pdf', name: 'a.pdf', proposal: 'b_2026.pdf', can_apply: true }],
    tags: [], mirror: {}, mirror_units: [],
    leftovers: [LEFT, { ...LEFT, unit: 'attachment:1:UL9ZM74E', item_key: 'UL9ZM74E', annotations: 3,
        selectable: false, reason: 'it carries 3 Zotero annotation(s); trashing it would trash them too — decide in Zotero' }],
};

function stubRun(applyResult) {
    fetch.mockImplementation(async (url) => String(url).startsWith('/api/models')
        ? ok({ models: [], configured: '' }) : ok({ session: { id: 'S1' } }));
    sse.mockImplementation(async (url, opts, handlers) => {
        if (url.endsWith('/analyze')) handlers.onOther({ library_analyze: REVIEW });
        if (url.endsWith('/apply')) { handlers.onOpen?.(); handlers.onOther({ library_apply: applyResult }); }
    });
}

beforeEach(() => {
    fetch.mockReset(); sse.mockReset(); confirms.length = 0;
    document.body.innerHTML = '<div id="host"></div>';
});

it('lists leftovers unticked, and an annotated one without a box', async () => {
    stubRun({});
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    const boxes = [...document.querySelectorAll('input[data-kind=leftover]')];
    expect(boxes.map((b) => b.dataset.unit)).toEqual(['attachment:1:MZHLRTT9']);
    expect(boxes.every((b) => !b.checked)).toBe(true);
    const text = document.getElementById('library-run-review').textContent;
    expect(text).toContain('Leftover Zotero attachments');
    expect(text).toContain('marti_carvajal_2026.pdf');
    expect(text).toContain('3 Zotero annotation');
});

it('the global "Select all" does not tick a leftover', async () => {
    stubRun({});
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    document.getElementById('library-run-select-all').click();
    expect(document.querySelector('input[data-kind=leftover]').checked).toBe(false);
    expect(document.querySelector('input[data-kind=rename]').checked).toBe(true);
});

it('the confirm names the count and the undo, and the approvals carry the units', async () => {
    stubRun({ mirror: null, pdf_links: [], tags: [], renames: [],
        leftovers: [{ unit: 'attachment:1:MZHLRTT9', item_key: 'MZHLRTT9', status: 'done', batch_id: 'B1' }],
        leftovers_undo: 'papermind tagging restore-batch B1 --confirm' });
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    document.querySelector('input[data-kind=leftover]').checked = true;
    document.getElementById('library-run-apply').click();
    expect(confirms[0].message).toContain('1 leftover Zotero attachment');
    expect(confirms[0].message).toContain('Tagging → Restore');
    await confirms[0].onConfirm(); await tick();
    const body = JSON.parse(sse.mock.calls[1][1].body);
    expect(body.approvals.leftovers).toEqual(['attachment:1:MZHLRTT9']);
    const results = document.getElementById('library-run-results').textContent;
    expect(results).toContain('Leftover Zotero attachments');
    expect(results).toContain('restore-batch B1');
});
