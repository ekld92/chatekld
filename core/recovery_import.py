"""Validate prepared recovery evidence; publication belongs to the online writer."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import unicodedata


def _read(root: Path, path: Path, limit=64 * 1024**2) -> bytes:
    path = path.absolute()
    if not path.is_relative_to(root.absolute()) or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Recovery evidence is outside its directory')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('Symlink recovery evidence refused')
        if part == root:
            break
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError('Missing or oversized recovery evidence')
    return path.read_bytes()


def prepare(base: Path, vault: Path, recovery_id: str, catalog_sha256: str):
    from core.page_ocr import _bytes, _sha
    from core.practical_payload import validate
    import fitz
    if not isinstance(recovery_id, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}-[0-9a-f]{8}', recovery_id):
        raise ValueError('Invalid recovery ID')
    root = base / 'document_recovery' / recovery_id
    if root.is_symlink() or root.parent.is_symlink():
        raise ValueError('Unsafe recovery directory')
    raw = _read(root, root / 'review-catalog-v2.json')
    if hashlib.sha256(raw).hexdigest() != catalog_sha256:
        raise ValueError('Recovery catalog changed')
    catalog = json.loads(raw)
    selection = json.loads(_read(root, root / 'selection.json'))
    if Path(selection['vault']) != vault or selection.get('provider') != 'deepseek':
        raise ValueError('Recovery vault/provider mismatch')
    expected = {r['source']: r['source_sha256'] for r in selection['sources']}
    entries = catalog['accepted']
    if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise ValueError('Invalid recovery selection')
    selected, deferred, seen = [], [], set()
    for entry in entries:
        for rel in entry['sources']:
            if not isinstance(rel, str) or Path(rel).is_absolute() or '..' in Path(rel).parts:
                raise ValueError('Unsafe source path')
            if rel in seen or rel not in expected or expected[rel] != entry['source_sha256']:
                raise ValueError('Duplicate or unbound recovery source')
            # Validate containment before fitz/validate can open the source.
            # The final hash check still guards changes during preparation.
            source = vault / rel
            if (source.is_symlink() or not source.resolve().is_relative_to(vault.resolve()) or
                    any(p.is_symlink() for p in source.parents if p.is_relative_to(vault))):
                raise ValueError('Unsafe recovery source path')
            seen.add(rel)
        if entry.get('needs_manual_review'):
            deferred.extend(entry['sources'])
            continue
        if entry.get('structurally_complete') is not True:
            raise ValueError('Incomplete recovery output')
        review = _read(root, Path(entry['review_copy']))
        if _sha(review) != entry['review_copy_sha256']:
            raise ValueError('Review copy changed')
        snapshot = json.loads(_read(root, Path(entry['raw_export']).with_suffix('.json')))
        if _sha(_bytes({k: v for k, v in snapshot.items() if k != 'snapshot_id'})) != snapshot['snapshot_id']:
            raise ValueError('Altered extraction snapshot')
        pages = snapshot['pages']
        payload = snapshot['payload']
        extraction_source = Path(payload['source'])
        image = not entry['sources'][0].lower().endswith('.pdf')
        if image:
            mapping = json.loads(_read(root, root / (entry['source_sha256'] + '-conversion.json')))
            if (mapping['source_sha256'] != entry['source_sha256'] or
                    sorted(mapping['aliases']) != sorted(entry['sources']) or
                    Path(mapping['derived_pdf']) != extraction_source or
                    _sha(_read(root, extraction_source)) != mapping['derived_sha256']):
                raise ValueError('Image conversion binding changed')
            for tile in mapping['tiles']:
                label = f"Frame {tile['frame']}, tile {tile['page']}, original pixel bounds {tile['bbox_pixels']}"
                x0, y0, x1, y1 = tile['bbox_pixels']
                width, height = tile['frame_dimensions']
                if label not in review.decode() or not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
                    raise ValueError('Tile mapping differs from reviewed evidence')
            count = len(mapping['tiles'])
            if [t['page'] for t in mapping['tiles']] != list(range(1, count + 1)):
                raise ValueError('Invalid tile order')
        else:
            if extraction_source != vault / entry['sources'][0]:
                raise ValueError('PDF source mismatch')
            with fitz.open(extraction_source) as document:
                count = len(document)
        if (snapshot['coverage'] != 'processed_unverified' or snapshot['range'] != [1, count]
                or snapshot['page_count'] != count or len(pages) != count
                or [p['binding']['page'] for p in pages] != list(range(1, count + 1))
                or any(p['status'] not in ('ocr', 'native') or
                       p['binding']['source_sha256'] != payload['source_sha256'] or
                       p['binding']['recipe'] != payload['recipe'] for p in pages)):
            raise ValueError('Incomplete page or source binding')
        if image:
            _read(root, extraction_source)
        text = validate(payload, extraction_source, payload['recipe'])
        if payload['outcome'] != 'processed':
            raise ValueError('Partial payload cannot be imported')
        if image:
            text = '\n\n'.join(f"[Frame {t['frame']}; original pixel bounds {t['bbox_pixels']}]\n" + p['text']
                               for t, p in zip(mapping['tiles'], pages, strict=True))
        text = unicodedata.normalize('NFC', ''.join(c for c in text if not 0xE0100 <= ord(c) <= 0xE01EF))
        if not text.strip():
            raise ValueError('Empty recovery text')
        recipe = _sha(_bytes({'catalog': catalog_sha256, 'snapshot': snapshot['snapshot_id'],
                             'conversion': mapping if image else None, 'normalization': 'NFC-strip-supplementary-selectors-v1'}))
        for rel in entry['sources']:
            source = vault / rel
            if (source.is_symlink() or not source.resolve().is_relative_to(vault.resolve()) or
                    any(p.is_symlink() for p in source.parents if p.is_relative_to(vault)) or
                    _sha(source.read_bytes()) != expected[rel]):
                raise ValueError('Original recovery source changed')
            selected.append((vault, source, rel, {'source': rel, 'source_sha256': expected[rel],
                             'recipe': recipe, 'status': 'processed_unverified',
                             'recovery_snapshot': snapshot['snapshot_id']}, text))
    if seen != set(expected) or not selected:
        raise ValueError('Recovery selection is incomplete')
    return root, selected, deferred
