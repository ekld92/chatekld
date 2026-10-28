// The 2026-09-06 harmonization report tabs — Entries With Several PDFs /
// Not in Zotero / Dead Curation — and their Phase 2 verbs.
//
// Source ratchets over the wiring a later drive-by would quietly undo: the
// tab exists in the tablist, the label used by the search-group headings
// matches it, the renderer branch exists, every mapping write is
// confirm-gated, the bulk verbs skip rows a table filter is hiding (the
// zotlink select-all rule), and the accession deck's `P` key reaches the
// pending verb.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const AUDIT_JS = readFileSync(join(ROOT, 'static/js/audit.js'), 'utf8');
const INDEX_HTML = readFileSync(join(ROOT, 'templates/index.html'), 'utf8');

const REPORTS = {
    multi_pdf: 'Entries With Several PDFs',
    not_in_zotero: 'PDF workspace',
    pdf_review: 'PDF identification review',
    dead_curation: 'Dead Curation',
};

function tabFor(report) {
    // A redirected tab keeps its report name in its id and carries its
    // `library_documents:<filter>` target in data-report (2026-09-17, D1).
    const re = new RegExp(`<button[^>]*(?:data-report="${report}"|id="audit-rtab-${report}")[^>]*>(.*?)</button>`, 's');
    const m = INDEX_HTML.match(re);
    expect(m, `no tab found for ${report}`).toBeTruthy();
    return { html: m[0], label: m[1].trim() };
}

function fnBody(name) {
    const re = new RegExp(`(?:async )?function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`);
    const m = AUDIT_JS.match(re);
    expect(m, `no function ${name}`).toBeTruthy();
    return m[0];
}

describe('harmonization report tabs', () => {
    for (const [name, label] of Object.entries(REPORTS)) {
        it(`${name}: tab, label and renderer agree`, () => {
            expect(tabFor(name).label).toBe(label);
            const block = AUDIT_JS.match(/const _REPORT_LABELS = \{[\s\S]*?\n\};/)[0];
            expect(block).toContain(`${name}: '${label}'`);
            expect(AUDIT_JS).toContain(`if (name === '${name}') {`);
        });
    }

    it('places Entries With Several PDFs beside Possible Duplicates', () => {
        const iNear = INDEX_HTML.indexOf('data-report="near_duplicates"');
        const iMulti = INDEX_HTML.indexOf('data-report="multi_pdf"');
        expect(iNear).toBeGreaterThan(-1);
        expect(iMulti).toBeGreaterThan(iNear);
    });

    it('renders the disclosure counts the server sends', () => {
        for (const f of ['mixed_count', 'all_filename_count', 'dismissed_count',
                         'with_doi_count', 'resolved_count', 'missing_count',
                         'pending_count', 'no_match_count',
                         'recovered_count', 'curated_no_match']) {
            expect(AUDIT_JS).toContain(`d.${f}`);
        }
    });

    it('the DOI copy skips rows hidden by the table filter', () => {
        const fn = fnBody('_nizCopyDois');
        expect(fn).toMatch(/tr\.hidden \|\| tr\.style\.display === 'none'/);
        expect(fn).toContain('navigator.clipboard.writeText');
        // Clipboard refusal falls back to a selectable textarea (accession rule).
        expect(fn).toContain('audit-niz-copy-export');
    });
});

