"""PDF <-> bib citation-key bridge.

Resolution order, highest priority first:
  0. the file Zotero links under an item          (zotero_link)
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
import fcntl
from contextlib import contextmanager
import logging
import os
import re
import tempfile
import threading
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings
from ..core import bib as core_bib
from ..core import doi_cache as core_doi_cache
from ..core import pdf_provenance as core_pdf_prov
from ..core import hashing as core_hashing
from ..core import obsidian as core_obs
from ..core import pdf_identifiers as core_pdf_ids

logger = logging.getLogger(__name__)

# Hash-recovery candidate budget per scan (Step 1b). Bounds the worst case a
# PERMANENT orphan creates: without a cap, one dead mapping entry would make
# every future scan full-hash the entire unmapped corpus. 500 files at the
# typical article size is a few seconds; hitting the cap WARNs.
_HASH_RECOVERY_MAX_FILES = 500

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
    matched PDF (manual / manual_hash / fm_pointer / fm_authoryear / wikilink /
    doi / authoryear / doi_text), feeding the diagnostics. ``fm_pointer_fields_seen`` / ``notes_with_fm`` are
    purely informational counters surfaced in the CLI and summary.
    """

    bib_to_pdfs: dict[str, list[Path]] = field(default_factory=dict)
    pdf_to_bib: dict[Path, str] = field(default_factory=dict)
    unmapped_pdfs: list[Path] = field(default_factory=list)
    confirmed_no_match_pdfs: list[Path] = field(default_factory=list)
    # The curated "to add to Zotero later" bucket (2026-09-06, the split of
    # ``no_match`` into two states). Treated exactly like ``no_match`` by the
    # ladder — matched, so no rung claims it — and listed by the
    # ``not_in_zotero`` report with ``state: "pending"``.
    pending_pdfs: list[Path] = field(default_factory=list)
    # ``{resolved path: {citation_key, …}}`` — the per-file exclusions the
    # ``detach`` verb writes: an automatic rung may never claim that PDF for
    # that key again, so the file returns to the orphan queue.
    exclusions_applied: int = 0
    source_per_pdf: dict[Path, str] = field(default_factory=dict)
    # "zotero_link" | "manual" | "manual_hash" | "accession" | "accession_hash" |
    # "fm_pointer" | "fm_authoryear" | "wikilink"
    # | "doi" | "authoryear" | "doi_text"
    ambiguous_pdfs: dict[Path, list[str]] = field(default_factory=dict)
    # Diagnostics
    fm_pointer_fields_seen: dict[str, int] = field(default_factory=dict)
    notes_with_fm: int = 0
    # Curated mapping entries whose stored path no longer resolves (hash
    # recovery ran for them this scan — recovered or not). Non-zero after the
    # scan means dead entries linger in mapping.json; surfaced in the summary
    # so the user can re-curate instead of paying the recovery pass forever.
    mapping_orphans: int = 0
    # Physical provenance of each UNMAPPED PDF (Batch C, 2026-08-26):
    # ``{path: {"klass", "pages", "producer", "emc_code", "source_url"}}``.
    # Computed only for the residual, because that is the only set anything
    # renders it for, and used to route a row to the right remedy: a printed
    # web page is offered its URL, not a DOI search.
    provenance: dict = field(default_factory=dict)
    # DOI-rung diagnostics (2026-08-19). ``doi_examined`` counts the PDFs the
    # embedded-metadata rung looked at — everything steps 1-3 left unclaimed,
    # which on the maintainer's library is ~4 980 files, NOT the ~968 residual,
    # because author+year runs after it by design. That is why the persistent
    # memo in ``core/doi_cache.py`` is load-bearing rather than an optimisation
    # (measured cold: +52 s per scan). ``doi_text_examined`` is the printed-DOI
    # rung's much smaller set — only what author+year also failed to claim.
    # ``doi_collisions`` maps a DOI carried by >1 bib entry to those keys: the
    # rung refuses to link on it, and the collision is itself a duplicate-BIB-
    # ENTRY signal worth reporting rather than silently swallowing.
    doi_examined: int = 0
    doi_text_examined: int = 0
    doi_collisions: dict[str, list[str]] = field(default_factory=dict)
    # Zotero-link rung (2026-09-19). ``zotero_linked`` counts the PDFs it
    # claimed. ``zotero_overrides`` lists every claimed PDF whose mapping.json
    # curation said something else — ``{path: {"zotero_key", "curation",
    # "curated_key"}}``, curation one of match / no_match / pending / exclude —
    # because the maintainer's rule is "Zotero wins, reported": the stale hand
    # decision is shown, never silently kept or silently deleted.
    # ``zotero_multi_links`` holds a PDF two or more Zotero items link (it is
    # NOT claimed; the rest of the ladder decides), and ``zotero_unexported``
    # a PDF whose Zotero item's key is not yet in _master.bib (Better BibTeX
    # has not exported it; the rest of the ladder decides this scan).
    zotero_linked: int = 0
    zotero_overrides: dict[Path, dict] = field(default_factory=dict)
    zotero_multi_links: dict[Path, list[str]] = field(default_factory=dict)
    zotero_unexported: dict[Path, str] = field(default_factory=dict)
    zotero_path_ambiguous: set[Path] = field(default_factory=set)


def zotero_unclaimed(bridge, path: Path) -> bool:
    """True when Zotero links *path* but the rung could not claim it.

    The two fall-through cases — an item Better BibTeX has not exported, and a
    file two items link — stay on ``unmapped_pdfs``, because the rest of the
    ladder still gets to decide. That is right for PAIRING and wrong for every
    surface that says "not in Zotero": the file demonstrably IS in Zotero, and
    offering to identify it or to create a second record for it is the one
    thing those surfaces must never do.

    ONE definition (2026-09-20): the accession routes, *PDFs Without Zotero*,
    its *↳ Unread Only* subset and *Not in Zotero* all ask this question, and
    a copy that drifted would hide the row on one surface while offering a
    create on another — or make the subset report a row its own superset does
    not list.
    """
    return (path in (getattr(bridge, "zotero_unexported", None) or {})
            or path in (getattr(bridge, "zotero_multi_links", None) or {})
            or path in (getattr(bridge, "zotero_path_ambiguous", None) or set()))


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


def bibliography_author_keys(entry) -> set[str]:
    """Retain historical surname keys while admitting evidenced institutions."""
    from audit.core.bib import naming_author
    chosen = naming_author(entry)
    names = ([entry.authors[0]] if entry.authors else []) + ([chosen] if chosen else [])
    return {key for name in names if (key := _normalize_author_lastname(name))}


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


@dataclass
class MappingData:
    """The whole of ``mapping.json`` in memory — ONE read, saved as ONE write.

    Every writer must load THIS (not the 4-tuple ``_load_mapping_full``) and
    hand every field back to :func:`save_mapping`, or a save silently drops
    the keys it did not carry — which is exactly how ``pending`` and
    ``exclude`` (2026-09-06) would be lost by an older writer.
    """

    matches: dict[str, list[str]] = field(default_factory=dict)
    no_match: set[str] = field(default_factory=set)
    hashes: dict[str, dict] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    #: Curated "add to Zotero later" PDFs (vault-relative), 2026-09-06.
    pending: set[str] = field(default_factory=set)
    #: ``{rel: [citation_key, …]}`` — keys an automatic rung must not claim
    #: that PDF for (the ``detach`` verb), 2026-09-06. A list holding
    #: :data:`DETACH_ALL` means no automatic rung may claim it for ANY key.
    exclude: dict[str, list[str]] = field(default_factory=dict)


