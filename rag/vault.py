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
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Any
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
from core.utils import ReaderWriterLock, RagOperationLock, log_storage_deletion
from core.providers import get_provider
from rag.lancedb_store import (
    VECTOR_BACKEND_LANCEDB,
    VECTOR_BACKEND_SIMPLE,
    compact_lancedb_vector_store,
    lancedb_available,
    lancedb_dir,
    lancedb_table_count,
    make_lancedb_vector_store,
    lancedb_list_doc_ids,
    lancedb_delete_ids,
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

# BM25 sidecar: the built retriever is persisted in bm25s' on-disk format
# under OBSIDIAN_INDEX_DIR so later launches mmap the score arrays and serve
# the corpus lazily from JSONL instead of re-tokenising the entire docstore
# (minutes on a large vault) and holding a second full copy of all vault
# text in RAM.  Lives inside the index dir on purpose: /api/reset's rmtree
# (with deletion audit) and the version-bump archive cover it automatically,
# while _persist_index_checkpoint promotes only _LLAMAINDEX_PERSIST_FILES
# and therefore never touches it.
_BM25_SIDECAR_DIRNAME = "bm25_index"
_BM25_SIDECAR_META_FILENAME = "sidecar_meta.json"

# On-disk sidecar for the wikilink note→note graph — the BM25 sidecar's
# sibling (same placement rationale: inside the index dir so /api/reset and
# the version-bump archive cover it, and _persist_index_checkpoint never
# touches it).  A single atomic JSON file rather than a dir + meta-last
# dance: the graph serializes to one document and _write_json_atomic makes
# a torn write impossible, so no separate meta file is needed.
_WIKILINK_SIDECAR_FILENAME = "wikilink_sidecar.json"


# Obsidian wikilinks: ![[image.png]], [[note]], [[note|alias]], [[note#heading]].
# Captures the target before any "|" alias or "#"/"^" suffix.
_OBSIDIAN_WIKILINK_RE = re.compile(r"!?\[\[([^|\]\n]+?)(?:\|[^\]\n]*)?\]\]")

# Inline markdown links: [label](path).  Captures the URL/path part.
_INLINE_LINK_RE = re.compile(r"\[[^\]\n]*\]\(([^)\n]+)\)")

# Schemes we never resolve as filesystem attachments.
_NON_ATTACHMENT_SCHEMES = ("http://", "https://", "mailto:", "ftp://", "data:")


def _closest_note_by_dir(candidates: list[str], note_dir: str) -> str:
    """Deterministically pick among same-basename note candidates for an
    ambiguous bare wikilink: prefer the candidate sharing the longest
    directory prefix with the linking note, then fewest path segments, then
    lexicographic order.

    Mirrors ``ObsidianVaultManager._pick_closest_attachment``'s tie-break but
    operates on vault-relative strings (no ``Path`` / filesystem), so the
    note→note graph build stays FS-free.
    """
    note_parts = [p for p in note_dir.split("/") if p]

    def shared_prefix(rel: str) -> int:
        # Compare directory components only (drop the trailing filename
        # segment) so a shared *folder* path drives locality, not a
        # coincidental basename match.
        dir_parts = rel.split("/")[:-1]
        n = 0
        for a, b in zip(note_parts, dir_parts):
            if a != b:
                break
            n += 1
        return n

    return sorted(candidates, key=lambda r: (-shared_prefix(r), r.count("/"), r))[0]


class _WikilinkGraph:
    """Immutable note→note adjacency derived from the vault docstore.

    Built once per indexing run (cached by docstore size) from the ``.md``
    nodes' link text — never from the filesystem — so it adds no reindex and
    no re-embedding.  ``outbound`` holds the notes a note links TO;
    ``backlinks`` the notes that link INTO it; ``neighbors`` is their
    deterministic union (consumed by the Phase-2 expansion retriever).
    ``node_ids_for`` maps a note's vault-relative ``source`` to the docstore
    node ids of its chunks so neighbours can be fetched without re-querying
    the vector store.
    """

    __slots__ = ("_forward", "_backward", "_note_to_node_ids")

    def __init__(
        self,
        forward: dict[str, set[str]],
        backward: dict[str, set[str]],
        note_to_node_ids: dict[str, list[str]],
    ) -> None:
        self._forward = forward
        self._backward = backward
        self._note_to_node_ids = note_to_node_ids

    def outbound(self, note: str) -> list[str]:
        """Notes *note* links to, sorted for determinism."""
        return sorted(self._forward.get(note, ()))

    def backlinks(self, note: str) -> list[str]:
        """Notes that link into *note*, sorted for determinism."""
        return sorted(self._backward.get(note, ()))

    def neighbors(self, note: str) -> list[str]:
        """Union of outbound links and backlinks for *note* (self excluded)."""
        merged = set(self._forward.get(note, ())) | set(self._backward.get(note, ()))
        merged.discard(note)
        return sorted(merged)

    def node_ids_for(self, note: str) -> list[str]:
        """Docstore node ids of *note*'s chunks (empty if *note* is unknown)."""
        return list(self._note_to_node_ids.get(note, ()))

    @property
    def note_count(self) -> int:
        return len(self._note_to_node_ids)

    @property
    def edge_count(self) -> int:
        return sum(len(targets) for targets in self._forward.values())

    def to_payload(self) -> dict:
        """JSON-serializable adjacency for the on-disk sidecar.

        Backlinks are deliberately omitted — they are a pure inversion of
        ``forward`` and are recomputed by ``from_payload``, so the two maps
        can never desync on disk.  Target sets are sorted for deterministic
        output (byte-stable sidecar across rebuilds of the same graph).
        """
        return {
            "forward": {s: sorted(t) for s, t in self._forward.items()},
            "note_to_node_ids": {
                s: list(ids) for s, ids in self._note_to_node_ids.items()
            },
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "_WikilinkGraph":
        """Rebuild a graph from ``to_payload`` output (raises on bad shape)."""
        forward_raw = payload["forward"]
        ids_raw = payload["note_to_node_ids"]
        if not isinstance(forward_raw, dict) or not isinstance(ids_raw, dict):
            raise ValueError("wikilink sidecar payload malformed")
        forward = {
            str(source): {str(t) for t in (targets or ())}
            for source, targets in forward_raw.items()
        }
        backward: dict[str, set[str]] = {}
        for source, targets in forward.items():
            for target in targets:
                backward.setdefault(target, set()).add(source)
        note_to_node_ids = {
            str(source): [str(i) for i in (ids or ())]
            for source, ids in ids_raw.items()
        }
        return cls(forward, backward, note_to_node_ids)


def _write_json_atomic(path: str, data: dict) -> None:
    """Write *data* as JSON to *path* atomically using a sibling temp file.

    Uses the same tempfile+os.replace() pattern as _save_pdf_cache_file so
    a crash or SIGTERM mid-write can never leave the file empty or corrupt.

    Adds a flush+fsync before the rename for crash *durability* (not just
    atomicity): this writer persists the index meta / indexed-materials manifest /
    PDF-signature cache — low-frequency (checkpoint cadence + final persist), so
    the fsync cost is negligible, and a torn meta on power loss could leave the
    on-disk index state unrecoverable. The per-image/per-PDF *description* cache
    (regenerable) uses the separate _atomic_write_text writer, which deliberately
    does NOT fsync — it runs per document during a multi-hour index and its loss
    only costs a re-extraction, not correctness.
    """
    dir_ = os.path.dirname(path) or "."
    os.makedirs(dir_, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".json")
    try:
        try:
            f = os.fdopen(fd, "w", encoding="utf-8")
        except Exception:
            os.close(fd)
            raise
        with f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _json_file_has_complete_tail(path: Path) -> bool:
    """Cheap EOF sanity check for large JSON files.

    This does not prove the whole file is valid JSON, but it catches the
    failure mode that prompted the repair tooling: a checkpoint truncated in
    the middle of a vector array.
    """
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return False
            f.seek(max(0, size - 4096))
            tail = f.read().rstrip()
        return tail.endswith(b"}")
    except OSError:
        return False


def _simple_vector_store_has_any_embedding(path: Path) -> bool:
    """Return True when SimpleVectorStore's embedding_dict is non-empty."""
    prefixes = (b'{"embedding_dict": {', b'{"embedding_dict":{')
    try:
        with path.open("rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    for prefix in prefixes:
        if head.startswith(prefix):
            pos = len(prefix)
            while pos < len(head) and head[pos:pos + 1] in b" \t\r\n":
                pos += 1
            return pos < len(head) and head[pos:pos + 1] != b"}"
    return False


# Chat-path bound on waiting for _index_mutation_lock (item 2.1): above the
# longest legitimate holder (a mid-run checkpoint re-serialising the store),
# below the SSE consumer stall floor (SSE_SINGLE_SHOT_FLOOR_S + margin) so the
# clean error beats the generic stall message. Patched down in tests.
_RETRIEVAL_LOCK_TIMEOUT_S = 120.0

# Minimum remaining agent budget (s) below which read_note refuses to START a
# fresh uncached-PDF extraction (item 2.4). Typical extracts finish in seconds;
# the floor only rejects starts that would mostly run past the turn's death.
_FRESH_EXTRACT_MIN_BUDGET_S = 20.0


def _iter_vault_scan_files(vault_root: Path, user_excluded_dirs: set):
    """Yield ``(path, apparent-rel)`` for every regular file under *vault_root*,
    pruning excluded directories at descent time (Track 5.4, 2026-07-04).

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
            continue  # unreadable dir: the old walk silently skipped it too
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


class ObsidianVaultManager:
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
        self._op_lock: RagOperationLock = RagOperationLock()
        self._stop_event: threading.Event = threading.Event()
        self._status_cb: Optional[Callable[[str], None]] = None
        self._index_state: str = "idle"  # "idle" | "running" | "paused" | "done" | "error"
        self._pause_requested: bool = False
        self._status_lock: threading.Lock = threading.Lock()
        self._messages_lock: threading.Lock = threading.Lock()
        self._status_messages: list[str] = []
        self._last_warning: str = ""
        self._index_integrity_error: str = ""
        self._skipped_image_count: int = 0
        # Item 2.8b: vault-relative sources whose READ failed this run (iCloud
        # dataless miss, transient I/O). Written only by _load_vault_documents
        # (single indexing thread), read by the stale-doc sweep so a
        # transiently unreadable file is never treated as deleted.
        self._scan_failed_sources: set[str] = set()
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
        # "loading_reranker", "ready", "skipped" (nothing on disk to warm),
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
            # Lazy recovery: on restart _index_state is always "idle" but a
            # valid (or partial) index may exist on disk.  Read obsidian_meta.json
            # (via the stat-keyed cache) and update in-memory state so the UI
            # shows the correct status.
            # A fresh-run pause-during-scan writes meta but never persists the
            # index, so docstore.json may be absent.  The has_vector_data field
            # is the authoritative signal — rely on meta alone.
            _meta = self._read_index_meta()
            if _meta is not None:
                try:
                    meta_claims_data = bool(_meta.get("has_vector_data"))
                    has_data = self._index_dir_has_vector_data(OBSIDIAN_INDEX_DIR)
                    if meta_claims_data and not has_data:
                        self._index_integrity_error = (
                            "Persisted index metadata claims vector data exists, "
                            "but the vector checkpoint files are missing or incomplete."
                        )
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
                    pass
            return self._index_state

    def docstore_doc_count(self) -> Optional[int]:
        """Loaded-docstore size for the health probe, or ``None``.

        ``None`` means "no loaded index" OR "index busy" — the probe uses a
        NON-blocking acquire of ``_index_mutation_lock`` because a 15 s UI
        poll must never stall behind (or stall) an insert/checkpoint; reading
        ``_index.docstore.docs`` without the lock would race the streaming
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
                return len(idx.docstore.docs)
            except Exception:
                return None
        finally:
            self._index_mutation_lock.release()

    def _acquire_retrieval_lock(self) -> None:
        """Timed acquire of ``_index_mutation_lock`` for the CHAT path (item 2.1).

        Defect this guards: the retrieval phase (``engine.query``/``retrieve``)
        embeds the query over HTTP while holding this lock. Before the embed
        call itself was bounded, one wedged local-backend call held the lock
        FOREVER — and because the old acquire here was also unbounded, every
        subsequent chat worker blocked on it in turn: each SSE consumer timed
        out (~330 s), abandoned its still-blocked worker thread, and the user
        retried into the same wall. Restart-only recovery.

        The timeout is generous on purpose: a legitimate holder can be a
        mid-run CHECKPOINT (which re-serialises the whole store under this
        lock — minutes on a multi-GB simple-backend store), so tripping early
        would false-error healthy chats during indexing. 120 s sits above any
        observed checkpoint yet below the SSE consumer stall floor (300 s+30),
        so the user gets THIS actionable message, not the generic
        "Generation timed out". Safe: acquire semantics unchanged on success
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

    def heartbeat(self, epoch: int) -> None:
        """Push the op-lock deadline out for the acquisition identified by *epoch*.

        Long external holders (the Note Refactor batch writers) must call this
        periodically so their lock does not passively expire mid-operation and
        get stolen by a concurrent indexing run — exactly the way the indexer
        heartbeats inside its own insert loop. *epoch* is the caller's own
        captured token; a stale/foreign epoch is a no-op inside
        ``RagOperationLock.heartbeat``, so a zombie can never extend the
        deadline of whoever holds the lock now. Exposed as a public method so
        ``refactor/`` never reaches into the private lock.
        """
        self._op_lock.heartbeat(epoch)

    def force_release(self) -> bool:
        """Unconditionally free the op-lock (recovery path, e.g. cancel); was-it-held."""
        return self._op_lock.force_release()

    def index_vault(
        self,
        llm_name: str,
        embed_name: str,
        provider_name: str = "ollama",
        op_epoch: int = 0,
    ) -> None:
        """Run a full incremental (re)index of the configured vault — the main orchestrator.

        ``op_epoch`` is the op-lock token the admitting route captured from
        ``try_acquire_lock`` — threaded through to the loader/streaming-insert
        heartbeats so this run can only ever extend its OWN acquisition (item
        1.5; ``0`` — e.g. a direct test call holding no lock — makes every
        heartbeat an inert no-op rather than an accidental extension of a
        foreign holder).

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
        """
        self._stop_event.clear()
        self._pause_requested = False
        self._pdf_cache_write_warning_emitted = False
        self._image_cache_write_warning_emitted = False
        self._vision_unavailable_warned = False
        self._emit("Starting Obsidian vault indexing…")
        with self._status_lock:
            self._index_state = "scanning"
            self._current_phase = "initialising"

        vault = self._vault_path
        if not vault or not os.path.isdir(vault):
            self._emit(f"ERROR: Vault path not set or not a directory: {vault!r}")
            with self._status_lock:
                self._index_state = "error"
            return

        from core.config import resolve_embed_provider, is_online_provider, load_config as _load_cfg
        embed_provider_name = resolve_embed_provider(_load_cfg(), provider_name)
        provider = get_provider(embed_provider_name)
        if is_online_provider(provider_name):
            self._emit(
                f"LLM: {llm_name} ({provider_name}, online) | "
                f"Embeddings: {embed_name} ({embed_provider_name}, local)"
            )
        else:
            self._emit(f"LLM: {llm_name} | Embeddings: {embed_name} ({provider_name})")

        # Preflight: a reachable local backend that simply has not pulled the
        # embed model otherwise fails per-chunk and only surfaces a GENERIC abort
        # ~87 s later (the consecutive-failure breaker's backoff window). Probe
        # the installed-model list and fail fast with an ACTIONABLE message. Only
        # aborts when the backend is reachable (no list error) and returns a
        # non-empty list that lacks the model by exact or base-name match — so an
        # unreachable backend (empty list + error) falls through to the existing
        # init/insert error paths rather than mis-reporting "not installed".
        try:
            _installed, _list_err = provider.get_models()
        except Exception:
            _installed, _list_err = [], "probe failed"
        if not _list_err and _installed:
            _base = embed_name.split(":")[0]
            if embed_name not in _installed and not any(
                m.split(":")[0] == _base for m in _installed
            ):
                self._emit(
                    f"ERROR: Embedding model '{embed_name}' is not installed on "
                    f"'{embed_provider_name}'. Pull or load it (e.g. "
                    f"`ollama pull {embed_name}`) in the Models panel, then reindex."
                )
                with self._status_lock:
                    self._index_state = "error"
                return

        try:
            embed_model = provider.get_embedding(embed_name)
            # Track 5.6: size the LlamaIndex-level sub-batching to the app's
            # embed batch, so one flushed batch is one provider HTTP call
            # (BaseEmbedding.get_text_embedding_batch otherwise re-splits our
            # K-chunk batches at its default embed_batch_size of 10). Set
            # post-construction (best-effort) rather than via a new
            # get_embedding kwarg so provider fakes/mocks and both real
            # adapters keep their existing signatures; the QUERY-path
            # constructions (prewarm/chat) deliberately keep the default —
            # they embed one text at a time.
            try:
                embed_model.embed_batch_size = self._embed_batch_size()
            except Exception:  # noqa: BLE001 — batching is an optimization only
                pass
        except Exception as e:
            self._emit(f"ERROR: Failed to initialize embedding model: {e}")
            with self._status_lock:
                self._index_state = "error"
            return

        # Preflight: refuse to overwrite an existing LanceDB index when the
        # LanceDB backend cannot be constructed in this environment (e.g. the
        # integration's `from pandas import DataFrame` fails, so the import guard
        # in rag/lancedb_store.py sets lancedb_available() = False). Without this
        # gate, _index_dir_has_vector_data() reports the intact LanceDB table as
        # "no data" (lancedb_table_count() -> -1), the run falls through to a
        # FRESH build, and the new SimpleVectorStore JSON silently OVERWRITES the
        # docstore/index-store/meta that the LanceDB table depends on — turning a
        # recoverable dependency gap into data loss. The vectors are fine; the
        # environment is the problem, so fail fast with an actionable message
        # instead of mutating the index. Mirrors the embed-model preflight above.
        _meta_path = os.path.join(OBSIDIAN_INDEX_DIR, "obsidian_meta.json")
        if os.path.exists(_meta_path):
            try:
                with open(_meta_path) as _f:
                    _existing_backend = (json.load(_f) or {}).get("vector_backend")
            except Exception:
                _existing_backend = None
            if _existing_backend == VECTOR_BACKEND_LANCEDB and not lancedb_available():
                self._emit(
                    "ERROR: The existing vault index uses the LanceDB backend, but "
                    "LanceDB cannot be loaded in this environment (its dependencies — "
                    "e.g. pandas — are missing). Refusing to reindex, which would "
                    "overwrite the existing index. Reinstall the dependencies "
                    "(pip install -r requirements.txt -c constraints.txt) and "
                    "restart, then reindex."
                )
                with self._status_lock:
                    self._index_state = "error"
                return

        try:
            # Setup phase: load or create the index under a brief write lock
            # and publish it immediately so chat queries that arrive during
            # indexing can use the in-progress index instead of blocking on a
            # multi-hour run.  The insert loop below runs without the rw lock.
            with self._rw_lock.write_lock():
                os.makedirs(OBSIDIAN_INDEX_DIR, exist_ok=True)
                meta_path = os.path.join(OBSIDIAN_INDEX_DIR, "obsidian_meta.json")
                is_incremental = False
                prev_meta: dict = {}
                if os.path.exists(meta_path):
                    try:
                        with open(meta_path) as f:
                            prev_meta = json.load(f)
                    except Exception:
                        prev_meta = {}
                    embed_changed = bool(prev_meta.get("embed")) and prev_meta.get("embed") != embed_name
                    # An index that recorded NO embed model cannot be safely
                    # extended: we can't prove its existing vectors match the
                    # current model, and a silent mix corrupts similarity search.
                    # Treat it like a model change (rebuild), not a free incremental.
                    embed_unknown = not bool(prev_meta.get("embed"))
                    has_vector_data = self._index_dir_has_vector_data(OBSIDIAN_INDEX_DIR)
                    if (
                        prev_meta.get("version") == OBSIDIAN_INDEX_VERSION
                        and has_vector_data
                        and not embed_changed
                        and not embed_unknown
                    ):
                        is_incremental = True
                    elif has_vector_data and (embed_changed or embed_unknown):
                        # Mixing incompatible vectors into the store would corrupt
                        # similarity search silently — force a rebuild.  Gated on
                        # has_vector_data: a paused_scan with no persisted vectors
                        # has nothing to be incompatible with.
                        if embed_changed:
                            self._emit(
                                f"WARNING: Existing index was built with embedding "
                                f"model '{prev_meta['embed']}', but the current model is '{embed_name}'. "
                                "New chunks would have incompatible vector representations; "
                                "starting a fresh vector index."
                            )
                        else:
                            self._emit(
                                "WARNING: Existing index did not record its embedding "
                                "model, so it cannot be safely extended; starting a "
                                "fresh vector index."
                            )

                # Item 2.8a follow-up (2026-07-05): the reindex/writer path must
                # honor the promotion marker the READERS enforce. A store torn by
                # a crash mid-promotion would otherwise be loaded incrementally
                # (the hash-skip can never repair its stranded chunks) and then
                # sealed by this run's fresh "complete" marker — defeating the
                # exact recovery the load-time integrity error tells the user to
                # take. Degrade a torn store to a fresh rebuild instead.
                if is_incremental and not self._incremental_store_is_intact(prev_meta):
                    is_incremental = False
                    self._emit(
                        "WARNING: the existing index checkpoint was interrupted "
                        "mid-promotion (mixed-generation store); rebuilding the "
                        "vector index from scratch to guarantee consistency."
                    )

                if not is_incremental:
                    if self._index_dir_has_vector_data(OBSIDIAN_INDEX_DIR):
                        self._migrate_legacy_caches()
                        bak = self._archive_old_index_dir(prev_meta)
                        if bak:
                            self._emit(
                                "WARNING: previous index version is incompatible — "
                                f"moved aside to {bak}. Indexing from scratch."
                            )
                    os.makedirs(OBSIDIAN_INDEX_DIR, exist_ok=True)
                    # Fresh build: backend chosen by the config knob (default
                    # lancedb). An incremental run instead inherits whatever the
                    # existing index was written with — never the config knob.
                    vector_backend = self._resolve_fresh_backend()
                    idx = self._build_index_for_backend(
                        fresh=True, backend=vector_backend, embed_model=embed_model
                    )
                else:
                    vector_backend = self._resolve_existing_backend(prev_meta)
                    idx = self._build_index_for_backend(
                        fresh=False, backend=vector_backend, embed_model=embed_model
                    )

                self._index = idx
                # Once the indexer publishes self._index, prewarm has nothing
                # left to do — chat will use this index directly.  Mark ready
                # so the UI banner clears even when the user reaches a populated
                # index via reindexing rather than via launch-time prewarm.
                self._set_prewarm("ready", "Vault is ready.")

            self._emit("Scanning vault for .md, .pdf, and referenced image files…")
            # Loader is now a generator (M4): documents are yielded one at a
            # time through the chunker and into the index, so the peak
            # memory cost no longer scales with the entire vault.  The
            # empty-vault check moves below — see the "no work done" branch
            # after `_index_documents_streaming` returns.
            raw_docs = self._load_vault_documents(vault, op_epoch=op_epoch)

            with self._status_lock:
                self._index_state = "embedding"
                self._current_phase = "embedding"
            self._emit("Splitting documents into chunks and embedding…")

            def _persist_callback(manifest_counts_so_far: dict[str, int]) -> None:
                """Mid-run checkpoint — bounds re-work on crash to the persist cadence.

                Holds ``_index_mutation_lock`` for the persist so a chat
                retrieval cannot iterate the vector store dict while
                LlamaIndex's persistence layer is reading it.  Python concurrent
                dict reads are safe today, but pinning persist behind the same
                lock that serialises inserts protects against future LlamaIndex
                changes that touch internal state during serialisation.
                """
                with self._rw_lock.write_lock():
                    with self._index_mutation_lock:
                        self._persist_index_checkpoint(idx, vector_backend)
                    self._write_index_meta(
                        meta_path,
                        embed_name,
                        provider_name,
                        partial=True,
                        phase="paused_partial",
                        has_vector_data=True,
                        inserted_this_run=sum(manifest_counts_so_far.values()),
                        vector_backend=vector_backend,
                    )
                    self._write_index_manifest(manifest_counts_so_far, vault)
                # Drop the cached BM25 retriever so the next hybrid chat
                # rebuilds against the just-persisted docstore.  Outside the
                # rw write lock so a concurrent chat that is mid-build does
                # not deadlock on a re-entrant acquire.
                self._invalidate_retrieval_caches()

            # Crash-drift recovery (lancedb only): the binary store is durable
            # on insert while the docstore is persisted at the checkpoint
            # cadence, so a hard crash can leave the vector table *ahead* of the
            # docstore. Detect that with two O(1) counts; when the table has
            # more rows than the docstore has nodes, re-process this run with
            # delete-before-insert so resumed chunks replace their orphan rows
            # instead of duplicating them. No-op for fresh builds and the simple
            # backend; a single cheap count comparison otherwise.
            lancedb_upsert = False
            if vector_backend == VECTOR_BACKEND_LANCEDB and is_incremental:
                try:
                    vec_rows = lancedb_table_count(OBSIDIAN_INDEX_DIR)
                    node_count = len(idx.docstore.docs)
                    if vec_rows > node_count:
                        lancedb_upsert = True
                        self._emit(
                            "Detected an interrupted previous run (vector store ahead of "
                            "docstore); reconciling to avoid duplicate chunks."
                        )
                        # Reconcile orphan rows (chunks whose docstore record —
                        # or whole source note — vanished in the crash window):
                        # the delete-before-insert upsert only heals chunks that
                        # get RE-PROCESSED, so rows for deleted notes need this
                        # explicit sweep. Fires only on this drift branch —
                        # offsetting add/delete drift is not detected (see
                        # Known Issues in the root CLAUDE.md).
                        lancedb_ids = lancedb_list_doc_ids(OBSIDIAN_INDEX_DIR)
                        docstore_keys = set(idx.docstore.docs.keys())
                        orphans = [i for i in lancedb_ids if i not in docstore_keys]
                        if orphans:
                            logger.info("Found %d LanceDB orphan rows to delete", len(orphans))
                            ok, detail = lancedb_delete_ids(idx.vector_store, orphans)
                            if ok:
                                self._emit(f"Reconciled {len(orphans)} LanceDB orphan rows.")
                            else:
                                # Orphans linger (duplicate retrieval hits until a
                                # full reindex) — that must be visible, not silent.
                                logger.warning("LanceDB orphan reconciliation failed: %s", detail)
                                self._emit(
                                    f"WARNING: could not reconcile {len(orphans)} LanceDB "
                                    f"orphan rows ({detail}); a full reindex will resync."
                                )
                    elif 0 <= vec_rows < node_count:
                        self._emit(
                            "WARNING: vector store has fewer rows than docstore nodes "
                            "(possible torn write); a full reindex will fully resync."
                        )
                except Exception:
                    logger.debug("lancedb drift check failed", exc_info=True)

            chunks_iter = self._chunk_raw_documents(raw_docs, vault)
            added, skipped, deleted, failed, manifest_counts = (
                self._index_documents_streaming(
                    idx, chunks_iter, _persist_callback,
                    lancedb_upsert=lancedb_upsert,
                    vector_backend=vector_backend,
                    op_epoch=op_epoch,
                )
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
                and not manifest_counts
            ):
                self._emit("ERROR: No indexable files found.")
                with self._status_lock:
                    self._index_state = "error"
                    self._current_phase = "error"
                return

            # Final phase: persist and update meta under a brief write lock.
            # The persist is also serialised against the chat retrieval path
            # via the mutation lock — see _persist_callback for the rationale.
            with self._rw_lock.write_lock():
                with self._index_mutation_lock:
                    self._persist_index_checkpoint(idx, vector_backend)
                has_vector_data = bool(manifest_counts) or self._index_has_vector_data(idx)
                if self._stop_event.is_set() and has_vector_data:
                    self._emit(
                        f"Checkpoint saved — {added} chunks embedded, "
                        f"{skipped} unchanged so far. "
                        "The partial index is queryable immediately."
                    )
                elif self._stop_event.is_set():
                    self._emit(
                        "Indexing stopped before any vector chunks were written."
                    )

                # A user-initiated cancel sets the stop event but clears
                # _pause_requested, so cancel must persist as a clean stop.
                with self._status_lock:
                    is_paused = self._stop_event.is_set() and self._pause_requested
                final_phase = (
                    "done"
                    if not self._stop_event.is_set()
                    else (
                        "paused_partial"
                        if is_paused and has_vector_data
                        else ("paused_scan" if is_paused else "idle")
                    )
                )
                self._write_index_meta(
                    meta_path,
                    embed_name,
                    provider_name,
                    partial=bool(is_paused),
                    phase=final_phase,
                    has_vector_data=has_vector_data,
                    inserted_this_run=sum(manifest_counts.values()),
                    vector_backend=vector_backend,
                )
                self._write_index_manifest(manifest_counts, vault)

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

            with self._status_lock:
                if not self._stop_event.is_set():
                    self._index_state = "done"
                elif self._pause_requested:
                    self._index_state = "paused_partial" if manifest_counts else "paused_scan"
                else:
                    self._index_state = "idle"
                self._current_phase = self._index_state
            tail = f", {failed} failed" if failed else ""
            if not self._stop_event.is_set():
                self._emit(
                    f"Indexing complete. {added} embedded, {skipped} unchanged, "
                    f"{deleted} removed{tail}."
                )
            elif self._pause_requested:
                if manifest_counts:
                    self._emit(
                        f"Indexing paused — {added} embedded, {skipped} unchanged{tail}. "
                        "Resume with the same embedding model to continue without re-processing "
                        "already-indexed chunks."
                    )
                else:
                    self._emit(
                        "Indexing paused before any chunks were embedded. Resume will restart "
                        "embedding, but extraction caches are preserved."
                    )
            else:
                self._emit(f"Indexing cancelled{tail}.")

        except Exception as e:
            self._emit(f"ERROR: Indexing failed: {e}")
            with self._status_lock:
                self._index_state = "error"
                self._current_phase = "error"
        finally:
            # Hygiene only: break LlamaIndex/parser reference cycles promptly
            # so the chunker/indexer backlog is reclaimable as soon as the run
            # ends, instead of waiting for a later generational collection.
            gc.collect()

    def _archive_old_index_dir(self, prev_meta: dict) -> str:
        """Move the existing index dir to a timestamped .bak sibling.

        Returns the absolute path of the bak directory, or "" if the move failed.
        Used when ``OBSIDIAN_INDEX_VERSION`` changes — we keep the prior work
        recoverable instead of ``shutil.rmtree``ing it.
        """
        from datetime import datetime as _dt
        try:
            stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
            prev_ver = str(prev_meta.get("version") or "unknown").replace("/", "_")
            bak = f"{OBSIDIAN_INDEX_DIR}.bak.{prev_ver}.{stamp}"
            os.rename(OBSIDIAN_INDEX_DIR, bak)
            # Bounded retention: a version bump archives the whole prior index
            # (docstore ~hundreds of MB + the LanceDB/JSON vectors), and nothing
            # else ever deletes these siblings, so they accumulate tens of GB on
            # the same disk the local LLM weights live on. Keep the newest few
            # and prune the rest.
            self._prune_old_index_backups(keep=2)
            return bak
        except Exception as exc:
            logger.warning("Could not archive prior index dir: %s", exc)
            try:
                if os.path.exists(OBSIDIAN_INDEX_DIR):
                    log_storage_deletion("archive_old_index_dir_rename_failed")
                    shutil.rmtree(OBSIDIAN_INDEX_DIR)
            except Exception:
                pass
            return ""

    def _prune_old_index_backups(self, keep: int = 2) -> None:
        """Delete all but the ``keep`` most recent ``<index>.bak.*`` siblings.

        Each ``_archive_old_index_dir`` call renames the entire prior index dir
        to a timestamped ``.bak`` sibling; without this sweep they grow without
        bound. Best-effort and never raises — a failed prune must not abort a
        reindex. Each confirmed removal is routed through ``log_storage_deletion``
        per the deletion-audit invariant (see the root ``CLAUDE.md``) — logged
        **after** the ``rmtree`` succeeds, because ``ignore_errors=True`` would
        otherwise let the audit line claim a removal that silently failed.
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
            for stale in baks[keep:]:
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
    ) -> None:
        _write_json_atomic(path, {
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
        })
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

        Driven by the ``vault_vector_backend`` config knob (default
        ``lancedb``). Falls back to ``simple`` with a one-time warning when
        lancedb is configured but not importable, mirroring the BM25/reranker
        import-guard discipline — a missing optional dep degrades, never crashes.
        """
        try:
            choice = (load_config().get("vault_vector_backend") or VECTOR_BACKEND_LANCEDB).strip().lower()
        except Exception:
            choice = VECTOR_BACKEND_LANCEDB
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
        binary ``lancedb/`` directory instead.
        """
        if backend == VECTOR_BACKEND_LANCEDB:
            vector_store = make_lancedb_vector_store(OBSIDIAN_INDEX_DIR)
            if fresh:
                return StorageContext.from_defaults(vector_store=vector_store)
            return StorageContext.from_defaults(
                persist_dir=OBSIDIAN_INDEX_DIR, vector_store=vector_store
            )
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
                        "(store files may be from mixed generations). Re-run "
                        "indexing to rebuild a consistent checkpoint."
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

    # TTL (seconds) for the status-poll lancedb count cache. Bounds how long a
    # stale count>0 boolean can be served; benign because that boolean only
    # gates an empty→non-empty transition that, once true, stays true.
    _LANCEDB_COUNT_TTL_S = 5.0

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
                except ValueError as exc:
                    if "Unknown document type" not in str(exc):
                        raise
                    # Unparseable markdown → treat the whole doc as one section;
                    # the secondary pass below still caps it (a free robustness
                    # win over the previous always-one-node fallback).
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
                        sub = md_secondary.get_nodes_from_documents([hn])
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
                except ValueError as exc:
                    if "Unknown document type" not in str(exc):
                        raise
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
                candidates = name_index.get(os.path.basename(target).lower())
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
        embed_batch: list = []          # [(doc, deleted_old), ...]
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
                for g_doc, g_deleted_old in batch_docs[start_idx:]:
                    if g_deleted_old and g_doc.doc_id:
                        reinsert_failed.add(g_doc.doc_id)

            for idx_in_batch, (b_doc, b_deleted_old) in enumerate(batch_docs):
                if self._stop_event.is_set():
                    _record_unprocessed_gaps(idx_in_batch)
                    return False
                try:
                    with self._index_mutation_lock:
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
            nonlocal added, consecutive_failures
            nonlocal pending_since_persist, pending_since_compact
            if not embed_batch:
                return True
            batch_docs = list(embed_batch)
            embed_batch.clear()
            # One long embed call follows — keep the op-lock TTL fed.
            self._op_lock.heartbeat(op_epoch)
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
                    [b_doc for b_doc, _ in batch_docs], idx._transformations,
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
                with self._index_mutation_lock:
                    idx.insert_nodes(nodes)
                    for b_doc, _ in batch_docs:
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
                for b_doc, _ in batch_docs:
                    try:
                        with self._index_mutation_lock:
                            idx.delete_ref_doc(b_doc.doc_id, delete_from_docstore=True)
                    except Exception:
                        pass
                return _insert_docs_individually(batch_docs)
            for b_doc, _ in batch_docs:
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
                vector_store = self._lancedb_vector_store_of(idx)
                with self._index_mutation_lock:
                    compact_lancedb_vector_store(vector_store)
                pending_since_compact = 0

        for i, doc in enumerate(chunks_iter):
            if self._stop_event.is_set():
                break
            if i % 10 == 0:
                self._op_lock.heartbeat(op_epoch)

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
            if can_increment and doc.doc_id and get_document_hash is not None:
                try:
                    if get_document_hash(doc.doc_id) == doc.hash:
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
                        with self._index_mutation_lock:
                            idx.delete_ref_doc(doc.doc_id, delete_from_docstore=True)
                        deleted_old = True
                except Exception:
                    pass

            # Track 5.6: buffer for the next batched flush (all insert
            # bookkeeping — counters, breaker, backoff — lives in the flush
            # helpers above). deleted_old rides with the doc so a failed
            # re-embed of a changed chunk is still reported as a gap.
            embed_batch.append((doc, deleted_old))
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
            for pending_doc, pending_deleted_old in embed_batch:
                if pending_deleted_old and pending_doc.doc_id:
                    reinsert_failed.add(pending_doc.doc_id)

        if can_increment and not self._stop_event.is_set():
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
            candidate_ids = previous_doc_ids - current_doc_ids
            if failed_sources:
                stale_doc_ids = {
                    d for d in candidate_ids
                    if d.split("::", 1)[0] not in failed_sources
                }
                withheld = len(candidate_ids) - len(stale_doc_ids)
                if withheld:
                    self._emit(
                        f"Note: {withheld} chunk(s) from {len(failed_sources)} "
                        f"unreadable file(s) were kept in the index (read failure "
                        f"≠ deletion); they will resync on the next run."
                    )
            else:
                stale_doc_ids = candidate_ids
            for i, doc_id in enumerate(sorted(stale_doc_ids)):
                if i % 25 == 0:
                    self._op_lock.heartbeat(op_epoch)
                try:
                    with self._index_mutation_lock:
                        idx.delete_ref_doc(doc_id, delete_from_docstore=True)
                    deleted += 1
                except Exception as exc:
                    logger.warning("Failed to delete stale indexed document %s: %s", doc_id, exc)
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

    def _load_vault_documents(self, vault_path: str, op_epoch: int = 0):
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
        self._scan_failed_sources = set()
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
            for scan_index, (path, rel) in enumerate(
                _iter_vault_scan_files(vault_root, user_excluded_dirs)
            ):
                if self._stop_event.is_set():
                    break
                if scan_index % 25 == 0:
                    self._op_lock.heartbeat(op_epoch)
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

            # Deterministic order parity with the old globally-sorted walk:
            # sorting each bucket by rel equals the old full-path sort order
            # (shared root prefix), so MD/PDF yield order and name_index bucket
            # order are byte-identical to the rglob era.
            md_paths_buffered.sort(key=lambda pr: pr[1])
            pdf_paths.sort(key=lambda pr: pr[1])
            image_entries.sort(key=lambda pr: pr[1])
            for path, rel in image_entries:
                name_index.setdefault(path.name.lower(), []).append(rel)

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
                if self._stop_event.is_set():
                    break
                if image_index % 10 == 0:
                    self._op_lock.heartbeat(op_epoch)
                try:
                    text = self._extract_image_description(path, vault_root, rel)
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
                if self._stop_event.is_set():
                    break
                if pdf_index % 10 == 0:
                    self._op_lock.heartbeat(op_epoch)
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
                    text = self._read_first_text_file(
                        [cache_file, self._legacy_pdf_cache_file(vault_root, signature)]
                    )
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
                                ocr_cb=glm_ocr_manager.extract_page_text,
                                ocr_max_pages=_VAULT_PDF_CHUNK,
                                page_done_cb=lambda _n: self._op_lock.heartbeat(op_epoch),
                            )
                            text = sections.full_text
                            if getattr(sections, "truncated", False):
                                # Incomplete coverage (OCR cap/budget): surface it
                                # and DO NOT cache the partial as complete, so the
                                # next run retries instead of baking in the loss.
                                self._emit(
                                    f"WARNING: {rel} was only partially extracted "
                                    "(scanned pages beyond the OCR limit were skipped); "
                                    "not cached so it will retry next run."
                                )
                            elif text:
                                self._save_pdf_cache_file(cache_file, text)
                        else:
                            # Large PDF: one document per 1000-page range, each
                            # extracted and cached independently — see the
                            # helper for the full rationale.
                            yield from self._load_pdf_range_documents(
                                path, rel, ext, vault_root, signature, page_count,
                                op_epoch=op_epoch,
                            )
                            continue
                    if text:
                        yield LlamaDocument(
                            text=text,
                            metadata={"file_path": str(path), "source": rel, "extension": ext},
                        )
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
        for start in range(0, page_count, EXTRACT_MAX_PAGES_PER_CALL):
            if self._stop_event.is_set():
                return
            end = min(start + EXTRACT_MAX_PAGES_PER_CALL, page_count)
            range_cache = self._pdf_range_cache_file(vault_root, signature, start, end)
            text = self._read_first_text_file([range_cache])
            if not text:
                # P1.6: refresh the operation-lock heartbeat between ranges so
                # multi-hour extractions of textbook PDFs don't let the TTL
                # expire while we are mid-file.
                self._op_lock.heartbeat(op_epoch)
                self._emit(
                    f"Extracting {rel} (pages {start + 1}-{end} of {page_count})…"
                )
                sections = extract_structured_from_pdf(
                    str(path),
                    start_page=start,
                    end_page=end,
                    ocr_cb=glm_ocr_manager.extract_page_text,
                    # OCR the WHOLE range (a scanned range used to stop at 100 of
                    # up to 1000 pages and cache that partial as complete).  The
                    # per-page heartbeat keeps the op-lock TTL alive on a long scan.
                    ocr_max_pages=EXTRACT_MAX_PAGES_PER_CALL,
                    page_done_cb=lambda _n: self._op_lock.heartbeat(op_epoch),
                )
                text = sections.full_text
                if getattr(sections, "truncated", False):
                    # Incomplete OCR coverage of this range: warn and DO NOT cache,
                    # so the range is retried next run instead of permanently
                    # serving a partial. Yield what we have (better than nothing).
                    self._emit(
                        f"WARNING: {rel} pages {start + 1}-{end} only partially "
                        "extracted (scanned pages beyond the OCR limit skipped); "
                        "not cached so it will retry next run."
                    )
                elif text:
                    self._save_pdf_cache_file(range_cache, text)
            if text:
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
        return hashlib.sha256(str(vault_root).encode("utf-8")).hexdigest()[:16]

    def _read_first_text_file(self, paths: list[Path]) -> str:
        for path in paths:
            if not path.exists():
                continue
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
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

    def _save_pdf_cache_file(self, cache_file: Path, text: str) -> None:
        try:
            self._atomic_write_text(cache_file, text)
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
            size = path.stat().st_size
        except OSError as exc:
            self._emit(f"WARNING: Could not stat image {rel}: {exc}")
            return ""
        if size > self._IMAGE_MAX_BYTES:
            self._emit(
                f"WARNING: Skipping {rel}: {size} bytes exceeds the "
                f"{self._IMAGE_MAX_BYTES}-byte vault image cap."
            )
            return ""
        try:
            data = path.read_bytes()
        except OSError as exc:
            self._emit(f"WARNING: Failed to read {rel}: {exc}")
            return ""
        digest = hashlib.sha256(data).hexdigest()
        cache_file = self._image_cache_file(vault_root, digest)
        cached = self._read_first_text_file(
            [cache_file, self._legacy_image_cache_file(vault_root, digest)]
        )
        if cached:
            return cached
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

    def _pdf_signatures_path(self) -> str:
        return os.path.join(OBSIDIAN_CACHE_DIR, "pdf_signatures.json")

    def _legacy_pdf_signatures_path(self) -> str:
        return os.path.join(OBSIDIAN_INDEX_DIR, "pdf_signatures.json")

    def _load_pdf_signature_cache(self) -> dict:
        """Return the persisted ``{rel_path: {size, mtime_ns, sha256}}`` map.

        Used by ``_pdf_file_signature`` to skip the expensive content hash
        when a PDF's size and mtime are unchanged from the previous run —
        textbook PDFs can be hundreds of MB, and hashing them every restart
        was the dominant cost of resuming an interrupted index.
        """
        merged: dict = {}
        for path in (self._legacy_pdf_signatures_path(), self._pdf_signatures_path()):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    merged.update(data)
            except Exception:
                pass
        return merged

    def _save_pdf_signature_cache(self, cache: dict) -> None:
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
        for path in (self._legacy_pdf_signatures_path(), self._pdf_signatures_path()):
            try:
                st = os.stat(path)
                key_parts.append((st.st_size, st.st_mtime_ns))
            except OSError:
                key_parts.append(None)
        key = tuple(key_parts)
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

        payload = {
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

    def _build_index_manifest_from_docstore(self) -> dict:
        """Recover an indexed-material manifest from an existing LlamaIndex docstore."""
        docstore_path = os.path.join(OBSIDIAN_INDEX_DIR, "docstore.json")
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

        payload = {
            "vault_path": str(vault_root) if vault_root else vault_path,
            "indexed_at": None,
            "materials": sorted(entries.values(), key=lambda item: item["source"].lower()),
        }

        meta = self._read_index_meta()
        if meta is not None:
            payload["indexed_at"] = meta.get("indexed_at")

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

    def _invalidate_retrieval_caches(self) -> None:
        """Drop the cached BM25 retriever and wikilink graph (in memory AND
        their on-disk sidecars) so the next chat rebuilds against the
        freshly-persisted docstore.

        Called after each successful ``idx.storage_context.persist(...)`` —
        both the mid-run checkpoint and the final persist — so concurrent
        chats observe the new chunks once indexing publishes them.  The
        reranker is unaffected: it depends on the model name only, not on
        index contents.
        """
        with self._bm25_build_lock:
            self._bm25_retriever = None
            self._bm25_cached_doc_count = -1
            shutil.rmtree(self._bm25_sidecar_dir(), ignore_errors=True)
        with self._wikilink_build_lock:
            self._wikilink_index = None
            self._wikilink_cached_doc_count = -1
            try:
                os.remove(self._wikilink_sidecar_path())
            except OSError:
                pass

    def _bm25_sidecar_dir(self) -> str:
        return os.path.join(OBSIDIAN_INDEX_DIR, _BM25_SIDECAR_DIRNAME)

    def _load_bm25_sidecar(self, live_count: int, top_k: int) -> Optional[Any]:
        """Try to mmap-load the persisted BM25 retriever from the sidecar.

        Returns None (after deleting the sidecar) when the sidecar is
        missing, torn, or stale.  Staleness is judged against BOTH the live
        docstore size and the index meta's ``indexed_at`` stamp — a count
        check alone could accept a sidecar persisted from a different
        docstore state that happens to have the same chunk count (e.g. a
        crash that landed between a checkpoint's persist and its cache
        invalidation).  Any failure degrades to a rebuild from the
        docstore, so deleting the sidecar by hand is always safe.

        ``from_persist_dir`` restores the ``similarity_top_k`` that was
        persisted, so the live ``top_k`` is re-applied explicitly.
        """
        from .engine import BM25Retriever
        sidecar = self._bm25_sidecar_dir()
        if BM25Retriever is None or not os.path.isdir(sidecar):
            return None
        try:
            meta_path = os.path.join(sidecar, _BM25_SIDECAR_META_FILENAME)
            with open(meta_path, encoding="utf-8") as f:
                sidecar_meta = json.load(f)
            if sidecar_meta.get("doc_count") != live_count:
                raise RuntimeError(
                    f"sidecar has {sidecar_meta.get('doc_count')} nodes, "
                    f"docstore has {live_count}"
                )
            index_meta = self._read_index_meta() or {}
            if sidecar_meta.get("indexed_at") != index_meta.get("indexed_at"):
                raise RuntimeError("sidecar predates the current index meta")
            retriever = BM25Retriever.from_persist_dir(sidecar, mmap=True)
            # bm25s serves the corpus as a lazy JsonlCorpus (supports len()).
            if len(retriever.corpus) != live_count:
                raise RuntimeError("sidecar corpus length mismatch")
            retriever.similarity_top_k = top_k
            logger.info(
                "BM25 retriever loaded from sidecar (%d nodes, mmap).", live_count
            )
            return retriever
        except Exception as exc:
            logger.info("BM25 sidecar unusable (%s); rebuilding from docstore.", exc)
            shutil.rmtree(sidecar, ignore_errors=True)
            return None

    def _persist_bm25_sidecar(self, retriever: Any, doc_count: int) -> None:
        """Persist a freshly-built BM25 retriever to the sidecar dir.

        Skipped while an indexing run is active: `_invalidate_retrieval_caches`
        fires at every mid-run checkpoint, so persisting then would churn
        full-corpus writes on every mid-run chat.  The state read is a benign
        TOCTOU — a run starting right after the check invalidates (rmtree)
        the sidecar at its next checkpoint or final persist anyway.

        The meta file is written LAST: the loader requires it, so a persist
        torn by a crash leaves a sidecar that self-invalidates on next load.
        """
        with self._status_lock:
            state = self._index_state
        if state not in ("idle", "done"):
            return
        sidecar = self._bm25_sidecar_dir()
        index_meta = self._read_index_meta() or {}
        try:
            shutil.rmtree(sidecar, ignore_errors=True)
            os.makedirs(sidecar, exist_ok=True)
            retriever.persist(sidecar)
            _write_json_atomic(
                os.path.join(sidecar, _BM25_SIDECAR_META_FILENAME),
                {
                    "doc_count": doc_count,
                    "indexed_at": index_meta.get("indexed_at"),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            logger.info("BM25 retriever persisted to sidecar (%d nodes).", doc_count)
        except Exception as exc:
            # Best-effort: a failed persist only costs the next launch a
            # rebuild.  Remove the partial sidecar so the loader cannot
            # trip over it.
            logger.warning("Could not persist BM25 sidecar: %s", exc)
            shutil.rmtree(sidecar, ignore_errors=True)

    def _get_bm25_retriever(self, top_k: int) -> Optional[Any]:
        """Return a cached BM25Retriever sized to *top_k*, or None if BM25
        is unavailable.

        Caches the retriever by docstore size so a vault with N chunks
        rebuilds at most once per indexing run.  The size fingerprint is
        cheap and catches every append (the dominant Obsidian workflow);
        same-size delete+insert races are tolerated as eventually-correct —
        the indexer's explicit ``_invalidate_retrieval_caches`` call after
        persist resyncs the BM25 view to the docstore.

        Process-cold misses consult the on-disk sidecar first
        (``_load_bm25_sidecar``): a previous build persisted there is
        mmap-loaded in seconds with its corpus served lazily from JSONL,
        instead of re-tokenising the docstore and duplicating all vault
        text in RAM.  A successful in-process build is persisted back to
        the sidecar (``_persist_bm25_sidecar``) unless an indexing run is
        active.

        Docstore reads that materialise the dict (``len(docs)`` for the
        miss path, ``list(docs.values())`` for the snapshot) are taken
        under ``_index_mutation_lock`` so an in-flight ``idx.insert`` /
        ``idx.delete_ref_doc`` from the streaming indexer cannot mutate
        the dict during iteration and raise
        ``RuntimeError: dictionary changed size during iteration``.  The
        BM25 build itself (tokenise + index N nodes) runs *outside* the
        mutation lock — the lock is held only for the O(N) pointer-list
        copy, not the O(N·tokens) build, so a long BM25 build cannot
        stall the indexer.
        """
        from .engine import BM25Retriever
        if BM25Retriever is None or self._index is None:
            return None
        docstore = getattr(self._index, "docstore", None)
        docs = getattr(docstore, "docs", None) if docstore else None
        if not docs:
            return None
        # Fast path: ``len()`` of a CPython dict is an O(1) atomic field
        # read, so the size-fingerprint heuristic does not need the
        # mutation lock here.  A torn count just re-enters the slow path,
        # which double-checks under the build lock.
        current_count = len(docs)
        cached = self._bm25_retriever
        if cached is not None and self._bm25_cached_doc_count == current_count:
            # Retune race fix (improvement plan 2026-07-04, item 2.7): this
            # fast path used to write ``cached.similarity_top_k = top_k`` here
            # — with NO lock — while a concurrent chat could be mid-retrieval
            # on the SAME shared object under ``_index_mutation_lock`` (a deck
            # run's vault_search k=12 retuned a concurrent user chat k=8
            # mid-flight). Tuning now happens in exactly ONE place: the
            # engine's ``_build_retrieval_pipeline`` sets ``similarity_top_k``
            # to the per-query breadth INSIDE the same mutation-lock hold that
            # runs the retrieval, so no other request can interleave a write
            # between tune and use. This fetch is read-only on purpose — do
            # not "restore" the convenience retune. Invariant (pinned by
            # ``TestSharedRetunerRace``): fetching a cached BM25/reranker
            # never mutates its tuning fields.
            return cached

        with self._bm25_build_lock:
            # Double-check under the build lock so two concurrent chats do
            # not both rebuild against the same docstore size.
            if (
                self._bm25_retriever is not None
                and self._bm25_cached_doc_count == current_count
            ):
                # Read-only return (item 2.7) — see the fast path above: the
                # build lock is NOT the lock that serialises retrieval, so a
                # retune here raced an in-flight query the same way.
                return self._bm25_retriever
            # Sidecar fast path: mmap-load the retriever persisted by a
            # previous build instead of re-tokenising the whole docstore.
            # Validated against the live docstore count and the index
            # meta stamp; any mismatch or load error deletes the sidecar
            # and falls through to the build below.
            loaded = self._load_bm25_sidecar(len(docs), top_k)
            if loaded is not None:
                self._bm25_retriever = loaded
                self._bm25_cached_doc_count = len(docs)
                return loaded
            # Snapshot the docstore under _index_mutation_lock so an
            # in-flight indexer insert/delete cannot mutate the dict while
            # we iterate.  Record the size from the same locked window so
            # the cache fingerprint reflects the exact snapshot we built
            # BM25 from — otherwise a concurrent insert between
            # ``list(docs.values())`` and ``len(docs)`` would let us cache
            # an N-node retriever under an N+1 fingerprint, missing a
            # subsequent re-check.
            with self._index_mutation_lock:
                nodes = list(docs.values())
                snapshot_count = len(docs)
            if not nodes:
                return None
            # Build BM25 outside the mutation lock — tokenisation + index
            # build is the expensive step and the indexer must remain
            # free to insert while it runs.
            try:
                retriever = BM25Retriever.from_defaults(
                    nodes=nodes,
                    similarity_top_k=top_k,
                )
            except Exception as exc:
                logger.warning("BM25 retriever build failed: %s", exc)
                return None
            self._bm25_retriever = retriever
            self._bm25_cached_doc_count = snapshot_count
            # Persist for the next launch (skipped while indexing is
            # active — see the helper).  Adds a one-off full-corpus write
            # after a fresh build; prewarm normally absorbs this off the
            # chat path.
            self._persist_bm25_sidecar(retriever, snapshot_count)
            return retriever

    @staticmethod
    def _resolve_note_link(
        target: str,
        src_note_rel: str,
        indexed_md_sources: set[str],
        note_name_index: dict[str, list[str]],
    ) -> str:
        """Resolve one raw link *target* found in note *src_note_rel* to the
        vault-relative ``source`` of an indexed ``.md`` note, or "" when the
        target is not a note link or cannot be resolved.

        Resolution mirrors Obsidian (and the image path in
        ``_resolve_md_attachment``) but checks membership against the
        in-memory ``indexed_md_sources`` set instead of the filesystem, so it
        performs ZERO stats — the whole point of building the graph from the
        docstore:

          1. Parent-relative to the linking note's folder (``.md`` appended
             when the link omits the extension).  An indexed source there wins.
          2. A bare filename (no directory component) that missed step 1 falls
             back to a vault-wide basename lookup (``note_name_index``) —
             Obsidian's shortest-path resolution — with ``_closest_note_by_dir``
             breaking ambiguous basenames.

        Non-note targets (images, PDFs, external URLs) never match an indexed
        ``.md`` source and therefore return "".
        """
        target = (target or "").strip()
        if not target:
            return ""
        target = unquote(target)
        # Drop anchor / block-reference suffixes (the ``|alias`` is already
        # stripped by the wikilink regex; ``#heading`` / ``^block`` and any
        # inline-link title are handled here).
        target = target.split("#", 1)[0].split("^", 1)[0].strip()
        if not target:
            return ""
        if any(target.lower().startswith(sch) for sch in _NON_ATTACHMENT_SCHEMES):
            return ""

        has_dir = "/" in target or os.sep in target

        def _with_md(p: str) -> str:
            return p if p.lower().endswith(".md") else p + ".md"

        # Step 1: parent-relative resolution against the indexed-source set.
        src_dir = os.path.dirname(src_note_rel)
        joined = os.path.normpath(os.path.join(src_dir, target) if src_dir else target)
        rel = joined.replace(os.sep, "/")
        if rel and not (rel == ".." or rel.startswith("../") or os.path.isabs(rel)):
            cand = _with_md(rel)
            if cand in indexed_md_sources:
                return cand

        # Step 2: bare-name shortest-path fallback (only when the link carried
        # no directory component, matching Obsidian and the image resolver).
        if not has_dir:
            candidates = note_name_index.get(
                _with_md(os.path.basename(target)).lower()
            )
            if candidates:
                if len(candidates) == 1:
                    return candidates[0]
                return _closest_note_by_dir(candidates, os.path.dirname(src_note_rel))
        return ""

    def _extract_note_links(
        self,
        text: str,
        src_note_rel: str,
        indexed_md_sources: set[str],
        note_name_index: dict[str, list[str]],
    ) -> list[str]:
        """Resolved vault-relative sources of every indexed note this chunk
        links to, de-duplicated in first-seen order.

        Scans the same wikilink + inline-link shapes as
        ``_extract_md_attachments`` but keeps only targets that resolve to an
        indexed ``.md`` note (via ``_resolve_note_link``).  Embeds (``![[…]]``)
        count as links — a transcluded note is a relationship; non-note embeds
        (images) are dropped by resolution.
        """
        if not text:
            return []
        out: list[str] = []
        seen: set[str] = set()

        def _accept(raw: str) -> None:
            resolved = self._resolve_note_link(
                raw, src_note_rel, indexed_md_sources, note_name_index
            )
            if resolved and resolved not in seen:
                seen.add(resolved)
                out.append(resolved)

        for match in _OBSIDIAN_WIKILINK_RE.finditer(text):
            _accept(match.group(1))
        for match in _INLINE_LINK_RE.finditer(text):
            # Strip an optional title segment: [label](url "title").
            _accept(match.group(1).split(" ", 1)[0])
        return out

    def _build_wikilink_graph(self, nodes: list) -> _WikilinkGraph:
        """Assemble the note→note adjacency from a docstore node snapshot.

        Pass 1 records every indexed ``.md`` note (its ``source``, its chunk
        node ids, and a basename index for shortest-path resolution).  Pass 2
        resolves each note's outbound links against that set and inverts them
        into backlinks.  Pure in-memory work over the docstore — no
        filesystem, no embeddings, no index mutation.  A note split into
        several chunks contributes all its node ids and the union of the links
        across its chunks.
        """
        # Pass 1 — enumerate the indexed .md notes.  Collect the set of valid
        # link targets (indexed_md_sources), the per-note chunk ids
        # (note_to_node_ids, used later to fetch a neighbour's chunks), and the
        # raw (source, text) pairs to scan for links in pass 2.  Non-.md nodes
        # (PDFs, images) are not notes and never participate in the graph.
        indexed_md_sources: set[str] = set()
        note_to_node_ids: dict[str, list[str]] = {}
        md_nodes: list[tuple[str, str]] = []  # (source, text)
        for node in nodes:
            meta = getattr(node, "metadata", None) or {}
            source = meta.get("source") or meta.get("file_path") or ""
            if not source:
                continue
            source = str(source)
            ext = str(meta.get("extension") or os.path.splitext(source)[1]).lower()
            if ext not in VAULT_MD_EXTS:
                continue
            indexed_md_sources.add(source)
            node_id = getattr(node, "node_id", None) or getattr(node, "id_", None)
            if node_id is not None:
                note_to_node_ids.setdefault(source, []).append(str(node_id))
            md_nodes.append((source, getattr(node, "text", "") or ""))

        # basename.lower() -> [source] index for Obsidian shortest-path
        # resolution of bare [[note]] links.  Built from the deduplicated
        # source set so a multi-chunk note contributes one entry, not one per
        # chunk.
        note_name_index: dict[str, list[str]] = {}
        for source in indexed_md_sources:
            note_name_index.setdefault(
                os.path.basename(source).lower(), []
            ).append(source)

        # Pass 2 — resolve each note's outbound links against the pass-1 set
        # (union across the note's chunks; self-links dropped), then invert the
        # forward edges into backlinks so neighbours() can union both.
        forward: dict[str, set[str]] = {}
        for source, text in md_nodes:
            for target in self._extract_note_links(
                text, source, indexed_md_sources, note_name_index
            ):
                if target != source:
                    forward.setdefault(source, set()).add(target)

        backward: dict[str, set[str]] = {}
        for source, targets in forward.items():
            for target in targets:
                backward.setdefault(target, set()).add(source)

        return _WikilinkGraph(forward, backward, note_to_node_ids)

    def _wikilink_sidecar_path(self) -> str:
        return os.path.join(OBSIDIAN_INDEX_DIR, _WIKILINK_SIDECAR_FILENAME)

    def _load_wikilink_sidecar(self, live_count: int) -> Optional[_WikilinkGraph]:
        """Try to load the persisted wikilink graph from the sidecar file.

        Returns None (after deleting the sidecar) when it is missing, torn,
        or stale.  Staleness mirrors ``_load_bm25_sidecar`` — the live
        docstore size AND the index meta's ``indexed_at`` stamp must both
        match — with one deliberate tightening: ``indexed_at`` must be
        *truthy* on both sides.  A sidecar written without a real index meta
        (None == None would pass the BM25-style check) proves nothing about
        which docstore state it came from, so it is treated as unusable.
        Any failure degrades to a rebuild from the docstore; deleting the
        sidecar by hand is always safe.
        """
        path = self._wikilink_sidecar_path()
        if not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
            if payload.get("doc_count") != live_count:
                raise RuntimeError(
                    f"sidecar has {payload.get('doc_count')} nodes, "
                    f"docstore has {live_count}"
                )
            index_meta = self._read_index_meta() or {}
            stamp = payload.get("indexed_at")
            if not stamp or stamp != index_meta.get("indexed_at"):
                raise RuntimeError("sidecar predates the current index meta")
            graph = _WikilinkGraph.from_payload(payload)
            logger.info(
                "Wikilink graph loaded from sidecar (%d notes, %d edges).",
                graph.note_count, graph.edge_count,
            )
            return graph
        except Exception as exc:
            logger.info(
                "Wikilink sidecar unusable (%s); rebuilding from docstore.", exc
            )
            try:
                os.remove(path)
            except OSError:
                pass
            return None

    def _persist_wikilink_sidecar(self, graph: _WikilinkGraph, doc_count: int) -> None:
        """Persist a freshly-built wikilink graph to the sidecar file.

        Skipped while an indexing run is active (mirrors
        ``_persist_bm25_sidecar`` — ``_invalidate_retrieval_caches`` fires at
        every mid-run checkpoint, so persisting then would churn writes on
        every mid-run chat) and when the index meta has no ``indexed_at``
        stamp (no real on-disk index → the loader could never validate the
        sidecar, so writing one is pure waste; this also keeps docstore-only
        unit tests write-free).  A failed write removes the partial file —
        best-effort, worth only the next launch's rebuild.
        """
        with self._status_lock:
            state = self._index_state
        if state not in ("idle", "done"):
            return
        index_meta = self._read_index_meta() or {}
        stamp = index_meta.get("indexed_at")
        if not stamp:
            return
        path = self._wikilink_sidecar_path()
        try:
            payload = graph.to_payload()
            payload.update(
                {
                    "doc_count": doc_count,
                    "indexed_at": stamp,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            _write_json_atomic(path, payload)
            logger.info(
                "Wikilink graph persisted to sidecar (%d nodes).", doc_count
            )
        except Exception as exc:
            logger.warning("Could not persist wikilink sidecar: %s", exc)
            try:
                os.remove(path)
            except OSError:
                pass

    def _get_wikilink_index(self) -> Optional[_WikilinkGraph]:
        """Return the cached note→note wikilink graph, building it on demand.

        Mirrors ``_get_bm25_retriever``'s discipline: cached by docstore size
        so a vault with N chunks rebuilds at most once per indexing run,
        double-checked under ``_wikilink_build_lock``, and the docstore
        snapshot taken under ``_index_mutation_lock`` so an in-flight indexer
        insert/delete cannot mutate the dict mid-iteration.  The build itself
        (regex parse of node text) runs OUTSIDE the mutation lock, like BM25's
        tokenisation, so it never stalls the indexer.  Process-cold misses
        consult the on-disk sidecar first (``_load_wikilink_sidecar``) —
        skipping both the O(N) docstore snapshot and the regex sweep — and a
        successful in-process build is persisted back
        (``_persist_wikilink_sidecar``) unless an indexing run is active.
        Returns None when no index/docstore is loaded.  Reindex-free and
        re-embed-free — reads only the docstore.
        """
        if self._index is None:
            return None
        docstore = getattr(self._index, "docstore", None)
        docs = getattr(docstore, "docs", None) if docstore else None
        if not docs:
            return None
        current_count = len(docs)
        cached = self._wikilink_index
        if cached is not None and self._wikilink_cached_doc_count == current_count:
            return cached
        with self._wikilink_build_lock:
            # Double-check under the build lock so two concurrent chats do not
            # both rebuild against the same docstore size.
            if (
                self._wikilink_index is not None
                and self._wikilink_cached_doc_count == current_count
            ):
                return self._wikilink_index
            # Sidecar first: a hit avoids the O(N) node-list copy and the
            # regex sweep entirely.  The unlocked len(docs) matches the BM25
            # caller's discipline; the loader's doc_count + indexed_at gates
            # keep a racing mutation from ever validating a stale graph.
            loaded = self._load_wikilink_sidecar(current_count)
            if loaded is not None:
                self._wikilink_index = loaded
                self._wikilink_cached_doc_count = current_count
                return loaded
            # Snapshot the docstore under _index_mutation_lock so an in-flight
            # indexer insert/delete cannot mutate the dict while we iterate;
            # record the size from the same locked window so the fingerprint
            # matches the exact snapshot we built from.
            with self._index_mutation_lock:
                nodes = list(docs.values())
                snapshot_count = len(docs)
            if not nodes:
                return None
            graph = self._build_wikilink_graph(nodes)
            self._wikilink_index = graph
            self._wikilink_cached_doc_count = snapshot_count
            self._persist_wikilink_sidecar(graph, snapshot_count)
            return graph

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

    def _build_reranker(
        self, reranker_cls: Any, model_name: str, top_n: int, device_mode: str
    ) -> Any:
        """Construct the cross-encoder for *device_mode*, degrading to CPU.

        ``"auto"`` omits the device argument entirely — byte-identical to
        the pre-knob construction.  A non-CPU failure (construction or
        warm-up inference) retries on CPU and warns instead of raising:
        a Metal hiccup must cost rerank *latency*, never rerank itself.
        Only the CPU attempt's failure propagates to the caller's sticky
        failure flag.
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
            self._emit(
                f"WARNING: Reranker could not run on '{device_mode}' ({exc}); "
                "using CPU for this session. Change vault_reranker_device or "
                "restart to retry."
            )
            return reranker

    def _get_reranker(self, model_name: str, top_n: int) -> Optional[Any]:
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
        """
        from .engine import SentenceTransformerRerank
        if SentenceTransformerRerank is None or not model_name:
            return None
        device_mode = self._resolve_reranker_device_mode()
        cache_key = f"{model_name}::{device_mode}"
        with self._reranker_load_lock:
            if (
                self._reranker is not None
                and self._reranker_model_loaded == cache_key
            ):
                # Read-only return (item 2.7): ``top_n`` used to be written
                # here under ``_reranker_load_lock`` — a DIFFERENT lock from
                # the ``_index_mutation_lock`` under which a concurrent
                # query's rerank executes, so an in-flight rerank could be
                # retrimmed to another request's top_n mid-pass. The engine's
                # ``_build_retrieval_pipeline`` (inside the mutation-lock
                # hold) is now the only writer of ``top_n``.
                return self._reranker
            # Reset the sticky failure flag when the requested model or
            # device differs from the last attempt — a config change is the
            # user's signal that they want us to retry.
            if cache_key != self._reranker_last_tried:
                self._reranker_failed = False
            if self._reranker_failed:
                return None
            try:
                reranker = self._build_reranker(
                    SentenceTransformerRerank, model_name, top_n, device_mode
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
                self._emit(
                    f"WARNING: Reranker '{model_name}' could not be loaded; "
                    f"vault chat will use retrieval without rerank ({exc})."
                    + offline_hint
                )
                return None
            self._reranker = reranker
            self._reranker_model_loaded = cache_key
            self._reranker_last_tried = cache_key
            return reranker

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
                    backend = self._resolve_existing_backend(self._read_index_meta())
                    try:
                        self._validate_persisted_index_files(
                            OBSIDIAN_INDEX_DIR, full=False, backend=backend,
                            check_promotion_marker=True,
                        )
                    except RuntimeError as exc:
                        self._index_integrity_error = str(exc)
                        raise
                    provider = get_provider(provider_name)
                    embed_model = provider.get_embedding(embed_name)
                    try:
                        self._index = self._build_index_for_backend(
                            fresh=False, backend=backend, embed_model=embed_model
                        )
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
        effective_embed = self._effective_embed_name(embed_name, _stage)
        self._ensure_index_loaded(
            provider_name=provider_name,
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
            self._get_bm25_retriever(top_k=top_k)
            if hybrid_enabled
            else None
        )
        if reranker_enabled and reranker_model:
            _stage("Loading cross-encoder reranker…")
        reranker = (
            self._get_reranker(model_name=reranker_model, top_n=top_k)
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
            engine = SimpleQueryEngine(
                local_index,
                llm_name,
                effective_embed,
                top_k,
                provider_name=provider_name,
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

        # Serialise retrieval against the indexer's idx.insert() calls.  With
        # streaming=True, query_engine.query() runs retrieval synchronously
        # and returns a StreamingResponse whose response_gen pulls LLM tokens
        # lazily, so this lock is held only for the brief retrieval phase —
        # the LLM streaming itself runs outside the lock.
        _stage("Retrieving context…")
        self._acquire_retrieval_lock()
        try:
            return engine.query(message)
        finally:
            self._index_mutation_lock.release()

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

        Holds :attr:`_index_mutation_lock` for the brief retrieval
        phase, matching :meth:`stream_chat`'s discipline.
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
        self._acquire_retrieval_lock()
        try:
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
            text, extract_truncated = self._read_pdf_text(
                candidate, vault_root, max_chars, time_budget_s=time_budget_s)

        size_truncated = len(text) > max_chars
        if size_truncated:
            text = text[:max_chars]
        return text, (size_truncated or extract_truncated)

    def _read_pdf_text(
        self,
        path: Path,
        vault_root: Path,
        char_budget: int,
        time_budget_s: Optional[float] = None,
    ) -> tuple[str, bool]:
        """Return cached PDF text if present, otherwise a bounded fresh extract.

        Returns ``(text, truncated)``. ``truncated`` is True when the
        bounded fresh extract did not cover the whole document (page or
        char limit reached). Cache hits are never reported as truncated
        — the caller still applies its own ``max_chars`` cap.

        The persisted signature map is consulted (same as the indexer) so an
        unchanged large PDF skips the full content re-hash on every call.
        """
        try:
            rel = path.relative_to(vault_root).as_posix()
        except ValueError:
            rel = None
        try:
            sig = self._pdf_file_signature(path, self._persisted_pdf_signatures(), rel)
        except OSError as exc:
            raise IOError(f"Could not stat PDF {path.name}: {exc}") from exc
        cache_file = self._pdf_cache_file(vault_root, sig)
        legacy_cache = self._legacy_pdf_cache_file(vault_root, sig)
        cached = self._read_first_text_file([cache_file, legacy_cache])
        if cached:
            return cached, False

        # Large PDFs indexed by the per-range loader have no whole-file cache;
        # stitch their range caches back together instead of re-extracting.
        range_text, range_truncated, range_found = self._read_pdf_range_caches(
            path, vault_root, sig, char_budget
        )
        if range_found:
            return range_text, range_truncated

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
        try:
            sections = extract_structured_from_pdf(
                str(path),
                char_budget=char_budget,
                end_page=EXTRACT_MAX_PAGES_PER_CALL,
            )
        except Exception as exc:
            raise IOError(f"PDF extraction failed for {path.name}: {exc}") from exc
        text = sections.full_text or ""
        truncated = bool(getattr(sections, "truncated", False)) or len(text) >= char_budget
        return text, truncated

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
        self._stop_event.set()
        was_held = self._op_lock.force_release()
        # State transition and on-disk meta clear happen under the same
        # status_lock so a concurrent get_status() cannot observe the
        # in-memory "idle" while the on-disk meta still says partial=True
        # (which would re-promote state to paused_* via the lazy-recovery
        # branch).  The bg thread's later final-persist converges on the
        # same partial=False values.
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
            meta_path = os.path.join(OBSIDIAN_INDEX_DIR, "obsidian_meta.json")
            if os.path.exists(meta_path):
                try:
                    with open(meta_path) as f:
                        prev = json.load(f)
                    if prev.get("partial"):
                        prev["partial"] = False
                        prev["phase"] = "idle"
                        prev["indexed_at"] = datetime.now(timezone.utc).isoformat()
                        _write_json_atomic(meta_path, prev)
                except Exception:
                    logger.debug("cancel_indexing: failed to clear partial meta", exc_info=True)
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
                    backend = self._resolve_existing_backend(self._read_index_meta())
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
                        provider = get_provider(provider_name)
                        embed_model = provider.get_embedding(embed_name)
                        self._index = self._build_index_for_backend(
                            fresh=False, backend=backend, embed_model=embed_model
                        )
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

            # Warm the wikilink graph silently (no prewarm stage): the build is
            # a fast docstore-only regex pass, much cheaper than BM25, and the
            # first chat's stage_cb still surfaces it if this is skipped.  Only
            # when expansion is enabled, so a disabled feature pays nothing.
            if bool(cfg.get("vault_wikilink_expansion", False)):
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
            self._index = None
        with self._status_lock:
            self._index_state = "idle"
        # Drop the BM25 cache — its nodes referenced the just-cleared index.
        # The reranker is index-independent and is kept so /api/reset does
        # not trigger another model download for the next vault.
        self._invalidate_retrieval_caches()
        # Drop the thesaurus cache too: a vault switch points at different
        # curated files (or none). Mtime-keyed, so a stale object would
        # otherwise survive a switch to a same-signature file by coincidence.
        with self._thesaurus_build_lock:
            self._thesaurus = None
            self._thesaurus_cache_key = None
        self.reset_prewarm()
        # Hygiene: the just-dropped index/BM25 objects sit in reference
        # cycles (LlamaIndex stores back-reference their context); collect
        # now so /api/reset actually releases them.
        gc.collect()

obsidian_manager = ObsidianVaultManager()
