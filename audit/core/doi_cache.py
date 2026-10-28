"""Persistent DOI memo for the bridge's DOI resolution rung (2026-08-19).

``BASE_DIR/audit/doi_cache.json`` maps ``{abs path: {size, mtime_ns, meta,
text}}`` where either value may be ``null`` — a *negative* result is worth
caching too, since "this PDF carries no DOI" is exactly the answer that cost
a full metadata read plus a page-1 text extraction to establish.

Modelled on ``hash_cache.py`` (same directory, same atomic-write-no-fsync
trade, same self-invalidating ``(size, mtime_ns)`` key, same stale-key prune
at save). It is a REGENERABLE cache: a lost entry costs one re-read, never
data.

.. rubric:: Why it is not optional

The rung examines every PDF no earlier step has claimed, which on the
maintainer's library is ~4 980 files, not the ~968 residual — because the
author+year step runs *after* it, by design, so an exact identifier can
correct a surname-and-year guess. Measured cold that is **+52 s per scan**.
With the memo only new or modified PDFs are read, so the steady-state cost
is a stat per file.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MAX_ENTRIES = 100_000  # backstop far above any real library


def cache_path() -> Path:
    """``BASE_DIR/audit/doi_cache.json`` (same dir as ``mapping.json``)."""
    from ..config import _audit_dir

    return Path(_audit_dir()) / "doi_cache.json"


def load_cache() -> dict[str, Any]:
    """Load the memo; any read/parse failure degrades to an empty dict."""
    try:
        raw = json.loads(cache_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def lookup(
    pdf_path: Path, cache: dict[str, Any], field: str
) -> tuple[bool, Any]:
    """Return ``(hit, value)`` for *pdf_path* in evidence class *field*.

    ``field`` is ``"meta"`` (embedded metadata only), ``"text"`` (metadata
    with a page-1 text fallback) or ``"prov"`` (the provenance memo, whose
    value is a small dict). ``meta``/``text`` are cached independently because
    a cached "no DOI in the metadata" says nothing about the text, and
    collapsing them would either re-read every DOI-less PDF each scan or
    answer the text question with a metadata-only result.

    ``hit`` distinguishes "cached, and the answer is no DOI" from "not
    cached" — the majority of the examined set has no DOI at all, so caching
    the negative is where most of the saving comes from.

    The value is returned **as stored**, matching :func:`store`'s contract
    ("whatever the field's owner puts there"). It used to coerce anything
    non-``str`` to ``None``, which was the half of that widening that never
    landed: a ``prov`` hit came back ``(True, None)``, the caller's
    ``isinstance(cached, dict)`` gate could never pass, and the memo was dead —
    every scan re-opened every residual PDF and re-ran the text provider. DOI
    consumers now coerce at the call site (``bridge._cached_doi``), so a
    hand-edited cache still cannot leak a non-string into the DOI join.
    """
    entry = cache.get(str(pdf_path))
    if not isinstance(entry, dict) or field not in entry:
        return False, None
    try:
        st = pdf_path.stat()
    except OSError:
        return False, None
    if entry.get("size") != st.st_size or entry.get("mtime_ns") != st.st_mtime_ns:
        return False, None
    return True, entry.get(field)


def store(
    pdf_path: Path, cache: dict[str, Any], field: str, value: Any
) -> None:
    """Record *value* (possibly ``None``) for *pdf_path* in class *field*.

    *value* is a DOI string for the ``meta``/``text`` classes and a small
    JSON-serialisable dict for the ``prov`` class (Batch C, 2026-08-26) — the
    memo is keyed on ``(size, mtime_ns)`` and stores whatever the field's
    owner puts there, so widening it costs nothing and keeps ONE
    self-invalidating memo per PDF rather than a second cache file.

    A size/mtime change discards any sibling class recorded for the old
    bytes, so the two fields can never describe different versions of a file.
    Never raises: an unstattable file is simply not memoised.
    """
    try:
        st = pdf_path.stat()
    except OSError:
        return
    entry = cache.get(str(pdf_path))
    if (not isinstance(entry, dict) or entry.get("size") != st.st_size
            or entry.get("mtime_ns") != st.st_mtime_ns):
        entry = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
        cache[str(pdf_path)] = entry
    entry[field] = value


def save_cache(cache: dict[str, Any]) -> None:
    """Persist the memo atomically; failures are logged, never raised."""
    try:
        pruned = {k: v for k, v in cache.items() if os.path.exists(k)}
        if len(pruned) > _MAX_ENTRIES:
            pruned = dict(list(pruned.items())[-_MAX_ENTRIES:])
        target = cache_path()
        fd, tmp = tempfile.mkstemp(
            dir=str(target.parent), prefix=".doi_cache-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(pruned, f)
            os.replace(tmp, str(target))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        logger.warning("doi_cache save failed (cache skipped): %s", exc)
