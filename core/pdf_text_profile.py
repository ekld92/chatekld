"""Explicit immutable PDF-text profiles; absent activation means legacy routing.

Preparation and activation affect only the supplied application-data directory.
No extraction, embedding, indexing, configuration edit or source write occurs.
Activation is an offline operation requiring separate human authorization.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
import re
from uuid import uuid4

from core.cache_exports import _directory, _locked, _publish, _publish_new, _safe_directory
from core.practical_payload import validate

_HASH = re.compile(r'[0-9a-f]{64}')


def _bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _optional(root: Path, name: str, limit: int) -> bytes | None:
    try:
        with _directory(root) as directory:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            except FileNotFoundError:
                return None
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                    raise ValueError('Invalid or oversized profile evidence')
                raw = stream.read(limit + 1)
                if len(raw) > limit:
                    raise ValueError('Oversized profile evidence')
                return raw
    except FileNotFoundError:
        return None


def _read(root: Path, name: str, limit: int = 32 * 1024**2):
    raw = _optional(root, name, limit)
    if raw is None:
        raise ValueError('Missing profile evidence')
    return raw


def prepare_profile(root: Path, vault: Path, records: list[dict], *, qualification_sha256: str) -> str:
    """Stage source-bound text; this does not authorize production indexing."""
    vault = vault.resolve()
    root = root.absolute()
    if (root.resolve().is_relative_to(vault) or vault.is_relative_to(root.resolve())
            or not _HASH.fullmatch(qualification_sha256) or not 1 <= len(records) <= 1000):
        raise ValueError('Unsafe profile destination or missing qualification receipt')
    rows: list[dict] = []
    payloads: dict[str, bytes] = {}
    for record in records:
        source = Path(record['source'])
        rel = source.relative_to(vault).as_posix()
        if source.suffix.lower() != '.pdf' or any(r['source_rel'] == rel for r in rows):
            raise ValueError('Expected unique source PDF')
        validate(record, source, record['recipe'])
        if record['outcome'] == 'partial':
            raise ValueError('Partial text cannot replace complete searchable PDF content')
        if not record['text'].strip():
            raise ValueError('Empty text cannot replace searchable PDF content')
        raw = _bytes(record)
        if len(raw) > 32 * 1024**2:
            raise ValueError('Canonical payload exceeds bounded cache size')
        name = _hash(raw) + '.json'
        payloads[name] = raw
        rows.append({'source_rel': rel, 'source_sha256': record['source_sha256'],
                     'recipe': record['recipe'], 'payload': name, 'sha256': _hash(raw)})
    body = {'schema': 1, 'kind': 'practical_pdf_text', 'vault': str(vault),
            'qualification_sha256': qualification_sha256,
            'rows': sorted(rows, key=lambda row: row['source_rel'])}
    generation = _hash(_bytes(body))
    directory = _safe_directory(root, (generation,))
    with _directory(directory) as fd:
        for name, raw in payloads.items():
            _publish_new(fd, name, raw)
        _publish_new(fd, 'manifest.json', _bytes(body))
    return generation


def _manifest(root: Path, generation: str) -> dict:
    if not isinstance(generation, str) or not _HASH.fullmatch(generation):
        raise ValueError('Invalid text generation')
    raw = _read(root / generation, 'manifest.json', 5 * 1024**2)
    if _hash(raw) != generation:
        raise ValueError('Changed profile manifest')
    value = json.loads(raw)
    if value.get('kind') != 'practical_pdf_text' or value.get('schema') != 1:
        raise ValueError('Invalid text profile')
    return value


def _payload(root: Path, generation: str, row: dict, vault: Path) -> dict:
    name = row['payload']
    if name != row['sha256'] + '.json' or not _HASH.fullmatch(row['sha256']):
        raise ValueError('Invalid profile payload reference')
    relative = Path(row['source_rel'])
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Source outside profile vault')
    source = vault / relative
    if not source.resolve().is_relative_to(vault.resolve()):
        raise ValueError('Source outside profile vault')
    raw = _read(root / generation, name)
    if _hash(raw) != row['sha256']:
        raise ValueError('Changed profile payload')
    record = json.loads(raw)
    if record['source_sha256'] != row['source_sha256']:
        raise ValueError('Profile source binding changed')
    validate(record, source, row['recipe'])
    if record['outcome'] == 'partial':
        raise ValueError('Partial text profile refused; retain whole-document baseline')
    return record


def current_generation(root: Path) -> str | None:
    raw = _optional(root, 'active.json', 1024 * 1024)
    if raw is None:
        return None
    pointer = json.loads(raw)
    if pointer.get('schema') != 1 or not pointer.get('approval'):
        raise ValueError('Invalid activation record')
    generation = pointer.get('generation')
    _manifest(root, generation)
    return generation


def activate_profile(root: Path, generation: str, *, expected_current: str | None,
                     confirm: bool, approval: str) -> None:
    """Offline switch/rollback only; caller must have explicit human approval.

    Does not switch an index. Production cutover must pair a compatible separately
    built index and this text selection under an excluded-writer boundary.
    """
    if confirm is not True or not approval.strip():
        raise ValueError('Explicit activation confirmation and approval required')
    preview = _manifest(root, generation)
    vault_root = Path(preview['vault']).resolve()
    if root.resolve().is_relative_to(vault_root) or vault_root.is_relative_to(root.resolve()):
        raise ValueError('Profile activation must stay outside the vault')
    _safe_directory(root, ())
    with _locked(root / 'activation.lock'):
        if current_generation(root) != expected_current:
            raise ValueError('Stale activation before-image')
        manifest = _manifest(root, generation)
        vault = Path(manifest['vault'])
        for row in manifest['rows']:
            _payload(root, generation, row, vault)
        history = _safe_directory(root, ('activation-history', uuid4().hex))
        previous = root / 'active.json'
        _publish(history / 'before.json', {'pointer': json.loads(previous.read_text()) if previous.exists() else None})
        pointer = {'schema': 1, 'generation': generation, 'approval': approval}
        _publish(history / 'after.json', pointer)
        _publish(previous, pointer)


def selected_payload(root: Path, vault: Path, source: Path) -> dict | None:
    """Cache-only read; missing profile/source uses legacy, damaged evidence raises."""
    generation = current_generation(root)
    if generation is None:
        return None
    manifest = _manifest(root, generation)
    if manifest['vault'] != str(vault.resolve()):
        raise ValueError('Activated PDF profile belongs to another vault')
    relative = source.relative_to(vault).as_posix()
    row = next((r for r in manifest['rows'] if r['source_rel'] == relative), None)
    return _payload(root, generation, row, vault) if row else None
