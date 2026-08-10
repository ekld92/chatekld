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
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request, send_file

from api.security import sanitise_error_msg
from api.sse import run_sse_worker
from core.constants import SSE_SINGLE_SHOT_FLOOR_S, SSE_STALL_MARGIN_S
from api.validators import (
    coerce_bool,
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
from tagging import journal, propose as tag_propose, text_source, zotero_write
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
    _revert_zotero_tag,
    _target_result,
    _zotero_base,
    _zotero_commit_locked,
    _zotero_prepare,
    _zotero_remove_commit_locked,
    _zotero_remove_journal,
    _zotero_remove_patch,
    _zotero_remove_prepare,
)

tagging_bp = Blueprint("tagging", __name__)
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


def _project_record(rec: Any, vault_root: Path, biblio_dir: Path) -> dict[str, Any] | None:
    """Flatten one audit ``Record`` into a tagging-table row, or None if out of scope.

    In scope = the record has at least one resolved PDF under
    ``biblio_articles/`` (the feature's fixed boundary). Emits the CURRENT tags
    for each of the three write targets side by side so the UI can show what a
    later Apply would union into:

    * ``finder_tags`` — union across the record's biblio PDFs (from the scan)
    * ``note``        — the Obsidian Zotero note's YAML tags (+ its rel path)
    * ``zotero``      — the parent item key, its parent tags, and child-note tags
    """
    biblio_pdfs = [p for p in rec.pdf_paths if _is_under(Path(p), biblio_dir)]
    if not biblio_pdfs:
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
        "title": bib.title if bib else None,
        "year": bib.year if bib else None,
        "first_author": (bib.authors[0] if bib and bib.authors else None),
        "pdf_paths": [
            {"abs": str(p), "rel": _rel(p), "name": Path(p).name} for p in biblio_pdfs
        ],
        "finder_tags": sorted(rec.finder_tag_set),
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
    on its own. Only records with a PDF under ``biblio_articles/`` are returned,
    ordered by year then citation key (matching the audit inventory table).
    """
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify({"error": "No scan results. Run an audit scan first."}), 404

    vault_root = settings.vault_root
    biblio_dir = settings.biblio_articles_dir
    rows: list[dict[str, Any]] = []
    for rec in inv.records.values():
        row = _project_record(rec, vault_root, biblio_dir)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda r: (r["year"] or "", r["citation_key"]))
    return jsonify({"rows": rows, "count": len(rows)})


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

    Body: ``{citation_key}``. Read-only — proposes, never writes: the UI shows
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
    if not biblio_pdfs:
        return jsonify({"error": "No biblio_articles PDF for that citation key."}), 404

    cfg = load_config()
    vocab, vocab_rel = _read_vocab(cfg, vault_root)
    if not vocab:
        return jsonify({"error": "Tag vocabulary (_tags.md) is unavailable."}), 400

    text_max = (coerce_int_in_range(cfg.get("tagging_text_max_chars"), 500, 40000)
                or _CFG_DEFAULTS["tagging_text_max_chars"])
    text, truncated = text_source.read_pdf_text(
        Path(biblio_pdfs[0]), vault_root, text_max, fresh_max_pages=8)
    # ONE no-text threshold, owned by text_source (it writes the .notext
    # marker on the same test) — an inline `40` here silently drifted once.
    if len(text.strip()) < text_source._MIN_USEFUL_CHARS:
        return jsonify({
            "citation_key": citation_key, "proposed": [],
            "status": "no_text", "truncated": truncated,
        })

    provider = str(cfg.get("provider") or "ollama")
    model = coerce_string_max_len(cfg.get("tagging_propose_model"), 120) or resolve_chat_model(cfg, provider)
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
        return tag_propose.propose_tags(
            title=title, text=text, vocab=effective_vocab,
            provider_name=provider, model=model, temperature=temperature,
            max_tokens=max_tokens, cfg=cfg, max_tags=max_tags,
            should_cancel=should_cancel, exemplars=exemplars,
        )

    try:
        proposed, timed_out, busy = _run_propose_bounded(_fn)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 502
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
    return jsonify({
        "citation_key": citation_key, "proposed": proposed,
        "status": status, "truncated": truncated,
    })


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
    """Probe the configured Zotero endpoint so the UI can enable/disable the write.

    ``available`` means "reachable AND usable": on the web transport an
    unauthenticated endpoint is reachable but cannot write, so a missing or
    rejected key reports unavailable rather than letting the user click a
    button that is guaranteed to 503. ``writable`` additionally flags the
    read-only local API (Zotero ≤ 9 answers GET but 501s every PATCH), so the
    UI can explain *why* rather than showing an opaque failure later.
    """
    cfg = load_config()
    transport = zotero_write.resolve_transport(cfg)
    available = (transport.can_write
                 and zotero_write.probe(transport.base_url, timeout=3.0,
                                        api_key=transport.api_key))
    return jsonify({
        "available": available,
        "base": transport.base_url,
        "transport": transport.kind,
        # Local writes are unsupported before Zotero 10; we cannot prove that
        # without attempting a write, so this is a capability HINT, not a probe.
        "writable": available,
        "note": ("Zotero's local API is read-only before Zotero 10 — set "
                 f"{zotero_write.API_KEY_ENV} to write via the web API."
                 if transport.kind == "local" else ""),
    })


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


@tagging_bp.route("/api/tagging/manifest")
def api_tagging_manifest():
    """Return the tagging restore manifest (newest op first) for the Restore UI."""
    try:
        vault_root = _vault_root()
    except AuditConfigError as exc:
        return jsonify({"error": str(exc)}), 400
    cfg = load_config()
    try:
        manifest = journal.load(vault_root, cfg)
    except journal.ScopeError as exc:
        return jsonify({"error": str(exc)}), 400
    except journal.JournalUnreadableError as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 503
    # "removed"/"batch_id" joined 2026-07-23: the untag/vocab_retire op kinds
    # carry `removed`, not `added` — without them the Restore view showed
    # those ops with no tag detail, making the per-op fallback undo unusable.
    fields = ("op_id", "kind", "note_rel", "citation_key", "added", "removed",
              "batch_id", "ts", "state")
    ops = [{k: op.get(k) for k in fields} for op in manifest.get("ops", [])]
    ops.reverse()
    return jsonify({"ops": ops, "count": len(ops)})


def _restore_batch(batch_id: str):
    """Revert every applied op stamped with *batch_id*, newest-first (SSE).

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
        peek = journal.load(vault_root, cfg)
    except (journal.ScopeError, journal.JournalUnreadableError) as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 503
    batch_ops = [op for op in peek["ops"]
                 if op.get("batch_id") == batch_id and op.get("state") == "applied"]
    if not batch_ops:
        return jsonify({"error": "No applied ops carry that batch id."}), 404

    consumer_timeout_s = SSE_SINGLE_SHOT_FLOOR_S + SSE_STALL_MARGIN_S

    def _worker(put, cancel):
        total_ops = len(batch_ops)
        # Phase 1 — Zotero reads, lock-free (a wedged Zotero must not stall
        # indexing or any other vault write).
        z_prepared: dict[str, dict] = {}
        zotero_ops = [op for op in batch_ops
                      if op.get("kind") in ("zotero_tag", "zotero_untag")]
        for i, op in enumerate(zotero_ops, start=1):
            # `cancel` is the skeleton's threading.Event, never a callable —
            # `cancel()` raised TypeError here and _runner's blanket handler
            # turned it into an {"error"} frame BEFORE phase 2, so a batch
            # containing any Zotero op reverted nothing at all. Every other
            # worker in this file uses .is_set(); this was the odd one out.
            if cancel.is_set():
                return
            put({"info": f"Zotero: reading {i}/{len(zotero_ops)}…"})
            z_prepared[op["op_id"]] = _revert_zotero_prepare(cfg, op)

        # Phase 2 — one lock hold for every revert + the manifest save.
        op_epoch = obsidian_manager.try_acquire_lock(ttl=3600)
        if not op_epoch:
            put({"error": "Indexing is in progress; try again shortly."})
            return
        results: list[dict] = []
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
                       if op.get("batch_id") == batch_id and op.get("state") == "applied"]
            for done, op in enumerate(ordered, start=1):
                # Act on a False heartbeat (CLAUDE.md Limits discipline): a
                # lapsed TTL means another writer may own the lock — stop
                # reverting rather than write into someone else's hold. Already-
                # reverted ops were flipped in-memory; the save below still runs
                # (accepted: the alternative loses those flips and re-running the
                # batch would re-revert already-restored stores).
                if not obsidian_manager.heartbeat(op_epoch):
                    results.append({"op_id": op["op_id"], "kind": op.get("kind"),
                                    "ok": False, "status": "error",
                                    "message": "op-lock TTL lapsed; batch restore "
                                               "aborted — retry to revert the rest"})
                    put({"info": "op-lock TTL lapsed — stopping; re-run to revert the rest."})
                    break
                kind = op.get("kind")
                try:
                    if kind in ("finder_tag", "finder_untag"):
                        result = _revert_finder_tag(vault_root, cfg, op)
                    elif kind in ("zotero_tag", "zotero_untag"):
                        prepared = z_prepared.get(op["op_id"])
                        if prepared is None or "early" in prepared:
                            result = (prepared or {}).get("early") or {
                                "ok": False, "status": "error",
                                "message": "Zotero read failed."}
                        else:
                            result = _revert_zotero_commit_locked(prepared, op)
                    else:
                        result = journal.revert_op(vault_root, cfg, op)
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "status": "error",
                              "message": sanitise_error_msg(exc)}
                results.append({"op_id": op["op_id"], "kind": kind, **result})
                put({"info": f"[{done}/{total_ops}] {kind or 'op'}: "
                             f"{result.get('status') or 'done'}"})
            journal.save(vault_root, cfg, manifest)
        finally:
            obsidian_manager.release_lock(op_epoch)

        reverted = sum(1 for r in results if r.get("status") == "reverted")
        put({"restore": {"ok": True, "batch_id": batch_id,
                         "reverted": reverted, "total": len(results),
                         "results": results}})

    return run_sse_worker(_worker, consumer_timeout_s=consumer_timeout_s)


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
        peek_manifest = journal.load(vault_root, cfg)
    except journal.ScopeError as exc:
        return jsonify({"error": str(exc)}), 400
    except journal.JournalUnreadableError as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 503
    peek_op = journal.find_op(peek_manifest, op_id)
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
        elif kind in ("zotero_tag", "zotero_untag"):
            # The read already ran pre-lock; only the guarded PATCH runs here.
            result = _revert_zotero_commit_locked(zotero_prepared, op)
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
    want = {k: (coerce_bool(raw_targets.get(k)) is not False) for k in ("note", "finder", "zotero")}
    want_sha = coerce_string_max_len(data.get("content_sha256"), 64)

    rec = ctx["rec"]
    results: dict = {}

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
        except Exception as exc:  # noqa: BLE001
            logger.warning("apply-all zotero prepare failed for %s",
                           ctx["citation_key"], exc_info=True)
            zotero_early = {"ok": False, "error": sanitise_error_msg(exc)}

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
            else:
                def _finder():
                    r, s = _finder_write_locked(
                        rec=rec, valid=ctx["valid"], rejected=ctx["rejected"], cfg=ctx["cfg"],
                        vault_root=ctx["vault_root"], biblio_dir=ctx["biblio_dir"],
                        citation_key=ctx["citation_key"], manifest=shared_manifest)
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
    want = {k: (coerce_bool(raw_targets.get(k)) is not False) for k in ("note", "finder", "zotero")}
    want_sha = coerce_string_max_len(data.get("content_sha256"), 64)

    rec = ctx["rec"]
    results: dict = {}

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
    if want["zotero"] and rec.zotero_item is not None:
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
            if rec.obsidian_note is None:
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
                        citation_key=ctx["citation_key"], manifest=shared_manifest)
                    return ({"ok": False, "skipped": "no_pdf"} if s == 404
                            else _removal_result(r, s))
                _isolated("finder", _finder)
        if want["zotero"]:
            if rec.zotero_item is None:
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

    return jsonify({"citation_key": ctx["citation_key"], "results": results}), 200


