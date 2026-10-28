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
function over(rgbaStr, bg0) {
    const [r, g, b, a] = parseRgba(rgbaStr);
    // `bg0` may already be a composited rgb array, not a hex string: the
    // side-by-side diff stacks a WORD tint on top of a LINE tint on top of the
    // surface, and only the doubly-composited colour is what the eye sees.
    const bg = typeof bg0 === 'string' ? srgb(bg0) : bg0;
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
    ['--accent-hover text on card (.refactor-diff .diff-hunk, .btn-link)', (t) => t['--accent-hover'], (t) => t['--bg-card'], 4.5],
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
    // Help popover body text on the card surface (ui.js::helpPopover).
    ['--text-secondary on card (help popover body)',
        (t) => t['--text-secondary'], (t) => t['--bg-card'], 4.5],
    // 2026-07-26 UX audit — two surfaces that passed but were pinned by
    // NOTHING, so a token tweak could have degraded them silently:
    //  (a) the hover/focus TOOLTIP body (ui.js::helpTip → .help-tip), the only
    //      place --text-primary sits on --bg-elevated as sustained reading
    //      text;
    //  (b) the 71 `.control-hint` rows. These reuse --text-secondary/--bg-card,
    //      the SAME pair as "help popover body" above — listed separately and
    //      by name on purpose: per CLAUDE.md ("when reusing a token on a new
    //      surface, add the pair there"), retargeting that one label at some
    //      future popover redesign would otherwise drop all 71 hints out of the
    //      ratchet without anyone noticing.
    ['--text-primary on elevated (.help-tip body)',
        (t) => t['--text-primary'], (t) => t['--bg-elevated'], 4.5],
    ['--text-secondary on card (.control-hint)',
        (t) => t['--text-secondary'], (t) => t['--bg-card'], 4.5],
    // Audit dashboard (2026-08-13). The tiles' own text was already covered by
    // the --bg-elevated pairs, but the group labels and the coverage caveat
    // note sit on the CARD surface, whose opaque token is `--card-bg` — a
    // different token from `--bg-card` and, until these rows, one no pair in
    // this list referenced. The `-sub` denominator sits inside a tile, so it
    // is on elevated; it is named separately anyway because a denominator that
    // silently drops below contrast turns a coverage figure back into the
    // undenominated number the dashboard exists to replace.
    // Tag Harmony's "not in vocab" badge sits on the elevated chip fill, not
    // on the warn tint the warning banner uses — a different pair, and the one
    // that tells the user a listed tag will NOT be written.
    ['--warn-fg on elevated (.harmony-badge)',
        (t) => t['--warn-fg'], (t) => t['--bg-elevated'], 4.5],
    // Audit UX pass (2026-08-19). The evidence chip reuses tokens already in
    // this list on surfaces they already sit on — named separately per the
    // house rule ("when reusing a token on a new surface, add the pair
    // there"), because these chips are what a bulk WRITE decision is read
    // off: chip-id says "an identifier resolved this and may be bulk-linked".
    ['--text-secondary on elevated (.audit-chip)',
        (t) => t['--text-secondary'], (t) => t['--bg-elevated'], 4.5],
    ['--accent-soft-fg on soft tint (.audit-chip.chip-id)',
        (t) => t['--accent-soft-fg'], (t) => over(t['--accent-soft-bg'], t['--bg-card']), 4.5],
    ['--accent-soft-fg on card (.audit-acc-disclosure summary)',
        (t) => t['--accent-soft-fg'], (t) => t['--bg-card'], 4.5],
    ['--text-secondary on card (.audit-inv-dl dt)',
        (t) => t['--text-secondary'], (t) => t['--bg-card'], 4.5],
    // Manual link picker (2026-08-19): the SELECTED option composites over the
    // accent tint, where --text-tertiary would fail — the .audit-ambig-num
    // lesson, re-pinned on its own surface so a future retint of the picker
    // cannot quietly drop it.
    ['--text-secondary on soft tint (.acc-keypicker-opt.is-sel meta/title)',
        (t) => t['--text-secondary'], (t) => over(t['--accent-soft-bg'], t['--bg-card']), 4.5],
    ['--text-primary on soft tint (.acc-keypicker-opt.is-sel key)',
        (t) => t['--text-primary'], (t) => over(t['--accent-soft-bg'], t['--bg-card']), 4.5],
    ['--text-secondary on card-bg (.audit-summary-group-label / -note)',
        (t) => t['--text-secondary'], (t) => t['--card-bg'], 4.5],
    ['--text-secondary on elevated (.audit-summary-sub denominator)',
        (t) => t['--text-secondary'], (t) => t['--bg-elevated'], 4.5],
    // Inline "not saved" marker under a refused settings field
    // (settings.js::_flagDropped → .settings-field-error).
    ['--danger on card (.settings-field-error)',
        (t) => t['--danger'], (t) => t['--bg-card'], 4.5],
    // Focus outlines / active borders: non-text 3:1 on the major surfaces.
    ['--accent outline on app bg', (t) => t['--accent'], (t) => t['--bg-app'], 3.0],
    ['--accent outline on card', (t) => t['--accent'], (t) => t['--bg-card'], 3.0],
    // 2026-07-19 tagging/a11y batch: placeholders carry instructional content
    // and now use a managed token (tertiary was 4.4:1 dark on --input-bg —
    // just under); the suggest gloss / chip-num / badge-muted moved from
    // tertiary to secondary on their elevated/subtle fills; the disabled
    // target toggle swapped opacity-fade for the tertiary tier on the card.
    ['--text-secondary as ::placeholder on --input-bg',
        (t) => t['--text-secondary'], (t) => t['--input-bg'], 4.5],
    ['--text-secondary on elevated (suggest gloss, hovered row)',
        (t) => t['--text-secondary'], (t) => t['--bg-elevated'], 4.5],
    ['--text-tertiary on card (disabled target-toggle label)',
        (t) => t['--text-tertiary'], (t) => t['--card-bg'], 4.5],
    // 2026-07-20 Card Catalog batch — previously-unverified pairs surfaced by
    // the design audit, now pinned:
    // .warning-banner is the shared class behind EVERY module's status/error
    // text (deck/docgen/refactor/vault/audit) — one regression here degrades
    // legibility everywhere at once.
    ['--warn-fg on warn tint over card (.warning-banner)',
        (t) => t['--warn-fg'], (t) => over(t['--warn-bg'], t['--bg-card']), 4.5],
    ['--ok-fg on card (.refactor-diff .diff-add, milestone strip)',
        (t) => t['--ok-fg'], (t) => t['--bg-card'], 4.5],
    // The one OUTCOME line (2026-09-20, ui.js::statusLine). The tone GLYPH is
    // real text at 12px carrying the tone, and the audit scan banner paints
    // itself on --bg-elevated, so each tone owes a pair on that surface too —
    // a tone nobody can read is a tone conveyed by nothing at all.
    ['--ok-fg on card (.status-line.tone-ok glyph)',
        (t) => t['--ok-fg'], (t) => t['--bg-card'], 4.5],
    ['--danger on card (.status-line.tone-error)',
        (t) => t['--danger'], (t) => t['--bg-card'], 4.5],
    ['--text-tertiary on card (.status-at stamp)',
        (t) => t['--text-tertiary'], (t) => t['--bg-card'], 4.5],
    ['--ok-fg on card (.maint-state-chip[data-state=ok])',
        (t) => t['--ok-fg'], (t) => t['--bg-card'], 4.5],
    // Shared side-by-side diff (2026-08-29, ui.js::renderSideBySideDiff). The
    // container sets `color: var(--text-primary)` and `background:
    // var(--bg-app)` explicitly, so these pairs are what is actually rendered
    // in BOTH call sites (a report-table cell and the refactor detail pane) —
    // that explicitness is the reason a single pair can speak for both.
    // The WORD tints composite over the LINE tint, which composites over the
    // surface; pinning only the line tier would miss the tighter of the two.
    ['--text-primary on diff del line (.diff-sxs-cell.is-del)',
        (t) => t['--text-primary'], (t) => over(t['--diff-del-bg'], t['--bg-app']), 4.5],
    ['--text-primary on diff add line (.diff-sxs-cell.is-add)',
        (t) => t['--text-primary'], (t) => over(t['--diff-add-bg'], t['--bg-app']), 4.5],
    ['--text-primary on diff del WORD (.is-del .diff-sxs-word)',
        (t) => t['--text-primary'],
        (t) => over(t['--diff-del-word'], over(t['--diff-del-bg'], t['--bg-app'])), 4.5],
    ['--text-primary on diff add WORD (.is-add .diff-sxs-word)',
        (t) => t['--text-primary'],
        (t) => over(t['--diff-add-word'], over(t['--diff-add-bg'], t['--bg-app'])), 4.5],
    // The gutter carries the line number and the -/+ sign — the sign is the
    // NON-COLOUR channel for the diff state, so it has to be readable.
    ['--text-tertiary on app bg (.diff-sxs-ln / .diff-sxs-sign)',
        (t) => t['--text-tertiary'], (t) => t['--bg-app'], 4.5],
    ['--accent-hover on elevated (.diff-sxs-fold)',
        (t) => t['--accent-hover'], (t) => t['--bg-elevated'], 4.5],
    // Plain Chat attachment chips (2026-08-29). Same tokens as neighbouring
    // chips on the same surface, named separately per the house rule — the
    // `chip-trunc` warning is what tells the user the model is seeing only
    // part of their file, so it must not degrade silently.
    ['--text-primary on elevated (.chat-attachment-chip)',
        (t) => t['--text-primary'], (t) => t['--bg-elevated'], 4.5],
    ['--text-secondary on elevated (.chat-attachment-chip .chip-meta / remove)',
        (t) => t['--text-secondary'], (t) => t['--bg-elevated'], 4.5],
    ['--warn-fg on elevated (.chat-attachment-chip .chip-trunc)',
        (t) => t['--warn-fg'], (t) => t['--bg-elevated'], 4.5],
    ['--danger on card (.audit-error-tag, .btn-danger text)',
        (t) => t['--danger'], (t) => t['--bg-card'], 4.5],
    // N/F/Z legend badges: --on-cat letter on each category fill.
    ...['note', 'finder', 'zotero'].map((c) => [
        `--on-cat letter on --cat-${c} (legend badge)`,
        (t) => t['--on-cat'], (t) => t[`--cat-${c}`], 4.5,
    ]),
    // Disabled buttons moved from opacity-fade to the tertiary tier — the
    // label must stay readable on every button surface it can sit on.
    ['--text-tertiary as disabled .btn label on card',
        (t) => t['--text-tertiary'], (t) => t['--bg-card'], 4.5],
    // 2026-07-20 audit finding (M2): ambiguous-PDF triage deck. The pick-number
    // (.audit-ambig-num) is the deck's primary affordance and sits on hovered /
    // selected candidate rows — the old --text-tertiary composited to ~3.9:1
    // there. Now --text-secondary on the hover fill, --accent-soft-fg on the
    // selected fill (already pinned via .doc-type-badge), and the selection is
    // carried by a solid accent-soft-fg left bar (non-text, >= 3:1).
    ['--text-secondary as .audit-ambig-num on hovered candidate (surface-hover over card)',
        (t) => t['--text-secondary'], (t) => over(t['--surface-hover'], t['--bg-card']), 4.5],
    ['--accent-soft-fg selection bar/border on card (.audit-ambig-cand.is-sel)',
        (t) => t['--accent-soft-fg'], (t) => t['--bg-card'], 3.0],
    ['--text-primary as .audit-ambig-keys kbd on subtle over card',
        (t) => t['--text-primary'], (t) => over(t['--surface-subtle'], t['--bg-card']), 4.5],
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
