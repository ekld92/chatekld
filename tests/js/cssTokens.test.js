// 2026-09-03 UI review — every `var(--x)` the stylesheet uses must be defined.
//
// DEFECT PINNED: `--font-mono` was used at seven sites (Prompt Hub model
// badges, the audit tables' `.mono` cells, three log/code surfaces) and defined
// nowhere, so all seven fell through to the `monospace` fallback — the browser
// default, Courier on macOS — while ten OTHER surfaces declared
// `ui-monospace, SFMono-Regular, Menlo, monospace` literally. The two mono
// conventions rendered in visibly different faces. `--bg` had the same shape at
// the command palette's scrim: `color-mix(… var(--bg, #000) 40% …)` always
// painted 40% black, ignoring the light theme and diverging from every modal,
// which uses the theme-aware `--overlay-scrim`.
//
// A fallback makes such a use degrade quietly rather than break, which is
// exactly why nothing caught it — the page looks fine, just wrong. This test
// is the ratchet: a `var()` whose token is never declared fails the suite,
// fallback or not.
import { readFileSync } from 'node:fs';

import { describe, it, expect } from 'vitest';

// Comments are stripped first: this file's own prose quotes the defective
// declarations, and a scan that counted those would fail on its own evidence.
const CSS = readFileSync('static/css/styles.css', 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '');

function used() {
    return new Set(Array.from(CSS.matchAll(/var\((--[A-Za-z0-9-]+)/g), (m) => m[1]));
}
function defined() {
    return new Set(Array.from(CSS.matchAll(/^\s*(--[A-Za-z0-9-]+)\s*:/gm), (m) => m[1]));
}

describe('CSS custom properties', () => {
    it('every token used is declared somewhere', () => {
        const missing = [...used()].filter((t) => !defined().has(t)).sort();
        expect(missing).toEqual([]);
    });

    it('the app has ONE mono stack, and it is a token', () => {
        expect(defined().has('--font-mono')).toBe(true);
        // No second spelling: `--mono` was a one-off alias at a single site.
        expect(used().has('--mono')).toBe(false);
    });

    it('the command palette uses the same theme-aware scrim as the modals', () => {
        const palette = CSS.slice(CSS.indexOf('.cmdk-overlay'), CSS.indexOf('.cmdk-overlay') + 700);
        expect(palette).toContain('var(--overlay-scrim)');
    });

    it('pending-review CSS uses theme tokens — no literal #aaa / #3765aa / #e6e6e6', () => {
        const start = CSS.indexOf('.pending-review');
        expect(start).toBeGreaterThan(-1);
        // Through end of stylesheet (pending block is last); keep the scope tight
        // enough that a future sibling section cannot reintroduce a literal.
        const block = CSS.slice(start);
        expect(block).not.toMatch(/#aaa\b|#3765aa\b|#e6e6e6\b/i);
        expect(block).toContain('var(--accent)');
        expect(block).toContain('var(--border)');
        expect(block).toContain('var(--bg-elevated)');
        expect(block).toContain('var(--surface-subtle)');
        // Accession-panel parity: accent top rule on the sticky batch bar.
        expect(block).toMatch(/\.pending-batch\s*\{[^}]*border-top:\s*3px\s+solid\s+var\(--accent\)/s);
    });
});
