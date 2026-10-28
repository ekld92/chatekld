"""Admission leases keep audit mutations in one configured context.

Requests only hold a counter, never a mutex across I/O. SSE retains its own
counter until both transport and worker finish. Context changes refuse while a
mutation is admitted; reads and unrelated preference changes remain available.
"""
from __future__ import annotations

import threading

from flask import g, has_request_context, jsonify, request

_lock = threading.Lock()
_readers = 0
_writer = False
_CONFIG_CONTEXT_KEYS = frozenset({
    'obsidian_vault_path', 'tagging_zotero_transport', 'tagging_zotero_api_base',
    'embed_provider',
})
_GENERATION_PATHS = frozenset({
    '/api/deck/generate', '/api/docgen/generate', '/api/docgen/generate-md',
})
_PREFIXES = (
    '/api/audit/', '/api/accession/', '/api/pending/', '/api/tagging/',
    '/api/notesync/', '/api/zotlink/', '/api/zotrecord/', '/api/zotcreate/',
    '/api/library/', '/api/maintenance/', '/api/widget/',
)


def _release_reader():
    global _readers
    with _lock:
        _readers -= 1


def retain_for_worker():
    """Transfer an independent lifetime to the existing SSE release guard."""
    global _readers
    if not has_request_context() or not getattr(g, '_audit_context_reader', False):
        return lambda: None
    with _lock:
        _readers += 1
    return _release_reader


def register_context_guard(app):
    @app.before_request
    def _admit_context():
        global _readers, _writer
        if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
            return None
        body = request.get_json(silent=True)
        changing = (
            request.path in ('/api/audit/config', '/api/reset')
            or (request.path == '/api/config' and isinstance(body, dict)
                and _CONFIG_CONTEXT_KEYS.intersection(body))
        )
        # Even an apparent no-op must reserve the writer: another request
        # could otherwise switch the context between comparison and save.
        mutation = ((request.path.startswith(_PREFIXES) and request.path != '/api/audit/config')
                    or request.path in _GENERATION_PATHS)
        if not changing and not mutation:
            return None
        with _lock:
            if _writer or (changing and _readers):
                return jsonify(error='A library operation is active. Retry after it finishes.',
                               reason='audit_context_busy'), 409
            if changing:
                _writer = True
                g._audit_context_writer = True
            else:
                _readers += 1
                g._audit_context_reader = True
        if changing and request.path != '/api/reset':
            from rag.vault import obsidian_manager
            epoch = obsidian_manager.try_acquire_lock(ttl=600)
            if not epoch:
                return jsonify(error='A vault operation is active. Retry after it finishes.',
                               reason='vault_context_busy'), 409
            g._audit_context_epoch = epoch
        return None

    @app.teardown_request
    def _release_context(_error):
        global _writer
        if getattr(g, '_audit_context_reader', False):
            g._audit_context_reader = False
            _release_reader()
        if getattr(g, '_audit_context_writer', False):
            g._audit_context_writer = False
            epoch = getattr(g, '_audit_context_epoch', None)
            if epoch is not None:
                from rag.vault import obsidian_manager
                obsidian_manager.release_lock(epoch)
            with _lock:
                _writer = False
