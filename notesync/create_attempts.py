"""Durable duplicate barrier for unkeyed note POSTs; never an undo baseline.

Uncertain attempts remain blocked. Releases are dispositions + a new
generation row that points at the old one — never a DELETE of history.
Terminal dispositions (``satisfied`` / ``superseded`` / ``uncertain``) shipped 2026-09-10; design plan deleted after live QA.
"""
import hashlib
import json
from pathlib import Path
import sqlite3

# Remotes that prove a note was created (we hold a key that names it).
_SATISFIED_REMOTES = frozenset({
    'matches_acknowledgement', 'changed', 'creation_baseline_unknown',
    'type_or_parent_changed', 'trashed',
})
_UNCERTAIN_REMOTES = frozenset({
    'unknown_outcome', 'absent_unresolved', 'unavailable_or_database_changed',
    'target_changed',
})


def store_path():
    from core.constants import BASE_DIR
    return Path(BASE_DIR) / 'notesync' / 'create_attempts.sqlite3'


def _vault_s(vault):
    return str(Path(vault).resolve())


def _subject_id(vault, citation_key):
    """Stable per-(vault, citation_key) barrier id (generation 0 row id)."""
    return hashlib.sha256(json.dumps([_vault_s(vault), citation_key]).encode()).hexdigest()


def _row_id(vault, citation_key, generation):
    if int(generation) == 0:
        return _subject_id(vault, citation_key)
    return hashlib.sha256(
        json.dumps([_vault_s(vault), citation_key, int(generation)]).encode()
    ).hexdigest()


def _token_for(payload, ack):
    return hashlib.sha256(json.dumps((payload, ack or {}), sort_keys=True).encode()).hexdigest()


def _ensure_schema(db):
    """Create or migrate attempts to generation + live-only UNIQUE indexes.

    Legacy rows used ``id = sha256([vault, key])`` and a table-level UNIQUE on
    ``target``. Supersession needs a second live row for the same parent, so the
    UNIQUE moves to a partial index on ``superseded_by IS NULL``.
    """
    # CREATE IF NOT EXISTS so concurrent first-openers do not race on the
    # table itself. Callers typically BEGIN IMMEDIATE *after* this helper;
    # the one-time legacy→v2 migration below is therefore best-effort outside
    # that transaction (crash mid-migration leaves an empty/unreadable store
    # that ``_open_readonly`` treats as empty — creation stays fail-closed on
    # true corruption).
    db.execute(
        'CREATE TABLE IF NOT EXISTS attempts ('
        'id TEXT PRIMARY KEY, subject TEXT NOT NULL, generation INTEGER NOT NULL, '
        'target TEXT NOT NULL, payload TEXT NOT NULL, acknowledgement TEXT, '
        'superseded_by TEXT)'
    )
    cols = {r[1] for r in db.execute('PRAGMA table_info(attempts)')}
    if 'subject' not in cols:
        db.execute(
            'CREATE TABLE IF NOT EXISTS attempts_v2 ('
            'id TEXT PRIMARY KEY, subject TEXT NOT NULL, generation INTEGER NOT NULL, '
            'target TEXT NOT NULL, payload TEXT NOT NULL, acknowledgement TEXT, '
            'superseded_by TEXT)'
        )
        # Another writer may have finished migration; only copy while legacy.
        if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attempts'"
        ).fetchone() and 'subject' not in {
            r[1] for r in db.execute('PRAGMA table_info(attempts)')
        }:
            db.execute(
                'INSERT OR IGNORE INTO attempts_v2 '
                '(id, subject, generation, target, payload, acknowledgement, superseded_by) '
                'SELECT id, id, 0, target, payload, acknowledgement, NULL FROM attempts'
            )
            db.execute('DROP TABLE attempts')
            db.execute('ALTER TABLE attempts_v2 RENAME TO attempts')
    db.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS attempts_live_subject '
        'ON attempts(subject) WHERE superseded_by IS NULL'
    )
    db.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS attempts_live_target '
        'ON attempts(target) WHERE superseded_by IS NULL'
    )
    db.execute(
        'CREATE TABLE IF NOT EXISTS resolutions '
        '(id TEXT PRIMARY KEY, evidence TEXT NOT NULL)'
    )


