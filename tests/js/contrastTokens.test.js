// Track 6a / 7.4 — contrast lint over the theme-token file.
//
// DEFECT CLASS this pins: a well-intentioned token tweak (or a new badge
// reusing an existing token on a new surface) silently dropping below WCAG AA
// — exactly how the pre-6a values shipped (#6f63ff put white button text at
// 4.28:1, the light ollama/openai badges sat at 4.31/4.37:1, the resting copy
// buttons at ~2.7:1). The lint parses static/css/styles.css directly, so any
// future edit to a pinned token re-runs the real math; editing a value below
// threshold fails the suite instead of shipping.
//
// The pair list mirrors REAL usage sites (which token sits on which surface),
// including alpha-composited tints (accent-glow / accent-soft-bg over the
// card) — a naive token-vs-token check misses those. Thresholds: 4.5:1 for
// text (WCAG 1.4.3 AA), 3:1 for non-text UI (1.4.11).
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const css = readFileSync(
    resolve(dirname(fileURLToPath(import.meta.url)), '../../static/css/styles.css'),
    'utf-8',
);

function blockAfter(needle) {
    const i = css.indexOf(needle);
    if (i === -1) throw new Error(`selector not found: ${needle}`);
    const j = css.indexOf('{', i);
    let depth = 0;
    for (let k = j; k < css.length; k++) {
        if (css[k] === '{') depth++;
        else if (css[k] === '}') { depth--; if (depth === 0) return css.slice(j + 1, k); }
    }
    throw new Error(`unbalanced block after ${needle}`);
}

function tokens(block) {
    const out = {};
    for (const m of block.matchAll(/(--[\w-]+):\s*(#[0-9a-fA-F]{3,6}|rgba\([^)]*\))/g)) {
        out[m[1]] = m[2];
    }
    return out;
}

// NOTE: the light selector is matched with its leading newline so the
// occurrence inside the theming header COMMENT is not picked up (that comment
// mentions `[data-theme="light"]` in prose — the bug our own first parse hit).
const DARK = tokens(blockAfter(':root,\n[data-theme="dark"]'));
const LIGHT = tokens(blockAfter('\n[data-theme="light"] {'));

function srgb(hex) {
    let h = hex.replace('#', '');
    if (h.length === 3) h = h.split('').map((c) => c + c).join('');
    return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);
}
function luminance(rgb) {
    const f = (c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
    const [r, g, b] = rgb;
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}
const asRgb = (x) => (typeof x === 'string' ? srgb(x) : x);
function ratio(a, b) {
    const la = luminance(asRgb(a));
    const lb = luminance(asRgb(b));
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}
function parseRgba(s) {
    const m = /rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)/.exec(s);
    if (!m) throw new Error(`not rgba: ${s}`);
    return [+m[1] / 255, +m[2] / 255, +m[3] / 255, +m[4]];
}
// Composite an rgba tint over an opaque hex background (what the eye sees for
// badge fills like accent-glow / accent-soft-bg).
function over(rgbaStr, bgHex) {
    const [r, g, b, a] = parseRgba(rgbaStr);
    const bg = srgb(bgHex);
    return [r * a + bg[0] * (1 - a), g * a + bg[1] * (1 - a), b * a + bg[2] * (1 - a)];
}

// (label, fg, bg, min) — fg/bg are resolvers over the theme's token map.
const PAIRS = [
    // Provider badge TEXT + dot on the sidebar (ui.js::updateProviderBadge).
    ...['ollama', 'lm-studio', 'openai', 'anthropic', 'google'].map((p) => [
        `--provider-${p} badge text on sidebar`,
        (t) => t[`--provider-${p}`], (t) => t['--bg-sidebar'], 4.5,
    ]),
    // Runtime-status dots: non-text UI on the sidebar (shape carries the
    // state too — see .dot[data-state] — but color still must be >= 3:1).
    ...['ok', 'warn', 'error'].map((s) => [
        `--status-${s} dot on sidebar`,
        (t) => t[`--status-${s}`], (t) => t['--bg-sidebar'], 3.0,
    ]),
    // White button/bubble text on accent surfaces (.btn-primary, .message-user).
    ['white on --accent (btn-primary)', () => '#ffffff', (t) => t['--accent'], 4.5],
    ['white on --accent-hover-bg (btn-primary:hover)', () => '#ffffff', (t) => t['--accent-hover-bg'], 4.5],
    ['white on --user-msg-from', () => '#ffffff', (t) => t['--user-msg-from'], 4.5],
    ['white on --user-msg-to', () => '#ffffff', (t) => t['--user-msg-to'], 4.5],
    // Accent-as-TEXT tokens on the surfaces they actually sit on.
    ['--accent-soft-fg on card (.audit-link-btn)', (t) => t['--accent-soft-fg'], (t) => t['--bg-card'], 4.5],
    ['--accent-hover text on card (.refactor-diff .diff-hunk)', (t) => t['--accent-hover'], (t) => t['--bg-card'], 4.5],
    ['--accent-hover on glow tint (.refactor-badge.badge-changed)',
        (t) => t['--accent-hover'], (t) => over(t['--accent-glow'], t['--bg-card']), 4.5],
    ['--accent-soft-fg on soft tint (.doc-type-badge)',
        (t) => t['--accent-soft-fg'], (t) => over(t['--accent-soft-bg'], t['--bg-card']), 4.5],
    ['--accent-hover on soft tint (.stat-pill)',
        (t) => t['--accent-hover'], (t) => over(t['--accent-soft-bg'], t['--bg-card']), 4.5],
    // Muted control text at FULL opacity (.copy-btn / .code-copy-btn /
    // .refactor-badge rest states — the old opacity:0.5 fade was ~2.7:1).
    ['--text-secondary on elevated (copy buttons, badges)',
        (t) => t['--text-secondary'], (t) => t['--bg-elevated'], 4.5],
    // Focus outlines / active borders: non-text 3:1 on the major surfaces.
    ['--accent outline on app bg', (t) => t['--accent'], (t) => t['--bg-app'], 3.0],
    ['--accent outline on card', (t) => t['--accent'], (t) => t['--bg-card'], 3.0],
];

describe.each([
    ['dark', DARK],
    ['light', LIGHT],
])('theme tokens meet WCAG AA (%s)', (_name, t) => {
    it.each(PAIRS.map(([label, fg, bg, min]) => [label, fg, bg, min]))(
        '%s',
        (_label, fg, bg, min) => {
            expect(ratio(fg(t), bg(t))).toBeGreaterThanOrEqual(min);
        },
    );

    it('user-bubble copy button scrim keeps white text >= 4.5:1', () => {
        // .message-user .copy-btn: rgba(0,0,0,0.28) scrim over the bubble
        // gradient; check the LIGHTER stop (worst case for white text).
        const from = srgb(t['--user-msg-from']).map((c) => c * (1 - 0.28));
        expect(ratio('#ffffff', from)).toBeGreaterThanOrEqual(4.5);
    });
});
