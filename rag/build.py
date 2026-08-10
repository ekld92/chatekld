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
from typing import Any, Callable, NamedTuple, Optional

from core.constants import OBSIDIAN_INDEX_VERSION
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

class IndexBuildMixin:
    """The eight ``index_vault`` phases, mixed into :class:`ObsidianVaultManager`.

    Never instantiated on its own. Every attribute these methods touch
    (``_index``, ``_rw_lock``, ``_status_lock``, ``_index_mutation_lock``,
    ``_stop_event``, the status ring) and every helper they call
    (``_write_index_meta``, ``_persist_index_checkpoint``,
    ``_archive_old_index_dir``, ``_build_index_for_backend``, …) is owned by the
    manager and resolved through ``self``.
    """

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
            os.makedirs(index_dir, exist_ok=True)
            meta_path = os.path.join(index_dir, "obsidian_meta.json")
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
                has_vector_data = self._index_dir_has_vector_data(index_dir)
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

            # Explicit rebuild opt-in (2026-07-27). Placed BEFORE the
            # identity gates so they never fire on a run the user already
            # approved: dropping straight to the shared not-incremental
            # branch is byte-identical to what a version bump does, so
            # there is no second code path to keep in sync. `protect_archive`
            # marks the resulting .bak as user-approved-rebuild fallout so
            # the retention sweep cannot evict it behind a couple of
            # unrelated version-bump archives (see _prune_old_index_backups).
            protect_archive = False
            if is_incremental and force_fresh:
                is_incremental = False
                protect_archive = True
                self._emit(
                    "Rebuilding the vector index from scratch (explicitly "
                    "requested); the previous index is moved aside, not deleted."
                )

            # A1 (2026-07-13): refuse a cross-vault incremental. The meta now
            # records the vault it was built from; if the recorded path is
            # present and differs from the vault we are indexing now, the
            # existing chunks belong to a DIFFERENT vault and extending them
            # would fuse two vaults' content into one store. A missing/blank
            # recorded path is a pre-fix index (grandfathered): adopt it for
            # the current vault silently — the field is stamped at the next
            # persist below. `vault` arrives resolved (same as _write_index_meta).
            if is_incremental:
                prev_vault = str(prev_meta.get("vault_path") or "")
                if prev_vault and vault and prev_vault != vault:
                    is_incremental = False
                    # A vault switch is an unambiguous user action with an
                    # unambiguous outcome (the other vault's index is useless
                    # here), so this one still rebuilds automatically — but
                    # its archive is protected: switching back and forth
                    # between two vaults must not grind the FIRST vault's
                    # index out of the retention window.
                    protect_archive = True
                    self._emit(
                        "WARNING: the existing index was built from a different "
                        "vault; rebuilding the vector index from scratch so the "
                        "two vaults' content cannot be mixed."
                    )

            # Embed-BACKEND gate (2026-07-26; turned from a silent rebuild
            # into a REFUSAL 2026-07-27).
            #
            # Why the gate exists: `embed` (checked above) pins the model
            # NAME, but the same name served by ollama vs LM Studio can be a
            # differently quantized/pooled build, so its vectors are not
            # comparable — exactly the silent similarity-search corruption
            # the embed-model gate exists to prevent. Extending such an index
            # incrementally must not happen.
            #
            # Why it must not rebuild on its own: the trigger is NOT a
            # deliberate "re-embed my vault" action. `resolve_embed_provider`
            # returns the CHAT provider unchanged whenever it is local and
            # only falls back to `embed_provider` when the chat provider is
            # online, so the resolved backend flips on routine chat-dropdown
            # moves the user reads as unrelated: ollama <-> lm_studio, and
            # lm_studio -> any online provider (which falls back to the
            # untouched default "ollama"). Auto-rebuilding there spends hours
            # of embedding on a real vault (measured: 411 823 chunks / 5.4 GB)
            # with no confirmation, silently flips the vector backend as a
            # side effect (the fresh branch calls `_resolve_fresh_backend()`,
            # i.e. the config knob, where an incremental inherits the index's
            # own), and archives the old index — so the THIRD such toggle used
            # to prune the last good copy. Refusing costs one failed run;
            # rebuilding costs the index.
            #
            # Precedent for the shape: the LanceDB-missing preflight above
            # refuses with an actionable message rather than proceeding into
            # a destructive fresh build. Same contract here — nothing is
            # archived, nothing is deleted, nothing is re-embedded, and the
            # message names both recovery routes (put the provider back, or
            # opt in explicitly).
            #
            # Grandfathering (item 2, 2026-07-27) is resolved by
            # `_prev_embed_provider`: a missing `embed_provider` falls back
            # to the recorded `provider` when THAT is local. See that helper
            # for why an online recorded provider is not usable as a proxy.
            # An index that yields no usable previous name at all is still
            # adopted silently and stamped at the next persist, so the
            # upgrade forces no reindex — the invariant this gate inherited
            # from the A1 vault_path stamp.
            #
            # Deliberately subordinate to the two gates above: after a vault
            # switch (or an approved force_fresh) the old vectors are being
            # discarded anyway, so refusing over their backend would block a
            # rebuild the user already asked for, for no protective gain.
            if is_incremental:
                prev_ep = self._prev_embed_provider(prev_meta)
                if prev_ep and embed_provider_name and prev_ep != embed_provider_name:
                    chunks = self._indexed_chunk_count_hint()
                    scale = (
                        f"all {chunks:,} indexed chunks"
                        if chunks else "every chunk in the vault"
                    )
                    msg = (
                        f"ERROR: the existing index was embedded through "
                        f"'{prev_ep}', but embeddings would now come from "
                        f"'{embed_provider_name}'. The same embedding model "
                        f"name can yield differently-quantized vectors across "
                        f"local backends, so the two cannot be mixed — and "
                        f"continuing would mean re-embedding {scale} from "
                        f"scratch (hours of compute). Refusing to touch the "
                        f"index. Either switch the provider back to "
                        f"'{prev_ep}' in Settings and reindex, or — if you "
                        f"really do want a full rebuild — use the app's "
                        f"“Rebuild from scratch” action (headless callers: "
                        f"reindex with force_fresh). The previous index is "
                        f"archived, not deleted."
                    )
                    self._emit(msg)
                    # The machine code is what lets the client swap the
                    # useless Retry (this gate re-fails identically) for a
                    # confirm-gated force_fresh rebuild (2026-07-29 audit
                    # 0.1: the refusal's recovery instructions previously
                    # never survived to the screen).
                    self._record_index_error("embed_backend_mismatch", msg)
                    with self._status_lock:
                        self._index_state = "error"
                        self._current_phase = "error"
                    # Returning from inside the rw write lock is safe: the
                    # context manager releases it, the op-lock is released by
                    # the admitting route's `finally`, and the outer `finally`
                    # still runs gc.collect(). Nothing has been written yet —
                    # this gate sits before the archive/build branch.
                    return None

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
                idx = self._build_index_for_backend(
                    fresh=False, backend=vector_backend, embed_model=embed_model
                )

            self._index = idx
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
        # docstore. Detect that with two O(1) counts; when the table has
        # more rows than the docstore has nodes, re-process this run with
        # delete-before-insert so resumed chunks replace their orphan rows
        # instead of duplicating them. No-op for fresh builds and the simple
        # backend; a single cheap count comparison otherwise.
        lancedb_upsert = False
        if vector_backend == VECTOR_BACKEND_LANCEDB and is_incremental:
            try:
                vec_rows = lancedb_table_count(index_dir)
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
                elif 0 <= vec_rows < node_count:
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
                vault_path=vault,
                embed_provider=embed_provider_name,
            )
            self._write_index_manifest(manifest_counts, vault)

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
        if not self._stop_event.is_set():
            try:
                if load_config().get("vault_hybrid_enabled", True):
                    threading.Thread(
                        target=lambda: self._get_bm25_retriever(top_k=8),
                        name="bm25-postindex-rebuild",
                        daemon=True,
                    ).start()
            except Exception:  # noqa: BLE001 — a failed warm just defers to the chat path
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
