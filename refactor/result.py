"""Shared, third-party-free result dataclasses for the note-refactor analyzer.

These mirror the ``deckgen.result`` / audit ``serialize`` split: pure data with
small ``*_frame()`` / ``to_jsonable()`` helpers so the route layer can stream
them as SSE / JSON without importing the orchestrator. No project imports here so
the types stay cheap to import in tests.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional


# Per-image extraction status (drives the UI badge and the "not extracted" count).
# These string VALUES are the contract between ``cache.digest_for`` (which
# produces them) and this module; ``cache.py`` imports these constants rather
# than re-typing the literals so a rename can never silently drift the two apart.
STATUS_OK = "ok"                 # bytes hashed; cache may or may not hold a description
STATUS_MISSING = "missing"       # embed resolved to a path that does not exist
STATUS_UNRESOLVED = "unresolved"  # link could not be resolved to any vault file
STATUS_DATALESS = "dataless"     # iCloud placeholder, not materialized (not read)
STATUS_TOO_BIG = "too_big"       # exceeds the 20 MB vault image cap (never described)
STATUS_READ_ERROR = "read_error"  # stat/read failed


@dataclass
class ImageRef:
    """One image-embed occurrence in a note (one per textual occurrence)."""

    raw_link: str            # the matched embed text, e.g. "![](7A27.png)"
    target: str              # the normalized link target
    rel_path: str            # resolved vault-relative path ("" if unresolved)
    line: int                # 1-based line number of the embed in the note
    status: str              # one of STATUS_*
    description: str = ""     # existing cached description ("" if none / not read)
    has_table: bool = False   # a refactor table extraction is already cached
    likely_table: bool = False
    likely_table_reason: str = ""
    size: int = -1            # file size in bytes (-1 unknown)
    classification: str = ""  # cached classify label ("" if not classified)
    ignored: bool = False     # on the sticky ignore-list (greyed, no callout)

    @property
    def extracted(self) -> bool:
        """True when something has already been extracted for this image.

        Either the indexer cached a prose description or the refactor tool cached
        a table transcription — both mean there is reusable text and the image is
        not a "not extracted" candidate.
        """
        return bool(self.description) or self.has_table

    def to_jsonable(self) -> dict:
        """Flatten to a plain JSON dict for the SSE ``note`` frame.

        ``extracted`` is materialized as a field (not just a property) so the UI
        does not have to recompute the description-or-table rule client-side.
        """
        return {
            "raw_link": self.raw_link,
            "target": self.target,
            "rel_path": self.rel_path,
            "line": self.line,
            "status": self.status,
            "description": self.description,
            "has_table": self.has_table,
            "likely_table": self.likely_table,
            "likely_table_reason": self.likely_table_reason,
            "size": self.size,
            "classification": self.classification,
            "ignored": self.ignored,
            "extracted": self.extracted,
        }


@dataclass
class HygieneNote:
    """An advisory, never-auto-applied formatting/link observation."""

    kind: str                # "broken_embed" | "unresolved_embed" | "frontmatter"
    message: str
    line: int = 0

    def to_jsonable(self) -> dict:
        """Flatten to a plain JSON dict for the SSE ``note`` frame."""
        return {"kind": self.kind, "message": self.message, "line": self.line}


@dataclass
class NoteProposal:
    """A single note's proposed (preview-only) refactor."""

    rel_path: str
    original: str
    proposed: str
    diff: str
    images: list = field(default_factory=list)         # list[ImageRef]
    hygiene_notes: list = field(default_factory=list)   # list[HygieneNote]
    # Phase 2 apply guards. content_sha256 is the sha256 of the note's RAW file
    # bytes as read by the planner; the UI echoes it back to /api/refactor/apply
    # and the writer re-reads the file and refuses the write if the on-disk bytes
    # changed (stale-diff guard). proposed_sha256 is the sha256 of
    # ``proposed.encode("utf-8")`` — the exact bytes the writer will lay down —
    # so the writer can confirm what it recomputes still matches what the user
    # previewed (a cache-drift / WYSIWYG tripwire). Both default "" for callers
    # (Phase 1 tests) that build a proposal without the hashes.
    content_sha256: str = ""
    proposed_sha256: str = ""

    @property
    def changed(self) -> bool:
        """True when the proposed body differs from the original.

        Drives the UI's "changed" badge and the ``changed_count`` summary; only a
        changed note is worth an Apply (an unchanged note's write would be a no-op).
        """
        return self.proposed != self.original

    def frame(self) -> dict:
        """SSE frame for one note.

        Includes the full ``original`` + ``proposed`` bodies so the UI's
        single-note detail pane can render ORIGINAL vs PROPOSED markdown
        client-side — no extra round-trip, no name_index rebuild, no staleness.
        This roughly doubles per-note frame size (~10 KB/note), which is
        negligible for the scoped sub-folder (~135 notes / ~0.7 MB). The unified
        ``diff`` is still sent for the diff view. ``content_sha256`` /
        ``proposed_sha256`` ride along so the UI can echo them back to the
        Phase 2 apply endpoint (stale-diff + WYSIWYG guards).
        """
        return {
            "rel_path": self.rel_path,
            "changed": self.changed,
            "diff": self.diff,
            "original": self.original,
            "proposed": self.proposed,
            "content_sha256": self.content_sha256,
            "proposed_sha256": self.proposed_sha256,
            "images": [im.to_jsonable() for im in self.images],
            "hygiene_notes": [h.to_jsonable() for h in self.hygiene_notes],
        }


