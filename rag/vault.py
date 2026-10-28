"""Obsidian vault indexing, status, retrieval caches, and chat entrypoints.

Owns :class:`ObsidianVaultManager` (the ``obsidian_manager`` singleton) and the
end-to-end indexing pipeline. The deep implementation notes — the streaming
indexing pipeline, the checkpoint cadence, the two vector-store backends, the
retrieval mechanics (RRF / MMR / rerank / wikilink), and the BM25/reranker/wikilink
cache contracts — live in ``rag/CLAUDE.md``; this module is their implementation.

The one thing worth internalising before editing here is the **lock hierarchy**.
The manager is touched concurrently by: the request thread serving chat, the
daemon indexing thread, the launch-time prewarm thread, and the UI status poller.
Several locks coordinate them, and they have an **ordering** that must be respected
to stay deadlock-free — see the :class:`ObsidianVaultManager` docstring for the
full map. Two rules summarise it: (1) the only lock *nesting* involving the meta
cache is ``_status_lock`` → ``_meta_cache_lock`` (``get_status`` reads meta while
holding the status lock), so no path may take ``_status_lock`` while already
holding ``_meta_cache_lock`` — taking ``_meta_cache_lock`` *alone*, as the many
``_read_index_meta`` read paths do, is always safe; (2) the multi-GB
``self._index`` is published under ``_rw_lock`` write while the slow insert loop
runs lock-free, so chat reads never block on a multi-hour reindex.
"""
import base64
import gc
import hashlib
import json
import logging
from core.extraction_status import ExtractionStatus, TextRead, status_of

import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional, cast
from urllib.parse import unquote

from llama_index.core import (
    VectorStoreIndex,
    StorageContext,
    load_index_from_storage,
    Document as LlamaDocument,
)
from llama_index.core.indices.base import BaseIndex
# Track 5.6: the batched insert path replicates BaseIndex.insert() (which is
# run_transformations + insert_nodes + set_document_hash — verified against
# llama-index-core 0.14.22) over K documents at once, so it needs the same
# transformation runner insert() uses.
from llama_index.core.ingestion import run_transformations
from llama_index.core.schema import TextNode
from llama_index.core.node_parser import MarkdownNodeParser as _MarkdownNodeParser
from llama_index.core.node_parser import SentenceSplitter as _SentenceSplitter

from rag.docstore_access import node_count
from rag.disk_docstore import DiskKVStore, load_disk_docstore
from core.perf_metrics import measured, span
from core.constants import (
    DEFAULT_EMBED,
    OBSIDIAN_INDEX_DIR,
    OBSIDIAN_CACHE_DIR,
    OBSIDIAN_INDEX_VERSION,
    OBSIDIAN_EXCLUDED_DIR_NAMES,
    EXACT_BLOCKED,
    MD_MAX_CHUNK_TOKENS,
    PDF_MAX_PAGES,
    SYSTEM_ROOTS,
    VAULT_MD_EXTS,
    VAULT_BINARY_EXTS,
    VAULT_IMAGE_EXTS,
)
from core.config import load_config, load_config_readonly
from core.utils import ReaderWriterLock, RagOperationLock, log_storage_deletion, nfc
from core.providers import get_provider
from rag.lancedb_store import (
    VECTOR_BACKEND_LANCEDB,
    VECTOR_BACKEND_SIMPLE,
    compact_lancedb_vector_store,
    lancedb_available,
    lancedb_dir,
    lancedb_table_count,
    make_lancedb_vector_store,
)
# Plan 9 step 0: the low-level index-store file primitives live in the leaf
# module rag/storage_io.py so rag/sidecars.py can use them without importing
# rag.vault. Re-exported here — `from rag.vault import _write_json_atomic`
# and every in-module call site keep resolving exactly as before.
from rag.storage_io import (  # noqa: F401 — re-exported for callers/tests
    _write_json_atomic,
    _json_file_has_complete_tail,
    _simple_vector_store_has_any_embedding,
)
from rag.storage_io import vault_key as _storage_vault_key
# Plan 9 step 2: the BM25 + wikilink retrieval-cache sidecars live in
# rag/sidecars.py. RetrievalCacheMixin is mixed into ObsidianVaultManager
# below, so every method it carries is still reached as
# `obsidian_manager._get_bm25_retriever(...)`. The names are re-exported so
# `rag.vault._WikilinkGraph` / `_OBSIDIAN_WIKILINK_RE` and the in-module call
# sites (the attachment resolver still uses the link regexes) resolve as before.
# Plan 9 step 4b: the eight index_vault phases live in rag/build.py as
# IndexBuildMixin. `index_vault` itself — the orchestration and every early
# return — stays here. The four globals the phases need (OBSIDIAN_INDEX_DIR,
# get_provider, lancedb_available, load_config) are read HERE, at each call
# site, and passed in: the suite patches them as `rag.vault.<name>`, and a
# global read relocated into rag/build.py would ignore those patches.
from rag.build import (  # noqa: F401 — re-exported for callers/tests
    IndexBuildMixin,
    _PersistTarget,
)
from rag.sidecars import (  # noqa: F401 — re-exported for callers/tests
    RetrievalCacheMixin,
    _WikilinkGraph,
    _closest_note_by_dir,
    _BM25_SIDECAR_DIRNAME,
    _BM25_SIDECAR_META_FILENAME,
    _WIKILINK_SIDECAR_FILENAME,
    _OBSIDIAN_WIKILINK_RE,
    _INLINE_LINK_RE,
    _NON_ATTACHMENT_SCHEMES,
)
from services.vision import glm_ocr_manager, vision_manager
from pdf_extractor import (
    extract_structured_from_pdf,
    get_pdf_page_count,
    EXTRACT_MAX_PAGES_PER_CALL,
)

logger = logging.getLogger(__name__)


_LLAMAINDEX_PERSIST_FILES = (
    "default__vector_store.json",
    "docstore.json",
    "index_store.json",
    "graph_store.json",
    "image__vector_store.json",
)

#: Wall-clock ceiling for the one-off archive size walk (P7.5). A multi-GB
#: index on a cold/iCloud-backed disk can take a while to stat; the size is a
#: disclosure nicety, so past this the walk gives up and reports nothing rather
#: than delaying a rebuild the user is waiting on.
_DIR_SIZE_WALK_BUDGET_S = 5.0


def _dir_size_note(path: str) -> str:
    """Human-readable size of *path*, or ``""`` when it can't be measured.

    Walked ONCE at archive time (never on the indexing hot path) and bounded by
    :data:`_DIR_SIZE_WALK_BUDGET_S` — a partial walk returns ``""`` rather than
    a misleading undercount. Follows no symlinks and swallows every OS error:
    this exists to tell the user how much disk an archive costs, and must never
    be the reason an archive or a rebuild fails.
    """
    deadline = time.monotonic() + _DIR_SIZE_WALK_BUDGET_S
    total = 0
    try:
        for root, dirs, files in os.walk(path, followlinks=False):
            if time.monotonic() > deadline:
                return ""   # incomplete → say nothing rather than under-report
            for name in files:
                try:
                    st = os.stat(os.path.join(root, name), follow_symlinks=False)
                except OSError:
                    continue
                total += st.st_size
    except OSError:
        return ""
    if total <= 0:
        return ""
    for unit, cutoff in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if total >= cutoff:
            return f"{total / cutoff:.1f} {unit}"
    return f"{total} B"




# Chat-path bound on waiting for _index_mutation_lock (item 2.1): above the
# longest legitimate holder (a mid-run checkpoint re-serialising the store),
# below the SSE consumer stall floor (SSE_SINGLE_SHOT_FLOOR_S + margin) so the
# clean error beats the generic stall message. Patched down in tests.
_RETRIEVAL_LOCK_TIMEOUT_S = 120.0

# Minimum remaining agent budget (s) below which read_note refuses to START a
# fresh uncached-PDF extraction (item 2.4). Typical extracts finish in seconds;
# the floor only rejects starts that would mostly run past the turn's death.
_FRESH_EXTRACT_MIN_BUDGET_S = 20.0


def _iter_vault_scan_files(vault_root: Path, user_excluded_dirs: set,
                           failed_prefixes: Optional[set] = None):
    """Yield ``(path, apparent-rel)`` for every regular file under *vault_root*,
    pruning excluded directories at descent time (Track 5.4, 2026-07-04).

    *failed_prefixes* (optional, mutable): when a SUB-directory cannot be
    ``scandir``'d (permissions revoked, iCloud eviction of one attachments
    folder), its vault-relative prefix (``"sub/dir/"``) is recorded here so the
    caller's stale sweep can withhold every doc under that subtree instead of
    de-indexing it. Without this, a run that otherwise scans ≥1 file bypasses
    the root empty-scan guard, and every doc under the unreadable subtree gets
    swept as stale — a durable partial purge on lancedb from one transient blip
    (2026-07-20 audit finding 4). The root failure still raises (see below).

    DEFECT this replaces: the scan pass ran ``sorted(vault_root.rglob("*"))`` +
    a per-file ``p.resolve()`` — it materialised and sorted the WHOLE tree
    (including every file inside ``.git``/``.obsidian``/``.trash`` and the
    user's ``vault_exclude_dirs``, which were listed and then discarded one by
    one) and paid a symlink-resolving syscall chain per entry. Worst on iCloud
    vaults, where merely listing a large excluded folder can be slow.

    Equivalence with the old ``rglob`` + ``_should_skip_path`` filter:
    * Reserved-name dirs (``OBSIDIAN_EXCLUDED_DIR_NAMES``) and user exclusions
      (vault-relative prefixes) are pruned BEFORE descent, so their contents are
      simply never generated — same surviving file set, since a skipped dir's
      descendants were all individually skipped before. *vault_root* arrives
      ``.resolve()``d, so for non-symlink entries the apparent rel used here
      equals the resolved rel the old check compared (deliberate divergence: a
      reserved name in the path of *vault_root itself* no longer blanks the
      whole vault — the old full-``p.parts`` check scanned above the root too).
    * Symlinked FILES keep the old resolve-gate: yielded only when their target
      resolves under the (resolved) root and outside user exclusions — a vault
      symlink can still not pull outside content into the index.
    * Symlinked DIRS are not descended (matches ``os.walk`` and Python 3.13+
      ``rglob`` defaults). The old 3.12 ``rglob`` descended them, but every file
      inside either resolved outside the root (skipped by the old per-file
      check) or resolved inside it (indexed TWICE, under both the real and the
      symlinked rel — a duplicate-chunk footgun, not a feature). The real files
      still index at their real paths.

    Yield order is scandir order — the consumer sorts its per-type buckets,
    which reproduces the old globally-sorted order per bucket (a sorted list's
    subsequence order equals the sorted subsequence; posix Path ordering is
    plain string ordering, and rel-key sorting equals full-path sorting under a
    shared root prefix). Pinned by TestVaultScanWalk.
    """
    def _rel_excluded(rel: str) -> bool:
        for excluded in user_excluded_dirs:
            if rel == excluded or rel.startswith(excluded + "/"):
                return True
        return False

    stack = [(str(vault_root), "")]
    while stack:
        dir_abs, dir_rel = stack.pop()
        try:
            entries = os.scandir(dir_abs)
        except OSError:
            # Unreadable SUB-dir. The ROOT is different — swallowing its failure
            # yields an empty scan indistinguishable from an emptied vault, and
            # an incremental run would then sweep the ENTIRE index as stale
            # (2026-07-20 audit H2). Propagate so the loader can abort instead.
            if dir_rel == "":
                raise
            # A sub-dir failure is NOT a deletion: record its prefix so the
            # stale sweep withholds every doc under it (2026-07-20 audit
            # finding 4). The old walk silently dropped these, so if any other
            # file scanned, the subtree's docs were purged as stale. dir_rel
            # already carries the trailing "/" (pushed as rel + "/" below), so
            # it is a clean prefix for `source.startswith(prefix)`.
            if failed_prefixes is not None:
                failed_prefixes.add(dir_rel)
            continue
        with entries:
            for entry in entries:
                rel = f"{dir_rel}{entry.name}"
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name in OBSIDIAN_EXCLUDED_DIR_NAMES or _rel_excluded(rel):
                            continue
                        stack.append((entry.path, rel + "/"))
                    elif entry.is_file(follow_symlinks=False):
                        if _rel_excluded(rel):
                            continue
                        yield Path(entry.path), rel
                    elif entry.is_symlink():
                        # Symlink-to-file: old resolve-gate preserved (see
                        # docstring); symlink-to-dir falls through un-descended.
                        try:
                            resolved = Path(entry.path).resolve()
                            resolved_rel = resolved.relative_to(vault_root).as_posix()
                        except (OSError, ValueError):
                            continue
                        if resolved.is_file() and not _rel_excluded(resolved_rel):
                            yield Path(entry.path), rel
                except OSError:
                    continue


