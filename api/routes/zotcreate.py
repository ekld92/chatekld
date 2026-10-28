"""``/api/zotcreate/*`` — make a Zotero item from an orphan vault PDF.

The forward verb beyond ``zotlink``: that one moves or removes a pointer to
an item Zotero already has, this one mints the item and links the file under
it. A Library Audit sub-view like Tagging and Note Sync — it reads the
audit's cached inventory and lives in its own blueprint so the vendored
``audit/`` tree keeps its read-only posture.

**Two POSTs, therefore two lost-response windows.** A parent is created,
then a ``linked_file`` attachment under its returned key. Both are unkeyed
(Zotero Desktop refuses a keyed create), so the duplicate barrier is
:mod:`zotcreate.attempts`, reserved before the first of them and keyed on
the PDF's sha256 — the bytes, so a rename cannot buy a second create. The
middle state is resumable rather than merely blocked: holding a proven
parent key we can read that item's children and adopt an attachment a lost
response hid from us.

**Gates, in order.** A completed scan · not while a scan runs · the PDF is
one of THIS scan's orphans or curated ``pending`` files (``no_match`` —
"not a paper for Zotero" — stays excluded; a detach exclusion does not
refuse a create since 2026-09-18) · the Zotero/plugin file-RENAMING
gate, which is why :func:`api.routes.zotlink.rename_gate` was lifted out of
the attach route rather than copied · a writable transport · ``confirm``.

Synchronous, not SSE: one item is a short bounded sequence of round-trips,
the repoint verb's posture. A bulk create would need the four-phase
choreography and belongs with the DOI work, not here.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

from flask import Blueprint, jsonify, request

from api.security import sanitise_error_msg
from api.validators import coerce_bool, coerce_string_max_len
from audit.manager import audit_manager
from core.config import load_config

logger = logging.getLogger(__name__)

zotcreate_bp = Blueprint("zotcreate", __name__)

#: One create at a time (the accession/zotlink posture). Overlapping them
#: would interleave journal appends and attempt-store transactions for no
#: benefit — and a create is the one verb where a race could duplicate.
_ZOTCREATE_GATE = threading.Lock()

#: Bounds on the caller-supplied item description. Zotero validates field
#: NAMES itself, but an unbounded map would let a client push arbitrary
#: weight through a route whose whole job is to be small and deliberate.
_MAX_FIELDS = 24
_MAX_FIELD_CHARS = 2000
_MAX_CREATORS = 60
_FIELD_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,39}$")

#: How much page-1 text rides the preview, for the title the
#: extractor got wrong. Enough to hold a title block and its
#: authors; not so much that the form becomes a reader.
_EXCERPT_CHARS = 900

def _gates():
    """``((inv, settings, cfg), None)`` or ``(None, flask_error)``."""
    if audit_manager.is_scanning():
        return None, (jsonify({
            "error": "A scan is running. Wait for it to finish."}), 409)
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        from api.routes.pending import context
        try:
            inv, settings = context()
        except (OSError, ValueError):
            return None, (jsonify({"error": "No completed scan or durable PDF workspace. Run a scan first."}), 404)
    return (inv, settings, load_config()), None


def _resolve(inv, settings, raw):
    """The PDF, re-validated from scratch. Shares ONE definition of "is this
    an orphan of the cached scan?" with the accession routes."""
    if getattr(inv, 'reconciliation_only', False):
        from api.routes.pending import target
        try:
            return target(settings, raw)
        except (OSError, ValueError):
            return None
    from api.routes.accession import _resolve_orphan
    return _resolve_orphan(inv, settings, raw)


def _provenance(path: Path, settings):
    """The memoised physical class for one PDF, computed if absent.

    Goes through the bridge's own classifier so the class the create verb
    reasons about is the class the report showed — a second implementation
    would drift and route a printed web page to a journal-article form.
    """
    from api.routes.audit import _build_pdf_text_provider
    from audit.engine import bridge

    try:
        out = bridge._classify_residual(
            [path], pdf_text_provider=_build_pdf_text_provider())
    except Exception:  # noqa: BLE001 — a class is advisory, never fatal
        logger.exception("zotcreate provenance failed")
        return {}
    return out.get(path) or {}


def _page1_text(path: Path) -> str | None:
    """Page-1 text through the SAME app-side provider the scan injects, so
    the title and identifiers a create reasons about come from the text the
    reports were built on — and from its caches, not a second extraction."""
    from api.routes.audit import _build_pdf_text_provider

    provider = _build_pdf_text_provider()
    return provider(path) if provider else None


def _cached_resolution(inv, sha256: str):
    """This PDF's accession resolution, from the memo on disk. CACHE-ONLY.

    Identifying is the accession tab's own verb, separately gated because it
    leaves the machine. Creating an item must never trigger one — a user who
    has never enabled online identification still gets everything the file
    and the last scan already know. A cached entry is revalidated against the
    CURRENT bib index (``/rows``' rule), so a paper catalogued since the
    lookup shows as a link rather than as something to create again.
    """
    from accession import cache as accession_cache
    from accession.candidates import Resolution
    from api.routes.accession import _apply_bib_check, _bib_doi_index

    cache = accession_cache.load_cache()
    stored = accession_cache.lookup(cache, sha256)
    if stored is None:
        return None, None
    res = Resolution.from_jsonable(stored)
    if res is None:
        return None, None
    res = _apply_bib_check(res, _bib_doi_index(inv))
    return (res.to_jsonable(),
            accession_cache.lookup_queried_at(cache, sha256))


def _draft(path: Path, rel: str, settings, inv):
    """The preview proposal — provenance, identifiers, and the cached draft.

    No network: everything here is the PDF itself plus memos the scan and any
    earlier identification already wrote, so opening the form costs nothing a
    user would notice and reveals nothing to anyone.
    """
    from accession import identify
    from accession.review import identity as _identity
    from audit.core import pdf_identifiers as pdf_ids
    from zotcreate import propose

    prov = _provenance(path, settings)
    text = _page1_text(path)
    klass = prov.get("klass") or "article"
    try:
        doi = pdf_ids.extract_embedded_doi(path) or None
    except Exception:  # noqa: BLE001
        doi = None
    if not doi:
        doi = pdf_ids.extract_printed_identifier(text) if text else None
    title = identify.extract_title(text) if text else None
    isbn = identify.extract_isbn(text) if text else None
    physical = _identity(path)
    sha256 = physical["sha256"]
    resolution, queried_at = _cached_resolution(inv, sha256)
    from accession.review import effective_resolution
    resolution, review_token = effective_resolution(settings.vault_root, rel, physical, resolution)
    if review_token and (resolution or {}).get("outcome") == "none":
        doi = None
    # The filename is NOT a fallback title. `abbott_2021.pdf` would fill the
    # form with "abbott 2021", which is worse than an empty required field:
    # the user must notice and retype it either way, but only one of the two
    # can be created by accident. When page-1 extraction found nothing, the
    # title stays empty and the create refuses until a human writes one.
    draft = propose.draft_from_resolution(
        resolution, klass=klass, fallback_title=title,
        fallback_doi=doi, source_url=prov.get("source_url"),
        filename_stem=path.stem)
    # A resolution the bib check turned into a link is not something to
    # create: the paper is already catalogued, and the remedy is the
    # accession tab's own link verb, not a second Zotero item for it.
    linked_key = ((resolution or {}).get("citation_key")
                  if (resolution or {}).get("outcome") == "link" else None)
    return {
        "pdf_rel": rel,
        "pdf_sha256": sha256,
        "physical": physical,
        "review_token": review_token,
        "klass": klass,
        "pages": prov.get("pages"),
        "producer": prov.get("producer"),
        "source_url": prov.get("source_url"),
        "doi": doi,
        "isbn": isbn,
        "title": title or path.stem.replace("_", " "),
        "suggested_item_type": draft["item_type"],
        "filename": path.name,
        "draft": draft,
        # The first page as text, so the one field the user most often has to
        # fix can be fixed without leaving the form. Measured on 40 sampled
        # pending PDFs: 29 yield a title and roughly 40% of those are a
        # running header or an acknowledgement line rather than the title.
        "page1_excerpt": (text or "")[:_EXCERPT_CHARS] or None,
        "already_in_bib": linked_key,
        "resolution": {
            "outcome": (resolution or {}).get("outcome"),
            "tier": (resolution or {}).get("tier"),
            "queried_at": queried_at,
            "consulted": list((resolution or {}).get("consulted") or []),
        } if resolution else None,
    }


