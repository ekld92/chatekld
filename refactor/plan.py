"""Read-only plan orchestrator: resolve → reuse cache → diff → discrepancy.

``build_plan`` makes **zero vision calls** and **zero vault writes**. It walks the
scoped sub-folder's notes, resolves each image embed, reuses the indexer's cached
description (and a refactor table cache if one exists), inlines an advisory
extracted-text callout beneath each described embed (preview only), computes a
unified diff, runs hygiene checks, and assembles the advisory cross-note dose
discrepancy report. Progress streams through the optional ``on_event`` callback.
"""
from __future__ import annotations

import difflib
from pathlib import Path
from typing import Callable, Optional

from core.constants import OBSIDIAN_EXCLUDED_DIR_NAMES, REFACTOR_THUMBS_DIRNAME

from refactor import cache, discrepancy, hints, hygiene, ignore
from refactor.resolver import build_name_index, excluded_dirs, is_excluded, scan_embeds
from refactor.result import (
    STATUS_OK,
    STATUS_UNRESOLVED,
    ImageRef,
    NoteProposal,
    PlanResult,
    sha256_bytes,
    sha256_text,
)


def _scope_notes(vault_root: Path, scope_subdir: str, excluded: set[str]) -> list[Path]:
    """Sorted ``.md`` files under the scope sub-folder.

    Skips both the reserved excluded dirs (checked on path parts) and the user's
    ``vault_exclude_dirs`` (*excluded*, checked on the vault-relative path), so a
    note inside a folder the user excluded from indexing is never analyzed.
    """
    scope = vault_root / scope_subdir
    notes: list[Path] = []
    for p in sorted(scope.rglob("*.md")):
        if not p.is_file():
            continue
        if any(part in OBSIDIAN_EXCLUDED_DIR_NAMES for part in p.parts):
            continue
        try:
            rel = p.relative_to(vault_root).as_posix()
        except ValueError:
            continue
        if is_excluded(rel, excluded):
            continue
        notes.append(p)
    return notes


def _callout(description: str) -> list[str]:
    """Render a description as an advisory Obsidian callout (list of lines)."""
    body = description.strip().splitlines() or [""]
    return ["> [!extracted] Image text (from cache — review before applying)"] + [
        ("> " + ln).rstrip() for ln in body
    ]


def _inline_callouts(text: str, callouts_by_line: dict[int, list[list[str]]]) -> str:
    """Insert callouts beneath the embed lines. No-op when there are none."""
    if not callouts_by_line:
        return text
    lines = text.split("\n")
    out: list[str] = []
    for idx, line in enumerate(lines, start=1):
        out.append(line)
        for callout in callouts_by_line.get(idx, ()):
            out.append("")
            out.extend(callout)
    return "\n".join(out)


def _is_thumb_rel(rel_path: str) -> bool:
    """True if *rel_path* lives in a refactor ``_thumbs/`` folder.

    The Phase 2 archiver writes small PNG thumbnails into ``<scope>/_thumbs/``
    and rewrites the note's embed to point at one. A re-plan after archiving
    would otherwise resolve that thumbnail as a fresh, undescribed image embed
    ("not extracted") — noise for an image the user deliberately archived. So
    the planner skips any embed already resolving into a ``_thumbs`` segment.
    """
    return REFACTOR_THUMBS_DIRNAME in rel_path.split("/")


