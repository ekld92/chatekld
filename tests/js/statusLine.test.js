// The app's one OUTCOME line (ui.js::statusLine) and the hosts migrated onto it.
//
// Five subsystems each grew their own `_xSay(msg, isError)` and they had
// drifted into three conventions — a bare muted <p>, a muted <div> holding a
// text span beside an undo button, and a `.warning-banner` wearing a
// `banner-neutral` modifier whose whole job was to say "ignore the warning
// colour". These pin the one shape, and the two behaviours that were actually
// wrong: a finished scan wearing the warning palette, and a persisted status
// line from earlier in the scan reading as if it had just happened.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const host = () => document.getElementById('h');
const textOf = (el) => el.querySelector('.status-text')?.textContent;

describe('statusLine()', () => {
    let ui;
    beforeEach(async () => {
        document.body.innerHTML = '<p id="h"></p>';
        vi.resetModules();
        ui = await import('../../static/js/ui.js');
    });

    it('renders glyph, text and tone class', () => {
        ui.statusLine(host(), { tone: 'ok', text: 'Linked 3 PDFs.' });
        expect(host().classList.contains('status-line')).toBe(true);
        expect(host().classList.contains('tone-ok')).toBe(true);
        expect(textOf(host())).toBe('Linked 3 PDFs.');
    });

    // The glyph is the tone's primary channel — a tone conveyed by colour
    // alone is unreadable under deuteranopia, and these lines report writes.
    it('carries a distinct glyph per tone, hidden from assistive tech', () => {
        const glyphs = new Set();
        for (const tone of ['info', 'ok', 'warn', 'error']) {
            ui.statusLine(host(), { tone, text: 'x' });
            const g = host().querySelector('.status-glyph');
            expect(g.getAttribute('aria-hidden')).toBe('true');
            glyphs.add(g.textContent);
        }
        expect(glyphs.size).toBe(4);
    });

    // `is-error` is what the app's own CSS and several existing tests key on;
    // the primitive must keep that name rather than teach a second one.
    it('keeps is-error as the error marker, and only for a real error', () => {
        ui.statusLine(host(), { tone: 'error', text: 'Failed (500)' });
        expect(host().classList.contains('is-error')).toBe(true);
        ui.statusLine(host(), { tone: 'ok', text: 'Done' });
        expect(host().classList.contains('is-error')).toBe(false);
        ui.statusLine(host(), { tone: 'error', text: '' });
        expect(host().classList.contains('is-error')).toBe(false);
    });

    it('an empty message clears the host completely', () => {
        ui.statusLine(host(), { tone: 'error', text: 'boom' });
        ui.statusLine(host(), { text: '' });
        expect(host().textContent).toBe('');
        expect(host().classList.contains('status-line')).toBe(false);
        expect(host().classList.contains('tone-error')).toBe(false);
    });

    // Vault paths, bib titles and server messages all land here.
    it('never interprets its text as markup', () => {
        ui.statusLine(host(), { text: '<img src=x onerror=alert(1)>' });
        expect(host().querySelector('img')).toBeNull();
        expect(textOf(host())).toBe('<img src=x onerror=alert(1)>');
    });

    it('is safe to call with no host', () => {
        expect(() => ui.statusLine(null, { text: 'x' })).not.toThrow();
    });

    describe('the stamp', () => {
        it('reads relatively and keeps the absolute time in the title', () => {
            const now = Date.now();
            ui.statusLine(host(), { text: 'Baselined 12 notes.', at: now - 7 * 60_000 });
            const at = host().querySelector('.status-at');
            expect(at.textContent).toBe('7 min ago');
            expect(at.title.length).toBeGreaterThan(0);
        });

        it('is omitted when there is nothing to stamp, and never shows a future time', () => {
            ui.statusLine(host(), { text: 'x' });
            expect(host().querySelector('.status-at')).toBeNull();
            ui.statusLine(host(), { text: 'x', at: Date.now() + 60_000 });
            expect(host().querySelector('.status-at')).toBeNull();
        });

        it('degrades to the title past a day rather than saying "34 h ago"', () => {
            ui.statusLine(host(), { text: 'x', at: Date.now() - 34 * 3600_000 });
            expect(host().querySelector('.status-at')).toBeNull();
        });

        it('relativeStamp covers the four readings', () => {
            const now = 1_700_000_000_000;
            expect(ui.relativeStamp(now - 10_000, now)).toBe('just now');
            expect(ui.relativeStamp(now - 5 * 60_000, now)).toBe('5 min ago');
            expect(ui.relativeStamp(now - 3 * 3600_000, now)).toBe('3 h ago');
            expect(ui.relativeStamp(null, now)).toBeNull();
        });
    });
});

describe('the information grammar is drawn', () => {
    const css = readFileSync(join(HERE, '../../static/css/styles.css'), 'utf8');

    // A modifier whose whole job is to say "ignore the colour of the shape I
    // am wearing" means the shape was wrong. It is retired for a tone.
    // (The NAME survives in the comments that explain why; the RULE and every
    // classList use of it are gone.)
    it('banner-neutral is retired as a rule and as a class the app applies', () => {
        expect(css).not.toMatch(/^\s*\.[^{]*banner-neutral[^{]*\{/m);
        for (const f of ['audit.js', 'ui.js', 'maintenance.js']) {
            const path = join(HERE, '../../static/js/', f);
            if (f === 'maintenance.js' && !existsSync(path)) continue;
            expect(readFileSync(path, 'utf8'), f)
                .not.toMatch(/classList\.[a-z]+\('banner-neutral'/);
        }
    });

    // `audit-status-line` was used on three hosts and defined nowhere — an
    // undefined class renders plausibly and wrong (the --mono lesson).
    it('every status class the app uses is defined', () => {
        for (const cls of ['.status-line', '.status-glyph', '.status-text',
                           '.status-at', '.audit-status-line', '.maint-state-chip']) {
            expect(css, `${cls} is used but never defined`).toContain(cls);
        }
    });
});

describe('the scan banner is an outcome, not a warning', () => {
    const src = readFileSync(join(HERE, '../../static/js/audit.js'), 'utf8');

    it('tones the banner by scan state instead of denying the warning shape', () => {
        expect(src).toMatch(/status\.state === 'done' \? 'ok'/);
        expect(src).toMatch(/status\.state === 'cancelled' \? 'warn'/);
        expect(src).toMatch(/status\.state === 'error' \? 'error'/);
    });
});
