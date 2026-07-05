"""Server-side staging cache for applyable LLM proposals (Phase 3 writes).

The deterministic Phase 2 writers (``apply.py`` / ``format_fix.py``) recompute the
proposed body server-side at apply time, so they never trust a body from the
client. An LLM rewrite/summary is **non-deterministic** — it cannot be recomputed
to the same bytes — so to keep the same "the server controls the bytes that land
in the vault" guarantee, the generate step writes its proposed whole-note body
**here** (under the app cache dir, never the vault) and the apply step reads it
back server-side. The client only ever passes hashes, never note content.

Layout (mirrors ``ignore.py`` / ``flags.py``):

    BASE_DIR/obsidian_cache/refactor/<vault_key>/staging/<sha256(rel)>.<action>.json

Each file holds ``{rel, action, content_sha256, proposed, proposed_sha256, ts}``
where ``content_sha256`` is the on-disk note hash the proposal was computed
against (so a note edited since staging is detected as stale at apply time) and
``proposed_sha256`` is the hash of the staged bytes (the WYSIWYG guard). A new
generate for the same (rel, action) overwrites the prior staging file, and a
successful apply clears it.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from core.constants import OBSIDIAN_CACHE_DIR
from rag.vault import obsidian_manager

from refactor.result import sha256_text

# Actions that may stage an applyable proposal. Restricted so the action string
# can never escape the filename it builds (it is part of the path).
ALLOWED_ACTIONS = frozenset({"rewrite", "summarize_pdf", "custom"})

# A previewed-but-never-applied proposal would otherwise linger forever (apply is
# the only path that clears it), so each stage() opportunistically sweeps staging
# files older than this. 7 days comfortably outlives a normal preview→apply cycle.
_STAGING_TTL_S = 7 * 24 * 3600

_LOCK = threading.Lock()


def _staging_dir(vault_root: Path) -> Path:
    vault_key = obsidian_manager._vault_cache_key(vault_root)
    return Path(OBSIDIAN_CACHE_DIR) / "refactor" / vault_key / "staging"


def _staging_file(vault_root: Path, rel: str, action: str) -> Path:
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"unknown staging action: {action!r}")
    key = hashlib.sha256(rel.encode("utf-8")).hexdigest()
    return _staging_dir(vault_root) / f"{key}.{action}.json"


def _sweep_expired(staging_dir: Path) -> None:
    """Best-effort: delete staging files older than ``_STAGING_TTL_S``. Never raises.

    Bounded to the small per-vault staging dir; ``glob`` on a not-yet-created dir
    yields nothing, so this is a no-op on first use.
    """
    import os
    import time
    cutoff = time.time() - _STAGING_TTL_S
    try:
        for p in staging_dir.glob("*.json"):
            try:
                if os.path.getmtime(p) < cutoff:
                    p.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


def stage(vault_root: Path, rel: str, content_sha256: str, proposed: str,
          action: str) -> dict:
    """Persist a generated proposal and return its descriptor.

    The descriptor ``{rel, action, content_sha256, proposed_sha256, ts}`` (no
    body) is what the route hands to the UI; the full body stays server-side.
    """
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"unknown staging action: {action!r}")
    proposed_sha256 = sha256_text(proposed)
    record = {
        "rel": rel,
        "action": action,
        "content_sha256": content_sha256,
        "proposed": proposed,
        "proposed_sha256": proposed_sha256,
        "ts": _now(),
    }
    with _LOCK:
        _sweep_expired(_staging_dir(vault_root))   # bound abandoned-preview disk
        obsidian_manager._atomic_write_text(
            _staging_file(vault_root, rel, action),
            json.dumps(record, ensure_ascii=False),
        )
    return {k: v for k, v in record.items() if k != "proposed"}


def load_staged(vault_root: Path, rel: str, action: str) -> dict | None:
    """Read back a staged proposal (full record incl. ``proposed``), or ``None``.

    ``os.replace`` makes the write atomic, so a reader sees a whole file or
    nothing — no lock needed on the read path (mirrors ``ignore.load_ignored``).
    """
    try:
        raw = _staging_file(vault_root, rel, action).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("proposed"), str):
        return None
    return data


def clear(vault_root: Path, rel: str, action: str) -> None:
    """Delete a staged proposal (best-effort; called after a successful apply)."""
    try:
        with _LOCK:
            _staging_file(vault_root, rel, action).unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
