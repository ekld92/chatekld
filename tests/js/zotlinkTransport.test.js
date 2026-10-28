// Source ratchet over audit.js's Zotero de-link client (2026-08-30).
//
// `POST /api/zotlink/trash` became an SSE stream so its network phase stops
// running under the vault op-lock and stops occupying a Waitress worker for its
// whole budget. The client had NO test at all, so the transport change could
// have left the verb silently doing nothing — the row would still be marked
// [trashed] on a `d` that never arrived — while every Python test stayed green.
//
// Ratcheted at the source rather than driven through the DOM: the handler is
// reached only from a rendered report row inside a confirmInline ceremony, and
// the properties worth pinning are all statically visible.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const AUDIT_JS = readFileSync(join(ROOT, 'static/js/audit.js'), 'utf8');

function trashBlock() {
    const fn = AUDIT_JS.indexOf('function _zotlinkTrash(');
    expect(fn, '_zotlinkTrash not found — update this ratchet').toBeGreaterThan(-1);
    // Stop at the next top-level declaration of EITHER form. Matching only
    // `\nfunction ` ran the slice past the end of the handler and into
    // `async function _noteSyncDiff`, so assertions about "this handler" were
    // silently reading its neighbour's body too.
    const rest = AUDIT_JS.slice(fn + 1);
    const ends = [rest.indexOf('\nfunction '), rest.indexOf('\nasync function ')]
        .filter((i) => i > -1);
    const next = ends.length ? Math.min(...ends) : -1;
    return next > -1 ? rest.slice(0, next) : rest;
}

describe('zotlink de-link client', () => {
    it('consumes the stream instead of parsing a JSON body', () => {
        const block = trashBlock();
        expect(block).toContain("consumeSSE('/api/zotlink/trash'");
        // The old JSON reader must be gone: `r.json()` on an event-stream
        // response resolves to nothing useful and the outcome check below it
        // would then pass on an empty object.
        expect(block).not.toMatch(/await\s+r\.json\(\)/);
        expect(block).not.toContain("secureFetch('/api/zotlink/trash'");
    });

    it('reads the outcome from the terminal frame, not from any frame', () => {
        const block = trashBlock();
        expect(block).toMatch(/frame\.zotlink/);
    });

    it('never marks a row trashed without a terminal frame', () => {
        const block = trashBlock();
        // A missing terminal frame must be an error path, not a silent success:
        // the row's "[trashed]" label is the only record the user sees.
        expect(block).toMatch(/if \(!d\)/);
        const marked = block.indexOf("'[trashed]'");
        const guard = block.indexOf('if (!d)');
        expect(guard).toBeGreaterThan(-1);
        expect(marked).toBeGreaterThan(guard);
    });

    it('surfaces an in-stream error rather than swallowing it', () => {
        const block = trashBlock();
        expect(block).toMatch(/onError:\s*\(\w+\)\s*=>/);
        expect(block).toContain('_showError(streamErr)');
    });

    it('re-enables the button on every failure path', () => {
        const block = trashBlock();
        // Three refusal branches (stream error, no terminal frame, per-key
        // failure) plus the catch. A disabled button with no outcome is a dead
        // row the user cannot retry.
        const reEnables = block.split('\n')
            .filter(l => l.includes('btn.disabled = false')).length;
        expect(reEnables).toBeGreaterThanOrEqual(4);
    });

    it('still routes through the shared confirm ceremony', () => {
        const block = trashBlock();
        expect(block).toContain('confirmInline(btn');
        expect(block).toContain('confirm: true');
    });

    it('sends accept_annotation_loss only for a row that has annotations', () => {
        const block = trashBlock();
        expect(block).toMatch(/ann \?[^:]*accept_annotation_loss: true/);
        const unconditional = block.split('\n')
            .filter(l => l.includes('accept_annotation_loss: true') && !l.includes('ann ?'));
        expect(unconditional,
               'a blanket true would disarm the server gate for every row')
            .toEqual([]);
    });
});

