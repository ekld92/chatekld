// "Mirror from Zotero" — the one confirmed verb that creates, refreshes and
// baselines the Obsidian mirror (2026-09-20).
//
// Why these are BEHAVIOURAL and not source ratchets: the defect this replaces
// is a rendering hole (the bootstrap affordance was painted only when
// zotero-authoritative mode was OFF, i.e. never, for the maintainer), and a
// source match on `notesync-bootstrap` would have passed the whole time it was
// invisible. Every assertion here is about what the tab shows and what it
// sends. The `_nsSay` assertions matter for the same reason: reporting an
// outcome only to the screen-reader announcer is a defect this repo has now
// shipped three times (accession deck, tagging deck, Not-in-Zotero tab).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const AUDIT_JS = readFileSync(join(ROOT, 'static/js/audit.js'), 'utf8');

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

function jsonOk(payload) {
    return { ok: true, status: 200, json: async () => payload };
}

/** A Response-shaped object `readSSE` can drain. */
function sse(frames) {
    const text = frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('') + 'data: [DONE]\n\n';
    const chunks = [new TextEncoder().encode(text)];
    let i = 0;
    return {
        ok: true,
        status: 200,
        body: {
            getReader: () => ({
                read: async () => (i < chunks.length
                    ? { value: chunks[i++], done: false }
                    : { value: undefined, done: true }),
                cancel: () => {},
                releaseLock: () => {},
            }),
        },
    };
}

function row(key, state, extra = {}) {
    return {
        citation_key: key, state,
        missing_words: 0, extra_words: 0,
        region_changed: false, zotero_changed: false, baselined: true,
        note_rel: `Z_Zotero_Notes/${key}.md`,
        ...extra,
    };
}

const DRY = {
    dry_run: true, ok: true, stopped: null, git_clean: true, git_detail: '',
    plan_digest: 'digest-of-the-reviewed-plan',
    scanned_at: Math.floor(Date.now() / 1000) - 60,
    to_create: 2, create_total: 2, create_capped_at: null,
    create_keys: ['newOne', 'newTwo'],
    to_refresh: 1, refresh_keys: ['staleOne'], to_baseline: 3,
    unchanged: 5, lossy_markup: [], listed: {}, stale_bases: 0,
    skipped: { create: {} },
    created: 0, refreshed: 0, bootstrapped: 0, bases_written: 0,
    stale_bases_removed: 0, failed: {}, aborted: false,
};

const DONE = {
    ...DRY, dry_run: false, created: 2, refreshed: 1, bootstrapped: 3,
    bases_written: 4, failed: {},
};

function harness({ authoritative = true, sync = null, status = null,
                   mirrorDry = DRY, mirrorWrite = DONE } = {}) {
    const calls = [];
    const store = new Map();
    vi.stubGlobal('localStorage', {
        getItem: (k) => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => store.set(k, String(v)),
        removeItem: (k) => store.delete(k),
    });
    vi.stubGlobal('fetch', vi.fn(async (url, opts) => {
        const u = String(url);
        calls.push([u, opts && opts.body ? JSON.parse(opts.body) : null]);
        if (u.includes('/api/notesync/mirror')) {
            const body = opts && opts.body ? JSON.parse(opts.body) : {};
            return sse([{ info: 'planning' },
                        { mirror: body.confirm ? mirrorWrite : mirrorDry }]);
        }
        if (u.includes('/api/audit/summary')) return jsonOk({ summary: {} });
        if (u.includes('/api/notesync/sync-status')) {
            return jsonOk(sync || {
                authoritative, total: 4,
                counts: { in_sync: 1, missing_obsidian: 2, pull: 1 },
                rows: [row('inStep', 'in_sync'),
                       row('newOne', 'missing_obsidian'),
                       row('newTwo', 'missing_obsidian'),
                       row('staleOne', 'pull', { zotero_changed: true, missing_words: 4 })],
            });
        }
        if (u.includes('/api/notesync/status')) {
            return jsonOk(status || { with_reading_note: 4, ok: 1, missing: [], drifted: [] });
        }
        if (u.includes('/api/notesync/create-attempts')) return jsonOk({ rows: [] });
        if (u.includes('/api/notesync/key-changes')) return jsonOk({ count: 0, rows: [] });
        return jsonOk({ ok: true });
    }));
    return calls;
}

