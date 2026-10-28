"""Retrieval-cache sidecars: the BM25 retriever and the wikilink note→note graph.

Originally extracted from ``rag/vault.py`` (Plan 9, step 2) as a move/re-export;
2026-10-01 adds serialized counts and process-local mutation fingerprints.
``ObsidianVaultManager`` picks them up by inheriting :class:`RetrievalCacheMixin`,
so ``obsidian_manager._get_bm25_retriever(...)`` /
``ObsidianVaultManager._build_wikilink_graph`` and every existing test patch
target keep working unchanged.

Both caches follow ONE documented discipline (``rag/CLAUDE.md`` → *Retrieval
caches*); co-locating them makes that discipline enforceable:

* cached in memory by index identity, mutation epoch and docstore size, double-checked under a per-cache build lock;
* the docstore snapshot is taken under ``_index_mutation_lock``, the expensive
  build (tokenise / regex-sweep) runs OUTSIDE it so it can never stall the indexer;
* a process-cold miss consults an on-disk sidecar under the index dir, validated
  against BOTH the live docstore count and the index meta's ``indexed_at`` stamp;
* persisting is skipped while an indexing run is active, because
  ``_invalidate_retrieval_caches`` fires at every mid-run checkpoint;
* any load failure deletes the sidecar and rebuilds — deleting one by hand is
  always safe.

**What deliberately stayed in ``rag/vault.py``** and is reached through ``self``:
``_bm25_sidecar_dir`` / ``_wikilink_sidecar_path`` (they read the module global
``OBSIDIAN_INDEX_DIR``, which ~48 tests patch as ``rag.vault.OBSIDIAN_INDEX_DIR``
— a global read moved into this module would silently ignore those patches), and
``_read_index_meta``. The thesaurus glue stays there too, per the plan.
"""
import json
import logging
import os
import re
import shutil
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import unquote

from core.constants import VAULT_MD_EXTS
from core.utils import nfc
from rag.storage_io import _write_json_atomic
from rag.docstore_access import node_count
from core.perf_metrics import measured, measured_lock, span

logger = logging.getLogger(__name__)


# --- Sidecar file names -----------------------------------------------------

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


# --- Shared vault link syntax -----------------------------------------------
# These are consumed both by the wikilink graph below AND by the attachment
# resolver still in rag/vault.py (which imports them back from here via the
# re-export). They live here because the graph build cannot import rag.vault
# without a cycle; a later step that extracts the attachment resolver may want
# to move them again into a shared leaf.

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

    Built per observed mutation generation from the ``.md``
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


