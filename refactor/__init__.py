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

Advisory analyzer helpers layered on top of the read-only plan: ``text``
(``strip_ocr_preamble`` — the per-image "strip the description preamble" opt-in),
``flags`` (sticky per-image flag sidecar driving that strip + the handwritten
"keep anyway" override), ``hints.likely_handwritten`` (zero-vision handwritten
auto-hide), ``hygiene.structure_notes`` (deterministic "you didn't skip a line"
checks), and ``review`` (the **opt-in, per-note LLM prose review** — the only
chat-LLM caller, never run by the read-only plan).

Imports flow refactor → {rag.vault, services.vision, core.utils, core.llm}; the
package never reaches into the indexer's or the adapters' internals beyond the
documented reuse points.
"""
