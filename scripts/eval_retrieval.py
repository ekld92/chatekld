#!/usr/bin/env python3
"""Score the retrieval golden set against the real vault index, and sweep knobs.

This is the *runner* half of the retrieval eval harness (the generator is
``scripts/gen_golden_retrieval.py``, the scoring is
``tests/eval/retrieval_metrics.py``). It answers one question the app itself
cannot: **does changing a retrieval knob actually retrieve better documents?**

For each "cell" (one combination of knobs) it replays every golden query
through ``ObsidianVaultManager.retrieve()`` — the LLM-free retrieval entry
point — collects the ranked ``RetrievedChunk.source`` list, scores it against
the golden qrels (recall@k / MRR / nDCG@k, document-level), and prints a
Δ-vs-baseline table. No text is generated and **no vault file is touched**.

What this run DOES write (honest disclosure)
--------------------------------------------
The earlier version of this docstring claimed "nothing is written". That was
wrong in two places, both inside the silent per-cell warm-up:

* ``<index>/bm25_index/`` — a cold or stale BM25 sidecar makes the first
  hybrid retrieval **re-tokenise the whole docstore** and persist a fresh
  sidecar (hundreds of MB up to ~2 GB on a large vault, minutes of CPU). The
  pre-flight below detects a likely-stale sidecar and warns *before* the run.
* ``<index>/wikilink_sidecar.json`` — built and persisted the first time a cell
  runs with ``wikilink_expansion`` on.
* ``config.json`` — rewritten for the duration of any cell carrying ``config``
  overrides (see *Config safety* below).

Everything else is read-only: the vector store, the docstore and the golden set
are only ever read.

Why the app MUST be closed
--------------------------
The manager's locks are **in-process only**. A live app holds the same index,
rewrites the BM25 sidecar, and checkpoints the store — two processes would race
on both. This is exactly the rationale ``papermind server`` uses to refuse to
start beside a live server, so the check here is the same one: the runner
**detects** a live server (``core/server_info.py`` record → pid → ``/api/health``
probe, the ``core/driver.py::resolve_base_url`` ladder) and refuses with exit 2.
``--yes`` skips the interactive prompt but can NOT bypass that refusal — an
advisory y/N question is not a correctness mechanism.

Cells
-----
A cell is ``{"name": str, "retrieve": {kwarg: value}, "config": {key: value}}``:

* ``retrieve`` — per-call overrides passed straight into ``retrieve()``
  (validated against :data:`RETRIEVE_KEYS`, which mirrors its signature).
* ``config``  — knobs the engine reads from ``config.json`` instead of from a
  kwarg (RRF leg weights, rerank pool sizing, reranker device, wikilink caps,
  …; :data:`CONFIG_KEYS`).

**Both the key NAME and the VALUE are validated** (:data:`_RETRIEVE_RULES` /
:data:`_CONFIG_RULES`) against the same ranges ``api/routes/config.py`` and
``api/routes/vault.py`` enforce — an out-of-range value aborts naming the key
and its accepted range. It is deliberately *rejected*, never clamped: a cell
that silently ran at a clamped value would report a Δ attributed to a knob
setting that never existed.

Config safety (crash-proof)
---------------------------
Sweeping a ``config`` knob means rewriting the user's real ``config.json``, so
before the first override write the original bytes are persisted to a sidecar
``config.json.evalbak`` (atomic + fsync'd). Restore runs from four independent
places: the per-cell ``finally``, the outer ``finally``, ``atexit``, and a
``SIGTERM`` handler. ``SIGKILL``/OOM cannot be caught — that is precisely what
the on-disk sidecar is for: a later run finds it, **refuses to start**, and
prints both paths so the user can restore by hand (never silently, since they
may have edited the config in between).

An implicit ``baseline`` cell (pure config resolution, no overrides) is always
run first and is the Δ reference. ``--preset`` selects a canned cell list;
``--cells FILE`` reads ``{"cells": [...]}`` from JSON. An unknown key in either
map aborts with **exit 2** naming it, rather than silently ignoring a typo'd
knob and reporting a meaningless "no difference".

Caveats the runner prints for you
---------------------------------
* **Inert overrides.** A knob that provably cannot reach an active code path
  under this cell's other settings is flagged LOUDLY next to its row (see
  :func:`detect_inert_overrides`). A Δ=0.000 from an override the engine never
  read is a *false* "this knob doesn't matter" conclusion.
* RRF leg weights ≠ 1.0/1.0 flip the fusion mode (``reciprocal_rerank`` →
  ``relative_score``); the two score on different scales, so compare such cells
  on **rank** metrics only.
* The reranker state is reported as an explicit tri-state
  (``none`` / ``loaded`` / ``failed`` / ``unknown``) determined **positively**
  from the manager's cache key, never inferred from a sticky flag a cell may
  not even have touched.
* Golden queries whose relevant documents have all left the index are
  **excluded and counted**, never scored as misses. A query whose retrieval
  *raised* is likewise excluded (a failure is the absence of a retrieval
  outcome, not a miss).

Usage:
    python scripts/eval_retrieval.py [--golden FILE] [--preset NAME | --cells FILE]
                                     [--k 1,5,10] [--limit N] [--out FILE]
                                     [--no-warmup] [--yes]
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import math
import os
import random
import re
import signal
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# Script lives in scripts/; put the project root on sys.path so core/, rag/ and
# tests.eval.retrieval_metrics import (migrate_vector_store.py pattern).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.validators import (  # noqa: E402
    coerce_bool,
    coerce_enum,
    coerce_float_in_range,
    coerce_int_in_range,
)
from core.constants import BASE_DIR, CONFIG_FILE, OBSIDIAN_INDEX_DIR  # noqa: E402
from core.utils import write_bytes_atomic, write_text_atomic  # noqa: E402
from tests.eval.retrieval_metrics import (  # noqa: E402
    DEFAULT_KS,
    RetrievalQuery,
    aggregate,
    dedupe_ranked_sources,
    format_cells_table,
    load_golden,
    norm_source,
    score_query,
)

#: Results-file schema. Bumped only on a breaking shape change; readers should
#: refuse an unknown version rather than mis-read it (same contract as the
#: golden set's ``schema_version``).
#:
#: **v1 → v2**: the per-cell boolean ``reranker_failed`` was replaced by the
#: tri-state ``reranker`` record (see :func:`_reranker_state`). The boolean was
#: not merely renamed — it was *unsound* (it fired on cells that never asked for
#: a reranker, and stayed False when no reranker ever loaded), so keeping it as
#: an alias would have preserved the wrong answer under a new name. v2 also adds
#: ``failed_queries`` / ``aborted`` (item 7) and ``partial`` (checkpointing).
RESULTS_SCHEMA_VERSION = 2

META_JSON = "obsidian_meta.json"
#: The indexer's per-source inventory, written by ``rag/vault.py``'s
#: ``_write_index_manifest`` beside the docstore — see :func:`docstore_sources`.
MATERIALS_JSON = "indexed_materials.json"

#: The implicit first cell — pure config resolution, the Δ reference.
BASELINE_CELL_NAME = "baseline"

#: Sidecar holding the pre-sweep ``config.json`` bytes. Lives beside the config
#: (same directory ⇒ same filesystem, so the atomic rename in
#: ``write_bytes_atomic`` is a true rename, never a cross-device copy).
#: ``.gitignore`` already covers it.
CONFIG_SIDECAR_SUFFIX = ".evalbak"

#: Consecutive per-query retrieval failures that abort the CELL (not the run).
#: Mirrors the indexer's consecutive-failure breaker: one flaky query is noise,
#: five in a row means the embed provider is down and every later query would
#: fail identically — there is nothing left to measure in this cell.
MAX_CONSECUTIVE_QUERY_FAILURES = 5

#: ``retrieve()`` keyword arguments a cell may override. Mirrors the signature
#: of ``rag/vault.py::ObsidianVaultManager.retrieve`` **minus** the five
#: parameters that are not knobs (:data:`RETRIEVE_NON_KNOBS`, which also lists
#: ``self`` so the ratchet can compare raw signature sets): ``message`` (the
#: query itself), ``stage_cb`` (a progress callback the runner owns — see
#: :func:`_run_one_cell`), and ``llm_name`` / ``embed_name`` /
#: ``provider_name`` — the identity of the models. The pre-2026-07 version of
#: this comment said "four" and then listed five, which is why the count is now
#: derived from a named tuple instead of retyped in prose.
#: The embed model in particular is pinned by the index (the golden
#: set's qrels only mean something in the vector space the index was built in),
#: so swapping it is a reindex, not a sweep.
#: Pinned against drift by ``test_eval_runner.py::test_retrieve_keys_match_signature``.
RETRIEVE_KEYS: Tuple[str, ...] = (
    "top_k",
    "top_k_explicit",
    "similarity_cutoff",
    "hybrid_enabled",
    "reranker_enabled",
    "reranker_model",
    "mmr_enabled",
    "mmr_lambda",
    "query_expansion",
    "num_queries",
    "rerank_pool_ceiling",
    "wikilink_expansion",
    "thesaurus_expansion",
    "primer_enabled",
)

#: ``retrieve()`` parameters that exist but are deliberately NOT sweepable.
#: Split out as data (rather than an inline literal in the ratchet test) so the
#: exclusion list is reviewable next to the reason above.
RETRIEVE_NON_KNOBS: Tuple[str, ...] = (
    "self", "message", "llm_name", "embed_name", "provider_name", "stage_cb",
)

#: Config-only retrieval knobs (the engine reads these from ``config.json``;
#: there is no ``retrieve()`` kwarg for them). Sweeping one rewrites the config
#: file for the duration of the cell — see the module docstring.
CONFIG_KEYS: Tuple[str, ...] = (
    "vault_rrf_dense_weight",
    "vault_rrf_bm25_weight",
    "vault_rerank_pool_multiplier",
    "vault_rerank_pool_floor",
    "vault_reranker_device",
    "vault_wikilink_neighbor_cap",
    "vault_wikilink_node_cap",
    "vault_wikilink_score_decay",
    "vault_thesaurus_max_variants",
    "context_window",
)

#: Reranker recommended for a bilingual FR/EN vault — the A/B candidate.
BGE_RERANKER = "BAAI/bge-reranker-v2-m3"

#: Engine defaults the inert-override detector needs when a knob is absent from
#: config. Kept here (not read from ``core.config._DEFAULTS``) because the
#: detector must model what ``rag/engine.py`` will do, and the engine reads
#: these through its own defensive ``_wl_int``-style casts with the same
#: literals. A drift here degrades the WARNING only — never a metric.
_POOL_MULTIPLIER_DEFAULT = 4
_POOL_FLOOR_DEFAULT = 20


class CellError(ValueError):
    """A malformed cell definition (unknown key, bad value). Maps to exit 2."""


class ConfigUnsafeError(RuntimeError):
    """The user's ``config.json`` could not be safely written or restored.

    Distinct from :class:`CellError` because the recovery instructions differ:
    a cell error is fixed by editing the cells file, this one by looking at the
    sidecar on disk. Maps to exit 1 (aborted).
    """


# --------------------------------------------------------------------------
# Cell VALUE validation
# --------------------------------------------------------------------------
# ``_validate_cell`` used to check key NAMES only, so a cells file could
# persist ``context_window: -1`` / ``vault_reranker_device: "cuda:0"`` /
# ``vault_rrf_dense_weight: 1e308`` straight into the user's LIVE config —
# values ``api/routes/config.py::_validate_llm_config_keys`` would have dropped.
# The rules below mirror that map (and, for the ``retrieve`` half,
# ``api/routes/vault.py::_resolve_chat_params``) key-for-key and range-for-range.
#
# The coercers are REUSED from ``api/validators.py`` rather than reimplemented,
# but their contract is "clamp into range", which is wrong here: a swept cell
# that silently ran at a clamped value would report a Δ attributed to a setting
# that never existed. So every rule below demands the coerced value be EQUAL to
# the input and raises otherwise — reuse for the parsing/finite-ness logic,
# reject for the range.

def _reject(name: str, key: str, value: object, accepted: str) -> "CellError":
    """Build the one error shape every value rule raises (names key + range)."""
    return CellError(
        f"cell {name!r}: invalid value for {key!r}: {value!r} — accepted: {accepted}"
    )


def _int_rule(lo: int, hi: int) -> Callable:
    """Integer in ``[lo, hi]``, rejected (not clamped) outside it.

    ``bool`` is excluded explicitly: it is an ``int`` subclass, so ``True``
    would satisfy ``coerce_int_in_range(..., 1, 32) == True`` and quietly sweep
    ``top_k=1``. Non-integral floats (``3.5``) are rejected the same way — the
    coercer truncates them, which is exactly the silent reinterpretation this
    validator exists to prevent.
    """
    accepted = f"integer {lo}–{hi}"

    def _rule(name: str, key: str, value: object) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _reject(name, key, value, accepted)
        coerced = coerce_int_in_range(value, lo, hi)
        if coerced is None or coerced != value:
            raise _reject(name, key, value, accepted)

    return _rule


def _float_rule(lo: float, hi: float) -> Callable:
    """Float in ``[lo, hi]``, rejected (not clamped) outside it.

    ``coerce_float_in_range`` already rejects NaN/±Inf (which ``json`` will
    happily parse from a hand-written cells file), so the equality test below
    can never be defeated by a NaN comparing False against everything.
    """
    accepted = f"number {lo}–{hi}"

    def _rule(name: str, key: str, value: object) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _reject(name, key, value, accepted)
        coerced = coerce_float_in_range(value, lo, hi)
        if coerced is None or coerced != float(value):
            raise _reject(name, key, value, accepted)

    return _rule


def _bool_rule() -> Callable:
    """Strict JSON boolean.

    ``coerce_bool`` also accepts ``"true"``/``1``; that leniency is right for an
    HTTP body typed by a browser, wrong for a hand-authored cells file where a
    quoted boolean is a mistake worth surfacing. The coercer is still consulted
    so the two can never disagree about what a real bool is.
    """
    def _rule(name: str, key: str, value: object) -> None:
        if not isinstance(value, bool) or coerce_bool(value) is not value:
            raise _reject(name, key, value, "true or false")

    return _rule


def _enum_rule(allowed: Sequence[str]) -> Callable:
    """One of *allowed* (exact match, same as ``coerce_enum``)."""
    accepted = " | ".join(allowed)

    def _rule(name: str, key: str, value: object) -> None:
        if coerce_enum(value, allowed) is None:
            raise _reject(name, key, value, accepted)

    return _rule


# Mirrors ``api/routes/config.py::_coerce_reranker_model`` — a HuggingFace repo
# id is ``name`` or ``namespace/name``, each segment starting alphanumeric, at
# most one ``/``, no traversal component, bounded length. It is duplicated here
# rather than imported because ``api.routes.config`` drags in Flask + rag.vault
# at import time, which this stdlib-light script (and its hermetic tests) must
# not pay. ``test_eval_runner.py::test_reranker_model_rule_matches_the_app`` is
# the parity ratchet: it imports the real coercer and asserts identical verdicts
# over a vector table, so a hardening there fails here.
#
# This is the ONE cell value with a side effect beyond retrieval quality: it is
# handed to ``SentenceTransformer``, which treats an existing local path as a
# model directory and otherwise downloads from HuggingFace. The app deliberately
# forbids it as a chat-body override for exactly that reason; a cells file is
# less trusted than the Settings window, not more.
_RERANKER_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?")


def _reranker_model_rule(name: str, key: str, value: object) -> None:
    accepted = "HuggingFace repo id ('name' or 'namespace/name'), or '' to disable"
    if not isinstance(value, str):
        raise _reject(name, key, value, accepted)
    s = value.strip()
    if s == "":
        return
    if len(s) > 128 or ".." in s or not _RERANKER_MODEL_RE.fullmatch(s):
        raise _reject(name, key, value, accepted)


#: Per-``retrieve``-kwarg value rules. Ranges mirror
#: ``api/routes/vault.py::_resolve_chat_params`` (``_TOP_K_*``, ``_CUTOFF_*``,
#: ``_MMR_*``, ``_NUM_QUERIES_*``, ``_RERANK_CEILING_*``).
_RETRIEVE_RULES: Dict[str, Callable] = {
    "top_k": _int_rule(1, 32),
    "top_k_explicit": _bool_rule(),
    "similarity_cutoff": _float_rule(0.0, 1.0),
    "hybrid_enabled": _bool_rule(),
    "reranker_enabled": _bool_rule(),
    "reranker_model": _reranker_model_rule,
    "mmr_enabled": _bool_rule(),
    "mmr_lambda": _float_rule(0.0, 1.0),
    "query_expansion": _bool_rule(),
    "num_queries": _int_rule(1, 5),
    "rerank_pool_ceiling": _int_rule(10, 200),
    "wikilink_expansion": _bool_rule(),
    "thesaurus_expansion": _bool_rule(),
    "primer_enabled": _bool_rule(),
}

#: Per-config-key value rules. Ranges mirror
#: ``api/routes/config.py::_LLM_CONFIG_VALIDATORS`` exactly — these values are
#: written into the user's real config.json, so anything that map would drop
#: must not be persistable from here either.
_CONFIG_RULES: Dict[str, Callable] = {
    "vault_rrf_dense_weight": _float_rule(0.0, 10.0),
    "vault_rrf_bm25_weight": _float_rule(0.0, 10.0),
    "vault_rerank_pool_multiplier": _int_rule(1, 16),
    "vault_rerank_pool_floor": _int_rule(1, 200),
    "vault_reranker_device": _enum_rule(("auto", "cpu", "mps")),
    "vault_wikilink_neighbor_cap": _int_rule(1, 100),
    "vault_wikilink_node_cap": _int_rule(1, 200),
    "vault_wikilink_score_decay": _float_rule(0.0, 1.0),
    "vault_thesaurus_max_variants": _int_rule(1, 8),
    "context_window": _int_rule(512, 131072),
}


# --------------------------------------------------------------------------
# Cells
# --------------------------------------------------------------------------

def _validate_cell(raw: object, where: str) -> Dict:
    """Coerce one raw cell mapping into ``{name, retrieve, config}``.

    Raises :class:`CellError` naming the offending key — a typo'd knob, or a
    knob set to a value the app would reject, must fail loudly rather than be
    dropped into a run that then reports "no change".
    """
    if not isinstance(raw, dict):
        raise CellError(f"{where}: each cell must be an object, got {type(raw).__name__}")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise CellError(f"{where}: every cell needs a non-empty 'name'")

    def _map(key: str, rules: Dict[str, Callable]) -> Dict:
        value = raw.get(key) or {}
        if not isinstance(value, dict):
            raise CellError(f"cell {name!r}: '{key}' must be an object")
        for k, v in value.items():
            rule = rules.get(k)
            if rule is None:
                raise CellError(
                    f"cell {name!r}: unknown {key} key {k!r} "
                    f"(allowed: {', '.join(sorted(rules))})"
                )
            rule(name, k, v)
        return dict(value)

    return {"name": name, "retrieve": _map("retrieve", _RETRIEVE_RULES),
            "config": _map("config", _CONFIG_RULES)}


def _preset_cells(preset: str) -> List[Dict]:
    """Canned cell lists (baseline is injected separately by :func:`resolve_cells`)."""
    if preset == "reranker-ab":
        return [
            {"name": "rerank-off", "retrieve": {"reranker_enabled": False}},
            {"name": "bge-v2-m3", "retrieve": {"reranker_model": BGE_RERANKER}},
        ]
    if preset == "rerank-onoff":
        return [{"name": "rerank-off", "retrieve": {"reranker_enabled": False}}]
    if preset == "rrf-sweep":
        # Any weight ≠ 1.0 switches that query to relative_score fusion — the
        # runner prints the warning per cell; compare on rank metrics.
        return [
            {"name": "rrf-dense-2x",
             "config": {"vault_rrf_dense_weight": 2.0, "vault_rrf_bm25_weight": 1.0}},
            {"name": "rrf-bm25-2x",
             "config": {"vault_rrf_dense_weight": 1.0, "vault_rrf_bm25_weight": 2.0}},
            {"name": "rrf-dense-lean",
             "config": {"vault_rrf_dense_weight": 1.5, "vault_rrf_bm25_weight": 0.5}},
        ]
    if preset == "cutoff-sweep":
        # ``similarity_cutoff`` reaches the pipeline through exactly ONE branch:
        # ``rag/engine.py`` L1008-L1029 attaches ``SimilarityPostprocessor`` in
        # the ``elif not uses_fusion:`` arm — i.e. only when there is NO reranker
        # AND no fusion (``uses_fusion``, L878, is true when BM25 is present or
        # multi-query expansion is on). The baseline resolves BOTH reranker and
        # hybrid to True from config, so the pre-2026-07 version of this preset
        # was a 100% no-op: four cells, four byte-identical result sets, four
        # Δ=0.000 rows reading as "the cutoff doesn't matter" — a false
        # conclusion that cost ~20 minutes to reach.
        #
        # Each cell therefore turns off the two blockers as well. That makes the
        # preset a measurement of the **dense-only** path (no BM25 leg, no
        # cross-encoder), which is the only configuration in which the cutoff is
        # live at all — so the Δ column here compares cutoffs against each
        # other, NOT against the hybrid+rerank baseline row.
        return [
            {"name": f"cutoff-{value}",
             "retrieve": {"similarity_cutoff": value,
                          "reranker_enabled": False,
                          "hybrid_enabled": False,
                          # Multi-query expansion is the second fusion trigger
                          # (L878). It is off by default, but a user whose
                          # config turned it on would silently re-break the
                          # preset, so pin it rather than assume.
                          "query_expansion": False}}
            for value in (0.0, 0.15, 0.35, 0.5)
        ]
    if preset == "pool-sweep":
        # The pool size is ``_rerank_pool_size`` (rag/engine.py L648-L660):
        #     min(max(top_k * multiplier, floor), ceiling)
        # With the runner's ``top_k = max(ks)`` (10 by default) and the config
        # defaults multiplier=4 / floor=20, the pre-clamp value is 40 — so the
        # old ceilings 100 and 200 BOTH clamped to 40 and produced identical
        # runs. Raising the multiplier to its maximum (16) lifts the pre-clamp
        # to 160 at top_k=10, so every ceiling below that genuinely binds and
        # the cells really do feed the cross-encoder 20 / 50 / 100 candidates.
        # A ceiling of 200 is deliberately absent: it can never bind (multiplier
        # caps at 16 and floor at 200, so the pre-clamp cannot exceed 200), and
        # the inert-override detector would flag it every time.
        return [
            {"name": f"pool-{value}",
             "retrieve": {"rerank_pool_ceiling": value},
             "config": {"vault_rerank_pool_multiplier": 16}}
            for value in (20, 50, 100)
        ]
    raise CellError(f"unknown preset {preset!r} (choose from: {', '.join(PRESETS)})")


PRESETS: Tuple[str, ...] = (
    "reranker-ab", "rerank-onoff", "rrf-sweep", "cutoff-sweep", "pool-sweep",
)


def resolve_cells(preset: Optional[str] = None, cells_path: Optional[str] = None) -> List[Dict]:
    """Build the cell list to run: the implicit ``baseline`` first, then the rest.

    Exactly one source may be given (a ``--preset`` name or a ``--cells`` JSON
    file holding ``{"cells": [...]}``); neither ⇒ baseline only. Every cell is
    validated against :data:`_RETRIEVE_RULES` / :data:`_CONFIG_RULES` (names AND
    values), and a cell literally named ``baseline`` replaces the implicit one
    (so a file can define its own reference) rather than producing a duplicate
    row.
    """
    if preset and cells_path:
        raise CellError("pass either --preset or --cells, not both")

    raw_cells: List = []
    if preset:
        raw_cells = _preset_cells(preset)
        where = f"preset {preset!r}"
    elif cells_path:
        data = json.loads(Path(cells_path).read_text(encoding="utf-8"))
        if isinstance(data, list):          # tolerate a bare list
            raw_cells = data
        elif isinstance(data, dict):
            raw_cells = data.get("cells") or []
        else:
            raise CellError(f"{cells_path}: expected an object with 'cells' or a list")
        if not isinstance(raw_cells, list):
            raise CellError(f"{cells_path}: 'cells' must be a list")
        where = str(cells_path)
    else:
        where = "(built-in)"

    cells = [_validate_cell(raw, where) for raw in raw_cells]
    if not any(c["name"] == BASELINE_CELL_NAME for c in cells):
        cells.insert(0, {"name": BASELINE_CELL_NAME, "retrieve": {}, "config": {}})
    else:  # a user-supplied baseline still leads, so the Δ column is stable
        cells.sort(key=lambda c: 0 if c["name"] == BASELINE_CELL_NAME else 1)
    return cells


# --------------------------------------------------------------------------
# Config backup / restore — durable, atomic, crash-evident
# --------------------------------------------------------------------------
# The pre-2026-07 version had its guarantees exactly inverted: the DISPOSABLE
# override write used ``write_text_atomic`` while the RESTORE — the write that
# must not fail — used a truncating, non-atomic, un-fsync'd ``path.write_bytes``
# against a backup held only in RAM. A crash between truncate and write left the
# user with a zero-length config; a SIGKILL left them with sweep values and no
# record of what had been there.
#
# Now: the original bytes hit the disk (atomically, fsync'd) BEFORE the first
# override write, restore goes through the same atomic writer and is VERIFIED by
# reading back, and the sidecar is unlinked only once that verification passes.
# Four restore paths cover everything a process can catch (per-cell finally,
# outer finally, atexit, SIGTERM); SIGKILL/OOM catch nothing, which is why the
# sidecar has to be a file rather than a Python object.

def sidecar_path(config_path: str) -> str:
    """Path of the durable pre-sweep backup for *config_path*."""
    return str(config_path) + CONFIG_SIDECAR_SUFFIX


#: Armed restore, shared with the atexit/SIGTERM handlers. A tuple
#: ``(config_path, original_bytes_or_None)`` or ``None``. Guarded by a lock
#: because the SIGTERM handler runs on the main thread at an arbitrary
#: bytecode boundary and must not observe a half-assigned tuple.
#:
#: An RLock, NOT a plain Lock (2026-07-29 audit 1.4): that same
#: at-any-bytecode-boundary property means SIGTERM can arrive while the main
#: thread is INSIDE ``_arm_pending``'s critical section — the handler then
#: calls ``_restore_pending``, which re-acquires this lock on the thread that
#: already holds it. With a plain Lock that is a self-deadlock: the process
#: hangs instead of dying AND the config is never restored — the one abnormal
#: exit this whole section exists to survive. Reentrancy is safe here: the
#: handler either sees the fully-assigned previous tuple or the new one (the
#: assignment is a single store), and restore is idempotent.
_PENDING_RESTORE: Optional[Tuple[str, Optional[bytes]]] = None
_PENDING_LOCK = threading.RLock()
_GUARDS_INSTALLED = False


def _ensure_restore_guards() -> None:
    """Install the atexit + SIGTERM restore hooks once, lazily.

    Lazily (on the first arm) rather than at import: importing this module must
    stay side-effect-free — the test suite imports it, and a module-level
    ``signal.signal`` would rewrite the *pytest* process's SIGTERM disposition.
    """
    global _GUARDS_INSTALLED
    if _GUARDS_INSTALLED:
        return
    _GUARDS_INSTALLED = True
    atexit.register(_restore_pending, "atexit")
    try:
        signal.signal(signal.SIGTERM, _sigterm_restore)
    except (ValueError, OSError):
        # ``signal.signal`` only works on the main thread of the main
        # interpreter. Off it we still have atexit + both ``finally`` blocks +
        # the on-disk sidecar, so degrade instead of refusing to sweep.
        pass


def _sigterm_restore(signum, frame) -> None:  # pragma: no cover - signal path
    """Restore, then re-raise SIGTERM with its DEFAULT disposition.

    Re-raising (rather than ``sys.exit``) keeps the process's exit status
    honest — a supervisor that sent SIGTERM must see "died by SIGTERM", not a
    clean exit code that hides why the sweep stopped. Resetting the handler to
    ``SIG_DFL`` first makes the re-raise terminal instead of re-entering here.
    """
    _restore_pending("SIGTERM")
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTERM)


def _arm_pending(config_path: str, blob: Optional[bytes]) -> None:
    global _PENDING_RESTORE
    _ensure_restore_guards()
    with _PENDING_LOCK:
        _PENDING_RESTORE = (str(config_path), blob)


def _disarm_pending() -> None:
    global _PENDING_RESTORE
    with _PENDING_LOCK:
        _PENDING_RESTORE = None


def _restore_pending(reason: str) -> None:
    """Restore an armed config if one is still outstanding (idempotent).

    Idempotence is what makes it safe to call from four places: the first
    successful call disarms, so atexit after a clean ``finally`` is a no-op.
    """
    with _PENDING_LOCK:
        pending = _PENDING_RESTORE
    if pending is None:
        return
    print(f"\n[{reason}] restoring {pending[0]} from the sweep backup…", file=sys.stderr)
    try:
        restore_config(*pending)
    except Exception as exc:  # noqa: BLE001 — last-ditch handler, must not raise
        print(f"ERROR: automatic restore failed ({exc}). Recover by hand:\n"
              f"  cp {sidecar_path(pending[0])!r} {pending[0]!r}",
              file=sys.stderr)


def check_stale_sidecar(config_path: str) -> Optional[str]:
    """Actionable message when a previous run died holding the config, else None.

    Deliberately NOT an auto-restore: an unknown amount of time may have passed
    and the user may have edited ``config.json`` since (through the app's
    Settings window, which has no idea a sweep ever ran). Silently reverting
    those edits would be a second data-loss bug on top of the first. Refusing
    and printing both paths leaves the decision where the information is.
    """
    side = sidecar_path(config_path)
    if not Path(side).exists():
        return None
    return (
        f"A previous eval run did not restore your config.\n"
        f"  sweep backup : {side}\n"
        f"  live config  : {config_path}\n"
        f"The backup holds your ORIGINAL config bytes (an EMPTY backup file means "
        f"there was no config at all, so the recovery is to delete the live one).\n"
        f"Inspect both, then either restore:\n"
        f"  cp {side!r} {config_path!r} && rm {side!r}\n"
        f"or, if the live config is already the one you want, just delete the backup:\n"
        f"  rm {side!r}\n"
        f"Refusing to run until the backup is gone — a second sweep would overwrite it."
    )


def backup_config(config_path: str, *, persist: bool = True) -> Optional[bytes]:
    """Snapshot the config file's raw bytes (``None`` when it does not exist).

    With *persist* (the default) the bytes are ALSO written to
    :func:`sidecar_path` before returning, so the backup survives a SIGKILL that
    the in-memory return value could not.

    The "no config existed" case is encoded as a **zero-byte sidecar**, which is
    unambiguous: a valid ``config.json`` is a JSON object and therefore at least
    two bytes, so an empty file cannot be mistaken for real content. Writing
    *something* matters — a crash with no sidecar at all would leave sweep values
    in place with no evidence a sweep ever ran.

    Takes the path explicitly (never reads ``constants.CONFIG_FILE`` itself) so
    the sweep's write/restore cycle is unit-testable against a tmp file.
    """
    path = Path(config_path)
    blob = path.read_bytes() if path.exists() else None
    if persist:
        try:
            Path(sidecar_path(config_path)).parent.mkdir(parents=True, exist_ok=True)
            write_bytes_atomic(sidecar_path(config_path), blob if blob is not None else b"")
        except OSError as exc:
            # Refuse to proceed: without a durable backup the override write
            # below would be the un-undoable operation this whole section exists
            # to prevent.
            raise ConfigUnsafeError(
                f"could not write the config backup {sidecar_path(config_path)}: {exc}"
            ) from exc
    return blob


def restore_config(config_path: str, blob: Optional[bytes]) -> None:
    """Put *blob* back byte-for-byte (``None`` ⇒ remove a file we created).

    Writes through ``write_bytes_atomic`` (temp sibling + fsync + rename) so a
    crash mid-restore leaves either the sweep's config or the original, never a
    truncated hybrid — the guarantee the previous ``path.write_bytes`` lacked.

    The restore is then **verified** by reading back, and the sidecar is unlinked
    only if the bytes match. A mismatch raises: leaving the sidecar in place is
    what makes the next run refuse to start (:func:`check_stale_sidecar`) instead
    of quietly sweeping over a config the user never got back.

    The config loaders are stat-cached; the restore changes the file's
    ``(size, mtime_ns)`` and we additionally drop the in-process cache so a
    same-size restore inside one timestamp tick can't be served from it.
    """
    path = Path(config_path)
    if blob is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            # (2026-07-29 audit) A permissions/IO failure here means the
            # sweep's config is still live and "restored to no-config" would
            # be a lie. Raise so the sidecar stays in place — the next run
            # then refuses via check_stale_sidecar instead of sweeping over a
            # state the user never got back. (The verification below would
            # also catch it, but only after _disarm_pending-adjacent paths;
            # failing at the operation that failed names the real cause.)
            raise ConfigUnsafeError(
                f"could not remove {config_path} while restoring the "
                f"no-config-existed state ({exc}); the backup marker is kept "
                f"at {sidecar_path(config_path)} — remove {config_path} by "
                f"hand, then delete the marker."
            ) from exc
        restored: Optional[bytes] = path.read_bytes() if path.exists() else None
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_bytes_atomic(str(path), blob)
        restored = path.read_bytes()
    _drop_config_cache()

    if restored != blob:
        raise ConfigUnsafeError(
            f"config restore VERIFICATION FAILED for {config_path}; the backup is "
            f"kept at {sidecar_path(config_path)} — restore it by hand."
        )
    _disarm_pending()
    try:
        Path(sidecar_path(config_path)).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # A leftover sidecar is fail-SAFE (the next run refuses and tells the
        # user how to check), so it must not turn a SUCCESSFUL restore into an
        # exception — especially not from inside a ``finally``.
        print(f"WARNING: could not remove {sidecar_path(config_path)} after a verified "
              f"restore; delete it manually or the next run will refuse to start.",
              file=sys.stderr)


def apply_config_overrides(config_path: str, overrides: Dict) -> None:
    """Merge *overrides* into the config file (atomically, other keys preserved).

    A parse failure **aborts**. The previous version fell back to ``current =
    {}`` and then wrote a config containing only the override keys — obliterating
    the vault path, provider, model selections and every prompt. An unreadable
    config is a reason to stop, never a reason to invent one.
    """
    path = Path(config_path)
    current: Dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigUnsafeError(
                f"{config_path} could not be read/parsed ({exc}); refusing to write "
                f"sweep overrides over it — the merge would have dropped every key it "
                f"could not read."
            ) from exc
        if not isinstance(loaded, dict):
            raise ConfigUnsafeError(
                f"{config_path} is not a JSON object (got {type(loaded).__name__}); "
                f"refusing to write sweep overrides over it."
            )
        current = loaded
    current.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Deliberately NOT ``core.config.save_config``: that stamps ``config_version``
    # and materialises every default key, so a save/restore round-trip would
    # hand the user's file back structurally rewritten (and could trigger the
    # ``_MIGRATIONS`` path on a downgrade). This sweep must be byte-transparent
    # — merge the overrides, restore the exact original bytes afterwards.
    write_text_atomic(str(path), json.dumps(current, indent=2))
    _drop_config_cache()


def _drop_config_cache() -> None:
    """Best-effort invalidation of ``core.config``'s stat cache.

    The stat key ``(path, size, mtime_ns)`` self-invalidates in practice, but a
    same-size rewrite inside one filesystem timestamp tick would not — and a
    sweep that silently read the *previous* cell's config would report garbage.
    Imported lazily so the helpers above stay importable with a tmp path and no
    app config at all. ``test_eval_runner.py`` ratchets the symbol's existence:
    a rename in ``core/config.py`` would make this except-branch swallow the
    ImportError and every config cell would fabricate its deltas.
    """
    try:
        from core.config import _invalidate_config_cache
        _invalidate_config_cache()
    except Exception:
        pass


# --------------------------------------------------------------------------
# Baseline kwargs (mirrors the chat route's config resolution)
# --------------------------------------------------------------------------

def _cfg(cfg: Dict, key: str, default, cast: Callable):
    """Read one config knob, degrading to *default* on a missing/garbage value."""
    try:
        value = cfg.get(key, default)
        if value is None:
            return default
        return cast(value)
    except (TypeError, ValueError):
        return default


def baseline_retrieve_kwargs(cfg: Dict, ks: Sequence[int]) -> Dict:
    """Resolve ``retrieve()`` kwargs from config exactly as the chat route does.

    Two deliberate deviations from ``api/routes/vault.py::_resolve_chat_params``:

    * ``top_k = max(ks)`` — a metric at k=10 needs ten ranked results, so the
      breadth is driven by the cutoffs, not by ``vault_top_k``.
    * ``top_k_explicit=True`` — bypasses ``_effective_top_k``'s context-window
      autoscaling, which would otherwise silently change retrieval breadth when
      the chat model changes and make two runs incomparable.
    """
    from core.config import resolve_chat_model

    provider = str(cfg.get("provider") or "ollama").strip() or "ollama"
    out: Dict = {
        "llm_name": resolve_chat_model(cfg, provider),
        "embed_name": str(cfg.get("embed") or ""),
        "provider_name": provider,
        "top_k": max(int(k) for k in ks),
        "top_k_explicit": True,
        "similarity_cutoff": _cfg(cfg, "vault_similarity_cutoff", 0.25, float),
        "hybrid_enabled": _cfg(cfg, "vault_hybrid_enabled", True, bool),
        "reranker_enabled": _cfg(cfg, "vault_reranker_enabled", True, bool),
        "mmr_enabled": _cfg(cfg, "vault_mmr_enabled", False, bool),
        "mmr_lambda": _cfg(cfg, "vault_mmr_lambda", 0.5, float),
        "query_expansion": _cfg(cfg, "vault_query_expansion", False, bool),
        "num_queries": _cfg(cfg, "vault_num_queries", 3, int),
        "rerank_pool_ceiling": _cfg(cfg, "vault_rerank_pool_ceiling", 50, int),
        "wikilink_expansion": _cfg(cfg, "vault_wikilink_expansion", False, bool),
        "thesaurus_expansion": _cfg(cfg, "vault_thesaurus_expansion", False, bool),
        "primer_enabled": _cfg(cfg, "vault_primer_enabled", False, bool),
    }
    # Reranker model is config-only on the chat route too (never a body
    # override — it decides what gets downloaded from HuggingFace).
    model = str(cfg.get("vault_reranker_model") or "").strip()
    if model:
        out["reranker_model"] = model
    return out


# --------------------------------------------------------------------------
# Effective knobs + inert-override detection
# --------------------------------------------------------------------------

def effective_knobs(cell: Dict, base_kwargs: Dict, base_config: Dict) -> Tuple[Dict, Dict]:
    """``(retrieve kwargs, config)`` this cell will actually run with."""
    kwargs = dict(base_kwargs or {})
    kwargs.update(cell.get("retrieve") or {})
    cfg = dict(base_config or {})
    cfg.update(cell.get("config") or {})
    return kwargs, cfg


def _reranker_active(kwargs: Dict) -> bool:
    """Whether the engine will hold a reranker object for these kwargs.

    Mirrors ``rag/vault.py`` L4985-L4989: the reranker is fetched only when
    ``reranker_enabled AND reranker_model`` — an enabled-but-model-less config
    silently runs rerank-free, which is the mis-attribution :func:`_reranker_state`
    exists to expose.
    """
    return bool(kwargs.get("reranker_enabled")) and bool(
        str(kwargs.get("reranker_model") or "").strip())


def _uses_fusion(kwargs: Dict) -> bool:
    """Mirrors ``rag/engine.py`` L878: BM25 leg present OR multi-query expansion."""
    return bool(kwargs.get("hybrid_enabled")) or (
        bool(kwargs.get("query_expansion")) and int(kwargs.get("num_queries") or 1) > 1)


def detect_inert_overrides(cell: Dict, base_kwargs: Dict, base_config: Dict) -> List[str]:
    """Overrides in *cell* that provably cannot reach an active code path.

    A knob the engine never reads produces a Δ of exactly 0.000, which reads as
    "this knob doesn't matter" — the opposite of the truth and the single most
    expensive way for a 20-minute sweep to mislead. Each rule below cites the
    ``rag/engine.py`` line that owns the decision, so a future engine change
    breaks a comment a reader is standing in front of.

    Detection only; never mutates the cell. A false positive costs one printed
    warning, so the rules are written to fire on provable inertness only.
    """
    kwargs, cfg = effective_knobs(cell, base_kwargs, base_config)
    overrides = cell.get("retrieve") or {}
    cfg_over = cell.get("config") or {}
    notes: List[str] = []

    reranked = _reranker_active(kwargs)
    fused = _uses_fusion(kwargs)

    # similarity_cutoff — rag/engine.py L1008-L1029: the SimilarityPostprocessor
    # is attached ONLY in the ``elif not uses_fusion:`` arm, i.e. no reranker and
    # no fusion. With either present the value is read into the engine object
    # and never used.
    if "similarity_cutoff" in overrides and (reranked or fused):
        blockers = " and ".join(
            x for x in (("an active reranker" if reranked else ""),
                        ("fusion (hybrid/query-expansion)" if fused else "")) if x)
        notes.append(
            f"similarity_cutoff={overrides['similarity_cutoff']!r} is INERT: "
            f"rag/engine.py L1008-L1029 attaches the cutoff postprocessor only when "
            f"there is no reranker and no fusion, but this cell runs with {blockers}."
        )

    # rerank_pool_ceiling / pool sizing — rag/engine.py L648-L660:
    #   pool = min(max(top_k * multiplier, floor), ceiling)
    # so the ceiling is inert whenever it sits at or above the pre-clamp value,
    # and the whole pool concept is inert with no reranker to feed.
    pool_keys = [k for k in ("rerank_pool_ceiling",) if k in overrides] + \
                [k for k in ("vault_rerank_pool_multiplier", "vault_rerank_pool_floor")
                 if k in cfg_over]
    if pool_keys and not reranked:
        notes.append(
            f"{', '.join(pool_keys)} INERT: the candidate pool "
            f"(rag/engine.py L648-L660) is only fetched to feed the cross-encoder, "
            f"and this cell runs without an active reranker."
        )
    elif "rerank_pool_ceiling" in overrides and reranked:
        multiplier = _cfg(cfg, "vault_rerank_pool_multiplier", _POOL_MULTIPLIER_DEFAULT, int)
        floor = _cfg(cfg, "vault_rerank_pool_floor", _POOL_FLOOR_DEFAULT, int)
        top_k = _cfg(kwargs, "top_k", 10, int)
        pre_clamp = max(top_k * multiplier, floor)
        ceiling = _cfg(overrides, "rerank_pool_ceiling", pre_clamp, int)
        if ceiling >= pre_clamp:
            notes.append(
                f"rerank_pool_ceiling={ceiling} is INERT: "
                f"min(max(top_k={top_k} × multiplier={multiplier}, floor={floor}), "
                f"ceiling) = {min(pre_clamp, ceiling)} — the ceiling never binds, so "
                f"this cell runs the SAME pool as any other ceiling ≥ {pre_clamp}."
            )

    # Sub-knobs whose owning feature is off. Each is a plain guard in the engine
    # (the retriever wrapper is simply not constructed), so the value is unread.
    for sub, owner, why in (
        ("mmr_lambda", "mmr_enabled", "the MMR retriever is only wrapped when MMR is on"),
        ("num_queries", "query_expansion",
         "multi-query expansion is forced to 1 query when the toggle is off "
         "(rag/engine.py L874-L876)"),
    ):
        if sub in overrides and not kwargs.get(owner):
            notes.append(f"{sub}={overrides[sub]!r} is INERT: {why} ({owner} is off).")

    for sub, owner, why in (
        ("vault_wikilink_neighbor_cap", "wikilink_expansion", "wikilink expansion is off"),
        ("vault_wikilink_node_cap", "wikilink_expansion", "wikilink expansion is off"),
        ("vault_wikilink_score_decay", "wikilink_expansion", "wikilink expansion is off"),
        ("vault_thesaurus_max_variants", "thesaurus_expansion", "thesaurus expansion is off"),
    ):
        if sub in cfg_over and not kwargs.get(owner):
            notes.append(f"{sub}={cfg_over[sub]!r} is INERT: {why}.")

    # Wikilink expansion itself is rerank-gated (rag/engine.py L778-L782: the
    # reranker's top_n trim is the only bound on the post-expansion set, so the
    # wrap is skipped entirely without one).
    if overrides.get("wikilink_expansion") and not reranked:
        notes.append(
            "wikilink_expansion=True is INERT: the engine only wraps the wikilink "
            "retriever when a reranker is present (rag/engine.py L778-L782)."
        )

    # RRF leg weights need a fusion retriever to weight.
    if any(k in cfg_over for k in ("vault_rrf_dense_weight", "vault_rrf_bm25_weight")) \
            and not fused:
        notes.append(
            "vault_rrf_* leg weights are INERT: no fusion retriever is built "
            "(rag/engine.py L878 — needs the BM25 leg or multi-query expansion)."
        )

    return notes


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------

def _percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile over *values* (0.0 for an empty list).

    Nearest-rank (``ceil(pct·n)``) rather than an interpolating percentile: the
    sample is a few dozen latencies and an interpolated p95 between two very
    different values would invent a number no query actually took.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil(pct * len(ordered)) - 1))
    return ordered[idx]


def _latency_summary(samples: Sequence[float]) -> Dict[str, float]:
    """``{p50, p95, mean}`` in milliseconds (zeros for an empty sample)."""
    if not samples:
        return {"p50": 0.0, "p95": 0.0, "mean": 0.0}
    return {
        "p50": round(statistics.median(samples), 1),
        "p95": round(_percentile(samples, 0.95), 1),
        "mean": round(statistics.fmean(samples), 1),
    }


def _effective_rrf_weights(cell: Dict, base_config: Dict) -> Tuple[float, float]:
    """The RRF leg weights this cell will actually run with (override → config → 1.0)."""
    overrides = cell.get("config") or {}

    def _one(key: str) -> float:
        if key in overrides:
            return _cfg(overrides, key, 1.0, float)
        return _cfg(base_config or {}, key, 1.0, float)

    return _one("vault_rrf_dense_weight"), _one("vault_rrf_bm25_weight")


def _resolve_device_mode(cfg: Dict) -> str:
    """Mirror of ``rag/vault.py::_resolve_reranker_device_mode`` (unknown ⇒ auto)."""
    mode = str(cfg.get("vault_reranker_device") or "auto").strip().lower()
    return mode if mode in ("auto", "cpu", "mps") else "auto"


def _reranker_state(manager, kwargs: Dict, cfg: Dict) -> Dict:
    """Positively determine what the reranker actually did for this cell.

    The old ``getattr(manager, "_reranker_failed", False)`` mis-attributed BOTH
    ways, and the second way was the dangerous one:

    (a) A ``reranker_enabled: False`` cell never calls ``_get_reranker``, so it
        inherited the PREVIOUS cell's flag — firing on ``reranker-ab``, the very
        first preset the guide recommends.
    (b) Worse: if ``vault_reranker_model`` resolves empty, ``_get_reranker`` is
        never called either (rag/vault.py L4985-L4989 gates on
        ``reranker_enabled AND reranker_model``), the flag stays False, and the
        cell runs rerank-free **while being scored as the reranked baseline** —
        every Δ in the table then measured against a mislabelled reference.

    So: derive ``requested`` from the cell's own resolved kwargs, and read the
    manager's state through its cache key ``f"{model}::{device_mode}"`` — the
    same key ``_get_reranker`` writes — so both the success and the failure are
    attributed to the model THIS cell asked for.

    * ``none``    — this cell did not request a reranker (positively known).
    * ``loaded``  — the manager holds a live reranker under this cell's key.
    * ``failed``  — the sticky failure flag refers to this cell's key.
    * ``unknown`` — requested, but neither is true (e.g. a fake manager in
      tests, or every query failed before the reranker was constructed). Named
      rather than silently folded into "loaded" or "none".
    """
    model = str(kwargs.get("reranker_model") or "").strip()
    device_mode = _resolve_device_mode(cfg)
    requested = _reranker_active(kwargs)
    cache_key = f"{model}::{device_mode}"

    if not requested:
        state = "none"
    elif (getattr(manager, "_reranker", None) is not None
            and getattr(manager, "_reranker_model_loaded", "") == cache_key):
        state = "loaded"
    elif (bool(getattr(manager, "_reranker_failed", False))
            and getattr(manager, "_reranker_last_tried", "") == cache_key):
        # ``_reranker_last_tried`` is what makes this positive rather than
        # inherited: the sticky flag is only ever about the last key TRIED, so
        # reading them together attributes the failure correctly.
        state = "failed"
    else:
        state = "unknown"

    return {"state": state, "requested": requested,
            "model": model, "device_mode": device_mode}


def _run_one_cell(
    manager,
    queries: Sequence[RetrievalQuery],
    cell: Dict,
    ks: Sequence[int],
    *,
    base_kwargs: Dict,
    base_config: Dict,
    warmup: bool,
    echo: Callable[[str], None],
) -> Dict:
    """Replay every query through ``manager.retrieve`` under this cell's knobs."""
    kwargs, cfg = effective_knobs(cell, base_kwargs, base_config)

    # ``stage_cb`` is passed through but is NOT a swept knob (it is excluded from
    # RETRIEVE_KEYS): it is a progress callback the runner owns, not a retrieval
    # parameter — letting a cell set it would let a cells file inject a callable
    # and would change nothing measurable. Here it gives the operator the one
    # thing an eval run otherwise hides: which phase a multi-minute first query
    # is stuck in (index load / BM25 rebuild / reranker download).
    last_stage: List[str] = [""]

    def _stage(msg: str) -> None:
        last_stage[0] = str(msg)

    if warmup and queries:
        # Discarded: pays the index / BM25 / reranker load once so the recorded
        # latencies compare like for like across cells. Stages ARE echoed here
        # (unlike the scored loop) because this is the call that can silently
        # take minutes.
        try:
            manager.retrieve(queries[0].query, **kwargs,
                             stage_cb=lambda m: echo(f"    · {m}"))
        except Exception as exc:  # a warm-up failure is not a result
            echo(f"  warm-up retrieve failed ({exc}) — continuing")

    per_query: List[Dict] = []
    scores = []
    latencies: List[float] = []
    failures: List[Dict] = []
    consecutive_failures = 0
    aborted = ""
    total = len(queries)
    # Print every query for a small set; throttle for a large one so the console
    # stays readable while still moving (a silent 20-minute run reads as a hang).
    step = 1 if total <= 30 else 5
    started_cell = time.perf_counter()

    for n, q in enumerate(queries, start=1):
        started = time.perf_counter()
        try:
            chunks = manager.retrieve(q.query, **kwargs, stage_cb=_stage)
        except Exception as exc:  # noqa: BLE001
            # Tolerated exactly like the warm-up above, and for the same reason:
            # the embed provider (LM Studio / Ollama) must be *running* for a
            # retrieval to happen, and the app being closed is precisely when it
            # often is not. An unguarded raise here killed the entire sweep on
            # query #1 with a traceback and zero results.
            #
            # The failed query is EXCLUDED from scoring rather than scored as a
            # miss: a miss is a retrieval outcome (the engine looked and found
            # nothing), a failure is the absence of one, and averaging the two
            # would silently depress every metric in the cell.
            consecutive_failures += 1
            failures.append({"id": q.id, "error": str(exc)[:300]})
            per_query.append({"id": q.id, "error": str(exc)[:300],
                              "stage": last_stage[0], "excluded": True})
            echo(f"  [{n}/{total}] {q.id}: retrieve FAILED at stage "
                 f"{last_stage[0] or '?'!r} ({exc})")
            if consecutive_failures >= MAX_CONSECUTIVE_QUERY_FAILURES:
                aborted = (f"{consecutive_failures} consecutive retrieval failures — "
                           f"aborting this cell (the remaining queries would fail "
                           f"identically); later cells still run.")
                echo(f"  {aborted}")
                break
            continue

        consecutive_failures = 0
        latencies.append((time.perf_counter() - started) * 1000.0)
        retrieved = [(str(getattr(c, "source", "") or ""), float(getattr(c, "score", 0.0) or 0.0))
                     for c in (chunks or [])]
        score = score_query([src for src, _ in retrieved], q, ks)
        scores.append(score)
        # ``hit_ranks`` are 1-based ranks over the DEDUPED document list
        # (retrieval_metrics.dedupe_ranked_sources), while ``retrieved`` is the
        # raw per-CHUNK list — three chunks of one note occupy one deduped rank
        # but three raw slots. So ``retrieved[first_hit_rank - 1]`` is NOT the
        # hit and must never be used to name it. Recording the deduped source
        # explicitly makes the payload self-describing instead of leaving the
        # reader to (mis)derive it.
        ranked_docs = dedupe_ranked_sources([src for src, _ in retrieved])
        first_rank = score.hit_ranks[0] if score.hit_ranks else None
        per_query.append({
            "id": q.id,
            "first_hit_rank": first_rank,
            "first_hit_source": ranked_docs[first_rank - 1] if first_rank else None,
            "retrieved": [[src, sc] for src, sc in retrieved],
        })

        if n % step == 0 or n == total:
            done = n
            elapsed = time.perf_counter() - started_cell
            eta = (elapsed / done) * (total - done) if done else 0.0
            echo(f"  [{done}/{total}] {_latency_summary(latencies)['p50']} ms p50 · "
                 f"ETA {eta:.0f}s")

    return {
        "name": cell["name"],
        "retrieve_kwargs": dict(cell.get("retrieve") or {}),
        "config_overrides": dict(cell.get("config") or {}),
        "reranker": _reranker_state(manager, kwargs, cfg),
        "metrics": aggregate(scores, ks),
        "latency_ms": _latency_summary(latencies),
        "scored_queries": len(scores),
        "failed_queries": failures,
        "aborted": aborted,
        "per_query": per_query,
    }


