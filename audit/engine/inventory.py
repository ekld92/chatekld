"""Per-citation-key unified record + cross-source inventory.

For each BBT citation key, we collect everything we know:
- bib_entry        — from _master.bib (incl. parent keywords)
- zotero_item      — from zotero.sqlite, matched by BBT citation key, with
                     normalized title as a fallback for key-less items
                     (parent tags via .tags, child_notes incl. their tags)
- obsidian_note    — Z_Zotero_Notes/<bbtkey>.md, YAML tags + body
- pdf_paths        — resolved via engine.bridge
- finder_tags      — union across resolved PDFs
- annotations_count_max — max across resolved PDFs (-1 if all unreadable)

Plus, on the side, the BridgeResult exposing unmapped/ambiguous PDFs so
reports can answer the "is this PDF in Zotero at all?" question.
"""

from __future__ import annotations

from ..core.pdf_paths import pdf_files

import os
import re
import time
import unicodedata
from collections import Counter
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
    return [p for p in pdf_files(root) if p.is_file()]


def _path_key(raw: str) -> str:
    """Scan-side alias of :func:`audit.core.zotero.linked_path_key`.

    The renamer looks up the same form (2026-09-19 review A1). Keep this name
    so the inventory module's call sites stay local; do not fork the recipe.
    """
    return zotero.linked_path_key(raw)


class ZoteroLinkIndex(dict[Path, list[str]]):
    """Observed links plus paths whose normalized identity is ambiguous."""
    def __init__(self):
        super().__init__()
        self.ambiguous_paths: set[Path] = set()


def zotero_link_index(
    items: list[zotero.ZoteroItem], pdfs: list[Path]
) -> dict[Path, list[str]]:
    """``{pdf: [citation key of every item linking it]}`` over *pdfs*.

    Only ``linked_file`` attachments count: an imported copy lives in Zotero's
    own storage and is not a vault file. An ``attachments:``-relative path is
    skipped (the base directory is deliberately unset, so it resolves to
    nothing). A linked item without a citation key is retained as an empty
    key so it is disclosed as unexported, never mistaken for an orphan.
    Exact paths win before normalization. A non-exact attachment path folding
    to several PDFs retains an ambiguity marker; it cannot establish absence
    or authorize a heuristic pairing.
    Trashed items and their attachments never reach here: ``read_items``
    excludes them.
    """
    by_key: dict[str, list[Path]] = {}
    exact = {str(p): p for p in pdfs}
    for p in pdfs:
        by_key.setdefault(_path_key(str(p)), []).append(p)
    links = ZoteroLinkIndex()
    for item in items:
        for att in item.attachments:
            raw = att.raw_path or ""
            if att.link_mode != "linked_file" or not raw or raw.startswith("attachments:"):
                continue
            candidates = by_key.get(_path_key(raw), [])
            pdf = exact.get(raw)
            if pdf is None and len(candidates) > 1:
                links.ambiguous_paths.update(candidates)
                continue
            if pdf is None and candidates:
                pdf = candidates[0]
            if pdf is not None:
                links.setdefault(pdf, []).append(item.citation_key or "")
    return links


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


def _index_zotero_by_citation_key(
    items: list[zotero.ZoteroItem],
) -> dict[str, zotero.ZoteroItem]:
    """Index Zotero parents by Better BibTeX citation key; first occurrence wins.

    This is the PRIMARY join (2026-08-12). BBT derives the ``_master.bib`` entry
    key from this exact field, so the link is authoritative where the title join
    below is a heuristic — and the heuristic was actively wrong: measured on the
    maintainer's library, 23 Zotero items were claimed by 2+ bib keys, affecting
    51 records. Four different papers titled "Schizophrenia" (Owen 2016, Marder
    2019, Leucht 2025, Jauhar 2022) all resolved to ONE item, so
    ``apply-zotero-tags`` wrote three of them onto the wrong item.

    First-wins is an explicit policy, not an accident: a Zotero library CAN hold
    two items with the same BBT key (two exist in the maintainer's — BBT only
    guarantees uniqueness at export time, and a duplicated item keeps the pinned
    key). Deterministic first-wins matches ``_index_zotero_by_title`` and keeps
    the scan reproducible; the loser is simply unjoined, never mis-joined.
    """
    idx: dict[str, zotero.ZoteroItem] = {}
    for it in items:
        k = (it.citation_key or "").strip()
        if k and k not in idx:
            idx[k] = it
    return idx


