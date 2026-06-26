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
from core.config import load_config, save_config
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


def _write_json_atomic(path: str, data: dict) -> None:
    """Write *data* as JSON to *path* atomically using a sibling temp file.

    Uses the same tempfile+os.replace() pattern as _save_pdf_cache_file so
    a crash or SIGTERM mid-write can never leave the file empty or corrupt.
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


class ObsidianVaultManager:
    """Singleton owning the vault index, its caches, and the chat/indexing entrypoints.

    Provider-agnostic (it takes the chat/embed/provider names per call rather than
    binding a backend) and **heavily concurrent**. The lock map, with each lock's
    job and the ordering rules that keep it deadlock-free:

    * ``_op_lock`` (:class:`RagOperationLock`) — coarse admission control: only one
      long operation (an index run, or a Note-Refactor write) at a time, with a TTL
      so a crashed worker self-expires. ``try_acquire_lock`` captures the epoch into
      ``_lock_epoch``; ``release_lock`` passes it back so a zombie cannot release a
      newer holder's lock.
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
        self._lock_epoch: int = 0
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
        # Cross-encoder reranker cache.  The model load is one-shot (the
        # weights are kept in memory) so the first chat pays the download +
        # warm-up cost and subsequent ones reuse the same object with only
        # ``top_n`` retuned per query.
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

    def set_vault_path(self, path: str) -> None:
        self._vault_path = self._normalise_vault_path(path)
        cfg = load_config()
        cfg["obsidian_vault_path"] = self._vault_path
        save_config(cfg)

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

    def try_acquire_lock(self, ttl: int = 3600) -> bool:
        """Admit one long operation; cache its epoch for the matching release.

        Returns ``True`` and records ``_lock_epoch`` on success. The epoch is what
        makes :meth:`release_lock` safe: a worker whose acquisition already expired
        (TTL lapsed, another caller stole the lock) will pass a stale epoch and so
        cannot release the new holder's lock.
        """
        acquired = self._op_lock.try_acquire(ttl)
        if acquired:
            self._lock_epoch = self._op_lock.epoch
        return acquired

    def release_lock(self):
        """Release the op-lock using the epoch captured at acquire time (no-op if stale)."""
        self._op_lock.release(self._lock_epoch)

    def force_release(self) -> bool:
        """Unconditionally free the op-lock (recovery path, e.g. cancel); was-it-held."""
        return self._op_lock.force_release()

    def index_vault(self, llm_name: str, embed_name: str, provider_name: str = "ollama") -> None:
        """Run a full incremental (re)index of the configured vault — the main orchestrator.

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

        try:
            embed_model = provider.get_embedding(embed_name)
        except Exception as e:
            self._emit(f"ERROR: Failed to initialize embedding model: {e}")
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
            raw_docs = self._load_vault_documents(vault)

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
                    node_count = len(getattr(idx, "docstore").docs)
                    if vec_rows > node_count:
                        lancedb_upsert = True
                        self._emit(
                            "Detected an interrupted previous run (vector store ahead of "
                            "docstore); reconciling to avoid duplicate chunks."
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
                # Resync the BM25 cache to the freshly-persisted docstore.
                # Done inside the write lock so a concurrent chat cannot
                # rebuild against a stale snapshot mid-persist.
                self._invalidate_retrieval_caches()
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
        reindex. Every removal is routed through ``log_storage_deletion`` per the
        deletion-audit invariant (see the root ``CLAUDE.md``).
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
                    log_storage_deletion(f"prune_old_index_backup:{stale.name}")
                    shutil.rmtree(stale, ignore_errors=True)
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
        ``(st_size, st_mtime_ns)`` with a short monotonic TTL backstop. The dir
        going away (``/api/reset``, version-bump archive) changes the stat key →
        immediate miss → fresh count; a missing dir falls back to the raw call.
        Only the monotonic ``count > 0`` gate uses this; exact-count callers do
        not.
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

    def _validate_persisted_index_files(
        self,
        index_dir: str,
        *,
        full: bool = True,
        backend: str = VECTOR_BACKEND_SIMPLE,
        require_vector_data: bool = True,
    ) -> None:
        """Validate the files that make up a LlamaIndex checkpoint.

        On the lancedb backend the vectors live in the binary ``lancedb/`` dir,
        not ``default__vector_store.json`` — so that file is neither required
        nor expected. ``require_vector_data`` is set False when validating a
        *temporary* checkpoint dir (the lancedb table is already durable in the
        live index dir and is not copied into the temp dir).
        """
        root = Path(index_dir)
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
                self._validate_persisted_index_files(
                    str(tmp_dir),
                    backend=backend,
                    require_vector_data=(backend != VECTOR_BACKEND_LANCEDB),
                )
            target.mkdir(parents=True, exist_ok=True)
            for name in _LLAMAINDEX_PERSIST_FILES:
                src = tmp_dir / name
                if src.exists():
                    os.replace(src, target / name)
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

    def _index_documents_streaming(
        self,
        idx,
        chunks_iter,
        persist_callback: Optional[Callable[[dict], None]] = None,
        lancedb_upsert: bool = False,
        vector_backend: str = VECTOR_BACKEND_SIMPLE,
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

        for i, doc in enumerate(chunks_iter):
            if self._stop_event.is_set():
                break
            if i % 10 == 0:
                self._op_lock.heartbeat(self._lock_epoch)

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

            try:
                with self._index_mutation_lock:
                    idx.insert(doc)
                source = self._manifest_source(doc)
                manifest_counts[source] = manifest_counts.get(source, 0) + 1
                added += 1
                pending_since_persist += 1
                pending_since_compact += 1
                consecutive_failures = 0
            except Exception as exc:
                failed += 1
                consecutive_failures += 1
                if deleted_old and doc.doc_id:
                    # Old copy already removed but the re-embed failed: this chunk
                    # is now absent until the next run re-yields it (hash will not
                    # match the now-missing docstore entry).  Record it so the gap
                    # is reported, not just folded into ``failed``.
                    reinsert_failed.add(doc.doc_id)
                logger.warning("Insert failed for %s: %s", doc.doc_id, exc)
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
                    break
                # Interruptible exponential backoff before the next attempt, so a
                # transiently-failing backend (instant 400s while the embed model
                # reloads) gets wall-clock time to recover rather than burning the
                # whole breaker budget in milliseconds.  Holds no lock (the insert
                # ``with`` block already exited) and runs off the chat path (the
                # indexer released the rw write lock for the insert loop).  A
                # Cancel/Pause sets _stop_event mid-sleep → wait() returns True →
                # abort promptly, mirroring the top-of-loop stop check.
                if consecutive_failures == 1:
                    self._emit(
                        "WARNING: embedding insert failed — pausing briefly before "
                        "continuing (transient backend error; will abort if it persists)."
                    )
                backoff_s = min(
                    self._FAILURE_BACKOFF_BASE_S * (2 ** (consecutive_failures - 1)),
                    self._FAILURE_BACKOFF_CAP_S,
                )
                if backoff_s > 0 and self._stop_event.wait(backoff_s):
                    break
                continue

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
                    # Reset both gates so we don't spin: instead of retrying the
                    # checkpoint on every subsequent insert, wait another full
                    # _PERSIST_EVERY inserts AND _PERSIST_MIN_INTERVAL_S before
                    # the next attempt.
                    pending_since_persist = 0
                    last_persist_time = time.monotonic()

            # Interim lancedb compaction — independent of the JSON checkpoint so a
            # fast embedder cannot accumulate a large O(n²) version-manifest spike
            # between the (≥10-min) checkpoints.  Under the mutation lock only
            # (no docstore/JSON work), so it is far cheaper than a checkpoint.
            if (
                vector_backend == VECTOR_BACKEND_LANCEDB
                and pending_since_compact >= self._LANCEDB_COMPACT_EVERY
            ):
                # Fetch the store OUTSIDE the lock (read-only ref grab, guarded so
                # a property raise can't abort the run), compact UNDER the mutation
                # lock so retrieval can't read the table mid-optimize.
                vector_store = self._lancedb_vector_store_of(idx)
                with self._index_mutation_lock:
                    compact_lancedb_vector_store(vector_store)
                pending_since_compact = 0

        if can_increment and not self._stop_event.is_set():
            stale_doc_ids = previous_doc_ids - current_doc_ids
            for i, doc_id in enumerate(sorted(stale_doc_ids)):
                if i % 25 == 0:
                    self._op_lock.heartbeat(self._lock_epoch)
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

    def _load_vault_documents(self, vault_path: str):
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

            for scan_index, path in enumerate(sorted(vault_root.rglob("*"))):
                if self._stop_event.is_set():
                    break
                if scan_index % 25 == 0:
                    self._op_lock.heartbeat(self._lock_epoch)
                if not path.is_file() or _should_skip_path(path):
                    continue
                ext = path.suffix.lower()
                if ext in configured_image_exts:
                    # Images enter the pipeline only via the MD-attachment
                    # branch, so they are indexed for name resolution but not
                    # buffered as standalone source documents.
                    name_index.setdefault(path.name.lower(), []).append(
                        path.relative_to(vault_root).as_posix()
                    )
                    continue
                if ext not in allowed_exts:
                    continue
                rel = path.relative_to(vault_root).as_posix()
                if ext in VAULT_MD_EXTS:
                    md_paths_buffered.append((path, rel))
                elif ext in VAULT_BINARY_EXTS:
                    pdf_paths.append((path, rel))

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
                    self._op_lock.heartbeat(self._lock_epoch)
                try:
                    text = self._extract_image_description(path, vault_root, rel)
                except Exception as exc:
                    self._emit(f"WARNING: Vision indexing failed for {rel}: {exc}")
                    self._skipped_image_count += 1
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
                    self._op_lock.heartbeat(self._lock_epoch)
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
                                page_done_cb=lambda _n: self._op_lock.heartbeat(self._lock_epoch),
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
                                path, rel, ext, vault_root, signature, page_count
                            )
                            continue
                    if text:
                        yield LlamaDocument(
                            text=text,
                            metadata={"file_path": str(path), "source": rel, "extension": ext},
                        )
                except Exception as exc:
                    self._emit(f"WARNING: Failed to extract {rel}: {exc}")
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
                self._op_lock.heartbeat(self._lock_epoch)
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
                    page_done_cb=lambda _n: self._op_lock.heartbeat(self._lock_epoch),
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
        """Write *text* to *cache_file* atomically via a sibling temp file."""
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
        """Drop the cached BM25 retriever (in memory AND the on-disk sidecar)
        and the wikilink graph so the next chat rebuilds against the
        freshly-persisted docstore.

        Called after each successful ``idx.storage_context.persist(...)`` —
        both the mid-run checkpoint and the final persist — so concurrent
        chats observe the new chunks once indexing publishes them.  The
        reranker is unaffected: it depends on the model name only, not on
        index contents.  The wikilink graph has no on-disk sidecar, so it is
        a pure in-memory drop.
        """
        with self._bm25_build_lock:
            self._bm25_retriever = None
            self._bm25_cached_doc_count = -1
            shutil.rmtree(self._bm25_sidecar_dir(), ignore_errors=True)
        with self._wikilink_build_lock:
            self._wikilink_index = None
            self._wikilink_cached_doc_count = -1

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
            try:
                cached.similarity_top_k = top_k
            except Exception:
                logger.debug("Could not retune cached BM25 top_k", exc_info=True)
            return cached

        with self._bm25_build_lock:
            # Double-check under the build lock so two concurrent chats do
            # not both rebuild against the same docstore size.
            if (
                self._bm25_retriever is not None
                and self._bm25_cached_doc_count == current_count
            ):
                try:
                    self._bm25_retriever.similarity_top_k = top_k
                except Exception:
                    logger.debug("Could not retune cached BM25 top_k", exc_info=True)
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

    def _get_wikilink_index(self) -> Optional[_WikilinkGraph]:
        """Return the cached note→note wikilink graph, building it on demand.

        Mirrors ``_get_bm25_retriever``'s discipline: cached by docstore size
        so a vault with N chunks rebuilds at most once per indexing run,
        double-checked under ``_wikilink_build_lock``, and the docstore
        snapshot taken under ``_index_mutation_lock`` so an in-flight indexer
        insert/delete cannot mutate the dict mid-iteration.  The build itself
        (regex parse of node text) runs OUTSIDE the mutation lock, like BM25's
        tokenisation, so it never stalls the indexer.  Returns None when no
        index/docstore is loaded.  Reindex-free and re-embed-free — reads only
        the docstore.
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
            return graph

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
            raw = load_config().get("vault_reranker_device", "auto")
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
        weights to ~/.cache/huggingface/hub on first use); we mutate
        ``top_n`` per query rather than rebuilding the reranker.  A sticky
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
                try:
                    self._reranker.top_n = top_n
                except Exception:
                    logger.debug("Could not retune cached reranker top_n", exc_info=True)
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
                            OBSIDIAN_INDEX_DIR, full=False, backend=backend
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
        stage_cb: Optional[Callable[[str], None]] = None,
    ):
        """Single-shot RAG: retrieve, then return a streaming LLM response.

        The public chat entrypoint for non-agent vault chat (the agent's RAG
        fallback also calls it). Every keyword is a query-time knob already resolved
        body→config→default by the route's ``_resolve_chat_params`` — none of them
        touches the index, so they are all reindex-free. Ordering chosen for
        concurrency: the index is lazy-loaded under the rw write lock (double-checked),
        then the BM25 build and first-time reranker load happen **before** the read
        lock so they don't block other readers or the indexer's persist; retrieval
        runs under ``_index_mutation_lock`` (so it can't race an insert) while the LLM
        token stream runs lock-free after retrieval returns. Always retrieves with the
        index's *own* recorded embed model (``_effective_embed_name``) so a config-only
        model switch can't fuse two vector spaces. ``stage_cb`` surfaces stage labels
        as SSE ``{info}`` frames. Returns a streaming response object (``.response_gen``).
        """
        from .engine import SimpleQueryEngine

        def _stage(message: str) -> None:
            if stage_cb is not None:
                try:
                    stage_cb(message)
                except Exception:
                    logger.debug("Vault chat stage callback failed.", exc_info=True)

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
            )

        # Serialise retrieval against the indexer's idx.insert() calls.  With
        # streaming=True, query_engine.query() runs retrieval synchronously
        # and returns a StreamingResponse whose response_gen pulls LLM tokens
        # lazily, so this lock is held only for the brief retrieval phase —
        # the LLM streaming itself runs outside the lock.
        _stage("Retrieving context…")
        with self._index_mutation_lock:
            return engine.query(message)

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
        stage_cb: Optional[Callable[[str], None]] = None,
    ):
        """Retrieve evidence chunks for *message* without invoking the LLM.

        Mirrors the retrieval phase of :meth:`stream_chat`: lazy-loads
        the saved index, optionally builds BM25 + reranker, then returns
        the retrieved :class:`RetrievedChunk` list. Designed for the
        agent loop's ``vault.search`` tool so a single agent turn can
        issue several searches without paying ``stream_chat``'s LLM
        round-trip per query.

        Holds :attr:`_index_mutation_lock` for the brief retrieval
        phase, matching :meth:`stream_chat`'s discipline.
        """
        from .engine import SimpleQueryEngine

        def _stage(msg: str) -> None:
            if stage_cb is not None:
                try:
                    stage_cb(msg)
                except Exception:
                    logger.debug("Vault retrieve stage callback failed.", exc_info=True)

        # Retrieve with the index's own embed model, not whatever config says (M1).
        effective_embed = self._effective_embed_name(embed_name, _stage)
        self._ensure_index_loaded(
            provider_name=provider_name,
            embed_name=effective_embed,
            stage_cb=stage_cb,
        )

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
        # Rerank-gated, same as stream_chat: only build the graph when a
        # reranker is active (the engine skips the wrap otherwise).
        want_wikilink = wikilink_expansion and reranker is not None
        if want_wikilink:
            _stage("Building wikilink graph…")
        wikilink_graph = self._get_wikilink_index() if want_wikilink else None

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
                top_k_explicit=top_k_explicit,
                bm25_retriever=bm25_retriever,
                reranker=reranker,
                mmr_enabled=mmr_enabled,
                mmr_lambda=mmr_lambda,
                query_expansion=query_expansion,
                num_queries=num_queries,
                rerank_pool_ceiling=rerank_pool_ceiling,
                wikilink_graph=wikilink_graph,
                wikilink_expansion=wikilink_expansion,
            )

        _stage("Retrieving context…")
        with self._index_mutation_lock:
            return engine.retrieve(message)

    def read_note(
        self,
        rel_path: str,
        *,
        max_chars: int = 32000,
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
            text, extract_truncated = self._read_pdf_text(candidate, vault_root, max_chars)

        size_truncated = len(text) > max_chars
        if size_truncated:
            text = text[:max_chars]
        return text, (size_truncated or extract_truncated)

    def _read_pdf_text(
        self,
        path: Path,
        vault_root: Path,
        char_budget: int,
    ) -> tuple[str, bool]:
        """Return cached PDF text if present, otherwise a bounded fresh extract.

        Returns ``(text, truncated)``. ``truncated`` is True when the
        bounded fresh extract did not cover the whole document (page or
        char limit reached). Cache hits are never reported as truncated
        — the caller still applies its own ``max_chars`` cap.
        """
        try:
            sig = self._pdf_file_signature(path)
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
        meta = self._read_index_meta()
        if meta is None:
            return ""
        cfg = load_config()
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
                            OBSIDIAN_INDEX_DIR, full=False, backend=backend
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
        self.reset_prewarm()
        # Hygiene: the just-dropped index/BM25 objects sit in reference
        # cycles (LlamaIndex stores back-reference their context); collect
        # now so /api/reset actually releases them.
        gc.collect()

obsidian_manager = ObsidianVaultManager()
