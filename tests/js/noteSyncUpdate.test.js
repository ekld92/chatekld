// Source ratchet over what is left of audit.js's Zotero-note surfaces
// (2026-08-28; re-aimed 2026-09-07).
//
// This file pinned `_noteSyncUpdate` / `_noteSyncDiff` — the one-directional
// "Update available" / "Needs review" buckets. Those were retired when the tab
// became one table with three buttons: the same note used to appear as [pull]
// in the sync table and again as [update] or [overwrite region] below it,
// three names for two writes, with the destructive one reachable from a list
// that looked like the safe one. Both handlers are gone from audit.js.
//
// The LESSON survives, aimed at the live surface. The old gate was
// `accept_overwrite`, sent only for the conflict bucket and derived from a
// FRESH preview rather than from the rendered row (a row can gain the user's
// words between render and click). Its successor is `accept_conflict`, and
// its equivalent ratchets live in noteSyncTwoWay.test.js. What stays here is
// what is still reachable: the preview panel's guards, and the `_tbodyHtml`
// call-site sweep that the retired tables were the original victims of.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const AUDIT_JS = readFileSync(join(ROOT, 'static/js/audit.js'), 'utf8');

describe('the retired one-directional verbs stay retired', () => {
    // Comments are stripped first: this file's own header names the retired
    // label to explain why it went, and a ratchet that its own rationale
    // trips is a ratchet nobody keeps.
    const CODE = AUDIT_JS
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .split('\n').filter((l) => !l.trim().startsWith('//')).join('\n');

    it('no handler and no button offers them', () => {
        for (const gone of ['_noteSyncUpdate', '_noteSyncDiff',
                            'notesync-update', 'notesync-diff',
                            '[overwrite region]']) {
            expect(CODE, `${gone} is back — the tab had two vocabularies for one write`)
                .not.toContain(gone);
        }
    });
});

