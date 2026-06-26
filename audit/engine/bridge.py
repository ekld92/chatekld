"""PDF <-> bib citation-key bridge.

Resolution order, highest priority first:
  1. mapping.json overrides                       (manual)
  2. Z_Zotero_Notes/<key>.md YAML frontmatter
       a. direct PDF pointer fields                (pdf / attachments / file / files)
       b. (author, year) extracted from FM         + filename matcher
  3. Z_Zotero_Notes/<key>.md wikilinks `[[*.pdf]]`
  4. Author+year heuristic on PDF filename → bib

Filename normalization (applied at every step that reads PDF names):
  - strip leading ``\\d+_`` (numeric thematic prefix like ``0_das_munshi_2020``)
  - strip trailing ``_\\d{1,3}`` (copy index like ``pellat_2022_1``)
  - split on ``_``; rightmost (19|20)\\d{2} token is the year; everything to its
    left is the author, joined into a single alpha-only lowercase string.

Bib-side author normalization:
  - first author surname only
  - NFKD-strip accents, drop non-alpha, lowercase
  - ``Das-Munshi, Jayati`` -> ``dasmunshi``
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings
from ..core import bib as core_bib
from ..core import obsidian as core_obs

# Filename patterns
_LEADING_NUM_PREFIX_RE = re.compile(r"^(?:\d+_)+", re.IGNORECASE)
_TRAILING_COPY_RE = re.compile(r"_\d{1,3}$")
_YEAR_TOKEN_RE = re.compile(r"^(?:19|20)\d{2}$")

# Frontmatter PDF-pointer fields (case-insensitive lookup; common across plugins)
_FM_PDF_KEYS = ("pdf", "pdfs", "pdf_url", "pdf_path", "file", "files", "attachments")
# Frontmatter author/year fields
_FM_AUTHOR_KEYS = ("authors", "author", "creators", "creator")
_FM_YEAR_KEYS = ("year", "date", "issued", "publication_year")


@dataclass
class BridgeResult:
    """Outcome of resolving every active PDF against the bibliography.

    Forward and reverse maps (``bib_to_pdfs`` / ``pdf_to_bib``) plus three
    disjoint residual buckets — ``unmapped_pdfs`` (resolved to nothing),
    ``ambiguous_pdfs`` (author+year matched >1 key, set aside rather than
    guessed) and ``confirmed_no_match_pdfs`` (the user's curated "not in the
    bib" list). ``source_per_pdf`` records *which* resolution step won for each
    matched PDF (manual / fm_pointer / fm_authoryear / wikilink / authoryear),
    feeding the diagnostics. ``fm_pointer_fields_seen`` / ``notes_with_fm`` are
    purely informational counters surfaced in the CLI and summary.
    """

    bib_to_pdfs: dict[str, list[Path]] = field(default_factory=dict)
    pdf_to_bib: dict[Path, str] = field(default_factory=dict)
    unmapped_pdfs: list[Path] = field(default_factory=list)
    confirmed_no_match_pdfs: list[Path] = field(default_factory=list)
    source_per_pdf: dict[Path, str] = field(default_factory=dict)
    # "manual" | "fm_pointer" | "fm_authoryear" | "wikilink" | "authoryear"
    ambiguous_pdfs: dict[Path, list[str]] = field(default_factory=dict)
    # Diagnostics
    fm_pointer_fields_seen: dict[str, int] = field(default_factory=dict)
    notes_with_fm: int = 0


# -------- normalization helpers --------


def _strip_accents(s: str) -> str:
    """NFKD-decompose and drop combining marks (``é`` → ``e``)."""
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def _normalize_author_lastname(raw: str) -> str:
    """Reduce an author string to a bare lowercase alpha surname key.

    Takes the surname (the part before the first comma if present, else the
    last whitespace token), strips accents, and removes every non-letter, so
    ``"Das-Munshi, Jayati"`` → ``"dasmunshi"``. This is the bib-side key that
    must equal a filename-derived author key for an author+year match.
    """
    s = _strip_accents(raw)
    if "," in s:
        last = s.split(",", 1)[0]
    else:
        tokens = s.split()
        last = tokens[-1] if tokens else ""
    return re.sub(r"[^a-zA-Z]", "", last).lower()


def _normalize_filename_authoryear(stem: str) -> tuple[str, str] | None:
    """Return the *primary* (joined author tokens, year) candidate, or None."""
    cands = _filename_authoryear_candidates(stem)
    return cands[0] if cands else None


def _filename_authoryear_candidates(stem: str) -> list[tuple[str, str]]:
    """Return (author_key, year) candidates in priority order.

    Two passes: joined-multi-token first (matches hyphenated bib surnames like
    Das-Munshi), then first-token-only (matches the common case where the
    user writes `firstauthor_secondauthor_year` and the bib has only the
    first author's surname).
    """
    s = _LEADING_NUM_PREFIX_RE.sub("", stem)
    s = _TRAILING_COPY_RE.sub("", s)

    year_matches = list(re.finditer(r"(?:19|20)\d{2}", s))
    if not year_matches:
        return []
    year_match = year_matches[-1]
    year = year_match.group(0)

    # Everything before the year is considered part of the author
    prefix = s[: year_match.start()]
    author_tokens = [t for t in prefix.split("_") if t]

    out: list[tuple[str, str]] = []
    if author_tokens:
        joined = re.sub(r"[^a-z]", "", "".join(author_tokens).lower())
        if joined:
            out.append((joined, year))
        if len(author_tokens) > 1:
            first = re.sub(r"[^a-z]", "", author_tokens[0].lower())
            if first and first != joined:
                out.append((first, year))
    return out


# -------- frontmatter helpers --------


def _fm_get_first(fm: dict[str, Any], keys: Iterable[str]) -> Any:
    """First non-empty value among ``keys``, matched case-insensitively.

    Frontmatter key casing varies across plugins/users, so the lookup is done
    over a lowercased copy of ``fm``. Empty strings and ``None`` are treated as
    absent so the next candidate key is tried.
    """
    if not fm:
        return None
    lower = {str(k).lower(): v for k, v in fm.items()}
    for k in keys:
        v = lower.get(k)
        if v is None or v == "":
            continue
        return v
    return None


def _fm_extract_year(fm: dict[str, Any]) -> str | None:
    """Pull a 4-digit 19xx/20xx year out of any year-ish frontmatter field.

    Tolerant of full dates (``2020-05-01``) and arbitrary surrounding text by
    regex-extracting the first plausible year token. Returns ``None`` when no
    year field is present or none contains a year.
    """
    v = _fm_get_first(fm, _FM_YEAR_KEYS)
    if v is None:
        return None
    s = str(v)
    m = re.search(r"(19|20)\d{2}", s)
    return m.group(0) if m else None


def _fm_extract_first_author(fm: dict[str, Any]) -> str | None:
    """Extract a single first-author string from varied frontmatter shapes.

    Handles a bare string, a list (first element), and a citation-style dict
    (``{family: ...}``-type keys). Returns ``None`` when no author field is
    usable; the caller normalizes the result to a surname key.
    """
    v = _fm_get_first(fm, _FM_AUTHOR_KEYS)
    if v is None:
        return None
    if isinstance(v, list):
        if not v:
            return None
        v = v[0]
    if isinstance(v, dict):
        # citation-style: {family: ..., given: ...}
        for k in ("family", "last", "lastName", "lastname"):
            if k in v and v[k]:
                return str(v[k])
        # fall through
        return None
    return str(v)


def _fm_extract_pdf_pointers(fm: dict[str, Any]) -> list[str]:
    """Collect any plausible filename-like strings from PDF-pointer fields."""
    out: list[str] = []
    if not fm:
        return out
    lower = {str(k).lower(): v for k, v in fm.items()}
    for k in _FM_PDF_KEYS:
        v = lower.get(k)
        if v is None:
            continue
        items = v if isinstance(v, list) else [v]
        for item in items:
            if item is None:
                continue
            s = str(item).strip()
            if not s:
                continue
            # strip wikilink syntax / markdown link
            m = re.match(r"^\[\[([^\]|#]+)", s)
            if m:
                s = m.group(1)
            else:
                s = re.sub(r"^\[.*?\]\((.*?)\)$", r"\1", s)
            # strip query/fragment/alias
            s = s.split("?", 1)[0].split("#", 1)[0].split("|", 1)[0]
            s = s.replace("%20", " ").strip()
            if s.lower().endswith(".pdf") or "/" in s or "." not in s:
                out.append(s)
    return out


def _fm_pointer_field_present(fm: dict[str, Any]) -> str | None:
    """Name of the first populated PDF-pointer field, for diagnostics only.

    Drives the ``fm_pointer_fields_seen`` counter (which pointer conventions
    the user's vault actually uses); not used for matching itself.
    """
    if not fm:
        return None
    lower = {str(k).lower(): v for k, v in fm.items()}
    for k in _FM_PDF_KEYS:
        if lower.get(k):
            return k
    return None


# -------- mapping.json I/O --------


def _load_mapping(path: Path) -> tuple[dict[str, list[str]], set[str]]:
    """Load ``mapping.json`` → ``(matches, no_match)``; ``({}, set())`` if absent.

    ``matches`` is ``{citation_key: [vault-relative-pdf, ...]}`` and
    ``no_match`` is the set of vault-relative PDFs the user confirmed are not
    in the bib. Any read/parse error degrades to empty rather than raising —
    a corrupt overrides file must not block a scan, only forfeit the curation.
    """
    if not path.exists():
        return {}, set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}, set()
    return data.get("matches", {}) or {}, set(data.get("no_match", []) or [])


def _write_text_atomic(path: Path, text: str) -> None:
    """Atomic text write: sibling temp file promoted via ``os.replace()``.

    mapping.json is the ONLY file the audit subsystem ever writes, and it
    holds the user's manually curated PDF→citation-key matches — data that
    cannot be regenerated by a rescan.  A plain ``write_text()`` truncates
    the destination before writing, so a crash mid-write would destroy that
    curation.  Rename within one directory (same filesystem) is atomic on
    POSIX, so readers see either the old or the new complete file, never a
    torn one.

    Defined locally (not imported from ``core.utils``) on purpose: the
    engine tree is vendored from kb_harmonizer and only ``audit/config.py``
    is allowed to depend on the host app.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), text=True)
    try:
        try:
            f = os.fdopen(fd, "w", encoding="utf-8")
        except Exception:
            os.close(fd)
            raise
        with f:
            f.write(text)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_mapping(path: Path, matches: dict[str, list[str]], no_match: set[str]) -> None:
    """Persist the mapping atomically, fully sorted for a stable diff.

    Keys, per-key path lists, and the no-match set are all sorted so the
    on-disk file is deterministic (clean git/iCloud diffs, no spurious churn).
    Writes via :func:`_write_text_atomic` since this is the audit's only
    writable file and the data is hand-curated.
    """
    payload = {
        "matches": {k: sorted(v) for k, v in sorted(matches.items())},
        "no_match": sorted(no_match),
    }
    _write_text_atomic(path, json.dumps(payload, indent=2, ensure_ascii=False))


def _rel_to_vault(pdf: Path, vault_root: Path) -> str:
    """Best-effort vault-relative path; falls back to absolute string."""
    # Both paths are ``resolve()``-d before the relative computation so the
    # stored key is canonical (the mapping is keyed by vault-relative path).
    try:
        return str(pdf.resolve().relative_to(vault_root.resolve()))
    except (OSError, ValueError):
        return str(pdf)


def add_matches(
    mapping_file: Path, items: list[tuple[Path, str]], vault_root: Path
) -> None:
    """Append multiple manual PDF→key matches, saving once."""
    matches, no_match = _load_mapping(mapping_file)
    for pdf, citation_key in items:
        rel = _rel_to_vault(pdf, vault_root)
        paths = matches.setdefault(citation_key, [])
        if rel not in paths:
            paths.append(rel)
        no_match.discard(rel)
    save_mapping(mapping_file, matches, no_match)


def add_match(
    mapping_file: Path, pdf: Path, citation_key: str, vault_root: Path
) -> None:
    """Append a manual PDF→key match, dropping any prior no-match for this PDF."""
    add_matches(mapping_file, [(pdf, citation_key)], vault_root)


def add_no_matches(mapping_file: Path, pdfs: list[Path], vault_root: Path) -> None:
    """Mark multiple PDFs as 'confirmed not in the bibliography', saving once."""
    matches, no_match = _load_mapping(mapping_file)
    for pdf in pdfs:
        rel = _rel_to_vault(pdf, vault_root)
        for key in list(matches):
            if rel in matches[key]:
                matches[key].remove(rel)
                if not matches[key]:
                    del matches[key]
        no_match.add(rel)
    save_mapping(mapping_file, matches, no_match)


def add_no_match(mapping_file: Path, pdf: Path, vault_root: Path) -> None:
    """Mark a PDF as 'confirmed not in the bibliography', dropping any prior match."""
    add_no_matches(mapping_file, [pdf], vault_root)


# -------- bridge --------


def build_bridge(
    settings: Settings,
    bib_entries: list[core_bib.BibEntry],
    pdfs: list[Path],
) -> BridgeResult:
    """Resolve every PDF to a citation key via the five-step priority cascade.

    Steps, highest priority first (a PDF is claimed by the first step that
    matches and never reconsidered — ``matched`` is the guard):
      1. manual ``mapping.json`` overrides (also seeds the confirmed-no-match
         bucket);
      2a. ``Z_Zotero_Notes/<key>.md`` frontmatter direct PDF pointers;
      2b. frontmatter (author, year) → filename matcher;
      3. wikilink ``[[*.pdf]]`` references in those notes;
      4. residual author+year heuristic on the filename against the bib's
         first authors — a *unique* hit links, a multi-candidate hit is parked
         in ``ambiguous_pdfs`` (never guessed);
      5. anything still unclaimed and not ambiguous becomes ``unmapped``.

    Read-only: it consults notes, the bib, and the mapping file but writes
    nothing. The two filename indexes (by name, by author+year candidates) are
    built once up front so each step is a dict lookup, not a rescan.
    """
    vault = settings.vault_root
    bib_by_key = {e.citation_key: e for e in bib_entries}

    matches_raw, no_match_raw = _load_mapping(settings.mapping_file)
    manual_pdf_to_bib: dict[Path, str] = {}
    for key, paths in matches_raw.items():
        for rel in paths:
            try:
                manual_pdf_to_bib[(vault / rel).resolve()] = key
            except OSError:
                continue
    manual_no_match: set[Path] = set()
    for rel in no_match_raw:
        try:
            manual_no_match.add((vault / rel).resolve())
        except OSError:
            continue

    pdfs_by_name: dict[str, list[Path]] = defaultdict(list)
    pdfs_by_authoryear: dict[tuple[str, str], list[Path]] = defaultdict(list)
    for p in pdfs:
        pdfs_by_name[p.name].append(p)
        # Index by every candidate the filename yields, so the FM author+year
        # pass can find PDFs whether the bib lists "Das-Munshi" or "Das" first.
        for ay in _filename_authoryear_candidates(p.stem):
            pdfs_by_authoryear[ay].append(p)

    # Index Z_Zotero_Notes by stem (= bbtkey when the user's convention holds)
    notes_dir = settings.zotero_notes_dir
    note_by_key: dict[str, core_obs.NoteInfo] = {}
    if notes_dir.exists():
        for note_path in notes_dir.rglob("*.md"):
            if any(part in settings.ignored_dirs for part in note_path.parts):
                continue
            n = core_obs.read_note(note_path)
            if n is not None:
                note_by_key[note_path.stem] = n

    result = BridgeResult()
    result.notes_with_fm = sum(1 for n in note_by_key.values() if n.has_frontmatter)
    matched: set[Path] = set()

    def _link(p: Path, key: str, source: str) -> None:
        # Idempotent claim: record the forward+reverse maps and the winning
        # source, then mark the PDF matched so no lower-priority step can
        # re-claim it. First step to call _link for a PDF wins.
        if p in matched:
            return
        result.pdf_to_bib[p] = key
        result.bib_to_pdfs.setdefault(key, []).append(p)
        result.source_per_pdf[p] = source
        matched.add(p)

    # -- Step 1: manual mapping --
    for p in pdfs:
        rp = p.resolve()
        if rp in manual_pdf_to_bib:
            _link(p, manual_pdf_to_bib[rp], "manual")
        elif rp in manual_no_match:
            result.confirmed_no_match_pdfs.append(p)
            matched.add(p)

    # -- Step 2a: frontmatter direct PDF pointers --
    fm_field_counter: dict[str, int] = defaultdict(int)
    for key, note in note_by_key.items():
        if key not in bib_by_key:
            continue
        seen_field = _fm_pointer_field_present(note.frontmatter)
        if seen_field:
            fm_field_counter[seen_field] += 1
        for pointer in _fm_extract_pdf_pointers(note.frontmatter):
            # Resolve to a Path in our PDF set.
            base = pointer.rsplit("/", 1)[-1]
            for candidate in pdfs_by_name.get(base, []):
                if candidate not in matched:
                    _link(candidate, key, "fm_pointer")
    result.fm_pointer_fields_seen = dict(fm_field_counter)

    # -- Step 2b: frontmatter (author, year) → filename matcher --
    for key, note in note_by_key.items():
        if key not in bib_by_key:
            continue
        if not note.has_frontmatter:
            continue
        fm_author = _fm_extract_first_author(note.frontmatter)
        fm_year = _fm_extract_year(note.frontmatter)
        if not fm_author or not fm_year:
            continue
        ay = (_normalize_author_lastname(fm_author), fm_year)
        if not ay[0]:
            continue
        for candidate in pdfs_by_authoryear.get(ay, []):
            if candidate not in matched:
                _link(candidate, key, "fm_authoryear")

    # -- Step 3: wikilinks in Z_Zotero_Notes (kept; near-zero on this vault) --
    for key, note in note_by_key.items():
        if key not in bib_by_key:
            continue
        for fname in note.pdf_links:
            for candidate in pdfs_by_name.get(fname, []):
                if candidate not in matched:
                    _link(candidate, key, "wikilink")

    # -- Step 4: residual author+year fuzzy via bib first-author --
    bib_by_authoryear: dict[tuple[str, str], list[str]] = defaultdict(list)
    for e in bib_entries:
        if not e.authors or not e.year:
            continue
        last = _normalize_author_lastname(e.authors[0])
        if last:
            bib_by_authoryear[(last, e.year)].append(e.citation_key)

    for p in pdfs:
        if p in matched:
            continue
        ay_list = _filename_authoryear_candidates(p.stem)
        if not ay_list:
            continue
        linked_here = False
        union: set[str] = set()
        for ay in ay_list:
            cands = bib_by_authoryear.get(ay, [])
            if len(cands) == 1:
                _link(p, cands[0], "authoryear")
                linked_here = True
                break
            union.update(cands)
        if not linked_here and len(union) > 1:
            result.ambiguous_pdfs[p] = sorted(union)

    # -- Step 5: residual --
    for p in pdfs:
        if p in matched or p in result.ambiguous_pdfs:
            continue
        result.unmapped_pdfs.append(p)

    return result