def _clean_fields(raw):
    """``(fields, error)``. Refuses rather than silently dropping: a create
    form that quietly discarded a field would put a user's typing into an
    item they never get to see before it is made."""
    if raw in (None, ""):
        return {}, None
    if not isinstance(raw, dict):
        return None, "fields must be an object."
    if len(raw) > _MAX_FIELDS:
        return None, f"At most {_MAX_FIELDS} fields."
    out = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not _FIELD_NAME_RE.match(name):
            return None, f"Invalid field name {str(name)[:40]!r}."
        if value in (None, ""):
            continue
        if not isinstance(value, (str, int, float)):
            return None, f"Field {name!r} must be text."
        text = str(value)
        if len(text) > _MAX_FIELD_CHARS:
            return None, f"Field {name!r} is too long."
        out[name] = text
    return out, None


def _clean_creators(raw):
    if raw in (None, ""):
        return [], None
    if not isinstance(raw, list):
        return None, "creators must be a list."
    if len(raw) > _MAX_CREATORS:
        return None, f"At most {_MAX_CREATORS} creators."
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            return None, "Each creator must be an object."
        ctype = coerce_string_max_len(entry.get("creatorType"), 40) or "author"
        if not _FIELD_NAME_RE.match(ctype):
            return None, f"Invalid creator type {ctype[:40]!r}."
        if entry.get("name"):
            out.append({"creatorType": ctype,
                        "name": coerce_string_max_len(entry["name"], 200)})
            continue
        last = coerce_string_max_len(entry.get("lastName"), 200) or ""
        if not last:
            return None, "A creator needs a last name or a single name."
        out.append({"creatorType": ctype, "lastName": last,
                    "firstName": coerce_string_max_len(entry.get("firstName"), 200) or ""})
    return out, None


@zotcreate_bp.route("/api/zotcreate/preview", methods=["POST"])
def api_zotcreate_preview():
    """Everything the create form needs, and nothing of the USER'S written.

    Not "nothing written": classifying a PDF goes through the bridge's own
    classifier, which memoises into ``doi_cache.json`` — a regenerable cache
    the scan owns. Worth stating rather than discovering from an mtime, the
    same disclosure the accession dry run carries.
    """
    ctx, err = _gates()
    if err:
        return err
    inv, settings, cfg = ctx
    hit = _resolve(inv, settings, (request.get_json(silent=True) or {}).get("pdf"))
    if hit is None:
        return jsonify({"error": "Not an orphan or pending PDF of the cached "
                                 "scan."}), 400
    path, rel = hit
    from accession.review import ReviewConflict
    try:
        draft = _draft(Path(path), rel, settings, inv)
    except ReviewConflict as exc:
        # The file moved under the read. That is a state of the world, not a
        # server fault, and the create route already answers 409 for it — a
        # 500 here would send the user hunting for a bug in the app.
        return jsonify({"error": sanitise_error_msg(exc)}), 409
    except Exception as exc:  # noqa: BLE001
        logger.exception("zotcreate preview failed")
        return jsonify({"error": sanitise_error_msg(exc)}), 500

    from tagging import zotero_write
    from zotcreate import attempts

    transport = zotero_write.resolve_transport(cfg)
    _identity, stage, retained = attempts.lookup(settings.vault_root,
                                                 draft["pdf_sha256"])
    from accession import reconciliation
    target, target_error, web_admission = None, None, None
    try:
        if transport.is_web:
            from accession import web_snapshot
            library = f'users/{zotero_write.resolve_user_id(transport.base_url, transport.api_key)}'
            observed = web_snapshot.observe(transport.base_url, library, api_key=transport.api_key)
            target = dict(base=observed['base'], library=library)
            web_admission = web_snapshot.admission(observed, path, draft['draft']['fields'], expected_target=target)
        else:
            observed = reconciliation.retain_history(reconciliation.saved(settings), reconciliation.observe(settings))
            target = dict(base=transport.base_url, library='users/0', server_id=observed['database'])
    except (OSError, ValueError, AttributeError, sqlite3.Error, zotero_write.ZoteroError) as exc:
        target_error = sanitise_error_msg(exc)
    return jsonify({
        **draft,
        "target": target, "target_error": target_error, "web_admission": web_admission,
        "item_types": list(zotero_write.PARENT_ITEM_TYPES),
        "transport": transport.kind,
        # On Web, eligibility comes from the COMPLETE observation's own
        # admission verdict rather than from a blanket refusal: the local
        # database cannot prove absence for that library, but
        # `web_snapshot.observe` + `admission` can, and that is what the
        # create path now runs immediately before its POST.
        "can_write": bool(transport.can_write and target and (
            not transport.is_web or (web_admission or {}).get("eligible"))),
        "registration_block_reason": (
            target_error if not transport.is_web
            else target_error or (
                None if (web_admission or {}).get("eligible")
                else ("The complete Web observation shows a conflict: " + "; ".join(
                    f"{x['key']}: {x['reason']}"
                    for x in ((web_admission or {}).get("conflicts") or [])[:4]))
                if web_admission else
                "The Web library could not be observed completely.")),
        "attempt": {
            "stage": stage,
            "retained": retained,
            "message": (None if stage == attempts.STAGE_FRESH
                        else attempts.block_message(stage, retained)),
        },
    })


