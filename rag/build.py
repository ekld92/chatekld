"""The vault index-build phases — the body of ``ObsidianVaultManager.index_vault``.

Extracted from ``rag/vault.py`` (Plan 9, step 4b) after step 4a had already
split the 654-line ``index_vault`` into these eight named phases. They arrive
here as :class:`IndexBuildMixin`, mixed into the manager, so every ``self.``
reference in the bodies below is unchanged and the orchestration — every early
return, and which of them run inside ``index_vault``'s ``try`` — stays in
``rag/vault.py`` where it can be read in one place.

**Why the four keyword-only parameters.** ``index_dir``, ``get_provider``,
``lancedb_available`` and ``load_config`` are module globals in ``rag/vault.py``
that the test suite patches by their ``rag.vault.<name>`` path. A global read
relocated into THIS module would resolve against this module's namespace and
silently ignore those patches — tests would keep passing while asserting
nothing. So ``index_vault`` reads them in its own namespace at call time and
passes them in. The parameter names deliberately shadow the global names, which
is why the bodies are byte-identical to their pre-move source apart from
``OBSIDIAN_INDEX_DIR`` -> ``index_dir``.

Nothing here imports ``rag.vault``; the dependency runs one way only.
"""
import json
import logging
import os
import threading
from typing import TYPE_CHECKING, Any, Callable, NamedTuple, Optional

if TYPE_CHECKING:
    from core.utils import ReaderWriterLock

from rag.docstore_access import node_count
from rag.lancedb_store import (
    VECTOR_BACKEND_LANCEDB,
    lancedb_delete_ids,
    lancedb_list_doc_ids,
    lancedb_table_count,
)

logger = logging.getLogger(__name__)


class _PersistTarget(NamedTuple):
    """Everything the two persist paths of one indexing run need to agree on.

    Introduced by the Plan 9 step-4a split. The mid-run checkpoint
    (``_index_make_persist_callback``) and the final persist
    (``_index_finalise_persist``) write the SAME meta fields for the SAME index
    into the SAME file; before the split they were a closure and an inline block
    that happened to close over identical locals. Bundling those locals makes
    that shared identity explicit and un-driftable — and keeps both signatures
    to one argument instead of seven.
    """
    idx: Any
    vector_backend: str
    meta_path: str
    embed_name: str
    provider_name: str
    vault: str
    embed_provider_name: str
    #: The run's op-lock acquisition token. Both persist paths heartbeat around
    #: their (long, unheartbeated before 2026-09-02) checkpoint write; 0 means
    #: "no lock held" and every heartbeat is inert, which is what direct/test
    #: callers pass.
    op_epoch: int = 0

