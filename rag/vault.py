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
    lancedb_available,
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
    """Refactored ObsidianVaultManager for modularity and provider agnosticism."""

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

        # Hybrid retrieval (BM25) cache.  Built lazily on the first chat that
        # asks for hybrid mode and reused thereafter so we do not retokenise
        # the entire docstore on every Send.  Invalidated by node-count
        # mismatch (cheap) and explicitly after each successful persist in
        # the indexer (correct).
        self._bm25_retriever: Optional[Any] = None
        self._bm25_cached_doc_count: int = -1
        self._bm25_build_lock: threading.Lock = threading.Lock()
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
        with self._messages_lock:
            messages = list(self._status_messages)
            self._status_messages.clear()
            return messages

    def clear_status_messages(self) -> None:
        with self._messages_lock:
            self._status_messages.clear()

    def try_acquire_lock(self, ttl: int = 3600) -> bool:
        acquired = self._op_lock.try_acquire(ttl)
        if acquired:
            self._lock_epoch = self._op_lock.epoch
        return acquired

    def release_lock(self):
        self._op_lock.release(self._lock_epoch)

    def force_release(self) -> bool:
        return self._op_lock.force_release()

    def index_vault(self, llm_name: str, embed_name: str, provider_name: str = "ollama") -> None:
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
                    has_vector_data = self._index_dir_has_vector_data(OBSIDIAN_INDEX_DIR)
                    if (
                        prev_meta.get("version") == OBSIDIAN_INDEX_VERSION
                        and has_vector_data
                        and not embed_changed
                    ):
                        is_incremental = True
                    elif embed_changed and has_vector_data:
                        # Mixing new-model vectors into an old-model store would
                        # corrupt similarity search silently — force a rebuild.
                        # A paused_scan with no persisted vectors has nothing to
                        # be incompatible with, so we don't warn in that case.
                        self._emit(
                            f"WARNING: Existing index was built with embedding "
                            f"model '{prev_meta['embed']}', but the current model is '{embed_name}'. "
                            "New chunks would have incompatible vector representations; "
                            "starting a fresh vector index."
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
                    idx, chunks_iter, _persist_callback, lancedb_upsert=lancedb_upsert
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
            # _simple_vector_store_has_any_embedding.
            return lancedb_table_count(index_dir) > 0
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
    # fail.  Tighter than "100 failures total" because in practice an embedding
    # backend that is going to recover does so within a handful of retries —
    # 20 consecutive failures almost always means the backend is unreachable
    # and continuing wastes hours.
    _PERSIST_EVERY = 500
    _PERSIST_MIN_INTERVAL_S = 600
    _MAX_CONSECUTIVE_FAILURES = 20

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
                    nodes = md_parser.get_nodes_from_documents([parser_doc])
                except ValueError as exc:
                    if "Unknown document type" not in str(exc):
                        raise
                    nodes = [TextNode(text=doc.text, metadata=doc.metadata)]
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
    ) -> list[str]:
        """Return vault-relative posix paths for attachment references in *text*.

        Scans both Obsidian wikilinks (``![[image.png]]``) and inline markdown
        links (``[label](file.pdf)``).  Targets are resolved relative to the
        markdown file's parent directory, deduplicated, and expressed as
        vault-relative posix paths.  External URLs and anchors are dropped.
        Retrieval can join these back to indexed chunks by matching the
        ``{rel_path}::`` prefix on doc_ids.
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
            resolved = self._resolve_md_attachment(target, md_path, vault_root)
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
    ) -> str:
        """Normalise *target* (relative to *md_path*'s parent) into a
        vault-relative posix path.  Returns "" if the target escapes the
        vault or cannot be expressed below *vault_root*."""
        try:
            base = md_path.parent.as_posix()
            joined = os.path.normpath(os.path.join(base, target))
            rel = os.path.relpath(joined, vault_root.as_posix())
        except ValueError:
            return ""
        rel = rel.replace(os.sep, "/")
        if rel == ".." or rel.startswith("../") or os.path.isabs(rel):
            return ""
        return rel

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
                except Exception:
                    pass

            try:
                with self._index_mutation_lock:
                    idx.insert(doc)
                source = self._manifest_source(doc)
                manifest_counts[source] = manifest_counts.get(source, 0) + 1
                added += 1
                pending_since_persist += 1
                consecutive_failures = 0
            except Exception as exc:
                failed += 1
                consecutive_failures += 1
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
                    last_persist_time = time.monotonic()
                except Exception as exc:
                    self._emit(f"WARNING: mid-run checkpoint failed: {exc}")
                    # Reset both gates so we don't spin: instead of retrying the
                    # checkpoint on every subsequent insert, wait another full
                    # _PERSIST_EVERY inserts AND _PERSIST_MIN_INTERVAL_S before
                    # the next attempt.
                    pending_since_persist = 0
                    last_persist_time = time.monotonic()

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

            for scan_index, path in enumerate(sorted(vault_root.rglob("*"))):
                if self._stop_event.is_set():
                    break
                if scan_index % 25 == 0:
                    self._op_lock.heartbeat(self._lock_epoch)
                if not path.is_file() or _should_skip_path(path):
                    continue
                ext = path.suffix.lower()
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
                for attachment in self._extract_md_attachments(text, path, vault_root):
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
                            # Normal case — extract in one call.  This path must
                            # stay byte-identical (text, metadata, single
                            # document) to the pre-per-range loader: any change
                            # would shift chunk hashes and re-embed every small
                            # PDF in the vault.
                            sections = extract_structured_from_pdf(
                                str(path), ocr_cb=glm_ocr_manager.extract_page_text
                            )
                            text = sections.full_text
                            if text:
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
                )
                text = sections.full_text
                if text:
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
        so the next chat rebuilds against the freshly-persisted docstore.

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
        stage_cb: Optional[Callable[[str], None]] = None,
    ):
        from .engine import SimpleQueryEngine

        def _stage(message: str) -> None:
            if stage_cb is not None:
                try:
                    stage_cb(message)
                except Exception:
                    logger.debug("Vault chat stage callback failed.", exc_info=True)

        self._ensure_index_loaded(
            provider_name=provider_name,
            embed_name=embed_name,
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
                embed_name,
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

        self._ensure_index_loaded(
            provider_name=provider_name,
            embed_name=embed_name,
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

        with self._rw_lock.read_lock():
            local_index = self._index
            if local_index is None:
                raise RuntimeError("Index not found. Please index the vault first.")
            engine = SimpleQueryEngine(
                local_index,
                llm_name,
                embed_name,
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
