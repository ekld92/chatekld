// JS module-hierarchy import lint (improvement plan 2026-07-04, Track 7.3).
//
// The hierarchy exists to make circular imports impossible (CLAUDE.md §JS
// Module Hierarchy): api.js imports nothing from the project; ui.js imports
// only api.js; every feature module imports only ui.js + api.js; app.js is
// the root and may import anything. The recurring audit defect shape is
// doc/code drift — and this rule HAS already drifted: vault.js and
// summarizer.js import from config.js (plan item 4.10 decides whether to fix
// or re-document that). So this is a RATCHET, not a snapshot:
//
//   * a NEW out-of-hierarchy import fails the suite immediately;
//   * the two known violations are allowlisted — and asserted to still
//     exist, so the 4.10 fix is forced to shrink the allowlist rather than
//     leave it as dead grandfathering.
//
// Static analysis over the real files (no jsdom needed): every module uses
// plain `import { … } from './x.js'` — no dynamic import() and no bare
// side-effect imports exist in static/js (verified when this lint landed;
// the regex below would miss them, so keep it that way or extend it).
import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const JS_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'static', 'js');

/** Project-relative import specifiers of one module (vendor/ = external, ignored). */
function importsOf(file) {
    const src = readFileSync(join(JS_DIR, file), 'utf8');
    return [...src.matchAll(/from\s+['"]\.\/([A-Za-z0-9_.-]+\.js)['"]/g)].map((m) => m[1]);
}

const MODULES = readdirSync(JS_DIR).filter(
    (f) => f.endsWith('.js') && statSync(join(JS_DIR, f)).isFile(),
);

// The documented hierarchy: module -> set of allowed project imports.
// app.js (the root) is deliberately absent — it may import anything.
//
// Item 4.10: vault.js and summarizer.js are allowed to import config.js as
// documented exceptions. They only read active settings (e.g. active provider)
// and do not create circular dependencies.
const ALLOWED = {
    'api.js': [],
    'ui.js': ['api.js'],
    'vault.js': ['api.js', 'ui.js', 'config.js'],
    'summarizer.js': ['api.js', 'ui.js', 'config.js'],
};
const FEATURE_ALLOWED = ['api.js', 'ui.js'];

describe('JS module hierarchy (ratchet lint, Track 7.3)', () => {
    it('found the expected module set', () => {
        // A rename/addition should be a conscious event for this lint, not a
        // silent scope change — update this list alongside CLAUDE.md.
        expect(MODULES.sort()).toEqual([
            'api.js', 'app.js', 'audit.js', 'config.js', 'deck.js',
            'plainchat.js', 'prompts.js', 'refactor.js', 'settings.js',
            'summarizer.js', 'ui.js', 'vault.js',
        ]);
    });

    it('no module imports outside the hierarchy (including allowed exceptions)', () => {
        const violations = [];
        for (const mod of MODULES) {
            if (mod === 'app.js') continue; // root: unrestricted
            const allowed = ALLOWED[mod] ?? FEATURE_ALLOWED;
            for (const imp of importsOf(mod)) {
                if (!allowed.includes(imp)) violations.push(`${mod}->${imp}`);
            }
        }
        expect(violations).toEqual([]);
    });

    it('the two known 4.10 drift edges still exist (shrink the allowlist when fixed)', () => {
        // The allowlist grandfathers vault.js->config.js and
        // summarizer.js->config.js. Assert they are STILL present so resolving
        // plan item 4.10 (dropping those config.js imports) is FORCED to also
        // remove them from ALLOWED, rather than leaving dead grandfathering
        // behind. Both this file's header and CLAUDE.md §JS Module Hierarchy
        // promise this ratchet; it was previously only promised, not asserted.
        expect(importsOf('vault.js')).toContain('config.js');
        expect(importsOf('summarizer.js')).toContain('config.js');
    });

    it('no dynamic import() or bare side-effect imports sneak past the regex', () => {
        for (const mod of MODULES) {
            const src = readFileSync(join(JS_DIR, mod), 'utf8');
            expect(src.includes('import('), `${mod} uses dynamic import()`).toBe(false);
            expect(/^import\s+['"]/m.test(src), `${mod} uses a bare side-effect import`).toBe(false);
        }
    });
});
