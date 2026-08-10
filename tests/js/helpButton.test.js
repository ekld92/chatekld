// Plan 1 (feature_plans_2026-08-05): single help source. The app used to ship
// TWO independently-authored full guides — the sidebar `#howto-modal` and the
// Help tab's guide.md — which had already drifted apart (the modal said
// "eleven report tabs", the guide said "nine"). The modal is deleted and the
// sidebar button now routes to the Help tab. Ratchets:
//  - index.html must not reintroduce a howto-modal;
//  - the sidebar button must call openHowTo();
//  - app.js's openHowTo must activate the Help tab AND fire the guide's lazy
//    loader (showTab alone leaves the tab empty — the loader is click-bound
//    to #tab-help, the exact reason the palette's "Go to Help" pairs them).
import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

const read = (rel) => fs.readFileSync(path.resolve(__dirname, '../..', rel), 'utf8');

describe('single help source (plan 2026-08-05/1)', () => {
    it('index.html has no howto-modal and routes the sidebar button to openHowTo', () => {
        const html = read('templates/index.html');
        expect(html).not.toMatch(/howto-modal/);
        expect(html).toMatch(/onclick="openHowTo\(\)"/);
    });

    it('app.js openHowTo shows the Help tab and loads the guide', () => {
        const src = read('static/js/app.js');
        const m = src.match(/window\.openHowTo\s*=\s*\(\)\s*=>\s*\{([^}]*)\}/);
        expect(m, 'window.openHowTo must be defined as an arrow block').toBeTruthy();
        expect(m[1]).toMatch(/showTab\('help'\)/);
        expect(m[1]).toMatch(/loadHelpTab\(\)/);
        expect(src).not.toMatch(/howto-modal/);
    });

    it('guide.md carries the merged modal-only content and no hardcoded tab count', () => {
        const guide = read('static/help/guide.md');
        expect(guide).toMatch(/## Tips & limits/);
        expect(guide).toMatch(/20 000 pages|20 000 pages/);
        // The 9-vs-11 drift class: never count the audit report tabs again.
        expect(guide).not.toMatch(/nine read-only reports/);
    });
});
