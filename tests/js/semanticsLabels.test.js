// Track 6c pinning — semantics & labels.
//
// Defects pinned: zero headings inside <main> (card titles were styled
// spans); unlabeled system-prompt textareas; the fallback checkbox group had
// no fieldset/legend; the materials toggle had no disclosure state; the audit
// tabpanel never renamed itself when the active report changed; audit table
// headers lacked scope="col"; French fragments rendered under lang="en";
// every refactor listbox row was a tabstop (Tab walked hundreds of notes).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const html = readFileSync(resolve(root, 'templates/index.html'), 'utf-8');

describe('markup semantics (Track 6c)', () => {
    it('card titles are h2 headings, not styled spans', () => {
        expect((html.match(/<h2 class="card-title">/g) || []).length).toBeGreaterThanOrEqual(7);
        // No card header may regress to a bare span title.
        expect(html).not.toMatch(/<div class="header">\s*<span>[^<]+<\/span>/);
    });

    it('sidebar section labels and the materials panel title are h3', () => {
        expect((html.match(/<h3 class="sidebar-label">/g) || []).length).toBeGreaterThanOrEqual(3);
        expect(html).toContain('<h3 class="panel-subtitle">Indexed Materials</h3>');
    });

    it('system-prompt textareas carry accessible names', () => {
        for (const id of ['vault-system-prompt', 'doc-system-prompt']) {
            const tag = new RegExp(`<textarea[^>]*id="${id}"[^>]*>`).exec(html);
            expect(tag, id).toBeTruthy();
            expect(tag[0]).toContain('aria-label=');
        }
    });

    it('fallback checkboxes are grouped under fieldset/legend', () => {
        const block = /<fieldset class="control-group checkbox-fieldset">[\s\S]*?<\/fieldset>/.exec(html);
        expect(block).toBeTruthy();
        expect(block[0]).toContain('<legend>Fall back on</legend>');
        expect((block[0].match(/type="checkbox"/g) || []).length).toBe(4);
    });

    it('materials toggle declares its disclosure state', () => {
        const tag = /<button[^>]*id="materials-toggle-btn"[^>]*>/.exec(html);
        expect(tag[0]).toContain('aria-expanded=');
        expect(tag[0]).toContain('aria-controls="materials-list-container"');
    });

    it('audit report tabs have ids and the panel names its initial tab', () => {
        expect((html.match(/id="audit-rtab-/g) || []).length).toBe(7);
        const panel = /<div id="audit-report-body"[^>]*>/.exec(html)[0];
        expect(panel).toContain('aria-labelledby="audit-rtab-inventory"');
    });

    it('decorative gear glyphs are hidden from AT', () => {
        // Every ⚙ ships inside an aria-hidden span; the adjacent text names
        // the target, so the symbol is pure decoration.
        expect(html).not.toMatch(/<strong>&#9881;/);
        expect(html).not.toMatch(/>&#9881; LLM Settings</);
        expect((html.match(/<span aria-hidden="true">&#9881;<\/span>/g) || []).length)
            .toBeGreaterThanOrEqual(10);
    });
});

describe('audit table + tabpanel behaviour (Track 6c)', () => {
    it('every audit-rendered <th> carries scope="col"', () => {
        const src = readFileSync(resolve(root, 'static/js/audit.js'), 'utf-8');
        // Source ratchet: no bare <th> may be reintroduced in the report builders.
        expect(src).not.toMatch(/<th(?![a-z])(?![^>]*scope=)/);   // (?![a-z]) skips <thead>
        expect((src.match(/<th scope="col">/g) || []).length).toBeGreaterThanOrEqual(30);
    });

    it('selectAuditReport renames the tabpanel after its active tab', async () => {
        document.body.innerHTML = `
          <button class="audit-report-tab" id="audit-rtab-inventory" data-report="inventory"></button>
          <button class="audit-report-tab" id="audit-rtab-duplicates" data-report="duplicates"></button>
          <div id="audit-report-body" role="tabpanel" aria-labelledby="audit-rtab-inventory"></div>
        `;
        vi.resetModules();
        // Never-resolving fetch: the labelledby sync happens synchronously
        // BEFORE the report load; the load itself is irrelevant here.
        vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})));
        const audit = await import('../../static/js/audit.js');
        audit.selectAuditReport('duplicates');
        expect(document.getElementById('audit-report-body').getAttribute('aria-labelledby'))
            .toBe('audit-rtab-duplicates');
        vi.unstubAllGlobals();
    });
});

describe('language + roving tabindex (Track 6c)', () => {
    afterEach(() => vi.unstubAllGlobals());

    it('example chips carry lang when the example set is French', async () => {
        document.body.innerHTML = '<textarea id="t"></textarea>';
        vi.resetModules();
        const ui = await import('../../static/js/ui.js');
        ui.renderExampleChips(document.getElementById('t'), [
            { label: 'Titres', text: 'Reformate cette note.' },
        ], { title: 'Examples:', lang: 'fr' });
        const chip = document.querySelector('.example-chip');
        expect(chip.lang).toBe('fr');
        // The English "Examples:" label must NOT be tagged French.
        expect(document.querySelector('.example-chips-label').lang).toBe('');
    });

    it('refactor listbox uses a roving tabindex (source ratchet)', () => {
        const src = readFileSync(resolve(root, 'static/js/refactor.js'), 'utf-8');
        expect(src).toContain('entry.tabIndex = -1;');
        expect(src).toContain('el.tabIndex = on ? 0 : -1;');
        expect(src).not.toContain('entry.tabIndex = 0;');
    });
});
