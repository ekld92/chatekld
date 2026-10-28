"""Explicit immutable snapshots of selected legacy caches (stdlib only).

No app configuration, PDF parser, inventory scan, provider, or model imports.
Cache directories are the per-vault directories, not their parent namespaces.
"""
from __future__ import annotations

import fcntl
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import unicodedata
from contextlib import contextmanager
from uuid import uuid4

_DIGEST = re.compile(r"[0-9a-f]{64}")
_RANGE = re.compile(r"-p(\d{5})-(\d{5})\.txt$")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _label(value: str) -> str:
    text = unicodedata.normalize("NFC", value)
    return re.sub(r"[^\w.-]+", "_", text, flags=re.UNICODE).strip("._")[:80] or "paper"


@contextmanager
def _directory(path: Path, *, create: bool = False):
    """Hold each path component open; later symlink swaps cannot redirect I/O."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.absolute().parts[1:]:
            if create:
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ValueError(f"Symlink or non-directory export path refused: {path}") from exc
                raise
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def _read_regular(directory_fd: int, name: str) -> bytes | None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError(f"Symlink file refused: {name}") from exc
        raise
    try:
        import stat
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"Regular file required: {name}")
        with os.fdopen(fd, 'rb', closefd=False) as handle:
            return handle.read()
    finally:
        os.close(fd)


def _cached(root: Path, legacy: Path | None, digest: str):
    for directory in (root, legacy):
        if directory is None:
            continue
        try:
            with _directory(directory) as directory_fd:
                raw = _read_regular(directory_fd, digest + '.txt')
        except FileNotFoundError:
            raw = None
        if raw:
            return raw, [{"name": digest + '.txt', "sha256": _hash(raw)}], []
    parts, receipts, ranges = [], [], []
    try:
        with _directory(root) as root_fd:
            for name in sorted(os.listdir(root_fd)):
                if not name.startswith(digest + '-p'):
                    continue
                match = _RANGE.search(name)
                if not match:
                    continue
                start, end = map(int, match.groups())
                if start >= end:
                    raise ValueError(f"Invalid cache range: {name}")
                raw = _read_regular(root_fd, name)
                if raw:
                    parts.append(raw)
                    ranges.append((start, end))
                    receipts.append({"name": name, "sha256": _hash(raw)})
    except FileNotFoundError:
        pass
    warnings = []
    covered = 0
    for start, end in ranges:
        if start != covered:
            warnings.append("Cache ranges have a gap or overlap.")
        covered = end
    if ranges:
        warnings.append("Range labels are implementation bounds, not verified page provenance; tail coverage is unknown.")
    return b'\n\n'.join(parts), receipts, sorted(set(warnings))


@contextmanager
def _locked(path: Path):
    """Non-blocking exclusive file lock over a sidecar lock path."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _safe_directory(root: Path, parts: tuple[str, ...]) -> Path:
    """Create and return a nested directory under ``root``, refusing symlinks."""
    root = root.absolute()
    for ancestor in (*reversed(root.parents), root):
        if ancestor.is_symlink():
            raise ValueError("Symlink candidate storage refused")
    root.mkdir(parents=True, exist_ok=True)
    for part in parts:
        root = root / part
        if root.is_symlink():
            raise ValueError("Symlink candidate storage refused")
        root.mkdir(exist_ok=True)
    return root


