"""Resolve relative linked attachments from a runtime-bound capability.

No profile discovery or preference-file guessing. The application currently
has no production provider for this capability and therefore refuses relative
paths. Only trusted observation adapters may supply a binding; routes never
accept it from a client. Expired bindings must be reacquired from the runtime.
"""
from pathlib import Path
import time


def resolve(raw, binding, *, database, database_path):
    if not raw.startswith('attachments:') or not isinstance(binding, dict):
        return None
    try:
        if (binding['database'] != database or not database
                or Path(binding['database_path']).resolve() != Path(database_path).resolve()
                or not Path(binding['profile']).is_absolute()
                or not 0 <= time.time() - binding['observed_at'] <= 60):
            return None
        base = Path(binding['base_path'])
        relative = Path(raw[len('attachments:'):])
        if not base.is_absolute() or relative.is_absolute() or '..' in relative.parts or not relative.parts:
            return None
        base = base.resolve(strict=True)
        result = (base/relative).resolve(strict=True)
        return result if result.is_file() and result.is_relative_to(base) else None
    except (KeyError, TypeError, ValueError, OSError):
        return None
