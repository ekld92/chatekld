// 2026-07-20 audit A5: the Restore UI mislabeled a create_note op (a docgen
// written note) as "Applied note" with a plain "Revert" button — but its
// revert DELETES the file (no prior snapshot). The row must name the kind and
// warn that Revert deletes, so a user doesn't expect a content rollback.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

function mountRestoreDom() {
    document.body.innerHTML = `
      <div id="refactor-restore-modal">
        <div id="refactor-restore-status"></div>
        <div id="refactor-restore-list"></div>
        <button id="refactor-restore-all"></button>
      </div>
    `;
}

let openRestore;
beforeEach(async () => {
    mountRestoreDom();
    // openModal toggles a class; stub matchMedia-free DOM is enough.
    ({ openRestore } = await import('../../static/js/refactor.js'));
});
afterEach(() => { vi.restoreAllMocks(); document.body.innerHTML = ''; });

function mockManifest(ops) {
    globalThis.fetch = vi.fn(async () => ({
        ok: true, status: 200, json: async () => ({ ops }),
    }));
}

describe('refactor Restore op labels', () => {
    it('labels a create_note op as "Created note" with a delete warning', async () => {
        mockManifest([{ id: '1', kind: 'create_note', note_rel: 'notes/new.md',
                        ts: 't', state: 'applied' }]);
        await openRestore();
        const list = document.getElementById('refactor-restore-list');
        expect(list.textContent).toContain('Created note');
        expect(list.textContent).toContain('Reverting deletes this created note.');
        const btn = list.querySelector('button');
        expect(btn.textContent).toBe('Delete note');
    });

    it('keeps the snapshot-restoring kinds as content reverts', async () => {
        mockManifest([
            { id: '2', kind: 'write_note', note_rel: 'a.md', ts: 't', state: 'applied' },
            { id: '3', kind: 'normalize_note', note_rel: 'b.md', ts: 't', state: 'applied' },
            { id: '4', kind: 'archive_image', image_rel: 'img.png', ts: 't', state: 'applied' },
        ]);
        await openRestore();
        const list = document.getElementById('refactor-restore-list');
        expect(list.textContent).toContain('Applied note');
        expect(list.textContent).toContain('Formatting fix');
        expect(list.textContent).toContain('Archived image');
        expect(list.textContent).not.toContain('Reverting deletes');
        // None of these carry the destructive "Delete note" button.
        const btns = [...list.querySelectorAll('button')].map((b) => b.textContent);
        expect(btns.every((t) => t === 'Revert')).toBe(true);
    });
});
