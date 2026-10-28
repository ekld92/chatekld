// Button grammar — the inline-verb role table (audit.js::ACTION_ROLES).
//
// RATCHET (the WRITE_ACTION_IDS shape): every `data-action` this file emits
// must have a row in ACTION_ROLES, and every row must name an action the file
// still emits. A new verb without a role, or a row left behind by a deleted
// one, fails the suite rather than shipping an undifferentiated button.
//
// Why a role table at all: 54 distinct actions across ~159 buttons were one
// undifferentiated blue, so nothing told a reader which click only looks at
// something and which takes something away.
import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const AUDIT_JS = join(HERE, '../../static/js/audit.js');
const STYLES = join(HERE, '../../static/css/styles.css');

/** Every `data-action` literal audit.js emits — from the HTML templates and
 *  from the one `setAttribute` site. */
function emittedActions(src) {
    const out = new Set();
    for (const m of src.matchAll(/data-action=(?:\\?["'])([a-zA-Z0-9_-]+)/g)) out.add(m[1]);
    for (const m of src.matchAll(/setAttribute\('data-action',\s*'([a-zA-Z0-9_-]+)'/g)) out.add(m[1]);
    return out;
}

describe('ACTION_ROLES table', () => {
    let audit, src;
    beforeAll(async () => {
        src = readFileSync(AUDIT_JS, 'utf8');
        audit = await import('../../static/js/audit.js');
    });

    it('covers every data-action audit.js emits', () => {
        const missing = [...emittedActions(src)].filter((a) => !(a in audit.ACTION_ROLES));
        expect(missing, `data-action(s) with no role: ${missing.join(', ')}`).toEqual([]);
    });

    it('has no row for an action the file no longer emits', () => {
        const emitted = emittedActions(src);
        const dead = Object.keys(audit.ACTION_ROLES).filter((a) => !emitted.has(a));
        expect(dead, `dead ACTION_ROLES row(s): ${dead.join(', ')}`).toEqual([]);
    });

    it('every role is one of the declared roles', () => {
        for (const [action, role] of Object.entries(audit.ACTION_ROLES)) {
            expect(audit.BUTTON_ROLES, `${action}: unknown role ${role}`).toContain(role);
        }
    });

    // The point of the grammar: a reader must be able to see the difference
    // BEFORE clicking, so all three buckets have to be genuinely populated.
    it('the three inline roles are all used', () => {
        const used = new Set(Object.values(audit.ACTION_ROLES));
        expect(used).toContain('read');
        expect(used).toContain('write');
        expect(used).toContain('destructive');
    });

    // "Overwrites text the user may have written" is the destructive test,
    // not "which HTTP verb". Both sync directions overwrite the OTHER side's
    // region — which is exactly why the route demands accept_conflict on top
    // of confirm — so calling push `write` would say the safer thing about
    // the more dangerous of the pair.
    it('both note-sync directions are destructive, not just the inbound one', () => {
        for (const a of ['notesync-pull', 'notesync-pull-all',
                         'notesync-push', 'notesync-push-all', 'notesync-refresh-all']) {
            expect(audit.ACTION_ROLES[a], a).toBe('destructive');
        }
    });

    // A dry run and an online identification change nothing the user can
    // lose; drawing them as writes would cry wolf on the two verbs a user
    // should feel free to press.
    it('dry runs, previews and lookups are read', () => {
        for (const a of ['zl-retitle-dry', 'niz-identify', 'notesync-preview',
                         'notesync-inspect-create', 'notesync-merge', 'zc-open']) {
            expect(audit.ACTION_ROLES[a], a).toBe('read');
        }
    });
});

describe('applyActionRoles() stamping', () => {
    let audit;
    beforeAll(async () => { audit = await import('../../static/js/audit.js'); });

    it('stamps a role from the table onto each verb', () => {
        const root = document.createElement('div');
        root.innerHTML = '<button data-action="open"></button>'
            + '<button data-action="apply-tags"></button>'
            + '<button data-action="detach"></button>';
        audit.applyActionRoles(root);
        const roles = [...root.querySelectorAll('button')].map((b) => b.dataset.role);
        expect(roles).toEqual(['read', 'write', 'destructive']);
    });

    // One action can serve several verbs — `zl-run` is both a dry run and the
    // real link, `niz-bulk` carries five data-verbs. The table gives the
    // DEFAULT; an emit site that declares its own role keeps it.
    it('never overrides a role the emit site declared', () => {
        const root = document.createElement('div');
        root.innerHTML = '<button data-action="zl-run" data-role="read"></button>'
            + '<button data-action="niz-bulk" data-role="destructive"></button>';
        audit.applyActionRoles(root);
        const roles = [...root.querySelectorAll('button')].map((b) => b.dataset.role);
        expect(roles).toEqual(['read', 'destructive']);
    });

    it('leaves an unknown action alone rather than guessing', () => {
        const root = document.createElement('div');
        root.innerHTML = '<button data-action="not-a-real-action"></button>';
        audit.applyActionRoles(root);
        expect(root.querySelector('button').dataset.role).toBeUndefined();
    });

    it('is idempotent and tolerates a missing root', () => {
        const root = document.createElement('div');
        root.innerHTML = '<button data-action="detach"></button>';
        audit.applyActionRoles(root);
        audit.applyActionRoles(root);
        expect(root.querySelector('button').dataset.role).toBe('destructive');
        expect(() => audit.applyActionRoles(null)).not.toThrow();
    });
});

describe('the grammar is drawn, and colour is never the only channel', () => {
    let css;
    beforeAll(() => { css = readFileSync(STYLES, 'utf8'); });

    it('write and destructive verbs carry a glyph, not just a colour', () => {
        expect(css).toMatch(/\[data-role="write"\]::before/);
        expect(css).toMatch(/\[data-role="destructive"\]::before/);
    });

    // Generated content is announced by some screen readers. The empty
    // alternative-text form keeps the glyph a channel for the eye only, so a
    // verb's own label stays exactly what AT reads.
    it('the glyphs declare empty alternative text', () => {
        const rules = css.match(/\[data-role="(?:write|destructive)"\]::before \{[^}]*\}/g) || [];
        expect(rules.length).toBe(2);
        for (const r of rules) expect(r, r).toMatch(/content:\s*"[^"]*"\s*\/\s*""/);
    });

    // The Library section strip chooses a view; it must not wear the shape
    // the app uses for "do something to my library".
    it('the Library section strip is not action-shaped', () => {
        expect(readFileSync(join(HERE, '../../templates/index.html'), 'utf8'))
            .not.toMatch(/btn-outline[^"]*audit-section-btn|audit-section-btn[^"]*btn-outline/);
        expect(css).toMatch(/\.audit-section-btn \{[^}]*border:\s*none/);
    });
});
