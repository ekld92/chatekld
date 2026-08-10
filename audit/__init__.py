"""Library Audit subsystem.

Vendored from the kb_harmonizer project. Audits an Obsidian vault against
Zotero (SQLite + Better BibTeX) and local PDFs, producing per-store reports
that surface tag drift, unread PDFs, duplicate PDFs, and bib/PDF gaps.

Strictly read-only against external stores; the writable state is limited to
sidecars under ``BASE_DIR/audit/`` — ``mapping.json`` (manual PDF<->bib
overrides), ``dup_decisions.json`` (dismissed near-duplicate pairs) and the
regenerable ``hash_cache.json``.

The scan is fully manual — call :class:`audit.manager.AuditManager.start_scan`
to trigger a run. Nothing in this package is invoked at app boot.
"""
