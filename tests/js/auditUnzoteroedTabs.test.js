// Pins the two un-Zotero'd-PDF report tabs (audit.js + index.html).
//
// `read_unzoterod` is the COMPLETE set of PDFs the bridge could not resolve to
// a bib entry — read and unread alike — and `unread_unzoterod` is a filtered
// SUBSET of it (annotations < threshold). They were once labelled "Read PDFs
// Missing Zotero" / "Unread PDFs", which reads as a partition; the full list
// was on screen the whole time and nobody could find it. These are source
// ratchets over the fixes, because every one of them is the kind of thing a
// later drive-by rename would quietly undo.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const AUDIT_JS = readFileSync(join(ROOT, 'static/js/audit.js'), 'utf8');
const INDEX_HTML = readFileSync(join(ROOT, 'templates/index.html'), 'utf8');

/** The tablist button for one report id. */
function tabFor(report) {
    const re = new RegExp(`<button[^>]*data-report="${report}"[^>]*>(.*?)</button>`, 's');
    const m = INDEX_HTML.match(re);
    expect(m, `no tab found for ${report}`).toBeTruthy();
    return { html: m[0], label: m[1] };
}

describe('un-Zotero\'d PDF tabs: naming and ordering', () => {
    it('names the superset tab for what it holds, not for "read"', () => {
        const { label } = tabFor('read_unzoterod');
        expect(label).toContain('PDFs Without Zotero');
        // The old name claimed a filter the report has never applied.
        expect(label).not.toMatch(/Read PDFs Missing Zotero/);
    });

    it('marks the subset tab as a subset rather than a sibling category', () => {
        const { label } = tabFor('unread_unzoterod');
        expect(label).toContain('Unread Only');
        // "Unread PDFs" reads as the complement of "Read PDFs".
        expect(label).not.toMatch(/>\s*Unread PDFs\s*</);
    });

    it('puts the superset immediately before its subset in the tablist', () => {
        const iRead = INDEX_HTML.indexOf('data-report="read_unzoterod"');
        const iUnread = INDEX_HTML.indexOf('data-report="unread_unzoterod"');
        expect(iRead).toBeGreaterThan(-1);
        expect(iUnread).toBeGreaterThan(-1);
        expect(iRead).toBeLessThan(iUnread);
    });

    it('hides the containment glyph from assistive tech', () => {
        // A bare "↳" is announced as a meaningless arrow name, so it stays
        // aria-hidden; the containment it conveys visually is restored to AT
        // by the aria-label below.
        const { html } = tabFor('unread_unzoterod');
        expect(html).toMatch(/<span aria-hidden="true">↳<\/span>/);
    });

    it('gives the subset tab an accessible name that carries its parent', () => {
        // DOM order implies the containment when arrowing through the
        // tablist, but an out-of-order listing (SR rotor/tab summary) hears
        // only the bare label — the aria-label reuses the exact string the
        // search-panel group headings already use (2026-08-04 audit).
        const { html } = tabFor('unread_unzoterod');
        expect(html).toContain('aria-label="PDFs Without Zotero · Unread Only"');
    });
});

describe('un-Zotero\'d PDF tabs: search-panel labels', () => {
    it('does not leak the tablist arrow into the search group headings', () => {
        // _REPORT_LABELS also titles the cross-report search groups, where there
        // is no adjacent tab to give "↳" any meaning.
        const block = AUDIT_JS.match(/const _REPORT_LABELS = \{[\s\S]*?\n\};/);
        expect(block).toBeTruthy();
        // Assert over the label VALUES, not the raw block — the surrounding
        // comment legitimately mentions the glyph it is explaining away.
        const values = [...block[0].matchAll(/^\s*\w+:\s*'([^']*)'/gm)].map(m => m[1]);
        expect(values.length).toBeGreaterThan(5);
        expect(values.some(v => v.includes('↳'))).toBe(false);
        expect(values).toContain('PDFs Without Zotero · Unread Only');
        expect(values).toContain('PDFs Without Zotero');
    });
});

describe('un-Zotero\'d PDF tabs: exclusion disclosure', () => {
    it('renders the three exclusion counts the server now sends', () => {
        // The server-side counts exist so the tab can say what it omits;
        // reading none of them would put us back where we started.
        for (const field of ['ambiguous_count', 'confirmed_no_match_count', 'skipped_count']) {
            expect(AUDIT_JS).toContain(`d.${field}`);
        }
    });

    it('suppresses zero counts instead of printing "0 excluded"', () => {
        // Guarded by truthiness, not rendered unconditionally.
        expect(AUDIT_JS).toMatch(/if \(d\.ambiguous_count\) \{/);
        expect(AUDIT_JS).toMatch(/if \(d\.confirmed_no_match_count\) \{/);
        expect(AUDIT_JS).toMatch(/if \(d\.skipped_count\) \{/);
        expect(AUDIT_JS).toContain('Nothing else under biblio_articles/ is excluded.');
    });

    it('cross-links to the reports that own the excluded rows', () => {
        expect(AUDIT_JS).toMatch(/_gotoReportBtn\('ambiguous_pdfs'/);
        expect(AUDIT_JS).toMatch(/_gotoReportBtn\('skipped_pdfs'/);
        // ...and the subset tab links UP to the full list.
        expect(AUDIT_JS).toMatch(/_gotoReportBtn\('read_unzoterod'/);
    });

    it('cross-links are real buttons wired by delegation, never inline handlers', () => {
        // Meta HTML is injected as a string, so an onclick= would be both a CSP
        // smell and unreachable through the module's scope.
        expect(AUDIT_JS).toMatch(/data-goto-report="\$\{_attr\(report\)\}"/);
        expect(AUDIT_JS).toMatch(/meta\.querySelectorAll\('\[data-goto-report\]'\)/);
        const helper = AUDIT_JS.match(/function _gotoReportBtn[\s\S]*?\n\}/)[0];
        expect(helper).not.toContain('onclick');
        expect(helper).toContain('type="button"');
    });
});
