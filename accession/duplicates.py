"""Duplicate and version recognition for bibliography PDFs (phase 7, 2026-09-16).

Pure. A filename suffix is a COLLISION CLUE, never proof of identical
content: ``smith_2020_1.pdf``, ``smith_2020 2.pdf``, ``smith_2020 (1).pdf``,
``smith_2020-1.pdf`` and ``smith_2020 copy.pdf`` all name a candidate group,
and only the bytes, the annotations and the dependencies of each member can
say what to keep. This module names the clue (:func:`variant_stem`) and
compares the members (:func:`compare`); it decides nothing and deletes
nothing. The kept copy is the user's explicit choice, and physical removal is
the separate, recoverable ``pdf_management`` step — refused while a file
carries native annotations, embedded annotations or dependencies unless the
user explicitly acknowledges each.
"""
from __future__ import annotations

import re
from pathlib import Path

#: Recognised collision/copy suffixes, in the order they are tried. Each group
#: keeps the base stem; the variant label is what the UI prints.
_VARIANTS = (
    (re.compile(r"^(?P<stem>.+?)[ _]+copy(?: (?P<n>\d+))?$", re.IGNORECASE), "copy"),
    (re.compile(r"^(?P<stem>.+?) \((?P<n>\d+)\)$"), "parenthesised"),
    (re.compile(r"^(?P<stem>.+?)_(?P<n>\d{1,3})$"), "underscore"),
    (re.compile(r"^(?P<stem>.+?) (?P<n>\d{1,3})$"), "space"),
    (re.compile(r"^(?P<stem>.+?)-(?P<n>\d{1,3})$"), "hyphen"),
)


def variant_stem(name: str) -> tuple[str, str | None, int | None]:
    """``(base stem, variant kind or None, counter or None)`` for a filename.

    The base stem is casefolded for grouping. A four-digit run is a year, not
    a counter (``smith_2020`` stays whole); ``smith_2020_1`` yields
    ``("smith_2020", "underscore", 1)``.
    """
    stem = Path(name).stem
    for pattern, kind in _VARIANTS:
        match = pattern.match(stem)
        if match:
            counter = match.group("n") if "n" in match.groupdict() else None
            return match.group("stem").casefold(), kind, int(counter) if counter else None
    return stem.casefold(), None, None


def group_candidates(names: list[str]) -> dict[str, list[str]]:
    """Group filenames by base stem; only groups with ≥2 members are candidates."""
    groups: dict[str, list[str]] = {}
    for name in names:
        groups.setdefault(variant_stem(name)[0], []).append(name)
    return {stem: sorted(members) for stem, members in groups.items() if len(members) > 1}


def compare(members: list[dict]) -> list[dict]:
    """Compare candidate members ``[{rel, sha256, size, native_annotations,
    embedded_annotations, note_dependencies, mapping_keys, attachment_keys}]``.

    Returns one row per member with ``identical_to`` (same bytes), ``relation``
    to the others (``identical`` / ``different`` / ``unknown``), the blocking
    facts a removal must show (annotations of either kind, dependencies) and a
    ``keep_hint`` that is only a suggestion — the explicit choice stays with the
    user. Nothing here authorises a removal.
    """
    rows = []
    for member in members:
        others = [m for m in members if m["rel"] != member["rel"]]
        identical = [m["rel"] for m in others if member.get("sha256") and m.get("sha256") == member["sha256"]]
        unknown = [m["rel"] for m in others if not member.get("sha256") or not m.get("sha256")]
        native = int(member.get("native_annotations") or 0)
        # ``None`` means the PDF could not be opened to count: UNKNOWN, never
        # zero (2026-09-17 review, B3). An absent key keeps the old default.
        unknown_count = "embedded_annotations" in member and member["embedded_annotations"] is None
        embedded = None if unknown_count else int(member.get("embedded_annotations") or 0)
        deps = list(member.get("note_dependencies") or [])
        blocking = []
        if native:
            blocking.append(f"{native} native Zotero annotation(s) on its attachment(s)")
        if unknown_count:
            blocking.append("annotation count unknown (the PDF could not be opened)")
        elif embedded:
            blocking.append(f"{embedded} annotation(s) embedded in the PDF")
        if deps:
            blocking.append(f"referenced by {len(deps)} vault note(s)")
        if len(member.get("mapping_keys") or []) > 1:
            blocking.append("mapped to more than one bibliography entry")
        stem, kind, counter = variant_stem(Path(member["rel"]).name)
        rows.append(dict(rel=member["rel"], variant=kind, counter=counter, base_stem=stem,
                         identical_to=identical, unknown_to=unknown,
                         different_from=[m["rel"] for m in others if m["rel"] not in identical and m["rel"] not in unknown],
                         native_annotations=native, embedded_annotations=embedded, note_dependencies=deps,
                         attachment_keys=list(member.get("attachment_keys") or []),
                         blocking=blocking, removable_without_acknowledgement=not blocking,
                         keep_hint=("annotation count unknown — keep" if unknown_count else
                                    "carries annotations — keep" if native or embedded else
                                    "has dependencies — keep or repair them first" if deps else
                                    "no annotations or dependencies" )))
    return rows
