"""Durable duplicate barrier for the two unkeyed POSTs of an item create.

The twin of :mod:`notesync.create_attempts`, and it exists for the same
reason: an unkeyed POST whose response is lost leaves us unable to tell
"nothing was created" from "an item was created and we never learned its
key", and retrying on that ambiguity mints a duplicate in the user's library.
A reservation is therefore taken BEFORE the first POST, and there is no
``DELETE`` — a retained attempt is evidence, not a lock to be cleared.

**Two stages, because a create is two POSTs.** The parent is minted first,
then the linked-file attachment under its returned key. The store records
each acknowledgement separately, which makes the middle state recoverable
rather than merely blocked: with a parent key in hand we can READ that
item's children and either adopt an attachment a lost response hid from us,
or attach for real. Only the parent stage is truly ambiguous.

Identity is ``sha256([resolved vault, pdf sha256])`` — the PDF's bytes, not
its path, so a rename cannot buy a second create.
"""
import hashlib
import json
from pathlib import Path
import sqlite3

#: What :func:`reserve` says about an existing row. Only ``fresh`` may POST a
#: parent; ``parent_unknown`` is the one state that must refuse outright.
STAGE_FRESH = 'fresh'
STAGE_PARENT_UNKNOWN = 'parent_unknown'
STAGE_PARENT_DONE = 'parent_done'
STAGE_COMPLETE = 'complete'


def store_path():
    from core.constants import BASE_DIR
    return Path(BASE_DIR) / 'zotcreate' / 'create_attempts.sqlite3'


def _vault_s(vault):
    return str(Path(vault).resolve())


def identity_for(vault, pdf_sha256):
    """Stable per-(vault, PDF bytes) barrier id."""
    return hashlib.sha256(
        json.dumps([_vault_s(vault), str(pdf_sha256)]).encode()).hexdigest()


def _ensure_schema(db):
    # CREATE IF NOT EXISTS so concurrent first-openers do not race on the
    # table itself; callers BEGIN IMMEDIATE after this helper.
    db.execute(
        'CREATE TABLE IF NOT EXISTS attempts ('
        'id TEXT PRIMARY KEY, vault TEXT NOT NULL, pdf_sha256 TEXT NOT NULL, '
        'payload TEXT NOT NULL, parent_ack TEXT, attach_ack TEXT)'
    )


def _open_readonly():
    """Read-only handle, or ``None`` when this store holds no evidence yet.

    ``reserve`` creates the FILE on connect and the table in the next
    statement, so a crash in between leaves a valid but empty database.
    Raising ``no such table`` there would block every future create behind an
    opaque 500; an empty store provably retains nothing, so it is the same
    answer as no store at all. Corruption still raises, and still blocks.
    """
    path = store_path()
    if not path.exists():
        return None
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attempts'"
    ).fetchone():
        db.close()
        return None
    return db


def _row_state(row):
    """``(stage, retained)`` for one stored row."""
    _id, _vault, _sha, raw, parent_ack, attach_ack = row
    payload = json.loads(raw)
    parent = json.loads(parent_ack) if parent_ack else None
    attach = json.loads(attach_ack) if attach_ack else None
    retained = {
        'pdf_rel': payload.get('pdf_rel'),
        'item_type': payload.get('item_type'),
        'title': payload.get('title'),
        'library': payload.get('library'),
        'base': payload.get('base'),
        'server_id': payload.get('server_id'),
        'attachment_dispatch': payload.get('attachment_dispatch', 'legacy_unknown'),
        'parent_key': (parent or {}).get('key'),
        'attachment_key': (attach or {}).get('key'),
        'parent': parent,
        'attachment': attach,
    }
    if parent is None:
        return STAGE_PARENT_UNKNOWN, retained
    if attach is None:
        return STAGE_PARENT_DONE, retained
    return STAGE_COMPLETE, retained


def lookup(vault, pdf_sha256):
    """``(identity, stage, retained)`` without creating or locking anything.

    ``stage`` is :data:`STAGE_FRESH` and ``retained`` ``None`` when nothing
    is recorded for this PDF.
    """
    identity = identity_for(vault, pdf_sha256)
    db = _open_readonly()
    if db is None:
        return identity, STAGE_FRESH, None
    try:
        row = db.execute(
            'SELECT id, vault, pdf_sha256, payload, parent_ack, attach_ack '
            'FROM attempts WHERE id=?', (identity,)).fetchone()
    finally:
        db.close()
    if row is None:
        return identity, STAGE_FRESH, None
    stage, retained = _row_state(row)
    return identity, stage, retained


