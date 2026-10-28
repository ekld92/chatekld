// Pins the retire-tag batch-undo affordances in audit.js (2026-08-04 audit).
//
// The server's batch restore is deliberately conservative: failed/skipped ops
// stay `applied` in the tagging journal so a re-run reverts exactly the
// remainder — and the stream itself says "re-run to revert the rest" on a
// TTL lapse. The client must therefore never destroy the only button that
// can re-run it. These are source ratchets, matching the house style of
// auditUnzoteroedTabs.test.js, because each is exactly the kind of thing a
// later cleanup would quietly undo.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const AUDIT_JS = readFileSync(join(ROOT, 'static/js/audit.js'), 'utf8');

/**
 * The RETIRE undo's onConfirm block.
 *
 * Anchored on `_retireTag` first, then on the restore call inside it. It used
 * to search the whole file for the first `'/api/tagging/restore'`, which broke
 * the moment a second batch-undo existed (Tag Harmony's, 2026-08-13): the
 * ratchet silently retargeted onto the new code and would have stopped
 * guarding retire entirely while still passing. A ratchet that can drift onto
 * a different function is not pinning anything.
 */
function undoBlock() {
    const fn = AUDIT_JS.indexOf('async function _retireTag(');
    expect(fn, '_retireTag not found — update this ratchet').toBeGreaterThan(-1);
    const start = AUDIT_JS.indexOf("'/api/tagging/restore'", fn);
    expect(start).toBeGreaterThan(-1);
    // Wide window: the whole confirm handler around the restore call.
    return AUDIT_JS.slice(Math.max(fn, start - 2000), start + 4000);
}

describe('retire undo: partial revert keeps its retry affordance', () => {
    it('branches on reverted < total and keeps the button enabled', () => {
        const block = undoBlock();
        expect(block).toMatch(/done\.reverted\s*<\s*done\.total/);
        const partial = block.match(/if \(done\.reverted < done\.total\) \{([\s\S]*?)\n\s*\}/);
        expect(partial, 'no partial-revert branch').toBeTruthy();
        // The branch re-enables and relabels — it must NOT remove the button.
        expect(partial[1]).toContain('undo.disabled = false');
        expect(partial[1]).toContain('retry undo retire');
        expect(partial[1]).not.toContain('undo.remove()');
        // ...and short-circuits so the full-revert removal below never runs.
        expect(partial[1]).toMatch(/return;/);
    });

    it('removes the button only on a full revert', () => {
        const block = undoBlock();
        const iPartial = block.indexOf('done.reverted < done.total');
        const iRemove = block.indexOf('undo.remove()');
        expect(iPartial).toBeGreaterThan(-1);
        expect(iRemove).toBeGreaterThan(iPartial);
    });

    it('tells the user the remainder stays retryable', () => {
        expect(undoBlock()).toMatch(/remain and stay retryable/);
    });
});

describe('retire undo: the stream is cancellable', () => {
    it('passes an abort signal to the restore SSE call', () => {
        const block = undoBlock();
        expect(block).toMatch(/new AbortController\(\)/);
        expect(block).toMatch(/signal:\s*ctrl\.signal/);
    });

    it('treats AbortError as a UI-only cancel that keeps the undo button', () => {
        const block = undoBlock();
        const abort = block.match(/AbortError'\) \{([\s\S]*?)\n\s*\}/);
        expect(abort, 'no AbortError branch').toBeTruthy();
        expect(abort[1]).toContain('undo.disabled = false');
        expect(abort[1]).not.toContain('undo.remove()');
        // Honest wording: the cancel does not un-revert anything.
        expect(abort[1]).toContain('cancelled');
        expect(abort[1]).toMatch(/reverted/);
    });

    it('the cancel affordance says what cancelling does and does not do', () => {
        const helper = AUDIT_JS.match(/function _mkUndoCancel[\s\S]*?\n\}/);
        expect(helper, 'no _mkUndoCancel helper').toBeTruthy();
        expect(helper[0]).toContain('aria-label');
        expect(helper[0]).toMatch(/already reverted stay/);
        // Removed when the stream ends, whatever the outcome.
        expect(undoBlock()).toMatch(/finally \{[\s\S]*?cancel\.remove\(\)/);
    });
});