@zotcreate_bp.route("/api/zotcreate/create", methods=["POST"])
def api_zotcreate_create():
    """Create the parent, link the PDF under it, journal both as one batch."""
    data = request.get_json(silent=True) or {}
    dry_run = coerce_bool(data.get("dry_run")) is True
    if not dry_run and coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "Confirmation required."}), 400

    ctx, err = _gates()
    if err:
        return err
    inv, settings, cfg = ctx

    hit = _resolve(inv, settings, data.get("pdf"))
    if hit is None:
        return jsonify({"error": "Not an orphan or pending PDF of the cached "
                                 "scan."}), 400
    path, rel = hit
    path = Path(path)

    from tagging import zotero_write
    item_type = coerce_string_max_len(data.get("item_type"), 40) or ""
    if item_type not in zotero_write.PARENT_ITEM_TYPES:
        return jsonify({"error": f"Unsupported item type {item_type[:40]!r}.",
                        "item_types": list(zotero_write.PARENT_ITEM_TYPES)}), 400
    fields, ferr = _clean_fields(data.get("fields"))
    if ferr:
        return jsonify({"error": ferr}), 400
    creators, cerr = _clean_creators(data.get("creators"))
    if cerr:
        return jsonify({"error": cerr}), 400
    if not fields.get("title"):
        return jsonify({"error": "A title is required."}), 400

    # The file-renaming gate, BEFORE the transport so a dry run carries the
    # same advice. It matters more here than on attach: the parent we are
    # about to mint is the thinnest metadata in the library, so Zotero's own
    # rename template would produce exactly the `_.pdf` shape of 2026-09-07.
    from api.routes.zotlink import _rule_matches, rename_gate
    gate_err, guard_rules, soft_problems = rename_gate(
        coerce_bool(data.get("acknowledge_plugins")) is True)
    if gate_err is not None:
        return gate_err
    if guard_rules is not None and not _rule_matches(guard_rules, path.stem):
        return jsonify({
            "error": ("Attanger's skip rule does not cover this filename, so "
                      "the plugin would rename the vault PDF as soon as it is "
                      "linked. Widen the rule, or re-send with "
                      "acknowledge_plugins: true."),
            "reason": "plugin_would_rename"}), 409

    from accession.review import ReviewConflict
    from accession.review import identity as _identity
    try:
        pdf_sha256 = _identity(path)["sha256"]
    except (ReviewConflict, OSError, ValueError) as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 409

    if not dry_run and data.get("physical") != _identity(path):
        return jsonify(error="PDF preview identity missing or changed. Reopen the registration preview."), 409
    from accession import review
    _, current_token = review.effective_resolution(settings.vault_root, rel, _identity(path), None)
    if not dry_run and data.get("review_token") != current_token:
        return jsonify(error="Review changed. Reopen the registration preview."), 409

    from zotcreate import attempts
    _id, stage, retained = attempts.lookup(settings.vault_root, pdf_sha256)
    if dry_run:
        return jsonify({
            "ok": stage in (attempts.STAGE_FRESH, attempts.STAGE_PARENT_DONE),
            "dry_run": True, "pdf_rel": rel, "item_type": item_type,
            "fields": fields, "creators": creators, "path": str(path),
            "attempt": {"stage": stage, "retained": retained},
            "message": (attempts.block_message(stage, retained)
                        if stage not in (attempts.STAGE_FRESH,
                                         attempts.STAGE_PARENT_DONE)
                        else "Nothing written. Confirm to create."),
            "warnings": soft_problems,
        })

    if not _ZOTCREATE_GATE.acquire(blocking=False):
        return jsonify({"error": "Another create is running; try again."}), 429
    try:
        payload, status = _create_one(
            settings, cfg, path=path, rel=rel, pdf_sha256=pdf_sha256,
            item_type=item_type, fields=fields, creators=creators,
            expected_physical=data.get("physical"), expected_review=data.get("review_token"), expected_target=data.get("target"))
    except ReviewConflict as exc:
        return jsonify(error=sanitise_error_msg(exc)), 409
    except Exception as exc:  # noqa: BLE001
        from tagging import zotero_write as _zw
        # Retryable vs not, and neither is a server fault: an unreachable
        # library should be tried again, an answer we got and did not like
        # should not. Collapsing both into 500 makes the app look broken
        # when Zotero is merely offline.
        if isinstance(exc, _zw.ZoteroUnavailable):
            return jsonify(error=sanitise_error_msg(exc)), 503
        if isinstance(exc, _zw.ZoteroError):
            return jsonify(error=sanitise_error_msg(exc)), 502
        logger.exception("zotcreate create failed")
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    finally:
        _ZOTCREATE_GATE.release()
    if soft_problems:
        payload.setdefault("warnings", []).extend(soft_problems)
    return jsonify(payload), status


