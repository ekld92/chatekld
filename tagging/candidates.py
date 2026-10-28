"""Label-space reduction + few-shot exemplars for the tag proposal.

Two prompt-side upgrades to ``propose.py``, both embedding-driven and both
strictly optional (their config knobs at 0 reproduce the historical prompt
byte-for-byte, and ANY failure — provider down, model missing, cache
unreadable — degrades to the full vocabulary with no exemplars rather than
blocking a proposal):

* **Candidate tags** (``tagging_candidate_tags``): embed each ``_tags.md``
  entry (``"tag — gloss"``) and the paper's opening text, keep only the top-N
  most similar tags in the prompt legend. Shorter prompt (precious on small
  local models) and measurably better accuracy than showing the whole label
  space (label-space-reduction literature). Gloss embeddings are cached on
  disk keyed by ``(embed model, _tags.md size+mtime)`` — the thesaurus-cache
  idiom — so repeat proposals cost one query embed.
* **Exemplars** (``tagging_fewshot_k``): the tagging journal is a growing
  labelled dataset (every Accept records ``citation_key`` + ``added``). Mine
  the non-reverted ops, rank the accepted papers by title-embedding
  similarity to the current paper (recency fallback), and inject the top-k
  as ``title → accepted tags`` few-shot lines.

This is the app's first embedding caller outside ``rag/``; it follows the
query-path template (``rag/engine.py``): construction-time
``request_timeout_s`` bound, provider resolved via ``resolve_embed_provider``.
The vector space here (glosses/titles/paper text) is self-contained — never
mixed with the index's vectors — so the *configured* ``embed`` model is used,
not the index's recorded one.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
from pathlib import Path

from tagging.journal import _vault_key

logger = logging.getLogger(__name__)

# Mirrors rag/engine.py::QUERY_EMBED_TIMEOUT_S — an interactive-path embed
# call is always construction-bounded.
_EMBED_TIMEOUT_S = 30.0
# The paper's opening text is enough to place it in tag space; embedding the
# propose prompt's full 6k chars would only slow the call down.
_PAPER_EMBED_MAX_CHARS = 2000

#: Exemplar pool bound: only the newest this many journal-tagged keys are
#: ranked. The prompt shows tagging_fewshot_k of them (default 3), so a deeper
#: pool buys a marginally better ranking for a linearly growing embed cost.
_EXEMPLAR_MAX_CANDIDATES = 200
#: Entries retained in the on-disk title-vector cache.
_TITLE_CACHE_MAX = 2000

_MEMO_LOCK = threading.Lock()
_GLOSS_MEMO: dict = {"key": None, "vectors": None}
# Serialises the whole miss path (disk read → batch embed → disk write → memo
# update). _MEMO_LOCK alone only guarded the dict assignment, so two
# concurrent callers would each run the expensive embed batch (masked today by
# the propose single-flight gate — this is the real double-checked-locking
# pattern so a future concurrent caller can't regress it). A separate lock so
# memo READS never queue behind a slow build.
_BUILD_LOCK = threading.Lock()


def _embed_model_name(cfg: dict) -> str:
    from core.constants import DEFAULT_EMBED
    return str(cfg.get("embed") or DEFAULT_EMBED)


def _embedder(cfg: dict):
    """A LlamaIndex embedding object from the configured LOCAL embed provider."""
    from core.config import resolve_embed_provider
    from core.providers import get_provider
    provider_name = str(cfg.get("provider") or "ollama")
    provider = get_provider(resolve_embed_provider(cfg, provider_name))
    return provider.get_embedding(_embed_model_name(cfg),
                                  request_timeout_s=_EMBED_TIMEOUT_S)


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _gloss_cache_file(vault_root: Path) -> Path:
    from core.constants import OBSIDIAN_CACHE_DIR
    return (Path(OBSIDIAN_CACHE_DIR) / "tagging" / _vault_key(Path(vault_root))
            / "gloss_embeds.json")


def _gloss_cache_key(cfg: dict, tags_path: Path) -> list:
    try:
        st = tags_path.stat()
        sig = [st.st_size, st.st_mtime_ns]
    except OSError:
        sig = [None, None]
    return [_embed_model_name(cfg), str(tags_path), *sig]


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Temp-sibling + os.replace, deliberately no fsync (regenerable cache)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, str(path))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def gloss_embeddings(cfg: dict, vocab: dict[str, str], tags_path: Path,
                     vault_root: Path, embedder=None) -> dict[str, list[float]]:
    """``{tag: vector}`` for the whole vocabulary, memo- and disk-cached.

    Cache key = (embed model, _tags.md path/size/mtime_ns) — the thesaurus
    idiom, self-invalidating on a vocabulary edit or an embed-model switch.
    The tag-id set is re-checked against *vocab* as defence in depth. Raises
    on embed failure (the caller owns degradation).
    """
    key = _gloss_cache_key(cfg, Path(tags_path))
    with _MEMO_LOCK:
        if _GLOSS_MEMO["key"] == key and set(_GLOSS_MEMO["vectors"] or {}) == set(vocab):
            return _GLOSS_MEMO["vectors"]

    # Miss path under _BUILD_LOCK, with the memo RE-checked after acquiring:
    # the builder we queued behind may have produced exactly these vectors.
    with _BUILD_LOCK:
        with _MEMO_LOCK:
            if _GLOSS_MEMO["key"] == key and set(_GLOSS_MEMO["vectors"] or {}) == set(vocab):
                return _GLOSS_MEMO["vectors"]
        cache_file = _gloss_cache_file(Path(vault_root))
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            vectors = data.get("vectors")
            if (data.get("key") == key and isinstance(vectors, dict)
                    and set(vectors) == set(vocab)):
                with _MEMO_LOCK:
                    _GLOSS_MEMO.update(key=key, vectors=vectors)
                return vectors
        except (OSError, ValueError, TypeError):
            # Narrowed from the old blanket `except Exception` (audit §3): a
            # stale/corrupt/missing cache file rebuilds; a programming error
            # in the block above should surface, not silently re-embed.
            pass

        if embedder is None:
            embedder = _embedder(cfg)
        texts = [f"{tag} — {gloss}" for tag, gloss in vocab.items()]
        embs = embedder.get_text_embedding_batch(texts)
        vectors = {tag: [float(x) for x in vec] for tag, vec in zip(vocab, embs)}
        try:
            _atomic_write_json(cache_file, {"key": key, "vectors": vectors})
        except OSError:
            logger.debug("gloss-embedding cache write failed", exc_info=True)
        with _MEMO_LOCK:
            _GLOSS_MEMO.update(key=key, vectors=vectors)
        return vectors


def _select_candidates(cfg, vault_root, tags_path, paper_text, vocab, n,
                       embedder) -> dict[str, str]:
    """Top-*n* vocabulary subset by gloss↔paper cosine (order-preserving)."""
    if n <= 0 or n >= len(vocab):
        return vocab
    vectors = gloss_embeddings(cfg, vocab, tags_path, vault_root, embedder=embedder)
    q = embedder.get_query_embedding(paper_text[:_PAPER_EMBED_MAX_CHARS])
    ranked = sorted(vocab, key=lambda t: _cosine(q, vectors[t]), reverse=True)
    keep = set(ranked[:n])
    return {t: g for t, g in vocab.items() if t in keep}


def mine_journal_exemplars(vault_root: Path, cfg: dict) -> list[dict]:
    """Aggregate the journal's accepted tags per citation key (pure read).

    All three op kinds count (an Accept with the note target off produces only
    ``finder_tag``/``zotero_tag`` ops); ``reverted`` ops are excluded. Returns
    ``[{key, tags, ts}]``, ts = the newest contributing op.
    """
    from tagging import journal
    manifest = journal.load(Path(vault_root), cfg)
    by_key: dict[str, dict] = {}
    for op in manifest.get("ops", []):
        if op.get("kind") not in ("note_tag", "finder_tag", "zotero_tag"):
            continue
        if op.get("state") == "reverted":
            continue
        ck = op.get("citation_key")
        added = op.get("added") or []
        if not ck or not added:
            continue
        entry = by_key.setdefault(ck, {"key": ck, "tags": [], "ts": ""})
        for t in added:
            if t not in entry["tags"]:
                entry["tags"].append(t)
        entry["ts"] = max(entry["ts"], str(op.get("ts") or ""))
    return list(by_key.values())


def _title_cache_file(vault_root: Path) -> Path:
    from core.constants import OBSIDIAN_CACHE_DIR
    return (Path(OBSIDIAN_CACHE_DIR) / "tagging" / _vault_key(Path(vault_root))
            / "title_embeds.json")


def title_embeddings(cfg: dict, vault_root: Path, cands: list[dict],
                     embedder) -> dict[str, list[float]]:
    """``{citation_key: vector}`` for each candidate's title, disk-cached.

    Keyed by the embed model and, per entry, by the TITLE the vector was
    built from — so a bib title edit or an embed-model switch re-embeds that
    key and nothing else. Only misses reach the provider. Raises on embed
    failure (the caller owns degradation, as for glosses).
    """
    model = _embed_model_name(cfg)
    cache_file = _title_cache_file(Path(vault_root))
    cached: dict = {}
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("model") == model:
            entries = data.get("entries")
            if isinstance(entries, dict):
                cached = entries
    except (OSError, ValueError, TypeError):
        cached = {}

    out: dict[str, list[float]] = {}
    misses: list[dict] = []
    for c in cands:
        hit = cached.get(c["key"])
        if (isinstance(hit, dict) and hit.get("title") == c["title"]
                and isinstance(hit.get("vec"), list)):
            out[c["key"]] = hit["vec"]
        else:
            misses.append(c)
    if misses:
        embs = embedder.get_text_embedding_batch([c["title"] for c in misses])
        for c, vec in zip(misses, embs):
            out[c["key"]] = [float(x) for x in vec]
        entries = dict(cached)
        for c in misses:
            entries[c["key"]] = {"title": c["title"], "vec": out[c["key"]]}
        if len(entries) > _TITLE_CACHE_MAX:
            # Bounded file: the keys this call needs are kept first, the rest
            # fill the remainder. No LRU metadata is stored — the cache is
            # regenerable and one re-embed is the whole cost of an eviction.
            keep = {c["key"]: entries[c["key"]] for c in cands if c["key"] in entries}
            for k2, v2 in entries.items():
                if len(keep) >= _TITLE_CACHE_MAX:
                    break
                keep.setdefault(k2, v2)
            entries = keep
        try:
            _atomic_write_json(cache_file, {"model": model, "entries": entries})
        except OSError:
            logger.debug("title-embedding cache write failed", exc_info=True)
    return out


def _select_exemplars(cfg, vault_root, inv, paper_text, k,
                      embedder) -> list[tuple[str, list[str]]]:
    """Top-*k* ``(title, accepted_tags)`` pairs, similarity-ranked.

    Titles come from the audit inventory (no PDF re-reads); similarity is
    title-embedding ↔ paper-text cosine, degrading to recency when the embed
    call fails mid-way.

    # PERF (2026-09-02): the exemplar pool is capped and its title vectors
    #   are disk-cached.
    # Before: every citation key the journal ever touched was embedded on
    #   EVERY propose. Measured with a counting fake embedder over a journal
    #   holding 3 000 tagged keys: 3 000 titles embedded per proposal, all of
    #   them inside the 30 s construction bound the propose gate is held
    #   across — which is why the field behaviour was a timeout followed by
    #   the recency fallback, i.e. the whole budget spent to reach the answer
    #   the fallback gives for free.
    # Change: the pool is truncated to the newest _EXEMPLAR_MAX_CANDIDATES
    #   (200) by op timestamp BEFORE anything is embedded, and each title's
    #   vector is cached on disk by (embed model, title) the way glosses are.
    #   Same journal: 200 embedded on the first proposal, 0 on the next
    #   (warm cache), against 3 000 every time.
    # Safe because:
    #   The cap is applied to the RANKING POOL, never to the k exemplars the
    #   prompt receives; k stays whatever tagging_fewshot_k says. Both the
    #   similarity sort and the recency fallback run over the same truncated
    #   pool, so the two paths still agree on which papers were considered.
    #   A title-vector cache miss is indistinguishable from today's behaviour
    #   for that key.
    # Preserves: "Degradation stays total. Nothing here may block a
    #   proposal." — every new failure mode (unreadable cache, embed error)
    #   still raises into propose_context's single try/except, which returns
    #   (vocab, []). k <= 0 still returns [] before any work.
    # Lock/thread contract: no lock taken or released; runs on the propose
    #   worker thread as before.
    # Pinned by: test_tagging.py::TestExemplarPoolBound (red before the
    #   change: "3000 != 200").
    """
    if k <= 0:
        return []
    cands = mine_journal_exemplars(vault_root, cfg)
    records = getattr(inv, "records", None) or {}
    for c in cands:
        rec = records.get(c["key"])
        bib = getattr(rec, "bib_entry", None) if rec is not None else None
        title = getattr(bib, "title", None) if bib is not None else None
        c["title"] = title or c["key"]
    if not cands:
        return []
    # Newest-first, then truncate: the pool an unbounded journal offers is
    # dominated by keys tagged months ago, and embedding them costs the whole
    # budget for a ranking the recency fallback approximates anyway.
    cands.sort(key=lambda c: c["ts"], reverse=True)
    del cands[_EXEMPLAR_MAX_CANDIDATES:]
    try:
        q = embedder.get_query_embedding(paper_text[:_PAPER_EMBED_MAX_CHARS])
        vectors = title_embeddings(cfg, Path(vault_root), cands, embedder)
        for c in cands:
            c["score"] = _cosine(q, vectors[c["key"]])
        cands.sort(key=lambda c: c["score"], reverse=True)
    except Exception:  # noqa: BLE001 — similarity is best-effort
        logger.debug("exemplar similarity ranking degraded to recency", exc_info=True)
        cands.sort(key=lambda c: c["ts"], reverse=True)
    return [(c["title"], list(c["tags"])) for c in cands[:k]]


def propose_context(*, cfg: dict, vault_root: Path, tags_path: Path, inv,
                    paper_text: str, vocab: dict[str, str],
                    candidate_n: int, fewshot_k: int, should_cancel=None):
    """Return ``(effective_vocab, exemplars)`` for one proposal.

    ONE try/except wraps the whole block, provider construction included: any
    failure degrades to ``(vocab, [])`` — the exact pre-feature behaviour —
    because a broken embed backend must never block a tag proposal. It logs at
    WARNING (not info): the catch-all also swallows programming errors, and
    those must at least be visible in the field log.

    *should_cancel* is polled between the embed stages (each stage is one
    provider round-trip bounded at ``_EMBED_TIMEOUT_S``): a proposal the route
    already timed out (504) stops after the in-flight stage instead of running
    the whole embed pipeline for a reply nobody will read — pre-fix, the cancel
    token only took effect once the LLM stream started, so this phase pinned
    the single-flight gate for up to ~90 s of dead work.
    """
    if candidate_n <= 0 and fewshot_k <= 0:
        return vocab, []
    cancelled = should_cancel if callable(should_cancel) else (lambda: False)
    try:
        if cancelled():
            return vocab, []
        embedder = _embedder(cfg)
        effective = _select_candidates(
            cfg, vault_root, tags_path, paper_text, vocab, candidate_n, embedder)
        if cancelled():
            return effective, []
        exemplars = _select_exemplars(
            cfg, vault_root, inv, paper_text, fewshot_k, embedder)
        return effective, exemplars
    except Exception:  # noqa: BLE001 — full degradation, never block propose
        logger.warning("tagging candidate/exemplar selection degraded to full vocab",
                       exc_info=True)
        return vocab, []
