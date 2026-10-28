"""Obsidian reference notes kept in step with Zotero reading notes.

Batch E (2026-08-26). The maintainer writes a reading note on a Zotero item,
then runs the Zotero Integration plugin by hand to export it into
``Z_Zotero_Notes/<citation key>.md``. Measured on the live library the
invariant holds exactly — 1,231 Obsidian notes against 1,231 Zotero items
carrying a child note, with **zero** mismatches in either direction and only
4 of 1,231 differing in content. So this subsystem is not a repair job. It
exists to remove the vigilance: today the invariant holds because the
maintainer remembers, and nothing would say so if they stopped.

Three parts, deliberately separable:

* :mod:`notesync.render` — pure ``Record`` → note text. No I/O.
* :mod:`notesync.compare` — pure "is this note in step?", answering
  ``missing`` / ``drifted`` / ``ok``.
* :mod:`notesync.apply` — the writer, which CREATES missing notes through
  ``vault_history.create_note`` (already journalled, confirm-gated, refuses to
  overwrite and reversible from the existing Restore UI) and **refuses** a
  drifted one.

Two measured constraints shape all of it, and neither is obvious:

**File mtime cannot detect staleness in this vault.** Comparing each note's
mtime against its newest Zotero note timestamp reports zero stale notes, which
is wrong: the August 2026 tagging campaigns rewrote the frontmatter of every
note, so all 1,231 mtimes now post-date every Zotero edit. Comparison has to
be on content.

**The user edits notes after import.** In all four measured drift cases the
Obsidian body is LONGER than the Zotero note — 508 characters against 13, in
one. A regenerating writer would delete that work, which is why the writer
refuses and names the note instead (the biblio PDF renamer's precedent: when
there is no safe automatic answer, refuse and say which file and why).
"""

from __future__ import annotations

from notesync.compare import NoteState, compare_note  # noqa: F401
from notesync.render import DEFAULT_TEMPLATE, render_note  # noqa: F401

__all__ = ["NoteState", "compare_note", "DEFAULT_TEMPLATE", "render_note"]