def _open_readonly():
    """Read-only handle, or ``None`` when this store holds no evidence yet.

    ``reserve`` creates the FILE on connect and the table in the next statement,
    so a crash or a full disk in between leaves a valid but empty database.
    Reading it raised ``no such table: attempts``, which the create route turns
    into a 500 -- blocking EVERY future note creation behind an opaque error
    until someone deletes the file by hand. An empty store provably retains
    nothing, so it is the same answer as no store at all. Corruption is NOT
    swallowed here: that raises, and creation stays blocked, which is right.
    """
    path = store_path()
    if not path.exists():
        return None
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attempts'").fetchone():
        db.close()
        return None
    return db


def _live_row(db, *, subject=None, identity=None, target=None):
    """Return ``(id, subject, generation, target, payload, acknowledgement)`` or None."""
    if identity is not None:
        row = db.execute(
            'SELECT id, subject, generation, target, payload, acknowledgement '
            'FROM attempts WHERE id=? AND superseded_by IS NULL', (identity,)
        ).fetchone()
        if row:
            return row
        # Pre-migration / gen-0: subject may equal id
        row = db.execute(
            'SELECT id, subject, generation, target, payload, acknowledgement '
            'FROM attempts WHERE subject=? AND superseded_by IS NULL', (identity,)
        ).fetchone()
        return row
    if subject is not None:
        return db.execute(
            'SELECT id, subject, generation, target, payload, acknowledgement '
            'FROM attempts WHERE subject=? AND superseded_by IS NULL', (subject,)
        ).fetchone()
    if target is not None:
        return db.execute(
            'SELECT id, subject, generation, target, payload, acknowledgement '
            'FROM attempts WHERE target=? AND superseded_by IS NULL', (target,)
        ).fetchone()
    return None


def _record(vault, citation_key):
    """Read retained live evidence without initializing a store or contacting Zotero."""
    subject = _subject_id(vault, citation_key)
    db = _open_readonly()
    if db is None:
        return None
    try:
        cols = {r[1] for r in db.execute('PRAGMA table_info(attempts)')}
        if 'subject' not in cols:
            # Legacy store: id == subject, no superseded_by.
            row = db.execute(
                'SELECT payload, acknowledgement FROM attempts WHERE id=?', (subject,)
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(row[0])
            ack = json.loads(row[1]) if row[1] else {}
            return payload, ack, subject, subject, 0
        row = _live_row(db, subject=subject)
    finally:
        db.close()
    if row is None:
        return None
    identity, _subj, generation, _target, raw, acknowledgement = row
    payload = json.loads(raw)
    ack = json.loads(acknowledgement) if acknowledgement else {}
    return payload, ack, subject, identity, int(generation)


def _resolution_for(db, subject, payload, ack):
    if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='resolutions'"
    ).fetchone():
        return None
    row = db.execute('SELECT evidence FROM resolutions WHERE id=?', (subject,)).fetchone()
    if not row:
        return None
    resolution = json.loads(row[0])
    if resolution.get('evidence_token') != _token_for(payload, ack):
        return None
    return resolution


def lookup(vault, citation_key):
    record = _record(vault, citation_key)
    if record is None:
        return None
    payload, ack, subject, identity, generation = record
    db = _open_readonly()
    resolution = None
    try:
        if db is not None:
            resolution = _resolution_for(db, subject, payload, ack)
    finally:
        if db is not None:
            db.close()
    status = 'acknowledged' if ack else 'uncertain'
    disposition = (resolution or {}).get('status')
    # bookkeeping_reconciled is a bookkeeping stamp; treat it as satisfied for messaging.
    if disposition == 'bookkeeping_reconciled':
        disposition = 'satisfied'
    retry = bool(
        disposition == 'superseded'
        and ack
        and resolution
        and resolution.get('automatic_retry_allowed')
    )
    out = dict(
        status=status,
        note_key=ack.get('note_key'),
        target={k: payload[k] for k in ('base', 'library', 'server_id', 'parent')},
        automatic_retry_allowed=retry,
        disposition=disposition,
        generation=generation,
        identity=identity,
        adopted=bool(ack.get('adopted')),
    )
    if disposition == 'satisfied' or (ack.get('note_key') and not retry):
        out['block_reason'] = 'note_exists'
    elif disposition == 'uncertain' or not ack:
        out['block_reason'] = 'uncertain'
    elif retry:
        out['block_reason'] = 'superseded_ready'
    else:
        out['block_reason'] = 'retained'
    return out


