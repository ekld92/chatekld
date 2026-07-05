"""Zotero SQLite connector (read-only).

Zotero holds a write lock on zotero.sqlite while running, and even with
`mode=ro` SQLite can balk on a hot WAL. We open the source DB read-only
and ``backup()`` it into an in-memory copy before running any queries,
so the live Zotero database is never touched.

Returned items distinguish:
- `tags`: parent-item tags (== _master.bib `keywords` once BBT exports them)
- `child_notes`: list of ZoteroChildNote (the user's reading notes attached
  to the parent). Each child note carries its own tags, body, mtime.
"""

from __future__ import annotations

import datetime as _dt
import shutil
import sqlite3
import tempfile
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup


def _strip_html(s: str | None) -> str:
    """Render Zotero's HTML note body down to plain text.

    Zotero stores child-note bodies as HTML; we only need the text (for the
    body-length proxy and any downstream tag/word inspection), so the markup
    is discarded. Returns ``""`` for an empty/None body so callers never have
    to None-guard the result.
    """
    if not s:
        return ""
    return BeautifulSoup(s, "html.parser").get_text()


def _parse_dt(s: str | None) -> _dt.datetime | None:
    """Parse Zotero's ``YYYY-MM-DD HH:MM:SS`` UTC timestamp, or None.

    Forgiving by design: a missing or non-conforming string yields ``None``
    rather than raising, since a malformed ``dateModified`` must not abort the
    whole read of an otherwise-valid library.
    """
    if not s:
        return None
    try:
        return _dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


@dataclass
class ZoteroAttachment:
    """One Zotero attachment row resolved against the storage tree.

    ``storage_path`` is populated only for ``storage:``-scheme attachments
    (Zotero-managed files under ``storage/<key>/``); linked/relative
    attachments leave it ``None`` (``is_stored=False``) because the audit
    resolves PDFs from the vault's ``biblio_articles`` tree, not from Zotero's
    own storage. Currently part of the read but unused downstream — see the
    "dead Zotero attachments pipeline" note in the root CLAUDE.md.
    """
    item_key: str
    parent_key: str | None
    filename: str
    storage_path: Path | None
    is_stored: bool
    content_type: str | None


@dataclass
class ZoteroChildNote:
    """A reading note attached to a parent bibliographic item.

    The user's workflow attaches a child note when they actually read a paper,
    so a parent item *having* a child note is the audit's "read" signal (see
    ``reports/zotero_unread``). ``tags`` are the note's own Zotero tags, which
    ``reports/note_tag_drift`` reconciles against the Obsidian note's YAML tags.
    ``body_length`` is the plain-text length, kept as a cheap engagement proxy.
    """

    note_key: str
    parent_item_id: int | None
    tags: list[str] = field(default_factory=list)
    body_plain: str = ""
    body_length: int = 0
    date_modified: _dt.datetime | None = None


@dataclass
class ZoteroItem:
    """A top-level (parent) Zotero item: the bibliographic record.

    Only non-note, non-attachment types become ``ZoteroItem``s. ``tags`` are
    the parent's own tags — equal to the ``keywords`` field of the matching
    ``_master.bib`` entry once Better BibTeX has exported them, which is why
    the inventory can join on title and then compare tag sets. ``child_notes``
    are the attached reading notes.
    """

    item_key: str
    item_type: str
    title: str | None
    tags: list[str] = field(default_factory=list)  # parent-item tags
    attachments: list[ZoteroAttachment] = field(default_factory=list)
    child_notes: list[ZoteroChildNote] = field(default_factory=list)


# Ceiling for the copy-based snapshot (sqlite + -wal + -shm combined). A
# Zotero DB is typically tens of MB; past this we fall back to the direct
# mode=ro backup rather than duplicating gigabytes on every scan.
_SNAPSHOT_COPY_MAX_BYTES = 2 * 1024**3


