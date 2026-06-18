"""Library Audit subsystem.

Vendored from the kb_harmonizer project. Audits an Obsidian vault against
Zotero (SQLite + Better BibTeX) and local PDFs, producing six reports
that surface tag drift, unread PDFs, duplicate PDFs, and bib/PDF gaps.

Strictly read-only against external stores; the only writable state is
``mapping.json`` under ``BASE_DIR/audit/`` (manual PDF<->bib overrides).

The scan is fully manual — call :class:`audit.manager.AuditManager.start_scan`
to trigger a run. Nothing in this package is invoked at app boot.
"""
