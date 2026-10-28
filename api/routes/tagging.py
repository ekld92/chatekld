"""Biblio Tagging HTTP endpoints (``/api/tagging/*``).

A write-extension of the Library Audit, scoped strictly to ``biblio_articles/``:
it projects the audit's cached per-citation-key ``Record`` join into a table of
current tags across the three targets (PDF Finder tags, Obsidian note tags,
Zotero parent tags) and — in later slices — proposes and writes tags to them.

Slice 1 is **read-only**: ``GET /api/tagging/vocab`` (the controlled ``_tags.md``
vocabulary for the manual picker + LLM legend) and ``GET /api/tagging/inventory``
(the side-by-side current-tags table). Every route is auto-gated by the global
``X-Requested-With`` origin hook (``api.security.register_origin_guard``); no
endpoint here mutates anything or triggers an audit scan.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request, send_file

from api.routes.open import run_open
from api.security import sanitise_error_msg
from api.sse import run_sse_worker
from core.constants import (SSE_SINGLE_SHOT_FLOOR_S, SSE_STALL_MARGIN_S,
                            TAGGING_TARGETS)
from api.validators import (
    coerce_bool,
    coerce_enum,
    coerce_float_in_range,
    coerce_int_in_range,
    coerce_non_empty_string,
    coerce_string_max_len,
)
from audit.config import AuditConfigError, load_settings
from audit.manager import audit_manager
# _DEFAULTS is imported so the route's coerce-fallbacks quote the ONE source of
# truth: the previous inline `or 300` silently contradicted a raised config
# default (the 2026-07-18 propose-truncation bug's third leg) — a garbage
# persisted value fell back to a number the config layer no longer used.
from core.config import _DEFAULTS as _CFG_DEFAULTS, load_config, resolve_chat_model
from tagging import candidates as tag_candidates
from accession import imports as imports_tracker
from tagging import journal, metadata_source, propose as tag_propose, session_llm, text_source, zotero_write
from widget.textclean import clean_display
# The domain writers were extracted to tagging/writers.py (2026-07-19
# controller-bloat fix); the route keeps HTTP parsing, lock acquisition and
# thin delegation. The private names are RE-EXPORTED so the historical
# ``api.routes.tagging._x`` patch targets in tests keep working — endpoints
# below reference these module globals, so patching here still intercepts.
from tagging.writers import (  # noqa: F401 — re-exports (see above)
    _canonicalise_tags,
    _finder_remove_locked,
    _finder_write_locked,
    _is_under,
    _note_remove_locked,
    _note_write_locked,
    _prepare_tag_removal,
    _prepare_tag_write,
    _read_vocab,
    _revert_finder_tag,
    _revert_zotero_commit_locked,
    _revert_zotero_prepare,
    _revert_zotero_attachment,
    _revert_zotero_attachment_commit,
    _revert_zotero_attachment_create,
    _revert_zotero_attachment_create_commit,
    _revert_zotero_attachment_create_prepare,
    _revert_zotero_item_create,
    _revert_zotero_item_create_commit,
    _revert_zotero_item_create_prepare,
    _revert_zotero_attachment_repath,
    _revert_zotero_attachment_repath_commit,
    _revert_zotero_attachment_repath_prepare,
    _revert_zotero_attachment_retitle,
    _revert_zotero_attachment_retitle_commit,
    _revert_zotero_attachment_retitle_prepare,
    _revert_zotero_metadata,
    _revert_zotero_metadata_commit,
    _revert_zotero_metadata_prepare,
    _revert_zotero_child_reparent_commit,
    _revert_zotero_child_reparent_prepare,
    _revert_zotero_item_trash_commit,
    _revert_zotero_item_trash_prepare,
    _revert_zotero_attachment_prepare,
    _revert_zotero_tag,
    _tag_key,
    _target_result,
    _zotero_base,
    _zotero_commit_locked,
    _zotero_journal,
    _zotero_patch,
    _zotero_prepare,
    _zotero_remove_commit_locked,
    _zotero_remove_journal,
    _zotero_remove_patch,
    _zotero_remove_prepare,
)

tagging_bp = Blueprint("tagging", __name__)

#: Note sync's Zotero-side op kinds (notesync/zotero_notes.py, 2026-09-04).
#: Journalled HERE so the shared Restore panel reverts them; dispatched by
#: name so this module never imports the notesync package at load time.
_NOTESYNC_ZOTERO_KINDS = ("zotero_note_write", "zotero_note_create")
logger = logging.getLogger(__name__)

# Single-flight gate for the (unbounded-on-local) propose call: at most one
# proposal runs at a time so a wedged local model can't stack daemon threads.
# A busy request 429s; a timed-out request frees its Waitress thread while the
# daemon keeps the gate until the provider call returns (accepted trade-off,
# mirrors refactor's _run_llm_action_bounded). Cancellation is cooperative —
# polled per streamed token and between embed stages — so a local call wedged
# BEFORE its first token cannot be cancelled: after a 504 the gate may stay
# held for up to the provider's own timeout, and an immediate retry 429s.
# That residual is why the 429 message says "try again shortly".
_PROPOSE_GATE = threading.BoundedSemaphore(1)
# Wall-clock bound on one proposal. Sized to the worst legitimate chain — the
# embed phase (≤ ~90 s: three 30 s-bounded calls) plus a slow local model's
# first tokens — while staying below anything a user would sit through. The
# client's fetch abort (tagging.js _PROPOSE_ABORT_MS) sits just ABOVE this so
# the server timeout, not a network abort, is what the user sees.
_PROPOSE_DEADLINE_S = 120.0



def _vault_root() -> Path:
    """Resolved vault root via the audit settings (raises AuditConfigError)."""
    return load_settings().vault_root


@tagging_bp.route("/api/tagging/vocab")
def api_tagging_vocab():
    """Return the controlled tag vocabulary parsed from the vault's ``_tags.md``.

    ``{"vocab": [{"tag", "gloss"}], "count", "source"}``. 404 when the file is
    not configured/present (the feature needs the curated vocabulary). The path
    is resolved under the vault root with the same traversal guard the thesaurus
    uses — defence in depth over the ``/api/config`` shape check.
    """
    try:
        cfg = load_config()
        vault_root = _vault_root()
    except AuditConfigError as exc:
        return jsonify({"error": str(exc)}), 400

    vocab, rel = _read_vocab(cfg, vault_root)
    if vocab is None:
        return jsonify({"error": "Tags file (_tags.md) not found or invalid."}), 404
    rows = [{"tag": tag, "gloss": gloss} for tag, gloss in vocab.items()]
    return jsonify({"vocab": rows, "count": len(rows), "source": rel})


def _project_record(rec: Any, vault_root: Path, biblio_dir: Path,
                    include_pdfless: bool = False) -> dict[str, Any] | None:
    """Flatten one audit ``Record`` into a tagging-table row, or None if out of scope.

    Emits the CURRENT tags for each of the three write targets side by side so
    the UI can show what a later Apply would union into:

    * ``finder_tags`` — union across the record's biblio PDFs (from the scan)
    * ``note``        — the Obsidian Zotero note's YAML tags (+ its rel path)
    * ``zotero``      — the parent item key, its parent tags, and child-note tags

    **Scope (widened 2026-08-11).** "In scope" used to mean "has at least one
    resolved PDF under ``biblio_articles/``" — described here as the feature's
    fixed boundary. It was not a boundary of the *write* path: ``_prepare_tag_write``
    never required a PDF, and ``apply-all`` already skipped an absent Finder
    target with a reason, so note and Zotero writes worked fine for a PDF-less
    entry. It was only this projection that dropped them, which made **2 081**
    perfectly writable entries invisible to the table, the triage deck and any
    inventory-driven batch.

    With ``include_pdfless`` those records are emitted with ``pdf_paths: []``
    and empty ``finder_tags``. Every row now carries **``has_pdf``** so a client
    can degrade the Finder column and its verbs rather than render an empty cell
    that looks like "no tags yet". The flag defaults to False so the historical
    payload is byte-identical: turning it on multiplies the row count several
    fold, which is a decision for the caller, not a surprise.
    """
    biblio_pdfs = [p for p in rec.pdf_paths if _is_under(Path(p), biblio_dir)]
    if not biblio_pdfs and not include_pdfless:
        return None

    bib = rec.bib_entry
    note = rec.obsidian_note
    zot = rec.zotero_item

    def _rel(p: Path) -> str:
        try:
            return str(Path(p).relative_to(vault_root))
        except ValueError:
            return str(p)

    return {
        "citation_key": rec.citation_key,
        # Display text: bib fields are LaTeX source (`schizophr\\'enie`,
        # `{{Swedish}}`) and no widget renders TeX (2026-09-19 UI review).
        "title": clean_display(bib.title) if bib and bib.title else None,
        "year": bib.year if bib else None,
        "first_author": (clean_display(bib.authors[0]) if bib and bib.authors else None),
        "has_pdf": bool(biblio_pdfs),
        "pdf_paths": [
            {"abs": str(p), "rel": _rel(p), "name": Path(p).name} for p in biblio_pdfs
        ],
        "finder_tags": sorted(rec.finder_tag_set) if biblio_pdfs else [],
        "note": {
            "present": note is not None,
            "rel": _rel(note.path) if note is not None else None,
            "tags": sorted(rec.obs_tags),
        },
        "zotero": {
            "present": zot is not None,
            "item_key": zot.item_key if zot is not None else None,
            "parent_tags": sorted(zot.tags) if zot is not None else [],
            "child_note_tags": sorted(rec.zotero_note_tags),
        },
        "bib_keywords": sorted(rec.bib_keywords),
    }


@tagging_bp.route("/api/tagging/inventory")
def api_tagging_inventory():
    """Project the last audit scan into the biblio-scoped tagging table.

    404 with a "Run a scan first" message when no completed audit scan is cached
    — the tagging view reuses the audit's ``Record`` join rather than scanning
    on its own. Ordered by year then citation key (matching the audit inventory
    table).

    ``?scope=`` selects which records are returned:

    * ``with_pdf`` (default) — only records with a PDF under ``biblio_articles/``.
      The historical behaviour, byte-identical.
    * ``all`` — also records with **no** local PDF (``has_pdf: false``,
      ``pdf_paths: []``, ``finder_tags: []``). Those entries were always
      writable (note + Zotero); only this projection hid them. 2 081 of them
      exist in the maintainer's library, so this is opt-in rather than the
      default: it multiplies the row count several fold and a client that
      renders every row would change character without asking.

    ``pdfless_count`` reports how many rows are PDF-less, so a caller can see
    what ``scope=all`` added (and, under the default, is told nothing is hidden
    because the count is 0).

    ``scanned_at`` (additive, 2026-08-12) is the epoch seconds of the scan this
    projection came from, or 0 when unknown. Every tag field here is read from
    the CACHED ``Record`` — no tagging write updates it — so a client that
    resumes work off this payload needs to know how stale it is. Since
    2026-08-18 it is the served ``Inventory``'s own ``built_at`` (one atomic
    read; the manager's ``get_scan_times`` lied mid-scan and after an errored
    rescan — see the field's comment in ``audit/engine/inventory.py``); never
    ``get_status_payload``, which drains the one-shot status feed.
    """
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify({"error": "No scan results. Run an audit scan first."}), 404

    # An unknown value falls back to the historical scope rather than 400ing —
    # this is a read-only projection, not a write destination.
    scope = coerce_enum(request.args.get("scope"), ("with_pdf", "all")) or "with_pdf"
    include_pdfless = scope == "all"

    vault_root = settings.vault_root
    biblio_dir = settings.biblio_articles_dir
    rows: list[dict[str, Any]] = []
    for rec in inv.records.values():
        row = _project_record(rec, vault_root, biblio_dir, include_pdfless)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda r: (r["year"] or "", r["citation_key"]))
    _built = getattr(inv, "built_at", 0.0)
    return jsonify({
        "rows": rows,
        "count": len(rows),
        "scope": scope,
        "pdfless_count": sum(1 for r in rows if not r["has_pdf"]),
        "scanned_at": _built if isinstance(_built, (int, float)) else 0.0,
    })


# ---------------------------------------------------------------------------
# Propose (slice 3): LLM tag suggestions over the cached PDF text (read-only)
# ---------------------------------------------------------------------------

def _run_propose_bounded(fn):
    """Run *fn(should_cancel)* on a daemon thread with a wall-clock bound.

    Returns ``(result, timed_out, busy)``. ``busy`` when another proposal is in
    flight (single-flight — 429). On timeout the request thread is freed
    (``timed_out=True``) and the daemon keeps running (and holding the gate)
    until the provider call returns; ``fn`` polls ``should_cancel`` so it aborts
    at the next token. Exceptions raised by ``fn`` re-raise on the request thread.
    """
    if not _PROPOSE_GATE.acquire(blocking=False):
        return None, False, True
    cancel = threading.Event()
    box: dict = {}

    def _worker():
        try:
            box["result"] = fn(cancel.is_set)
        except BaseException as exc:  # noqa: BLE001 — ferried to the request thread
            box["exc"] = exc
        finally:
            _PROPOSE_GATE.release()

    t = threading.Thread(target=_worker, name="tagging-propose", daemon=True)
    try:
        t.start()
    except RuntimeError:
        # Thread-limit exhaustion. Without this release the gate would be held
        # by a worker that never existed (the release lives in the worker's
        # `finally`) and every later propose would 429 until app restart.
        _PROPOSE_GATE.release()
        raise
    t.join(_PROPOSE_DEADLINE_S)
    if t.is_alive():
        cancel.set()
        return None, True, False
    if "exc" in box:
        raise box["exc"]
    return box.get("result"), False, False


@tagging_bp.route("/api/tagging/propose", methods=["POST"])
def api_tagging_propose():
    """Propose vocabulary tags for one citation key from its cached PDF text.

    Body: ``{citation_key, session?, unit?}``. Writes no library content;
    a session records proposal cache and cost in app state. The UI shows
    the suggestions in the picker and the user applies via ``apply-note-tags``
    (which re-validates against the vocabulary). Resolves the key's first
    ``biblio_articles/`` PDF, reads its text cache-first (bounded fresh extract
    fallback), and runs one bounded, single-flight LLM classification. Returns
    ``{citation_key, proposed:[{tag, confidence}], status, truncated}`` where
    ``status`` ∈ ``ok | low_confidence | empty | no_text``. 429 if another
    proposal is running, 504 on timeout, 502 on an LLM/provider error.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400
    citation_key = coerce_non_empty_string(data.get("citation_key"), max_len=128)
    if not citation_key:
        return jsonify({"error": "citation_key required"}), 400

    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify({"error": "No scan results. Run an audit scan first."}), 404
    rec = inv.records.get(citation_key)
    if rec is None:
        return jsonify({"error": "Unknown citation key."}), 404

    vault_root = settings.vault_root
    biblio_dir = settings.biblio_articles_dir
    biblio_pdfs = [p for p in rec.pdf_paths if _is_under(Path(p), biblio_dir)]
    unit_ctx = {"settings": settings, "vault_root": vault_root, "biblio_dir": biblio_dir}
    if data.get("unit") is not None:
        # Use the same server-owned identity as apply-all. A cached scan path
        # may have been reused by another PDF after our earlier rename.
        pdf, unit_error = _unit_pdf(data["unit"], rec, unit_ctx)
        if unit_error:
            return jsonify({"error": unit_error.get("error") or "No current PDF for this unit.",
                            "reason": unit_error.get("skipped")}), 409
        biblio_pdfs = [pdf]

    cfg = load_config()
    vocab, vocab_rel = _read_vocab(cfg, vault_root)
    if not vocab:
        return jsonify({"error": "Tag vocabulary (_tags.md) is unavailable."}), 400

    # A library-maintenance SESSION (library-workflow lot 3) brings its own
    # model choice, spending limit and proposal cache. Without one, nothing
    # below changes: the global provider, its fallback, no cache.
    session_ctx = None
    if data.get("session") is not None:
        session_ctx, err = _propose_session(data.get("session"), settings, cfg)
        if err is not None:
            return jsonify(err[0]), err[1]
    force = coerce_bool(data.get("force")) is True

    text_max = (coerce_int_in_range(cfg.get("tagging_text_max_chars"), 500, 40000)
                or _CFG_DEFAULTS["tagging_text_max_chars"])
    from core.extraction_status import ExtractionStatus, status_of
    extraction_status = ExtractionStatus().to_dict()
    text, truncated = "", False
    if biblio_pdfs:
        pdf_read = text_source.read_pdf_text(
            Path(biblio_pdfs[0]), vault_root, text_max, fresh_max_pages=8)
        text, truncated = pdf_read
        extraction_status = status_of(pdf_read).to_dict()
        if data.get("unit") is not None:
            # Recheck after extraction as well: never send text whose file
            # identity changed during the bounded read to a provider.
            after, unit_error = _unit_pdf(data["unit"], rec, unit_ctx)
            if unit_error or after != biblio_pdfs[0]:
                return jsonify({"error": "The PDF changed during reading; analyse again.",
                                "reason": "unit_stale"}), 409

    # Metadata mode (2026-08-11). No PDF, or a PDF that yielded no usable text
    # (a scan with no OCR layer), falls back to classifying from the abstract +
    # bibliographic fields instead of giving up. 2 081 entries in the
    # maintainer's library have no local PDF at all and previously had NO
    # classification path — propose 404'd or returned no_text and stopped.
    #
    # ONE no-text threshold for the PDF half, owned by text_source (it writes
    # the .notext marker on the same test) — an inline `40` here silently
    # drifted once.
    source_label = "PAPER TEXT"
    if len(text.strip()) < text_source._MIN_USEFUL_CHARS:
        # Exclude the controlled vocabulary from EXISTING KEYWORDS: those are
        # precisely the tags this app writes, so including them would show the
        # classifier the answer it is being asked for (2026-08-12).
        document, substantive = metadata_source.from_record(
            rec, max_chars=text_max, exclude_keywords=set(vocab or ()))
        if not metadata_source.is_classifiable(substantive):
            # Genuinely nothing to go on: no usable PDF text AND no abstract or
            # keywords. Reported as no_text exactly as before, so a client that
            # predates metadata mode sees the unchanged contract.
            return jsonify({
                "citation_key": citation_key, "proposed": [],
                "status": "no_text", "truncated": truncated,
                "source": "none", "has_pdf": bool(biblio_pdfs),
            })
        text, truncated = document, False
        source_label = "PAPER METADATA"

    choice = session_ctx["choice"] if session_ctx is not None else None
    if session_ctx is not None:
        provider, model = choice.provider, choice.model
        # A deep copy with every fallback removed: the session names ONE
        # model, and a failure must be visible rather than quietly paid for
        # by another provider. The global config is never touched.
        call_cfg = session_llm.call_config(cfg)
    else:
        provider = str(cfg.get("provider") or "ollama")
        model = coerce_string_max_len(cfg.get("tagging_propose_model"), 120) or resolve_chat_model(cfg, provider)
        call_cfg = cfg
    max_tags = coerce_int_in_range(cfg.get("tagging_max_tags"), 1, 12) or _CFG_DEFAULTS["tagging_max_tags"]
    max_tokens = (coerce_int_in_range(cfg.get("tagging_propose_max_tokens"), 64, 2048)
                  or _CFG_DEFAULTS["tagging_propose_max_tokens"])
    temperature = coerce_float_in_range(cfg.get("tagging_propose_temperature"), 0.0, 2.0)
    if temperature is None:
        temperature = _CFG_DEFAULTS["tagging_propose_temperature"]
    min_conf = coerce_float_in_range(cfg.get("tagging_min_conf"), 0.0, 1.0)
    if min_conf is None:
        min_conf = _CFG_DEFAULTS["tagging_min_conf"]
    candidate_n = coerce_int_in_range(cfg.get("tagging_candidate_tags"), 0, 200)
    if candidate_n is None:
        candidate_n = _CFG_DEFAULTS["tagging_candidate_tags"]
    fewshot_k = coerce_int_in_range(cfg.get("tagging_fewshot_k"), 0, 10)
    if fewshot_k is None:
        fewshot_k = _CFG_DEFAULTS["tagging_fewshot_k"]

    title = (rec.bib_entry.title if rec.bib_entry and rec.bib_entry.title else citation_key)
    source = "pdf" if source_label == "PAPER TEXT" else "metadata"

    cache_key = None
    if session_ctx is not None:
        cache_key = _proposal_key(provider=provider, model=model, vocab=vocab, text=text,
                                  title=title, max_tags=max_tags, temperature=temperature,
                                  max_tokens=max_tokens, source_label=source_label,
                                  candidate_n=candidate_n, fewshot_k=fewshot_k,
                                  prompt_sha256=hashlib.sha256(
                                      tag_propose._SYSTEM_PROMPT.encode("utf-8")).hexdigest())
        hit = None if force else imports_tracker.cached_proposal(settings, cache_key)
        if hit is not None and hit.get("proposed"):
            # Resumption: the same model, vocabulary and source text already
            # answered. Nothing is sent and nothing is spent. The status is
            # recomputed from TODAY's min_conf — it is a threshold over the
            # stored confidences, not part of the model's answer.
            replay = dict(hit, citation_key=citation_key, from_cache=True, sent=None,
                          session=session_ctx["id"], confidence_is_calibrated=False,
                          spend=(imports_tracker.session(settings, session_ctx["id"])
                                 or {}).get("spend"))
            replay["extraction_status"] = extraction_status
            replay["status"] = ("low_confidence"
                                if min(p["confidence"] for p in hit["proposed"]) < min_conf
                                else "ok")
            return jsonify(replay)

    def _fn(should_cancel):
        # Label-space reduction + journal exemplars run inside the same
        # bounded worker (embed calls carry their own 30 s construction bound
        # and the whole block degrades to (full vocab, []) on any failure).
        # should_cancel is threaded in so a 504'd request's worker stops
        # between embed stages instead of finishing the pipeline for nobody.
        effective_vocab, exemplars = tag_candidates.propose_context(
            cfg=cfg, vault_root=vault_root,
            tags_path=vault_root / (vocab_rel or ""), inv=inv,
            paper_text=text, vocab=vocab,
            candidate_n=candidate_n, fewshot_k=fewshot_k,
            should_cancel=should_cancel,
        )
        if session_ctx is None:
            return tag_propose.propose_tags(
                title=title, text=text, vocab=effective_vocab,
                provider_name=provider, model=model, temperature=temperature,
                max_tokens=max_tokens, cfg=call_cfg, max_tags=max_tags,
                should_cancel=should_cancel, exemplars=exemplars,
                source_label=source_label,
            )
        # Budget check on the EXACT prompt this call will send (the candidate
        # subset and exemplars are only known here), before anything leaves.
        # The propose gate is single-flight process-wide, so no other
        # proposal can spend between this check and the charge below.
        messages = tag_propose.build_messages(title, text, effective_vocab, max_tags,
                                              exemplars=exemplars, source_label=source_label)
        prompt_text = tag_propose._SYSTEM_PROMPT + "".join(m["content"] for m in messages)
        worst = session_llm.worst_case_usd(choice, cfg, prompt_text=prompt_text,
                                           max_tokens=max_tokens)
        spend = (imports_tracker.session(settings, session_ctx["id"]) or {}).get("spend") or {}
        session_llm.check_budget(choice, None if spend.get("unknown") else float(spend.get("usd", 0.0)),
                                 worst)
        sent_box["sent"] = session_llm.disclosure(choice, chars=len(prompt_text), source=source)
        start_iso = datetime.now(timezone.utc).isoformat()
        try:
            return tag_propose.propose_tags(
                title=title, text=text, vocab=effective_vocab,
                provider_name=provider, model=model, temperature=temperature,
                max_tokens=max_tokens, cfg=call_cfg, max_tags=max_tags,
                should_cancel=should_cancel, exemplars=exemplars,
                source_label=source_label,
            )
        finally:
            # Charged in the worker, even when the provider raised or the
            # request already answered 504: what the provider billed is spent
            # whether or not anyone is still listening.
            try:
                sent_box["spend"] = imports_tracker.charge_session(
                    settings, session_ctx["id"], _measured_cost(choice, start_iso, worst))
            except Exception:  # noqa: BLE001 - never mask the call's own outcome
                # The spend could not be recorded (store locked past its
                # timeout). Audit remediation 2026-09-23 (L4, maintainer
                # decision): the flag was set and then read by NOTHING, so the
                # session silently under-counted. Now the response says so
                # (`spend_error`) and the session's spend is marked UNKNOWN in
                # a second, smaller write — a budgeted session then refuses
                # further proposals rather than keep spending blind. If even
                # that cannot be written, `spend_unrecorded` tells the run to
                # stop proposing.
                logger.error("tagging propose: session %s spend not recorded",
                             session_ctx["id"], exc_info=True)
                sent_box["spend_error"] = True
                try:
                    sent_box["spend"] = imports_tracker.mark_spend_unknown(
                        settings, session_ctx["id"])
                except Exception:  # noqa: BLE001
                    logger.error("tagging propose: session %s spend could not even be "
                                 "marked unknown", session_ctx["id"], exc_info=True)
                    sent_box["spend_unrecorded"] = True

    sent_box: dict = {}
    try:
        proposed, timed_out, busy = _run_propose_bounded(_fn)
    except session_llm.BudgetExceeded as exc:
        # Nothing was sent: the check runs before the call.
        return jsonify({"error": str(exc), "reason": "budget",
                        "session": session_ctx["id"] if session_ctx else None}), 409
    except Exception as exc:  # noqa: BLE001
        # A session names ONE model and has no fallback, so say which one
        # failed: "no silent fallback" is only useful if the failure is loud.
        body = {"error": sanitise_error_msg(exc)}
        if session_ctx is not None:
            body.update(provider=provider, model=model, sent=sent_box.get("sent"),
                        spend=sent_box.get("spend"), **_spend_flags(sent_box))
        return jsonify(body), 502
    if busy:
        return jsonify({"error": "A tag proposal is already running; try again shortly."}), 429
    if timed_out:
        return jsonify({"error": "Tag proposal timed out."}), 504

    proposed = proposed or []
    if not proposed:
        status = "empty"
    elif min(p["confidence"] for p in proposed) < min_conf:
        status = "low_confidence"
    else:
        status = "ok"
    payload = {
        "citation_key": citation_key, "proposed": proposed,
        "status": status, "truncated": truncated,
        "extraction_status": extraction_status,
        # Which evidence produced this: the PDF's text, or the abstract +
        # bibliographic fields. The deck shows it so a user can weigh a
        # metadata-only proposal differently before accepting it.
        "source": source,
        "has_pdf": bool(biblio_pdfs),
        # Which model actually spent the tokens (additive, 2026-08-13). It was
        # invisible everywhere: this response carried no model field, the CLI
        # batch echoed none, and `papermind status` reports the CHAT model —
        # which is the wrong answer precisely when `tagging_propose_model` is
        # set, i.e. exactly when a user has expressed a preference. Resolved
        # above, so this cannot drift from what was called.
        "provider": provider,
        "model": model,
    }
    if session_ctx is not None:
        # Stored WITHOUT the per-call fields, so a later cache hit reports
        # what it is (a replay, nothing sent) rather than this call's facts.
        if proposed:
            # An EMPTY parse is usually transient (a token cap spent on
            # thinking, a prose reply) — caching it would replay the failure
            # on every resume without ever asking again.
            imports_tracker.store_proposal(settings, cache_key, dict(payload))
        payload.update(session=session_ctx["id"], from_cache=False,
                       sent=sent_box.get("sent"), spend=sent_box.get("spend"),
                       **_spend_flags(sent_box),
                       # The model's own confidence is NOT a calibrated
                       # probability and never authorises a write; it only
                       # orders the chips (plan §7).
                       confidence_is_calibrated=False)
    return jsonify(payload)


