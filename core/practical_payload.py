"""Inactive canonical PDF text/export adapter. No extraction or model calls.

Display provenance lives in a separate manifest. The .txt bytes are exactly the
canonical text given to the PDF chunker; no exported file is an indexing input.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from core.cache_exports import _directory, _label, _publish_new


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source_hash(path: Path) -> str:
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            size += len(block)
            if size > 500 * 1024**2:
                raise ValueError('PDF exceeds bounded profile source size')
            digest.update(block)
    return digest.hexdigest()


def project_page(projection: dict) -> str:
    """Preserve automatic order/paragraphs, delimit table columns explicitly.

    The evaluated projection includes every text/table item once. Tables' rows
    occur as contiguous assembly entries. Refuse an ambiguous/missing sequence
    rather than appending another text stream or dropping a table.
    """
    assembly = list(projection['assembly'])
    cursor = 0
    for table in projection['tables']:
        rows = table['rows']
        flattened = [' '.join(row) for row in rows]
        if not flattened:
            continue
        positions = [i for i in range(cursor, len(assembly) - len(rows) + 1)
                     if assembly[i:i + len(rows)] == flattened]
        if len(positions) != 1:
            raise ValueError('Ambiguous table-to-assembly correspondence')
        start = positions[0]
        assembly[start:start + len(rows)] = [' | '.join(row) for row in rows]
        cursor = start + len(rows)
    return '\n\n'.join(assembly)


def canonical(source: Path, expected_sha256: str, recipe: str,
              pages: list[tuple[int, str]], *, outcome: str) -> dict:
    if (not recipe or not pages or outcome not in {'processed', 'baseline_fallback', 'partial'}
            or source.is_symlink() or _source_hash(source) != expected_sha256):
        raise ValueError('Invalid or stale canonical source/recipe')
    # Page zero explicitly denotes unknown physical provenance for legacy text.
    numbers = [p for p, _ in pages]
    if numbers != sorted(set(numbers)) or any(p < 0 for p in numbers):
        raise ValueError('Invalid canonical page sequence')
    spans: list[dict] = []
    parts: list[str] = []
    offset = 0
    for page, text in pages:
        if not isinstance(text, str):
            raise ValueError('Text required')
        if parts:
            offset += 2
        spans.append({'physical_page': page or None, 'start': offset, 'end': offset + len(text)})
        parts.append(text)
        offset += len(text)
    text = '\n\n'.join(parts)
    return {'schema': 1, 'source': str(source), 'source_sha256': expected_sha256,
            'recipe': recipe, 'outcome': outcome, 'text': text,
            'text_sha256': _sha(text.encode()), 'pages': spans,
            'display_only': {'source_label': source.name, 'page_coordinates': 'Unicode character offsets',
                             'warning': 'Automatic extraction; inspect original PDF for claims.'}}


def validate(record: dict, source: Path, recipe: str) -> str:
    if (record.get('schema') != 1 or record.get('outcome') not in
            {'processed', 'baseline_fallback', 'partial'} or not isinstance(record.get('pages'), list)
            or not record['pages']):
        raise ValueError('Invalid canonical payload schema')
    cursor = 0
    physical = []
    for i, span in enumerate(record['pages']):
        if (type(span.get('start')) is not int or type(span.get('end')) is not int
                or span['start'] != cursor or not cursor <= span['end'] <= len(record['text'])
                or (span.get('physical_page') is not None and
                    (type(span['physical_page']) is not int or span['physical_page'] < 1))):
            raise ValueError('Invalid canonical page mapping')
        physical.append(span['physical_page'])
        cursor = span['end']
        if i < len(record['pages']) - 1:
            if record['text'][cursor:cursor + 2] != '\n\n':
                raise ValueError('Invalid canonical page separation')
            cursor += 2
    if cursor != len(record['text']) or (None in physical and len(physical) != 1):
        raise ValueError('Incomplete canonical page mapping')
    if None not in physical and physical != sorted(set(physical)):
        raise ValueError('Unordered canonical page mapping')
    if (source.is_symlink() or str(source) != record['source'] or recipe != record['recipe']
            or _source_hash(source) != record['source_sha256']
            or _sha(record['text'].encode()) != record['text_sha256']):
        raise ValueError('Stale or altered canonical payload')
    return record['text']


def export_payload(record: dict, source: Path, recipe: str, destination: Path,
                   *, input_root: Path) -> Path:
    """Immutable cache-only plain text export outside all indexed inputs."""
    raw = validate(record, source, recipe).encode()
    target, inputs = destination.resolve(), input_root.resolve()
    if not source.resolve().is_relative_to(inputs):
        raise ValueError('PDF source must belong to indexed inputs')
    if target == inputs or target.is_relative_to(inputs) or inputs.is_relative_to(target):
        raise ValueError('Export must be outside indexed inputs')
    # Full text hash ensures a new recipe/text cannot overwrite an old export.
    stem = f'{_label(source.stem)}__{record["source_sha256"][:12]}__{record["text_sha256"]}'
    metadata = {k: v for k, v in record.items() if k != 'text'}
    metadata['content_file'] = stem + '.txt'
    with _directory(destination.absolute(), create=True) as fd:
        _publish_new(fd, stem + '.txt', raw)
        _publish_new(fd, stem + '.json', json.dumps(metadata, sort_keys=True,
                                                  ensure_ascii=False).encode())
    return destination / (stem + '.txt')


def chunk_page_map(record: dict, chunks: list) -> dict:
    """Sidecar only: map exact chunk text to all overlapping physical pages.

    Unknown legacy pages and nonmatching text stay unknown. Repeated snippets
    expose ambiguity instead of claiming the first occurrence is authoritative.
    Nothing is added to embedding text or ordinary-note metadata.
    """
    result = {}
    for chunk in chunks:
        positions, cursor = [], 0
        while chunk.text:
            start = record['text'].find(chunk.text, cursor)
            if start < 0:
                break
            positions.append(start)
            cursor = start + 1
        pages = sorted({span['physical_page'] for start in positions for span in record['pages']
                        if span['physical_page'] is not None and span['start'] < start + len(chunk.text)
                        and start < span['end']})
        result[chunk.doc_id] = {'source': record['source'], 'source_sha256': record['source_sha256'],
                                'text_sha256': record['text_sha256'], 'pages': pages,
                                'mapping': 'exact' if len(positions) == 1 and pages else 'ambiguous' if pages else 'unknown'}
    return result


def export_markdown_batch(entries: list[dict], destination: Path, *,
                          input_roots: list[Path]) -> dict:
    """Immutable, explicit inventory export from canonical chunking payloads.

    Each inventory entry supplies source, expected recipe, optional payload and
    hash-verified citation association. Missing/failed/stale/partial entries stay
    in the manifest. A changed inventory needs a new destination; rerunning the
    identical inventory is idempotent. No source discovery or extraction occurs.
    All ingestion roots must be supplied by the caller, including ordinary notes.
    """
    import fcntl
    import os
    import stat
    from urllib.parse import quote
    from core.cache_exports import _read_regular

    roots = [Path(root).resolve() for root in input_roots]
    target = destination.absolute()
    resolved = target.resolve()
    if not roots or any(resolved == root or resolved.is_relative_to(root)
                        or root.is_relative_to(resolved) for root in roots):
        raise ValueError('Exports must be outside every indexed input root')
    rows, contents, seen = [], {}, set()
    for entry in sorted(entries, key=lambda e: str(e['source'])):
        source = Path(entry['source']).absolute()
        identity = str(source)
        if (identity in seen or source.suffix.lower() != '.pdf'
                or not any(source.resolve().is_relative_to(root) for root in roots)):
            raise ValueError('Expected unique PDF inside indexed input roots')
        seen.add(identity)
        record = entry.get('payload')
        row = {'source': identity, 'recipe': entry['recipe'], 'status': 'missing',
               'source_reference': source.as_uri(), 'citation_key': None}
        rows.append(row)
        if record is None:
            if entry.get('error'):
                row.update(status='failed', error=str(entry['error']))
            continue
        try:
            text = validate(record, source, entry['recipe'])
        except FileNotFoundError:
            row.update(status='missing_source')
            continue
        except (ValueError, KeyError, TypeError, OSError) as exc:
            row.update(status='stale_or_invalid', error=str(exc))
            continue
        row.update({key: record[key] for key in
                    ('source_sha256', 'text_sha256', 'outcome', 'pages')})
        row['payload_sha256'] = _sha(json.dumps(record, sort_keys=True,
                                               ensure_ascii=False).encode())
        if not text.strip():
            row.update(status='empty')
            continue
        association = entry.get('association') or {}
        keys = association.get('citation_keys', [])
        if (association.get('verified_sha256') == record['source_sha256']
                and isinstance(keys, list) and len(keys) == 1
                and isinstance(keys[0], str) and keys[0].strip()):
            row['citation_key'] = keys[0]
        row['association'] = association
        row['status'] = 'partial' if record['outcome'] == 'partial' else 'exported'
        row['structure'] = 'canonical paragraphs and literal delimiters; no inferred heading or table roles'
        row['page_provenance'] = ('unknown' if any(p['physical_page'] is None for p in record['pages'])
                                  else 'physical page spans from canonical payload')
        # Display wrappers never enter record/text or the production chunker.
        label = _label(row['citation_key'] or source.stem)
        body = [f'# {label}', '', f'[Source PDF](<{source.as_uri()}>)', '',
                f'Extraction outcome: {record["outcome"]}. Page provenance: {row["page_provenance"]}.',
                'Automatic extraction; verify claims in the original PDF. Literal discrepancies are preserved.',
                'Paragraphs and existing markup are retained; heading/table semantics are not inferred.', '']
        if record['outcome'] == 'partial':
            body += ['**Partial source content — not a complete PDF export.**', '']
        for span in record['pages']:
            page = span['physical_page']
            if page is not None:
                body += [f'> [Physical PDF page {page}](<{source.as_uri()}#page={page}>)', '']
            body += [text[span['start']:span['end']], '']
        contents[identity] = (label, '\n'.join(body).encode())
    # Only ambiguous display names receive a stable identity suffix. The whole
    # snapshot is immutable, so later batches cannot rename earlier user files.
    from collections import Counter
    counts = Counter(label.casefold() for label, _ in contents.values())
    names: dict[str, str] = {}
    for identity, (label, _) in contents.items():
        ambiguous = counts[label.casefold()] > 1 or label.casefold() == 'index'
        names[identity] = label + ('__' + _sha(identity.encode())[:8] if ambiguous else '') + '.md'
    # Include adversarial stems resembling generated suffixes, not just equal labels.
    width = 8
    while len({name.casefold() for name in names.values()}) != len(names):
        width += 4
        if width > 64:
            raise ValueError('Cannot disambiguate export filenames')
        names = {key: label + '__' + _sha(key.encode())[:width] + '.md'
                 for key, (label, _) in contents.items()}
    outputs = {}
    index = ['# PDF exports', '',
             'Coverage applies only to the explicit indexed-PDF inventory supplied to this batch.',
             'Partial and unavailable content is listed independently of retrieval quality.', '']
    for row in rows:
        identity = row['source']
        label = _label(Path(identity).stem)
        if identity in contents:
            name = names[identity]
            raw = contents[identity][1]
            row.update(file=name, export_sha256=_sha(raw))
            outputs[name] = raw
            index.append(f'- [{label}]({quote(name)}) — {row["status"]}')
        else:
            index.append(f'- {label} — {row["status"]}')
    manifest = {'schema': 1, 'inventory_count': len(rows), 'rows': rows,
                'coverage': {status: sum(row['status'] == status for row in rows)
                             for status in sorted({row['status'] for row in rows})}}
    outputs['manifest.json'] = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode()
    # Reserve index/manifest names before writing any bytes.
    if any(name.casefold() in {'index.md', 'manifest.json', '.export.lock'} for name in names.values()):
        raise ValueError('Source filename collides with reserved export index; use a verified citation key')
    outputs['index.md'] = ('\n'.join(index) + '\n').encode()
    with _directory(target, create=True) as fd:
        lock_fd = os.open('.export.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(lock_fd, 'a+b') as lock:
            if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                raise ValueError('Regular export lock required')
            fcntl.flock(lock, fcntl.LOCK_EX)
            for name, raw in outputs.items():
                existing = _read_regular(fd, name)
                if existing is not None and existing != raw:
                    raise ValueError(f'Existing or user-edited export differs: {name}; use a new snapshot directory')
            for name, raw in outputs.items():
                _publish_new(fd, name, raw)
            os.fsync(fd)
    return manifest
