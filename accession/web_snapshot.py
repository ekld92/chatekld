"""Complete Web-library observations, never a Desktop database substitute.

Uses API v3 Total-Results and Last-Modified-Version across live/trash pages
and final probes. No link-following, credential redirects or negative-search
release of creation barriers. https://www.zotero.org/support/dev/web_api/v3/basics
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from tagging import zotero_write as zw


def observe(base, library, *, api_key, max_items=100000):
    base = zw._validated_web_base(base)
    if not re.fullmatch(r'(users|groups)/[1-9][0-9]*', library):
        raise ValueError('A specific Web library is required.')
    version = None
    counts = {}
    items = {}

    def page(resource, start, limit):
        nonlocal version
        zw._await_backoff()
        url = zw._web_url(base, f'{library}/{resource}?format=json&include=data&sort=dateAdded&direction=asc&limit={limit}&start={start}')
        request = urllib.request.Request(url, headers=zw._headers(api_key), method='GET')
        try:
            with zw._urlopen(request, zw._TIMEOUT_S) as response:
                zw._note_backoff(response.headers)
                total = response.headers.get('Total-Results', '')
                current = response.headers.get('Last-Modified-Version', '')
                if not total.isdigit() or not current.isdigit():
                    raise zw.ZoteroError('Web snapshot is missing count/version evidence.')
                total, current = int(total), int(current)
                if total > max_items or (version is not None and version != current):
                    raise zw.ZoteroError('Web library changed or exceeds the complete observation limit.')
                version = current
                if resource in counts and counts[resource] != total:
                    raise zw.ZoteroError('Web result count changed during observation.')
                counts[resource] = total
                raw = response.read(8_000_001)
                if len(raw) > 8_000_000:
                    raise zw.ZoteroError('Web page exceeds the response limit.')
                batch = json.loads(raw)
        except urllib.error.HTTPError as exc:
            zw._raise_for_http(exc, 'GET')
        except (urllib.error.URLError, OSError) as exc:
            raise zw.ZoteroUnavailable(str(exc)) from exc
        except ValueError as exc:
            raise zw.ZoteroError('Malformed Web snapshot JSON.') from exc
        if not isinstance(batch, list) or len(batch) != min(limit, max(0, total - start)):
            raise zw.ZoteroError('Incomplete Web snapshot page.')
        for wrapped in batch:
            if (not isinstance(wrapped, dict) or not isinstance(wrapped.get('data'), dict)
                    or not re.fullmatch(r'[A-Z0-9]{8}', str(wrapped.get('key', '')))
                    or wrapped['data'].get('key') != wrapped['key']
                    or not wrapped['data'].get('itemType')
                    or type(wrapped.get('version')) is not int
                    or wrapped['version'] < 0
                    or wrapped['version'] != wrapped['data'].get('version')
                    or wrapped['version'] > version):
                raise zw.ZoteroError('Malformed Web item identity/version.')
        return batch

    for resource in ('items', 'items/trash'):
        start = 0
        while True:
            batch = page(resource, start, 100)
            for wrapped in batch:
                key = wrapped['key']
                if key in items:
                    raise zw.ZoteroError('Duplicate Web item across pages or trash.')
                items[key] = {**wrapped['data'], 'deleted': resource == 'items/trash'}
            start += len(batch)
            if start == counts[resource]:
                break
    # Validate the version/count again after the last page of both collections.
    for resource in ('items', 'items/trash'):
        page(resource, 0, 1)
    return dict(base=base, library=library, checked_at=time.time(), version=version,
                complete=True, counts=counts, items=items)


def inspect_attempt(snapshot, retained):
    if (not snapshot.get('complete') or snapshot['base'].rstrip('/') != (retained.get('base') or '').rstrip('/')
            or snapshot['library'] != retained.get('library')):
        raise ValueError('Original Web target does not match the current observation.')
    parent = snapshot['items'].get(retained.get('parent_key'))
    attachment = snapshot['items'].get(retained.get('attachment_key'))
    return dict(parent=parent, attachment=attachment, observed_version=snapshot['version'],
                read_only=True, automatic_retry=False, undo='manual',
                message='Original Web keys inspected. Missing items do not establish that no create occurred; duplicate barriers remain.')


def admission(snapshot, path, fields, *, expected_target):
    """Pure admission assessment from a complete original-target observation.

    Imported-file checksums are duplicate candidates only, never proof that a
    remote attachment is the local physical file. Missing remote evidence and
    relative linked paths refuse admission. This does not dispatch a write.
    """
    from hashlib import md5
    from pathlib import Path
    from audit.core.bib import normalise_doi
    from accession import review
    target = dict(base=snapshot['base'], library=snapshot['library'])
    if (not snapshot.get('complete') or expected_target != target
            or not 0 <= time.time() - snapshot['checked_at'] <= 900):
        raise ValueError('Web observation expired or original target changed.')
    physical = review.identity(path)
    digest = md5(Path(path).read_bytes(), usedforsecurity=False).hexdigest()
    if not review.same_identity(review.identity(path), physical):
        raise ValueError('PDF changed during Web admission.')
    doi = normalise_doi(fields.get('DOI') or '')
    conflicts = []
    for key, item in snapshot['items'].items():
        if doi and normalise_doi(item.get('DOI') or '') == doi:
            conflicts.append(dict(key=key, reason='DOI already present, including trash'))
        if item.get('itemType') != 'attachment':
            continue
        mode = item.get('linkMode')
        if mode == 'linked_file':
            raw = item.get('path') or ''
            if not Path(raw).is_absolute() or raw.startswith('attachments:'):
                conflicts.append(dict(key=key, reason='Unresolved linked-file location'))
            elif Path(raw).resolve() == Path(path).resolve():
                conflicts.append(dict(key=key, reason='Existing exact linked-file path'))
        elif mode in ('imported_file', 'imported_url'):
            checksum = item.get('md5') or ''
            if not re.fullmatch('[a-fA-F0-9]{32}', checksum) or checksum.lower() == digest:
                conflicts.append(dict(key=key, reason='Unknown or potentially identical imported copy'))
    return dict(eligible=not conflicts, conflicts=conflicts, physical=physical,
                target=target, version=snapshot['version'], automatic_retry=False)


# ---------------------------------------------------------------------------
# Carrying one complete observation forward (T2, 2026-09-18)
# ---------------------------------------------------------------------------
#
# A complete observation takes ~6 minutes on the live library, and the create
# path used to run TWO per item (preview + before_dispatch). A batch cannot
# afford that, and does not need it: Zotero's own sync protocol lets a client
# ask "what changed since version V?" — `items?since=V`, `items/trash?since=V`
# and `deleted?since=V` — and the API guarantees the answer is complete for
# that interval. A snapshot that was complete at V plus every change in
# (V, V'] is a snapshot complete at V'. That is what makes ONE observation a
# valid absence proof for the 286th create: not that nothing moved, but that
# we know exactly what moved, and refuse if any of it was not ours.


class ForeignChange(zw.ZoteroError):
    """The library changed in a way this batch did not cause."""


def _delta_get(base, library, resource, since, start, api_key):
    zw._await_backoff()
    sep = '&' if '?' in resource else '?'
    url = zw._web_url(base, f'{library}/{resource}{sep}since={int(since)}'
                            + ('' if resource == 'deleted' else
                               f'&format=json&include=data&sort=dateAdded&direction=asc&limit=100&start={start}'))
    request = urllib.request.Request(url, headers=zw._headers(api_key), method='GET')
    try:
        with zw._urlopen(request, zw._TIMEOUT_S) as response:
            zw._note_backoff(response.headers)
            current = response.headers.get('Last-Modified-Version', '')
            total = response.headers.get('Total-Results', '')
            if not current.isdigit():
                raise zw.ZoteroError('Web delta is missing version evidence.')
            raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise zw.ZoteroError('Web delta exceeds the response limit.')
            body = json.loads(raw)
    except urllib.error.HTTPError as exc:
        zw._raise_for_http(exc, 'GET')
    except (urllib.error.URLError, OSError) as exc:
        raise zw.ZoteroUnavailable(str(exc)) from exc
    except ValueError as exc:
        raise zw.ZoteroError('Malformed Web delta JSON.') from exc
    return body, (int(total) if total.isdigit() else None), int(current)


def delta(base, library, since, *, api_key):
    """Every item changed (live or trashed) and every key permanently deleted
    after library version ``since``, all at ONE library version.

    Refuses (``ZoteroError``) when the library moved between the reads, so a
    delta is never a mix of two versions — the same rule ``observe`` applies
    across its pages.
    """
    base = zw._validated_web_base(base)
    if not re.fullmatch(r'(users|groups)/[1-9][0-9]*', library):
        raise ValueError('A specific Web library is required.')
    if type(since) is not int or since < 0:
        raise ValueError('A delta needs the version it starts from.')
    version = None
    changed = {}

    def _pin(current):
        nonlocal version
        if version is not None and current != version:
            raise zw.ZoteroError('Web library changed during the delta read.')
        version = current

    for resource in ('items', 'items/trash'):
        start = 0
        while True:
            batch, total, current = _delta_get(base, library, resource, since, start, api_key)
            _pin(current)
            if total is None or not isinstance(batch, list):
                raise zw.ZoteroError('Web delta page is missing its count.')
            for wrapped in batch:
                if (not isinstance(wrapped, dict) or not isinstance(wrapped.get('data'), dict)
                        or not re.fullmatch(r'[A-Z0-9]{8}', str(wrapped.get('key', '')))
                        or wrapped['data'].get('key') != wrapped['key']
                        or type(wrapped.get('version')) is not int
                        or not since < wrapped['version'] <= current):
                    raise zw.ZoteroError('Malformed Web delta item.')
                if wrapped['key'] in changed:
                    raise zw.ZoteroError('Duplicate item across the Web delta.')
                changed[wrapped['key']] = {**wrapped['data'], 'deleted': resource == 'items/trash'}
            start += len(batch)
            if start >= total or not batch:
                if start != total:
                    raise zw.ZoteroError('Incomplete Web delta.')
                break
    body, _total, current = _delta_get(base, library, 'deleted', since, 0, api_key)
    _pin(current)
    removed = body.get('items') if isinstance(body, dict) else None
    if not isinstance(removed, list):
        raise zw.ZoteroError('Malformed Web deletion delta.')
    return dict(since=since, version=version, changed=changed,
                removed=sorted(str(k) for k in removed))


def apply_delta(snapshot, change):
    """The snapshot as it stands at ``change['version']``. Pure."""
    if not snapshot.get('complete') or change['since'] != snapshot['version']:
        raise ValueError('A delta applies only to the complete snapshot it starts from.')
    items = dict(snapshot['items'])
    items.update(change['changed'])
    for key in change['removed']:
        items.pop(key, None)
    return {**snapshot, 'items': items, 'version': change['version'],
            'checked_at': time.time()}


class BatchProof:
    """One complete observation, carried forward across a batch of creates.

    ``refresh()`` reads the delta since the last known version and accepts it
    only when every changed item is one this batch created (``claim``) and is
    still live. Anything else — an edit, a sync from another device, a
    trashed or deleted item, even one of our own — raises
    :class:`ForeignChange` and the batch stops. Non-item objects (settings,
    collections, searches) are not read: they cannot create a duplicate item.
    """

    def __init__(self, snapshot, fetch_delta):
        if not snapshot.get('complete'):
            raise ValueError('A batch proof starts from a complete observation.')
        self.snapshot = snapshot
        self._fetch = fetch_delta
        self.own = set()

    def claim(self, *keys):
        self.own.update(str(k) for k in keys if k)

    def refresh(self):
        change = self._fetch(self.snapshot['version'])
        existing = self.snapshot.get('items') or {}
        # A claimed key that is not yet in the snapshot is our own create.
        # A claimed key already in the snapshot that moves again is an edit
        # (Better BibTeX citationKey write-back included) and stops the batch.
        foreign = sorted(key for key, item in change['changed'].items()
                         if key not in self.own or item.get('deleted')
                         or key in existing)
        foreign += [key for key in change['removed']]
        if foreign:
            raise ForeignChange(
                'The Zotero library changed in a way this batch did not cause '
                f"(items {', '.join(foreign[:6])}{' …' if len(foreign) > 6 else ''}, "
                f"version {change['since']} → {change['version']}). Stopped before "
                'the next create; every earlier item is complete. Re-run to resume '
                'from a fresh observation.')
        self.snapshot = apply_delta(self.snapshot, change)
        return self.snapshot
