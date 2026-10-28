"""Accession — keyless bibliographic resolution for orphan PDFs (Phase 2).

The audit's bridge (Phase 1, 2026-08-19) re-links every orphan whose own DOI
is already in ``_master.bib`` — 74% of the identifiable ones. This package
handles the residue: it turns what a PDF discloses about itself (embedded
metadata, page-1 text, the filename) into ranked queries, resolves them
against **keyless** public sources (Crossref, Europe PMC, arXiv, HAL,
theses.fr), and scores the candidates into the evidence tiers the review UI
gates on. Headless in this phase — no route, no UI, no config keys; the
report extension (Phase 3) is the first user-visible surface.

Module layout (imports flow left → right, never back):

* ``identify.py``   — pure: (embedded DOI, page-1 text, filename stem) →
  ranked :class:`~accession.identify.Query` list. No network, no app imports.
* ``sources.py``    — stdlib-urllib clients for the five keyless sources
  behind one :class:`~accession.sources.Source` protocol, over a hardened
  HTTP core mirroring ``tagging/zotero_write.py``.
* ``candidates.py`` — merge, score, tier, dedupe; the ``resolve_queries``
  orchestrator that checks ``_master.bib`` **before any network call** and
  refuses when two candidates score within a margin.
* ``cache.py``      — ``BASE_DIR/audit/accession_cache.json``, keyed by PDF
  sha256, following the ``hash_cache.json`` / ``doi_cache.json`` discipline.

The package may import the audit's pure helpers (the shared DOI/filename
recipes) but nothing under ``audit/`` may ever import ``accession`` — the
vendored tree depends only on ``audit/config.py`` and promises no egress.
"""