def analyze_note(
    note_path: Path,
    vault_root: Path,
    name_index: dict[str, list[str]],
    ignored: set[str],
) -> tuple[NoteProposal, list]:
    """Build one note's proposal; return ``(proposal, dose_occurrences)``.

    Public because the Phase 2 writer (``refactor/apply.py``) recomputes the
    **identical** callout body at apply time — sharing this one analyzer is what
    guarantees preview == apply.

    *ignored* is the sticky ignore-list (vault-relative paths): a matching image
    is greyed in the UI, dropped from the candidate counts, and gets **no**
    inlined callout (so the proposed body never adds text for a set-aside image).

    Reads the note's **raw bytes** (not ``read_text``) so the returned proposal
    can carry ``content_sha256`` (the on-disk byte hash, the apply stale-diff
    guard) and ``proposed_sha256`` (the exact bytes the writer will lay down).
    Decoding uses ``errors="replace"`` for preview fidelity; the writer itself
    refuses any note whose bytes are not a clean UTF-8 round-trip.
    """
    rel = note_path.relative_to(vault_root).as_posix()
    raw = note_path.read_bytes()
    text = raw.decode("utf-8", errors="replace")

    images: list[ImageRef] = []
    callouts_by_line: dict[int, list[list[str]]] = {}

    for occ in scan_embeds(text, note_path, vault_root, name_index):
        if not occ["is_image"]:
            continue  # non-image attachments (mp3/webp-as-audio/etc.) are out of scope
        rel_path = occ["rel_path"]
        if rel_path and _is_thumb_rel(rel_path):
            continue  # already-archived thumbnail — not a candidate for inlining
        if not rel_path:
            images.append(ImageRef(
                raw_link=occ["raw"], target=occ["target"], rel_path="",
                line=occ["line"], status=STATUS_UNRESOLVED,
            ))
            continue
        # Read-only: never materialize an iCloud placeholder during the plan
        # (status comes back STATUS_DATALESS instead). digest_for's status
        # strings ARE the STATUS_* values, so for any non-OK status we pass it
        # through verbatim; only OK proceeds to the cache lookup.
        info = cache.digest_for(rel_path, vault_root, materialize=False)
        description = ""
        has_table = False
        likely, reason = False, ""
        classification = ""
        status = info["status"]
        if status == STATUS_OK:
            digest = info["digest"]
            description = cache.best_description(digest, vault_root)
            has_table = bool(cache.read_mode(digest, vault_root, "table"))
            classification = cache.read_mode(digest, vault_root, "classify")
            likely, reason = hints.likely_table(description)
        is_ignored = rel_path in ignored
        images.append(ImageRef(
            raw_link=occ["raw"], target=occ["target"], rel_path=rel_path,
            line=occ["line"], status=status, description=description,
            has_table=has_table, likely_table=likely, likely_table_reason=reason,
            size=info["size"], classification=classification, ignored=is_ignored,
        ))
        if description and not is_ignored:
            callouts_by_line.setdefault(occ["line"], []).append(_callout(description))

    proposed = _inline_callouts(text, callouts_by_line)
    diff = "".join(difflib.unified_diff(
        text.splitlines(keepends=True),
        proposed.splitlines(keepends=True),
        fromfile=rel, tofile=rel,
    ))
    hygiene_notes = hygiene.embed_notes(images) + hygiene.frontmatter_notes(text)
    proposal = NoteProposal(
        rel_path=rel, original=text, proposed=proposed, diff=diff,
        images=images, hygiene_notes=hygiene_notes,
        content_sha256=sha256_bytes(raw), proposed_sha256=sha256_text(proposed),
    )
    return proposal, discrepancy.extract_doses(rel, text)


def build_plan(
    vault_root: Path,
    scope_subdir: str,
    *,
    on_event: Optional[Callable[[dict], None]] = None,
    stop: Optional[Callable[[], bool]] = None,
) -> PlanResult:
    """Analyze the scoped notes read-only and return a :class:`PlanResult`.

    *on_event* (optional) receives ``{"info": ...}`` progress + ``{"note": ...}``
    per-note frames as they complete. *stop* (optional) is polled to abort early
    (client disconnect). No vision calls; no vault writes.
    """
    def emit(evt: dict) -> None:
        if on_event is not None:
            on_event(evt)

    vault_root = Path(vault_root)
    # Compute the user's exclusion set once and apply it to BOTH the vault-wide
    # attachment index and the scoped note walk, so the analyzer's view matches
    # the indexer's exactly (see resolver.excluded_dirs).
    excluded = excluded_dirs(vault_root)
    emit({"info": "Indexing vault attachments for link resolution…"})
    name_index = build_name_index(vault_root, excluded)
    # Sticky ignore-list (rel-path-keyed sidecar under obsidian_cache, never the
    # vault); loaded once so every note sees the same set.
    ignored = ignore.load_ignored(vault_root)

    notes = _scope_notes(vault_root, scope_subdir, excluded)
    emit({"info": f"Analyzing {len(notes)} note(s) in {scope_subdir}…"})

    result = PlanResult(scope_subdir=scope_subdir)
    all_doses: list = []
    for i, note_path in enumerate(notes, start=1):
        if stop is not None and stop():
            break
        try:
            proposal, doses = analyze_note(note_path, vault_root, name_index, ignored)
        except OSError:
            continue  # unreadable note — skip rather than abort the whole plan
        result.notes.append(proposal)
        all_doses.extend(doses)
        emit({"note": proposal.frame()})

    result.discrepancies = discrepancy.cross_check(all_doses)
    return result