def _spend_flags(sent_box: dict) -> dict:
    """``spend_error``/``spend_unrecorded`` only when set (L4): an unchanged
    response shape for every call whose spend was recorded normally."""
    return {k: True for k in ("spend_error", "spend_unrecorded") if sent_box.get(k)}


def _unit_pdf(raw_unit, rec, ctx):
    """``(abs_path, None)`` for the PDF an import-tracker unit names, or
    ``(None, finder_result)`` explaining why it cannot be used.

    Server-owned identity, never a client path: the unit must be a present
    ATTACHMENT of THIS record's Zotero item, and the file at its recorded path
    must still be the file the tracker observed (same inode + birth time). A
    file replaced since the last observation is refused — tagging it would
    tag bytes nobody reviewed — and a unit of another record is refused, so a
    key cannot be pointed at someone else's PDF.
    """
    uid = coerce_string_max_len(raw_unit, 200)
    settings = ctx["settings"]
    tracked = imports_tracker.unit(settings, uid) if uid else None
    if not tracked or tracked.get("kind") != "attachment" or tracked.get("state") != "present":
        return None, {"ok": False, "error": "Unknown or absent import unit.", "skipped": "unit"}
    item = rec.zotero_item
    item_key = getattr(item, "item_key", None) if item is not None else None
    parent = str(tracked.get("parent") or "")
    lib_id = getattr(item, "library_id", None) if item is not None else None
    if not item_key or not parent.endswith(f":{item_key}") or (
            lib_id is not None and parent != f"entry:{lib_id}:{item_key}"):
        return None, {"ok": False, "error": "That PDF belongs to another record.", "skipped": "unit"}
    if not tracked.get("rel") or not tracked.get("doc"):
        return None, {"ok": False, "skipped": "no_pdf"}
    path = Path(ctx["vault_root"]) / tracked["rel"]
    if not _is_under(path.resolve(), Path(ctx["biblio_dir"]).resolve()):
        # Finder tags are scoped to `biblio_articles/` (the writer re-checks
        # too; saying so here keeps the refusal explicit).
        return None, {"ok": False, "skipped": "no_pdf"}
    try:
        from accession import review
        fp = review.fingerprint(path)
    except OSError:
        return None, {"ok": False, "error": "The PDF moved since the last observation; observe again.",
                      "skipped": "unit_stale"}
    if f"doc:{fp['ino']}:{fp.get('birth')}" != tracked["doc"]:
        return None, {"ok": False, "error": "The PDF was replaced since the last observation; observe again.",
                      "skipped": "unit_stale"}
    return path, None


def _propose_session(raw_sid, settings, cfg):
    """``({id, choice}, None)`` or ``(None, (body, status))`` for a propose."""
    sid = coerce_string_max_len(raw_sid, 64)
    if not sid:
        return None, ({"error": "session must be a session id."}, 400)
    record = imports_tracker.session(settings, sid)
    if record is None:
        return None, ({"error": "Unknown session for this library."}, 404)
    if not record.get("llm"):
        return None, ({"error": "Choose the model for this session first."}, 400)
    try:
        # Re-validated per call: a price may have gone, a provider may have
        # been renamed. The stored choice is never trusted blindly.
        choice = session_llm.validate(record["llm"], cfg)
    except session_llm.SessionLLMError as exc:
        return None, ({"error": str(exc)}, 400)
    return {"id": sid, "choice": choice}, None


def _proposal_key(**parts) -> str:
    """Digest of everything that decides a proposal.

    The vocabulary and the source text are hashed, so an edit to ``_tags.md``
    or a re-extracted PDF is a miss. Few-shot exemplars are deliberately NOT
    part of it: they are mined from the tagging journal, which grows with
    every apply, and keying on them would make every cache entry die at the
    first write of the session it was meant to resume.
    """
    vocab = parts.pop("vocab")
    text = parts.pop("text")
    parts["vocab_sha256"] = hashlib.sha256(
        json.dumps(sorted(vocab.items()), ensure_ascii=False).encode()).hexdigest()
    parts["text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _measured_cost(choice, start_iso, worst):
    """What this call cost, never LESS than can be shown (review 2026-09-21).

    * a local model costs nothing billable: 0;
    * an UNPRICED online model: None — unknown, never zero. The usage
      tracker records such a call at $0.00, and trusting that record made an
      unpriced session read as free and disabled the "unmeasured spend" guard;
    * a priced online model: the tracker's records since the call started
      (single-flight, so they are this call's) — but ONLY when every record
      is a clean success. A failed call is recorded at $0 although the
      provider billed what it read and generated, and a cancelled stream
      (the 504 path) is recorded without its input tokens, because usage
      arrives on the final chunk. In those cases, and when no record exists,
      the pre-call worst case is charged instead: an over-estimate, which is
      the only direction a spending limit can tolerate.
    """
    if not choice.online:
        return 0.0
    if worst is None:
        return None
    try:
        from core.llm.usage import usage_tracker
        records = usage_tracker.records_since(start_iso, workflow="tagging_propose",
                                              provider=choice.provider, model=choice.model)
    except Exception:  # noqa: BLE001 - accounting must not break the call
        records = []
    clean = bool(records) and all(
        getattr(r, "success", True) and not getattr(r, "error_category", "")
        and float(getattr(r, "cost_usd", 0.0) or 0.0) > 0.0 for r in records)
    if clean:
        return float(sum(r.cost_usd for r in records))
    return worst


# ---------------------------------------------------------------------------
# Write surface (slice 2): Obsidian note-frontmatter tags, journalled + reversible
# ---------------------------------------------------------------------------

@tagging_bp.route("/api/tagging/apply-note-tags", methods=["POST"])
def api_tagging_apply_note_tags():
    """Union chosen tags into a citation key's Obsidian note frontmatter.

    Body: ``{citation_key, tags:[...], content_sha256?, confirm:true}``.

    Guardrails (mirroring the audit's ``apply-tag-drift`` writer, plus a journal):

    * ``confirm:true`` required (400).
    * The note is resolved from the cached audit scan (``citation_key`` → record
      → ``obsidian_note.path``); a scan must have run (404).
    * Requested tags are intersected **server-side** with the controlled
      ``_tags.md`` vocabulary — the client cannot inject an arbitrary tag; any
      out-of-vocabulary tag is reported under ``rejected`` and dropped (400 if
      none survive).
    * Optional ``content_sha256`` stale-diff guard (409 on mismatch).
    * The write holds the obsidian op-lock (503 if indexing). Before mutating,
      the note's prior bytes are snapshotted into the tagging archive (OUTSIDE
      the vault) and an ``op`` recorded in the restore manifest, so the write is
      reversible from ``/api/tagging/restore``. Only the ``tags:`` field is
      edited (``merge_tags_into_frontmatter`` refuses rather than reformat), the
      change is ``log_vault_write``-audited, and the write is atomic.
    """
    data = request.get_json(silent=True)
    ctx, err = _prepare_tag_write(data)
    if err is not None:
        body, status = err
        return jsonify(body), status
    rec = ctx["rec"]
    if rec.obsidian_note is None:
        return jsonify({"error": "No Obsidian note for that citation key."}), 404
    want_sha = coerce_string_max_len((data or {}).get("content_sha256"), 64)

    from rag.vault import obsidian_manager

    op_epoch = obsidian_manager.try_acquire_lock(ttl=3600)
    if not op_epoch:
        return jsonify({"error": "Indexing is in progress; try again shortly."}), 503
    try:
        result, status = _note_write_locked(
            rec=rec, valid=ctx["valid"], rejected=ctx["rejected"], want_sha=want_sha,
            cfg=ctx["cfg"], vault_root=ctx["vault_root"], citation_key=ctx["citation_key"],
        )
    except journal.JournalUnreadableError as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 503
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    finally:
        obsidian_manager.release_lock(op_epoch)
    return jsonify(result), status


_PDF_LINK_MAX_KEYS = 200

#: Checkpoint cadence for the batch — refactor/apply.py's JOURNAL_FLUSH_EVERY
#: and _BatchJournalKeeper._FLUSH_EVERY agree on 25: a once-per-batch save
#: leaves written notes invisible to Restore on a mid-batch crash, and a
#: per-note save is the O(N) fsync extreme.
_PDF_LINK_FLUSH_EVERY = 25


@tagging_bp.route("/api/tagging/apply-pdf-links", methods=["POST"])
def api_tagging_apply_pdf_links():
    """Write each record's mapped PDFs into its note as a ``pdf:`` wikilink list.

    Body: ``{citation_keys: [...], confirm: true}`` (or a single
    ``citation_key``). The note gains

        pdf:
          - "[[smith_2020.pdf]]"

    so the reference note links to its own file, one click away in Obsidian.

    **The client sends KEYS ONLY.** The filenames come from the cached scan's
    PDF↔bib join, never from the request — the same posture as ``harmonize``,
    and the reason a client cannot make a note point at an arbitrary file.

    Bounded at ``_PDF_LINK_MAX_KEYS`` and at half the tagging journal's
    retention window, because each write is a note op with a snapshot and the
    response promises an undo the journal has to be able to keep
    (``_HARMONIZE_MAX_NOTE_OPS``' precedent). Journalled as ``note_pdf_links``,
    reversible from Tagging → Restore.

    Note the interaction that makes this safe to use at all: writing this
    field makes those PDFs "referenced from a note", and
    ``maintenance/rename.py`` refuses to rename a referenced PDF. The rename
    route recognises THIS shape and rewrites it in the same journalled op; any
    other reference still refuses. Do not change the shape written here
    without changing `_audit_tagwrite`'s matcher in the same commit.
    """
    from tagging.writers import _note_pdf_links_locked

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400
    if coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "Write must be explicitly confirmed (confirm: true)."}), 400
    raw = data.get("citation_keys")
    if raw is None and data.get("citation_key"):
        raw = [data.get("citation_key")]
    if not isinstance(raw, list) or not raw:
        return jsonify({"error": "citation_keys must be a non-empty list."}), 400
    keys, seen = [], set()
    for k in raw:
        key = coerce_non_empty_string(k, max_len=128)
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    if not keys:
        return jsonify({"error": "citation_keys must be a non-empty list."}), 400
    if len(keys) > _PDF_LINK_MAX_KEYS:
        return jsonify({"error": f"At most {_PDF_LINK_MAX_KEYS} keys per request."}), 400
    if len(keys) > _HARMONIZE_MAX_NOTE_OPS:
        return jsonify({"error": (
            f"That is more note writes than the restore journal retains "
            f"({_HARMONIZE_MAX_NOTE_OPS}); split it so the undo this offers "
            "can actually be kept.")}), 400

    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify({"error": "No scan results. Run an audit scan first."}), 404
    cfg = load_config()
    vault_root = settings.vault_root

    from rag.vault import obsidian_manager

    op_epoch = obsidian_manager.try_acquire_lock(ttl=3600)
    if not op_epoch:
        return jsonify({"error": "Indexing is in progress; try again shortly."}), 503
    results: dict = {}
    written = 0
    aborted = False
    manifest = None
    op_ids: set = set()

    def _keep_landed_ops() -> None:
        """Best-effort persistence of the ops of notes ALREADY written, before
        answering an error (audit remediation 2026-09-23, N1 — the fix
        `_create_notes` got in 2026-09-21). Ops are appended with
        `defer_save=True` and checkpointed every `_PDF_LINK_FLUSH_EVERY`, so
        an exception between two checkpoints used to answer 500 while up to
        24 rewritten notes had no undo record. Never raises: the original
        error is the answer."""
        if manifest is None or not op_ids:
            return
        try:
            journal.prune(vault_root, cfg, manifest, protect_op_ids=op_ids)
            journal.save(vault_root, cfg, manifest)
        except Exception:  # noqa: BLE001
            logger.exception("apply-pdf-links: journal save after failure failed")

    try:
        from api.routes._audit_tagwrite import (
            ambiguous_pdf_basenames, expected_pdf_links,
        )
        ambiguous = ambiguous_pdf_basenames(vault_root)
        manifest = journal.load(vault_root, cfg)
        for i, key in enumerate(keys, start=1):
            rec = inv.records.get(key)
            if rec is None:
                results[key] = {"ok": False, "reason": "unknown_key"}
                continue
            if rec.obsidian_note is None:
                results[key] = {"ok": False, "reason": "no_note"}
                continue
            names = expected_pdf_links(rec.pdf_paths, vault_root, ambiguous)
            if not names:
                results[key] = {"ok": False, "reason": "no_pdf"}
                continue
            res, status = _note_pdf_links_locked(
                rec=rec, names=names, want_sha=None, cfg=cfg,
                vault_root=vault_root, citation_key=key,
                manifest=manifest, defer_save=True)
            results[key] = res
            if res.get("op_id"):
                op_ids.add(res["op_id"])
            if status == 200 and res.get("changed"):
                written += 1
            if i % _PDF_LINK_FLUSH_EVERY == 0:
                if not obsidian_manager.heartbeat(op_epoch):
                    # A refused heartbeat means the lock was stolen (our TTL
                    # lapsed); stop writing rather than race the new holder.
                    # Reported as its own field, never as a `results` entry —
                    # a client iterating results by citation key would read
                    # `_aborted` as a paper.
                    aborted = True
                    break
                journal.save(vault_root, cfg, manifest)
        if aborted:
            # The lock is someone else's now (audit remediation 2026-09-23,
            # N2): our in-memory view of the journal may be stale, so a
            # `prune` on it could evict what the new holder just wrote, and
            # a plain save would write that stale view back. Merge ONLY our
            # ops into what is on disk (CLAUDE.md's lost-lock rule), no prune.
            journal.save_merged(vault_root, cfg, manifest, prefer_ours=op_ids)
        else:
            journal.prune(vault_root, cfg, manifest, protect_op_ids=op_ids)
            journal.save(vault_root, cfg, manifest)
    except journal.JournalUnreadableError as exc:
        _keep_landed_ops()
        return jsonify({"error": sanitise_error_msg(exc), "written": written,
                        "results": results}), 503
    except Exception as exc:  # noqa: BLE001
        logger.exception("apply-pdf-links failed")
        _keep_landed_ops()
        return jsonify({"error": sanitise_error_msg(exc), "written": written,
                        "results": results}), 500
    finally:
        obsidian_manager.release_lock(op_epoch)
    return jsonify({"ok": True, "written": written, "results": results,
                    **({"aborted": "lock_lost"} if aborted else {})}), 200


