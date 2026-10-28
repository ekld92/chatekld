// 2026-09-02 — one closure per row verb in the audit report body.
//
// DEFECT PINNED: _wireRowActions bound a click listener to every
// .audit-link-btn and every inventory [data-inv-view] / [data-inv-detail] on
// each render — 12 000 to 20 000 closures for the 6 000-row inventory, and
// re-created on every re-render (an accession write repaints the whole body).
//
// The invariants: the verbs still work through delegation, and the DECK views
// still own their own card wiring — their [data-action="reveal"] buttons carry
// .audit-link-btn too, so a delegate that ignored that would fire twice.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const DOM = `
  <div class="content-area active" id="audit-tab">
    <button id="audit-run-btn"></button>
    <button id="audit-cancel-btn" style="display:none;"></button>
    <div id="audit-summary" style="display:none;"></div>
    <div id="audit-report-tabs" style="display:none;"></div>
    <div id="audit-search-bar" style="display:none;">
      <input id="audit-search-input"><button id="audit-search-clear"></button>
    </div>
    <div id="audit-report-meta"></div>
    <div id="audit-search-results" style="display:none;"></div>
    <div id="audit-report-body"></div>
    <div id="audit-empty-state"></div>
    <div id="audit-init-error"></div>
    <div id="audit-status-banner" style="display:none;"></div>
  </div>
  <div id="sr-status-announcer" role="status"></div>
`;

const SUMMARY = { record_count: 2, tag_coverage: null };
const jsonOk = (p) => ({ ok: true, status: 200, json: async () => p });

function routes(reportPayload) {
    return vi.fn(async (url) => {
        const u = String(url);
        if (u.includes('/api/audit/summary')) return jsonOk({ summary: SUMMARY });
        if (u.includes('/api/audit/config')) return jsonOk({ config: {} });
        if (u.includes('/api/audit/reveal')) return jsonOk({ ok: true });
        if (u.includes('/api/audit/status')) {
            return jsonOk({
                state: 'done', error: null, has_results: true,
                has_duplicates: false, has_near_duplicates: false,
                started_at: 1, finished_at: 2, messages: [],
            });
        }
        return jsonOk(reportPayload);
    });
}

const revealCalls = (m) =>
    m.mock.calls.filter((c) => String(c[0]).includes('/api/audit/reveal')).length;

beforeEach(() => {
    document.body.innerHTML = DOM;
    vi.stubGlobal('localStorage', {
        getItem: () => null, setItem: () => {}, removeItem: () => {},
    });
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

async function load() {
    vi.resetModules();
    return import('../../static/js/audit.js');
}

const flush = () => new Promise((r) => setTimeout(r, 0));

describe('audit report row verbs are delegated', () => {
    it('a [reveal] in a table row still fires exactly one request', async () => {
        const fetchMock = routes({
            name: 'duplicates', total_wasted_bytes: 10,
            rows: [{ content_hash: 'abcdef0123456789', size_bytes: 10,
                     wasted_bytes: 10, suggested_keep: 'a.pdf',
                     paths: [{ rel: 'a.pdf', abs: '/v/a.pdf' },
                             { rel: 'b.pdf', abs: '/v/b.pdf' }] }],
        });
        vi.stubGlobal('fetch', fetchMock);
        const { selectAuditReport } = await load();
        await selectAuditReport('duplicates');

        const btn = document.querySelector(
            '#audit-report-body .audit-link-btn[data-action="reveal"]');
        expect(btn).toBeTruthy();
        btn.click();
        await flush();
        expect(revealCalls(fetchMock)).toBe(1);
    });

    it('binds one body listener, not one per button', async () => {
        const rows = Array.from({ length: 25 }, (_, i) => ({
            content_hash: `hash${i}0123456789`, size_bytes: 10, wasted_bytes: 10,
            suggested_keep: `a${i}.pdf`,
            paths: [{ rel: `a${i}.pdf`, abs: `/v/a${i}.pdf` },
                    { rel: `b${i}.pdf`, abs: `/v/b${i}.pdf` }],
        }));
        vi.stubGlobal('fetch', routes({
            name: 'duplicates', total_wasted_bytes: 250, rows }));
        const { selectAuditReport } = await load();

        const body = document.getElementById('audit-report-body');
        const added = [];
        const realAdd = body.addEventListener.bind(body);
        body.addEventListener = (type, fn, opts) => {
            added.push(type); return realAdd(type, fn, opts);
        };
        const btnAdds = [];
        const proto = window.HTMLButtonElement.prototype;
        const realProtoAdd = proto.addEventListener;
        proto.addEventListener = function (type, fn, opts) {
            if (this.classList && this.classList.contains('audit-link-btn')) {
                btnAdds.push(type);
            }
            return realProtoAdd.call(this, type, fn, opts);
        };
        try {
            await selectAuditReport('duplicates');
        } finally {
            proto.addEventListener = realProtoAdd;
        }

        expect(document.querySelectorAll(
            '#audit-report-body .audit-link-btn').length).toBeGreaterThan(20);
        expect(btnAdds.length).toBe(0);
        expect(added.filter((t) => t === 'click').length).toBe(1);
    });

    it('the ambiguous DECK keeps its own wiring — reveal fires once', async () => {
        const fetchMock = routes({
            name: 'ambiguous_pdfs', count: 1,
            rows: [{ pdf: { rel: 'u.pdf', abs: '/v/u.pdf', name: 'u.pdf' },
                     candidates: [{ citation_key: 'a2020', title: 'T' }] }],
        });
        vi.stubGlobal('fetch', fetchMock);
        vi.stubGlobal('localStorage', {
            getItem: (k) => (k === 'audit_ambig_view' ? 'deck' : null),
            setItem: () => {}, removeItem: () => {},
        });
        const { selectAuditReport } = await load();
        await selectAuditReport('ambiguous_pdfs');
        await flush();

        const btn = document.querySelector(
            '#audit-report-body [data-action="reveal"]');
        expect(btn).toBeTruthy();
        btn.click();
        await flush();
        expect(revealCalls(fetchMock)).toBe(1);   // not 2
    });
});