class IndexBuildMixin:
    """The eight ``index_vault`` phases, mixed into :class:`ObsidianVaultManager`.

    Never instantiated on its own. Every attribute these methods touch
    (``_index``, ``_rw_lock``, ``_status_lock``, ``_index_mutation_lock``,
    ``_stop_event``, the status ring) and every helper they call
    (``_write_index_meta``, ``_persist_index_checkpoint``,
    ``_archive_old_index_dir``, ``_build_index_for_backend``, …) is owned by the
    manager and resolved through ``self``.

    The ``TYPE_CHECKING`` block below DECLARES the host-class members these
    methods reach through ``self``. It has no runtime effect (type-only declarations)
    and exists so mypy can check this file rather than reporting one
    "IndexBuildMixin has no attribute" per use — the class of error that made
    69 of this file's findings noise, and would have hidden a real typo among
    them. Anything added here must actually exist on ``ObsidianVaultManager``.
    """

    if TYPE_CHECKING:  # pragma: no cover - declarations, never executed
        _status_lock: threading.Lock
        _rw_lock: ReaderWriterLock
        _index_mutation_lock: threading.Lock
        _stop_event: threading.Event
        _pause_requested: bool
        _bm25_postindex_generation: int
        _bm25_fingerprint: tuple[int, int, int, int, int]
        _emit: Callable[[str], None]
        _heartbeat_or_stop: Callable[[int], None]
        _replace_index: Callable[[Any], None]

        def _record_index_error(self, code: str, detail: str) -> None: ...
        def _embed_batch_size(self) -> int: ...
        @staticmethod
        def _prev_embed_provider(prev_meta: Optional[dict]) -> str: ...
        def _read_index_meta(self) -> Optional[dict]: ...
        def _indexed_chunk_count_hint(self) -> int: ...
        def _index_dir_has_vector_data(self, index_dir: str) -> bool: ...
        def _migrate_legacy_caches(self) -> None: ...
        def _archive_old_index_dir(self, prev_meta: dict, *, protected: bool = False) -> str: ...
        def _resolve_fresh_backend(self) -> str: ...
        def _build_index_for_backend(self, *, fresh: bool, backend: str, embed_model: Any) -> Any: ...
        def _resolve_existing_backend(self, prev_meta: Optional[dict]) -> str: ...
        def _set_prewarm(self, status: str, message: str = '', generation: int | None = None) -> None: ...
        def _persist_index_checkpoint(self, idx: Any, backend: str = 'simple') -> None: ...
        def _write_index_meta(
            self, path: str, embed_name: str, provider_name: str, *, partial: bool,
            phase: str, has_vector_data: bool, inserted_this_run: int,
            vector_backend: str = 'simple', vault_path: Optional[str] = None,
            embed_provider: str = '',
        ) -> None: ...
        def _write_index_manifest(self, counts: dict[str, int], vault_path: str) -> None: ...
        def _invalidate_retrieval_caches(self, *, persistent: bool = True) -> None: ...
        def _index_has_vector_data(self, idx: Any) -> bool: ...
        def _get_bm25_retriever(
            self, top_k: int, stage_cb: Optional[Callable[[str], None]] = None,
        ) -> Optional[Any]: ...
        def _bm25_sidecar_dir(self) -> str: ...
        def _cache_fingerprint(self, idx: Any = None) -> tuple[int, int, int, int, int] | None: ...

    def _index_prepare_embedding(
        self, llm_name: str, embed_name: str, provider_name: str,
        *, get_provider: Callable[[str], Any],
    ) -> Optional[tuple[Any, str]]:
        """Resolve the embed provider + model for this run.

        Returns ``(embed_model, embed_provider_name)``, or ``None`` when the run
        must abort — in which case the message, the error record and the
        ``error`` state have already been set, exactly as when this was inline.
        Called BEFORE ``index_vault``'s ``try``, so an exception it does not
        catch itself propagates out of the run without hitting ``gc.collect()``
        — unchanged from the pre-split flow.
        """
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

        # Availability is admission evidence, not a reason to mutate caches and
        # discover failure later during insertion. No store opens on failure.
        try:
            _installed, _list_err = provider.get_models()
        except Exception:
            _installed, _list_err = [], "probe failed"
        if _list_err or not _installed:
            msg = f"ERROR: Local embedding provider '{embed_provider_name}' is unavailable or reports no installed models."
            self._emit(msg)
            self._record_index_error("embedding_unavailable", msg)
            with self._status_lock:
                self._index_state = "error"
            return None
        if not _list_err and _installed:
            _base = embed_name.split(":")[0]
            if embed_name not in _installed and not any(
                m.split(":")[0] == _base for m in _installed
            ):
                msg = (
                    f"ERROR: Embedding model '{embed_name}' is not installed on "
                    f"'{embed_provider_name}'. Pull or load it (e.g. "
                    f"`ollama pull {embed_name}`) in the Models panel, then reindex."
                )
                self._emit(msg)
                self._record_index_error("index_failed", msg)
                with self._status_lock:
                    self._index_state = "error"
                return None

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
            msg = f"ERROR: Failed to initialize embedding model: {e}"
            self._emit(msg)
            self._record_index_error("index_failed", msg)
            with self._status_lock:
                self._index_state = "error"
            return None
        return embed_model, embed_provider_name

    def _index_lancedb_preflight(
        self, *, index_dir: str, lancedb_available: Callable[[], bool],
    ) -> bool:
        """False when an existing LanceDB index cannot be opened here.

        The caller aborts on False; the refusal message/state are set here.
        """
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
        _meta_path = os.path.join(index_dir, "obsidian_meta.json")
        if os.path.exists(_meta_path):
            try:
                with open(_meta_path) as _f:
                    _existing_backend = (json.load(_f) or {}).get("vector_backend")
            except Exception:
                _existing_backend = None
            if _existing_backend == VECTOR_BACKEND_LANCEDB and not lancedb_available():
                msg = (
                    "ERROR: The existing vault index uses the LanceDB backend, but "
                    "LanceDB cannot be loaded in this environment (its dependencies — "
                    "e.g. pandas — are missing). Refusing to reindex, which would "
                    "overwrite the existing index. Reinstall the dependencies "
                    "(pip install -r requirements.txt -c constraints.txt) and "
                    "restart, then reindex."
                )
                self._emit(msg)
                self._record_index_error("lancedb_unavailable", msg)
                with self._status_lock:
                    self._index_state = "error"
                    # Consistency (2026-07-29 audit): the terminal refusals in
                    # the run body (embed-backend gate, generic except) stamp
                    # the phase too; this refusal is the same class and used to
                    # leave the phase at "initialising".
                    self._current_phase = "error"
                return False
        return True

    def _index_open_store(
        self,
        vault: str,
        embed_name: str,
        embed_provider_name: str,
        embed_model: Any,
        force_fresh: bool,
        *,
        index_dir: str,
        op_epoch: int = 0,
    ) -> Optional[tuple[Any, str, bool, str]]:
        """Decide incremental-vs-fresh, run the identity gates, open/build the
        index and publish it — the whole rw-write-locked setup phase.

        Returns ``(idx, vector_backend, is_incremental, meta_path)``, or ``None``
        when the embed-BACKEND gate refuses the run. Returning from inside the
        write lock is safe and is what the inline version did: the context
        manager releases it, nothing has been written yet, and the caller's
        ``finally`` still runs ``gc.collect()``.
        """
        # Setup phase: load or create the index under a brief write lock
        # and publish it immediately so chat queries that arrive during
        # indexing can use the in-progress index instead of blocking on a
        # multi-hour run.  The insert loop below runs without the rw lock.
        with self._rw_lock.write_lock():
            from rag.index_admission import index_admission
            admission = index_admission(self, index_dir, vault, embed_name,
                                        embed_provider_name, force_fresh=force_fresh)
            if not admission["ready"]:
                msg = (f"ERROR: Index update refused: {admission['reason']}. "
                       "Restore compatible settings or explicitly confirm Rebuild from scratch.")
                if admission["reason"] == "embed_backend_mismatch":
                    previous = self._prev_embed_provider(self._read_index_meta() or {})
                    chunks = self._indexed_chunk_count_hint()
                    scale = f"all {chunks:,} indexed chunks" if chunks else "every chunk in the vault"
                    msg = (f"ERROR: Existing index was embedded through '{previous}', but the selected "
                           f"backend is '{embed_provider_name}'. Refusing to change {scale}. "
                           f"Either switch the provider back to '{previous}' or explicitly confirm "
                           "Rebuild from scratch. The existing index is unchanged.")
                self._emit(msg)
                self._record_index_error(admission["reason"], msg)
                with self._status_lock:
                    self._index_state = "error"
                    self._current_phase = "error"
                return None
            meta_path = os.path.join(index_dir, "obsidian_meta.json")
            prev_meta = self._read_index_meta() or {}
            is_incremental = admission["mode"] == "incremental"
            protect_archive = bool(force_fresh)
            if not is_incremental:
                if self._index_dir_has_vector_data(index_dir):
                    self._migrate_legacy_caches()
                    bak = self._archive_old_index_dir(
                        prev_meta, protected=protect_archive
                    )
                    if bak:
                        self._emit(
                            "WARNING: previous index version is incompatible — "
                            f"moved aside to {bak}. Indexing from scratch."
                        )
                os.makedirs(index_dir, exist_ok=True)
                # Fresh build: backend chosen by the config knob (default
                # simple). An incremental run instead inherits whatever the
                # existing index was written with — never the config knob.
                vector_backend = self._resolve_fresh_backend()
                idx = self._build_index_for_backend(
                    fresh=True, backend=vector_backend, embed_model=embed_model
                )
            else:
                vector_backend = self._resolve_existing_backend(prev_meta)
                # PERF (2026-09-02): one long, unheartbeated, silent window.
                #   Before:    loading an existing store is a single call that
                #              parses the whole docstore/index-store — minutes
                #              on a large vault — inside the rw write lock and
                #              with NO heartbeat against the op-lock's
                #              3600 s TTL and NO status line. A stall here read
                #              as a hang, and a TTL lapse went unnoticed.
                #   Change:    a stage line before, and `_heartbeat_or_stop`
                #              after, so the wait reads as a stage and a lost
                #              lock aborts the run instead of continuing as a
                #              second concurrent writer.
                #   Safe because:
                #              a heartbeat only refreshes a deadline; the load
                #              itself is untouched, and `op_epoch == 0` (every
                #              direct/test caller) makes both calls inert.
                #   Preserves: "A `False` heartbeat sets the stop event, never
                #              discarded" — `_heartbeat_or_stop` is the shared
                #              helper that does exactly that.
                #   Lock/thread contract:
                #              unchanged; still inside the rw write lock.
                #   Pinned by: test_vault_regressions.py::TestLongPhasesHeartbeat
                self._emit("Loading the existing index from disk…")
                idx = self._build_index_for_backend(
                    fresh=False, backend=vector_backend, embed_model=embed_model
                )
                self._heartbeat_or_stop(op_epoch)

            self._replace_index(idx)
            # Once the indexer publishes self._index, prewarm has nothing
            # left to do — chat will use this index directly.  Mark ready
            # so the UI banner clears even when the user reaches a populated
            # index via reindexing rather than via launch-time prewarm.
            self._set_prewarm("ready", "Vault is ready.")
        return idx, vector_backend, is_incremental, meta_path

    def _index_make_persist_callback(
        self, target: "_PersistTarget",
    ) -> Callable[[dict[str, int]], None]:
        """Build the mid-run checkpoint callback handed to the streaming inserter."""
        idx = target.idx
        vector_backend = target.vector_backend
        meta_path = target.meta_path
        embed_name = target.embed_name
        provider_name = target.provider_name
        vault = target.vault
        embed_provider_name = target.embed_provider_name
        op_epoch = target.op_epoch

        def _persist_callback(manifest_counts_so_far: dict[str, int]) -> None:
            """Mid-run checkpoint — bounds re-work on crash to the persist cadence.

            Holds ``_index_mutation_lock`` for the persist so a chat
            retrieval cannot iterate the vector store dict while
            LlamaIndex's persistence layer is reading it.  Python concurrent
            dict reads are safe today, but pinning persist behind the same
            lock that serialises inserts protects against future LlamaIndex
            changes that touch internal state during serialisation.
            """
            # PERF (2026-09-02): the checkpoint persist is one unheartbeated
            # window against a 3600 s TTL (it re-serialises and re-validates
            # the ENTIRE store). Same treatment as the store load above: a
            # stage line so a stall reads as a stage, and a heartbeat after so
            # a lapse aborts rather than continuing as a second writer.
            self._emit("Saving a checkpoint of the index…")
            with self._rw_lock.write_lock():
                with self._index_mutation_lock:
                    self._persist_index_checkpoint(idx, vector_backend)
                self._heartbeat_or_stop(op_epoch)
                self._write_index_meta(
                    meta_path,
                    embed_name,
                    provider_name,
                    partial=True,
                    phase="paused_partial",
                    has_vector_data=True,
                    inserted_this_run=sum(manifest_counts_so_far.values()),
                    vector_backend=vector_backend,
                    vault_path=vault,
                    embed_provider=embed_provider_name,
                )
                self._write_index_manifest(manifest_counts_so_far, vault)
            # Drop the cached BM25 retriever so the next hybrid chat
            # rebuilds against the just-persisted docstore.  Outside the
            # rw write lock so a concurrent chat that is mid-build does
            # not deadlock on a re-entrant acquire.
            self._invalidate_retrieval_caches()

        return _persist_callback

    def _index_reconcile_lancedb_drift(
        self, idx: Any, vector_backend: str, is_incremental: bool,
        *, index_dir: str,
    ) -> bool:
        """Return the ``lancedb_upsert`` flag, sweeping orphan rows when the
        vector table is ahead of the docstore (crash-drift recovery)."""
        # Crash-drift recovery (lancedb only): the binary store is durable
        # on insert while the docstore is persisted at the checkpoint
        # cadence, so a hard crash can leave the vector table *ahead* of the
        # docstore. Detect that with two backend counts; when the table has
        # more rows than the docstore has nodes, re-process this run with
        # delete-before-insert so resumed chunks replace their orphan rows
        # instead of duplicating them. No-op for fresh builds and the simple
        # backend; a single cheap count comparison otherwise.
        lancedb_upsert = False
        if vector_backend == VECTOR_BACKEND_LANCEDB and is_incremental:
            try:
                vec_rows = lancedb_table_count(index_dir)
                live_node_count = node_count(idx.docstore)
                if vec_rows > live_node_count:
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
                    lancedb_ids = lancedb_list_doc_ids(index_dir)
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
                elif 0 <= vec_rows < live_node_count:
                    self._emit(
                        "WARNING: vector store has fewer rows than docstore nodes "
                        "(possible torn write); a full reindex will fully resync."
                    )
            except Exception:
                logger.debug("lancedb drift check failed", exc_info=True)
        return lancedb_upsert

    def _index_finalise_persist(
        self,
        target: "_PersistTarget",
        *,
        added: int,
        skipped: int,
        manifest_counts: dict[str, int],
    ) -> None:
        """Final persist + meta + manifest under one brief write lock."""
        idx = target.idx
        vector_backend = target.vector_backend
        meta_path = target.meta_path
        embed_name = target.embed_name
        provider_name = target.provider_name
        vault = target.vault
        embed_provider_name = target.embed_provider_name

        # Final phase: persist and update meta under a brief write lock.
        # The persist is also serialised against the chat retrieval path
        # via the mutation lock — see _persist_callback for the rationale.
        # Stage line + heartbeat for the same reason as the mid-run one.
        self._emit("Saving the index…")
        with self._rw_lock.write_lock():
            with self._index_mutation_lock:
                self._persist_index_checkpoint(idx, vector_backend)
            self._heartbeat_or_stop(target.op_epoch)
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
            # DURABILITY (2026-09-02, performance plan Phase 5): the manifest
            # is written BEFORE the meta that advertises it.
            # Before: the meta file — the one every loader reads first, and
            #   whose `phase: done` is what makes an index look complete — was
            #   written first, so a crash in the gap left a "done" index whose
            #   indexed-materials manifest was missing or stale. The materials
            #   route then falls back to parsing the whole docstore (the very
            #   cost Phase 2 removed from the request thread).
            # Change: swap the two writes. Both are already atomic; only the
            #   order changes.
            # Safe because: the manifest is self-describing and harmless on its
            #   own — a manifest newer than its meta is exactly what an
            #   interrupted run should leave, and the next run rewrites it.
            #   Nothing between the two writes reads either.
            # Lock/thread contract: unchanged; both stay inside the same brief
            #   write-lock hold.
            # Pinned by: test_vault_regressions.py::TestFinalPersistWriteOrder.
            self._write_index_manifest(manifest_counts, vault)
            self._write_index_meta(
                meta_path,
                embed_name,
                provider_name,
                partial=bool(is_paused),
                phase=final_phase,
                has_vector_data=has_vector_data,
                inserted_this_run=sum(manifest_counts.values()),
                vector_backend=vector_backend,
                vault_path=vault,
                embed_provider=embed_provider_name,
            )

    def _index_kick_bm25_rebuild(
        self, *, load_config: Callable[[], dict],
    ) -> None:
        """Warm BM25 off the chat path after a run that COMPLETED."""
        # Background BM25 rebuild (2026-07-22 audit P4a): the invalidation
        # above rmtree'd the sidecar, so the FIRST hybrid chat after a
        # reindex previously re-tokenised the whole docstore on the chat
        # worker with a single info frame — on a large vault that exceeds
        # the SSE stall guard and reports "Generation timed out" while
        # the build keeps running (and holds _bm25_build_lock against the
        # retry). Rebuild off the chat path instead, prewarm-style:
        # best-effort, daemon, only after a run that COMPLETED (a
        # cancelled/paused run resumes soon and would churn the build).
        # The build-lock discipline in _get_bm25_retriever makes a chat
        # arriving mid-rebuild simply queue on the lock and reuse the
        # result.
        with self._status_lock:
            self._bm25_postindex_generation += 1
            generation = self._bm25_postindex_generation
            self._bm25_postindex_status = 'unknown'
        if not self._stop_event.is_set():
            try:
                if load_config().get("vault_hybrid_enabled", True):
                    with self._status_lock:
                        self._bm25_postindex_status = 'preparing'

                    def warm() -> None:
                        status = 'unknown'
                        try:
                            retriever = self._get_bm25_retriever(top_k=8)
                            meta = self._read_index_meta() or {}
                            with open(os.path.join(self._bm25_sidecar_dir(), 'sidecar_meta.json'), encoding='utf-8') as f:
                                sidecar = json.load(f)
                            with self._index_mutation_lock:
                                fingerprint = self._cache_fingerprint()
                                current = fingerprint == self._bm25_fingerprint
                            if (retriever is not None and current and fingerprint
                                    and meta.get('indexed_at') and sidecar.get('indexed_at') == meta['indexed_at']
                                    and sidecar.get('doc_count') == fingerprint[-1]):
                                status = 'ready'
                        except Exception:  # noqa: BLE001 — best-effort warm; never claim readiness
                            logger.debug('Post-index BM25 status unavailable', exc_info=True)
                        with self._status_lock:
                            if self._bm25_postindex_generation == generation:
                                self._bm25_postindex_status = status

                    threading.Thread(
                        target=warm,
                        name="bm25-postindex-rebuild",
                        daemon=True,
                    ).start()
                else:
                    with self._status_lock:
                        self._bm25_postindex_status = 'disabled'
            except Exception:  # noqa: BLE001 — a failed warm just defers to the chat path
                with self._status_lock:
                    if self._bm25_postindex_generation == generation:
                        self._bm25_postindex_status = 'unknown'
                logger.debug("post-index BM25 rebuild kick failed", exc_info=True)

    def _index_report_outcome(
        self,
        added: int,
        skipped: int,
        deleted: int,
        failed: int,
        manifest_counts: dict[str, int],
    ) -> None:
        """Set the terminal state and emit the run summary."""
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