def _release_outstanding(
    outstanding: Optional[Tuple[str, Optional[bytes]]],
    echo: Callable[[str], None],
) -> None:
    """Restore an outstanding config override exactly once, never masking.

    Called from a ``finally``, so a failure here MUST NOT raise: doing so would
    replace whatever exception was already in flight with a restore error, and
    the pre-fix code compounded that — the inner ``finally`` raised before
    clearing ``outstanding``, so the outer ``finally`` retried the identical
    failing write and its exception masked the first one. Disarming happens
    inside :func:`restore_config` on success, and the sidecar plus the loud
    message below are the recovery path on failure.
    """
    if outstanding is None:
        return
    try:
        restore_config(*outstanding)
    except Exception as exc:  # noqa: BLE001 — see docstring
        echo(f"ERROR: could not restore {outstanding[0]} ({exc}); the original bytes "
             f"are preserved at {sidecar_path(outstanding[0])}.")
        print(f"ERROR: config NOT restored — recover with:\n"
              f"  cp {sidecar_path(outstanding[0])!r} {outstanding[0]!r}", file=sys.stderr)


def run_cells(
    manager,
    queries: Sequence[RetrievalQuery],
    cells: Sequence[Dict],
    ks: Sequence[int] = DEFAULT_KS,
    *,
    base_kwargs: Optional[Dict] = None,
    base_config: Optional[Dict] = None,
    config_path: Optional[str] = None,
    warmup: bool = True,
    echo: Callable[[str], None] = print,
    checkpoint: Optional[Callable[[List[Dict]], None]] = None,
) -> List[Dict]:
    """Run every cell over every query and return the per-cell result records.

    *manager* only needs the ``retrieve(message, **kwargs) -> [RetrievedChunk]``
    contract the real ``ObsidianVaultManager`` exposes (plus, optionally, the
    reranker cache attributes :func:`_reranker_state` reads), so the whole sweep
    is drivable with a fake in tests.

    A cell carrying ``config`` overrides temporarily rewrites *config_path*:
    durable sidecar backup → merged write → run → **restore in a ``finally``**.
    An outer ``finally`` covers the interpreter-level exits, and the module's
    atexit/SIGTERM guards cover the rest — see the *Config safety* section of the
    module docstring.

    *checkpoint*, when given, is called with the results-so-far after EVERY
    cell. A sweep is minutes-to-tens-of-minutes long; losing all of it because
    the run was interrupted at cell 4 of 5 is pure waste when the completed
    cells are already fully scored.
    """
    base_kwargs = dict(base_kwargs or {})
    base_config = dict(base_config or {})
    results: List[Dict] = []
    outstanding: Optional[Tuple[str, Optional[bytes]]] = None
    try:
        for cell in cells:
            overrides = cell.get("config") or {}
            dense_w, bm25_w = _effective_rrf_weights(cell, base_config)
            echo(f"\n[cell] {cell['name']}"
                 + (f"  retrieve={cell.get('retrieve')}" if cell.get("retrieve") else "")
                 + (f"  config={overrides}" if overrides else ""))
            if dense_w != 1.0 or bm25_w != 1.0:
                echo(f"  NOTE: RRF weights {dense_w}/{bm25_w} ≠ 1.0/1.0 — this cell runs "
                     f"relative_score fusion instead of reciprocal_rerank. Score SCALES "
                     f"differ from the baseline; compare rank metrics only.")
            inert = detect_inert_overrides(cell, base_kwargs, base_config)
            for note in inert:
                echo(f"  !! INERT OVERRIDE: {note}")
                print(f"WARNING: cell {cell['name']!r}: {note}", file=sys.stderr)

            if overrides:
                if not config_path:
                    raise CellError(
                        f"cell {cell['name']!r} has config overrides but no config path "
                        f"was provided"
                    )
                blob = backup_config(config_path)
                outstanding = (config_path, blob)
                _arm_pending(config_path, blob)
                apply_config_overrides(config_path, overrides)
            try:
                result = _run_one_cell(
                    manager, queries, cell, ks,
                    base_kwargs=base_kwargs, base_config=base_config,
                    warmup=warmup, echo=echo,
                )
            finally:
                if overrides:
                    # Clear the local handle FIRST, unconditionally: the outer
                    # ``finally`` must never re-attempt the same restore (see
                    # _release_outstanding's docstring — that retry is what
                    # masked the original exception pre-fix).
                    pending, outstanding = outstanding, None
                    _release_outstanding(pending, echo)
            result["inert_overrides"] = inert
            results.append(result)

            # Drain the manager's status ring so its warnings (embed mismatch,
            # reranker load failure, BM25 rebuild) reach the operator. The app
            # polls this ring from the UI; a standalone runner that never read it
            # left ~200 buffered lines of exactly the diagnostics that explain a
            # surprising number.
            for line in _drain_status(manager):
                echo(f"  [manager] {line}")

            rr = result["reranker"]
            if rr["state"] == "failed":
                print(
                    f"WARNING: cell {cell['name']!r} ran with a FAILED reranker load "
                    f"({rr['model']!r} on {rr['device_mode']}) — its numbers reflect "
                    f"retrieval WITHOUT rerank, not a reranker that did not help. "
                    f"Check chatekld.log.", file=sys.stderr)
            elif rr["state"] == "unknown" and rr["requested"]:
                print(
                    f"WARNING: cell {cell['name']!r} requested reranker {rr['model']!r} "
                    f"but the manager holds no loaded instance for it — treat this "
                    f"cell's rerank attribution as unverified.", file=sys.stderr)
            if result["failed_queries"]:
                print(
                    f"WARNING: cell {cell['name']!r} EXCLUDED "
                    f"{len(result['failed_queries'])} quer"
                    f"{'y' if len(result['failed_queries']) == 1 else 'ies'} whose "
                    f"retrieval raised; metrics cover {result['scored_queries']} "
                    f"quer{'y' if result['scored_queries'] == 1 else 'ies'}.",
                    file=sys.stderr)

            metrics = result["metrics"]
            echo("  " + "  ".join(f"{k}={metrics[k]:.3f}" for k in sorted(metrics) if k != "n")
                 + f"  ({result['latency_ms']['p50']} ms p50, rerank={rr['state']})")

            if checkpoint is not None:
                try:
                    checkpoint(results)
                except Exception as exc:  # noqa: BLE001
                    # A checkpoint is a convenience; failing to write one must
                    # not lose the in-memory results it was trying to protect.
                    echo(f"  (checkpoint write failed: {exc})")
    finally:
        pending, outstanding = outstanding, None
        _release_outstanding(pending, echo)
    return results


