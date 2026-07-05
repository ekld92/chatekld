"""Sticky, per-vault per-image *flag* sidecar (the strip / keep-handwritten store).

A small generalization of ``refactor.ignore``: instead of a single set of
ignored paths, this stores a **set of named flags per image** so two distinct
per-image opt-ins can share one sidecar and one endpoint rather than spawning a
near-identical module each:

* ``strip``            — strip the descriptive "metadata" preamble from this
  image's extracted-text callout (keep only the transcription;
  ``refactor.text.strip_ocr_preamble``).
* ``keep_handwritten`` — force this image's OCR callout to be inlined even though
  the zero-vision handwritten heuristic (or a cached ``handwritten`` classify
  label) would otherwise auto-hide it (the per-image "Keep anyway" override).

``ignore`` stays its own module (separately tested, different semantics — it drops
the image from the candidate counts entirely); this only ever changes the callout
body or whether a heuristically-handwritten callout is shown.

The list is **rel_path-keyed** (vault-relative posix paths) and persisted as JSON
at ``BASE_DIR/obsidian_cache/refactor/<vault_key>/image_flags.json`` — the app
cache dir, **never the vault** (Phase 1 writes zero vault files). Keyed by vault
like the image-description cache. Reuses the indexer's atomic text writer +
vault-cache-key helpers on the singleton, exactly as ``ignore.py`` / ``cache.py``
do (so the vendored reuse surface stays in one place).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from core.constants import OBSIDIAN_CACHE_DIR
from rag.vault import obsidian_manager

_FLAGS_FILENAME = "image_flags.json"

# The only flag names a caller may store. Restricting the set keeps a malformed
# request from polluting the sidecar with arbitrary keys and documents the
# contract in one place. ``analyze_note`` reads these literals.
ALLOWED_FLAGS = frozenset({"strip", "keep_handwritten"})

# Serialize the sidecar's read-modify-write, exactly like ``ignore._LOCK``: each
# ``_save`` is atomic on its own, but ``add`` / ``remove`` are load → mutate →
# save sequences that two concurrent toggles of different images could otherwise
# interleave into a lost update under the multi-threaded waitress server. Pure
# readers (``load_flags`` / ``list_flags``) intentionally skip the lock —
# ``os.replace`` guarantees a reader sees the whole old or whole new file.
_LOCK = threading.Lock()


def _flags_file(vault_root: Path) -> Path:
    vault_key = obsidian_manager._vault_cache_key(vault_root)
    return Path(OBSIDIAN_CACHE_DIR) / "refactor" / vault_key / _FLAGS_FILENAME


def load_flags(vault_root: Path) -> dict[str, set[str]]:
    """Map of ``rel_path -> {flag, …}`` (empty if absent / corrupt).

    Only known flags survive the read, so a sidecar hand-edited with a stray key
    can never make ``analyze_note`` act on an unrecognized flag.
    """
    try:
        raw = _flags_file(vault_root).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    table = data.get("flags") if isinstance(data, dict) else None
    if not isinstance(table, dict):
        return {}
    out: dict[str, set[str]] = {}
    for rel, names in table.items():
        if not isinstance(rel, str) or not rel or not isinstance(names, list):
            continue
        kept = {n for n in names if isinstance(n, str) and n in ALLOWED_FLAGS}
        if kept:
            out[rel] = kept
    return out


def list_flags(vault_root: Path) -> dict[str, list[str]]:
    """Sorted-list form of :func:`load_flags` (for JSON responses)."""
    table = load_flags(vault_root)
    return {rel: sorted(names) for rel, names in sorted(table.items())}


def has(table: dict[str, set[str]], rel: str, flag: str) -> bool:
    """True if *rel* carries *flag* in an already-loaded table (read helper)."""
    return flag in table.get(rel, ())


def _save(vault_root: Path, table: dict[str, set[str]]) -> dict[str, list[str]]:
    ordered = {rel: sorted(names) for rel, names in sorted(table.items()) if names}
    obsidian_manager._atomic_write_text(
        _flags_file(vault_root), json.dumps({"flags": ordered}, ensure_ascii=False)
    )
    return ordered


def add(vault_root: Path, rel: str, flag: str) -> dict[str, list[str]]:
    """Add *flag* to *rel*; return the new sorted table.

    Raises ``ValueError`` on an unknown flag (defence — the route validates the
    enum, this is belt-and-braces). The whole load → mutate → save runs under
    ``_LOCK`` so a concurrent toggle of a different image can't clobber it.
    """
    if flag not in ALLOWED_FLAGS:
        raise ValueError(f"unknown flag: {flag!r}")
    with _LOCK:
        table = load_flags(vault_root)
        table.setdefault(rel, set()).add(flag)
        return _save(vault_root, table)


def remove(vault_root: Path, rel: str, flag: str) -> dict[str, list[str]]:
    """Remove *flag* from *rel*; return the new sorted table.

    Pruning the rel entry when its last flag is removed keeps the sidecar from
    accumulating empty arrays. Same lock discipline as :func:`add`.
    """
    if flag not in ALLOWED_FLAGS:
        raise ValueError(f"unknown flag: {flag!r}")
    with _LOCK:
        table = load_flags(vault_root)
        if rel in table:
            table[rel].discard(flag)
            if not table[rel]:
                del table[rel]
        return _save(vault_root, table)