@contextmanager
def _open_snapshot(sqlite_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield an in-memory read-only snapshot of the Zotero database.

    Hardened technique (improvement plan 0.7 — adopted from the
    ``zotero-debug`` CLI helper, which never exhibited the hot-WAL issues the
    direct path did): copy ``zotero.sqlite`` plus any ``-wal``/``-shm``
    siblings to a temp dir, open the COPY with ``mode=ro&immutable=1``, and
    ``backup()`` that into a throwaway ``:memory:`` connection all queries
    run against. The live database is only ever touched by ``shutil.copy2``
    (plain file reads) — no SQLite locks are taken on it at all, so a
    running Zotero (write lock + hot WAL) can neither block us nor be
    perturbed. The copy is deleted before the snapshot is used.

    Fallback: if the files exceed ``_SNAPSHOT_COPY_MAX_BYTES``, the previous
    direct technique (``mode=ro`` open + ``backup()``) is used — weaker
    against a hot WAL, but proven adequate and copy-free.
    """
    if not sqlite_path.exists():
        raise FileNotFoundError(f"Zotero DB not found at {sqlite_path}")

    siblings = [
        sqlite_path.with_name(sqlite_path.name + suffix)
        for suffix in ("-wal", "-shm")
    ]
    try:
        total = sqlite_path.stat().st_size + sum(
            s.stat().st_size for s in siblings if s.exists()
        )
    except OSError:
        total = None

    mem_conn = sqlite3.connect(":memory:")
    try:
        copied = False
        if total is not None and total <= _SNAPSHOT_COPY_MAX_BYTES:
            try:
                with tempfile.TemporaryDirectory(prefix="chatekld-zotero-") as td:
                    tmp = Path(td) / "zotero.sqlite"
                    shutil.copy2(sqlite_path, tmp)
                    for suffix in ("-wal", "-shm"):
                        sib = sqlite_path.with_name(sqlite_path.name + suffix)
                        if sib.exists():
                            shutil.copy2(sib, tmp.with_name(tmp.name + suffix))
                    source_conn = sqlite3.connect(
                        f"file:{tmp}?mode=ro&immutable=1", uri=True,
                    )
                    try:
                        source_conn.backup(mem_conn)
                    finally:
                        source_conn.close()
                copied = True
            except (sqlite3.Error, OSError):
                # A copy of a HOT WAL can be torn mid-checkpoint (or the copy
                # itself can fail); degrade to the direct path below rather
                # than failing the scan. A full backup() overwrites the whole
                # destination, so any partial copy-branch content is replaced.
                copied = False
        if not copied:
            source_conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
            try:
                source_conn.backup(mem_conn)
            finally:
                source_conn.close()
        mem_conn.row_factory = sqlite3.Row
        yield mem_conn
    finally:
        mem_conn.close()


def _resolve_storage_path(
    storage_root: Path, key: str, raw_path: str
) -> tuple[Path | None, bool, str]:
    """Decode an ``itemAttachments.path`` cell into ``(path, is_stored, filename)``.

    Zotero encodes an attachment's location with a small scheme vocabulary:
    - ``storage:<name>`` — a Zotero-managed file at ``storage/<key>/<name>``;
      resolves to a concrete path with ``is_stored=True``.
    - ``attachments:<rel>`` — a "linked attachments" base-dir relative path;
      we cannot resolve it without the user's base-dir setting, so only the
      basename is returned (``path=None``).
    - a bare path — kept only if absolute; relative bare paths resolve to None.
    Returns the filename in every branch so callers always have a display name.
    """
    if raw_path is None:
        return None, False, ""
    if raw_path.startswith("storage:"):
        fname = raw_path[len("storage:") :]
        return storage_root / key / fname, True, fname
    if raw_path.startswith("attachments:"):
        rel = raw_path[len("attachments:") :]
        return None, False, Path(rel).name
    p = Path(raw_path)
    return (p if p.is_absolute() else None), False, p.name


def read_items(sqlite_path: Path, storage_root: Path) -> list[ZoteroItem]:
    """Read every non-trashed parent item, with tags, attachments and notes.

    Read-only: all work happens against the in-memory snapshot from
    :func:`_open_snapshot`. Each table is pulled in one bulk query and joined
    in Python (cheaper than N correlated subqueries for a personal library),
    excluding rows present in ``deletedItems`` so trashed entries never surface
    in a report. Only non-note/non-attachment types become parents; notes and
    attachments are folded onto their parent by ``parentItemID``. Standalone
    (parent-less) notes and attachments are intentionally dropped — they don't
    fit the "bibliographic item with reading notes" model the reports assume.
    """
    with _open_snapshot(sqlite_path) as conn:
        items_rows = conn.execute(
            """
            SELECT items.itemID AS id, items.key AS key, itemTypes.typeName AS type,
                   items.dateModified AS dt
            FROM items
            JOIN itemTypes ON itemTypes.itemTypeID = items.itemTypeID
            WHERE items.itemID NOT IN (SELECT itemID FROM deletedItems)
            """
        ).fetchall()

        title_rows = conn.execute(
            """
            SELECT itemData.itemID AS id, itemDataValues.value AS value
            FROM itemData
            JOIN fields ON fields.fieldID = itemData.fieldID
            JOIN itemDataValues ON itemDataValues.valueID = itemData.valueID
            WHERE fields.fieldName = 'title'
            """
        ).fetchall()
        titles = {r["id"]: r["value"] for r in title_rows}

        tag_rows = conn.execute(
            """
            SELECT itemTags.itemID AS id, tags.name AS name
            FROM itemTags JOIN tags ON tags.tagID = itemTags.tagID
            """
        ).fetchall()
        tags_by_item: dict[int, list[str]] = defaultdict(list)
        for r in tag_rows:
            tags_by_item[r["id"]].append(r["name"])

        att_rows = conn.execute(
            """
            SELECT itemAttachments.itemID AS id,
                   itemAttachments.parentItemID AS parent_id,
                   itemAttachments.path AS path,
                   itemAttachments.contentType AS content_type,
                   items.key AS key
            FROM itemAttachments
            JOIN items ON items.itemID = itemAttachments.itemID
            WHERE itemAttachments.itemID NOT IN (SELECT itemID FROM deletedItems)
            """
        ).fetchall()

        note_rows = conn.execute(
            """
            SELECT itemNotes.itemID AS id,
                   itemNotes.parentItemID AS parent_id,
                   itemNotes.note AS note,
                   items.key AS key,
                   items.dateModified AS dt
            FROM itemNotes
            JOIN items ON items.itemID = itemNotes.itemID
            WHERE itemNotes.itemID NOT IN (SELECT itemID FROM deletedItems)
            """
        ).fetchall()

        items_by_id: dict[int, ZoteroItem] = {}
        item_types: dict[int, str] = {}
        for r in items_rows:
            item_types[r["id"]] = r["type"]
            # Only build "parent" ZoteroItems for non-note, non-attachment types.
            # Standalone notes/attachments at top level get their own entries below.
            if r["type"] not in ("note", "attachment"):
                items_by_id[r["id"]] = ZoteroItem(
                    item_key=r["key"],
                    item_type=r["type"],
                    title=titles.get(r["id"]),
                    tags=tags_by_item.get(r["id"], []),
                )

        id_to_key = {r["id"]: r["key"] for r in items_rows}

        for r in att_rows:
            resolved, is_stored, fname = _resolve_storage_path(
                storage_root, r["key"], r["path"] or ""
            )
            att = ZoteroAttachment(
                item_key=r["key"],
                parent_key=id_to_key.get(r["parent_id"]) if r["parent_id"] else None,
                filename=fname,
                storage_path=resolved,
                is_stored=is_stored,
                content_type=r["content_type"],
            )
            owner_id = r["parent_id"] if r["parent_id"] in items_by_id else r["id"]
            if owner_id in items_by_id:
                items_by_id[owner_id].attachments.append(att)

        for r in note_rows:
            child_id = r["id"]
            body_plain = _strip_html(r["note"])
            note = ZoteroChildNote(
                note_key=r["key"],
                parent_item_id=r["parent_id"],
                tags=tags_by_item.get(child_id, []),
                body_plain=body_plain,
                body_length=len(body_plain),
                date_modified=_parse_dt(r["dt"]),
            )
            owner_id = r["parent_id"] if r["parent_id"] in items_by_id else None
            if owner_id is not None:
                items_by_id[owner_id].child_notes.append(note)
            # Standalone notes (no parent) are intentionally dropped — they
            # don't fit the "child note on a bibliographic item" model.

        return list(items_by_id.values())