def _drain_status(manager) -> List[str]:
    """Best-effort ``drain_status_messages()`` (fakes in tests may not have it)."""
    drain = getattr(manager, "drain_status_messages", None)
    if not callable(drain):
        return []
    try:
        return [str(line) for line in (drain() or [])]
    except Exception:  # noqa: BLE001
        return []


# --------------------------------------------------------------------------
# Index inspection
# --------------------------------------------------------------------------

def read_index_meta(index_dir: str) -> Dict:
    """Parse ``obsidian_meta.json`` (``{}`` when missing/corrupt)."""
    path = Path(index_dir) / META_JSON
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _sources_from_materials(index_dir: str) -> set:
    """Tier 1: the indexer's own per-source manifest — 1.3 MB instead of 1.5 GB.

    **Why this is the same set.** ``rag/vault.py::_write_index_manifest`` is fed
    ``_manifest_source(doc)``, which reads ``metadata["source"] or
    metadata["file_path"]`` — the *identical* field precedence
    :func:`_sources_from_full_docstore` uses, from the identical per-document
    metadata. The writer then reduces per-source (skipping only the ``""``
    bucket, which the docstore reader also skips via its truthiness test), so
    both produce the set of distinct non-empty sources of the same documents.

    The writer's ``Path(raw).resolve().relative_to(vault_root)`` step does not
    break the equality either: the indexer stamps ``source`` as a vault-RELATIVE
    posix path (``rag/vault.py`` L3325/L3457), so ``resolve()`` resolves it
    against the CWD and ``relative_to(vault_root)`` raises — falling through to
    ``source = str(raw_source)``, the input unchanged. In the one case where it
    does NOT raise (the app was launched with its CWD *inside* the vault), the
    relativisation reproduces exactly the same relative path. Either way the
    string is the raw ``metadata["source"]``, which is also what
    ``RetrievedChunk.source`` carries (``rag/engine.py`` L1065) and what the
    golden set's ``relevant_sources`` record — so all three sides compare in one
    space, and :func:`norm_source` folds case/NFC on top.

    **Why a stale manifest degrades safely.** This set is used for exactly one
    purpose: dropping golden queries whose documents have all left the index. A
    manifest older than the index can only mis-classify *which queries are
    excluded* — it can never change a retrieved ranking or a metric value. An
    over-inclusive manifest keeps a query that then scores a genuine 0 (visible
    in the numbers); an under-inclusive one drops a query, and the drop is
    printed and counted in ``excluded_stale``. Neither silently biases a Δ,
    because every cell is scored over the identical query list.
    """
    path = Path(index_dir) / MATERIALS_JSON
    payload = json.loads(path.read_text(encoding="utf-8"))
    materials = payload.get("materials") if isinstance(payload, dict) else None
    if not isinstance(materials, list):
        raise ValueError(f"{path}: no 'materials' list")
    sources = set()
    for row in materials:
        if not isinstance(row, dict):
            continue
        raw = row.get("source") or ""
        if raw:
            sources.add(norm_source(str(raw)))
    if not sources:
        raise ValueError(f"{path}: manifest lists no sources")
    return sources