def list_attempts(vault, known_keys=()):
    """List this vault's evidence, including subjects absent from the scan.

    Legacy records have only hashed subjects: match those against known keys,
    never infer their vault from a source path or disclose another vault's row.
    """
    db = _open_readonly()
    if db is None:
        return []
    vault = _vault_s(vault)
    known = {_subject_id(vault, key): key for key in known_keys}
    try:
        cols = {r[1] for r in db.execute('PRAGMA table_info(attempts)')}
        if 'subject' not in cols:
            rows = db.execute(
                'SELECT id, id, 0, payload, acknowledgement FROM attempts ORDER BY id'
            ).fetchall()
        else:
            rows = db.execute(
                'SELECT id, subject, generation, payload, acknowledgement '
                'FROM attempts WHERE superseded_by IS NULL ORDER BY id'
            ).fetchall()
        resolutions = {}
        if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='resolutions'"
        ).fetchone():
            resolutions = dict(db.execute('SELECT id, evidence FROM resolutions'))
    finally:
        db.close()
    result = []
    for identity, subject, generation, raw, acknowledgement in rows:
        payload = json.loads(raw)
        key = payload.get('citation_key') if payload.get('vault') == vault else known.get(subject)
        if not key:
            continue
        # Verify even stored readable subjects against the immutable primary key.
        if _subject_id(vault, key) != subject:
            continue
        ack = json.loads(acknowledgement) if acknowledgement else {}
        resolution = json.loads(resolutions[subject]) if subject in resolutions else None
        if resolution and resolution.get('evidence_token') != _token_for(payload, ack):
            resolution = None
        disposition = (resolution or {}).get('status')
        if disposition == 'bookkeeping_reconciled':
            disposition = 'satisfied'
        result.append(dict(
            citation_key=key, note_key=ack.get('note_key'),
            status='acknowledged' if ack else 'uncertain',
            resolution=resolution,
            disposition=disposition,
            generation=int(generation),
            parent=payload['parent'], in_inventory=key in known.values(),
            automatic_retry_allowed=bool(
                disposition == 'superseded'
                and (resolution or {}).get('automatic_retry_allowed')
            ),
            adopted=bool(ack.get('adopted')),
        ))
    return result


def inspect(vault, citation_key, *, target, read_item):
    """Compare retained evidence with source and remote reads; never repair."""
    record = _record(vault, citation_key)
    if record is None:
        return None
    payload, ack, subject, identity, generation = record
    original = {k: payload[k] for k in ('base', 'library', 'server_id', 'parent')}
    result = dict(
        note_key=ack.get('note_key'), target=original,
        automatic_retry_allowed=False, automatic_undo_allowed=False,
        generation=generation, adopted=bool(ack.get('adopted')),
    )
    result['evidence_token'] = _token_for(payload, ack)
    db = _open_readonly()
    try:
        if db is not None:
            resolution = _resolution_for(db, subject, payload, ack)
            if resolution:
                result['disposition'] = (
                    'satisfied' if resolution.get('status') == 'bookkeeping_reconciled'
                    else resolution.get('status')
                )
                if (resolution.get('status') == 'superseded'
                        and resolution.get('automatic_retry_allowed')):
                    result['automatic_retry_allowed'] = True
    finally:
        if db is not None:
            db.close()
    if target != original:
        # Name WHICH field moved. The recorded values came from the transport and
        # the live `Zotero-Server-ID` header at creation time; the compared ones
        # from the cached scan's SQLite snapshot. "The target changed" alone
        # cannot distinguish a genuine change from a stale scan, or from a
        # database that predates the local API's persistent identity -- and this
        # state is a dead end for repair, so the reason is the whole message.
        return {**result, 'remote': 'target_changed', 'source': 'not_checked',
                'target_changed_fields': sorted(k for k in original if original[k] != target.get(k)),
                'current_target': dict(target)}
    from notesync.compare import split_note_parts
    from notesync.convert import md_to_html
    try:
        source = Path(payload['source']).resolve()
        if not source.is_relative_to(Path(vault).resolve()):
            raise ValueError('Source outside original vault')
        raw_bytes = source.read_bytes()
        raw = raw_bytes.decode('utf-8')
        result['source_sha256'] = hashlib.sha256(raw_bytes).hexdigest()
        parts = split_note_parts(raw)
        result['source'] = ('unchanged' if parts is not None
            and md_to_html(parts[1].strip('\n')) == payload['html'] else 'changed_or_unparsed')
        if ack.get('note_key'):
            from notesync.migrate import set_frontmatter_keys
            proposed, reason = set_frontmatter_keys(
                raw, {'zotero-key': original['parent'], 'zotero-note-key': ack['note_key']},
                strict_keys=True)
            result['frontmatter'] = reason or ('already_keyed' if proposed == raw else 'missing_keys')
    except (OSError, UnicodeError, ValueError):
        result['source'] = 'unavailable'
    if not ack.get('note_key'):
        return {**result, 'remote': 'unknown_outcome'}
    from tagging import zotero_write
    try:
        data, version = read_item(ack['note_key'])
    except zotero_write.ZoteroNotFound:
        return {**result, 'remote': 'absent_unresolved'}
    except (zotero_write.ZoteroError, zotero_write.ZoteroUnavailable):
        return {**result, 'remote': 'unavailable_or_database_changed'}
    if data.get('deleted'):
        state = 'trashed'
    elif data.get('itemType') != 'note' or data.get('parentItem') != original['parent']:
        state = 'type_or_parent_changed'
    elif not ack.get('creation_data') or not ack.get('version'):
        state = 'creation_baseline_unknown'
    elif data != ack['creation_data'] or int(version) != ack['version']:
        state = 'changed'
    else:
        state = 'matches_acknowledgement'
    return {**result, 'remote': state}