async function loadTab() {
    vi.resetModules();
    const { selectAuditReport } = await import('../../static/js/audit.js');
    await selectAuditReport('note_sync');
    await vi.waitFor(() => {
        expect(document.getElementById('audit-ns-status')).toBeTruthy();
    });
}

const statusText = () => document.getElementById('audit-ns-status').textContent;

describe('Mirror from Zotero — what the tab shows', () => {
    beforeEach(() => { document.body.innerHTML = DOM; });
    afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = ''; });

    it('offers the one verb in authoritative mode, beside the narrower sweep', async () => {
        harness({ authoritative: true });
        await loadTab();
        expect(document.querySelector('[data-action="notesync-mirror"]')).toBeTruthy();
        expect(document.querySelector('[data-action="notesync-refresh-all"]')).toBeTruthy();
        const body = document.getElementById('audit-report-body').textContent;
        expect(body).toContain('[mirror from Zotero]');
    });

    it('a missing mirror is a ROW in the sync table, not only a separate list', async () => {
        // It was counted in the summary line and then absent from the table:
        // the only list of it came from the CACHED scan, a different source.
        harness();
        await loadTab();
        const tr = document.querySelector(
            '#audit-report-body .notesync-twoway tbody tr[data-state="missing_obsidian"]');
        expect(tr).toBeTruthy();
        expect(tr.getAttribute('data-key')).toBe('newOne');
        expect(tr.textContent).toContain('no reference note');
        expect(tr.querySelector('[data-action="notesync-create"]')).toBeTruthy();
    });

    it('the cached-scan Missing section shows only what the fresh read disagrees with', async () => {
        harness({
            status: {
                with_reading_note: 4, ok: 1, drifted: [],
                missing: [
                    { citation_key: 'newOne', note_rel: 'x.md', zotero_chars: 10 },
                    { citation_key: 'onlyTheScanThinksSo', note_rel: 'y.md', zotero_chars: 10 },
                ],
            },
        });
        await loadTab();
        const rows = [...document.querySelectorAll(
            '#audit-report-body .notesync-missing tbody tr[data-key]')]
            .map((tr) => tr.getAttribute('data-key'));
        expect(rows).toEqual(['onlyTheScanThinksSo']);
        expect(document.getElementById('audit-report-body').textContent)
            .toContain('Missing according to the last scan only');
    });

    it('baselining is offered in authoritative mode too', async () => {
        // It writes only the app's own records, and hiding it under `!auth`
        // made a note with no sync point unbaselinable from the app.
        harness({
            sync: {
                authoritative: true, total: 1, counts: { unbaselined: 1 },
                rows: [row('needsABase', 'unbaselined',
                           { baselined: false, bootstrap_ready: true })],
            },
        });
        await loadTab();
        expect(document.querySelector('[data-action="notesync-bootstrap"]')).toBeTruthy();
    });
});