// ── The bulk de-link (2026-09-04) ─────────────────────────────────────────
//
// `POST /api/zotlink/trash` has accepted `item_keys: [...]` up to 200 since it
// shipped, with SSE progress, the annotation acknowledgement and one batch_id
// for the undo — but the report only offered a per-row verb, so clearing a
// folder's worth of attachments meant N confirms. This is the client half.
//
// The properties pinned here are the ones whose absence would be SILENT: a
// blanket acknowledgement disarms the server's second gate for every batch, a
// filtered "select all" over a destructive verb picks up rows the user just
// narrowed away, and a batch that reports success per-run rather than per-key
// tells the user a partial failure was a clean sweep.
function bulkBlock() {
    const fn = AUDIT_JS.indexOf('async function _zotlinkBulk(');
    expect(fn, '_zotlinkBulk not found — update this ratchet').toBeGreaterThan(-1);
    const rest = AUDIT_JS.slice(fn + 1);
    const ends = [rest.indexOf('\nfunction '), rest.indexOf('\nasync function ')]
        .filter((i) => i > -1);
    const next = ends.length ? Math.min(...ends) : -1;
    return next > -1 ? rest.slice(0, next) : rest;
}

describe('zotlink bulk de-link', () => {
    it('streams the batch rather than issuing one request per key', () => {
        const block = bulkBlock();
        expect(block).toContain("consumeSSE('/api/zotlink/trash'");
        expect(block).toContain('item_keys: keys');
        // A per-key loop around the request would defeat the point and would
        // take the server's single-op gate N times.
        expect(block).not.toMatch(/for\s*\([^)]*\)\s*\{[^}]*consumeSSE/);
    });

    it('never sends a blanket accept_annotation_loss', () => {
        const block = bulkBlock();
        expect(block).toContain('accept_annotation_loss');
        const unconditional = block.split('\n').filter(
            (l) => l.includes('accept_annotation_loss: true')
                && !l.includes('acceptAnnotationLoss'));
        expect(unconditional,
               'the second gate must stay armed unless the selection has annotations')
            .toEqual([]);
    });

    it('derives the acknowledgement and the confirm from the SELECTION', () => {
        // The figure the confirm promises has to be the figure the server
        // computes for the same keys, so it is summed over selected rows —
        // not over the table, and not over a per-row attribute of one row.
        const total = AUDIT_JS.slice(AUDIT_JS.indexOf('function _zotlinkAnnotationTotal('));
        expect(total).toContain('_zotlinkSelected.has(');
        const wiring = AUDIT_JS.slice(AUDIT_JS.indexOf("getElementById('zotlink-apply-btn')"));
        expect(wiring).toContain('_zotlinkAnnotationTotal()');
        expect(wiring).toMatch(/annotations\s*>\s*0/);
    });

    it('reports per-key truth and keeps failures selected for a retry', () => {
        const block = bulkBlock();
        expect(block).toContain('terminal.results');
        // A failed key must go back into the selection, or the retry would
        // have to be rebuilt by hand from a status line.
        expect(block).toMatch(/failed\.push\(key\)/);
        expect(block).toMatch(/_zotlinkSelected\.add\(key\)/);
        // And success must be read per key, never inferred from the run.
        expect(block).toMatch(/results\[key\]/);
    });

    it('never marks anything trashed without a terminal frame', () => {
        const block = bulkBlock();
        const guard = block.indexOf('if (!terminal)');
        const firstMark = block.indexOf('_zotlinkMarkRow');
        expect(guard).toBeGreaterThan(-1);
        expect(firstMark).toBeGreaterThan(guard);
    });

    it('unfreezes the surface on every exit path', () => {
        const block = bulkBlock();
        // In a `finally`, or an error leaves the whole report inert.
        expect(block).toMatch(/finally \{[\s\S]*_zotlinkSetAllDisabled\(false\)/);
    });

    it('routes through the shared confirm ceremony', () => {
        const wiring = AUDIT_JS.slice(AUDIT_JS.indexOf("getElementById('zotlink-apply-btn')"));
        expect(wiring.slice(0, 2000)).toContain('confirmInline(');
    });

    it('respects the table filter when selecting all', () => {
        // `installTableControls` HIDES a non-matching row rather than removing
        // it, so a filtered "select all" would otherwise sweep in every row the
        // user had just narrowed away — on a destructive verb.
        const block = AUDIT_JS.slice(AUDIT_JS.indexOf('function _zotlinkSelectAll('));
        expect(block.slice(0, 900)).toMatch(/row\.hidden/);
    });

    it('mirrors the server cap and refuses before spending a confirm', () => {
        expect(AUDIT_JS).toMatch(/_ZOTLINK_MAX_KEYS\s*=\s*200/);
        const upd = AUDIT_JS.slice(AUDIT_JS.indexOf('function _zotlinkUpdateButton('));
        expect(upd.slice(0, 900)).toMatch(/>\s*_ZOTLINK_MAX_KEYS/);
    });

    it('offers the batch undo only when something was actually trashed', () => {
        const block = bulkBlock();
        expect(block).toMatch(/terminal\.batch_id && ok/);
        expect(block).toContain("consumeSSE('/api/tagging/restore'");
        expect(block).toContain('batch_id: terminal.batch_id');
    });

    it('resets the selection when a new report payload arrives', () => {
        // The rows are one scan's snapshot; restoring a selection across a
        // rescan would re-offer keys that may no longer exist.
        const render = AUDIT_JS.slice(AUDIT_JS.indexOf("if (name === 'zotero_attachments')"));
        expect(render.slice(0, 400)).toContain('zotlinkResetSelection()');
    });
});

