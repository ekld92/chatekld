// Pins the Tag Hygiene "unmapped PDFs" bucket (audit.js).
//
// The server has computed `unmapped_rows` since 2026-08-11 — Finder tags on
// biblio PDFs the bridge never joined to a citation key — and `serialize.py`
// has shipped it on the wire, but the renderer read only `d.rows`, so the
// bucket was computed on every GET (a ~1250-file xattr sweep) and thrown away
// by the client. The headline "outside the vocabulary" figure stayed a floor
// presented as a total, which is the exact defect the bucket was added to fix.
//
// Guarded classes:
//   1. RENDERED AT ALL — the second table exists and its counts appear.
//   2. SEPARATE — unmapped rows never leak into the main table, whose totals
//      are per paper while these are per PDF.
//   3. RETIRABLE — the rows carry [retire], which is only honest because
//      retire-tag now enumerates unmapped PDFs too (test_tagging.py pins the
//      server half).
//   4. ESCAPED — row text is user data from a filesystem xattr.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { selectAuditReport } from '../../static/js/audit.js';

function jsonOk(payload) {
    return { ok: true, status: 200, json: async () => payload };
}

function hygieneRow(variants, total, inVocab, extra = {}) {
    return {
        fingerprint: variants[0].toLowerCase(),
        variants,
        counts: { finder: total },
        total,
        in_vocab: inVocab,
        vocab_tag: inVocab ? variants[0] : null,
        near_vocab: null,
        ...extra,
    };
}

const REPORT = {
    name: 'tag_hygiene',
    vocab_size: 176,
    sources: ['finder', 'note', 'zotero', 'zotero_notes', 'bib'],
    rows: [hygieneRow(['mapped_only'], 12, true)],
    cooccurrence: [],
    unmapped_rows: [
        hygieneRow(['sr_ma'], 298, true),
        hygieneRow(['<img src=x onerror=alert(1)>'], 4, false),
    ],
    unmapped_pdf_count: 1253,
    unmapped_tagged_count: 1253,
};

const DOM = `
  <div id="audit-summary" style="display:none;"></div>
  <div id="audit-report-tabs" style="display:none;"></div>
  <div id="audit-search-bar" style="display:none;">
    <input id="audit-search-input"><button id="audit-search-clear"></button>
  </div>
  <div id="audit-report-meta"></div>
  <div id="audit-search-results" style="display:none;"></div>
  <div id="audit-report-body"></div>
  <div id="audit-empty-state"></div>
  <div id="audit-status-banner" style="display:none;"></div>
  <div id="sr-status-announcer" role="status"></div>
`;

function tables() {
    return [...document.querySelectorAll('#audit-report-body table.audit-table')];
}

describe('tag hygiene — unmapped-PDF bucket', () => {
    beforeEach(async () => {
        document.body.innerHTML = DOM;
        const store = new Map();
        vi.stubGlobal('localStorage', {
            getItem: (k) => (store.has(k) ? store.get(k) : null),
            setItem: (k, v) => store.set(k, String(v)),
            removeItem: (k) => store.delete(k),
        });
        vi.stubGlobal('fetch', vi.fn(async () => jsonOk(REPORT)));
        await selectAuditReport('tag_hygiene');
    });

    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it('renders the bucket as its own table', () => {
        expect(tables().length).toBe(2);
        const body = document.getElementById('audit-report-body');
        expect(body.textContent).toContain('Finder tags on PDFs with no citation key');
        expect(body.textContent).toContain('sr_ma');
    });

    it('discloses how many PDFs it swept and how many carry tags', () => {
        const text = document.getElementById('audit-report-body').textContent;
        expect(text).toContain('1253');
        // The whole point: the out-of-vocab count for this bucket is stated
        // separately rather than folded into the headline number above.
        expect(text).toContain('1 outside the vocabulary');
    });

    it('keeps the buckets separate — main totals stay per paper', () => {
        const main = tables()[0];
        expect(main.textContent).toContain('mapped_only');
        expect(main.textContent).not.toContain('sr_ma');
        const meta = document.getElementById('audit-report-meta').textContent;
        // The meta line counts d.rows only (1 distinct tag), not 1 + 2.
        expect(meta).toContain('1 distinct tags');
    });

    it('offers [retire] on unmapped rows', () => {
        const unmapped = tables()[1];
        const btns = [...unmapped.querySelectorAll('button[data-action="retire-tag"]')];
        expect(btns.map((b) => b.dataset.tag)).toContain('sr_ma');
    });

    it('escapes tag text — it comes from a filesystem xattr', () => {
        const unmapped = tables()[1];
        expect(unmapped.querySelector('img')).toBeNull();
        expect(unmapped.textContent).toContain('<img src=x onerror=alert(1)>');
    });

    it('renders nothing extra when no unmapped PDFs were swept', async () => {
        vi.stubGlobal('fetch', vi.fn(async () => jsonOk({
            ...REPORT, unmapped_rows: [], unmapped_pdf_count: 0,
            unmapped_tagged_count: 0,
        })));
        await selectAuditReport('tag_hygiene');
        expect(tables().length).toBe(1);
        expect(document.getElementById('audit-report-body').textContent)
            .not.toContain('no citation key');
    });
});