describe('Mirror from Zotero — dry run, confirm, write', () => {
    beforeEach(() => { document.body.innerHTML = DOM; });
    afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = ''; });

    async function clickMirror() {
        const btn = document.querySelector('[data-action="notesync-mirror"]');
        btn.click();
        await vi.waitFor(() => {
            expect(document.getElementById('ns-mirror-plan')).toBeTruthy();
        });
        return btn;
    }

    it('dry-runs first and names all three phases plus all three undo stories', async () => {
        const calls = harness();
        await loadTab();
        await clickMirror();
        const mirrorCalls = calls.filter(([u]) => u.includes('/api/notesync/mirror'));
        expect(mirrorCalls).toHaveLength(1);
        expect(mirrorCalls[0][1]).toEqual({ dry_run: true });
        const strip = document.querySelector('.confirm-inline, [data-confirm-inline]')
            || document.getElementById('audit-report-body');
        const text = strip.textContent;
        expect(text).toContain('Create 2 note(s)');
        expect(text).toContain('rewrite the Zotero region of 1 note(s)');
        expect(text).toContain('record 3 sync point(s)');
        // three different undos, named
        expect(text).toContain('Settings → History and Restore');
        expect(text).toContain('NOT journalled');
        expect(text).toContain('git');
        expect(text).toContain('write nothing to your vault or to Zotero');
    });

    it('sends confirm only after the confirm is clicked, and reports the outcome VISIBLY', async () => {
        const calls = harness();
        await loadTab();
        const btn = await clickMirror();
        const confirmBtn = [...document.querySelectorAll('button')]
            .find((b) => /^Mirror 3$/.test(b.textContent.trim()));
        expect(confirmBtn, 'the confirm button must quote the real count').toBeTruthy();
        confirmBtn.click();
        await vi.waitFor(() => {
            expect(statusText()).toContain('Created 2');
        });
        const bodies = calls.filter(([u]) => u.includes('/api/notesync/mirror')).map(([, b]) => b);
        // The confirm is bound to the plan the user saw (2026-09-21, lot 1):
        // it sends back the dry run's digest, and nothing else decides it.
        expect(bodies).toEqual([{ dry_run: true },
            { confirm: true, plan_digest: 'digest-of-the-reviewed-plan' }]);
        // Visible, not only announced (the three-times-shipped defect).
        expect(statusText()).toContain('refreshed 1');
        expect(statusText()).toContain('baselined 3');
        expect(btn).toBeTruthy();
    });

    it('a capped batch says "N of M" rather than implying the rest were not there', async () => {
        harness({ mirrorDry: { ...DRY, to_create: 100, create_total: 137, create_capped_at: 100 } });
        await loadTab();
        await clickMirror();
        const text = document.getElementById('audit-report-body').textContent;
        expect(text).toContain('Only 100 of 137 missing note(s) fit one undoable batch');
    });

    it('a stale snapshot names the remedy in the confirm', async () => {
        harness({ mirrorDry: { ...DRY, scanned_at: Math.floor(Date.now() / 1000) - 3600 } });
        await loadTab();
        await clickMirror();
        expect(document.getElementById('audit-report-body').textContent)
            .toContain('papermind audit scan');
    });

    it('a nothing-to-do plan says so and never spends a confirm', async () => {
        const calls = harness({
            mirrorDry: { ...DRY, to_create: 0, to_refresh: 0, to_baseline: 0, stale_bases: 0,
                         create_keys: [], refresh_keys: [], unchanged: 1237 },
        });
        await loadTab();
        document.querySelector('[data-action="notesync-mirror"]').click();
        await vi.waitFor(() => {
            expect(statusText()).toContain('Nothing to mirror');
        });
        expect(statusText()).toContain('1237');
        expect([...document.querySelectorAll('button')]
            .some((b) => /^Mirror /.test(b.textContent.trim()))).toBe(false);
        expect(calls.filter(([u]) => u.includes('/api/notesync/mirror'))).toHaveLength(1);
    });

    it('a refusal reaches the visible status line, not only the announcer', async () => {
        vi.stubGlobal('localStorage', {
            getItem: () => null, setItem: () => {}, removeItem: () => {},
        });
        const base = harness();
        void base;
        const realFetch = globalThis.fetch;
        vi.stubGlobal('fetch', vi.fn(async (url, opts) => {
            if (String(url).includes('/api/notesync/mirror')) {
                return { ok: false, status: 409,
                         json: async () => ({ error: 'Refusing to mirror the notes: 2 uncommitted change(s).',
                                              reason: 'git_not_clean' }) };
            }
            return realFetch(url, opts);
        }));
        await loadTab();
        document.querySelector('[data-action="notesync-mirror"]').click();
        await vi.waitFor(() => {
            expect(statusText()).toContain('uncommitted');
        });
        expect(document.getElementById('audit-ns-status').classList.contains('is-error')).toBe(true);
    });
});

describe('Mirror from Zotero — wiring', () => {
    it('is dispatched, and the write body carries confirm and the reviewed digest', () => {
        expect(AUDIT_JS).toContain("if (action === 'notesync-mirror') { _noteSyncMirror(btn); return; }");
        const fn = AUDIT_JS.indexOf('async function _noteSyncMirror(');
        const next = AUDIT_JS.indexOf('\n/** Zotero-authoritative sweep', fn);
        const b = AUDIT_JS.slice(fn, next);
        expect(b).toContain("JSON.stringify({ dry_run: true })");
        expect(b).toContain("JSON.stringify({ confirm: true, plan_digest: plan.plan_digest })");
        expect(b).toContain('confirmInline(btn');
        // Never chunked past the cap: the client only ever asks the server for
        // one sweep and reports what it capped at.
        expect(b).not.toContain('slice(0, _NOTESYNC_MAX_CREATE)');
    });
});