def _create_one(settings, cfg, *, path, rel, pdf_sha256, item_type, fields,
                creators, expected_physical=None, expected_review=None, expected_target=None,
                web_observe=None, batch_id=None,
                with_reconciliation=True, decision_ref=None):
    """Reserve → parent POST → attach → journal. ``(payload, status)``.

    The batch route (T2) passes ``web_observe`` — a delta-refreshed snapshot
    in place of a fresh complete observation.

    The absence proof runs ONCE, immediately before the reservation, on both
    paths. It moved there from ``before_dispatch`` because a Web observation
    that timed out after the reservation left ``parent_unknown`` with nothing
    POSTed (2026-09-19 review A4); the second, adjacent call that briefly rode
    with that fix is gone (2026-09-20), since two identical observations with
    nothing between them cost a second complete read of the library — minutes,
    on Web — and prove exactly what the first one proved. Between the proof
    and the POST there is then only a local SQLite insert.

    Network runs with NO app lock held (the writers' standing rule); the
    obsidian op-lock is taken once, at the end, to append both restore ops.
    A crash before that hold loses only the undo records — the attempt store
    still holds both keys, which is what makes the state inspectable rather
    than merely lost.
    """
    from tagging import journal
    from tagging.writers import (
        zotero_attachment_create_journal,
        zotero_attachment_create_post,
        zotero_attachment_create_prepare,
        zotero_item_create_journal,
        zotero_item_create_post,
        zotero_item_create_prepare,
    )
    from zotcreate import attempts

    prepared = zotero_item_create_prepare(cfg, item_type=item_type,
                                          fields=fields, creators=creators)
    if "early" in prepared:
        early = prepared["early"]
        # 503 for "Zotero cannot be written to right now" — unreachable, or a
        # transport with no write path — and 502 for an answer we got and did
        # not like. The first is retryable, the second is not, and a client
        # that cannot tell them apart retries the wrong one.
        retryable = (early.get("status") == "unreachable"
                     or early.get("reason") == "read_only_transport")
        return early, (503 if retryable else 502)

    from accession import reconciliation, review
    c = prepared["commit"]

    def _local_curation():
        """Our OWN decisions about this PDF, read from ``mapping.json``.

        Transport-independent by construction: a PDF the user has already
        mapped, or refused as "not for Zotero", must not get a new item
        whatever Zotero itself holds. Kept separate from the absence proof
        below because the two answer different questions and the Web path
        can only replace the second one.

        A detach EXCLUSION is deliberately not a refusal (maintainer
        decision 2026-09-18): it says "this file is not THAT record", not
        "this file must not become A record". It stays in mapping.json and
        keeps the automatic ladder from re-linking the wrong record.
        """
        from audit.engine import bridge

        state = reconciliation.mapping_state(
            bridge._load_mapping_all(settings.mapping_file), rel)
        if state["keys"] or state["no_match"]:
            raise review.ReviewConflict(
                "Existing association or conflicting decision requires "
                "reconciliation; no create sent.")
        return state

    def web_preflight():
        """The Web absence proof: a COMPLETE observation, then admission.

        The local Zotero database cannot prove absence for the Web library —
        it may lag a sync, and "I did not find it" from a lagging copy is
        exactly the evidence a duplicate barrier must not accept. What can
        prove it is ``web_snapshot.observe``, whose completeness is
        established rather than assumed: Total-Results and a stable
        Last-Modified-Version across every page of items AND trash, then a
        re-probe of both. ``admission`` then applies the same duplicate
        reasoning the Desktop preflight does — the DOI already present
        (trash included), an attachment already pointing at this exact file,
        an unresolved relative linked path anywhere in the library, an
        imported copy whose checksum is unknown or identical — and refuses
        on any of them.

        Deliberately NOT a substitute for the Desktop path's workspace-row
        check: that row is built from the local observation, so on Web the
        curation half is read from ``mapping.json`` directly (above) and the
        Zotero half comes from the observation.
        """
        from accession import web_snapshot

        current = review.identity(path)
        if not review.same_identity(current, expected_physical):
            raise review.ReviewConflict("PDF changed before dispatch.")
        review.validate_creation(settings.vault_root, rel, current,
                                 expected_review, fields)
        state = _local_curation()
        observed = (web_observe() if web_observe else
                    web_snapshot.observe(c["transport"].base_url, c["lib_path"],
                                         api_key=c["api_key"]))
        # An observation that cannot prove completeness, or a target that
        # moved since the preview, is a state of the world — the same class
        # the preview route already answers 409 for. A 500 here would send
        # the user hunting for a bug in the app. `ZoteroUnavailable` is
        # deliberately NOT swallowed: it is retryable and the route answers
        # 503 for it, which a 409 would misreport as a decision to make.
        try:
            verdict = web_snapshot.admission(observed, path, fields,
                                             expected_target=expected_target)
        except ValueError as exc:
            raise review.ReviewConflict(str(exc)) from exc
        if not verdict["eligible"]:
            raise review.ReviewConflict(
                "The complete Web observation shows a conflict: "
                + "; ".join(f"{x['key']}: {x['reason']}"
                            for x in verdict["conflicts"][:4]))
        c["mapping_state"] = state

    def preflight():
        current = review.identity(path)
        if not review.same_identity(current, expected_physical):
            raise review.ReviewConflict("PDF changed before dispatch.")
        review.validate_creation(settings.vault_root, rel, current, expected_review, fields)
        observed = reconciliation.retain_history(reconciliation.saved(settings), reconciliation.observe(settings))
        if any(i['item_type'] == 'attachment' and i.get('link_mode') == 'linked_file'
               and reconciliation.attachment_path(i, settings, observed) is None
               for i in observed['items'].values()):
            raise review.ReviewConflict('An unresolved linked-file location prevents complete duplicate inspection. Verify the attachment base before creating.')
        row = next(r for r in reconciliation.rows(settings, observed, only=rel) if r['pdf']['rel'] == rel)
        if row['state'] in ('Discrepancies', 'Accepted differences') or row['associations'] or row['imported_copies'] or row['mapping']['keys'] or row['mapping']['no_match']:
            # An exclusion alone is not a refusal to create; see _local_curation.
            raise review.ReviewConflict("Existing association or conflicting decision requires reconciliation; no create sent.")
        from audit.core.bib import normalise_doi
        doi = normalise_doi(fields.get('DOI') or '')
        if doi and any(normalise_doi(i['fields'].get('DOI') or '') == doi for i in observed['items'].values() if i['item_type'] not in ('attachment','note')):
            raise review.ReviewConflict("This DOI already exists in Zotero, including trash. Review the existing item.")
        if expected_target != dict(base=c['transport'].base_url, library=c['lib_path'], server_id=observed['database']):
            raise review.ReviewConflict('Zotero target changed since preview. Reopen the registration preview.')
        c['server_id'] = observed['database']
        c['mapping_state'] = row['mapping']
    gate = web_preflight if c["transport"].is_web else preflight
    _, existing_stage, _ = attempts.lookup(settings.vault_root, pdf_sha256)
    if existing_stage == attempts.STAGE_FRESH:
        # Moment-of-dispatch proof BEFORE the reservation. Running it as
        # ``before_dispatch`` after reserve left parent_unknown when a Web
        # observation timed out with nothing POSTed (2026-09-19 review A4).
        gate()
    identity, stage, retained = attempts.reserve(
        settings.vault_root, pdf_sha256, pdf_rel=rel, item_type=item_type,
        title=fields.get("title", ""),
        library=prepared["commit"]["lib_path"],
        base=prepared["commit"]["transport"].base_url, server_id=c.get("server_id"))

    parent = None
    parent_result = None
    if stage == attempts.STAGE_FRESH:
        c['acknowledge'] = lambda key: attempts.acknowledge_parent(
            identity, key=str(key), version=getattr(key, 'creation_version', None),
            creation_data=getattr(key, 'creation_data', None))
        parent_result, parent = zotero_item_create_post(prepared)
        if parent is None:
            if parent_result.get("status") == "refused":
                # A 4xx we SAW: nothing was minted. Retire so the PDF is not
                # blocked behind a 6-min settle (2026-09-19 review A5).
                try:
                    seq = attempts.retire_never_created(identity, evidence={
                        "http_code": parent_result.get("http_code"),
                        "message": parent_result.get("message"),
                        "status": "refused",
                    })
                except RuntimeError:
                    seq = None
                return ({"ok": False, "stage": "parent", "never_created": True,
                         "retired": seq, **parent_result}, 409)
            return {"ok": False, "stage": "parent", **parent_result}, 502
    elif stage == attempts.STAGE_PARENT_DONE:
        # Resume: the parent is proven, only the attachment is unfinished.
        parent = _reread_parent(prepared, retained)
        if parent is None:
            return ({"ok": False, "stage": "parent",
                     "message": ("An item was created for this PDF but could "
                                 "not be read back; not linking the file. "
                                 f"Check item {retained.get('parent_key')} in "
                                 "Zotero."),
                     "attempt": {"stage": stage, "retained": retained}}, 409)
        parent_result = {"version": (retained.get("parent") or {}).get("version"),
                         "creation_data": (retained.get("parent") or {}).get("creation_data")}
    else:
        return ({"ok": False, "stage": "blocked",
                 "message": attempts.block_message(stage, retained),
                 "attempt": {"stage": stage, "retained": retained}}, 409)

    if stage == attempts.STAGE_PARENT_DONE and c["transport"].is_web:
        # Resume on Web: the parent is proven, so what must be re-established
        # is that nothing ELSE has since claimed this file and that our own
        # curation is unchanged. The observation is complete, so an
        # attachment pointing at this path is found if it exists — and if it
        # hangs off our own proven parent, the children re-read further down
        # adopts it rather than reposting.
        from accession import web_snapshot

        try:
            observed = (web_observe() if web_observe else
                        web_snapshot.observe(c["transport"].base_url, c["lib_path"],
                                             api_key=c["api_key"]))
        except Exception as exc:  # noqa: BLE001 — a resume must not guess
            return ({"ok": False, "stage": "recovery",
                     "message": ("The Web library could not be observed completely, "
                                 "so the retained attempt cannot be resumed safely: "
                                 f"{sanitise_error_msg(exc)}")}, 409)
        foreign = [
            key for key, item in observed["items"].items()
            if item.get("itemType") == "attachment"
            and item.get("linkMode") == "linked_file"
            and (item.get("path") or "") == str(path)
            and item.get("parentItem") != parent.item_key
            and not item.get("deleted")
        ]
        if foreign:
            return ({"ok": False, "stage": "recovery",
                     "message": ("Another Zotero item already links this file "
                                 f"({', '.join(foreign[:4])}); inspect the retained "
                                 "attempt. No POST sent.")}, 409)
        try:
            c['mapping_state'] = _local_curation()
        except review.ReviewConflict as exc:
            return {"ok": False, "message": str(exc)}, 409
    elif stage == attempts.STAGE_PARENT_DONE:
        observed = reconciliation.retain_history(reconciliation.saved(settings), reconciliation.observe(settings))
        row = next(r for r in reconciliation.rows(settings, observed, only=rel) if r['pdf']['rel'] == rel)
        if (observed['database'] != retained.get('server_id') or row['imported_copies']
                or any(a['parent'] != parent.item_key or a['deleted'] for a in row['associations'])
                or row['mapping']['keys'] or row['mapping']['no_match']):
            return {"ok":False,"message":"Current association or curation conflicts with the retained attempt. Use recovery inspection; no POST sent."},409
        c['mapping_state'] = row['mapping']
    att_prepared = zotero_attachment_create_prepare(cfg, parent, path)
    if "early" in att_prepared:
        return ({"ok": False, "stage": "attachment",
                 "item_key": parent.item_key, **att_prepared["early"],
                 "message": (att_prepared["early"].get("message", "") +
                             f" The item was created (key {parent.item_key}); "
                             "only the file link is missing.")}, 502)
    ac = att_prepared['commit']
    def check_source():
        if not review.same_identity(review.identity(path), expected_physical):
            raise review.ReviewConflict('PDF changed before attachment dispatch.')
        review.validate_creation(settings.vault_root, rel, expected_physical, expected_review, fields)
        from audit.engine import bridge
        actual = reconciliation.mapping_state(bridge._load_mapping_all(settings.mapping_file), rel)
        if c.get('mapping_state') is not None and actual != c['mapping_state']:
            raise review.ReviewConflict('Mapping changed before attachment dispatch.')
    if stage == attempts.STAGE_PARENT_DONE:
        from tagging import zotero_write
        children = zotero_write.get_item_children(c['transport'].base_url, c['lib_path'], parent.item_key,
                                                  api_key=c['api_key'], server_id=parent.server_id)
        matches = [child for child in children if child.get('itemType') == 'attachment'
                   and child.get('linkMode') == 'linked_file' and child.get('path') == str(path)
                   and child.get('parentItem') == parent.item_key and not child.get('deleted')]
        check_source()
        if len(matches) == 1:
            attempts.acknowledge_attachment(identity, key=matches[0]['key'], adopted=True)
            return {"ok": True, "undo": "manual", "item_key": parent.item_key,
                    "attachment_key": matches[0]['key'], "note": "Existing attachment recovered. No POST sent; undo requires manual review."}, 200
        if len(matches) > 1 or retained.get('attachment_dispatch') != 'not_sent':
            return {"ok": False, "stage": "recovery", "message": "Attachment outcome remains uncertain. Automatic repost is blocked; inspect the retained attempt and Zotero children."}, 409
    def before_attachment():
        check_source()
        attempts.mark_attachment_dispatch(identity)
    ac['before_dispatch'] = before_attachment
    ac['acknowledge'] = lambda key: attempts.acknowledge_attachment(
        identity, key=str(key), version=getattr(key, 'creation_version', None),
        creation_data=getattr(key, 'creation_data', None))
    att_result, att_key, att_version = zotero_attachment_create_post(att_prepared)
    if not att_key:
        return ({"ok": False, "stage": "attachment", "item_key": parent.item_key, **att_result}, 502)

    # ---- one short hold to journal both creates as one batch --------------
    from rag.vault import obsidian_manager

    batch_id = batch_id or journal.new_op_id({})
    journal_warning, op_ids = None, {}
    epoch = None
    for _ in range(20):
        epoch = obsidian_manager.try_acquire_lock(ttl=600)
        if epoch:
            break
        time.sleep(1)
    if not epoch:
        journal_warning = (
            "could not acquire the op-lock to journal the creates; the item "
            "and its linked file exist in Zotero but have no restore record "
            "— remove them in Zotero if that was not wanted")
    else:
        try:
            manifest = journal.load(settings.vault_root, cfg)
            pop = zotero_item_create_journal(
                prepared=prepared, parent=parent,
                version=parent_result.get("version"),
                creation_data=parent_result.get("creation_data"),
                pdf_rel=rel, pdf_sha256=pdf_sha256,
                attachment_key=str(att_key), manifest=manifest,
                batch_id=batch_id)
            aop = zotero_attachment_create_journal(
                prepared=att_prepared, citation_key=None, att_key=str(att_key),
                version=att_version, manifest=manifest, batch_id=batch_id)
            if decision_ref:
                pop["decision_ref"] = aop["decision_ref"] = decision_ref
            op_ids = {"item": pop["op_id"], "attachment": aop["op_id"]}
            journal.prune(settings.vault_root, cfg, manifest,
                          protect_batch_id=batch_id)
            journal.save(settings.vault_root, cfg, manifest)
        except Exception as exc:  # noqa: BLE001
            logger.exception("zotcreate journal failed")
            journal_warning, batch_id, op_ids = sanitise_error_msg(exc), None, {}
        finally:
            obsidian_manager.release_lock(epoch)

    # An op with no creation baseline reverts by refusing (see
    # `_revert_zotero_item_create_prepare`). That is the right conservatism,
    # but the response must not offer an undo the journal will decline.
    undo = "manual"
    payload = {
        "ok": True, "undo": undo, "pdf_rel": rel, "item_key": parent.item_key,
        "item_type": parent.item_type, "title": parent.title,
        "attachment_key": str(att_key), "attachment_status": att_result.get("status"),
        "batch_id": batch_id, "op_ids": op_ids,
        "transport": prepared["commit"]["transport"].kind,
        "note": "Creation acknowledged. Refresh Zotero changes to verify registration; bibliography export may arrive later. Automatic Desktop undo is unavailable.",
    }
    if journal_warning:
        payload["journal_warning"] = journal_warning
    if not with_reconciliation:
        return payload, 200
    try:
        from api.routes.pending import workspace
        payload['reconciliation'] = workspace(settings, force=True)
    except Exception as exc:  # noqa: BLE001
        payload['reconciliation_warning'] = sanitise_error_msg(exc)
    return payload, 200


