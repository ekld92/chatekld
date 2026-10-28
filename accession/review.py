"""Durable, per-file PDF reviews. Network evidence is never a confirmation.

Report reads are read-only. Explicit opens hash one file; device/inode/birth
identity may recover a same-volume rename only when the old path is absent.
Copies (including hard links with an existing old path) require their own review.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class ReviewConflict(ValueError):
    """The file, proposal, or decision changed since the client read it."""


def store_path() -> Path:
    from core.constants import BASE_DIR
    return Path(BASE_DIR) / 'audit' / 'pdf_reviews.sqlite3'


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def fingerprint(path: Path) -> dict:
    s = path.stat()
    return dict(dev=s.st_dev, ino=s.st_ino, size=s.st_size, mtime=s.st_mtime_ns,
                birth=getattr(s, 'st_birthtime', None))


#: Identity fields that do not survive a reboot. macOS reassigns a volume's
#: device number at boot (2026-09-17: 16777229 → 16777230 after a restart
#: made 6,399 unchanged PDFs read as "replaced"). Records keep the value —
#: the same-volume check needs the CURRENT number — but no comparison ACROSS
#: TIME may use it. Content hash, inode and birth time still identify a file.
VOLATILE_IDENTITY_KEYS = frozenset({'dev'})


def same_identity(a, b) -> bool:
    """Equal file identities, ignoring fields a reboot renumbers."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return a == b
    strip = lambda d: {k: v for k, v in d.items() if k not in VOLATILE_IDENTITY_KEYS}  # noqa: E731
    return strip(a) == strip(b)


def identity(path: Path) -> dict:
    before = fingerprint(path)
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    if fingerprint(path) != before:
        raise ReviewConflict('PDF changed while being read. Open it again.')
    return {**before, 'sha256': h.hexdigest()}


@contextmanager
def connection(write=False):
    path = store_path()
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, timeout=10)
        con.execute('PRAGMA synchronous=FULL')
        con.executescript('''
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY, vault TEXT NOT NULL, rel TEXT NOT NULL,
                data TEXT NOT NULL, UNIQUE(vault, rel));
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY, file_id TEXT NOT NULL,
                at REAL NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_by_file_id ON events(file_id, sequence);
        ''')
        con.execute('BEGIN IMMEDIATE')
    else:
        if not path.exists():
            yield None
            return
        con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)
    try:
        yield con
        if write:
            con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def _save(con, record):
    record['updated_at'] = time.time()
    raw = json.dumps(record, ensure_ascii=False)
    con.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?)',
                (record['id'], record['vault'], record['rel'], raw))
    con.execute('INSERT INTO events(file_id,at,data) VALUES (?,?,?)',
                (record['id'], record['updated_at'], raw))


def _records(con, vault):
    return [] if con is None else [json.loads(r[0]) for r in con.execute(
        'SELECT data FROM files WHERE vault=?', (str(vault.resolve()),))]


def snapshot(vault: Path) -> dict:
    with connection() as con:
        return {r['rel']: r for r in _records(con, vault)}


def token(record):
    return {k: record[k] for k in ('id', 'sha256', 'revision', 'proposal_revision')}


def _check(record, expected):
    if not isinstance(expected, dict) or token(record) != expected:
        raise ReviewConflict('Review changed. Reopen the PDF before trying again.')


def public(record, path=None):
    out = dict(record)
    out.pop('vault', None)
    out['token'] = token(record)
    out['identity_state'] = 'verified'
    if path is not None:
        try:
            if not same_identity(fingerprint(path), record['fingerprint']):
                out['identity_state'] = 'changed'
        except OSError:
            out['identity_state'] = 'unavailable'
    chosen = record.get('chosen') or {}
    proposed = {c.get('doi') for c in (record.get('proposal') or {}).get('candidates', [])}
    out['conflicting_evidence'] = bool(chosen.get('doi') and proposed
                                       and chosen['doi'] not in proposed)
    out['export_eligible'] = (record['status'] == 'confirmed' and bool(chosen.get('doi'))
                              and out['identity_state'] == 'verified')
    return out


def open_review(vault: Path, rel: str, current: dict, proposal: dict | None):
    path = vault / rel
    fp = {k: v for k, v in current.items() if k != 'sha256'}
    with connection(True) as con:
        records = _records(con, vault)
        record = next((r for r in records if r['rel'] == rel), None)
        if record is None and fp['birth'] is not None:
            moved = [r for r in records if r['sha256'] == current['sha256']
                     and not (vault / r['rel']).exists()
                     and all(r['fingerprint'].get(k) == fp[k] for k in ('ino', 'birth'))]
            if len(moved) == 1:
                record = moved[0]
                con.execute('DELETE FROM files WHERE id=?', (record['id'],))
                record['rel'] = rel
        if record is None:
            record = dict(id=uuid.uuid4().hex, vault=str(vault.resolve()), rel=rel,
                          sha256=current['sha256'], fingerprint=fp, revision=0,
                          proposal_revision='', proposal=None, search=None,
                          status='unreviewed', chosen=None, rejected=[])
        if record['sha256'] != current['sha256'] or any(record['fingerprint'].get(k) != fp[k] for k in ('ino','birth')):
            record.update(sha256=current['sha256'], status='unreviewed', chosen=None,
                          rejected=[], search=None, proposal=None)
            record['revision'] += 1
        record['fingerprint'] = fp
        # Corrected searches belong to this file, not the shared automatic cache.
        if not record.get('search'):
            record['proposal'] = proposal
        record['proposal_revision'] = digest(record['proposal'])
        if fingerprint(path) != fp:
            raise ReviewConflict('PDF changed. Open it again.')
        _save(con, record)
        return public(record)


