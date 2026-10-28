"""Readable, no-clobber copies of text already obtained by the PDF loader."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from core.cache_exports import _directory, _label, _publish_new


def export_pdf_text(source: Path, vault: Path, destination: Path,
                    signature: dict, text: str, *, page_range: tuple[int, int] | None = None,
                    citation_key: str | None = None) -> Path:
    """Never extract, embed or overwrite. Caller supplies only complete text.

    Page ranges are extraction bounds, not per-paragraph page provenance.
    Size/mtime must still match the loader's signature before publication.
    """
    vault = vault.resolve()
    target = destination.absolute()
    resolved = target.resolve()
    if resolved == vault or resolved.is_relative_to(vault) or vault.is_relative_to(resolved):
        raise ValueError('Markdown exports must be outside the indexed vault')
    source = source.absolute()
    relative = source.resolve().relative_to(vault)
    digest = signature['sha256']
    if (not isinstance(digest, str) or len(digest) != 64
            or any(c not in '0123456789abcdef' for c in digest)):
        raise ValueError('Invalid PDF fingerprint')
    stat = source.stat()
    if (stat.st_size != signature['size'] or stat.st_mtime_ns != signature['mtime_ns']):
        raise ValueError('PDF changed during extraction; export skipped')
    if not text.strip():
        raise ValueError('Empty extracted text')
    # Include the relative path so same-named/same-content files retain their
    # own source links. Full identity is in the header; short collisions refuse
    # publication rather than replacing either document.
    identity = hashlib.sha256((relative.as_posix() + '\n' + digest).encode()).hexdigest()
    suffix = f'__p{page_range[0] + 1:05d}-{page_range[1]:05d}' if page_range else ''
    label = _label(citation_key) if citation_key else _label(source.stem)
    name = f'{label}__{identity[:12]}{suffix}.md'
    uri = source.as_uri()
    bounds = (f'Extraction range: PDF pages {page_range[0] + 1}–{page_range[1]}. '
              'Individual paragraph pages are unknown.' if page_range else
              'Page provenance and extraction completeness are not certified.')
    header = (f'# {label}\n\n[Source PDF](<{uri}>)\n\n'
              f'PDF SHA-256: `{digest}`\n\n{bounds}\n\n'
              'Automatic extracted text; check figures, tables and numbers in the PDF. '
              'The source link follows the current file at that path; a replacement '
              'may differ from this fingerprint. Edited exports are never overwritten.\n\n')
    with _directory(target, create=True) as fd:
        _publish_new(fd, name, (header + text).encode('utf-8'))
        os.fsync(fd)
    return target / name
