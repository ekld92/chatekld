"""Durable PDF observations. Reads never initialize stores or alter decisions.

A snapshot is complete or rejected. Item identity never depends on bibliography
keys. JSON mapping and SQLite evidence are coordinated by retained intents, not
by pretending their commits form one transaction.
"""
from __future__ import annotations

from audit.core.pdf_paths import pdf_files

from collections import Counter, defaultdict
from contextlib import contextmanager
import json
import hashlib
import os
import threading
from pathlib import Path
import sqlite3
import time

from accession import review
from audit.core import bib, zotero
from audit.engine import bridge


def store_path():
    from core.constants import BASE_DIR
    return Path(BASE_DIR) / 'audit' / 'reconciliation.sqlite3'


@contextmanager
def connection(write=False):
    path = store_path()
    if not write and not path.exists():
        yield None
        return
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path if write else path.resolve().as_uri() + '?mode=ro',
                          uri=not write, timeout=10)
    try:
        if write:
            con.execute('PRAGMA synchronous=FULL')
            con.executescript('''
                CREATE TABLE IF NOT EXISTS snapshots(scope TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS availability(scope TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS decisions(subject TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            ''')
            con.execute('BEGIN IMMEDIATE')
        yield con
        if write:
            con.commit()
            invalidate_saved_cache()
    finally:
        con.close()


def scope(settings):
    zotero_sqlite = getattr(settings, "zotero_sqlite", None)
    zotero_part = str(zotero_sqlite.resolve()) if zotero_sqlite is not None else ""
    return review.digest([str(settings.vault_root.resolve()), zotero_part])


_SAVED_CACHE_LOCK = threading.Lock()
_SAVED_CACHE: tuple[tuple, str, dict | None] | None = None


def invalidate_saved_cache() -> None:
    global _SAVED_CACHE
    with _SAVED_CACHE_LOCK:
        _SAVED_CACHE = None


def _copy_snapshot(data: dict | None) -> dict | None:
    if data is None:
        return None
    out = dict(data)
    for k in ("items", "physical", "relationship_history"):
        if k in out and isinstance(out[k], dict):
            out[k] = dict(out[k])
    if "changes" in out and isinstance(out["changes"], list):
        out["changes"] = list(out["changes"])
    return out


def _store_signature(path: Path) -> tuple | None:
    try:
        st = path.stat()
        real = os.path.realpath(path)
        wal = Path(str(path) + "-wal")
        try:
            wal_st = wal.stat()
            wal_sig = (wal_st.st_ino, wal_st.st_mtime_ns, wal_st.st_size)
        except OSError:
            wal_sig = None
        return (real, st.st_ino, st.st_mtime_ns, st.st_size, wal_sig)
    except OSError:
        return None


def saved(settings, con=None):
    global _SAVED_CACHE
    if con is not None:
        row = con.execute('SELECT data FROM snapshots WHERE scope=?', (scope(settings),)).fetchone()
        return json.loads(row[0]) if row else None

    target_scope = scope(settings)
    path = store_path()
    sig = _store_signature(path)
    if sig is not None:
        with _SAVED_CACHE_LOCK:
            if _SAVED_CACHE is not None and _SAVED_CACHE[0] == sig and _SAVED_CACHE[1] == target_scope:
                return _copy_snapshot(_SAVED_CACHE[2])

    with connection() as db_con:
        row = db_con.execute('SELECT data FROM snapshots WHERE scope=?', (target_scope,)).fetchone() if db_con else None
    parsed = json.loads(row[0]) if row else None

    post_sig = _store_signature(path)
    if sig is not None and post_sig == sig:
        with _SAVED_CACHE_LOCK:
            _SAVED_CACHE = (sig, target_scope, parsed)

    return _copy_snapshot(parsed)


