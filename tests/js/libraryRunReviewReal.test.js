// Adversarial review of lot 6 (2026-09-21), REAL ui.js (focus, status stamp): red on 89485693.
import { beforeEach, expect, it, vi } from 'vitest';
const { fetch, sse } = vi.hoisted(() => ({ fetch: vi.fn(), sse: vi.fn() }));
vi.mock('../../static/js/api.js', () => ({ secureFetch: fetch, consumeSSE: sse, logError: () => {} }));
import { mountLibraryRun } from '../../static/js/libraryRun.js';
const tick = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
beforeEach(() => { document.body.innerHTML = '<div id="sr-status-announcer"></div><div id="host"></div>'; });
it('R1 focus is not dropped to <body> after confirming apply', async () => {
    let release;
    fetch.mockResolvedValue({ ok: true, json: async () => ({ session: { id: 'S1' } }) });
    sse.mockImplementation(async (url, o, h) => {
        if (url.endsWith('/analyze')) h.onOther({ library_analyze: { observation: { new: {} }, blocked: [], renames: [{ unit: 'u', rel: 'a.pdf', name: 'a.pdf', proposal: 'b_2026.pdf', can_apply: true }], tags: [], mirror: null } });
        if (url.endsWith('/apply')) { await new Promise((r) => { release = r; }); h.onOther({ library_apply: { renames: [{ unit: 'u', status: 'done', from: 'a.pdf', to: 'b_2026.pdf' }], tags: [], mirror: null } }); }
    });
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    document.getElementById('library-run-select-all').click();
    const apply = document.getElementById('library-run-apply');
    apply.focus(); apply.click();
    const yes = document.querySelector('.inline-confirm-strip .btn-primary');
    yes.focus(); yes.click(); await tick();
    const during = document.activeElement === document.body;
    release(); await tick();
    const after = document.activeElement === document.body;
    expect({ during, after }).toEqual({ during: false, after: false });
});
it('R2 the status line shows a relative time stamp', async () => {
    fetch.mockResolvedValue({ ok: false, json: async () => ({ error: 'nope' }) });
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    expect(document.querySelector('#library-run-status .status-at')).not.toBeNull();
});