def _reread_parent(prepared, retained):
    """Re-verify a parent from an interrupted attempt; ``None`` if it cannot
    be confirmed to be the item we made."""
    from tagging import zotero_write

    c = prepared["commit"]
    key = (retained or {}).get("parent_key")
    # A Web attempt carries no server id (the Desktop database identity has no
    # Web equivalent; all 8 live Web attempts store None), so requiring one
    # made every Web `parent_done` resume refuse forever.
    if (not key or retained.get('base') != c['transport'].base_url
            or retained.get('library') != c['lib_path']
            or (not c['transport'].is_web and not retained.get('server_id'))
            or not (retained.get('parent') or {}).get('creation_data')):
        return None
    try:
        data, version = zotero_write.get_item(
            c["transport"].base_url, c["lib_path"], key, api_key=c["api_key"],
            **({} if c["transport"].is_web else {"server_id": retained["server_id"]}))
    except (zotero_write.ZoteroError, zotero_write.ZoteroUnavailable):
        return None
    if data.get("deleted") or data != retained["parent"]["creation_data"]:
        return None
    from tagging.writers import NewParent
    return NewParent(key, data["itemType"], data.get("title") or "",
                     doi=data.get("DOI") or "",
                     server_id=getattr(version, "server_id", None))


@zotcreate_bp.route('/api/zotcreate/recover', methods=['POST'])
def api_zotcreate_recover():
    """Inspect the original target and recover one exact acknowledged attachment.

    Never POST to Zotero, never remove a barrier, never fabricate undo evidence.
    """
    from accession import review
    from tagging import zotero_write
    from tagging.writers import zotero_item_create_prepare
    from zotcreate import attempts
    data = request.get_json(silent=True) or {}
    if data.get('confirm') is not True:
        return jsonify(error='Explicit confirmation required.'), 400
    ctx, err = _gates()
    if err:
        return err
    inv, settings, cfg = ctx
    from api.routes.pending import target
    try:
        hit = target(settings, data.get('pdf'))
    except (OSError, ValueError):
        return jsonify(error='PDF is not in the bibliography scope.'), 400
    path, rel = hit
    try:
        physical = review.identity(Path(path))
        if physical != data.get('physical'):
            raise review.ReviewConflict('PDF changed since inspection.')
        identity, stage, retained = attempts.lookup(settings.vault_root, physical['sha256'])
        if not retained or not retained.get('parent_key'):
            return jsonify(message='Parent outcome unknown. Search Zotero manually; automatic retry remains blocked.', attempt=retained), 409
        prepared = zotero_item_create_prepare(cfg, item_type=retained['item_type'], fields={'title':retained['title']}, creators=[])
        if 'early' in prepared:
            return jsonify(prepared['early']), 409
        if prepared['commit']['transport'].is_web:
            from accession import web_snapshot
            c = prepared['commit']
            if retained.get('base') != c['transport'].base_url or retained.get('library') != c['lib_path']:
                raise ValueError('Original Web target differs from current configuration.')
            observed = web_snapshot.observe(retained['base'], retained['library'], api_key=c['api_key'])
            return jsonify(ok=True, **web_snapshot.inspect_attempt(observed, retained))
        parent = _reread_parent(prepared, retained)
        if parent is None:
            return jsonify(message='Original database, transport or parent evidence cannot be verified. Manual reconciliation required.', attempt=retained), 409
        c = prepared['commit']
        children = zotero_write.get_item_children(c['transport'].base_url, c['lib_path'], parent.item_key,
                                                  api_key=c['api_key'], server_id=parent.server_id)
        matches = [child for child in children if child.get('itemType') == 'attachment'
                   and child.get('parentItem') == parent.item_key and child.get('linkMode') == 'linked_file'
                   and child.get('path') == str(path) and not child.get('deleted')]
        if review.identity(Path(path)) != physical:
            raise review.ReviewConflict('PDF changed during recovery inspection.')
        if len(matches) != 1:
            return jsonify(message='No unique exact live attachment could be verified. Automatic repost remains blocked.', children=children), 409
        if stage == attempts.STAGE_PARENT_DONE:
            attempts.acknowledge_attachment(identity, key=matches[0]['key'], adopted=True)
        elif retained.get('attachment_key') != matches[0]['key']:
            return jsonify(message='Observed attachment differs from the acknowledged key; manual review required.'), 409
        return jsonify(ok=True, message='Existing attachment verified. No Zotero POST sent; retained duplicate barrier preserved.', undo='manual')
    except (OSError, ValueError, zotero_write.ZoteroError, zotero_write.ZoteroUnavailable) as exc:
        return jsonify(error=sanitise_error_msg(exc)), 409