def observe(settings, *, allow_live_fallback=True, include_child_counts=False,
            include_note_digests=False):
    """Complete metadata/relationship snapshot, including trash and standalone PDFs.

    Uses the existing copy/backup infrastructure. All required queries must
    succeed; no swallowed schema failures can turn an incomplete read into absence.
    """
    with zotero._open_snapshot(settings.zotero_sqlite, allow_live_fallback=allow_live_fallback) as con:
        identity = con.execute("SELECT value FROM settings WHERE setting='localAPI' AND key='serverID'").fetchone()
        if not identity or not identity[0]:
            raise ValueError('Zotero database identity unavailable; reconciliation is disabled.')
        items = {}
        for r in con.execute('''SELECT i.itemID, i.key, i.libraryID, t.typeName,
                EXISTS(SELECT 1 FROM deletedItems d WHERE d.itemID=i.itemID) AS deleted
                FROM items i JOIN itemTypes t ON t.itemTypeID=i.itemTypeID'''):
            items[r['itemID']] = dict(key=r['key'], library=r['libraryID'], item_type=r['typeName'],
                                      deleted=bool(r['deleted']), fields={}, creators=[], tags=[])
        for r in con.execute('''SELECT d.itemID, f.fieldName, v.value FROM itemData d
                JOIN fields f ON f.fieldID=d.fieldID JOIN itemDataValues v ON v.valueID=d.valueID'''):
            if r['itemID'] in items:
                items[r['itemID']]['fields'][r['fieldName']] = r['value']
        for r in con.execute('''SELECT c.firstName,c.lastName,c.fieldMode,t.creatorType,
                ic.itemID FROM itemCreators ic JOIN creators c ON c.creatorID=ic.creatorID
                JOIN creatorTypes t ON t.creatorTypeID=ic.creatorTypeID ORDER BY ic.orderIndex'''):
            creator = {'creatorType': r['creatorType']}
            creator.update({'name': r['lastName']} if r['fieldMode'] else
                           {'firstName': r['firstName'], 'lastName': r['lastName']})
            if r['itemID'] in items:
                items[r['itemID']]['creators'].append(creator)
        for r in con.execute('SELECT it.itemID,t.name FROM itemTags it JOIN tags t ON t.tagID=it.tagID'):
            if r['itemID'] in items:
                items[r['itemID']]['tags'].append(r['name'])
        try:
            att_rows = list(con.execute(
                'SELECT itemID,parentItemID,path,linkMode,contentType FROM itemAttachments'))
            have_content_type = True
        except sqlite3.OperationalError:
            att_rows = list(con.execute(
                'SELECT itemID,parentItemID,path,linkMode FROM itemAttachments'))
            have_content_type = False
        for r in att_rows:
            item = items[r['itemID']]
            item.update(parent=items.get(r['parentItemID'], {}).get('key'), path=r['path'],
                        link_mode=zotero.LINK_MODES.get(r['linkMode'], 'unknown'))
            if have_content_type:
                item['content_type'] = r['contentType']
        if include_note_digests:
            # Opt-in, so the default snapshot (which `changes()` diffs field by
            # field and `refresh()` persists) keeps its historical shape. The
            # import tracker needs to know a note CHANGED, not its text: a
            # digest of the stored HTML is enough and keeps the snapshot small.
            for r in con.execute('SELECT itemID, parentItemID, note FROM itemNotes'):
                if r['itemID'] in items:
                    items[r['itemID']]['parent'] = items.get(r['parentItemID'], {}).get('key')
                    items[r['itemID']]['note_sha256'] = hashlib.sha256(
                        (r['note'] or '').encode('utf-8')).hexdigest()
        if include_child_counts:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, label in (('itemAnnotations', 'annotations'), ('itemNotes', 'notes')):
                query = {
                    'itemAnnotations': 'SELECT parentItemID, COUNT(*) FROM itemAnnotations GROUP BY parentItemID',
                    'itemNotes': 'SELECT parentItemID, COUNT(*) FROM itemNotes GROUP BY parentItemID',
                }[table]
                counts = dict(con.execute(query)) if table in tables else None
                for item_id, item in items.items():
                    if item['item_type'] == 'attachment':
                        item.setdefault('child_counts', {})[label] = counts.get(item_id, 0) if counts is not None else None
        out = {f"{i['library']}:{i['key']}": i for i in items.values()}
        if len(out) != len(items):
            raise ValueError('Duplicate scoped Zotero keys.')
    return dict(database=str(identity[0]), checked_at=time.time(), complete=True, items=out)