// The broken-link repair (2026-09-07).
//
// A linked attachment is only a path, so anything that renames or moves the
// file orphans it with no sign anywhere — Zotero's own rename preference did
// that to five vault files and the Attanger plugin to four more, all nine
// repaired by hand. The report now says which links are broken and offers the
// repair; these pin the parts that would be silently wrong.
describe('zotlink re-point', () => {
    function block(name) {
        const fn = AUDIT_JS.indexOf(`function ${name}(`);
        expect(fn, `${name} not found — update this ratchet`).toBeGreaterThan(-1);
        const rest = AUDIT_JS.slice(fn + 1);
        const ends = [rest.indexOf('\nfunction '), rest.indexOf('\nasync function ')]
            .filter((i) => i > -1);
        const next = ends.length ? Math.min(...ends) : -1;
        return next > -1 ? rest.slice(0, next) : rest;
    }

    it('offers the verb only on a row whose file is REALLY missing', () => {
        // `exists` is null for an imported attachment (Zotero owns those bytes
        // and their location is not the user's business) and boolean only for
        // a linked one. A truthiness test would offer the repair on every
        // imported row and on every row of a pre-2026-09-07 payload.
        const render = AUDIT_JS.slice(AUDIT_JS.indexOf("name === 'zotero_attachments'"));
        expect(render).toContain('const broken = r.exists === false;');
        expect(render).toMatch(/broken \? `<button[^`]*data-action="zl-repoint"/);
    });

    it('states the breakage in text, not by colour alone', () => {
        const render = AUDIT_JS.slice(AUDIT_JS.indexOf("name === 'zotero_attachments'"));
        expect(render).toContain('file missing');
    });

    it('the picker is a row attachment so a sort cannot strand it', () => {
        // installTableControls moves and hides `data-row-attachment` WITH its
        // row; without the marker a sort floats the picker to the top, beside
        // an unrelated attachment, on a surface whose job is aiming a write.
        expect(block('_zlRepointPicker'))
            .toContain("tr.setAttribute('data-row-attachment', '')");
    });

    it('never builds the candidate list with innerHTML', () => {
        // Every candidate is a vault filename. `list.innerHTML = ''` clears;
        // the rows themselves are createElement + textContent.
        const b = block('_zlRepointPicker');
        expect(b).toContain('b.textContent = c.rel;');
        // The only assignment may be the empty-string clear. Written as a
        // whole-match list rather than a negative lookahead: `\s*` backtracks
        // to zero width, so /innerHTML\s*=\s*(?!'')/ matches `innerHTML = ''`
        // and the ratchet would have passed on any markup at all.
        const writes = b.match(/innerHTML\s*=\s*[^;]*/g) || [];
        expect(writes).toEqual(["innerHTML = ''"]);
    });

    it('is confirm-gated and sends confirm: true with the chosen rel', () => {
        const b = block('_zlRepoint');
        expect(b).toContain('confirmInline(btn, {');
        expect(b).toContain('item_key: key, rel, confirm: true');
    });

    it('treats an ok:false body as a failure, not a success', () => {
        // The route answers 200 for a skipped/early outcome, so checking only
        // `r.ok` would announce a re-point that never happened.
        expect(block('_zlRepoint')).toContain('!r.ok || d.ok === false');
    });
});
