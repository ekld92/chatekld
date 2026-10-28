"""Storage check (phase 6, reduced by the 2026-09-16 decision): a READ-ONLY
control over the saved Zotero observation, with manual guidance.

The goal is zero PDFs in Zotero's own storage or cloud: every PDF is a linked
file under ``biblio_articles/``. Nothing here converts, moves, copies or
cleans anything — the plan's phase 6 conversion was replaced by this control,
and each finding names the manual step to take in Zotero or Attanger.

Findings, over every non-deleted attachment in the saved snapshot:

* ``stored``          — ``imported_file``, and any ``imported_url`` that is
                        not a web page: the bytes live in Zotero's storage
                        (and sync to the cloud). An ``imported_url`` whose
                        ``contentType`` is HTML/XHTML is a Connector snapshot
                        and is excluded; every other type (PDF, EPUB, DOCX,
                        an image) is a stored file and is reported.
* ``outside_biblio``  — a linked file whose absolute path is not under the
                        bibliography folder.
* ``broken_path``     — a linked file whose path does not resolve to a file.
* ``relative_link``   — a linked file expressed against Zotero's Linked
                        Attachment Base Directory (unset by decision).
* ``duplicate_path``  — two or more attachments point at the same file.
* ``remote``          — ``linked_url``: informational, not a file.

An attachment that is a linked file under the bibliography folder and
resolves is ``linked_ok`` and produces no finding.

Out of scope, never a finding (2026-09-17 review, decision D3): an
``embedded_image`` and any attachment whose parent is a NOTE — images Zotero
stores for its own notes, not PDFs. They are counted in the report's
``excluded`` field so the number stays visible.
"""
from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

STORED_MODES = ("imported_file", "imported_url")
GUIDANCE = {
    "stored": [
        "In Zotero, select the parent item and open the attachment's row.",
        "With Attanger installed: right-click the attachment → Attanger → Move attachment (to the configured bibliography folder). "
        "Attanger moves the file out of Zotero storage and turns the attachment into a linked file; check its path afterwards.",
        "Without Attanger: File → Manage Attachments offers only the reverse conversion in Zotero 7/10. Copy the file into "
        "biblio_articles/ yourself, add it as a linked file (Add Attachment → Link to File…), then move the stored attachment "
        "to the trash from Zotero once the linked copy is verified.",
        "Re-run a Library scan; the stored attachment must disappear from this list and the linked one appear as linked_ok.",
    ],
    "outside_biblio": [
        "Move the file into the bibliography folder (Finder), then re-point the attachment: Zotero Attachments tab → [re-point…], "
        "or in Zotero right-click the attachment → Show File / Locate to fix the path.",
        "If Attanger placed it there, check Attanger's destination directory setting so future links land under biblio_articles/.",
    ],
    "broken_path": [
        "The file is not at the recorded path. Find it (Finder search by name), put it back under biblio_articles/, then re-point "
        "the attachment from the Zotero Attachments tab ([re-point…]) or in Zotero (right-click → Locate…).",
        "If the file is gone for good, decide in Zotero whether to keep the attachment record; CHERQAiN never deletes it.",
    ],
    "relative_link": [
        "This path is relative to Zotero's Linked Attachment Base Directory, which is deliberately unset here. In Zotero → "
        "Settings → Advanced → Files and Folders, either leave the base directory unset and re-point the attachment to an "
        "absolute path under biblio_articles/, or set the base directory consciously for every relative link at once.",
    ],
    "duplicate_path": [
        "Two attachment records point at the same file. In Zotero, keep the one on the correct parent and move the other "
        "attachment record to the trash (its annotations go with it — check the annotation count first).",
    ],
    "remote": ["A linked URL is not a file; nothing to convert. Attach the PDF as a linked file if you have it."],
}


#: ``contentType`` prefixes of a Zotero Connector web snapshot — the only
#: ``imported_url`` rows out of scope. Everything else it imports is a file.
_SNAPSHOT_TYPES = ("text/html", "application/xhtml")


