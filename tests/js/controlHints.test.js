// 2026-07-26 UX audit — `.control-hint` → aria-describedby wiring
// (ui.js::wireControlHints).
//
// DEFECT PINNED: templates/index.html carried 71 `.control-hint` elements and
// exactly 2 aria-describedby attributes, so a screen-reader user landing on a
// setting heard its NAME ("Reranker model (HuggingFace id), edit text") and
// none of its operative instructions — not the restart requirement, not the
// download size, not one of the 71 hints. The fix is one central DOM pass, so
// this file pins the ASSOCIATION RULE (nearest preceding control in the same
// .control-group) rather than any individual control: get the rule wrong and a
// multi-control group describes the wrong widget, which is worse than silence.
import { describe, it, expect, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const indexHtml = readFileSync(resolve(here, '../../templates/index.html'), 'utf-8');

describe('wireControlHints()', () => {
    let ui;
    beforeEach(async () => {
        ui = await import('../../static/js/ui.js');
    });

    it('describes the control that PRECEDES the hint', () => {
        document.body.innerHTML = `
          <div class="control-group">
            <label for="a">Reranker model</label>
            <input id="a">
            <small class="control-hint">Restart after changing it.</small>
          </div>`;
        expect(ui.wireControlHints()).toBe(1);
        const hint = document.querySelector('.control-hint');
        expect(hint.id).toBeTruthy();
        expect(document.getElementById('a').getAttribute('aria-describedby')).toBe(hint.id);
    });

    it('reaches into a wrapping <label class="checkbox-row"> for its input', () => {
        document.body.innerHTML = `
          <div class="control-group">
            <label class="checkbox-row"><input type="checkbox" id="cb"> Hybrid retrieval</label>
            <small class="control-hint">Fuses lexical and semantic search.</small>
          </div>`;
        ui.wireControlHints();
        const hint = document.querySelector('.control-hint');
        expect(document.getElementById('cb').getAttribute('aria-describedby')).toBe(hint.id);
    });

    // The agent group in the real template is checkbox + slider + a SECOND
    // checkbox + its hint. Describing every control in the group with the last
    // hint would tell a screen-reader user that the agent toggle needs a Tavily
    // key — worse than no description at all.
    it('does NOT describe earlier controls in a multi-control group', () => {
        document.body.innerHTML = `
          <div class="control-group">
            <label class="checkbox-row"><input type="checkbox" id="agent"> Agent mode</label>
            <small class="control-hint">Multi-step tool use.</small>
            <input type="range" id="iters">
            <label class="checkbox-row"><input type="checkbox" id="web"> Web search</label>
            <small class="control-hint">Needs TAVILY_API_KEY.</small>
          </div>`;
        ui.wireControlHints();
        const [h1, h2] = document.querySelectorAll('.control-hint');
        expect(document.getElementById('agent').getAttribute('aria-describedby')).toBe(h1.id);
        expect(document.getElementById('web').getAttribute('aria-describedby')).toBe(h2.id);
        // The slider sits between the two hints and is described by neither.
        expect(document.getElementById('iters').getAttribute('aria-describedby')).toBe(null);
    });

    it('skips the "?" help badge (a button is not a form control)', () => {
        document.body.innerHTML = `
          <div class="control-group">
            <label for="x">Setting</label>
            <button class="help-badge" type="button">?</button>
            <input id="x">
            <small class="control-hint">Hint text.</small>
          </div>`;
        ui.wireControlHints();
        const hint = document.querySelector('.control-hint');
        expect(document.getElementById('x').getAttribute('aria-describedby')).toBe(hint.id);
    });

    it('is idempotent and preserves a hand-authored aria-describedby', () => {
        document.body.innerHTML = `
          <div class="control-group">
            <span id="preexisting">extra</span>
            <label for="y">Setting</label>
            <input id="y" aria-describedby="preexisting">
            <small class="control-hint">Hint text.</small>
          </div>`;
        expect(ui.wireControlHints()).toBe(1);
        expect(ui.wireControlHints()).toBe(0);            // second pass is a no-op
        const hint = document.querySelector('.control-hint');
        const ids = document.getElementById('y').getAttribute('aria-describedby').split(/\s+/);
        expect(ids).toContain('preexisting');
        expect(ids).toContain(hint.id);
    });

    // Cheap guard on the thing that motivated the helper: if someone strips the
    // hints out of the template, the ratchet above is testing nothing.
    it('the template still has the hint corpus this helper exists for', () => {
        const n = (indexHtml.match(/class="control-hint"/g) || []).length;
        expect(n).toBeGreaterThan(50);
    });
});