def prepare_frontmatter(vault, citation_key, *, target, read_item, token, source_sha256,
                        expected_path, inspected=None):
    """Return a byte-bound insertion proposal, never write or advance a base.

    *inspected* is an :func:`inspect` result the CALLER already obtained, so the
    Zotero read can happen before the vault operation lease is taken. Passing it
    changes nothing about safety: every gate below re-reads the retained record
    and re-hashes the source under the caller's lease.
    """
    result = inspected if inspected is not None else inspect(
        vault, citation_key, target=target, read_item=read_item)
    if (not result or result.get('evidence_token') != token or result.get('source_sha256') != source_sha256
            or result['remote'] != 'matches_acknowledgement' or result['source'] != 'unchanged'):
        raise ValueError('Source or creation evidence changed; inspect again.')
    payload, ack, *_rest = _record(vault, citation_key)
    if _token_for(payload, ack) != token:
        raise ValueError('Creation evidence changed.')
    path = Path(payload['source']).resolve()
    if path != Path(expected_path).resolve() or not path.is_relative_to(Path(vault).resolve()):
        raise ValueError('The current note mapping differs from the original source.')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != source_sha256:
        raise ValueError('The source changed after inspection.')
    from notesync.migrate import set_frontmatter_keys
    text = raw.decode('utf-8')
    proposed, reason = set_frontmatter_keys(
        text, {'zotero-key': target['parent'], 'zotero-note-key': ack['note_key']},
        strict_keys=True)
    if proposed is None:
        raise ValueError('Frontmatter repair refused: ' + reason)
    return dict(path=path, raw=raw, proposed=proposed, unchanged=proposed == text)


def repair_journal(vault, citation_key, *, target, read_item, token, cfg, inspected=None):
    """Reconstruct evidence only. Caller owns the vault operation lease.

    See :func:`prepare_frontmatter` for *inspected*: the remote read belongs
    outside the lease, the re-verification below belongs inside it.
    """
    from tagging import journal
    result = inspected if inspected is not None else inspect(
        vault, citation_key, target=target, read_item=read_item)
    if (not result or result.get('evidence_token') != token
            or result['remote'] != 'matches_acknowledgement' or result['source'] != 'unchanged'):
        return {'ok': False, 'status': 'conflict',
                'error': 'Inspection changed or is incomplete; no journal repair applied.'}
    payload, ack, subject, identity, _generation = _record(vault, citation_key)
    # Bind the second evidence read as well. An acknowledgement may have been
    # replaced by external maintenance while the remote read was in progress.
    if _token_for(payload, ack) != token:
        return {'ok': False, 'status': 'conflict', 'error': 'Retained evidence changed.'}
    manifest = journal.load(Path(vault), cfg)
    for op in manifest['ops']:
        if (op.get('kind') == 'zotero_note_create' and op.get('item_key') == ack['note_key']
                and op.get('base_url') == target['base'] and op.get('lib_path') == target['library']
                and op.get('server_id') == target['server_id']):
            return {'ok': True, 'status': 'existing', 'op_id': op['op_id'], 'state': op.get('state')}
    op_id = 'note-create-' + identity
    op = dict(op_id=op_id, kind='zotero_note_create', ts=journal.now_iso(),
        citation_key=citation_key, item_key=ack['note_key'], parent_item=target['parent'],
        base_url=target['base'], lib_path=target['library'], server_id=target['server_id'],
        transport='local' if target['server_id'] else 'web', html=payload['html'],
        version_after=0, creation_data=None, state='applied',
        recovered_evidence=True, acknowledgement=ack,
        adopted=bool(ack.get('adopted')))
    # Atomic disk-wins insertion preserves concurrent/terminal operations and
    # tombstones. Reconstructed evidence cannot grant automatic trash authority.
    journal.save_merged(Path(vault), cfg, {'ops': [op]})
    saved = journal.find_op(journal.load(Path(vault), cfg), op_id)
    if saved is None:
        return {'ok': False, 'status': 'conflict',
                'error': 'A removed journal record was not resurrected.'}
    return {'ok': True, 'status': 'recorded', 'op_id': op_id, 'state': saved.get('state')}