#: The states of a record's ``pdf:`` list, as ``pdf-links-status`` reports
#: them. ``current`` is the only one that needs nothing.
PDF_LINK_STATES = ("current", "absent", "stale", "dirty", "unreadable",
                   "no_note", "no_pdf")


def _pdf_link_state(rec, vault_root, ambiguous: set[str]
                    ) -> tuple[str, list[str], list[str]]:
    """``(state, expected entries, entries the note lists)`` for one record.

    The expected entries come from the same ``expected_pdf_links`` call
    ``apply-pdf-links`` makes, so ``current`` here means "a write would
    change nothing" and never a looser notion of close enough.
    """
    from api.routes._audit_tagwrite import (
        expected_pdf_links, pdf_list_is_clean, read_pdf_links,
    )

    names = expected_pdf_links(rec.pdf_paths, vault_root, ambiguous)
    if rec.obsidian_note is None:
        return ("no_note" if names else ""), names, []
    if not names:
        return "no_pdf", names, []
    try:
        text = rec.obsidian_note.path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return "unreadable", names, []
    if not pdf_list_is_clean(text):
        return "dirty", names, read_pdf_links(text)
    links = read_pdf_links(text)
    if links == names:
        return "current", names, links
    return ("absent" if not links else "stale"), names, links


@tagging_bp.route("/api/tagging/pdf-links-status")
def api_tagging_pdf_links_status():
    """Which reference notes carry an accurate ``pdf:`` list (read-only).

    The observational twin of ``apply-pdf-links`` (2026-09-17, annual cleanup
    T1 — nothing reported this before, so "notes without a pdf: list" had no
    number). Per record of the cached scan:

    * ``current`` — the list equals the mapped PDFs; a write changes nothing;
    * ``absent`` — no app-shaped list; ``apply-pdf-links`` would add one;
    * ``stale`` — an app-shaped list naming other files;
    * ``dirty`` — a list this app did not write; the writer refuses it;
    * ``unreadable`` — the note cannot be read as UTF-8;
    * ``no_note`` — mapped PDFs but no reference note; ``no_pdf`` — a note but
      no mapped PDF (both informational: nothing to write).

    ``rows`` lists every record NOT ``current`` (records with neither a note
    nor a PDF are omitted); ``?state=`` narrows them. Reads the notes from
    disk on the request thread: ~1.2k small files on the live vault.
    """
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify({"error": "No scan results. Run an audit scan first."}), 404
    from api.routes._audit_tagwrite import ambiguous_pdf_basenames
    ambiguous = ambiguous_pdf_basenames(settings.vault_root)
    wanted = request.args.get("state") or ""
    if wanted and wanted not in PDF_LINK_STATES:
        return jsonify({"error": "Unknown state. Known: " + ", ".join(PDF_LINK_STATES)}), 400
    counts = {s: 0 for s in PDF_LINK_STATES}
    rows = []
    for key in sorted(inv.records):
        rec = inv.records[key]
        state, names, links = _pdf_link_state(rec, settings.vault_root, ambiguous)
        if not state:
            continue
        counts[state] += 1
        if state == "current" or (wanted and state != wanted):
            continue
        note = rec.obsidian_note.path if rec.obsidian_note is not None else None
        rows.append({"citation_key": key, "state": state, "expected": names,
                     "listed": links,
                     "note": note.name if note is not None else None})
    return jsonify({"counts": counts, "rows": rows, "count": len(rows),
                    "scanned_at": getattr(inv, "built_at", 0)})


@tagging_bp.route("/api/tagging/apply-finder-tags", methods=["POST"])
def api_tagging_apply_finder_tags():
    """Union chosen tags into the citation key's ``biblio_articles/`` PDF Finder tags.

    Body: ``{citation_key, tags:[...], confirm:true}``. macOS-only (501 elsewhere
    — Finder tags are a macOS feature). Confirm-gated, obsidian op-lock (single
    manifest writer), tags server-intersected with the controlled vocabulary.
    Reversible from ``/api/tagging/restore``.
    """
    from api.routes import _finder_tagwrite

    if not _finder_tagwrite.is_macos():
        return jsonify({"error": "Finder tags are a macOS-only feature."}), 501

    ctx, err = _prepare_tag_write(request.get_json(silent=True))
    if err is not None:
        body, status = err
        return jsonify(body), status

    from rag.vault import obsidian_manager

    op_epoch = obsidian_manager.try_acquire_lock(ttl=3600)
    if not op_epoch:
        return jsonify({"error": "Indexing is in progress; try again shortly."}), 503
    try:
        result, status = _finder_write_locked(
            rec=ctx["rec"], valid=ctx["valid"], rejected=ctx["rejected"],
            cfg=ctx["cfg"], vault_root=ctx["vault_root"], biblio_dir=ctx["biblio_dir"],
            citation_key=ctx["citation_key"],
        )
    except journal.JournalUnreadableError as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 503
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    finally:
        obsidian_manager.release_lock(op_epoch)
    return jsonify(result), status


# ---------------------------------------------------------------------------
# Zotero item tags (slice 5) — via the LOCAL API, never SQLite
# ---------------------------------------------------------------------------

@tagging_bp.route("/api/tagging/zotero-available")
def api_tagging_zotero_available():
    """Read readiness for this Desktop-selected tagging surface; never authorize.

    Reachability is distinct from permission. A Web transport cannot consume
    scan identities and therefore never advertises an eligible tag target.
    """
    from tagging import local_api
    cfg = load_config()
    transport = zotero_write.resolve_transport(cfg)
    result = dict(available=False, writable=False, transport=transport.kind,
                  base=transport.base_url, mode=cfg.get("tagging_zotero_transport", "auto"),
                  authorized=False, supports_writes=False)
    if transport.is_web:
        # This surface only selects Desktop scan records. Probing a Web key
        # cannot make those identities eligible for a Web write.
        result["note"] = ("These tags target a Zotero Desktop selection. Authorize Desktop "
                          "to use it in automatic mode, or explicitly switch Web to local. "
                          "No Web request has been sent.")
        return jsonify(result)
    try:
        state = local_api.capabilities(transport.base_url, opener=zotero_write._opener, timeout=3)
        result.update(available=True, authorized=state['authorized'],
                      supports_writes=state['supports_writes'])
        result['writable'] = bool(state['authorized'] and state['supports_writes'])
        result['note'] = ("Zotero Desktop is authorized for this session." if result['writable']
                          else "Authorize CHERQAiN in Zotero Desktop before applying tags."
                          if state['supports_writes'] else "This Zotero endpoint does not advertise local writes.")
    except Exception:
        result['note'] = "Zotero Desktop is unavailable. Open Zotero and refresh access."
    return jsonify(result)


@tagging_bp.route("/api/tagging/apply-zotero-tags", methods=["POST"])
def api_tagging_apply_zotero_tags():
    """Union chosen tags into the citation key's parent Zotero item, via the local API.

    Body: ``{citation_key, tags, confirm}``. Writes go through Zotero's local
    HTTP API (never SQLite). Confirm-gated, obsidian op-lock (single manifest
    writer), tags server-intersected with the controlled vocabulary. 503 when
    Zotero is unreachable, 502 on other API errors, 409 on a version conflict.
    Reversible.
    """
    ctx, err = _prepare_tag_write(request.get_json(silent=True))
    if err is not None:
        body, status = err
        return jsonify(body), status
    rec = ctx["rec"]
    if rec.zotero_item is None:
        return jsonify({"error": "No Zotero item for that citation key."}), 404

    # Read phase BEFORE the lock: the slow/fragile part (a 15 s-timeout HTTP
    # GET against local Zotero) must not stall indexing and other tag writes.
    try:
        prepared, early, early_status = _zotero_prepare(
            rec=rec, valid=ctx["valid"], rejected=ctx["rejected"],
            cfg=ctx["cfg"], citation_key=ctx["citation_key"])
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    if prepared is None:
        return jsonify(early), early_status

    from rag.vault import obsidian_manager

    op_epoch = obsidian_manager.try_acquire_lock(ttl=3600)
    if not op_epoch:
        return jsonify({"error": "Indexing is in progress; try again shortly."}), 503
    try:
        result, status = _zotero_commit_locked(
            prepared=prepared, rejected=ctx["rejected"],
            cfg=ctx["cfg"], vault_root=ctx["vault_root"], citation_key=ctx["citation_key"],
        )
    except journal.JournalUnreadableError as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 503
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    finally:
        obsidian_manager.release_lock(op_epoch)
    return jsonify(result), status


