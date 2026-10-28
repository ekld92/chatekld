// Notes Sync outcomes must be visible on the tab, not only announced
// (2026-09-19). Fourth instance of the class: refresh-all / bootstrap /
// layout migrate reported through announceStatus, then `_loadReport`
// rebuilt the body and the identical rows made the write look like a no-op.
//
// Behavioural on purpose. A source ratchet on announceStatus would pass
// against the broken code — it called the announcer faithfully.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

let selectAuditReport;

function jsonOk(payload) {
    return { ok: true, status: 200, json: async () => payload };
}

const DOM = `
  <div id="audit-summary" style="display:none;"></div>
  <div id="audit-report-tabs" style="display:none;"></div>
  <div id="audit-search-bar" style="display:none;">
    <input id="audit-search-input"><button id="audit-search-clear"></button>
  </div>
  <div id="audit-status-banner" role="status" style="display:none;"></div>
  <div id="audit-report-meta"></div>
  <div id="audit-search-results" style="display:none;"></div>
  <div id="audit-report-body"></div>
  <div id="audit-empty-state"></div>
  <div id="sr-status-announcer" role="status"></div>
`;

const STATUS = {
    with_reading_note: 2, ok: 1, missing: [], drifted: [],
};

function syncPayload(extra = {}) {
    return {
        total: 2,
        counts: { in_sync: 1, unbaselined: 1, pull: 0, push: 0, conflict: 0 },
        rows: [{
            citation_key: 'paperA', state: 'unbaselined',
            bootstrap_ready: true, missing_words: 0, extra_words: 0,
            region_changed: false, zotero_changed: false, baselined: false,
        }],
        ...extra,
    };
}

const KEY_CHANGES = {
    count: 1,
    rows: [{
        old_key: 'oldKey', new_key: 'newKey', note_rel: 'Z_Zotero_Notes/oldKey.md',
        item_key: 'ABC123', new_rel: 'Z_Zotero_Notes/newKey.md', state: 'ready',
    }],
};

function statusText() {
    const el = document.getElementById('audit-ns-status');
    return el ? el.textContent : null;
}

function acceptConfirm() {
    const strip = document.querySelector('.inline-confirm-strip');
    expect(strip, 'no confirm strip appeared').toBeTruthy();
    const buttons = [...strip.querySelectorAll('button')];
    buttons[buttons.length - 1].click();
}

function mkFetch(overrides = {}) {
    return vi.fn(async (url, opts) => {
        const u = String(url);
        for (const [needle, resp] of Object.entries(overrides)) {
            if (u.includes(needle)) {
                return typeof resp === 'function' ? resp(u, opts) : resp;
            }
        }
        if (u.includes('/api/audit/summary')) return jsonOk({ summary: {} });
        if (u.includes('/api/notesync/sync-status')) return jsonOk(syncPayload());
        if (u.includes('/api/notesync/status')) return jsonOk(STATUS);
        if (u.includes('/api/notesync/create-attempts')) return jsonOk({ rows: [] });
        if (u.includes('/api/notesync/key-changes')) return jsonOk(KEY_CHANGES);
        return jsonOk({ ok: true });
    });
}

async function load(fetchMock) {
    const store = new Map();
    vi.stubGlobal('localStorage', {
        getItem: (k) => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => store.set(k, String(v)),
        removeItem: (k) => store.delete(k),
    });
    vi.stubGlobal('fetch', fetchMock);
    vi.resetModules();
    ({ selectAuditReport } = await import('../../static/js/audit.js'));
    await selectAuditReport('note_sync');
    await vi.waitFor(() => {
        expect(document.getElementById('audit-ns-status')).toBeTruthy();
    });
}

beforeEach(() => { document.body.innerHTML = DOM; });
afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = ''; });

describe('Notes Sync: an outcome is visible after the report re-renders', () => {
    it('keeps the bootstrap outcome on #audit-ns-status after _loadReport', async () => {
        const fetchMock = mkFetch({
            '/api/notesync/bootstrap': jsonOk({ written: 3, skipped: {}, rebaselined: [] }),
        });
        await load(fetchMock);
        const btn = document.querySelector('[data-action="notesync-bootstrap"]');
        expect(btn, 'bootstrap verb missing').toBeTruthy();
        btn.click();
        acceptConfirm();
        await vi.waitFor(() => {
            expect(statusText()).toContain('Baselined 3');
        });
        expect(document.getElementById('audit-ns-status').textContent)
            .toContain('Baselined 3');
    });

    it('lists the refresh-all dry run, then keeps the write outcome after reload', async () => {
        const plan = {
            planned: 2, unchanged: 0, stale_bases: 0, plan_digest: 'reviewed',
            git_restorable: true, git_detail: '', git_restore_commit: 'abcdef0123456789',
            planned_keys: ['paperA', 'paperB'],
            lossy_markup: ['paperA'],
            listed: { deleted_in_zotero: ['goneNote'], no_record: ['orphan.md'] },
        };
        const fetchMock = mkFetch({
            '/api/notesync/sync-status': jsonOk(syncPayload({
                authoritative: true,
                counts: { in_sync: 0, pull: 1, push: 1, conflict: 0, unbaselined: 0 },
                rows: [{
                    citation_key: 'paperA', state: 'pull',
                    missing_words: 2, extra_words: 0,
                    region_changed: false, zotero_changed: true, baselined: true,
                }],
            })),
            '/api/notesync/refresh-all': (u, opts) => {
                const body = JSON.parse(opts.body);
                if (body.dry_run) return jsonOk(plan);
                // Bound to the reviewed plan (2026-09-21, lot 1).
                if (body.plan_digest !== 'reviewed') return jsonOk({ error: 'unbound' });
                return jsonOk({
                    written: 2, failed: {}, aborted: false, bases_written: 2,
                    listed: plan.listed,
                });
            },
        });
        await load(fetchMock);
        const btn = document.querySelector('[data-action="notesync-refresh-all"]');
        expect(btn, 'refresh-all verb missing').toBeTruthy();
        btn.click();
        await vi.waitFor(() => {
            const det = document.getElementById('ns-refresh-plan');
            expect(det, 'dry-run details missing').toBeTruthy();
            expect(det.textContent).toContain('paperA');
            expect(det.textContent).toContain('paperB');
            expect(det.textContent).toContain('goneNote');
            expect(det.textContent).toContain('orphan.md');
        });
        acceptConfirm();
        await vi.waitFor(() => {
            expect(statusText()).toContain('Refreshed 2');
        });
        expect(document.getElementById('audit-ns-status').textContent)
            .toContain('Refreshed 2');
    });

    it('shows Key changed in Zotero and keeps a rename outcome after reload', async () => {
        const fetchMock = mkFetch({
            '/api/notesync/rename-note': (u, opts) => {
                const body = JSON.parse(opts.body);
                const plan = { new_key: 'newKey', changes: ['alias oldKey', 'header line'] };
                if (body.dry_run) return jsonOk({ plan });
                return jsonOk({ ok: true, plan });
            },
        });
        await load(fetchMock);
        expect(document.getElementById('audit-report-body').textContent)
            .toContain('Key changed in Zotero');
        const btn = document.querySelector('[data-action="notesync-rename-note"]');
        expect(btn, 'rename-note verb missing').toBeTruthy();
        btn.click();
        await vi.waitFor(() => {
            expect(document.querySelector('.inline-confirm-strip')).toBeTruthy();
        });
        acceptConfirm();
        await vi.waitFor(() => {
            expect(statusText()).toContain('Renamed oldKey → newKey');
        });
    });
});
