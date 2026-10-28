"""Bounded current PDF-link inspection of notes already known to a saved scan.

No directory walk, persistent cache, path repair or link rewrite. Descriptor-based
reads refuse symlink traversal; unavailable evidence never establishes absence.
"""
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
from urllib.parse import unquote, urlsplit

MAX_NOTE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 32_000_000
MAX_NOTES = 10_000


def _open_inside(root, relative):
    parts = PurePosixPath(relative).parts
    if not parts or relative.startswith('/') or '..' in parts:
        raise ValueError('Path is outside the inspected vault.')
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read(root, relative, remaining):
    fd = _open_inside(root, relative)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > min(MAX_NOTE_BYTES, remaining):
            raise ValueError('Note is not a bounded regular file.')
        raw = stream.read(min(MAX_NOTE_BYTES, remaining) + 1)
        if len(raw) > min(MAX_NOTE_BYTES, remaining) or _identity(before) != _identity(os.fstat(stream.fileno())):
            raise ValueError('Note changed during inspection or exceeded the size limit.')
    check = _open_inside(root, relative)
    try:
        if _identity(os.fstat(check)) != _identity(before):
            raise ValueError('Note path changed during inspection.')
    finally:
        os.close(check)
    return raw.decode('utf-8'), dict(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw),
                                   identity=list(_identity(before)))


def extract(text):
    # Exclude fenced and inline code; reported scope deliberately does not claim
    # a full Markdown parser, embedded HTML or application/plugin link semantics.
    text = re.sub(r'(?ms)^\s*(`{3,}|~{3,}).*?^\s*\1\s*$', '', text)
    text = re.sub(r'`+[^`\n]*`+', '', text)
    candidates = []
    for match in re.finditer(r'\[\[([^\]\n]+)\]\]', text):
        candidates.append(('wikilink', match.group(1).split('|', 1)[0]))
    for match in re.finditer(r'\]\((<[^>\n]+>|[^)\n]+)\)', text):
        value = match.group(1)
        value = value[1:-1] if value.startswith('<') else re.split(r'\s+["\']', value, 1)[0]
        candidates.append(('markdown', value))
    for match in re.finditer(r'(?m)^\s*\[[^\]\n]+\]:\s*(<[^>\n]+>|\S+)', text):
        value = match.group(1).strip('<>')
        candidates.append(('reference_definition', value))
    return [(kind, value.strip()) for kind, value in candidates if '.pdf' in unquote(value).lower()]


def _resolve(root, note, kind, raw, known_files):
    parsed = urlsplit(raw)
    target = unquote(parsed.path)
    if parsed.scheme or parsed.netloc or target.startswith('/'):
        return dict(status='external_or_unsupported', candidates=[], reason='External and absolute targets are not opened.')
    if not target.lower().endswith('.pdf'):
        return dict(status='unsupported', candidates=[], reason='PDF target syntax requires review.')
    if kind == 'wikilink' and '/' not in target:
        candidates = sorted(p for p in known_files if Path(p).name.casefold() == target.casefold())
        if len(candidates) != 1:
            return dict(status='ambiguous' if candidates else 'unresolved', candidates=candidates,
                        reason='Basename resolution is limited to the saved file population; no global uniqueness is inferred.')
    else:
        joined = root / target if kind == 'wikilink' else root / Path(note).parent / target
        normalized = Path(os.path.abspath(joined))
        try:
            candidates = [normalized.relative_to(root).as_posix()]
        except ValueError:
            return dict(status='outside_vault', candidates=[], reason='Target escapes the inspected vault.')
    try:
        fd = _open_inside(root, candidates[0])
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('Target is not a regular file.')
        finally:
            os.close(fd)
        check = _open_inside(root, candidates[0])
        try:
            if _identity(os.fstat(check)) != _identity(info):
                raise ValueError('Target path changed during inspection.')
        finally:
            os.close(check)
    except FileNotFoundError:
        return dict(status='not_found_at_path', candidates=candidates,
                    reason='Exact path was not found at inspection time; this does not establish document deletion.')
    except (OSError, ValueError):
        return dict(status='unavailable', candidates=candidates, reason='Target is unreadable, nonregular or uses a symlink.')
    return dict(status='resolved_candidate' if kind == 'wikilink' and '/' not in target else 'resolved_path',
                candidates=candidates, target_identity=list(_identity(info)),
                reason='Current regular file observed; document identity and annotations are not certified.')


def inspect(settings, inventory, documents):
    root = Path(settings.vault_root).resolve()
    known_files = {d['row']['pdf']['rel'] for d in documents if d['kind'] == 'file'}
    known_files.update(d['path'] for d in documents if d['kind'] == 'scan_file' and not Path(d['path']).is_absolute())
    paths = {}
    for key, record in inventory.records.items():
        note = getattr(record, 'obsidian_note', None)
        if note:
            paths.setdefault(Path(note.path), []).append(key)
    for path in getattr(inventory, 'notes_without_record', []):
        paths.setdefault(Path(path), [])
    notes, remaining = [], MAX_TOTAL_BYTES
    for path, keys in sorted(paths.items()):
        if len(notes) >= MAX_NOTES:
            break
        try:
            relative = path.relative_to(settings.vault_root).as_posix()
            text, evidence = _read(root, relative, remaining)
            remaining -= evidence['bytes']
            links = [dict(syntax=kind, target=target, **_resolve(root, relative, kind, target, known_files))
                     for kind, target in extract(text)]
            notes.append(dict(note=relative, citation_keys=keys, status='inspected', evidence=evidence, links=links))
        except (OSError, ValueError, UnicodeError):
            notes.append(dict(note=str(path), citation_keys=keys, status='unavailable', links=[],
                              reason='Note is outside scope, changed, unreadable, symlinked or over the inspection limit.'))
    return dict(notes=notes, observed_at=time.time(), writes=False,
                complete=len(notes) == len(paths) and all(n['status'] == 'inspected' for n in notes),
                known_notes=len(paths), inspected_notes=len(notes),
                scope='Current known scan notes, including orphan reference notes; no vault walk.',
                syntax_scope='PDF wikilinks, inline Markdown links and reference definitions; embedded HTML/plugin links require review.',
                repair_available=False)