def changes(previous, current):
    if not previous:
        return []
    if previous['database'] != current['database']:
        return [dict(kind='database_changed', attribution='Changed in Zotero')]
    old, new = previous['items'], current['items']
    result = []
    for key in sorted(old.keys() | new.keys()):
        a, b = old.get(key), new.get(key)
        if a == b:
            continue
        fields = sorted(k for k in (a or {}).keys() | (b or {}).keys() if (a or {}).get(k) != (b or {}).get(k))
        result.append(dict(identity=key, kind='added' if a is None else 'removed_or_merged' if b is None else 'changed',
                           fields=fields, before=a, current=b, attribution='Changed in Zotero'))
    return result


def availability(settings):
    with connection() as con:
        if con is None or not con.execute("SELECT 1 FROM sqlite_master WHERE name='availability'").fetchone():
            return None
        row = con.execute('SELECT data FROM availability WHERE scope=?', (scope(settings),)).fetchone()
    return json.loads(row[0]) if row else None


def _availability(settings, available, error=None):
    with connection(True) as con:
        con.execute('INSERT OR REPLACE INTO availability VALUES (?,?)',
                    (scope(settings), json.dumps(dict(available=available, error=error, attempted_at=time.time()))))


def retain_history(previous, current):
    """Carry durable barriers into fresh dispatch observations without writing."""
    current = dict(current)
    current['database_changed_from'] = (previous['database'] if previous and previous['database'] != current['database'] else (previous or {}).get('database_changed_from'))
    history = dict((previous or {}).get('relationship_history', {})) if previous and previous['database'] == current['database'] else {}
    for item in (previous or {}).get('items', {}).values():
        if previous['database'] == current['database'] and item['item_type'] == 'attachment' and item.get('path'):
            history[review.digest([item['library'], item['key'], item['path']])] = item
    current['relationship_history'] = history
    current['physical'] = dict((previous or {}).get('physical', {}))
    return current


def refresh(settings):
    invalidate_saved_cache()
    previous = saved(settings)
    try:
        current = retain_history(previous, observe(settings))
        delta = changes(previous, current)
        current['changes'] = delta
        for row in rows(settings, current):
            if row['physical']:
                current['physical'].setdefault(row['pdf']['rel'], row['physical'])
        with connection(True) as con:
            con.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?)', (scope(settings), json.dumps(current)))
            con.execute('INSERT OR REPLACE INTO availability VALUES (?,?)',
                        (scope(settings), json.dumps(dict(available=True, error=None, attempted_at=time.time()))))
            invalidate_saved_cache()
    except (OSError, sqlite3.Error, ValueError, review.ReviewConflict) as exc:
        _availability(settings, False, str(exc))
        return dict(available=False, error=str(exc), snapshot=previous, changes=[])
    return dict(available=True, snapshot=current, changes=delta)


_bib_lock = threading.Lock()
_bib_cached = None


def bibliography_snapshot(path):
    """Parse captured bytes once per content hash; tolerate metadata-only rewrites.

    A changed content hash retries twice. Dispatch still binds the resulting
    full identity, so an earlier proposal cannot authorize a different file.
    """
    global _bib_cached
    for _ in range(3):
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        with _bib_lock:
            cached = _bib_cached
        entries = cached[1] if cached and cached[0] == digest else bib.parse_bib_text(raw.decode('utf-8', errors='replace'))
        current = review.identity(path)
        if current['sha256'] == digest:
            with _bib_lock:
                _bib_cached = (digest, entries)
            return entries, current
    raise review.ReviewConflict('Bibliography is changing. Wait for its export to finish, then refresh reconciliation.')


def mapping_state(mapping, rel):
    return dict(keys=sorted(k for k, paths in mapping.matches.items() if rel in paths),
                pending=rel in mapping.pending, no_match=rel in mapping.no_match,
                exclude=sorted(mapping.exclude.get(rel, [])))


def attachment_path(item, settings, snapshot=None):
    raw = item.get('path') or ''
    if raw.startswith('storage:'):
        return settings.zotero_storage / item['key'] / raw[8:]
    # A relative linked path needs Zotero's configured attachment base. Do
    # not guess it from the vault or filename.
    if raw.startswith('attachments:'):
        from accession import attachment_base
        return attachment_base.resolve(raw, (snapshot or {}).get('attachment_base'),
            database=(snapshot or {}).get('database'), database_path=settings.zotero_sqlite)
    if not Path(raw).is_absolute():
        return None
    return Path(raw)