@zotcreate_bp.route("/api/zotcreate/attempts")
def api_zotcreate_attempts():
    """This vault's retained item-create attempts, by stage (read-only).

    Added 2026-09-17 (annual cleanup T1): the attempt store is the only record
    of a create whose response was lost, and nothing listed it, so "no create
    is stuck at ``parent_unknown``" — a cleanup exit criterion — had no
    observable number. App-owned evidence only; no Zotero read.
    """
    from audit.config import AuditConfigError, load_settings
    from zotcreate import attempts

    _inv, settings = audit_manager.get_inventory()
    if settings is None:
        try:
            settings = load_settings()
        except AuditConfigError as exc:
            return jsonify({"error": sanitise_error_msg(exc)}), 400
    try:
        rows = attempts.list_attempts(settings.vault_root)
    except Exception:  # noqa: BLE001
        logger.exception("Cannot list retained item-create attempts")
        return jsonify({"error": "Retained create attempts cannot be read."}), 500
    stages: dict = {}
    for row in rows:
        stages[row["stage"]] = stages.get(row["stage"], 0) + 1
    try:
        retired = attempts.list_retired(settings.vault_root)
    except Exception:  # noqa: BLE001
        logger.exception("Cannot list retired item-create attempts")
        retired = None
    return jsonify({"rows": rows, "count": len(rows), "stages": stages, "retired": retired})


def _fold_title(text) -> str:
    import unicodedata
    folded = unicodedata.normalize("NFKD", str(text or "")).casefold()
    return " ".join("".join(c if c.isalnum() else " " for c in folded
                            if not unicodedata.combining(c)).split())


def _never_created_evidence(observed, retained, pdf_name):
    """Items in a COMPLETE observation that would contradict "never created":
    any non-child item whose folded title equals the reserved one, and any
    attachment whose stored path or filename names the PDF."""
    wanted = _fold_title(retained.get("title"))
    title_hits, file_hits = [], []
    for key, item in observed["items"].items():
        kind = item.get("itemType")
        if kind == "attachment":
            stored = str(item.get("path") or "")
            if pdf_name and (Path(stored.replace("attachments:", "")).name == pdf_name
                             or item.get("filename") == pdf_name):
                file_hits.append(dict(key=key, path=stored, deleted=item.get("deleted")))
        elif kind not in ("note", "annotation") and wanted and _fold_title(item.get("title")) == wanted:
            title_hits.append(dict(key=key, item_type=kind, title=item.get("title"),
                                   deleted=item.get("deleted")))
    return title_hits, file_hits


@zotcreate_bp.route("/api/zotcreate/settle-never-created", methods=["POST"])
def api_zotcreate_settle_never_created():
    """Unblock a PDF whose earlier create never reached Zotero (2026-09-17).

    ``{pdf, confirm?, dry_run?}``. Maintainer-authorised override of the
    attempt barrier (annual cleanup decision): only for a ``parent_unknown``
    attempt against the Web library, and only when a COMPLETE observation of
    that library — live items AND trash, verified by result counts and library
    version (``web_snapshot.observe``) — holds no item with the reserved title
    and no attachment naming the file. The dry run returns that evidence and
    writes nothing; a confirmed run MOVES the attempt to ``retired_attempts``
    with the evidence (``reinstate-attempt`` is the undo). Never POSTs.
    """
    from accession import review, web_snapshot
    from api.routes.pending import target
    from audit.config import AuditConfigError, load_settings
    from tagging import zotero_write
    from zotcreate import attempts

    data = request.get_json(silent=True) or {}
    dry_run = coerce_bool(data.get("dry_run")) is True
    if not dry_run and coerce_bool(data.get("confirm")) is not True:
        return jsonify(error="Confirmation required (or send dry_run: true)."), 400
    try:
        settings = load_settings()
        path, rel = target(settings, data.get("pdf"))
        physical = review.identity(Path(path))
    except (AuditConfigError, OSError, ValueError, review.ReviewConflict) as exc:
        return jsonify(error=sanitise_error_msg(exc)), 400
    identity, stage, retained = attempts.lookup(settings.vault_root, physical["sha256"])
    if stage != attempts.STAGE_PARENT_UNKNOWN:
        return jsonify(error=f"This PDF has no unacknowledged creation attempt (stage: {stage}).",
                       stage=stage), 409
    base = (retained.get("base") or "").rstrip("/")
    library = retained.get("library") or ""
    if not base.startswith("https://") or not library.startswith(("users/", "groups/")):
        return jsonify(error="Only an attempt against the Zotero Web library can be settled this way; "
                             "this one targeted the Desktop database.", attempt=retained), 409
    api_key = zotero_write.api_key_from_env()
    if not api_key:
        return jsonify(error="ZOTERO_API_KEY is not set; the Web library cannot be observed."), 503
    if not _ZOTCREATE_GATE.acquire(blocking=False):
        return jsonify(error="A create is running; try again."), 429
    try:
        observed = web_snapshot.observe(base, library, api_key=api_key)
        title_hits, file_hits = _never_created_evidence(observed, retained, Path(path).name)
        evidence = dict(library=library, base=base, observed_version=observed["version"],
                        counts=observed["counts"], checked_at=observed["checked_at"],
                        complete=observed["complete"], title=retained.get("title"),
                        pdf_rel=rel, pdf_sha256=physical["sha256"],
                        title_matches=title_hits, file_matches=file_hits)
        if title_hits or file_hits:
            return jsonify(error="The Web library holds an item with this title or a link to this "
                                 "file; the attempt stays blocked. Inspect those items.",
                           reason="possible_existing_item", evidence=evidence), 409
        if dry_run:
            return jsonify(ok=True, dry_run=True, pdf_rel=rel, evidence=evidence,
                           message="No matching item in the complete Web observation. Confirm to "
                                   "retire the attempt and unblock this PDF.")
        seq = attempts.retire_never_created(identity, evidence=evidence)
    except (zotero_write.ZoteroError, zotero_write.ZoteroUnavailable, ValueError, RuntimeError) as exc:
        return jsonify(error=sanitise_error_msg(exc)), 409
    except sqlite3.Error:
        logger.exception("Cannot retire creation attempt for %s", rel)
        return jsonify(error="The attempt store cannot be written; nothing was changed."), 500
    finally:
        _ZOTCREATE_GATE.release()
    from core.utils import log_vault_write
    log_vault_write("zotcreate_attempt_retired", rel,
                    f"never_created; retired #{seq}; web version {evidence['observed_version']}")
    return jsonify(ok=True, retired=seq, pdf_rel=rel, evidence=evidence,
                   undo=f"POST /api/zotcreate/reinstate-attempt {{seq: {seq}, confirm: true}}",
                   message="Attempt retired with its evidence; a new create for this PDF reserves afresh.")