def sha256_text(text: str) -> str:
    """sha256 hex of *text* encoded as UTF-8 (the bytes the writer lays down)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(raw: bytes) -> str:
    """sha256 hex of raw bytes (a note file's on-disk content)."""
    return hashlib.sha256(raw).hexdigest()


@dataclass
class DoseOccurrence:
    """One ``(note, line, dose)`` hit from the cross-note dose check.

    ``value_mg`` is the dose normalized to milligrams when the unit was
    recognized (else ``None``) — that normalized value is what ``cross_check``
    compares across notes to flag a spread.
    """

    note: str
    line: int
    dose: str               # raw matched dose text, e.g. "20 mg"
    value_mg: Optional[float] = None  # normalized to mg when possible

    def to_jsonable(self) -> dict:
        """Flatten to a plain JSON dict for the discrepancy report frame."""
        return {"note": self.note, "line": self.line, "dose": self.dose, "value_mg": self.value_mg}


@dataclass
class Discrepancy:
    """An advisory cross-note dose inconsistency for one subject."""

    subject: str
    reason: str
    occurrences: list = field(default_factory=list)   # list[DoseOccurrence]

    def to_jsonable(self) -> dict:
        """Flatten to a plain JSON dict (with nested occurrences) for the report frame."""
        return {
            "subject": self.subject,
            "reason": self.reason,
            "occurrences": [o.to_jsonable() for o in self.occurrences],
        }


@dataclass
class PlanResult:
    """The whole read-only analysis of one refactor-scope run.

    Carries the per-note proposals and the advisory cross-note discrepancies; the
    summary properties (counts) are derived on demand from ``notes`` so they can
    never drift out of sync with the proposals they summarize.
    """

    scope_subdir: str = ""
    notes: list = field(default_factory=list)          # list[NoteProposal]
    discrepancies: list = field(default_factory=list)   # list[Discrepancy]

    @property
    def note_count(self) -> int:
        return len(self.notes)

    @property
    def image_count(self) -> int:
        # Total image-embed OCCURRENCES across all notes (an image embedded
        # twice counts twice — the planner records one ImageRef per occurrence).
        return sum(len(n.images) for n in self.notes)

    @property
    def not_extracted_count(self) -> int:
        # Every ImageRef is an image embed (plan.py skips non-image attachments
        # before constructing one), so "not extracted" means simply: no cached
        # description AND no cached table (``ImageRef.extracted``). Missing /
        # dataless / too-big images count here too — they have nothing extracted
        # and are precisely what the user may want to act on. Ignored images are
        # excluded: the user deliberately set them aside.
        return sum(1 for n in self.notes for im in n.images
                   if not im.extracted and not im.ignored)

    @property
    def likely_table_count(self) -> int:
        # Images whose existing prose tripped the zero-vision table heuristic —
        # the candidates worth a manual "Extract table" pass (ignored excluded).
        return sum(1 for n in self.notes for im in n.images
                   if im.likely_table and not im.ignored)

    @property
    def ignored_count(self) -> int:
        # Images on the sticky ignore-list (greyed in the UI, no inlined callout).
        return sum(1 for n in self.notes for im in n.images if im.ignored)

    @property
    def handwritten_count(self) -> int:
        # Images a classify pass labelled "handwritten" (can't reliably OCR).
        return sum(1 for n in self.notes for im in n.images
                   if im.classification == "handwritten")

    @property
    def changed_count(self) -> int:
        # Notes whose proposed body differs from the original (i.e. at least one
        # cached description was inlined as a preview callout).
        return sum(1 for n in self.notes if n.changed)

    def summary_frame(self) -> dict:
        """Terminal SSE frame: counts + the advisory discrepancy report."""
        return {
            "scope_subdir": self.scope_subdir,
            "note_count": self.note_count,
            "image_count": self.image_count,
            "changed_count": self.changed_count,
            "not_extracted_count": self.not_extracted_count,
            "likely_table_count": self.likely_table_count,
            "ignored_count": self.ignored_count,
            "handwritten_count": self.handwritten_count,
            "discrepancies": [d.to_jsonable() for d in self.discrepancies],
        }
