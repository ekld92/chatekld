// Pins "Bib Entries Without PDFs" as a QUEUE rather than an overview.
//
// The report could only ever grow: mapping.json can record "this PDF is
// deliberately not in the bibliography", and nothing said the same thing in
// the other direction. 1,502 rows on the maintainer's library, 293 of them
// item types that never have a PDF at all.
//
// Guarded classes:
//   1. DISCLOSURE — a filtered list must never read as the whole truth. The
//      withheld count is shown (the Tag Hygiene under-reporting lesson).
//   2. CEREMONY — every dismissal is confirmInline-gated, and the bulk one
//      puts the real COUNT in the confirm text: "dismiss 40 webpages" is a
//      different decision from "dismiss one".
//   3. SCOPE — the bulk verb acts on exactly the visible rows of that type,
//      never on rows the filter is already hiding.
//   4. UNDO — clear_all is a server-side verb because the report ships the
//      count of what it hid, not the keys; the client cannot enumerate them.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

let auditMod;

function jsonOk(payload) {
    return { ok: true, status: 200, json: async () => payload };
}

function row(key, { item_type = 'journalArticle', year = '2020' } = {}) {
    return {
        citation_key: key, title: `Title ${key}`, year,
        author: 'Smith, A', has_zotero_match: true, item_type,
    };
}

const DOM = `
  <div id="audit-report-meta"></div>
  <div id="audit-report-body"></div>
  <div id="audit-report-tabs"></div>
  <div id="sr-status-announcer" role="status"></div>
`;

let payload;
let posted;

async function flush() {
    for (let i = 0; i < 6; i += 1) {
        await new Promise((resolve) => { setTimeout(resolve, 0); });
    }
}

function stubFetch() {
    posted = [];
    vi.stubGlobal('fetch', vi.fn(async (url, init) => {
        const u = String(url);
        if (init && init.method === 'POST') {
            posted.push({ url: u, body: JSON.parse(init.body || '{}') });
            return jsonOk({ ok: true, changed: 1 });
        }
        if (u.includes('/api/audit/reports/zotero_no_pdf')) return jsonOk(payload);
        return jsonOk({});
    }));
}

async function render() {
    stubFetch();
    await auditMod.selectAuditReport('zotero_no_pdf');
    await flush();
}

function confirmButton(label) {
    return [...document.querySelectorAll('button')]
        .find((b) => b.textContent.trim() === label);
}

describe('Bib Entries Without PDFs — the no-PDF queue', () => {
    beforeEach(async () => {
        document.body.innerHTML = DOM;
        vi.stubGlobal('localStorage', {
            getItem: () => null, setItem: () => {}, removeItem: () => {},
        });
        vi.resetModules();
        auditMod = await import('../../static/js/audit.js');
        payload = {
            name: 'zotero_no_pdf',
            rows: [row('a2020', { item_type: 'webpage' }),
                   row('b2021', { item_type: 'webpage' }),
                   row('c2022', { item_type: 'journalArticle' })],
            dismissed_count: 0,
            by_item_type: { webpage: 2, journalArticle: 1 },
            never_pdf_types: ['webpage', 'blogPost'],
        };
    });

    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it('offers a bulk dismissal only for the configured item types', async () => {
        await render();
        const body = document.getElementById('audit-report-body');
        const bulk = [...body.querySelectorAll('[data-action="nopdf-dismiss-type"]')]
            .map((b) => b.getAttribute('data-type'));
        // journalArticle is NOT in never_pdf_types, so it gets no bulk offer.
        expect(bulk).toEqual(['webpage']);
    });

    it('drives the bulk offer from the server list, not a hard-coded one', async () => {
        payload.never_pdf_types = [];
        await render();
        const body = document.getElementById('audit-report-body');
        expect(body.querySelector('[data-action="nopdf-dismiss-type"]')).toBeFalsy();
    });

    it('discloses how many rows are hidden rather than shortening silently', async () => {
        payload.dismissed_count = 293;
        await render();
        const body = document.getElementById('audit-report-body');
        expect(body.textContent).toContain('293');
        expect(body.querySelector('[data-action="nopdf-restore-all"]')).toBeTruthy();
    });

    it('a single dismissal is confirm-gated and sends one key', async () => {
        await render();
        const body = document.getElementById('audit-report-body');
        body.querySelector('[data-action="nopdf-dismiss"]').click();
        await flush();
        expect(posted).toHaveLength(0);          // strip open, nothing sent

        confirmButton('No PDF needed').click();
        await flush();
        const decision = posted.filter((p) => p.url.includes('pdf-decision'));
        expect(decision).toHaveLength(1);
        expect(decision[0].body).toMatchObject({
            citation_keys: ['a2020'], dismiss: true,
        });
    });

    it('the bulk confirm names the real count and sends exactly those keys', async () => {
        await render();
        const body = document.getElementById('audit-report-body');
        body.querySelector('[data-action="nopdf-dismiss-type"]').click();
        await flush();
        // The count, not a description, is what the user is agreeing to.
        expect(document.body.textContent).toContain('all 2 webpage');

        confirmButton('Dismiss 2').click();
        await flush();
        const decision = posted.filter((p) => p.url.includes('pdf-decision'));
        expect(decision).toHaveLength(1);
        expect(decision[0].body.citation_keys.sort()).toEqual(['a2020', 'b2021']);
    });

    it('the bulk verb never touches rows of another type', async () => {
        await render();
        const body = document.getElementById('audit-report-body');
        body.querySelector('[data-action="nopdf-dismiss-type"]').click();
        await flush();
        confirmButton('Dismiss 2').click();
        await flush();
        const sent = posted.find((p) => p.url.includes('pdf-decision')).body;
        expect(sent.citation_keys).not.toContain('c2022');
    });

    it('the undo is a server-side clear_all, because the client has no keys', async () => {
        payload.dismissed_count = 5;
        await render();
        document.getElementById('audit-report-body')
            .querySelector('[data-action="nopdf-restore-all"]').click();
        await flush();
        confirmButton('Show them again').click();
        await flush();
        const decision = posted.filter((p) => p.url.includes('pdf-decision'));
        expect(decision).toHaveLength(1);
        expect(decision[0].body).toEqual({ clear_all: true });
        expect(decision[0].body.citation_keys).toBeUndefined();
    });

    it('reloads the report after a decision, since it applies immediately', async () => {
        await render();
        const before = fetch.mock.calls.filter(
            (c) => String(c[0]).includes('reports/zotero_no_pdf')).length;
        document.getElementById('audit-report-body')
            .querySelector('[data-action="nopdf-dismiss"]').click();
        await flush();
        confirmButton('No PDF needed').click();
        await flush();
        const after = fetch.mock.calls.filter(
            (c) => String(c[0]).includes('reports/zotero_no_pdf')).length;
        // A dismissal takes effect on the next LOAD, not the next scan, so the
        // report must be re-fetched rather than left showing the stale row.
        expect(after).toBeGreaterThan(before);
    });
});
