// Source ratchet over audit.js's two-way Zotero note sync (2026-09-04,
// re-aimed 2026-09-07 when the tab became one table with three buttons).
//
// The safety argument moved but did not weaken. It WAS "one verb per state":
// a `conflict` row carried only [merge…] because either direction deletes
// something. That also made "take Zotero's" unreachable on a row the app had
// summarised as `push`, which is the one row a person most wants to say it
// about. Now every comparable row carries all three verbs and the protection
// is the SECOND acknowledgement, attached per DIRECTION rather than per row:
// `_nsNeedsAccept` must mirror the server's `_needs_accept`, the per-row
// dispatch must read it off the row, and `accept_conflict` must still never
// ride a bulk request. Every one of those is a client-side decision the
// Python suite cannot see, so it is pinned here.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const AUDIT_JS = readFileSync(join(ROOT, 'static/js/audit.js'), 'utf8');

function block(name) {
    const fn = AUDIT_JS.indexOf(`function ${name}(`);
    expect(fn, `${name} not found — update this ratchet`).toBeGreaterThan(-1);
    const next = AUDIT_JS.indexOf('\nfunction ', fn + 1);
    return AUDIT_JS.slice(fn, next > -1 ? next : undefined);
}

describe('two-way sync: one verb per state', () => {
    it('every comparable row carries all three verbs', () => {
        const b = block('_renderTwoWaySync');
        const branch = b.slice(b.indexOf('_NS_COMPARABLE.has(r.state)'), b.indexOf("r.state === 'missing_zotero'"));
        for (const action of ['notesync-pull', 'notesync-push', 'notesync-merge']) {
            expect(branch, action).toContain(action);
        }
    });

    it('a row with no comparable region carries none of them', () => {
        // `missing_zotero` gets its own create verb; `unparsed` and
        // `multiple_notes` get nothing, because there is no region to write
        // and nothing to write from. The set is what bounds the widening, so
        // it is pinned member by member — adding `unparsed` to it would hand
        // three destructive buttons to a note whose halves cannot be told
        // apart.
        const m = AUDIT_JS.match(/const _NS_COMPARABLE = new Set\(\[([^\]]*)\]\)/);
        expect(m, '_NS_COMPARABLE not found — update this ratchet').toBeTruthy();
        const members = m[1].split(',').map((x) => x.trim().replace(/^'|'$/g, '')).filter(Boolean);
        expect(members.sort()).toEqual(
            ['conflict', 'in_sync', 'pull', 'push', 'unbaselined'].sort());
        // And the renderer falls through to no verb at all for anything else.
        const b = block('_renderTwoWaySync');
        const tail = b.slice(b.indexOf("r.state === 'missing_zotero'"));
        expect(tail).toContain("return '';");
    });

    it('the client mirrors the server\'s acknowledgement rule exactly', () => {
        // A courtesy, not the gate — the route re-derives it — but a client
        // that under-reported it would spend a bare confirm on a destructive
        // write and be refused, which reads as a broken button.
        const b = block('_nsNeedsAccept');
        expect(b).toContain("direction === 'pull' ? r.region_changed : r.zotero_changed");
        expect(b).toContain("direction === 'pull' ? r.extra_words : r.missing_words");
        expect(b).toContain('r.baselined');
    });

    it('a missing Zotero note gets the opt-in create verb, per row only', () => {
        const b = block('_renderTwoWaySync');
        expect(b).toContain("r.state === 'missing_zotero'");
        expect(b).toContain('notesync-create-zotero');
        expect(b).not.toContain('notesync-create-zotero-all');
    });

    it('in-step notes are counted, not listed', () => {
        const b = block('_renderTwoWaySync');
        expect(b).toContain("r.state !== 'in_sync'");
    });

    it('baselining is offered FIRST while any note lacks a sync point', () => {
        // Without a base the state is a word-count guess and the merge view
        // has no "before"; the button writes nothing to the vault or Zotero,
        // so there is no reason to bury it under the table.
        const b = block('_renderTwoWaySync');
        const cta = b.indexOf('notesync-bootstrap');
        const table = b.indexOf('notesync-twoway');
        expect(cta).toBeGreaterThan(-1);
        expect(cta, 'the baseline call-to-action must precede the table').toBeLessThan(table);
        expect(b).toContain('bootstrappable.length');
    });
});

