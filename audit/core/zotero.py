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
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup


def _strip_html(s: str | None) -> str:
    if not s:
        return ""
    return BeautifulSoup(s, "html.parser").get_text()


def _parse_dt(s: str | None) -> _dt.datetime | None:
    if not s:
        return None
    try:
        return _dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


@dataclass
class ZoteroAttachment:
    item_key: str
    parent_key: str | None
    filename: str
    storage_path: Path | None
    is_stored: bool
    content_type: str | None


@dataclass
class ZoteroChildNote:
    note_key: str
    parent_item_id: int | None
    tags: list[str] = field(default_factory=list)
    body_plain: str = ""
    body_length: int = 0
    date_modified: _dt.datetime | None = None


@dataclass
class ZoteroItem:
    item_key: str
    item_type: str
    title: str | None
    tags: list[str] = field(default_factory=list)  # parent-item tags
    attachments: list[ZoteroAttachment] = field(default_factory=list)
    child_notes: list[ZoteroChildNote] = field(default_factory=list)


@contextmanager
def _open_snapshot(sqlite_path: Path) -> Iterator[sqlite3.Connection]:
    if not sqlite_path.exists():
        raise FileNotFoundError(f"Zotero DB not found at {sqlite_path}")

    uri = f"file:{sqlite_path}?mode=ro"
    source_conn = sqlite3.connect(uri, uri=True)
    try:
        mem_conn = sqlite3.connect(":memory:")
        source_conn.backup(mem_conn)
        mem_conn.row_factory = sqlite3.Row
        try:
            yield mem_conn
        finally:
            mem_conn.close()
    finally:
        source_conn.close()


def _resolve_storage_path(
    storage_root: Path, key: str, raw_path: str
) -> tuple[Path | None, bool, str]:
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