describe('the expandable panels are guarded and announced', () => {
    // Neither handler disabled its button or tracked a pending request, so two
    // clicks during a slow preview inserted TWO panels — and the toggle tests
    // `nextElementSibling`, so neither could then be closed. There was also no
    // spinner, no aria-busy and no announcement: a screen-reader user pressing
    // the button got nothing, and focus stayed on it while content appeared
    // below (2026-08-30).
    function block(name) {
        const fn = AUDIT_JS.indexOf(`function ${name}(`);
        expect(fn, `${name} not found — update this ratchet`).toBeGreaterThan(-1);
        const rest = AUDIT_JS.slice(fn + 1);
        const ends = [rest.indexOf('\nfunction '), rest.indexOf('\nasync function ')]
            .filter((i) => i > -1);
        const next = ends.length ? Math.min(...ends) : -1;
        return next > -1 ? rest.slice(0, next) : rest;
    }

    for (const name of ['_noteSyncMerge', '_noteSyncPreview']) {
        it(`${name} refuses a concurrent request`, () => {
            const b = block(name);
            expect(b).toMatch(/dataset\.busy === '1'/);
            expect(b).toContain("btn.dataset.busy = '1'");
        });

        it(`${name} always clears its busy state`, () => {
            const b = block(name);
            // In a `finally`, or an error leaves the row permanently dead.
            expect(b).toMatch(/finally \{[\s\S]*delete btn\.dataset\.busy/);
            expect(b).toMatch(/finally \{[\s\S]*btn\.disabled = false/);
        });

        it(`${name} announces both opening and closing`, () => {
            const b = block(name);
            // `_nsSay` is the announcer path since 2026-09-19: it paints
            // `#audit-ns-status` and calls `announceStatus`. Counting the raw
            // announcer alone made this ratchet red for a change that IMPROVED
            // what the user sees, which is the wrong direction for a ratchet.
            const calls = b.split('\n').filter(
                (l) => l.includes('announceStatus(') || l.includes('_nsSay('));
            expect(calls.length,
                   'open and close must both reach the status line / announcer')
                .toBeGreaterThanOrEqual(2);
            expect(b).toMatch(/aria-expanded/);
        });
    }

    it('the merge view hands region text to the shared renderer, never markup', () => {
        // Inherited from the retired `_noteSyncDiff`: vault and Zotero text
        // reaches the DOM through ui.js's renderer, which is textContent-only
        // (pinned separately in sideBySideDiff.test.js — the split is
        // deliberate, so neither half can be dropped by editing the other).
        const b = block('_noteSyncMerge');
        expect(b).toContain('renderSideBySideDiff(');
        expect(b).not.toContain('innerHTML');
    });
});

// Every `_tbodyHtml` caller must hand it a STRING, not an array (2026-09-04).
//
// `_tbodyHtml` interpolates its argument into a template literal. An array is
// coerced by `Array.prototype.toString`, which joins on "," — and because the
// result lands between `<tbody>` and `<tr>`, the HTML parser FOSTER-PARENTS
// those text nodes out of the table and drops them immediately before it. The
// user saw a run of commas floating above the "Needs review" table, one per row
// gap: 15 conflicts, 14 commas. Both notesync update tables had it while the
// four sibling tables in the same function joined correctly.
//
// It is invisible at 0 or 1 row, so it cannot be caught by eyeballing a small
// fixture — hence a ratchet over every call site in the file, not just these
// two. `_tbodyHtml` also joins defensively now; this pins the call sites so the
// intent stays visible at the point of use.
describe('_tbodyHtml is never handed a bare array', () => {
    it('every call site passes a joined string', () => {
        const offenders = [];
        const re = /_tbodyHtml\(/g;
        let m;
        while ((m = re.exec(AUDIT_JS)) !== null) {
            // Walk to the matching close paren so nested calls are included.
            let depth = 1;
            let i = m.index + m[0].length;
            for (; i < AUDIT_JS.length && depth > 0; i += 1) {
                if (AUDIT_JS[i] === '(') depth += 1;
                else if (AUDIT_JS[i] === ')') depth -= 1;
            }
            const arg = AUDIT_JS.slice(m.index + m[0].length, i - 1);
            // A `.map(` that is not followed by `.join(` yields an array.
            if (arg.includes('.map(') && !arg.includes('.join(')) {
                const line = AUDIT_JS.slice(0, m.index).split('\n').length;
                offenders.push(`audit.js:${line} — ${arg.slice(0, 60)}`);
            }
        }
        expect(offenders,
               'an unjoined array prints commas above its own table').toEqual([]);
    });

    it('the helper itself joins an array rather than coercing it', () => {
        const fn = AUDIT_JS.indexOf('function _tbodyHtml(');
        expect(fn).toBeGreaterThan(-1);
        const body = AUDIT_JS.slice(fn, AUDIT_JS.indexOf('\nfunction ', fn + 1));
        expect(body).toMatch(/Array\.isArray\(rowsHtml\)/);
    });
});

// The hazard the ratchet above exists for, demonstrated rather than asserted:
// stray text between rows does not stay inside the table.
//
// WHERE it lands is engine-specific and deliberately not asserted: the HTML
// spec's foster-parenting inserts the text immediately BEFORE the table (which
// is what put the commas above the filter strip in the real window, since
// `installTableControls` mounts there too), while jsdom emits it after. The
// engine-independent half — that it escapes the table entirely, so no amount of
// table styling can hide it — is what makes the defect user-visible, and that
// is what is pinned.
describe('foster-parenting: why an unjoined array is visible at all', () => {
    it('moves a comma out of the table into the surrounding flow', () => {
        const host = document.createElement('div');
        host.innerHTML = '<table class="t"><tbody>'
            + '<tr><td>a</td></tr>,<tr><td>b</td></tr>'
            + '</tbody></table>';
        expect(host.textContent).toContain(',');
        expect(host.querySelector('table').textContent).not.toContain(',');
        const stray = [...host.childNodes].filter((n) => n.nodeType === 3);
        expect(stray.map((n) => n.textContent)).toEqual([',']);
    });
});
