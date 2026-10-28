"""Persist exact-file attachment keys before creating remote items.

Retries read the preallocated key first. Creation with version=0 cannot update
an existing record. This intent survives missing responses and undo-journal
failures; it is not itself an undo record.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from pathlib import Path

ALPHABET = '23456789ABCDEFGHIJKLMNPQRSTUVWXYZ'


def store_path():
    from core.constants import BASE_DIR
    return Path(BASE_DIR) / 'tagging' / 'attachment_intents.sqlite3'


#: Identity fields hashed into a reservation. `dev` was hashed too until
#: 2026-09-17, but macOS renumbers a volume's device at every boot, so a retry
#: after a restart computed a NEW identity and reserved a SECOND key — the
#: exact duplicate-create this barrier exists to prevent. Rows written under
#: the old scheme stay findable through :func:`_legacy_key`.
_IDENTITY_KEYS = ('ino', 'birth', 'sha256')
_SCHEME = 2


def _legacy_key(con, target):
    """The key of a pre-2026-09-17 reservation for the same file, if any.

    Legacy rows hashed `dev` and do not store the review-file id, so they are
    matched on everything else they recorded: the same database instance,
    library and parent, and the same content hash, inode and birth time — one
    physical file, whatever number the volume carries today. Rows written
    under the current scheme carry `scheme` and are never matched here, so
    two review files keep separate keys exactly as before.
    """
    fields = ('vault', 'base', 'instance', 'library', 'parent')
    for item_key, raw in con.execute('SELECT item_key, target FROM intents'):
        try:
            old = json.loads(raw)
        except ValueError:
            continue
        if old.get('scheme') or any(old.get(f) != target[f] for f in fields):
            continue
        if all((old.get('identity') or {}).get(k) == target['identity'][k] for k in _IDENTITY_KEYS):
            return item_key
    return None


def reserve(vault, commit, *, create=True):
    target = dict(vault=str(Path(vault).resolve()), base=commit['transport'].base_url,
                  instance=commit.get('server_id'), library=commit['lib_path'],
                  parent=commit['parent_key'], path=commit['path'],
                  identity=commit['expected_identity'])
    # Touching a file does not create a fresh remote-write intent. Separate
    # copies retain separate paths/review IDs even when their bytes match.
    stable = {k: v for k, v in target.items() if k not in ('path', 'identity')}
    stable['file'] = (commit.get('review_token') or {}).get('id') or target['path']
    stable['identity'] = {k: target['identity'][k] for k in _IDENTITY_KEYS}
    stable['scheme'] = _SCHEME
    identity = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()
    raw = json.dumps({**target, 'scheme': _SCHEME, 'file': stable['file']}, sort_keys=True)
    path = store_path()
    if not create and not path.exists():
        return None
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path if create else path.resolve().as_uri() + '?mode=ro', timeout=10, uri=not create)
    try:
        if not create:
            row = con.execute('SELECT item_key FROM intents WHERE identity=?', (identity,)).fetchone()
            return row[0] if row else _legacy_key(con, target)
        con.execute('PRAGMA synchronous=FULL')
        con.execute('CREATE TABLE IF NOT EXISTS intents (identity TEXT PRIMARY KEY, item_key TEXT NOT NULL UNIQUE, target TEXT NOT NULL)')
        with con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT item_key FROM intents WHERE identity=?', (identity,)).fetchone()
            if row:
                return row[0]
            legacy = _legacy_key(con, target)
            if legacy:
                return legacy
            con.execute('INSERT INTO intents VALUES (?,?,?)',
                        (identity, ''.join(secrets.choice(ALPHABET) for _ in range(8)), raw))
            return con.execute('SELECT item_key FROM intents WHERE identity=?', (identity,)).fetchone()[0]
    finally:
        con.close()
