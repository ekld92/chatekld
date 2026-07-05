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

from refactor import (
    cache,
    discrepancy,
    flags as flags_mod,
    hints,
    hygiene,
    ignore,
    normalize,
    resolver,
    text,
)
from refactor.resolver import (
    excluded_dirs,
    is_excluded,
    scan_embeds,
)
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
    image_flags: dict[str, set[str]] | None = None,
    *,
    strip_default: bool = False,
    link_index: dict[str, list[str]] | None = None,
) -> tuple[NoteProposal, list]:
    """Build one note's proposal; return ``(proposal, dose_occurrences)``.

    Public because the Phase 2 writers recompute the **identical** body at apply
    time — sharing this one analyzer is what guarantees preview == apply (the
    callout ``proposed`` body for ``apply.py``, the ``normalized`` body for
    ``format_fix.py``).

    *ignored* is the sticky ignore-list (vault-relative paths): a matching image
    is greyed in the UI, dropped from the candidate counts, and gets **no**
    inlined callout (so the proposed body never adds text for a set-aside image).

    *image_flags* is the per-image flag table (``refactor.flags``; rel_path ->
    set of flags). Two flags affect the proposed body, so they must be applied in
    this single shared analyzer (preview == apply, ``proposed_sha256`` honest):
      * ``strip``            — drop the descriptive preamble from this image's
        callout (``text.strip_ocr_preamble``);
      * ``keep_handwritten`` — force-inline a callout the handwritten auto-hide
        would otherwise suppress.

    *strip_default* is the scope-wide "strip the preamble for every image" config
    default (``refactor_strip_preamble_default``); it is **additive** to the
    per-image ``strip`` flag. Both the plan route and ``apply.py`` resolve it from
    the same config key so the recomputed ``proposed_sha256`` matches the preview.

    *link_index* (basename -> paths, whole-vault) drives the broken-wikilink
    advisory; ``None`` skips that check. It affects only ``hygiene_notes`` (never
    the sha-guarded body), so ``apply.py`` may omit it without breaking
    preview == apply.

    Reads the note's **raw bytes** (not ``read_text``) so the returned proposal
    can carry ``content_sha256`` (the on-disk byte hash, the apply stale-diff
    guard) and ``proposed_sha256`` (the exact bytes the writer will lay down).
    Decoding uses ``errors="replace"`` for preview fidelity; the writer itself
    refuses any note whose bytes are not a clean UTF-8 round-trip.
    """
    rel = note_path.relative_to(vault_root).as_posix()
    raw = note_path.read_bytes()
    body = raw.decode("utf-8", errors="replace")
    image_flags = image_flags or {}

    images: list[ImageRef] = []
    callouts_by_line: dict[int, list[list[str]]] = {}

    for occ in scan_embeds(body, note_path, vault_root, name_index):
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
        hw_likely, hw_reason = False, ""
        classification = ""
        status = info["status"]
        if status == STATUS_OK:
            digest = info["digest"]
            description = cache.best_description(digest, vault_root)
            has_table = bool(cache.read_mode(digest, vault_root, "table"))
            classification = cache.read_mode(digest, vault_root, "classify")
            likely, reason = hints.likely_table(description)
            hw_likely, hw_reason = hints.likely_handwritten(description)
        these_flags = image_flags.get(rel_path, frozenset())
        is_ignored = rel_path in ignored
        # Effective strip = the scope-wide default OR the per-image opt-in.
        strip_meta = strip_default or ("strip" in these_flags)
        kept_hw = "keep_handwritten" in these_flags
        image = ImageRef(
            raw_link=occ["raw"], target=occ["target"], rel_path=rel_path,
            line=occ["line"], status=status, description=description,
            has_table=has_table, likely_table=likely, likely_table_reason=reason,
            size=info["size"], classification=classification, ignored=is_ignored,
            likely_handwritten=hw_likely, likely_handwritten_reason=hw_reason,
            metadata_stripped=strip_meta, kept_handwritten=kept_hw,
        )
        images.append(image)
        # Inline an extracted-text callout unless the image is set aside (ignored)
        # or auto-hidden as handwritten (and not force-kept). When stripping is
        # opted in, the callout carries only the transcription, not the preamble.
        if description and not is_ignored and not image.handwritten_hidden:
            shown = text.strip_ocr_preamble(description) if strip_meta else description
            callouts_by_line.setdefault(occ["line"], []).append(_callout(shown))

    proposed = _inline_callouts(body, callouts_by_line)
    diff = "".join(difflib.unified_diff(
        body.splitlines(keepends=True),
        proposed.splitlines(keepends=True),
        fromfile=rel, tofile=rel,
    ))
    # Deterministic formatting-fix variant (independent of the callout body) —
    # the second Phase 2 batch action; recomputed identically by format_fix.py.
    normalized = normalize.normalize_text(body)
    normalize_diff = "".join(difflib.unified_diff(
        body.splitlines(keepends=True),
        normalized.splitlines(keepends=True),
        fromfile=rel, tofile=rel,
    ))
    hygiene_notes = (hygiene.embed_notes(images)
                     + hygiene.frontmatter_notes(body)
                     + hygiene.link_notes(body, link_index)
                     + hygiene.whitespace_notes(body)
                     + hygiene.structure_notes(body))
    proposal = NoteProposal(
        rel_path=rel, original=body, proposed=proposed, diff=diff,
        images=images, hygiene_notes=hygiene_notes,
        content_sha256=sha256_bytes(raw), proposed_sha256=sha256_text(proposed),
        normalized=normalized, normalized_sha256=sha256_text(normalized),
        normalize_diff=normalize_diff,
    )
    return proposal, discrepancy.extract_doses(rel, body)