describe('two-way sync: bulk verbs', () => {
    it('select by state from VISIBLE rows and cap at the server bounds', () => {
        const b = block('_noteSyncVisibleKeys');
        expect(b).toContain("getAttribute('data-state') === state");
        expect(b).toContain('!tr.hidden');
        expect(b).toContain('.slice(0, cap)');
        expect(AUDIT_JS).toContain('const _NOTESYNC_MAX_PUSH = 50;');
        expect(AUDIT_JS).toContain('const _NOTESYNC_MAX_PULL = 100;');
        expect(AUDIT_JS).toContain("_noteSyncVisibleKeys('pull', _NOTESYNC_MAX_PULL)");
        expect(AUDIT_JS).toContain("_noteSyncVisibleKeys('push', _NOTESYNC_MAX_PUSH)");
    });

    it('never send accept_conflict on a bulk request', () => {
        // The bulk dispatch lines pass no `accept`; only the per-row dispatch
        // does, and only when that row's own flag for that DIRECTION is set.
        // Match the DISPATCH branches, not every mention of the action name:
        // the ACTION_ROLES table also names them, and a declaration is not a
        // request (2026-09-20 — the looser filter counted 4 and failed).
        const bulkLines = AUDIT_JS.split('\n').filter(l => l.includes("action === 'notesync-pull-all'") || l.includes("action === 'notesync-push-all'"));
        expect(bulkLines.length).toBe(2);
        for (const l of bulkLines) expect(l).not.toContain('accept');
        const rowLines = AUDIT_JS.split('\n').filter(l => l.includes("action === 'notesync-pull')") || l.includes("action === 'notesync-push')"));
        expect(rowLines.length).toBe(2);
        // Per DIRECTION, off the row the renderer stamped — never a literal,
        // and never the row's state name (which cannot distinguish the two).
        expect(rowLines[0]).toContain("getAttribute('data-accept-pull') === '1'");
        expect(rowLines[1]).toContain("getAttribute('data-accept-push') === '1'");
        const b = block('_noteSyncBulk');
        expect(b).toContain('if (accept) body.accept_conflict = true;');
        expect(b.split('\n').filter(l => l.includes('accept_conflict: true'))).toEqual([]);
    });

    it('every write is confirm-gated through the shared ceremony and sends confirm: true', () => {
        for (const name of ['_noteSyncBulk', '_noteSyncBootstrap', '_noteSyncCreateZotero', '_noteSyncMergeWrite']) {
            const b = block(name);
            expect(b, name).toContain('confirmInline(btn, {');
            expect(b, name).toContain('confirm: true');
        }
    });
});

