// Restrained gamification (2026-09-20).
//
// The house rule these pin, and the reason they are worth pinning at all:
// nothing here may be load-bearing, and nothing here may nudge anyone toward a
// WRITE. Explicitly declined and asserted absent: streaks, points, levels,
// badges, and any prompt beside a write verb. What is allowed is showing a
// user a true fact about their own library they would otherwise compute.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));

function memoryStorage() {
    const m = new Map();
    return {
        getItem: (k) => (m.has(k) ? m.get(k) : null),
        setItem: (k, v) => m.set(k, String(v)),
        removeItem: (k) => m.delete(k),
        clear: () => m.clear(),
    };
}

describe('primitives', () => {
    let ui;
    beforeEach(async () => {
        vi.stubGlobal('localStorage', memoryStorage());
        document.body.innerHTML = '<div id="sr-status-announcer"></div><p id="anchor"></p>';
        vi.resetModules();
        ui = await import('../../static/js/ui.js');
    });

    describe('dailyCounter', () => {
        it('counts today and resets on a new day', () => {
            const c = ui.dailyCounter('k');
            expect(c.count()).toBe(0);
            c.bump(); c.bump();
            expect(c.count()).toBe(2);
            // Yesterday's record is not today's count.
            localStorage.setItem('k', JSON.stringify({ d: '1999-01-01', n: 99 }));
            expect(c.count()).toBe(0);
        });

        // A streak turns a day off into a penalty. Deliberately not built.
        it('stores only a date and a number — no streak to break', () => {
            ui.dailyCounter('k').bump();
            expect(Object.keys(JSON.parse(localStorage.getItem('k'))).sort())
                .toEqual(['d', 'n']);
        });

        it('survives storage being unavailable', () => {
            vi.stubGlobal('localStorage', {
                getItem() { throw new Error('private mode'); },
                setItem() { throw new Error('private mode'); },
            });
            const c = ui.dailyCounter('k');
            expect(c.count()).toBe(0);
            expect(() => c.bump()).not.toThrow();
        });
    });

    describe('trendSeries', () => {
        // Without an identity, opening the tab twice would draw a flat line
        // and call it a trend.
        it('records one sample per identity, never per read', () => {
            const t = ui.trendSeries('h', 10);
            t.record('scan-1', 10);
            t.record('scan-1', 10);
            t.record('scan-2', 7);
            expect(t.values().map((p) => p.n)).toEqual([10, 7]);
        });

        it('keeps only the last N', () => {
            const t = ui.trendSeries('h', 3);
            for (let i = 0; i < 6; i++) t.record(`s${i}`, i);
            expect(t.values().map((p) => p.n)).toEqual([3, 4, 5]);
        });

        it('ignores a sample with no identity or no number', () => {
            const t = ui.trendSeries('h', 10);
            t.record('', 5);
            t.record('s', NaN);
            expect(t.values()).toEqual([]);
        });

        it('survives a corrupt or foreign value in storage', () => {
            localStorage.setItem('h', '{not json');
            expect(ui.trendSeries('h').values()).toEqual([]);
            localStorage.setItem('h', '{"a":1}');
            expect(ui.trendSeries('h').values()).toEqual([]);
        });
    });

    describe('sparkline', () => {
        it('draws a polyline and marks the latest reading', () => {
            const el = ui.sparkline([5, 3, 9, 1], { label: 'x' });
            expect(el.tagName.toLowerCase()).toBe('svg');
            expect(el.querySelector('polyline').getAttribute('points').split(' ').length).toBe(4);
            expect(el.querySelector('circle')).toBeTruthy();
        });

        // The shape is decoration; the numbers are the content.
        it('is an image with a label a reader can hear', () => {
            const el = ui.sparkline([2, 1], { label: 'Needs attention: 2, 1. Now 1.' });
            expect(el.getAttribute('role')).toBe('img');
            expect(el.getAttribute('aria-label')).toContain('Now 1');
        });

        // One point is not a trend; a lone dot would look like a measurement.
        it('draws nothing below two points', () => {
            expect(ui.sparkline([4])).toBeNull();
            expect(ui.sparkline([])).toBeNull();
        });

        it('does not divide by zero on a flat series', () => {
            const el = ui.sparkline([3, 3, 3]);
            expect(el.querySelector('polyline').getAttribute('points')).not.toMatch(/NaN/);
        });
    });

    describe('milestoneOnce', () => {
        it('shows once, ever', () => {
            const host = document.getElementById('anchor');
            expect(ui.milestoneOnce('m', host, { title: 'Done' })).toBeTruthy();
            expect(ui.milestoneOnce('m', host, { title: 'Done' })).toBeNull();
            expect(document.querySelectorAll('.milestone-strip').length).toBe(1);
        });

        // The historical defect: burning the flag on a run with no number to
        // show made the real milestone permanently unshowable.
        it('does not burn the flag when there is nothing to show', () => {
            expect(ui.milestoneOnce('m', null, { title: 'Done' })).toBeNull();
            expect(localStorage.getItem('m')).toBeNull();
            expect(ui.milestoneOnce('m', document.getElementById('anchor'), { title: 'Done' }))
                .toBeTruthy();
        });

        it('is a status region and never interprets its text as markup', () => {
            const strip = ui.milestoneOnce('m', document.getElementById('anchor'),
                { title: '<b>x</b>', detail: '<i>y</i>' });
            expect(strip.getAttribute('role')).toBe('status');
            expect(strip.querySelector('b')).toBeNull();
            expect(strip.textContent).toContain('<b>x</b>');
        });
    });
});