def _merge_resolution_stamp(old: dict, evidence: dict, *, token: str) -> dict:
    """Idempotent / provenance-preserving merge for the resolutions row.

    Why this exists: ``mark_satisfied`` deliberately omits ``op_id`` /
    ``source_sha256`` that ``resolve`` (bookkeeping) recorded. A naive
    INSERT OR REPLACE would thin that audit trail while creation stayed
    blocked either way. Rules:
    - Same token + same status → keep the richer prior stamp (idempotent).
    - Same token + status change → carry forward provenance fields the new
      stamp omitted; never invent fields the prior stamp lacked.
    - Token mismatch → caller already refused; this helper is not reached.
    """
    if old.get('evidence_token') != token:
        return evidence
    if old.get('status') == evidence.get('status'):
        return old
    # bookkeeping_reconciled already means "satisfied" for messaging; a later
    # mark_satisfied is a no-op that keeps the dated bookkeeping check.
    if (old.get('status') == 'bookkeeping_reconciled'
            and evidence.get('status') == 'satisfied'):
        return old
    merged = dict(evidence)
    for key in ('op_id', 'source_sha256'):
        if key not in merged and key in old:
            merged[key] = old[key]
    return merged


def _write_resolution(vault, citation_key, *, token, evidence):
    """Persist a disposition / bookkeeping stamp keyed by subject."""
    record = _record(vault, citation_key)
    if record is None:
        raise ValueError('No retained creation attempt for this citation key.')
    payload, ack, subject, identity, generation = record
    if _token_for(payload, ack) != token:
        raise ValueError('Retained evidence changed.')
    evidence = dict(evidence)
    evidence['evidence_token'] = token
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as db:
        db.execute('PRAGMA synchronous=FULL')
        _ensure_schema(db)
        db.execute('BEGIN IMMEDIATE')
        live = _live_row(db, subject=subject)
        if live is None:
            raise ValueError('Retained evidence changed.')
        cur_payload = json.loads(live[4])
        cur_ack = json.loads(live[5]) if live[5] else {}
        if _token_for(cur_payload, cur_ack) != token:
            raise ValueError('Retained evidence changed during resolution.')
        previous = db.execute(
            'SELECT evidence FROM resolutions WHERE id=?', (subject,)
        ).fetchone()
        if previous:
            evidence = _merge_resolution_stamp(
                json.loads(previous[0]), evidence, token=token)
        db.execute(
            'INSERT OR REPLACE INTO resolutions VALUES (?,?)',
            (subject, json.dumps(evidence)),
        )
        db.commit()
    return {'ok': True, **evidence}


