"""Accession HTTP endpoints — Phase 3 of the Accession Deck plan.

Three routes over the ``accession/`` core, driven from the "PDFs Without
Zotero" report (no new tab):

* ``GET  /api/accession/rows``     — the cached scan's orphans — and, with
  ``?scope=pending``/``all``, the curated ``pending`` bucket — joined to any
  cached resolutions. **No network**, and no hashing either: a PDF whose sha
  is not already memoised (``hash_cache.json``) simply renders unresolved.
* ``POST /api/accession/identify`` — run the ladder for ONE orphan on the
  bounded SSE worker. Single-flight and transport-cancellable.
* ``POST /api/accession/batch``    — SSE over an explicit selection,
  confirm-gated, capped at ``accession_batch_max``. Resumable by
  construction: results persist in the sha-keyed accession cache, so
  re-running a selection re-pays only what was never resolved.

The egress gates, in order, on every network route: ``accession_enabled``
(default **false**) → not while a scan is running (the audit's manual-only
invariant stays untouched; identification is never a scan side effect) → a
completed scan exists → the path validates and IS one of that scan's
orphans. Linking a result reuses ``POST /api/audit/mapping`` unchanged —
this module adds **no write surface**: it writes only the two regenerable
memos (``accession_cache.json``, and ``hash_cache.json`` for the rel→sha
binding ``/rows`` reads). On ``hash_cache`` this is deliberately a second
writer beside the scan's duplicates phase: the accession routes refuse to
*start* during a scan, and a scan started mid-operation can at worst lose
memo entries to a last-writer race — a re-hash, never data.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from flask import Blueprint, jsonify, request

from api.security import sanitise_error_msg
from api.sse import run_sse_worker
from api.validators import coerce_bool, coerce_enum, coerce_int_in_range
from core.config import load_config
from core.constants import (
    ACCESSION_BATCH_MAX_CEILING,
    ACCESSION_BATCH_MAX_DEFAULT,
    SSE_STALL_MARGIN_S,
)
from core.paths import resolve_under_root

from accession import cache as accession_cache
from accession import sources as acc_sources
from accession.candidates import Resolution, _apply_bib_check, resolve_queries
from accession.identify import identify
from audit.core import doi_cache as core_doi_cache
from audit.core import hash_cache as core_hash_cache
from audit.core import hashing as core_hashing
from audit.core.pdf_identifiers import extract_embedded_doi
from audit.engine.bridge import _load_mapping_all, _rel_to_vault, zotero_unclaimed
from audit.manager import audit_manager

logger = logging.getLogger(__name__)

accession_bp = Blueprint("accession", __name__)

#: Wall-clock bound across every source call for ONE PDF (the per-source
#: timeout is the ``accession_timeout_s`` config key). Measured live: a
#: typical resolution is ~1.4 s; this is the wedged-endpoint ceiling.
ACCESSION_TOTAL_BUDGET_S = 60.0

#: One accession operation at a time (identify OR batch) — the sources share
#: per-host pacing state and the caches have one writer per op. A concurrent
#: request gets 429, the tagging-propose posture.
_ACCESSION_GATE = threading.Lock()

#: Persist the memos every N PDFs during a batch, so a crash/cancel keeps
#: what was already paid for (the refactor JOURNAL_FLUSH_EVERY pattern).
_BATCH_FLUSH_EVERY = 10

#: Abort a batch after this many CONSECUTIVE unexpected per-PDF crashes —
#: source failures degrade inside a resolution (notes), so consecutive
#: crashes mean something environmental, and sweeping the rest of the
#: selection through it would be the tagging batch's wedged-provider bug.
_MAX_CONSECUTIVE_FAILURES = 5


# ── config + context ───────────────────────────────────────────────────────


def _accession_cfg() -> dict[str, Any]:
    """Resolved accession knobs, re-clamped defensively (hand-edited config)."""
    cfg = load_config()
    names = cfg.get("accession_sources")
    if not isinstance(names, list):
        names = list(acc_sources.default_source_names())
    names = [n for n in names if isinstance(n, str)]
    if not names:
        # A hand-edited config can hold [] (the validator only guards writes
        # through /api/config); "resolve against no sources" is never a
        # meaningful state, so degrade to the full registry.
        names = list(acc_sources.default_source_names())
    return {
        "enabled": coerce_bool(cfg.get("accession_enabled")) or False,
        "contact_email": str(cfg.get("accession_contact_email") or "").strip(),
        "sources": names,
        "max_candidates": coerce_int_in_range(
            cfg.get("accession_max_candidates"), 1, 10) or 5,
        "timeout_s": coerce_int_in_range(
            cfg.get("accession_timeout_s"), 5, 60) or 15,
        "batch_max": coerce_int_in_range(
            cfg.get("accession_batch_max"), 1, ACCESSION_BATCH_MAX_CEILING)
        or ACCESSION_BATCH_MAX_DEFAULT,
    }


@dataclass
class _OpContext:
    """Everything one identify/batch operation shares across its PDFs."""

    settings: Any
    bib_index: dict[str, list[str]]
    sources: list[acc_sources.Source]
    max_candidates: int
    timeout_s: float
    hash_memo: dict[str, Any] = field(default_factory=dict)
    doi_memo: dict[str, Any] = field(default_factory=dict)
    acc_cache: dict[str, Any] = field(default_factory=dict)
    text_provider: Callable[[Path], str | None] | None = None


def _bib_doi_index(inv) -> dict[str, list[str]]:
    """``{normalised doi: [citation_key, …]}`` from the CACHED scan's records.

    Built from the same ``BibEntry.doi`` values the bridge joins on, so the
    offline Link fast path and the bridge agree about what "already in the
    bib" means for this snapshot.
    """
    index: dict[str, list[str]] = {}
    for rec in inv.records.values():
        entry = rec.bib_entry
        if entry is not None and entry.doi:
            index.setdefault(entry.doi, []).append(rec.citation_key)
    return index


def _excluded_keys_by_rel(settings) -> dict[str, list[str]]:
    """``{vault-rel: [citation_key, …]}`` from current ``mapping.json`` exclude.

    Read-only injection for accession rows (the vendored ``audit/`` tree must
    not import this module). Additive on the wire — identify may still
    *propose* an excluded key; the mapping gate refuses the rematch unless
    ``accept_excluded``.
    """
    try:
        mapping = _load_mapping_all(settings.mapping_file)
    except Exception:  # noqa: BLE001 — degrade to no exclusions
        return {}
    return {rel: list(keys) for rel, keys in (mapping.exclude or {}).items()}


def _build_context(inv, settings, knobs: dict[str, Any]) -> _OpContext:
    from api.routes.audit import _build_pdf_text_provider

    by_name = {s.name: s for s in acc_sources.default_sources(
        knobs["contact_email"])}
    return _OpContext(
        settings=settings,
        bib_index=_bib_doi_index(inv),
        sources=[by_name[n] for n in knobs["sources"] if n in by_name],
        max_candidates=knobs["max_candidates"],
        timeout_s=float(knobs["timeout_s"]),
        hash_memo=core_hash_cache.load_cache(),
        doi_memo=core_doi_cache.load_cache(),
        acc_cache=accession_cache.load_cache(),
        text_provider=_build_pdf_text_provider(),
    )


def _save_memos(ctx: _OpContext) -> None:
    accession_cache.save_cache(ctx.acc_cache)
    core_hash_cache.save_cache(ctx.hash_memo)


# ── the per-PDF pipeline ───────────────────────────────────────────────────


def _embedded_doi(pdf_path: Path, doi_memo: dict[str, Any]) -> str | None:
    """Embedded-metadata DOI: the scan's memo first, a fresh read on a miss.

    The miss result is deliberately NOT stored — ``doi_cache.json`` has one
    writer (the scan). A fresh pikepdf read is ~50 ms and the memo is warm
    for essentially every scanned file, so the un-memoised read is rare.
    """
    hit, doi = core_doi_cache.lookup(pdf_path, doi_memo, "meta")
    if hit:
        return doi
    return extract_embedded_doi(pdf_path)


def _identify_one(pdf_path: Path, rel: str, ctx: _OpContext,
                  *, force: bool = False,
                  progress_cb: Callable[[str], None] | None = None,
                  should_cancel: Callable[[], bool] | None = None,
                  ) -> dict[str, Any]:
    """Run the full ladder for one orphan; returns the /rows row shape.

    ``force`` bypasses the cache consult (never the store) — the explicit
    per-row re-identify, and since 2026-09-06 the batch's ``force`` too. An
    unforced batch never forces: its cache hits ARE its
    resume semantics. ``progress_cb`` narrates each source call into the SSE
    stream — the pacing sleeps otherwise read as dead air — and is passed by
    BOTH streaming routes since single-item identify became SSE (AP-05).
    ``should_cancel`` is polled at each source-call boundary, so an abandoned
    stream stops the ladder instead of only being noticed once it has spent its
    whole budget.
    """
    sha = core_hashing.cached_full_hash(pdf_path, ctx.hash_memo)
    text = None
    if ctx.text_provider is not None:
        text = ctx.text_provider(pdf_path)
    queries = identify(_embedded_doi(pdf_path, ctx.doi_memo), text,
                       pdf_path.stem)
    res = resolve_queries(
        queries,
        bib_doi_index=ctx.bib_index,
        sources=ctx.sources,
        total_budget_s=ACCESSION_TOTAL_BUDGET_S,
        rows=ctx.max_candidates,
        per_call_timeout_s=ctx.timeout_s,
        cache=ctx.acc_cache,
        sha256=sha,
        force_refresh=force,
        progress_cb=progress_cb,
        should_cancel=should_cancel,
    )
    payload = res.to_jsonable()
    payload["from_cache"] = res.from_cache
    excluded = _excluded_keys_by_rel(ctx.settings)
    return {"pdf": {"rel": rel, "name": pdf_path.name},
            "sha256": sha, "resolution": payload,
            "excluded_keys": list(excluded.get(rel, []))}


# ── shared gates ───────────────────────────────────────────────────────────


def _gates(*, network: bool):
    """Common preconditions → ``(inv, settings, knobs)`` or a Flask error.

    ``network=True`` (identify / batch) adds the egress gates in message
    order: a disabled feature answers 403 before anything else is revealed,
    and a running scan answers 409 — identification is never a scan side
    effect. ``/rows`` skips both: it is cache-only and must stay readable
    whenever the report tabs are (a scan serves cached reports too). Both
    modes require a completed scan (404, the report routes' own wording).
    """
    knobs = _accession_cfg()
    if network:
        if not knobs["enabled"]:
            return None, (jsonify({
                "error": "Online identification is disabled. Enable "
                         "'Identify sources online' on the PDFs Without "
                         "Zotero report first (accession_enabled)."}), 403)
        if audit_manager.is_scanning():
            return None, (jsonify({
                "error": "A scan is running — identification never runs "
                         "during a scan. Wait for it to finish."}), 409)
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return None, (jsonify(
            {"error": "No completed scan cached. Run a scan first."}), 404)
    return (inv, settings, knobs), None


def _resolve_orphan(inv, settings, raw_rel) -> tuple[Path, str] | None:
    """Validate *raw_rel* and require it to be one of THIS scan's orphans
    or pending PDFs.

    Path trust is re-derived from scratch (the ``/api/audit/pdf`` posture):
    shape check → ``resolve_under_root`` → realpath containment — then
    membership in the cached bridge's unmapped set, because identifying a
    mapped PDF is meaningless and a typo'd rel must fail loudly, never
    resolve some other file.
    """
    from api.routes.audit import _normalise_rel_path

    rel = _normalise_rel_path(raw_rel)
    if not rel:
        return None
    resolved = resolve_under_root(rel, settings.vault_root)
    if not resolved:
        return None
    candidate = settings.vault_root / resolved
    try:
        real = Path(os.path.realpath(candidate))
        real_root = Path(os.path.realpath(settings.vault_root))
        if real_root not in real.parents:
            return None
        if not real.is_file() or real.suffix.lower() != ".pdf":
            return None
    except OSError:
        return None
    # Orphans AND the curated "pending" bucket (2026-09-06): a PDF the user
    # marked "add to Zotero later" has by definition no bib entry yet, and
    # identifying it is exactly what the Not in Zotero tab needs — its
    # resolution feeds the DOI hand-off to Zotero. `no_match` ("not a paper
    # for Zotero") stays excluded: identifying it is meaningless.
    identifiable = {_rel_to_vault(p, settings.vault_root): p
                    for p in inv.bridge.unmapped_pdfs}
    for p in (getattr(inv.bridge, "pending_pdfs", None) or []):
        identifiable.setdefault(_rel_to_vault(p, settings.vault_root), p)
    canonical = _rel_to_vault(candidate, settings.vault_root)
    orphan = identifiable.get(canonical)
    if orphan is None:
        return None
    if _zotero_unclaimed(inv, orphan):
        return None
    return orphan, canonical


def _zotero_unclaimed(inv, path) -> bool:
    """True if Zotero links *path* but the pairing rung could not claim it.

    Unexported citation keys and files two parents link stay on
    ``unmapped_pdfs`` (the rung does not claim them). Accession must not
    treat them as orphans to identify or create: they already have a
    Zotero attachment.

    Delegates to ``bridge.zotero_unclaimed`` so this route, *PDFs Without
    Zotero*, its *↳ Unread Only* subset and *Not in Zotero* cannot drift apart
    (2026-09-20) — a copy that aged would hide the row on one surface and
    offer a create on another.
    """
    return zotero_unclaimed(inv.bridge, path)


# ── routes ─────────────────────────────────────────────────────────────────


#: ``?scope=`` on the rows read. ``orphans`` is the historical set and stays
#: the default (the ``tagging inventory`` precedent: widening what a GET
#: returns by default changes every existing client's table without asking).
_ROW_SCOPES = ("orphans", "pending", "all")


@accession_bp.route("/api/accession/rows")
def api_accession_rows():
    """Orphans — and, with ``?scope=``, the curated ``pending`` bucket —
    joined to cached resolutions. Read-only: no network, no hashing (an
    un-memoised sha renders unresolved rather than costing a 579-file
    hashing pass on a GET), and cached entries are revalidated against the
    CURRENT snapshot's bib index so a since-catalogued paper shows as a Link
    without re-identification.

    **Why ``pending`` is reachable here (2026-09-17).** ``_resolve_orphan``
    has admitted a curated ``pending`` PDF to ``identify``/``batch`` since
    2026-09-06, and the Not in Zotero tab identifies those rows — but this
    read still listed only ``unmapped_pdfs``, so a resolution the app had
    already paid for was unreachable except by re-running the ladder. On the
    live library that is the whole of Phase 2's population: 0 orphans and 296
    ``pending``. The default stays ``orphans`` so the accession panel's table
    is unchanged; the annual cleanup's proposer asks for ``pending``.
    """
    got, err = _gates(network=False)
    if err:
        return err
    inv, settings, knobs = got
    # An ABSENT scope defaults; a PRESENT but unknown one is a 400, never a
    # silent fallback. Falling back would hand a caller that asked for
    # `pending` the orphan set — 0 rows on this library — which reads as
    # "nothing to propose" rather than as a typo. Same empty-vs-unset rule
    # the tagging `--targets` hole was closed by.
    raw_scope = request.args.get("scope")
    scope = "orphans" if raw_scope is None else coerce_enum(raw_scope, _ROW_SCOPES)
    if scope is None:
        return jsonify({"error": f"scope must be one of {', '.join(_ROW_SCOPES)}.",
                        "scopes": list(_ROW_SCOPES)}), 400
    hash_memo = core_hash_cache.load_cache()
    acc_cache = accession_cache.load_cache()
    bib_index = _bib_doi_index(inv)
    excluded = _excluded_keys_by_rel(settings)
    from accession import review
    reviews = review.snapshot(settings.vault_root)

    # Kept as (path, state) so a row can say WHICH bucket it came from: a
    # pending PDF is a decision the user already recorded ("add to Zotero
    # later"), an orphan is one nobody has looked at, and a client that
    # cannot tell them apart would offer the wrong verb.
    wanted: list[tuple[Any, str]] = []
    seen: set[str] = set()
    if scope in ("orphans", "all"):
        for p in inv.bridge.unmapped_pdfs:
            if _zotero_unclaimed(inv, p):
                continue
            if _rel_to_vault(p, settings.vault_root) not in seen:
                seen.add(_rel_to_vault(p, settings.vault_root))
                wanted.append((p, "orphan"))
    if scope in ("pending", "all"):
        for p in (getattr(inv.bridge, "pending_pdfs", None) or []):
            if _zotero_unclaimed(inv, p):
                continue
            rel = _rel_to_vault(p, settings.vault_root)
            if rel not in seen:
                seen.add(rel)
                wanted.append((p, "pending"))

    rows = []
    resolved_count = 0
    for p, state in wanted:
        rel = _rel_to_vault(p, settings.vault_root)
        sha = core_hashing._memo_hit(p, hash_memo)
        payload = None
        if sha:
            stored = accession_cache.lookup(acc_cache, sha)
            if stored is not None:
                res = Resolution.from_jsonable(stored)
                if res is not None:
                    res = _apply_bib_check(res, bib_index)
                    payload = res.to_jsonable()
                    payload["from_cache"] = True
                    # Cache AGE, for the UI's "cached · N d ago" chip — the
                    # stamp existed for eviction and was never surfaced.
                    payload["queried_at"] = accession_cache.lookup_queried_at(
                        acc_cache, sha)
                    resolved_count += 1
        record = reviews.get(rel)
        if record:
            if review.public(record, p)['identity_state'] != 'verified':
                payload = None
            else:
                effective, _ = review.effective_resolution(settings.vault_root, rel,
                    {**record['fingerprint'], 'sha256':record['sha256']}, payload)
                payload = effective
        rows.append({"pdf": {"rel": rel, "name": p.name}, "state": state,
                     "sha256": sha, "resolution": payload,
                     "excluded_keys": list(excluded.get(rel, []))})
    return jsonify({
        "enabled": knobs["enabled"],
        "batch_max": knobs["batch_max"],
        "scope": scope,
        "orphan_count": sum(1 for _, s in wanted if s == "orphan"),
        "pending_count": sum(1 for _, s in wanted if s == "pending"),
        # The CONFIGURED source subset + whether a contact email rides the
        # Crossref User-Agent — the panel's egress disclosure is built from
        # these instead of a hard-coded five-source string that could
        # overstate (narrowed sources) or understate (email) what leaves.
        "sources": knobs["sources"],
        "contact_email_set": bool(knobs["contact_email"]),
        "rows": rows,
        "resolved_count": resolved_count,
    })


@accession_bp.route("/api/accession/identify", methods=["POST"])
def api_accession_identify():
    """Stream the ladder result for one orphan without running it on WSGI.

    State/resource invariant: ``_ACCESSION_GATE`` still admits exactly one
    single or batch operation, so the shared pacing state and cache dictionaries
    retain one writer. ``run_sse_worker`` owns one bounded daemon worker and a
    bounded queue; the Waitress request thread only drains frames and never
    executes the remote ladder. A disconnected client sets ``cancel`` and the
    worker stops before publishing if possible; an already-active HTTP call is
    allowed to finish inside ``ACCESSION_TOTAL_BUDGET_S``, after which memos are
    saved and the gate is released exactly once by the SSE guard. No persisted
    format changes, and partial failure cannot expose half-written user data
    because these are regenerable caches only.
    """
    data = request.get_json(silent=True) or {}
    got, err = _gates(network=True)
    if err:
        return err
    inv, settings, knobs = got
    hit = _resolve_orphan(inv, settings, data.get("pdf"))
    if hit is None:
        return jsonify({"error": "Not an orphan PDF of the cached scan."}), 400
    pdf_path, rel = hit

    if not _ACCESSION_GATE.acquire(blocking=False):
        return jsonify({"error": "An identification is already running."}), 429
    try:
        ctx = _build_context(inv, settings, knobs)
    except Exception as exc:  # noqa: BLE001 — gate ownership has not transferred
        _ACCESSION_GATE.release()
        logger.exception("accession identify context build failed for %s", rel)
        return jsonify({"error": sanitise_error_msg(str(exc))}), 502

    force = coerce_bool(data.get("force")) or False

    def worker(put, cancel):
        try:
            put({"info": f"Identifying {pdf_path.name}…"})
            if cancel.is_set():
                return

            def _narrate(source: str) -> None:
                put({"info": f"Identifying {pdf_path.name}: querying {source}…"})

            row = _identify_one(
                pdf_path, rel, ctx, force=force, progress_cb=_narrate,
                should_cancel=cancel.is_set,
            )
            _save_memos(ctx)
            if not cancel.is_set():
                put({"row": row})
        except Exception as exc:  # noqa: BLE001 — sanitised SSE terminal frame
            logger.exception("accession identify failed for %s", rel)
            put({"error": sanitise_error_msg(str(exc))})

    return run_sse_worker(
        worker,
        consumer_timeout_s=int(ACCESSION_TOTAL_BUDGET_S) + SSE_STALL_MARGIN_S,
        release=_ACCESSION_GATE.release,
    )


@accession_bp.route("/api/accession/batch", methods=["POST"])
def api_accession_batch():
    """SSE identification over an explicit selection (confirm-gated).

    One ``{"row"}`` frame per PDF as it resolves (the UI updates
    incrementally; each frame also feeds the SSE stall guard, so the
    consumer timeout only has to cover ONE PDF's budget), then a terminal
    ``{"accession"}`` summary. Cancellation is between PDFs — one PDF's
    in-flight source calls are bounded by ``ACCESSION_TOTAL_BUDGET_S``.
    """
    data = request.get_json(silent=True) or {}
    if data.get("confirm") is not True:
        return jsonify({"error": "Missing confirm"}), 400
    got, err = _gates(network=True)
    if err:
        return err
    inv, settings, knobs = got

    raw = data.get("pdfs")
    if not isinstance(raw, list) or not raw:
        return jsonify({"error": "pdfs must be a non-empty list"}), 400
    # `force` (2026-09-06): bulk RE-identification. Bypasses the cache consult
    # (never the store) for every PDF in the selection — the per-row verb's
    # semantics, batched. Off by default so a plain batch still resumes for
    # free from its own cache hits.
    force = coerce_bool(data.get("force")) is True
    if len(raw) > knobs["batch_max"]:
        return jsonify({"error": f"Selection exceeds accession_batch_max "
                                 f"({knobs['batch_max']})."}), 400
    targets: list[tuple[Path, str]] = []
    for raw_rel in raw:
        hit = _resolve_orphan(inv, settings, raw_rel)
        if hit is None:
            # A typo'd rel must fail the request, not silently thin the batch
            # (the --keys lesson: a dropped selector reads as "already done").
            return jsonify({"error": f"Not an orphan or pending PDF of the "
                                     f"cached scan: {raw_rel!r}"}), 400
        targets.append(hit)

    if not _ACCESSION_GATE.acquire(blocking=False):
        return jsonify({"error": "An identification is already running."}), 429

    try:
        ctx = _build_context(inv, settings, knobs)
    except Exception as exc:  # noqa: BLE001 — the gate must not leak (identify parity)
        # Between acquire and run_sse_worker the SSE guard does not own the
        # release yet; an escaping exception here would 429 every later
        # accession op until restart.
        _ACCESSION_GATE.release()
        logger.exception("accession batch context build failed")
        return jsonify({"error": sanitise_error_msg(str(exc))}), 502

    def worker(put, cancel):
        done = 0
        outcomes: dict[str, int] = {}
        consecutive_failures = 0
        cancelled = False
        try:
            put({"info": f"Identifying {len(targets)} PDF(s)…"})
            for i, (pdf_path, rel) in enumerate(targets):
                if cancel.is_set():
                    cancelled = True
                    break
                if audit_manager.is_scanning():
                    # The gates refuse to START during a scan; a scan started
                    # MID-batch must stop it too, or "nothing runs during a
                    # scan" (the documented egress condition) only holds at
                    # t=0. Checked between PDFs — the one in flight finishes
                    # inside its own budget. Already-paid results are cached,
                    # so re-running the batch afterwards resumes for free.
                    put({"info": "A scan started — identification stops so "
                                 "nothing runs during a scan. Re-run the "
                                 "batch when it finishes."})
                    cancelled = True
                    break
                terminal_error = None
                try:
                    def _narrate(src: str, n: int = i + 1,
                                 name: str = pdf_path.name) -> None:
                        put({"info": f"Identifying {n}/{len(targets)} "
                                     f"({name}): querying {src}…"})
                    row = _identify_one(pdf_path, rel, ctx, force=force,
                                        progress_cb=_narrate,
                                        should_cancel=cancel.is_set)
                    consecutive_failures = 0
                except Exception as exc:  # noqa: BLE001 — one PDF, not the batch
                    logger.exception("accession batch failed for %s", rel)
                    consecutive_failures += 1
                    row = {"pdf": {"rel": rel, "name": pdf_path.name},
                           "sha256": None, "resolution": None,
                           "error": sanitise_error_msg(str(exc))}
                    if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                        terminal_error = (f"Aborting after "
                                          f"{consecutive_failures} consecutive "
                                          f"failures — check the log.")
                done += 1
                outcome = ((row.get("resolution") or {}).get("outcome")
                           or "error")
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
                put({"row": row})
                if terminal_error:
                    # Error frames are terminal in the shared SSE contract, so
                    # the partial tally must ride this SAME frame. Emitting a
                    # later summary would be dead protocol: compliant clients
                    # stop consuming immediately after {error}. Counting and
                    # publishing the failed row first keeps the visible rows,
                    # terminal totals and persisted regenerable memo state in
                    # agreement after a partial failure.
                    put({"error": terminal_error, "accession": {
                        "total": len(targets), "done": done,
                        "outcomes": outcomes, "cancelled": False,
                        "failed_early": True,
                    }})
                    return
                if (i + 1) % _BATCH_FLUSH_EVERY == 0:
                    _save_memos(ctx)
            put({"accession": {
                "total": len(targets), "done": done,
                "outcomes": outcomes, "cancelled": cancelled,
            }})
        finally:
            _save_memos(ctx)

    return run_sse_worker(
        worker,
        consumer_timeout_s=int(ACCESSION_TOTAL_BUDGET_S) + SSE_STALL_MARGIN_S,
        release=_ACCESSION_GATE.release,
    )
