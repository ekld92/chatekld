"""Biblio tagging subsystem (app-side, core/route split like ``refactor``/``deckgen``).

Turns the read-only Library Audit ``Record`` join into a *write* surface for
tags, scoped strictly to ``biblio_articles/``. Three eventual write targets —
the PDF's macOS Finder tags, the Obsidian Zotero note's YAML frontmatter, and
the parent Zotero item's tags (via Zotero's local HTTP API, never SQLite) —
each confirm-gated, journalled, and audit-logged when they land.

This package's core is app-independent where possible: ``vocab`` is a pure
``_tags.md`` → ``{tag: gloss}`` parser (stdlib + ``rag.thesaurus`` only). The
HTTP surface lives in ``api/routes/tagging.py``; the writers (Finder xattr,
Zotero local API) live in the route layer, keeping the vendored ``audit/`` tree
read-only.
"""