def _sources_from_ref_doc_info(index_dir: str) -> set:
    """Tier 2: stream ``docstore/ref_doc_info`` with ijson (bounded memory).

    ``ijson`` is already a pinned dependency (``scripts/migrate_vector_store.py``
    uses it for exactly this reason — streaming a multi-GB docstore without
    materialising it). ``ref_doc_info`` carries one entry per source DOCUMENT
    with its metadata, so it yields the same source set as the per-node scan at
    a fraction of the cost; ``rag/vault.py::_build_index_manifest_from_docstore``
    already recovers the manifest from this very key, which is the in-app
    precedent for treating the two as equivalent.
    """
    import ijson

    path = Path(index_dir) / "docstore.json"
    sources = set()
    with open(path, "rb") as f:
        for _ref_id, info in ijson.kvitems(f, "docstore/ref_doc_info"):
            if not isinstance(info, dict):
                continue
            meta = info.get("metadata")
            if not isinstance(meta, dict):
                continue
            raw = meta.get("source") or meta.get("file_path") or ""
            if raw:
                sources.add(norm_source(str(raw)))
    if not sources:
        raise ValueError(f"{path}: ref_doc_info yielded no sources")
    return sources


def _sources_from_full_docstore(index_dir: str) -> set:
    """Tier 3 (last resort): load the whole docstore through LlamaIndex.

    Measured on the maintainer's vault: ``SimpleDocumentStore.from_persist_dir``
    ``json.loads`` a 1.456 GB file and ``.docs`` then rebuilds every node object
    — ~7.2 GB peak RSS and 11-20 s to produce a set of ~9.7k strings. Kept only
    as the fallback for an index whose manifest is missing AND whose docstore
    ijson cannot stream (a shape change), because returning an empty set here is
    not free: it disables the stale-qrel filter entirely.
    """
    from llama_index.core.storage.docstore import SimpleDocumentStore

    docstore = SimpleDocumentStore.from_persist_dir(str(index_dir))
    sources = set()
    for node in docstore.docs.values():
        meta = getattr(node, "metadata", None) or {}
        raw = meta.get("source") or meta.get("file_path") or ""
        if raw:
            sources.add(norm_source(raw))
    return sources


