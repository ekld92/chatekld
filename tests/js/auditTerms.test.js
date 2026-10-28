// Library Audit terminology ratchet (2026-09-19).
//
// The user could not tell what the dashboard counted: no tile, heading or
// report tab carried a definition, and the note-sync header named only some of
// the states, so its parts did not add up to the total it announced
// (1 080 + 0 + 0 + 0 + 0 + 0 of 1 237). Both defects are silent — the page
// renders either way — so they are pinned here against the SOURCE of truth:
// the labels audit.js actually renders and the states the server can emit.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const AUDIT_JS = readFileSync(join(ROOT, 'static/js/audit.js'), 'utf8');
const INDEX = readFileSync(join(ROOT, 'templates/index.html'), 'utf8');
const NOTESYNC_ROUTE = readFileSync(join(ROOT, 'api/routes/notesync.py'), 'utf8');
const NOTESYNC_STATE = readFileSync(join(ROOT, 'notesync/state.py'), 'utf8');
const HELP_JS = readFileSync(join(ROOT, 'static/js/help.js'), 'utf8');

function termKeys() {
    const start = AUDIT_JS.indexOf('export const _AUDIT_TERMS = {');
    expect(start, '_AUDIT_TERMS not found — update this ratchet').toBeGreaterThan(-1);
    const block = AUDIT_JS.slice(start, AUDIT_JS.indexOf('\n};', start));
    return new Set([...block.matchAll(/^\s*'([^']+)':/gm)].map((m) => m[1]));
}

function block(name) {
    const fn = AUDIT_JS.indexOf(`function ${name}(`);
    expect(fn, `${name} not found — update this ratchet`).toBeGreaterThan(-1);
    const next = AUDIT_JS.indexOf('\nfunction ', fn + 1);
    return AUDIT_JS.slice(fn, next > -1 ? next : undefined);
}

describe('every dashboard label is defined', () => {
    it('each tile and section heading has an entry in _AUDIT_TERMS', () => {
        const keys = termKeys();
        const summary = block('_summaryHtml');
        const labels = [
            ...[...summary.matchAll(/_tile\('([^']+)'/g)].map((m) => m[1]),
            ...[...summary.matchAll(/group\('([^']+)'/g)].map((m) => m[1]),
        ];
        expect(labels.length).toBeGreaterThan(20);
        const undefinedLabels = labels.filter((l) => !keys.has(l));
        expect(undefinedLabels).toEqual([]);
    });

    it('definitions reach the page as accessible "?" badges, not only titles', () => {
        // title= is mouse-only; helpTip's badge is focusable and sets
        // aria-describedby. Both render sites must decorate.
        expect(AUDIT_JS.match(/_decorateDefs\((box|summaryBox)\)/g)?.length).toBe(2);
        expect(block('_decorateDefs')).toContain('helpTip(');
    });

    it('each count is shown once on the dashboard', () => {
        const summary = block('_summaryHtml');
        const labels = [...summary.matchAll(/_tile\('([^']+)'/g)].map((m) => m[1]);
        const dupes = labels.filter((l, i) => labels.indexOf(l) !== i);
        expect(dupes).toEqual([]);
    });

    // A definition can come from EITHER surface: a native `title`, or a help
    // badge, which is strictly better (keyboard-reachable, escapable, and it
    // does not fight the cursor). What must never happen is a tab with
    // neither — and a tab with BOTH is pinned against by helpTable.test.js,
    // since two explanations competing for one hover is worse than either.
    it('every report tab carries a one-line definition, by title or by badge', () => {
        const tabs = [...INDEX.matchAll(/<button class="audit-report-tab"[^>]*>/g)].map((m) => m[0]);
        expect(tabs.length).toBeGreaterThan(15);
        const bare = tabs
            .filter((t) => {
                if (/ title="[^"]{20,}"/.test(t)) return false;
                const id = (t.match(/ id="([^"]+)"/) || [])[1];
                return !(id && HELP_JS.includes(`['${id}',`));
            })
            .map((t) => (t.match(/data-report="([^"]+)"/) || [])[1]);
        expect(bare).toEqual([]);
    });
});

describe('the note-sync header adds up', () => {
    it('names every state the server can emit', () => {
        // notesync/state.py::STATES is the declared vocabulary; the route's own
        // assignments are unioned in case one ever escapes it.
        const declared = NOTESYNC_STATE.match(/^STATES = \(([^)]*)\)/m);
        expect(declared, 'STATES not found in notesync/state.py').toBeTruthy();
        const serverStates = new Set([
            ...[...declared[1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]),
            ...[...NOTESYNC_ROUTE.matchAll(/row\["state"\] = "([a-z_]+)"/g)].map((m) => m[1]),
        ]);
        expect(serverStates.size).toBeGreaterThanOrEqual(9);
        const header = block('_renderTwoWaySync');
        const partsBlock = header.slice(header.indexOf('const parts = ['), header.indexOf('].filter('));
        const named = new Set([...partsBlock.matchAll(/\['([a-z_]+)',/g)].map((m) => m[1]));
        const missing = [...serverStates].filter((s) => !named.has(s));
        expect(missing).toEqual([]);
    });

    it('one word per state: "in step" and "changed on both sides" are gone', () => {
        // Same state, several names was half the confusion.
        const code = (b) => b.split('\n').filter((l) => !/^\s*(\/\/|\*|\/\*)/.test(l)).join('\n');
        expect(code(block('_renderTwoWaySync'))).not.toMatch(/in step|in-step|changed on both sides|Note contents match/);
        expect(code(block('_renderNoteSync'))).not.toMatch(/in step|in-step/);
    });
});