def update(vault: Path, rel: str, expected: dict, current: dict, *,
           action: str, candidate_id=None, proposal=None, search=None):
    with connection(True) as con:
        record = next((r for r in _records(con, vault) if r['rel'] == rel), None)
        if record is None:
            raise ReviewConflict('Open this file before reviewing it.')
        _check(record, expected)
        if current['sha256'] != record['sha256'] or any(current[k] != record['fingerprint'].get(k) for k in ('ino','birth')):
            raise ReviewConflict('PDF content changed. Review the new file.')
        if action == 'proposal':
            record.update(proposal=proposal, search=search,
                          proposal_revision=digest(proposal))
        elif action in ('confirm', 'reject'):
            candidates = (record.get('proposal') or {}).get('candidates', [])
            candidate = next((c for c in candidates if digest(c) == candidate_id), None)
            if candidate is None:
                raise ReviewConflict('Candidate changed. Reopen the PDF.')
            if action == 'confirm':
                record.update(status='confirmed', chosen=candidate)
                record['rejected'] = [c for c in record['rejected'] if c != candidate_id]
            else:
                if candidate_id not in record['rejected']:
                    record['rejected'].append(candidate_id)
                if record.get('chosen') == candidate:
                    record.update(chosen=None, status='unreviewed')
                elif record['status'] != 'confirmed':
                    record['status'] = 'rejected'
        elif action in ('defer', 'undo'):
            record.update(status='deferred' if action == 'defer' else 'unreviewed', chosen=None)
            if action == 'undo':
                record['rejected'] = []
        else:
            raise ValueError('Unknown review action.')
        record['revision'] += 1
        record['fingerprint'] = {k: v for k, v in current.items() if k != 'sha256'}
        if not same_identity(fingerprint(vault / rel), record['fingerprint']):
            raise ReviewConflict('PDF changed before the decision could be saved.')
        _save(con, record)
        return public(record)


def validate(vault, rel, expected):
    record = snapshot(vault).get(rel)
    if record is None:
        raise ReviewConflict('This file has not been reviewed.')
    _check(record, expected)
    current = identity(vault / rel)
    if current['sha256'] != record['sha256'] or any(current[k] != record['fingerprint'].get(k) for k in ('ino','birth')):
        raise ReviewConflict('PDF content changed. Review it again.')
    if record['status'] != 'confirmed' or not (record.get('chosen') or {}).get('doi'):
        raise ReviewConflict('No confirmed DOI for this file.')
    return record


@contextmanager
def decision_guard(vault, rel, expected):
    """Serialize a mapping decision against review edits without a network lock."""
    with connection(True) as con:
        record = next((r for r in _records(con, vault) if r['rel'] == rel), None)
        if record is None:
            raise ReviewConflict('Review no longer exists.')
        _check(record, expected)
        yield record


def effective_resolution(vault, rel, current, fallback):
    """Saved confirmation/search/rejections take precedence over automatic cache."""
    record = snapshot(vault).get(rel)
    if record is None:
        return fallback, None
    if record['sha256'] != current['sha256'] or any(current[k] != record['fingerprint'].get(k) for k in ('ino', 'birth')):
        raise ReviewConflict('PDF identity changed since review. Open and review it again.')
    if record['status'] == 'confirmed' and record.get('chosen'):
        result = dict(outcome='candidates', tier='confirmed', candidates=[record['chosen']])
    else:
        result = dict(record.get('proposal') or {})
        result['candidates'] = [c for c in result.get('candidates', []) if digest(c) not in record.get('rejected', [])]
        if record.get('rejected'):
            result.pop('doi', None)
            result.pop('citation_key', None)
            result['outcome'] = 'candidates' if result['candidates'] else 'none'
    return result, token(record)


def validate_creation(vault, rel, current, expected, fields):
    """Creation cannot bypass a rejected or confirmed identification via raw JSON."""
    _, actual = effective_resolution(vault, rel, current, None)
    if actual != expected:
        raise ReviewConflict('Review changed. Reopen the registration preview.')
    record = snapshot(vault).get(rel)
    if record is None:
        return
    from audit.core.bib import normalise_doi
    doi = normalise_doi(fields.get('DOI') or '')
    chosen = normalise_doi((record.get('chosen') or {}).get('doi') or '')
    if record['status'] == 'confirmed' and chosen and doi != chosen:
        raise ReviewConflict('Draft DOI differs from the confirmed identity. Correct and confirm the identity first.')
    for candidate in (record.get('proposal') or {}).get('candidates', []):
        if digest(candidate) in record.get('rejected', []) and doi and normalise_doi(candidate.get('doi') or '') == doi:
            raise ReviewConflict('This candidate was rejected. Reconsider it in the review workspace before registration.')
