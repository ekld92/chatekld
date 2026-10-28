"""Entry PDF management with durable, individually recoverable local removals.

No reads refresh stores. Physical removal is admitted only after every Zotero
reference has been removed explicitly. We never delete Zotero-owned storage.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import time
import uuid
from urllib.parse import unquote

from accession import reconciliation as rec, review, reparent
from audit.engine import bridge


class VaultBusy(Exception):
    """The vault op-lock is held (indexing or another vault writer)."""


@contextlib.contextmanager
def _vault_hold():
    """Hold the obsidian op-lock around a physical move or restore.

    2026-09-17 review, B5: these renames under ``biblio_articles/`` ran under
    the accession flock only, so an indexing run could read a PDF mid-move.
    Lock order is cleanup apply/restore: accession flock → obsidian op-lock.
    Zotero network steps (link, trash) never run inside this hold."""
    from rag.vault import obsidian_manager
    epoch = obsidian_manager.try_acquire_lock(ttl=600)
    if not epoch:
        raise VaultBusy('Indexing or another vault write is in progress; nothing was moved. Try again shortly.')
    try:
        yield lambda: obsidian_manager.heartbeat(epoch)
    finally:
        obsidian_manager.release_lock(epoch)


def _lib_path(cfg, parent):
    """Library segment from the journalled writers — never a hard-coded ``users/0``."""
    from types import SimpleNamespace
    from tagging.writers import _zotero_lib_path, _zotero_transport
    zot = SimpleNamespace(
        library_type=(parent or {}).get('library_type') or 'user',
        group_id=(parent or {}).get('group_id'),
        item_key=(parent or {}).get('key'),
    )
    return _zotero_lib_path(_zotero_transport(cfg), zot)


def _journal_attachment_trash(settings, cfg, prepared):
    from tagging import journal as tagging_journal
    from tagging import writers
    manifest = tagging_journal.load_for_ops(settings.vault_root, cfg)
    writers.zotero_attachment_journal(prepared=prepared, manifest=manifest)
    tagging_journal.save(settings.vault_root, cfg, manifest)


def _journal_attachment_create(settings, cfg, prepared, citation_key, att_key, version):
    from tagging import journal as tagging_journal
    from tagging import writers
    manifest = tagging_journal.load_for_ops(settings.vault_root, cfg)
    writers.zotero_attachment_create_journal(
        prepared=prepared, citation_key=citation_key, att_key=att_key,
        version=version, manifest=manifest)
    tagging_journal.save(settings.vault_root, cfg, manifest)


def _trash_prepared(cfg, plan):
    """The journalled trash writer's commit dict, built from a previewed plan."""
    from tagging.writers import _zotero_base, _zotero_transport
    transport = _zotero_transport(cfg)
    before = plan.get('before') or {}
    return {"commit": {
        "base": _zotero_base(cfg), "transport": transport,
        "api_key": transport.api_key if transport.is_web else None,
        "lib_path": plan['lib_path'], "item_key": plan['attachment'],
        "version": plan['version'], "link_mode": before.get('linkMode'),
        "parent_item": before.get('parentItem'),
        "filename": before.get('filename') or before.get('path') or '',
        "title": before.get('title') or '',
    }}


def recovery_root():
    from core.constants import BASE_DIR
    return Path(BASE_DIR).resolve() / 'audit' / 'pdf_recovery'


def _recovery_device():
    """st_dev of the recovery root, or of its nearest existing ancestor."""
    probe = recovery_root()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe.stat().st_dev


def _other_volume(path):
    """True when a recoverable move of *path* would cross volumes (a rename
    across devices fails, and a copy would not be the recorded identity)."""
    return _recovery_device() != path.stat().st_dev


