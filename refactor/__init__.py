"""Note Refactor — analyzer (read-only) + Phase 2 vault writers for a sub-folder.

The analyzer is **read-only with respect to the vault**: ``plan.build_plan``
resolves image embeds, reuses already-extracted descriptions from
``obsidian_cache/`` (zero vision calls), applies pure-text hygiene checks, and
produces per-note proposed diffs plus an advisory cross-note dose discrepancy
report. The only code path that calls the vision model is
``extract.extract_table`` / ``extract.redescribe`` / ``extract.classify`` —
invoked one image at a time from ``/api/refactor/extract-image`` — and it writes
only ``obsidian_cache/*.txt``.

**Phase 2** adds the app's first vault writers, all opt-in and per-action
confirmed: ``apply`` (callout-only batch note writer), ``archive`` (per-image
move-out + thumbnail), and ``journal`` (the restore manifest + scope-lock +
``log_vault_write`` audit core). Every vault write is atomic, scope-locked,
audit-logged, and reversible. Design + phasing: ``docs/project_note_refactor.md``.
Imports flow refactor → {rag.vault, services.vision, core.utils}; the package
never reaches into the indexer's internals beyond the documented reuse points.
"""
