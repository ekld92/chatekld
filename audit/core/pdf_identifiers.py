"""Extract a paper's own identifier (DOI / arXiv id) from a PDF.

Two independent sources, cheapest and most reliable first:

* **embedded metadata** — the PDF ``/Info`` dictionary and its XMP packet.
  Publisher PDFs routinely carry ``prism:doi`` / ``dc:identifier``; measured
  on the maintainer's library this alone yields a DOI for **30.7%** of the
  PDFs the bridge could not otherwise place.
* **printed text** — a DOI or arXiv id printed on page 1, which adds a
  further **23.6%**.

.. rubric:: Why printed text is the FALLBACK and not the primary

A DOI found in a PDF's text is frequently *not the paper's own*: it may
belong to a cited work, or to the journal. Zotero's own recognizer orders
its ladder the same way for the same stated reason, resolving a title to a
DOI before it will trust a regexed one. :func:`extract_printed_identifier`
mitigates what remains by taking the match **nearest the top of the page**
(references come later), but the ordering in :func:`pdf_doi` is the real
guard: embedded metadata is asserted by the publisher about *this* file.

.. rubric:: Ownership

``_DOI_RE`` / ``_ARXIV_RE`` / :func:`extract_printed_identifier` were moved
here verbatim from ``audit/engine/near_duplicates.py`` (2026-08-19), which
now imports them, so the two consumers cannot drift apart. The move was
required by the import graph as much as by tidiness: ``near_duplicates``
imports *from* ``bridge``, so the bridge's new DOI resolution rung could not
reach back into it without a cycle. ``core/`` has no engine imports and is
reachable from both.

Stdlib + pikepdf only — pikepdf is already this package's PDF dependency
(``core/pdf_annotations.py``), so the vendored tree gains nothing new.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import bib as core_bib

logger = logging.getLogger(__name__)

# Moved verbatim from near_duplicates (see module docstring) — a change here
# changes BOTH the near-duplicate identifier tier and the bridge's DOI rung.
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>]+", re.IGNORECASE)
_ARXIV_RE = re.compile(r"arxiv[:\s/]*(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)

# XMP/Info values are short; a pathological packet must not be scanned whole.
_META_SCAN_MAX_CHARS = 20_000


def extract_printed_identifier(text: str) -> str | None:
    """First DOI or arXiv id printed on the page, canonicalised, else None."""
    if not text:
        return None
    doi = _DOI_RE.search(text)
    arxiv = _ARXIV_RE.search(text)
    # Nearest the top of the page wins (cited papers' DOIs come later).
    best = None
    if doi and arxiv:
        best = doi if doi.start() < arxiv.start() else arxiv
    else:
        best = doi or arxiv
    if best is None:
        return None
    if best is doi:
        return "doi:" + doi.group(0).lower().rstrip(".,;)]}")
    return "arxiv:" + arxiv.group(1)


def read_pdf_meta(pdf_path: Path) -> dict:
    """One pikepdf open → ``{producer, creator, pages, info_blob}``.

    Exists so the provenance classifier (Batch C) never adds a second open of
    the same file: ``extract_embedded_doi`` is refactored onto it, and the
    bridge's provenance pass reuses the identical shape. Every failure mode
    degrades to empty values — a class is advisory evidence, so refusing to
    classify is always preferable to raising inside a scan.

    ``info_blob`` is the joined ``/Info`` + XMP text, which is what both the
    DOI regex and the browser-print URL recovery scan.
    """
    out: dict[str, Any] = {
        "producer": "", "creator": "", "pages": 0, "info_blob": ""}
    try:
        import pikepdf
    except ImportError:  # pragma: no cover - pikepdf is a hard dependency
        return out

    blob_parts: list[str] = []
    try:
        with pikepdf.open(pdf_path) as pdf:
            try:
                out["pages"] = len(pdf.pages)
            except Exception:  # noqa: BLE001 - malformed page tree
                pass
            try:
                # pikepdf's Object mapping is not dict-typed; read defensively
                # and let the broad except cover a malformed /Info, which is
                # common enough that it must never abort the read.
                docinfo = pdf.docinfo
                if docinfo is not None:
                    for raw_key, raw_val in docinfo.items():
                        text = str(raw_val)
                        blob_parts.append(text)
                        if str(raw_key) == "/Producer":
                            out["producer"] = text
                        elif str(raw_key) == "/Creator":
                            out["creator"] = text
            except Exception:  # noqa: BLE001 - malformed /Info is common
                pass
            try:
                # pikepdf raises on an unparseable packet and logs its own
                # warning; either way the printed-text path still applies.
                blob_parts.append(str(pdf.open_metadata()))
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001 - unreadable/encrypted/corrupt
        return out
    out["info_blob"] = " ".join(blob_parts)[:_META_SCAN_MAX_CHARS]
    return out


def extract_embedded_doi(pdf_path: Path) -> str | None:
    """Return a normalised DOI from the PDF's ``/Info`` dict or XMP, else None.

    Opens the file read-only and never writes back. Every failure mode — a
    corrupt file, an unparseable XMP packet, a permission error — degrades to
    ``None``: an identifier is advisory evidence, so refusing to place a PDF
    is always preferable to raising inside a scan.
    """
    blob = read_pdf_meta(pdf_path)["info_blob"]
    if not blob:
        return None
    m = _DOI_RE.search(blob)
    if not m:
        return None
    # normalise_doi is core/bib.py's — the SAME canonicalisation applied to
    # BibEntry.doi, so the two sides of the join are directly comparable.
    return core_bib.normalise_doi(m.group(0).rstrip(".,;)]}"))


def pdf_doi(
    pdf_path: Path,
    text_provider: Callable[[Path], str | None] | None = None,
) -> str | None:
    """A PDF's own DOI, normalised to match ``BibEntry.doi``, or ``None``.

    Embedded metadata first, then — only when that finds nothing and a
    ``text_provider`` was supplied — the identifier printed on page 1. An
    arXiv id is deliberately NOT returned: this function exists to key a join
    against ``_master.bib``, whose ``doi`` field never holds one.

    ``text_provider`` is injected (the ``pdf_text_provider`` seam) so this
    module, and the vendored tree around it, never import the app's PDF text
    cache. Passing ``None`` degrades to embedded metadata only, which is the
    CLI and test path.
    """
    embedded = extract_embedded_doi(pdf_path)
    if embedded:
        return embedded
    if text_provider is None:
        return None
    try:
        text = text_provider(pdf_path)
    except Exception:  # noqa: BLE001 - a text failure must not abort a scan
        return None
    ident = extract_printed_identifier(text or "")
    if not ident or not ident.startswith("doi:"):
        return None
    return core_bib.normalise_doi(ident[len("doi:"):])