def resolve(vault, citation_key, *, target, read_item, token, source_sha256, expected_path,
            cfg, inspected=None):
    """Record a checked bookkeeping outcome; never release the reservation.

    This is a dated observation, not a guarantee that external state stays put.
    Every later repair still performs its own fresh checks.
    """
    from tagging import journal
    proposal = prepare_frontmatter(vault, citation_key, target=target, read_item=read_item,
        token=token, source_sha256=source_sha256, expected_path=expected_path,
        inspected=inspected)
    if not proposal['unchanged']:
        raise ValueError('The note still needs its frontmatter keys repaired.')
    payload, ack, subject, identity, generation = _record(vault, citation_key)
    manifest = journal.load(Path(vault), cfg)
    matches = [op for op in manifest['ops'] if op.get('kind') == 'zotero_note_create'
        and op.get('item_key') == ack['note_key'] and op.get('base_url') == target['base']
        and op.get('lib_path') == target['library'] and op.get('server_id') == target['server_id']]
    if (len(matches) != 1 or matches[0].get('state') != 'applied'
            or matches[0].get('parent_item') != target['parent']):
        raise ValueError('Exactly one matching applied creation journal record is required.')
    evidence = dict(
        status='bookkeeping_reconciled', checked_at=journal.now_iso(),
        evidence_token=token, source_sha256=source_sha256, op_id=matches[0]['op_id'],
        note_key=ack['note_key'], automatic_retry_allowed=False,
        disposition='satisfied',
    )
    path = store_path()
    with sqlite3.connect(path, timeout=10) as db:
        db.execute('PRAGMA synchronous=FULL')
        _ensure_schema(db)
        db.execute('BEGIN IMMEDIATE')
        live = _live_row(db, subject=subject)
        if not live or _token_for(
                json.loads(live[4]), json.loads(live[5]) if live[5] else {}) != token:
            raise ValueError('Retained evidence changed during resolution.')
        if hashlib.sha256(proposal['path'].read_bytes()).hexdigest() != source_sha256:
            raise ValueError('The source changed during resolution.')
        previous = db.execute(
            'SELECT evidence FROM resolutions WHERE id=?', (subject,)
        ).fetchone()
        if previous:
            evidence = _merge_resolution_stamp(
                json.loads(previous[0]), evidence, token=token)
        db.execute(
            'INSERT OR REPLACE INTO resolutions VALUES (?,?)',
            (subject, json.dumps(evidence)),
        )
        db.commit()
    return {'ok': True, **evidence}


def mark_satisfied(vault, citation_key, *, target, read_item, token, inspected=None):
    """Record that creation succeeded; stay blocked because the note exists."""
    from tagging import journal
    result = inspected if inspected is not None else inspect(
        vault, citation_key, target=target, read_item=read_item)
    if not result or result.get('evidence_token') != token:
        raise ValueError('Inspect again; evidence token no longer matches.')
    if result.get('remote') not in _SATISFIED_REMOTES:
        raise ValueError(
            'Satisfied is only for a remote state that names a created note '
            f'(got {result.get("remote")!r}).'
        )
    note_key = result.get('note_key')
    if not note_key:
        raise ValueError('Satisfied requires an acknowledged note key.')
    return _write_resolution(vault, citation_key, token=token, evidence=dict(
        status='satisfied', disposition='satisfied', checked_at=journal.now_iso(),
        note_key=note_key, remote=result['remote'], automatic_retry_allowed=False,
    ))


def mark_uncertain(vault, citation_key, *, target, read_item, token, inspected=None):
    """Record that the outcome is permanently undecidable by the app."""
    from tagging import journal
    result = inspected if inspected is not None else inspect(
        vault, citation_key, target=target, read_item=read_item)
    if not result or result.get('evidence_token') != token:
        raise ValueError('Inspect again; evidence token no longer matches.')
    if result.get('remote') not in _UNCERTAIN_REMOTES:
        raise ValueError(
            'Uncertain is only for remotes the app cannot decide '
            f'(got {result.get("remote")!r}).'
        )
    return _write_resolution(vault, citation_key, token=token, evidence=dict(
        status='uncertain', disposition='uncertain', checked_at=journal.now_iso(),
        note_key=result.get('note_key'), remote=result['remote'],
        automatic_retry_allowed=False,
    ))


def allow_retry_after_trash(vault, citation_key, *, target, read_item, token, inspected=None):
    """Permit exactly one new POST after a confirmed *trashed* observation.

    Not reachable from ``absent_unresolved`` (a 404 is not erasure).
    """
    from tagging import journal
    result = inspected if inspected is not None else inspect(
        vault, citation_key, target=target, read_item=read_item)
    if not result or result.get('evidence_token') != token:
        raise ValueError('Inspect again; evidence token no longer matches.')
    if result.get('remote') != 'trashed':
        raise ValueError(
            'One retry is only allowed when the acknowledged note is in Zotero’s trash '
            f'(got {result.get("remote")!r}).'
        )
    note_key = result.get('note_key')
    if not note_key:
        raise ValueError('Trash-gated retry requires an acknowledged note key.')
    return _write_resolution(vault, citation_key, token=token, evidence=dict(
        status='superseded', disposition='superseded', checked_at=journal.now_iso(),
        note_key=note_key, remote='trashed', automatic_retry_allowed=True,
        superseded_note_key=note_key,
    ))


