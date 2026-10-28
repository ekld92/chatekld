"""Library-wide tagging batch — pure orchestration over injected callables.

Extracted from ``cli/main.py::tagging_batch_cmd`` (2026-08-21, Maintenance
Phase 2) on the deckgen ``client.py``-vs-``inprocess.py`` pattern: the loop —
selection, the already-tagged gate, target narrowing, snapshot/model
disclosure, backoff-and-abort, the summary — lives HERE once, and each surface
binds its own transports:

* the CLI binds the ``core.driver`` HTTP functions (resolved at call time in
  ``cli/main.py`` so its tests' ``cli.main.tagging_*`` patches keep working);
* the private Maintenance step (``maintenance/steps/batchtags.py``) binds
  in-process wrappers over the app's own tagging routes via the Flask test
  client — no loopback HTTP from inside the server.

Argument-SHAPE validation stays with each caller (the CLI's UsageErrors, the
route's option coercion): this module assumes a well-formed ``BatchOptions``.
Behaviour contract: ``papermind tagging batch`` must stay byte-identical —
``tests/test_cli.py::TestTaggingBatch*`` is the guard.

``time.sleep`` is looked up at CALL time (never captured in a default arg) so
the existing tests' ``mock.patch("time.sleep")`` keeps seeing every wait.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

# The ONE definition of "the same tag" for depth (see `fold_tag`). Imported
# rather than re-spelled: the dashboard histogram and the table's `#` cell
# select the same papers as `--max-tags`, or the cohort a user picks is not
# the cohort this batch processes.
from audit.engine.normalize import fold_tag

#: Consecutive propose failures that stop a batch. One wedged provider used to
#: sweep the whole library at ~1/s, marking every remaining paper "failed".
MAX_CONSECUTIVE_FAILURES = 5
#: Ceiling for the exponential backoff between failed proposes, in seconds.
MAX_BACKOFF_S = 60.0


def backoff_s(consecutive: int, base_s: float = 1.0) -> float:
    """The wait after the *consecutive*-th failed propose in a row.

    The ONE schedule for every loop that proposes tags one paper at a time —
    this batch and the "update my library" analysis (`accession/library_run`,
    audit remediation 2026-09-23) — so a stuck provider is treated the same
    way wherever it is met.
    """
    return min(base_s * (2 ** consecutive), MAX_BACKOFF_S)
#: Snapshot age (minutes) past which the batch warns that "already tagged" may
#: be stale. A scan takes minutes, so anything fresher than this is very likely
#: the scan the user just ran for this batch.
STALE_SNAPSHOT_MIN = 30


@dataclass(frozen=True)
class BatchOptions:
    limit: int = 0
    dry_run: bool = False
    targets: tuple = ("note", "finder", "zotero")
    pdf_orphans: bool = True          # include rows that DO have a local PDF
    no_pdf_entries: bool = False      # also process PDF-less entries
    min_conf: float = 0.0
    sleep_s: float = 1.0
    wanted_keys: Optional[frozenset] = None  # None = no subset selection
    include_tagged: bool = False
    # Tag-DEPTH selection (2026-08-26). None = unbounded on that side; giving
    # either one replaces the "some requested target has zero tags" gate with a
    # bounded cohort, which is what makes re-proposing the UNDER-tagged (1-2
    # tags) reachable without hand-listing keys. Measured on the maintainer's
    # library: 431 papers at 0 tags, 484 at 1, 486 at 2.
    min_tags: Optional[int] = None
    max_tags: Optional[int] = None


@dataclass
class BatchOutcome:
    applied: int = 0
    skipped: int = 0
    failed: int = 0
    to_process: int = 0
    aborted: bool = False             # the consecutive-failure abort fired
    cancelled: bool = False           # should_cancel() stopped the walk
    error: Optional[str] = None       # a run-level error (inventory failed)


def _row_targets(row: Dict[str, Any], target_list) -> list:
    """The requested targets this row can actually be written to.

    A row with no local PDF has no Finder target; asking for it would only
    produce a skip-with-reason. Narrowing at SELECTION time rather than after
    the propose call is what makes an impossible combination (`--targets
    finder` with `--no-pdf-entries`) cost zero LLM calls instead of one per
    row — and it keeps the empty list away from the driver, where an empty
    selection used to serialise as "all targets on".
    """
    if row.get("has_pdf") is False:
        return [t for t in target_list if t != "finder"]
    return list(target_list)


def _untagged(row: Dict[str, Any], wanted) -> bool:
    """True when some REQUESTED target this row HAS carries no tags yet.

    Scoped to *wanted* so `--targets note` stops selecting a paper whose note
    is already tagged just because its Finder tags are empty — that row cost a
    full propose and then applied a union that changed nothing.
    """
    note, zot = row.get("note") or {}, row.get("zotero") or {}
    if ("finder" in wanted and row.get("has_pdf") is not False
            and not row.get("finder_tags")):
        return True
    if "note" in wanted and note.get("present") and not note.get("tags"):
        return True
    if "zotero" in wanted and zot.get("present") and not zot.get("parent_tags"):
        return True
    return False


def tag_depth(row: Dict[str, Any], wanted) -> int:
    """How many DISTINCT tags this row carries across its requested surfaces.

    The union, not a per-surface count or a minimum, because that is the
    question a depth selection is actually asking: "how well described is this
    paper?". The three surfaces are kept in step by the harmonize path, and a
    tag present on one but not another is that path's job, not a reason to
    re-run the classifier.

    Scoped to *wanted* and to the surfaces the row HAS, exactly like
    :func:`_untagged` — a record with no reference note is not a zero-tag note.
    Comparison goes through ``audit.engine.normalize.fold_tag`` — NFC, leading
    ``#``s stripped, case-folded — so a spelling variant across two surfaces
    counts once. That helper is the SINGLE definition shared with the dashboard
    histogram and the table's ``#`` cell; a private copy here is how those
    three drifted apart before.
    """
    note, zot = row.get("note") or {}, row.get("zotero") or {}
    tags: set[str] = set()
    if "finder" in wanted and row.get("has_pdf") is not False:
        tags.update(fold_tag(t) for t in (row.get("finder_tags") or []))
    if "note" in wanted and note.get("present"):
        tags.update(fold_tag(t) for t in (note.get("tags") or []))
    if "zotero" in wanted and zot.get("present"):
        tags.update(fold_tag(t) for t in (zot.get("parent_tags") or []))
    return len(tags)


def _in_depth_range(row: Dict[str, Any], wanted, opts: "BatchOptions") -> bool:
    """Whether *row* falls inside the requested ``--min-tags``/``--max-tags``."""
    depth = tag_depth(row, wanted)
    if opts.min_tags is not None and depth < opts.min_tags:
        return False
    if opts.max_tags is not None and depth > opts.max_tags:
        return False
    return True


def run_batch(
    opts: BatchOptions,
    *,
    inventory: Callable[..., Dict[str, Any]],
    propose: Callable[[str], Dict[str, Any]],
    apply: Callable[[str, list, list], Dict[str, Any]],
    model_info: Optional[Callable[[], Dict[str, Any]]] = None,
    echo: Callable[..., None],
    should_cancel: Optional[Callable[[], bool]] = None,
) -> BatchOutcome:
    """Walk the inventory, propose, apply. See the module docstring.

    *echo(msg, err=False)* receives every progress/report line (the CLI binds
    ``click.echo``; the maintenance step binds ``ctx.emit``). *should_cancel*
    is polled between papers (the CLI has none — Ctrl-C is its cancel).
    """
    out = BatchOutcome()
    cancelled = should_cancel or (lambda: False)

    scope = "all" if opts.no_pdf_entries else "with_pdf"
    inv = inventory(scope=scope)
    if "error" in inv:
        echo(f"Error: {inv['error']}", err=True)
        out.error = str(inv["error"])
        return out

    all_rows = inv.get("rows", [])
    rows = all_rows
    if not opts.no_pdf_entries:
        rows = [r for r in rows if r.get("has_pdf") is not False]
    if not opts.pdf_orphans:
        rows = [r for r in rows if r.get("has_pdf") is False]

    # Explicit subset selection (2026-08-13). Client-side, beside the has_pdf
    # filters: the inventory route returns every row and has no server-side
    # filter, so narrowing here costs one payload and no new endpoint.
    if opts.wanted_keys is not None:
        wanted_keys = set(opts.wanted_keys)
        rows = [r for r in rows if r.get("citation_key") in wanted_keys]
        # Name what was asked for and not found, rather than silently
        # processing fewer papers than the user listed — a typo'd or stale key
        # would otherwise look like "that paper was already tagged".
        found = {r.get("citation_key") for r in rows}
        missing = sorted(wanted_keys - found)
        echo(f"Selection: {len(wanted_keys)} key(s) requested, "
             f"{len(rows)} matched in this scope")
        if missing:
            # Blame the right filter: a key the inventory DID return but our
            # local --with-pdf/--no-with-pdf filter removed is in scope — saying
            # "outside scope" for it sent the user chasing the wrong flag.
            in_inventory = {r.get("citation_key") for r in all_rows}
            filtered_out = [k for k in missing if k in in_inventory]
            unknown = [k for k in missing if k not in in_inventory]

            def _head(ks):
                return ", ".join(ks[:10]) + ("…" if len(ks) > 10 else "")

            if filtered_out:
                echo(f"  ! Excluded by the --with-pdf/--no-with-pdf "
                     f"filter (in scope, removed client-side): "
                     f"{_head(filtered_out)}")
            if unknown:
                echo(f"  ! Not in the inventory (unknown, or outside "
                     f"scope={scope}): {_head(unknown)}")

    # Selection gate, in precedence order:
    #   1. an explicit depth range  -> the cohort it names (bounded by
    #      construction, so no key list is required);
    #   2. --include-tagged         -> no gate at all (requires --keys, which
    #      the caller validates before we get here);
    #   3. neither                  -> the historical "some requested target
    #      this row HAS carries no tags" gate, byte-identical.
    depth_selected = opts.min_tags is not None or opts.max_tags is not None
    todo = [(r, w) for r in rows
            if (w := _row_targets(r, opts.targets))
            and (_in_depth_range(r, w, opts) if depth_selected
                 else (opts.include_tagged or _untagged(r, w)))]
    if opts.limit > 0:
        todo = todo[:opts.limit]
    out.to_process = len(todo)

    echo(f"Inventory: {inv.get('count', 0)} rows (scope={scope}, "
         f"{inv.get('pdfless_count', 0)} without a PDF)")
    # The inventory is a projection of the audit scan snapshot, and no tagging
    # write updates it — so the age is what tells the user whether "already
    # tagged" can be trusted after an interrupted run.
    scanned_at = inv.get("scanned_at") or 0
    if scanned_at:
        age_min = max(0, int((time.time() - scanned_at) / 60))
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(scanned_at))
        age = f"{age_min} min ago" if age_min < 120 else f"{age_min // 60} h ago"
        echo(f"Snapshot: audit scan from {stamp} ({age})")
        if age_min >= STALE_SNAPSHOT_MIN:
            echo("  ! Tag writes since that scan are NOT reflected. "
                 "Run `papermind audit scan` first for an accurate resume.")
    else:
        echo("Snapshot: audit scan time unknown (older server?) — "
             "'already tagged' may be stale.")
    if depth_selected:
        lo = "0" if opts.min_tags is None else str(opts.min_tags)
        hi = "∞" if opts.max_tags is None else str(opts.max_tags)
        echo(f"Cohort: papers carrying {lo}–{hi} tag(s) across the requested "
             f"targets they have")
    echo(f"To process: {len(todo)}"
         + (" — DRY RUN, nothing will be written" if opts.dry_run else ""))
    if not todo:
        return out
    # Name the model BEFORE spending on it (2026-08-13). A batch is the app's
    # largest single LLM spend and nothing anywhere said which model it used.
    # Best-effort: a lookup failure must not stop a run that is otherwise fine.
    if model_info is not None:
        try:
            _cfg = model_info() or {}
            _prov, _model = _cfg.get("provider"), _cfg.get("tagging_model")
            if _prov and _model:
                echo(f"Model: {_model} (provider: {_prov})"
                     + ("" if not opts.dry_run else " — dry run still calls it"))
        except Exception:  # noqa: BLE001 — disclosure, never a gate
            pass

    consecutive = 0
    for i, (row, row_targets) in enumerate(todo, 1):
        if cancelled():
            out.cancelled = True
            echo("Cancelled — nothing after the last reported paper was "
                 "attempted.")
            break
        key = row.get("citation_key")
        res = propose(key)
        if "error" in res:
            echo(f"[{i}/{len(todo)}] {key}: propose failed — {res['error']}",
                 err=True)
            out.failed += 1
            consecutive += 1
            # The propose gate is single-flight, and a call wedged BEFORE its
            # first token holds it past our timeout — so the next paper 429s,
            # and the next. Sleeping only `--sleep` swept the library at ~1/s,
            # marking hundreds of papers failed for one stuck provider. Back
            # off, and stop entirely once it is clearly not transient.
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                echo(f"\nAborting: {consecutive} consecutive failures — "
                     f"the provider or the propose gate looks stuck. "
                     f"Nothing after this paper was attempted.", err=True)
                out.aborted = True
                break
            time.sleep(backoff_s(consecutive, opts.sleep_s))
            continue
        consecutive = 0
        proposal = res.get("proposal") or {}
        tags = [p["tag"] for p in (proposal.get("proposed") or [])
                if p.get("confidence", 0) >= opts.min_conf]
        source = proposal.get("source", "?")
        if not tags:
            echo(f"[{i}/{len(todo)}] {key}: no tags "
                 f"({proposal.get('status')}, source={source})")
            out.skipped += 1
            time.sleep(opts.sleep_s)
            continue
        if opts.dry_run:
            echo(f"[{i}/{len(todo)}] {key}: would apply {tags} (source={source})")
            out.skipped += 1
            time.sleep(opts.sleep_s)
            continue
        applied_res = apply(key, tags, row_targets)
        if "error" in applied_res:
            echo(f"[{i}/{len(todo)}] {key}: apply failed — "
                 f"{applied_res['error']}", err=True)
            out.failed += 1
        else:
            ok = [t for t, r in (applied_res.get("results") or {}).items()
                  if r.get("ok")]
            echo(f"[{i}/{len(todo)}] {key}: applied {tags} → {ok or 'nothing'} "
                 f"(source={source})")
            out.applied += 1
        time.sleep(opts.sleep_s)

    echo(f"\n{out.applied} applied, {out.skipped} skipped, {out.failed} failed.")
    return out