def _path(settings, rel, *, exists=True):
    if not isinstance(rel, str) or not rel or Path(rel).is_absolute() or '..' in Path(rel).parts:
        raise ValueError('Choose a vault-relative PDF.')
    path = settings.vault_root / rel
    root = settings.biblio_articles_dir.resolve()
    if not path.resolve().is_relative_to(root) or path.suffix.lower() != '.pdf':
        raise ValueError('PDF must be inside the bibliography folder.')
    # Never operate through a symlink BELOW the vault root, directory aliases
    # included. The vault root itself, and anything above it, may legitimately
    # be reached through a symlinked ancestor (a synced folder); that is the
    # user's layout, not an alias inside the managed tree (2026-09-17, F5).
    below = [path]
    for parent in path.parents:
        if parent == settings.vault_root:
            break
        below.append(parent)
    else:
        raise ValueError('PDF must be inside the vault.')
    if any(p.is_symlink() for p in below):
        raise ValueError('Symlink paths require manual review.')
    if exists and (not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError('Missing, nonregular or multiply linked PDF; manual review required.')
    return path


def operations(settings):
    with rec.connection() as con:
        if con is None or not con.execute("SELECT 1 FROM sqlite_master WHERE name='pdf_operations'").fetchone():
            return []
        values = [json.loads(r[0]) for r in con.execute('SELECT data FROM pdf_operations')]
    return [r for r in values if r['scope'] == rec.scope(settings)]


SETTLED = ('complete', 'restored', 'failed')
#: The survivor fields a plan digests: identity, not derived comparison facts.
STABLE_SURVIVOR_KEYS = ('rel', 'physical', 'error', 'mapping', 'attachments')


def require_settled(settings, key):
    # Every retained operation on this entry must have a settled outcome before
    # cleanup: an uncertain link, trash or removal blocks until it is inspected
    # or restored. Cleanup never passes this on its own. ``failed`` is settled:
    # it is recorded only when nothing was created (a refused POST, or a
    # reserved key Zotero proves absent).
    blocking = [o for o in operations(settings) if o['key'] == key and o['status'] not in SETTLED]
    if blocking:
        op = blocking[0]
        raise review.ReviewConflict(
            f"Retained PDF operation {op['id']} ({op.get('kind', 'cleanup')}, {op['status']}) has an uncertain outcome. "
            'Inspect it first, then complete or restore it; until then this entry is blocked.')


def _log_op(op):
    """One traceable line per operation outcome: the PDF's vault-relative path
    as ``path``, the operation id and state in ``detail`` (2026-09-17, F4)."""
    from core.utils import log_vault_write
    plan = op.get('plan') or {}
    rels = [step['rel'] for step in op.get('steps') or []] or [plan.get('rel') or (plan.get('before') or {}).get('path') or op.get('key')]
    log_vault_write('pdf_management_' + op.get('kind', 'cleanup'), ', '.join(str(r) for r in rels),
                    f"operation {op['id']}: {op['status']}")


def _save(op):
    with rec.connection(True) as con:
        con.execute('CREATE TABLE IF NOT EXISTS pdf_operations(id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        con.execute('INSERT OR REPLACE INTO pdf_operations VALUES (?,?)', (op['id'], json.dumps(op)))


def _mapping(settings, rel):
    if settings.mapping_file.exists():
        raw = json.loads(settings.mapping_file.read_text())
        if not isinstance(raw, dict) or not isinstance(raw.get('matches', {}), dict):
            raise ValueError('Mapping store is malformed; repair it before PDF management.')
    m = bridge._load_mapping_all(settings.mapping_file)
    return {**rec.mapping_state(m, rel), 'hash': m.hashes.get(rel), 'source': m.sources.get(rel)}


def _set_mapping(settings, rel, expected, desired):
    # Caller holds the cross-process mapping guard.
    if _mapping(settings, rel) != expected:
        raise review.ReviewConflict('Mapping changed. Inspect the retained operation before continuing.')
    m = bridge._load_mapping_all(settings.mapping_file)
    for key in list(m.matches):
        m.matches[key] = [p for p in m.matches[key] if p != rel]
        if not m.matches[key]:
            del m.matches[key]
    for key in desired['keys']:
        m.matches.setdefault(key, []).append(rel)
    for name in ('pending', 'no_match'):
        values = getattr(m, name)
        values.discard(rel)
        if desired[name]:
            values.add(rel)
    for name, value in [('exclude', desired['exclude']), ('hashes', desired['hash']), ('sources', desired['source'])]:
        values = getattr(m, name)
        values.pop(rel, None)
        if value:
            values[rel] = value
    bridge.save_mapping(settings.mapping_file, m.matches, m.no_match, m.hashes, m.sources,
                        pending=m.pending, exclude=m.exclude)

def note_index(settings):
    """ONE walk of the vault's Markdown notes: ``[(rel, folded_text | None)]``.

    2026-09-17 review, B4: the old per-row/per-step ``rglob`` + strict UTF-8
    ``read_text`` crashed the whole panel on one non-UTF-8 note and re-walked
    the vault for every row and step. Bytes are decoded with ``errors=
    'replace'`` — a reference check is a substring test on the PDF basename, so
    a replacement character cannot hide an ASCII basename, and a spurious
    match only makes removal more cautious. The walk never follows directory
    symlinks; a symlinked or unreadable note is recorded with ``None`` text,
    which :func:`_notes` reports as an unverifiable dependency rather than
    proving absence. (``accession/dependent_links``' reader was not reused: it
    is strict UTF-8 and bounded to scan-known notes, not a vault walk.)
    """
    root = settings.vault_root
    index = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in ('.git', '.trash'))
        for name in sorted(files):
            if not name.endswith('.md'):
                continue
            path = Path(directory) / name
            rel = path.relative_to(root).as_posix()
            try:
                if path.is_symlink():
                    raise OSError('symlinked note')
                text = unquote(path.read_bytes().decode('utf-8', errors='replace')).casefold()
            except OSError:
                text = None
            index.append((rel, text))
    return index


def _notes(settings, path, index=None):
    refs = []
    name = path.name.casefold()
    for rel, text in (note_index(settings) if index is None else index):
        # Conservative: ambiguous basename references also block removal, and
        # a note that could not be read cannot prove it does not refer here.
        if text is None:
            refs.append(rel + ' (unreadable; cannot verify)')
        elif name in text:
            refs.append(rel)
    return sorted(refs)


def _references(settings, snapshot, path):
    refs, unresolved = [], []
    for item in snapshot['items'].values():
        if item['item_type'] != 'attachment' or item.get('deleted'):
            continue
        linked = rec.attachment_path(item, settings, snapshot)
        if linked is None:
            # A remote URL is not an unresolved local file.
            if item.get('link_mode') not in ('linked_url', 'imported_url'):
                unresolved.append(item['key'])
            continue
        if linked.resolve() == path.resolve():
            refs.append(item)
    return refs, unresolved


def entry(settings, key, parent_key=None, *, notes=None):
    snapshot = rec.observe(settings, allow_live_fallback=False, include_child_counts=True)
    bibliography, revision = rec.bibliography_snapshot(settings.master_bib)
    if sum(e.citation_key == key for e in bibliography) != 1:
        raise ValueError('Choose one unique current bibliography entry.')
    parents = [i for i in snapshot['items'].values() if not i['deleted'] and i['item_type'] not in ('attachment','note','annotation')
               and ((parent_key and i['key'] == parent_key) or (not parent_key and i['fields'].get('citationKey') == key))]
    if len(parents) != 1 or parents[0]['library'] != 1:
        raise ValueError('One current personal-library Zotero entry is required.')
    parent = parents[0]
    if parent['fields'].get('citationKey') not in (None, '', key):
        raise review.ReviewConflict('The Zotero citation key changed. Run a fresh scan.')
    attached = [i for i in snapshot['items'].values() if i['item_type'] == 'attachment' and not i['deleted']
                and i.get('parent') == parent['key'] and i['library'] == parent['library']]
    paths = set(bridge._load_mapping_all(settings.mapping_file).matches.get(key, []))
    attachments = []
    for item in attached:
        path = rec.attachment_path(item, settings, snapshot)
        rel = path.relative_to(settings.vault_root).as_posix() if path and path.is_relative_to(settings.vault_root) else None
        if rel and path.suffix.lower() == '.pdf':
            paths.add(rel)
        attachments.append(dict(key=item['key'], path=str(path) if path else item.get('path'), rel=rel,
                                mode=item.get('link_mode'), title=item['fields'].get('title'), child_counts=item.get('child_counts', {})))
    rows = []
    if notes is None and paths:
        notes = note_index(settings)
    for rel in sorted(paths):
        try:
            path = _path(settings, rel)
            physical = review.identity(path)
            error = None
        except (OSError, ValueError, review.ReviewConflict) as exc:
            path, physical, error = None, None, str(exc)
        mapping = _mapping(settings, rel)
        own = [a for a in attachments if a['rel'] == rel]
        embedded = None
        if physical is not None and path is not None:
            from audit.core.pdf_annotations import count_annotations
            try:
                embedded = count_annotations(path)
                if embedded < 0:  # the counter's "could not open" sentinel: unknown, not a count
                    embedded = None
            except Exception:  # noqa: BLE001 — an unreadable PDF is reported, not raised
                embedded = None
        rows.append(dict(rel=rel, physical=physical, error=error, mapping=mapping, attachments=own,
                         native_annotations=sum(int((a.get('child_counts') or {}).get('annotations') or 0) for a in own),
                         embedded_annotations=embedded,
                         note_dependencies=_notes(settings, path, notes) if path is not None else [],
                         other_keys=[k for k in mapping['keys'] if k != key]))
    # Phase 7 (2026-09-16): the comparison a removal decision needs, computed
    # once here so the UI, the preview and the CLI say the same thing.
    from accession import duplicates
    comparison = duplicates.compare([dict(rel=r['rel'], sha256=(r['physical'] or {}).get('sha256'),
                                          native_annotations=r['native_annotations'],
                                          embedded_annotations=r['embedded_annotations'],
                                          note_dependencies=r['note_dependencies'], mapping_keys=r['mapping']['keys'],
                                          attachment_keys=[a['key'] for a in r['attachments']]) for r in rows])
    for row, compared in zip(rows, comparison):
        row['comparison'] = compared
    return dict(key=key, parent=parent, database=snapshot['database'], bibliography=revision,
                rows=rows, attachments=attachments, operations=[o for o in operations(settings) if o['key'] == key],
                removal_rule='A file is removed only when it carries no annotations and no dependencies, or after you acknowledge each explicitly; the kept copy is your choice.'), snapshot


def preview(settings, key, survivor, actions, parent_key=None, *, acknowledge_annotations=False, notes=None):
    if not isinstance(actions, dict) or not actions or len(actions) > 20:
        raise ValueError('Select between one and twenty PDF actions.')
    notes = note_index(settings) if notes is None else notes
    data, snapshot = entry(settings, key, parent_key, notes=notes)
    by_rel = {r['rel']: r for r in data['rows']}
    if survivor not in by_rel or not by_rel[survivor]['physical'] or not by_rel[survivor]['attachments']:
        raise ValueError('The survivor must be a readable PDF already linked to this Zotero entry.')
    if survivor in actions:
        raise ValueError('The survivor cannot also be removed.')
    steps, blockers = [], []
    for rel, action in sorted(actions.items()):
        if action not in ('unmap', 'delete') or rel not in by_rel:
            raise ValueError('Select a listed PDF and a supported action.')
        row = by_rel[rel]
        if not row['physical']:
            raise ValueError('Selected PDF is unreadable; manual review required.')
        path = _path(settings, rel)
        references, unresolved = _references(settings, snapshot, path)
        notes_for = _notes(settings, path, notes) if action == 'delete' else []
        if action == 'delete':
            if references:
                blockers.append(f'{rel}: remove its Zotero attachment(s) first; the file is still linked.')
            if unresolved:
                blockers.append(f'{rel}: unresolved local Zotero paths prevent proving that this file is unused.')
            if notes_for:
                blockers.append(f'{rel}: referenced by vault notes; repair those references first.')
            if set(row['mapping']['keys']) - {key}:
                blockers.append(f'{rel}: another bibliography entry uses this file.')
            if _other_volume(path):
                # Known before the confirm (2026-09-17 review, F2).
                blockers.append(f'{rel}: recovery storage is on another volume; the file cannot be moved recoverably.')
            if path.resolve().is_relative_to(settings.zotero_storage.resolve()):
                blockers.append(f'{rel}: Zotero owns this file; use Zotero to remove it.')
            # Phase 7: a duplicate that carries annotations is kept by default.
            # Embedded (in-file) annotations travel with the recovered file and
            # native ones live on attachments that must be trashed first (with
            # their own consent); either way the removal needs an explicit
            # acknowledgement, never a silent default.
            # An UNKNOWN embedded count is not acknowledgeable: nobody can
            # accept the loss of a number nobody has (2026-09-17 review, B3).
            if row.get('embedded_annotations') is None:
                blockers.append(f'{rel}: annotation count unknown (the PDF could not be opened); removal is refused until the file can be read.')
            annotated = int(row.get('embedded_annotations') or 0) + int(row.get('native_annotations') or 0)
            if annotated and acknowledge_annotations is not True:
                blockers.append(f'{rel}: carries {annotated} annotation(s); acknowledge explicitly that this copy is removed anyway, or keep both copies.')
            if any(i.get('link_mode') in rec.zotero.ZOTERO_OWNED_MODES and
                   (p := rec.attachment_path(i, settings, snapshot)) and p.resolve() == path.resolve()
                   for i in snapshot['items'].values() if i['item_type'] == 'attachment'):
                blockers.append(f'{rel}: Zotero-owned attachment bytes cannot be removed here.')
        before = row['mapping']
        after = dict(before, keys=[k for k in before['keys'] if k != key],
                     exclude=sorted(set(before['exclude']) | {bridge.DETACH_ALL if action == 'delete' else key}))
        if not after['keys']:
            after['source'] = None
            if not after['pending'] and not after['no_match']:
                after['hash'] = None
        if action == 'delete':
            after.update(keys=[], pending=False, no_match=False, hash=None, source=None)
        steps.append(dict(rel=rel, action=action, physical=row['physical'], before=before, after=after,
                          references=references, notes=notes_for))
    # The plan's survivor is the STABLE identity (rel, bytes, mapping, live
    # attachments): the comparison facts computed by entry() legitimately change
    # as earlier steps complete, and the mid-apply revalidation compares this.
    stable_survivor = {k: v for k, v in by_rel[survivor].items() if k in STABLE_SURVIVOR_KEYS}
    plan = dict(key=key, parent=data['parent'], database=data['database'], bibliography=data['bibliography'],
                survivor=stable_survivor, steps=steps, acknowledge_annotations=acknowledge_annotations is True)
    return dict(plan=plan, token=review.digest(plan), blockers=blockers, executable=not blockers,
                message='Delete moves the physical PDF to recoverable storage. Annotations inside that PDF stay with the recovered file. Zotero attachments are removed separately.')


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _decision_ref(value):
    """The approving decision-file row a move is recorded against (cleanup T6,
    2026-09-17). Optional; bounded and printable, because it is stored on the
    operation and printed back in the holding listing."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 256 \
            or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('decision_ref must be a short printable reference to the approving decision row.')
    return value.strip()


def apply(settings, key, survivor, actions, token, *, confirm=False, parent_key=None, acknowledge_annotations=False,
          decision_ref=None):
    if confirm is not True:
        raise ValueError('Confirm the displayed PDF plan.')
    # Validated before any lock or move; stored on the OPERATION, never in the
    # digested plan, so the preview token a decision file carries stays valid.
    decision_ref = _decision_ref(decision_ref)
    with reparent.operation_guard():
        require_settled(settings, key)
        # One vault walk for the whole apply (2026-09-17 review, B4).
        notes = note_index(settings)
        proposed = preview(settings, key, survivor, actions, parent_key, acknowledge_annotations=acknowledge_annotations, notes=notes)
        if proposed['token'] != token:
            raise review.ReviewConflict('PDF evidence changed. Preview the plan again.')
        if proposed['blockers']:
            raise ValueError(' '.join(proposed['blockers']))
        active = [op for op in operations(settings) if op['status'] not in SETTLED]
        if any(op['key'] == key for op in active):
            raise review.ReviewConflict('An earlier PDF operation needs inspection or restore first.')
        with _vault_hold() as heartbeat:
            return _apply_held(settings, key, survivor, parent_key, acknowledge_annotations, notes, proposed, heartbeat,
                               decision_ref=decision_ref)


def _apply_held(settings, key, survivor, parent_key, acknowledge_annotations, notes, proposed, heartbeat,
                decision_ref=None):
    for step in proposed['plan']['steps']:
        if step['action'] == 'delete' and _other_volume(_path(settings, step['rel'])):
            raise ValueError(f"{step['rel']}: recovery storage is on another volume; no file was removed.")
    op = dict(id=uuid.uuid4().hex, scope=rec.scope(settings), key=key, plan=proposed['plan'],
              status='prepared', at=time.time(), steps=[])
    if decision_ref is not None:
        op['decision_ref'] = decision_ref
    for step in proposed['plan']['steps']:
        op['steps'].append({**step, 'status':'prepared'})
    _save(op)
    try:
        for step in op['steps']:
            if not heartbeat():
                raise review.ReviewConflict('The vault lease was lost; remaining work stopped.')
            # Revalidate external references and survivor at each destructive boundary.
            remaining = {s['rel']:s['action'] for s in op['steps'] if s['status'] == 'prepared'}
            fresh = preview(settings, key, survivor, remaining, parent_key, acknowledge_annotations=acknowledge_annotations, notes=notes)
            original = op['plan']
            if fresh['blockers'] or any(fresh['plan'][f] != original[f] for f in ('parent','database','bibliography','survivor')):
                raise review.ReviewConflict('Survivor or external evidence changed; remaining work stopped.')
            current = next(s for s in fresh['plan']['steps'] if s['rel'] == step['rel'])
            if current != {k:v for k,v in step.items() if k != 'status'}:
                raise review.ReviewConflict('Selected PDF changed; remaining work stopped.')
            path = _path(settings, step['rel'])
            with bridge.mapping_guard(settings.mapping_file):
                if _mapping(settings, step['rel']) != step['before'] or not review.same_identity(review.identity(path), step['physical']):
                    raise review.ReviewConflict('PDF or mapping changed before removal.')
                if step['action'] == 'delete':
                    root = recovery_root()
                    if root.resolve().is_relative_to(settings.vault_root.resolve()):
                        raise ValueError('Recovery storage must be outside the vault.')
                    folder = root / op['id']
                    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                    if any(p.is_symlink() for p in (folder, *folder.parents)):
                        raise ValueError('Recovery storage cannot use symlinks.')
                    if folder.stat().st_dev != path.stat().st_dev:
                        raise ValueError('Recovery storage is on another volume; no file was removed.')
                    dest = folder / (str(op['steps'].index(step)) + '.pdf')
                    if dest.exists():
                        raise ValueError('Recovery destination already exists.')
                    step['recovery'] = str(dest)
                    step['status'] = 'move_prepared'
                    _save(op)
                    os.rename(path, dest)
                    moved = review.identity(dest)
                    if not review.same_identity(moved, step['physical']):
                        step['moved_identity'] = moved
                        _save(op)
                        # An external editor raced rename. Put those bytes
                        # back only if the original path is still free.
                        os.link(dest, path)
                        dest.unlink()
                        _fsync_dir(path.parent)
                        _fsync_dir(folder)
                        raise review.ReviewConflict('PDF changed during removal; its replacement was restored and mapping was not changed.')
                    _fsync_dir(path.parent)
                    _fsync_dir(folder)
                    step['status'] = 'moved'
                    _save(op)
                # Preserve unrelated mappings under the already-held guard.
                _set_mapping(settings, step['rel'], step['before'], step['after'])
                step['status'] = 'complete'
                _save(op)
        op['status'] = 'complete'
    except Exception as exc:
        op['status'], op['error'] = 'needs_inspection', str(exc)
    _save(op)
    _log_op(op)
    return op


def holding(settings):
    """Everything currently in recoverable storage, and every unsettled op.

    The cleanup plan's "holding folder" (T6, 2026-09-17) is this module's
    recovery root: ONE known folder, outside the vault (``apply`` refuses one
    inside it), refused when on another volume, so nothing held there is
    indexed or scanned. Each moved file is listed with its ORIGINAL path, the
    sha256 recorded before the move, its size, where it is now, whether it is
    still there, and the approving ``decision_ref`` when one was given.
    Read-only: this never creates the store or the folder.
    """
    root = recovery_root()
    try:
        biblio_dev = settings.biblio_articles_dir.stat().st_dev
        same_volume = _recovery_device() == biblio_dev
    except OSError:
        same_volume = None
    held, ops, unsettled = [], [], []
    for op in sorted(operations(settings), key=lambda o: o.get('at') or 0):
        if op['status'] not in SETTLED:
            unsettled.append(dict(id=op['id'], kind=op.get('kind', 'cleanup'), key=op.get('key'),
                                  status=op['status'], error=op.get('error')))
        deletes = [s for s in op.get('steps') or [] if s.get('action') == 'delete']
        if not deletes:
            continue
        files = []
        for step in deletes:
            recovery = Path(step['recovery']) if step.get('recovery') else None
            present = bool(recovery and recovery.is_file() and not recovery.is_symlink())
            physical = step.get('physical') or {}
            entry = dict(rel=step['rel'], sha256=physical.get('sha256'), size=physical.get('size'),
                         recovery=str(recovery) if recovery else None, present=present,
                         step_status=step.get('status'))
            files.append(entry)
            if present and op['status'] != 'restored':
                held.append(entry)
        ops.append(dict(id=op['id'], key=op.get('key'), status=op['status'], at=op.get('at'),
                        decision_ref=op.get('decision_ref'), files=files))
    return dict(root=str(root), same_volume=same_volume, held_count=len(held),
                held_bytes=sum(int(f['size'] or 0) for f in held), operations=ops,
                unsettled=unsettled, unsettled_count=len(unsettled))


def restore(settings, operation_id, *, confirm=False):
    if confirm is not True:
        raise ValueError('Confirm restore of the retained PDF operation.')
    with reparent.operation_guard():
        op = next((o for o in operations(settings) if o['id'] == operation_id), None)
        if not op:
            raise ValueError('Unknown PDF operation.')
        if op['status'] == 'restored':
            return op
        if op.get('kind') == 'link' and op['status'] == 'failed':
            return _restore_failed_link(settings, op)
        if op.get('kind') in ('link', 'trash'):
            raise ValueError('This operation changed Zotero. Inspect it in Zotero; automatic undo is unavailable.')
        with _vault_hold() as heartbeat:
            return _restore_held(settings, op, heartbeat)


def _restore_held(settings, op, heartbeat):
    # No Zotero mutation is replayed. This restores physical files and exact
    # local mapping decisions only, preserving later external edits.
    try:
        for step in reversed(op['steps']):
            if step['status'] == 'restored':
                continue
            if not heartbeat():
                raise review.ReviewConflict('The vault lease was lost; restore stopped.')
            path = _path(settings, step['rel'], exists=False)
            with bridge.mapping_guard(settings.mapping_file):
                mapping = _mapping(settings, step['rel'])
                if mapping not in (step['before'], step['after']):
                    raise review.ReviewConflict('Mapping edited after removal; restore needs manual review.')
                dest = Path(step['recovery']) if step.get('recovery') else None
                if dest and (any(p.is_symlink() for p in (dest, *dest.parents)) or not dest.resolve().is_relative_to(recovery_root().resolve())):
                    raise review.ReviewConflict('Recovery path changed; manual review required.')
                if dest and dest.exists():
                    if path.exists() or path.is_symlink():
                        if not path.is_symlink() and not dest.is_symlink() and os.path.samefile(path, dest) and review.same_identity(review.identity(path), step['physical']):
                            # Resume a process exit between restore link/unlink.
                            dest.unlink()
                        else:
                            raise review.ReviewConflict('Original path is occupied; restore will not overwrite it.')
                if dest and dest.exists():
                    if dest.is_symlink() or not dest.resolve().is_relative_to(recovery_root().resolve()) or not review.same_identity(review.identity(dest), step['physical']):
                        raise review.ReviewConflict('Recovery file changed; manual review required.')
                    # link() never overwrites; a crash between link/unlink is
                    # detectable and never justifies overwriting either path.
                    os.link(dest, path)
                    dest.unlink()
                    _fsync_dir(path.parent)
                    _fsync_dir(dest.parent)
                if not path.exists() or not review.same_identity(review.identity(path), step['physical']):
                    raise review.ReviewConflict('Original PDF unavailable or changed; restore stopped.')
                if mapping == step['after']:
                    _set_mapping(settings, step['rel'], step['after'], step['before'])
                step['status'] = 'restored'
                _save(op)
        op['status'] = 'restored'
        op.pop('error', None)
    except Exception as exc:
        op['status'], op['error'] = 'needs_inspection', str(exc)
    _save(op)
    _log_op(op)
    return op


def _restore_failed_link(settings, op):
    """A ``failed`` link created nothing in Zotero; revert only what landed
    locally — a mapping written by an operation recorded before the
    2026-09-17 reorder, when the mapping preceded the POST. The reserved key
    is retained, so a repeat link of this file stays refused."""
    plan = op['plan']
    rel = plan['rel']
    with bridge.mapping_guard(settings.mapping_file):
        current = _mapping(settings, rel)
        before = plan['mapping']
        landed = (current['keys'] == [op['key']] and set(before['keys']) <= {op['key']}
                  and all(current[k] == before[k] for k in ('pending', 'no_match', 'exclude')))
        if current != before:
            if not landed:
                raise review.ReviewConflict('Mapping edited after the failed link; restore needs manual review.')
            _set_mapping(settings, rel, current, before)
    op['status'] = 'restored'
    op.pop('error', None)
    _save(op)
    from core.utils import log_vault_write
    log_vault_write('pdf_management_link', rel, f"restored failed operation {op['id']}")
    return op


def _transport(cfg):
    from tagging import zotero_write as zw
    transport = zw.resolve_transport(cfg)
    if transport.is_web or not transport.can_write:
        raise ValueError('Authorize Zotero Desktop for this action. This workflow never switches to a Web library.')
    return transport


# Create outcomes in which Zotero answered and refused: no item exists.
DEFINITIVE_REFUSALS = ('unsupported', 'conflict', 'error')


def link_preview(settings, key, rel, parent_key=None):
    data, snapshot = entry(settings, key, parent_key)
    path = _path(settings, rel)
    rows = rec.rows(settings, snapshot, only=rel)
    if not rows:
        raise ValueError('PDF is outside the managed bibliography collection.')
    row = rows[0]
    if row['review'] and row['review'].get('identity_state') != 'verified':
        raise review.ReviewConflict('PDF review identity changed. Reopen and review it first.')
    if row['review'] and row['review'].get('status') == 'confirmed':
        from audit.core.bib import normalise_doi
        if normalise_doi((row['review'].get('chosen') or {}).get('doi') or '') != normalise_doi(data['parent']['fields'].get('DOI') or ''):
            raise review.ReviewConflict('Confirmed PDF DOI differs from this parent. Reconsider that identification first.')
    if row['attempt']['stage'] != 'fresh' or row['imported_copies']:
        raise ValueError('Existing creation or imported-copy evidence needs review before linking.')
    if row['mapping']['keys'] and row['mapping']['keys'] != [key]:
        raise ValueError('PDF is mapped to another entry. Resolve that mapping first.')
    if row['mapping']['exclude'] or row['mapping']['no_match']:
        raise ValueError('PDF has an exclusion decision. Reconsider that decision in the PDF workspace first.')
    refs, unresolved = _references(settings, snapshot, path)
    if unresolved or any(i.get('parent') != data['parent']['key'] for i in refs):
        raise ValueError('An unresolved or different Zotero relationship needs review first.')
    if len(refs) > 1:
        raise ValueError('Several Zotero attachments already use this file. Choose a survivor first.')
    plan = dict(key=key, rel=rel, physical=review.identity(path), parent=data['parent'], database=data['database'],
                bibliography=data['bibliography'], mapping=_mapping(settings, rel),
                review=row['review'], references=refs)
    return dict(plan=plan, token=review.digest(plan), executable=True,
                message='Map this PDF to the displayed entry and create a linked attachment if absent. Existing PDFs remain until a separate cleanup is confirmed.')


def link(settings, cfg, key, rel, token, *, confirm=False, parent_key=None):
    if confirm is not True:
        raise ValueError('Confirm the displayed entry and PDF before linking.')
    from types import SimpleNamespace
    from tagging import writers, attachment_intents
    with reparent.operation_guard():
        require_settled(settings, key)
        proposed = link_preview(settings, key, rel, parent_key)
        if proposed['token'] != token:
            raise review.ReviewConflict('Link evidence changed. Preview again.')
        plan = proposed['plan']
        transport = _transport(cfg)
        earlier = [o for o in operations(settings) if o.get('kind') == 'link' and o['plan']['rel'] == rel and o['status'] != 'complete']
        if any(o['status'] in ('failed', 'restored') for o in earlier):
            raise review.ReviewConflict('An earlier link of this file created nothing; its reserved key is retained and no repeat create is allowed here.')
        if earlier:
            raise review.ReviewConflict('An earlier link outcome is uncertain. Inspect the retained operation; no repeat create is allowed.')
        parent = plan['parent']
        zot = SimpleNamespace(item_key=parent['key'], item_type=parent['item_type'], library_type='user',
                              title=parent['fields'].get('title'), doi=parent['fields'].get('DOI'), server_id=plan['database'])
        prepared = writers.zotero_attachment_create_prepare(cfg, zot, _path(settings, rel))
        if 'early' in prepared:
            raise ValueError(prepared['early']['message'])
        c = prepared['commit']
        c.update(expected_identity=plan['physical'], mapping_file=str(settings.mapping_file),
                 citation_key=key, review_rel=rel, vault_root=str(settings.vault_root),
                 review_token=review.token(plan['review']) if plan['review'] else None)
        op = dict(id=uuid.uuid4().hex, kind='link', scope=rec.scope(settings), key=key, plan=plan,
                  status='prepared', at=time.time(), base=transport.base_url)
        _save(op)

        def map_pdf():
            # The mapping is the cheap, local, reversible half and is written
            # only AFTER a verified create (2026-09-17, decision D1): the
            # reserved key, not the mapping, is the barrier against a second
            # create, so a refused or unknown POST leaves mapping.json untouched.
            def validate():
                if link_preview(settings, key, rel, parent_key)['token'] != token:
                    raise review.ReviewConflict('Link evidence changed before mapping.')
            bridge.add_match(settings.mapping_file, _path(settings, rel), key, settings.vault_root,
                             source='manual', require_hash=True, expected_state={k:plan['mapping'][k] for k in ('keys','pending','no_match','exclude')},
                             expected_sha256=plan['physical']['sha256'], validate_evidence=validate)
            op['mapped'] = True

        try:
            if plan['references']:
                map_pdf()
                op.update(status='complete', attachment=plan['references'][0]['key'], result='Existing linked attachment verified.')
            else:
                # Same durable reserved-key writer as exact-file attachment.
                # Unsupported native creates never fall back to unkeyed POST.
                c['item_key'] = attachment_intents.reserve(settings.vault_root, c)
                op.update(status='sent_or_unknown', attachment=c['item_key'])
                _save(op)
                result, attachment, version = writers.zotero_attachment_create_post(prepared)
                op['result'] = result
                if result.get('ok') and not result.get('verification_pending'):
                    op['attachment'] = attachment or c['item_key']
                    _journal_attachment_create(
                        settings, cfg, prepared, key, op['attachment'], version)
                    map_pdf()
                    op['status'] = 'complete'
                elif not result.get('ok') and result.get('status') in DEFINITIVE_REFUSALS:
                    # Zotero answered and refused: nothing was created and
                    # nothing was mapped. The reservation is retained.
                    op['status'] = 'failed'
                else:
                    op['status'] = 'needs_inspection'
        except Exception as exc:
            op['status'], op['error'] = 'needs_inspection', str(exc)
        _save(op)
        _log_op(op)
        return op


def trash_preview(settings, cfg, key, attachment, survivor, parent_key=None):
    from tagging import zotero_write as zw
    data, _ = entry(settings, key, parent_key)
    if attachment not in {a['key'] for a in data['attachments']}:
        raise ValueError('Choose an attachment belonging to this entry.')
    keep = next((r for r in data['rows'] if r['rel'] == survivor and r['physical'] and r['attachments']), None)
    if not keep or not any(a['key'] != attachment for a in keep['attachments']):
        raise ValueError('Choose a different readable linked PDF to survive before removing this attachment.')
    transport = _transport(cfg)
    lib_path = _lib_path(cfg, data['parent'])
    current, version = zw.get_item(transport.base_url, lib_path, attachment, server_id=data['database'])
    if current.get('itemType') != 'attachment' or current.get('parentItem') != data['parent']['key'] or current.get('deleted'):
        raise review.ReviewConflict('Attachment changed. Refresh the entry.')
    # The survivor's STABLE identity only (2026-09-17 review, F3): comparison
    # facts and dependency lists change legitimately and must not invalidate
    # the token between preview and apply.
    stable = {k: v for k, v in keep.items() if k in STABLE_SURVIVOR_KEYS}
    plan = dict(key=key, database=data['database'], parent=data['parent'], survivor=stable, attachment=attachment,
                before=current, version=int(version), base=transport.base_url, lib_path=lib_path)
    return dict(plan=plan, token=review.digest(plan), executable=True,
                message='Move this Zotero attachment, including any annotations and child notes, to Zotero trash. The external file and CHERQAiN mapping remain. Annotation-free removal cannot be guaranteed by the Desktop API.')


def trash(settings, cfg, key, attachment, survivor, token, *, confirm=False, accept_annotation_loss=False, parent_key=None):
    if confirm is not True or accept_annotation_loss is not True:
        raise ValueError('Explicitly confirm removal including annotations and child notes.')
    from tagging import zotero_write as zw
    with reparent.operation_guard():
        require_settled(settings, key)
        proposed = trash_preview(settings, cfg, key, attachment, survivor, parent_key)
        if proposed['token'] != token:
            raise review.ReviewConflict('Attachment evidence changed. Preview again.')
        plan = proposed['plan']
        if any(o.get('kind') == 'trash' and o['plan']['attachment'] == attachment and o['status'] != 'complete' for o in operations(settings)):
            raise review.ReviewConflict('An earlier removal is uncertain. Inspect Zotero; no automatic repeat is allowed.')
        lib_path = plan.get('lib_path') or _lib_path(cfg, plan.get('parent') or {})
        current, version = zw.get_item(plan['base'], lib_path, attachment, server_id=plan['database'])
        if current != plan['before'] or int(version) != plan['version']:
            raise review.ReviewConflict('Attachment changed before removal.')
        op = dict(id=uuid.uuid4().hex, kind='trash', scope=rec.scope(settings), key=key, plan=plan,
                  status='sent_or_unknown', at=time.time())
        _save(op)
        try:
            from tagging.writers import zotero_attachment_patch
            prepared = _trash_prepared(cfg, {**plan, 'lib_path': lib_path})
            _journal_attachment_trash(settings, cfg, prepared)
            result, status, _ver = zotero_attachment_patch(prepared)
            after, _ = zw.get_item(plan['base'], lib_path, attachment, server_id=plan['database'])
            op['observed_after'] = after
            op['journal'] = result
            landed = bool(after.get('deleted') and after.get('parentItem') == plan['before'].get('parentItem'))
            op['status'] = 'complete' if landed and status == 200 else 'needs_inspection'
        except Exception as exc:
            op['status'], op['error'] = 'needs_inspection', str(exc)
        _save(op)
        _log_op(op)
        return op


def inspect(settings, cfg, operation_id):
    """Read original-target evidence. Never retry a write or invent undo history."""
    from tagging import zotero_write as zw
    op = next((o for o in operations(settings) if o['id'] == operation_id), None)
    if op and not op.get('kind') and op['status'] == 'needs_inspection':
        return _inspect_cleanup(settings, op)
    if not op or op.get('kind') not in ('link','trash'):
        raise ValueError('Choose a retained Zotero operation.')
    plan = op['plan']
    transport = _transport(cfg)
    base = plan.get('base') or op.get('base')
    if transport.base_url != base:
        raise review.ReviewConflict('The configured Zotero target differs from the retained operation.')
    key = op.get('attachment') or plan.get('attachment')
    if not key:
        return dict(operation=op['id'], completable=False, reason='No attachment was dispatched. Inspect the retained mapping; no automatic retry is allowed.')
    try:
        lib_path = plan.get('lib_path') or _lib_path(cfg, plan.get('parent') or {})
        data, _ = zw.get_item(base, lib_path, key, server_id=plan['database'])
    except zw.ZoteroNotFound:
        acknowledged = bool((op.get('result') or {}).get('ok'))
        if op['kind'] != 'link' or acknowledged or op['status'] in SETTLED:
            raise
        # The only key the POST could have used is absent and no create was
        # acknowledged: nothing was created. Settle as ``failed``; the
        # reservation is retained and a repeat create stays refused.
        with reparent.operation_guard():
            op = next(o for o in operations(settings) if o['id'] == operation_id)
            if op['status'] not in SETTLED:
                op['prior_status'], op['status'] = op['status'], 'failed'
                op['failure_observation'] = f'Reserved attachment {key} is absent from Zotero.'
                _save(op)
        return dict(operation=op['id'], completable=False, failed=True, observed=None,
                    reason='No attachment was created. The operation is recorded as failed; restore it to revert any local mapping. No automatic retry is allowed.')
    if op['kind'] == 'link':
        matches = (data.get('itemType') == 'attachment' and not data.get('deleted')
                   and data.get('parentItem') == plan['parent']['key'] and data.get('linkMode') == 'linked_file'
                   and data.get('path') == str(_path(settings, plan['rel']))
                   and review.same_identity(review.identity(_path(settings, plan['rel'])), plan['physical'])
                   and _mapping(settings, plan['rel'])['keys'] == [op['key']])
    else:
        matches = bool(data.get('deleted')) and all(data.get(k) == plan['before'].get(k)
                    for k in ('itemType','parentItem','path','linkMode'))
    result = dict(operation=op['id'], completable=matches, observed=data,
                  reason='The intended outcome is currently visible.' if matches else 'Current state differs from the intended outcome; manual review required.')
    result['token'] = review.digest(result)
    return result


def _inspect_cleanup(settings, op):
    """A cleanup stuck at ``needs_inspection`` (a restore that could not finish)
    is completable when, for every unsettled step, the original path holds the
    exact recorded bytes, or neither the original nor its recovery copy exists
    (nothing is left for this tool to restore). Otherwise the reason names the
    manual step. Reads only (2026-09-17 review, F1)."""
    reasons, manual = [], []
    for step in op['steps']:
        try:
            path = _path(settings, step['rel'], exists=False)
        except ValueError as exc:
            manual.append(f"{step['rel']}: {exc}")
            continue
        recovery = Path(step['recovery']) if step.get('recovery') else None
        at_original = path.exists() and not path.is_symlink() and review.same_identity(review.identity(path), step['physical'])
        if at_original:
            reasons.append(f"{step['rel']}: the recorded file is at its original path.")
        elif path.exists() or path.is_symlink():
            manual.append(f"{step['rel']}: the original path holds different content; review it by hand.")
        elif recovery is not None and (recovery.exists() or recovery.is_symlink()):
            manual.append(f"{step['rel']}: a recovery copy remains at {recovery}; restore it, or move it back by hand.")
        else:
            reasons.append(f"{step['rel']}: neither the original nor a recovery copy exists; nothing is left to restore.")
    completable = not manual
    result = dict(operation=op['id'], completable=completable, observed=dict(steps=reasons + manual),
                  reason=' '.join(reasons if completable else manual))
    result['token'] = review.digest(result)
    return result


def complete(settings, cfg, operation_id, token, *, confirm=False):
    if confirm is not True:
        raise ValueError('Confirm the inspected outcome before recording completion.')
    with reparent.operation_guard():
        result = inspect(settings, cfg, operation_id)
        if result.get('token') != token or not result['completable']:
            raise review.ReviewConflict('Outcome is changed or unverified. Inspect it again.')
        op = next(o for o in operations(settings) if o['id'] == operation_id)
        op['prior_status'] = op['status']
        op['status'] = 'complete'
        op['completion_observation'] = result
        op.pop('error', None)
        _save(op)
        _log_op(op)
        return op
