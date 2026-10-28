"""Offline, whole-base-directory recovery snapshots for upgrade preparation.

This module cannot establish that the application is stopped. The caller must
hold a maintenance boundary that excludes *all* writers for the entire copy.
It has no production caller or command-line entry point.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
import fcntl


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inventory(root: Path) -> list[dict]:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Symlink refused: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"Non-file state refused: {path}")
        rel = path.relative_to(root).as_posix()
        rows.append({"path": rel, "size": path.stat().st_size, "sha256": _hash(path)})
    return rows


def _check_databases(root: Path, rows: list[dict]) -> None:
    """Check disposable database copies so SQLite cannot create source sidecars."""
    paths = {row["path"] for row in rows}
    for rel in paths:
        if rel.endswith(("-wal", "-shm", "-journal")):
            parent = rel.rsplit("-", 1)[0]
            if Path(parent).suffix in {".db", ".sqlite", ".sqlite3"} and parent not in paths:
                raise ValueError(f"Orphan SQLite sidecar: {rel}")
    for row in rows:
        path = root / row["path"]
        if path.suffix not in {".db", ".sqlite", ".sqlite3"}:
            continue
        with tempfile.TemporaryDirectory(prefix="upgrade-sqlite-check-") as temporary:
            copied = Path(temporary) / path.name
            shutil.copy2(path, copied)
            for suffix in ("-wal", "-shm", "-journal"):
                sidecar = root / (row["path"] + suffix)
                if sidecar.is_file():
                    shutil.copy2(sidecar, Path(temporary) / (path.name + suffix))
            connection = sqlite3.connect(f"file:{copied}?mode=ro", uri=True)
            try:
                if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise ValueError(f"SQLite integrity check failed: {row['path']}")
            finally:
                connection.close()


def _sync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _exclude_app_server(source: Path):
    """Hold the GUI/headless startup lease; direct writers need separate exclusion."""
    lock_path = source / "server.json.lock"
    if lock_path.is_symlink():
        raise ValueError("Symlink server lease refused")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("PaperMind server lease is held") from exc
        info_path = source / "server.json"
        if info_path.is_symlink():
            raise ValueError("Symlink server record refused")
        if info_path.exists():
            try:
                record = json.loads(info_path.read_text(encoding="utf-8"))
                pid = record["pid"]
                if type(pid) is not int or pid <= 0:
                    raise ValueError("Invalid server pid")
            except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError("Unverifiable server record") from exc
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise ValueError("Registered PaperMind server may be running") from exc
            else:
                raise ValueError("Registered PaperMind server is running")
        yield
    finally:
        os.close(fd)


def create_offline_snapshot(source: Path, destination: Path, *, source_state: str) -> dict:
    """Copy a quiesced BASE_DIR; refuse a changed source or existing destination.

    ``source_state`` is a caller-recorded description of the stopped app/version
    and writer boundary. It is evidence to review, not a concurrency lock.
    """
    source, destination = Path(source).absolute(), Path(destination).absolute()
    if (not source_state.strip() or not source.is_dir() or source.is_symlink()
            or destination.exists() or destination.is_relative_to(source)
            or source.is_relative_to(destination)):
        raise ValueError("Unsafe snapshot source/destination or missing source state")
    with _exclude_app_server(source):
        before = _inventory(source)
        staging = Path(tempfile.mkdtemp(prefix=".upgrade-snapshot-", dir=destination.parent))
        try:
            data = staging / "data"
            shutil.copytree(source, data)
            copied = _inventory(data)
            after = _inventory(source)
            if before != copied or before != after:
                raise ValueError("Source changed during snapshot or copy differs")
            _check_databases(data, copied)
            _sync_tree(data)
            manifest = {"schema": 1, "source": str(source), "source_state": source_state,
                        "files": copied}
            with (staging / "manifest.json").open("w", encoding="utf-8") as stream:
                json.dump(manifest, stream, sort_keys=True, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            for directory in (data, staging):
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            os.rename(staging, destination)
            return manifest
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise


def verify_snapshot(snapshot: Path) -> dict:
    snapshot = Path(snapshot).absolute()
    if snapshot.is_symlink() or (snapshot / "manifest.json").is_symlink():
        raise ValueError("Symlink snapshot refused")
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("schema") != 1 or not isinstance(manifest.get("source_state"), str)
            or not manifest["source_state"].strip()
            or not isinstance(manifest.get("files"), list)
            or _inventory(snapshot / "data") != manifest["files"]):
        raise ValueError("Snapshot inventory mismatch")
    _check_databases(snapshot / "data", manifest["files"])
    return manifest


def restore_to_empty(snapshot: Path, destination: Path) -> dict:
    """Verify and copy into a new path; never overwrite existing application data."""
    manifest = verify_snapshot(snapshot)
    destination = Path(destination).absolute()
    snapshot = Path(snapshot).absolute()
    if (destination.exists() or destination.is_relative_to(snapshot)
            or snapshot.is_relative_to(destination)):
        raise ValueError("Restore destination must be new and separate")
    staging = Path(tempfile.mkdtemp(prefix=".upgrade-restore-", dir=destination.parent))
    try:
        shutil.rmtree(staging)
        shutil.copytree(snapshot / "data", staging)
        if _inventory(staging) != manifest["files"]:
            raise ValueError("Restored copy differs from snapshot")
        _check_databases(staging, manifest["files"])
        os.rename(staging, destination)
        return manifest
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def generation_binding(base: Path) -> dict:
    """Verify a complete text/index/settings unit before offline snapshotting.

    This checks recorded identities and exact stored bytes, not extraction or
    vector quality. Sources must still be available and unchanged. No activation
    pointer is written; the consumer opens the whole restored BASE_DIR together.
    The caller must exclude all writers throughout validation/copy/use.
    """
    from core.pdf_text_profile import current_generation, _manifest, _payload

    base = Path(base).absolute()
    # Also rejects symlinks before reading the coupled files.
    inventory = _inventory(base)
    config = json.loads((base / 'config.json').read_text())
    meta = json.loads((base / 'obsidian_storage/obsidian_meta.json').read_text())
    embedding = json.loads((base / 'embedding-identity.json').read_text())
    marker = base / 'obsidian_storage/promotion_state.json'
    if marker.exists():
        try:
            promotion = json.loads(marker.read_text())
        except (OSError, ValueError) as exc:
            raise ValueError('Invalid index checkpoint promotion') from exc
        if not isinstance(promotion, dict) or promotion.get('state') != 'complete':
            raise ValueError('Incomplete index checkpoint promotion')
    profiles = base / 'pdf_text_profiles'
    generation = current_generation(profiles)
    if generation is None or meta.get('pdf_text_profile') != generation:
        raise ValueError('Index and text profile generation differ')
    if meta.get('partial') is not False or meta.get('phase') != 'done' or meta.get('has_vector_data') is not True:
        raise ValueError('Incomplete generation index')
    if (meta.get('vector_backend') not in {'lancedb', 'simple'}
            or config.get('vault_vector_backend') != meta['vector_backend']):
        raise ValueError('Index backend and configuration differ')
    if (not embedding.get('alias') or embedding.get('backend') not in {'lm_studio', 'ollama'}
            or type(embedding.get('dimension')) is not int or embedding['dimension'] <= 0
            or not isinstance(embedding.get('artifact_sha256'), str)
            or len(embedding['artifact_sha256']) != 64
            or any(c not in '0123456789abcdef' for c in embedding['artifact_sha256'])):
        raise ValueError('Incomplete embedding identity')
    if any(obj.get('embed') != embedding['alias'] or obj.get('embed_provider') != embedding['backend']
           for obj in (meta, config)):
        raise ValueError('Embedding model/backend binding differs')
    manifest = _manifest(profiles, generation)
    vault = Path(manifest['vault'])
    if any(obj.get(key) != str(vault) for obj, key in
           ((meta, 'vault_path'), (config, 'obsidian_vault_path'))):
        raise ValueError('Generation vault identity differs')
    sources = []
    for row in manifest['rows']:
        record = _payload(profiles, generation, row, vault)
        sources.append({'source': row['source_rel'], 'source_sha256': row['source_sha256'],
                        'recipe': row['recipe'], 'text_sha256': record['text_sha256'],
                        'outcome': record['outcome']})
    index_files = [row for row in inventory if row['path'].startswith('obsidian_storage/')]
    if not any(row['path'].endswith('docstore.json') or row['path'].endswith('.sqlite') for row in index_files):
        raise ValueError('Missing generation docstore')
    if not any('/lancedb/' in row['path'] or row['path'].endswith('vector_store.json')
               for row in index_files):
        raise ValueError('Missing generation vectors')
    return {'schema': 1, 'text_generation': generation, 'sources': sources,
            'embedding': embedding, 'vector_backend': meta['vector_backend'],
            'index_files': index_files, 'config_sha256': _hash(base / 'config.json')}


def create_generation_snapshot(source: Path, destination: Path, *, source_state: str) -> dict:
    """Reuse the whole-base snapshot with a checked identity binding inside it."""
    from core.cache_exports import _directory, _publish_new

    # Never insert new files into the source. Seal only the new snapshot.
    before = generation_binding(source)
    manifest = create_offline_snapshot(source, destination, source_state=source_state)
    after = generation_binding(destination / 'data')
    if before != after:
        raise ValueError('Generation changed during snapshot; preserve copy for diagnosis')
    with _directory(destination) as directory:
        _publish_new(directory, 'generation-binding.json',
                     json.dumps(after, sort_keys=True, allow_nan=False).encode())
    return {'binding': after, 'snapshot': manifest}


def restore_generation_to_empty(snapshot: Path, destination: Path) -> dict:
    """Restore one complete generation, never a text-only activation.

    Existing directories are refused by the shared recovery writer. Old complete
    generations remain available for rollback by restoring their own snapshot to
    another empty base. Choosing a production base remains separately authorized.
    """
    verify_snapshot(snapshot)
    before = generation_binding(snapshot / 'data')
    receipt = snapshot / 'generation-binding.json'
    if receipt.is_symlink() or json.loads(receipt.read_text()) != before:
        raise ValueError('Generation binding receipt differs')
    manifest = restore_to_empty(snapshot, destination)
    if generation_binding(destination) != before:
        raise ValueError('Restored generation binding differs')
    return {'binding': before, 'snapshot': manifest}