@tagging_bp.route("/api/tagging/add-vocab-tag", methods=["POST"])
def api_tagging_add_vocab_tag():
    """Append one tag id to the ``_tags.md`` controlled vocabulary (journalled).

    Body: ``{tag, gloss?, confirm:true}``. The drift-fix "add to vocabulary,
    case by case" verb (2026-08-06) — the mirror of ``retire-tag``'s
    ``include_vocab`` edit: the finder-drift report badges out-of-vocabulary
    surfaces as unapplyable (the writers' security gate refuses them), and this
    endpoint is the confirmed way to admit one so the apply can follow.

    Guardrails: confirm-gated; the tag id is shape-checked (single token — the
    vocabulary parser skips space-containing ids); an id already present (NFC
    +lower, the writers' own gate identity) is a no-op answered with the
    existing id so the client can chain the apply; the edit runs under the
    obsidian op-lock, snapshots ``_tags.md`` into the tagging archive first
    (op kind ``vocab_add``, reversible from ``/api/tagging/restore``), is
    ``log_vault_write``-audited and written atomically.
    """
    import re as _re

    from core.utils import log_vault_write, write_text_atomic
    from tagging.vocab import add_tag_to_vocab

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400
    if coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "Vocabulary edit must be explicitly confirmed (confirm: true)."}), 400
    tag = coerce_non_empty_string(data.get("tag"), max_len=128)
    if tag:
        tag = tag.strip().lstrip("#").strip()
    if not tag:
        return jsonify({"error": "tag required"}), 400
    if _re.search(r"\s", tag):
        return jsonify({"error": "Tag ids must be single tokens (no whitespace)."}), 400
    gloss = coerce_string_max_len(data.get("gloss"), 500) or ""

    try:
        vault_root = _vault_root()
    except AuditConfigError as exc:
        return jsonify({"error": str(exc)}), 400
    cfg = load_config()
    _vocab, vocab_rel = _read_vocab(cfg, vault_root)
    if not vocab_rel:
        return jsonify({"error": "Tag vocabulary (_tags.md) is not configured or not found."}), 404

    from rag.vault import obsidian_manager

    op_epoch = obsidian_manager.try_acquire_lock(ttl=3600)
    if not op_epoch:
        return jsonify({"error": "Indexing is in progress; try again shortly."}), 503
    try:
        vpath = vault_root / vocab_rel
        try:
            raw = vpath.read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return jsonify({"error": f"Cannot read _tags.md: {sanitise_error_msg(exc)}"}), 400

        new_text, add_status = add_tag_to_vocab(text, tag, gloss)
        if add_status == "exists":
            return jsonify({"ok": True, "already": True, "tag": tag,
                            "message": "Tag is already in the vocabulary."})
        if add_status != "added":
            return jsonify({"error": "No tags table found in _tags.md to append to."}), 422

        try:
            manifest = journal.load(vault_root, cfg)
        except journal.JournalUnreadableError as exc:
            return jsonify({"error": sanitise_error_msg(exc)}), 503
        except journal.ScopeError as exc:
            return jsonify({"error": f"Tagging archive misconfigured: {exc}"}), 400
        op_id = journal.new_op_id(manifest)
        snapshot_rel = journal.write_note_snapshot(
            vault_root, cfg, vocab_rel, op_id, raw)
        new_bytes = new_text.encode("utf-8")
        op = {
            "op_id": op_id, "kind": "vocab_add", "note_rel": vocab_rel,
            "added": [tag],
            "hash_before": hashlib.sha256(raw).hexdigest(),
            "hash_after": hashlib.sha256(new_bytes).hexdigest(),
            "snapshot_rel": snapshot_rel,
            "ts": journal.now_iso(), "state": "applied",
        }
        manifest["ops"].append(op)
        # Journal-before-write (the note writers' order): the applied op +
        # snapshot are durable before _tags.md changes.
        journal.save(vault_root, cfg, manifest)
        log_vault_write("tagging_vocab_add", str(vpath), f"+#{tag}")
        try:
            write_text_atomic(str(vpath), new_text)
        except OSError as exc:
            # Same failure discipline as vocab_retire: never leave an
            # "applied" claim for an edit that didn't land.
            op["state"] = "failed"
            journal.save(vault_root, cfg, manifest)
            return jsonify({"error": sanitise_error_msg(exc)}), 500
        journal.prune(vault_root, cfg, manifest)
        journal.save(vault_root, cfg, manifest)
    except journal.ScopeError as exc:
        return jsonify({"error": f"Tagging archive misconfigured: {exc}"}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    finally:
        obsidian_manager.release_lock(op_epoch)
    return jsonify({"ok": True, "op_id": op_id, "tag": tag, "rel": vocab_rel})


#: Fields the Restore view renders per op.
#
# "removed"/"batch_id" joined 2026-07-23: the untag/vocab_retire op kinds carry
# `removed`, not `added` — without them the Restore view showed those ops with
# no tag detail, making the per-op fallback undo unusable.
# "pdf_rel" joined 2026-08-11: a path-addressed finder_untag (a PDF with no
# citation key) has citation_key None, so without it the op renders with no
# identity and its undo button is unusable.
_MANIFEST_FIELDS = ("op_id", "kind", "note_rel", "citation_key", "pdf_rel",
                    "added", "removed", "batch_id", "ts", "state")


@tagging_bp.route("/api/tagging/manifest")
def api_tagging_manifest():
    """One newest-first PAGE of the tagging restore manifest.

    Paginated 2026-08-26. The unpaged version loaded and JSON-parsed the whole
    history on every open — measured at 11,651 ops / 8 MB / ~110 ms on the
    maintainer's library — and then rendered one table row per op. It now goes
    through ``journal.load_page`` (bounded SQL, ~1 ms), and the response gained
    ``total``/``next_before``/``limit`` so the client can page. ``count`` still
    means "ops in THIS response" and the newest-first ordering is unchanged, so
    a caller that ignores the new fields sees the first page and nothing breaks.

    ``?limit=`` (1..``MAX_PAGE_SIZE``) and ``?before=`` (the previous page's
    ``next_before`` cursor) are both optional.
    """
    try:
        vault_root = _vault_root()
    except AuditConfigError as exc:
        return jsonify({"error": str(exc)}), 400
    cfg = load_config()
    limit = (coerce_int_in_range(request.args.get("limit"), 1,
                                 journal.MAX_PAGE_SIZE)
             or journal.DEFAULT_PAGE_SIZE)
    before = request.args.get("before")
    before_seq = None
    if before not in (None, ""):
        before_seq = coerce_int_in_range(before, 1, 2 ** 62)
    try:
        page = journal.load_page(
            vault_root, cfg, limit=limit, before_seq=before_seq)
    except journal.ScopeError as exc:
        return jsonify({"error": str(exc)}), 400
    except journal.JournalUnreadableError as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 503
    ops = [{k: op.get(k) for k in _MANIFEST_FIELDS} for op in page["ops"]]
    return jsonify({
        "ops": ops,
        "count": len(ops),
        "total": page["total"],
        "next_before": page["next_before"],
        "limit": limit,
    })


@tagging_bp.route("/api/tagging/journal-stats")
def api_tagging_journal_stats():
    """Size and composition of the restore journal — no payloads parsed.

    Read-only. Exists because nothing in the app said how large this file had
    become, while applied Finder/Zotero ops are exempt from eviction by design
    and therefore accumulate for the lifetime of the library.
    """
    try:
        vault_root = _vault_root()
    except AuditConfigError as exc:
        return jsonify({"error": str(exc)}), 400
    cfg = load_config()
    try:
        st = journal.stats(vault_root, cfg)
    except journal.ScopeError as exc:
        return jsonify({"error": str(exc)}), 400
    except journal.JournalUnreadableError as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 503
    except journal.JournalTransientReadError as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 503
    st["retention"] = {
        "enabled": bool(cfg.get("tagging_journal_retention_enabled")),
        "policy": cfg.get("tagging_journal_retention_policy"),
        "days": cfg.get("tagging_journal_retention_days"),
        "keep": cfg.get("tagging_journal_retention_keep"),
    }
    return jsonify(st)


@tagging_bp.route("/api/tagging/compact-journal", methods=["POST"])
def api_tagging_compact_journal():
    """Drop applied Finder/Zotero ops per a chosen policy. Confirm-gated.

    ``{policy: "age_days"|"keep_newest", value: int, dry_run?: bool,
    confirm: true}``. ``dry_run`` needs no confirm and writes nothing — it is
    how the UI shows the count before asking.

    This is the one action in the subsystem that deliberately destroys restore
    mappings, so it is confirm-gated, op-locked and ``log_vault_write``-audited
    like any other write, and it never touches note or vocab ops (they keep
    their file snapshots and the existing ``_MAX_OPS`` window).
    """
    data = request.get_json(silent=True) or {}
    policy = coerce_enum(data.get("policy"), journal.RETENTION_POLICIES)
    if policy is None:
        return jsonify({
            "error": "policy must be one of: "
                     + ", ".join(journal.RETENTION_POLICIES)}), 400
    value = coerce_int_in_range(data.get("value"), 1, 100_000)
    if not value:
        return jsonify({"error": "value must be a positive integer."}), 400
    # Absent means dry run: the destructive branch must be asked for, never
    # arrived at by omitting a field.
    dry_run = coerce_bool(data.get("dry_run"))
    dry_run = True if dry_run is None else dry_run
    if not dry_run and data.get("confirm") is not True:
        return jsonify({"error": "confirm: true is required."}), 400
    try:
        vault_root = _vault_root()
    except AuditConfigError as exc:
        return jsonify({"error": str(exc)}), 400
    cfg = load_config()

    if dry_run:
        try:
            return jsonify(journal.compact(
                vault_root, cfg, policy=policy, value=value, dry_run=True))
        except journal.ScopeError as exc:
            return jsonify({"error": str(exc)}), 400
        except (journal.JournalUnreadableError,
                journal.JournalTransientReadError) as exc:
            return jsonify({"error": sanitise_error_msg(exc)}), 503

    from rag.vault import obsidian_manager

    op_epoch = obsidian_manager.try_acquire_lock(ttl=600)
    if not op_epoch:
        return jsonify({"error": "Indexing is in progress; try again shortly."}), 503
    try:
        result = journal.compact(
            vault_root, cfg, policy=policy, value=value, dry_run=False)
    except journal.ScopeError as exc:
        return jsonify({"error": str(exc)}), 400
    except (journal.JournalUnreadableError,
            journal.JournalTransientReadError) as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 503
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    finally:
        obsidian_manager.release_lock(op_epoch)
    return jsonify(result)


def _prepare_zotero_revert(cfg, op):
    """The lock-free READ half of one Zotero op's revert, by kind."""
    kind = op.get("kind")
    if kind == "zotero_attachment_trash":
        return _revert_zotero_attachment_prepare(cfg, op)
    if kind == "zotero_attachment_create":
        return _revert_zotero_attachment_create_prepare(cfg, op)
    if kind == "zotero_item_create":
        return _revert_zotero_item_create_prepare(cfg, op)
    if kind == "zotero_attachment_repath":
        return _revert_zotero_attachment_repath_prepare(cfg, op)
    if kind == "zotero_attachment_retitle":
        return _revert_zotero_attachment_retitle_prepare(cfg, op)
    if kind == "zotero_metadata_write":
        return _revert_zotero_metadata_prepare(cfg, op)
    if kind == "zotero_child_reparent":
        return _revert_zotero_child_reparent_prepare(cfg, op)
    if kind == "zotero_item_trash":
        return _revert_zotero_item_trash_prepare(cfg, op)
    if kind in _NOTESYNC_ZOTERO_KINDS:
        from notesync import zotero_notes as _zn
        return _zn.revert_prepare(cfg, op)
    return _revert_zotero_prepare(cfg, op)


def _restore_batch(batch_id: str):
    """Recover applied and uncertain ops of *batch_id*, newest-first (SSE).

    The batch twin of the single-op restore, and **streamed since 2026-07-30**
    (fix-plan Plan 5, closing the 2026-07-23 deferral): undoing a 150-op retire
    is N pre-lock Zotero GETs plus N reverts, which as one synchronous JSON
    request meant minutes of silence on a Waitress thread with no progress and
    no way to tell a slow run from a wedged one. Per-op ``{"info"}`` frames now
    report progress and the terminal ``{"restore": {...}}`` frame carries the
    same payload the JSON response used to.

    Lock choreography is unchanged (it was already correct): Zotero reads run
    BEFORE the op-lock (per op, the D4 discipline), then ONE hold reverts note
    / finder / Zotero ops with a per-op heartbeat that is acted on. Per-op
    conservatism is unchanged — a store edited since the retire skips rather
    than clobbering later work.

    Validation errors still surface as JSON + a real status code (they are
    decided before any streaming begins); only the long-running phase streams.
    """
    try:
        vault_root = _vault_root()
    except AuditConfigError as exc:
        return jsonify({"error": str(exc)}), 400
    cfg = load_config()
    from rag.vault import obsidian_manager

    try:
        # An indexed SELECT, not a whole-history parse: this peek only decides a
        # 404 and which Zotero reads phase 1 must do. It returns PLAIN dicts and
        # is never handed to save() — the real manifest is re-loaded under the
        # op-lock below (2026-09-02; see journal._scoped_ops).
        batch_ops = [op for op in journal.peek_batch_ops(vault_root, cfg, batch_id)
                     if op.get("state") in ("applied", "outcome_unknown", "observed_after_recovery")]
    except (journal.ScopeError, journal.JournalUnreadableError,
            journal.JournalTransientReadError) as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 503
    if not batch_ops:
        return jsonify({"error": "No unresolved ops carry that batch id."}), 404

    consumer_timeout_s = SSE_SINGLE_SHOT_FLOOR_S + SSE_STALL_MARGIN_S

    def _worker(put, cancel):
        # PERF (2026-09-02): batch restore PATCHed Zotero serially under the
        # obsidian op-lock.
        #   Before:    phase 2 held the lock across every revert INCLUDING each
        #              Zotero op's PATCH (and, for a `zotero_attachment_trash`
        #              op, a GET as well). Each is a network write with a
        #              15 s timeout, so a 50-op batch against an unreachable
        #              Zotero froze indexing and every other vault write for
        #              50 x 15 s = 750 s (>12 min) in one hold. Retire-tag and
        #              harmonize were refactored away from exactly this in the
        #              2026-07-23 audit; batch restore never was.
        #   Change:    retire-tag's four phases. 1: every Zotero READ lock-free
        #              (the tag reads already were; the attachment GET now is
        #              too, via the new prepare/commit split in
        #              tagging/writers.py). 2: ONE hold for the vault-local
        #              reverts (note / finder) and the manifest save. 3: every
        #              Zotero PATCH lock-free. 4: a short second hold that
        #              RELOADS the manifest, records the phase-3 outcomes and
        #              prunes. Worst-case lock hold is now the local reverts
        #              only: zero network time, whatever Zotero is doing.
        #   Safe because:
        #              the lock never protected the Zotero round-trip in the
        #              first place — `If-Unmodified-Since-Version` does, and it
        #              is unchanged. Phase 4 reloads rather than saving the
        #              phase-2 object, so a writer that took the lock in the gap
        #              is never clobbered; the state flips are carried across by
        #              op id. Result ORDER is preserved: results are collected
        #              per op id and emitted in the original newest-first
        #              sequence, so the terminal frame is byte-identical to
        #              before for any batch that succeeds.
        #   Preserves: per-op conservatism — a store edited since the write is
        #              still `skipped`, and an unreachable Zotero is still an
        #              `error` that KEEPS the op `applied` and therefore
        #              retryable (never a "reverted" claim for a PATCH that did
        #              not land). "Zotero reads run BEFORE the op-lock (per op,
        #              the D4 discipline)" now covers the attachment op too.
        #   Lock/thread contract:
        #              entry: no lock. Phase 2 acquires and releases the
        #              obsidian op-lock (heartbeat still acted on). Phase 3
        #              holds nothing. Phase 4 re-acquires with a bounded retry
        #              and releases; failing to re-acquire leaves the affected
        #              ops `applied`, which a re-run resolves conservatively.
        #   Pinned by: test_tagging.py::TestBatchRestoreLockChoreography
        total_ops = len(batch_ops)
        # Phase 1 — Zotero reads, lock-free (a wedged Zotero must not stall
        # indexing or any other vault write).
        z_prepared: dict[str, dict] = {}
        zotero_ops = [op for op in batch_ops
                      if op.get("kind") in ("zotero_tag", "zotero_untag",
                                            "zotero_attachment_trash",
                                            "zotero_attachment_create",
                                            "zotero_attachment_repath",
                                            "zotero_attachment_retitle",
                                            "zotero_item_create",
                                            "zotero_metadata_write",
                                            "zotero_child_reparent",
                                            "zotero_item_trash")
                      or op.get("kind") in _NOTESYNC_ZOTERO_KINDS]
        for i, op in enumerate(zotero_ops, start=1):
            # `cancel` is the skeleton's threading.Event, never a callable —
            # `cancel()` raised TypeError here and _runner's blanket handler
            # turned it into an {"error"} frame BEFORE phase 2, so a batch
            # containing any Zotero op reverted nothing at all. Every other
            # worker in this file uses .is_set(); this was the odd one out.
            if cancel.is_set():
                return
            put({"info": f"Zotero: reading {i}/{len(zotero_ops)}…"})
            z_prepared[op["op_id"]] = _prepare_zotero_revert(cfg, op)

        # Results are keyed by op id and emitted in `ordered` sequence at the
        # end, so splitting the loop across three phases cannot reorder the
        # terminal frame.
        by_id: dict[str, dict] = {}
        state_flips: dict[str, str] = {}
        done_count = 0

        def _record(op, result):
            nonlocal done_count
            kind = op.get("kind")
            by_id[op["op_id"]] = {"op_id": op["op_id"], "kind": kind, **result}
            done_count += 1
            put({"info": f"[{done_count}/{total_ops}] {kind or 'op'}: "
                         f"{result.get('status') or 'done'}"})

        # Phase 2 — one lock hold for every VAULT-LOCAL revert + the save.
        op_epoch = obsidian_manager.try_acquire_lock(ttl=3600)
        if not op_epoch:
            put({"error": "Indexing is in progress; try again shortly."})
            return
        lock_lost = False
        ordered: list[dict] = []
        try:
            try:
                manifest = journal.load(vault_root, cfg)
            except (journal.ScopeError, journal.JournalUnreadableError) as exc:
                put({"error": sanitise_error_msg(exc)})
                return
            # Newest-first so a later op's revert can't be invalidated by an
            # earlier one's (independent stores make order mostly moot; this is
            # the safe default).
            ordered = [op for op in reversed(manifest["ops"])
                       if op.get("batch_id") == batch_id and op.get("state") in ("applied", "outcome_unknown", "observed_after_recovery")]
            for op in ordered:
                if op.get("op_id") in z_prepared:
                    continue  # phase 3 owns it — no network under this lock
                # Act on a False heartbeat (CLAUDE.md Limits discipline): a
                # lapsed TTL means another writer may own the lock — stop
                # reverting rather than write into someone else's hold.
                # Already-reverted ops were flipped in-memory; the save below
                # goes through the prefer-ours merge so those flips persist
                # WITHOUT clobbering ops a new lock owner appended during the
                # lapse (2026-08-18 — the plain save here was the same clobber
                # class the write path's merge-save fixed).
                if not obsidian_manager.heartbeat(op_epoch):
                    lock_lost = True
                    _record(op, {"ok": False, "status": "error",
                                 "message": "op-lock TTL lapsed; batch restore "
                                            "aborted — retry to revert the rest"})
                    put({"info": "op-lock TTL lapsed — stopping; re-run to revert the rest."})
                    break
                kind = op.get("kind")
                try:
                    if kind in ("finder_tag", "finder_untag"):
                        result = _revert_finder_tag(vault_root, cfg, op)
                    else:
                        result = journal.revert_op(vault_root, cfg, op)
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "status": "error",
                              "message": sanitise_error_msg(exc)}
                _record(op, result)
            if lock_lost:
                # prefer_ours: disk-wins would discard the in-memory
                # `reverted` flips (leaving reverted stores double-revertible)
                # while a plain save would drop a new owner's ops. Prefer OUR
                # copies for exactly this batch's ops, nothing else.
                try:
                    journal.save_merged(
                        vault_root, cfg, manifest,
                        prefer_ours={op["op_id"] for op in ordered})
                except journal.JournalTransientReadError:
                    put({"info": "could not persist the reverted flags "
                                 "(journal volume unreadable); the reverted "
                                 "ops still show as applied — a re-run will "
                                 "conservatively re-check each store."})
            else:
                journal.save(vault_root, cfg, manifest)
        finally:
            obsidian_manager.release_lock(op_epoch)

        # Phase 3 — Zotero PATCHes, lock-free. A wedged Zotero now costs this
        # batch its own wall clock and nothing else; indexing runs throughout.
        touched_items: set = set()
        if not lock_lost:
            for op in ordered:
                if op.get("op_id") not in z_prepared:
                    continue
                if cancel.is_set():
                    break
                prepared = z_prepared[op["op_id"]]
                # Two ops on ONE item in a batch (a record merge writes the
                # kept item's tags AND its Extra): the phase-1 read's version
                # is stale once the first of them has been PATCHed, and the
                # second would 412. Re-read — still lock-free — before it.
                # Found on the disposable copy, 2026-09-17.
                if op.get("item_key") in touched_items and "commit" in prepared:
                    prepared = _prepare_zotero_revert(cfg, op)
                is_attachment = op.get("kind") == "zotero_attachment_trash"
                try:
                    if "early" in prepared:
                        result = prepared["early"]
                        if prepared.get("state"):
                            state_flips[op["op_id"]] = prepared["state"]
                    elif "commit" not in prepared:
                        result = {"ok": False, "status": "error",
                                  "message": "Zotero read failed."}
                    else:
                        # The commit helpers flip `op["state"]` on THIS object,
                        # which belongs to the phase-2 manifest we are no longer
                        # going to save; the flip is carried to the reloaded
                        # manifest in phase 4 by op id.
                        if is_attachment:
                            result = _revert_zotero_attachment_commit(prepared, op)
                        elif op.get("kind") == "zotero_attachment_create":
                            result = _revert_zotero_attachment_create_commit(prepared, op)
                        elif op.get("kind") == "zotero_item_create":
                            result = _revert_zotero_item_create_commit(prepared, op)
                        elif op.get("kind") == "zotero_attachment_repath":
                            result = _revert_zotero_attachment_repath_commit(prepared, op)
                        elif op.get("kind") == "zotero_attachment_retitle":
                            result = _revert_zotero_attachment_retitle_commit(prepared, op)
                        elif op.get("kind") == "zotero_metadata_write":
                            result = _revert_zotero_metadata_commit(prepared, op)
                        elif op.get("kind") == "zotero_child_reparent":
                            result = _revert_zotero_child_reparent_commit(prepared, op)
                        elif op.get("kind") == "zotero_item_trash":
                            result = _revert_zotero_item_trash_commit(prepared, op)
                        elif op.get("kind") in _NOTESYNC_ZOTERO_KINDS:
                            from notesync import zotero_notes as _zn
                            result = _zn.revert_commit(prepared, op)
                        else:
                            result = _revert_zotero_commit_locked(prepared, op)
                        if op.get("state") == "reverted":
                            state_flips[op["op_id"]] = "reverted"
                        if op.get("item_key"):
                            touched_items.add(op["item_key"])
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "status": "error",
                              "message": sanitise_error_msg(exc)}
                _record(op, result)

        # Phase 4 — a short second hold to RECORD the phase-3 outcomes. Never
        # saves the phase-2 object: it reloads, so a writer that took the lock
        # in the gap keeps its ops. Failing to re-acquire leaves those ops
        # `applied`, which is the conservative reading — a re-run re-reads the
        # store, sees it already matches `before`, and answers `skipped`.
        journal_recorded = not lock_lost
        if state_flips:
            epoch2 = None
            for _ in range(20):
                epoch2 = obsidian_manager.try_acquire_lock(ttl=300)
                if epoch2:
                    break
                time.sleep(1)
            if not epoch2:
                journal_recorded = False
                put({"info": "could not re-acquire the op-lock to record the "
                             "Zotero reverts; they still show as applied — a "
                             "re-run will conservatively re-check each item."})
            else:
                try:
                    m2 = journal.load(vault_root, cfg)
                    for op in m2["ops"]:
                        flip = state_flips.get(op.get("op_id"))
                        if flip and op.get("state") in ("applied", "outcome_unknown", "observed_after_recovery"):
                            op["state"] = flip
                    journal.save(vault_root, cfg, m2)
                except (journal.ScopeError, journal.JournalUnreadableError,
                        journal.JournalTransientReadError) as exc:
                    journal_recorded = False
                    put({"info": "could not persist the Zotero reverted flags: "
                                 f"{sanitise_error_msg(exc)}"})
                finally:
                    obsidian_manager.release_lock(epoch2)

        results = [by_id.get(op["op_id"], {"op_id": op["op_id"], "kind": op.get("kind"),
                   "ok": False, "status": "not_attempted", "message": "Restore stopped before this operation."})
                   for op in ordered]
        reverted = sum(1 for r in results if r.get("status") == "reverted")
        unresolved = sum(1 for r in results if r.get("status") not in ("reverted", "already_reverted")
                         and state_flips.get(r["op_id"]) != "reverted")
        put({"restore": {"ok": unresolved == 0 and journal_recorded and not cancel.is_set(),
                         "batch_id": batch_id, "reverted": reverted, "total": len(results),
                         "unresolved": unresolved, "journal_recorded": journal_recorded,
                         "cancelled": cancel.is_set(), "results": results}})

    return run_sse_worker(_worker, consumer_timeout_s=consumer_timeout_s)