@zotcreate_bp.route("/api/zotcreate/reinstate-attempt", methods=["POST"])
def api_zotcreate_reinstate_attempt():
    """Undo ``settle-never-created``: move a retired attempt back (re-blocks)."""
    from zotcreate import attempts

    data = request.get_json(silent=True) or {}
    if coerce_bool(data.get("confirm")) is not True:
        return jsonify(error="Confirmation required."), 400
    seq = data.get("seq")
    if not isinstance(seq, int) or seq < 1:
        return jsonify(error="seq must be a positive integer."), 400
    if not _ZOTCREATE_GATE.acquire(blocking=False):
        return jsonify(error="A create is running; try again."), 429
    try:
        identity = attempts.reinstate(seq)
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 409
    except sqlite3.Error:
        logger.exception("Cannot reinstate retired attempt %s", seq)
        return jsonify(error="The attempt store cannot be read; nothing was changed."), 500
    finally:
        _ZOTCREATE_GATE.release()
    from core.utils import log_vault_write
    log_vault_write("zotcreate_attempt_reinstated", identity, f"retired #{seq} moved back")
    return jsonify(ok=True, reinstated=seq)


# ── batch create (T2, annual cleanup 2026-09-18) ───────────────────────────
#
# On the Web transport a single create costs two complete library
# observations (~6 minutes each, measured 2026-09-18), so 286 approved drafts
# looped through `/create` would take ~57 hours. This route shares ONE
# complete observation across the whole batch and carries it forward with
# Zotero's own sync deltas (`web_snapshot.BatchProof`): before each create it
# reads what changed since the last known version, accepts only the items
# this batch made, and STOPS on anything else. Measured before designing it:
# each single-object POST moves the library version by exactly one (all 8
# live attempts: parent N, attachment N+1), and an OPEN Zotero Desktop writes
# back to a freshly created parent (Better BibTeX's citation key: lancet's
# parent went 119945 → 119948) — so the batch runs with Desktop quit, and a
# write-back is a foreign change, never tolerated.
#
# Stop rules, each because continuing could burn more PDFs than it saves:
# a foreign change; any failure AFTER a reservation (a refused or lost POST
# leaves that PDF at `parent_unknown`, so the batch stops at the first one
# rather than burning N); a network error; a journal failure; a cancelled
# client. A refusal BEFORE the reservation (a conflict the proof found, our
# own curation, a missing file) is per-row and the batch continues.
#
# Rows carry exactly what the maintainer approved; the moment-in-time tokens
# (`physical`, `review_token`, `target`) are computed here at dispatch, which
# is where the single route's preview-binding compares them anyway. What binds
# the batch to a human decision is the decision file's approval digest,
# checked by the caller before it sends a single row.

#: Upper bound on one request. The live Phase 2 has 286 creates.
_BATCH_MAX_ROWS = 400

#: The longest silent stage is the opening complete observation (~6-7 min on
#: ~14,000 items); after it, every row emits a frame.
_BATCH_STALL_S = 3600


@zotcreate_bp.route("/api/zotcreate/batch", methods=["POST"])
def api_zotcreate_batch():
    """SSE: create many approved drafts against ONE Web observation.

    Body ``{rows: [{id?, pdf, item_type, fields, creators, decision_ref?}],
    confirm | dry_run, acknowledge_plugins?}``. One ``{"row"}`` frame per row
    reached (status ``created`` · ``already_created`` · ``admitted`` (dry run)
    · ``refused`` · ``failed``) and a terminal ``{"zotcreate_batch"}``. A dry
    run costs one observation and writes nothing.
    """
    data = request.get_json(silent=True) or {}
    dry_run = coerce_bool(data.get("dry_run")) is True
    if not dry_run and coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "Confirmation required."}), 400
    raw_rows = data.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        return jsonify({"error": "rows must be a non-empty list."}), 400
    if len(raw_rows) > _BATCH_MAX_ROWS:
        return jsonify({"error": f"At most {_BATCH_MAX_ROWS} rows per batch."}), 400

    ctx, err = _gates()
    if err:
        return err
    inv, settings, cfg = ctx

    from tagging import zotero_write
    rows, seen = [], set()
    for i, raw in enumerate(raw_rows):
        if not isinstance(raw, dict) or not isinstance(raw.get("pdf"), str):
            return jsonify({"error": f"Row {i + 1} must be an object with a pdf."}), 400
        if raw["pdf"] in seen:
            return jsonify({"error": f"Row {i + 1} repeats {raw['pdf'][:120]!r}."}), 400
        seen.add(raw["pdf"])
        row = {"id": coerce_string_max_len(raw.get("id"), 300) or f"row{i + 1}",
               "pdf": raw["pdf"],
               "decision_ref": coerce_string_max_len(raw.get("decision_ref"), 256)}
        item_type = coerce_string_max_len(raw.get("item_type"), 40) or ""
        fields, ferr = _clean_fields(raw.get("fields"))
        creators, cerr = _clean_creators(raw.get("creators"))
        problem = (None if item_type in zotero_write.PARENT_ITEM_TYPES
                   else f"Unsupported item type {item_type[:40]!r}.") or ferr or cerr
        if problem is None and not fields.get("title"):
            problem = "A title is required."
        row.update(item_type=item_type, fields=fields or {}, creators=creators or [],
                   error=problem)
        rows.append(row)

    transport = zotero_write.resolve_transport(cfg)
    if not transport.is_web:
        return jsonify({"error": (
            "A batch create proves absence with Web-library sync deltas, which "
            "the Desktop transport has no equivalent of. Use the Web transport "
            "(ZOTERO_API_KEY), or create items one at a time."),
            "reason": "web_transport_required"}), 409
    if not transport.can_write:
        return jsonify({"error": "The Web transport cannot write (no API key)."}), 503

    from api.routes.zotlink import rename_gate
    gate_err, guard_rules, soft_problems = rename_gate(
        coerce_bool(data.get("acknowledge_plugins")) is True)
    if gate_err is not None:
        return gate_err

    if not _ZOTCREATE_GATE.acquire(blocking=False):
        return jsonify({"error": "Another create is running; try again."}), 429

    from api.sse import run_sse_worker

    def _worker(put, cancel):
        _run_batch(put, cancel, rows, inv, settings, cfg, dry_run, guard_rules)

    return run_sse_worker(_worker, consumer_timeout_s=_BATCH_STALL_S,
                          preflight_msgs=soft_problems or None,
                          release=_ZOTCREATE_GATE.release)


