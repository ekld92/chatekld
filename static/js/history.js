import { secureFetch, safeJson, logError } from './api.js';
import { openModal, confirmInline } from './ui.js';
const $ = id => document.getElementById(id);
const _clear = el => el.replaceChildren();

export async function openRestore() {
    _clear($('history-restore-list'));
    $('history-restore-status').textContent = 'Loading…';
    openModal('history-restore-modal');
    await _loadManifest();
}

async function _loadManifest(outcome = '') {
    const list = $('history-restore-list');
    _clear(list);
    const status = $('history-restore-status');
    try {
        const resp = await secureFetch('/api/history/manifest');
        const d = await safeJson(resp);
        if (!resp.ok || d.error) {
            status.textContent = (outcome ? outcome + ' ' : '') + 'Error loading journal: ' + (d.error || `HTTP ${resp.status}`);
            return;
        }
        const ops = (d.ops || []).slice().reverse();   // newest first
        status.textContent = outcome || (ops.length ? '' : 'No vault changes recorded yet.');
        for (const op of ops) list.appendChild(_renderOp(op));
    } catch (e) {
        logError('History manifest load failed', e);
        status.textContent = (outcome ? outcome + ' ' : '') + 'Failed to load manifest (see console).';
    }
}

// Per-op-kind Restore labels (2026-07-20 audit A5). The journal now holds
// four note-ish kinds plus archive_image; a create_note op (a docgen-written
// note) reverts by DELETING the file — it has no prior snapshot to restore —
// so it must NOT read as the snapshot-restoring "Applied note" with a plain
// Revert button, which invited a "restore prior content" expectation and got
// file deletion instead.
const _OP_LABELS = {
    archive_image: 'Archived image',
    create_note: 'Created note',
    studio_markdown: 'Studio additions',
    normalize_note: 'Formatting fix',
    llm_note: 'LLM edit',
    notesync_update: 'Zotero region update',
    rename_note: 'Renamed note (citation key changed)',
    rename_attachment: 'Renamed PDF',
    apply_note: 'Applied note',
    write_note: 'Applied note',
};

function _renderOp(op) {
    const row = document.createElement('div');
    row.className = 'history-restore-op';
    const main = document.createElement('div');
    const strong = document.createElement('strong');
    strong.textContent = _OP_LABELS[op.kind] || 'Applied note';
    main.appendChild(strong);
    const path = document.createElement('span');
    path.className = 'muted';
    path.textContent = ' ' + (op.kind === 'rename_note' && op.old_rel ? op.old_rel + ' → ' : '') + (op.note_rel || '')
        + (op.image_rel ? ' · ' + op.image_rel : '');
    main.appendChild(path);
    row.appendChild(main);
    const meta = document.createElement('div');
    meta.className = 'muted';
    meta.textContent = (op.ts || '') + ' · ' + (op.state || '');
    row.appendChild(meta);
    // A create_note revert is a DELETE, so say so plainly on the row and the
    // button rather than let "Revert" imply a content rollback.
    const isCreate = op.kind === 'create_note';
    if (isCreate) {
        const warn = document.createElement('div');
        warn.className = 'muted';
        warn.textContent = 'Reverting deletes this created note.';
        row.appendChild(warn);
    }
    const btn = document.createElement('button');
    btn.className = 'btn btn-outline btn-sm';
    if (op.state === 'reverted') { btn.textContent = 'reverted'; btn.disabled = true; }
    else {
        btn.textContent = isCreate ? 'Delete note' : 'Revert';
        // Row context (2026-08-18): N identical "Revert" names in a modal
        // read as indistinguishable in Tab/forms mode.
        btn.setAttribute('aria-label',
            `${isCreate ? 'Delete' : 'Revert'} ${_OP_LABELS[op.kind] || 'applied note'}`
            + `: ${op.note_rel || op.image_rel || op.id}`);
        btn.onclick = () => confirmInline(btn, { message: isCreate ? 'Delete this created note if unchanged?' : 'Restore this recorded change if unchanged?', confirmLabel: isCreate ? 'Delete note' : 'Restore change', onConfirm: () => _revertOp(op.id, btn) });
    }
    row.appendChild(btn);
    return row;
}

async function _revertOp(opId, btn) {
    if (btn) btn.disabled = true;
    const status = $('history-restore-status');
    status.textContent = 'Reverting…';
    try {
        const resp = await secureFetch('/api/history/restore', {
            method: 'POST', body: JSON.stringify({ op_id: opId, confirm: true }),
        });
        const d = await safeJson(resp);
        if (!resp.ok || d.error) {
            status.textContent = 'Error: ' + (d.error || `HTTP ${resp.status}`);
            if (btn) btn.disabled = false;
            return;
        }
        const r = (d.results || [])[0] || {};
        await _loadManifest(`${r.status || 'unknown'}${r.message ? ': ' + r.message : ''}`);
        status.setAttribute('tabindex', '-1');
        if ($('history-restore-modal').classList.contains('open')) status.focus();
    } catch (e) {
        logError('History revert failed', e);
        status.textContent = 'Revert failed (see console).';
        if (btn) btn.disabled = false;
    }
}

/** Revert every non-reverted op (newest first, server-side). */
export function revertAll() {
    // Track 6e: this rewrites every recorded vault change in one click —
    // the single most destructive button in the app had NO ceremony while
    // each individual write it undoes was confirm-gated.
    const btn = $('history-restore-all');
    if (!btn) return _doRevertAll();
    confirmInline(btn, {
        message: 'Revert EVERY recorded vault change (newest first)? '
            + 'Created notes are deleted if unchanged; recorded edits are restored from snapshots, '
            + 'renames and supported links are reversed, and archived files move back into the vault. '
            + 'Git-only note mirror rewrites are not restored here.',
        confirmLabel: 'Revert all changes',
        onConfirm: _doRevertAll,
    });
}

async function _doRevertAll() {
    const status = $('history-restore-status');
    const btn = $('history-restore-all');
    if (btn) btn.disabled = true;
    // Honest about the duration: the server restores one op at a time under
    // the op-lock (heartbeating per op precisely because it is long). Per-op
    // progress needs the endpoint converted to SSE — recorded as a deferral
    // in CLAUDE.md Known Issues (the tagging batch-restore is the precedent).
    status.textContent = 'Reverting all… one restore per recorded change; '
        + 'a long history can take minutes.';
    try {
        const resp = await secureFetch('/api/history/restore', {
            method: 'POST', body: JSON.stringify({ all: true, confirm: true }),
        });
        const d = await safeJson(resp);
        if (!resp.ok || d.error) {
            status.textContent = 'Error: ' + (d.error || `HTTP ${resp.status}`);
            return;
        }
        const details = (d.results || []).filter(r => r.status !== 'reverted')
            .map(r => `${r.status || 'unknown'}${r.message ? ': ' + r.message : ''}`);
        await _loadManifest(`Reverted ${d.reverted || 0} change(s).${details.length ? ' ' + details.join('; ') : ''}`);
        status.setAttribute('tabindex', '-1');
        if ($('history-restore-modal').classList.contains('open')) status.focus();
    } catch (e) {
        logError('History revert-all failed', e);
        status.textContent = 'Revert all failed (see console).';
    } finally {
        if (btn) btn.disabled = false;
    }
}