def _index_zotero_by_title(
    items: list[zotero.ZoteroItem],
) -> dict[str, list[zotero.ZoteroItem]]:
    """Index Zotero parents by normalized title, keeping EVERY colliding item.

    The FALLBACK join, used only for a record whose citation key matched no
    Zotero item (an item predating BBT, or one whose key was never generated).
    Empty-title items are skipped.

    Collisions are kept rather than collapsed (2026-08-12). First-wins made a
    duplicate title indistinguishable from a unique one at the call site, so
    the fallback silently picked an arbitrary paper — the same failure the
    citation-key join was introduced to end ("Schizophrenia" matching four
    different papers). :func:`_resolve_title_fallback` refuses an ambiguous
    title outright; leaving the record unjoined is recoverable, writing tags
    onto the wrong paper is not.
    """
    idx: dict[str, list[zotero.ZoteroItem]] = {}
    for it in items:
        n = _norm_title(it.title)
        if n:
            idx.setdefault(n, []).append(it)
    return idx


def _resolve_title_fallback(
    norm_title: str,
    by_title: dict[str, list[zotero.ZoteroItem]],
    claimed_item_keys: set[str],
) -> zotero.ZoteroItem | None:
    """Return the one unambiguous, unclaimed item for *norm_title*, else None.

    Two refusals, both of which used to be silent mis-joins:

    * **Ambiguous** — the title names more than one Zotero item, so no evidence
      picks between them.
    * **Claimed** — the single candidate already belongs to another record via
      the authoritative citation-key join. A keyed item is spoken for; letting a
      title heuristic re-claim it is exactly how one item ended up owned by
      several bib keys, and it is reachable today whenever ``_master.bib`` holds
      a STALE key (the item was re-keyed in BBT after the last export) whose
      title collides with a keyed item.

    Refusing leaves ``rec.zotero_item`` None, which every consumer already
    handles as "no Zotero item for this record" (the tagging writers report
    ``skipped: no_item``). That is the safe direction: a missing join is
    visible and fixable, a wrong one writes tags onto someone else's paper.
    """
    candidates = by_title.get(norm_title) or []
    if len(candidates) != 1:
        return None
    item = candidates[0]
    if item.item_key in claimed_item_keys:
        return None
    return item


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
class OrphanZoteroItem:
    """A Zotero parent no record claimed, reduced to display fields.

    Deliberately NOT the ``ZoteroItem`` itself: that object carries
    ``child_notes``, each holding a full ``body_plain``, so retaining the
    unjoined items verbatim would pin every orphan's note text in memory for
    the lifetime of the cached scan. These five small fields are everything a
    report or a dashboard needs, and ``tags`` is the one that matters — an
    orphan item's tags are invisible to every per-record surface.
    """

    item_key: str
    item_type: str | None = None
    title: str | None = None
    citation_key: str | None = None
    tags: list[str] = field(default_factory=list)
    # Library routing (2026-08-18, additive): a group-library orphan needs
    # ``zotero://select/groups/<id>/items/<key>`` — the personal-library form
    # selects nothing for it, so dropping these fields made the UI's [Zotero]
    # verb silently dead for every group item. An entire group library whose
    # items carry no BBT keys presents as a wall of orphans; the deep link is
    # how the user goes and looks.
    library_type: str | None = None
    group_id: int | None = None