def _classify(item, settings):
    mode = item.get("link_mode") or "unknown"
    raw = str(item.get("path") or "")
    if mode == "imported_url":
        ctype = str(item.get("content_type") or "").lower()
        # Only a WEB PAGE is out of scope. The first cut excluded every
        # non-PDF type, so an EPUB, a DOCX or an image saved from a URL —
        # real bytes in Zotero's storage, which is exactly what this check
        # exists to find — vanished under a label that misdescribed it
        # (2026-09-20). Fail closed: a row that never recorded a type is a
        # stored file, never a web page we guessed.
        if any(ctype.startswith(t) for t in _SNAPSHOT_TYPES):
            return "web_snapshot", None
        return "stored", None
    if mode in STORED_MODES:
        return "stored", None
    if mode == "linked_url":
        return "remote", None
    if mode != "linked_file":
        return "unknown_mode", None
    if raw.startswith("attachments:"):
        return "relative_link", raw
    path = Path(raw) if raw else None
    if path is None or not path.is_absolute():
        return "relative_link", raw
    try:
        biblio = Path(settings.biblio_articles_dir).resolve()
        resolved = path.resolve() if path.exists() else path
        inside = resolved == biblio or biblio in resolved.parents
    except OSError:
        inside = False
    try:
        exists = os.path.isfile(raw)
    except OSError:
        exists = False
    if not exists:
        return "broken_path", raw
    if not inside:
        return "outside_biblio", raw
    return "linked_ok", raw


def check(settings, snapshot: dict | None, *, citation_keys: dict | None = None) -> dict:
    """Pure control over *snapshot* (``reconciliation.saved(settings)``).
    ``citation_keys`` maps ``(library, item_key)`` → citation key when known.
    Never writes; every result carries ``applicable: False``."""
    snapshot = snapshot or {}
    native = snapshot.get("items", {})
    parents = {(i["library"], i["key"]): i for i in native.values()
               if not i.get("deleted") and i.get("item_type") not in ("note", "attachment")}
    results: list[dict] = []
    by_path: dict[str, list[dict]] = {}
    population: Counter[str] = Counter()
    excluded: Counter[str] = Counter()
    notes = {(i["library"], i["key"]) for i in native.values() if i.get("item_type") == "note"}
    # Zotero flags only the PARENT when an item is trashed; its attachments
    # go to the trash with it without a flag of their own (2026-09-18, a
    # record merge's retired item left its linked copy counted here).
    trashed = {(i["library"], i["key"]) for i in native.values() if i.get("deleted")}
    for item in native.values():
        if item.get("deleted") or item.get("item_type") != "attachment":
            continue
        if (item["library"], item.get("parent")) in trashed:
            continue
        if item.get("link_mode") == "embedded_image":
            excluded["embedded_image"] += 1
            continue
        if (item["library"], item.get("parent")) in notes:
            excluded["note_child"] += 1
            continue
        kind, path = _classify(item, settings)
        if kind == "web_snapshot":
            excluded["web_snapshot"] += 1
            continue
        population["attachments"] += 1
        population[kind] += 1
        parent = parents.get((item["library"], item.get("parent")))
        entry = dict(id=f"attachment:{snapshot.get('database')}:{item['library']}:{item['key']}",
                     attachment_key=item["key"], parent_key=item.get("parent"),
                     parent_title=(parent or {}).get("fields", {}).get("title") if parent else None,
                     citation_key=(citation_keys or {}).get((item["library"], item.get("parent"))),
                     library=item["library"], link_mode=item.get("link_mode"), path=item.get("path"),
                     finding=kind, applicable=False, writes=False)
        if kind not in ("linked_ok", "unknown_mode"):
            entry["guidance"] = GUIDANCE.get(kind, [])
        results.append(entry)
        if path:
            try:
                normalised = os.path.normcase(os.path.normpath(path))
            except (TypeError, ValueError):
                normalised = path
            by_path.setdefault(normalised, []).append(entry)
    for entries in by_path.values():
        if len(entries) < 2:
            continue
        keys = [e["attachment_key"] for e in entries]
        for entry in entries:
            entry["duplicate_of"] = [k for k in keys if k != entry["attachment_key"]]
            if entry["finding"] == "linked_ok":
                entry["finding"] = "duplicate_path"
                entry["guidance"] = GUIDANCE["duplicate_path"]
            else:
                entry.setdefault("also", []).append("duplicate_path")
            population["duplicate_path"] += 1
    findings = [r for r in results if r["finding"] not in ("linked_ok", "unknown_mode", "remote")]
    return dict(
        results=results, findings=findings, excluded=dict(excluded),
        excluded_reason="Images embedded in Zotero notes are not PDFs and are out of scope for this control.",
        counts=dict(Counter(r["finding"] for r in results)),
        population=dict(population),
        goal="Zero PDFs in Zotero storage or cloud: every PDF a linked file under the bibliography folder.",
        destination=str(settings.biblio_articles_dir),
        observation=dict(database=snapshot.get("database"), checked_at=snapshot.get("checked_at"),
                         available=bool(native)),
        method="read-only control over the saved Zotero observation; manual correction in Zotero or Attanger",
        writes=False, applicable=False, conversion_offered=False,
    )
