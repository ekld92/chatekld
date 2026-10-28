"""Persisted "this entry needs no local PDF" decisions (Batch B, 2026-08-26).

``BASE_DIR/audit/pdf_wanted.json`` holds the citation keys the user has said
do not need a PDF under ``biblio_articles/``. It is the missing MIRROR of
``mapping.json``'s ``no_match`` list: that one records "this PDF is deliberately
not in the bibliography", and there was no way to say the same thing in the
other direction — so the *Bib Entries Without PDFs* report could only ever
grow. Measured on the maintainer's library it stood at 1,502 rows, of which
293 are item types that by nature have no PDF (books, webpages, blog posts,
video recordings, reports, software).

Discipline is ``dup_decisions.py``'s exactly, and for the same reason: these
are hand-made, unregenerable decisions, so the write is RMW-locked, atomic AND
fsynced (unlike the regenerable ``hash_cache.json``), and the read used by the
write verbs REFUSES on a corrupt file rather than restarting the list from
empty. ``clear`` is the undo verb.

The report filters at RENDER time rather than the scan filtering at build time
(the near-duplicate precedent filters in the scan): a dismissal here changes
only which rows are shown, needs no rescan to take effect, and the count of
what was hidden is always disclosed.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_RMW_LOCK = threading.Lock()
#: Backstop only; a citation key is ~40 bytes and the library has ~6,300.
_MAX_KEYS = 50_000


def decisions_path() -> Path:
    """``BASE_DIR/audit/pdf_wanted.json`` (same dir as ``mapping.json``)."""
    from ..config import _audit_dir

    return Path(_audit_dir()) / "pdf_wanted.json"


def _parse(text: str) -> set[str]:
    raw = json.loads(text)
    keys = raw.get("no_pdf_needed") if isinstance(raw, dict) else None
    return {s for s in keys if isinstance(s, str)} if isinstance(keys, list) else set()


def load_dismissed() -> set[str]:
    """The dismissed citation-key set; read failures degrade to empty.

    Degrading is right for the REPORT filter — a corrupt file must not fail the
    report, the dismissed rows merely reappear — and wrong for the write verbs,
    which use :func:`_load_for_rmw` instead.
    """
    try:
        return _parse(decisions_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()


def _load_for_rmw() -> set[str]:
    """Strict load for the write verbs.

    A MISSING file is an empty set (the first ever decision), but an
    unreadable or corrupt file RAISES so the read-modify-write refuses rather
    than silently restarting a hand-made list from empty and clobbering it on
    the next save.
    """
    try:
        text = decisions_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    return _parse(text)


def _fsync_parent_dir(path) -> None:
    """Best-effort fsync of *path*'s directory entry (see the caller's note)."""
    try:
        fd = os.open(str(Path(path).parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _save(keys: set[str]) -> None:
    target = decisions_path()
    payload = {"no_pdf_needed": sorted(keys)[:_MAX_KEYS]}
    fd, tmp = tempfile.mkstemp(
        dir=str(target.parent), prefix=".pdf_wanted-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(target))
        # DURABILITY (2026-09-02, performance plan Phase 5): fsync the PARENT
        # DIRECTORY after the rename. The app-wide policy deliberately omits
        # this for regenerable files, but pdf_wanted.json is one of the three
        # UNREGENERABLE decision sidecars: fsyncing the file alone makes its
        # CONTENT durable while the directory ENTRY that names it may not be,
        # so a power loss right after the rename can leave the old file — or
        # no file — despite a successful save. Best-effort: a platform or
        # filesystem that refuses to fsync a directory is not a reason to fail
        # a write that has already landed.
        _fsync_parent_dir(target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def add_dismissed(citation_keys) -> int:
    """Record that these entries need no PDF. Returns how many were NEW.

    Takes an iterable so the bulk "dismiss every webpage" action is ONE atomic
    write rather than N; a partial bulk write would leave the user unable to
    tell which half landed.

    Raises ``OSError``/``ValueError`` when the existing file is unreadable or
    corrupt — never clobbers it.
    """
    wanted = {k for k in citation_keys if isinstance(k, str) and k.strip()}
    if not wanted:
        return 0
    with _RMW_LOCK:
        keys = _load_for_rmw()
        new = wanted - keys
        if not new:
            return 0
        keys |= new
        _save(keys)
        return len(new)


def clear_dismissed(citation_keys) -> int:
    """The undo: these entries need a PDF after all. Returns how many changed.

    Same strict-load contract as :func:`add_dismissed`, and the same bulk
    shape so an "undo that bulk dismiss" is also one atomic write.
    """
    wanted = {k for k in citation_keys if isinstance(k, str) and k.strip()}
    if not wanted:
        return 0
    with _RMW_LOCK:
        keys = _load_for_rmw()
        gone = wanted & keys
        if not gone:
            return 0
        keys -= gone
        _save(keys)
        return len(gone)