@dataclass
class ZoteroAttachmentRow:
    """One Zotero attachment, flattened with just enough of its parent.

    Collected across EVERY Zotero item, joined or not (2026-08-28). Two reasons
    it is a flat list on the inventory rather than something read off
    ``records``: an attachment on an item no record claimed would otherwise be
    invisible (``OrphanZoteroItem`` deliberately keeps only display fields), and
    the question "what does Zotero hold files for?" is not a per-citation-key
    question at all.

    ``file_owned_by_zotero`` is carried rather than re-derived downstream so the
    report, the confirm text and the route cannot disagree about which side owns
    the bytes.
    """

    item_key: str                 # the ATTACHMENT's key — the API write target
    parent_key: str | None
    parent_title: str | None = None
    parent_citation_key: str | None = None
    parent_item_type: str | None = None
    link_mode: str | None = None
    content_type: str | None = None
    filename: str = ""
    raw_path: str | None = None
    annotation_count: int = 0
    file_owned_by_zotero: bool = False
    library_type: str | None = None
    group_id: int | None = None
    #: The attachment's own display title (additive, 2026-09-18).
    title: str | None = None


@dataclass
class Inventory:
    """The full cross-source join plus the side data the reports need.

    ``records`` is the per-citation-key map every report iterates. ``bridge``
    is kept alongside so reports can answer "is this PDF in Zotero at all?"
    from ``unmapped_pdfs``/``ambiguous_pdfs`` without rerunning resolution.
    ``zotero_error`` is non-None when the Zotero read failed — reports must not
    silently trust an inventory with zero Zotero rows as "nothing is in Zotero".

    ``notes_without_record`` / ``zotero_items_without_record`` are the two
    orphan buckets (2026-08-13). Before them, a reference note whose stem is in
    neither the bib nor the bridge, and a Zotero item no record claimed, were
    dropped in local variables during the build — producing no record, landing
    in no bucket and not even being counted, so "what does the vault hold that
    the audit cannot see?" was unanswerable from a completed scan.
    """

    records: dict[str, Record]
    bridge: eng_bridge.BridgeResult
    pdfs_skipped: list[Path]  # z_item* PDFs (exempt from everything except duplicates)
    zotero_matched_by_title: int  # diagnostic — the FALLBACK join's hit count (RECORDS)
    zotero_error: str | None = (
        None  # diagnostic; reports should not silently trust zero Zotero rows
    )
    # Diagnostic — the PRIMARY join's hit count (2026-08-12). Read together
    # with ``zotero_matched_by_title``: a healthy library is nearly all key,
    # and a title count that starts climbing means BBT keys are going missing.
    zotero_matched_by_key: int = 0
    # Diagnostic — records whose key missed AND whose title fallback was
    # REFUSED (2026-08-12): the title named several items, the one item was
    # already claimed by the key join, or two records contended for it. Each
    # refusal is a record left with no Zotero item that a first-wins fallback
    # would have joined to an arbitrary paper.
    zotero_title_refused: int = 0
    # Annotation counts for the bridge's *unmapped* PDFs, precomputed during
    # the scan so the unread_unzoterod / read_unzoterod reports serve from
    # memory instead of opening ~thousands of PDFs on the request thread.
    # Empty when count_annotations was False or the scan was cancelled before
    # this phase; in that case the reports fall back to a lazy disk read.
    unmapped_annotations: dict[Path, AnnotationsResult] = field(default_factory=dict)
    # Orphan bucket — reference notes under ``zotero_notes_dir`` whose stem
    # matched no record (neither a bib entry nor a bridge-resolved key). Their
    # tags are invisible to every drift report, since a report iterates records.
    notes_without_record: list[Path] = field(default_factory=list)
    # Every attachment Zotero holds, flattened (2026-08-28). Populated from the
    # full item list before the citation-key join, so it is complete regardless
    # of which items became records. Read by the ``zotero_attachments`` report
    # and by the de-link route's candidate check; the audit itself never writes.
    zotero_attachments: list[ZoteroAttachmentRow] = field(default_factory=list)
    # ``attachments:``-relative linked_file paths the pairing index skipped
    # (Zotero's Linked Attachment Base Directory is unset). Counted during
    # the flatten so the summary can disclose them without changing
    # ``zotero_link_index``'s return type.
    zotero_relative_links: int = 0
    # Orphan bucket — Zotero parents no record claimed: items predating BBT,
    # the deterministic first-wins losers of a duplicated citation key, and
    # every item left unjoined by a REFUSED title fallback (the refusals are
    # counted by ``zotero_title_refused``, which keeps no identities).
    zotero_items_without_record: list[OrphanZoteroItem] = field(default_factory=list)
    # Build timestamp (2026-08-18): stamped at construction, so serving it
    # beside the records is ONE atomic read of one object. The manager's
    # get_scan_times() is the wrong source for "how old is THIS snapshot":
    # start_scan zeroes _finished_at while deliberately keeping the previous
    # inventory live (mid-scan the old data read as scanned_at 0), and
    # _finalise stamps it even on an errored scan that kept the old inventory
    # (a stale snapshot read as freshly scanned — silencing the CLI staleness
    # warning in exactly the case it was built for).
    built_at: float = 0.0


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
    pdf_text_provider: Callable[[Path], str | None] | None = None,
) -> Inventory:
    """Build the cross-source inventory.

    ``cancel_fn`` is checked between phases so a long annotation pass can be
    aborted by the manager — the per-PDF pikepdf open is not interruptible.
    ``progress_fn`` (if given) receives human-readable progress lines for the
    unmapped-PDF annotation phase, surfaced in the scan's status feed.
    ``pdf_text_provider`` is forwarded to the bridge's DOI rung; omitting it
    restricts that rung to embedded PDF metadata (see ``build_bridge``).
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
    # B3 (2026-07-13): compare case-insensitively on BOTH sides. The filenames
    # are already lower()ed but the configured prefix kept the user's case, so a
    # prefix saved as ``Z_item`` silently matched nothing.
    skip_prefix = (settings.biblio_skip_prefix or "").lower() or None
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

    # Zotero is read BEFORE the bridge (2026-09-19) because the file Zotero
    # links under an item is the bridge's first rung. A Zotero read failure
    # leaves the observation unavailable (None), not successfully empty.
    bridge_result = eng_bridge.build_bridge(
        settings, entries, pdfs_active, pdf_text_provider=pdf_text_provider,
        zotero_links=(zotero_link_index(zot_items, pdfs_active)
                      if zotero_error is None else None),
    )

    notes_by_stem = _index_obsidian_notes_by_stem(settings)
    # Flatten every attachment before the join runs. Built here rather than
    # from ``records`` so an attachment on an item no record claimed is still
    # visible, and so a cancelled join cannot silently shorten the list.
    zotero_attachments = [
        ZoteroAttachmentRow(
            item_key=att.item_key,
            parent_key=att.parent_key,
            parent_title=item.title,
            parent_citation_key=item.citation_key,
            parent_item_type=item.item_type,
            link_mode=att.link_mode,
            content_type=att.content_type,
            filename=att.filename,
            raw_path=att.raw_path,
            annotation_count=att.annotation_count,
            file_owned_by_zotero=att.file_owned_by_zotero,
            library_type=item.library_type,
            group_id=item.group_id,
            title=getattr(att, "title", None),
        )
        for item in zot_items
        for att in item.attachments
    ]
    zotero_relative_links = sum(
        1 for att in zotero_attachments
        if att.link_mode == "linked_file"
        and (att.raw_path or "").startswith("attachments:")
    )

    zot_by_key = _index_zotero_by_citation_key(zot_items)
    zot_by_title = _index_zotero_by_title(zot_items)
    # Counted in RECORDS, like `matched_by_key` — the old title figure counted
    # distinct normalized titles, so the two diagnostics the docstring tells you
    # to read as a ratio had different denominators.
    matched_by_title = 0
    matched_by_key = 0
    title_refused = 0

    records: dict[str, Record] = {}
    all_keys = set(bib_by_key) | set(bridge_result.bib_to_pdfs)
    # The title fallback runs in a SECOND pass because it must know which items
    # the authoritative key join claimed — and that is only complete once every
    # record has been visited. Parked here as (record, normalized title).
    pending_title: list[tuple[Record, str]] = []
    claimed_item_keys: set[str] = set()
    for key in all_keys:
        rec = Record(citation_key=key, bib_entry=bib_by_key.get(key))
        rec.obsidian_note = notes_by_stem.get(key)
        rec.pdf_paths = list(bridge_result.bib_to_pdfs.get(key, []))
        # Citation key first (authoritative), normalized title only as a
        # fallback for an item Zotero has no BBT key for. The key join needs no
        # bib_entry — the record's own key IS the BBT key — so a PDF-derived
        # record with no `.bib` row can still reach its Zotero item.
        zk = zot_by_key.get(key)
        if zk is not None:
            rec.zotero_item = zk
            claimed_item_keys.add(zk.item_key)
            matched_by_key += 1
        elif rec.bib_entry:
            nt = _norm_title(rec.bib_entry.title)
            if nt:
                pending_title.append((rec, nt))
        for p in rec.pdf_paths:
            rec.finder_tag_set |= finder_tags.read_tag_names(p)
            src = bridge_result.source_per_pdf.get(p)
            if src:
                rec.match_sources.add(src)
        # A cancel used to `break` here, which dropped every REMAINING citation
        # key from `records` — and since `all_keys` is a set, WHICH keys
        # survived was hash-order-arbitrary. The manager stores a cancelled
        # scan's inventory (`_finalise(STATE_CANCELLED, inv=inv)`) and every
        # consumer gates on presence, never state, so reports, the tagging
        # inventory, tag_coverage and the widget all served an arbitrary
        # subset as the whole library (2026-08-12 adjudicated finding, fixed
        # 2026-08-13). The joins are cheap; only the per-record pikepdf
        # annotation reads are worth cancelling — so a cancel now skips those
        # (the default -1 renders as "unknown") and keeps building records.
        if count_annotations and rec.pdf_paths and not _cancelled():
            rec.annotations_count_max = _annotations_for(rec.pdf_paths)
        records[key] = rec

    # Pass 2: the title fallback, now that `claimed_item_keys` is complete.
    # Only records whose citation key matched NOTHING get here, and each still
    # has to clear both refusals in _resolve_title_fallback.
    #
    # Contention on the RECORD side is refused the same way: when two unjoined
    # records normalize to one title, whichever ran first would take the item
    # and the other be refused as "claimed" — and `all_keys` is a set, so which
    # one won could change between runs of the same library. Refusing both is
    # deterministic and keeps the failure visible.
    contested = {nt for nt, n in Counter(nt for _r, nt in pending_title).items() if n > 1}
    for rec, nt in pending_title:
        zt = (None if nt in contested
              else _resolve_title_fallback(nt, zot_by_title, claimed_item_keys))
        if zt is not None:
            rec.zotero_item = zt
            claimed_item_keys.add(zt.item_key)
            matched_by_title += 1
        else:
            title_refused += 1

    # The two orphan buckets, both computed against the FULL intended universe.
    #
    # `notes_without_record` is exactly cancel-safe: `all_keys` is built before
    # the records loop, so a cancel cannot change it.
    #
    # The BBT-key recompute below is belt-and-braces since 2026-08-13: the
    # records loop no longer breaks on cancel (a cancel only skips annotation
    # reads), so `claimed_item_keys` and pass 2's input are complete on every
    # exit path. The recompute is kept because it is a pure function of
    # `all_keys` and `zot_by_key` — cheap insurance that a future early exit
    # from the loop cannot silently turn unvisited records' items into
    # phantom orphans again.
    claimed = set(claimed_item_keys)
    for k in all_keys:
        it = zot_by_key.get(k)
        if it is not None:
            claimed.add(it.item_key)
    notes_without_record = [
        notes_by_stem[stem].path for stem in sorted(set(notes_by_stem) - all_keys)
    ]
    zotero_items_without_record = [
        OrphanZoteroItem(
            item_key=it.item_key,
            item_type=it.item_type,
            title=it.title,
            citation_key=it.citation_key,
            tags=list(it.tags),
            library_type=getattr(it, "library_type", None),
            group_id=getattr(it, "group_id", None),
        )
        for it in zot_items
        if it.item_key not in claimed
    ]

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
        zotero_matched_by_title=matched_by_title,
        zotero_matched_by_key=matched_by_key,
        zotero_title_refused=title_refused,
        zotero_error=zotero_error,
        unmapped_annotations=unmapped_annotations,
        notes_without_record=notes_without_record,
        zotero_items_without_record=zotero_items_without_record,
        zotero_attachments=zotero_attachments,
        zotero_relative_links=zotero_relative_links,
        built_at=time.time(),
    )
