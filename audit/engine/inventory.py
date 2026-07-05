"""Per-citation-key unified record + cross-source inventory.

For each BBT citation key, we collect everything we know:
- bib_entry        — from _master.bib (incl. parent keywords)
- zotero_item      — from zotero.sqlite, matched by normalized title
                     (parent tags via .tags, child_notes incl. their tags)
- obsidian_note    — Z_Zotero_Notes/<bbtkey>.md, YAML tags + body
- pdf_paths        — resolved via engine.bridge
- finder_tags      — union across resolved PDFs
- annotations_count_max — max across resolved PDFs (-1 if all unreadable)

Plus, on the side, the BridgeResult exposing unmapped/ambiguous PDFs so
reports can answer the "is this PDF in Zotero at all?" question.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..core import bib, finder_tags, obsidian, pdf_annotations, zotero
from ..core.pdf_annotations import AnnotationsResult
from . import bridge as eng_bridge

# Upper bound on threads used to read PDF annotations.  pikepdf/qpdf is
# native C++ that releases the GIL while parsing, so a thread pool scales
# across cores here.  Kept a module constant (not an audit setting) to
# avoid widening the api/routes/audit.py config validators; promote it to
# a validated setting later if a UI knob is wanted.
_ANNOTATION_MAX_WORKERS = min(8, (os.cpu_count() or 4))


def _norm_title(s: str | None) -> str:
    """Canonicalize a title for cross-source matching.

    The bib↔Zotero join is by title, but the two stores differ in accents,
    punctuation and casing — so both sides are reduced to the same key:
    NFKD-decompose, drop combining marks, lowercase, and collapse every
    non-alphanumeric run to a single space. ``None``/empty yields ``""``,
    which the indexers treat as "no usable title" and skip.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return s.strip()


def _walk_biblio_pdfs(root: Path) -> list[Path]:
    """List every ``*.pdf`` file under ``root`` (empty if the dir is absent)."""
    if not root.exists():
        return []
    return [p for p in root.rglob("*.pdf") if p.is_file()]


def _index_obsidian_notes_by_stem(settings: Settings) -> dict[str, obsidian.NoteInfo]:
    """Index ``Z_Zotero_Notes`` by file stem (the BBT key by convention).

    The user's convention names each note ``<citation_key>.md``, so keying by
    stem lets the inventory join a note to its record by citation key. Ignored
    directories are pruned and unreadable notes dropped. Reads go through the
    cached ``obsidian.read_note`` so this walk shares parses with the bridge's.
    """
    notes_dir = settings.zotero_notes_dir
    if not notes_dir.exists():
        return {}
    index: dict[str, obsidian.NoteInfo] = {}
    for p in notes_dir.rglob("*.md"):
        if any(part in settings.ignored_dirs for part in p.parts):
            continue
        note = obsidian.read_note(p)
        if note:
            index[p.stem] = note
    return index


def _index_zotero_by_title(
    items: list[zotero.ZoteroItem],
) -> dict[str, zotero.ZoteroItem]:
    """Index Zotero parents by normalized title; first occurrence wins.

    Keeping the *first* item for a colliding normalized title is deliberate —
    duplicate-titled Zotero entries are rare, and a stable first-wins choice
    keeps the join deterministic. Empty-title items are skipped.
    """
    idx: dict[str, zotero.ZoteroItem] = {}
    for it in items:
        n = _norm_title(it.title)
        if n and n not in idx:
            idx[n] = it
    return idx


@dataclass
class Record:
    """Everything the audit knows about one citation key, joined across sources.

    A record exists for every key seen in the bib *or* produced by the bridge.
    The four source slots (``bib_entry``, ``zotero_item``, ``obsidian_note``,
    ``pdf_paths``) are each optional; a "fully triangulated" record has all
    four. ``finder_tag_set`` and ``annotations_count_max`` are aggregated
    across the record's resolved PDFs, and ``match_sources`` records *how* the
    bridge tied each PDF to this key (manual / fm_pointer / wikilink / …).
    """

    citation_key: str
    bib_entry: bib.BibEntry | None = None
    zotero_item: zotero.ZoteroItem | None = None
    obsidian_note: obsidian.NoteInfo | None = None
    pdf_paths: list[Path] = field(default_factory=list)
    finder_tag_set: set[str] = field(default_factory=set)
    annotations_count_max: int = -1
    match_sources: set[str] = field(
        default_factory=set
    )  # {"manual","wikilink","authoryear"}

    @property
    def zotero_note_tags(self) -> set[str]:
        """Union of tags across all the parent's Zotero child notes."""
        if not self.zotero_item:
            return set()
        out: set[str] = set()
        for cn in self.zotero_item.child_notes:
            out.update(cn.tags)
        return out

    @property
    def obs_tags(self) -> set[str]:
        """The Obsidian note's YAML tags as a set (empty if no note)."""
        return set(self.obsidian_note.tags) if self.obsidian_note else set()

    @property
    def bib_keywords(self) -> set[str]:
        """The bib entry's keywords (≈ Zotero parent tags once BBT-exported)."""
        return self.bib_entry.keywords if self.bib_entry else set()

    @property
    def has_zotero_child_note(self) -> bool:
        """True iff the matched Zotero parent has ≥1 child note (the read proxy)."""
        return bool(self.zotero_item and self.zotero_item.child_notes)