def docstore_sources(index_dir: str, echo: Callable[[str], None] = print) -> set:
    """Normalized set of every source path present in the index.

    Cheapest-first fallback chain (see each tier for its equivalence argument):
    the indexer's ``indexed_materials.json`` manifest → a streamed
    ``docstore/ref_doc_info`` scan → the full LlamaIndex docstore load.

    **Contract preserved exactly:** an empty set means "we could not read the
    index", and :func:`filter_stale_queries` drops NOTHING in that case. Every
    tier therefore raises (rather than returning an empty set) when it produces
    no sources, so a truthy-but-empty read can never be mistaken for "the index
    contains nothing" and silently exclude the whole golden set.
    """
    for label, reader in (
        (MATERIALS_JSON, _sources_from_materials),
        ("docstore/ref_doc_info (streamed)", _sources_from_ref_doc_info),
        ("docstore.json (full load)", _sources_from_full_docstore),
    ):
        try:
            sources = reader(index_dir)
        except Exception as exc:  # noqa: BLE001
            echo(f"  ({label}: unavailable — {exc})")
            continue
        if sources:
            echo(f"  source inventory read from {label} ({len(sources)} sources)")
            return sources
    return set()


def filter_stale_queries(
    queries: Sequence[RetrievalQuery],
    known_sources: set,
) -> Tuple[List[RetrievalQuery], List[RetrievalQuery]]:
    """Split *queries* into (scoreable, stale).

    A query is **stale** when every one of its relevant documents has vanished
    from the index — scoring it would count a guaranteed miss against every
    cell equally, which is noise, not signal. An empty *known_sources* (we
    could not read the docstore) proves nothing, so nothing is dropped.
    """
    if not known_sources:
        return list(queries), []
    keep: List[RetrievalQuery] = []
    stale: List[RetrievalQuery] = []
    for q in queries:
        wanted = {norm_source(s) for s in (q.relevant_sources or [])}
        wanted.discard("")
        (keep if (wanted & known_sources) else stale).append(q)
    return keep, stale


