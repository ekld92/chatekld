"""Checkpoint + heartbeat state shared by every journalled batch loop.

Hoisted out of ``api/routes/tagging.py`` 2026-08-30 so the Zotero de-link
route can reuse it instead of growing a third copy — the divergence class the
original hoist (retire/harmonize, 2026-08-18) was itself fixing. ``tagging.py``
re-exports the name, so existing ``api.routes.tagging._BatchJournalKeeper``
patch targets keep resolving.
"""

from __future__ import annotations

from tagging import journal


class _BatchJournalKeeper:
    """Checkpoint + heartbeat state shared by the retire/harmonize batch loops.

    Hoisted 2026-08-18: the two flows carried verbatim-duplicate ``_bump_save``
    / ``_tick`` closures — the divergence class the batch_id/defer_save dedup
    was itself fixing. ``bump_save`` checkpoints the manifest every
    ``_FLUSH_EVERY`` op records (the writers run ``defer_save=True``; per-op
    note snapshots stay durable regardless, so a crash costs at most one
    checkpoint window of op records while their snapshots + the
    ``log_vault_write`` trail survive). After a lost lock it routes through
    ``journal.save_merged`` so a new lock owner's ops are never clobbered; a
    TRANSIENT read failure there keeps the pending count, so the next
    checkpoint retries instead of overwriting a possibly-intact disk copy.
    ``tick`` emits ``[done/total]`` progress and ACTS on a False heartbeat
    (CLAUDE.md Limits discipline — never discard it) by setting ``lock_lost``,
    which the loops check to stop issuing new writes.
    """

    _FLUSH_EVERY = 25

    def __init__(self, *, vault_root, cfg, manifest, op_epoch, put, total,
                 manager):
        self.vault_root, self.cfg, self.manifest = vault_root, cfg, manifest
        self.op_epoch, self.put, self.total = op_epoch, put, total
        self.manager = manager
        self.lock_lost = False
        self.done = 0
        self.pending_saves = 0

    def bump_save(self, force=False):
        if not force:
            self.pending_saves += 1
        if self.pending_saves and (force or self.pending_saves >= self._FLUSH_EVERY):
            try:
                if self.lock_lost:
                    journal.save_merged(self.vault_root, self.cfg, self.manifest)
                else:
                    journal.save(self.vault_root, self.cfg, self.manifest)
            except journal.JournalTransientReadError:
                # save_merged refused: the disk copy could not be READ (e.g.
                # an unmounted archive volume) and may be intact. Keep the
                # pending count so the next checkpoint retries the merge.
                return
            self.pending_saves = 0

    def tick(self, label):
        self.done += 1
        if not self.manager.heartbeat(self.op_epoch):
            self.lock_lost = True
        self.put({"info": f"[{self.done}/{self.total}] {label}"})
