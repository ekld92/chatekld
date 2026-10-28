"""The "update my library" run: one orchestrator for the interface and the CLI.

Library-workflow plan, lot 5 (2026-09-21). Everything this module does is a
CALL to a route that already exists and already owns its safety: the
renamer (`/api/maintenance/rename*`, private build only), tag proposals and
writes (`/api/tagging/propose`, `/api/tagging/apply-all`), the note mirror
(`/api/notesync/mirror`). It writes nothing itself except, through the import
tracker (``accession.imports``), what each step's outcome was. So there is
exactly one implementation of every write, and this file can be read as the
maintenance run's plan.

It is pure over an injected :class:`Transport` — the route binds in-process
calls through the Flask test client (the Maintenance `batchtags` precedent:
full route validation, no loopback HTTP), and a test binds fakes.

Two phases, both user-triggered, never scheduled:

``analyze``
    Observe (the tracker's pass), then gather proposals for what is OPEN:
    names for PDFs the renamer can name, tags when a session model is set,
    and the mirror's dry run (whose ``plan_digest`` a later apply must send).
    Units whose work is already done close here: a PDF that already carries
    its derived name (``rename: skipped``) and a unit tagged outside any run
    (``tag: skipped``, read from the cached scan). Leftover imported
    attachments are listed for review, not blocked on.
    Writes app tracking state and, when requested, proposal cache and spend.
    Does not write library content.
``apply``
    Execute exactly the approved rows, in dependency order (follow-ups lot 2,
    2026-09-22): the mirror with the reviewed digest FIRST (carrying, for the
    notes it creates, the tags ticked for their records — ``apply-all``'s
    note target cannot address a note the cached scan does not know), then
    the ``pdf:`` list for EXISTING notes, then tags (addressed by UNIT), then
    renames last — the rename route rewrites the app-shaped ``pdf:`` entry in
    every note on disk, freshly created ones included, inside its own
    journalled op, so a note never carries a name that no longer exists.
    Approved leftover imported attachments are trashed after everything else
    (the journalled ``/api/zotlink/trash``, reversible from Tagging →
    Restore), each re-derived from a fresh observation first.
    Refused while an audit scan runs, and stopped before any later phase a
    scan would race (audit remediation 2026-09-23, L10). A note unit closes
    only on the mirror's per-key evidence (``settled_keys``); everything it
    did not settle stays open with its reason (L1).
    Each unit's outcome is recorded per step as it lands. A failed step is
    retried only when asked; a step whose outcome is UNKNOWN (a server error
    after the request may have been acted on) is recorded ``uncertain`` and
    never retried by this module.

    Tag and ``pdf:`` writes make notes git-dirty; the next mirror refuses to
    REFRESH those notes until the notes folder is committed (its per-file git
    proof). The result says so.

Non-atomic by nature — a file rename, a Zotero PATCH and a vault note are
three systems — so the result is always per step and per unit, never one
global "ok" that could hide a half-done run.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

# The ONE failure policy for a propose loop (shared with `papermind tagging
# batch`): how many failures in a row stop it, and how long to wait between
# them. Imported, never re-spelled (audit remediation 2026-09-23, L6/P13).
from tagging.batch import MAX_CONSECUTIVE_FAILURES, backoff_s

#: Indirection so tests can observe the waits without sleeping.
_sleep = time.sleep

#: Tag targets written by default — all three, the Obsidian note included
#: (maintainer decision, follow-ups plan 2026-09-22; notes were opt-in until
#: then). The CLI's ``--no-note-tags`` and the review's target boxes opt out.
DEFAULT_TAG_TARGETS = {"finder": True, "zotero": True, "note": True}

#: The renamer's preview takes at most this many PDFs per request.
RENAME_PREVIEW_CHUNK = 200

#: ``apply-pdf-links`` takes at most this many keys per request (the tagging
#: route's ``_HARMONIZE_MAX_NOTE_OPS``, half the journal's retention window).
PDF_LINKS_CHUNK = 100

#: ``pdf-links-status`` states a write can act on; ``dirty``/``unreadable``
#: are listed with their reason and never selectable.
PDF_LINK_SELECTABLE = ("absent", "stale")


@dataclass
class Transport:
    """What the orchestrator can ask. Each returns ``(status, body)``."""
    observe: Callable[[str], tuple]
    #: ``record_step(session, unit, step, status, detail, note_sha256=None)``.
    #: ``note_sha256`` is passed only for a ``mirror`` step: the digest of the
    #: Zotero note the mirror actually used (its ``settled_keys``), so the
    #: tracker can reopen a note edited in Zotero while the sweep ran
    #: (audit remediation 2026-09-23, L8).
    record_step: Callable[..., tuple]
    note_rename: Callable[[str, str, str], tuple]
    rename_preview: Callable[[list], tuple]
    rename: Callable[[str, str], tuple]
    propose: Callable[[str, str, Optional[str]], tuple]
    apply_tags: Callable[[str, list, str, dict], tuple]
    #: ``mirror(plan_digest, note_tags)``: ``None`` digest = dry run.
    mirror: Callable[..., tuple]
    scan_state: Callable[[], dict]
    biblio_prefix: str
    #: The tracked unit (server-owned), or None. Every approved row is checked
    #: against it: the client names a unit, never trusts its own copy of the
    #: path or the citation key (review 2026-09-21).
    unit: Callable[[str], Optional[dict]] = lambda _uid: None
    #: ``GET /api/tagging/pdf-links-status`` and ``POST /api/tagging/apply-pdf-links``
    #: (follow-ups lot 2). Defaults answer "not available" so a transport
    #: built before them still analyses and applies the rest.
    pdf_links_status: Callable[[], tuple] = lambda: (404, {"error": "pdf-links-status unavailable"})
    apply_pdf_links: Callable[[list], tuple] = lambda _keys: (404, {"error": "apply-pdf-links unavailable"})
    #: ``rename_op(op_id)`` → ``(200, {state, old_rel, new_rel, old_exists,
    #: new_exists})`` from the refactor journal (audit remediation
    #: 2026-09-23, L2): how an uncertain rename is settled from what the
    #: renamer itself recorded. The default answers "unknown", which settles
    #: nothing.
    rename_op: Callable[[str], tuple] = lambda _op: (404, {"error": "rename journal unavailable"})
    #: ``tag_state(citation_key, unit)`` → ``(200, {targets: {finder|zotero|note:
    #: {present, tags}}})`` with ``tags`` already narrowed to the vocabulary
    #: (CLI proof run 2026-09-23, finding 2). The default answers
    #: "unavailable", and then nothing is closed.
    tag_state: Callable[[str, str], tuple] = lambda _k, _u: (404, {"error": "tag state unavailable"})
    #: ``leftovers()`` → ``(200, {rows: [...]})`` from a FRESH observation that
    #: records nothing, and ``trash_attachments(item_keys)`` → the
    #: ``/api/zotlink/trash`` terminal frame (CLI proof run 2026-09-23,
    #: finding 4). Defaults answer "unavailable": no leftover is trashed.
    leftovers: Callable[[], tuple] = lambda: (404, {"error": "leftover check unavailable"})
    trash_attachments: Callable[[list], tuple] = lambda _k: (404, {"error": "trash unavailable"})


def _outcome(status: int, body: Optional[dict] = None, *, refused=(400, 404, 409, 422, 429)) -> str:
    """How a write's HTTP status maps onto a step status.

    2xx → done. A status in *refused* → failed: that route refuses it BEFORE
    acting. Everything else — 5xx, no answer, and any 4xx the route may send
    AFTER it acted — → uncertain: inspect, never retry blindly (plan §9).
    A 503 that says the vault or the store is BUSY was refused before any
    write and is failed, not uncertain.

    The renamer is called with a narrower *refused* (no 409): the same status
    covers both its pre-write refusals and its post-write "Rename failed
    safely"/"Rename undone" answers. Since the 2026-09-23 audit remediation
    (L2) it marks the former ``"wrote": false`` — honoured by
    :func:`_apply_rename`, never here — and the latter with ``op_id`` +
    ``op_state``, which :func:`settle_uncertain_rename` reads back from the
    journal. An unmarked 409 stays uncertain.
    """
    if 200 <= status < 300:
        return "done"
    if status in refused:
        return "failed"
    err = str((body or {}).get("error") or "").lower()
    if status == 503 and any(w in err for w in ("busy", "indexing", "in progress")):
        return "failed"
    return "uncertain"


def settle_uncertain_rename(t: "Transport", session: str, unit: str,
                            tracked: dict) -> Optional[str]:
    """Settle an ``uncertain`` rename step from the refactor journal.

    Audit remediation 2026-09-23 (L2, maintainer decision: automatic AND
    manual). An uncertain rename blocked its unit forever: retries stop
    before calling the renamer, and nothing cleared the state. The renamer
    records every attempt as a journal op, and the step keeps its ``op_id``;
    reading it back decides without guessing:

    * ``applied`` → ``done``, and the tracker learns the new path (the same
      ``note_rename`` a successful rename performs);
    * ``failed``/``reverted`` with the file back at its ORIGINAL path and
      nothing at the new one → ``failed`` (retryable);
    * anything else — a pending op, a missing op, a file in neither or both
      places — stays ``uncertain`` for the manual "mark resolved" verb.

    Returns the status recorded, or None when nothing was settled.
    """
    step = (tracked.get("steps") or {}).get("rename") or {}
    detail = step.get("detail") or {}
    op_id = detail.get("op_id")
    if step.get("status") != "uncertain" or not op_id:
        return None
    try:
        code, op = t.rename_op(str(op_id))
    except Exception:  # noqa: BLE001 - an unreadable journal settles nothing
        return None
    if code != 200 or not isinstance(op, dict):
        return None
    state = op.get("state")
    if state == "applied" and op.get("new_exists") and not op.get("old_exists"):
        new_rel = str(op.get("new_rel") or "")
        try:
            s2, _b2 = t.note_rename(session, unit, new_rel)
        except Exception:  # noqa: BLE001 - identity in doubt: stay uncertain
            return None
        if s2 != 200:
            return None
        outcome = "done"
    elif state in ("failed", "reverted") and op.get("old_exists") and not op.get("new_exists"):
        outcome = "failed"
    else:
        return None
    settled = _settle(t, session, unit, "rename", outcome,
                      {**detail, "settled_from_journal": state}, {"status": outcome})
    return outcome if settled.get("status") == outcome else None


def analyze(t: Transport, session: str, *, propose_tags: bool = False,
            echo: Callable[[str], None] = lambda _m: None,
            should_cancel: Callable[[], bool] = lambda: False) -> dict:
    """Observe, then propose. Returns the review document (see module doc)."""
    out: dict[str, Any] = {"session": session, "blocked": [], "renames": [], "tags": [],
                           "mirror": None, "cancelled": False}
    scan = t.scan_state()
    out["scan"] = scan
    if scan.get("state") in ("scanning", "running"):
        out["blocked"].append("A scan is running; wait for it, then analyse again.")
        return out
    if not scan.get("finished_at"):
        out["blocked"].append("No completed scan. Run an audit scan, then analyse.")
        return out

    echo("Observing Zotero and the biblio folder…")
    status, obs = t.observe(session)
    if status != 200:
        out["blocked"].append(f"Observation failed: {obs.get('error', status)}")
        return out
    out["observation"] = obs
    if obs.get("initialized"):
        # First activation: the whole library is the baseline and nothing
        # is proposed — the historical backlog is never sent to a model.
        echo("First run: the current library was recorded as the starting point.")
    if obs.get("settling"):
        out["blocked"].append(
            f"{len(obs['settling'])} imported file(s) are still being moved by Attanger; "
            "their PDFs are left for the next run.")
    # Leftover imported copies (finding 4): a review group, not a blocker.
    out["leftovers"] = [dict(r, **_leftover_selectable(r)) for r in obs.get("leftovers") or []]
    open_rows = obs.get("open") or []

    # Settle uncertain renames from the journal before proposing anything
    # (L2): a settled `done` leaves the rename proposals, a settled `failed`
    # is proposed again. Only units owing `rename` can hold one.
    settled_renames = []
    for r in open_rows:
        if "rename" not in (r.get("steps") or []):
            continue
        tracked = t.unit(r["id"]) or {}
        outcome = settle_uncertain_rename(t, session, r["id"], tracked)
        if outcome:
            settled_renames.append({"unit": r["id"], "status": outcome})
            if outcome == "done":
                r["steps"] = [x for x in r["steps"] if x != "rename"]
    if settled_renames:
        out["settled_renames"] = settled_renames

    # Renames: open attachments that owe `rename` and whose file is in the
    # biblio folder. The preview is the renamer's own derivation, so a row it
    # marks `can_apply` is executable while its inputs and earlier selected
    # renames remain unchanged. Each writer still revalidates live state.
    rename_rows = [r for r in open_rows if "rename" in r["steps"] and r.get("rel")
                   and r["rel"].startswith(t.biblio_prefix)]
    for i in range(0, len(rename_rows), RENAME_PREVIEW_CHUNK):
        if should_cancel():
            out["cancelled"] = True
            return out
        chunk = rename_rows[i:i + RENAME_PREVIEW_CHUNK]
        rels = [r["rel"][len(t.biblio_prefix):] for r in chunk]
        status, body = t.rename_preview(rels)
        if status == 404 and not body.get("rows"):
            out["renames_unavailable"] = body.get("error") or "The renamer is not in this build."
            break
        if status != 200:
            out["blocked"].append(f"Rename preview failed: {body.get('error', status)}")
            break
        for unit, row in zip(chunk, body.get("rows") or []):
            if row.get("code") == "already_named":
                # The file already carries the name the renamer derives
                # (CLI proof run 2026-09-23, finding 1): nothing will ever be
                # renamed, so close the step instead of listing it forever.
                # Keyed on the renamer's machine `code`, never its prose.
                receipt = _settle(t, session, unit["id"], "rename", "skipped",
                                  {"reason": "already_named", "name": row.get("name")},
                                  {"status": "skipped"})
                if receipt["status"] == "skipped":
                    out.setdefault("closed_renames", []).append(unit["id"])
                    continue
            same = row.get("code") == "already_named" or (
                row.get("proposal") and row.get("proposal") == row.get("name"))
            out["renames"].append({
                "unit": unit["id"], "rel": row.get("rel"), "name": row.get("name"),
                "proposal": row.get("proposal"), "can_apply": bool(row.get("can_apply")) and not same,
                "already_named": bool(same), "citation_key": row.get("citation_key"),
                "reason": row.get("reason"), "error": row.get("error"),
                "linked_attachments": row.get("linked_attachments")})

    # Units tagged OUTSIDE any run (by hand, in Zotero, by another tool)
    # close before anything is proposed (CLI proof run 2026-09-23, finding 2,
    # maintainer decision D-b), so the model is never asked again for them.
    for r in open_rows:
        if "tag" not in (r.get("steps") or []) or not r.get("citation_key"):
            continue
        tags = _tagged_outside_run(t, r)
        if tags is None:
            continue
        receipt = _settle(t, session, r["id"], "tag", "skipped",
                          {"reason": "tagged_outside_run", "tags": tags}, {"status": "skipped"})
        if receipt["status"] == "skipped":
            r["steps"] = [x for x in r["steps"] if x != "tag"]
            out.setdefault("closed_tags", []).append(
                {"unit": r["id"], "citation_key": r["citation_key"], "tags": tags})

    # Tags: one proposal per open unit owing `tag`, through the session's model.
    #
    # A failing provider (429 busy gate, 5xx, a timeout, no answer) used to be
    # asked again at once for every remaining paper — the sweep `tagging
    # batch` learnt to stop in 2026-08 (audit remediation 2026-09-23, L6/P13).
    # Same policy here: back off between failures (the cancel is polled during
    # the wait), stop after MAX_CONSECUTIVE_FAILURES in a row, list the rest as
    # not proposed (nothing sent, nothing charged). A success resets it.
    if propose_tags:
        wanted = [r for r in open_rows if "tag" in r["steps"]]
        consecutive = 0
        for i, r in enumerate(wanted, 1):
            if should_cancel():
                out["cancelled"] = True
                return out
            key = r.get("citation_key")
            row = {"unit": r["id"], "citation_key": key}
            if not key:
                row["error"] = "No citation key yet: let Better BibTeX export it, then rescan."
                out["tags"].append(row)
                continue
            echo(f"Proposing tags for {key}… ({i}/{len(wanted)})")
            tracked = t.unit(r["id"]) or {}
            pdf_unit = r["id"] if tracked.get("kind") == "attachment" and tracked.get("in_biblio") is not False else None
            try:
                status, body = t.propose(key, session, pdf_unit)
            except Exception as exc:  # noqa: BLE001 - no answer counts as a failure
                status, body = 0, {"error": str(exc)}
            row.update(status_code=status, **{k: body.get(k) for k in (
                "proposed", "status", "source", "sent", "from_cache", "spend", "error", "reason",
                "spend_error", "spend_unrecorded")})
            if status == 404:
                row["needs_scan"] = True
            out["tags"].append(row)
            if status == 409 and body.get("reason") == "budget":
                out["blocked"].append(body.get("error") or "Spending limit reached.")
                break
            if body.get("spend_unrecorded"):
                # The call ran and its cost could not be recorded even as
                # "unknown" (L4): proposing more would spend blind.
                out["blocked"].append("The spend of a proposal could not be recorded; "
                                      "stopping before the next one.")
                out["tags_not_proposed"] = [x["id"] for x in wanted[i:]]
                break
            if status == 0 or status == 429 or status >= 500:
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    out["blocked"].append(f"provider failing: {consecutive} consecutive errors; "
                                          "the remaining papers were not proposed.")
                    out["tags_not_proposed"] = [x["id"] for x in wanted[i:]]
                    break
                if _wait(backoff_s(consecutive), should_cancel):
                    out["cancelled"] = True
                    return out
            else:
                consecutive = 0

    if should_cancel():
        out["cancelled"] = True
        return out
    echo("Planning the note mirror…")
    status, body = t.mirror(None)
    out["mirror"] = body if status == 200 else {"error": body.get("error", status),
                                                  "reason": body.get("reason")}
    # The note units a confirmed mirror would settle; apply records their
    # `mirror` step from the mirror's own per-key outcome.
    out["mirror_units"] = [r["id"] for r in open_rows if "mirror" in r["steps"]]

    # `pdf:` lists of EXISTING notes (follow-ups lot 2): absent/stale rows
    # are selectable; dirty (hand-written, the writer refuses) and unreadable
    # ones are listed with the reason. Not a tracked unit step — the next
    # analysis recomputes it from the notes.
    status, body = t.pdf_links_status()
    if status == 200:
        rows = []
        for r in body.get("rows") or []:
            state = str(r.get("state") or "")
            if state not in ("absent", "stale", "dirty", "unreadable"):
                continue
            rows.append({"citation_key": r.get("citation_key"), "state": state,
                         "expected": list(r.get("expected") or []),
                         "listed": list(r.get("listed") or []), "note": r.get("note"),
                         "selectable": state in PDF_LINK_SELECTABLE,
                         "reason": {"dirty": "a pdf: list this app did not write; edit it by hand",
                                    "unreadable": "the note cannot be read as UTF-8"}.get(state)})
        out["pdf_links"] = {"rows": rows, "counts": body.get("counts") or {}}
    else:
        out["pdf_links"] = {"rows": [], "error": body.get("error", status)}
    return out


def _leftover_selectable(row: dict) -> dict:
    """A leftover carrying Zotero annotations — or whose count is unknown — is
    listed but never selectable: trashing the attachment trashes its
    highlights, and this run never sends ``accept_annotation_loss``."""
    n = row.get("annotations")
    if n == 0:
        return {"selectable": True, "reason": None}
    return {"selectable": False,
            "reason": ("its annotation count is unknown" if n is None else
                       f"it carries {n} Zotero annotation(s); trashing it would trash "
                       "them too — decide in Zotero")}


def _apply_leftovers(t: "Transport", ids: list, echo) -> tuple[list, Optional[str]]:
    """Trash the approved leftover attachments through ``/api/zotlink/trash``.

    Every approved row is re-derived from a FRESH observation first: a row
    that no longer qualifies (the linked copy went, Zotero synced the file
    back, annotations appeared) is refused, never trashed. Returns the
    per-row results and the batch id (the one-click undo).
    """
    try:
        code, body = t.leftovers()
    except Exception as exc:  # noqa: BLE001 - nothing was written
        code, body = 0, {"error": str(exc)}
    if code != 200:
        why = f"the leftover check failed: {(body or {}).get('error', code)}; nothing was trashed"
        return [{"unit": u, "status": "failed", "reason": why} for u in ids], None
    fresh = {r.get("unit"): r for r in (body.get("rows") or []) if isinstance(r, dict)}
    rows: list = []
    send: list = []
    for uid in ids:
        r = fresh.get(uid)
        if r is None:
            rows.append({"unit": uid, "status": "failed",
                         "reason": "no longer a leftover (analyse again); nothing was trashed"})
            continue
        sel = _leftover_selectable(r)
        if not sel["selectable"]:
            rows.append({"unit": uid, "item_key": r.get("item_key"), "status": "failed",
                         "reason": f"not trashed: {sel['reason']}"})
            continue
        send.append((uid, str(r.get("item_key") or "")))
    batch_id = None
    if send:
        echo(f"Trashing {len(send)} leftover Zotero attachment(s)…")
        try:
            code, body = t.trash_attachments([k for _u, k in send])
        except Exception as exc:  # noqa: BLE001 - the outcome is unknown
            code, body = 0, {"error": str(exc)}
        body = body if isinstance(body, dict) else {}
        results = body.get("results") or {}
        batch_id = body.get("batch_id") or None
        for uid, key in send:
            row: dict = {"unit": uid, "item_key": key}
            r = results.get(key) if code == 200 else None
            if code != 200:
                row.update(status=_outcome(code, body), reason=body.get("error"))
            elif not r:
                row.update(status="uncertain", reason="the trash returned no result for this key")
            elif r.get("ok"):
                row.update(status="done", batch_id=batch_id)
            else:
                row.update(status="failed", reason=r.get("error") or r.get("status"))
            rows.append(row)
    order = {u: i for i, u in enumerate(ids)}
    rows.sort(key=lambda x: order.get(x["unit"], 0))
    return rows, batch_id


def owed_tag_targets(tracked: dict) -> tuple[str, ...]:
    """The tag targets a unit owes — ``apply-all``'s own skip rule.

    An entry (no PDF of its own) owes Zotero and its reference note; a PDF in
    the biblio folder owes its Finder tags as well; a PDF elsewhere owes only
    Zotero. A target the record does not HAVE (no note, no Zotero item) is
    dropped by the caller, exactly as ``apply-all`` skips it.
    """
    if tracked.get("kind") == "attachment":
        return ("zotero",) if tracked.get("in_biblio") is False else ("finder", "zotero", "note")
    return ("zotero", "note")


def _tagged_outside_run(t: "Transport", row: dict) -> Optional[dict]:
    """``{target: [vocabulary tags]}`` when every owed target the unit has
    already carries a vocabulary tag, else None (it still owes tags).

    Reads the CACHED scan through ``t.tag_state``: tags written after the last
    scan are invisible, so such a unit still owes and is proposed — the
    conservative direction. Any failure closes nothing.
    """
    try:
        code, body = t.tag_state(row["citation_key"], row["id"])
    except Exception:  # noqa: BLE001 - an unknown state closes nothing
        return None
    if code != 200 or not isinstance(body, dict):
        return None
    targets = body.get("targets") or {}
    owed = [k for k in owed_tag_targets(t.unit(row["id"]) or {})
            if (targets.get(k) or {}).get("present")]
    if not owed or not all((targets.get(k) or {}).get("tags") for k in owed):
        return None
    return {k: list(targets[k]["tags"]) for k in owed}


def _wait(seconds: float, should_cancel: Callable[[], bool]) -> bool:
    """Sleep *seconds* in 0.25 s steps, polling the cancel; True when cancelled.

    Counts down what it slept rather than reading a clock, so a test that
    replaces ``_sleep`` observes the exact schedule without waiting."""
    remaining = float(seconds)
    while remaining > 0:
        if should_cancel():
            return True
        step = min(0.25, remaining)
        _sleep(step)
        remaining -= step
    return should_cancel()


def apply(t: Transport, session: str, approvals: dict, *,
          echo: Callable[[str], None] = lambda _m: None,
          should_cancel: Callable[[], bool] = lambda: False) -> dict:
    """Execute the approved rows; record every unit's outcome as it lands.

    ``approvals``: ``{mirror: {plan_digest, units?, create_keys?} | None,
    pdf_links: [citation_key], tags: [{unit, citation_key, tags, targets?}],
    renames: [{unit, rel, new_name}], leftovers: [unit]}``. ``create_keys`` (the reviewed plan's)
    narrows the note tags the mirror receives. Executed in that order — mirror → ``pdf:`` → tags →
    renames → leftover trash (see the module doc for why). Cancellation is checked before
    every write; what was not reached is listed, never silently dropped.
    """
    result: dict[str, Any] = {"session": session, "mirror": None, "pdf_links": [], "tags": [],
                              "renames": [], "leftovers": [], "not_reached": [], "cancelled": False,
                              "ignored": [], "notes_written": 0, "stopped": None}
    # One row per (kind, unit): a duplicate would re-run a write whose first
    # outcome it then overwrote — a finished rename recorded `failed` by its
    # own repeat (review 2026-09-21). Later duplicates are reported, not run.
    seen: set = set()
    tag_rows: list = []
    rename_rows: list = []
    for kind, rows, dest in (("tag", approvals.get("tags") or [], tag_rows),
                             ("rename", approvals.get("renames") or [], rename_rows)):
        for r in rows:
            k = (kind, r.get("unit") if isinstance(r, dict) else None)
            if not isinstance(r, dict) or k in seen:
                result["ignored"].append({"kind": kind, "unit": k[1], "reason": "duplicate or malformed"})
                continue
            seen.add(k)
            dest.append(r)
    leftover_ids: list = []
    for u in approvals.get("leftovers") or []:
        if not isinstance(u, str) or not u or u in leftover_ids:
            result["ignored"].append({"kind": "leftovers", "unit": u if isinstance(u, str) else None,
                                      "reason": "duplicate or malformed"})
            continue
        leftover_ids.append(u)
    pdf_keys: list = []
    for k in approvals.get("pdf_links") or []:
        if not isinstance(k, str) or not k or k in pdf_keys:
            result["ignored"].append({"kind": "pdf_links", "unit": k if isinstance(k, str) else None,
                                      "reason": "duplicate or malformed"})
            continue
        pdf_keys.append(k)

    def _stop(rest: list) -> dict:
        result["cancelled"] = True
        result["not_reached"] = rest
        return result

    pending = (["mirror"] if (approvals.get("mirror") or {}).get("plan_digest") else []) \
        + [f"pdf_links:{k}" for k in pdf_keys] \
        + [f"tag:{r.get('unit')}" for r in tag_rows] \
        + [f"rename:{r.get('unit')}" for r in rename_rows] \
        + [f"leftover:{u}" for u in leftover_ids]

    def _scan_running() -> bool:
        try:
            return (t.scan_state() or {}).get("state") in ("scanning", "running")
        except Exception:  # noqa: BLE001 - an unreadable state is not "idle"
            return True

    def _halt(rest: list) -> dict:
        """A scan started: stop BEFORE the next phase (audit remediation
        2026-09-23, L10). A scan replaces the cached inventory every write
        route resolves against mid-run, so a phase started now could act on a
        half-built join. What was not reached is listed, like a cancel, but
        the terminal says why."""
        result["stopped"] = "scan_running"
        result["blocked"] = "An audit scan is running; nothing more was applied. " \
                            "Wait for it, then analyse again."
        result["not_reached"] = rest
        return result

    # Refused outright while a scan runs: analyse refuses the same state, and
    # an apply racing a scan would write against a join being rebuilt.
    if _scan_running():
        return _halt(pending)

    # 1. The mirror, with the note tags ticked for the records whose notes
    #    it creates (apply-all cannot address a note the scan does not know).
    mirror = approvals.get("mirror")
    if mirror and mirror.get("plan_digest"):
        if should_cancel():
            return _stop(pending)
        if _scan_running():
            return _halt(pending)
        pending.pop(0)
        note_tags = _mirror_note_tags(t, tag_rows, mirror.get("create_keys"))
        echo("Mirroring the Zotero notes…")
        result["mirror"] = _apply_mirror(t, session, mirror, echo, note_tags=note_tags)
        result["notes_written"] += int((result["mirror"] or {}).get("created") or 0) \
            + int((result["mirror"] or {}).get("refreshed") or 0)
        # Which ticked note tags did NOT land in a note the mirror created
        # (audit remediation 2026-09-23, L5). The tag step reads the note
        # target of those keys as FAILED with this reason: apply-all cannot
        # address a note the cached scan does not know, so it answers
        # `skipped: no_note`, which used to close the step `done` although
        # the user's tags were written nowhere. A terminal without the field
        # (a mirror that did not finish) leaves every sent key unaccounted.
        reported = (result["mirror"] or {}).get("note_tags_unwritten")
        if isinstance(reported, dict):
            unwritten = {str(k): str(v) for k, v in reported.items()}
        else:
            unwritten = {k: "the mirror did not report whether these tags were written"
                         for k in note_tags}
        result["note_tags_unwritten"] = unwritten
        result["pdf_links_unwritten"] = dict((result["mirror"] or {}).get("pdf_links_unwritten") or {})
    else:
        unwritten = {}

    # 2. `pdf:` lists of existing notes, in the route's own chunks.
    if pdf_keys and _scan_running():
        return _halt(pending)
    for i in range(0, len(pdf_keys), PDF_LINKS_CHUNK):
        if should_cancel():
            return _stop(pending)
        chunk = pdf_keys[i:i + PDF_LINKS_CHUNK]
        del pending[:len(chunk)]
        echo(f"Writing pdf: links into {len(chunk)} note(s)…")
        try:
            status, body = t.apply_pdf_links(chunk)
        except Exception as exc:  # noqa: BLE001
            status, body = 0, {"error": str(exc)}
        results = (body.get("results") or {}) if isinstance(body, dict) else {}
        # The route stops at a refused heartbeat and returns 200 with
        # `aborted`; the keys it never reached have NO results entry. Those
        # were not written — never read "200, no row" as done (review
        # 2026-09-22).
        aborted = status == 200 and isinstance(body, dict) and bool(body.get("aborted"))
        lost = "the lock was lost before this note; nothing was written"
        for key in chunk:
            r = results.get(key) or {}
            if status == 200 and r:
                ok = bool(r.get("ok"))
                row = {"citation_key": key, "status": "done" if ok else "failed",
                       "changed": bool(r.get("changed")),
                       "reason": r.get("reason") or r.get("error")}
                if ok and r.get("changed"):
                    result["notes_written"] += 1
            elif aborted:
                row = {"citation_key": key, "status": "failed", "reason": lost}
            elif status == 200:
                row = {"citation_key": key, "status": "uncertain",
                       "reason": "the route returned no result for this note"}
            else:
                row = {"citation_key": key, "status": _outcome(status, body),
                       "reason": (body or {}).get("error")}
            result["pdf_links"].append(row)
        if aborted:
            # Stop here: sending the next chunk would write notes already
            # reported as failed, and would desynchronise `pending` from what
            # really ran (a later cancel then listed nothing as not reached).
            rest = pdf_keys[i + PDF_LINKS_CHUNK:]
            for key in rest:
                result["pdf_links"].append({"citation_key": key, "status": "failed", "reason": lost})
            del pending[:len(rest)]
            break

    # 3. Tags, by unit.
    if tag_rows and _scan_running():
        return _halt(pending)
    # `(i/N)` rides every per-item line (audit remediation 2026-09-23, U6):
    # the interface shows a progress bar and throttles announcements on it.
    for i, r in enumerate(tag_rows, 1):
        if should_cancel():
            return _stop(pending)
        pending.pop(0)
        res = _apply_tags(t, session, r, lambda m, i=i: echo(f"{m} ({i}/{len(tag_rows)})"),
                          note_unwritten=unwritten)
        result["tags"].append(res)
        if res.get("status") == "done" and (res.get("targets") or {}).get("note") == "done":
            result["notes_written"] += 1

    # 4. Renames last: the route rewrites every app-shaped `pdf:` entry.
    if rename_rows and _scan_running():
        return _halt(pending)
    for i, r in enumerate(rename_rows, 1):
        if should_cancel():
            return _stop(pending)
        pending.pop(0)
        result["renames"].append(_apply_rename(
            t, session, r, lambda m, i=i: echo(f"{m} ({i}/{len(rename_rows)})")))

    # 5. Leftover imported attachments (finding 4), trashed last through the
    #    journalled zotlink route. No tracker step: the unit goes `gone` at
    #    the next observation.
    if leftover_ids:
        if _scan_running():
            return _halt(pending)
        if should_cancel():
            return _stop(pending)
        del pending[:len(leftover_ids)]
        result["leftovers"], batch_id = _apply_leftovers(t, leftover_ids, echo)
        if batch_id and any(r.get("status") == "done" for r in result["leftovers"]):
            result["leftovers_undo"] = f"papermind tagging restore-batch {batch_id} --confirm"
    return result


def _settle(t, session, unit, step, status, detail, result, *, note_sha256=None):
    """A successful effect is not a durable outcome until its receipt saves.

    Keep the effect's details when the tracker refuses or fails; returning
    uncertain prevents a caller from treating this as a safe replay.
    *note_sha256* is forwarded only when given, so a transport that predates
    it still settles every other step.
    """
    try:
        if note_sha256 is not None:
            code, body = t.record_step(session, unit, step, status, detail,
                                       note_sha256=note_sha256)
        else:
            code, body = t.record_step(session, unit, step, status, detail)
        error = body.get('error') or 'Outcome could not be recorded.'
    except Exception as exc:  # noqa: BLE001 - retain the already-landed effect
        code, error = 0, str(exc)
    if code != 200:
        result.update(status='uncertain', tracker=error)
    return result


def _uncertain_step(tracked, step):
    return ((tracked.get('steps') or {}).get(step) or {}).get('status') == 'uncertain'


def _apply_rename(t, session, row, echo) -> dict:
    unit, rel, new_name = row.get("unit"), row.get("rel"), row.get("new_name")
    tracked = t.unit(unit) or {}
    if _uncertain_step(tracked, 'rename'):
        # Settle from the journal first (L2): a rename the journal shows as
        # landed is done (never re-run), one it shows as undone is failed and
        # may be retried now.
        settled = settle_uncertain_rename(t, session, unit, tracked)
        if settled == "done":
            return {'unit': unit, 'status': 'done', 'settled_from_journal': True}
        if settled != "failed":
            return {'unit': unit, 'status': 'uncertain',
                    'error': 'Inspect the previous rename before retrying.'}
        tracked = t.unit(unit) or {}
    current = str(tracked.get("rel") or "")
    if (tracked.get("kind") != "attachment" or tracked.get("state", "present") != "present"
            or not current.startswith(t.biblio_prefix) or current[len(t.biblio_prefix):] != rel):
        # The approved row must name THIS unit's current file, or the renamer
        # would move another PDF while this unit was marked renamed.
        refusal = {"from": rel, "to": new_name,
                   "error": "The approved file is not this unit's current file; analyse again."}
        return _settle(t, session, unit, 'rename', 'failed', refusal,
                       {"unit": unit, "status": "failed", **refusal})
    echo(f"Renaming {rel} → {new_name}")
    try:
        status, body = t.rename(rel, new_name)
    except Exception as exc:  # noqa: BLE001 - no answer: the outcome is unknown
        status, body = 0, {"error": str(exc)}
    body = body if isinstance(body, dict) else {}
    step = _outcome(status, body, refused=(400, 404, 422, 429))
    if step != "done" and body.get("wrote") is False:
        # The renamer says it refused BEFORE touching the file (L2): a plain,
        # retryable failure — never the uncertain state that blocks the unit.
        step = "failed"
    detail: dict[str, Any] = {"from": rel, "to": new_name}
    if body.get("op_id"):
        # Kept so an uncertain outcome can be settled from the journal later.
        detail["op_id"] = body["op_id"]
        if body.get("op_state"):
            detail["op_state"] = body["op_state"]
    if step != "done":
        detail["error"] = body.get("error")
        return _settle(t, session, unit, 'rename', step, detail,
                       {"unit": unit, "status": step, **detail})
    # The route answers the VAULT-relative new path; the rename keeps the
    # directory, so the fallback swaps only the basename.
    new_rel = body.get("new_rel") or (t.biblio_prefix + "/".join([*rel.split("/")[:-1], new_name]))
    detail["new_rel"] = new_rel
    zot = body.get("zotero") or {}
    linked = int(zot.get("linked_attachments") or 0)
    moved = int(zot.get("repointed") or 0)
    result_status = "done"
    if zot.get("error") or moved < linked or any(not r.get("ok") for r in zot.get("results") or []):
        # The file moved, the Zotero pointer did not (fully): the rename is
        # real and journalled, so the step is done, but the RESULT says
        # "partial" and names what still points at the old path — the
        # Zotero Attachments tab's [re-point…] is the repair (phase04
        # precedent: a landed rename with an unmoved pointer is not ok).
        result_status = "partial"
        detail["zotero"] = (f"{moved} of {linked} Zotero link(s) moved"
                            + (f"; {zot['error']}" if zot.get("error") else ""))
    # The tracker learns the new path FROM THE RENAME ITSELF, verified against
    # the file's identity (a rename keeps inode and birth time); waiting for
    # Zotero's local database to record the repath would leave the unit
    # unaddressable. If the tracker cannot verify it, the file moved but the
    # unit's identity is in doubt: uncertain, never done.
    try:
        s2, b2 = t.note_rename(session, unit, new_rel)
    except Exception as exc:  # noqa: BLE001 - the file already moved
        s2, b2 = 0, {'error': str(exc)}
    if s2 != 200:
        detail["tracker"] = b2.get("error")
        return _settle(t, session, unit, 'rename', 'uncertain', detail,
                       {"unit": unit, "status": "uncertain", **detail})
    return _settle(t, session, unit, 'rename', 'done', detail,
                   {"unit": unit, "status": result_status, **detail})


def _record_key(t, tracked: dict) -> Optional[str]:
    """The citation key of the RECORD a unit belongs to (entry or attachment)."""
    if tracked.get("kind") == "entry":
        return tracked.get("citation_key")
    parent = t.unit(tracked.get("parent") or "") if tracked.get("parent") else None
    return (parent or {}).get("citation_key")


def _tag_row_refusal(t, row: dict, tracked: dict) -> Optional[dict]:
    """Why a tag row may not be applied, or None. ONE check, used by the tag
    step AND before the mirror receives the row's note tags — so a row the tag
    step refuses tags nothing anywhere (review 2026-09-22)."""
    unit, key = row.get("unit"), row.get("citation_key")
    if _uncertain_step(tracked, 'tag'):
        return {'unit': unit, 'status': 'uncertain', 'error': 'Inspect the previous tag write before retrying.'}
    if tracked.get("state", "present") != "present" or tracked.get("kind") not in ("attachment", "entry"):
        return {"unit": unit, "status": "failed", "error": "Unknown or absent unit; analyse again."}
    if not key or key != _record_key(t, tracked):
        # Ties the write to the unit's own record: otherwise a row could write
        # record B's tags and close unit A (review 2026-09-21).
        return {"unit": unit, "status": "failed",
                "error": "That citation key is not this unit's record; analyse again."}
    return None


def _mirror_note_tags(t, tag_rows: list, create_keys) -> dict:
    """The note tags the mirror receives: ``{citation_key: [tags]}``.

    Only rows that (a) target the note, (b) pass the tag step's own check and
    (c) name a record whose note THIS mirror creates, when the reviewed plan's
    ``create_keys`` are known. Sending every tagged paper made a run with more
    than 100 tag rows exceed the route's bound and lose the whole mirror
    (review 2026-09-22). Tags are de-duplicated per key, in order.
    """
    wanted = None if create_keys is None else {str(k) for k in create_keys}
    out: dict[str, list] = {}
    for r in tag_rows:
        targets = {**DEFAULT_TAG_TARGETS, **(r.get("targets") or {})}
        key = r.get("citation_key")
        if not (targets.get("note") and key and r.get("tags")):
            continue
        if wanted is not None and str(key) not in wanted:
            continue
        if _tag_row_refusal(t, r, t.unit(r.get("unit")) or {}) is not None:
            continue
        bucket = out.setdefault(str(key), [])
        for x in r["tags"]:
            if x and str(x) not in bucket:
                bucket.append(str(x))
    return {k: v for k, v in out.items() if v}


def _apply_tags(t, session, row, echo, note_unwritten: Optional[dict] = None) -> dict:
    unit, key, tags = row.get("unit"), row.get("citation_key"), row.get("tags") or []
    tracked = t.unit(unit) or {}
    refusal = _tag_row_refusal(t, row, tracked)
    if refusal is not None:
        return refusal
    targets = {**DEFAULT_TAG_TARGETS, **(row.get("targets") or {})}
    prior = (tracked.get('steps') or {}).get('tag') or {}
    prior_detail = prior.get('detail') or {}
    resuming = prior.get('status') == 'failed' and set(prior_detail.get('tags') or []) == set(tags)
    if row.get('retry_failed_only') is True and (not resuming or not prior_detail.get('targets')):
        return {'unit': unit, 'status': 'failed', 'error': 'The saved failed destinations changed; review the current result before retrying.'}
    if resuming:
        unresolved = unresolved_targets(prior)
        if unresolved:
            targets = {k: bool(v) and unresolved.get(k, False) for k, v in targets.items()}
    if row.get('retry_failed_only') is True and not any(targets.values()):
        return {'unit': unit, 'status': 'failed', 'error': 'No selected destination is still failed; refresh the saved result.'}
    send_unit: Optional[str] = unit
    if tracked.get("kind") == "entry" or tracked.get("in_biblio") is False:
        # No PDF in the biblio folder: no Finder target, and no unit to
        # address (apply-all's unit names a PDF). Zotero (and the note, if
        # asked) are written by record.
        targets["finder"] = False
        send_unit = None
    if not tags:
        return _settle(t, session, unit, 'tag', 'skipped', {'reason': 'no tags approved'},
                       {"unit": unit, "status": "skipped"})
    if not any(targets.values()):
        # Every destination off (the user unticked them all, or the only one
        # left was Finder on a row with no PDF in the biblio folder). Nothing
        # is written, so the step is `skipped` WITH its reason — never `done`:
        # `all([])` used to report done for a write that never happened
        # (audit remediation 2026-09-23, L5).
        return _settle(t, session, unit, 'tag', 'skipped',
                       {'reason': 'no target selected', 'tags': list(tags)},
                       {"unit": unit, "status": "skipped", "reason": "no target selected"})
    echo(f"Tagging {key}: {', '.join(tags)}")
    try:
        status, body = t.apply_tags(key, tags, send_unit, targets)
    except Exception as exc:  # noqa: BLE001
        status, body = 0, {"error": str(exc)}
    if status != 200:
        step = _outcome(status, body)
        failed_detail = {**(prior_detail if resuming else {}), 'error': body.get('error')}
        return _settle(t, session, unit, 'tag', step, failed_detail,
                       {"unit": unit, "status": step, "error": body.get("error")})
    results = body.get("results") or {}
    # Retain completed destinations across repeated partial retries. Without
    # this, retry 2 drops Finder's receipt and retry 3 would enable it again.
    per: dict[str, str] = {k: v for k, v in (prior_detail.get('targets') or {}).items()
                           if resuming and v in ('done', 'skipped')}
    reasons: dict[str, str] = {}
    unwritten_notes: dict = note_unwritten or {}
    for target, wanted in targets.items():
        if not wanted:
            continue
        r = results.get(target) or {}
        if target == "note" and key in unwritten_notes:
            # The mirror created this note without the ticked tags (see
            # `apply`): whatever apply-all says about a note it cannot see,
            # the tags are not in it.
            per[target] = "failed"
            reasons[target] = str(unwritten_notes[key])
        elif r.get("ok"):
            per[target] = "done"
        elif (target == "note" and r.get("skipped") == "no_note" and resuming
              and (prior_detail.get("reasons") or {}).get("note")):
            # A retry of a note the mirror created untagged: apply-all still
            # cannot see it until a rescan, and "no note" is not true of it.
            per[target] = "failed"
            reasons[target] = RESCAN_NEEDED
        elif r.get("skipped") in ("no_item", "no_note", "no_pdf", "not_macos"):
            # The target does not exist for this record: nothing to do, and
            # re-running would not change that.
            per[target] = "skipped"
        elif target == 'zotero' and not r.get('write_not_started'):
            # A lost response or exception in the commit phase can follow a
            # landed PATCH. Only a confirmed pre-write refusal is retryable.
            per[target] = 'uncertain'
        else:
            per[target] = "failed"
    step = ('uncertain' if 'uncertain' in per.values() else
            "done" if all(v in ("done", "skipped") for v in per.values()) else "failed")
    # Per-target outcomes ride the step, so a resumed run re-applies only
    # the targets still failed (tag writes are unions: an already-written
    # target would be a no-op anyway, never a double add).
    detail: dict[str, Any] = {"targets": per, "tags": list(tags)}
    res: dict[str, Any] = {"unit": unit, "citation_key": key, "tags": list(tags), "status": step, "targets": per,
                           "results": {k: results.get(k) for k in per}}
    if reasons:
        detail["reasons"] = reasons
        res["reasons"] = reasons
    return _settle(t, session, unit, 'tag', step, detail, res)


def _mirror_reasons(body: dict) -> dict:
    """``{citation_key: reason}`` for every key the mirror named but did not
    settle: its ``listed`` buckets (deleted in Zotero, unparsed, several
    reading notes, deferred past the create limit, …) and its ``skipped``
    ones (create and baseline). The FIRST reason seen wins; the order is the
    mirror's own, which is stable."""
    out: dict = {}
    for reason, keys in (body.get("listed") or {}).items():
        for k in keys or []:
            out.setdefault(str(k), str(reason))
    for phase, buckets in (body.get("skipped") or {}).items():
        for reason, keys in (buckets or {}).items():
            for k in keys or []:
                out.setdefault(str(k), f"{reason} ({phase})")
    return out


#: Why a note unit stays open when the mirror did not mention its record at
#: all: the mirror's rows come from the cached scan's join, so a Zotero record
#: created (or re-keyed) since the last scan has no row to act on.
RESCAN_NEEDED = "rescan needed"


def _apply_mirror(t, session, mirror: dict, echo, note_tags: Optional[dict] = None) -> dict:
    """Run the reviewed mirror and settle each note unit's `mirror` step.

    Status is read from the mirror's OWN terminal frame, never from the HTTP
    200 alone. A stop may follow successful creates, so its effect counts
    decide partial versus failed. A missing terminal is uncertain.

    A note unit closes only on POSITIVE evidence (audit remediation
    2026-09-23, L1): its key in the terminal's ``settled_keys`` → ``done``,
    recorded with the note digest the mirror used (so a note edited in Zotero
    meanwhile reopens at the next pass); in ``failed`` → ``failed``. Before,
    a unit closed ``done`` whenever its key was absent from ``failed`` and
    ``listed`` — which is exactly the case of a Zotero record the last scan
    does not know (the mirror has no row for it), so such a note was closed
    and never reopened. Every other unit stays OPEN, reported in ``open`` with
    the reason (``rescan needed``, ``multiple_notes``, …); the rest of the run
    proceeds. A stopped/aborted/cancelled sweep settles nothing.
    """
    try:
        status, body = t.mirror(mirror["plan_digest"], note_tags or {})
    except Exception as exc:  # noqa: BLE001
        status, body = 0, {"error": str(exc)}
    body = body if isinstance(body, dict) else {}
    if status != 200:
        state = _outcome(status, body)
    elif not isinstance(body.get('ok'), bool):
        state = 'uncertain'
    elif body.get("stopped"):
        state = 'partial' if any(body.get(k) for k in (
            'created', 'refreshed', 'bases_written', 'bootstrapped', 'stale_bases_removed')) else 'failed'
    elif body.get("ok") is False or body.get("failed"):
        state = "partial"
    else:
        state = "done"
    out = {**body, "status": state}
    opened: list = []
    complete = state in ("done", "partial") and not any(
        body.get(k) for k in ('stopped', 'aborted', 'cancelled'))
    settled_keys = body.get("settled_keys") if isinstance(body.get("settled_keys"), dict) else None
    failed = set((body.get("failed") or {}).keys())
    reasons = _mirror_reasons(body)
    settled = []
    for uid in mirror.get("units") or []:
        tracked = t.unit(uid) or {}
        if tracked.get("kind") != "note" or tracked.get("state", "present") != "present":
            continue
        key = _record_key(t, tracked)
        if not complete:
            opened.append({"unit": uid, "citation_key": key,
                           "reason": f"the mirror did not finish ({body.get('stopped') or state})"})
            continue
        if not key:
            opened.append({"unit": uid, "citation_key": None,
                           "reason": "no citation key yet: let Better BibTeX export it, then rescan"})
            continue
        if key in failed:
            step, sha = "failed", None
        elif settled_keys is not None and key in settled_keys:
            step, sha = "done", str(settled_keys[key] or "") or None
        else:
            reason = reasons.get(key)
            if reason is None:
                reason = (RESCAN_NEEDED if settled_keys is not None
                          else "the mirror did not report per-note outcomes")
            opened.append({"unit": uid, "citation_key": key, "reason": reason})
            continue
        receipt = _settle(t, session, uid, 'mirror', step, {"citation_key": key},
                          {"unit": uid, "status": step}, note_sha256=sha)
        settled.append(receipt)
        if receipt['status'] == 'uncertain':
            out['status'] = 'uncertain'
    out["settled"] = settled
    out["open"] = opened
    return out


def unresolved_targets(step: Optional[dict]) -> Optional[dict]:
    """The targets a failed tag step still owes, or None for "all defaults"."""
    targets = ((step or {}).get("detail") or {}).get("targets")
    if not targets:
        return None
    return {k: v == "failed" for k, v in targets.items()}