def bm25_sidecar_is_stale(index_dir: str) -> Optional[str]:
    """Warning text when the first hybrid query will likely rebuild BM25, else None.

    The rebuild re-tokenises every node and persists a sidecar that can reach
    ~2 GB — minutes of CPU and a large write, inside what the runner presents as
    a silent "warm-up". The user deserves to know before, not after.

    Heuristic (deliberately conservative — it only prints a warning): missing
    sidecar ⇒ certain rebuild; sidecar older than the docstore ⇒ the index has
    been written since, so the sidecar's doc-count guard will very likely reject
    it. A false positive costs one line of text.
    """
    sidecar = Path(index_dir) / "bm25_index"
    docstore = Path(index_dir) / "docstore.json"
    if not sidecar.exists():
        return ("no BM25 sidecar at "
                f"{sidecar} — the first hybrid query will re-tokenise the whole "
                f"docstore and WRITE a fresh sidecar (minutes; can reach ~2 GB).")
    try:
        if docstore.exists() and docstore.stat().st_mtime > sidecar.stat().st_mtime:
            return (f"the BM25 sidecar at {sidecar} is OLDER than {docstore.name} — "
                    f"it will most likely be rejected and rebuilt (minutes; a large "
                    f"write).")
    except OSError:
        return None
    return None