def reserve(vault, pdf_sha256, *, pdf_rel, item_type, title, library, base,
            server_id=None):
    """Claim the right to create for this PDF; return ``(identity, stage,
    retained)``.

    On :data:`STAGE_FRESH` the row is inserted and the caller may POST a
    parent. Every other stage means a row already existed — the caller must
    NOT mint a second parent, and the stage says whether it can resume
    (:data:`STAGE_PARENT_DONE`) or must refuse (:data:`STAGE_PARENT_UNKNOWN`,
    :data:`STAGE_COMPLETE`).
    """
    identity = identity_for(vault, pdf_sha256)
    payload = dict(vault=_vault_s(vault), pdf_sha256=str(pdf_sha256),
                   pdf_rel=str(pdf_rel), item_type=str(item_type),
                   title=str(title or ''), library=str(library),
                   base=str(base), server_id=server_id, attachment_dispatch='not_sent')
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as db:
        db.execute('PRAGMA synchronous=FULL')
        _ensure_schema(db)
        db.execute('BEGIN IMMEDIATE')
        row = db.execute(
            'SELECT id, vault, pdf_sha256, payload, parent_ack, attach_ack '
            'FROM attempts WHERE id=?', (identity,)).fetchone()
        if row is not None:
            stage, retained = _row_state(row)
            db.execute('ROLLBACK')
            return identity, stage, retained
        db.execute(
            'INSERT INTO attempts (id, vault, pdf_sha256, payload, parent_ack, '
            'attach_ack) VALUES (?,?,?,?,NULL,NULL)',
            (identity, payload['vault'], payload['pdf_sha256'],
             json.dumps(payload)))
        db.commit()
    return identity, STAGE_FRESH, None


def _acknowledge(identity, column, value):
    query = {
        'parent_ack': 'UPDATE attempts SET parent_ack=? WHERE id=? AND parent_ack IS NULL',
        'attach_ack': 'UPDATE attempts SET attach_ack=? WHERE id=? AND attach_ack IS NULL',
    }[column]
    with sqlite3.connect(store_path(), timeout=10) as db:
        db.execute('PRAGMA synchronous=FULL')
        _ensure_schema(db)
        db.execute('BEGIN IMMEDIATE')
        updated = db.execute(
            query,
            (json.dumps(value), identity))
        if updated.rowcount != 1:
            db.execute('ROLLBACK')
            raise RuntimeError(
                'Creation attempt is missing or already acknowledged '
                f'({column}).')
        db.commit()


def acknowledge_parent(identity, *, key, version=None, creation_data=None):
    """Record the parent key. Persisted BEFORE the journal write, so a crash
    in between leaves evidence that a parent exists rather than none."""
    _acknowledge(identity, 'parent_ack',
                 dict(key=str(key), version=version,
                      creation_data=creation_data))


def acknowledge_attachment(identity, *, key, version=None, creation_data=None,
                           adopted=False):
    """Record the attachment key. ``adopted`` marks one recovered by reading
    the parent's children after a lost response, rather than created here."""
    _acknowledge(identity, 'attach_ack',
                 dict(key=str(key), version=version,
                      creation_data=creation_data, adopted=bool(adopted)))


def block_message(stage, retained):
    """User-facing copy for a stage that refuses. Names the key when we hold
    one, because "already created" is actionable only with somewhere to look.
    """
    retained = retained or {}
    if stage == STAGE_COMPLETE:
        return (
            'This PDF was already made into a Zotero item '
            f"(item {retained.get('parent_key')}, attachment "
            f"{retained.get('attachment_key')}). Review it in Zotero if it "
            'was a mistake. Automatic Desktop undo is unavailable; creating again would duplicate it.'
        )
    if stage == STAGE_PARENT_UNKNOWN:
        return (
            'An earlier attempt to create an item for this PDF was reserved '
            'and never acknowledged, so an item may exist in Zotero without '
            'this app knowing its key. Automatic retry is blocked. Search '
            'Zotero for the title; if nothing was created, the attempt can be '
            'investigated manually. A missing search result does not authorize retry.'
        )
    return 'An earlier creation attempt for this PDF is retained.'


def mark_attachment_dispatch(identity):
    """One-way durable barrier immediately before the unkeyed attachment POST."""
    with sqlite3.connect(store_path(), timeout=10) as db:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT payload FROM attempts WHERE id=?', (identity,)).fetchone()
        payload = json.loads(row[0])
        if payload.get('attachment_dispatch') != 'not_sent':
            raise RuntimeError('Attachment dispatch is uncertain; automatic repost is blocked.')
        payload['attachment_dispatch'] = 'sent_or_unknown'
        db.execute('UPDATE attempts SET payload=? WHERE id=?', (json.dumps(payload), identity))


