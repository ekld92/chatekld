// Batch E's create verb, and the two bounds the batch-undo fix put on it.
//
// The confirm dialog promises "Reversible from Settings → History and Restore". The
// refactor journal retains a bounded number of evictable ops, so a batch
// larger than half that window cannot keep the promise once any later write
// prunes — the server refuses it, and this pins that the UI never offers one.
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';

const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

function statusPayload(nMissing) {
    return {
        threshold: 0.85,
        with_reading_note: nMissing + 3,
        ok: 3,
        missing: Array.from({ length: nMissing }, (_, i) => ({
            citation_key: `miss${String(i).padStart(4, '0')}`,
            note_rel: `Z_Zotero_Notes/miss${i}.md`, zotero_chars: 40,
        })),
        drifted: [{
            citation_key: 'driftedKey', note_rel: 'Z_Zotero_Notes/d.md',
            containment: 0.5, zotero_chars: 13, note_chars: 508,
        }],
    };
}

describe('Zotero Notes Sync — the create verb', () => {
    let audit; let posted;

    async function render(nMissing) {
        posted = [];
        document.body.innerHTML = `
            <div id="audit-report-tabs"></div>
            <div id="audit-summary"></div>
            <div id="audit-report-meta"></div>
            <div id="audit-report-body"></div>
            <div id="audit-search-bar"></div>
            <div id="audit-search-results"></div>
            <div id="sr-status-announcer" role="status"></div>`;
        vi.stubGlobal('fetch', vi.fn(async (url, opts) => {
            if (opts && opts.method === 'POST') {
                posted.push({ url, body: JSON.parse(opts.body) });
                return { ok: true, status: 200, json: async () => ({ ok: true, created: 1, results: {} }) };
            }
            if (String(url).includes('/api/notesync/status')) {
                return { ok: true, status: 200, json: async () => statusPayload(nMissing) };
            }
            if (String(url).includes('/api/audit/summary')) {
                return { ok: true, status: 200, json: async () => ({ summary: {} }) };
            }
            return { ok: true, status: 200, json: async () => ({}) };
        }));
        vi.resetModules();
        audit = await import('../../static/js/audit.js');
        await audit.selectAuditReport('note_sync');
        await flush();
        return document.getElementById('audit-report-body');
    }

    beforeEach(() => {
        vi.stubGlobal('localStorage', {
            getItem: () => null, setItem: () => {}, removeItem: () => {},
        });
    });
    afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

    it('offers to create every missing note when the batch fits', async () => {
        const body = await render(4);
        const btn = body.querySelector('[data-action="notesync-create-all"]');
        expect(btn.textContent).toContain('4');
        expect(btn.textContent).toContain('all');
    });

    it('never offers a batch the restore journal could not undo', async () => {
        const body = await render(260);
        const btn = body.querySelector('[data-action="notesync-create-all"]');
        // Says the real number rather than "all 260": the server refuses that.
        expect(btn.textContent).toContain('100');
        expect(btn.textContent).toContain('of 260');

        btn.click();
        await flush();
        document.querySelector('.inline-confirm-strip button.btn-danger, .inline-confirm-strip button:last-of-type').click();
        await flush();
        const create = posted.filter((p) => p.url.includes('/api/notesync/create'));
        expect(create).toHaveLength(1);
        expect(create[0].body.citation_keys).toHaveLength(100);
    });

    it('a drifted note is never posted to a create', async () => {
        const body = await render(2);
        body.querySelector('[data-action="notesync-create-all"]').click();
        await flush();
        document.querySelector('.inline-confirm-strip button.btn-danger, .inline-confirm-strip button:last-of-type').click();
        await flush();
        const keys = posted.find((p) => p.url.includes('/api/notesync/create')).body.citation_keys;
        expect(keys).not.toContain('driftedKey');
        expect(keys).toEqual(['miss0000', 'miss0001']);
    });
});
