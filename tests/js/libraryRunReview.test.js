// Adversarial review of lot 6 (2026-09-21), mocked ui.js: each test was red on 89485693.
import { beforeEach, expect, it, vi } from 'vitest';
const { fetch, sse, confirms, lines } = vi.hoisted(() => ({ fetch: vi.fn(), sse: vi.fn(), confirms: [], lines: [] }));
vi.mock('../../static/js/api.js', () => ({ secureFetch: fetch, consumeSSE: sse }));
vi.mock('../../static/js/ui.js', () => ({
    taskBegin: vi.fn(), taskEnd: vi.fn(), openModal: vi.fn(),
    confirmInline: (btn, opts) => confirms.push(opts),
    announceStatus: () => {},
    statusLine: (host, o) => { lines.push(o); host.textContent = o.text; return host; },
    relativeStamp: () => 'just now',
}));
import { mountLibraryRun } from '../../static/js/libraryRun.js';
const tick = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const ok = (b) => ({ ok: true, json: async () => b });
const BASE = { observation: { initialized: false, new: {} }, blocked: [], renames: [], tags: [], mirror: null, mirror_units: [] };
function run(review, applyResult, applyError) {
    fetch.mockResolvedValue(ok({ session: { id: 'S1' } }));
    sse.mockImplementation(async (url, opts, h) => {
        if (url.endsWith('/analyze')) h.onOther({ library_analyze: review });
        // A mid-stream outcome: the stream opened first (api.js onOpen).
        if (url.endsWith('/apply')) { h.onOpen?.(); if (applyError) h.onError(applyError); else h.onOther({ library_apply: applyResult }); }
    });
}
async function analyse(review, applyResult, applyError) {
    run(review, applyResult, applyError);
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
}
const MIRROR = { plan_digest: 'd', to_create: 1, to_refresh: 0, to_baseline: 0, listed: {} };
const RENAME = { unit: 'attachment:1:A', rel: 'a.pdf', name: 'a.pdf', proposal: 'jones_2026.pdf', can_apply: true };
beforeEach(() => { fetch.mockReset(); sse.mockReset(); confirms.length = 0; lines.length = 0; document.body.innerHTML = '<div id="host"></div>'; });

it('T1 select all ticks the mirror when it is the only proposal', async () => {
    await analyse({ ...BASE, mirror: MIRROR });
    document.getElementById('library-run-select-all').click();
    expect(document.getElementById('library-run-mirror').checked).toBe(true);
});
it('T2 select all with renames ticked but mirror unticked ticks everything', async () => {
    await analyse({ ...BASE, renames: [RENAME], mirror: MIRROR });
    document.querySelector('input[data-kind=rename]').checked = true;
    document.getElementById('library-run-select-all').click();
    expect(document.getElementById('library-run-mirror').checked).toBe(true);
});
it('T3 a blocked analysis (no observation) does not claim nothing is new', async () => {
    await analyse({ ...BASE, observation: undefined, blocked: ['A scan is running; wait for it, then analyse again.'] });
    const t = document.getElementById('library-run-review').textContent;
    expect(t).not.toContain('Nothing new since the last run');
    expect(document.getElementById('library-run-apply')).toBeNull();
});
it('T4 statusLine gets a millisecond timestamp (ui.js relativeStamp contract)', async () => {
    await analyse({ ...BASE });
    expect(lines.at(-1).at).toBeGreaterThan(1e12);
});
it('T5 after a successful apply the stale review cannot be re-applied', async () => {
    await analyse({ ...BASE, renames: [RENAME] }, { renames: [{ unit: RENAME.unit, from: 'a.pdf', to: 'jones_2026.pdf', status: 'done' }], tags: [], mirror: null });
    document.getElementById('library-run-select-all').click();
    document.getElementById('library-run-apply').click();
    await confirms[0].onConfirm(); await tick();
    const apply = document.getElementById('library-run-apply');
    const stale = apply && !apply.disabled && document.querySelector('input[data-kind=rename]').checked;
    expect(stale).toBe(false);
});
it('T6 a not_attempted tag row shows its reason', async () => {
    await analyse({ ...BASE, renames: [RENAME] }, { renames: [], tags: [{ unit: 'attachment:1:A', status: 'not_attempted', reason: 'its rename is failed' }], mirror: null });
    document.getElementById('library-run-select-all').click();
    document.getElementById('library-run-apply').click();
    await confirms[0].onConfirm(); await tick();
    expect(document.getElementById('library-run-results').textContent).toContain('its rename is failed');
});
it('T7 a stopped mirror says why', async () => {
    await analyse({ ...BASE, mirror: MIRROR }, { renames: [], tags: [], mirror: { status: 'failed', ok: false, stopped: 'plan_changed' } });
    document.getElementById('library-run-mirror').checked = true;
    document.getElementById('library-run-apply').click();
    await confirms[0].onConfirm(); await tick();
    const t = document.getElementById('library-run-results').textContent;
    expect(t).toMatch(/plan_changed|changed since/);
});
it('T8 an uncertain rename (tracker refused) shows the tracker error', async () => {
    await analyse({ ...BASE, renames: [RENAME] }, { renames: [{ unit: RENAME.unit, status: 'uncertain', from: 'a.pdf', to: 'jones_2026.pdf', new_rel: 'x', tracker: 'identity mismatch' }], tags: [], mirror: null });
    document.getElementById('library-run-select-all').click();
    document.getElementById('library-run-apply').click();
    await confirms[0].onConfirm(); await tick();
    expect(document.getElementById('library-run-results').textContent).toContain('identity mismatch');
});
it('T9 an apply stream that ends in an error tells the user something may have been written', async () => {
    await analyse({ ...BASE, renames: [RENAME] }, null, 'Generation timed out');
    document.getElementById('library-run-select-all').click();
    document.getElementById('library-run-apply').click();
    await confirms[0].onConfirm(); await tick();
    const all = document.getElementById('host').textContent;
    expect(all).toMatch(/may have|some .*written|analy[sz]e again/i);
});
it('T10 a no_text tag row with source none is not labelled "from the PDF text"', async () => {
    await analyse({ ...BASE, tags: [{ unit: 'entry:1:E', citation_key: 'k', status: 'no_text', source: 'none', proposed: [], error: null, from_cache: false, sent: null }] });
    const t = document.getElementById('library-run-review').textContent;
    expect(t).not.toContain('from the PDF text');
});
it('T11 two distinct tags get distinct checkbox ids', async () => {
    await analyse({ ...BASE, tags: [{ unit: 'attachment:1:A', citation_key: 'k', source: 'pdf', proposed: [{ tag: 'dépression' }, { tag: 'dèpression' }], sent: { online: false } }] });
    const ids = [...document.querySelectorAll('input[data-kind=tag]')].map((b) => b.id);
    expect(new Set(ids).size).toBe(2);
});
it('T12 a mirror whose dry-run plans nothing still offers a checkbox that would send an apply', async () => {
    await analyse({ ...BASE, mirror: { plan_digest: 'd', to_create: 0, to_refresh: 0, to_baseline: 0, listed: {} } });
    expect(document.getElementById('library-run-mirror')).toBeNull();
});