def _publish(path: Path, record: dict) -> None:
    """Atomically write and fsync a JSON record plus its parent directory."""
    import tempfile
    fd, temporary = tempfile.mkstemp(prefix=".candidate-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, sort_keys=True, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _publish_new(directory_fd: int, name: str, data: bytes) -> None:
    """Atomic no-clobber publish; a crash leaves only unclaimed temp files."""
    existing = _read_regular(directory_fd, name)
    if existing is not None:
        if existing != data:
            raise ValueError(f"Existing or user-edited export differs: {name}")
        return
    temporary = '.export-' + uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # link is exclusive: a concurrent file creator cannot be overwritten.
        try:
            os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        except FileExistsError:
            if _read_regular(directory_fd, name) != data:
                raise ValueError(f"Existing or user-edited export differs: {name}") from None
    finally:
        os.unlink(temporary, dir_fd=directory_fd)


def export_selected(request: dict, destination: Path | None = None) -> dict:
    """Export only supplied hashes; a snapshot never replaces differing files.

    ``association`` is caller-supplied reviewed evidence, bound to the source
    digest, with ``citation_keys`` (exactly one to use it), ``verified_sha256``,
    and optional title, identities and former_aliases. No live identity is read.
    """
    vault = Path(request['vault_root']).expanduser().resolve()
    cache = Path(request['cache_dir']).expanduser().resolve()
    legacy = Path(request['legacy_cache_dir']).expanduser().resolve() if request.get('legacy_cache_dir') else None
    target = Path(destination) if destination else cache.parent / 'readable_exports'
    target = target.expanduser().absolute()
    if target.is_symlink():
        raise ValueError('Symlink export directory refused.')
    target = target.resolve()
    for protected in (vault, cache, legacy):
        if protected and (target == protected or target.is_relative_to(protected)
                          or protected.is_relative_to(target)):
            raise ValueError('Exports must be outside the vault and cache trees.')
    rows, files = [], {}
    entries = request['entries']
    seen = set()
    for entry in sorted(entries, key=lambda e: (e['sha256'], e['source_pdf'])):
        digest = entry['sha256']
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ValueError('Every selection requires a full lowercase SHA-256.')
        # One canonical rendition per digest; ambiguous source aliases must be
        # reconciled in the selection, never silently chosen by iteration order.
        if digest in seen:
            raise ValueError('Duplicate source hash; consolidate aliases explicitly.')
        seen.add(digest)
        association = entry.get('association') or {}
        keys = association.get('citation_keys', [])
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            raise ValueError('citation_keys must be a list of strings')
        verified = association.get('verified_sha256') == digest and len(keys) == 1 and bool(keys[0])
        key = keys[0] if verified else None
        role = entry.get('role', 'paper')
        if role not in {'paper', 'supplement', 'version'}:
            raise ValueError('role must be paper, supplement or version')
        warnings = ['Legacy cache: extraction completeness and page provenance are unknown.']
        if not verified:
            warnings.append('Citation association missing, ambiguous, or not hash-verified; fallback filename used.')
        raw, receipts, cache_warnings = _cached(cache, legacy, digest)
        warnings.extend(cache_warnings)
        row = {"source_sha256": digest, "source_pdf": entry['source_pdf'],
               "citation_key": key, "role": role, "association": association,
               "source_aliases": entry.get('source_aliases', []),
               "title": association.get('title', ''), "origin": "legacy_cache",
               "completeness": "partial" if "Cache ranges have a gap or overlap." in warnings else "unknown",
               "warnings": warnings, "caches": receipts}
        if not raw:
            row['status'] = 'missing_cache'
            rows.append(row)
            continue
        # Validate without normalising newlines or adding/removing source text.
        raw.decode('utf-8', errors='strict')
        stem = _label(key or Path(entry['source_pdf']).stem)
        if role != 'paper':
            stem += '__' + role
        row.update(status='exported', text_sha256=_hash(raw))
        rows.append(row)
        files[digest] = (stem, raw)
    # Extend suffixes on case-folded collisions, independently of input order.
    widths = {digest: 8 for digest in files}
    while True:
        names: dict[str, list[str]] = {}
        for digest, (stem, _) in files.items():
            name = f'{stem}__{digest[:widths[digest]]}.md'.casefold()
            names.setdefault(name, []).append(digest)
        collisions = [group for group in names.values() if len(group) > 1]
        if not collisions:
            break
        for group in collisions:
            for digest in group:
                widths[digest] += 4
    payloads = {}
    for row in rows:
        digest = row['source_sha256']
        if digest not in files:
            continue
        stem, raw = files[digest]
        name = f'{stem}__{digest[:widths[digest]]}.md'
        # A JSON fence escapes metadata, while the cached text follows verbatim.
        header = ('# Cached PDF text\n\n```json\n' + _json(row) + '\n```\n\n').encode()
        payload = header + raw
        row.update(file='papers/' + name, export_sha256=_hash(payload))
        payloads[name] = payload
    manifest = ''.join(_json(row) + '\n' for row in rows).encode()
    with _directory(target, create=True) as target_fd:
        try:
            lock_fd = os.open('.export.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                              0o600, dir_fd=target_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ValueError('Symlink lock refused.') from exc
            raise
        with os.fdopen(lock_fd, 'a+b') as lock:
            import stat
            if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                raise ValueError('Regular export lock required.')
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                os.mkdir('papers', dir_fd=target_fd)
            except FileExistsError:
                pass
            try:
                papers_fd = os.open('papers', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=target_fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ValueError('Symlink papers directory refused.') from exc
                raise
            try:
                outputs = [(papers_fd, name, data) for name, data in payloads.items()]
                outputs.append((target_fd, 'manifest.jsonl', manifest))
                # Refuse all known conflicts before publishing even one new file.
                for directory_fd, name, data in outputs:
                    existing = _read_regular(directory_fd, name)
                    if existing is not None and existing != data:
                        raise ValueError(f'Existing or user-edited export differs: {name}')
                for directory_fd, name, data in outputs:
                    _publish_new(directory_fd, name, data)
            finally:
                os.close(papers_fd)
    return {'destination': str(target), 'exported': len(payloads),
            'missing_cache': sum(row['status'] == 'missing_cache' for row in rows)}
