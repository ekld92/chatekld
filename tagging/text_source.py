"""PDF text source for tag proposal — cache-first, page-bounded fresh fallback.

Reuses the indexer's PDF-text chokepoint (``obsidian_manager._read_pdf_text``):
if a biblio PDF was already indexed, its extracted text is served straight from
the ``obsidian_cache/pdf_cache/<vault_key>/<sha256>.txt`` cache (free, and for
scanned PDFs it is the OCR text); otherwise a genuinely bounded fresh
extraction runs: at most ``fresh_max_pages`` pages and no whole-file
MarkItDown/pdfminer fallback (``use_markitdown=False``) — the historical
``time_budget_s=20`` here was a no-op (equal to the start-refusal floor, and
never a bound on a started extraction), which let one scanned PDF cost a
multi-minute synchronous pdfminer pass per card.

**Negative cache**: a fresh extract that finds no usable text writes a
``<sha256>.notext`` marker beside the real cache, so the next snippet/propose
call fast-fails in milliseconds instead of re-scanning. The marker is
consulted only when NO real cache exists — a later indexing run that OCRs the
document writes ``<sha256>.txt`` (or ``<sha256>-pSSSSS-EEEEE.txt`` per-range
files for >1000-page documents; the sha does not change) and silently wins.

The result is capped at ``max_chars`` before it ever reaches the LLM. Writes
nothing into the vault — only the regenerable marker under ``obsidian_cache/``.
"""

from __future__ import annotations

from core.extraction_status import TextRead, status_of

import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Minimum stripped length below which an extract counts as "no usable text"
# (the propose route imports this — one threshold, not two drifting copies).
# Why 40: empirical floor from the 2026-07-19 batch — shorter than any real
# abstract's first sentence, but comfortably above the stray artifacts a
# text-layer-less scan still yields (a page number, a watermark word, an
# OCR'd header fragment), so a scanned PDF is recognised as no-text instead
# of feeding the LLM ten junk characters as "the paper".
_MIN_USEFUL_CHARS = 40

# Serialises FRESH extractions only (cache hits never touch it). The snippet
# route runs on the request thread with no single-flight gate, so rapid deck
# navigation could stack N concurrent PyMuPDF extractions of the same PDF and
# race the .notext write; under the lock the second caller re-probes and finds
# the first's cache/marker instead of re-extracting. One process-wide lock (not
# per-file) is deliberate: extractions are page-bounded and the workload is one
# user triaging serially — simpler beats a lock table here.
_FRESH_EXTRACT_LOCK = threading.Lock()


def _cache_probe(manager, pdf_path: Path, vault_root: Path, signature=None):
    """Resolve ``(marker_path, has_real_cache, signature)``, or (None, False, None).

    ``has_real_cache`` is True when a whole-file, legacy or per-range cache
    file exists for the document's current digest — the marker must never
    shadow real (possibly OCR'd) text. Any failure resolves to
    ``(None, False, None)``: the caller just skips the negative-cache fast
    path. The computed ``signature`` rides back out so the read below reuses
    it instead of hashing the file a second time (perf plan Phase 3.5); the
    indexer re-validates it against a fresh stat before trusting it.
    """
    try:
        try:
            rel = pdf_path.relative_to(vault_root).as_posix()
        except ValueError:
            rel = None
        # The re-probe under _FRESH_EXTRACT_LOCK asks a different question
        # (did the thread ahead of us write a cache?) about the SAME bytes, so
        # it reuses the first probe's fingerprint — re-validated against a
        # fresh stat by the manager, and recomputed if the file moved under us.
        sig = manager._reuse_pdf_signature(pdf_path, signature) if signature else None
        if sig is None:
            sig = manager._pdf_file_signature(
                pdf_path, manager._persisted_pdf_signatures(), rel)
        cache_file = manager._pdf_cache_file(vault_root, sig)
        legacy_file = manager._legacy_pdf_cache_file(vault_root, sig)
        digest = str(sig["sha256"])
        marker = cache_file.with_name(f"{digest}.notext")
        has_real = cache_file.exists() or legacy_file.exists()
        if not has_real:
            has_real = any(cache_file.parent.glob(f"{digest}-p*.txt"))
        return marker, has_real, sig
    except Exception:  # noqa: BLE001 — probe is best-effort
        return None, False, None