@dataclass
class Inventory:
    """The full cross-source join plus the side data the reports need.

    ``records`` is the per-citation-key map every report iterates. ``bridge``
    is kept alongside so reports can answer "is this PDF in Zotero at all?"
    from ``unmapped_pdfs``/``ambiguous_pdfs`` without rerunning resolution.
    ``zotero_error`` is non-None when the Zotero read failed — reports must not
    silently trust an inventory with zero Zotero rows as "nothing is in Zotero".
    """

    records: dict[str, Record]
    bridge: eng_bridge.BridgeResult
    pdfs_skipped: list[Path]  # z_item* PDFs (exempt from everything except duplicates)
    zotero_matched_by_title: int  # diagnostic
    zotero_error: str | None = (
        None  # diagnostic; reports should not silently trust zero Zotero rows
    )
    # Annotation counts for the bridge's *unmapped* PDFs, precomputed during
    # the scan so the unread_unzoterod / read_unzoterod reports serve from
    # memory instead of opening ~thousands of PDFs on the request thread.
    # Empty when count_annotations was False or the scan was cancelled before
    # this phase; in that case the reports fall back to a lazy disk read.
    unmapped_annotations: dict[Path, AnnotationsResult] = field(default_factory=dict)


def _annotations_for(paths: list[Path]) -> int:
    """Max annotation count across a record's PDFs (-1 if none / all unreadable).

    A record may resolve to several copies of the same paper; the *most*
    annotated copy is the best evidence the paper was read, so the max wins.
    Serial on purpose — this runs over a record's handful of mapped PDFs, the
    small set; the heavy *unmapped*-PDF pass is the one parallelised in
    :func:`_read_annotations_parallel`.
    """
    if not paths:
        return -1
    best = -1
    for p in paths:
        n = pdf_annotations.count_annotations(p)
        if n > best:
            best = n
    return best


def _read_annotations_parallel(
    paths: list[Path],
    *,
    cancel_fn: Callable[[], bool] | None = None,
    progress_fn: Callable[[str], None] | None = None,
    max_workers: int = _ANNOTATION_MAX_WORKERS,
) -> dict[Path, AnnotationsResult]:
    """Read annotations for ``paths`` concurrently.

    Returns ``{path: AnnotationsResult}``.  All reads are submitted to the
    pool **eagerly** up front; only ``max_workers`` run at once and the rest
    queue.  ``cancel_fn`` is polled between completions — on a cancel the
    still-*queued* (not-yet-started) reads are dropped and the up-to
    ``max_workers`` reads already in flight are allowed to finish (a single
    pikepdf open is not interruptible), so worst-case cancel latency is one
    slow PDF, not the whole remaining backlog.  The partial result collected
    so far is returned.

    ``progress_fn`` (if given) receives a coarse ``done/total`` tick every
    500 files — it is invoked only from this collecting thread, so the
    callback need not be re-entrant.
    """
    results: dict[Path, AnnotationsResult] = {}
    if not paths:
        return results
    total = len(paths)
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        # Eager submit: every path becomes a queued future immediately; the
        # pool runs at most ``max_workers`` concurrently.
        futures = {ex.submit(pdf_annotations.read_annotations, p): p for p in paths}
        for fut in as_completed(futures):
            if cancel_fn is not None and cancel_fn():
                # Load-bearing: ThreadPoolExecutor.__exit__ calls
                # shutdown(wait=True) WITHOUT cancel_futures, which would let
                # every still-queued read run to completion before the `with`
                # returns — defeating cancel entirely.  Calling shutdown with
                # cancel_futures=True here is the only thing that actually
                # drops the backlog.  Do not remove this in favour of the
                # implicit __exit__ shutdown.
                ex.shutdown(cancel_futures=True)
                break
            results[futures[fut]] = fut.result()
            done += 1
            if progress_fn is not None and done % 500 == 0:
                progress_fn(f"  …annotations {done}/{total}")
    return results