describe('what the library-health number is made of', () => {
    const src = readFileSync(join(HERE, '../../static/js/audit.js'), 'utf8');

    // Each of these goes to zero when the library is clean, which is the
    // property that makes a trend line mean anything.
    it('counts the five things a cleanup closes', () => {
        const block = src.slice(src.indexOf('const _HEALTH_FIELDS'),
            src.indexOf('const _healthTrend'));
        for (const f of ['pdfs_unmapped', 'pdfs_ambiguous', 'zotero_items_without_record',
                         'notes_without_record', 'mapping_orphans']) {
            expect(block, f).toContain(f);
        }
    });

    // "Tagged nowhere" is a tagging-campaign metric in the hundreds and would
    // swamp every reconciliation fix; "entry, no local PDF" is ~2 000 and the
    // decisions that retire those rows are not in this payload, so it would
    // never move however much work was done. (Not red against the code that
    // had no table at all — mutation-checked instead, by adding none_tagged
    // back to the list.)
    it('excludes the two metrics that would drown or never move', () => {
        const block = src.slice(src.indexOf('const _HEALTH_FIELDS'),
            src.indexOf('const _healthTrend'));
        expect(block).not.toContain('none_tagged');
        expect(block).not.toContain('bib_with_pdf');
    });

    it('names its composition on screen rather than showing a bare number', () => {
        expect(src).toContain('audit-health-note');
        expect(src).toMatch(/_HEALTH_FIELDS\.map\(\(\[, label\]\) => label\)/);
    });
});

describe('the house rule holds', () => {
    // CODE only: every file comments "no streaks, no goals" to explain why,
    // and a ratchet that its own rationale trips is a ratchet nobody can keep.
    const stripComments = (src) => src
        .replace(/\/\*[\s\S]*?\*\//g, ' ')
        .replace(/(^|[^:'"\\])\/\/.*$/gm, '$1');
    const JS = ['audit.js', 'ui.js', 'tagging.js']
        .map((f) => stripComments(readFileSync(join(HERE, '../../static/js/', f), 'utf8')))
        .join('\n');

    // Explicitly declined in the 2026-09-20 brief. A ratchet, so a later
    // "small addition" has to argue with this test first.
    it('builds no streaks, points, levels or badges', () => {
        // Substrings, not \b-anchored words: `_streakDays` is exactly how one
        // of these would arrive, and a word boundary lets it straight through
        // (checked by mutation, 2026-09-20). "points" alone is unbannable —
        // an SVG polyline has some — so it is banned in its scoring phrasings.
        for (const banned of [/streak/i, /points?\s*(earned|scored|total)/i,
                              /level\s*up/i, /levelUp/i, /achievement/i, /leaderboard/i,
                              /\bbadgesEarned\b/i]) {
            expect(JS, `banned gamification concept: ${banned}`).not.toMatch(banned);
        }
    });

    // Nothing decorative may talk anyone into a write: a milestone states a
    // fact that is ALREADY true, and the counters count what already landed.
    it('every milestone reports a state already reached, never invites a write', () => {
        const src = readFileSync(join(HERE, '../../static/js/audit.js'), 'utf8');
        const calls = [...src.matchAll(/milestoneOnce\([^)]*?title:\s*'([^']+)'/gs)]
            .map((m) => m[1]);
        expect(calls.length).toBeGreaterThanOrEqual(2);
        for (const t of calls) {
            expect(t, `${t} reads as an instruction`).not.toMatch(/^(Run|Click|Try|Fix|Add|Start)\b/);
        }
    });
});