def _load_mapping_all(path: Path) -> MappingData:
    """ONE read of ``mapping.json`` → :class:`MappingData` (every key).

    The 4-tuple :func:`_load_mapping_full` below is the historical reader and
    is kept for read-only consumers; writers use this one so a save can never
    drop ``pending`` / ``exclude``.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return MappingData()
    if not isinstance(data, dict):
        return MappingData()
    matches, no_match, hashes, sources = _parse_mapping_fields(data)
    pending = {x for x in (data.get("pending", []) or []) if isinstance(x, str) and x}
    exclude: dict[str, list[str]] = {}
    for rel, keys in (data.get("exclude", {}) or {}).items():
        if isinstance(keys, list):
            ks = sorted({k for k in keys if isinstance(k, str) and k})
            if ks:
                exclude[str(rel)] = ks
    # A rel is in exactly one curated bucket: a stale hand edit that lists
    # it in two is read with ``no_match`` winning (the older, stricter state).
    pending -= no_match
    return MappingData(matches=matches, no_match=no_match, hashes=hashes,
                       sources=sources, pending=pending, exclude=exclude)


def _load_mapping_full(
    path: Path,
) -> tuple[dict[str, list[str]], set[str], dict[str, dict], dict[str, str]]:
    """ONE read of ``mapping.json`` → ``(matches, no_match, hashes, sources)``.

    ``matches`` is ``{citation_key: [vault-relative-pdf, ...]}``; ``no_match``
    the set of PDFs the user confirmed are not in the bib; ``hashes`` the
    hash-anchoring index ``{rel: {"sha256": str, "size": int|None}}`` (the
    ``size`` is the cheap pre-filter for recovery — see ``build_bridge`` Step
    1b). Legacy entries where the value is a bare sha256 string are normalised
    to the dict shape with ``size=None`` (no pre-filter, still recoverable).
    ``sources`` (additive, 2026-08-19) annotates HOW a curated match was made
    — ``{rel: label}``, only non-default labels stored (``"accession"``
    today; an absent rel means plain hand curation, ``manual``) — so the
    pairings board can distinguish an accession-made link from a hand one.

    A single read matters: the two-call predecessor (`_load_mapping` +
    `_load_hashes`) could observe two different on-disk versions around a
    concurrent atomic save, pairing new matches with old hashes. Any read /
    parse error degrades to empty rather than raising — a corrupt overrides
    file must not block a scan, only forfeit the curation.
    """
    mapping = _load_mapping_all(path)
    return mapping.matches, mapping.no_match, mapping.hashes, mapping.sources


def _parse_mapping_fields(data: dict):
    """Parse the historical fields from the same document as curation state."""
    matches = data.get("matches", {}) or {}
    no_match = set(data.get("no_match", []) or [])
    hashes: dict[str, dict] = {}
    for k, v in (data.get("hashes", {}) or {}).items():
        if isinstance(v, str) and v:
            hashes[str(k)] = {"sha256": v, "size": None}
        elif isinstance(v, dict) and isinstance(v.get("sha256"), str) and v["sha256"]:
            size = v.get("size")
            hashes[str(k)] = {
                "sha256": v["sha256"],
                "size": int(size) if isinstance(size, (int, float)) else None,
            }
    sources = {
        str(k): str(v)
        for k, v in (data.get("sources", {}) or {}).items()
        if isinstance(v, str) and v
    }
    return matches, no_match, hashes, sources


def _preserve_corrupt_mapping(path: Path) -> None:
    """If ``path`` holds non-empty, unparseable JSON, rename it aside before a
    writer clobbers it (B10/#39, 2026-07-13).

    ``_load_mapping_full`` degrades a corrupt file to empty for the SCAN read
    path (a corrupt overrides file must not block a scan). But the RMW writers
    then save an empty-loaded file over the corrupt one — permanently replacing a
    hand-recoverable curation file (mapping.json is unregenerable). Preserve the
    bytes in a non-clobbering ``mapping.json.corrupt-N`` sibling first. Called
    under ``_MAPPING_RMW_LOCK`` by the writers only. Best-effort — never raises.
    """
    try:
        if not path.exists():
            return
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return
        json.loads(raw)   # parses fine → nothing to preserve
        return
    except OSError:
        return
    except Exception:
        pass  # unparseable → fall through to preserve
    try:
        n = 0
        while True:
            dest = path.with_name(f"{path.name}.corrupt-{n}")
            if not dest.exists():
                os.replace(str(path), str(dest))
                logger.warning("Preserved corrupt %s as %s before overwrite.", path.name, dest.name)
                return
            n += 1
    except OSError:
        logger.warning("Could not preserve corrupt %s before overwrite.", path.name, exc_info=True)


def _load_mapping(path: Path) -> tuple[dict[str, list[str]], set[str]]:
    """Back-compat wrapper over :func:`_load_mapping_full` (tests import it)."""
    matches, no_match, _hashes, _sources = _load_mapping_full(path)
    return matches, no_match


def _load_hashes(path: Path) -> dict[str, dict]:
    """Back-compat wrapper over :func:`_load_mapping_full` (tests import it)."""
    _matches, _no_match, hashes, _sources = _load_mapping_full(path)
    return hashes


def _fsync_parent_dir(path) -> None:
    """Best-effort fsync of *path*'s directory entry (see the callers' note)."""
    try:
        fd = os.open(str(Path(path).parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


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
            # fsync BEFORE the rename for crash *durability*, not just
            # atomicity — same W6 discipline as core.utils.write_text_atomic
            # (see the root CLAUDE.md): mapping.json holds hand-curated,
            # unregenerable matches, so a rename-without-fsync could lose it
            # on power loss.
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
        # DURABILITY (2026-09-02, performance plan Phase 5): fsync the PARENT
        # DIRECTORY after the rename. The app-wide policy deliberately omits
        # this for regenerable files, but mapping.json is one of the three
        # UNREGENERABLE decision sidecars: fsyncing the file alone makes its
        # CONTENT durable while the directory ENTRY that names it may not be,
        # so a power loss right after the rename can leave the old file — or
        # no file — despite a successful save. Best-effort: a platform or
        # filesystem that refuses to fsync a directory (or lacks O_DIRECTORY)
        # is not a reason to fail a write that has already landed.
        _fsync_parent_dir(path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_mapping(
    path: Path,
    matches: dict[str, list[str]],
    no_match: set[str],
    hashes: dict[str, dict] | None = None,
    sources: dict[str, str] | None = None,
    *,
    pending: set[str] | None = None,
    exclude: dict[str, list[str]] | None = None,
) -> None:
    """Persist the mapping atomically, fully sorted for a stable diff.

    Keys, per-key path lists, and the no-match set are all sorted so the
    on-disk file is deterministic (clean git/iCloud diffs, no spurious churn).
    ``hashes`` (optional) is the anchoring index ``{rel: {sha256, size}}``; it
    is pruned to only the paths still referenced by ``matches``/``no_match``
    so a removed entry never leaves a stale hash behind, and omitted entirely
    when empty so legacy files that never used anchoring stay byte-identical.
    ``sources`` (optional, 2026-08-19) is the provenance annotation
    ``{rel: label}`` — pruned to rels still in ``matches`` (a no-match is not
    a match made by anything) and likewise omitted when empty.
    Writes via :func:`_write_text_atomic` since this is the audit's only
    writable file and the data is hand-curated.
    """
    payload: dict[str, Any] = {
        "matches": {k: sorted(v) for k, v in sorted(matches.items())},
        "no_match": sorted(no_match),
    }
    # ``pending`` / ``exclude`` (2026-09-06): emitted only when non-empty so a
    # file that never used them stays byte-identical. A rel cannot be both
    # pending and no_match; no_match wins, matching the loader.
    pending_clean = {r for r in (pending or set()) if r not in no_match}
    if pending_clean:
        payload["pending"] = sorted(pending_clean)
    exclude_clean = {
        rel: sorted(set(keys)) for rel, keys in sorted((exclude or {}).items())
        if keys
    }
    if exclude_clean:
        payload["exclude"] = exclude_clean
    referenced = set(no_match) | pending_clean
    for paths in matches.values():
        referenced.update(paths)
    if hashes:
        pruned = {k: hashes[k] for k in sorted(hashes) if k in referenced}
        if pruned:
            payload["hashes"] = pruned
    if sources:
        matched_rels: set[str] = set()
        for paths in matches.values():
            matched_rels.update(paths)
        pruned_src = {k: sources[k] for k in sorted(sources)
                      if k in matched_rels}
        if pruned_src:
            payload["sources"] = pruned_src
    _write_text_atomic(path, json.dumps(payload, indent=2, ensure_ascii=False))


def _rel_to_vault(pdf: Path, vault_root: Path) -> str:
    """Best-effort vault-relative path; falls back to absolute string."""
    # Both paths are ``resolve()``-d before the relative computation so the
    # stored key is canonical (the mapping is keyed by vault-relative path).
    try:
        return str(pdf.resolve().relative_to(vault_root.resolve()))
    except (OSError, ValueError):
        return str(pdf)


# Item 2.9 (improvement plan 2026-07-04): mapping.json is the audit's ONLY
# writable, hand-curated (unregenerable) file, and both writers below are
# load→mutate→save sequences. Two concurrent /api/audit/mapping POSTs could
# interleave the read-modify-write and silently drop one request's matches —
# a data-loss class the atomic+fsync save alone cannot prevent. One module
# mutex serialises the whole RMW. (Vendored-tree constraint respected:
# threading is stdlib; no new project imports.)
_MAPPING_RMW_LOCK = threading.Lock()


@contextmanager
def mapping_guard(mapping_file: Path):
    """Serialize participating processes on a stable sidecar across JSON replace.

    External editors and older application versions do not participate.
    """
    with _MAPPING_RMW_LOCK:
        mapping_file.parent.mkdir(parents=True, exist_ok=True)
        with mapping_file.with_name(mapping_file.name + '.lock').open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _hash_entry(pdf: Path) -> dict | None:
    """``{"sha256", "size"}`` for *pdf*, or ``None`` when unreadable.

    Best-effort: a read failure (missing file, permissions) leaves the entry
    un-anchored — path matching still works; hash recovery just won't kick in
    for it. Deliberately called OUTSIDE ``_MAPPING_RMW_LOCK`` (the digest
    depends only on the PDF, never on mapping state), so hashing a multi-MB
    file can't serialise concurrent mapping POSTs behind disk-bound work.
    """
    digest = core_hashing.full_hash(pdf)
    if not digest:
        return None
    try:
        size = pdf.stat().st_size
    except OSError:
        size = None
    return {"sha256": digest, "size": size}


def add_matches(
    mapping_file: Path, items: list[tuple[Path, str]], vault_root: Path,
    source: str = "manual",
    *, require_hash: bool = False, expected_state: dict | None = None,
    expected_sha256: str | None = None, expected_fingerprint: dict | None = None,
    validate_evidence: Callable[[], None] | None = None,
) -> None:
    """Append multiple manual PDF→key matches, saving once.

    ``source`` annotates HOW the match was made (2026-08-19): ``"manual"``
    (the default — not stored, it is the implied label) or ``"accession"``
    (the accession UI's link writes). The next scan stamps it into
    ``BridgeResult.source_per_pdf`` so the pairings board can tell the two
    apart; re-curating by hand clears the annotation.
    """
    # Hash outside the lock (see _hash_entry).
    new_hashes: dict[str, dict] = {}
    def check_identity(pdf):
        if expected_fingerprint is not None:
            s = pdf.stat()
            actual = dict(dev=s.st_dev, ino=s.st_ino, size=s.st_size,
                          mtime=s.st_mtime_ns, birth=getattr(s, 'st_birthtime', None))
            # `dev` is renumbered by macOS at boot; comparing it across time
            # refused every mapping after a restart (2026-09-17). Kept inline:
            # the vendored tree may not import accession.review.same_identity.
            if ({k: v for k, v in actual.items() if k != 'dev'}
                    != {k: v for k, v in expected_fingerprint.items() if k != 'dev'}):
                raise ValueError("PDF changed before mapping. Review it again.")
    for pdf, _key in items:
        check_identity(pdf)
        entry = _hash_entry(pdf)
        if entry is None:
            if require_hash:
                # Route callers use strict mode because a manual decision is
                # unregenerable. Direct scan/migration helpers retain the
                # historical best-effort default for compatibility.
                raise ValueError("PDF could not be read and hashed")
            continue
        if expected_sha256 is not None and entry["sha256"] != expected_sha256:
            raise ValueError("PDF content changed. Review it again.")
        new_hashes[_rel_to_vault(pdf, vault_root)] = entry
    with mapping_guard(mapping_file):
        for pdf, _key in items:
            check_identity(pdf)
        _preserve_corrupt_mapping(mapping_file)   # B10/#39
        m = _load_mapping_all(mapping_file)
        matches, no_match, hashes, sources = m.matches, m.no_match, m.hashes, m.sources
        if expected_state is not None:
            if len(items) != 1:
                raise ValueError("Expected mapping state requires one PDF.")
            rel = _rel_to_vault(items[0][0], vault_root)
            actual = dict(keys=sorted(k for k, paths in matches.items() if rel in paths),
                          pending=rel in m.pending, no_match=rel in no_match,
                          exclude=sorted(m.exclude.get(rel, [])))
            if actual != expected_state:
                raise ValueError("Mapping changed. Reconcile again before linking.")
        for pdf, citation_key in items:
            rel = _rel_to_vault(pdf, vault_root)
            if source and source != "manual":
                sources[rel] = source
            else:
                sources.pop(rel, None)
            # A hand link to a key the user had detached this PDF from is a
            # newer decision: lift that one exclusion, keep any other. The
            # DETACH_ALL wildcard goes too — without this, hand-linking a
            # never-auto-map PDF would write a match that `_link` then
            # refuses, so the link would silently not take at the next scan.
            if rel in m.exclude:
                m.exclude[rel] = [k for k in m.exclude[rel]
                                  if k != citation_key and k != DETACH_ALL]
                if not m.exclude[rel]:
                    del m.exclude[rel]
            m.pending.discard(rel)
            # Sweep prior claims FIRST — a PDF belongs to exactly one key.
            # Without this, re-assigning a PDF from key A to key B left it
            # listed under both and the next scan gave both records the PDF
            # (double-counted in drift, tagging, Finder unions and duplicate
            # detection). `add_no_matches` below has always done this sweep,
            # which is what makes the omission here a bug rather than policy.
            for other in list(matches):
                if other == citation_key:
                    continue
                if rel in matches[other]:
                    matches[other].remove(rel)
                    if not matches[other]:
                        del matches[other]
            paths = matches.setdefault(citation_key, [])
            if rel not in paths:
                paths.append(rel)
            no_match.discard(rel)
        hashes.update(new_hashes)
        if validate_evidence is not None:
            validate_evidence()
        save_mapping(mapping_file, matches, no_match, hashes, sources,
                     pending=m.pending, exclude=m.exclude)


def add_match(
    mapping_file: Path, pdf: Path, citation_key: str, vault_root: Path,
    source: str = "manual", *, require_hash: bool = False,
    expected_state: dict | None = None, expected_sha256: str | None = None,
    expected_fingerprint: dict | None = None,
    validate_evidence: Callable[[], None] | None = None,
) -> None:
    """Append a manual PDF→key match, dropping any prior no-match for this PDF."""
    add_matches(mapping_file, [(pdf, citation_key)], vault_root, source=source,
                require_hash=require_hash, expected_state=expected_state,
                expected_sha256=expected_sha256, expected_fingerprint=expected_fingerprint,
                validate_evidence=validate_evidence)


#: The two curated "this PDF has no bib entry" states (2026-09-06). Both are
#: matched by the ladder (no rung claims them); they differ in intent —
#: ``no_match`` = "not a paper for Zotero", ``pending`` = "add to Zotero later".
CURATED_STATES: tuple[str, ...] = ("no_match", "pending")


def set_curated_state(
    mapping_file: Path, pdfs: list[Path], vault_root: Path, state: str, *,
    require_hash: bool = False,
) -> None:
    """Put every *pdf* into ONE curated bucket (``no_match`` | ``pending``),
    dropping any prior match and the other bucket, saving once.

    The list form is what makes a bulk re-sort atomic: a partial write would
    leave the user unable to tell which half landed.
    """
    if state not in CURATED_STATES:
        raise ValueError(f"unknown curated state: {state!r}")
    # Hash outside the lock (see _hash_entry).
    new_hashes: dict[str, dict] = {}
    for pdf in pdfs:
        entry = _hash_entry(pdf)
        if entry is None:
            if require_hash:
                raise ValueError("PDF could not be read and hashed")
            continue
        new_hashes[_rel_to_vault(pdf, vault_root)] = entry
    with mapping_guard(mapping_file):
        _preserve_corrupt_mapping(mapping_file)   # B10/#39
        m = _load_mapping_all(mapping_file)
        matches, no_match, hashes, sources = m.matches, m.no_match, m.hashes, m.sources
        for pdf in pdfs:
            rel = _rel_to_vault(pdf, vault_root)
            for key in list(matches):
                if rel in matches[key]:
                    matches[key].remove(rel)
                    if not matches[key]:
                        del matches[key]
            if state == "no_match":
                no_match.add(rel)
                m.pending.discard(rel)
            else:
                m.pending.add(rel)
                no_match.discard(rel)
            sources.pop(rel, None)   # a no-match is not a match made by anything
        hashes.update(new_hashes)
        save_mapping(mapping_file, matches, no_match, hashes, sources,
                     pending=m.pending, exclude=m.exclude)


def add_no_matches(mapping_file: Path, pdfs: list[Path], vault_root: Path, *,
                   require_hash: bool = False) -> None:
    """Mark multiple PDFs as 'confirmed not in the bibliography', saving once."""
    set_curated_state(mapping_file, pdfs, vault_root, "no_match",
                      require_hash=require_hash)


def add_pending(mapping_file: Path, pdfs: list[Path], vault_root: Path, *,
                require_hash: bool = False) -> None:
    """Mark multiple PDFs as 'to add to Zotero later', saving once."""
    set_curated_state(mapping_file, pdfs, vault_root, "pending",
                      require_hash=require_hash)


def add_no_match(mapping_file: Path, pdf: Path, vault_root: Path, *,
                 require_hash: bool = False) -> None:
    """Mark a PDF as 'confirmed not in the bibliography', dropping any prior match."""
    add_no_matches(mapping_file, [pdf], vault_root,
                   require_hash=require_hash)


def clear_curations(mapping_file: Path, pdfs: list[Path], vault_root: Path) -> int:
    """Remove every curated decision for each *pdf*; return how many changed.

    The undo primitive behind the triage decks and the Not-in-Zotero tab: a
    mis-assigned match, an over-eager "not in the bib"/"pending", or a detach
    exclusion is otherwise unreachable — the writers above can only *replace*
    a decision with another kind, never return the PDF to its un-curated
    state so the next scan re-derives it.

    Drops the PDF from every match list (deleting a key whose last path goes),
    from both curated buckets, from the exclusion map, and — via
    ``save_mapping``'s prune of unreferenced entries — its stored content
    hash. Idempotent: clearing an un-curated PDF changes nothing. One save.
    """
    rels = [_rel_to_vault(p, vault_root) for p in pdfs]
    with mapping_guard(mapping_file):
        _preserve_corrupt_mapping(mapping_file)   # B10/#39
        m = _load_mapping_all(mapping_file)
        matches, no_match, hashes, sources = m.matches, m.no_match, m.hashes, m.sources
        changed = 0
        for rel in rels:
            this = False
            for key in list(matches):
                if rel in matches[key]:
                    matches[key].remove(rel)
                    this = True
                    if not matches[key]:
                        del matches[key]
            if rel in no_match:
                no_match.discard(rel)
                this = True
            if rel in m.pending:
                m.pending.discard(rel)
                this = True
            if rel in m.exclude:
                del m.exclude[rel]
                this = True
            if this:
                changed += 1
        if not changed:
            return 0
        # save_mapping's prune drops the cleared rel's hash AND source label.
        save_mapping(mapping_file, matches, no_match, hashes, sources,
                     pending=m.pending, exclude=m.exclude)
        return changed


def clear_curation(mapping_file: Path, pdf: Path, vault_root: Path) -> bool:
    """Single-PDF form of :func:`clear_curations`; returns whether it changed."""
    return clear_curations(mapping_file, [pdf], vault_root) > 0


class CurationConflict(ValueError):
    """``move_curation`` refused: the destination path already carries curation."""


def curation_of(mapping_file: Path, rel: str) -> list[str]:
    """Which sections of ``mapping.json`` name *rel* (read-only, no lock)."""
    m = _load_mapping_all(mapping_file)
    found = []
    if any(rel in paths for paths in m.matches.values()):
        found.append("matches")
    for name, bucket in (("no_match", m.no_match), ("pending", m.pending),
                         ("exclude", m.exclude), ("hashes", m.hashes),
                         ("sources", m.sources)):
        if rel in bucket:
            found.append(name)
    return found


def move_curation(mapping_file: Path, old_rel: str, new_rel: str) -> bool:
    """Re-key every decision about *old_rel* to *new_rel*; return whether any moved.

    The app's own rename (``POST /api/maintenance/rename``, 2026-09-19) calls
    this inside its journalled op, and its undo calls it with the two paths
    swapped. A rename moves the SAME bytes, so every decision about the file
    follows it: its matches, its curated bucket, its detach exclusions (unlike
    an external rename, which :func:`detach` deliberately treats as a signal
    to re-review), its content hash and its source label.

    Before this, the curation stayed under the old path: the next scan found
    the file again only by content hash (``manual_hash``) and reported the old
    entry as dead curation — 37 files in the 2026 cleanup's Phase 4, repaired
    after the fact by the harness.

    Paths are the stored vault-relative strings, not ``Path`` objects: by the
    time a rename calls this, one of the two files no longer exists and
    :func:`_rel_to_vault` resolves through the filesystem. Refuses with
    :class:`CurationConflict` when *new_rel* already appears anywhere, rather
    than merging two files' decisions into one. One save, under the RMW lock.
    """
    if old_rel == new_rel:
        return False
    with mapping_guard(mapping_file):
        _preserve_corrupt_mapping(mapping_file)   # B10/#39
        m = _load_mapping_all(mapping_file)
        taken = (any(new_rel in paths for paths in m.matches.values())
                 or new_rel in m.no_match or new_rel in m.pending
                 or new_rel in m.exclude or new_rel in m.hashes
                 or new_rel in m.sources)
        if taken:
            raise CurationConflict(
                f"mapping.json already holds decisions for {new_rel}; clear them "
                "before renaming a file to that name.")
        moved = False
        for paths in m.matches.values():
            if old_rel in paths:
                paths[paths.index(old_rel)] = new_rel
                moved = True
        for bucket in (m.no_match, m.pending):
            if old_rel in bucket:
                bucket.discard(old_rel)
                bucket.add(new_rel)
                moved = True
        tables: tuple[dict[str, Any], ...] = (m.exclude, m.hashes, m.sources)
        for table in tables:
            if old_rel in table:
                table[new_rel] = table.pop(old_rel)
                moved = True
        if not moved:
            return False
        save_mapping(mapping_file, m.matches, m.no_match, m.hashes, m.sources,
                     pending=m.pending, exclude=m.exclude)
        return True


#: The wildcard member of an ``exclude`` list: "no automatic rung may claim
#: this PDF for ANY key" (2026-09-07). Not a citation key — Better BibTeX
#: keys are alphanumeric, so ``*`` can never collide with a real one, and it
#: is written only by :func:`detach` under an explicit request.
#:
#: It exists because per-key exclusion is whack-a-mole: the ladder finds the
#: next candidate, so a file that belongs to no bib entry has to be detached
#: once per key the surname+year rung can reach. Measured on the live library,
#: 2 of 127 exclusions had needed a second round (``gomez_penedo_2026.pdf``
#: from both ``gomezpenedoHowInterpretCorrelational2026`` and
#: ``keshavanGenerativeAIChatbots2026``) with no upper bound in sight.
#:
#: A HAND link still wins: :func:`add_matches` drops the wildcard, because an
#: explicit link is a newer decision than a blanket refusal. Undo is
#: :func:`clear_curations`, exactly as for a per-key exclusion.
DETACH_ALL = "*"


def link_would_lift_exclude(exclude_keys: list[str] | None, citation_key: str) -> bool:
    """True when linking *citation_key* would lift a prior detach exclusion.

    A hand link has historically dropped the matching exclude entry (and the
    :data:`DETACH_ALL` wildcard). That made Accession's "Link selected" silently
    re-map a key the user had just unmapped. Callers that want an explicit
    rematch must pass ``accept_excluded`` at the route; this helper is the
    shared predicate so route and tests agree.
    """
    if not exclude_keys:
        return False
    return citation_key in exclude_keys or DETACH_ALL in exclude_keys


def detach(mapping_file: Path, pdf: Path, citation_key: str, vault_root: Path) -> bool:
    """Detach one PDF from one citation key; return whether anything changed.

    The verb the multi-PDF review needs (2026-09-06): an automatically matched
    file had no exit but ``no_match`` (which hides it) or a re-link to a
    different key (which asserts something). Detaching records ``citation_key``
    under ``exclude[rel]`` so no automatic rung claims that PDF for that key
    again — the file returns to the orphan queue at the next scan, where the
    accession deck can identify it — and removes any curated match to that key.
    Undo is :func:`clear_curations`. A later hand link to the same key lifts
    the exclusion (see :func:`add_matches`).

    Keyed by vault-relative path, NOT content hash: a renamed detached file
    loses its exclusion and may be re-absorbed — the rename itself is the
    signal to re-review, and anchoring exclusions by hash would make a byte-
    identical duplicate inherit a decision made about a different file.
    """
    rel = _rel_to_vault(pdf, vault_root)
    with mapping_guard(mapping_file):
        _preserve_corrupt_mapping(mapping_file)   # B10/#39
        m = _load_mapping_all(mapping_file)
        changed = False
        # The wildcard means "no key at all", so it sweeps every curated
        # match too — leaving one behind would keep the PDF mapped by the
        # manual rung while claiming nothing may map it.
        targets = list(m.matches) if citation_key == DETACH_ALL else [citation_key]
        for target in targets:
            paths = m.matches.get(target)
            if paths and rel in paths:
                paths.remove(rel)
                changed = True
                if not paths:
                    del m.matches[target]
        keys = m.exclude.setdefault(rel, [])
        if citation_key not in keys:
            keys.append(citation_key)
            changed = True
        if not changed:
            return False
        save_mapping(mapping_file, m.matches, m.no_match, m.hashes, m.sources,
                     pending=m.pending, exclude=m.exclude)
        return True


def refresh_mapping_hashes(mapping_file: Path, vault_root: Path) -> int:
    """Re-hash every curated entry whose path still resolves; return the count.

    The anchoring limitation this addresses: the stored hash reflects the file
    as of the LAST curation action, and annotating a PDF changes its bytes —
    so *annotate → move* would not be recoverable if nothing re-hashed the
    still-present file in between. Called at SCAN time only (audit/manager._run)
    — cost is O(curated) full-file hashes, which the 2026-07-21 audit finding 2
    moved OFF the per-POST path (`add_match`/`add_no_match` anchor just the
    written PDF); do not "restore" a per-POST call here, that was the fixed
    perf bug. Missing files are left as-is — they are exactly the orphans
    Step 1b needs the OLD hash for.

    Hashing runs outside the RMW lock (same rationale as ``_hash_entry``); the
    lock is retaken only for the re-read + save, and entries curated away by a
    concurrent writer in the window are simply not re-added (`rel in hashes`
    guard against the freshly-loaded state).
    """
    # DURABILITY (2026-09-02, performance plan Phase 5): compare-and-set.
    # Before: the second (locked) pass compared only PRESENCE — `rel in hashes`
    #   — and then overwrote. The hashing between the two loads takes O(curated)
    #   full-file reads, and a `POST /api/audit/mapping` landing in that window
    #   re-anchors a PDF to its CURRENT bytes; the refresh then wrote its own
    #   older digest back over that fresher one, silently reverting a curation
    #   the user had just made.
    # Change: the value seen at the first load is carried through, and an entry
    #   is written only when the freshly loaded value still equals it.
    # Safe because: a concurrent writer's anchor is by definition at least as
    #   fresh as ours (it hashed the file later), so skipping is always the
    #   right answer; an entry nobody touched updates exactly as before, and an
    #   entry curated AWAY in the window is still skipped by the `rel in
    #   hashes` guard this keeps.
    # Lock/thread contract: unchanged — hashing stays outside
    #   _MAPPING_RMW_LOCK, which is still taken only for the re-read + save.
    # Pinned by: tests/audit/test_bridge_reanchor.py (red before the change:
    #   the concurrent anchor was reverted).
    _matches, _no_match, hashes, _sources = _load_mapping_full(mapping_file)
    seen_at_load = dict(hashes)
    refreshed: dict[str, dict] = {}
    for rel in hashes:
        p = vault_root / rel
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        entry = _hash_entry(p)
        if entry is not None:
            refreshed[rel] = entry
    if not refreshed:
        return 0
    with mapping_guard(mapping_file):
        m = _load_mapping_all(mapping_file)
        matches, no_match, hashes, sources = m.matches, m.no_match, m.hashes, m.sources
        updated = 0
        for rel, entry in refreshed.items():
            if rel not in hashes:
                continue                      # curated away in the window
            current = hashes[rel]
            if current != seen_at_load.get(rel):
                continue                      # a fresher anchor won; leave it
            if current != entry:
                hashes[rel] = entry
                updated += 1
        if updated:
            save_mapping(mapping_file, matches, no_match, hashes, sources,
                         pending=m.pending, exclude=m.exclude)
        return updated


# -------- bridge --------


def build_bridge(
    settings: Settings,
    bib_entries: list[core_bib.BibEntry],
    pdfs: list[Path],
    *,
    pdf_text_provider: Callable[[Path], str | None] | None = None,
    zotero_links: dict[Path, list[str]] | None = None,
) -> BridgeResult:
    """Resolve every PDF to a citation key via the six-step priority cascade.

    Steps, highest priority first (a PDF is claimed by the first step that
    matches and never reconsidered — ``matched`` is the guard):
      0. **the Zotero link** (2026-09-19): a PDF that exactly one Zotero item
         links as a ``linked_file``, whose citation key is in the bib, belongs
         to that item. ``zotero_links`` maps each such PDF to the keys of the
         items linking it (built by ``inventory.zotero_link_index``); omitted
         (the CLI and every existing test), the rung does not exist. It beats
         the hand curation of step 1 and the detach exclusions — the
         maintainer's rule after the 2026 cleanup, when every one of 6,248
         PDFs was linked by exactly one item and none disagreed with the
         ladder — and each disagreement is recorded in ``zotero_overrides``
         so the stale decision is reported rather than silently kept;
      1. manual ``mapping.json`` overrides (also seeds the confirmed-no-match
         bucket);
      2a. ``Z_Zotero_Notes/<key>.md`` frontmatter direct PDF pointers;
      2b. frontmatter (author, year) → filename matcher;
      3. wikilink ``[[*.pdf]]`` references in those notes;
      3.5. **embedded-metadata DOI** joined against ``BibEntry.doi``;
      4. residual author+year heuristic on the filename against the bib's
         first authors — a *unique* hit links, a multi-candidate hit is parked
         in ``ambiguous_pdfs`` (never guessed);
      4.5. **printed-text DOI**, gap-fill only — it can supply a missing
         match but never overrule one (see the rung's own comment);
      5. anything still unclaimed and not ambiguous becomes ``unmapped``.

    .. rubric:: Why the DOI rung sits at 3.5 and not higher

    It is placed *below* every note-derived step and *above* the author+year
    heuristic, which means it can only ever claim a PDF the existing ladder
    left unclaimed or would have resolved by filename. That is deliberate: it
    makes the rung purely additive for a library already mapped by steps 1–3,
    so adding it cannot silently re-own thousands of files. Above step 4 is
    equally deliberate — an exact identifier must beat a surname-and-year
    guess whenever the two disagree.

    .. rubric:: Why it exists

    Measured on the maintainer's library (2026-08-18): of 968 PDFs the bridge
    could not place, 525 carried a resolvable identifier and **390 of those
    (74%) were already in _master.bib** — the file and the record both
    present, merely unconnected. Step 4 had missed them because the filename
    year and the bib year disagree (online-first vs issue year: 235 of the 264
    year mismatches were exactly ±1) or because the surname was transliterated
    differently (``boege``/``böge``, ``vandebroucke``/``vandenbroucke``).
    Loosening step 4 to a fuzzy ±1 match would trade those misses for false
    positives across 5,442 files; an exact DOI needs no threshold at all.

    ``pdf_text_provider`` is the injected page-1 text seam. Omitting it
    (the CLI and test path) restricts the rung to embedded PDF metadata,
    which is a degradation, not a failure.

    Read-only: it consults notes, the bib, the mapping file and the PDFs'
    metadata, but writes nothing. The two filename indexes (by name, by
    author+year candidates) are built once up front so each step is a dict
    lookup, not a rescan.
    """
    vault = settings.vault_root
    bib_by_key = {e.citation_key: e for e in bib_entries}

    # ONE read: matches/no_match/hashes must come from the same on-disk
    # version (a two-call load could straddle a concurrent atomic save).
    _mdata = _load_mapping_all(settings.mapping_file)
    matches_raw, no_match_raw, hashes_raw, sources_raw = (
        _mdata.matches, _mdata.no_match, _mdata.hashes, _mdata.sources)
    manual_pdf_to_bib: dict[Path, str] = {}
    # Provenance label per resolved path ("manual" unless annotated —
    # "accession" for links written by the accession UI, 2026-08-19).
    manual_source_by_path: dict[Path, str] = {}
    mapped_rels: set[str] = set()
    for key, paths in matches_raw.items():
        for rel in paths:
            mapped_rels.add(rel)
            try:
                resolved = (vault / rel).resolve()
            except OSError:
                continue
            manual_pdf_to_bib[resolved] = key
            manual_source_by_path[resolved] = sources_raw.get(rel, "manual")
    manual_no_match: set[Path] = set()
    for rel in no_match_raw:
        mapped_rels.add(rel)
        try:
            manual_no_match.add((vault / rel).resolve())
        except OSError:
            continue
    manual_pending: set[Path] = set()
    for rel in _mdata.pending:
        mapped_rels.add(rel)
        try:
            manual_pending.add((vault / rel).resolve())
        except OSError:
            continue
    # Detach exclusions (2026-09-06): ``{resolved path: {key, …}}``; consulted
    # by _link so no automatic rung can re-absorb a detached file.
    excluded_keys: dict[Path, set[str]] = {}
    for rel, keys in _mdata.exclude.items():
        try:
            excluded_keys[(vault / rel).resolve()] = set(keys)
        except OSError:
            continue

    # Hash-anchoring recovery index (axis-ii): map each ORPHANED curated
    # entry (a stored rel that no longer resolves to a file on disk) by its
    # stored content hash, so a renamed/moved PDF is re-linked automatically
    # on the next scan. Only orphaned entries are indexed — a still-present
    # path is claimed directly above and needs no hashing — which bounds the
    # extra hashing below to the actual recovery case. A digest shared by TWO
    # orphaned entries mapped to DIFFERENT keys (byte-identical duplicate PDFs
    # curated under distinct keys, then both moved) is ambiguous — recovery
    # for it is refused rather than picking an iteration-order-dependent
    # winner (the rows resurface as unmapped, where the user can re-curate).
    orphan_hash_to_key: dict[str, str] = {}
    orphan_hash_source: dict[str, str] = {}
    orphan_hash_no_match: set[str] = set()
    orphan_hash_pending: set[str] = set()
    orphan_sizes: set[int] = set()
    orphan_sizes_unknown = False
    ambiguous_digests: set[str] = set()
    if hashes_raw:
        no_match_rels = set(no_match_raw)
        pending_rels = set(_mdata.pending)
        for rel, entry in hashes_raw.items():
            try:
                exists = (vault / rel).is_file()
            except OSError:
                exists = False
            if exists:
                continue  # path still present → claimed by direct matching
            digest = entry["sha256"]
            if entry.get("size") is not None:
                orphan_sizes.add(entry["size"])
            else:
                # Legacy entry without a size: the pre-filter can't screen for
                # it, so candidates must be hashed regardless of their size.
                orphan_sizes_unknown = True
            if rel in no_match_rels:
                orphan_hash_no_match.add(digest)
            elif rel in pending_rels:
                orphan_hash_pending.add(digest)
            else:
                for key, paths in matches_raw.items():
                    if rel in paths:
                        prior = orphan_hash_to_key.get(digest)
                        if prior is not None and prior != key:
                            ambiguous_digests.add(digest)
                        else:
                            orphan_hash_to_key[digest] = key
                            orphan_hash_source[digest] = sources_raw.get(
                                rel, "manual")
                        break
        for digest in ambiguous_digests:
            orphan_hash_to_key.pop(digest, None)
    result_orphan_count = (
        len(orphan_hash_to_key) + len(orphan_hash_no_match)
        + len(orphan_hash_pending) + len(ambiguous_digests)
    )

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
    result.zotero_path_ambiguous = set(getattr(zotero_links, "ambiguous_paths", ()))
    result.notes_with_fm = sum(1 for n in note_by_key.values() if n.has_frontmatter)
    matched: set[Path] = set()

    def _link(p: Path, key: str, source: str) -> bool:
        # Idempotent claim: record the forward+reverse maps and the winning
        # source, then mark the PDF matched so no lower-priority step can
        # re-claim it. First step to call _link for a PDF wins. Returns
        # whether THIS call made the claim.
        if p in matched:
            return False
        if p in result.zotero_path_ambiguous and source not in {"manual", "manual_hash"}:
            return False
        # A successful current Zotero observation outranks stale notes and
        # filename guesses. Removing an attachment must not recreate the old
        # association from its Obsidian mirror. Manual/identifier evidence is
        # retained; no curation or note is rewritten by this read.
        if (zotero_links is not None and not zotero_links.get(p)
                and source in {"fm_pointer", "fm_authoryear", "wikilink", "authoryear"}):
            return False
        # A detached (PDF, key) pair is refused for every AUTOMATIC rung (the
        # manual rung never offers one: add_matches lifts the exclusion). The
        # PDF stays unclaimed here, so a lower rung — or the orphan queue —
        # gets it, which is the whole point of detaching.
        if excluded_keys:
            try:
                rp = p.resolve()
            except OSError:
                rp = p
            ex = excluded_keys.get(rp, ())
            if key in ex or DETACH_ALL in ex:
                result.exclusions_applied += 1
                return False
        result.pdf_to_bib[p] = key
        result.bib_to_pdfs.setdefault(key, []).append(p)
        result.source_per_pdf[p] = source
        matched.add(p)
        return True

    # -- Step 0: the Zotero link --
    # Claimed directly rather than through _link: an exclusion is a hand
    # decision too, and "Zotero wins" covers it (it is reported below).
    if zotero_links:
        for p in pdfs:
            if p in result.zotero_path_ambiguous:
                continue
            linked_by = zotero_links.get(p)
            if not linked_by:
                continue
            distinct = sorted(set(linked_by))
            if len(distinct) > 1:
                result.zotero_multi_links[p] = distinct
                continue
            key = distinct[0]
            if key not in bib_by_key:
                result.zotero_unexported[p] = key
                continue
            try:
                rp = p.resolve()
            except OSError:
                rp = p
            override: dict[str, str | None] | None = None
            curated = manual_pdf_to_bib.get(rp)
            if curated is not None and curated != key:
                override = {"curation": "match", "curated_key": curated}
            elif rp in manual_no_match:
                override = {"curation": "no_match", "curated_key": None}
            elif rp in manual_pending:
                override = {"curation": "pending", "curated_key": None}
            else:
                ex = excluded_keys.get(rp, ())
                if key in ex or DETACH_ALL in ex:
                    override = {"curation": "exclude", "curated_key": None}
            if override is not None:
                result.zotero_overrides[p] = {"zotero_key": key, **override}
            result.pdf_to_bib[p] = key
            result.bib_to_pdfs.setdefault(key, []).append(p)
            result.source_per_pdf[p] = "zotero_link"
            matched.add(p)
            result.zotero_linked += 1

    # -- Step 1: manual mapping (by path) --
    for p in pdfs:
        if p in matched:
            continue   # the Zotero link won (step 0); its override is recorded
        rp = p.resolve()
        if rp in manual_pdf_to_bib:
            _link(p, manual_pdf_to_bib[rp],
                  manual_source_by_path.get(rp, "manual"))
        elif rp in manual_no_match:
            result.confirmed_no_match_pdfs.append(p)
            matched.add(p)
        elif rp in manual_pending:
            result.pending_pdfs.append(p)
            matched.add(p)

    # -- Step 1b: hash-anchoring recovery (renamed/moved curated PDFs) --
    # Only runs when there are orphaned curated entries, and only hashes PDFs
    # whose current rel path is NOT already in the mapping (a "new" location
    # that could be the moved file) and that no path step has claimed. So a
    # library with no moved files pays nothing here. Two further bounds keep
    # a PERMANENT orphan (a curated PDF deleted outright, or moved out of the
    # vault) from turning every future scan into a full-corpus hashing pass:
    # (1) a size pre-filter — a candidate is hashed only when its byte size
    # matches some orphan's recorded size (identical content ⇒ identical
    # size; legacy entries without a size disable the filter for that scan);
    # (2) a hard cap on hashed candidates per scan, WARNING-logged when hit
    # so truncated recovery is never silent.
    if orphan_hash_to_key or orphan_hash_no_match or orphan_hash_pending:
        # Read-only consult of the duplicate scan's persistent hash memo
        # (plan 2026-08-05/4): a size+mtime-valid entry saves the full read.
        # Deliberately NOT saved back from here — the duplicates phase (same
        # scan, later) owns the load→save cycle, so two writers never race.
        from ..core import hash_cache as core_hash_cache
        _hash_memo = core_hash_cache.load_cache()
        hashed_candidates = 0
        recovered = 0
        for p in pdfs:
            if p in matched:
                continue
            rel = _rel_to_vault(p, vault)
            if rel in mapped_rels:
                continue
            if not orphan_sizes_unknown:
                try:
                    if p.stat().st_size not in orphan_sizes:
                        continue
                except OSError:
                    continue
            if hashed_candidates >= _HASH_RECOVERY_MAX_FILES:
                logger.warning(
                    "hash-recovery: candidate cap (%d) reached; remaining "
                    "unmapped PDFs were not checked this scan. Re-curate or "
                    "remove dead mapping entries to shrink the orphan set.",
                    _HASH_RECOVERY_MAX_FILES,
                )
                break
            digest = core_hashing.cached_full_hash(p, _hash_memo)
            hashed_candidates += 1
            if not digest:
                continue
            if digest in orphan_hash_to_key:
                # "manual_hash" unless the curation carried a provenance
                # label — an accession-made link recovered by hash reads
                # "accession_hash", same suffix convention.
                _link(p, orphan_hash_to_key[digest],
                      orphan_hash_source.get(digest, "manual") + "_hash")
                recovered += 1
            elif digest in orphan_hash_no_match:
                result.confirmed_no_match_pdfs.append(p)
                matched.add(p)
                recovered += 1
            elif digest in orphan_hash_pending:
                result.pending_pdfs.append(p)
                matched.add(p)
                recovered += 1
        logger.info(
            "hash-recovery: %d orphaned entrie(s), %d candidate(s) hashed, "
            "%d recovered", result_orphan_count, hashed_candidates, recovered,
        )
    result.mapping_orphans = result_orphan_count

    def pointer_candidates(pointer: str) -> list[Path]:
        # A qualified path is exact, never a basename fallback. Unqualified
        # names are usable only when unique in the active PDF population.
        if "/" in pointer:
            target = (vault / pointer).resolve()
            return [p for p in pdfs_by_name.get(target.name, [])
                    if p.resolve() == target and target.is_relative_to(vault.resolve())]
        candidates = pdfs_by_name.get(pointer, [])
        return candidates if len(candidates) == 1 else []

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
            for candidate in pointer_candidates(pointer):
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
        for fname in note.pdf_pointers or note.pdf_links:
            for candidate in pointer_candidates(fname):
                if candidate not in matched:
                    _link(candidate, key, "wikilink")

    # -- DOI join, split into two rungs by EVIDENCE CLASS --
    #
    # A DOI in a PDF's embedded metadata is the publisher asserting an
    # identity for *this file*. A DOI printed in its page-1 text may belong
    # to a neighbouring article: measured on the maintainer's library, of the
    # 33 cases where a printed DOI disagreed with the author+year match, 13
    # were on multi-article journal pages and named a different paper
    # entirely (a World Psychiatry page resolving to its neighbour's DOI).
    # Both classes recover unmapped PDFs equally well (237 + 152 of 389), so
    # the split costs nothing in recall and removes every risky override:
    #
    #   step 3.5  embedded DOI — trusted, runs BEFORE author+year so an
    #             identifier the publisher asserted can correct a
    #             surname-and-year coincidence (20 real corrections)
    #   step 4.5  printed DOI  — gap-fill only, runs AFTER author+year, so
    #             it can never overrule a match, only supply a missing one
    doi_to_keys: dict[str, list[str]] = defaultdict(list)
    for e in bib_entries:
        if e.doi:
            doi_to_keys[e.doi].append(e.citation_key)

    _doi_memo = core_doi_cache.load_cache() if doi_to_keys else {}
    _memo_dirty = False

    def _doi_for(p: Path, field: str) -> str | None:
        """Memoised DOI lookup in evidence class *field* (``meta``/``text``)."""
        nonlocal _memo_dirty
        hit, cached = core_doi_cache.lookup(p, _doi_memo, field)
        if hit:
            # `lookup` returns the value as stored (the memo also holds a dict
            # under `prov`), so the DOI classes coerce here.
            return _cached_doi(cached)
        doi = core_pdf_ids.pdf_doi(
            p, pdf_text_provider if field == "text" else None
        )
        core_doi_cache.store(p, _doi_memo, field, doi)
        _memo_dirty = True
        return doi

    def _claim_by_doi(p: Path, doi: str | None, source_label: str) -> bool:
        # Re-normalise at the join (2026-08-19): a doi_cache entry written
        # BEFORE normalise_doi stripped control characters still carries the
        # poisoned form (e.g. a trailing \x08 from BMJ page-1 text), and the
        # memo hands it back verbatim — re-normalising here heals every
        # stale memo entry instead of requiring a cache flush.
        doi = core_bib.normalise_doi(doi) if doi else None
        if not doi:
            return False
        keys = doi_to_keys.get(doi)
        if not keys:
            return False
        if len(keys) > 1:
            # One DOI, several bib entries: the BIBLIOGRAPHY is duplicated.
            # Refuse to pick a winner (the bridge's standing posture) and
            # record it — this is a finding about the .bib, not this PDF.
            result.doi_collisions[doi] = sorted(keys)
            return False
        return _link(p, keys[0], source_label)

    # -- Step 3.5: embedded-metadata DOI (trusted; beats author+year) --
    if doi_to_keys:
        linked_meta = 0
        for p in pdfs:
            if p in matched:
                continue
            result.doi_examined += 1
            if _claim_by_doi(p, _doi_for(p, "meta"), "doi"):
                linked_meta += 1
        logger.info(
            "doi-join(metadata): %d examined, %d linked", result.doi_examined,
            linked_meta,
        )

    # -- Step 4: residual author+year fuzzy via bib first-author --
    bib_by_authoryear: dict[tuple[str, str], list[str]] = defaultdict(list)
    for e in bib_entries:
        if not e.authors or not e.year:
            continue
        for last in bibliography_author_keys(e):
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
                if _link(p, cands[0], "authoryear"):
                    linked_here = True
                    break
                # Excluded for that key: fall through to the next candidate
                # form, then to the printed-DOI rung / the orphan queue.
                continue
            union.update(cands)
        if (not linked_here and len(union) > 1
                and (zotero_links is None or zotero_links.get(p))):
            result.ambiguous_pdfs[p] = sorted(union)

    # -- Step 4.5: printed-DOI join (gap-fill only) --
    # Runs AFTER author+year precisely so it can never overrule a match. A
    # DOI printed on page 1 may belong to a neighbouring article on the same
    # journal page; as a gap-filler that costs nothing, as an override it
    # produced 13 wrong re-assignments on the measured library. Only the
    # residual set is examined, and only when a text provider was injected.
    if doi_to_keys and pdf_text_provider is not None:
        linked_text = 0
        for p in pdfs:
            if p in matched or p in result.ambiguous_pdfs:
                continue
            result.doi_text_examined += 1
            if _claim_by_doi(p, _doi_for(p, "text"), "doi_text"):
                linked_text += 1
        logger.info(
            "doi-join(printed): %d examined, %d linked", result.doi_text_examined,
            linked_text,
        )

    if _memo_dirty:
        core_doi_cache.save_cache(_doi_memo)

    # -- Step 5: residual --
    for p in pdfs:
        if p in matched or p in result.ambiguous_pdfs:
            continue
        result.unmapped_pdfs.append(p)

    # -- Step 6: provenance of the residual (Batch C) --
    #
    # A separate pass rather than data threaded out of the DOI rung: it runs
    # over the ~500 unmapped files, not the ~5,000 the rung examines, and it
    # memoises into the SAME doi_cache entry (self-invalidating on
    # size/mtime_ns), so a warm scan pays nothing and a cold one pays a few
    # hundred opens against a scan already measured in minutes. Threading it
    # through `pdf_doi` would save those opens at the cost of complicating the
    # rung that the 2026-08-19 measurement work depends on.
    if result.unmapped_pdfs:
        result.provenance = _classify_residual(
            result.unmapped_pdfs, pdf_text_provider)

    return result


def _prov_memo_usable(entry: dict, has_text_source: bool) -> bool:
    """Whether a cached provenance entry answers the question being asked.

    A ``browser_print`` row's whole remedy is its source URL, and recovering
    one needs page-1 text. An entry computed without a text source therefore
    carries ``source_url: None`` for a reason that has nothing to do with the
    file, so it must not satisfy a later run that DOES have text (the CLI
    scan path injects no provider; the app always does). Every other class is
    decided from metadata alone and caches unconditionally.

    Entries written before this flag existed lack it and are treated as
    text-less, so they are recomputed once and then stick.
    """
    if entry.get("klass") != "browser_print":
        return True
    if entry.get("source_url"):
        return True
    return bool(entry.get("text_seen")) or not has_text_source


def _cached_doi(value) -> str | None:
    """A memoised DOI value, or None for anything that is not a DOI string.

    The cache is field-typed by convention only (``meta``/``text`` hold a DOI
    or ``None``; ``prov`` holds a dict), so the DOI join coerces rather than
    trusting a file a user could hand-edit.
    """
    return value if isinstance(value, str) else None


def _classify_residual(paths, pdf_text_provider) -> dict:
    """Physical class of each residual PDF, memoised in the DOI cache.

    Failures degrade to an absent entry, never an exception: a class is
    advisory routing evidence, and a scan must not fail because one PDF is
    corrupt.
    """
    memo = core_doi_cache.load_cache()
    has_text_source = pdf_text_provider is not None
    dirty = False
    out: dict = {}
    for path in paths:
        hit, cached = core_doi_cache.lookup(path, memo, "prov")
        if hit and isinstance(cached, dict) and _prov_memo_usable(
                cached, has_text_source):
            out[path] = cached
            continue
        try:
            meta = core_pdf_ids.read_pdf_meta(path)
            text = None
            if pdf_text_provider is not None:
                try:
                    text = pdf_text_provider(path)
                except Exception:  # noqa: BLE001 - text is advisory
                    text = None
            # The URL is recovered BEFORE classifying, because it is what
            # corroborates the weak macOS producer marker (see
            # `pdf_provenance._WEAK_BROWSER_MARKERS`). `may_be_browser_print`
            # keeps that extraction off the ~330 files where it decides
            # nothing.
            source_url = None
            if core_pdf_prov.may_be_browser_print(
                    meta.get("producer"), meta.get("creator")):
                source_url = core_pdf_prov.extract_source_url(
                    text, meta.get("info_blob"))
            klass = core_pdf_prov.classify(
                meta.get("producer"), meta.get("creator"),
                int(meta.get("pages") or 0), source_url=source_url)
            entry = {
                "klass": klass,
                "pages": int(meta.get("pages") or 0),
                "producer": (meta.get("producer") or "")[:120],
                # Whether page-1 TEXT was available when this was computed.
                # The memo key is (size, mtime_ns), which cannot distinguish a
                # scan that had the text provider from one that did not — and
                # the CLI path has none. Without this flag a provider-less run
                # would durably memoise `source_url: null` for every printed
                # web page and the GUI would serve that answer forever.
                "text_seen": bool(text),
                # Only the class for which a URL is the whole remedy; storing
                # it for every PDF would memoise a lot of licence boilerplate.
                "source_url": source_url if klass == "browser_print" else None,
            }
        except Exception:  # noqa: BLE001 - never fail a scan over a class
            continue
        out[path] = entry
        core_doi_cache.store(path, memo, "prov", entry)
        dirty = True
    if dirty:
        core_doi_cache.save_cache(memo)
    return out