def analyze_one(vault_root: Path, rel: str, *, strip_default: bool = False) -> NoteProposal:
    """Re-analyze a **single** note (read-only) and return its fresh proposal.

    The cheap counterpart of ``build_plan`` for the per-image OCR-inclusion panel:
    toggling an image's ignore-list / ``keep_handwritten`` flag changes which
    callouts ``analyze_note`` inlines, so the UI re-runs *this one note* (not the
    whole 135-note plan) to refresh the previewed ``proposed`` body + its WYSIWYG
    hash. Builds the same indexes + ignore-set + flag-table as ``build_plan`` and
    delegates to the shared ``analyze_note`` so preview == apply still holds. No
    vision calls, no vault writes. Raises ``OSError`` if the note is unreadable.
    """
    vault_root = Path(vault_root)
    excluded = excluded_dirs(vault_root)
    # Reuse the file index the preceding build_plan warmed (refresh=False). A
    # per-image OCR-inclusion toggle debounce-re-analyzes THIS note repeatedly;
    # the vault file set can't change under a toggle, so the cached index is
    # correct and we skip a full-vault walk per toggle. Falls back to a fresh
    # single-pass walk on a cold cache. See resolver's cache notes.
    name_index, link_index = resolver.get_file_index(vault_root, excluded)
    ignored = ignore.load_ignored(vault_root)
    image_flags = flags_mod.load_flags(vault_root)
    proposal, _doses = analyze_note(
        vault_root / rel, vault_root, name_index, ignored, image_flags,
        strip_default=strip_default, link_index=link_index)
    return proposal


def build_plan(
    vault_root: Path,
    scope_subdir: str,
    *,
    on_event: Optional[Callable[[dict], None]] = None,
    stop: Optional[Callable[[], bool]] = None,
    strip_default: bool = False,
) -> PlanResult:
    """Analyze the scoped notes read-only and return a :class:`PlanResult`.

    *on_event* (optional) receives ``{"info": ...}`` progress + ``{"note": ...}``
    per-note frames as they complete. *stop* (optional) is polled to abort early
    (client disconnect). *strip_default* is the scope-wide preamble-strip default
    (``refactor_strip_preamble_default``), resolved from config by the route and
    threaded into every note's analysis. No vision calls; no vault writes.
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
    # Single-pass whole-vault walk producing BOTH the image name_index and the
    # all-files link_index (the broken-wikilink advisory needs the superset).
    # refresh=True: the explicit "Run plan" action always re-walks from disk (a
    # user asking to re-scan must never get a stale view) AND repopulates the
    # cache that the subsequent debounced analyze_one bursts reuse. Halves the
    # directory I/O this plan previously spent on two separate walks.
    name_index, link_index = resolver.get_file_index(
        vault_root, excluded, refresh=True)
    # Sticky ignore-list + per-image flag table (both rel-path-keyed sidecars
    # under obsidian_cache, never the vault); loaded once so every note sees the
    # same sets.
    ignored = ignore.load_ignored(vault_root)
    image_flags = flags_mod.load_flags(vault_root)

    notes = _scope_notes(vault_root, scope_subdir, excluded)
    emit({"info": f"Analyzing {len(notes)} note(s) in {scope_subdir}…"})

    result = PlanResult(scope_subdir=scope_subdir)
    all_doses: list = []
    for i, note_path in enumerate(notes, start=1):
        if stop is not None and stop():
            break
        try:
            proposal, doses = analyze_note(
                note_path, vault_root, name_index, ignored, image_flags,
                strip_default=strip_default, link_index=link_index)
        except OSError:
            continue  # unreadable note — skip rather than abort the whole plan
        result.notes.append(proposal)
        all_doses.extend(doses)
        emit({"note": proposal.frame()})

    result.discrepancies = discrepancy.cross_check(all_doses)
    return result