def adopt_note_key(vault, citation_key, *, note_key, target, read_item, token, inspected=None):
    """User-asserted identity for ``unknown_outcome``; never grants trash authority.

    Verifies the pasted key is a note under the retained parent whose HTML
    matches the retained payload. Records ``adopted=True`` and clears
    ``creation_data`` / ``version`` so revert_prepare always asks for manual review.
    """
    from tagging import journal
    result = inspected if inspected is not None else inspect(
        vault, citation_key, target=target, read_item=read_item)
    if not result or result.get('evidence_token') != token:
        raise ValueError('Inspect again; evidence token no longer matches.')
    if result.get('remote') != 'unknown_outcome':
        raise ValueError(
            'Adopt-by-key is only for an unacknowledged attempt '
            f'(got {result.get("remote")!r}).'
        )
    key = str(note_key or '').strip()
    if not key or len(key) > 64:
        raise ValueError('A Zotero note key is required.')
    from tagging import zotero_write
    try:
        data, _version = read_item(key)
    except zotero_write.ZoteroNotFound as exc:
        raise ValueError('That key was not found in Zotero.') from exc
    except (zotero_write.ZoteroError, zotero_write.ZoteroUnavailable) as exc:
        raise ValueError('Zotero could not be read; adopt refused.') from exc
    if data.get('deleted'):
        raise ValueError('That key is in Zotero’s trash; adopt refused.')
    payload, ack, subject, identity, generation = _record(vault, citation_key)
    if _token_for(payload, ack) != token:
        raise ValueError('Retained evidence changed.')
    if data.get('itemType') != 'note' or data.get('parentItem') != payload['parent']:
        raise ValueError('Adopted key must be a note under the retained parent item.')
    remote_html = data.get('note') or ''
    if remote_html != payload['html']:
        raise ValueError(
            'Adopted note HTML does not match the retained creation payload; '
            'find the note that was created for this attempt.'
        )
    if ack:
        raise ValueError('This attempt is already acknowledged.')
    new_ack = dict(note_key=key, version=None, creation_data=None, adopted=True)
    path = store_path()
    with sqlite3.connect(path, timeout=10) as db:
        db.execute('PRAGMA synchronous=FULL')
        _ensure_schema(db)
        db.execute('BEGIN IMMEDIATE')
        live = _live_row(db, subject=subject)
        if live is None or live[5] is not None:
            raise ValueError('Attempt is missing or already acknowledged.')
        cur_payload = json.loads(live[4])
        if _token_for(cur_payload, {}) != token:
            raise ValueError('Retained evidence changed during adopt.')
        updated = db.execute(
            'UPDATE attempts SET acknowledgement=? WHERE id=? AND acknowledgement IS NULL',
            (json.dumps(new_ack), live[0]),
        )
        if updated.rowcount != 1:
            raise ValueError('Attempt is missing or already acknowledged.')
        # Adopt converts uncertain → acknowledged; stamp satisfied for messaging.
        evidence = dict(
            status='satisfied', disposition='satisfied', checked_at=journal.now_iso(),
            evidence_token=_token_for(cur_payload, new_ack),
            note_key=key, remote='adopted', automatic_retry_allowed=False, adopted=True,
        )
        db.execute(
            'INSERT OR REPLACE INTO resolutions VALUES (?,?)',
            (subject, json.dumps(evidence)),
        )
        db.commit()
    return {'ok': True, 'note_key': key, 'adopted': True, 'automatic_undo_allowed': False,
            **{k: evidence[k] for k in ('status', 'disposition', 'checked_at')}}