def decisions():
    with connection() as con:
        return {r[0]: json.loads(r[1]) for r in con.execute('SELECT subject,data FROM decisions')} if con else {}


def rows(settings, snapshot, *, available=True, only=None):
    """Pure join: PDF identity, review, retained operations, Zotero, bib, mapping."""
    from zotcreate import attempts
    from accession.vocabulary import STATE_LABELS
    if settings.mapping_file.exists():
        raw_mapping = json.loads(settings.mapping_file.read_text())
        if not isinstance(raw_mapping, dict) or not isinstance(raw_mapping.get('matches', {}), dict):
            raise ValueError('Mapping store is malformed; no reconciliation applied.')
    mapping = bridge._load_mapping_all(settings.mapping_file)
    reviews = review.snapshot(settings.vault_root)
    bibliography, bibliography_identity = bibliography_snapshot(settings.master_bib)
    entry_counts = Counter(e.citation_key for e in bibliography)
    doi_entries = defaultdict(set)
    for entry in bibliography:
        if entry.doi:
            doi_entries[entry.doi].add(entry.citation_key)
    entries = {key for key, count in entry_counts.items() if count == 1}
    accepted = decisions()
    intents = mapping_intents(settings)
    from accession import reparent
    repairs = reparent.operations(settings)
    items = (snapshot or {}).get('items', {})
    database = (snapshot or {}).get('database')
    key_counts = Counter(i['fields'].get('citationKey') for i in items.values() if not i['deleted'])
    from accession import pdf_management
    retired = {step['rel']: op['id'] for op in pdf_management.operations(settings)
               for step in op.get('steps', []) if step['action'] == 'delete'
               and step['status'] in ('move_prepared', 'moved', 'complete')
               and not (settings.vault_root / step['rel']).exists()}
    paths = set(pdf_files(settings.biblio_articles_dir))
    paths.update(settings.vault_root / rel for rel in retired)
    paths.update(settings.vault_root / rel for rel in mapping.pending | mapping.no_match)
    paths.update(settings.vault_root / rel for rel in reviews)
    for rels in mapping.matches.values():
        paths.update(settings.vault_root / rel for rel in rels)
    # Parent evidence index, built ONCE (2026-09-17 review, C2): the per-PDF
    # scan over every item was O(pdfs × items), ~91 M iterations on the live
    # library. Candidates are gathered by the three join keys and returned in
    # the observation's own item order, so the result is identical.
    parent_order, by_identity, by_citation_key, by_doi = {}, {}, defaultdict(list), defaultdict(list)
    for position, i in enumerate(items.values()):
        if i['deleted'] or i['item_type'] in ('attachment', 'note'):
            continue
        parent_order[id(i)] = position
        by_identity[(i['library'], i['key'])] = i
        if i['fields'].get('citationKey'):
            by_citation_key[i['fields']['citationKey']].append(i)
        doi = bib.normalise_doi(i['fields'].get('DOI') or '')
        if doi:
            by_doi[doi].append(i)
    result = []
    if only is not None:
        paths = {settings.vault_root / rel for rel in ([only] if isinstance(only, str) else only)}
    historical = []
    for old_item in (snapshot or {}).get('relationship_history', {}).values():
        live_item = items.get(f"{old_item['library']}:{old_item['key']}")
        if live_item is None or live_item.get('path') != old_item.get('path'):
            historical.append({**old_item, 'deleted': True})
    exact_index, copy_index, copy_hashes = defaultdict(list), defaultdict(list), {}
    for item in [*items.values(), *historical]:
        if item['item_type'] != 'attachment':
            continue
        linked = attachment_path(item, settings, snapshot)
        if linked is None:
            continue
        exact_index[str(linked.resolve())].append(item)
        if item.get('link_mode') in zotero.ZOTERO_OWNED_MODES:
            try:
                copy_index[linked.stat().st_size].append(item)
            except OSError:
                pass
    for path in sorted(paths):
        if not path.resolve().is_relative_to(settings.biblio_articles_dir.resolve()):
            continue
        rel = path.relative_to(settings.vault_root).as_posix()
        state = mapping_state(mapping, rel)
        record = reviews.get(rel)
        try:
            memo = (snapshot or {}).get('physical', {}).get(rel)
            fingerprint = review.fingerprint(path)
            physical = memo if memo and review.same_identity({k:v for k,v in memo.items() if k != 'sha256'}, fingerprint) else review.identity(path)
            identity_error = None
        except (OSError, review.ReviewConflict) as exc:
            physical, identity_error = None, str(exc)
        _, attempt_stage, attempt = attempts.lookup(settings.vault_root, physical['sha256']) if physical else (None, 'fresh', None)
        associations, copies = [], []
        candidates = exact_index[str(path.resolve())] + (copy_index[physical['size']] if physical else [])
        seen = set()
        for item in candidates:
            item_identity = review.digest(item)
            if item_identity in seen:
                continue
            seen.add(item_identity)
            linked = attachment_path(item, settings, snapshot)
            if linked is None or physical is None:
                continue
            exact = linked.resolve() == path.resolve()
            # Same bytes elsewhere is evidence of a copy, never this physical file.
            same = False
            if not exact and item.get('link_mode') in zotero.ZOTERO_OWNED_MODES:
                try:
                    if str(linked) not in copy_hashes:
                        copy_hashes[str(linked)] = review.identity(linked)['sha256']
                    same = copy_hashes[str(linked)] == physical['sha256']
                except (OSError, review.ReviewConflict):
                    pass
            if not exact and not same:
                continue
            parent = items.get(f"{item['library']}:{item.get('parent')}")
            assoc = dict(database=database, library=item['library'], attachment=item['key'],
                         parent=item.get('parent'), path=item.get('path'), link_mode=item.get('link_mode'),
                         deleted=item['deleted'] or bool(parent and parent['deleted']),
                         metadata=(parent or {}).get('fields', {}),
                         creators=(parent or {}).get('creators', []), parent_type=(parent or {}).get('item_type'))
            (associations if exact else copies).append(assoc)
        associations.sort(key=lambda a:(a['library'],a['attachment'],a['path'] or ''))
        live = [a for a in associations if not a['deleted'] and a['parent']]
        candidate_keys = set()
        bibliography_ambiguous = False
        for association in live:
            native_key = association['metadata'].get('citationKey')
            if native_key:
                bibliography_ambiguous |= key_counts[native_key] > 1 or entry_counts[native_key] > 1
                if native_key in entries and key_counts[native_key] == 1:
                    candidate_keys.add(native_key)
            else:
                doi_keys = doi_entries.get(bib.normalise_doi(association['metadata'].get('DOI') or ''), set())
                bibliography_ambiguous |= any(entry_counts[k] != 1 for k in doi_keys)
                candidate_keys.update(doi_keys & entries)
        bibliography_ambiguous |= len(candidate_keys) > 1
        keys = sorted(candidate_keys)
        parents = {(a['library'], a['parent']) for a in live}
        observed_parents = parents | {(a['library'], a['parent']) for a in copies if not a['deleted']}
        candidate_doi = bib.normalise_doi(((record or {}).get('chosen') or {}).get('doi') or '')
        # Parent evidence is independent of attachment/path evidence. Citation
        # keys are attributes, never native identity or permission to create.
        matched = {id(i): i for i in (
            [by_identity[p] for p in observed_parents if p in by_identity]
            + [i for k in state['keys'] for i in by_citation_key.get(k, [])]
            + (by_doi.get(candidate_doi, []) if candidate_doi else []))}
        parent_candidates = [dict(database=database, library=i['library'],
                                  key=i['key'], metadata=i['fields'])
                             for i in sorted(matched.values(), key=lambda i: parent_order[id(i)])]
        parent_state = ('unavailable' if not available else 'observed' if len(parent_candidates) == 1
                        else 'ambiguous' if parent_candidates else 'no_match_in_observation')

        relationship = [{k: a[k] for k in ('database','library','attachment','parent','path','link_mode','deleted')} for a in associations]
        relevant_metadata = [{k:a['metadata'].get(k) for k in ('DOI','citationKey')} for a in associations]
        review_evidence = {k:(record or {}).get(k) for k in ('chosen','rejected')}
        evidence = review.digest(dict(database=database, physical=physical, relationship=relationship, metadata=relevant_metadata, mapping=state, review=review_evidence))
        subject = review.digest([str(settings.vault_root.resolve()), rel])
        decision = accepted.get(subject)
        prior_physical = (snapshot or {}).get('physical', {}).get(rel)
        moved_from = [old_rel for old_rel, old_physical in (snapshot or {}).get('physical', {}).items()
                      if physical and old_rel != rel and old_physical.get('sha256') == physical['sha256']
                      and not (settings.vault_root / old_rel).exists()]
        moved_decisions = {old_rel:mapping_state(mapping, old_rel) for old_rel in moved_from}
        moved_conflict = any(m['keys'] or m['no_match'] or m['exclude'] for m in moved_decisions.values())
        replaced = bool(prior_physical and physical and any(prior_physical.get(k) != physical.get(k) for k in ('sha256','ino','birth')))
        confirmed_doi = bib.normalise_doi(((record or {}).get('chosen') or {}).get('doi') or '')
        identification_conflict = bool(confirmed_doi and any(
            bib.normalise_doi(a['metadata'].get('DOI') or '') not in ('', confirmed_doi) for a in live))
        conflict = identification_conflict or moved_conflict or bibliography_ambiguous or bool((snapshot or {}).get('database_changed_from')) or replaced or bool(associations and (len(parents) != 1 or len(live) != 1 or state['no_match'] or state['exclude']
                        or (state['keys'] and state['keys'] != keys)))
        old = decision and decision.get('evidence') != evidence
        if rel in retired:
            category = 'Retired'
        elif identity_error or not available:
            category = 'Recovery'
        elif conflict:
            category = 'Accepted differences' if decision and not old and decision['action'] == 'keep' else 'Discrepancies'
        elif len(live) == 1:
            category = 'Linked' if keys and state['keys'] == keys else 'Needs review' if keys else 'Awaiting bibliography'
        elif attempt_stage != 'fresh':
            category = 'Recovery'
        elif state['no_match']:
            # Only "not for Zotero" excludes a PDF from registration. A detach
            # exclusion ("not THAT record") does not (2026-09-18).
            category = 'Excluded'
        elif state['keys'] or parent_candidates:
            category = 'Needs review'
        elif record and record['status'] == 'confirmed' and review.public(record, path)['identity_state'] == 'verified':
            category = 'Ready to register'
        elif record and record.get('proposal'):
            category = 'Needs review'
        else:
            category = 'Needs identification'
        result.append(dict(pdf=dict(rel=rel,name=path.name,abs=str(path)), exists=physical is not None,
                           physical=physical, state=category, state_label=STATE_LABELS.get(category, category), retirement=retired.get(rel), mapping=state, mapped_key=state['keys'][0] if len(state['keys']) == 1 else None,
                           review=review.public(record, path) if record else None, resolution=(record or {}).get('proposal'),
                           associations=associations, imported_copies=copies, bibliography_keys=keys, bibliography_identity=bibliography_identity,
                           registration_state='registered' if len(live) == 1 else 'ambiguous' if live else 'unverified',
                           parent_state=parent_state, parents=parent_candidates,
                           attachment_state='unavailable' if not available else 'observed' if len(live) == 1 else 'ambiguous' if live else 'none_observed',
                           bibliography_state='available' if keys or (state['keys'] and set(state['keys']) <= entries) else 'awaiting_export' if parent_candidates else 'unknown',
                           migration_review_required=bool(attempt and not attempt.get('server_id')),
                           moved_from=moved_decisions,
                           bookkeeping=[i for i in intents if i['pdf'] == rel],
                           external_repairs=[i for i in repairs if i['pdf'] == rel],
                           attempt=dict(stage=attempt_stage, retained=attempt), evidence=evidence, subject=subject,
                           accepted=decision, reopened='PDF, mapping or Zotero relationship changed since acceptance.' if old and conflict else None,
                           reason=identity_error or ('Saved CHERQAiN decision differs from the observed Zotero relationship.' if conflict else 'Registered in Zotero; bibliography is available. Complete the CHERQAiN mapping.' if len(live) == 1 and keys and not state['keys'] else None),
                           can_create=category in ('Ready to register','Needs identification','Needs review') and not parent_candidates and not associations and not copies and not state['keys'] and not state['no_match'] and attempt_stage == 'fresh'))
    return result


