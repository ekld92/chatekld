"""Load ``.env`` files into the process environment — one loader, two entry points.

The GUI (``launch.py``) and the headless server (``cherqain server``) must see
the same keys. Until the 2026-09-23 CLI proof run only the GUI loaded them, so
the headless server had to be started with ``set -a; source .env`` by hand, or
``ZOTERO_API_KEY`` was missing and the Zotero transport silently fell back to
the read-only local API (plan C4).

Import-light by design: stdlib only at import time (``python-dotenv`` is
imported inside the function), because both callers run it BEFORE the heavy
app import so a ``.env`` can also supply ``HF_HOME`` and the like.
"""
from __future__ import annotations

import logging
import os
from typing import Iterable

logger = logging.getLogger(__name__)


def load_dotenv_file(path: str | os.PathLike[str], *, override: bool = False) -> bool:
    """Load a single ``.env`` file after enforcing owner-only (``0o600``) mode.

    Returns ``True`` if the file existed and set at least one variable.
    Missing ``python-dotenv`` or an unreadable file never blocks startup.
    """
    file_path = os.fspath(path)
    try:
        if not os.path.isfile(file_path):
            return False
        from dotenv import load_dotenv
    except Exception:  # noqa: BLE001 - optional dependency or stat error
        logger.debug("python-dotenv not installed or unreadable %s; skipping.", file_path)
        return False
    try:
        try:
            os.chmod(file_path, 0o600)
        except OSError:
            logger.debug("Could not set owner-only mode on %s", file_path, exc_info=True)
        if load_dotenv(file_path, override=override):
            logger.info("Loaded environment overrides from %s", file_path)
            return True
    except Exception:  # noqa: BLE001 - one unreadable file never blocks startup
        logger.debug("Failed to load .env from %s", file_path, exc_info=True)
    return False


def load_env_files(dirs: Iterable[str]) -> list[str]:
    """Load ``<dir>/.env`` for each directory, in priority order.

    ``override=False``: a variable already in the environment (the shell, or
    an earlier file) always wins. Each file is first made owner-only
    (``0o600``) — it holds provider keys, and a world-readable one would leak
    them to other users of the machine. A missing ``python-dotenv`` disables
    loading and never blocks startup. Returns the files that set something.
    """
    loaded: list[str] = []
    for directory in dirs:
        path = os.path.join(directory, ".env")
        if load_dotenv_file(path, override=False):
            loaded.append(path)
    return loaded