describe('Phase 2 verbs: pending, detach, bulk', () => {
    it('every Not-in-Zotero write is confirm-gated and posts to the mapping route', () => {
        for (const name of ['_nizSetState', '_nizClear', '_detachConfirm']) {
            expect(fnBody(name)).toContain('confirmInline(btn, {');
        }
        expect(fnBody('_nizWrite')).toContain("secureFetch('/api/audit/mapping'");
        expect(fnBody('_detach')).toContain('detach_from: key');
        expect(fnBody('_detach')).toContain('trash_zotero_links');
        expect(fnBody('_detachConfirm')).toContain('checkbox:');
        expect(fnBody('_invDetailHtml')).toContain('data-source=');
        expect(fnBody('_detachConfirm')).toContain('zotero_link');
        expect(fnBody('_detach')).toContain('d.warning');
    });

    it('a bulk write uses the list form and never re-caps to one PDF', () => {
        const fn = fnBody('_nizSetState');
        expect(fn).toMatch(/\{ pdfs: rels, \[state\]: true, confirm: true \}/);
        expect(fnBody('_nizClear')).toMatch(/\{ pdfs: rels, clear: true, confirm: true \}/);
    });

    it('the selection skips rows the table filter is hiding', () => {
        for (const name of ['_nizSelectedRels', '_nizSelectAll']) {
            expect(fnBody(name)).toMatch(/tr\.hidden \|\| tr\.style\.display === 'none'/);
        }
    });

    it('the ambiguous-PDF table gates pending like no-match', () => {
        expect(AUDIT_JS).toMatch(/if \(action === 'no-match' \|\| action === 'pending'\) \{/);
        expect(AUDIT_JS).toMatch(/confirmInline\(btn,[\s\S]{0,700}?_markState\(btn, state\)/);
    });

    it('the accession deck offers pending on the P key and the ambiguous deck as a verb', () => {
        expect(AUDIT_JS).toMatch(/case 'p': case 'P': \{\s*const b = btn\('pending'\);/);
        expect(AUDIT_JS).toContain('data-acc-deck="pending"');
        expect(AUDIT_JS).toContain('data-ambig="pending"');
        expect(AUDIT_JS).toMatch(/case 'pending': _ambigConfirmWrite\(btn, 'pending'\); break;/);
    });

    it('the mapping body helper knows exactly the three shapes', () => {
        const fn = fnBody('_mappingStateBody');
        expect(fn).toContain('no_match: true');
        expect(fn).toContain('pending: true');
        expect(fn).toContain("source: 'accession'");
    });

    it('Dead Curation offers [clear decision] for a Zotero-overridden row', () => {
        expect(AUDIT_JS).toContain('data-action="dc-clear"');
        expect(fnBody('_deadCurationClear')).toContain('confirmInline(btn, {');
        expect(fnBody('_deadCurationClear')).toContain("secureFetch('/api/audit/mapping'");
        expect(fnBody('_deadCurationClear')).toContain('clear: true');
        expect(AUDIT_JS).toContain("if (action === 'dc-clear') { _deadCurationClear(btn); return; }");
    });

    it('PDFs Without Zotero discloses the pending bucket with a jump', () => {
        expect(AUDIT_JS).toMatch(/if \(d\.pending_count\) \{[\s\S]{0,200}?_gotoReportBtn\('not_in_zotero'/);
    });

    it('both PDF entry points mount the shared workspace', () => {
        expect(tabFor('pdf_review').label).toBe('PDF identification review');
        // Mount only when the dedicated tab loads with review_scope.
        expect(AUDIT_JS).toContain("(name === 'pdf_review' || name === 'not_in_zotero') && d.review_scope");

        expect(AUDIT_JS).toContain("zl-remove-h");
        expect(AUDIT_JS).toContain("Link vault PDFs into Zotero");
        expect(AUDIT_JS).toMatch(/details class="notesync-attempts"/);
        expect(AUDIT_JS).toMatch(/_gotoReportBtn\('pdf_review', '\[PDF identification review\]'/);
    });
});

describe('identify from Not in Zotero (2026-09-06)', () => {
    it('offers identify on pending rows only, confirm-gates the batch, and repaints in place', () => {
        expect(AUDIT_JS).toMatch(/data-action="niz-identify"[^>]*\$\{st === 'pending' \? '' : ' hidden'\}/);
        expect(fnBody('_nizIdentifyMany')).toContain('confirmInline(btn, {');
        expect(fnBody('_nizRunBatch')).toContain("consumeSSE('/api/accession/batch'");
        // Bulk RE-identify sends force; plain identify does not.
        expect(fnBody('_nizRunBatch')).toMatch(/\.\.\.\(force \? \{ force: true \} : \{\}\)/);
        expect(AUDIT_JS).toContain('data-verb="reidentify"');
        expect(fnBody('_nizIdentifyOne')).toContain("consumeSSE('/api/accession/identify'");
        // Repaint uses textContent / a created <code>, never innerHTML with DOI text.
        const apply = fnBody('_nizApplyRow');
        expect(apply).toContain("document.createElement('code')");
        expect(apply).not.toMatch(/dc\.innerHTML/);
    });
});

describe('Phase 4: link vault PDFs into Zotero', () => {
    it('lives on the Zotero Attachments tab, confirm-gates the write, and skips filter-hidden rows', () => {
        expect(AUDIT_JS).toContain('data-action="zl-load"');
        expect(fnBody('_zlLoad')).toContain("secureFetch('/api/zotlink/attach-candidates')");
        expect(fnBody('_zlConfirm')).toContain('confirmInline(btn, {');
        // The primary button is a `.btn`, invisible to the `.audit-link-btn`
        // delegate — it must be bound directly or it never fires (the
        // 2026-09-06 dead-button report).
        expect(fnBody('_zlRenderTable')).toMatch(/\[data-action="zl-run"\]'\)\.forEach\(b => \{[\s\S]*?addEventListener\('click'/);
        expect(AUDIT_JS).not.toMatch(/if \(action === 'zl-run'\)/);
        expect(fnBody('_zlRun')).toContain("consumeSSE('/api/zotlink/attach'");
        expect(fnBody('_zlSelected')).toMatch(/tr\.hidden \|\| tr\.style\.display === 'none'/);
        // A dry run needs no confirm (it writes nothing) but the real run does.
        expect(fnBody('_zlConfirm')).toMatch(/if \(dry\) \{ _zlRun\(keys, true\); return; \}/);
        // Group-library rows can never be selected.
        expect(AUDIT_JS).toMatch(/data-zl-sel="1"\$\{group \? ' disabled' : ''\}/);
    });
});
