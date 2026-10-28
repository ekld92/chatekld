"""Explicit, version-guarded single linked-file reparent repair.

Local API only. Durable dispatch evidence precedes PATCH; no automatic retry or
undo is granted. Parent and file checks are observations, not a shared Zotero
transaction. The existing runtime Local API permission remains mandatory.
"""
from __future__ import annotations

import json
import fcntl
from contextlib import contextmanager
import time
import urllib.error
import urllib.request

from accession import reconciliation as rec, review
from tagging import local_api, zotero_write as zw


def operations(settings):
    with rec.connection() as con:
        if con is None or not con.execute("SELECT 1 FROM sqlite_master WHERE name='reparents'").fetchone():
            return []
        values = [json.loads(r[0]) for r in con.execute('SELECT data FROM reparents')]
    return [v for v in values if v['scope'] == rec.scope(settings)]


def preview(settings, cfg, rel):
    transport = zw.resolve_transport(cfg)
    if transport.is_web:
        raise ValueError('Reparent repair requires the reviewed Desktop transport.')
    snapshot = rec.retain_history(rec.saved(settings), rec.observe(settings))
    row = rec.rows(settings, snapshot, only=rel)[0]
    live = [a for a in row['associations'] if not a['deleted']]
    if (len(live) != 1 or len(row['associations']) != 1 or len(row['mapping']['keys']) != 1
            or row['mapping']['exclude'] or row['mapping']['no_match']
            or live[0]['link_mode'] != 'linked_file' or live[0]['library'] != 1
            or row['moved_from'] or not row['physical']):
        raise ValueError('Repair needs one exact personal-library linked file, one mapping, and no exclusion or move ambiguity.')
    key = row['mapping']['keys'][0]
    from audit.core import bib
    if (sum(e.citation_key == key for e in bib.parse_bib(settings.master_bib)) != 1
            or not review.same_identity(review.identity(settings.master_bib), row['bibliography_identity'])):
        raise ValueError('Mapped bibliography key is missing, duplicated or changed.')
    parents = [i for i in snapshot['items'].values() if i['library'] == live[0]['library']
               and i['fields'].get('citationKey') == key and not i['deleted']
               and i['item_type'] not in ('attachment','note','annotation')]
    if len(parents) != 1 or parents[0]['key'] == live[0]['parent']:
        raise ValueError('No unique different native citation-key parent matches the saved mapping.')
    database, base = snapshot['database'], transport.base_url
    identities = [live[0]['attachment'], live[0]['parent'], parents[0]['key']]
    fetched = [zw.get_item(base, 'users/0', k, server_id=database) for k in identities]
    attachment, old_parent, desired = [x[0] for x in fetched]
    if (any(d.get('deleted') for d in (attachment,old_parent,desired))
            or [d.get('key') for d in (attachment, old_parent, desired)] != identities
            or attachment.get('itemType') != 'attachment'
            or desired.get('itemType') not in zw.PARENT_ITEM_TYPES
            or old_parent.get('itemType') not in zw.PARENT_ITEM_TYPES
            or attachment.get('parentItem') != live[0]['parent']
            or attachment.get('linkMode') != 'linked_file'
            or attachment.get('path') != str(settings.vault_root/rel)
            or desired.get('citationKey') != key):
        raise ValueError('Current API relationship differs from the snapshot; refresh and inspect again.')
    command = dict(scope=rec.scope(settings), pdf=rel, base=base, database=database,
                   attachment=identities[0], old_parent=identities[1], desired_parent=identities[2],
                   before=attachment, parents=[old_parent,desired], versions=[int(x[1]) for x in fetched],
                   physical=row['physical'], mapping=row['mapping'], review=row['review'],
                   bibliography_identity=row['bibliography_identity'])
    return dict(command=command, token=review.digest(command), executable=True,
                message='Review both complete parent records. Confirming changes only this attachment’s parent. Automatic undo is unavailable.')


