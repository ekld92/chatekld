"""Shared read-only projection for scan views, search and PDF workspace.

No observation refresh or bookkeeping writes occur here. Scan counts retain
explicit scan provenance; PDF states join current decisions with the last
successful observation and disclose its age. Dispatch always reobserves.
"""
from collections import Counter
import time
import copy
import os
import threading
from pathlib import Path

from accession import reconciliation as rec
from accession.vocabulary import STATE_LABELS, DEFINITIONS


_lock = threading.Lock()
_cached = None


def _tree(root):
    """Every path under *root* (files AND directories, like ``rglob('*')``),
    via ``os.scandir`` — the ``Path.rglob`` + ``Path.stat`` pair cost ~0.5 s per
    signature on the live library (2026-09-17 review, C2). Directory symlinks
    are not followed, matching ``rglob``."""
    out = []
    stack = [str(root)]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    out.append(entry.path)
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                    except OSError:
                        pass
        except OSError:
            continue
    return out


def _signature(settings):
    from accession import review
    from zotcreate import attempts
    stores = [rec.store_path(), review.store_path(), attempts.store_path()]
    paths = [str(p) for store in stores for p in (store, Path(str(store)+'-wal'), Path(str(store)+'-shm'))]
    paths += [str(settings.mapping_file), str(settings.master_bib)]
    paths += _tree(settings.biblio_articles_dir)
    paths += _tree(settings.zotero_storage)
    def stat(path):
        try:
            value = os.stat(path)
            return (path, value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        except FileNotFoundError:
            return (path, None)
    return tuple(stat(path) for path in sorted(set(paths)))


def read(settings):
    """Memoize the join, invalidating on decisions, PDF or stored-copy changes."""
    global _cached
    # No snapshot means no reconciliation scope is available yet.
    if not rec.store_path().exists():
        return _read(settings)
    identity = (rec.scope(settings), _signature(settings))
    with _lock:
        cached = _cached
    if cached and cached[0] == identity:
        result = copy.deepcopy(cached[1])
    else:
        result = _read(settings)
        if _signature(settings) == identity[1]:
            with _lock:
                _cached = (identity, copy.deepcopy(result))
    checked = result['freshness']['checked_at']
    result['freshness']['fresh'] = bool(result['freshness']['available'] and checked
                                       and 0 <= time.time() - checked <= 60)
    return result


def _read(settings):
    snapshot = rec.saved(settings)
    status = rec.availability(settings) or {}
    checked = (snapshot or {}).get('checked_at')
    available = bool(snapshot and status.get('available', True))
    fresh = bool(available and checked and 0 <= time.time() - checked <= 60)
    try:
        rows = rec.rows(settings, snapshot, available=available) if snapshot else []
    except (OSError, ValueError, rec.review.ReviewConflict) as exc:
        # A GET must remain readable, and must not write availability/decisions.
        rows, available, fresh = [], False, False
        status = {'error': str(exc)}
    return dict(rows=rows, labels=STATE_LABELS, definitions=DEFINITIONS, counts=dict(Counter(r['state'] for r in rows)), count=len(rows),
                review_scope=rec.scope(settings) if snapshot else None, freshness=dict(available=available, fresh=fresh,
                checked_at=checked, error=status.get('error'),
                basis='Current local decisions joined to the last successful Zotero observation; actions revalidate.'))


def search_corpus(corpus, projection):
    from audit.search import _Entry, _hay, _row
    # The queue report now mounts the reconciliation workspace, so its search
    # membership must come from that same projection, not old orphan joins.
    if not projection.get('review_scope'):
        return list(corpus)
    result = [entry for entry in corpus if entry.report != 'not_in_zotero']
    for row in projection['rows']:
        pdf = row['pdf']
        result.append(_Entry('not_in_zotero',
            _hay(pdf['rel'], pdf['name'], row['state'], row.get('bibliography_keys')),
            _row(pdf['name'], row['state'], pdf_rel=pdf['rel'])))
    return result