def accept(row, action):
    if action not in ('keep', 'later', 'revoke'):
        raise ValueError('Unknown discrepancy decision.')
    data = dict(action=action, evidence=row['evidence'], relationship=row['associations'],
                mapping=row['mapping'], physical=row['physical'], at=time.time())
    with connection(True) as con:
        con.execute('INSERT OR REPLACE INTO decisions VALUES (?,?)', (row['subject'], json.dumps(data)))


def use_association(settings, row, *, validate_evidence=None):
    """Retained intent + guarded JSON write + completion. Retry is idempotent.

    Reobserve external and local evidence inside the mapping writer. This is
    recoverable coordination, not a transaction with external applications.
    """
    if len(row['bibliography_keys']) != 1 or len([a for a in row['associations'] if not a['deleted']]) != 1:
        raise ValueError('One live Zotero association and one bibliography key are required.')
    key = row['bibliography_keys'][0]
    identity = review.digest([row['subject'], row['evidence'], key])
    intent = dict(id=identity, scope=scope(settings), pdf=row['pdf']['rel'], evidence=row['evidence'], key=key, status='prepared',
                  physical=row['physical'], associations=row['associations'],
                  bibliography_identity=row.get('bibliography_identity'), review=row['review'],
                  attempt=row['attempt'], accepted=row['accepted'])
    with connection(True) as con:
        con.execute('INSERT OR IGNORE INTO intents VALUES (?,?)', (identity, json.dumps(intent)))
    bibliography = row.get('bibliography_identity')
    if not bibliography or not review.same_identity(review.identity(settings.master_bib), bibliography):
        raise ValueError('Bibliography changed since inspection. Refresh reconciliation.')
    def validate():
        if validate_evidence:
            validate_evidence()
        if not review.same_identity(review.identity(settings.master_bib), bibliography):
            raise ValueError('Bibliography changed during reconciliation.')
        current = rows(settings, retain_history(saved(settings), observe(settings, allow_live_fallback=False)),
                       only=row['pdf']['rel'])[0]
        for field in ('physical', 'associations', 'bibliography_keys', 'bibliography_identity',
                      'review', 'attempt', 'accepted', 'evidence'):
            if current[field] != row[field]:
                raise ValueError(f'Reconciliation evidence changed before mapping: {field}')
        if validate_evidence:
            validate_evidence()
    bridge.add_match(settings.mapping_file, settings.vault_root / row['pdf']['rel'], key, settings.vault_root,
                     source='zotero_reconciliation', require_hash=True, expected_state=row['mapping'],
                     expected_sha256=row['physical']['sha256'],
                     expected_fingerprint={k:v for k,v in row['physical'].items() if k != 'sha256'}, validate_evidence=validate)
    intent['status'] = 'complete'
    with connection(True) as con:
        con.execute('UPDATE intents SET data=? WHERE id=?', (json.dumps(intent), identity))