@contextmanager
def operation_guard():
    """Serialize dispatch and completion across participating processes."""
    path = rec.store_path().with_suffix('.repair.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def apply(settings, cfg, rel, expected, *, confirm=False):
    with operation_guard():
        return _apply(settings, cfg, rel, expected, confirm=confirm)


def _apply(settings, cfg, rel, expected, *, confirm=False):
    if confirm is not True:
        raise ValueError('Explicit confirmation of the displayed parent records is required.')
    prepared = preview(settings, cfg, rel)
    if prepared['token'] != expected:
        raise ValueError('Repair evidence changed. Preview and confirm again.')
    command = prepared['command']
    identity = prepared['token']
    entry = dict(**command, id=identity, status='sent_or_unknown', at=time.time())
    with rec.connection(True) as con:
        con.execute('CREATE TABLE IF NOT EXISTS reparents(id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        for (raw,) in con.execute('SELECT data FROM reparents'):
            prior = json.loads(raw)
            if (prior['database'] == command['database'] and prior['attachment'] == command['attachment']
                    and prior['status'] == 'sent_or_unknown'):
                raise ValueError('An earlier repair outcome is uncertain. Inspect Zotero; automatic repeat is blocked.')
        con.execute('INSERT INTO reparents VALUES (?,?)', (identity, json.dumps(entry)))
    from audit.engine import bridge
    path = settings.vault_root/rel
    record = review.snapshot(settings.vault_root).get(rel)
    if (not review.same_identity(review.identity(path), command['physical'])
            or not review.same_identity(review.identity(settings.master_bib), command['bibliography_identity'])
            or rec.mapping_state(bridge._load_mapping_all(settings.mapping_file), rel) != command['mapping']
            or (review.public(record, path) if record else None) != command['review']):
        raise ValueError('PDF or CHERQAiN decision changed before repair dispatch; retained operation requires inspection.')
    for index, parent in enumerate((command['old_parent'], command['desired_parent'])):
        data, parent_version = zw.get_item(command['base'], 'users/0', parent, server_id=command['database'])
        if data != command['parents'][index] or int(parent_version) != command['versions'][index + 1]:
            raise ValueError('Parent changed before repair dispatch; inspect the retained operation.')
    version = local_api.Version(command['versions'][0], command['database'])
    request = urllib.request.Request(zw._url(command['base'],'users/0',command['attachment']),
        data=json.dumps(dict(parentItem=command['desired_parent'])).encode(), method='PATCH',
        headers=zw._headers(None, {'Content-Type':'application/json', **local_api.version_headers(version)}))
    try:
        with zw._urlopen(request, zw._TIMEOUT_S) as response:
            if response.status != 204:
                raise zw.ZoteroError('Unexpected repair response; inspect the retained operation.')
        # Preserve acknowledgement before logging or readback can fail.
        entry['status'] = 'acknowledged'
        with rec.connection(True) as con:
            con.execute('UPDATE reparents SET data=? WHERE id=?', (json.dumps(entry), identity))
    except urllib.error.HTTPError as exc:
        zw._raise_for_http(exc, 'PATCH')
    except (urllib.error.URLError, OSError) as exc:
        raise zw.ZoteroUnavailable('Repair response lost; automatic repeat is blocked.') from exc
    from core.utils import log_vault_write
    log_vault_write('pdf_reparent', command['attachment'], 'Explicitly reviewed parent change')
    return dict(ok=True, undo='manual', operation=entry,
                message='Parent change acknowledged. Refresh to inspect the current relationship. Automatic undo is unavailable.')


def inspect(settings, cfg, operation_id):
    """Observe the original target; absence never grants retry or undo."""
    operation = next((o for o in operations(settings) if o['id'] == operation_id), None)
    if operation is None:
        raise ValueError('Unknown repair operation for this workspace.')
    transport = zw.resolve_transport(cfg)
    if transport.is_web or transport.base_url != operation['base']:
        raise ValueError('Original repair transport differs from current configuration.')
    snapshot = rec.retain_history(rec.saved(settings), rec.observe(settings, allow_live_fallback=False))
    if snapshot['database'] != operation['database']:
        raise ValueError('Original repair database differs from the observed database.')
    row = rec.rows(settings, snapshot, only=operation['pdf'])[0]
    keys = [operation['attachment'], operation['old_parent'], operation['desired_parent']]
    fetched = [zw.get_item(operation['base'], 'users/0', key, server_id=operation['database']) for key in keys]
    data = [value[0] for value in fetched]
    if [value.get('key') for value in data] != keys:
        raise ValueError('Repair read returned a different item identity.')
    expected_attachment = {**operation['before'], 'parentItem': operation['desired_parent']}
    def content(value):
        return {k: v for k, v in value.items() if k not in ('version', 'dateModified')}
    live = [a for a in row['associations'] if not a['deleted']]
    eligible = (operation['status'] in ('sent_or_unknown', 'acknowledged')
                and content(data[0]) == content(expected_attachment)
                and all(content(a) == content(b) for a, b in zip(data[1:], operation['parents']))
                and len(live) == len(row['associations']) == 1
                and live[0]['attachment'] == operation['attachment']
                and live[0]['parent'] == operation['desired_parent']
                and all(review.same_identity(row[field], operation[field]) for field in
                        ('physical', 'bibliography_identity'))
                and all(row[field] == operation[field] for field in ('mapping', 'review')))
    evidence = dict(operation=operation, current=data, versions=[int(v[1]) for v in fetched],
                    physical=row['physical'], mapping=row['mapping'], review=row['review'],
                    bibliography_identity=row['bibliography_identity'], associations=row['associations'])
    return dict(evidence=evidence, token=review.digest(evidence), eligible=bool(eligible),
                read_only=True, automatic_retry=False, undo='manual',
                message='Current relationship inspected. Completion records this observation only; it does not establish who changed Zotero or authorize undo.')


def complete(settings, cfg, operation_id, token, *, confirm=False):
    if confirm is not True:
        raise ValueError('Separate confirmation of the inspected completion is required.')
    with operation_guard():
        inspected = inspect(settings, cfg, operation_id)
        if not inspected['eligible'] or inspected['token'] != token:
            raise ValueError('Repair completion evidence changed or is not eligible. Inspect again.')
        original = inspected['evidence']['operation']
        updated = {**original, 'status': 'completion_observed',
                   'original_outcome': original['status'],
                   'completion': dict(at=time.time(), evidence=inspected['evidence'], undo='manual')}
        with rec.connection(True) as con:
            raw = con.execute('SELECT data FROM reparents WHERE id=?', (operation_id,)).fetchone()
            if not raw or json.loads(raw[0]) != original:
                raise ValueError('Repair operation changed during completion.')
            con.execute('UPDATE reparents SET data=? WHERE id=?', (json.dumps(updated), operation_id))
        return dict(ok=True, operation=updated, automatic_retry=False, undo='manual')
