"""Process-wide, model-scoped cache of parameters rejected by online APIs."""
from __future__ import annotations

import logging
import threading

_lock = threading.Lock()
_rejected: set[tuple[str, str, str, str]] = set()
_log = logging.getLogger(__name__)


def remember_rejected(provider: str, base_url: str, model: str, param: str) -> None:
    key = (provider, base_url, model, param)
    with _lock:
        if key in _rejected:
            return
        _rejected.add(key)
    _log.info("%s %s rejected %s; omit it on subsequent requests", provider, model, param)


def is_rejected(provider: str, base_url: str, model: str, param: str) -> bool:
    with _lock:
        return (provider, base_url, model, param) in _rejected