def list_attempts(vault):
    db = _open_readonly()
    if db is None:
        return []
    try:
        rows = db.execute('SELECT id,vault,pdf_sha256,payload,parent_ack,attach_ack FROM attempts WHERE vault=?', (_vault_s(vault),)).fetchall()
        return [dict(identity=r[0], sha256=r[2], stage=_row_state(r)[0], retained=_row_state(r)[1]) for r in rows]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Maintainer-authorised retirement of a parent_unknown attempt (2026-09-17)
# ---------------------------------------------------------------------------
#
# The rule above still holds for AUTOMATIC paths: nothing in the app releases
# a barrier on its own, and a missing search result alone never authorises a
# retry. Retirement exists because the maintainer decided (annual cleanup,
# 2026-09-17) that a COMPLETE observation of the original library — live items
# and trash, verified by count and version — showing no item with the reserved
# title and no attachment to the file, is enough evidence to unblock a PDF
# they ask to unblock. The row is MOVED, never deleted: it lands in
# `retired_attempts` with that evidence, and `reinstate` puts it back.


def _ensure_retired_schema(db):
    db.execute(
        'CREATE TABLE IF NOT EXISTS retired_attempts ('
        'seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, vault TEXT NOT NULL, '
        'pdf_sha256 TEXT NOT NULL, payload TEXT NOT NULL, parent_ack TEXT, attach_ack TEXT, '
        'retired_at REAL NOT NULL, reason TEXT NOT NULL, evidence TEXT NOT NULL)'
    )


def retire_never_created(identity, *, evidence):
    """Move a ``parent_unknown`` row to ``retired_attempts``; return its seq.

    Refuses (``RuntimeError``) unless the row exists and carries NO
    acknowledgement at all — a known parent key is never "never created".
    """
    import time
    with sqlite3.connect(store_path(), timeout=10) as db:
        db.execute('PRAGMA synchronous=FULL')
        _ensure_schema(db)
        _ensure_retired_schema(db)
        db.execute('BEGIN IMMEDIATE')
        row = db.execute(
            'SELECT id, vault, pdf_sha256, payload, parent_ack, attach_ack '
            'FROM attempts WHERE id=?', (identity,)).fetchone()
        if row is None or row[4] is not None or row[5] is not None:
            db.execute('ROLLBACK')
            raise RuntimeError('Only an unacknowledged (parent_unknown) attempt can be retired.')
        cur = db.execute(
            'INSERT INTO retired_attempts (id, vault, pdf_sha256, payload, parent_ack, attach_ack, '
            'retired_at, reason, evidence) VALUES (?,?,?,?,?,?,?,?,?)',
            (*row, time.time(), 'never_created', json.dumps(evidence, sort_keys=True)))
        db.execute('DELETE FROM attempts WHERE id=?', (identity,))
        db.commit()
        return cur.lastrowid


def reinstate(seq):
    """Undo a retirement: move the row back. Refuses when a newer attempt for
    the same PDF now exists (it would be overwritten)."""
    if not store_path().exists():
        raise RuntimeError('No retired attempt with that number.')
    with sqlite3.connect(store_path(), timeout=10) as db:
        db.execute('PRAGMA synchronous=FULL')
        _ensure_schema(db)
        _ensure_retired_schema(db)
        db.execute('BEGIN IMMEDIATE')
        row = db.execute(
            'SELECT id, vault, pdf_sha256, payload, parent_ack, attach_ack '
            'FROM retired_attempts WHERE seq=?', (int(seq),)).fetchone()
        if row is None:
            db.execute('ROLLBACK')
            raise RuntimeError('No retired attempt with that number.')
        if db.execute('SELECT 1 FROM attempts WHERE id=?', (row[0],)).fetchone():
            db.execute('ROLLBACK')
            raise RuntimeError('A newer creation attempt exists for this PDF; reinstating would overwrite it.')
        db.execute('INSERT INTO attempts (id, vault, pdf_sha256, payload, parent_ack, attach_ack) '
                   'VALUES (?,?,?,?,?,?)', row)
        db.execute('DELETE FROM retired_attempts WHERE seq=?', (int(seq),))
        db.commit()
        return row[0]


def list_retired(vault):
    db = _open_readonly()
    if db is None:
        return []
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='retired_attempts'").fetchone():
            return []
        rows = db.execute('SELECT seq, pdf_sha256, payload, retired_at, reason, evidence FROM retired_attempts '
                          'WHERE vault=? ORDER BY seq', (_vault_s(vault),)).fetchall()
    finally:
        db.close()
    return [dict(seq=r[0], sha256=r[1], pdf_rel=json.loads(r[2]).get('pdf_rel'),
                 title=json.loads(r[2]).get('title'), retired_at=r[3], reason=r[4],
                 evidence=json.loads(r[5])) for r in rows]