# ---------------------------------------------------------------------------
# Vault-wide tag retirement (Batch G, 2026-07-22): one tag → every store
# ---------------------------------------------------------------------------

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
    want = {k: (coerce_bool(raw_targets.get(k)) is not False) for k in ("note", "finder", "zotero")}
    include_vocab = coerce_bool(data.get("include_vocab")) is True

    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify({"error": "No scan results. Run an audit scan first."}), 404
    cfg = load_config()
    vault_root = settings.vault_root
    biblio_dir = settings.biblio_articles_dir
    tl = tag.lower()

    note_keys: list[str] = []
    finder_keys: list[str] = []
    zotero_keys: list[str] = []
    for key, rec in inv.records.items():
        if want["note"] and rec.obsidian_note is not None \
                and tl in {str(t).lower() for t in (rec.obs_tags or [])}:
            note_keys.append(key)
        if want["finder"] and tl in {str(t).lower() for t in (rec.finder_tag_set or set())} \
                and any(_is_under(Path(p), biblio_dir) for p in rec.pdf_paths):
            finder_keys.append(key)
        if want["zotero"] and rec.zotero_item is not None \
                and tl in {str(t).lower() for t in (rec.zotero_item.tags or [])}:
            zotero_keys.append(key)

    from api.routes import _finder_tagwrite
    from rag.vault import obsidian_manager

    if not _finder_tagwrite.is_macos():
        finder_keys = []

    consumer_timeout_s = SSE_SINGLE_SHOT_FLOOR_S + SSE_STALL_MARGIN_S

    def _worker(put, cancel):
        batch_id = _uuid.uuid4().hex[:12]
        counts = {t: {"removed": 0, "skipped": 0, "failed": 0}
                  for t in ("note", "finder", "zotero")}
        failures: list[str] = []
        total = len(note_keys) + len(finder_keys) + len(zotero_keys)
        put({"info": f"Retiring '{tag}': {len(note_keys)} note(s), "
                     f"{len(finder_keys)} PDF record(s), {len(zotero_keys)} "
                     f"Zotero item(s) from the last scan."})

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
        lock_lost = False
        patch_jobs: list[tuple[str, dict, str]] = []  # (key, prepared, op_id)
        pending_saves = 0
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

            # Manifest checkpointing (refactor/apply.py JOURNAL_FLUSH_EVERY
            # precedent): the writers run defer_save=True and we persist every
            # 25 ops + force-save before anything that must be durable (the
            # vocab write's journal-before-write, the lock release). Per-op
            # note snapshots stay durable regardless — only the op-record
            # persistence is batched, so a crash costs ≤25 op records while
            # their snapshots + log_vault_write trail survive.
            def _bump_save(force=False):
                nonlocal pending_saves
                if not force:
                    pending_saves += 1
                if pending_saves and (force or pending_saves >= 25):
                    journal.save(vault_root, cfg, manifest)
                    pending_saves = 0

            done = 0

            def _tick(label):
                nonlocal done, lock_lost
                done += 1
                # Act on a False heartbeat (CLAUDE.md Limits discipline —
                # "never discard it"): a lapsed TTL means another writer may
                # own the lock, so we stop writing, including manifest saves
                # (saving into a manifest we no longer own could clobber the
                # new owner's ops — worse than losing ≤25 of our op records).
                if not obsidian_manager.heartbeat(op_epoch):
                    lock_lost = True
                put({"info": f"[{done}/{total}] {label}"})

            for key in note_keys:
                if cancel.is_set() or lock_lost:
                    break
                _tick(f"note {key}")
                if lock_lost:
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
                if cancel.is_set() or lock_lost:
                    break
                _tick(f"finder {key}")
                if lock_lost:
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

            # Zotero: journal the ops now (append + one save with the rest),
            # PATCH after release. Journalling BEFORE the PATCH keeps the
            # crash/cancel trail conservative — an op whose PATCH never ran
            # reverts by re-adding tags that were never removed, an
            # idempotent no-op (see _zotero_remove_journal).
            for key in zotero_keys:
                if cancel.is_set() or lock_lost:
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
            if include_vocab and not cancel.is_set() and not lock_lost:
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
            if lock_lost:
                failures.append(
                    "op-lock TTL lapsed mid-batch; remaining writes skipped "
                    "and the last few op records may be unpersisted")
            if not patch_jobs and not lock_lost:
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
            done += 1
            put({"info": f"[{done}/{total}] zotero {key}"})
            try:
                r, s, new_version = _zotero_remove_patch(prepared)
            except Exception as exc:  # noqa: BLE001
                r, s, new_version = {"error": sanitise_error_msg(exc)}, 500, None
            if s == 200:
                outcomes.append((op_id, {"version_after": new_version}))
                counts["zotero"]["removed"] += 1
                _stamp(op_id)
                _log_vw("tagging_zotero_tag_remove", prepared["item_key"],
                        f"-{len(prepared['removed'])} tags: "
                        f"{', '.join(prepared['removed'])}")
            else:
                outcomes.append((op_id, {"state": "failed"}))
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
        subprocess.Popen(["open", pdf])
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
    text, _trunc = text_source.read_pdf_text(
        Path(biblio_pdfs[0]), settings.vault_root, _SNIPPET_MAX_CHARS, fresh_max_pages=3)
    snippet = text.strip()
    return jsonify({
        "citation_key": citation_key, "snippet": snippet,
        "truncated": bool(_trunc) or len(snippet) >= _SNIPPET_MAX_CHARS,
        "chars": len(snippet),
    })
