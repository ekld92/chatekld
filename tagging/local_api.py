"""Zotero 10 local authorization. Credentials stay in process memory only.

Reads never authorize. Only the explicit authorize route requests a Zotero
dialog. A write must carry the server ID from its own fresh read; a different
database cannot inherit versions or authorization from another instance.
"""
from __future__ import annotations

import json
import threading
from urllib.parse import urlsplit

_lock = threading.Lock()
_grants: dict[tuple[str, str], tuple[str, bool]] = {}
_preferred: set[str] = set()
_authorizing: set[str] = set()


def prefers_local(base):
    with _lock:
        return origin(base) in _preferred


class Version(int):
    server_id: str | None
    def __new__(cls, value, server_id=None):
        obj = super().__new__(cls, value)
        obj.server_id = server_id
        return obj


def origin(url):
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or p.hostname not in ('localhost', '127.0.0.1', '::1'):
        return None
    if p.username or p.password:
        return None
    return f'{p.scheme}://{p.netloc}'


def capabilities(base, *, opener, timeout=10):
    from urllib.request import Request
    from tagging.zotero_write import ZoteroError
    base = origin(base)
    if not base:
        raise ZoteroError('Local Zotero must use a loopback HTTP address.')
    with _lock:
        observed_grants = {key: grant for key, grant in _grants.items() if key[0] == base}
    with opener.open(Request(base + '/api/', headers={'Zotero-API-Version': '3'}), timeout=timeout) as r:
        server_id = r.headers.get('Zotero-Server-ID')
        version = r.headers.get('Zotero-Version', '')
        api_version = r.headers.get('Zotero-API-Version')
    with _lock:
        for key, grant in observed_grants.items():
            # A late response may only invalidate credentials it preceded with
            # an observation, never a grant installed during this request.
            if key[1] != server_id and _grants.get(key) is grant:
                del _grants[key]
        authorized = (base, server_id) in _grants
    return dict(version=version, api_version=api_version, server_id=server_id,
                supports_writes=bool(server_id and api_version == '3'), authorized=authorized)


def _authorize(base, expected_server_id, *, opener, timeout=90):
    from urllib.request import Request
    from tagging.zotero_write import ZoteroAuthError, ZoteroVersionConflict
    state = capabilities(base, opener=opener)
    if not state['supports_writes'] or state['server_id'] != expected_server_id:
        raise ZoteroVersionConflict('Zotero instance changed or does not support local writes. Refresh its status.')
    base = origin(base)
    req = Request(base + '/api/local/authorize', method='POST',
                  # Keep the external authorization identity used by remembered grants.
                  data=json.dumps({'appName': 'PaperMind'}).encode(),
                  headers={'Content-Type': 'application/json', 'Zotero-Server-ID': expected_server_id,
                           'Zotero-API-Version': '3'})
    with opener.open(req, timeout=timeout) as r:
        data = json.loads(r.read(4097))
    key = data.get('key')
    if not isinstance(key, str) or len(key) != 32 or not key.isascii() or not key.isalnum() or not isinstance(data.get('remember'), bool):
        raise ZoteroAuthError('Zotero did not return a valid local authorization.')
    with _lock:
        _grants[(base, expected_server_id)] = (key, data['remember'])
        _preferred.add(base)
    return {'authorized': True, 'remembered_for_session': data['remember'], 'server_id': expected_server_id}


def open_request(req, timeout, *, opener):
    """Single-use grants are consumed conservatively even on an uncertain reply."""
    from tagging.zotero_write import ZoteroAuthError, ZoteroVersionConflict
    base = origin(req.full_url)
    if not base or req.get_method() in ('GET', 'HEAD'):
        return opener.open(req, timeout=timeout)
    instance = req.get_header('Zotero-server-id')
    if not instance:
        raise ZoteroVersionConflict('Local write has no instance-bound version. Read and review the item again.')
    with _lock:
        grant = _grants.get((base, instance))
        if not grant:
            raise ZoteroAuthError('Authorize CHERQAiN in Zotero before this local write. No write was sent.')
        key, remember = grant
        if not remember:
            del _grants[(base, instance)]
    req.add_header('Zotero-API-Key', key)
    try:
        return opener.open(req, timeout=timeout)
    except Exception as exc:
        if getattr(exc, 'code', None) in (401, 403, 412):
            with _lock:
                # An older in-flight request must not revoke a newly granted
                # credential when its delayed failure finally arrives.
                if _grants.get((base, instance)) == grant:
                    _grants.pop((base, instance), None)
        raise


def version_headers(version):
    result = {'If-Unmodified-Since-Version': str(int(version))}
    if getattr(version, 'server_id', None):
        result['Zotero-Server-ID'] = version.server_id
    return result


def restore_kwargs(op):
    """Legacy local journals lack a database identity and must be re-reviewed."""
    if op.get('transport') == 'web':
        return {}
    return {'server_id': op.get('server_id') or ''}


def read_headers(base, server_id):
    from tagging.zotero_write import ZoteroError
    if server_id is None:
        return {}
    if not origin(base) or not server_id:
        raise ZoteroError('Restore has no verified Zotero database identity. Review this operation manually; no write was sent.')
    return {'Zotero-Server-ID': server_id}


def authorize(base, expected_server_id, *, opener, timeout=90):
    from tagging.zotero_write import ZoteroAuthError
    address = origin(base)
    if not address:
        raise ZoteroAuthError('Local Zotero must use a loopback HTTP address.')
    with _lock:
        if address in _authorizing:
            raise ZoteroAuthError('A Zotero authorization request is already pending.')
        _authorizing.add(address)
    try:
        return _authorize(base, expected_server_id, opener=opener, timeout=timeout)
    finally:
        with _lock:
            _authorizing.discard(address)