# --------------------------------------------------------------------------
# App-liveness detection (a running server makes this run unsafe)
# --------------------------------------------------------------------------

def detect_live_server() -> Optional[str]:
    """Base URL of a live ChatEKLD server, or ``None``.

    Reuses the app's own discovery ladder (``core/driver.py::resolve_base_url``:
    ``CHATEKLD_API_URL`` → ``BASE_DIR/server.json`` pid+health → the legacy
    ``:5000`` probe) rather than inventing a second one, so "is the app running"
    has exactly one definition in this codebase. ``papermind server`` refuses to
    start beside a live server on precisely this rationale — two processes with
    two independent in-process op-locks over one vault — and a retrieval sweep
    that rewrites the BM25 sidecar under the app's feet is the same hazard.

    Any failure inside discovery yields ``None`` (cannot detect ⇒ fall back to
    the interactive prompt), never a crash.
    """
    try:
        from core.driver import resolve_base_url
        return resolve_base_url()
    except Exception:  # noqa: BLE001
        return None


def confirm_proceed(assume_yes: bool) -> bool:
    """Interactive "is the app closed?" gate; ``--yes`` (``assume_yes``) skips it.

    Advisory ONLY — the binding check is :func:`detect_live_server`, which
    ``--yes`` cannot bypass. Returns ``True`` only on an explicit y/yes (or
    ``assume_yes``); an EOF (piped / non-interactive) is treated as "no".

    The text below is deliberately explicit about the writes: the pre-2026-07
    version claimed the run only reads the index, which was false for the BM25
    and wikilink sidecars (see the module docstring).
    """
    if assume_yes:
        return True
    print(
        "\nThe ChatEKLD app MUST be closed before evaluating — the manager's\n"
        "locks are in-process only, so a running app would race this run on\n"
        "the BM25 sidecar and the index checkpoints.\n"
        "\nThis run WRITES:\n"
        "  · <index>/bm25_index/        (rebuilt + persisted if cold or stale;\n"
        "                                minutes of CPU, can reach ~2 GB)\n"
        "  · <index>/wikilink_sidecar.json (only if a cell enables wikilink\n"
        "                                expansion)\n"
        "  · config.json                (only for cells with config overrides;\n"
        "                                original bytes backed up to\n"
        "                                config.json.evalbak and restored after)\n"
        "  · the results file under BASE_DIR/eval/results/\n"
        "It never writes to your vault, and never re-embeds anything."
    )
    try:
        answer = input("Is the app closed and do you want to proceed? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _parse_ks(raw: str) -> List[int]:
    """``"1,5,10"`` → ``[1, 5, 10]`` (sorted, positive, de-duplicated)."""
    values = sorted({int(part) for part in str(raw).split(",") if part.strip()})
    values = [v for v in values if v > 0]
    if not values:
        raise ValueError(f"no positive cutoffs in --k {raw!r}")
    return values


def _default_golden() -> str:
    return os.path.join(BASE_DIR, "eval", "golden_retrieval.json")


def _default_out() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(BASE_DIR, "eval", "results", f"retrieval_eval_{stamp}.json")


def _print_preflight(
    cells: Sequence[Dict],
    queries: Sequence[RetrievalQuery],
    base_kwargs: Dict,
    base_config: Dict,
    warmup_ms: Optional[float],
    *,
    warmup: bool,
) -> None:
    """Cost + validity estimate printed before the first cell.

    Everything here is knowable *before* paying for the run, and every line of
    it is something that has previously made a completed run worthless: a knob
    the engine never read, a Δ smaller than the golden set's own noise, or an
    ETA the operator would not have accepted.
    """
    n_q, n_c = len(queries), len(cells)
    print("\n--- pre-flight -------------------------------------------------")
    print(f"Cells × queries : {n_c} × {n_q} = {n_c * n_q} retrievals"
          + (f" (+{n_c} discarded warm-ups)" if warmup else ""))
    if warmup_ms:
        total_s = (warmup_ms / 1000.0) * (n_c * n_q + (n_c if warmup else 0))
        print(f"ETA             : ~{total_s / 60.0:.1f} min "
              f"(from a {warmup_ms:.0f} ms warm-up retrieval; the FIRST query of the "
              f"run is far slower — index/model load — and is excluded here)")
    # Metric noise: each query contributes an independent 0/1-ish outcome, so the
    # standard error of a mean over N queries is ~1/(2·sqrt(N)) at worst and is
    # conventionally quoted as ~1/sqrt(N). A Δ below this is not a signal.
    noise = 1.0 / math.sqrt(n_q) if n_q else 0.0
    print(f"Noise floor     : ±{noise:.3f} (~1/sqrt({n_q})) — treat any Δ smaller "
          f"than this as no difference")
    for cell in cells:
        kwargs, cfg = effective_knobs(cell, base_kwargs, base_config)
        pool = "n/a (no reranker)"
        if _reranker_active(kwargs):
            mult = _cfg(cfg, "vault_rerank_pool_multiplier", _POOL_MULTIPLIER_DEFAULT, int)
            floor = _cfg(cfg, "vault_rerank_pool_floor", _POOL_FLOOR_DEFAULT, int)
            ceil_ = _cfg(kwargs, "rerank_pool_ceiling", 50, int)
            pool = str(min(max(_cfg(kwargs, "top_k", 10, int) * mult, floor), ceil_))
        print(f"  · {cell['name']:<16} top_k={kwargs.get('top_k')} "
              f"cutoff={kwargs.get('similarity_cutoff')} "
              f"hybrid={bool(kwargs.get('hybrid_enabled'))} "
              f"rerank={_reranker_active(kwargs)} pool={pool} "
              f"fusion={_uses_fusion(kwargs)}")
        for note in detect_inert_overrides(cell, base_kwargs, base_config):
            print(f"      !! INERT: {note}")
    print("----------------------------------------------------------------")


def main(argv: Optional[list] = None) -> int:
    """Parse args, refuse unsafe preconditions, run the sweep, report.

    Exit codes:
      0 success
      1 aborted by the user / unusable index, golden set or config
      2 refused: a malformed cell definition, a live app, a stale sweep backup,
        or a preset whose prerequisite is missing — i.e. every case where
        running would produce numbers that cannot mean anything.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--golden", default=None,
                        help=f"Golden set JSON (default: {_default_golden()})")
    parser.add_argument("--cells", default=None,
                        help="JSON file with {'cells': [...]} knob combinations to sweep.")
    parser.add_argument("--preset", default=None, choices=list(PRESETS),
                        help="Canned cell list instead of --cells.")
    parser.add_argument("--k", default=",".join(str(k) for k in DEFAULT_KS),
                        help="Comma-separated metric cutoffs (default: 1,5,10).")
    parser.add_argument("--limit", type=int, default=0,
                        help="Score at most N queries (seeded subsample; 0 = all).")
    parser.add_argument("--seed", type=int, default=0, help="Subsample seed (default: 0).")
    parser.add_argument("--out", default=None,
                        help="Results JSON path (default: BASE_DIR/eval/results/…).")
    parser.add_argument("--index-dir", default=OBSIDIAN_INDEX_DIR,
                        help="Index dir to inspect for meta + docstore "
                             f"(default: {OBSIDIAN_INDEX_DIR}).")
    parser.add_argument("--no-warmup", action="store_true",
                        help="Skip the discarded warm-up retrieve per cell "
                             "(cell 1's latency stats then INCLUDE the index/model load).")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the interactive prompts. Does NOT bypass the "
                             "live-app refusal.")
    args = parser.parse_args(argv)

    # Resolve cells FIRST: a typo'd knob (or an out-of-range value) should fail
    # before we prompt, load an index, or touch the config file.
    try:
        cells = resolve_cells(args.preset, args.cells)
    except CellError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"ERROR: could not read --cells: {exc}", file=sys.stderr)
        return 2

    try:
        ks = _parse_ks(args.k)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    # Refusal 1: a previous run died holding the config. Checked before anything
    # else touches config.json — including the load below, which would otherwise
    # read sweep values as if they were the user's settings.
    stale_msg = check_stale_sidecar(CONFIG_FILE)
    if stale_msg:
        print(f"ERROR: {stale_msg}", file=sys.stderr)
        return 2

    # Refusal 2: the app is running. Detected, not asked — and --yes cannot
    # bypass it (see detect_live_server).
    live = detect_live_server()
    if live:
        print(f"ERROR: a ChatEKLD server is LIVE at {live}. Two processes hold two "
              f"independent in-process locks over one index — this run would race it "
              f"on the BM25 sidecar and the index checkpoints, and its numbers would "
              f"be unreproducible. Quit the app (or stop `papermind server`) and "
              f"re-run.", file=sys.stderr)
        return 2

    if not confirm_proceed(args.yes):
        print("Aborted.")
        return 1

    if args.no_warmup:
        print("NOTE: --no-warmup — cell 1's latency stats will INCLUDE the one-off "
              "index / BM25 / reranker load, so its p50/p95 are not comparable with "
              "the later cells'.")

    golden_path = args.golden or _default_golden()
    if not Path(golden_path).exists():
        print(f"ERROR: no golden set at {golden_path}. Generate one first:\n"
              f"  python scripts/gen_golden_retrieval.py", file=sys.stderr)
        return 1
    try:
        header, queries = load_golden(golden_path)
    except (OSError, ValueError, KeyError) as exc:
        print(f"ERROR: could not load {golden_path}: {exc}", file=sys.stderr)
        return 1
    if not queries:
        print(f"ERROR: {golden_path} contains no queries.", file=sys.stderr)
        return 1
    golden_sha256 = hashlib.sha256(Path(golden_path).read_bytes()).hexdigest()

    from core.config import load_config
    cfg = load_config()

    vault_path = str(cfg.get("obsidian_vault_path") or "").strip()
    if not vault_path:
        print("ERROR: no obsidian_vault_path configured — set the vault in the app "
              "first.", file=sys.stderr)
        return 1
    golden_vault = str(header.get("vault_path") or "").strip()
    if golden_vault and norm_source(golden_vault) != norm_source(vault_path):
        print(f"WARNING: golden set was generated for vault {golden_vault!r} but the "
              f"configured vault is {vault_path!r} — the qrels may not match this index.")

    meta = read_index_meta(args.index_dir)
    if not meta:
        print(f"ERROR: no readable {META_JSON} under {args.index_dir}. Index the vault "
              f"in the app (Obsidian tab → Index) before evaluating retrieval.",
              file=sys.stderr)
        return 1
    if not meta.get("has_vector_data"):
        print(f"ERROR: {args.index_dir}/{META_JSON} reports has_vector_data=false — the "
              f"index holds no vectors. Run (or finish) an indexing pass in the app "
              f"before evaluating retrieval.", file=sys.stderr)
        return 1

    golden_embed = str(header.get("embed") or "").strip()
    index_embed = str(meta.get("embed") or "").strip()
    if golden_embed and index_embed and golden_embed != index_embed:
        print(f"WARNING: golden set records embed {golden_embed!r}, index records "
              f"{index_embed!r}.")

    # HARD refusal: the model that will embed every QUERY vs the model the index
    # was built with. This is the check the README promised and the runner never
    # made — it only ever compared golden-vs-index. ``cfg["embed"]`` is what
    # ``baseline_retrieve_kwargs`` passes as ``embed_name``, so a mismatch means
    # every query vector lands in a FOREIGN space: recall collapses to ~0.05
    # while the banner cheerfully prints the index's model, concealing the cause.
    # There is no partial-credit reading of such a run, so it is exit 1, not a
    # warning.
    cfg_embed = str(cfg.get("embed") or "").strip()
    if not cfg_embed:
        print("ERROR: no 'embed' model configured — every query would be embedded by "
              "an unset model. Set the embedding model in the app first.",
              file=sys.stderr)
        return 1
    if index_embed and cfg_embed != index_embed:
        print(f"ERROR: embedding-space mismatch. Queries would be embedded with "
              f"{cfg_embed!r} (config 'embed') while the index was built with "
              f"{index_embed!r} ({META_JSON}). Every query would search a foreign "
              f"vector space and every metric would be garbage (recall ≈ 0.05). "
              f"Set 'embed' back to {index_embed!r}, or reindex the vault with "
              f"{cfg_embed!r} and regenerate the golden set.", file=sys.stderr)
        return 1

    base_kwargs = baseline_retrieve_kwargs(cfg, ks)

    # Refusal 3: reranker-ab with no reranker model configured. Both of its cells
    # would then run rerank-free (rag/vault.py L4985-L4989 gates on
    # ``reranker_enabled AND reranker_model``) — "rerank-off" vs "rerank-off",
    # reported as a reranker A/B. Nothing to measure.
    if args.preset in ("reranker-ab", "rerank-onoff") \
            and not str(base_kwargs.get("reranker_model") or "").strip():
        print(f"ERROR: --preset {args.preset} needs a reranker: 'vault_reranker_model' "
              f"resolves empty, so EVERY cell would run without rerank and the A/B "
              f"would compare two identical configurations. Set the reranker model in "
              f"the LLM Settings window first.", file=sys.stderr)
        return 2

    # Stale qrels: drop (and count) queries whose documents all left the index.
    known = docstore_sources(args.index_dir)
    if not known:
        print("WARNING: could not read the index's source inventory — skipping the "
              "stale-qrel check; every golden query will be scored.")
    queries, stale = filter_stale_queries(queries, known)
    if stale:
        print(f"Excluded {len(stale)} golden quer{'y' if len(stale) == 1 else 'ies'} whose "
              f"relevant documents are no longer in the index.")
    if not queries:
        print("ERROR: every golden query is stale — regenerate the golden set.",
              file=sys.stderr)
        return 1

    if args.limit and args.limit > 0 and args.limit < len(queries):
        random.Random(args.seed).shuffle(queries)
        queries = sorted(queries[:args.limit], key=lambda q: q.id)
        print(f"--limit {args.limit}: scoring a seeded subsample of the golden set.")

    bm25_warning = bm25_sidecar_is_stale(args.index_dir)
    if bm25_warning and any(
        _uses_fusion(effective_knobs(c, base_kwargs, cfg)[0]) for c in cells
    ):
        print(f"\nWARNING: {bm25_warning}")

    # A FRESH manager, not rag.vault.obsidian_manager: this script is standalone
    # and single-threaded, and the app is closed (detected above), so a private
    # instance shares no lock or index state with anything. Unlike
    # tests/eval/run_eval.py we do NOT redirect CHATEKLD_BASE_DIR — the whole
    # point is to evaluate the REAL index at the real BASE_DIR.
    from rag.vault import ObsidianVaultManager
    manager = ObsidianVaultManager()
    manager.restore_vault_path(vault_path)

    print(f"\nGolden set : {golden_path} ({len(queries)} queries, k={ks})")
    print(f"Vault      : {vault_path}")
    print(f"Index      : {args.index_dir} "
          f"(backend={meta.get('vector_backend') or 'simple'}, embed={index_embed or '?'})")
    print(f"Cells      : {', '.join(c['name'] for c in cells)}")

    # One timed retrieval BEFORE the pre-flight so the ETA is measured, not
    # guessed. It also pays the index/BM25/reranker load exactly once, which is
    # what makes the per-cell warm-ups cheap.
    warmup_ms: Optional[float] = None
    print("\nWarming up (index load, BM25 sidecar, reranker) — this is the slow part…")
    try:
        t0 = time.perf_counter()
        manager.retrieve(queries[0].query, **base_kwargs,
                         stage_cb=lambda m: print(f"  · {m}"))
        warmup_ms = (time.perf_counter() - t0) * 1000.0
        print(f"  warm-up retrieval took {warmup_ms / 1000.0:.1f}s")
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: the warm-up retrieval FAILED ({exc}). The sweep will still "
              f"run, but if this is your embed provider being down, every query will "
              f"fail the same way and each cell will abort after "
              f"{MAX_CONSECUTIVE_QUERY_FAILURES} failures.")
    for line in _drain_status(manager):
        print(f"  [manager] {line}")

    _print_preflight(cells, queries, base_kwargs, cfg, warmup_ms, warmup=not args.no_warmup)
    if not args.yes:
        try:
            if input("Proceed with the sweep? [y/N] ").strip().lower() not in {"y", "yes"}:
                print("Aborted.")
                return 1
        except EOFError:
            print("Aborted.")
            return 1

    out_path = args.out or _default_out()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    def _payload(results: List[Dict], *, partial: bool) -> Dict:
        return {
            "schema_version": RESULTS_SCHEMA_VERSION,
            # ``partial`` is the honest marker for a checkpoint: a reader must be
            # able to tell "the sweep stopped at cell 3" from "the sweep had 3
            # cells", because the two produce identical-looking cell lists.
            "partial": partial,
            "run_at": datetime.now(timezone.utc).isoformat(),
            "golden_set": str(golden_path),
            "golden_sha256": golden_sha256,
            "num_queries": len(queries),
            "excluded_stale": len(stale),
            "ks": list(ks),
            "index": {
                "vector_backend": str(meta.get("vector_backend") or "simple"),
                "embed": index_embed,
                "vault_path": vault_path,
            },
            "cells": results,
        }

    def _checkpoint(results: List[Dict]) -> None:
        write_text_atomic(out_path, json.dumps(_payload(results, partial=True),
                                               indent=2, ensure_ascii=False))

    try:
        results = run_cells(
            manager, queries, cells, ks,
            base_kwargs=base_kwargs,
            base_config=cfg,
            config_path=CONFIG_FILE,
            warmup=not args.no_warmup,
            checkpoint=_checkpoint,
        )
    except ConfigUnsafeError as exc:
        # Surfaced as a message, not a traceback: the actionable part is the
        # sidecar path, and run_cells' finally chain has already restored (or
        # loudly reported that it could not).
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except CellError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    write_text_atomic(out_path, json.dumps(_payload(results, partial=False),
                                           indent=2, ensure_ascii=False))

    print()
    print(format_cells_table(results, BASELINE_CELL_NAME))
    print(f"\nResults written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