def _flip_early(vault_root, cfg, op_id, state):
    """Persist an early-exit state flip (e.g. a 404 that means "gone for good")
    for the single-op restore, which otherwise returns before the op-lock."""
    if not state:
        return
    from rag.vault import obsidian_manager
    epoch = obsidian_manager.try_acquire_lock(ttl=300)
    if not epoch:
        return
    try:
        manifest = journal.load(vault_root, cfg)
        op = journal.find_op(manifest, op_id)
        if op is not None and op.get("state") != state:
            op["state"] = state
            journal.save(vault_root, cfg, manifest)
    except Exception:  # noqa: BLE001 — a missed flip only leaves the op retryable
        logger.exception("could not persist an early restore state")
    finally:
        obsidian_manager.release_lock(epoch)


@tagging_bp.route("/api/tagging/restore", methods=["POST"])
def api_tagging_restore():
    """Revert one tagging op by id. Body ``{op_id, confirm:true}``.

    Confirm-gated and under the obsidian op-lock, like the write. Delegates to
    ``journal.revert_op``, which is conservative: it refuses (``skipped``) when
    the note was edited after the tag write rather than clobber later work. The
    manifest is persisted whether the revert succeeds or is skipped.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400
    if coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "Restore must be explicitly confirmed (confirm: true)."}), 400
    # Batch restore (Batch G, 2026-07-22): {batch_id, confirm} reverts every
    # applied op a retire-tag run stamped, newest-first (its own handler —
    # the single-op flow below stays byte-identical).
    #
    # NOTE (2026-07-30): the batch branch answers with **SSE**, the single-op
    # branch with JSON. Same URL, two content types, decided by the body —
    # deliberate, because the batch is the only long-running one and the whole
    # point of Plan 5 was per-op progress. Safe for the other client: the
    # CLI/MCP driver's `tagging_restore` only ever sends `op_id` (pinned by
    # tests/test_driver_contracts.py + test_driver_replay.py). A validation
    # failure below still returns JSON + a status code either way.
    batch_id = coerce_non_empty_string(data.get("batch_id"), max_len=64)
    if batch_id and not data.get("op_id"):
        return _restore_batch(batch_id)
    op_id = coerce_non_empty_string(data.get("op_id"), max_len=64)
    if not op_id:
        return jsonify({"error": "op_id required"}), 400

    try:
        vault_root = _vault_root()
    except AuditConfigError as exc:
        return jsonify({"error": str(exc)}), 400
    cfg = load_config()

    from rag.vault import obsidian_manager

    # D4 (2026-07-20 audit): for a zotero_tag revert, do the Zotero READ (and any
    # `Backoff` sleep) BEFORE taking the obsidian op-lock — a slow/wedged Zotero
    # must not stall indexing/other vault writes while we hold the lock. The
    # PATCH's If-Unmodified-Since-Version guard makes the early read safe. A
    # peek at the op kind needs the manifest, which is a cheap read; we re-load
    # it under the lock for the actual write so the save stays single-writer.
    try:
        # One indexed SELECT rather than parsing the whole journal for one op
        # (2026-09-02). PLAIN dict, never saved; the write below re-loads the
        # real manifest under the op-lock.
        peek_op = journal.peek_op(vault_root, cfg, op_id)
    except journal.ScopeError as exc:
        return jsonify({"error": str(exc)}), 400
    except (journal.JournalUnreadableError,
            journal.JournalTransientReadError) as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 503
    if peek_op is None:
        return jsonify({"error": "Unknown op id."}), 404

    zotero_prepared = None
    # zotero_untag (2026-07-22) stores the same before/after fields, so the
    # split revert handles both kinds — restoring `before_raw` re-adds the
    # removed tags exactly as it un-adds added ones.
    if peek_op.get("kind") in ("zotero_tag", "zotero_untag"):
        # Runs OUTSIDE the lock — this is the only network read + possible sleep.
        zotero_prepared = _revert_zotero_prepare(cfg, peek_op)
        if "early" in zotero_prepared:
            # Nothing to write (unreachable / changed-in-Zotero / no key): return
            # without ever taking the op-lock, so a wedged Zotero can't stall it.
            return jsonify({**zotero_prepared["early"], "op_id": op_id})
    elif peek_op.get("kind") in _NOTESYNC_ZOTERO_KINDS:
        # Note sync's Zotero note ops (2026-09-04): same D4 split, read pre-lock.
        from notesync import zotero_notes as _zn
        zotero_prepared = _zn.revert_prepare(cfg, peek_op)
        if "early" in zotero_prepared:
            return jsonify({**zotero_prepared["early"], "op_id": op_id})
    elif peek_op.get("kind") == "zotero_metadata_write":
        # Bibliography-campaign metadata fields (2026-09-16): same split.
        zotero_prepared = _revert_zotero_metadata_prepare(cfg, peek_op)
        if "early" in zotero_prepared:
            return jsonify({**zotero_prepared["early"], "op_id": op_id})
    elif peek_op.get("kind") in ("zotero_child_reparent", "zotero_item_trash"):
        # Record merge (2026-09-17): same split, read pre-lock.
        fn = (_revert_zotero_child_reparent_prepare if peek_op.get("kind") == "zotero_child_reparent"
              else _revert_zotero_item_trash_prepare)
        zotero_prepared = fn(cfg, peek_op)
        if "early" in zotero_prepared:
            _flip_early(vault_root, cfg, op_id, zotero_prepared.get("state"))
            return jsonify({**zotero_prepared["early"], "op_id": op_id})

    op_epoch = obsidian_manager.try_acquire_lock(ttl=3600)
    if not op_epoch:
        return jsonify({"error": "Indexing is in progress; try again shortly."}), 503
    try:
        try:
            manifest = journal.load(vault_root, cfg)
        except journal.ScopeError as exc:
            return jsonify({"error": str(exc)}), 400
        except journal.JournalUnreadableError as exc:
            return jsonify({"error": sanitise_error_msg(exc)}), 503
        op = journal.find_op(manifest, op_id)
        if op is None:
            return jsonify({"error": "Unknown op id."}), 404
        kind = op.get("kind")
        # note_tag reverts are self-contained in the journal (vault-note bytes);
        # external-system reverts (Finder xattr, Zotero API) live in the route
        # layer so the journal never imports api/finder/Zotero code.
        if kind in ("finder_tag", "finder_untag"):
            result = _revert_finder_tag(vault_root, cfg, op)
        elif kind == "zotero_attachment_trash":
            # Restores the attachment from Zotero's trash. One network call, so
            # it rides the same locked path as a tag revert rather than needing
            # the prepare/commit split (there is nothing to read first).
            result = _revert_zotero_attachment(vault_root, cfg, op)
        elif kind == "zotero_attachment_create":
            # The inverse of a linked-file create is a trash (never a DELETE).
            result = _revert_zotero_attachment_create(vault_root, cfg, op)
        elif kind == "zotero_item_create":
            # The inverse of an item create is a trash of the parent. It runs
            # AFTER the attachment op in a batch (ops revert newest-first, and
            # the attachment is journalled second), so by the time it fires our
            # own child is already gone.
            result = _revert_zotero_item_create(vault_root, cfg, op)
        elif kind == "zotero_attachment_repath":
            # Put the stored path back — exactly the string Zotero held, not
            # one recomputed here.
            result = _revert_zotero_attachment_repath(vault_root, cfg, op)
        elif kind == "zotero_attachment_retitle":
            # Put the title back, unless it was retitled again after us.
            result = _revert_zotero_attachment_retitle(vault_root, cfg, op)
        elif kind in ("zotero_tag", "zotero_untag"):
            # The read already ran pre-lock; only the guarded PATCH runs here.
            result = _revert_zotero_commit_locked(zotero_prepared, op)
        elif kind in _NOTESYNC_ZOTERO_KINDS:
            from notesync import zotero_notes as _zn
            result = _zn.revert_commit(zotero_prepared, op)
        elif kind == "zotero_metadata_write":
            result = _revert_zotero_metadata_commit(zotero_prepared, op)
        elif kind == "zotero_child_reparent":
            result = _revert_zotero_child_reparent_commit(zotero_prepared, op)
        elif kind == "zotero_item_trash":
            result = _revert_zotero_item_trash_commit(zotero_prepared, op)
        else:
            result = journal.revert_op(vault_root, cfg, op)
        journal.save(vault_root, cfg, manifest)
        return jsonify({**result, "op_id": op_id})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    finally:
        obsidian_manager.release_lock(op_epoch)


# ---------------------------------------------------------------------------
# Fan-out write (triage deck): one confirmed decision → all three targets
# ---------------------------------------------------------------------------

@tagging_bp.route("/api/tagging/apply-all", methods=["POST"])
def api_tagging_apply_all():
    """Fan one confirmed tag decision out to all three targets under one lock.

    Body ``{citation_key, tags:[...], targets?:{note,finder,zotero}, content_sha256?,
    confirm:true}``. Each enabled+available target is written by the SAME
    per-target helper the standalone endpoints use, so each still journals its
    own op (Restore stays per-target granular) and re-validates against the
    vocabulary. A target that is absent (no note / no Zotero item / no biblio
    PDF), off-platform (Finder off macOS) or unreachable (Zotero) is *skipped*
    with a reason rather than failing the whole write — the primary note write
    still lands. A target key omitted from ``targets`` defaults ON. Returns
    ``{citation_key, results:{note,finder,zotero}}`` (HTTP 200); a per-target
    write error is carried inside its sub-result.

    ``unit`` (optional, library-workflow lot 3): an import-tracker attachment
    id. The Finder target is then THAT PDF only — not every PDF of the record
    — found by identity, so a rename since the scan does not matter. A record
    with two PDFs needs one call per unit for its Finder tags; the note and
    Zotero targets are per record and simply converge (unions).
    """
    data = request.get_json(silent=True)
    ctx, err = _prepare_tag_write(data)
    if err is not None:
        body, status = err
        return jsonify(body), status

    raw_targets = data.get("targets") if isinstance(data, dict) else None
    if not isinstance(raw_targets, dict):
        raw_targets = {}
    # A target is ON unless the body explicitly sets it false (omitted ⇒ on).
    want = {k: (coerce_bool(raw_targets.get(k)) is not False) for k in TAGGING_TARGETS}
    want_sha = coerce_string_max_len(data.get("content_sha256"), 64)

    rec = ctx["rec"]
    results: dict = {}

    # Library-workflow lot 3: the maintenance run names the PDF by its import
    # tracker UNIT, because a rename made earlier in the same run leaves the
    # cached scan's path dead. Resolved before the lock (a stat, no I/O on
    # the manifest); an unusable unit fails ONLY the Finder target.
    unit_pdf, unit_err, unit_doc = (None, None, None)
    if isinstance(data, dict) and data.get("unit") is not None and want["finder"]:
        unit_pdf, unit_err = _unit_pdf(data.get("unit"), rec, ctx)
        if unit_pdf is not None:
            # Re-checked by the writer UNDER the lock, right before it reads
            # the xattr: this pre-lock check alone left a window (the Zotero
            # prepare phase, the lock wait) in which a replaced file would
            # have been tagged (review 2026-09-21).
            unit_doc = (imports_tracker.unit(ctx["settings"], data.get("unit")) or {}).get("doc")

    from api.routes import _finder_tagwrite
    from rag.vault import obsidian_manager

    # Zotero READ phase before the lock (same rationale as the standalone
    # endpoint): its up-to-15 s GET must not extend the one lock hold that
    # also covers the note + Finder writes. The version-guarded PATCH in the
    # commit phase is what makes the early read safe.
    zotero_prepared = None
    zotero_early = None
    if want["zotero"] and rec.zotero_item is not None:
        try:
            zotero_prepared, z_result, z_status = _zotero_prepare(
                rec=rec, valid=ctx["valid"], rejected=ctx["rejected"],
                cfg=ctx["cfg"], citation_key=ctx["citation_key"])
            if zotero_prepared is None:
                # Carry the underlying detail: a 503 covers both "Zotero not
                # running" AND "web transport with no API key" — flattening it
                # to the bare "unreachable" hid the actionable configure-a-key
                # message (2026-07-23 audit).
                zotero_early = ({"ok": False, "skipped": "unreachable",
                                 "detail": (z_result or {}).get("error")}
                                if z_status == 503
                                else _target_result(z_result, z_status))
                zotero_early["write_not_started"] = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("apply-all zotero prepare failed for %s",
                           ctx["citation_key"], exc_info=True)
            zotero_early = {"ok": False, "error": sanitise_error_msg(exc), "write_not_started": True}

    op_epoch = obsidian_manager.try_acquire_lock(ttl=3600)
    if not op_epoch:
        return jsonify({"error": "Indexing is in progress; try again shortly."}), 503

    # ONE manifest load + ONE prune for the whole fan-out. Standalone, each
    # helper loads/prunes/saves its own copy — fine for one write, but three
    # full read→prune→rewrite cycles of a 200-op indented-JSON manifest per
    # Accept was redundant I/O inside the critical section on the deck's
    # hottest path. Each helper still SAVES after appending its op (the
    # crash-trail: the record is durable before its target write runs).
    try:
        shared_manifest = journal.load(ctx["vault_root"], ctx["cfg"])
        # Ops already on disk; anything appended past this point belongs to
        # THIS request and must survive its own end-of-request prune.
        _pre_ids = {o.get("op_id") for o in shared_manifest["ops"]}
    except journal.ScopeError as exc:
        obsidian_manager.release_lock(op_epoch)
        return jsonify({"error": f"Tagging archive misconfigured: {exc}"}), 400
    except journal.JournalUnreadableError as exc:
        obsidian_manager.release_lock(op_epoch)
        return jsonify({"error": sanitise_error_msg(exc)}), 503

    def _isolated(target: str, fn):
        """Run one target's write; an unexpected crash stays IN that target.

        A shared try around all three targets turned a late-target crash into
        an HTTP 500 AFTER an earlier target had already committed + journalled
        — the client saw total failure (and never learned the note op's id for
        its undo stack) while a write had permanently landed. Per-target
        isolation keeps the response a truthful per-target report (HTTP 200).
        """
        try:
            results[target] = fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("apply-all %s target failed for %s", target,
                           ctx["citation_key"], exc_info=True)
            results[target] = {"ok": False, "error": sanitise_error_msg(exc)}

    try:
        if want["note"]:
            if rec.obsidian_note is None:
                results["note"] = {"ok": False, "skipped": "no_note"}
            else:
                def _note():
                    r, s = _note_write_locked(
                        rec=rec, valid=ctx["valid"], rejected=ctx["rejected"], want_sha=want_sha,
                        cfg=ctx["cfg"], vault_root=ctx["vault_root"], citation_key=ctx["citation_key"],
                        manifest=shared_manifest)
                    return _target_result(r, s)
                _isolated("note", _note)
                # DELIBERATE: a note failure (incl. a 409 stale-diff) does NOT
                # abort the remaining targets. The three writes are independent
                # unions of the same vocabulary-validated tag set — the sha
                # guards the note FILE's bytes, not the tag decision — and
                # Finder/Zotero tags have their own conservative guards
                # (superset skip, If-Unmodified-Since-Version). Aborting would
                # leave the deck's one-confirm contract ("Accept writes every
                # enabled target") silently unmet. Today the deck sends no
                # content_sha256, so the 409 arm is future-caller-only.
        if want["finder"]:
            if not _finder_tagwrite.is_macos():
                results["finder"] = {"ok": False, "skipped": "not_macos"}
            elif unit_err is not None:
                results["finder"] = unit_err
            else:
                def _finder():
                    r, s = _finder_write_locked(
                        rec=rec, valid=ctx["valid"], rejected=ctx["rejected"], cfg=ctx["cfg"],
                        vault_root=ctx["vault_root"], biblio_dir=ctx["biblio_dir"],
                        citation_key=ctx["citation_key"], manifest=shared_manifest,
                        pdf_paths=[unit_pdf] if unit_pdf is not None else None,
                        expect_doc=unit_doc)
                    return ({"ok": False, "skipped": "no_pdf"} if s == 404
                            else _target_result(r, s))
                _isolated("finder", _finder)
        if want["zotero"]:
            if rec.zotero_item is None:
                results["zotero"] = {"ok": False, "skipped": "no_item"}
            elif zotero_early is not None:
                results["zotero"] = zotero_early
            else:
                def _zotero():
                    r, s = _zotero_commit_locked(
                        prepared=zotero_prepared, rejected=ctx["rejected"], cfg=ctx["cfg"],
                        vault_root=ctx["vault_root"], citation_key=ctx["citation_key"],
                        manifest=shared_manifest)
                    return ({"ok": False, "skipped": "unreachable",
                             "detail": (r or {}).get("error")} if s == 503
                            else _target_result(r, s))
                _isolated("zotero", _zotero)
        # The batch's single prune + final save (best-effort: every op record
        # is already durable from the per-append saves above).
        try:
            journal.prune(
                ctx["vault_root"], ctx["cfg"], shared_manifest,
                protect_op_ids={o.get("op_id") for o in shared_manifest["ops"]}
                - _pre_ids)
            journal.save(ctx["vault_root"], ctx["cfg"], shared_manifest)
        except Exception:  # noqa: BLE001
            logger.warning("apply-all manifest prune/save failed", exc_info=True)
    finally:
        obsidian_manager.release_lock(op_epoch)

    return jsonify({"citation_key": ctx["citation_key"], "results": results}), 200


# ---------------------------------------------------------------------------
# Fan-out tag REMOVAL (2026-07-22): one confirmed deletion → chosen targets
# ---------------------------------------------------------------------------

@tagging_bp.route("/api/tagging/remove-tags", methods=["POST"])
def api_tagging_remove_tags():
    """Remove tags from the chosen targets under one lock (apply-all's twin).

    Body ``{citation_key, tags:[...], targets?:{note,finder,zotero},
    content_sha256?, confirm:true}``. Validation differs from apply-all in ONE
    deliberate way: the tags are NOT intersected with the ``_tags.md``
    vocabulary — a removal targets what is actually present (often a tag that
    already fell out of the vocabulary), and each per-target writer skips
    targets that don't carry it. Every removal is journalled as its own
    reversible ``*_untag`` op (Restore re-adds, colours/metadata included).
    Same per-target isolation + skip-with-reason semantics as apply-all
    (HTTP 200 with per-target truth).
    """
    data = request.get_json(silent=True)
    ctx, err = _prepare_tag_removal(data)
    if err is not None:
        body, status = err
        return jsonify(body), status

    raw_targets = data.get("targets") if isinstance(data, dict) else None
    if not isinstance(raw_targets, dict):
        raw_targets = {}
    want = {k: (coerce_bool(raw_targets.get(k)) is not False) for k in TAGGING_TARGETS}
    want_sha = coerce_string_max_len(data.get("content_sha256"), 64)

    rec = ctx["rec"]
    results: dict = {}

    # `unit` addressing (library-workflow lot 3) is apply-all's only; a
    # removal naming one would otherwise be silently resolved from the scan
    # path instead — refuse rather than remove from a file nobody named.
    if isinstance(data, dict) and data.get("unit") is not None:
        return jsonify({"error": "unit is accepted by apply-all only; "
                                 "use pdf_rel to address a file here."}), 400

    from api.routes import _finder_tagwrite
    from rag.vault import obsidian_manager

    def _removal_result(r, s):
        if s == 200:
            return {"ok": True, "removed": r.get("removed", []),
                    "op_id": r.get("op_id"), "message": r.get("message"),
                    **({"partial": True, "failed": r.get("failed", [])} if r.get("partial") else {})}
        return {"ok": False, "error": r.get("error"), "status": s}

    # Zotero READ phase before the lock (apply-all's rationale verbatim).
    zotero_prepared = None
    zotero_early = None
    if want["zotero"] and rec is not None and rec.zotero_item is not None:
        try:
            zotero_prepared, z_result, z_status = _zotero_remove_prepare(
                rec=rec, valid=ctx["valid"], cfg=ctx["cfg"],
                citation_key=ctx["citation_key"])
            if zotero_prepared is None:
                # See apply-all's twin: keep the actionable 503 detail (no-key
                # vs genuinely unreachable) instead of a bare "unreachable".
                zotero_early = ({"ok": False, "skipped": "unreachable",
                                 "detail": (z_result or {}).get("error")}
                                if z_status == 503
                                else _removal_result(z_result, z_status))
        except Exception as exc:  # noqa: BLE001
            logger.warning("remove-tags zotero prepare failed for %s",
                           ctx["citation_key"], exc_info=True)
            zotero_early = {"ok": False, "error": sanitise_error_msg(exc)}

    op_epoch = obsidian_manager.try_acquire_lock(ttl=3600)
    if not op_epoch:
        return jsonify({"error": "Indexing is in progress; try again shortly."}), 503

    try:
        shared_manifest = journal.load(ctx["vault_root"], ctx["cfg"])
        # Ops already on disk; anything appended past this point belongs to
        # THIS request and must survive its own end-of-request prune.
        _pre_ids = {o.get("op_id") for o in shared_manifest["ops"]}
    except journal.ScopeError as exc:
        obsidian_manager.release_lock(op_epoch)
        return jsonify({"error": f"Tagging archive misconfigured: {exc}"}), 400
    except journal.JournalUnreadableError as exc:
        obsidian_manager.release_lock(op_epoch)
        return jsonify({"error": sanitise_error_msg(exc)}), 503

    def _isolated(target: str, fn):
        try:
            results[target] = fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("remove-tags %s target failed for %s", target,
                           ctx["citation_key"], exc_info=True)
            results[target] = {"ok": False, "error": sanitise_error_msg(exc)}

    try:
        if want["note"]:
            # Path-addressed (rec is None): a note is reached THROUGH the
            # record, so with no record there is nothing to name. Same
            # skip-with-reason shape as an absent note on the keyed path.
            if rec is None:
                results["note"] = {"ok": False, "skipped": "no_record"}
            elif rec.obsidian_note is None:
                results["note"] = {"ok": False, "skipped": "no_note"}
            else:
                def _note():
                    r, s = _note_remove_locked(
                        rec=rec, valid=ctx["valid"], want_sha=want_sha,
                        cfg=ctx["cfg"], vault_root=ctx["vault_root"],
                        citation_key=ctx["citation_key"], manifest=shared_manifest)
                    return _removal_result(r, s)
                _isolated("note", _note)
        if want["finder"]:
            if not _finder_tagwrite.is_macos():
                results["finder"] = {"ok": False, "skipped": "not_macos"}
            else:
                def _finder():
                    r, s = _finder_remove_locked(
                        rec=rec, valid=ctx["valid"], cfg=ctx["cfg"],
                        vault_root=ctx["vault_root"], biblio_dir=ctx["biblio_dir"],
                        citation_key=ctx["citation_key"], manifest=shared_manifest,
                        pdf_paths=ctx.get("pdf_paths"))
                    return ({"ok": False, "skipped": "no_pdf"} if s == 404
                            else _removal_result(r, s))
                _isolated("finder", _finder)
        if want["zotero"]:
            if rec is None:
                results["zotero"] = {"ok": False, "skipped": "no_record"}
            elif rec.zotero_item is None:
                results["zotero"] = {"ok": False, "skipped": "no_item"}
            elif zotero_early is not None:
                results["zotero"] = zotero_early
            else:
                def _zotero():
                    r, s = _zotero_remove_commit_locked(
                        prepared=zotero_prepared, cfg=ctx["cfg"],
                        vault_root=ctx["vault_root"], citation_key=ctx["citation_key"],
                        manifest=shared_manifest)
                    return ({"ok": False, "skipped": "unreachable",
                             "detail": (r or {}).get("error")} if s == 503
                            else _removal_result(r, s))
                _isolated("zotero", _zotero)
        try:
            journal.prune(
                ctx["vault_root"], ctx["cfg"], shared_manifest,
                protect_op_ids={o.get("op_id") for o in shared_manifest["ops"]}
                - _pre_ids)
            journal.save(ctx["vault_root"], ctx["cfg"], shared_manifest)
        except Exception:  # noqa: BLE001
            logger.warning("remove-tags manifest prune/save failed", exc_info=True)
    finally:
        obsidian_manager.release_lock(op_epoch)

    return jsonify({"citation_key": ctx["citation_key"],
                    "pdf_rel": ctx.get("pdf_rel"), "results": results}), 200


# ---------------------------------------------------------------------------
# Vault-wide tag retirement (Batch G, 2026-07-22): one tag → every store
# ---------------------------------------------------------------------------

# Hoisted to api/routes/_batch_journal.py 2026-08-30 so the Zotero de-link
# route can bind the same checkpoint/heartbeat discipline rather than grow a
# third copy. Re-exported here: `api.routes.tagging._BatchJournalKeeper` is an
# established patch target.
from api.routes._batch_journal import _BatchJournalKeeper  # noqa: E402


@tagging_bp.route("/api/tagging/retire-tag", methods=["POST"])
def api_tagging_retire_tag():
    """Retire one tag from EVERY record's chosen stores + optionally `_tags.md` (SSE).

    Body ``{tag, targets?{note,finder,zotero}, include_vocab?, confirm:true}``.
    Driven from the Tag Hygiene report: candidates come from the cached scan
    snapshot (the report's own semantics), each removal re-checks live state
    via the Batch-B writers (skip-if-absent), and every op is journalled with
    a shared ``batch_id`` so ``POST /restore {batch_id}`` reverts the whole
    retire. Lock choreography (2026-07-23): Zotero reads run BEFORE the
    op-lock (one GET per item) and the Zotero PATCHes run AFTER it (phase 3,
    lock-free — the version guard, not the lock, prevents lost updates; see
    ``_zotero_remove_journal``); vault-local writes + all journal appends run
    under one hold (phase 2, per-record heartbeat, manifest checkpointed
    every 25 ops), and a second short hold (phase 4) records PATCH outcomes
    + prunes with the batch protected from cap-eviction.
    ``include_vocab`` also removes the ``#tag`` token from ``_tags.md``
    (journalled snapshot, kind ``vocab_retire`` — reversible like a note op).
    No vocabulary gate (removal targets what exists). Terminal frame:
    ``{"retire": {tag, batch_id, results, vocab_rows, failures}}``.

    **Finder coverage (2026-08-12).** The candidate loop below reads
    ``inv.records``, so it could only ever see PDFs the bridge mapped to a
    citation key — a retire left the tag in place on the ~1250 unmapped /
    ambiguous / not-in-bib biblio PDFs, the same blindness Tag Hygiene had on
    the read side. Those PDFs are now enumerated by a direct xattr sweep and
    written through the PATH-addressed form of ``_finder_remove_locked``, so
    there is still exactly one Finder write path and their ops revert
    unchanged. Still out of reach, by design: Zotero child-note tags and
    ``.bib`` keywords (see Known Issues).
    """
    import uuid as _uuid

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400
    if coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "Tag retirement must be explicitly confirmed (confirm: true)."}), 400
    tag = coerce_non_empty_string(data.get("tag"), max_len=128)
    tag = (tag or "").strip().lstrip("#").strip()
    if not tag:
        return jsonify({"error": "tag required"}), 400
    raw_targets = data.get("targets")
    if not isinstance(raw_targets, dict):
        raw_targets = {}
    want = {k: (coerce_bool(raw_targets.get(k)) is not False) for k in TAGGING_TARGETS}
    include_vocab = coerce_bool(data.get("include_vocab")) is True

    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify({"error": "No scan results. Run an audit scan first."}), 404
    cfg = load_config()
    vault_root = settings.vault_root
    biblio_dir = settings.biblio_articles_dir
    # `_tag_key`, not `.lower()`: macOS stores Finder tags in NFD, and the
    # writers these candidates feed are NFC-insensitive. A `.lower()` sweep
    # therefore MISSED an NFD-stored spelling that `_finder_remove_locked`
    # would happily have removed — the tag survived while the terminal frame
    # reported a clean retire, which is the "floor presented as a total" shape
    # the unmapped-PDF work was written to end.
    tl = _tag_key(tag)

    note_keys: list[str] = []
    finder_keys: list[str] = []
    zotero_keys: list[str] = []
    for key, rec in inv.records.items():
        if want["note"] and rec.obsidian_note is not None \
                and tl in {_tag_key(t) for t in (rec.obs_tags or [])}:
            note_keys.append(key)
        if want["finder"] and tl in {_tag_key(t) for t in (rec.finder_tag_set or set())} \
                and any(_is_under(Path(p), biblio_dir) for p in rec.pdf_paths):
            finder_keys.append(key)
        if want["zotero"] and rec.zotero_item is not None \
                and tl in {_tag_key(t) for t in (rec.zotero_item.tags or [])}:
            zotero_keys.append(key)

    from api.routes import _finder_tagwrite
    from rag.vault import obsidian_manager

    # Unmapped biblio PDFs (2026-08-12). The loop above iterates `inv.records`,
    # the citation-key join, so a PDF the bridge never mapped is invisible to
    # it — the same blindness Tag Hygiene had on the READ side, here on the
    # WRITE side: `[retire]` silently left those copies of the tag in place.
    # These PDFs have no Record, so they are addressed by PATH, reusing the
    # `pdf_paths=` form of `_finder_remove_locked` rather than a new write path.
    # Enumerating means an xattr sweep of the residual buckets (~1250 files,
    # ~0.3 s) — paid once per retire, which is a confirmed write, not a poll.
    finder_pdfs: list[Path] = []
    if want["finder"] and _finder_tagwrite.is_macos():
        from audit.core import finder_tags as _ft
        bridge = getattr(inv, "bridge", None)
        residual: list[Path] = []
        for attr in ("unmapped_pdfs", "ambiguous_pdfs", "confirmed_no_match_pdfs"):
            # `ambiguous_pdfs` is a dict keyed by Path; iterating yields keys.
            residual.extend(p for p in (getattr(bridge, attr, None) or [])
                            if isinstance(p, Path))
        for p in residual:
            if not _is_under(p, biblio_dir):
                continue
            try:
                names = _ft.read_tag_names(p)
            except Exception:  # noqa: BLE001 — one unreadable PDF must not abort
                continue
            if tl in {_tag_key(t) for t in names}:
                finder_pdfs.append(p)

    if not _finder_tagwrite.is_macos():
        finder_keys = []
        finder_pdfs = []

    consumer_timeout_s = SSE_SINGLE_SHOT_FLOOR_S + SSE_STALL_MARGIN_S

    def _worker(put, cancel):
        batch_id = _uuid.uuid4().hex[:12]
        counts = {t: {"removed": 0, "skipped": 0, "failed": 0}
                  for t in TAGGING_TARGETS}
        failures: list[str] = []
        total = (len(note_keys) + len(finder_keys) + len(finder_pdfs)
                 + len(zotero_keys))
        unmapped_note = (f" (+{len(finder_pdfs)} unmapped PDF(s))"
                         if finder_pdfs else "")
        put({"info": f"Retiring '{tag}': {len(note_keys)} note(s), "
                     f"{len(finder_keys)} PDF record(s){unmapped_note}, "
                     f"{len(zotero_keys)} Zotero item(s) from the last scan."})

        # Phase 1 — Zotero READS before the lock (apply-all discipline).
        z_prepared: dict[str, dict | None] = {}
        for key in zotero_keys:
            if cancel.is_set():
                return
            put({"info": f"Zotero: reading {key}…"})
            try:
                prepared, _res, status = _zotero_remove_prepare(
                    rec=inv.records[key], valid=[tag], cfg=cfg, citation_key=key)
                z_prepared[key] = prepared
                if prepared is None:
                    if status == 200:
                        counts["zotero"]["skipped"] += 1
                    else:
                        counts["zotero"]["failed"] += 1
                        failures.append(f"zotero {key}: {(_res or {}).get('error', status)}")
            except Exception as exc:  # noqa: BLE001
                z_prepared[key] = None
                counts["zotero"]["failed"] += 1
                failures.append(f"zotero {key}: {sanitise_error_msg(exc)}")

        # Phase 2 — vault-local writes (notes, Finder xattrs, _tags.md) plus
        # the Zotero JOURNAL APPENDS under one lock hold. The Zotero PATCHes
        # themselves moved to phase 3, AFTER the lock is released: a PATCH
        # touches neither the vault nor the manifest, and the
        # If-Unmodified-Since-Version guard — not the op-lock — is what
        # prevents lost updates on the Zotero side (the same argument that
        # already justified the pre-lock GETs). Holding the lock across N
        # sequential network PATCHes froze indexing and every other vault
        # write for the whole batch (2026-07-23 audit). Phase 4 re-acquires
        # briefly to record the PATCH outcomes + prune.
        op_epoch = obsidian_manager.try_acquire_lock(ttl=3600)
        if not op_epoch:
            put({"error": "Indexing is in progress; try again shortly."})
            return
        vocab_rows = 0
        stamped: list[str] = []
        patch_jobs: list[tuple[str, dict, str]] = []  # (key, prepared, op_id)
        try:
            try:
                manifest = journal.load(vault_root, cfg)
            except (journal.ScopeError, journal.JournalUnreadableError) as exc:
                put({"error": sanitise_error_msg(exc)})
                return

            # batch_id is stamped INSIDE each writer (batch_id= kwarg), before
            # the writer's own append+save — an after-the-fact stamp here only
            # reached disk via the NEXT save, so a cancel/crash persisted the
            # last op applied-but-unstamped and dropped it from the batch
            # restore (2026-07-23 audit). `stamped` just tracks op ids now.
            def _stamp(op_id):
                if op_id:
                    stamped.append(op_id)

            # Checkpointing + heartbeat: the shared _BatchJournalKeeper (see
            # its docstring — refactor/apply.py's JOURNAL_FLUSH_EVERY
            # precedent, merge-save after a lost lock).
            keeper = _BatchJournalKeeper(
                vault_root=vault_root, cfg=cfg, manifest=manifest,
                op_epoch=op_epoch, put=put, total=total,
                manager=obsidian_manager)
            _bump_save, _tick = keeper.bump_save, keeper.tick

            for key in note_keys:
                if cancel.is_set() or keeper.lock_lost:
                    break
                _tick(f"note {key}")
                if keeper.lock_lost:
                    break
                try:
                    r, s = _note_remove_locked(
                        rec=inv.records[key], valid=[tag], want_sha=None,
                        cfg=cfg, vault_root=vault_root, citation_key=key,
                        manifest=manifest, batch_id=batch_id, defer_save=True)
                except Exception as exc:  # noqa: BLE001
                    r, s = {"error": sanitise_error_msg(exc)}, 500
                if s == 200 and r.get("removed"):
                    counts["note"]["removed"] += 1
                    _stamp(r.get("op_id"))
                    _bump_save()
                elif s == 200:
                    counts["note"]["skipped"] += 1
                else:
                    counts["note"]["failed"] += 1
                    failures.append(f"note {key}: {r.get('error')}")
                    _bump_save()  # a failed op record is still an op record

            for key in finder_keys:
                if cancel.is_set() or keeper.lock_lost:
                    break
                _tick(f"finder {key}")
                if keeper.lock_lost:
                    break
                try:
                    r, s = _finder_remove_locked(
                        rec=inv.records[key], valid=[tag], cfg=cfg,
                        vault_root=vault_root, biblio_dir=biblio_dir,
                        citation_key=key, manifest=manifest, batch_id=batch_id,
                        defer_save=True)
                except Exception as exc:  # noqa: BLE001
                    r, s = {"error": sanitise_error_msg(exc)}, 500
                if s == 200 and r.get("removed"):
                    counts["finder"]["removed"] += 1
                    _stamp(r.get("op_id"))
                    _bump_save()
                elif s in (200, 404):
                    counts["finder"]["skipped"] += 1
                else:
                    counts["finder"]["failed"] += 1
                    failures.append(f"finder {key}: {r.get('error')}")
                    _bump_save()

            # Unmapped biblio PDFs — same writer, addressed by path because
            # they have no Record. Tallied into the `finder` counters: from the
            # user's side it is one target, and splitting the count would imply
            # a distinction the confirm text never made.
            for pdf in finder_pdfs:
                if cancel.is_set() or keeper.lock_lost:
                    break
                _tick(f"finder {pdf.name}")
                if keeper.lock_lost:
                    break
                try:
                    r, s = _finder_remove_locked(
                        rec=None, valid=[tag], cfg=cfg,
                        vault_root=vault_root, biblio_dir=biblio_dir,
                        citation_key=None, manifest=manifest,
                        batch_id=batch_id, defer_save=True, pdf_paths=[pdf])
                except Exception as exc:  # noqa: BLE001
                    r, s = {"error": sanitise_error_msg(exc)}, 500
                if s == 200 and r.get("removed"):
                    counts["finder"]["removed"] += 1
                    _stamp(r.get("op_id"))
                    _bump_save()
                elif s in (200, 400, 404):
                    counts["finder"]["skipped"] += 1
                else:
                    counts["finder"]["failed"] += 1
                    failures.append(f"finder {pdf.name}: {r.get('error')}")
                    _bump_save()

            # Zotero: journal the ops now (append + one save with the rest),
            # PATCH after release. Journalling BEFORE the PATCH keeps the
            # crash/cancel trail conservative — an op whose PATCH never ran
            # reverts by re-adding tags that were never removed, an
            # idempotent no-op (see _zotero_remove_journal).
            for key in zotero_keys:
                if cancel.is_set() or keeper.lock_lost:
                    break
                prepared = z_prepared.get(key)
                if prepared is None:
                    # Tallied in phase 1; ticked so [done/total] can complete.
                    _tick(f"zotero {key} (skipped)")
                    continue
                op = _zotero_remove_journal(
                    prepared=prepared, citation_key=key,
                    manifest=manifest, batch_id=batch_id)
                patch_jobs.append((key, prepared, op["op_id"]))
                _bump_save()

            # Optional vocabulary retirement — a journalled _tags.md row edit.
            # Force-checkpoint first so the vocab block's own
            # journal-before-write save is not carrying unrelated deferred ops.
            _bump_save(force=True)
            if include_vocab and not cancel.is_set() and not keeper.lock_lost:
                from core.utils import log_vault_write, write_text_atomic
                from tagging.vocab import retire_tag_from_vocab
                _vocab, vocab_rel = _read_vocab(cfg, vault_root)
                if vocab_rel:
                    try:
                        vpath = vault_root / vocab_rel
                        raw = vpath.read_bytes()
                        text = raw.decode("utf-8")
                        new_text, vocab_rows = retire_tag_from_vocab(text, tag)
                        if vocab_rows:
                            op_id = journal.new_op_id(manifest)
                            snapshot_rel = journal.write_note_snapshot(
                                vault_root, cfg, vocab_rel, op_id, raw)
                            new_bytes = new_text.encode("utf-8")
                            op = {
                                "op_id": op_id, "kind": "vocab_retire",
                                "note_rel": vocab_rel, "removed": [tag],
                                "hash_before": hashlib.sha256(raw).hexdigest(),
                                "hash_after": hashlib.sha256(new_bytes).hexdigest(),
                                "snapshot_rel": snapshot_rel,
                                "batch_id": batch_id,
                                "ts": journal.now_iso(), "state": "applied",
                            }
                            manifest["ops"].append(op)
                            # Journal-before-write (the note writers' order):
                            # the applied op + snapshot are durable before
                            # _tags.md changes, so a crash between the two
                            # leaves a restorable trail, never a silent edit.
                            journal.save(vault_root, cfg, manifest)
                            log_vault_write("tagging_vocab_retire", str(vpath),
                                            f"-#{tag} from {vocab_rows} row(s)")
                            try:
                                write_text_atomic(str(vpath), new_text)
                            except OSError as exc:
                                # Mirror _note_remove_locked's failure path: an
                                # op left "applied" after a failed write would
                                # claim forever a _tags.md edit that never
                                # landed (its hash_after matches nothing), and
                                # the terminal frame would still report the
                                # rows as edited (2026-07-23 audit).
                                op["state"] = "failed"
                                journal.save(vault_root, cfg, manifest)
                                vocab_rows = 0
                                failures.append(
                                    f"vocab: {sanitise_error_msg(exc)}")
                            else:
                                stamped.append(op_id)
                    except Exception as exc:  # noqa: BLE001
                        vocab_rows = 0
                        failures.append(f"vocab: {sanitise_error_msg(exc)}")

            _bump_save(force=True)
            if keeper.lock_lost:
                failures.append(
                    "op-lock TTL lapsed mid-batch; remaining writes were "
                    "skipped, and already-performed writes' journal records "
                    "were merged into the on-disk manifest")
            if not patch_jobs and not keeper.lock_lost:
                try:
                    # protect_batch_id: without it, a retire touching more
                    # notes than the journal's op cap would evict its OWN
                    # earliest ops (snapshots deleted) in the same request
                    # that promised a one-click undo — batch restore silently
                    # partial. The batch is exempt from THIS prune only; it
                    # ages out normally later. With patch_jobs pending, the
                    # prune happens in phase 4 instead.
                    journal.prune(vault_root, cfg, manifest,
                                  protect_batch_id=batch_id)
                    journal.save(vault_root, cfg, manifest)
                except Exception:  # noqa: BLE001
                    logger.warning("retire-tag manifest prune/save failed",
                                   exc_info=True)
        finally:
            obsidian_manager.release_lock(op_epoch)

        # Phase 3 — the Zotero PATCHes, lock-free (see _zotero_remove_journal
        # for why this is safe). A cancel here doesn't skip finalization:
        # the ops are already durable as "applied", so an un-run PATCH MUST
        # be flipped to failed in phase 4 or the journal would claim a
        # removal that never happened.
        from core.utils import log_vault_write as _log_vw
        outcomes: list[tuple[str, dict]] = []  # (op_id, mutation)
        for key, prepared, op_id in patch_jobs:
            if cancel.is_set():
                outcomes.append((op_id, {"state": "failed"}))
                counts["zotero"]["failed"] += 1
                failures.append(f"zotero {key}: cancelled before the write")
                continue
            keeper.done += 1
            put({"info": f"[{keeper.done}/{total}] zotero {key}"})
            try:
                r, s, new_version = _zotero_remove_patch(prepared)
            except Exception as exc:  # noqa: BLE001
                r, s, new_version = {"error": sanitise_error_msg(exc), "outcome_unknown": True}, 500, None
            if s == 200:
                outcomes.append((op_id, {"state": "applied", "version_after": new_version}))
                counts["zotero"]["removed"] += 1
                _stamp(op_id)
                _log_vw("tagging_zotero_tag_remove", prepared["item_key"],
                        f"-{len(prepared['removed'])} tags: "
                        f"{', '.join(prepared['removed'])}")
            else:
                outcomes.append((op_id, {"state": "outcome_unknown" if r.get("outcome_unknown") else "failed"}))
                counts["zotero"]["failed"] += 1
                failures.append(f"zotero {key}: {r.get('error')}")

        # Phase 4 — record the PATCH outcomes + prune under a fresh, short
        # hold. The manifest is RELOADED here: another writer may have taken
        # the lock between our two holds, and saving the phase-2 object would
        # clobber their ops (single-writer discipline). Outcomes are re-keyed
        # by op_id (applied ops can't have been cap-evicted meanwhile — the
        # zotero carve-out in journal.prune protects them).
        if patch_jobs:
            epoch2 = None
            for _ in range(20):
                epoch2 = obsidian_manager.try_acquire_lock(ttl=600)
                if epoch2:
                    break
                time.sleep(1)
            if not epoch2:
                failures.append(
                    "could not re-acquire the op-lock to record Zotero "
                    "outcomes; failed Zotero ops may show as applied in the "
                    "journal (their revert is an idempotent re-add)")
            else:
                try:
                    manifest2 = journal.load(vault_root, cfg)
                    for op_id, mut in outcomes:
                        op2 = journal.find_op(manifest2, op_id)
                        if op2 is not None:
                            op2.update(mut)
                    journal.prune(vault_root, cfg, manifest2,
                                  protect_batch_id=batch_id)
                    journal.save(vault_root, cfg, manifest2)
                except Exception:  # noqa: BLE001
                    logger.warning("retire-tag phase-4 finalize failed",
                                   exc_info=True)
                finally:
                    obsidian_manager.release_lock(epoch2)

        put({"retire": {
            "tag": tag, "batch_id": batch_id if stamped else "",
            "results": counts, "vocab_rows": vocab_rows,
            "op_count": len(stamped), "failures": failures,
        }})

    return run_sse_worker(_worker, consumer_timeout_s=consumer_timeout_s)


_HARMONIZE_MAX_KEYS = 2000

# The batch's own one-click undo is only honest while its ops survive in the
# manifest, and `protect_batch_id` shields them from THIS request's prune only.
# `journal.prune`'s cap loop never evicts an applied finder/zotero op (their
# before-lists live inline, with no snapshot file behind them) but note ops ARE
# evictable: they age out of the `_MAX_OPS`-deep evictable retention window
# (2026-08-13 — the window is now real; the cap budget previously counted the
# never-evictable ops too, so a triage-crowded manifest evicted EVERY note op
# on the very next write). A batch whose note writes exceed the window would
# still have its earliest ops dropped, snapshots and all, by later writes. A
# 500-paper harmonize followed by 100 deck Accepts would silently lose the undo
# for its earliest notes while the button still offered it.
#
# Half the window, so pre-existing note ops of ordinary number cannot push the
# batch over on their own. This is a REFUSAL rather than a warning: the confirm
# dialog promises "undoable in one click", and a promise the journal cannot
# keep is worse than making the user run two batches.
_HARMONIZE_MAX_NOTE_OPS = journal._MAX_OPS // 2


def _harmonize_plan(inv, settings, cfg, keys, want):
    """Resolve the per-record union into vocabulary-gated per-target tag lists.

    Returns ``(plan, out_of_vocab, missing_keys)`` where ``plan`` is a list of
    ``(citation_key, {target: [vocab ids]})`` for records with something to
    write. ``missing_keys`` names every selected key that yields no write:
    unknown keys, rows with nothing to harmonize, AND rows whose every missing
    surface resolved to nothing writable — so ``len(plan) + len(missing_keys)``
    always accounts for the whole selection.

    The union is computed SERVER-SIDE from the cached record — the client sends
    only citation keys. That is the same posture as every other tag write here
    (the client never supplies the bytes that land), and it means the report,
    the confirm dialog and the write can't disagree about what "the union" is:
    all three come from ``find_harmony`` with the same injected synonym map.

    Two resolution steps, in this order, and the order matters:

    1. ``_vocab_id_resolver`` maps each drift surface to the exact ``_tags.md``
       id sharing its canonical key. The report compares by OpenRefine
       fingerprint; the writers gate on the LITERAL id under ``nfc().lower()``,
       which folds neither accents nor separators. Skipping this step would
       drop every accented or hyphenated tag as out-of-vocabulary while the
       row reappeared at the next scan — the unresolvable-row loop that
       ``apply-tag-drift`` already had to be fixed for once.
    2. ``_canonicalise_tags`` re-gates the resolved ids against the vocabulary.
       Belt and braces: the resolver is advisory (it returns ``None`` when
       ``_tags.md`` is unavailable), and this is the security gate every tag
       write in this module shares. It must not be bypassed just because the
       ids came from our own resolver.

    A surface that resolves to nothing is REPORTED, never auto-added: admitting
    a new vocabulary term is a separate, separately-confirmed decision
    (``add-vocab-tag``), and a batch that quietly grew the controlled vocabulary
    would defeat the point of having one.
    """
    from api.routes.audit import _drift_synonyms, _vocab_id_resolver
    from audit.engine.reports import tag_harmony as r_tag_harmony

    vocab, _rel = _read_vocab(cfg, settings.vault_root)
    resolve = _vocab_id_resolver(settings)
    rows = {r.citation_key: r for r in r_tag_harmony.find_harmony(
        inv, synonyms=_drift_synonyms(settings))}

    plan: list[tuple[str, dict[str, list[str]]]] = []
    out_of_vocab: set[str] = set()
    missing_keys: list[str] = []
    for key in keys:
        row = rows.get(key)
        if row is None:
            # Either an unknown key or one with nothing to harmonize. Both are
            # reported rather than silently dropped: a selection built from a
            # stale report should say so, not look like it worked.
            missing_keys.append(key)
            continue
        per_target: dict[str, list[str]] = {}
        for target, surfaces in (("note", row.missing_in_note),
                                 ("finder", row.missing_in_finder),
                                 ("zotero", row.missing_in_zotero)):
            if not want[target] or not surfaces:
                continue
            ids: list[str] = []
            for surface in sorted(surfaces):
                vid = resolve(surface) if resolve is not None else None
                if vid:
                    ids.append(vid)
                else:
                    out_of_vocab.add(surface)
            if not ids or not vocab:
                continue
            valid, _rejected = _canonicalise_tags(ids, vocab)
            if valid:
                per_target[target] = valid
        if per_target:
            plan.append((key, per_target))
        else:
            # A selected row whose every missing surface resolved to nothing
            # writable (out-of-vocab, or filtered by `want`): report it like
            # an unknown key rather than letting the terminal `records` count
            # shrink without attribution (2026-08-18 — this class landed in
            # neither bucket, two lines under a comment claiming otherwise).
            missing_keys.append(key)
    return plan, sorted(out_of_vocab), missing_keys


@tagging_bp.route("/api/tagging/harmonize", methods=["POST"])
def api_tagging_harmonize():
    """Propagate each selected record's tag union to its writable surfaces (SSE).

    Body ``{keys:[...], targets?{note,finder,zotero}, confirm:true}``. For every
    requested citation key the server computes the union of the tags already
    present on that record's note / Finder / Zotero-parent surfaces and writes
    whatever each surface is missing. **Purely additive** — nothing is ever
    removed, so an unwanted tag stays the removal path's job.

    Selection-driven by design, not library-wide. The tagging journal is the
    binding constraint: every write rewrites the whole manifest, which is
    already ~3 MB, and a full-library harmonize would add thousands of ops and
    permanently raise the cost of every future single tag write. Capped at
    ``_HARMONIZE_MAX_KEYS`` keys per call for the same reason.

    Lock choreography is ``retire-tag``'s, for the same reasons (2026-07-23):
    Zotero GETs BEFORE the op-lock; vault-local writes + ALL journal appends
    under ONE hold with a per-record heartbeat that is acted on and the
    manifest checkpointed every 25 ops; Zotero PATCHes AFTER release
    (lock-free — the ``If-Unmodified-Since-Version`` guard, not the lock,
    prevents lost updates, and N sequential PATCHes under the lock would
    freeze indexing for the whole batch); then a short second hold to record
    the PATCH outcomes and prune with the batch protected from cap-eviction.

    Every op carries a shared ``batch_id``, so the whole run is undone by the
    existing ``POST /api/tagging/restore {batch_id}``.

    Terminal frame ``{"harmonize": {batch_id, results, records, op_count,
    out_of_vocab, unresolved_keys, failures}}``.
    """
    import uuid as _uuid

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400
    if coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "Harmonize must be explicitly confirmed (confirm: true)."}), 400
    raw_keys = data.get("keys")
    if not isinstance(raw_keys, list) or not raw_keys:
        return jsonify({"error": "keys must be a non-empty list"}), 400
    keys: list[str] = []
    bad: list[str] = []
    for k in raw_keys:
        ck = coerce_non_empty_string(k, max_len=128)
        if ck is None:
            # Name what failed instead of silently dropping it — the CLI half
            # of this feature already refuses malformed keys by name, and a
            # dropped key here would read as "already harmonized".
            bad.append(repr(k)[:48])
        elif ck not in keys:
            keys.append(ck)
    if bad:
        return jsonify({
            "error": f"Invalid citation key value(s): {', '.join(bad[:5])}"
                     + ("…" if len(bad) > 5 else ""),
        }), 400
    if not keys:
        return jsonify({"error": "keys must be a non-empty list"}), 400
    # Checked AFTER coercion + dedup: the cap bounds the journal growth of a
    # real run, so 2001 duplicates of one key must not be refused.
    if len(keys) > _HARMONIZE_MAX_KEYS:
        return jsonify({
            "error": f"Too many keys (max {_HARMONIZE_MAX_KEYS} per request). "
                     f"Harmonize in batches — each write appends to the "
                     f"restore journal.",
        }), 400

    raw_targets = data.get("targets")
    if not isinstance(raw_targets, dict):
        raw_targets = {}
    want = {k: (coerce_bool(raw_targets.get(k)) is not False) for k in TAGGING_TARGETS}

    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify({"error": "No scan results. Run an audit scan first."}), 404
    cfg = load_config()
    vault_root = settings.vault_root
    biblio_dir = settings.biblio_articles_dir

    from api.routes import _finder_tagwrite
    from rag.vault import obsidian_manager

    if not _finder_tagwrite.is_macos():
        want["finder"] = False

    try:
        plan, out_of_vocab, unresolved = _harmonize_plan(
            inv, settings, cfg, keys, want)
    except Exception as exc:  # noqa: BLE001
        logger.warning("harmonize planning failed", exc_info=True)
        return jsonify({"error": sanitise_error_msg(exc)}), 500

    if not plan:
        # Decided before streaming starts, so it keeps a real status code.
        return jsonify({
            "error": "Nothing to harmonize for those keys.",
            "out_of_vocab": out_of_vocab,
            "unresolved_keys": unresolved,
        }), 400

    # A batch containing Zotero work must be eligible before any Finder/note
    # effect starts. The per-item guard remains authoritative at dispatch.
    zotero_keys = [key for key, targets in plan if "zotero" in targets]
    if zotero_keys:
        from tagging import local_api
        from tagging.writers import _zotero_read_identity

        transport = zotero_write.resolve_transport(cfg)
        if transport.is_web:
            return jsonify({"error": (
                "This selection comes from Zotero Desktop. Writing via Web requires "
                "an independently verified Web selection; no request was sent. "
                "Authorize Zotero Desktop in Tagging, then review this selection again."),
                "zotero_keys": len(zotero_keys)}), 409
        try:
            for key in zotero_keys:
                _zotero_read_identity(transport, inv.records[key].zotero_item)
            state = local_api.capabilities(transport.base_url,
                                           opener=zotero_write._opener, timeout=3)
        except zotero_write.ZoteroError as exc:
            return jsonify({"error": sanitise_error_msg(exc),
                            "zotero_keys": len(zotero_keys)}), 409
        except Exception:
            return jsonify({"error": "Zotero Desktop is unavailable. Open Zotero, authorize "
                            "CHERQAiN in Tagging, then review this selection again.",
                            "zotero_keys": len(zotero_keys)}), 409
        if not (state['authorized'] and state['supports_writes']):
            return jsonify({"error": "Zotero Desktop is not authorized for tag writes. "
                            "Use Authorize Zotero Desktop in Tagging, then review this selection again.",
                            "zotero_keys": len(zotero_keys)}), 409

    # Bound the batch by the ops the journal can actually RETAIN, not just by
    # the keys requested — see _HARMONIZE_MAX_NOTE_OPS. Checked after planning
    # because it is the note writes that matter, not the selection size: 500
    # papers of which 30 need a note write is fine, 150 that all do is not.
    note_ops = sum(1 for _k, t in plan if "note" in t)
    if note_ops > _HARMONIZE_MAX_NOTE_OPS:
        return jsonify({
            "error": (
                f"This selection needs {note_ops} note writes, more than the "
                f"restore journal can keep ({_HARMONIZE_MAX_NOTE_OPS}). The "
                f"one-click undo would be silently incomplete after the next "
                f"tag write, so harmonize in smaller batches."),
            "note_ops": note_ops,
            "max_note_ops": _HARMONIZE_MAX_NOTE_OPS,
        }), 400

    consumer_timeout_s = SSE_SINGLE_SHOT_FLOOR_S + SSE_STALL_MARGIN_S

    def _worker(put, cancel):
        batch_id = _uuid.uuid4().hex[:12]
        counts = {t: {"written": 0, "skipped": 0, "failed": 0}
                  for t in TAGGING_TARGETS}
        failures: list[str] = []
        stamped: list[str] = []
        # `total` drives the [done/total] counter, so it counts the UNITS OF
        # WORK — one per (record, target) — while the message quotes the tag
        # count, which is what the report's meta line and the confirm dialog
        # promised. Conflating the two made a run the confirm announced as
        # "37 tags" stream "[12/12]".
        total = sum(len(t) for _k, t in plan)
        tag_writes = sum(len(ids) for _k, t in plan for ids in t.values())
        put({"info": f"Harmonizing {len(plan)} paper(s): {tag_writes} tag "
                     f"write(s) across {total} target(s)."})
        if out_of_vocab:
            put({"info": f"{len(out_of_vocab)} tag(s) skipped — absent from "
                         f"_tags.md: {', '.join(out_of_vocab[:8])}"
                         + ("…" if len(out_of_vocab) > 8 else "")})

        # Phase 1 — Zotero READS before the lock (apply-all discipline).
        z_prepared: dict[str, dict | None] = {}
        for key, targets in plan:
            if "zotero" not in targets:
                continue
            if cancel.is_set():
                return
            put({"info": f"Zotero: reading {key}…"})
            try:
                prepared, res, status = _zotero_prepare(
                    rec=inv.records[key], valid=targets["zotero"], rejected=[],
                    cfg=cfg, citation_key=key)
                z_prepared[key] = prepared
                if prepared is None:
                    if status == 200:
                        counts["zotero"]["skipped"] += 1
                    else:
                        counts["zotero"]["failed"] += 1
                        failures.append(
                            f"zotero {key}: {(res or {}).get('error', status)}")
            except Exception as exc:  # noqa: BLE001
                z_prepared[key] = None
                counts["zotero"]["failed"] += 1
                failures.append(f"zotero {key}: {sanitise_error_msg(exc)}")

        # Phase 2 — vault-local writes + ALL journal appends under one hold.
        op_epoch = obsidian_manager.try_acquire_lock(ttl=3600)
        if not op_epoch:
            put({"error": "Indexing is in progress; try again shortly."})
            return
        patch_jobs: list[tuple[str, dict, str]] = []
        try:
            try:
                manifest = journal.load(vault_root, cfg)
            except (journal.ScopeError, journal.JournalUnreadableError) as exc:
                put({"error": sanitise_error_msg(exc)})
                return

            # Checkpointing + heartbeat: the shared _BatchJournalKeeper
            # (retire's discipline, hoisted — see its docstring).
            keeper = _BatchJournalKeeper(
                vault_root=vault_root, cfg=cfg, manifest=manifest,
                op_epoch=op_epoch, put=put, total=total,
                manager=obsidian_manager)
            _bump_save, _tick = keeper.bump_save, keeper.tick

            for key, targets in plan:
                if cancel.is_set() or keeper.lock_lost:
                    break
                rec = inv.records[key]
                if "note" in targets:
                    _tick(f"note {key}")
                    if keeper.lock_lost:
                        break
                    try:
                        r, s = _note_write_locked(
                            rec=rec, valid=targets["note"], rejected=[],
                            want_sha=None, cfg=cfg, vault_root=vault_root,
                            citation_key=key, manifest=manifest,
                            batch_id=batch_id, defer_save=True)
                    except Exception as exc:  # noqa: BLE001
                        r, s = {"error": sanitise_error_msg(exc)}, 500
                    if s == 200 and r.get("added"):
                        counts["note"]["written"] += 1
                        stamped.append(r.get("op_id"))
                        _bump_save()
                    elif s == 200:
                        counts["note"]["skipped"] += 1
                    else:
                        counts["note"]["failed"] += 1
                        failures.append(f"note {key}: {r.get('error')}")
                        _bump_save()

                if "finder" in targets:
                    if cancel.is_set() or keeper.lock_lost:
                        break
                    _tick(f"finder {key}")
                    if keeper.lock_lost:
                        break
                    try:
                        r, s = _finder_write_locked(
                            rec=rec, valid=targets["finder"], rejected=[],
                            cfg=cfg, vault_root=vault_root,
                            biblio_dir=biblio_dir, citation_key=key,
                            manifest=manifest, batch_id=batch_id,
                            defer_save=True)
                    except Exception as exc:  # noqa: BLE001
                        r, s = {"error": sanitise_error_msg(exc)}, 500
                    if s == 200 and r.get("added"):
                        counts["finder"]["written"] += 1
                        stamped.append(r.get("op_id"))
                        _bump_save()
                    elif s in (200, 404):
                        counts["finder"]["skipped"] += 1
                    else:
                        counts["finder"]["failed"] += 1
                        failures.append(f"finder {key}: {r.get('error')}")
                        _bump_save()

                # Zotero: journal now, PATCH after the lock is released. Only
                # the SKIPPED ones tick here — a queued op is ticked in phase 3
                # when its PATCH actually runs, so [done/total] counts each
                # unit once (retire's discipline; ticking in both phases made
                # the counter overshoot its own total).
                if "zotero" in targets:
                    if cancel.is_set() or keeper.lock_lost:
                        break
                    prepared = z_prepared.get(key)
                    if prepared is None:
                        _tick(f"zotero {key} (skipped)")
                        continue
                    op = _zotero_journal(prepared=prepared, citation_key=key,
                                         manifest=manifest, batch_id=batch_id)
                    patch_jobs.append((key, prepared, op["op_id"]))
                    _bump_save()

            _bump_save(force=True)
            if keeper.lock_lost:
                failures.append(
                    "the operation lock lapsed mid-batch; remaining writes "
                    "were skipped (nothing already written was lost)")
            # `not keeper.lock_lost` matters as much as `not patch_jobs`: a lapsed TTL
            # means another writer may own the lock, and pruning + saving into
            # a manifest we no longer own could clobber their ops. Phase 4
            # re-acquires properly and reloads, so nothing is lost by skipping.
            if not patch_jobs and not keeper.lock_lost:
                try:
                    journal.prune(vault_root, cfg, manifest,
                                  protect_batch_id=batch_id)
                    journal.save(vault_root, cfg, manifest)
                except Exception:  # noqa: BLE001
                    logger.warning("harmonize manifest prune/save failed",
                                   exc_info=True)
        finally:
            obsidian_manager.release_lock(op_epoch)

        # Phase 3 — the Zotero PATCHes, lock-free.
        from core.utils import log_vault_write as _log_vw
        outcomes: list[tuple[str, dict]] = []
        for key, prepared, op_id in patch_jobs:
            if cancel.is_set():
                # An op whose PATCH never ran must NOT stay "applied": the
                # revert would conservatively SKIP it (the item never reached
                # the op's `after` state — _revert_zotero_prepare's equality
                # check), so the hazard is not a wrong write but a journal
                # claiming a write that did not happen, which inflates the
                # batch-restore's accounting. Flip it to failed.
                outcomes.append((op_id, {"state": "failed"}))
                counts["zotero"]["failed"] += 1
                failures.append(f"zotero {key}: cancelled before the write")
                continue
            keeper.done += 1
            put({"info": f"[{keeper.done}/{total}] zotero {key}"})
            try:
                r, s, new_version = _zotero_patch(prepared)
            except Exception as exc:  # noqa: BLE001
                r, s, new_version = {"error": sanitise_error_msg(exc), "outcome_unknown": True}, 500, None
            if s == 200:
                outcomes.append((op_id, {"state": "applied", "version_after": new_version}))
                counts["zotero"]["written"] += 1
                stamped.append(op_id)
                _log_vw("tagging_zotero_tag_add", prepared["item_key"],
                        f"+{len(prepared['added'])} tags: "
                        f"{', '.join(prepared['added'])}")
            else:
                outcomes.append((op_id, {"state": "outcome_unknown" if r.get("outcome_unknown") else "failed"}))
                counts["zotero"]["failed"] += 1
                failures.append(f"zotero {key}: {r.get('error')}")

        # Phase 4 — record the outcomes + prune under a fresh short hold. The
        # manifest is RELOADED: another writer may have taken the lock between
        # our two holds, and saving the phase-2 object would clobber their ops.
        if patch_jobs:
            epoch2 = None
            for _ in range(20):
                epoch2 = obsidian_manager.try_acquire_lock(ttl=600)
                if epoch2:
                    break
                time.sleep(1)
            if not epoch2:
                failures.append(
                    "could not re-acquire the op-lock to record Zotero "
                    "outcomes; a failed Zotero op may show as applied in the "
                    "journal — its revert will be conservatively skipped (the "
                    "item never reached the written state), but the batch "
                    "undo will report more ops than it can actually revert")
            else:
                try:
                    manifest2 = journal.load(vault_root, cfg)
                    for op_id, mut in outcomes:
                        op2 = journal.find_op(manifest2, op_id)
                        if op2 is not None:
                            op2.update(mut)
                    journal.prune(vault_root, cfg, manifest2,
                                  protect_batch_id=batch_id)
                    journal.save(vault_root, cfg, manifest2)
                except Exception:  # noqa: BLE001
                    logger.warning("harmonize phase-4 finalize failed",
                                   exc_info=True)
                finally:
                    obsidian_manager.release_lock(epoch2)

        put({"harmonize": {
            "batch_id": batch_id if stamped else "",
            "results": counts,
            "records": len(plan),
            "op_count": len([s for s in stamped if s]),
            "out_of_vocab": out_of_vocab,
            "unresolved_keys": unresolved,
            "failures": failures,
        }})

    return run_sse_worker(_worker, consumer_timeout_s=consumer_timeout_s)


# ---------------------------------------------------------------------------
# In-app PDF viewing (deck side panel): blob-fetched full PDF + macOS fallback
# ---------------------------------------------------------------------------

def _resolve_biblio_pdf(citation_key: str):
    """Resolve a citation key's first ``biblio_articles/`` PDF, re-validated.

    Returns ``(abs_path, None, None)`` or ``(None, error_dict, status)``.
    Every path derives from the cached audit scan — never a client-supplied
    filesystem path — but the realpath is still independently re-checked under
    the resolved biblio dir (mirroring ``deck.py::_resolve_deck_pdf``'s
    sibling re-check): the scan snapshot could be stale and a path swapped
    for a symlink since. No deny-prefix pass — that guard is for user-typed
    absolute paths; here everything derives from the vault root (the same
    posture as the audit's ``reveal``).
    """
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return None, {"error": "No scan results. Run an audit scan first."}, 404
    rec = inv.records.get(citation_key)
    if rec is None:
        return None, {"error": "Unknown citation key."}, 404
    biblio_dir = settings.biblio_articles_dir
    biblio_pdfs = [p for p in rec.pdf_paths if _is_under(Path(p), biblio_dir)]
    if not biblio_pdfs:
        return None, {"error": "No biblio_articles PDF for that citation key."}, 404
    try:
        real = Path(os.path.realpath(biblio_pdfs[0]))
        real_dir = Path(os.path.realpath(biblio_dir))
    except (OSError, ValueError):
        return None, {"error": "PDF path could not be resolved."}, 404
    if (not _is_under(real, real_dir) or not real.is_file()
            or real.suffix.lower() != ".pdf"):
        return None, {"error": "PDF not found."}, 404
    return str(real), None, None


@tagging_bp.route("/api/tagging/pdf")
def api_tagging_pdf():
    """Serve a citation key's biblio PDF bytes for the deck's in-app viewer.

    ``?citation_key=`` → the raw PDF (``conditional=True`` for range requests).
    The client MUST fetch this with the ``X-Requested-With`` header and render
    via a ``blob:`` URL — an ``<iframe src>`` pointing at any ``/api`` route is
    403'd by the app-level origin guard (the deck preview-pdf constraint,
    pinned by the tagging source ratchet in ``tests/js/taggingPdf.test.js``).
    """
    citation_key = coerce_non_empty_string(request.args.get("citation_key"), max_len=128)
    if not citation_key:
        return jsonify({"error": "citation_key required"}), 400
    pdf, err, status = _resolve_biblio_pdf(citation_key)
    if err is not None:
        return jsonify(err), status
    return send_file(pdf, mimetype="application/pdf", conditional=True)


@tagging_bp.route("/api/tagging/open-pdf", methods=["POST"])
def api_tagging_open_pdf():
    """Open the citation key's biblio PDF in the system viewer (macOS only).

    Body ``{citation_key}``. 501 off macOS. The fallback for WKWebView builds
    that refuse inline ``blob:`` PDFs (same role as the deck's ``open-pdf``).
    """
    if sys.platform != "darwin":
        return jsonify({"error": "Opening files is only supported on macOS."}), 501
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400
    citation_key = coerce_non_empty_string(data.get("citation_key"), max_len=128)
    if not citation_key:
        return jsonify({"error": "citation_key required"}), 400
    pdf, err, status = _resolve_biblio_pdf(citation_key)
    if err is not None:
        return jsonify(err), status
    try:
        run_open(["open", pdf])
    except OSError as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    return jsonify({"ok": True})


# A short display excerpt for the deck card (context while deciding). Cheap on a
# cache hit; independent of the propose prompt's larger `tagging_text_max_chars`.
# Why 1400: the card renders ~900 chars (tagging.js slices there) — 1400 keeps
# a paragraph of slack so the ellipsis decision is made client-side without a
# refetch, while staying far under a level that would turn the cache-miss
# extraction into real work.
_SNIPPET_MAX_CHARS = 1400


@tagging_bp.route("/api/tagging/snippet", methods=["POST"])
def api_tagging_snippet():
    """Return a bounded PDF-text excerpt for a citation key (deck-card context).

    Body ``{citation_key}``. Read-only; reuses the cache-first PDF-text source
    (free on a cache hit). Returns ``{citation_key, snippet, truncated, chars}``;
    ``snippet`` is empty when the PDF has no extractable text (scanned/unreadable).

    Unlike ``/propose``, this route deliberately runs ON the request thread with
    no single-flight gate: its worst case is one fresh extraction genuinely
    bounded to the document's first 3 pages with the whole-file MarkItDown
    fallback disabled (vs propose's unbounded-on-local LLM call, which is why
    propose got the daemon+429 treatment), a no-text-layer document fast-fails
    on the ``.notext`` marker after its first scan, and a cache-hit (the common
    case — biblio PDFs are indexed) returns in milliseconds. If a future caller
    fans snippet requests out in parallel, give it the ``_run_propose_bounded``
    treatment.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400
    citation_key = coerce_non_empty_string(data.get("citation_key"), max_len=128)
    if not citation_key:
        return jsonify({"error": "citation_key required"}), 400
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify({"error": "No scan results. Run an audit scan first."}), 404
    rec = inv.records.get(citation_key)
    if rec is None:
        return jsonify({"error": "Unknown citation key."}), 404
    biblio_dir = settings.biblio_articles_dir
    biblio_pdfs = [p for p in rec.pdf_paths if _is_under(Path(p), biblio_dir)]
    if not biblio_pdfs:
        return jsonify({"citation_key": citation_key, "snippet": "", "truncated": False, "chars": 0})
    result = text_source.read_pdf_text(
        Path(biblio_pdfs[0]), settings.vault_root, _SNIPPET_MAX_CHARS, fresh_max_pages=3)
    from core.extraction_status import status_of
    text, _trunc = result
    snippet = text.strip()
    return jsonify({
        "citation_key": citation_key, "snippet": snippet,
        "extraction_status": status_of(result).to_dict(),
        "truncated": bool(_trunc) or len(snippet) >= _SNIPPET_MAX_CHARS,
        "chars": len(snippet),
    })
