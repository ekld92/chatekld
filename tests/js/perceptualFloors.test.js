// Track 6d pinning — perceptual floors.
//
// Defects pinned: all type was px-sized inside a height:100vh/overflow:hidden
// shell, so text could not be enlarged AT ALL (browser/user font scaling has
// no effect on px); 10–11px micro-type throughout; mini controls (~18px copy
// buttons, sliver-padded audit link buttons) under the 24px WCAG 2.2 target
// minimum; fixed-height headers that clip scaled text; a tab strip with no
// overflow affordance once labels grow.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const css = readFileSync(
    resolve(dirname(fileURLToPath(import.meta.url)), '../../static/css/styles.css'),
    'utf-8',
);

function block(selector) {
    const i = css.indexOf(selector);
    expect(i, `selector not found: ${selector}`).toBeGreaterThan(-1);
    const j = css.indexOf('{', i);
    return css.slice(j + 1, css.indexOf('}', j));
}

describe('type scales with the user (Track 6d)', () => {
    it('no px font sizes remain (rem passes user scaling through)', () => {
        expect(css).not.toMatch(/font-size:\s*[0-9.]+px/);
    });

    it('no type below the 12px floor (0.75rem)', () => {
        for (const m of css.matchAll(/font-size:\s*([0-9.]+)rem/g)) {
            expect(parseFloat(m[1])).toBeGreaterThanOrEqual(0.75);
        }
    });
});

describe('layout floors (Track 6d)', () => {
    it('headers use min-height, not fixed height', () => {
        expect(block('\n.header {')).toMatch(/min-height:\s*56px/);
        expect(block('.card > .header {')).toMatch(/min-height:\s*62px/);
        expect(block('\n.header {')).not.toMatch(/(?<!min-)height:\s*56px/);
        expect(block('.card > .header {')).not.toMatch(/(?<!min-)height:\s*62px/);
    });

    it('the tab strip has an overflow affordance', () => {
        expect(block('\n.tabs {')).toMatch(/overflow-x:\s*auto/);
    });

    it.each(['.copy-btn {', '.code-copy-btn {', '.audit-link-btn {', '.example-chip {'])(
        '%s meets the 24px minimum target',
        (sel) => {
            expect(block(sel)).toMatch(/min-height:\s*24px/);
        },
    );
});