def _run_batch(put, cancel, rows, inv, settings, cfg, dry_run, guard_rules):
    from collections import Counter

    from accession import reconciliation, review, web_snapshot
    from api.routes.zotlink import _rule_matches
    from audit.core.bib import normalise_doi
    from audit.engine import bridge
    from tagging import journal, zotero_write
    from tagging.writers import zotero_item_create_prepare
    from zotcreate import attempts

    counts = Counter()

    def emit(row, status, **extra):
        counts[status] += 1
        put({"row": {"id": row["id"], "pdf": row["pdf"], "status": status, **extra}})

    first = zotero_item_create_prepare(cfg, item_type="document", fields={}, creators=[])
    if "early" in first:
        put({"error": first["early"].get("message") or "Zotero cannot be written to."})
        return
    c = first["commit"]
    base, lib, api_key = c["transport"].base_url, c["lib_path"], c["api_key"]
    put({"info": "Observing the Web library completely (items and trash; about "
                 "6 minutes). Keep Zotero Desktop closed until the batch ends."})
    try:
        snapshot = web_snapshot.observe(base, lib, api_key=api_key)
    except (zotero_write.ZoteroError, zotero_write.ZoteroUnavailable, ValueError) as exc:
        put({"error": "The Web library could not be observed completely: "
                      + sanitise_error_msg(exc)})
        return
    target = dict(base=snapshot["base"], library=lib)
    proof = web_snapshot.BatchProof(
        snapshot, lambda since: web_snapshot.delta(base, lib, since, api_key=api_key))
    observed_version = snapshot["version"]
    put({"info": f"Observed {len(snapshot['items'])} items at library version "
                 f"{observed_version}."})
    batch_id = None if dry_run else journal.new_op_id({})
    batch_dois, batch_paths = {}, {}
    stopped = None

    for row in rows:
        if cancel.is_set():
            stopped = "Cancelled: the client went away. Every earlier item is complete."
            break
        if row["error"]:
            emit(row, "refused", reason=row["error"])
            continue
        hit = _resolve(inv, settings, row["pdf"])
        if hit is None:
            emit(row, "refused", reason="Not an orphan or pending PDF of the cached scan.")
            continue
        path, rel = Path(hit[0]), hit[1]
        if guard_rules is not None and not _rule_matches(guard_rules, path.stem):
            emit(row, "refused", reason="Attanger's skip rule does not cover this filename.")
            continue
        try:
            physical = review.identity(path)
        except (review.ReviewConflict, OSError, ValueError) as exc:
            emit(row, "refused", reason=sanitise_error_msg(exc))
            continue
        sha = physical["sha256"]
        _identity, stage, retained = attempts.lookup(settings.vault_root, sha)
        if stage == attempts.STAGE_COMPLETE:
            emit(row, "already_created", item_key=(retained or {}).get("parent_key"),
                 attachment_key=(retained or {}).get("attachment_key"))
            continue
        if stage == attempts.STAGE_PARENT_UNKNOWN:
            emit(row, "refused", reason=attempts.block_message(stage, retained))
            continue
        fields = row["fields"]
        doi = normalise_doi(fields.get("DOI") or "")
        where = str(path.resolve())
        if doi and doi in batch_dois:
            emit(row, "refused", reason=f"Same DOI as {batch_dois[doi]} earlier in this batch.")
            continue
        if where in batch_paths:
            emit(row, "refused", reason=f"Same file as {batch_paths[where]} earlier in this batch.")
            continue
        _, token = review.effective_resolution(settings.vault_root, rel, physical, None)

        if dry_run:
            try:
                review.validate_creation(settings.vault_root, rel, physical, token, fields)
                state = reconciliation.mapping_state(
                    bridge._load_mapping_all(settings.mapping_file), rel)
                if state["keys"] or state["no_match"]:
                    raise review.ReviewConflict(
                        "Existing association or conflicting decision in mapping.json.")
                if time.time() - proof.snapshot["checked_at"] > 600:
                    proof.refresh()  # admission refuses a snapshot past 900 s
                verdict = web_snapshot.admission(proof.snapshot, path, fields,
                                                 expected_target=target)
            except web_snapshot.ForeignChange as exc:
                stopped = str(exc)
                break
            except (review.ReviewConflict, ValueError) as exc:
                emit(row, "refused", reason=sanitise_error_msg(exc))
                continue
            except (zotero_write.ZoteroError, zotero_write.ZoteroUnavailable) as exc:
                stopped = "Zotero could not be read: " + sanitise_error_msg(exc)
                break
            if not verdict["eligible"]:
                emit(row, "refused", reason="; ".join(
                    f"{x['key']}: {x['reason']}" for x in verdict["conflicts"][:4]))
                continue
            if doi:
                batch_dois[doi] = row["id"]
            batch_paths[where] = row["id"]
            emit(row, "admitted", resume=stage == attempts.STAGE_PARENT_DONE,
                 would=f"create {row['item_type']} {fields['title'][:80]!r} and link {path.name}")
            continue

        def _reserved():
            return attempts.lookup(settings.vault_root, sha)[1] != stage

        try:
            payload, status = _create_one(
                settings, cfg, path=path, rel=rel, pdf_sha256=sha,
                item_type=row["item_type"], fields=fields, creators=row["creators"],
                expected_physical=physical, expected_review=token, expected_target=target,
                web_observe=proof.refresh, batch_id=batch_id,
                with_reconciliation=False, decision_ref=row["decision_ref"])
        except web_snapshot.ForeignChange as exc:
            stopped = str(exc)
            break
        except review.ReviewConflict as exc:
            if not _reserved():
                emit(row, "refused", reason=sanitise_error_msg(exc))
                continue
            emit(row, "failed", reason=sanitise_error_msg(exc))
            stopped = f"{row['id']} failed after its reservation; stopped."
            break
        except Exception as exc:  # noqa: BLE001 — stop, never guess past it
            logger.exception("zotcreate batch row failed")
            emit(row, "failed", reason=sanitise_error_msg(exc), reserved=_reserved())
            stopped = f"{row['id']} failed: {sanitise_error_msg(exc)}"
            break
        keys = [payload.get("item_key"), payload.get("attachment_key")]
        proof.claim(*keys)
        if status == 200 and payload.get("ok") is True:
            if doi:
                batch_dois[doi] = row["id"]
            batch_paths[where] = row["id"]
            emit(row, "created", item_key=payload.get("item_key"),
                 attachment_key=payload.get("attachment_key"),
                 op_ids=payload.get("op_ids"), batch_id=payload.get("batch_id"),
                 journal_warning=payload.get("journal_warning"))
            if payload.get("journal_warning"):
                stopped = f"{row['id']} was created but not journalled; stopped."
                break
            continue
        reason = payload.get("message") or payload.get("error") or payload.get("status")
        if status == 409 and not _reserved():
            emit(row, "refused", reason=str(reason))
            continue
        emit(row, "failed", reason=str(reason), stage=payload.get("stage"),
             item_key=payload.get("item_key"), reserved=_reserved())
        stopped = f"{row['id']} failed ({payload.get('stage') or status}); stopped."
        break

    reached = sum(counts.values())
    put({"zotcreate_batch": {
        "ok": stopped is None, "dry_run": dry_run, "batch_id": batch_id,
        "counts": dict(counts), "rows_total": len(rows), "reached": reached,
        "not_reached": len(rows) - reached, "stopped": stopped,
        "observed_version": observed_version,
        "final_version": proof.snapshot["version"],
        "undo": (f"papermind tagging restore-batch {batch_id} --confirm"
                 if batch_id and counts.get("created") else None),
    }})