describe('two-way sync: the merge view', () => {
    it('refuses a result that still carries conflict markers before asking', () => {
        const b = block('_noteSyncMergeWrite');
        expect(b).toContain('<<<<<<< ');
        expect(b.indexOf('conflict markers')).toBeLessThan(b.indexOf('confirmInline'));
    });

    it('renders both diffs against the last sync point and never uses innerHTML for vault text', () => {
        const b = block('_noteSyncMerge');
        expect((b.match(/renderSideBySideDiff\(d\.base, d\.(ours|theirs)/g) || []).length).toBe(2);
        expect(b).toContain("tr.setAttribute('data-row-attachment', '')");
        expect(b).toContain('ta.value = d.merged');
        expect(b).not.toContain('innerHTML');
    });

    it('sends the preview\'s content hash so the write is stale-diff guarded', () => {
        const b = block('_noteSyncMergeWrite');
        expect(b).toContain('content_sha256: contentSha');
    });
});

describe('zotero-authoritative mode (2026-09-19)', () => {
    // The reading notes are edited only in Zotero and the folder is a
    // read-only mirror. On, the Obsidian → Zotero verbs must vanish — a
    // [keep mine] on a mirror would push stale text back — and the one
    // sweep replaces the per-row decisions. A note whose reading note is
    // gone is listed, never offered [create in Zotero].
    it('hides keep-mine, merge and push-all when authoritative', () => {
        const b = block('_renderTwoWaySync');
        expect(b).toContain('const auth = !!sync.authoritative;');
        expect(b).toContain("if (auth) return `<button type=\"button\" class=\"audit-link-btn\" data-action=\"notesync-pull\"");
        expect(b).toContain("n('push') && !auth ?");
        const tail = b.slice(b.indexOf("r.state === 'missing_zotero'"));
        expect(tail).toContain("if (auth) return '';");
    });

    it('offers the sweep behind a dry run and a confirm', () => {
        expect(block('_renderTwoWaySync')).toContain('notesync-refresh-all');
        const b = block('_noteSyncRefreshAll');
        expect(b).toContain("JSON.stringify({ dry_run: true })");
        expect(b).toContain('confirmInline(btn');
        expect(b).toContain("JSON.stringify({ confirm: true })");
        expect(AUDIT_JS).toContain("if (action === 'notesync-refresh-all') { _noteSyncRefreshAll(btn); return; }");
    });
});

// Behavioural twin of the ratchet above. A source match on `if (auth)` would
// still pass if the verbs were painted anyway — the question is what the
// tab actually shows.
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

function row(key, state) {
    return {
        citation_key: key, state,
        missing_words: 1, extra_words: 1,
        region_changed: true, zotero_changed: true, baselined: true,
    };
}

function syncPayload(authoritative) {
    return {
        authoritative,
        total: 4,
        counts: {
            in_sync: 0, pull: 1, push: 1, conflict: 1,
            missing_zotero: 1, unbaselined: 0,
        },
        rows: [
            row('keepMe', 'push'),
            row('mergeMe', 'conflict'),
            row('takeMe', 'pull'),
            row('goneNote', 'missing_zotero'),
        ],
    };
}

async function loadTab(authoritative) {
    const store = new Map();
    vi.stubGlobal('localStorage', {
        getItem: (k) => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => store.set(k, String(v)),
        removeItem: (k) => store.delete(k),
    });
    vi.stubGlobal('fetch', vi.fn(async (url) => {
        const u = String(url);
        if (u.includes('/api/audit/summary')) return jsonOk({ summary: {} });
        if (u.includes('/api/notesync/sync-status')) return jsonOk(syncPayload(authoritative));
        if (u.includes('/api/notesync/status')) {
            return jsonOk({ with_reading_note: 4, ok: 0, missing: [], drifted: [] });
        }
        if (u.includes('/api/notesync/create-attempts')) return jsonOk({ rows: [] });
        if (u.includes('/api/notesync/key-changes')) return jsonOk({ count: 0, rows: [] });
        return jsonOk({ ok: true });
    }));
    vi.resetModules();
    const { selectAuditReport } = await import('../../static/js/audit.js');
    await selectAuditReport('note_sync');
    await vi.waitFor(() => {
        expect(document.getElementById('audit-ns-status')).toBeTruthy();
    });
}

describe('zotero-authoritative mode: what the tab actually shows', () => {
    beforeEach(() => { document.body.innerHTML = DOM; });
    afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = ''; });

    it('offers refresh-all and hides keep-mine, merge, push-all and create-in-Zotero', async () => {
        await loadTab(true);
        const body = document.getElementById('audit-report-body').textContent;
        expect(body).toContain('[refresh all from Zotero]');
        expect(body).toContain('will be overwritten');
        expect(document.querySelector('[data-action="notesync-refresh-all"]')).toBeTruthy();
        expect(document.querySelector('[data-action="notesync-push"]')).toBeNull();
        expect(document.querySelector('[data-action="notesync-merge"]')).toBeNull();
        expect(document.querySelector('[data-action="notesync-push-all"]')).toBeNull();
        expect(document.querySelector('[data-action="notesync-pull-all"]')).toBeNull();
        expect(document.querySelector('[data-action="notesync-create-zotero"]')).toBeNull();
        expect(document.querySelector('[data-action="notesync-pull"]')).toBeTruthy();
        expect(body).not.toContain('[keep mine]');
        expect(body).not.toContain('[merge…]');
        expect(body).not.toContain('[create in Zotero]');
        expect(body).not.toContain('[take Zotero’s for');
    });

    it('when the mode is off, the Obsidian → Zotero verbs are on the tab', async () => {
        await loadTab(false);
        expect(document.querySelector('[data-action="notesync-refresh-all"]')).toBeNull();
        expect(document.querySelector('[data-action="notesync-push"]')).toBeTruthy();
        expect(document.querySelector('[data-action="notesync-merge"]')).toBeTruthy();
        expect(document.querySelector('[data-action="notesync-push-all"]')).toBeTruthy();
        expect(document.querySelector('[data-action="notesync-pull-all"]')).toBeTruthy();
        expect(document.querySelector('[data-action="notesync-create-zotero"]')).toBeTruthy();
    });
});