def reserve(vault, citation_key, source, commit):
    subject = _subject_id(vault, citation_key)
    payload = dict(vault=_vault_s(vault), citation_key=citation_key,
        source=str(Path(source).resolve()), parent=commit['parent_key'],
        base=commit['transport'].base_url, library=commit['lib_path'],
        server_id=commit.get('server_id'), html=commit['html'])
    target = json.dumps([payload[k] for k in ('base', 'library', 'server_id', 'parent')])
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as db:
        db.execute('PRAGMA synchronous=FULL')
        _ensure_schema(db)
        db.execute('BEGIN IMMEDIATE')
        by_subject = _live_row(db, subject=subject)
        by_target = _live_row(db, target=target)
        row = by_subject or by_target
        if row is not None:
            identity, row_subject, generation, _t, raw, acknowledgement = row
            ack = json.loads(acknowledgement) if acknowledgement else {}
            cur_payload = json.loads(raw)
            resolution = _resolution_for(db, row_subject, cur_payload, ack)
            # Exactly one retry: confirmed trashed → superseded disposition.
            if (resolution and resolution.get('status') == 'superseded'
                    and resolution.get('automatic_retry_allowed')
                    and acknowledgement is not None):
                new_gen = int(generation) + 1
                new_id = _row_id(vault, citation_key if row_subject == subject
                                 else cur_payload.get('citation_key', citation_key), new_gen)
                # Prefer the caller's citation_key / source for the new generation.
                new_payload = dict(payload)
                if row_subject != subject:
                    # Same target, different subject (alias vault) — keep blocking.
                    return identity, ack
                db.execute(
                    'UPDATE attempts SET superseded_by=? WHERE id=? AND superseded_by IS NULL',
                    (new_id, identity),
                )
                db.execute(
                    'INSERT INTO attempts '
                    '(id, subject, generation, target, payload, acknowledgement, superseded_by) '
                    'VALUES (?,?,?,?,?,NULL,NULL)',
                    (new_id, subject, new_gen, target, json.dumps(new_payload)),
                )
                # Consume the one-shot licence: leave a trail without retry.
                spent = dict(resolution)
                spent['automatic_retry_allowed'] = False
                spent['consumed_by'] = new_id
                spent['status'] = 'superseded'
                db.execute(
                    'INSERT OR REPLACE INTO resolutions VALUES (?,?)',
                    (subject, json.dumps(spent)),
                )
                db.commit()
                return new_id, None
            return identity, ack
        identity = _row_id(vault, citation_key, 0)
        try:
            db.execute(
                'INSERT INTO attempts '
                '(id, subject, generation, target, payload, acknowledgement, superseded_by) '
                'VALUES (?,?,?,?,?,NULL,NULL)',
                (identity, subject, 0, target, json.dumps(payload)),
            )
            db.commit()
        except sqlite3.IntegrityError:
            # Concurrent first insert won the live subject/target slot.
            db.execute('ROLLBACK')
            db.execute('BEGIN IMMEDIATE')
            row = _live_row(db, subject=subject) or _live_row(db, target=target)
            if row is None:
                raise
            identity, _s, _g, _t, _raw, acknowledgement = row
            return identity, (json.loads(acknowledgement) if acknowledgement else {})
    return identity, None


def acknowledge(identity, *, key, version, creation_data):
    # Persist before the separate tagging journal/frontmatter writes. An
    # uncertain record remains useful if this acknowledgement itself fails.
    # BEGIN IMMEDIATE matches reserve/resolve peers so a concurrent writer
    # cannot interleave schema ensure with the single-row UPDATE.
    with sqlite3.connect(store_path(), timeout=10) as db:
        db.execute('PRAGMA synchronous=FULL')
        _ensure_schema(db)
        db.execute('BEGIN IMMEDIATE')
        updated = db.execute(
            'UPDATE attempts SET acknowledgement=? WHERE id=? AND acknowledgement IS NULL',
            (json.dumps(dict(note_key=str(key), version=version,
                            creation_data=creation_data)), identity),
        )
        if updated.rowcount != 1:
            db.execute('ROLLBACK')
            raise RuntimeError('Note creation attempt is missing or already acknowledged')
        db.commit()


def block_message(retained):
    """User-facing 409 copy for a retained attempt (create route)."""
    if not retained:
        return 'An earlier creation attempt is retained; automatic retry is blocked.'
    if retained.get('automatic_retry_allowed'):
        return (
            'A prior creation was marked superseded after trash; '
            'this request may create exactly one replacement note.'
        )
    note_key = retained.get('note_key')
    disposition = retained.get('disposition')
    if disposition == 'satisfied' or retained.get('block_reason') == 'note_exists':
        if note_key:
            return (
                f'A Zotero note already exists for this creation (key {note_key}). '
                'Creation stays blocked because that note is retained, not because '
                'the outcome is uncertain. Inspect the retained attempt to repair '
                'journal or frontmatter keys.'
            )
        return (
            'An earlier creation is recorded as satisfied; automatic retry is blocked.'
        )
    if disposition == 'uncertain' or retained.get('block_reason') == 'uncertain' or not note_key:
        return (
            'An earlier creation attempt is retained without a verified note key '
            '(unknown or undecidable outcome). Inspect it; automatic retry stays '
            'blocked. If you found the note in Zotero, use Adopt key…'
        )
    return (
        f'An earlier creation attempt is retained (note key {note_key}). '
        'Inspect the original Zotero target; automatic retry is blocked.'
    )
