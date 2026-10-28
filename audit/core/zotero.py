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
import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup


#: Tags whose content is a block: the plain-text rendering must put a line
#: break after each, or two adjacent blocks fuse into one word.
_BLOCK_TAGS = (
    "p", "div", "li", "ul", "ol", "tr", "td", "th", "table", "pre",
    "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
)

_BLANK_RUN_RE = re.compile(r"\n{3,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.MULTILINE)


def _strip_html(s: str | None) -> str:
    """Render Zotero's HTML note body down to plain text, block-aware.

    Zotero stores child-note bodies as HTML. A bare ``get_text()`` concatenates
    across ``<br>`` (a void element contributing no text) and across adjacent
    inline elements, which FUSES the words on either side::

        html : ...reliability of appendicitis<br>Lebrun : we treat AI models...
        bare : ...reliability of appendicitisLebrun : we treat AI models...

    That is not cosmetic. Two consumers depend on this text being real words:

    * :func:`notesync.render.render_note` writes it into the vault, so every
      created reference note carried the fused spellings.
    * :mod:`notesync.compare` measures word containment against the Obsidian
      note, and a fused word is missing from BOTH sides' vocabularies — it
      counts as drift that no edit can ever close. Measured on the live
      library (2026-08-28) the artifact inflated the "Zotero text is missing
      from the note" set from 9 notes to 91, a 10x over-report, and the
      ``notesync.compare`` docstring had already recorded one instance
      ("depressionutile") as an unexplained tokenisation artifact.

    So ``<br>`` becomes a newline and every block-level element is terminated
    by one, before the text is extracted. Runs of blank lines are collapsed to
    at most one and trailing spaces are dropped, so the result is stable enough
    to write into a note verbatim.
    """
    if not s:
        return ""
    soup = BeautifulSoup(s, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for el in soup.find_all(_BLOCK_TAGS):
        # Append rather than insert a separator: ``get_text()`` walks the tree
        # in document order, so a trailing newline inside the block lands
        # between this block's text and the next sibling's.
        el.append("\n")
    text = soup.get_text()
    text = _TRAILING_WS_RE.sub("", text)
    return _BLANK_RUN_RE.sub("\n\n", text)


def linked_path_key(raw: str) -> str:
    """One comparison form for a filesystem path: realpath, NFC, casefolded.

    Zotero stores a linked file's absolute path as typed when it was linked,
    macOS returns NFD from a directory walk and APFS is case-insensitive, so a
    byte comparison would miss the same file spelled two ways. The scan's
    ``zotero_link_index`` and the renamer's ``find_linked_attachments`` share
    this key (2026-09-19 review A1): matching on bytes at rename time while
    the scan folded left Zotero pointing at a missing file.
    """
    return unicodedata.normalize("NFC", os.path.realpath(raw)).casefold()


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


#: ``itemAttachments.linkMode`` integers, as Zotero writes them. The names are
#: the Web API's own vocabulary (verified live 2026-08-28: an attachment stored
#: with ``linkMode`` 4 comes back from api.zotero.org as ``"embedded_image"``),
#: so a row read here can be compared with an API response without a second
#: mapping. The distinction that MATTERS is which side owns the bytes:
#: ``imported_*`` files live under ``~/Zotero/storage/<key>/`` and go wherever
#: the item goes; ``linked_*`` only reference a path Zotero never owned.
LINK_MODES = {
    0: "imported_file",
    1: "imported_url",
    2: "linked_file",
    3: "linked_url",
    4: "embedded_image",
}

#: Link modes whose FILE is stored inside Zotero's own data directory, and is
#: therefore destroyed when the attachment item is permanently deleted. Any
#: de-link path must treat these differently from a link Zotero merely points
#: with — see ``api/routes/zotlink.py``.
ZOTERO_OWNED_MODES = frozenset({"imported_file", "imported_url", "embedded_image"})


@dataclass
class ZoteroAttachment:
    """One Zotero attachment row resolved against the storage tree.

    ``storage_path`` is populated only for ``storage:``-scheme attachments
    (Zotero-managed files under ``storage/<key>/``); linked/relative
    attachments leave it ``None`` (``is_stored=False``) because the audit
    resolves PDFs from the vault's ``biblio_articles`` tree, not from Zotero's
    own storage.

    ``item_key`` is the attachment's OWN Zotero key, which is what the Web API
    addresses for a de-link (``PATCH /users/<id>/items/<item_key>``).
    ``link_mode`` and ``annotation_count`` were added 2026-08-28 for that path:
    the first says whether Zotero owns the bytes, the second says how much
    highlighting rides along, because trashing a PDF attachment trashes its
    annotations with it and a count is the only honest way to disclose that.
    ``raw_path`` is the undecoded ``itemAttachments.path`` cell, kept so a
    linked attachment can be shown as the location the user recognises.
    """
    item_key: str
    parent_key: str | None
    filename: str
    storage_path: Path | None
    is_stored: bool
    content_type: str | None
    link_mode: str | None = None
    annotation_count: int = 0
    raw_path: str | None = None
    #: The attachment's OWN display title (2026-09-18, annual cleanup T4:
    #: decision D3 wants it equal to the filename). ``filename`` above is the
    #: basename of the PATH, not this.
    title: str | None = None

    @property
    def file_owned_by_zotero(self) -> bool:
        """True when the bytes live in Zotero's data directory.

        Purely derived, so a caller cannot get the rule wrong in one place: the
        de-link route, the report and the confirm text all read this.
        """
        return (self.link_mode or "") in ZOTERO_OWNED_MODES


@dataclass
class ZoteroChildNote:
    """A reading note attached to a parent bibliographic item.

    The user's workflow attaches a child note when they actually read a paper,
    so a parent item *having* a child note is the audit's "read" signal (see
    ``reports/zotero_unread``). ``tags`` are the note's own Zotero tags, which
    ``reports/note_tag_drift`` reconciles against the Obsidian note's YAML tags.
    ``body_length`` is the plain-text length, kept as a cheap engagement proxy.

    ``body_html`` (the stored HTML, verbatim — Zotero's ``<div class="zotero-note
    znv1">`` shell included) and ``version`` (the note ITEM's own ``items.version``,
    the value the web API's ``If-Unmodified-Since-Version`` guard compares
    against) were added 2026-09-04 for the note-sync path. Both are additive:
    every pre-existing consumer reads ``body_plain`` exactly as before. The
    Markdown rendering deliberately does NOT live here — the vendored ``audit/``
    tree depends on nothing but ``audit/config.py`` and the converter is a
    third-party package — so ``notesync.convert`` derives it from ``body_html``.
    """

    note_key: str
    parent_item_id: int | None
    tags: list[str] = field(default_factory=list)
    body_plain: str = ""
    body_length: int = 0
    date_modified: _dt.datetime | None = None
    body_html: str = ""
    version: int = 0


@dataclass
class ZoteroItem:
    """A top-level (parent) Zotero item: the bibliographic record.

    Only non-note, non-attachment types become ``ZoteroItem``s. ``tags`` are
    the parent's own tags — equal to the ``keywords`` field of the matching
    ``_master.bib`` entry once Better BibTeX has exported them, so the
    inventory can compare tag sets once the two stores are joined.
    ``child_notes`` are the attached reading notes.

    The join itself is on ``citation_key`` (see the field's note). A record
    whose key matches no item falls back to normalized title, but only when
    that title names exactly ONE item and that item was not already claimed by
    the key join — see ``inventory._resolve_title_fallback``. (Until 2026-08-12
    the fallback matched against every item including keyed ones, so a stale
    bib key could re-claim a paper that already belonged to another record.)
    """

    item_key: str
    item_type: str
    title: str | None
    # Zotero's ``abstractNote`` field (2026-08-11). Read-only and additive: the
    # audit never uses it, but for an entry with no local PDF it is the only
    # substantive text that exists anywhere — ``_master.bib`` has no abstract
    # (see ``BibEntry``) and BBT does not export one by default — so it is what
    # the tagging subsystem classifies from when there is no PDF to extract.
    abstract: str | None = None
    # Better BibTeX's citation key, as Zotero stores it (2026-08-12). This is
    # the AUTHORITATIVE link to ``_master.bib``: BBT derives the ``.bib`` entry
    # key from this very field, so joining on it is exact where joining on
    # title is a heuristic. Measured on the maintainer's library: all 6194
    # items carry one and all 6122 bib keys match, versus a title join that
    # mis-joined 51 records (see ``inventory._index_zotero_by_citation_key``).
    citation_key: str | None = None
    tags: list[str] = field(default_factory=list)  # parent-item tags
    attachments: list[ZoteroAttachment] = field(default_factory=list)
    child_notes: list[ZoteroChildNote] = field(default_factory=list)
    # Local-API routing (Tagging write path): which library the item lives in.
    # ``library_type`` is ``"user"`` or ``"group"``; ``group_id`` is set only for
    # group libraries. Read-only metadata — the audit itself never writes Zotero.
    library_id: int | None = None
    library_type: str | None = None
    group_id: int | None = None
    # Display fields for the reference-note renderer (Batch E, 2026-08-26).
    # `container` prefers the journal abbreviation over the full publication
    # title — the maintainer's note template renders `journalAbbreviation`, and
    # falling back keeps a note readable for an item type that has no
    # abbreviation. `date` is Zotero's raw string (often "2024-03" or a full
    # ISO date); the renderer takes the year from it. `date_added` is the only
    # real recency signal anywhere in the system — the bib has no such field.
    container: str | None = None
    date: str | None = None
    doi: str | None = None
    date_added: str | None = None
    server_id: str | None = None  # Local API identity from the same SQLite snapshot


# Ceiling for the copy-based snapshot (sqlite + -wal + -shm combined). A
# Zotero DB is typically tens of MB; past this we fall back to the direct
# mode=ro backup rather than duplicating gigabytes on every scan.
_SNAPSHOT_COPY_MAX_BYTES = 2 * 1024**3


@contextmanager
def _open_snapshot(sqlite_path: Path, *, allow_live_fallback: bool = True) -> Iterator[sqlite3.Connection]:
    """Yield an in-memory read-only snapshot of the Zotero database.

    Hardened technique (improvement plan 0.7 — adopted from the
    ``zotero-debug`` CLI helper, which never exhibited the hot-WAL issues the
    direct path did): copy ``zotero.sqlite`` plus any ``-wal``/``-shm``
    siblings to a temp dir, open the COPY with ``mode=ro``, and
    ``backup()`` that into a throwaway ``:memory:`` connection all queries
    run against. On this copied path the live database is touched only by
    plain file reads. The copy is deleted before the snapshot is used.

    Fallback: an oversized or changing/failed copy uses a live ``mode=ro``
    connection and SQLite backup. This can take read locks and interact with
    source SHM; it does not have the copied path's isolation guarantees.
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
                    def source_signature():
                        stamps = []
                        for candidate in [sqlite_path, *siblings]:
                            try:
                                st = candidate.stat()
                                stamps.append((st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns))
                            except FileNotFoundError:
                                stamps.append(None)
                        return stamps
                    before_copy = source_signature()
                    shutil.copy2(sqlite_path, tmp)
                    for suffix in ("-wal", "-shm"):
                        sib = sqlite_path.with_name(sqlite_path.name + suffix)
                        if sib.exists():
                            shutil.copy2(sib, tmp.with_name(tmp.name + suffix))
                    if source_signature() != before_copy:
                        raise sqlite3.DatabaseError("Zotero changed during snapshot copy")
                    # immutable=1 ignores the WAL, silently losing committed
                    # changes. Any SQLite locks/SHM work here affect only tmp.
                    source_conn = sqlite3.connect(
                        tmp.as_uri() + "?mode=ro", uri=True,
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
            if not allow_live_fallback:
                raise sqlite3.DatabaseError("Could not obtain a stable copied snapshot; live fallback disabled.")
            source_conn = sqlite3.connect(sqlite_path.resolve().as_uri() + "?mode=ro", uri=True)
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
        # Older databases can still be audited, but cannot authorize a local
        # attachment until a fresh scan contains Desktop's persistent identity.
        server_id = None
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
            row = conn.execute("SELECT value FROM settings WHERE setting='localAPI' AND key='serverID'").fetchone()
            if row and isinstance(row[0], str) and row[0]:
                server_id = row[0]
        items_rows = conn.execute(
            """
            SELECT items.itemID AS id, items.key AS key, itemTypes.typeName AS type,
                   items.dateModified AS dt, items.dateAdded AS da,
                   items.libraryID AS library_id
            FROM items
            JOIN itemTypes ON itemTypes.itemTypeID = items.itemTypeID
            WHERE items.itemID NOT IN (SELECT itemID FROM deletedItems)
            """
        ).fetchall()

        # Library routing for the Tagging local-API writer: map each libraryID to
        # its type ('user'/'group') and, for groups, the groupID the local API
        # addresses (``groups/<groupID>``). Best-effort — a missing table (older
        # schema) just leaves the routing fields None and the writer treats the
        # item as the personal library.
        lib_index: dict[int, dict] = {}
        try:
            for r in conn.execute("SELECT libraryID, type FROM libraries").fetchall():
                lib_index[r["libraryID"]] = {"type": r["type"], "group_id": None}
            for r in conn.execute("SELECT libraryID, groupID FROM groups").fetchall():
                entry = lib_index.setdefault(r["libraryID"], {"type": "group", "group_id": None})
                entry["group_id"] = r["groupID"]
        except sqlite3.Error:
            lib_index = {}

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

        # Abstracts (2026-08-11). Same shape as the title query — one extra
        # pass over itemData rather than a second trip per item. Read-only and
        # additive: the audit itself never uses it, but it is the ONLY
        # substantive text a PDF-less entry has, and the tagging subsystem's
        # metadata-mode propose classifies from it when there is no PDF to
        # extract. `_master.bib` cannot supply it — BibEntry has no abstract
        # field, and BBT does not export one by default.
        abstract_rows = conn.execute(
            """
            SELECT itemData.itemID AS id, itemDataValues.value AS value
            FROM itemData
            JOIN fields ON fields.fieldID = itemData.fieldID
            JOIN itemDataValues ON itemDataValues.valueID = itemData.valueID
            WHERE fields.fieldName = 'abstractNote'
            """
        ).fetchall()
        abstracts = {r["id"]: r["value"] for r in abstract_rows}

        # Better BibTeX citation keys (2026-08-12). Same one-extra-pass shape as
        # titles and abstracts. This is what the inventory joins on: BBT derives
        # the `_master.bib` entry key from this field, making the link exact
        # rather than the title heuristic it replaced.
        citekey_rows = conn.execute(
            """
            SELECT itemData.itemID AS id, itemDataValues.value AS value
            FROM itemData
            JOIN fields ON fields.fieldID = itemData.fieldID
            JOIN itemDataValues ON itemDataValues.valueID = itemData.valueID
            WHERE fields.fieldName = 'citationKey'
            """
        ).fetchall()
        citation_keys = {r["id"]: r["value"] for r in citekey_rows}

        # Display fields for the reference-note renderer (Batch E, 2026-08-26).
        # ONE extra pass over itemData for all of them, the same shape as the
        # three above rather than a query each: the note template needs the
        # journal abbreviation (falling back to the full publication title),
        # the date, and the DOI, none of which BibEntry carries in a usable
        # form. Read-only and additive; every consumer before this ignores them.
        display_rows = conn.execute(
            """
            SELECT itemData.itemID AS id, fields.fieldName AS name,
                   itemDataValues.value AS value
            FROM itemData
            JOIN fields ON fields.fieldID = itemData.fieldID
            JOIN itemDataValues ON itemDataValues.valueID = itemData.valueID
            WHERE fields.fieldName IN
                  ('journalAbbreviation', 'publicationTitle', 'date', 'DOI')
            """
        ).fetchall()
        display: dict[int, dict[str, str]] = defaultdict(dict)
        for r in display_rows:
            display[r["id"]][r["name"]] = r["value"]

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
                   itemAttachments.linkMode AS link_mode,
                   items.key AS key
            FROM itemAttachments
            JOIN items ON items.itemID = itemAttachments.itemID
            WHERE itemAttachments.itemID NOT IN (SELECT itemID FROM deletedItems)
            """
        ).fetchall()

        # Annotations hang off the ATTACHMENT, not the parent item, and they go
        # to the trash with it. Counted here (one grouped query, not one per
        # attachment) so the de-link surface can disclose the cost instead of
        # discovering it afterwards. Guarded: ``itemAnnotations`` arrived in
        # Zotero 6, and a de-link must not fail against an older database — a
        # missing table simply means zero annotations.
        annotations_by_attachment: dict[int, int] = {}
        try:
            annotations_by_attachment = {
                r["pid"]: r["n"]
                for r in conn.execute(
                    """
                    SELECT parentItemID AS pid, COUNT(*) AS n
                    FROM itemAnnotations
                    WHERE itemID NOT IN (SELECT itemID FROM deletedItems)
                    GROUP BY parentItemID
                    """
                ).fetchall()
            }
        except sqlite3.Error:
            annotations_by_attachment = {}

        note_rows = conn.execute(
            """
            SELECT itemNotes.itemID AS id,
                   itemNotes.parentItemID AS parent_id,
                   itemNotes.note AS note,
                   items.key AS key,
                   items.dateModified AS dt,
                   items.version AS version
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
                lib_id = r["library_id"]
                lib = lib_index.get(lib_id, {})
                items_by_id[r["id"]] = ZoteroItem(
                    item_key=r["key"], server_id=server_id,
                    item_type=r["type"],
                    title=titles.get(r["id"]),
                    abstract=abstracts.get(r["id"]),
                    citation_key=citation_keys.get(r["id"]),
                    tags=tags_by_item.get(r["id"], []),
                    library_id=lib_id,
                    library_type=lib.get("type"),
                    group_id=lib.get("group_id"),
                    container=(display[r["id"]].get("journalAbbreviation")
                               or display[r["id"]].get("publicationTitle")),
                    date=display[r["id"]].get("date"),
                    doi=display[r["id"]].get("DOI"),
                    date_added=r["da"],
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
                link_mode=LINK_MODES.get(r["link_mode"]),
                annotation_count=annotations_by_attachment.get(r["id"], 0),
                raw_path=r["path"] or None,
                title=titles.get(r["id"]),
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
                body_html=r["note"] or "",
                version=int(r["version"] or 0),
            )
            owner_id = r["parent_id"] if r["parent_id"] in items_by_id else None
            if owner_id is not None:
                items_by_id[owner_id].child_notes.append(note)
            # Standalone notes (no parent) are intentionally dropped — they
            # don't fit the "child note on a bibliographic item" model.

        return list(items_by_id.values())