def mapping_intents(settings):
    with connection() as con:
        values = [json.loads(r[0]) for r in con.execute('SELECT data FROM intents')] if con else []
    return [v for v in values if v.get('scope') == scope(settings)]


def finish_mapping_intents(settings, current_rows):
    """Finish a lost SQLite completion only after observing the intended JSON.

    No JSON replay, no external write, and no creation barrier removal.
    """
    by_rel = {r['pdf']['rel']:r for r in current_rows}
    for intent in mapping_intents(settings):
        if intent['status'] != 'prepared':
            continue
        row = by_rel.get(intent['pdf'])
        if not row or not row['physical']:
            continue
        if (row['mapping']['keys'] != [intent['key']] or row['bibliography_keys'] != [intent['key']]
                or row['mapping']['exclude'] or row['mapping']['no_match'] or row['mapping']['pending']
                or not review.same_identity(row['physical'], intent['physical']) or row['associations'] != intent['associations']
                or any(field not in intent or row[field] != intent[field]
                       for field in ('bibliography_identity', 'review', 'attempt', 'accepted'))):
            continue
        intent['status'] = 'complete'
        intent['completion_observed_at'] = time.time()
        with connection(True) as con:
            con.execute('UPDATE intents SET data=? WHERE id=?', (json.dumps(intent), intent['id']))