class ObsidianVaultManager(IndexBuildMixin, RetrievalCacheMixin):
    """Singleton owning the vault index, its caches, and the chat/indexing entrypoints.

    Provider-agnostic (it takes the chat/embed/provider names per call rather than
    binding a backend) and **heavily concurrent**. The lock map, with each lock's
    job and the ordering rules that keep it deadlock-free:

    * ``_op_lock`` (:class:`RagOperationLock`) — coarse admission control: only one
      long operation (an index run, or a Note-Refactor write) at a time, with a TTL
      so a crashed worker self-expires. ``try_acquire_lock`` RETURNS the acquisition
      epoch (a per-operation token the caller must hold on its own stack) and
      ``release_lock``/``heartbeat`` take it back as a required argument. The epoch
      is deliberately NOT stored on this shared singleton: a manager-level
      ``_lock_epoch`` attribute meant any new acquisition overwrote the token a
      still-running previous holder would later release with — e.g. cancel an index
      run, start a refactor Apply, and the indexer's ``finally`` released the
      *refactor's* lock mid-batch (improvement plan 2026-07-04, item 1.5).
    * ``_rw_lock`` (:class:`ReaderWriterLock`) — guards ``self._index`` *publication*.
      Chat takes the read lock; the indexer takes the write lock only to publish the
      index, to checkpoint, and for the final persist — the multi-hour insert loop
      runs **outside** it, so chat is never blocked for the duration of a reindex
      (double-checked locking on the lazy load).
    * ``_index_mutation_lock`` (``Lock``) — serialises operations that mutate the
      LlamaIndex internals: ``idx.insert`` / ``idx.delete_ref_doc``, the checkpoint
      persist, lancedb compaction, and the *retrieval* phase of a query — so a chat
      iterating the vector store cannot race an indexer insert ("dict changed size").
    * ``_meta_write_lock`` — serialises the two WRITERS of ``obsidian_meta.json``
      against each other (``_write_index_meta`` and ``cancel_indexing``'s
      read-modify-write, which shared no lock before 2026-09-02). Held alone, or
      as the outer of ``_meta_write_lock`` → ``_meta_cache_lock``; **never** taken
      while holding ``_status_lock``, and never taken by a reader.
    * ``_status_lock`` — guards the indexing state machine (``_index_state`` etc.).
      ``_meta_cache_lock`` (the ``obsidian_meta.json`` read cache) is nested *inside*
      ``_status_lock`` by ``get_status`` (the order is ``_status_lock`` →
      ``_meta_cache_lock``); the ordering invariant is therefore that no path may
      acquire ``_status_lock`` while holding ``_meta_cache_lock``. Taking
      ``_meta_cache_lock`` on its own — as ``_read_index_meta``'s many other callers
      do — is safe (a lock held alone cannot deadlock).
    * ``_messages_lock`` — guards the bounded status-message ring (drained by the poll).
    * Per-cache build locks — ``_bm25_build_lock`` / ``_wikilink_build_lock`` /
      ``_reranker_load_lock`` / ``_prewarm_lock`` — each serialises one lazy, cached
      build so concurrent first-Sends don't duplicate an expensive load.

    Every field is initialised in ``__init__`` with an inline note on what guards it;
    read those alongside this map.
    """

    def __init__(self) -> None:
        self._vault_path: Optional[str] = None
        # Holds either a freshly-built VectorStoreIndex or one re-hydrated by
        # load_index_from_storage (typed as BaseIndex by llama-index).  Both
        # expose the methods we use (insert / delete_ref_doc / docstore /
        # storage_context), so widen the static type accordingly.
        self._index: Optional[BaseIndex[Any]] = None
        self._rw_lock: ReaderWriterLock = ReaderWriterLock()
        # Serialises mutating operations on the LlamaIndex store (idx.insert,
        # idx.delete_ref_doc) with chat-time retrieval, so a query iterating
        # the vector store dict cannot race with an indexer insert and raise
        # RuntimeError("dictionary changed size during iteration").
        self._index_mutation_lock: threading.Lock = threading.Lock()
        self._docstore_generation: int = 0
        self._index_generation: int = 0
        self._op_lock: RagOperationLock = RagOperationLock()
        self._stop_event: threading.Event = threading.Event()
        self._status_cb: Optional[Callable[[str], None]] = None
        self._index_state: str = "idle"  # "idle" | "running" | "paused" | "done" | "error"
        self._pause_requested: bool = False
        # The op-lock epoch of the CURRENT indexing run, so `cancel_indexing`
        # can release that acquisition and only that one. Epochs are monotonic,
        # so a stale value can never match a later holder's acquisition — the
        # field is therefore safe to leave set after a run ends.
        self._index_op_epoch: int = 0
        # The epoch whose admission bookkeeping `begin_index_run` already did.
        # `index_vault` skips its own preamble when this matches its op_epoch,
        # so the route-published state (and, critically, a Cancel that landed
        # in the gap before the thread ran) is not overwritten. Starts None,
        # and BECOMES 0 whenever a direct caller reaches `begin_index_run(0)`
        # — harmless, because the skip is gated on a truthy op_epoch, so an
        # epoch of 0 can never latch and a direct call always runs the
        # preamble. (The comment used to claim "None, never 0", which is false
        # as written.)
        self._index_run_begun_epoch: Optional[int] = None
        self._index_run_id: Optional[str] = None
        self._status_lock: threading.Lock = threading.Lock()
        self._messages_lock: threading.Lock = threading.Lock()
        self._status_messages: list[str] = []
        self._last_warning: str = ""
        self._index_integrity_error: str = ""
        # (2026-07-29 audit 0.1) Persistent record of WHY the last indexing run
        # errored. `_emit` alone is not a durable channel: the status poll
        # drains `_status_messages` destructively and the client's
        # `state === 'error'` branch then *blanks* the status line and shows a
        # generic "Indexing failed." — so a refusal like the embed-backend
        # gate's (whose whole value is its recovery instructions) was erased in
        # the same poll tick that delivered it. These two fields ride every
        # status payload (`error_code`/`error_detail`, additive like
        # `integrity_error`) so the client can render the real reason and key
        # its recovery actions on the machine code. Cleared at the top of each
        # `index_vault` run; set beside every `_index_state = "error"`.
        self._index_error_code: str = ""
        self._index_error_detail: str = ""
        self._skipped_image_count: int = 0
        # Item 2.8b: vault-relative sources whose READ failed this run (iCloud
        # dataless miss, transient I/O). Written only by _load_vault_documents
        # (single indexing thread), read by the stale-doc sweep so a
        # transiently unreadable file is never treated as deleted.
        self._scan_failed_sources: set[str] = set()
        # Finding 4 (2026-07-20 audit): vault-relative PREFIXES ("sub/dir/")
        # whose directory could not be scandir'd this run. Same protection as
        # _scan_failed_sources but subtree-wide — the stale sweep withholds any
        # doc whose source starts with one of these, so an unreadable folder is
        # not purged when an unrelated file elsewhere keeps the scan non-empty.
        self._scan_failed_prefixes: set[str] = set()
        # 2026-07-22 audit A5: extensions the loader treated as images this
        # run, plus whether any markdown note failed to read. Image chunks are
        # discovered ONLY by parsing readable notes, so an unreadable note
        # hides its referenced images from `current_doc_ids` — if one of them
        # was that image's only referencer, the sweep would delete the image's
        # chunks over a note-read blip. When any note read failed, the sweep
        # conservatively withholds ALL image-extension doc_ids for that run
        # (one run of staleness for genuinely deleted images vs. never a
        # wrongful durable delete). Extension-based because stamping the
        # referencing note into image metadata would change document hashes
        # and force a full image re-embed.
        self._scan_image_exts: set[str] = set()
        self._scan_md_read_failed: bool = False
        # NOTE: no ``_lock_epoch`` field. The op-lock epoch is a per-operation
        # token returned by ``try_acquire_lock`` and passed back explicitly to
        # ``release_lock``/``heartbeat`` — storing it here let one operation
        # clobber another's token (see the class docstring's ``_op_lock`` entry).
        # P0.4: throttle cache-write warnings to one per indexing run per
        # cache type.  A chronic disk-full or permissions issue would
        # otherwise emit a warning per file, flooding the status feed.
        # PDF and image caches use independent flags so a failed PDF write
        # cannot suppress a separate image-cache warning, and vice versa.
        self._pdf_cache_write_warning_emitted: bool = False
        self._image_cache_write_warning_emitted: bool = False
        self._vision_unavailable_warned: bool = False
        # Tracks the background indexing thread so callers (e.g. /api/reset)
        # can wait for it to finish before touching the storage directory.
        self._index_thread: Optional[threading.Thread] = None
        self._current_phase: str = "idle"

        # obsidian_meta.json read cache, keyed by (st_size, st_mtime_ns).
        # One /api/obsidian/status poll used to open + parse the file up to
        # three times (get_status, is_partial_index, get_index_warning), and
        # the UI polls continuously while indexing.  The stat key makes the
        # cache self-correcting: every rewrite (always via _write_json_atomic,
        # i.e. os.replace) bumps the mtime and forces a re-parse, so callers
        # can never observe stale metadata.  Lock ordering: this lock is only
        # ever taken INSIDE _status_lock (get_status calls _read_index_meta
        # while holding it) — never acquire _status_lock while holding this.
        self._meta_cache_lock: threading.Lock = threading.Lock()
        self._meta_cache: Optional[dict] = None
        self._meta_cache_key: Optional[tuple] = None
        # Serialises WRITERS of obsidian_meta.json against each other — the
        # indexer's `_write_index_meta` and `cancel_indexing`'s
        # read-modify-write, which had no shared lock at all (2026-09-02).
        # Held alone or as the OUTER of `_meta_write_lock` → `_meta_cache_lock`;
        # never taken while holding `_status_lock`, and never taken by a reader
        # (`_read_index_meta` reads an atomically-replaced file).
        self._meta_write_lock: threading.Lock = threading.Lock()

        # Status-poll lancedb row-count cache (see _cached_lancedb_count).
        # get_status() is polled ~1 Hz and only needs the boolean count>0, but
        # each raw lancedb_table_count opens a fresh connection that re-lists the
        # fragment manifest. Stat-key + short TTL collapses that churn.
        self._lancedb_count_lock: threading.Lock = threading.Lock()
        self._lancedb_count_cache: Optional[int] = None
        self._lancedb_count_key: Optional[tuple] = None
        self._lancedb_count_at: float = 0.0

        # Hybrid retrieval (BM25) cache.  Built lazily on the first chat that
        # asks for hybrid mode and reused thereafter so we do not retokenise
        # the entire docstore on every Send.  Invalidated by node-count
        # mismatch (cheap) and explicitly after each successful persist in
        # the indexer (correct).
        self._bm25_retriever: Optional[Any] = None
        self._bm25_cached_doc_count: int = -1
        self._bm25_build_lock: threading.Lock = threading.Lock()
        self._bm25_postindex_status = 'unknown'
        self._bm25_postindex_generation = 0
        # Wikilink note→note graph cache (query-time graph augmentation).
        # Built lazily from the docstore like BM25 — never from the filesystem
        # — so it adds no reindex and no re-embedding.  Cached by docstore size
        # and invalidated alongside BM25 after each persist.
        self._wikilink_index: Optional[Any] = None
        self._wikilink_cached_doc_count: int = -1
        self._wikilink_build_lock: threading.Lock = threading.Lock()
        # Persisted PDF-signature read cache (pdf_signatures.json).  The
        # read paths (read_note → _read_pdf_text, refactor pdf-refs) consult
        # the map the indexer persists so an unchanged multi-hundred-MB PDF
        # is not re-hashed on every call just to locate its text cache.
        # Stat-keyed on the signature files so an indexing run's rewrite is
        # picked up on the next read; read-only (the indexer owns writes).
        self._pdf_sig_read_cache: dict = {}
        self._pdf_sig_read_cache_key: Optional[tuple] = None
        self._pdf_sig_read_lock: threading.Lock = threading.Lock()
        # Vault thesaurus cache (query expansion + system-prompt primer).
        # Parsed from the raw `_abreviations.md` / `_tags.md` files at the vault
        # root — NOT from the index — so it is keyed by those files' mtimes
        # (rebuilt when the user edits either), independent of the docstore and
        # of any reindex. None until first use / when neither file exists.
        self._thesaurus: Optional[Any] = None
        self._thesaurus_cache_key: Optional[tuple] = None
        self._thesaurus_build_lock: threading.Lock = threading.Lock()
        # Cross-encoder reranker cache.  The model load is one-shot (the
        # weights are kept in memory) so the first chat pays the download +
        # warm-up cost and subsequent ones reuse the same object. ``top_n``
        # is tuned per query by the engine's locked pipeline build ONLY
        # (item 2.7) — never at fetch time.
        self._reranker: Optional[Any] = None
        self._reranker_model_loaded: str = ""
        # Sticky failure flag.  A failed load logs once and is not retried
        # against the same model name until the user changes the config —
        # this prevents every Send from re-downloading a model that the
        # network or disk just refused.
        self._reranker_failed: bool = False
        self._reranker_last_tried: str = ""
        self._reranker_load_lock: threading.Lock = threading.Lock()

        # Background prewarm state.  prewarm() is called once at app launch
        # and walks the same load path that stream_chat would otherwise hit
        # lazily on the first query, so cold-disk reads of the docstore
        # (~620 MB) and vector store (~3 GB) do not steal the chat-time
        # token timeout.  The UI polls these fields via get_status_payload().
        # States: "idle" (not started), "loading_index", "building_bm25",
        # "loading_reranker", "building_wikilinks", "ready", "skipped" (nothing on disk to warm),
        # "error" (load raised — chat will still try lazy-load).
        self._prewarm_generation: int = 0
        self._prewarm_status: str = "idle"
        self._prewarm_message: str = ""
        self._prewarm_started: bool = False
        self._prewarm_lock: threading.Lock = threading.Lock()

    def set_status_callback(self, cb: Callable[[str], None]) -> None:
        self._status_cb = cb

    def _emit(self, msg: str) -> None:
        """Append a status line for the UI poll (and fire the optional callback).

        Logs, then pushes onto the bounded message ring under ``_messages_lock``
        (capped at the last 200 lines so a long run can't grow it without bound);
        the ``/api/obsidian/status`` poll drains it via :meth:`drain_status_messages`.
        """
        logger.info("ObsidianVaultManager: %s", msg)
        with self._messages_lock:
            self._status_messages.append(msg)
            del self._status_messages[:-200]
        if self._status_cb:
            try:
                self._status_cb(msg)
            except Exception as e:
                logger.error("Status callback error: %s", e)

    # (set_vault_path removed 2026-07-05 audit D8: the 4.10 "delete/defang"
    # item only defanged it; it had zero callers. The canonical in-memory
    # setter is restore_vault_path; config persistence is owned by POST
    # /api/config — never a manager-side whole-config save.)

    def restore_vault_path(self, path: str) -> None:
        self._vault_path = self._normalise_vault_path(path)

    def get_vault_path(self) -> Optional[str]:
        return self._vault_path

    def _normalise_vault_path(self, path: str) -> Optional[str]:
        raw = str(path or "").strip()
        if not raw:
            return None
        try:
            resolved = Path(raw).expanduser().resolve()
        except OSError:
            self._emit(f"WARNING: Ignoring invalid vault path: {raw!r}")
            return None
        if not resolved.is_dir():
            self._emit(f"WARNING: Ignoring non-directory vault path: {raw!r}")
            return None
        if resolved in EXACT_BLOCKED or any(
            resolved == root or root in resolved.parents for root in SYSTEM_ROOTS
        ):
            self._emit(f"WARNING: Ignoring unsafe vault path: {resolved}")
            return None
        return str(resolved)

    def _read_index_meta(self) -> Optional[dict]:
        """Return parsed obsidian_meta.json, re-reading only when it changed.

        Cache hit costs one stat() syscall instead of open + read + parse.
        Returns the SHARED cached dict — callers must treat it as read-only
        (all current callers only .get() from it).  Returns None when the
        file is missing, unreadable, or not a JSON object.
        """
        meta_path = os.path.join(OBSIDIAN_INDEX_DIR, "obsidian_meta.json")
        try:
            stat = os.stat(meta_path)
            key = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            return None
        with self._meta_cache_lock:
            if self._meta_cache is not None and self._meta_cache_key == key:
                return self._meta_cache
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            return None
        if not isinstance(meta, dict):
            return None
        # Stored under the key statted BEFORE the read: a rewrite landing in
        # between caches fresh content under a stale key, which the next
        # call's re-stat corrects.  Never serves content older than its key.
        with self._meta_cache_lock:
            self._meta_cache = meta
            self._meta_cache_key = key
        return meta

    def get_status(self) -> str:
        """Return the current indexing state, recovering a persisted one when idle.

        While a run is active this is just the live ``_index_state``. On a fresh
        process that field is ``"idle"`` even though a complete/partial index may sit
        on disk, so this lazily reads ``obsidian_meta.json`` (via the
        ``_meta_cache_lock`` cache, taken *inside* ``_status_lock`` per the ordering
        rule) to recover ``done`` / ``paused_partial`` / ``paused_scan`` and to detect
        a checkpoint whose vector files are missing/incomplete (→ integrity warning).
        """
        with self._status_lock:
            if self._index_state != "idle":
                return self._index_state
        # PROBE OUTSIDE the lock (2026-07-22 audit P5): the idle lazy-recovery
        # path calls _index_dir_has_vector_data, which on the lancedb backend
        # opens a connection and re-lists the fragment manifest — real disk
        # I/O that on a cold-evicted/slow volume previously wedged inside
        # _status_lock, blocking pause_indexing / cancel_indexing / the
        # indexer's own state transitions behind a ~1 Hz UI poll. The meta
        # read + probe are pure reads, so running them unlocked is safe; the
        # re-check below handles a run that started meanwhile.
        # A fresh-run pause-during-scan writes meta but never persists the
        # index, so docstore.json may be absent.  The has_vector_data field
        # is the authoritative signal — rely on meta alone.
        _meta = self._read_index_meta()
        has_data = False
        probe_failed = False
        if _meta is not None:
            try:
                has_data = self._index_dir_has_vector_data(OBSIDIAN_INDEX_DIR)
            except Exception:  # noqa: BLE001 — degrade like the old swallow, but visibly
                probe_failed = True
        with self._status_lock:
            if self._index_state != "idle":
                # An indexing run started while we probed — its live state is
                # authoritative; discard the (now stale) recovery attempt.
                return self._index_state
            if _meta is not None:
                try:
                    if probe_failed:
                        raise RuntimeError("vector-data probe failed")
                    meta_claims_data = bool(_meta.get("has_vector_data"))
                    if meta_claims_data and not has_data:
                        self._index_integrity_error = (
                            "Persisted index metadata claims vector data exists, "
                            "but the vector checkpoint files are missing or incomplete."
                        )
                    elif has_data:
                        # 2026-07-20 audit: clear a STALE integrity error a
                        # later poll disproves. _index_dir_has_vector_data can
                        # transiently return False (a lancedb.connect blip in
                        # _cached_lancedb_count returns -1 → has_data=False),
                        # arming the error on one idle poll; without this the
                        # scary banner rode every subsequent healthy poll until
                        # a reindex. Only clears the lazy-recovery-set message,
                        # never one set by a real load/checkpoint failure path
                        # (those set _index_state != "idle", so we returned
                        # early above and never reach here).
                        self._index_integrity_error = ""
                    if _meta.get("partial"):
                        self._index_state = "paused_partial" if has_data else "paused_scan"
                    elif has_data:
                        self._index_state = "done"
                    elif self._index_integrity_error:
                        # Meta claims vector data but the checkpoint is missing
                        # or incomplete.  Recover as paused_scan (not idle) so the
                        # UI surfaces the integrity error and offers resume/reindex
                        # instead of looking like a never-indexed vault.
                        self._index_state = "paused_scan"
                    else:
                        self._index_state = "idle"
                    # Surface the persisted phase so the status payload doesn't
                    # report "idle" for a recovered paused state.
                    recovered_phase = _meta.get("phase")
                    if isinstance(recovered_phase, str) and recovered_phase:
                        self._current_phase = recovered_phase
                    else:
                        self._current_phase = self._index_state
                except Exception:
                    # Silent degradation to "idle" is the right POSTURE here
                    # (the poll must never crash and the next poll retries),
                    # but it must not be INVISIBLE (2026-07-22 audit): a
                    # corrupt meta / failed probe previously left the index
                    # looking never-built with zero trace. Warn once per
                    # process, debug-log thereafter.
                    if not getattr(self, "_status_recovery_warned", False):
                        self._status_recovery_warned = True
                        logger.warning(
                            "index-state lazy recovery failed; status stays "
                            "'idle' until a poll succeeds", exc_info=True)
                    else:
                        logger.debug("index-state lazy recovery failed",
                                     exc_info=True)
            return self._index_state

    def docstore_doc_count(self) -> Optional[int]:
        """Loaded-docstore size for the health probe, or ``None``.

        ``None`` means "no loaded index" OR "index busy" — the probe uses a
        NON-blocking acquire of ``_index_mutation_lock`` because a 15 s UI
        poll must never stall behind (or stall) an insert/checkpoint; reading
        the serialized docstore collection without the lock would race the streaming
        indexer's mutations. Callers distinguish "not loaded" from "no index
        on disk" via the persisted docstore file, not this accessor.
        """
        if not self._index_mutation_lock.acquire(blocking=False):
            return None
        try:
            idx = self._index
            if idx is None:
                return None
            try:
                return node_count(idx.docstore)
            except Exception:
                return None
        finally:
            self._index_mutation_lock.release()

    @measured("index.retrieval.wait")
    def _acquire_retrieval_lock(self) -> None:
        """Timed acquire of ``_index_mutation_lock`` for the CHAT path (item 2.1).

        AP-09 moved query embedding, rewriting, reranking, and synthesis outside
        this lock. The remaining hold is the coherent bounded vector/BM25/
        docstore snapshot read, but acquisition must still be bounded because a
        concurrent index checkpoint legitimately owns the same mutation lock.

        The timeout is generous on purpose: a legitimate holder can be a
        mid-run CHECKPOINT (which re-serialises the whole store under this
        lock — up to ~1-2 minutes on a multi-GB simple-backend store) so
        tripping early would false-error healthy chats during indexing. 120 s
        sits below the SSE consumer stall floor (300 s+30) so the user gets
        THIS actionable message, not the generic "Generation timed out". NOTE
        (2026-07-20 audit M2): a pathologically large simple-backend store
        whose checkpoint serialisation exceeds 120 s WOULD surface the busy
        error for the duration — it degrades cleanly (a clear message, not a
        hang), but the bound is not proven above every possible checkpoint;
        the lancedb backend, whose checkpoint promotes only the docstore JSON,
        is well inside it. Safe: acquire semantics unchanged on success
        (caller releases in its ``finally``); on timeout nothing was acquired
        and the raise surfaces through the worker's normal ``{"error"}`` path.
        Invariant (pinned by ``test_acquire_retrieval_lock_times_out_cleanly``):
        a chat can wait at most ``_RETRIEVAL_LOCK_TIMEOUT_S`` on the mutation
        lock before failing with an explanatory error instead of hanging.
        """
        if not self._index_mutation_lock.acquire(timeout=_RETRIEVAL_LOCK_TIMEOUT_S):
            raise RuntimeError(
                "The vault index is busy (a long-running index operation is "
                "holding the retrieval lock). Please try again shortly; if this "
                "persists, cancel or restart the indexing run."
            )

    def _record_index_error(self, code: str, detail: str) -> None:
        """Persist the reason an indexing run errored (2026-07-29 audit 0.1).

        Called BESIDE (not instead of) the existing `_emit` + state assignment
        at each error site, so message ordering and state semantics are
        byte-identical — this only adds the durable copy the destructive
        message drain cannot erase. The code is a machine key the client uses
        to pick recovery actions (e.g. `embed_backend_mismatch` offers a
        confirm-gated rebuild instead of a Retry that re-fails identically).
        """
        with self._status_lock:
            self._index_error_code = code
            self._index_error_detail = detail

    def begin_index_run(self, op_epoch: int) -> None:
        """Publish "a run is starting" SYNCHRONOUSLY, before the thread exists.

        Defect this closes: ``POST /api/obsidian/index`` spawned the indexing
        thread and returned 200 immediately, but the state transition lived on
        that thread (after an `_emit` that writes to the log file). The client
        polls ``/api/obsidian/status`` with **zero delay** on the 200 — every
        later poll is on a 2 s timer, so the first one is the only racy one —
        and the previous run's terminal state is served instantly (the lazy
        recovery in :meth:`get_status` materialises ``"done"`` into
        ``_index_state``, so the poll takes the early-return fast path with no
        disk I/O at all). The client therefore read ``done``, flashed
        "Indexing complete.", stopped polling and restored the idle buttons —
        while the run it had just started carried on invisibly. Clicking Index
        again then 503'd and `refreshIndexState` re-attached, which is why the
        run looked like it needed two clicks. The same window served a STALE
        ``error_code``/``error_detail`` (cleared on the thread) and a stale
        ``paused_*``.

        Called by the route between ``try_acquire_lock`` and ``t.start()``, so
        by the time the 200 is written the status is already non-terminal for
        every client (GUI, CLI, MCP). :meth:`index_vault` records the epoch and
        skips its own preamble for it, which is what makes a Cancel landing in
        the gap survive: without that, ``index_vault``'s unconditional
        ``_stop_event.clear()`` would swallow it — and since the route also
        publishes ``_index_op_epoch`` here, such a cancel releases THIS run's
        acquisition rather than a stale one.
        """
        with self._status_lock:
            # Clear the previous run's stop/pause BEFORE publishing the epoch,
            # and inside the same lock hold. `cancel_indexing` reads
            # `_index_op_epoch` under this lock and then releases that epoch's
            # op-lock acquisition, so publishing first and clearing after left
            # a window where a Cancel could release THIS run's lock and then
            # have its `_stop_event` erased by the clear below — the run would
            # carry on with no lock held (through store-open/archive, until the
            # first heartbeat noticed). Clearing first means a Cancel that sees
            # the epoch also keeps its stop flag, and one that arrives earlier
            # only ever targets the previous run.
            self._stop_event.clear()
            self._pause_requested = False
            self._index_state = "scanning"
            self._current_phase = "initialising"
            # A fresh run supersedes the previous run's error record — leaving
            # it would keep showing a stale reason after a successful rerun.
            self._index_error_code = ""
            self._index_error_detail = ""
            self._index_op_epoch = op_epoch
            self._index_run_begun_epoch = op_epoch
            from uuid import uuid4
            self._index_run_id = uuid4().hex

    def abort_index_run(self, op_epoch: int) -> None:
        """Undo :meth:`begin_index_run` when the thread never started.

        Only the admission it published (epoch match) is reverted, so a late
        call can never reset a *different* run's live state. Recovers to
        ``"idle"``; the next poll's lazy recovery re-derives the real on-disk
        state from ``obsidian_meta.json``.
        """
        with self._status_lock:
            if self._index_run_begun_epoch != op_epoch:
                return
            self._index_run_begun_epoch = None
            self._index_state = "idle"
            self._current_phase = "idle"

    def get_progress_snapshot(self) -> dict:
        """Observe indexing without consuming the single-reader status feed."""
        with self._messages_lock:
            message = self._status_messages[-1] if self._status_messages else ''
        state = self.get_status()
        with self._status_lock:
            return {"state": state, "phase": self._current_phase,
                    "message": message, "run_id": self._index_run_id,
                    "error_detail": self._index_error_detail}

    def get_postindex_bm25_status(self) -> str:
        with self._status_lock:
            return self._bm25_postindex_status

    def get_status_payload(self) -> dict:
        messages = self.drain_status_messages()
        warning = self.get_index_warning()
        prewarm_status, prewarm_message = self.get_prewarm_state()
        return {
            "state": self.get_status(),
            "vault_path": self.get_vault_path(),
            "is_partial": self.is_partial_index(),
            "messages": messages,
            "skipped_image_count": self._skipped_image_count,
            "warning": warning,
            "warnings": [warning] if warning else [],
            "integrity_error": self._index_integrity_error,
            # Additive (2026-07-29): why the last run errored, surviving the
            # destructive `messages` drain — see _record_index_error.
            "error_code": self._index_error_code,
            "error_detail": self._index_error_detail,
            "phase": self._current_phase,
            "prewarm_status": prewarm_status,
            "prewarm_message": prewarm_message,
        }

    def drain_status_messages(self) -> list[str]:
        """Atomically return and clear the pending status lines (read-once by the poll)."""
        with self._messages_lock:
            messages = list(self._status_messages)
            self._status_messages.clear()
            return messages

    def clear_status_messages(self) -> None:
        """Drop any buffered status lines without returning them."""
        with self._messages_lock:
            self._status_messages.clear()

    def try_acquire_lock(self, ttl: int = 3600) -> Optional[int]:
        """Admit one long operation; return its epoch token (None if refused).

        Per-operation token fix (improvement plan 2026-07-04, item 1.5). The
        previous version cached the epoch in a shared ``self._lock_epoch``
        attribute that ``release_lock``/``heartbeat`` read *at call time* — so
        any NEW acquisition overwrote the token a still-running previous holder
        would later release with. Concrete failure: cancel an index run
        (``force_release``), start a refactor Apply (new epoch stored on the
        singleton), and the cancelled indexer's ``finally`` then released the
        *refactor's* lock mid-batch, letting a second writer in. The docstring
        claimed exactly the zombie-safety the shared field destroyed.

        Now the epoch lives only on the caller's stack: it is RETURNED here and
        must be passed back to :meth:`release_lock` / :meth:`heartbeat`. Safe
        w.r.t. existing state: epochs start at 1 (``RagOperationLock`` bumps
        from 0 on first acquire) so the return is truthy exactly when the old
        ``True`` was — every ``if not try_acquire_lock(...)`` guard keeps its
        behaviour — and no on-disk state or lock-acquisition ORDER changes.
        Invariant (pinned by ``test_concurrency.py::TestOpLockEpochToken``): a
        holder can only release/extend the acquisition whose epoch it captured
        at acquire time; a stale holder's release/heartbeat is a no-op.
        """
        # Atomic acquire+epoch capture (2026-07-05 audit m2): reading the epoch
        # in a SEPARATE critical section (the old `try_acquire()` then `.epoch`)
        # left a window where a force_release + re-acquire by another operation
        # could hand back a NEWER acquisition's token — reintroducing the item
        # 1.5 zombie-release class. One call, one `_meta_lock` critical section.
        # Returns None on refusal, the caller's own epoch (≥1, truthy) on grant.
        return self._op_lock.try_acquire_epoch(ttl)

    def release_lock(self, epoch: int) -> None:
        """Release the op-lock acquisition identified by *epoch* (no-op if stale).

        *epoch* is the token :meth:`try_acquire_lock` returned to THIS caller —
        never a value read from shared state. A falsy epoch is refused outright:
        ``RagOperationLock.release(0)`` would be an unconditional release, which
        must never be reachable from the normal caller path (that is what
        :meth:`force_release` is for).
        """
        if not epoch:
            return
        self._op_lock.release(epoch)

    def heartbeat(self, epoch: int) -> bool:
        """Push the op-lock deadline out; return whether *epoch* still holds it.

        Long external holders (the Note Refactor batch writers) must call this
        periodically so their lock does not passively expire mid-operation and
        get stolen by a concurrent indexing run — exactly the way the indexer
        heartbeats inside its own insert loop. *epoch* is the caller's own
        captured token; a stale/foreign epoch is a no-op inside
        ``RagOperationLock.heartbeat``, so a zombie can never extend the
        deadline of whoever holds the lock now. Exposed as a public method so
        ``refactor/`` never reaches into the private lock.

        **Returns the underlying bool** (2026-07-20 audit finding 1): ``False``
        once the acquisition's epoch no longer matches — i.e. the TTL lapsed
        while this holder was wedged and another operation legitimately took the
        lock. The refactor batch writers MUST act on that ``False`` and stop
        writing (mirroring the indexer's ``_heartbeat_or_stop``); the prior
        ``-> None`` swallowed the signal, so a lapsed-then-stolen refactor
        restore would keep writing vault notes concurrently with the new
        holder's indexing run — the exact single-writer breach the epoch token
        exists to prevent. ``epoch == 0`` (a direct/test caller holding no lock)
        returns ``True`` (nothing to lose).
        """
        if not epoch:
            return True
        return self._op_lock.heartbeat(epoch)

    def _heartbeat_or_stop(self, op_epoch: int) -> None:
        """Indexer-side heartbeat that ABORTS the run when the lock was lost.

        ``RagOperationLock.heartbeat`` returns ``False`` once the acquisition's
        epoch no longer matches — i.e. the TTL lapsed while this run was
        wedged (an unbounded indexing embed, a slow OCR page) and another
        operation legitimately acquired the lock. Discarding that ``False``
        (the pre-2026-07-20 behaviour at every call site) left the loser
        RUNNING: the indexer resumed inserting/checkpointing/sweeping
        concurrently with the new holder's vault writes, silently voiding the
        one-operation-at-a-time invariant the lock exists for. Setting the
        stop event aborts along the same paths as a user cancel; the partial
        index stays queryable and the next run resyncs. ``op_epoch == 0``
        (direct/test callers holding no lock) stays inert.
        """
        if not op_epoch:
            return
        if not self._op_lock.heartbeat(op_epoch) and not self._stop_event.is_set():
            self._emit(
                "ERROR: this indexing run's operation lock expired and was "
                "taken over by another operation; aborting to avoid "
                "concurrent vault writes. Re-run indexing to resume."
            )
            self._stop_event.set()

    def force_release(self) -> bool:
        """Unconditionally free the op-lock (recovery path, e.g. cancel); was-it-held."""
        return self._op_lock.force_release()

    def index_vault(
        self,
        llm_name: str,
        embed_name: str,
        provider_name: str = "ollama",
        op_epoch: int = 0,
        force_fresh: bool = False,
        online_extraction: bool = False,
        extraction_manifest: dict | None = None,
    ) -> None:
        """Run a full incremental (re)index of the configured vault — the main orchestrator.

        ``op_epoch`` is the op-lock token the admitting route captured from
        ``try_acquire_lock`` — threaded through to the loader/streaming-insert
        heartbeats so this run can only ever extend its OWN acquisition (item
        1.5; ``0`` — e.g. a direct test call holding no lock — makes every
        heartbeat an inert no-op rather than an accidental extension of a
        foreign holder).

        ``force_fresh`` (2026-07-27) is the EXPLICIT user opt-in to discard the
        existing vector index and re-embed the whole vault. It exists because
        the embed-BACKEND gate below now *refuses* rather than silently
        rebuilding (a full re-embed of a large vault is hours of compute the
        user never asked for — see that gate's comment), and a refusal is only
        acceptable if there is a way to say "yes, do it". Semantics are
        deliberately general and simple: it forces ``is_incremental = False``,
        while version/model changes refuse routine admission. It reuses the
        archive-then-rebuild path only after explicit confirmation. Exposed end-to-end
        since 2026-07-29: ``POST /api/obsidian/index`` accepts a ``force_fresh``
        body field, sent only by the confirm-gated "Rebuild from scratch"
        action the client offers on an ``embed_backend_mismatch`` error —
        programmatic callers (CLI, ``tests/eval``) use the same hook.

        ``online_extraction`` is used only after a confirmed online staging run.
        It disables local PDF OCR callbacks and permits referenced images to
        use completed description caches only. Empty or failed sources keep
        their previous chunks under the normal scan-failure protections.

        Called on the daemon thread admitted by ``/api/obsidian/index`` (which already
        holds the op-lock). Drives the streaming pipeline documented in ``rag/CLAUDE.md``:
        scan → chunk → stream-insert with periodic checkpoints → final persist →
        cache invalidation. Concurrency-critical structure: it publishes ``self._index``
        under the ``_rw_lock`` write lock after setup, then releases it so the
        multi-hour insert loop runs lock-free; the write lock is re-taken only for each
        mid-run checkpoint and the final persist. ``_stop_event`` (cancel) and
        ``_pause_requested`` are polled cooperatively in the loop. Per-run warning flags
        are reset up front so each run can warn once. Returns nothing — progress and
        terminal state are surfaced through the status machine + message ring; the
        caller releases the op-lock in its ``finally``.

        Phase map (Plan 9 step 4a, 2026-07-30 — this body was ~654 lines).
        Each phase below is a method on this class; the ORCHESTRATION, and in
        particular every early return and which of them run inside the outer
        ``try`` (i.e. which ones reach ``finally: gc.collect()``), stayed here
        deliberately — that control flow is the part worth reading in one place:

          * ``_index_prepare_embedding``  — provider/embed resolution + the
            model-installed preflight. Runs BEFORE the try, like it always did.
          * ``_index_lancedb_preflight``  — refuse to overwrite a LanceDB index
            we cannot open. Also before the try.
          * ``_index_open_store``         — the whole rw-write-locked setup:
            incremental decision, the promotion-marker / force_fresh /
            vault-path / embed-backend gates, archive, build, publish.
          * ``_index_make_persist_callback`` / ``_index_finalise_persist`` —
            the mid-run checkpoint and the final persist, which share one
            :class:`_PersistTarget` so they cannot drift apart.
          * ``_index_reconcile_lancedb_drift`` — the crash-drift/orphan sweep.
          * ``_index_report_outcome`` / ``_index_kick_bm25_rebuild`` — the tail,
            in that order: the BM25 warm's persist refuses a non-terminal
            state, so kicking it first wrote no sidecar (2026-09-02).
        """
        # Admission bookkeeping — `_stop_event`/`_pause_requested`, this run's
        # op-lock epoch (so a Cancel releases THIS acquisition and never a
        # concurrent vault writer's, see `cancel_indexing`), the state/phase
        # transition and the previous run's error record. The admitting route
        # already did all of it via `begin_index_run` so the status is
        # non-terminal before its 200 is written; re-running it here would
        # erase a Cancel that landed in the gap. Direct callers (tests, eval
        # scripts, in-process CLI) pass op_epoch=0, which short-circuits the
        # `and` and keeps the pre-2026-08-12 behaviour byte-for-byte.
        if not (op_epoch and self._index_run_begun_epoch == op_epoch):
            self.begin_index_run(op_epoch)
        self._pdf_cache_write_warning_emitted = False
        self._readable_export_warning_count = 0
        self._image_cache_write_warning_emitted = False
        self._vision_unavailable_warned = False
        self._emit("Starting Obsidian vault indexing…")

        vault = self._vault_path
        if not vault or not os.path.isdir(vault):
            msg = f"ERROR: Vault path not set or not a directory: {vault!r}"
            self._emit(msg)
            self._record_index_error("index_failed", msg)
            with self._status_lock:
                self._index_state = "error"
            return

        if extraction_manifest is not None:
            from rag.index_admission import validate_extraction_manifest
            try:
                validate_extraction_manifest(extraction_manifest)
            except ValueError as exc:
                self._record_index_error("invalid_extraction_manifest", str(exc))
                with self._status_lock:
                    self._index_state = "error"
                return

        prepared = self._index_prepare_embedding(
            llm_name, embed_name, provider_name, get_provider=get_provider,
        )
        if prepared is None:
            return
        embed_model, embed_provider_name = prepared

        if not self._index_lancedb_preflight(
            index_dir=OBSIDIAN_INDEX_DIR, lancedb_available=lancedb_available,
        ):
            return

        try:
            opened = self._index_open_store(
                vault, embed_name, embed_provider_name, embed_model, force_fresh,
                index_dir=OBSIDIAN_INDEX_DIR, op_epoch=op_epoch,
            )
            if opened is None:
                return
            idx, vector_backend, is_incremental, meta_path = opened
            target = _PersistTarget(
                idx=idx,
                vector_backend=vector_backend,
                meta_path=meta_path,
                embed_name=embed_name,
                provider_name=provider_name,
                vault=vault,
                embed_provider_name=embed_provider_name,
                op_epoch=op_epoch,
            )

            self._emit("Scanning vault for .md, .pdf, and referenced image files…")
            # Loader is now a generator (M4): documents are yielded one at a
            # time through the chunker and into the index, so the peak
            # memory cost no longer scales with the entire vault.  The
            # empty-vault check moves below — see the "no work done" branch
            # after `_index_documents_streaming` returns.
            loader_options = {"extraction_manifest": extraction_manifest} if extraction_manifest is not None else {}
            raw_docs = self._load_vault_documents(
                vault, op_epoch=op_epoch, online_extraction=online_extraction, **loader_options)

            with self._status_lock:
                self._index_state = "embedding"
                self._current_phase = "embedding"
            self._emit("Splitting documents into chunks and embedding…")

            _persist_callback = self._index_make_persist_callback(target)
            # A no-change pass may retain the durable corpus and its sidecars.
            # Counts alone are insufficient: mutation/publication generations
            # detect same-size replacements and failed mutation attempts too.
            with self._index_mutation_lock:
                before_fingerprint = self._cache_fingerprint(idx)
                before_meta = self._read_index_meta() or {}

            lancedb_upsert = self._index_reconcile_lancedb_drift(
                idx, vector_backend, is_incremental, index_dir=OBSIDIAN_INDEX_DIR,
            )

            chunks_iter = self._chunk_raw_documents(raw_docs, vault)
            added, skipped, deleted, failed, manifest_counts = (
                self._index_documents_streaming(
                    idx, chunks_iter, _persist_callback,
                    lancedb_upsert=lancedb_upsert,
                    vector_backend=vector_backend,
                    op_epoch=op_epoch,
                )
            )

            if self._readable_export_warning_count:
                self._emit(
                    f"Readable Markdown copies: {self._readable_export_warning_count} export(s) skipped; "
                    "existing copies were preserved. This is separate from PDF extraction. "
                    "Only the first three details are shown; all details remain in the log."
                )

            # M4: the skipped-image counter is incremented *during* loader
            # iteration, so its final value is only meaningful after the
            # streamer has drained the generator.
            if self._skipped_image_count:
                self._emit(
                    f"Skipped {self._skipped_image_count} referenced image files "
                    "(vision model unavailable, size cap exceeded, or description empty)."
                )

            # Empty-vault diagnostic: with the list-based loader this was a
            # cheap `if not raw_docs:` up front.  With streaming we can only
            # tell after the streamer has finished — if it received nothing
            # to insert AND we did not stop, the vault has no indexable
            # files and the run should surface that to the UI rather than
            # writing a "done" meta with zero documents.
            if (
                not self._stop_event.is_set()
                and added == 0
                and skipped == 0
                and failed == 0
                and deleted == 0  # deletions must reach the final persist
                and not manifest_counts
            ):
                self._emit("ERROR: No indexable files found.")
                self._record_index_error("index_failed", "ERROR: No indexable files found.")
                with self._status_lock:
                    self._index_state = "error"
                    self._current_phase = "error"
                return

            with self._index_mutation_lock:
                unchanged_corpus = (
                    is_incremental and not force_fresh and not lancedb_upsert
                    and not self._stop_event.is_set() and skipped > 0
                    and added == deleted == failed == 0
                    and before_fingerprint is not None
                    and before_fingerprint == self._cache_fingerprint(idx)
                    and before_meta.get("phase") == "done"
                    and not before_meta.get("partial") and bool(before_meta.get("indexed_at"))
                    and before_meta == (self._read_index_meta() or {})
                )
            if unchanged_corpus:
                self._heartbeat_or_stop(op_epoch)
                self._emit("No searchable content changed; retaining the saved index and lexical search cache.")
                self._index_report_outcome(added, skipped, deleted, failed, manifest_counts)
                # Loads a matching sidecar when available; missing/stale caches
                # still take the normal validated rebuild path.
                self._index_kick_bm25_rebuild(load_config=load_config)
                return

            self._index_finalise_persist(
                target, added=added, skipped=skipped, manifest_counts=manifest_counts
            )

            # Resync the BM25/wikilink caches to the freshly-persisted docstore
            # — OUTSIDE the rw write lock, matching the mid-run checkpoint path
            # (item 2.8c, improvement plan 2026-07-04). The previous inline
            # position held the WRITE lock while _invalidate_retrieval_caches
            # contended the BM25 build lock: a concurrent chat mid-way through
            # a minutes-long BM25 rebuild made the invalidation wait on the
            # build lock while every reader (all chats) waited on the write
            # lock — the whole app froze for the rebuild's duration (the
            # mid-run path documented exactly this hazard and stayed outside;
            # the two paths had drifted). Safe: the invalidation itself is
            # idempotent and ordering-tolerant — a chat that rebuilt against
            # the pre-persist docstore inside the window keeps a stale cache
            # only until this call lands (the same eventual-consistency
            # contract the mid-run checkpoint has always run under; staleness
            # means "yesterday's BM25 corpus", never corruption). Invariant
            # (pinned by test_final_persist_invalidates_caches_outside_write_lock):
            # no _invalidate_retrieval_caches call runs while the rw write
            # lock is held.
            self._invalidate_retrieval_caches()

            # PERF (2026-09-02): publish the terminal state BEFORE warming BM25.
            #   Before:    the kick ran here, one line ahead of
            #              _index_report_outcome. The warm daemon ends in
            #              _persist_bm25_sidecar, which BAILS unless
            #              _index_state is "idle" or "done" — measured at the
            #              kick, the state was "embedding" — so on a fast run
            #              the thread rebuilt the retriever and wrote NO
            #              sidecar. The next cold launch then paid the full
            #              docstore re-tokenise the sidecar exists to avoid,
            #              silently and intermittently (whichever side of the
            #              flip the daemon happened to land on).
            #   Change:    _index_report_outcome first, then the kick.
            #   Safe because:
            #              the two are independent tail steps — the kick reads
            #              only _stop_event and vault_hybrid_enabled, neither
            #              of which _index_report_outcome touches; it sets the
            #              terminal state and emits the summary. Nothing in the
            #              kick is observable to the summary, and a warm that
            #              starts a few microseconds later is by construction
            #              off the critical path.
            #   Preserves: "only after a run that COMPLETED (a cancelled/paused
            #              run resumes soon and would churn the build)" — the
            #              kick still gates on _stop_event, which
            #              _index_report_outcome only READS. A cancelled run
            #              lands on "idle", which IS in the persist allowlist,
            #              so the _stop_event gate (not the state) is what keeps
            #              a cancelled run from churning; it is unchanged.
            #   Lock/thread contract:
            #              both callees take _status_lock internally and neither
            #              is called holding it; the rw write lock is already
            #              released by here (see the invalidation note above).
            #   Pinned by: test_vault_regressions.py::TestVaultChatBM25Manager
            #              ::test_bm25_warm_is_kicked_after_the_terminal_state_is_published
            self._index_report_outcome(
                added, skipped, deleted, failed, manifest_counts
            )

            self._index_kick_bm25_rebuild(load_config=load_config)

        except Exception as e:
            msg = f"ERROR: Indexing failed: {e}"
            self._emit(msg)
            self._record_index_error("index_failed", msg)
            with self._status_lock:
                self._index_state = "error"
                self._current_phase = "error"
        finally:
            # Hygiene only: break LlamaIndex/parser reference cycles promptly
            # so the chunker/indexer backlog is reclaimable as soon as the run
            # ends, instead of waiting for a later generational collection.
            gc.collect()

    # Retention split (2026-07-27). ``.bak`` siblings created by a VERSION BUMP
    # are routine and interchangeable — the newest two are plenty. Siblings
    # created by an IDENTITY rebuild (vault switch, explicit force_fresh) are
    # the user's only copy of an index that took hours to build and that no
    # future run will ever reproduce from cache, so they get their own, larger
    # window that routine archives cannot consume. Both sets stay BOUNDED: a
    # true never-prune exemption would let a user who toggles between two
    # vaults accumulate tens of GB per switch, which is the failure mode the
    # prune sweep was added for in the first place.
    _INDEX_BAK_PROTECTED_SUFFIX = ".keep"
    _INDEX_BAK_PROTECTED_KEEP = 5

    def _archive_old_index_dir(self, prev_meta: dict, *, protected: bool = False) -> str:
        """Move the existing index dir to a timestamped .bak sibling.

        Returns the absolute path of the bak directory, or "" if the move failed.
        Used when ``OBSIDIAN_INDEX_VERSION`` changes — we keep the prior work
        recoverable instead of ``shutil.rmtree``ing it.

        ``protected`` marks the archive as identity-rebuild fallout (vault
        switch / explicit ``force_fresh``) by appending
        ``_INDEX_BAK_PROTECTED_SUFFIX`` to its name, which moves it into the
        prune sweep's separate, larger retention window. The marker lives in the
        directory NAME so it is visible to the user in Finder and survives any
        process restart — no sidecar state to lose.
        """
        from datetime import datetime as _dt
        try:
            stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
            prev_ver = str(prev_meta.get("version") or "unknown").replace("/", "_")
            bak = f"{OBSIDIAN_INDEX_DIR}.bak.{prev_ver}.{stamp}"
            if protected:
                bak += self._INDEX_BAK_PROTECTED_SUFFIX
            os.rename(OBSIDIAN_INDEX_DIR, bak)
            logger.warning(
                "Archived prior index to %s (retention: %d routine / %d protected)",
                bak, 2, self._INDEX_BAK_PROTECTED_KEEP,
            )
            # PERF (2026-09-02): a bounded size walk and an UNBOUNDED rmtree ran
            # inside the rw WRITE lock.
            #   Before:    the rename was followed, still under the write lock,
            #              by `_dir_size_note` (a walk bounded at 5 s) and
            #              `_prune_old_index_backups` (an rmtree of up to tens
            #              of GB of archives — the retention windows are 2
            #              routine + 5 protected, and the maintainer's index is
            #              5.4 GB, so ~27 GB of deletes is in range). Every chat
            #              read is a writer-preference reader on that lock and
            #              waits with NO timeout, so a rebuild could stall
            #              retrieval for the whole sweep.
            #   Change:    the RENAME stays under the lock — it is the part that
            #              must be atomic with the decision to rebuild. The walk
            #              and the prune move to one short daemon started here
            #              and joined by the suite's `join_post_index_daemons`.
            #   Safe because:
            #              neither touches the NEW index dir: the walk reads the
            #              renamed-aside archive and the prune only removes
            #              `<index>.bak.*` siblings older than the retention
            #              windows — the archive just created is the newest by
            #              mtime, so a prune racing a LATER archive can never
            #              take either one. The user-visible size line still
            #              arrives, on the same `_emit` ring, a moment later.
            #   Preserves: "Every removal still routes through
            #              log_storage_deletion" — `_prune_old_index_backups` is
            #              called unchanged — and both retention windows stay
            #              bounded and independent.
            #   Lock/thread contract:
            #              entry: rw WRITE lock held by the caller. Exit: same.
            #              The walk and prune now run on a daemon that holds NO
            #              lock at all.
            #   Pinned by: test_vault_regressions.py::TestArchivePruneOffTheWriteLock
            self._start_archive_sweep(bak)
            return bak
        except Exception as exc:
            logger.warning("Could not archive prior index dir: %s", exc)
            raise RuntimeError("Could not archive prior index; rebuild refused") from exc

    def _start_archive_sweep(self, bak: str) -> None:
        """Measure *bak* and prune stale archives on a daemon, holding no lock.

        Started from inside the rw write lock (see the caller) but doing all of
        its work after that frame returns. Named ``index-archive-sweep`` so the
        suite's ``join_post_index_daemons`` can wait for it — an unjoined daemon
        writing into a patched temp dir is the 2026-07-31 flake class.
        """
        def _sweep() -> None:
            try:
                # Footprint disclosure (2026-07-30). Retention is COUNT-bounded
                # (2 routine, 5 protected), so on a multi-GB index the protected
                # window alone can hold tens of GB with nothing telling the user.
                # A byte cap was rejected — it needs a du-walk over multi-GB dirs
                # on every archive — but measuring the directory ONCE pays for
                # itself: the size goes in the log and in the status ring, where
                # the user actually looks. Best-effort; a failed walk never
                # blocks anything.
                size_note = _dir_size_note(bak)
                if size_note:
                    logger.warning("Archive %s is %s", bak, size_note)
                    self._emit(
                        f"Archived the previous index to {os.path.basename(bak)} "
                        f"({size_note}). Old archives are pruned automatically "
                        f"(newest {2} routine / {self._INDEX_BAK_PROTECTED_KEEP} "
                        f"rebuild archives are kept)."
                    )
                # Bounded retention: a version bump archives the whole prior
                # index (docstore ~hundreds of MB + the LanceDB/JSON vectors),
                # and nothing else ever deletes these siblings, so they
                # accumulate tens of GB on the same disk the local LLM weights
                # live on. Keep the newest few and prune the rest.
                self._prune_old_index_backups(keep=2)
            except Exception:
                logger.debug("Index-archive sweep failed.", exc_info=True)

        threading.Thread(target=_sweep, name="index-archive-sweep",
                         daemon=True).start()

    def _prune_old_index_backups(self, keep: int = 2) -> None:
        """Delete all but the ``keep`` most recent ``<index>.bak.*`` siblings.

        Each ``_archive_old_index_dir`` call renames the entire prior index dir
        to a timestamped ``.bak`` sibling; without this sweep they grow without
        bound. Best-effort and never raises — a failed prune must not abort a
        reindex. Each confirmed removal is routed through ``log_storage_deletion``
        per the deletion-audit invariant (see the root ``CLAUDE.md``) — logged
        **after** the ``rmtree`` succeeds, because ``ignore_errors=True`` would
        otherwise let the audit line claim a removal that silently failed.

        Two INDEPENDENT windows (2026-07-27): archives whose name ends with
        ``_INDEX_BAK_PROTECTED_SUFFIX`` (identity rebuilds — vault switch,
        explicit ``force_fresh``) are ranked and capped among themselves at
        ``_INDEX_BAK_PROTECTED_KEEP``, so no number of routine version-bump
        archives can push the user's only good index copy out of retention.
        Before this split a *third* rebuild — and the embed-provider gate could
        fire three times from three unrelated chat-provider toggles — deleted
        the original outright. Both windows are bounded, so the sweep's
        disk-reclaim purpose is preserved.
        """
        try:
            index_dir = Path(OBSIDIAN_INDEX_DIR)
            prefix = index_dir.name + ".bak."
            parent = index_dir.parent
            if not parent.is_dir():
                return
            baks = sorted(
                (p for p in parent.iterdir()
                 if p.is_dir() and p.name.startswith(prefix)),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            suffix = self._INDEX_BAK_PROTECTED_SUFFIX
            protected = [p for p in baks if p.name.endswith(suffix)]
            routine = [p for p in baks if not p.name.endswith(suffix)]
            for stale in routine[keep:] + protected[self._INDEX_BAK_PROTECTED_KEEP:]:
                try:
                    shutil.rmtree(stale, ignore_errors=True)
                    # rmtree swallowed any error (ignore_errors), so confirm the
                    # dir is actually gone before recording the deletion — the
                    # audit trail must never assert a removal that didn't happen.
                    if stale.exists():
                        logger.warning(
                            "Could not fully prune stale index backup %s", stale.name,
                        )
                    else:
                        log_storage_deletion(f"prune_old_index_backup:{stale.name}")
                except Exception:
                    logger.warning(
                        "Could not prune stale index backup %s", stale.name,
                    )
        except Exception:
            logger.debug("Index-backup prune sweep failed.", exc_info=True)

    def _write_index_meta(
        self,
        path: str,
        embed_name: str,
        provider_name: str,
        *,
        partial: bool,
        phase: str,
        has_vector_data: bool,
        inserted_this_run: int,
        vector_backend: str = "simple",
        vault_path: Optional[str] = None,
        embed_provider: str = "",
    ) -> None:
        # PERF/CORRECTNESS (2026-09-02): one writer at a time for the meta file.
        #   Before:    this wrote holding only the rw write lock, while
        #              cancel_indexing did a read-modify-write of the same file
        #              under _status_lock — no shared serialisation, so a cancel
        #              that read before this write and wrote after it reverted
        #              phase, has_vector_data and embed_provider.
        #   Change:    both writers take _meta_write_lock.
        #   Safe because:
        #              this is a whole-file atomic replace that reads nothing;
        #              taking a leaf lock around it changes no value written.
        #   Preserves: unknown meta keys survive a cancel (its RMW is now atomic
        #              against this write rather than interleaved with it).
        #   Lock/thread contract:
        #              acquires _meta_write_lock, then _meta_cache_lock (outer →
        #              inner). Never called holding _status_lock.
        #   Pinned by: test_vault_regressions.py::TestCancelDoesNotClobberFinalMeta
        with self._meta_write_lock:
            self._write_index_meta_locked(path, {
                "version": OBSIDIAN_INDEX_VERSION,
                "indexed_at": datetime.now(timezone.utc).isoformat(),
                "embed": embed_name,
                "provider": provider_name,
                "partial": partial,
                "phase": phase,
                "has_vector_data": has_vector_data,
                "inserted_this_run": inserted_this_run,
                # Which on-disk vector store this index uses. Authoritative for
                # load/incremental decisions; absence (older meta) ⇒ "simple".
                "vector_backend": vector_backend,
                # A1 (2026-07-13): which vault this index was built from. Absent in
                # every pre-fix meta ⇒ the incremental gate adopts the current vault
                # (no forced rebuild on upgrade); once stamped, a mismatch forces a
                # fresh rebuild so a vault switch cannot serve a cross-vault mix.
                "vault_path": vault_path or "",
                # (2026-07-26): which LOCAL backend produced the vectors. Absent in
                # every pre-fix meta; the gate then falls back to the recorded
                # `provider` when that is local (`_prev_embed_provider`), and only
                # adopts-and-stamps silently when neither field yields a usable
                # local name — so the upgrade still forces no reindex. Once a
                # previous backend IS known, a mismatch REFUSES the run (2026-07-27)
                # rather than rebuilding: see the gate in `index_vault`.
                "embed_provider": embed_provider or "",
            })

    def _write_index_meta_locked(self, path: str, payload: dict) -> None:
        """Write *payload* to the meta file. Caller holds ``_meta_write_lock``."""
        _write_json_atomic(path, payload)
        # Drop the read cache eagerly.  The stat key would catch the rewrite
        # on its own; the explicit clear just removes any dependence on the
        # filesystem's mtime resolution for back-to-back writes.
        with self._meta_cache_lock:
            self._meta_cache = None
            self._meta_cache_key = None

    # --- Vector store backend (Batch 4) -------------------------------------

    def _resolve_existing_backend(self, prev_meta: Optional[dict]) -> str:
        """Backend an EXISTING index is stored in — authoritative for loads.

        Read from ``obsidian_meta.json``; a missing key means the legacy JSON
        ``SimpleVectorStore`` (every index written before Batch 4). This must
        never be overridden by the config knob, or an incremental run would try
        to open the wrong store.
        """
        backend = (prev_meta or {}).get("vector_backend") or VECTOR_BACKEND_SIMPLE
        return backend if backend in (VECTOR_BACKEND_LANCEDB, VECTOR_BACKEND_SIMPLE) else VECTOR_BACKEND_SIMPLE

    @staticmethod
    def _prev_embed_provider(prev_meta: Optional[dict]) -> str:
        """Best evidence for WHICH LOCAL BACKEND produced an index's vectors.

        Returns the recorded ``embed_provider`` when present; otherwise the
        recorded ``provider`` when that names a LOCAL backend; otherwise ``""``
        (no usable evidence ⇒ the caller grandfathers and adopts silently).

        Why the fallback (2026-07-27). ``embed_provider`` only started being
        stamped on 2026-07-26, so every pre-existing index lacks it — and the
        FIRST run after that upgrade is exactly the run most likely to be a
        provider switch. Treating "absent" as "adopt" therefore mixed two vector
        spaces once and then stamped the current backend over the poisoned
        index, after which the gate could never detect the damage. But the
        evidence was already on disk: ``provider`` records the CHAT provider at
        index time, and ``resolve_embed_provider`` returns a LOCAL chat provider
        UNCHANGED — so a locally-recorded ``provider`` is, by construction,
        exactly the name that call would have returned for that run. Using it
        turns a silent mix into a refusal at the one moment it matters.

        Why an ONLINE recorded ``provider`` is NOT usable: with an online chat
        provider, ``resolve_embed_provider`` ignores it entirely and consults
        the separate ``embed_provider`` CONFIG key (defaulting to ollama). The
        recorded value therefore says nothing about which local backend served
        the embeddings — inferring one would be a guess, and a wrong guess here
        refuses a legitimate incremental. Those indexes keep the true
        grandfathering (adopt + stamp at the next persist).

        ``is_local_provider`` is a whitelist membership test, deliberately used
        instead of ``not is_online_provider`` so a garbage/typo'd recorded name
        falls into "no usable evidence" rather than being trusted as local.
        """
        meta = prev_meta or {}
        recorded = str(meta.get("embed_provider") or "").strip()
        if recorded:
            return recorded
        from core.config import is_local_provider
        prev_chat = str(meta.get("provider") or "").strip()
        return prev_chat if prev_chat and is_local_provider(prev_chat) else ""

    def _indexed_chunk_count_hint(self) -> int:
        """Approximate chunk count of the on-disk index, or 0 if unknown.

        Read from ``indexed_materials.json`` (a small aggregate the indexer
        writes at every checkpoint), NOT from the docstore — the refusal path
        that uses it runs before any index is loaded and must stay cheap and
        infallible. Purely cosmetic: it turns "a full re-embed" into "a full
        re-embed of 411,823 chunks" in the refusal message, which is what makes
        the cost legible. Never raises, never blocks a decision.
        """
        try:
            with open(self._manifest_path(), encoding="utf-8") as f:
                payload = json.load(f)
            materials = payload.get("materials") if isinstance(payload, dict) else None
            if not isinstance(materials, list):
                return 0
            return sum(
                int(m.get("chunk_count") or 0)
                for m in materials
                if isinstance(m, dict)
            )
        except Exception:  # noqa: BLE001 — a cosmetic hint must never fail a run
            return 0

    def _incremental_store_is_intact(self, prev_meta: Optional[dict]) -> bool:
        """Whether the on-disk store is safe to LOAD incrementally — the
        WRITER-side twin of the readers' promotion-marker check (item 2.8a
        follow-up, 2026-07-05).

        Defect it guards: checkpoint promotion replaces the store files one
        ``os.replace`` at a time, so a hard crash (SIGKILL/power loss) between
        two replaces leaves a MIXED-generation ("torn") store. The READERS
        (``_ensure_index_loaded`` / ``prewarm``) already refuse a torn store via
        ``_validate_persisted_index_files(check_promotion_marker=True)`` and
        surface "Re-run indexing to rebuild a consistent checkpoint." But the
        WRITER (``index_vault``'s incremental branch) skipped that check, so the
        documented recovery re-loaded the torn store INCREMENTALLY — the
        document-hash skip never re-inserts the stranded chunks — and then
        stamped a fresh ``"complete"`` marker over it, permanently sealing the
        inconsistency from every future load-time check. Honoring the marker
        here forces ``is_incremental=False`` so the run archives + rebuilds
        fresh: the recovery actually rebuilds.

        Safe w.r.t. existing state: reuses the SAME validator, files, and
        ``full=False`` tail-only check the readers use (no vector re-scan), so a
        normal store passes exactly as it does for chat/prewarm; only a torn (or
        structurally-incomplete) store returns False. No on-disk format changes.
        Pinned by ``TestReindexHonorsPromotionMarker``.
        """
        try:
            self._validate_persisted_index_files(
                OBSIDIAN_INDEX_DIR,
                full=False,
                backend=self._resolve_existing_backend(prev_meta),
                require_vector_data=False,
                check_promotion_marker=True,
            )
            return True
        except Exception:
            return False

    def _resolve_fresh_backend(self) -> str:
        """Backend to use for a brand-new index build.

        Driven by the ``vault_vector_backend`` config knob, whose default is
        ``simple`` (root CLAUDE.md is authoritative). ``load_config`` materialises
        every default, so the knob is present on virtually every read; the
        ``or SIMPLE`` fallback (B10/#31, 2026-07-13 — was ``LANCEDB``, which
        contradicted the config default) only covers a hand-cleared empty value,
        which now also resolves to ``simple`` instead of silently flipping to the
        binary backend. Falls back to ``simple`` with a one-time warning when
        lancedb is configured but not importable, mirroring the BM25/reranker
        import-guard discipline — a missing optional dep degrades, never crashes.
        """
        try:
            choice = (load_config().get("vault_vector_backend") or VECTOR_BACKEND_SIMPLE).strip().lower()
        except Exception:
            choice = VECTOR_BACKEND_SIMPLE
        if choice == VECTOR_BACKEND_LANCEDB:
            if not lancedb_available():
                self._emit(
                    "WARNING: vault_vector_backend is 'lancedb' but the lancedb "
                    "package is not installed; building a legacy JSON index "
                    "instead. Install lancedb to use the binary backend."
                )
                return VECTOR_BACKEND_SIMPLE
            return VECTOR_BACKEND_LANCEDB
        return VECTOR_BACKEND_SIMPLE

    def _build_storage_context(self, *, fresh: bool, backend: str) -> StorageContext:
        """Construct the StorageContext for *backend*.

        ``fresh`` ⇒ no ``persist_dir`` (empty stores); otherwise re-hydrate the
        docstore/index_store from ``OBSIDIAN_INDEX_DIR``. On the lancedb path an
        explicit ``vector_store=`` is supplied, so LlamaIndex never reads (or
        requires) ``default__vector_store.json`` — the vectors live in the
        binary ``lancedb/`` directory instead. Its existing docstore is imported
        record-wise into disposable disk scratch rather than materialised in RAM.
        """
        if backend == VECTOR_BACKEND_LANCEDB:
            vector_store = make_lancedb_vector_store(OBSIDIAN_INDEX_DIR)
            if fresh:
                return StorageContext.from_defaults(vector_store=vector_store)
            docstore = load_disk_docstore(
                os.path.join(OBSIDIAN_INDEX_DIR, "docstore.json"), OBSIDIAN_CACHE_DIR,
            )
            try:
                return StorageContext.from_defaults(
                    persist_dir=OBSIDIAN_INDEX_DIR, vector_store=vector_store,
                    docstore=docstore,
                )
            except BaseException:
                if isinstance(docstore._kvstore, DiskKVStore):
                    docstore._kvstore.close()
                raise
        # Legacy SimpleVectorStore — byte-identical to the pre-Batch-4 calls.
        return StorageContext.from_defaults() if fresh else StorageContext.from_defaults(
            persist_dir=OBSIDIAN_INDEX_DIR
        )

    def _build_index_for_backend(
        self, *, fresh: bool, backend: str, embed_model: Any
    ) -> Any:
        """Create (fresh) or load (incremental) the index on *backend*.

        ``store_nodes_override=True`` is mandatory on the lancedb path: the
        external vector store does not persist node text, so without it the
        docstore would stop being populated on incremental runs and BM25 +
        the document-hash skip-check would silently break.
        """
        storage_ctx = self._build_storage_context(fresh=fresh, backend=backend)
        override = backend == VECTOR_BACKEND_LANCEDB
        if fresh:
            return VectorStoreIndex.from_documents(
                [],
                storage_context=storage_ctx,
                embed_model=embed_model,
                store_nodes_override=override,
            )
        return load_index_from_storage(
            storage_ctx, embed_model=embed_model, store_nodes_override=override
        )

    def _cached_lancedb_count(self, index_dir: str) -> int:
        """Cached wrapper over ``lancedb_table_count`` for the status-poll path.

        ``get_status`` is polled ~1 Hz and only needs the boolean ``count > 0``,
        but each raw ``lancedb_table_count`` opens a fresh ``lancedb.connect()``
        that re-lists the fragment manifest — avoidable per-poll churn during a
        reindex (exactly the RAM-pressure window). Cache by the lancedb dir's
        ``(st_size, st_mtime_ns)`` with a short monotonic TTL backstop.

        The stat key reliably catches the dir *going away* (``/api/reset``,
        version-bump archive): ``os.stat`` then raises and we fall back to the
        raw count. It does NOT reliably catch row *inserts*, though — LanceDB
        writes fragments into nested subdirs, which need not bump the top dir's
        mtime — so the ``_LANCEDB_COUNT_TTL_S`` TTL is the effective invalidator
        for the empty→non-empty flip during a fresh build. That is fine here: the
        only consumer is the monotonic ``count > 0`` status gate, so the worst
        case is a ≤TTL-second cosmetic lag before the status banner reflects the
        first inserted row; it never reports stale *non-empty* (count only drops
        to 0 on a reset, which the stat key catches). Exact-count callers do not
        use this path.
        """
        db_dir = lancedb_dir(index_dir)
        try:
            st = os.stat(db_dir)
            key = (db_dir, st.st_size, st.st_mtime_ns)
        except OSError:
            return lancedb_table_count(index_dir)
        now = time.monotonic()
        with self._lancedb_count_lock:
            if (
                self._lancedb_count_cache is not None
                and self._lancedb_count_key == key
                and now - self._lancedb_count_at < self._LANCEDB_COUNT_TTL_S
            ):
                return self._lancedb_count_cache
        # Compute outside the lock — lancedb.connect() does file I/O.
        count = lancedb_table_count(index_dir)
        with self._lancedb_count_lock:
            self._lancedb_count_cache = count
            self._lancedb_count_key = key
            self._lancedb_count_at = now
        return count

    def _index_dir_has_vector_data(self, index_dir: str) -> bool:
        """Return True only when a persisted LlamaIndex store has useful data.

        Cache-only legacy directories should not be archived as recoverable
        vector indexes, and an empty docstore/vector store should not surface a
        misleading Resume path.  The checks are deliberately cheap because
        get_status() is polled by the UI; full JSON/schema validation happens
        before loading a persisted index and before promoting a new checkpoint.
        """
        root = Path(index_dir)
        docstore = root / "docstore.json"
        index_store = root / "index_store.json"
        if not docstore.exists() or not index_store.exists():
            return False
        if not (
            _json_file_has_complete_tail(docstore)
            and _json_file_has_complete_tail(index_store)
        ):
            return False
        backend = self._resolve_existing_backend(self._read_index_meta())
        if backend == VECTOR_BACKEND_LANCEDB:
            # Vectors live in the binary lancedb/ dir, not a JSON file. A
            # populated table (>0 rows) is the lancedb analogue of
            # _simple_vector_store_has_any_embedding. Use the cached count: this
            # path is on the ~1 Hz status poll, and the exact-count callers
            # (crash-drift recovery, checkpoint validation) keep calling
            # lancedb_table_count directly.
            return self._cached_lancedb_count(index_dir) > 0
        vector_store = root / "default__vector_store.json"
        if not vector_store.exists():
            return False
        return (
            _json_file_has_complete_tail(vector_store)
            and _simple_vector_store_has_any_embedding(vector_store)
        )

    _PROMOTION_MARKER = "promotion_state.json"

    def _validate_persisted_index_files(
        self,
        index_dir: str,
        *,
        full: bool = True,
        backend: str = VECTOR_BACKEND_SIMPLE,
        require_vector_data: bool = True,
        check_promotion_marker: bool = False,
    ) -> None:
        """Validate the files that make up a LlamaIndex checkpoint.

        On the lancedb backend the vectors live in the binary ``lancedb/`` dir,
        not ``default__vector_store.json`` — so that file is neither required
        nor expected. ``require_vector_data`` is set False when validating a
        *temporary* checkpoint dir (the lancedb table is already durable in the
        live index dir and is not copied into the temp dir).

        ``check_promotion_marker`` (improvement plan 2026-07-04, item 2.8a) is
        set by the LOAD-time callers only: checkpoint promotion replaces the
        store files one ``os.replace`` at a time — per-file atomic, but not
        transactional across files — so a crash between the docstore replace
        and the index_store replace left a MIXED-generation store that parsed
        cleanly and loaded silently; chunks recorded in one file but not the
        other were then stranded forever (the document-hash skip never
        re-inserts what the docstore claims to have). The promotion now brackets
        the replaces with a marker file ("promoting" before the first replace,
        "complete" after the last, both fsync'd atomic writes): a load that
        finds the marker still saying "promoting" refuses with THIS clear error
        instead of silently serving the torn store, and the existing
        integrity-error recovery (paused_scan + rebuild guidance) takes over.
        A missing marker is accepted — every pre-marker checkpoint is legacy
        and grandfathered. The temp-dir validation during checkpointing never
        passes this flag (no marker exists there by design).
        """
        root = Path(index_dir)
        if check_promotion_marker:
            marker = root / self._PROMOTION_MARKER
            if marker.exists():
                try:
                    state = json.loads(marker.read_text(encoding="utf-8")).get("state")
                except (OSError, json.JSONDecodeError, AttributeError):
                    state = "unreadable"
                if state != "complete":
                    raise RuntimeError(
                        "The index checkpoint was interrupted mid-promotion "
                        "(store files may be from mixed generations). Use "
                        "the explicitly confirmed Rebuild from scratch action to restore a consistent checkpoint."
                    )
        required = ["docstore.json", "index_store.json"]
        if backend != VECTOR_BACKEND_LANCEDB:
            required.append("default__vector_store.json")
        missing = [name for name in required if not (root / name).exists()]
        if missing:
            raise RuntimeError(f"Index checkpoint is missing required file(s): {', '.join(missing)}")

        for name in required:
            path = root / name
            if not _json_file_has_complete_tail(path):
                raise RuntimeError(f"Index checkpoint file {name} appears incomplete.")

        if require_vector_data:
            if backend == VECTOR_BACKEND_LANCEDB:
                if lancedb_table_count(index_dir) <= 0:
                    raise RuntimeError("Index checkpoint has no vector embeddings (empty lancedb table).")
            else:
                vector_store = root / "default__vector_store.json"
                if not _simple_vector_store_has_any_embedding(vector_store):
                    raise RuntimeError("Index checkpoint has no vector embeddings.")
        if not full:
            return

        for name in _LLAMAINDEX_PERSIST_FILES:
            path = root / name
            if not path.exists():
                continue
            try:
                with path.open(encoding="utf-8") as f:
                    json.load(f)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Index checkpoint file {name} is corrupt or incomplete "
                    f"({exc.msg} at line {exc.lineno}, column {exc.colno})."
                ) from exc
            except OSError as exc:
                raise RuntimeError(f"Could not read index checkpoint file {name}: {exc}") from exc

    @staticmethod
    def _lancedb_vector_store_of(idx: Any) -> Any:
        """Best-effort fetch of *idx*'s vector store, for compaction.

        ``StorageContext.vector_store`` is a *property* that can raise (e.g. a
        ``KeyError`` if the default-store key is somehow absent), and a bare
        ``getattr`` only swallows ``AttributeError`` — so an unguarded access
        could propagate out and abort a multi-hour indexing run.  The compaction
        this feeds is documented strictly best-effort, so a failure to even
        locate the store must degrade to "no compaction", never raise.  Returns
        None on any failure; ``compact_lancedb_vector_store(None)`` then no-ops.
        """
        try:
            return idx.storage_context.vector_store
        except Exception:
            return None

    def _persist_index_checkpoint(self, idx: Any, backend: str = VECTOR_BACKEND_SIMPLE) -> None:
        """Persist LlamaIndex storage through a validated temporary checkpoint.

        SimpleVectorStore writes its (very large) JSON directly, so persisting to
        a sibling temp dir first prevents an interrupted write from truncating
        the active checkpoint; only after every persisted JSON file parses do we
        promote them into the active storage directory.

        On the lancedb backend the vector data is written transactionally into
        the binary ``lancedb/`` dir during ``idx.insert`` — it is already durable
        and is **not** part of this temp-promote dance. Only ``docstore.json`` /
        ``index_store.json`` (plus the empty graph/image stores) are promoted, so
        the temp validation skips the vector-data check (``require_vector_data``
        False) — the table is in the live dir, not the temp one.
        """
        target = Path(OBSIDIAN_INDEX_DIR)
        parent = target.parent
        parent.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(prefix=f".{target.name}.checkpoint.", dir=parent))
        try:
            idx.storage_context.persist(persist_dir=str(tmp_dir))
            persisted = [
                path.name for path in tmp_dir.iterdir()
                if path.is_file() and path.name in _LLAMAINDEX_PERSIST_FILES
            ]
            if persisted:
                # full=False: keep the cheap structural tail-check + the vector-data
                # check, but SKIP the full json.load() sweep of every persisted file.
                # On the simple backend that sweep re-parsed the ~3 GB
                # default__vector_store.json (and the ~620 MB docstore.json) into a
                # throwaway object graph on EVERY checkpoint — with the GIL + rw
                # write lock held, so chat stalled for the whole parse.
                #
                # Why this is safe (no integrity guarantee lost):
                #   1. These files were just written by LlamaIndex's own json.dump
                #      moments earlier; json.dump writes a complete document or the
                #      process died mid-write — and a truncated tail is exactly what
                #      _json_file_has_complete_tail (still run) catches.
                #   2. The read-back happens immediately after the write, so a
                #      json.load() served from the page cache re-parses the very
                #      bytes we just wrote — it never validated the ON-DISK bytes, so
                #      it never actually guarded against disk/FS corruption.
                #   3. The real parse still happens at LOAD time: _ensure_index_loaded
                #      and prewarm already validate with full=False and then let
                #      _build_index_for_backend do the authoritative parse, converting
                #      any JSONDecodeError into a checkpoint-integrity error + a
                #      paused_scan recovery. So genuine corruption is still caught —
                #      at load, where the parse cost is paid once, not on every
                #      write-lock-blocking checkpoint.
                self._validate_persisted_index_files(
                    str(tmp_dir),
                    full=False,
                    backend=backend,
                    require_vector_data=(backend != VECTOR_BACKEND_LANCEDB),
                )
            target.mkdir(parents=True, exist_ok=True)
            # Item 2.8a: the per-file os.replace loop is atomic per FILE but
            # not transactional across files. Bracket it with a marker so a
            # crash inside the loop is detectable at load instead of silently
            # serving a mixed-generation store (see the validator docstring).
            # Marker writes are fsync'd (_write_json_atomic); this guards the
            # process-crash/SIGKILL window (where rename ordering holds) —
            # power-loss rename reordering is out of scope, same as the
            # documented no-parent-fsync stance of the atomic writers.
            marker_path = str(target / self._PROMOTION_MARKER)
            _write_json_atomic(marker_path, {
                "state": "promoting",
                "started_at": datetime.now(timezone.utc).isoformat(),
            })
            for name in _LLAMAINDEX_PERSIST_FILES:
                src = tmp_dir / name
                if src.exists():
                    os.replace(src, target / name)
            _write_json_atomic(marker_path, {
                "state": "complete",
                "finished_at": datetime.now(timezone.utc).isoformat(),
            })
            if backend == VECTOR_BACKEND_LANCEDB:
                # Both callers hold _index_mutation_lock, so compaction here is
                # safe without re-locking.  Compact + prune superseded versions so
                # the binary store's on-disk footprint stays O(n) instead of the
                # O(n²) per-insert version-manifest growth.  Best-effort: both the
                # store fetch and the compaction itself swallow failures so a
                # compaction hiccup never aborts the checkpoint.
                compact_lancedb_vector_store(self._lancedb_vector_store_of(idx))
            self._index_integrity_error = ""
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _index_has_vector_data(self, idx: Any) -> bool:
        docstore = getattr(idx, "docstore", None)
        get_all_ref_doc_info = getattr(docstore, "get_all_ref_doc_info", None)
        if callable(get_all_ref_doc_info):
            try:
                return bool(get_all_ref_doc_info())
            except Exception:
                return False
        return False

    def _migrate_legacy_caches(self) -> None:
        """Best-effort copy of old cache files out of the vector index dir."""
        legacy_root = Path(OBSIDIAN_INDEX_DIR)
        cache_root = Path(OBSIDIAN_CACHE_DIR)
        for name in ("pdf_cache", "image_cache"):
            src = legacy_root / name
            dst = cache_root / name
            if not src.exists():
                continue
            try:
                for item in src.rglob("*"):
                    if not item.is_file() or item.name == ".DS_Store":
                        continue
                    rel = item.relative_to(src)
                    target = dst / rel
                    if target.exists():
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, target)
            except Exception:
                logger.debug("Could not migrate legacy %s.", name, exc_info=True)
        sig_src = legacy_root / "pdf_signatures.json"
        if sig_src.exists():
            try:
                old = json.loads(sig_src.read_text(encoding="utf-8"))
                current = self._load_pdf_signature_cache()
                if isinstance(old, dict):
                    current.update(old)
                    self._save_pdf_signature_cache(current)
            except Exception:
                logger.debug("Could not migrate legacy pdf signatures.", exc_info=True)

    # Per-doc circuit-breaker thresholds for _index_documents_streaming.
    # PERSIST_EVERY: how many successful inserts before a mid-run checkpoint
    # becomes eligible.  Bounds re-work on a crash to roughly this many
    # embeddings.
    # PERSIST_MIN_INTERVAL_S: additionally, at least this much wall-clock time
    # must have passed since the previous checkpoint (attempt).  Each
    # checkpoint re-serialises and re-validates the ENTIRE store
    # (_persist_index_checkpoint dumps + json-parses every persisted file with
    # the GIL held, and chat is blocked on the rw write lock for the
    # duration), so on a fast embedder a count-only cadence spends a growing
    # fraction of the run inside O(N) dumps.  Both conditions must hold, so
    # the crash re-work bound is max(_PERSIST_EVERY chunks, ~10 min) — never
    # worse than the old count-only bound on slow embedders, and a bounded
    # dump frequency on fast ones.  Class attrs so tests can patch them
    # (set the interval to 0 to restore count-only behaviour).
    # MAX_CONSECUTIVE_FAILURES: abort the run when this many inserts in a row
    # fail.  Paired with the interruptible backoff below (_FAILURE_BACKOFF_*),
    # the breaker is a *wall-clock* window, not an instant count: each
    # consecutive failure waits a little longer before the next attempt, so a
    # backend that briefly hiccups (e.g. LM Studio JIT-reloading the embed model
    # under memory pressure) gets ~tens of seconds to recover and reset the
    # streak, while a truly-down backend still aborts in well under a minute
    # rather than wasting hours.  Without the backoff a backend that instant-
    # rejects (HTTP 400 in ~10 ms) burned through all 20 "retries" in ~0.2 s and
    # aborted a multi-hour run before the model could finish loading.
    _PERSIST_EVERY = 500
    _PERSIST_MIN_INTERVAL_S = 600
    _MAX_CONSECUTIVE_FAILURES = 20
    # Track 5.6: fallback embed-batch size when the config knob is missing or
    # garbage. Class attr so tests can patch it (like the cadence knobs above).
    _EMBED_BATCH_DEFAULT = 16
    # Beat interval for the reranker-load heartbeat (2026-07-27). Well under the
    # SSE consumer's 330 s stall window (SSE_SINGLE_SHOT_FLOOR_S +
    # SSE_STALL_MARGIN_S) so several beats land before it could ever fire. Class
    # attr for the same reason as the cadence knobs above: tests patch it to a
    # few milliseconds instead of sleeping (TestRerankerLoadHeartbeat).
    _RERANKER_HEARTBEAT_S = 30.0
    # Backoff between *consecutive* insert failures: wait
    # min(BASE * 2**(streak-1), CAP) seconds before retrying the next chunk.
    # A single failure followed by success costs one BASE pause, then the streak
    # resets to 0.  Class attrs so tests patch them to 0 for instant runs (same
    # pattern as _PERSIST_MIN_INTERVAL_S).  With 1.0/5.0 the 19 sleeps before the
    # 20th-failure abort sum to ~87 s of recovery window.
    _FAILURE_BACKOFF_BASE_S = 1.0
    _FAILURE_BACKOFF_CAP_S = 5.0
    # LanceDB only: compact + prune superseded versions every this many inserts,
    # INDEPENDENTLY of the ≥10-min JSON-checkpoint cadence.  The checkpoint gate
    # (max 500 inserts AND 600 s) is too coarse to bound the O(n²) version-manifest
    # bloat on a fast embedder — the first checkpoint can be ~30k inserts in, by
    # which point tens of GB of interim single-row-fragment manifests have piled
    # up.  A tighter insert-count cadence keeps the live fragment count (and thus
    # each manifest) small throughout.  Runs under _index_mutation_lock only (it
    # changes neither query results nor the docstore), so it is much lighter than
    # the full checkpoint.  No-op on the simple backend.  Class attr so tests can
    # patch it down.
    _LANCEDB_COMPACT_EVERY = 2000

    # LanceDB only: how many stale chunks the sweep retires per vector-store
    # round-trip.  ``idx.delete_ref_doc`` issues ONE delete per chunk, which on
    # lancedb is a ``table.delete("doc_id = …")`` — a full scan of a multi-GB
    # table plus a new version manifest (~3 s/chunk measured).  Reorganising a
    # large attachments folder strands hundreds of thousands of stale chunks, so
    # the per-chunk path turns a ~1 h sweep into a multi-week one (measured 0.33
    # deletes/s against a 436k backlog — the run never finished, and each
    # interrupted attempt re-stranded the same backlog).  Batching resolves each
    # ref_doc's node ids and hands them to ``delete_nodes``, which the
    # integration renders as ONE ``id in (…)`` predicate.  500 matches the
    # batch size ``lancedb_delete_ids`` already uses for the orphan sweep.
    # Class attr so tests can patch it down.
    _STALE_DELETE_BATCH = 500

    # LanceDB changed-chunk replacement uses the same bounded predicate size as
    # stale cleanup. Normal embed batches are at most 256, but keeping a hard
    # independent delete bound protects tests/config evolution from coupling the
    # table query's size to the provider's embedding batch limit.
    _CHANGED_DELETE_BATCH = 500
    _CHANGED_DELETE_NODE_BATCH = 2000

    # Progress-emit cadence (seconds) for the stale sweep. Frequent enough that
    # a multi-minute lancedb sweep visibly moves (the old loop emitted nothing
    # for its whole duration and read as a hang), rare enough that a fast
    # simple-backend sweep doesn't spam the status feed.
    _STALE_PROGRESS_EMIT_S = 5.0

    # TTL (seconds) for the status-poll lancedb count cache. Bounds how long a
    # stale count>0 boolean can be served; benign because that boolean only
    # gates an empty→non-empty transition that, once true, stays true.
    _LANCEDB_COUNT_TTL_S = 5.0

    def _delete_stale_group(self, idx: Any, batch: list[str], batched: bool) -> None:
        """Delete one group of stale ref_doc ids.

        ``batched`` selects the lancedb path (one round-trip for the whole
        group).  The simple backend passes False and keeps the original
        per-chunk ``delete_ref_doc`` call verbatim — its vector delete is an
        in-memory pop, so batching buys nothing and re-deriving the docstore
        bookkeeping would be risk without benefit.
        """
        if not batched:
            with self._mutate_index():
                idx.delete_ref_doc(batch[0], delete_from_docstore=True)
            return
        self._delete_stale_batch(idx, batch)

    def _delete_stale_batch(self, idx: Any, batch: list[str]) -> None:
        """Retire a batch of stale ref_doc ids in ONE vector-store round-trip.

        Reproduces exactly what ``delete_ref_doc`` does per chunk, minus the
        per-chunk vector delete:

        * ``delete_nodes(delete_from_docstore=True)`` drops the rows (one
          ``id in (…)`` predicate), each node's ``index_struct`` entry, and each
          node from the docstore.  Node ids are UUIDs, so they cannot carry the
          quoting hazards a ``doc_id`` predicate has to escape.
        * ``docstore.delete_ref_doc`` then retires the ``ref_doc_info`` entry.
          It is a no-op when dropping the last node already removed it, but it
          must not be skipped: ``previous_doc_ids`` is derived from
          ``get_all_ref_doc_info()``, so a surviving entry would make every
          later run re-sweep a chunk that is already gone.

        The ``get_ref_doc_info`` reads below run OUTSIDE ``_index_mutation_lock``
        on purpose: the sweep is a phase of the single indexing thread — the
        only docstore *mutator* — so the ids cannot be deleted from under it,
        and keeping the read-side resolution out of the lock means a concurrent
        chat's retrieval only contends the (short) delete call, not the whole
        batch's bookkeeping. If the sweep is ever moved off the indexing thread,
        the reads must move inside the lock with the delete.
        """
        docstore = idx.docstore
        node_ids: list[str] = []
        for doc_id in batch:
            try:
                info = docstore.get_ref_doc_info(doc_id)
            except Exception:
                info = None
            if info is not None:
                node_ids.extend(info.node_ids)
        with self._mutate_index():
            if node_ids:
                idx.delete_nodes(node_ids, delete_from_docstore=True)
            for doc_id in batch:
                docstore.delete_ref_doc(doc_id, raise_error=False)

    def _delete_changed_lancedb_batch(
        self, idx: Any, doc_ids: list[str]
    ) -> tuple[set[str], dict[str, Exception]]:
        """Delete old versions for changed chunks with bounded round-trips.

        The indexing thread is the sole docstore mutator and callers invoke this
        immediately before inserting the same buffered documents. Ref-doc-info
        resolution can therefore run outside ``_index_mutation_lock``; retrieval
        is read-only, while the one vector/docstore mutation remains serialized
        with it under the lock. One failed batch degrades to per-doc deletion so
        a bad row cannot block its peers. A returned id is the only authority for
        marking ``deleted_old=True``; if later embedding/insertion/cancellation
        fails, the existing gap accounting reports that exact id for the next
        run. Nothing persisted changes shape: doc ids, hashes, checkpoint order,
        and migration compatibility are unchanged. The optimization replaces up
        to 500 full-table LanceDB predicates with bounded ≤2,000-node predicates
        (normally one) without weakening the delete-before-insert or
        partial-failure invariants.
        """
        wanted = list(dict.fromkeys(doc_ids))
        if len(wanted) > self._CHANGED_DELETE_BATCH:
            raise ValueError("changed-delete batch exceeds its resource bound")
        docstore = idx.docstore
        node_ids: list[str] = []
        batchable: list[str] = []
        fallback: list[str] = []
        for doc_id in wanted:
            try:
                info = docstore.get_ref_doc_info(doc_id)
                ids = list(info.node_ids) if info is not None else []
            except Exception:
                ids = []
            if ids:
                node_ids.extend(ids)
                batchable.append(doc_id)
            else:
                # A crash can leave a durable LanceDB row ahead of its JSON
                # ref-doc entry. Only delete_ref_doc(doc_id) can address that
                # exceptional row, so retain the old isolated fallback for it.
                fallback.append(doc_id)

        succeeded: set[str] = set()
        errors: dict[str, Exception] = {}
        if batchable:
            try:
                with self._mutate_index():
                    unique_node_ids = list(dict.fromkeys(node_ids))
                    for start in range(0, len(unique_node_ids),
                                       self._CHANGED_DELETE_NODE_BATCH):
                        idx.delete_nodes(
                            unique_node_ids[
                                start:start + self._CHANGED_DELETE_NODE_BATCH],
                            delete_from_docstore=True)
                    for doc_id in batchable:
                        docstore.delete_ref_doc(doc_id, raise_error=False)
                succeeded.update(batchable)
            except Exception:
                # delete_nodes may have failed before or after a partial store
                # mutation. Per-doc delete_ref_doc is idempotent for missing
                # rows and re-establishes both vector/docstore cleanup truth.
                fallback.extend(batchable)

        for doc_id in fallback:
            try:
                with self._mutate_index():
                    idx.delete_ref_doc(doc_id, delete_from_docstore=True)
                succeeded.add(doc_id)
            except Exception as exc:
                errors[doc_id] = exc
        return succeeded, errors

    def _sweep_stale_doc_ids(
        self,
        idx: Any,
        stale_doc_ids: list[str],
        vector_backend: str = VECTOR_BACKEND_SIMPLE,
        op_epoch: int = 0,
    ) -> int:
        """Delete chunks whose source is gone, batching the round-trip on lancedb.

        On the simple backend a vector delete is an in-memory dict pop, so
        batching buys nothing and the per-chunk path is kept byte-for-byte.  On
        lancedb it is a multi-second table scan — see ``_STALE_DELETE_BATCH``.

        The loop polls ``_stop_event`` per batch: the sweep used to check it only
        *before* the loop, so once started it ran to completion and Cancel could
        not stop it — the only exit was killing the process, which is what left
        the table ahead of the docstore ("possible torn write") on the next load.
        An interrupted sweep is safe: it deletes a prefix of the set, and the
        next completed run re-derives the remainder from scratch.

        Returns the number of chunks deleted.
        """
        total = len(stale_doc_ids)
        if not total:
            return 0
        batched = vector_backend == VECTOR_BACKEND_LANCEDB
        batch_size = self._STALE_DELETE_BATCH if batched else 1
        self._emit(f"Removing {total} stale chunk(s) from deleted/moved files…")
        deleted = 0
        last_emit = time.monotonic()
        for start in range(0, total, batch_size):
            if self._stop_event.is_set():
                self._emit(
                    f"Note: stale cleanup stopped at {deleted}/{total} chunk(s); "
                    "the rest are removed on the next completed run."
                )
                break
            batch = stale_doc_ids[start : start + batch_size]
            try:
                self._delete_stale_group(idx, batch, batched)
                deleted += len(batch)
            except Exception as exc:
                # One malformed id must not block its batch-mates: retry the
                # batch one chunk at a time (mirrors lancedb_delete_ids).
                if batched and len(batch) > 1:
                    logger.warning(
                        "Batched stale delete failed (%s); retrying %d chunk(s) singly.",
                        exc,
                        len(batch),
                    )
                    for doc_id in batch:
                        try:
                            self._delete_stale_group(idx, [doc_id], True)
                            deleted += 1
                        except Exception as one_exc:
                            logger.warning(
                                "Failed to delete stale indexed document %s: %s",
                                doc_id,
                                one_exc,
                            )
                else:
                    logger.warning(
                        "Failed to delete stale indexed document %s: %s",
                        batch[0],
                        exc,
                    )
            self._heartbeat_or_stop(op_epoch)
            now = time.monotonic()
            if now - last_emit >= self._STALE_PROGRESS_EMIT_S:
                self._emit(f"Removing stale chunks… {deleted}/{total}")
                last_emit = now
        return deleted

    def _chunk_raw_documents(
        self,
        raw_docs: List[LlamaDocument],
        vault_path: str,
    ):
        """Yield chunked LlamaDocuments one at a time, releasing the raw text
        as each source document finishes splitting.

        Streaming rather than building a full list keeps the RAM peak bounded
        by the largest single source document's chunk count, not the whole
        vault's chunk count.  A vault with five 1000-page textbooks no longer
        holds tens of thousands of LlamaDocument objects in memory before
        embedding starts.
        """
        md_parser = _MarkdownNodeParser(include_metadata=True)
        # 512-token chunks with 64-token overlap — keeps paragraphs together
        # while preserving cross-sentence context at boundaries.
        sentence_splitter = _SentenceSplitter(chunk_size=512, chunk_overlap=64)
        # Secondary cap for the MD branch only: MarkdownNodeParser splits at
        # heading boundaries with no size ceiling, so a long single-heading
        # section can exceed the embedding token limit and be truncated. This
        # re-splits only such oversized sections (see MD_MAX_CHUNK_TOKENS and the
        # conditional pass below). 64-token overlap matches the PDF splitter so a
        # sentence straddling a forced cut stays retrievable. Distinct object
        # from the pinned 512-token PDF splitter above.
        md_secondary = _SentenceSplitter(chunk_size=MD_MAX_CHUNK_TOKENS, chunk_overlap=64)
        # Warn at most once per run if the secondary split ever raises (see the
        # except below). The fallback there is intentionally silent-safe for the
        # run, but a SYSTEMATIC failure would re-truncate every oversized section
        # at embed time — exactly what this pass prevents — so it must stay
        # observable in the log rather than failing invisibly.
        md_split_warned = False
        vault_root = Path(vault_path).resolve()
        for doc in raw_docs:
            if self._stop_event.is_set():
                return
            file_path = doc.metadata.get("file_path") or doc.metadata.get("source", "")
            try:
                rel_str = str(Path(file_path).relative_to(vault_path))
            except ValueError:
                rel_str = file_path

            ext = doc.metadata.get("extension", "").lower()
            # Some tests reload llama_index modules, which can make an older
            # Document instance fail isinstance() checks inside parser internals.
            # Rewrap with the currently-loaded class before parsing.
            from llama_index.core import Document as _CurrentLlamaDocument
            parser_doc = _CurrentLlamaDocument(text=doc.text, metadata=doc.metadata)
            if ext in VAULT_MD_EXTS:
                try:
                    heading_nodes = md_parser.get_nodes_from_documents([parser_doc])
                except Exception as exc:  # noqa: BLE001 — generator guard, see below
                    # BROAD on purpose (2026-07-22 audit P7): this runs inside
                    # a generator, so any unhandled parser exception (a
                    # RecursionError on pathological nesting, a parser
                    # regression) aborts the ENTIRE indexing run — bypassing
                    # the per-chunk insert tolerance — instead of costing one
                    # note. Degrade to one whole-doc section; the secondary
                    # cap pass below still bounds it. (The secondary splitter
                    # has carried this exact guard + rationale for the same
                    # reason; the primary parse had only the narrow
                    # ValueError branch.)
                    self._emit(
                        f"WARNING: markdown parse failed for {rel_str}: {exc} "
                        "— indexed as one unsplit section."
                    )
                    heading_nodes = [TextNode(text=doc.text, metadata=doc.metadata)]
                # SECONDARY CAP PASS. MarkdownNodeParser only splits at heading
                # boundaries, so one long section can exceed the embedding token
                # limit and be silently truncated. Re-split ONLY oversized
                # sections; pass every under-cap section through as the SAME node
                # object so its chunk id (sha1 of i+text, computed below) is
                # byte-identical to the pre-change output → zero re-embed churn
                # for the ~97% of notes with no oversized section.
                nodes = []
                for hn in heading_nodes:
                    hn_text = getattr(hn, "text", "") or ""
                    # Fast path: a cl100k BPE token is always ≥ 1 UTF-8 byte, so
                    # byte_len ≤ cap PROVES token_count ≤ cap — skip tokenizing
                    # the many tiny note-sections. (A char-count floor would be
                    # WRONG for multibyte scripts, where one code point can be
                    # several tokens; bytes are the safe lower bound.)
                    # NOTE: this checks TEXT bytes only, so it bypasses the
                    # splitter's metadata-aware count — a section near the cap is
                    # kept whole even if text+metadata would tip just over it.
                    # That is intentional and safe: the cap (1024) sits ~2x under
                    # the ~2048 embedding limit, so a kept-whole section (text ≤
                    # cap + at most a few hundred metadata tokens) is still well
                    # under the real limit. We trade exact-cap adherence for
                    # byte-identity + speed; do not "tighten" this to bytes(text+
                    # metadata) without re-checking that headroom.
                    if len(hn_text.encode("utf-8")) <= MD_MAX_CHUNK_TOKENS:
                        nodes.append(hn)
                        continue
                    try:
                        # Metadata-aware count: the splitter measures text + the
                        # embedded metadata (file_path/source/header_path), which
                        # is exactly what counts against the embedding limit.
                        # attachments are NOT attached yet (see below), so a big
                        # link list cannot inflate this decision (the _tags.md
                        # case). >1 result ⇒ the section was over cap.
                        # LlamaIndex's splitter annotation accepts Document,
                        # while the first Markdown splitter returns BaseNode;
                        # both implement the exact text/metadata protocol used
                        # here and the runtime API accepts the node unchanged.
                        sub = md_secondary.get_nodes_from_documents(
                            [cast(Any, hn)])
                    except Exception as exc:
                        # A pathological section must never abort the whole run
                        # (this is a generator — an exception here would kill the
                        # consuming indexer, bypassing its per-chunk insert
                        # guard). Degrade to the un-split section, but log once so
                        # a systematic failure (which would silently re-truncate
                        # every oversized section at embed) stays visible.
                        if not md_split_warned:
                            md_split_warned = True
                            logger.warning(
                                "MD secondary split failed for %s; section left "
                                "un-split (may truncate at embed time): %s",
                                rel_str, exc,
                            )
                        sub = [hn]
                    if len(sub) <= 1:
                        # Under cap after the precise count: keep the ORIGINAL
                        # node, not sub[0] — the splitter may normalise
                        # whitespace, and preserving hn guarantees byte-identity.
                        nodes.append(hn)
                    else:
                        nodes.extend(sub)
                # Attachment extraction runs over the FINAL (post-split) node
                # list so each sub-chunk records only the links present in its own
                # text, and the (potentially large) attachments list is attached
                # AFTER the split so it never inflates the split decision above.
                md_path = Path(file_path) if file_path else None
                if md_path is not None:
                    for _raw_node in nodes:
                        attachments = self._extract_md_attachments(
                            getattr(_raw_node, "text", "") or "",
                            md_path,
                            vault_root,
                        )
                        if attachments:
                            meta = getattr(_raw_node, "metadata", None)
                            if not isinstance(meta, dict):
                                meta = {}
                                _raw_node.metadata = meta
                            meta["attachments"] = attachments
            else:
                try:
                    nodes = sentence_splitter.get_nodes_from_documents([parser_doc])
                except Exception as exc:  # noqa: BLE001 — generator guard (P7, as above)
                    self._emit(
                        f"WARNING: sentence split failed for {rel_str}: {exc} "
                        "— indexed as one unsplit chunk."
                    )
                    nodes = [TextNode(text=doc.text, metadata=doc.metadata)]

            # Page-range documents (large PDFs split by _load_pdf_range_documents)
            # salt the chunk hash with their page_start.  Without it, two ranges
            # of the same file could produce an identical (i, text) pair — e.g.
            # boilerplate pages at the same ordinal — yielding duplicate doc_ids,
            # and the streaming indexer would silently drop the second chunk as
            # "unchanged".  Single-document files (page_start absent) keep the
            # original unsalted input so their chunk IDs — and therefore their
            # stored embeddings — survive this change without a reindex.
            page_start = doc.metadata.get("page_start") if doc.metadata else None
            for i, _raw_node in enumerate(nodes):
                # Both node parsers return TextNode instances (BaseNode is the
                # abstract supertype that lacks .text on its public typing).
                node: TextNode = _raw_node  # type: ignore[assignment]
                # P2.7: 16 hex chars (64 bits) instead of 12 (48 bits) — birthday
                # collision space rises from ~16M chunks to ~4B, so a large
                # textbook vault no longer flirts with silent ID collisions.
                if page_start is not None:
                    hash_input = f"{page_start}:{i}\n{node.text}"
                else:
                    hash_input = f"{i}\n{node.text}"
                chunk_hash = hashlib.sha1(
                    hash_input.encode(), usedforsecurity=False
                ).hexdigest()[:16]
                # ``attachments`` (a markdown note's resolved link/embed targets)
                # is kept in metadata for retrieval/manifest use, but must NOT be
                # folded into the embedded/LLM text: an index/MOC note can link to
                # 100+ files, and that list serialises past LlamaIndex's per-node
                # metadata budget (the insert-time splitter's chunk_size), failing
                # the insert (this is why ``_tags.md`` reported "Metadata length
                # (1068) is longer than chunk size (1024)"). Excluding the key
                # keeps the value stored while dropping it from the EMBED/LLM
                # metadata string the metadata-aware splitter measures. The
                # exclusion does not affect the document hash (computed over
                # MetadataMode.ALL), so existing chunks stay "unchanged" and are
                # not re-embedded on the next incremental run.
                excluded_keys = (
                    ["attachments"]
                    if node.metadata and "attachments" in node.metadata
                    else []
                )
                yield LlamaDocument(
                    text=node.text,
                    doc_id=f"{rel_str}::{chunk_hash}",
                    metadata=node.metadata,
                    excluded_embed_metadata_keys=excluded_keys,
                    excluded_llm_metadata_keys=excluded_keys,
                )

    def _extract_md_attachments(
        self,
        text: str,
        md_path: Path,
        vault_root: Path,
        name_index: Optional[dict[str, list[str]]] = None,
    ) -> list[str]:
        """Return vault-relative posix paths for attachment references in *text*.

        Scans both Obsidian wikilinks (``![[image.png]]``) and inline markdown
        links (``[label](file.pdf)``).  Targets are resolved relative to the
        markdown file's parent directory, deduplicated, and expressed as
        vault-relative posix paths.  External URLs and anchors are dropped.
        Retrieval can join these back to indexed chunks by matching the
        ``{rel_path}::`` prefix on doc_ids.

        When *name_index* (a ``basename.lower() -> [vault-relative path]`` map
        of the vault's image files) is supplied, a bare filename that does not
        resolve beside the note falls back to a vault-wide basename lookup,
        matching how Obsidian resolves ``![[image.png]]`` for vaults that keep
        attachments in a central folder.  Callers that want the historical
        parent-relative-only behaviour (the ``attachments`` metadata path) omit
        it.
        """
        if not text:
            return []
        targets: list[str] = []
        seen: set[str] = set()

        def _accept(raw_target: str) -> None:
            target = raw_target.strip()
            if not target:
                return
            target = unquote(target)
            # Drop anchor/block-reference suffixes (#heading, ^block-id).
            target = target.split("#", 1)[0].split("^", 1)[0].strip()
            if not target:
                return
            lowered = target.lower()
            if any(lowered.startswith(sch) for sch in _NON_ATTACHMENT_SCHEMES):
                return
            resolved = self._resolve_md_attachment(
                target, md_path, vault_root, name_index=name_index
            )
            if resolved and resolved not in seen:
                targets.append(resolved)
                seen.add(resolved)

        for match in _OBSIDIAN_WIKILINK_RE.finditer(text):
            _accept(match.group(1))
        for match in _INLINE_LINK_RE.finditer(text):
            # Strip an optional title segment: [label](url "title").
            raw = match.group(1).split(" ", 1)[0]
            _accept(raw)
        return targets

    def _resolve_md_attachment(
        self,
        target: str,
        md_path: Path,
        vault_root: Path,
        name_index: Optional[dict[str, list[str]]] = None,
    ) -> str:
        """Normalise *target* into a vault-relative posix path.

        Resolution order, mirroring Obsidian:
          1. Relative to *md_path*'s parent (handles explicit relative paths
             and images stored beside the note).  An existing file here wins
             outright.
          2. If *name_index* is supplied and *target* is a bare filename (no
             directory component) that did not resolve in step 1, fall back to
             a vault-wide basename lookup — Obsidian's "shortest path" link
             resolution, which is how attachments in a central folder are
             referenced from notes elsewhere in the vault.

        Returns "" if the target escapes the vault.  With no *name_index* the
        return value is identical to the historical parent-relative behaviour
        (the step-1 path, whether or not it exists on disk) — and, crucially,
        that path does **zero** filesystem stats, since the only consumer of a
        non-existent step-1 path (the ``attachments`` metadata) wants it
        verbatim.  The ``is_file`` probe and the vault-wide fallback are gated
        on a supplied *name_index* precisely so they never run there.
        """
        # Step 1: resolve relative to the note's own folder.  ``parent_rel``
        # holds that path (empty if the target escapes the vault) and is the
        # default return value for every caller.
        parent_rel = ""
        try:
            base = md_path.parent.as_posix()
            joined = os.path.normpath(os.path.join(base, target))
            rel = os.path.relpath(joined, vault_root.as_posix())
        except ValueError:
            rel = ""
        if rel:
            rel = rel.replace(os.sep, "/")
            if not (rel == ".." or rel.startswith("../") or os.path.isabs(rel)):
                parent_rel = rel

        # Steps that touch the filesystem (the ``is_file`` probe) and the
        # vault-wide basename fallback run ONLY on the image-load path, which
        # is the one that passes a name_index (possibly empty).  The
        # ``attachments``-metadata path passes None and returns ``parent_rel``
        # verbatim — no stat, byte-identical to the pre-fix behaviour.
        if name_index is not None:
            # An explicit/beside-note file that actually exists wins outright,
            # mirroring Obsidian preferring the literal target before a search.
            if parent_rel and (vault_root / parent_rel).is_file():
                return parent_rel
            # Obsidian shortest-path fallback: a bare filename (no directory
            # component in the link) resolves by basename anywhere in the vault.
            if "/" not in target and os.sep not in target:
                candidates = name_index.get(nfc(os.path.basename(target)).lower())
                if candidates:
                    if len(candidates) == 1:
                        return candidates[0]
                    return self._pick_closest_attachment(
                        candidates, md_path, vault_root
                    )
        return parent_rel

    @staticmethod
    def _pick_closest_attachment(
        candidates: list[str], md_path: Path, vault_root: Path
    ) -> str:
        """Deterministically pick among same-basename files for an ambiguous
        bare wikilink: prefer the candidate sharing the longest directory
        prefix with the linking note (Obsidian-like locality), then fewest
        path segments, then lexicographic order."""
        # Note's own vault-relative directory.  Use the unresolved parent (as
        # the rest of the loader does — ``vault_root`` is already resolved
        # once), so symlinked subdirs are treated consistently across the two
        # resolution sites; a path that cannot be expressed below vault_root
        # falls back to "" (no locality preference, still deterministic).
        try:
            note_dir = md_path.parent.relative_to(vault_root).as_posix()
        except ValueError:
            note_dir = ""
        note_parts = [p for p in note_dir.split("/") if p]

        def shared_prefix(rel: str) -> int:
            # Compare directory components only: drop the trailing filename
            # segment (``[:-1]``) so a shared *folder* path — not a coincidental
            # basename match — drives locality.
            dir_parts = rel.split("/")[:-1]
            n = 0
            for a, b in zip(note_parts, dir_parts):
                if a != b:
                    break
                n += 1
            return n

        # Sort key: most shared dir segments first (negated for ascending
        # sort), then fewest directory levels (``count("/")`` = segment count,
        # favouring a top-level/central file), then lexicographic for a stable
        # tie-break.  ``sorted`` is stable so the result is fully deterministic.
        return sorted(
            candidates, key=lambda r: (-shared_prefix(r), r.count("/"), r)
        )[0]

    @staticmethod
    def _manifest_source(doc: LlamaDocument) -> str:
        """Raw manifest key for *doc* — the same field precedence
        ``_write_index_manifest`` uses.  Docs without source metadata land in
        the ``""`` bucket: they still count towards the totals that
        ``index_vault`` derives (``has_vector_data``, ``inserted_this_run``,
        the empty-vault diagnostic), while the manifest writer skips the
        bucket exactly as it previously skipped sourceless documents.
        """
        meta = doc.metadata or {}
        return str(meta.get("source") or meta.get("file_path") or "")

    def _embed_batch_size(self) -> int:
        """``vault_embed_batch_size``, clamped 1-256; garbage → the default.

        ≤1 selects the legacy per-chunk ``idx.insert`` path byte-for-byte
        (the escape hatch if a provider's batch endpoint ever misbehaves).
        """
        try:
            raw = load_config_readonly().get("vault_embed_batch_size")
            val = int(raw)  # type: ignore[arg-type]
        except Exception:
            return self._EMBED_BATCH_DEFAULT
        return max(1, min(val, 256))

    def _index_documents_streaming(
        self,
        idx,
        chunks_iter,
        persist_callback: Optional[Callable[[dict], None]] = None,
        lancedb_upsert: bool = False,
        vector_backend: str = VECTOR_BACKEND_SIMPLE,
        op_epoch: int = 0,
    ) -> tuple[int, int, int, int, dict[str, int]]:
        """Stream chunks into the index with per-insert fault tolerance and
        optional periodic persistence.

        Differs from the legacy _index_documents_incrementally:
          * Accepts an iterator instead of a pre-materialised list (P0.2).
          * Tolerates per-insert failures with a circuit breaker (P0.1):
            a single transient embedding error no longer kills the run,
            but _MAX_CONSECUTIVE_FAILURES failures in a row aborts.
          * Calls ``persist_callback`` once _PERSIST_EVERY successful inserts
            have accumulated AND _PERSIST_MIN_INTERVAL_S has elapsed since
            the previous checkpoint, bounding crash re-work to
            max(_PERSIST_EVERY chunks, that interval).

        Returns ``(added, skipped, deleted, failed, manifest_counts)`` where
        ``manifest_counts`` maps each chunk's raw source (see
        ``_manifest_source``) to the number of chunks currently indexed for
        it — accumulated on both the skip and insert paths, replacing the
        old list of full ``LlamaDocument`` objects that pinned a duplicate
        copy of every chunk's text for the whole run.
        """
        added = 0
        skipped = 0
        deleted = 0
        failed = 0
        consecutive_failures = 0
        pending_since_persist = 0
        pending_since_compact = 0
        # doc_ids whose stale copy was deleted but whose re-insert then failed in
        # the same run (delete-before-insert is not atomic).  Tracked so the gap
        # is surfaced specifically rather than hidden in the aggregate ``failed``.
        reinsert_failed: set[str] = set()
        manifest_counts: dict[str, int] = {}
        current_doc_ids: set[str] = set()

        docstore = getattr(idx, "docstore", None)
        # getattr returns Any at runtime but Pylance infers ``object`` — cast
        # them through Callable so subsequent .keys() / equality checks type.
        get_document_hash: Optional[Callable[[str], Any]] = getattr(docstore, "get_document_hash", None)
        get_all_ref_doc_info: Optional[Callable[[], dict]] = getattr(docstore, "get_all_ref_doc_info", None)
        can_increment = callable(get_document_hash) and callable(get_all_ref_doc_info)

        previous_doc_ids: set[str] = set()
        if can_increment and get_all_ref_doc_info is not None:
            try:
                previous_doc_ids = set(get_all_ref_doc_info().keys())
            except Exception:
                previous_doc_ids = set()
                can_increment = False

        last_emit_time = time.monotonic()
        last_persist_time = time.monotonic()

        # ── Track 5.6: batched embedding ────────────────────────────────────
        # DEFECT: every chunk was inserted alone (`idx.insert(doc)`), and each
        # insert embeds its node(s) in a dedicated provider HTTP round-trip —
        # one call per chunk for the whole vault (and, on lancedb, one
        # transaction per chunk, the very churn the compaction cadence exists
        # to clean up). FIX: chunks needing (re-)embedding are buffered and
        # flushed as ONE `insert_nodes` call per batch — its `embed_nodes`
        # hands the entire batch to `get_text_embedding_batch`, whose
        # sub-batching is sized to the same knob via the indexing embed
        # model's `embed_batch_size`, so a flush is one HTTP call.
        # REINDEX-NEUTRAL: chunk doc_ids and hashes are untouched, and the
        # batch path replicates `BaseIndex.insert()` exactly
        # (run_transformations → insert_nodes → set_document_hash, verified
        # against llama-index-core 0.14.22), so the docstore/vector rows are
        # what K individual inserts would have produced.
        # FAILURE SEMANTICS PRESERVED: a failed batch is retried chunk-by-
        # chunk on the ORIGINAL per-doc path (after an idempotency cleanup),
        # so the consecutive-failure breaker, interruptible backoff, and
        # reinsert_failed gap-tracking still count per chunk — one bad batch
        # costs one extra HTTP call, never K breaker strikes.
        # INVARIANT (pinned by TestEmbedBatchParity): a batched run produces
        # an identical docstore (node texts, doc hashes, added/skipped
        # counts) to a batch-size-1 run, with strictly fewer embed calls.
        # (doc, needs_lancedb_delete, deleted_old). LanceDB changed rows wait
        # here so the whole bounded flush can retire them in one predicate;
        # simple-store rows retain their immediate in-memory delete path.
        embed_batch: list = []
        # Cap at _PERSIST_EVERY so a batch can never straddle the checkpoint
        # cadence (the cadence gates are evaluated per flush, between batches).
        # For shipping config the cap never actually binds — _embed_batch_size()
        # is clamped to <=256 and _PERSIST_EVERY is 500 — so it exists ONLY to
        # keep the never-straddle invariant true for a test that patches
        # _PERSIST_EVERY down below the knob to force count-only checkpoints.
        batch_target = min(self._embed_batch_size(), self._PERSIST_EVERY)

        def _insert_docs_individually(batch_docs) -> bool:
            """Per-doc inserts with the original failure bookkeeping.

            Returns False when the run must abort (breaker fired, or a
            backoff sleep / stop check observed a cancel).
            """
            nonlocal added, failed, consecutive_failures
            nonlocal pending_since_persist, pending_since_compact

            def _record_unprocessed_gaps(start_idx: int) -> None:
                # Track 5.6 gap-report completeness: on an early abort
                # (stop/breaker/cancel) the docs from start_idx onward never got
                # (re-)inserted. Any of them whose OLD copy was already deleted at
                # buffer time (deleted_old) is a content gap until the next run
                # re-yields it — the tail-flush gap report (below) cannot catch
                # these because _flush_embed_batch already clear()ed the buffer,
                # so record them here to keep "reinsert_failed counts per chunk"
                # true. reinsert_failed is a set, so re-recording a doc already
                # counted in the except-branch above is a harmless no-op (which
                # is why every early return can safely pass its own idx).
                for g_doc, _g_needs_delete, g_deleted_old in batch_docs[start_idx:]:
                    if g_deleted_old and g_doc.doc_id:
                        reinsert_failed.add(g_doc.doc_id)

            for idx_in_batch, (b_doc, _b_needs_delete, b_deleted_old) in enumerate(batch_docs):
                if self._stop_event.is_set():
                    _record_unprocessed_gaps(idx_in_batch)
                    return False
                try:
                    with self._mutate_index():
                        idx.insert(b_doc)
                    source = self._manifest_source(b_doc)
                    manifest_counts[source] = manifest_counts.get(source, 0) + 1
                    added += 1
                    pending_since_persist += 1
                    pending_since_compact += 1
                    consecutive_failures = 0
                except Exception as exc:
                    failed += 1
                    consecutive_failures += 1
                    if b_deleted_old and b_doc.doc_id:
                        # Old copy already removed but the re-embed failed:
                        # this chunk is now absent until the next run
                        # re-yields it. Record the gap explicitly.
                        reinsert_failed.add(b_doc.doc_id)
                    logger.warning("Insert failed for %s: %s", b_doc.doc_id, exc)
                    if failed == 1 or failed % 5 == 0:
                        self._emit(
                            f"WARNING: insertion failure ({failed} total). "
                            f"Latest: {type(exc).__name__}: {exc}"
                        )
                    if consecutive_failures >= self._MAX_CONSECUTIVE_FAILURES:
                        self._emit(
                            f"ERROR: {self._MAX_CONSECUTIVE_FAILURES} consecutive insert failures — "
                            "embedding backend appears unavailable. Aborting indexing run; "
                            "partial work has been preserved."
                        )
                        self._stop_event.set()
                        _record_unprocessed_gaps(idx_in_batch)
                        return False
                    # Interruptible exponential backoff — identical to the
                    # historical per-doc loop (see the breaker notes at the
                    # class attrs): recovery window for a transiently-failing
                    # backend, cancel-aware via _stop_event.wait().
                    if consecutive_failures == 1 and failed > 1:
                        self._emit(
                            "WARNING: embedding insert failed — pausing briefly before "
                            "continuing (transient backend error; will abort if it persists)."
                        )
                    backoff_s = min(
                        self._FAILURE_BACKOFF_BASE_S * (2 ** (consecutive_failures - 1)),
                        self._FAILURE_BACKOFF_CAP_S,
                    )
                    if backoff_s > 0 and self._stop_event.wait(backoff_s):
                        _record_unprocessed_gaps(idx_in_batch)
                        return False
            return True

        def _flush_embed_batch() -> bool:
            """Insert the buffered chunks; True = keep going, False = abort."""
            nonlocal added, failed, consecutive_failures
            nonlocal pending_since_persist, pending_since_compact
            if not embed_batch:
                return True
            batch_docs = list(embed_batch)
            embed_batch.clear()
            changed_ids = [
                b_doc.doc_id for b_doc, needs_delete, _deleted_old in batch_docs
                if needs_delete and b_doc.doc_id
            ]
            if changed_ids:
                succeeded, delete_errors = self._delete_changed_lancedb_batch(
                    idx, changed_ids)
                ready = []
                for b_doc, needs_delete, deleted_old in batch_docs:
                    if not needs_delete:
                        ready.append((b_doc, False, deleted_old))
                        continue
                    if b_doc.doc_id in succeeded:
                        ready.append((b_doc, False, True))
                        continue
                    failed += 1
                    exc = delete_errors.get(b_doc.doc_id)
                    self._emit(
                        f"WARNING: could not replace changed chunk "
                        f"{b_doc.doc_id}: old-copy delete failed ({exc or 'unknown error'}); "
                        "keeping the previous version this run — it will be "
                        "retried on the next indexing run."
                    )
                batch_docs = ready
                if not batch_docs:
                    return True
            # One long embed call follows — keep the op-lock TTL fed.
            self._heartbeat_or_stop(op_epoch)
            if len(batch_docs) == 1 or batch_target <= 1:
                return _insert_docs_individually(batch_docs)
            # Stage 1 — NO writes: run the same transformation pipeline
            # insert() would, outside the mutation lock (pure CPU; the legacy
            # path parsed inside the lock as part of insert()). A failure here
            # (including a test double lacking _transformations) needs no
            # cleanup — nothing was touched — so it degrades straight to the
            # per-doc path.
            try:
                nodes = run_transformations(
                    [b_doc for b_doc, _needs_delete, _deleted_old in batch_docs],
                    idx._transformations,
                )
            except Exception as exc:
                logger.warning(
                    "Batch transformation of %d chunk(s) failed (%s: %s); "
                    "inserting individually.",
                    len(batch_docs), type(exc).__name__, exc,
                )
                return _insert_docs_individually(batch_docs)
            # Stage 2 — the store mutation + hash records, under the lock,
            # exactly like K sequential insert() calls would be.
            try:
                with self._mutate_index():
                    idx.insert_nodes(nodes)
                    for b_doc, _needs_delete, _deleted_old in batch_docs:
                        idx.docstore.set_document_hash(b_doc.id_, b_doc.hash)
            except Exception as exc:
                logger.warning(
                    "Batched insert of %d chunk(s) failed (%s: %s); retrying individually.",
                    len(batch_docs), type(exc).__name__, exc,
                )
                # Idempotency cleanup before the per-doc retry: an embed HTTP
                # failure (the overwhelmingly likely case) raises before any
                # store write, but a mid-add failure is indistinguishable from
                # outside — delete whatever partially landed so the retry can
                # never duplicate nodes. delete_ref_doc on a never-inserted
                # doc raises internally; ignored.
                for b_doc, _needs_delete, _deleted_old in batch_docs:
                    try:
                        with self._mutate_index():
                            idx.delete_ref_doc(b_doc.doc_id, delete_from_docstore=True)
                    except Exception:
                        pass
                return _insert_docs_individually(batch_docs)
            for b_doc, _needs_delete, _deleted_old in batch_docs:
                source = self._manifest_source(b_doc)
                manifest_counts[source] = manifest_counts.get(source, 0) + 1
            added += len(batch_docs)
            pending_since_persist += len(batch_docs)
            pending_since_compact += len(batch_docs)
            consecutive_failures = 0
            return True

        def _maybe_checkpoint_and_compact() -> None:
            """The historical post-insert cadence blocks, now run per flush."""
            nonlocal pending_since_persist, pending_since_compact, last_persist_time
            if (
                persist_callback is not None
                and pending_since_persist >= self._PERSIST_EVERY
                and time.monotonic() - last_persist_time >= self._PERSIST_MIN_INTERVAL_S
            ):
                try:
                    persist_callback(manifest_counts)
                    self._emit(
                        f"Checkpoint saved: {added} embedded, {skipped} unchanged."
                    )
                    pending_since_persist = 0
                    # The checkpoint persist already compacted the lancedb table
                    # (_persist_index_checkpoint), so reset the compaction gate too.
                    pending_since_compact = 0
                    last_persist_time = time.monotonic()
                except Exception as exc:
                    self._emit(f"WARNING: mid-run checkpoint failed: {exc}")
                    # Reset both gates so we don't spin: wait another full
                    # _PERSIST_EVERY inserts AND _PERSIST_MIN_INTERVAL_S before
                    # the next attempt.
                    pending_since_persist = 0
                    last_persist_time = time.monotonic()

            # Interim lancedb compaction — independent of the JSON checkpoint so a
            # fast embedder cannot accumulate a large O(n²) version-manifest spike
            # between the (≥10-min) checkpoints. Batching already cut the
            # transaction count ~K-fold; this bounds what remains.
            if (
                vector_backend == VECTOR_BACKEND_LANCEDB
                and pending_since_compact >= self._LANCEDB_COMPACT_EVERY
            ):
                # PERF (2026-09-02): compaction ran under the mutation lock with
                # no bound, no heartbeat and no status line — the 120 s
                # retrieval-acquire rationale is argued for CHECKPOINTS only, so
                # a long compaction stalled chat while reading as a hang, and a
                # lapsed op-lock TTL went unnoticed. A stage line and a
                # `_heartbeat_or_stop` after it fix both; compaction stays
                # best-effort and otherwise untouched, and `op_epoch == 0` makes
                # the heartbeat inert for direct/test callers.
                # Pinned by test_vault_regressions.py::TestLongPhasesHeartbeat
                vector_store = self._lancedb_vector_store_of(idx)
                self._emit("Compacting the vector store…")
                with self._index_mutation_lock:
                    compact_lancedb_vector_store(vector_store)
                self._heartbeat_or_stop(op_epoch)
                pending_since_compact = 0

        for i, doc in enumerate(chunks_iter):
            if self._stop_event.is_set():
                break
            if i % 10 == 0:
                self._heartbeat_or_stop(op_epoch)

            now = time.monotonic()
            if now - last_emit_time >= 5.0:
                verb = "Scanning" if can_increment else "Indexing"
                msg = f"{verb}… {added} embedded, {skipped} unchanged"
                if failed:
                    msg += f", {failed} failed"
                self._emit(msg)
                last_emit_time = now

            if doc.doc_id:
                current_doc_ids.add(doc.doc_id)

            # Set when this chunk's old copy is deleted below, so an insert
            # failure that follows a successful delete can be recorded as a
            # content gap (delete-before-insert is not atomic).
            deleted_old = False
            needs_lancedb_delete = False
            if can_increment and doc.doc_id and get_document_hash is not None:
                # The hash lookup and the old-copy delete were previously one
                # bare try/except-pass (2026-07-22 audit A6). That swallowed a
                # FAILED delete and re-inserted anyway: new node UUIDs landed in
                # the vector table while the old rows survived under the same
                # doc_id — permanent duplicates the drift check can never
                # reclaim (the doc_id stays live), with stale and fresh chunk
                # text competing in retrieval. The two operations now fail
                # differently: a hash-lookup failure degrades to "treat as
                # changed" (old behaviour, harmless), a delete failure SKIPS
                # the re-insert — the old chunk stays one run stale and the
                # unchanged hash makes the next run retry the delete.
                try:
                    doc_hash_unchanged = get_document_hash(doc.doc_id) == doc.hash
                except Exception:
                    doc_hash_unchanged = False
                if doc_hash_unchanged:
                    source = self._manifest_source(doc)
                    manifest_counts[source] = manifest_counts.get(source, 0) + 1
                    skipped += 1
                    continue
                # Changed chunk → drop the old copy before re-inserting.
                # ``lancedb_upsert`` additionally clears the *new*-chunk path:
                # after a hard crash the lancedb table can hold rows whose
                # docstore entry was lost before the last checkpoint, so a
                # plain re-insert would duplicate the row (the binary store
                # is durable-on-insert; the JSON docstore is not). A
                # delete-by-doc_id first makes the re-insert idempotent. The
                # flag is set only when a row-count drift is detected at load
                # (rare), so normal runs and fresh builds pay nothing.
                if doc.doc_id in previous_doc_ids or lancedb_upsert:
                    if vector_backend == VECTOR_BACKEND_LANCEDB:
                        needs_lancedb_delete = True
                    else:
                        try:
                            with self._mutate_index():
                                idx.delete_ref_doc(doc.doc_id, delete_from_docstore=True)
                            deleted_old = True
                        except Exception as exc:
                            failed += 1
                            self._emit(
                                f"WARNING: could not replace changed chunk "
                                f"{doc.doc_id}: old-copy delete failed ({exc}); "
                                "keeping the previous version this run — it will "
                                "be retried on the next indexing run."
                            )
                            continue

            # Track 5.6: buffer for the next batched flush (all insert
            # bookkeeping — counters, breaker, backoff — lives in the flush
            # helpers above). deleted_old rides with the doc so a failed
            # re-embed of a changed chunk is still reported as a gap.
            embed_batch.append((doc, needs_lancedb_delete, deleted_old))
            if len(embed_batch) >= batch_target:
                if not _flush_embed_batch():
                    break
                _maybe_checkpoint_and_compact()

        # Tail flush: the stream ended (or a stop was requested) with a
        # partial batch buffered.
        if embed_batch and not self._stop_event.is_set():
            if _flush_embed_batch():
                _maybe_checkpoint_and_compact()
        if embed_batch:
            # Cancelled with chunks still buffered: any whose OLD copy was
            # already deleted at buffer time is a content gap until the next
            # run re-yields it — surface it exactly like a failed re-insert
            # (the legacy path had no such window because delete and insert
            # were adjacent; the buffer opens one, so it must be reported).
            for pending_doc, _pending_needs_delete, pending_deleted_old in embed_batch:
                if pending_deleted_old and pending_doc.doc_id:
                    reinsert_failed.add(pending_doc.doc_id)

        if (
            can_increment
            and not self._stop_event.is_set()
            and not current_doc_ids
            and previous_doc_ids
        ):
            # Empty-scan guard (2026-07-20 audit H2): this run saw ZERO current
            # documents while the index holds some, so "previous - current"
            # would sweep the ENTIRE index. An empty scan is overwhelmingly a
            # scan failure (over-broad vault_exclude_dirs, permissions, cloud
            # eviction) rather than a genuinely emptied vault — and on lancedb
            # the row deletes are durable immediately, so a wrong sweep costs a
            # full re-embed. Refuse; a truly emptied vault is handled by Reset
            # or a fresh (non-incremental) rebuild.
            self._emit(
                f"ERROR: this run found no indexable files while the index "
                f"holds {len(previous_doc_ids)} document(s); refusing the "
                "stale sweep to protect the index. Check vault exclusions/"
                "permissions, or use Reset if the vault really is empty."
            )
        elif can_increment and not self._stop_event.is_set():
            # Stale-sweep protection (improvement plan 2026-07-04, item 2.8b).
            # "previous - current" conflates two very different absences: a
            # file DELETED from the vault (sweep it) and a file whose READ
            # failed this run — an iCloud dataless placeholder, a transient
            # I/O error (keep it!). The loader records read/extract failures
            # in _scan_failed_sources; chunks whose doc_id prefix ("{rel}::")
            # matches a failed source are withheld from the sweep, so a blip
            # costs one run of staleness instead of a silent de-index that
            # only a full re-embed would repair. Safe: withheld ids are
            # re-examined next run (the sweep is re-derived from scratch);
            # genuinely deleted files still sweep normally. Invariant (pinned
            # by TestStaleSweepSparesScanFailures): a source that failed to
            # read is never deleted by the sweep of the same run.
            failed_sources = self._scan_failed_sources
            # Finding 4: also withhold docs under an unreadable SUBTREE. An
            # unscannable folder yields none of its files, so without this every
            # doc beneath it would fall into `previous - current` and be swept —
            # a durable partial purge whenever an unrelated file kept the scan
            # non-empty (so the root empty-scan guard above never fired).
            failed_prefixes = self._scan_failed_prefixes
            # A5 (2026-07-22 audit): images are note-driven — they enter
            # `current_doc_ids` only via a readable referencing note. If ANY
            # note failed to read this run, an image referenced only by that
            # note looks deleted; withholding every image-extension doc_id for
            # the run turns that wrongful durable delete into one run of
            # staleness for genuinely deleted images (they sweep on the next
            # clean run). See the __init__ comment on `_scan_image_exts`.
            protect_images = self._scan_md_read_failed and self._scan_image_exts
            image_exts = self._scan_image_exts
            candidate_ids = previous_doc_ids - current_doc_ids
            if failed_sources or failed_prefixes or protect_images:
                def _withheld(doc_id: str) -> bool:
                    source = doc_id.split("::", 1)[0]
                    if source in failed_sources:
                        return True
                    if protect_images and Path(source).suffix.lower() in image_exts:
                        return True
                    return any(source.startswith(p) for p in failed_prefixes)

                stale_doc_ids = {d for d in candidate_ids if not _withheld(d)}
                withheld = len(candidate_ids) - len(stale_doc_ids)
                if withheld:
                    n_reasons = len(failed_sources) + len(failed_prefixes)
                    self._emit(
                        f"Note: {withheld} chunk(s) from {n_reasons} "
                        f"unreadable file(s)/folder(s) were kept in the index "
                        f"(read failure ≠ deletion"
                        + ("; image chunks kept because a referencing note "
                           "was unreadable" if protect_images else "")
                        + "); they will resync on the next run."
                    )
            else:
                stale_doc_ids = candidate_ids
            deleted += self._sweep_stale_doc_ids(
                idx,
                sorted(stale_doc_ids),
                vector_backend=vector_backend,
                op_epoch=op_epoch,
            )
        elif can_increment and self._stop_event.is_set():
            stale_count = len(previous_doc_ids - current_doc_ids)
            if stale_count:
                self._emit(
                    f"Note: {stale_count} stale document(s) (files deleted from the vault) "
                    "were not removed because indexing was interrupted. "
                    "They will be cleaned up automatically when a full run completes."
                )

        if reinsert_failed:
            self._emit(
                f"WARNING: {len(reinsert_failed)} changed chunk(s) were removed but "
                "could not be re-embedded this run (embedding backend error); they "
                "will be restored automatically on the next indexing run."
            )

        return added, skipped, deleted, failed, manifest_counts

    def _load_vault_documents(self, vault_path: str, op_epoch: int = 0,
                              online_extraction: bool = False,
                              extraction_manifest: dict | None = None):
        """Yield one ``LlamaDocument`` at a time, streaming through the vault.

        Yield order (preserved from the previous list-based loader): all
        MD docs in sorted scan order first, then sorted referenced image
        descriptions, then PDFs in sorted scan order.  This ordering is
        important: chunk IDs are scoped per document (`{rel_path}::...`)
        so cross-document ordering does not affect ID stability, but a
        stable order keeps progress emits and the manifest deterministic.

        The previous implementation returned ``List[LlamaDocument]``,
        which pre-materialised every extracted PDF (potentially hundreds
        of MB of concatenated text from textbook-sized PDFs) before the
        chunker saw the first byte.  Streaming bounds peak memory at one
        document plus the chunker / indexer backlog.

        The PDF signature cache write moves into a ``finally`` block so
        it runs whether iteration completes, is short-circuited by
        ``_stop_event``, or is closed by the consumer (e.g. a test that
        consumes only the first N yields).
        """
        cfg = load_config()
        # Per-run reset (2.8b): this generator runs once per indexing run on
        # the single indexer thread; the sweep reads the set after iteration.
        self._scan_failed_sources = set((extraction_manifest or {}).get("deferred", []))
        def admitted_binary(rel, path):
            if extraction_manifest is None:
                return True
            approved = extraction_manifest.get("approved", {})
            digest = approved.get(rel)
            try:
                current = self._pdf_file_signature(path, prev_sig_cache, rel)["sha256"] if digest else None
            except OSError:
                current = None
            if not digest or current != digest:
                self._scan_failed_sources.add(rel)
                self._emit(f"Deferred extraction: {rel}; prior chunks retained if present.")
                return False
            return True
        self._scan_failed_prefixes = set()
        self._scan_image_exts = set()
        self._scan_md_read_failed = False
        vault_root = Path(vault_path).resolve()
        user_excluded_dirs = self._normalised_excluded_dirs(vault_root, cfg.get("vault_exclude_dirs", []))
        raw_image_exts = cfg.get("vault_image_exts")
        if not isinstance(raw_image_exts, list):
            raw_image_exts = list(VAULT_IMAGE_EXTS)
        # An empty list disables image indexing.  Reserved markdown/PDF
        # extensions are stripped so a hand-edited config can never reroute
        # core vault files through the vision branch.
        configured_image_exts = (
            frozenset(raw_image_exts) - VAULT_MD_EXTS - VAULT_BINARY_EXTS
        )
        # Recorded for the stale sweep's image-protection rule (A5, see
        # __init__): the sweep classifies a doc_id as an image by extension.
        self._scan_image_exts = set(configured_image_exts)
        # Images reach the indexer only through the MD-attachment branch, so
        # the outer scan filter intentionally excludes image extensions.
        allowed_exts = VAULT_MD_EXTS | VAULT_BINARY_EXTS
        self._skipped_image_count = 0
        self._vision_unavailable_warned = False
        # P1.5: previous-run signature cache lets us skip re-hashing large PDFs
        # when their size and mtime are unchanged.  Updated below as we go.
        prev_sig_cache = self._load_pdf_signature_cache()
        new_sig_cache: dict = {}
        # Set to True only after the PDF loop runs to completion.  Any other
        # exit path — stop event, exception bubbling up, generator closed by
        # the consumer before exhaustion — leaves this False, which tells the
        # ``finally`` block to merge forward prior-run signatures so unvisited
        # PDFs are not silently evicted from the on-disk cache.
        scan_completed = False

        def _should_skip_path(p: Path) -> bool:
            if any(part in OBSIDIAN_EXCLUDED_DIR_NAMES for part in p.parts):
                return True
            try:
                rel = p.resolve().relative_to(vault_root).as_posix()
            except ValueError:
                return True
            for excluded in user_excluded_dirs:
                if rel == excluded or rel.startswith(excluded + "/"):
                    return True
            return False

        # Hard page cap for vault PDF extraction (shared with the single-paper
        # upload worker).  Since big PDFs are now yielded as one document per
        # 1000-page range, peak RAM no longer scales with file size — this cap
        # only bounds total extraction *time* for pathological files.  It was
        # 5000 when the loader concatenated all ranges into a single in-memory
        # document.
        _VAULT_PDF_MAX_PAGES = PDF_MAX_PAGES
        # extraction call page chunk — matches extract_structured_from_pdf limit.
        _VAULT_PDF_CHUNK = EXTRACT_MAX_PAGES_PER_CALL

        try:
            # First pass: walk the vault once and stream MD docs as they are
            # read.  PDF paths and referenced images are collected during the
            # walk because (1) image-yield order depends on sorting the full
            # attachment set, and (2) the PDF extraction step is expensive
            # enough that we want all MD/image yields to drain through the
            # chunker before we start heavy extraction work.
            md_paths_buffered: list[tuple[Path, str]] = []
            pdf_paths: list[tuple[Path, str]] = []
            referenced_image_paths: dict[str, Path] = {}
            # NFC-normalized forms already recorded (2026-07-22 audit P-NFC):
            # a path-ful wikilink carries the NFC rel as typed in the note,
            # while a bare-name lookup returns the on-disk (often NFD) rel —
            # APFS resolves both, so one physical image indexed twice under
            # two rels (duplicate chunks, divergent `source`). First form
            # seen wins (deterministic: notes stream in sorted order).
            referenced_image_nfc: set[str] = set()
            # Obsidian resolves a bare wikilink (![[image.png]]) by searching
            # the whole vault for that basename ("shortest path"), not relative
            # to the linking note — so vaults that keep attachments in a central
            # folder reference images that live nowhere near the note.  Build a
            # basename -> [vault-relative path] index for the vault's image
            # files during this single walk; _extract_md_attachments consults it
            # to resolve those bare links.  Built from non-excluded images only
            # (the same _should_skip_path guard the per-image load applies), so a
            # bare name never resolves to a file we would then skip.  This map is
            # fully populated before the first MD doc is yielded below, which is
            # what makes it available when the MD-attachment loop runs.
            name_index: dict[str, list[str]] = {}

            # Pruned single-pass walk (Track 5.4): excluded dirs are never
            # descended and no per-file resolve() runs — see
            # _iter_vault_scan_files for the equivalence argument. Buckets are
            # sorted below, reproducing the old sorted(rglob) per-type order.
            image_entries: list[tuple[Path, str]] = []
            try:
                for scan_index, (path, rel) in enumerate(
                    _iter_vault_scan_files(
                        vault_root, user_excluded_dirs,
                        failed_prefixes=self._scan_failed_prefixes,
                    )
                ):
                    if self._stop_event.is_set():
                        break
                    if scan_index % 25 == 0:
                        self._heartbeat_or_stop(op_epoch)
                    ext = path.suffix.lower()
                    if ext in configured_image_exts:
                        # Images enter the pipeline only via the MD-attachment
                        # branch, so they are indexed for name resolution but not
                        # buffered as standalone source documents.
                        image_entries.append((path, rel))
                        continue
                    if ext not in allowed_exts:
                        continue
                    if ext in VAULT_MD_EXTS:
                        md_paths_buffered.append((path, rel))
                    elif ext in VAULT_BINARY_EXTS:
                        pdf_paths.append((path, rel))
            except OSError as exc:
                # The vault ROOT could not be scanned (permissions revoked,
                # cloud eviction, unmounted volume). Yielding nothing here
                # would read as "vault is empty" and an incremental run would
                # sweep every existing chunk as stale — a full-index purge from
                # one transient blip (2026-07-20 audit H2). Abort the run with
                # the stop event so the sweep (gated on it) never fires.
                self._emit(
                    f"ERROR: Could not scan the vault root ({exc}); aborting "
                    "this run to protect the existing index."
                )
                self._stop_event.set()
                return

            # Deterministic order parity with the old globally-sorted walk:
            # sorting each bucket by rel equals the old full-path sort order
            # (shared root prefix), so MD/PDF yield order and name_index bucket
            # order are byte-identical to the rglob era.
            md_paths_buffered.sort(key=lambda pr: pr[1])
            pdf_paths.sort(key=lambda pr: pr[1])
            image_entries.sort(key=lambda pr: pr[1])
            for path, rel in image_entries:
                # B2 (2026-07-13): NFC-normalize the basename key so an NFD
                # on-disk filename matches an NFC link target (and vice-versa);
                # the stored value stays the original rel path.
                name_index.setdefault(nfc(path.name).lower(), []).append(rel)

            # Stream MD docs.  Reading and yielding interleaved so each
            # file's bytes can be freed once the chunker consumes its chunks
            # — important on vaults with very large markdown files.
            for path, rel in md_paths_buffered:
                if self._stop_event.is_set():
                    break
                ext = path.suffix.lower()
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    self._emit(f"WARNING: Failed to read {rel}: {exc}")
                    # 2.8b: a read failure is NOT a deletion — keep the file's
                    # indexed chunks out of this run's stale sweep.
                    self._scan_failed_sources.add(rel)
                    # A5 (2026-07-22): this note's referenced images were never
                    # discovered (discovery = parsing the note), so the sweep
                    # must also spare image doc_ids this run — see __init__.
                    self._scan_md_read_failed = True
                    continue
                for attachment in self._extract_md_attachments(
                    text, path, vault_root, name_index=name_index
                ):
                    image_ext = Path(attachment).suffix.lower()
                    if image_ext not in configured_image_exts:
                        continue
                    image_path = vault_root / attachment
                    if _should_skip_path(image_path) or not image_path.is_file():
                        continue
                    # Dedup across NFC/NFD forms of the SAME file (see the
                    # referenced_image_nfc comment at its definition).
                    nfc_form = nfc(attachment)
                    if attachment not in referenced_image_paths \
                            and nfc_form in referenced_image_nfc:
                        continue
                    referenced_image_nfc.add(nfc_form)
                    referenced_image_paths.setdefault(attachment, image_path)
                yield LlamaDocument(
                    text=text,
                    metadata={"file_path": str(path), "source": rel, "extension": ext},
                )

            # Stream image descriptions.  Vision call is per-image and the
            # description is small, so back-pressure from the chunker is
            # rarely a concern here; the streaming win is mostly about not
            # holding hundreds of MD documents alongside.
            if configured_image_exts and referenced_image_paths:
                self._emit(f"Found {len(referenced_image_paths)} referenced image file(s).")
            for image_index, (rel, path) in enumerate(sorted(referenced_image_paths.items())):
                if not admitted_binary(rel, path):
                    continue
                if self._stop_event.is_set():
                    break
                if image_index % 10 == 0:
                    self._heartbeat_or_stop(op_epoch)
                try:
                    new_sig_cache[rel] = self._pdf_file_signature(path, prev_sig_cache, rel)
                    text = self._extract_image_description(
                        path, vault_root, rel, cache_only=online_extraction)
                except Exception as exc:
                    self._emit(f"WARNING: Vision indexing failed for {rel}: {exc}")
                    self._skipped_image_count += 1
                    # 2.8b: a vision hiccup must not delete the image's
                    # previously-indexed description chunks.
                    self._scan_failed_sources.add(rel)
                    continue
                if text:
                    yield LlamaDocument(
                        text=text,
                        metadata={
                            "file_path": str(path),
                            "source": rel,
                            "extension": path.suffix.lower(),
                            "is_image": True,
                        },
                    )
                else:
                    # A4 (2026-07-22 audit): an empty description is ALSO the
                    # failure sentinel — describe_image "returns '' — never
                    # raises", and the vision_failure_cooldown_s window
                    # short-circuits to '' too. Only the RAISING path above
                    # recorded the rel, so a vision outage let the stale sweep
                    # durably delete this image's previously-indexed chunks.
                    # Protect it like any other extract failure: for a
                    # never-indexed image this is a no-op (no previous doc_ids
                    # carry its prefix), so unconditional is safe.
                    self._scan_failed_sources.add(rel)
                    # Empty description. For a format a raster vision model simply
                    # cannot read (SVG is XML; ``.img`` is not a real image),
                    # name the cause so a second user is not left wondering why a
                    # referenced figure never indexed — vs. the generic
                    # vision-model-unavailable case handled elsewhere.
                    if path.suffix.lower() in (".svg", ".img"):
                        self._emit(
                            f"WARNING: Skipped {rel}: '{path.suffix.lower()}' is not "
                            f"a raster image a vision model can read — convert it to "
                            f"PNG/JPEG to index it."
                        )
                    self._skipped_image_count += 1

            # Stream PDFs.  This is where the memory win is largest: a
            # 1000-page textbook can produce 5-50 MB of extracted text, and
            # holding the full set in a list before chunking was the original
            # peak.  With streaming, each PDF is freed as soon as the
            # chunker iterates past its yielded document.
            for pdf_index, (path, rel) in enumerate(pdf_paths):
                if not admitted_binary(rel, path):
                    continue
                if self._stop_event.is_set():
                    break
                if pdf_index % 10 == 0:
                    self._heartbeat_or_stop(op_epoch)
                ext = path.suffix.lower()
                if ext not in VAULT_BINARY_EXTS:
                    continue
                try:
                    signature = self._pdf_file_signature(path, prev_sig_cache, rel)
                    new_sig_cache[rel] = signature
                    cache_file = self._pdf_cache_file(vault_root, signature)
                    # Whole-file cache lookup covers two populations: PDFs small
                    # enough for a single extraction call, and large PDFs that a
                    # pre-per-range version of this loader extracted and cached
                    # as one concatenated text.  The latter are GRANDFATHERED as
                    # a single document on purpose: re-splitting them would
                    # change their chunk hashes and silently trigger an
                    # hours-long re-extraction + re-embed on the user's next
                    # index run.  Deleting the cache file opts a PDF into
                    # per-range documents.
                    selected = self._selected_pdf_payload(path, vault_root)
                    text = (selected["text"] if selected is not None else
                            self._read_first_text_file(
                                [cache_file, self._legacy_pdf_cache_file(vault_root, signature)]))
                    cache_backed = selected is None and bool(text)
                    if selected is not None and selected["outcome"] == "partial":
                        self._scan_failed_sources.add(rel)
                        self._emit(f"WARNING: {rel} has partial selected text; keeping existing indexed chunks.")
                        continue
                    if not text and extraction_manifest is not None and not extraction_manifest.get("allow_local_extraction"):
                        from core.extraction_cache import cache_coverage
                        coverage = cache_coverage((OBSIDIAN_CACHE_DIR, OBSIDIAN_INDEX_DIR),
                                                  "pdf_cache", self._vault_cache_key(vault_root),
                                                  signature["sha256"],
                                                  page_count=lambda: get_pdf_page_count(str(path)))
                        if coverage != "ranges_unverified":
                            self._scan_failed_sources.add(rel)
                            self._emit(f"Deferred {rel}: approved cache unavailable; no fresh parsing permitted.")
                            continue
                        # Cache-only mode must never reach the whole-PDF parser,
                        # even for short PDFs represented by range caches.
                        page_count = get_pdf_page_count(str(path))
                        if page_count > _VAULT_PDF_MAX_PAGES:
                            self._scan_failed_sources.add(rel)
                            self._emit(f"Deferred {rel}: PDF exceeds the vault page limit.")
                            continue
                        yield from self._load_pdf_range_documents(
                            path, rel, ext, vault_root, signature, page_count,
                            op_epoch=op_epoch, online_extraction=True, cache_only=True)
                        continue
                    if not text:
                        page_count = get_pdf_page_count(str(path))
                        if page_count > _VAULT_PDF_MAX_PAGES:
                            self._emit(
                                f"WARNING: Skipping {rel}: {page_count} pages exceeds "
                                f"the {_VAULT_PDF_MAX_PAGES}-page vault limit."
                            )
                        elif page_count <= _VAULT_PDF_CHUNK:
                            # Normal case — extract in one call.  For a text-layer
                            # PDF (the common case) OCR is never invoked, so text
                            # stays byte-identical to the pre-per-range loader and
                            # chunk hashes are unchanged.  ``ocr_max_pages`` only
                            # affects a SCANNED PDF: it lets the OCR fallback cover
                            # the whole doc (up to _VAULT_PDF_CHUNK) instead of
                            # silently stopping at 100 pages; the per-page heartbeat
                            # keeps the op-lock TTL alive on a long scan.
                            sections = extract_structured_from_pdf(
                                str(path),
                                ocr_cb=(None if online_extraction else
                                        glm_ocr_manager.extract_page_text),
                                ocr_max_pages=_VAULT_PDF_CHUNK,
                                page_done_cb=lambda _n: self._heartbeat_or_stop(op_epoch),
                                cancel_cb=self._stop_event.is_set,
                            )
                            text = sections.full_text
                            if self._pdf_extraction_incomplete(sections):
                                # Known extraction failure or limit: surface it
                                # and DO NOT cache the partial as complete, so the
                                # next run retries instead of baking in the loss.
                                self._emit(
                                    f"WARNING: {rel} was only partially extracted "
                                    "(known extraction failure or limit); "
                                    "not cached or indexed so it will retry next run."
                                )
                                self._scan_failed_sources.add(rel)
                                continue
                            elif text:
                                cache_backed = self._save_pdf_cache_file(cache_file, text)
                        else:
                            # Large PDF: one document per 1000-page range, each
                            # extracted and cached independently — see the
                            # helper for the full rationale.
                            yield from self._load_pdf_range_documents(
                                path, rel, ext, vault_root, signature, page_count,
                                op_epoch=op_epoch, online_extraction=online_extraction,
                                cache_only=extraction_manifest is not None and not extraction_manifest.get("allow_local_extraction"),
                            )
                            continue
                    if text:
                        if cache_backed:
                            self._export_readable_pdf(path, vault_root, signature, text)
                        yield LlamaDocument(
                            text=text,
                            metadata={"file_path": str(path), "source": rel, "extension": ext},
                        )
                    else:
                        # An empty read is not evidence that previously indexed
                        # text disappeared from an unchanged source.
                        self._scan_failed_sources.add(rel)
                except Exception as exc:
                    self._emit(f"WARNING: Failed to extract {rel}: {exc}")
                    # 2.8b: extraction failure ≠ deletion — protect its chunks.
                    self._scan_failed_sources.add(rel)
            # The PDF for-loop exited via its normal clause (no exception,
            # no early consumer close).  This still doesn't mean every PDF
            # was visited — a stop event raised mid-run causes inner loops
            # to break early — so the finally checks both conditions.
            scan_completed = True
        finally:
            # Persist the updated signature cache so the next run can fast-path
            # unchanged PDFs.  Best-effort — failure here only slows the next run.
            # Merge forward prior-run signatures whenever this run was not
            # authoritative:
            #   * exception or generator close before the PDF loop returned
            #     → ``scan_completed`` is still False;
            #   * stop event raised mid-run (cancel / pause) → the outer
            #     rglob scan and the inner MD/image/PDF loops break early,
            #     so we may not have visited every PDF on disk.
            # On a clean, full run, omitting unvisited keys correctly evicts
            # signatures for files actually deleted from the vault.
            if not scan_completed or self._stop_event.is_set():
                for rel, sig in prev_sig_cache.items():
                    new_sig_cache.setdefault(rel, sig)
            if new_sig_cache:
                self._save_pdf_signature_cache(new_sig_cache)

    def _load_pdf_range_documents(
        self,
        path: Path,
        rel: str,
        ext: str,
        vault_root: Path,
        signature: dict,
        page_count: int,
        op_epoch: int = 0,
        online_extraction: bool = False,
        cache_only: bool = False,
    ):
        """Yield one ``LlamaDocument`` per 1000-page range of a large PDF.

        Replaces the old concatenate-all-ranges approach for PDFs above
        ``EXTRACT_MAX_PAGES_PER_CALL`` pages.  Per-range documents buy three
        things the single concatenated document could not:

        * Peak RAM is bounded by ~1000 pages of text (plus the chunker's
          backlog) regardless of file size — the old path materialised the
          entire textbook, making the largest PDF the indexing run's
          high-water mark.
        * Each range is cached the moment it is extracted
          (``_pdf_range_cache_file``), so a cancel or crash at page 3900 of
          4000 keeps the first three ranges; the old path only cached after
          the full loop completed, discarding hours of extraction work.
        * Ranges already cached are skipped on resume, so an interrupted
          textbook continues from its last completed range.

        Chunk-ID compatibility: documents carry ``page_start`` metadata,
        which ``_chunk_raw_documents`` mixes into the chunk hash.  The
        ``{rel}::`` doc-id prefix (used by retrieval joins and the manifest)
        is unchanged, so only the per-range chunk hashes differ from the old
        concatenated form — i.e. a previously indexed multi-range PDF
        re-embeds once, while every other file in the vault is untouched.

        Empty ranges (e.g. scanned pages beyond the OCR cap) yield nothing
        and are not cached, so they are retried on the next run.
        """
        # Stage complete ranges in their existing caches before yielding any
        # document. A later failed range must not replace earlier indexed chunks.
        ready_ranges: list[tuple[int, int, Path]] = []
        for start in range(0, page_count, EXTRACT_MAX_PAGES_PER_CALL):
            if self._stop_event.is_set():
                self._scan_failed_sources.add(rel)
                return
            end = min(start + EXTRACT_MAX_PAGES_PER_CALL, page_count)
            range_cache = self._pdf_range_cache_file(vault_root, signature, start, end)
            text = self._read_first_text_file([range_cache])
            if not text and cache_only:
                self._scan_failed_sources.add(rel)
                self._emit(f"Deferred {rel}: approved range cache unavailable.")
                return
            if not text:
                # P1.6: refresh the operation-lock heartbeat between ranges so
                # multi-hour extractions of textbook PDFs don't let the TTL
                # expire while we are mid-file.
                self._heartbeat_or_stop(op_epoch)
                self._emit(
                    f"Extracting {rel} (pages {start + 1}-{end} of {page_count})…"
                )
                sections = extract_structured_from_pdf(
                    str(path),
                    start_page=start,
                    end_page=end,
                    ocr_cb=(None if online_extraction else
                            glm_ocr_manager.extract_page_text),
                    # OCR the WHOLE range (a scanned range used to stop at 100 of
                    # up to 1000 pages and cache that partial as complete).  The
                    # per-page heartbeat keeps the op-lock TTL alive on a long scan.
                    ocr_max_pages=EXTRACT_MAX_PAGES_PER_CALL,
                    page_done_cb=lambda _n: self._heartbeat_or_stop(op_epoch),
                    cancel_cb=self._stop_event.is_set,
                )
                text = sections.full_text
                if self._pdf_extraction_incomplete(sections):
                    # Incomplete OCR coverage of this range: warn and DO NOT cache,
                    # so the range is retried next run instead of permanently
                    # serving a partial. Keep the whole source's old chunks.
                    self._emit(
                        f"WARNING: {rel} pages {start + 1}-{end} only partially "
                        "extracted (known extraction failure or limit); "
                        "not cached or indexed so it will retry next run."
                    )
                    self._scan_failed_sources.add(rel)
                    return
                elif text and not self._save_pdf_cache_file(range_cache, text):
                    self._scan_failed_sources.add(rel)
                    return
            if not text:
                self._scan_failed_sources.add(rel)
                return
            ready_ranges.append((start, end, range_cache))
        for start, end, range_cache in ready_ranges:
            if self._stop_event.is_set():
                self._scan_failed_sources.add(rel)
                return
            text = self._read_first_text_file([range_cache])
            if not text:
                self._scan_failed_sources.add(rel)
                return
            self._export_readable_pdf(path, vault_root, signature, text,
                                      page_range=(start, end))
            yield LlamaDocument(
                text=text,
                metadata={
                    "file_path": str(path),
                    "source": rel,
                    "extension": ext,
                    "page_start": start,
                    "page_end": end,
                },
            )

    def _export_readable_pdf(self, path: Path, vault_root: Path, signature: dict,
                             text: str, *, page_range: Optional[tuple[int, int]] = None) -> None:
        """Best-effort display copy; never change the PDF indexing payload."""
        if self._stop_event.is_set():
            return
        from core.readable_pdf_export import export_pdf_text
        destination = (Path(OBSIDIAN_CACHE_DIR).parent / "readable_pdf_exports"
                       / self._vault_cache_key(vault_root))
        try:
            export_pdf_text(path, vault_root, destination, signature, text, page_range=page_range)
        except Exception as exc:
            message = f"WARNING: Markdown export skipped for {path.name}: {exc}"
            if self._index_state in ("running", "scanning", "embedding"):
                self._readable_export_warning_count = getattr(self, "_readable_export_warning_count", 0) + 1
                if self._readable_export_warning_count <= 3:
                    self._emit(message)
                else:
                    logger.warning(message)
            else:
                self._emit(message)

    @staticmethod
    def _pdf_extraction_incomplete(sections: Any) -> bool:
        status = getattr(sections, "extraction_status", None)
        return bool(getattr(sections, "truncated", False)
                    or getattr(status, "completeness", None) == "partial")

    def _pdf_cache_file(self, vault_root: Path, signature: dict) -> Path:
        vault_key = self._vault_cache_key(vault_root)
        digest = str(signature["sha256"])
        return Path(OBSIDIAN_CACHE_DIR) / "pdf_cache" / vault_key / f"{digest}.txt"

    # Page ranges are zero-padded to 5 digits so lexicographic order equals
    # page order — _read_pdf_range_caches relies on sorted() to stitch ranges
    # back together in sequence.  Width 5 covers PDF_MAX_PAGES (20 000).
    _PDF_RANGE_CACHE_RE = re.compile(r"-p(\d{5})-(\d{5})\.txt$")

    def _pdf_range_cache_file(
        self, vault_root: Path, signature: dict, start: int, end: int
    ) -> Path:
        """Cache file for one extracted page range of a large PDF.

        Sibling of the whole-file cache, distinguished by a ``-pSSSSS-EEEEE``
        suffix on the same content digest, so deleting a PDF's cache entries
        (whole-file or per-range) remains a simple glob on the digest.
        """
        vault_key = self._vault_cache_key(vault_root)
        digest = str(signature["sha256"])
        return (
            Path(OBSIDIAN_CACHE_DIR) / "pdf_cache" / vault_key
            / f"{digest}-p{start:05d}-{end:05d}.txt"
        )

    def _legacy_pdf_cache_file(self, vault_root: Path, signature: dict) -> Path:
        vault_key = hashlib.sha256(str(vault_root).encode("utf-8")).hexdigest()[:16]
        digest = str(signature["sha256"])
        return Path(OBSIDIAN_INDEX_DIR) / "pdf_cache" / vault_key / f"{digest}.txt"

    def _vault_cache_key(self, vault_root: Path) -> str:
        return _storage_vault_key(vault_root)

    def _read_first_text_file(self, paths: list[Path],
                              max_chars: Optional[int] = None) -> str:
        """First readable file's text; ``max_chars`` bounds the READ itself.

        The bound matters on the read paths (2026-07-22 audit P8): a
        grandfathered concatenated cache for a 1000-page textbook is ~40 MB,
        and ``vault_read_note`` materialised all of it per agent tool call to
        serve a 32 kB budget. ``max_chars`` reads one char past the cap so a
        caller comparing ``len(text) > budget`` still detects overflow;
        ``None`` (the indexer's chunking path) keeps the full read.
        """
        for path in paths:
            if not path.exists():
                continue
            try:
                if max_chars is None:
                    return path.read_text(encoding="utf-8")
                with open(path, encoding="utf-8") as fh:
                    return fh.read(max_chars + 1)
            except (OSError, ValueError):
                continue
        return ""

    def _atomic_write_text(self, cache_file: Path, text: str) -> None:
        """Write *text* to *cache_file* atomically via a sibling temp file.

        Deliberately does NOT fsync (unlike _write_json_atomic / core.utils
        writers): this is the per-document PDF-text / image-description cache,
        written once per document during a multi-hour index run. Its contents are
        regenerable (re-extract / re-describe), so trading crash-durability for
        the per-write fsync latency across thousands of documents is the right
        call — a lost cache entry costs a re-extraction, never correctness.
        os.replace still guarantees no torn/partial cache file.
        """
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(cache_file.parent), suffix=".txt")
        try:
            try:
                f = os.fdopen(fd, "w", encoding="utf-8")
            except Exception:
                os.close(fd)
                raise
            with f:
                f.write(text)
            os.replace(tmp, cache_file)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _save_pdf_cache_file(self, cache_file: Path, text: str) -> bool:
        try:
            self._atomic_write_text(cache_file, text)
            return True
        except Exception as exc:
            # P0.4: surface cache write failures so the user sees why the next
            # restart re-extracts every PDF.  Throttled to one emit per run to
            # avoid flooding the status feed on chronic disk-full / permissions.
            logger.debug("Could not persist PDF extraction cache.", exc_info=True)
            if not self._pdf_cache_write_warning_emitted:
                self._pdf_cache_write_warning_emitted = True
                self._emit(
                    "WARNING: could not persist PDF extraction cache "
                    f"({type(exc).__name__}: {exc}). Subsequent indexing runs will "
                    "re-extract affected PDFs. Further cache errors will be logged "
                    "to chatekld.log only."
                )
            return False

    # Image-description cache.  Mirrors the PDF cache layout under
    # obsidian_storage/image_cache/{vault_key}/{sha256}.txt so the vision
    # model is only called the first time an image is seen.
    _IMAGE_MAX_BYTES = 20 * 1024 * 1024  # 20 MB per file

    def _image_cache_file(self, vault_root: Path, digest: str) -> Path:
        vault_key = self._vault_cache_key(vault_root)
        return Path(OBSIDIAN_CACHE_DIR) / "image_cache" / vault_key / f"{digest}.txt"

    def _legacy_image_cache_file(self, vault_root: Path, digest: str) -> Path:
        vault_key = self._vault_cache_key(vault_root)
        return Path(OBSIDIAN_INDEX_DIR) / "image_cache" / vault_key / f"{digest}.txt"

    def _extract_image_description(
        self,
        path: Path,
        vault_root: Path,
        rel: str,
        *, cache_only: bool = False,
    ) -> str:
        """Return a text description of the image at *path*.

        Cache hit returns the stored description without invoking the vision
        model.  On cache miss the image bytes are sent through
        ``vision_manager.describe_image`` and the result is cached for next
        run.  An empty string means the file was too large, the vision model
        was unavailable, or the description came back empty — the caller
        treats this as "skip and count".
        """
        try:
            before = path.stat()
            # Prepared text can cover an oversized original via bounded online
            # tiles. Hash as a stream; do not allocate the original to reuse it.
            with path.open('rb') as handle:
                digest = hashlib.file_digest(handle, 'sha256').hexdigest()
            after = path.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                self._emit(f"WARNING: Image changed while checking cache: {rel}")
                return ""
        except OSError as exc:
            self._emit(f"WARNING: Failed to hash image {rel}: {exc}")
            return ""
        cache_file = self._image_cache_file(vault_root, digest)
        cached = self._read_first_text_file(
            [cache_file, self._legacy_image_cache_file(vault_root, digest)]
        )
        if cached:
            return cached
        if cache_only:
            self._emit(f"WARNING: No completed online vision cache for {rel}; keeping previous chunks.")
            return ""
        if after.st_size > self._IMAGE_MAX_BYTES:
            self._emit(f"WARNING: Skipping {rel}: {after.st_size} bytes exceeds the "
                       f"{self._IMAGE_MAX_BYTES}-byte vault image cap.")
            return ""
        try:
            with path.open('rb') as handle:
                data = handle.read(self._IMAGE_MAX_BYTES + 1)
            if len(data) > self._IMAGE_MAX_BYTES or hashlib.sha256(data).hexdigest() != digest:
                self._emit(f"WARNING: Image changed or exceeds byte limit: {rel}")
                return ""
        except OSError as exc:
            self._emit(f"WARNING: Failed to read {rel}: {exc}")
            return ""
        try:
            b64 = base64.b64encode(data).decode("ascii")
            del data
            description = vision_manager.describe_image(b64) or ""
        except Exception as exc:
            self._emit(f"WARNING: Vision call failed for {rel}: {exc}")
            return ""
        description = description.strip()
        if not description:
            if not self._vision_unavailable_warned:
                self._vision_unavailable_warned = True
                self._emit(
                    "WARNING: Vision call returned no description. Verify the "
                    "configured vision model is loaded for the selected provider."
                )
            return ""
        try:
            self._atomic_write_text(cache_file, description)
        except Exception as exc:
            logger.debug("Could not persist image description cache.", exc_info=True)
            if not self._image_cache_write_warning_emitted:
                self._image_cache_write_warning_emitted = True
                self._emit(
                    "WARNING: could not persist image description cache "
                    f"({type(exc).__name__}: {exc}). Subsequent indexing runs "
                    "will re-call the vision model for affected images."
                )
        return description

    def _current_vault_key(self) -> str:
        """Cache key for the CONFIGURED vault, or ``""`` when unset/unresolvable.

        The signature cache is written from the loader and read from paths that
        take no vault argument (``_read_pdf_text``, ``refactor/pdfref``), so the
        key is resolved from config here rather than threaded through every
        caller. An empty key degrades to the shared legacy file — the pre-2026-07-30
        behaviour, never an exception.
        """
        vault_path = self.get_vault_path()
        if not vault_path:
            return ""
        try:
            return self._vault_cache_key(Path(vault_path).resolve())
        except OSError:
            return ""

    def _pdf_signatures_path(self) -> str:
        """Per-vault signature map (2026-07-30).

        This was ONE global file for every vault. The map is keyed by
        vault-RELATIVE path, so two vaults sharing a rel shared an entry, and —
        the practical cost — the loader persists the whole map it built for the
        vault it just scanned, silently EVICTING the other vault's entries and
        forcing a full re-hash of every large PDF there on its next run. The
        sibling PDF-text and image caches have been per-vault since they were
        written (``pdf_cache/<vault_key>/``, ``image_cache/<vault_key>/``); this
        brings the signature map into line. Legacy files are still READ (see
        :meth:`_legacy_pdf_signature_paths`) so the upgrade costs no re-hashing.
        """
        key = self._current_vault_key()
        if not key:
            return os.path.join(OBSIDIAN_CACHE_DIR, "pdf_signatures.json")
        return os.path.join(OBSIDIAN_CACHE_DIR, "pdf_signatures", f"{key}.json")

    def _legacy_pdf_signature_paths(self) -> tuple[str, ...]:
        """Read-only legacy locations, oldest first.

        Both are the pre-per-vault SHARED maps. They are read and merged (so an
        upgrade keeps its skip-the-hash benefit) but never written again, which
        also lets a SECOND vault adopt the same legacy entries instead of one
        vault's save destroying them. Adoption is safe even when the entries
        belong to another vault: ``_pdf_file_signature`` re-validates size +
        mtime_ns before reusing a sha, so a stale or foreign entry degrades to a
        full hash rather than serving wrong text.
        """
        return (
            os.path.join(OBSIDIAN_INDEX_DIR, "pdf_signatures.json"),
            os.path.join(OBSIDIAN_CACHE_DIR, "pdf_signatures.json"),
        )

    def _pdf_signature_read_paths(self) -> tuple[str, ...]:
        """Every path the read side merges, in precedence order (last wins)."""
        return (*self._legacy_pdf_signature_paths(), self._pdf_signatures_path())

    def _load_pdf_signature_cache(self) -> dict:
        """Return the persisted ``{rel_path: {size, mtime_ns, sha256}}`` map.

        Used by ``_pdf_file_signature`` to skip the expensive content hash
        when a PDF's size and mtime are unchanged from the previous run —
        textbook PDFs can be hundreds of MB, and hashing them every restart
        was the dominant cost of resuming an interrupted index.

        Merges the legacy shared maps first, then THIS vault's map, so a
        per-vault entry always wins over an inherited one.
        """
        merged: dict = {}
        for path in self._pdf_signature_read_paths():
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    merged.update(data)
            except Exception:
                pass
        return merged

    def _save_pdf_signature_cache(self, cache: dict) -> None:
        """Persist to THIS vault's map only — never back to a legacy shared file."""
        try:
            _write_json_atomic(self._pdf_signatures_path(), cache)
        except Exception as exc:
            logger.debug("Could not persist pdf_signatures.json: %s", exc)

    def _persisted_pdf_signatures(self) -> dict:
        """Stat-cached view of the persisted PDF-signature map, for read paths.

        ``_read_pdf_text`` (read_note, agent ``vault_read_note``) and
        ``refactor/pdfref.py`` pass this to ``_pdf_file_signature`` so an
        unchanged multi-hundred-MB PDF is not fully re-hashed on every call
        just to locate its extracted-text cache.  Keyed by both signature
        files' ``(size, mtime_ns)`` so the indexer's rewrite (or a first
        persist) is picked up on the next call.  The returned dict is shared
        — callers must treat it as read-only; only the indexer writes the
        persisted map.
        """
        key_parts: list = []
        for path in self._pdf_signature_read_paths():
            try:
                st = os.stat(path)
                key_parts.append((st.st_size, st.st_mtime_ns))
            except OSError:
                key_parts.append(None)
        # The per-vault path is part of the key too: a vault SWITCH changes
        # which file the last element refers to, and two vaults can easily have
        # identical (size, mtime_ns) tuples — without the path the switched-to
        # vault would be served the previous vault's parsed map.
        key = (tuple(key_parts), self._pdf_signatures_path())
        with self._pdf_sig_read_lock:
            if key == self._pdf_sig_read_cache_key:
                return self._pdf_sig_read_cache
        cache = self._load_pdf_signature_cache()
        with self._pdf_sig_read_lock:
            self._pdf_sig_read_cache = cache
            self._pdf_sig_read_cache_key = key
            return self._pdf_sig_read_cache

    def _pdf_file_signature(self, path: Path, sig_cache: dict | None = None, rel: str | None = None) -> dict:
        """Compute (or recover) the size/mtime/sha256 fingerprint of a PDF.

        When *sig_cache* is provided and contains an entry for *rel* whose
        size and mtime_ns match the file on disk, the previously-computed
        sha256 is reused — avoiding a multi-hundred-MB read for unchanged
        textbook PDFs.  Falls back to a full hash on first sight or mismatch.
        """
        stat = path.stat()
        size = stat.st_size
        mtime_ns = stat.st_mtime_ns
        if sig_cache is not None and rel:
            prev = sig_cache.get(rel)
            if (
                isinstance(prev, dict)
                and prev.get("size") == size
                and prev.get("mtime_ns") == mtime_ns
                and isinstance(prev.get("sha256"), str)
            ):
                return {"size": size, "mtime_ns": mtime_ns, "sha256": prev["sha256"]}
        return {
            "size": size,
            "mtime_ns": mtime_ns,
            "sha256": self._sha256_file(path),
        }

    def _reuse_pdf_signature(self, path: Path, signature: Optional[dict]) -> Optional[dict]:
        """*signature* iff it still describes the bytes on disk, else None.

        The re-stat is what makes handing a signature in safe: a file changed
        between the caller's fingerprint and this read falls back to a fresh
        hash rather than reading or writing a cache under a stale digest.
        """
        if not isinstance(signature, dict) or not isinstance(
                signature.get("sha256"), str):
            return None
        st = path.stat()
        if (signature.get("size") == st.st_size
                and signature.get("mtime_ns") == st.st_mtime_ns):
            return {"size": st.st_size, "mtime_ns": st.st_mtime_ns,
                    "sha256": signature["sha256"]}
        return None

    def _sha256_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _normalised_excluded_dirs(self, vault_root: Path, entries: list[Any]) -> set[str]:
        excluded: set[str] = set()
        for entry in entries:
            raw = str(entry).strip()
            if not raw:
                continue
            p = Path(raw).expanduser()
            try:
                rel = p.resolve().relative_to(vault_root) if p.is_absolute() else Path(raw)
            except ValueError:
                continue
            rel_str = rel.as_posix().strip("/")
            if rel_str and ".." not in Path(rel_str).parts:
                excluded.add(rel_str)
        return excluded

    def _write_index_manifest(self, counts: dict[str, int], vault_path: str) -> None:
        """Persist a small human-readable inventory of the current vault index.

        *counts* maps raw sources (as produced by ``_manifest_source``) to
        chunk counts.  The path resolution below runs once per unique source
        instead of once per chunk; two raw sources that resolve to the same
        vault-relative path still aggregate into a single entry, matching the
        old per-document reduction.  The ``""`` bucket (documents without
        source metadata) is skipped, as those documents always were.
        """
        vault_root = Path(vault_path).resolve()
        entries: dict[str, dict] = {}
        for raw_source, count in counts.items():
            if not raw_source:
                continue
            try:
                source = Path(raw_source).resolve().relative_to(vault_root).as_posix()
            except (OSError, ValueError):
                source = str(raw_source)
            entry = entries.setdefault(source, {
                "source": source,
                "extension": Path(source).suffix.lower(),
                "chunk_count": 0,
            })
            entry["chunk_count"] += count

        payload: dict[str, Any] = {
            "vault_path": str(vault_root),
            "indexed_at": datetime.now(timezone.utc).isoformat(),
            "materials": sorted(entries.values(), key=lambda item: item["source"].lower()),
        }
        os.makedirs(OBSIDIAN_INDEX_DIR, exist_ok=True)
        _write_json_atomic(self._manifest_path(), payload)

    def get_indexed_materials(self) -> dict:
        """Return the persisted manifest used by the Indexed Materials panel."""
        try:
            with open(self._manifest_path(), encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, dict) and isinstance(payload.get("materials"), list):
                return payload
        except Exception:
            pass
        fallback = self._build_index_manifest_from_docstore()
        if fallback["materials"]:
            return fallback
        return {
            "vault_path": self.get_vault_path(),
            "indexed_at": None,
            "materials": [],
        }

    def _manifest_path(self) -> str:
        return os.path.join(OBSIDIAN_INDEX_DIR, "indexed_materials.json")

    #: Guards the manifest-recovery parse below, and memoises its result on the
    #: docstore's ``(size, mtime_ns)``. Module-scoped rather than per-instance:
    #: ``obsidian_manager`` is a singleton and the two callers reach it from
    #: different threads.
    _MANIFEST_FALLBACK_LOCK = threading.Lock()
    _manifest_fallback_memo: tuple | None = None

    def _build_index_manifest_from_docstore(self) -> dict:
        """Recover an indexed-material manifest from an existing LlamaIndex docstore.

        # PERF (2026-09-02): an unserialised whole-docstore parse on a request
        # thread.
        #   Before:    when `indexed_materials.json` is missing, BOTH the
        #              materials route and the agent's `vault_list_materials`
        #              tool parsed `docstore.json` — several hundred MB on a
        #              real vault — with nothing serialising them, and then both
        #              wrote the manifest. Measured on a 20 000-document
        #              docstore stand-in: 561 ms per parse, per caller, on a
        #              Waitress worker (a memo hit is 0.013 ms); a real vault's
        #              docstore is far larger. The indexer's own docs cite exactly
        #              this cost as the reason to SKIP the parse at checkpoint
        #              time.
        #   Change:    one build lock plus a memo keyed on the docstore's
        #              (size, mtime_ns). Concurrent callers wait for the first
        #              rather than each parsing; a repeat caller after a failed
        #              manifest write (a read-only index dir) gets the memo.
        #   Safe because:
        #              the payload is derived purely from that file, so an
        #              identical (size, mtime_ns) cannot yield a different
        #              answer; a reindex rewrites the docstore and moves both.
        #              The recovered manifest is still written atomically in the
        #              existing shape, by the elected caller, exactly once.
        #   Preserves: "The recovered manifest is written atomically in the
        #              existing shape" — `_write_json_atomic` is unchanged, and
        #              a failed write still degrades to returning the payload.
        #   Lock/thread contract:
        #              a module lock held across the parse. That is deliberate
        #              and is the POINT — the alternative is N concurrent
        #              several-hundred-MB parses — and it is a leaf: no other
        #              lock is taken beneath it.
        #   Pinned by: test_vault_regressions.py::TestManifestFallbackSingleFlight
        """
        docstore_path = os.path.join(OBSIDIAN_INDEX_DIR, "docstore.json")
        try:
            st = os.stat(docstore_path)
            sig = (docstore_path, st.st_size, st.st_mtime_ns)
        except OSError:
            sig = None
        with ObsidianVaultManager._MANIFEST_FALLBACK_LOCK:
            memo = ObsidianVaultManager._manifest_fallback_memo
            if sig is not None and memo is not None and memo[0] == sig:
                return memo[1]
            payload = self._build_index_manifest_from_docstore_locked(
                docstore_path)
            if sig is not None:
                ObsidianVaultManager._manifest_fallback_memo = (sig, payload)
            return payload

    def _invalidate_manifest_fallback_memo(self) -> None:
        """Drop the recovery memo (vault switch / reset / a fresh index)."""
        with ObsidianVaultManager._MANIFEST_FALLBACK_LOCK:
            ObsidianVaultManager._manifest_fallback_memo = None

    def _build_index_manifest_from_docstore_locked(self, docstore_path: str) -> dict:
        """The parse itself; the caller holds ``_MANIFEST_FALLBACK_LOCK``."""
        try:
            with open(docstore_path, encoding="utf-8") as f:
                docstore = json.load(f)
        except Exception:
            return {"vault_path": self.get_vault_path(), "indexed_at": None, "materials": []}

        ref_doc_info = docstore.get("docstore/ref_doc_info")
        if not isinstance(ref_doc_info, dict):
            return {"vault_path": self.get_vault_path(), "indexed_at": None, "materials": []}

        vault_path = self.get_vault_path()
        vault_root = None
        if vault_path:
            try:
                vault_root = Path(vault_path).resolve()
            except OSError:
                vault_root = None

        entries: dict[str, dict] = {}
        for ref_doc_id, info in ref_doc_info.items():
            if not isinstance(info, dict):
                continue
            _raw_meta = info.get("metadata")
            meta: dict = _raw_meta if isinstance(_raw_meta, dict) else {}
            source = self._material_source_from_metadata(meta, str(ref_doc_id), vault_root)
            if not source:
                continue
            entry = entries.setdefault(source, {
                "source": source,
                "extension": Path(source).suffix.lower(),
                "chunk_count": 0,
            })
            entry["chunk_count"] += 1

        payload: dict[str, Any] = {
            "vault_path": str(vault_root) if vault_root else vault_path,
            "indexed_at": None,
            "materials": sorted(entries.values(), key=lambda item: item["source"].lower()),
        }

        index_meta = self._read_index_meta()
        if index_meta is not None:
            payload["indexed_at"] = index_meta.get("indexed_at")

        try:
            _write_json_atomic(self._manifest_path(), payload)
        except Exception:
            logger.debug("Could not persist recovered indexed-material manifest.", exc_info=True)

        return payload

    def _material_source_from_metadata(
        self,
        meta: dict,
        ref_doc_id: str,
        vault_root: Optional[Path],
    ) -> str:
        raw_source = meta.get("source") or meta.get("file_path") or ""
        if raw_source:
            try:
                source_path = Path(str(raw_source)).resolve()
                if vault_root:
                    return source_path.relative_to(vault_root).as_posix()
            except (OSError, ValueError):
                pass
            return str(raw_source)

        source = ref_doc_id
        for marker in ("::heading:", "::page:", "::flat"):
            if marker in source:
                source = source.split(marker, 1)[0]
                break
        if "::" in source:
            source = source.rsplit("::", 1)[0]
        return source

    # The two sidecar-path accessors stay HERE, not in rag/sidecars.py, even
    # though everything else about both sidecars moved: they read the module
    # global OBSIDIAN_INDEX_DIR, and the suite patches that as
    # `rag.vault.OBSIDIAN_INDEX_DIR`. A global read relocated into another
    # module would resolve against *that* module's namespace and silently
    # ignore the patch. RetrievalCacheMixin reaches both through `self`.
    def _bm25_sidecar_dir(self) -> str:
        return os.path.join(OBSIDIAN_INDEX_DIR, _BM25_SIDECAR_DIRNAME)

    def _wikilink_sidecar_path(self) -> str:
        return os.path.join(OBSIDIAN_INDEX_DIR, _WIKILINK_SIDECAR_FILENAME)

    # Historical default curated-thesaurus filenames (see `_get_thesaurus`). The
    # active paths are config-driven (`vault_thesaurus_abbrev_path` /
    # `vault_thesaurus_tags_path`); these are only the fallbacks.
    _THESAURUS_DEFAULT_ABBREV = "_abreviations.md"
    _THESAURUS_DEFAULT_TAGS = "_tags.md"

    def _resolve_thesaurus_rel(self, vault_root: str, rel: Any) -> Optional[str]:
        """Resolve a configured vault-relative thesaurus path under *vault_root*.

        Returns the absolute real path when *rel* is a safe vault-relative file,
        or ``None`` for an empty/disabled slot or any traversal/absolute/escaping
        value (defence in depth — `/api/config` already shape-checks it). Mirrors
        the refactor route's `_resolve_scope` posture.
        """
        if not isinstance(rel, str):
            return None
        s = rel.strip().replace("\\", "/")
        if not s or any(ch in s for ch in ("\x00", "\n", "\r")):
            return None
        if os.path.isabs(s) or ".." in s.split("/"):
            return None
        real = os.path.realpath(os.path.join(vault_root, s))
        if real != vault_root and not real.startswith(vault_root + os.sep):
            return None
        return real

    def _get_thesaurus(self) -> Optional[Any]:
        """Return the cached vault :class:`rag.thesaurus.Thesaurus`, parsing the
        configured curated glossary files on demand.

        The two source files are config-driven, vault-relative, and resolved
        under the vault root: ``vault_thesaurus_abbrev_path`` (default
        ``_abreviations.md``) and ``vault_thesaurus_tags_path`` (default
        ``_tags.md``); an empty/invalid value disables that slot. Independent of
        the index: cached by the resolved paths plus each file's ``(size, mtime)``
        signature — so an edit to either file *or* a config change to either path
        refreshes it on the next chat. Returns ``None`` when no vault path is set
        or neither configured file exists (the caller then degrades to no
        expansion / no primer). Reindex-free and re-embed-free — touches no
        LlamaIndex state.
        """
        vault = self._vault_path
        if not vault:
            return None
        vault_root = os.path.realpath(vault)
        # Read-only view (no deepcopy): this per-query helper only .get()s scalars.
        cfg = load_config_readonly()
        abbrev_rel = cfg.get("vault_thesaurus_abbrev_path", self._THESAURUS_DEFAULT_ABBREV)
        tags_rel = cfg.get("vault_thesaurus_tags_path", self._THESAURUS_DEFAULT_TAGS)
        abbrev_path = self._resolve_thesaurus_rel(vault_root, abbrev_rel)
        tags_path = self._resolve_thesaurus_rel(vault_root, tags_rel)
        # Build the (rel, abs-path, size, mtime) signature; a missing/disabled
        # file contributes a None stat slot so creating it later — or changing
        # the configured path — busts the cache too.
        sig_parts: list[tuple] = []
        present = False
        for rel, path in (("abbrev", abbrev_path), ("tags", tags_path)):
            if path is None:
                sig_parts.append((rel, None, None, None))
                continue
            try:
                st = os.stat(path)
                sig_parts.append((rel, path, st.st_size, st.st_mtime_ns))
                present = True
            except OSError:
                sig_parts.append((rel, path, None, None))
        if not present:
            return None
        cache_key = tuple(sig_parts)
        cached = self._thesaurus
        if cached is not None and self._thesaurus_cache_key == cache_key:
            return cached

        # Read + parse OUTSIDE the lock. The vault lives under iCloud
        # (~/Library/Mobile Documents), where open().read() of a dataless
        # placeholder can block on a network materialisation; doing it under the
        # build lock would stall every other first-time caller. Two concurrent
        # misses may each read+parse (cheap, idempotent) — the lock below only
        # guards the compare-and-set, so the result is still consistent.
        from .thesaurus import Thesaurus

        def _read(path: Optional[str]) -> str:
            if not path:
                return ""
            try:
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    return fh.read()
            except OSError:
                return ""
        try:
            thes = Thesaurus.from_files(
                abbrev_text=_read(abbrev_path),
                tags_text=_read(tags_path),
            )
        except Exception:
            logger.debug("Thesaurus parse failed; expansion/primer disabled.", exc_info=True)
            return None

        with self._thesaurus_build_lock:
            # Re-check: a concurrent miss may have already stored this signature.
            if self._thesaurus is not None and self._thesaurus_cache_key == cache_key:
                return self._thesaurus
            self._thesaurus = thes
            self._thesaurus_cache_key = cache_key
            return thes

    # Accepted vault_reranker_device values; anything else behaves as "auto".
    _RERANKER_DEVICE_MODES = ("auto", "cpu", "mps")

    def _resolve_reranker_device_mode(self) -> str:
        """Read the ``vault_reranker_device`` knob via stat-cached load_config.

        ``"auto"`` (and any unknown/missing value) keeps the pre-knob
        behaviour: the reranker is constructed without a ``device``
        argument, so llama-index's ``infer_torch_device()`` picks MPS on
        Apple Silicon when available, else CPU.  ``"cpu"`` is the escape
        hatch that keeps unified memory free for the LLM; ``"mps"``
        requires Metal (and still degrades to CPU on failure).
        """
        try:
            # Read-only view (no deepcopy): scalar .get() only, invoked per query.
            raw = load_config_readonly().get("vault_reranker_device", "auto")
        except Exception:
            return "auto"
        mode = str(raw or "auto").strip().lower()
        return mode if mode in self._RERANKER_DEVICE_MODES else "auto"

    @staticmethod
    def _warmup_reranker(reranker: Any) -> None:
        """Run one tiny cross-encoder predict when the model is NOT on CPU.

        Metal/MPS failures often surface on the first forward pass rather
        than at construction.  Raising here keeps the failure inside the
        construction ``try`` so the CPU retry catches it — without this, an
        inference-time MPS failure would error every chat (the engine's
        postprocessor walk does not degrade) and never trip the fallback.
        Skipped silently when the package layout differs (no ``_model`` /
        ``_device`` private attrs).
        """
        device = str(getattr(reranker, "_device", "") or "").lower()
        if not device or device.startswith("cpu"):
            return
        predict = getattr(getattr(reranker, "_model", None), "predict", None)
        if callable(predict):
            predict([("warm-up", "warm-up")])

    def _retrieval_warning(
        self, msg: str, stage_cb: Optional[Callable[[str], None]]
    ) -> None:
        """Surface a retrieval-time warning on a channel the user can actually see.

        DEFECT this fixes (2026-07-27): ``_emit`` appends to ``_status_messages``,
        which is drained ONLY by ``/api/obsidian/status`` — a poll the client runs
        only while INDEXING, and which renders ``data.warnings``, never
        ``data.messages``. The drain is destructive. So a reranker warning raised
        during a CHAT was written to a ring nobody was reading and then discarded:
        a typo'd ``vault_reranker_model``, or an offline first-run, silently
        disabled reranking for the whole session (the failure flag is sticky) with
        ZERO user-visible signal — answers just quietly got worse.

        ``stage_cb`` is the caller's SSE ``{info}`` sink and is non-None exactly
        when we are inside a chat/retrieval request, so routing through it puts
        the warning in front of the user who triggered it. ``_emit`` is kept
        unconditionally: it is the log line + the indexing path's channel (where
        ``stage_cb`` is None and the status poll IS running), so the indexing
        behaviour is unchanged rather than moved. Guarded like every other
        ``stage_cb`` use — a failing UI callback must not break retrieval.
        """
        self._emit(msg)
        if stage_cb is not None:
            try:
                stage_cb(msg)
            except Exception:  # noqa: BLE001 — a warning channel must never break retrieval
                logger.debug("Reranker warning callback failed.", exc_info=True)

    def _build_reranker(
        self, reranker_cls: Any, model_name: str, top_n: int, device_mode: str,
        stage_cb: Optional[Callable[[str], None]] = None,
    ) -> Any:
        """Construct the cross-encoder for *device_mode*, degrading to CPU.

        ``"auto"`` omits the device argument entirely — byte-identical to
        the pre-knob construction.  A non-CPU failure (construction or
        warm-up inference) retries on CPU and warns instead of raising:
        a Metal hiccup must cost rerank *latency*, never rerank itself.
        Only the CPU attempt's failure propagates to the caller's sticky
        failure flag.

        ``stage_cb`` (optional) routes the MPS→CPU degradation warning to the
        caller's SSE ``{info}`` channel as well as the status ring — see
        ``_retrieval_warning`` for why the ring alone was invisible during a chat.
        """
        kwargs: dict[str, Any] = {"model": model_name, "top_n": top_n}
        if device_mode != "auto":
            kwargs["device"] = device_mode
        try:
            reranker = reranker_cls(**kwargs)
            self._warmup_reranker(reranker)
            return reranker
        except Exception as exc:
            if device_mode == "cpu":
                raise
            logger.warning(
                "Reranker failed on device mode %r (%s); retrying on CPU.",
                device_mode, exc,
            )
            reranker = reranker_cls(model=model_name, top_n=top_n, device="cpu")
            self._retrieval_warning(
                f"WARNING: Reranker could not run on '{device_mode}' ({exc}); "
                "using CPU for this session. Change vault_reranker_device or "
                "restart to retry.",
                stage_cb,
            )
            return reranker

    @measured("init.reranker")
    def _get_reranker(self, model_name: str, top_n: int,
                      stage_cb: Optional[Callable[[str], None]] = None) -> Optional[Any]:
        """Return a cached SentenceTransformerRerank for *model_name*, or
        None if the package or model cannot be loaded.

        The model itself is loaded once (sentence-transformers downloads
        weights to ~/.cache/huggingface/hub on first use); the cached fetch
        is READ-ONLY (item 2.7 — ``top_n`` is tuned per query by the
        engine's locked pipeline build, never here).  A sticky
        failure flag prevents a hot-loop of re-attempted downloads when the
        configured model is unavailable — the user must change the model
        name or the ``vault_reranker_device`` knob (both are folded into
        the cache/failure key) to retry.

        ``stage_cb`` (the chat worker's SSE ``{info}`` sink; ``None`` from
        prewarm) does two jobs, both added 2026-07-27 — see the heartbeat
        comment below and ``_retrieval_warning``.
        """
        from .engine import SentenceTransformerRerank
        if SentenceTransformerRerank is None or not model_name:
            return None
        device_mode = self._resolve_reranker_device_mode()
        cache_key = f"{model_name}::{device_mode}"
        # Heartbeat (2026-07-27) — mirrors the BM25 build's P4b ticker.
        # DEFECT: the first use of a reranker DOWNLOADS its weights inside
        # one blocking call and emitted nothing while doing so. The SSE
        # consumer's stall guard is fed by frames and fires at
        # SSE_SINGLE_SHOT_FLOOR_S + SSE_STALL_MARGIN_S = 330 s
        # (core/constants.py), so a large cross-encoder (BAAI/bge-reranker-v2-m3
        # is ~2.3 GB) needs a sustained ~7 MB/s or the user's first query dies
        # with "The operation timed out — the model may be overloaded",
        # blaming the model instead of the download — while the download
        # keeps running on this worker, which is why a later retry
        # "mysteriously" succeeds. A beat every 30 s resets that timer, so the
        # guard again measures genuine stalls rather than honest progress.
        #
        # The ticker starts BEFORE `_reranker_load_lock` (2026-07-29 audit 1.1):
        # the first cut started it inside the lock, which covered exactly one
        # caller — the thread doing the load. Every other chat blocked on the
        # acquisition one line above it, emitting nothing, fully exposed to the
        # stall guard. The realistic trigger is launch PREWARM, which calls
        # this with stage_cb=None and holds the lock for the entire multi-GB
        # download — so the documented upgrade flow ("relaunch, ask one
        # question") walked the first chat straight into a silent 330 s wait:
        # the very failure the ticker was written to eliminate. Beating from
        # the waiter's side covers both the wait and the load. Cost: one
        # short-lived daemon thread per rerank-enabled query; the first beat
        # only fires after a full interval, so the cached fast path emits
        # nothing and the thread exits at the `finally` in microseconds.
        # A periodic ticker (not an HF progress callback) is deliberate: it
        # is transport-agnostic, so it also covers the model *load* of an
        # already-cached multi-GB model and the warm-up predict, and it adds
        # no coupling to huggingface_hub internals. A None stage_cb (prewarm,
        # off the chat path) starts no thread — silent exactly as before.
        hb_stop = threading.Event()
        if stage_cb is not None:
            interval = self._RERANKER_HEARTBEAT_S

            def _hb() -> None:
                waited = 0.0
                while not hb_stop.wait(interval):
                    waited += interval
                    try:
                        stage_cb(
                            f"Still waiting for the cross-encoder reranker "
                            f"'{model_name}' ({waited:.0f}s) — a first run "
                            f"downloads the model…")
                    except Exception:  # noqa: BLE001 — heartbeat must never kill the load
                        return
            threading.Thread(target=_hb, name="reranker-load-heartbeat",
                             daemon=True).start()
        try:
            with self._reranker_load_lock:
                if (
                    self._reranker is not None
                    and self._reranker_model_loaded == cache_key
                ):
                    # Read-only return (item 2.7): ``top_n`` used to be written
                    # here under ``_reranker_load_lock``, so an in-flight rerank
                    # could be retrimmed to another request's top_n mid-pass.
                    # Nobody writes ``top_n`` on THIS shared object any more.
                    # Since AP-09 each request retunes its own shallow
                    # ``copy.copy`` facade in ``_build_retrieval_pipeline``,
                    # which runs in ``engine.prepare()`` — deliberately OUTSIDE
                    # ``_index_mutation_lock``, so no earlier statement about
                    # that lock protecting the retune still applies. The facades
                    # share this object's loaded model, and reranking now runs
                    # lock-free, so concurrent inference on it is bounded by
                    # ``rag.engine._RERANK_GATE`` instead.
                    return self._reranker
                # Reset the sticky failure flag when the requested model or
                # device differs from the last attempt — a config change is the
                # user's signal that they want us to retry.
                if cache_key != self._reranker_last_tried:
                    self._reranker_failed = False
                if self._reranker_failed:
                    # Per-query notice (2026-07-29 audit 1.2): the load-failure
                    # warning below fires once per PROCESS — this early return
                    # then silently degraded every later query for the session.
                    # Routed through the guarded stage_cb ONLY, never
                    # `_retrieval_warning`: its `_emit` half would append one
                    # ring line per query forever, and the log already carries
                    # the original failure. The sticky flag itself is untouched
                    # — still set only by a load failure, still cleared only by
                    # a model/device change.
                    if stage_cb is not None:
                        try:
                            stage_cb(
                                f"Reranking is disabled for this session (the "
                                f"reranker '{model_name}' failed to load "
                                f"earlier). Change the reranker model or device "
                                f"in Settings to retry.")
                        except Exception:  # noqa: BLE001 — a notice must never break retrieval
                            pass
                    return None
                try:
                    reranker = self._build_reranker(
                        SentenceTransformerRerank, model_name, top_n, device_mode,
                        stage_cb=stage_cb,
                    )
                except Exception as exc:
                    self._reranker_failed = True
                    self._reranker_last_tried = cache_key
                    logger.warning("Reranker load failed for %s: %s", model_name, exc)
                    # launch.py enables HF offline mode when the *configured*
                    # model is cached; a model changed mid-session to an
                    # uncached name cannot download until the next launch.
                    offline_hint = (
                        " HF offline mode is active — restart the app to allow "
                        "a first-time download of this model."
                        if os.environ.get("HF_HUB_OFFLINE") else ""
                    )
                    # Routed through _retrieval_warning (2026-07-27): this is the
                    # warning that used to vanish. The sticky-failure semantics are
                    # untouched — the flag is still set here and still cleared only
                    # by a model/device change — only the channel widened, so the
                    # user learns their whole session lost reranking instead of
                    # silently getting worse answers.
                    self._retrieval_warning(
                        f"WARNING: Reranker '{model_name}' could not be loaded; "
                        f"vault chat will use retrieval without rerank — answers "
                        f"come from raw retrieval ranking and quality may drop "
                        f"({exc})." + offline_hint,
                        stage_cb,
                    )
                    return None
                self._reranker = reranker
                self._reranker_model_loaded = cache_key
                self._reranker_last_tried = cache_key
                return reranker
        finally:
            # Stop the ticker on EVERY exit (success, degraded-to-CPU,
            # failure). Request-bound, not instantaneous: `set()` signals, so
            # a beat already inside stage_cb completes — but stage_cb is this
            # request's own sink, so a late beat can only land in the stream
            # it belongs to, never a later turn's.
            hb_stop.set()

    def _query_embedding_identity(self, provider_name, embed_name, stage):
        from .embedding_identity import resolve_query_identity
        identity = resolve_query_identity(self._read_index_meta(), load_config(),
                                          provider_name, embed_name)
        for warning in identity.warnings:
            logger.warning(warning)
            stage(warning)
        return identity

    def _effective_embed_name(
        self, requested_embed: str, stage: Callable[[str], None]
    ) -> str:
        """Return the embed model retrieval must actually use.

        The stored chunk vectors were produced by the model recorded in
        ``obsidian_meta.json``.  Embedding the query with a DIFFERENT model fuses
        two incompatible vector spaces and silently wrecks retrieval, so when the
        configured embed model differs from the index's we retrieve with the
        INDEX's model and warn — rather than honour the stale config and return
        garbage.  Re-indexing is what actually switches models (the indexer
        rebuilds on an embed change); this keeps chat correct until the user does.
        """
        indexed_embed = (self._read_index_meta() or {}).get("embed")
        if indexed_embed and requested_embed and indexed_embed != requested_embed:
            stage(
                f"Index was built with embedding model '{indexed_embed}'; "
                f"retrieving with it (re-index to switch to '{requested_embed}')."
            )
            return indexed_embed
        return requested_embed

    def _assert_index_matches_vault(self, meta: Optional[dict]) -> None:
        """Refuse to serve an on-disk index recorded against a DIFFERENT vault.

        The A1 vault-switch cleanup drops the in-memory index, but the on-disk
        index dir is vault-independent — without this check the very next chat
        lazy-RELOADED the previous vault's index and answered from it (a
        correctness and privacy leak; the switched-away vault's content came
        back with citations). A missing/blank recorded path is a pre-A1 index:
        grandfathered for the current vault, same contract as the indexer's
        incremental gate, which stamps the field at the next persist. Both
        sides are resolved paths (_write_index_meta stores the resolved vault;
        _normalise_vault_path resolves the current one), so string equality is
        the right comparison.
        """
        recorded = str((meta or {}).get("vault_path") or "")
        current = self._vault_path
        if recorded and current and recorded != current:
            raise RuntimeError(
                f"The saved index was built from a different vault "
                f"({recorded}). Re-run indexing to use the current vault."
            )

    @measured("init.index")
    def _ensure_index_loaded(
        self,
        *,
        provider_name: str,
        embed_name: str,
        stage_cb: Optional[Callable[[str], None]] = None,
    ) -> Any:
        """Lazy-load the saved index from disk if it isn't in memory.

        Double-checked locking: a read-lock check, then a write-lock
        re-check + load. Writing ``self._index`` inside a read lock
        would allow two concurrent readers to both see ``None`` and
        both assign — a TOCTOU race. Raises ``RuntimeError`` if no
        index exists on disk or if the checkpoint files fail
        validation.
        """
        def _stage(msg: str) -> None:
            if stage_cb is not None:
                try:
                    stage_cb(msg)
                except Exception:
                    logger.debug("Vault stage callback failed.", exc_info=True)

        with self._rw_lock.read_lock():
            local_index = self._index
        if local_index is not None:
            return local_index

        with self._rw_lock.write_lock():
            if self._index is None:
                if os.path.exists(os.path.join(OBSIDIAN_INDEX_DIR, "docstore.json")):
                    _stage("Loading saved vault index…")
                    meta = self._read_index_meta()
                    self._assert_index_matches_vault(meta)
                    backend = self._resolve_existing_backend(meta)
                    try:
                        self._validate_persisted_index_files(
                            OBSIDIAN_INDEX_DIR, full=False, backend=backend,
                            check_promotion_marker=True,
                        )
                    except RuntimeError as exc:
                        self._index_integrity_error = str(exc)
                        raise
                    identity = self._query_embedding_identity(provider_name, embed_name, _stage)
                    provider = get_provider(identity.provider)
                    embed_model = provider.get_embedding(identity.model)
                    try:
                        self._replace_index(self._build_index_for_backend(
                            fresh=False, backend=backend, embed_model=embed_model
                        ))
                    except json.JSONDecodeError as exc:
                        self._index_integrity_error = (
                            f"Index checkpoint is corrupt or incomplete "
                            f"({exc.msg} at line {exc.lineno}, column {exc.colno})."
                        )
                        raise RuntimeError(self._index_integrity_error) from exc
                    self._index_integrity_error = ""
                else:
                    raise RuntimeError("Index not found. Please index the vault first.")
            return self._index


    def _build_engine(
        self,
        *,
        llm_name: str,
        embed_name: str,
        top_k: int,
        provider_name: str,
        similarity_cutoff: float,
        prompt_mode: str = "strict",
        temperature: float | None = None,
        top_k_explicit: bool = False,
        hybrid_enabled: bool = False,
        reranker_enabled: bool = False,
        reranker_model: str = "",
        custom_system_prompt: str = "",
        mmr_enabled: bool = False,
        mmr_lambda: float | None = None,
        query_expansion: bool = False,
        num_queries: int = 1,
        rerank_pool_ceiling: int | None = None,
        wikilink_expansion: bool = False,
        thesaurus_expansion: bool = False,
        primer_enabled: bool = False,
        stage_cb: Callable[[str], None] | None = None,
        log_label: str = "Vault chat",
    ):
        """Shared retrieval-engine build for stream_chat/retrieve (item 4.8).

        DEFECT this consolidates: the two entrypoints carried ~70 duplicated
        build lines that had already drifted (the thesaurus/primer stages were
        missing from ``retrieve``) — and the first 4.8 extraction (d06cd73)
        shipped a self-recursive stub in place of this body, killing vault
        chat with a RecursionError on the first message. This is the real
        extraction, line-identical to the pre-d06cd73 ``stream_chat`` build.
        ``retrieve`` gains the thesaurus/primer knobs (defaults off; the
        agent's ``vault_search`` never passes them, so the documented "no
        thesaurus on the agent's active search" contract is unchanged).

        Lock ordering preserved exactly (it is what rag/CLAUDE.md documents):
        index lazy-load under the rw write lock (double-checked), BM25 build +
        first-time reranker load BEFORE the read lock so they don't block
        other readers or the indexer's persist, engine construction under the
        read lock — and NO retrieval here: callers wrap engine.query()/
        .retrieve() in ``_acquire_retrieval_lock`` themselves so the lock is
        held only for the brief retrieval phase. Returns ``(engine, stage)``
        so callers emit their stage labels through the same guarded callback.
        """
        from .engine import SimpleQueryEngine

        def _stage(msg: str) -> None:
            if stage_cb is not None:
                try:
                    stage_cb(msg)
                except Exception:
                    logger.debug("%s stage callback failed.", log_label, exc_info=True)

        # Retrieve with the index's own embed model, not whatever config says,
        # so a config switch without a re-index cannot mix vector spaces (M1).
        identity = self._query_embedding_identity(provider_name, embed_name, _stage)
        effective_embed = identity.model
        self._ensure_index_loaded(
            provider_name=identity.provider,
            embed_name=effective_embed,
            stage_cb=stage_cb,
        )

        # Build retrieval helpers before taking the read lock around engine
        # construction so the BM25 build (which iterates the docstore) and
        # the first-time reranker load (which downloads weights) do not
        # block other readers or the indexer's persist.
        if hybrid_enabled:
            _stage("Building lexical BM25 retriever…")
        bm25_retriever = (
            # stage_cb feeds the SSE stall guard during a long build (P4b).
            self._get_bm25_retriever(top_k=top_k, stage_cb=_stage)
            if hybrid_enabled
            else None
        )
        if reranker_enabled and reranker_model:
            _stage("Loading cross-encoder reranker…")
        reranker = (
            # stage_cb feeds the SSE stall guard during a first-use model
            # download AND carries the load/degradation warnings to a channel
            # the chat client actually renders (2026-07-27) — same contract as
            # the BM25 build above.
            self._get_reranker(model_name=reranker_model, top_n=top_k,
                               stage_cb=_stage)
            if reranker_enabled and reranker_model
            else None
        )
        # Wikilink note→note graph (query-time, no reindex).  Built lazily from
        # the docstore and cached like BM25.  Built only when expansion is
        # requested AND a reranker is active: expansion is rerank-gated (the
        # engine attaches it only with a reranker present, since the reranker is
        # what trims seeds+neighbours back to top_k), so without one the build
        # would be wasted work and the stage label misleading.
        want_wikilink = wikilink_expansion and reranker is not None
        if want_wikilink:
            _stage("Building wikilink graph…")
        wikilink_graph = self._get_wikilink_index() if want_wikilink else None

        # Vault thesaurus (query expansion + system-prompt primer).  Parsed
        # from the curated root files, cached by mtime, index-independent.
        # Built once if either feature is on; None degrades to no expansion.
        # Unlike wikilink it is NOT rerank-gated (it preserves the score scale
        # and caps the pool), so no reranker precondition here.
        want_thesaurus = thesaurus_expansion or primer_enabled
        thesaurus = self._get_thesaurus() if want_thesaurus else None
        # Emit the stage label only AFTER a successful build: a missing
        # _abreviations.md/_tags.md makes _get_thesaurus() return None, and we
        # must not tell the user we expanded the query when we did not.
        if thesaurus_expansion and thesaurus is not None:
            _stage("Expanding query via vault thesaurus…")

        # Re-read self._index inside a read lock to guard against a concurrent
        # reset nulling it between the slow-path write lock and here.  Only
        # the engine construction needs the lock; the actual query (embedding
        # + LLM I/O) runs outside so network latency never blocks index writes.
        with self._rw_lock.read_lock():
            local_index = self._index
            if local_index is None:
                raise RuntimeError("Index not found. Please index the vault first.")
            # Every construction/load path publishes VectorStoreIndex; the
            # broader BaseIndex annotation exists only because LlamaIndex's
            # loader is generic. Narrowing here documents the query-engine
            # runtime invariant without changing lock scope or state.
            vector_index = cast(VectorStoreIndex, local_index)
            engine = SimpleQueryEngine(
                vector_index,
                llm_name,
                effective_embed,
                top_k,
                provider_name=provider_name,
                embed_provider_name=identity.provider,
                similarity_cutoff=similarity_cutoff,
                prompt_mode=prompt_mode,
                temperature=temperature,
                top_k_explicit=top_k_explicit,
                bm25_retriever=bm25_retriever,
                reranker=reranker,
                custom_system_prompt=custom_system_prompt,
                mmr_enabled=mmr_enabled,
                mmr_lambda=mmr_lambda,
                query_expansion=query_expansion,
                num_queries=num_queries,
                rerank_pool_ceiling=rerank_pool_ceiling,
                wikilink_graph=wikilink_graph,
                wikilink_expansion=wikilink_expansion,
                thesaurus=thesaurus,
                thesaurus_expansion=thesaurus_expansion,
                primer_enabled=primer_enabled,
                stage_cb=_stage,
            )
        return engine, _stage

    def stream_chat(
        self,
        message: str,
        llm_name: str,
        embed_name: str,
        top_k: int = 6,
        provider_name: str = "ollama",
        similarity_cutoff: float = 0.25,
        prompt_mode: str = "strict",
        temperature: float | None = None,
        top_k_explicit: bool = False,
        hybrid_enabled: bool = False,
        reranker_enabled: bool = False,
        reranker_model: str = "",
        custom_system_prompt: str = "",
        mmr_enabled: bool = False,
        mmr_lambda: Optional[float] = None,
        query_expansion: bool = False,
        num_queries: int = 1,
        rerank_pool_ceiling: Optional[int] = None,
        wikilink_expansion: bool = False,
        thesaurus_expansion: bool = False,
        primer_enabled: bool = False,
        stage_cb: Optional[Callable[[str], None]] = None,
    ):
        """Single-shot RAG: retrieve, then return a streaming LLM response.

        The public chat entrypoint for non-agent vault chat (the agent's RAG
        fallback also calls it). Every keyword is a query-time knob already resolved
        body→config→default by the route's ``_resolve_chat_params`` — none of them
        touches the index, so they are all reindex-free. The engine build (and its
        concurrency-ordering rationale) lives in :meth:`_build_engine`; retrieval
        runs under ``_index_mutation_lock`` (so it can't race an insert) while the
        LLM token stream runs lock-free after retrieval returns. Always retrieves
        with the index's *own* recorded embed model (``_effective_embed_name``) so a
        config-only model switch can't fuse two vector spaces. ``stage_cb`` surfaces
        stage labels as SSE ``{info}`` frames. Returns a streaming response object
        (``.response_gen``).
        """
        started_at = time.monotonic()
        engine, _stage = self._build_engine(
            llm_name=llm_name,
            embed_name=embed_name,
            top_k=top_k,
            provider_name=provider_name,
            similarity_cutoff=similarity_cutoff,
            prompt_mode=prompt_mode,
            temperature=temperature,
            top_k_explicit=top_k_explicit,
            hybrid_enabled=hybrid_enabled,
            reranker_enabled=reranker_enabled,
            reranker_model=reranker_model,
            custom_system_prompt=custom_system_prompt,
            mmr_enabled=mmr_enabled,
            mmr_lambda=mmr_lambda,
            query_expansion=query_expansion,
            num_queries=num_queries,
            rerank_pool_ceiling=rerank_pool_ceiling,
            wikilink_expansion=wikilink_expansion,
            thesaurus_expansion=thesaurus_expansion,
            primer_enabled=primer_enabled,
            stage_cb=stage_cb,
        )

        # AP-09 staged retrieval. Prepare performs bounded query rewrite,
        # embedding, and request-local helper setup without touching mutable
        # index state. The mutation lock then protects one coherent vector /
        # BM25 / docstore read epoch; read_snapshot deep-detaches every bounded
        # candidate before release. Reranking and synthesis use only those
        # detached nodes, so they cannot race insert/delete and no slow model or
        # network call holds the writer lock. Any prepare/read/finish failure or
        # cancellation publishes no partial index state and is retry-safe. The
        # persisted index format and lazy-load/migration contract are unchanged.
        _stage("Retrieving context…")
        if callable(getattr(type(engine), "prepare", None)):
            prepared = engine.prepare(message)
            self._acquire_retrieval_lock()
            with span("index.retrieval.hold"):
                try:
                    nodes = engine.read_snapshot(prepared)
                finally:
                    self._index_mutation_lock.release()
            response = engine.finish_query(prepared, nodes)
        else:  # compatibility for injected/custom legacy engine adapters
            self._acquire_retrieval_lock()
            with span("index.retrieval.hold"):
                try:
                    response = engine.query(message)
                finally:
                    self._index_mutation_lock.release()

        # Usage visibility (axis vi): the ONLINE path returns
        # `_OnlineStreamingResponse`, which records usage itself. The LOCAL path
        # streams straight out of LlamaIndex and never touches usage_tracker, so
        # /api/usage never saw local vault chats. Wrap the local response's
        # token generator to record an (estimated) usage row on completion —
        # local models are unpriced ($0), so this is a visibility record.
        from core.config import is_online_provider
        if not is_online_provider(provider_name) and hasattr(response, "response_gen"):
            self._wrap_local_chat_usage(
                response, message, provider_name, llm_name, started_at=started_at,
            )
        return response

    @staticmethod
    def _wrap_local_chat_usage(response, message, provider_name, model, *, started_at=None):
        """Replace `response.response_gen` with a usage-recording passthrough.

        Estimates input tokens from the retrieved source-node text plus the
        query, accumulates the streamed answer for the output estimate, and
        records once the stream ends (success or error). Best-effort — never
        alters the tokens the caller sees. Latency covers manager entry through
        stream termination, including retrieval and eager synthesis. It is a
        server workflow duration, not isolated provider latency or UI paint time.
        """
        import time as _time

        from core.llm.usage import estimate_tokens, record_local_usage

        start = _time.monotonic() if started_at is None else started_at
        inner = response.response_gen
        # Estimate prompt size from what actually fed the model: retrieved
        # context + the question. The exact assembled prompt is internal to
        # LlamaIndex, so this is a floor, not an exact count.
        ctx_tokens = 0
        try:
            for node in (getattr(response, "source_nodes", None) or []):
                ctx_tokens += estimate_tokens(node.node.get_content())
        except Exception:  # noqa: BLE001 — estimation must not break chat
            ctx_tokens = 0
        input_tokens = ctx_tokens + estimate_tokens(message or "")

        def _gen():
            parts: list[str] = []
            ok = True
            err = ""
            try:
                for tok in inner:
                    if tok:
                        parts.append(tok)
                    yield tok
            except GeneratorExit:
                # Client abort: recorded as a labelled failure, never a
                # success — an abandoned stream must not inflate the success
                # counts in /api/usage.
                ok = False
                err = "aborted"
                raise
            except Exception:
                ok = False
                err = "stream_error"
                raise
            finally:
                record_local_usage(
                    provider_name, model,
                    input_tokens=input_tokens,
                    output_text="".join(parts),
                    latency_ms=int((_time.monotonic() - start) * 1000),
                    stream=True,
                    success=ok,
                    error_category="" if ok else err,
                    # JSONL attribution only — prompt capture is untouched
                    # (record_local_usage never runs the factory capture seam).
                    workflow="vault_rag",
                )

        response.response_gen = _gen()

    def retrieve(
        self,
        message: str,
        *,
        llm_name: str,
        embed_name: str,
        top_k: int = 6,
        provider_name: str = "ollama",
        similarity_cutoff: float = 0.25,
        top_k_explicit: bool = False,
        hybrid_enabled: bool = False,
        reranker_enabled: bool = False,
        reranker_model: str = "",
        mmr_enabled: bool = False,
        mmr_lambda: Optional[float] = None,
        query_expansion: bool = False,
        num_queries: int = 1,
        rerank_pool_ceiling: Optional[int] = None,
        wikilink_expansion: bool = False,
        thesaurus_expansion: bool = False,
        primer_enabled: bool = False,
        stage_cb: Optional[Callable[[str], None]] = None,
    ):
        """Retrieve evidence chunks for *message* without invoking the LLM.

        Mirrors the retrieval phase of :meth:`stream_chat`: lazy-loads
        the saved index, optionally builds BM25 + reranker, then returns
        the retrieved :class:`RetrievedChunk` list. Designed for the
        agent loop's ``vault_search`` tool so a single agent turn can
        issue several searches without paying ``stream_chat``'s LLM
        round-trip per query.

        Holds :attr:`_index_mutation_lock` only for immutable snapshot reads,
        matching :meth:`stream_chat`'s discipline.
        """
        engine, _stage = self._build_engine(
            llm_name=llm_name,
            embed_name=embed_name,
            top_k=top_k,
            provider_name=provider_name,
            similarity_cutoff=similarity_cutoff,
            top_k_explicit=top_k_explicit,
            hybrid_enabled=hybrid_enabled,
            reranker_enabled=reranker_enabled,
            reranker_model=reranker_model,
            mmr_enabled=mmr_enabled,
            mmr_lambda=mmr_lambda,
            query_expansion=query_expansion,
            num_queries=num_queries,
            rerank_pool_ceiling=rerank_pool_ceiling,
            wikilink_expansion=wikilink_expansion,
            thesaurus_expansion=thesaurus_expansion,
            primer_enabled=primer_enabled,
            stage_cb=stage_cb,
            log_label="Vault retrieve",
        )

        _stage("Retrieving context…")
        if callable(getattr(type(engine), "prepare", None)):
            prepared = engine.prepare(message)
            self._acquire_retrieval_lock()
            with span("index.retrieval.hold"):
                try:
                    nodes = engine.read_snapshot(prepared)
                finally:
                    self._index_mutation_lock.release()
            return engine.finish_retrieve(prepared, nodes)
        self._acquire_retrieval_lock()
        with span("index.retrieval.hold"):
            try:  # compatibility for injected/custom legacy engine adapters
                return engine.retrieve(message)
            finally:
                self._index_mutation_lock.release()

    def read_note(
        self,
        rel_path: str,
        *,
        max_chars: int = 32000,
        time_budget_s: Optional[float] = None,
    ) -> tuple[str, bool]:
        """Read the full text of a vault note (.md or .pdf) by vault-relative path.

        Path-safe: rejects traversal outside the configured vault root,
        any path under :data:`OBSIDIAN_EXCLUDED_DIR_NAMES` or the user's
        ``vault_exclude_dirs``, and any extension not in
        :data:`VAULT_MD_EXTS` ∪ :data:`VAULT_BINARY_EXTS`. PDFs are
        served from :meth:`_pdf_cache_file` when present; uncached PDFs
        fall back to a fresh extract bounded by
        :data:`EXTRACT_MAX_PAGES_PER_CALL` (no vision OCR — that is an
        indexing-time operation).

        Returns ``(text, truncated)``. ``truncated`` is True when the
        file exceeded ``max_chars`` or, for PDFs, when the bounded
        extract did not consume the whole file.

        ``time_budget_s`` (improvement plan 2026-07-04, item 2.4): the agent's
        remaining wall-clock budget. The fresh-extract fallback for an
        UNCACHED PDF is the one code path here whose cost is minutes, not
        milliseconds — the agent loop's deadline bounds only LLM calls, so a
        near-expired turn used to start a 1000-page in-process extraction it
        could never use. When a budget is given and it is below
        ``_FRESH_EXTRACT_MIN_BUDGET_S``, the fresh extract is REFUSED with an
        explanatory ``IOError`` (which the agent surfaces as a recoverable
        tool-error observation); cache and range-cache hits are served
        regardless (they are fast). ``None`` — every non-agent caller —
        keeps the legacy unbounded contract byte-identically.

        Raises:
            ValueError: invalid / disallowed path, unsupported extension.
            FileNotFoundError: path resolves outside or to a non-file.
            RuntimeError: vault path is not configured.
            IOError: read or extraction failure.
        """
        if not isinstance(rel_path, str) or not rel_path.strip():
            raise ValueError("rel_path must be a non-empty string")
        if "\x00" in rel_path:
            raise ValueError("rel_path contains a null byte")

        vault_path = self.get_vault_path()
        if not vault_path:
            raise RuntimeError("Vault path is not configured.")
        vault_root = Path(vault_path).resolve()

        try:
            candidate = (vault_root / rel_path).resolve()
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"Could not resolve path: {rel_path!r}") from exc

        try:
            rel_resolved = candidate.relative_to(vault_root)
        except ValueError:
            raise ValueError(f"Path is outside the vault: {rel_path!r}")

        if any(part in OBSIDIAN_EXCLUDED_DIR_NAMES for part in rel_resolved.parts):
            raise ValueError(f"Path is in an excluded directory: {rel_path!r}")

        cfg = load_config()
        user_excluded = self._normalised_excluded_dirs(
            vault_root, cfg.get("vault_exclude_dirs", []) or []
        )
        rel_posix = rel_resolved.as_posix()
        for excluded in user_excluded:
            if rel_posix == excluded or rel_posix.startswith(excluded + "/"):
                raise ValueError(f"Path is in a user-excluded directory: {rel_path!r}")

        if not candidate.is_file():
            raise FileNotFoundError(f"Not a file: {rel_path!r}")

        ext = candidate.suffix.lower()
        if ext not in VAULT_MD_EXTS and ext not in VAULT_BINARY_EXTS:
            raise ValueError(f"Unsupported extension: {ext!r}")

        if ext in VAULT_MD_EXTS:
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise IOError(f"Could not read {rel_path!r}: {exc}") from exc
            extract_truncated = False
        else:
            pdf_read = self._read_pdf_text(
                candidate, vault_root, max_chars, time_budget_s=time_budget_s)
            text, extract_truncated = pdf_read

        size_truncated = len(text) > max_chars
        if size_truncated:
            text = text[:max_chars]
        status = status_of(pdf_read) if ext in VAULT_BINARY_EXTS else ExtractionStatus(origin="note")
        return TextRead(text, size_truncated or extract_truncated, status)

    def _selected_pdf_payload(self, path: Path, vault_root: Path) -> Optional[dict]:
        """Read an explicitly activated immutable profile; never run extraction.

        No active pointer preserves legacy routing. Damaged/stale evidence emits
        a warning and falls back to the established cache/extractor rather than
        losing searchable content. Preparation alone never selects a profile.
        """
        from core.pdf_text_profile import selected_payload
        try:
            record = selected_payload(Path(OBSIDIAN_INDEX_DIR).parent / "pdf_text_profiles",
                                      vault_root, path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._emit(f"WARNING: PDF text profile refused for {path.name} "
                       f"({type(exc).__name__}); using legacy extraction/cache.")
            return None
        if record is not None and record["outcome"] != "processed":
            self._emit(f"WARNING: {path.name}: cached PDF text outcome "
                       f"{record['outcome']}; inspect its provenance manifest.")
        return record

    def _read_pdf_text(
        self,
        path: Path,
        vault_root: Path,
        char_budget: int,
        time_budget_s: Optional[float] = None,
        *,
        fresh_max_pages: Optional[int] = None,
        use_markitdown: bool = True,
        signature: Optional[dict] = None,
    ) -> tuple[str, bool]:
        """Return cached PDF text if present, otherwise a bounded fresh extract.

        Returns ``(text, truncated)``. ``truncated`` is True when the
        bounded fresh extract did not cover the whole document (page or
        char limit reached). Cache hits are never reported as truncated
        — the caller still applies its own ``max_chars`` cap.

        ``time_budget_s`` is a START-refusal floor only (item 2.4): a fresh
        extract is refused when the remaining budget is below
        ``_FRESH_EXTRACT_MIN_BUDGET_S``, but an extraction that does start is
        NOT time-bounded by it — callers that need a hard cost ceiling must
        bound by pages instead. ``fresh_max_pages`` caps a fresh extract to the
        document's first N pages (cache reads are unaffected), and
        ``use_markitdown=False`` disables the whole-file pdfminer fallback —
        together they are the real bound the tagging snippet/propose text
        source relies on.

        The persisted signature map is consulted (same as the indexer) so an
        unchanged large PDF skips the full content re-hash on every call.
        """
        try:
            rel = path.relative_to(vault_root).as_posix()
        except ValueError:
            rel = None
        # PERF (2026-09-02): a caller that already fingerprinted this file may
        # hand its signature in instead of paying for a second sha256.
        # Before: tagging/text_source.py::_cache_probe computed the full
        #   signature to locate the .notext marker, and this method computed
        #   it again, and once more on the fresh-extract path, which re-probes
        #   under its serialisation lock: three whole-file sha256 reads for
        #   one snippet, measured by counting _sha256_file calls. The persisted map spares an INDEXED
        #   PDF, so the cost lands on exactly the unindexed ones a snippet or
        #   propose reaches: measured on the maintainer's biblio directory
        #   (5 943 PDFs, p50 0.6 MB / p99 16.6 MB / max 114.5 MB), ~1 ms per
        #   hash at the median and ~130 ms at the maximum, paid two to three
        #   times per call (3 -> 1 hashes; ~2 ms saved at the median size,
        #   ~260 ms on the 114.5 MB outlier).
        # Change: an optional *signature*, re-validated against a FRESH stat
        #   before it is trusted, so the hash happens once.
        # Safe because:
        #   The signature is used only when the file's current size and
        #   mtime_ns still equal the ones it was computed from; any mismatch
        #   (or no signature at all) falls through to the original
        #   _pdf_file_signature call, so the cache path and the digest a
        #   cache/marker file is named after are identical either way.
        # Preserves: "The dataless and size gates still run on a fresh stat."
        # Lock/thread contract: unchanged — no lock is taken here.
        # Pinned by: test_tagging.py::TestSnippetHashesOnce (red before the
        #   change: "3 != 1" whole-file hashes for one snippet read).
        try:
            sig = self._reuse_pdf_signature(path, signature)
            if sig is None:
                sig = self._pdf_file_signature(
                    path, self._persisted_pdf_signatures(), rel)
        except OSError as exc:
            raise IOError(f"Could not stat PDF {path.name}: {exc}") from exc
        selected = self._selected_pdf_payload(path, vault_root)
        if selected is not None:
            text = selected["text"]
            limited = len(text) > char_budget
            pages = tuple(span["physical_page"] for span in selected["pages"]
                          if span["physical_page"] is not None)
            return TextRead(text[:char_budget], limited, ExtractionStatus(
                completeness="unknown", origin="practical_pdf_text", profile=selected["recipe"],
                source_sha256=selected["source_sha256"], attempted_pages=pages,
                completed_pages=pages if selected["outcome"] == "processed" else (),
                warnings=("Automatic cached text; processing does not certify transcription accuracy.",
                          f"Cached outcome: {selected['outcome']}.")))
        cache_file = self._pdf_cache_file(vault_root, sig)
        legacy_cache = self._legacy_pdf_cache_file(vault_root, sig)
        # Bounded read (P8): callers cap at char_budget anyway, so bytes past
        # budget+1 were pure peak-RAM waste on multi-MB grandfathered caches.
        cached = self._read_first_text_file([cache_file, legacy_cache],
                                            max_chars=char_budget)
        if cached:
            return TextRead(cached, False, ExtractionStatus(origin="legacy_cache", source_sha256=sig["sha256"],
                warnings=("Legacy cache completeness and page provenance are unknown.",)))

        # Large PDFs indexed by the per-range loader have no whole-file cache;
        # stitch their range caches back together instead of re-extracting.
        range_text, range_truncated, range_found = self._read_pdf_range_caches(
            path, vault_root, sig, char_budget
        )
        if range_found:
            return TextRead(range_text, range_truncated, ExtractionStatus(origin="legacy_range_cache", source_sha256=sig["sha256"],
                warnings=("Legacy range labels do not certify extracted page coverage.",)))

        # Item 2.4 budget floor: a fresh extract is the only expensive branch
        # (everything above is cache reads). Refusing to START one on a
        # nearly-exhausted budget is the whole protection — extraction cannot
        # be interrupted mid-flight, so a check here is the last opportunity.
        # Residual (documented): an extract started with budget above the
        # floor can still overrun it; the floor + the loop's per-dispatch
        # deadline gate bound how stale such a start can be.
        if time_budget_s is not None and time_budget_s < _FRESH_EXTRACT_MIN_BUDGET_S:
            raise IOError(
                f"{path.name} is not in the PDF text cache and the remaining "
                f"time budget ({time_budget_s:.0f}s) is too small for a fresh "
                f"extraction. It will be cached by the next indexing run; "
                f"try vault_search snippets instead."
            )
        if fresh_max_pages is not None and fresh_max_pages > 0:
            end_page = min(fresh_max_pages, EXTRACT_MAX_PAGES_PER_CALL)
        else:
            end_page = EXTRACT_MAX_PAGES_PER_CALL
        try:
            sections = extract_structured_from_pdf(
                str(path),
                char_budget=char_budget,
                end_page=end_page,
                use_markitdown=use_markitdown,
            )
        except Exception as exc:
            raise IOError(f"PDF extraction failed for {path.name}: {exc}") from exc
        text = sections.full_text or ""
        # Extraction failures/budgets are reported by ArticleSections; a
        # caller requesting only the first pages also limits document coverage.
        page_bounded = (
            fresh_max_pages is not None
            and int(getattr(sections, "page_count", 0) or 0) > end_page
        )
        truncated = (bool(getattr(sections, "truncated", False))
                     or len(text) >= char_budget or page_bounded)
        return TextRead(text, truncated, getattr(sections, "extraction_status", None))

    def _read_pdf_range_caches(
        self,
        path: Path,
        vault_root: Path,
        signature: dict,
        char_budget: int,
    ) -> tuple[str, bool, bool]:
        """Stitch a large PDF's per-range cache files back into one text.

        Returns ``(text, truncated, found)``; ``found`` is False when no
        range cache exists for this digest (caller falls through to a fresh
        bounded extract).  ``truncated`` is True when the char budget cut
        the stitch short, or when the cached ranges do not cover the whole
        document (a cancelled indexing run leaves a partial range set —
        served as-is rather than blocking a read_note call on a fresh
        multi-hour extraction).
        """
        cache_dir = self._pdf_cache_file(vault_root, signature).parent
        digest = str(signature["sha256"])
        try:
            candidates = sorted(cache_dir.glob(f"{digest}-p*.txt"))
        except OSError:
            return "", False, False
        # Zero-padded range suffixes make sorted() == page order; the regex
        # both validates the suffix shape and recovers the page bounds.
        ranges: list[tuple[int, int, Path]] = []
        for candidate in candidates:
            m = self._PDF_RANGE_CACHE_RE.search(candidate.name)
            if m:
                ranges.append((int(m.group(1)), int(m.group(2)), candidate))
        if not ranges:
            return "", False, False

        parts: list[str] = []
        total = 0
        budget_hit = False
        covered_to = 0
        gap = False
        for start, end, candidate in ranges:
            if total >= char_budget:
                budget_hit = True
                break
            try:
                part = candidate.read_text(encoding="utf-8")
            except OSError:
                continue
            if part:
                # A sparse range set (one cache file failed to save or
                # read while later ranges succeeded) must not pass as
                # full coverage just because the last range reaches the
                # final page.
                if start > covered_to:
                    gap = True
                # Same separator the old concatenating loader used between
                # ranges, so stitched output matches what a whole-file cache
                # of the same PDF would have contained.
                parts.append(part)
                total += len(part)
                covered_to = max(covered_to, end)
        text = "\n\n".join(parts)
        if not text:
            return "", False, False

        truncated = budget_hit or gap or len(text) >= char_budget
        if not truncated:
            # Partial range set (cancelled run) ⇒ report truncated so the
            # agent tool surfaces that the tail of the document is missing.
            try:
                truncated = covered_to < get_pdf_page_count(str(path))
            except Exception:
                # Unreadable page count must not fail a cache-served read.
                pass
        return text, truncated, True

    def get_index_warning(self) -> str:
        # Goes through the stat-keyed meta cache: this runs on every status
        # poll alongside get_status/is_partial_index, which used to cost
        # three independent open+parse cycles of the same small file.
        #
        # Item 4.6: Switch to load_config_readonly() to avoid deep-copying the
        # config dictionary on this ~1 Hz UI status poll hot path.
        # Defect/Scenario: deep-copying the dict on high-frequency status polls
        # wastes CPU and generates garbage collection overhead for read-only access.
        # Safe: get_index_warning only reads the "embed" model preference and never
        # mutates it; load_config_readonly() returns a MappingProxyType over the
        # cache that guarantees value parity.
        # Invariant: get_index_warning returns the same mismatch warning message
        # without mutating or deep-copying config state.
        meta = self._read_index_meta()
        if meta is None:
            return ""
        cfg = load_config_readonly()
        current_embed = cfg.get("embed", "")
        indexed_embed = meta.get("embed", "")
        if indexed_embed and current_embed and indexed_embed != current_embed:
            return f"Embedding model mismatch. Indexed with {indexed_embed}; current selection is {current_embed}. Re-index the vault."
        return ""

    def is_partial_index(self) -> bool:
        meta = self._read_index_meta()
        return bool(meta.get("partial", False)) if meta is not None else False

    def pause_indexing(self) -> bool:
        with self._status_lock:
            if self._index_state not in ("running", "scanning", "embedding"):
                return False
            self._pause_requested = True
        self._stop_event.set()
        return True

    def cancel_indexing(self) -> bool:
        with self._status_lock:
            self._pause_requested = False
            index_epoch = self._index_op_epoch
        self._stop_event.set()
        # Release ONLY this indexing run's own acquisition.
        #
        # This used to be an unconditional `force_release()`, and
        # `POST /api/obsidian/cancel` is ungated — it neither checks the index
        # state nor who holds the lock, and the Cancel button stays on screen
        # after a pause, when no indexer is running at all. So a Cancel pressed
        # while a Note Refactor Apply / a tagging write / a docgen note write
        # held the lock simply handed it away: a second vault writer could then
        # be admitted, and since each writer loads its own copy of the restore
        # manifest, the last `journal.save` silently erased the other's op
        # records — notes already rewritten, no longer restorable.
        #
        # Being strict leaks nothing: the indexer's own `finally`
        # (api/routes/vault.py::run_index) releases when the aborted run
        # unwinds, `_stop_event` above is what actually stops it, and a truly
        # dead holder is still reclaimed by the lock's TTL. `force_release`
        # stays for the operator escape hatch (`/api/reset`), which is the
        # documented use in `RagOperationLock.force_release`.
        was_held = False
        if index_epoch:
            held_before = self._op_lock.is_held
            self._op_lock.release(index_epoch)
            was_held = held_before and not self._op_lock.is_held
        # PERF (2026-09-02): the meta edit leaves _status_lock, and runs FIRST.
        #   Before:    this read-modify-write of obsidian_meta.json ran inside
        #              _status_lock, while the indexing thread's
        #              _write_index_meta held only the rw write lock — two
        #              writers with no shared serialisation. A cancel that read
        #              before the run's final persist and wrote after it
        #              silently reverted `phase`, `has_vector_data` and
        #              `embed_provider`. The last of those is the costly one:
        #              the incremental gate REFUSES a run whose recorded local
        #              backend differs and adopts silently when the field is
        #              empty, so reverting it to "" turns a refusal into an
        #              unnoticed adoption and mixes two vector spaces in one
        #              index. It was also file I/O with an fsync inside the very
        #              lock the 2026-07-22 P5 audit removed I/O from.
        #   Change:    the RMW moves under the dedicated _meta_write_lock (shared
        #              with _write_index_meta, so the pair is atomic against the
        #              indexer) and happens BEFORE the in-memory flip; only the
        #              flip stays under _status_lock.
        #   Safe because:
        #              ordering the disk clear first is what preserves the
        #              original rationale WITHOUT holding both locks. In the new
        #              window the on-disk meta already says partial=False while
        #              _index_state is still running/paused_* — and get_status
        #              returns a non-idle state early, so lazy recovery never
        #              runs. The forbidden observation (in-memory idle + on-disk
        #              partial) is now unreachable in either order. If the
        #              indexer's final write wins the lock first, this read sees
        #              partial=False and writes nothing at all.
        #   Preserves: "a concurrent get_status() cannot observe the in-memory
        #              idle while the on-disk meta still says partial=True".
        #              Unknown meta keys still survive (the RMW is unchanged).
        #   Lock/thread contract:
        #              was: _status_lock held across a read + fsynced write.
        #              now: _meta_write_lock for the RMW, released, then
        #              _status_lock for two field assignments. Never both at
        #              once, so no ordering edge is added.
        #   Pinned by: test_vault_regressions.py::TestCancelDoesNotClobberFinalMeta
        meta_path = os.path.join(OBSIDIAN_INDEX_DIR, "obsidian_meta.json")
        if os.path.exists(meta_path):
            try:
                with self._meta_write_lock:
                    with open(meta_path) as f:
                        prev = json.load(f)
                    if prev.get("partial"):
                        prev["partial"] = False
                        prev["phase"] = "idle"
                        prev["indexed_at"] = datetime.now(timezone.utc).isoformat()
                        self._write_index_meta_locked(meta_path, prev)
            except Exception:
                logger.debug("cancel_indexing: failed to clear partial meta", exc_info=True)
        with self._status_lock:
            if self._index_state in (
                "running",
                "scanning",
                "embedding",
                "paused",
                "paused_scan",
                "paused_partial",
            ):
                self._index_state = "idle"
                self._current_phase = "idle"
        return was_held

    def request_stop(self) -> None:
        self._stop_event.set()

    def register_index_thread(self, thread: threading.Thread) -> None:
        """Record the background indexing thread so callers can wait for it."""
        self._index_thread = thread

    def wait_for_indexing(self, timeout: float = 30.0) -> bool:
        """Join the active indexing thread (if any) up to *timeout* seconds.

        Returns True when no indexing is in flight at exit, False if the
        timeout expired with the thread still running.
        """
        t = self._index_thread
        if t is None or not t.is_alive():
            return True
        t.join(timeout)
        return not t.is_alive()

    def _set_prewarm(self, status: str, message: str = "", generation: int | None = None) -> None:
        """Update prewarm status.  When ``generation`` is provided, the
        update is silently dropped if it does not match the current
        generation — this prevents a late callback from an in-flight
        prewarm that was reset out from under it from clobbering the
        post-reset state.
        """
        with self._prewarm_lock:
            if generation is not None and generation != self._prewarm_generation:
                return
            self._prewarm_status = status
            self._prewarm_message = message

    def get_prewarm_state(self) -> tuple[str, str]:
        with self._prewarm_lock:
            return self._prewarm_status, self._prewarm_message

    def reset_prewarm(self) -> None:
        """Reset prewarm tracking so a fresh prewarm() can run.

        Called after /api/reset and after a vault switch — the previous
        warm-state no longer corresponds to what's on disk.  Bumps the
        generation token so any still-running prewarm thread cannot
        write its terminal status into the now-stale slot.
        """
        with self._prewarm_lock:
            self._prewarm_generation += 1
            self._prewarm_status = "idle"
            self._prewarm_message = ""
            self._prewarm_started = False

    def prewarm(self) -> None:
        """Pre-load the vector index, BM25 retriever, and cross-encoder
        reranker on a background thread so the first chat after launch
        does not pay the cold-start cost inside the chat-token timeout.

        Idempotent: a second call while one is running, or after ``ready``
        / ``skipped`` has been reached, returns immediately.  Errors mark
        the status as ``error`` and return — chat will still attempt a
        lazy load along the existing path, surfacing the same error to
        the user there.

        Honours ``vault_hybrid_enabled`` and ``vault_reranker_enabled``
        from config so we only warm components the user has turned on.
        """
        with self._prewarm_lock:
            if self._prewarm_started:
                return
            self._prewarm_started = True
            generation = self._prewarm_generation

        try:
            cfg = load_config()
            # Opt-out knob: the user chose to defer the multi-GB index /
            # BM25 / reranker load to the first chat (vault.js treats
            # "skipped" as terminal — banner hidden, Send enabled — and
            # stream_chat lazy-loads along the existing path).
            if not bool(cfg.get("vault_prewarm_enabled", True)):
                self._set_prewarm(
                    "skipped", "Prewarm disabled in settings.", generation=generation
                )
                return

            docstore_path = os.path.join(OBSIDIAN_INDEX_DIR, "docstore.json")
            if not os.path.exists(docstore_path):
                self._set_prewarm("skipped", "No vault index found on disk.", generation=generation)
                return

            self._set_prewarm("loading_index", "Loading saved vault index…", generation=generation)
            embed_name = cfg.get("embed", DEFAULT_EMBED)
            provider_name = cfg.get("provider", "ollama")

            # Walk the same path stream_chat uses lazily, under the same
            # write lock.  If indexing has already published self._index,
            # the inner None-check short-circuits.
            with self._rw_lock.write_lock():
                if self._index is None:
                    meta = self._read_index_meta()
                    try:
                        self._assert_index_matches_vault(meta)
                    except RuntimeError as exc:
                        # Not an integrity failure — the index is healthy, it
                        # just belongs to another vault. Surface via the
                        # prewarm banner; chat's own lazy load re-raises the
                        # same message.
                        self._set_prewarm("error", str(exc), generation=generation)
                        return
                    backend = self._resolve_existing_backend(meta)
                    try:
                        self._validate_persisted_index_files(
                            OBSIDIAN_INDEX_DIR, full=False, backend=backend,
                            check_promotion_marker=True,
                        )
                    except RuntimeError as exc:
                        self._index_integrity_error = str(exc)
                        self._set_prewarm("error", f"Integrity check failed: {exc}", generation=generation)
                        return
                    try:
                        identity = self._query_embedding_identity(provider_name, embed_name, logger.warning)
                        provider = get_provider(identity.provider)
                        embed_model = provider.get_embedding(identity.model)
                        self._replace_index(self._build_index_for_backend(
                            fresh=False, backend=backend, embed_model=embed_model
                        ))
                        self._index_integrity_error = ""
                    except json.JSONDecodeError as exc:
                        self._index_integrity_error = (
                            f"Index checkpoint is corrupt or incomplete "
                            f"({exc.msg} at line {exc.lineno}, column {exc.colno})."
                        )
                        self._set_prewarm("error", self._index_integrity_error, generation=generation)
                        return
                    except Exception as exc:
                        self._set_prewarm("error", f"Index load failed: {exc}", generation=generation)
                        return

            hybrid_enabled = bool(cfg.get("vault_hybrid_enabled", True))
            reranker_enabled = bool(cfg.get("vault_reranker_enabled", True))
            reranker_model = (cfg.get("vault_reranker_model") or "").strip()
            warm_top_k = 6  # nominal — top_k is re-tuned per query

            if hybrid_enabled:
                self._set_prewarm("building_bm25", "Building lexical BM25 retriever…", generation=generation)
                try:
                    self._get_bm25_retriever(top_k=warm_top_k)
                except Exception as exc:
                    logger.warning("Prewarm BM25 build failed: %s", exc)

            if reranker_enabled and reranker_model:
                self._set_prewarm("loading_reranker", "Loading cross-encoder reranker…", generation=generation)
                try:
                    self._get_reranker(model_name=reranker_model, top_n=warm_top_k)
                except Exception as exc:
                    logger.warning("Prewarm reranker load failed: %s", exc)

            # The graph can materialize a large docstore snapshot. Identify
            # that work separately instead of leaving the reranker label up.
            # Only enabled expansion pays this cost.
            if bool(cfg.get("vault_wikilink_expansion", False)):
                self._set_prewarm(
                    "building_wikilinks", "Preparing note-link graph…", generation=generation,
                )
                try:
                    self._get_wikilink_index()
                except Exception as exc:
                    logger.warning("Prewarm wikilink graph build failed: %s", exc)

            self._set_prewarm("ready", "Vault is ready.", generation=generation)
        except Exception as exc:
            logger.exception("Prewarm failed unexpectedly.")
            self._set_prewarm("error", f"Prewarm failed: {exc}", generation=generation)

    def cleanup(self) -> None:
        self._stop_event.set()
        with self._rw_lock.write_lock():
            self._replace_index(None)
        with self._status_lock:
            self._index_state = "idle"
        # Drop the BM25 cache — its nodes referenced the just-cleared index.
        # The reranker is index-independent and is kept so /api/reset does
        # not trigger another model download for the next vault.
        self._invalidate_retrieval_caches(persistent=False)
        # Drop the thesaurus cache too: a vault switch points at different
        # curated files (or none). Mtime-keyed, so a stale object would
        # otherwise survive a switch to a same-signature file by coincidence.
        with self._thesaurus_build_lock:
            self._thesaurus = None
            self._thesaurus_cache_key = None
        # A vault switch points at a different index dir; the memo is keyed on
        # (path, size, mtime_ns) so it could not be wrong, but holding a whole
        # manifest for a vault we just left is pointless.
        self._invalidate_manifest_fallback_memo()
        self.reset_prewarm()
        # Hygiene: the just-dropped index/BM25 objects sit in reference
        # cycles (LlamaIndex stores back-reference their context); collect
        # now so /api/reset actually releases them.
        gc.collect()

obsidian_manager = ObsidianVaultManager()
