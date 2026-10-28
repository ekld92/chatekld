// Pins the Tag Harmony batch-undo affordances in audit.js (2026-08-13 review).
//
// The mirror of auditRetireUndo.test.js for the SECOND batch undo: the review
// found the harmony undo shipped with no coverage at all and without retire's
// cancel affordance — the exact drift the anchored ratchet pattern exists to
// catch. Same conservative server contract: failed/skipped ops stay `applied`
// so a re-run reverts exactly the remainder, and the client must never destroy
// the only button that can re-run it.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const AUDIT_JS = readFileSync(join(ROOT, 'static/js/audit.js'), 'utf8');

/**
 * The HARMONY undo's onConfirm block — anchored on `_harmonyApply` first,
 * then on the restore call inside it (the anchor-drift lesson from
 * auditRetireUndo: a whole-file search retargets silently when another
 * batch undo appears).
 */
function undoBlock() {
    const fn = AUDIT_JS.indexOf('async function _harmonyApply(');
    expect(fn, '_harmonyApply not found — update this ratchet').toBeGreaterThan(-1);
    const start = AUDIT_JS.indexOf("'/api/tagging/restore'", fn);
    expect(start).toBeGreaterThan(-1);
    // Wide window: the whole confirm handler around the restore call.
    return AUDIT_JS.slice(Math.max(fn, start - 2000), start + 4000);
}

/** The whole _harmonyApply function body, for the selection-reset pins. */
function applyBlock() {
    const fn = AUDIT_JS.indexOf('async function _harmonyApply(');
    expect(fn).toBeGreaterThan(-1);
    const next = AUDIT_JS.indexOf('\nfunction ', fn + 1);
    return AUDIT_JS.slice(fn, next > -1 ? next : undefined);
}

describe('harmony undo: partial revert keeps its retry affordance', () => {
    it('branches on reverted < total and keeps the button enabled', () => {
        const block = undoBlock();
        expect(block).toMatch(/done\.reverted\s*<\s*done\.total/);
        const partial = block.match(/if \(done\.reverted < done\.total\) \{([\s\S]*?)\n\s*\}/);
        expect(partial, 'no partial-revert branch').toBeTruthy();
        // The branch re-enables and relabels — it must NOT remove the button.
        expect(partial[1]).toContain('undo.disabled = false');
        expect(partial[1]).toContain('retry undo harmonize');
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

describe('harmony undo: the stream is cancellable', () => {
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

    it('removes the cancel affordance when the stream ends', () => {
        expect(undoBlock()).toMatch(/finally \{[\s\S]*?cancel\.remove\(\)/);
    });
});

describe('harmony apply: in-flight busy gate (2026-08-18)', () => {
    // The review's repro: _harmonyApply disabled only the button, but any
    // checkbox change re-ran _harmonyUpdateButton, which recomputed
    // `disabled` purely from selection size — tick a row mid-stream and a
    // second confirm launched a concurrent run. Retire's _retireSetAllDisabled
    // is the pattern; these pins hold its harmonize twin in place.
    it('freezes the whole surface for the run, and releases in finally', () => {
        const block = applyBlock();
        expect(block).toMatch(/_harmonySetAllDisabled\(true\)/);
        expect(block).toMatch(/finally \{[\s\S]*?_harmonySetAllDisabled\(false\)/);
    });

    it('the count-driven button recompute honors the busy flag', () => {
        const fn = AUDIT_JS.indexOf('function _harmonyUpdateButton(');
        expect(fn).toBeGreaterThan(-1);
        const body = AUDIT_JS.slice(fn, AUDIT_JS.indexOf('\nfunction ', fn + 1));
        expect(body).toMatch(/_harmonyBusy\s*\|\|/);
    });

    it('a re-entry while busy is refused outright', () => {
        expect(applyBlock()).toMatch(/_harmonyBusy\) return;/);
    });
});

describe('status hosts are report-identity-aware (2026-08-18)', () => {
    // A minutes-long run outlives tab switches; lazily re-creating the host
    // inside whatever #audit-report-body now holds sprayed status + the undo
    // button into a foreign report.
    it('harmonize status only renders into tag_harmony', () => {
        const fn = AUDIT_JS.indexOf('function _harmonyStatusHost(');
        expect(fn).toBeGreaterThan(-1);
        const body = AUDIT_JS.slice(fn, AUDIT_JS.indexOf('\nfunction ', fn + 1));
        expect(body).toMatch(/_currentReport !== 'tag_harmony'/);
    });

    it('retire status only renders into tag_hygiene', () => {
        const fn = AUDIT_JS.indexOf('function _retireStatusHost(');
        expect(fn).toBeGreaterThan(-1);
        const body = AUDIT_JS.slice(fn, AUDIT_JS.indexOf('\nfunction ', fn + 1));
        expect(body).toMatch(/_currentReport !== 'tag_hygiene'/);
    });
});

describe('harmony apply: a successful run spends the selection', () => {
    it('clears the selection and unchecks the boxes on success', () => {
        // Without this, the still-armed "Harmonize selected (N)" re-issued
        // every Zotero read for rows that were just written, to write nothing.
        const block = applyBlock();
        expect(block).toMatch(/_harmonySelected = new Set\(\)/);
        expect(block).toMatch(/data-harmony-key/);
        expect(block).toMatch(/cb\.checked = false/);
    });
});