def build_inventory(
    settings: Settings,
    *,
    count_annotations: bool = True,
    cancel_fn: Callable[[], bool] | None = None,
    progress_fn: Callable[[str], None] | None = None,
) -> Inventory:
    """Build the cross-source inventory.

    ``cancel_fn`` is checked between phases so a long annotation pass can be
    aborted by the manager — the per-PDF pikepdf open is not interruptible.
    ``progress_fn`` (if given) receives human-readable progress lines for the
    unmapped-PDF annotation phase, surfaced in the scan's status feed.
    """

    def _cancelled() -> bool:
        return cancel_fn is not None and cancel_fn()

    entries = bib.parse_bib(settings.master_bib)
    bib_by_key = {e.citation_key: e for e in entries}

    all_pdfs = _walk_biblio_pdfs(settings.biblio_articles_dir)
    # An empty ``biblio_skip_prefix`` must mean "skip nothing", not "skip
    # everything".  Without this guard ``str.startswith("")`` is True for
    # every filename and the active set collapses to zero, which silently
    # produces an empty inventory.
    skip_prefix = settings.biblio_skip_prefix or None
    if skip_prefix:
        pdfs_skipped = [
            p for p in all_pdfs if p.name.lower().startswith(skip_prefix)
        ]
        pdfs_active = [
            p for p in all_pdfs if not p.name.lower().startswith(skip_prefix)
        ]
    else:
        pdfs_skipped = []
        pdfs_active = list(all_pdfs)

    bridge_result = eng_bridge.build_bridge(settings, entries, pdfs_active)

    notes_by_stem = _index_obsidian_notes_by_stem(settings)

    zot_items: list[zotero.ZoteroItem] = []
    zotero_error: str | None = None
    if settings.zotero_sqlite.exists():
        try:
            zot_items = zotero.read_items(
                settings.zotero_sqlite, settings.zotero_storage
            )
        except Exception as e:
            zotero_error = f"{type(e).__name__}: {e}"
            zot_items = []
    else:
        # No DB at the configured (often default ~/Zotero) path. Surface an
        # actionable note so the empty Zotero reports are explained — a second
        # user without Zotero, or with it installed elsewhere, otherwise sees
        # blank reports with no reason. zotero_error flows to the status feed
        # (manager.py) and the inventory payload (serialize.py).
        zotero_error = (
            f"Zotero database not found at {settings.zotero_sqlite}. If you use "
            f"Zotero, set its path in Library Audit settings; otherwise the "
            f"Zotero reports stay empty (this is not an error)."
        )
    zot_by_title = _index_zotero_by_title(zot_items)
    matched_zot_titles: set[str] = set()

    records: dict[str, Record] = {}
    all_keys = set(bib_by_key) | set(bridge_result.bib_to_pdfs)
    for key in all_keys:
        rec = Record(citation_key=key, bib_entry=bib_by_key.get(key))
        rec.obsidian_note = notes_by_stem.get(key)
        rec.pdf_paths = list(bridge_result.bib_to_pdfs.get(key, []))
        if rec.bib_entry:
            zt = zot_by_title.get(_norm_title(rec.bib_entry.title))
            if zt is not None:
                rec.zotero_item = zt
                matched_zot_titles.add(_norm_title(rec.bib_entry.title))
        for p in rec.pdf_paths:
            rec.finder_tag_set |= finder_tags.read_tag_names(p)
            src = bridge_result.source_per_pdf.get(p)
            if src:
                rec.match_sources.add(src)
        if count_annotations and rec.pdf_paths:
            if _cancelled():
                break
            rec.annotations_count_max = _annotations_for(rec.pdf_paths)
        records[key] = rec

    # Precompute annotation counts for the *unmapped* PDFs (the set the
    # records loop above never touches) so unread_unzoterod / read_unzoterod
    # serve from memory instead of re-opening every PDF on the request
    # thread.  Parallelised because this is the heavy phase (thousands of
    # PDFs).  Skipped when annotations were opted out or a cancel landed.
    unmapped_annotations: dict[Path, AnnotationsResult] = {}
    if count_annotations and bridge_result.unmapped_pdfs and not _cancelled():
        if progress_fn is not None:
            progress_fn(
                f"Reading annotations for {len(bridge_result.unmapped_pdfs)} "
                "unmapped PDFs (parallel)…"
            )
        unmapped_annotations = _read_annotations_parallel(
            list(bridge_result.unmapped_pdfs),
            cancel_fn=cancel_fn,
            progress_fn=progress_fn,
        )

    return Inventory(
        records=records,
        bridge=bridge_result,
        pdfs_skipped=pdfs_skipped,
        zotero_matched_by_title=len(matched_zot_titles),
        zotero_error=zotero_error,
        unmapped_annotations=unmapped_annotations,
    )