def read_pdf_text(pdf_path: Path, vault_root: Path, max_chars: int,
                  *, fresh_max_pages: int = 8) -> tuple[str, bool]:
    """Return ``(text, truncated)`` for *pdf_path*, capped at *max_chars*.

    ``truncated`` is True when the extract did not cover the whole document
    (a fresh extract hit the page/char budget, or cached page-ranges are
    incomplete). Delegates to the indexer's ``_read_pdf_text`` so the cache and
    the persisted PDF-signature map are shared with indexing; a fresh extract
    is bounded to the document's first *fresh_max_pages* pages with the
    whole-file MarkItDown fallback disabled. Returns ``("", False)`` on any
    read failure rather than raising — the caller treats an empty string as
    "no usable text" (scanned/unreadable).
    """
    from rag.vault import obsidian_manager

    marker, has_real_cache, sig = _cache_probe(
        obsidian_manager, Path(pdf_path), Path(vault_root))
    if (marker is not None and not has_real_cache and marker.exists()
            and _marker_covers(marker, fresh_max_pages)):
        # Known no-text-layer document (and still no real cache): fast-fail.
        return "", False

    if has_real_cache or marker is None:
        # Cache hit (or an unprobeable path — best-effort, as before): no
        # extraction will run, so no serialisation is needed.
        return _read_capped(obsidian_manager, pdf_path, vault_root, max_chars,
                            fresh_max_pages, marker, has_real_cache, sig)

    # Fresh-extract path: serialise, then RE-probe — while this thread waited,
    # the extraction it queued behind may have written the cache or the
    # .notext marker, making this call a cheap read instead of a second scan.
    with _FRESH_EXTRACT_LOCK:
        marker, has_real_cache, sig = _cache_probe(
            obsidian_manager, Path(pdf_path), Path(vault_root), signature=sig)
        if (marker is not None and not has_real_cache and marker.exists()
                and _marker_covers(marker, fresh_max_pages)):
            return "", False
        return _read_capped(obsidian_manager, pdf_path, vault_root, max_chars,
                            fresh_max_pages, marker, has_real_cache, sig)


def _marker_covers(marker: Path, fresh_max_pages: int) -> bool:
    """True when the ``.notext`` marker's scan was at least *fresh_max_pages* deep.

    The marker records ``pages_scanned`` for exactly this comparison
    (2026-07-20 audit: it was written but never read, so a 3-page snippet
    probe on a PDF whose text layer starts at page 4 permanently suppressed
    the 8-page propose extraction). A deeper request than the recorded scan
    ignores the marker and re-extracts; a still-empty result rewrites it with
    the deeper count. Unreadable/legacy markers (no field) are treated as
    depth 0 — one bounded re-scan, after which the rewrite self-heals them.
    Pre-status markers are also rechecked once: they may have been written
    after an OCR failure, before that failure was distinguished from no text.
    """
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        scanned = int(data.get("pages_scanned", 0)) if data.get("schema") == 2 else 0
    except Exception:  # noqa: BLE001
        scanned = 0
    return scanned >= int(fresh_max_pages)


def _read_capped(manager, pdf_path, vault_root, max_chars, fresh_max_pages,
                 marker, has_real_cache, signature=None) -> tuple[str, bool]:
    """The read + negative-cache + cap tail of :func:`read_pdf_text`."""
    try:
        result = manager._read_pdf_text(
            Path(pdf_path), Path(vault_root),
            char_budget=int(max_chars),
            fresh_max_pages=int(fresh_max_pages),
            use_markitdown=False,
            signature=signature,
        )
    except Exception:
        return "", False
    text, truncated = result
    if not isinstance(text, str):
        return "", False
    text = text.strip()

    extraction_status = status_of(result)
    known_extraction_failure = bool(
        extraction_status.failed_pages or extraction_status.truncated_pages
    )
    if (marker is not None and not has_real_cache
            and len(text) < _MIN_USEFUL_CHARS and not known_extraction_failure):
        # A fresh page-bounded extract just ran (a non-empty cache would have
        # short-circuited it) and found nothing usable: remember that so the
        # next call skips the scan entirely. Regenerable cache — the atomic
        # no-fsync writer is deliberate (same trade-off as the PDF text cache).
        try:
            manager._atomic_write_text(
                marker,
                json.dumps({"schema": 2, "ts": time.time(),
                            "pages_scanned": int(fresh_max_pages)}),
            )
        except Exception:  # noqa: BLE001 — the marker is an optimisation only
            logger.debug("could not write .notext marker for %s", pdf_path, exc_info=True)

    if len(text) > max_chars:
        return TextRead(text[:max_chars], True, extraction_status)
    return TextRead(text, bool(truncated), extraction_status)