class RetrievalCacheMixin:
    """The BM25 + wikilink retrieval-cache half of :class:`ObsidianVaultManager`.

    Mixed into the manager, never instantiated on its own. Every attribute it
    touches (``_index``, the two build locks, ``_index_mutation_lock``,
    ``_status_lock`` / ``_index_state``) is owned by the manager's ``__init__``,
    and the two sidecar-path helpers plus ``_read_index_meta`` are resolved
    through ``self`` back into ``rag/vault.py``.
    """

    # Host-owned fields: declare the mixin seam without allocating another store.
    _index: Any
    _index_mutation_lock: threading.Lock
    _bm25_build_lock: threading.Lock
    _wikilink_build_lock: threading.Lock
    _bm25_retriever: Any
    _wikilink_index: Optional[_WikilinkGraph]

    def _replace_index(self, idx):
        """Publish under the caller's existing rw-write lock.

        A scalar epoch also detects identity reuse after cleanup, without
        retaining a reference to a retired index/docstore and its full nodes.
        """
        self._index_generation = getattr(self, "_index_generation", 0) + 1
        self._index = idx

    @contextmanager
    def _mutate_index(self):
        """Existing mutation lock, with an epoch even on partial failures.

        No build lock is acquired here. Checkpoints do not mutate the node
        collection and keep their existing lock/invalidation discipline.
        """
        with measured_lock(self._index_mutation_lock, "index.mutation"):
            try:
                yield
            finally:
                self._docstore_generation = getattr(self, "_docstore_generation", 0) + 1

    def _cache_fingerprint(self, idx=None):
        # Index and docstore replacement are visible even at constant count.
        idx = self._index if idx is None else idx
        if idx is None or getattr(idx, "docstore", None) is None:
            return None
        return (id(idx), id(idx.docstore),
                getattr(self, "_docstore_generation", 0),
                getattr(self, "_index_generation", 0), node_count(idx.docstore))

    def _invalidate_retrieval_caches(self, *, persistent: bool = True) -> None:
        """Drop the cached BM25 retriever and wikilink graph (in memory AND
        their on-disk sidecars) so the next chat rebuilds against the
        freshly-persisted docstore.

        ``persistent=False`` clears process memory only when switching vaults;
        existing sidecars remain available when that vault is reopened.

        Called after each successful ``idx.storage_context.persist(...)`` —
        both the mid-run checkpoint and the final persist — so concurrent
        chats observe the new chunks once indexing publishes them.  The
        reranker is unaffected: it depends on the model name only, not on
        index contents.
        """
        with measured_lock(self._bm25_build_lock, "cache.bm25.lock"):
            self._bm25_retriever = None
            self._bm25_cached_doc_count = -1
            if persistent:
                shutil.rmtree(self._bm25_sidecar_dir(), ignore_errors=True)
        with measured_lock(self._wikilink_build_lock, "cache.wikilink.lock"):
            self._wikilink_index = None
            self._wikilink_cached_doc_count = -1
            if persistent:
                try:
                    os.remove(self._wikilink_sidecar_path())
                except OSError:
                    pass

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
        # DURABILITY (2026-09-02, performance plan Phase 5): persist to a
        # SIBLING directory, then swap.
        # Before: the existing sidecar was rmtree'd and only then rebuilt in
        #   place, so from the first line onwards there was a window — the
        #   whole persist of a large index — in which a crash, or simply a
        #   concurrent cold launch, found NO sidecar and paid the full
        #   re-tokenise the sidecar exists to avoid.
        # Change: build in `<sidecar>.new`, then replace the live directory.
        #   The old one is only removed once the new one is complete.
        # Safe because: the meta file is still written LAST inside the new
        #   directory, so the self-invalidation rule above is untouched; a
        #   failed build removes only the staging directory and leaves the
        #   previous sidecar standing, which is strictly better than the old
        #   behaviour (which left none). The swap is a rename within the same
        #   parent, so a reader sees the old or the new tree, never a mixture.
        # Pinned by: test_vault_regressions.py::TestBm25SidecarSwap.
        staging = sidecar + ".new"
        try:
            shutil.rmtree(staging, ignore_errors=True)
            os.makedirs(staging, exist_ok=True)
            retriever.persist(staging)
            _write_json_atomic(
                os.path.join(staging, _BM25_SIDECAR_META_FILENAME),
                {
                    "doc_count": doc_count,
                    "indexed_at": index_meta.get("indexed_at"),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            shutil.rmtree(sidecar, ignore_errors=True)
            os.replace(staging, sidecar)
            logger.info("BM25 retriever persisted to sidecar (%d nodes).", doc_count)
        except Exception as exc:
            # Best-effort: a failed persist only costs the next launch a
            # rebuild. Remove the STAGING tree; the previous sidecar (if any)
            # is left in place — a stale sidecar self-invalidates on load,
            # and having one beats having none.
            logger.warning("Could not persist BM25 sidecar: %s", exc)
            shutil.rmtree(staging, ignore_errors=True)

    @measured("cache.bm25")
    def _get_bm25_retriever(self, top_k: int,
                            stage_cb: Optional[Callable[[str], None]] = None) -> Optional[Any]:
        """Return a cached BM25Retriever sized to *top_k*, or None if BM25
        is unavailable.

        Caches by index/docstore identity, mutation/publication generations
        and serialized node count. Same-count replacements invalidate the
        in-memory fingerprint too; publication explicitly invalidates the
        persisted sidecar through ``_invalidate_retrieval_caches``.

        Process-cold misses consult the on-disk sidecar first
        (``_load_bm25_sidecar``): a previous build persisted there is
        mmap-loaded in seconds with its corpus served lazily from JSONL,
        instead of re-tokenising the docstore and duplicating all vault
        text in RAM.  A successful in-process build is persisted back to
        the sidecar (``_persist_bm25_sidecar``) unless an indexing run is
        active.

        Counts use the localized serialized-store accessor; only rebuilds
        materialize nodes. Those snapshots are taken
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
        with measured_lock(self._index_mutation_lock, "index.cache"):
            fingerprint = self._cache_fingerprint()
        if fingerprint is None or not fingerprint[-1]:
            return None
        current_count = fingerprint[-1]
        cached = self._bm25_retriever
        if (cached is not None and self._bm25_cached_doc_count == current_count
                and getattr(self, "_bm25_fingerprint", fingerprint) == fingerprint):
            # Retune race fix (improvement plan 2026-07-04, item 2.7): this
            # fast path used to write ``cached.similarity_top_k = top_k`` here
            # — with NO lock — while a concurrent chat could be mid-retrieval
            # on the SAME shared object under ``_index_mutation_lock`` (a deck
            # run's vault_search k=12 retuned a concurrent user chat k=8
            # mid-flight). Tuning now happens in exactly ONE place: the
            # engine's ``_build_retrieval_pipeline`` now shallow-copies the
            # cached helper and tunes only its request-local facade (AP-09). This fetch is read-only on purpose — do
            # not "restore" the convenience retune. Invariant (pinned by
            # ``TestSharedRetunerRace``): fetching a cached BM25/reranker
            # never mutates its tuning fields.
            return cached

        with measured_lock(self._bm25_build_lock, "cache.bm25.lock"):
            with measured_lock(self._index_mutation_lock, "index.cache"):
                fingerprint = self._cache_fingerprint()
            if fingerprint is None or not fingerprint[-1]:
                return None
            current_count = fingerprint[-1]
            # Double-check under the build lock so two concurrent chats do
            # not both rebuild against the same docstore size.
            if (
                self._bm25_retriever is not None
                and self._bm25_cached_doc_count == current_count
                and getattr(self, "_bm25_fingerprint", fingerprint) == fingerprint
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
            loaded = (self._load_bm25_sidecar(current_count, top_k)
                      if fingerprint[2] == 0
                      and getattr(self, "_bm25_fingerprint", fingerprint)[:2] == fingerprint[:2]
                      and getattr(self, "_bm25_fingerprint", fingerprint)[3] == fingerprint[3]
                      else None)
            if loaded is not None:
                self._bm25_retriever = loaded
                self._bm25_cached_doc_count = current_count
                self._bm25_fingerprint = fingerprint
                return loaded
            # Snapshot the docstore under _index_mutation_lock so an
            # in-flight indexer insert/delete cannot mutate the dict while
            # we iterate.  Record the size from the same locked window so
            # the cache fingerprint reflects the exact snapshot we built
            # BM25 from — otherwise a concurrent insert between
            # ``list(docs.values())`` and ``len(docs)`` would let us cache
            # an N-node retriever under an N+1 fingerprint, missing a
            # subsequent re-check.
            with measured_lock(self._index_mutation_lock, "index.cache"):
                idx = self._index
                if idx is None:
                    return None
                fingerprint = self._cache_fingerprint(idx)
                if fingerprint is None:
                    return None
                with span("docstore.snapshot"):
                    nodes = list(idx.docstore.docs.values())
                snapshot_count = len(nodes)
            if not nodes:
                return None
            # Build BM25 outside the mutation lock — tokenisation + index
            # build is the expensive step and the indexer must remain
            # free to insert while it runs.
            # Heartbeat (2026-07-22 audit P4b): from_defaults is ONE blocking
            # library call that can run minutes on a large docstore, and when
            # the caller is a chat worker the SSE consumer's stall guard is
            # fed by frames — a silent build tripped it despite healthy
            # progress. A daemon ticker emits a stage line every 30 s while
            # the build runs; stage_cb=None (prewarm, background rebuild)
            # keeps the build silent as before.
            hb_stop = threading.Event()
            if stage_cb is not None:
                def _hb():
                    waited = 0
                    while not hb_stop.wait(30):
                        waited += 30
                        try:
                            stage_cb(
                                f"Still building the lexical index "
                                f"({len(nodes)} chunks, {waited}s)…")
                        except Exception:  # noqa: BLE001 — heartbeat must never kill the build
                            return
                threading.Thread(target=_hb, name="bm25-build-heartbeat",
                                 daemon=True).start()
            try:
                with span("cache.bm25.build"):
                    retriever = BM25Retriever.from_defaults(
                        nodes=nodes,
                        similarity_top_k=top_k,
                    )
            except Exception as exc:
                logger.warning("BM25 retriever build failed: %s", exc)
                return None
            finally:
                hb_stop.set()
            self._bm25_retriever = retriever
            self._bm25_cached_doc_count = snapshot_count
            self._bm25_fingerprint = fingerprint
            # Persist for the next launch (skipped while indexing is
            # active — see the helper).  Adds a one-off full-corpus write
            # after a fresh build; prewarm normally absorbs this off the
            # chat path.
            with measured_lock(self._index_mutation_lock, "index.cache"):
                still_current = self._cache_fingerprint() == fingerprint
            if still_current:
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
                nfc(_with_md(os.path.basename(target))).lower()
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

    @measured("cache.wikilink.build")
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
            # B2 (2026-07-13): NFC-normalize the basename key (parity with the
            # image name_index) so an NFD source name matches an NFC link target.
            note_name_index.setdefault(
                nfc(os.path.basename(source)).lower(), []
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

    @measured("cache.wikilink")
    def _get_wikilink_index(self) -> Optional[_WikilinkGraph]:
        """Return the cached note→note wikilink graph, building it on demand.

        Mirrors ``_get_bm25_retriever``'s discipline: cached by index identity,
        mutation epoch and docstore count,
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
        with measured_lock(self._index_mutation_lock, "index.cache"):
            fingerprint = self._cache_fingerprint()
        if fingerprint is None or not fingerprint[-1]:
            return None
        current_count = fingerprint[-1]
        cached = self._wikilink_index
        if (cached is not None and self._wikilink_cached_doc_count == current_count
                and getattr(self, "_wikilink_fingerprint", fingerprint) == fingerprint):
            return cached
        with measured_lock(self._wikilink_build_lock, "cache.wikilink.lock"):
            with measured_lock(self._index_mutation_lock, "index.cache"):
                fingerprint = self._cache_fingerprint()
            if fingerprint is None or not fingerprint[-1]:
                return None
            current_count = fingerprint[-1]
            # Double-check under the build lock so two concurrent chats do not
            # both rebuild against the same docstore size.
            if (
                self._wikilink_index is not None
                and self._wikilink_cached_doc_count == current_count
                and getattr(self, "_wikilink_fingerprint", fingerprint) == fingerprint
            ):
                return self._wikilink_index
            # Sidecar first: a hit avoids the O(N) node-list copy and the
            # regex sweep entirely. Mutated in-process generations cannot
            # reuse disk sidecars; count + indexed_at still validate legacy
            # sidecars on a cold load. A racing mutation makes this fingerprint
            # stale, forcing the next lookup to rebuild (eventual consistency).
            loaded = (self._load_wikilink_sidecar(current_count)
                      if fingerprint[2] == 0
                      and getattr(self, "_wikilink_fingerprint", fingerprint)[:2] == fingerprint[:2]
                      and getattr(self, "_wikilink_fingerprint", fingerprint)[3] == fingerprint[3]
                      else None)
            if loaded is not None:
                self._wikilink_index = loaded
                self._wikilink_cached_doc_count = current_count
                self._wikilink_fingerprint = fingerprint
                return loaded
            # Snapshot the docstore under _index_mutation_lock so an in-flight
            # indexer insert/delete cannot mutate the dict while we iterate;
            # record the size from the same locked window so the fingerprint
            # matches the exact snapshot we built from.
            with measured_lock(self._index_mutation_lock, "index.cache"):
                idx = self._index
                if idx is None:
                    return None
                fingerprint = self._cache_fingerprint(idx)
                if fingerprint is None:
                    return None
                with span("docstore.snapshot"):
                    nodes = list(idx.docstore.docs.values())
                snapshot_count = len(nodes)
            if not nodes:
                return None
            graph = self._build_wikilink_graph(nodes)
            self._wikilink_index = graph
            self._wikilink_cached_doc_count = snapshot_count
            self._wikilink_fingerprint = fingerprint
            with measured_lock(self._index_mutation_lock, "index.cache"):
                still_current = self._cache_fingerprint() == fingerprint
            if still_current:
                self._persist_wikilink_sidecar(graph, snapshot_count)
            return graph
