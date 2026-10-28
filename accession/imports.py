"""What counts as a NEW import, and what the library-maintenance run has done with it.

Library-workflow plan, lot 2 (2026-09-21). The maintainer adds papers with
Zotero's Chrome connector; Attanger then moves the file into
``biblio_articles/``. A later "update my library" run must know which of those
arrivals it has not handled yet — and "new" is NOT "untagged": an import the
connector already tagged is still new, and a paper the maintainer deliberately
left untagged in 2024 is not.

Identity (no parallel registry — every id below is one the app already uses)
-------------------------------------------------------------------------
* A Zotero ENTRY, ATTACHMENT or NOTE is ``<kind>:<library>:<key>`` — the scoped
  item key :func:`accession.reconciliation.observe` already keys its snapshot
  by. It survives a file rename (Zotero moves the linked path, not the key),
  a Better BibTeX citation-key change (the citation key is a FIELD, displayed
  here, never part of an id) and Attanger converting an imported copy into a
  linked file (same attachment key, new link mode and path).
* A vault PDF that no Zotero attachment links is ``doc:<ino>:<birth>`` — the
  path-independent half of :func:`accession.review.fingerprint`, deliberately
  WITHOUT ``dev``, which macOS renumbers at boot
  (:data:`accession.review.VOLATILE_IDENTITY_KEYS`). A same-volume rename keeps
  inode and birth time; a copy does not, and is correctly a different file.
  No content hash: hashing every biblio PDF on each run is the cost this
  identity exists to avoid, and a hash would change on every annotation save.

Two identities for one file are reconciled rather than double-counted: when a
tracked orphan ``doc:`` is later linked by an attachment, or an attachment is
re-created under a new key for the same file, the new unit ADOPTS the old one's
step results and the old one is marked ``absorbed``.

State machine
-------------
* The FIRST pass for a library (vault + Zotero database, the reconciliation
  ``scope``) records every current unit as ``baseline``: nothing historical is
  ever proposed, so no model is ever sent the backlog by accident.
* A later pass registers every unit it has not seen, with the category it had
  WHEN FIRST SEEN (new entry with PDF, PDF added to a known entry, ...). That
  category is stored and never recomputed, so a unit is never "reclassified".
* Each step (``rename``, ``tag``, ``mirror``, ``associate``) records its own
  outcome per unit. A unit stays open while one of ITS steps is not terminal;
  a failed ``tag`` does not reopen a ``rename`` that succeeded, and a unit whose
  steps are all done is never listed again — except a note whose Zotero text
  changed since it was last mirrored, which reopens exactly ``mirror``.
* A unit that disappears (trashed, deleted, file gone) is marked ``gone`` once
  and reported; it keeps its history, and comes back unchanged if it returns.

Sessions: one id per run (``uuid4``), any number per day. Every step outcome
names the session that produced it, and the event log keeps them all.

Concurrency and freshness
-------------------------
The observation (a complete Zotero snapshot through the WAL-aware copy, plus
one ``stat`` per biblio PDF) is taken WITHOUT a lock; persistence then runs in
one ``BEGIN IMMEDIATE`` transaction on the reconciliation store and re-reads
the tracked units inside it. Registration is insert-if-absent, so two passes
committing the SAME observation register each unit once. An observation OLDER
than the newest one already committed is refused (:class:`StaleObservation`):
otherwise the pass that observed first but committed last would report the
other's fresher imports as ``gone`` and roll renames back. ``record_step``
takes the same lock, so a step recorded between an observation and its commit
is re-read, never overwritten. An incomplete Zotero read raises (``observe``
never turns a failed query into absence), so a partial read can never mark
units ``gone``.

Known limits (also in CLAUDE.md → Known Issues): a file Attanger moves AND
renames is one transient orphan for a pass (reported in ``settling``) until
Zotero records the new path; two live attachments linking the same file are
two units; a filesystem without birth times makes ``doc:`` ids inode-only,
which are then never used to adopt.
"""
from __future__ import annotations

import json
import os
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from accession import reconciliation as rec
from accession import review

#: Steps a unit can go through, by kind. A step not listed for a kind is
#: refused by :func:`record_step` — recording "tagged" on a note would close a
#: step the orchestrator never ran.
KIND_STEPS: dict[str, tuple[str, ...]] = {
    # A PDF attachment: the renamer names the file; tagging writes the Finder
    # tags of that file and the Zotero tags of its PARENT entry.
    'attachment': ('rename', 'tag'),
    # An entry is tracked so the classification knows whether an attachment's
    # parent is new; its own work is tagging only when it has no PDF (with a
    # PDF, the attachment's tag step covers it — see `entry_steps`).
    'entry': ('tag',),
    'note': ('mirror',),
    # A vault PDF no attachment links: the only step is to associate it.
    'document': ('associate',),
}

#: ``done`` and ``skipped`` close a step. ``uncertain`` does NOT: a network
#: write whose outcome is unknown must be inspected, never retried blindly, so
#: it stays open but is reported apart from ``failed``.
TERMINAL = frozenset({'done', 'skipped'})
STATUSES = frozenset({'done', 'skipped', 'failed', 'uncertain'})

#: Categories a unit may have at first sight. Stored once, never recomputed.
CATEGORIES = ('new_entry_with_pdf', 'pdf_added_to_known_entry', 'new_entry_without_pdf',
              'new_note', 'pdf_not_in_zotero')

_NON_ENTRY_TYPES = frozenset({'attachment', 'note', 'annotation'})


class ImportTrackingError(ValueError):
    """A request the tracker refuses (unknown session, unit or step)."""


# ---------------------------------------------------------------------------
# Observation (pure over a reconciliation snapshot + a filesystem walk)
# ---------------------------------------------------------------------------

def _doc_id(fp: dict) -> Optional[str]:
    if not fp or fp.get('ino') is None:
        return None
    return f"doc:{fp['ino']}:{fp.get('birth')}"


def _is_pdf_attachment(item: dict) -> bool:
    ctype = (item.get('content_type') or '').lower()
    return ctype == 'application/pdf' or (item.get('path') or '').lower().endswith('.pdf')


def _resolved(path: Path) -> Optional[Path]:
    """``path.resolve()``, or None when it cannot be resolved."""
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return None


def _rel_to(real: Optional[Path], root_real: Optional[Path]) -> Optional[str]:
    """``real`` relative to ``root_real`` (both already resolved), or None."""
    if real is None or root_real is None:
        return None
    try:
        return real.relative_to(root_real).as_posix()
    except ValueError:
        return None


def _rel(path: Path, vault: Path) -> Optional[str]:
    try:
        return path.resolve().relative_to(vault.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def build_observation(settings, snapshot: dict, *,
                      walk: Optional[Callable[[Path], Iterable[Path]]] = None,
                      fingerprint: Callable[[Path], dict] = review.fingerprint) -> dict:
    """``{unit_id: unit}`` for everything a run could have to handle.

    ``snapshot`` is :func:`reconciliation.observe` with ``include_note_digests``.
    Trashed items are left out (a trashed paper is not an import to process),
    which is what lets a trash be reported as ``gone``.
    """
    items = snapshot['items']
    vault = settings.vault_root
    # Resolved ONCE per observation, and each attachment path once below
    # (audit remediation 2026-09-23, P3): `_rel` and `_under` each resolved
    # both sides for every attachment — four `realpath` walks per PDF, over
    # thousands of PDFs, on every observation. Same answers: a path that
    # cannot be resolved still yields no rel and "not in the biblio folder".
    vault_r = _resolved(vault)
    biblio_r = _resolved(settings.biblio_articles_dir)
    by_key = {(i['library'], i['key']): i for i in items.values()}
    units: dict[str, dict] = {}
    linked_docs: set[str] = set()
    unresolved_names: set[str] = set()

    def alive(item):
        # A child of a trashed parent is in the trash too: Zotero flags only
        # the parent (the storage check learnt this 2026-09-18).
        if item.get('deleted'):
            return False
        parent = by_key.get((item['library'], item.get('parent'))) if item.get('parent') else None
        return not (parent and parent.get('deleted'))

    for item in items.values():
        if not alive(item):
            continue
        lib, key, kind = item['library'], item['key'], item['item_type']
        if kind not in _NON_ENTRY_TYPES:
            units[f'entry:{lib}:{key}'] = dict(
                kind='entry', library=lib, key=key,
                citation_key=item['fields'].get('citationKey'),
                title=item['fields'].get('title'))
        elif kind == 'note':
            parent = by_key.get((lib, item.get('parent')))
            # Only a reading note under an entry is mirrored; standalone notes
            # and notes under attachments are outside the mirror's contract.
            if parent and parent['item_type'] not in _NON_ENTRY_TYPES and item.get('note_sha256'):
                units[f'note:{lib}:{key}'] = dict(
                    kind='note', library=lib, key=key, parent=f'entry:{lib}:{parent["key"]}',
                    note_sha256=item['note_sha256'])
        elif kind == 'attachment' and _is_pdf_attachment(item):
            parent = by_key.get((lib, item.get('parent'))) if item.get('parent') else None
            try:
                path = rec.attachment_path(item, settings, snapshot)
            except (OSError, ValueError):
                path = None
            fp, rel, real = None, None, None
            if path is not None:
                try:
                    fp = fingerprint(path)
                    real = _resolved(path)
                    rel = _rel_to(real, vault_r)
                except OSError:
                    fp = None
            doc = _doc_id(fp) if fp else None
            if doc:
                linked_docs.add(doc)
            elif item.get('path'):
                # The link names a file this pass could not stat: a relative
                # `attachments:` path (the base directory is deliberately
                # unset, so it resolves to nothing), or a `storage:` copy
                # Attanger is moving right now. Remember the NAME, so the walk
                # below does not also register that same file as an orphan —
                # one file counted twice is exactly the double processing
                # this module exists to prevent. Hiding a real orphan that
                # merely shares a basename costs one pass at most: it is
                # registered as soon as the link resolves or disappears.
                unresolved_names.add(_fold(Path(str(item['path']).split(':', 1)[-1]).name))
            if not parent or parent['item_type'] in _NON_ENTRY_TYPES:
                # A standalone PDF or a note image. Zotero holds it, so it is
                # not an orphan (hence `linked_docs` above), but it is not a
                # paper to name or tag either: no unit.
                continue
            in_biblio = bool(path is not None and fp is not None
                             and _rel_to(real, biblio_r) is not None)
            unit = dict(
                kind='attachment', library=lib, key=key, parent=f'entry:{lib}:{parent["key"]}',
                link_mode=item.get('link_mode'), rel=rel, doc=doc, file_present=fp is not None,
                in_biblio=in_biblio)
            if fp is None and str(item.get('link_mode') or '').startswith('imported'):
                # Facts for the leftover classification (CLI proof run
                # 2026-09-23, finding 4); never persisted (_TRANSIENT_FIELDS).
                unit['stored_path'] = item.get('path')
                unit['annotations'] = (item.get('child_counts') or {}).get('annotations')
            units[f'attachment:{lib}:{key}'] = unit

    root = settings.biblio_articles_dir
    for path in (walk or _walk_pdfs)(root):
        try:
            fp = fingerprint(path)
        except OSError:
            continue  # vanished between the listing and the stat
        doc = _doc_id(fp)
        if doc and doc not in linked_docs and _fold(path.name) not in unresolved_names:
            units[doc] = dict(kind='document', rel=_rel_to(_resolved(path), vault_r), doc=doc)
    return units


#: Observation-only facts that describe THIS pass, never the unit's history:
#: stripped before a unit is persisted.
_TRANSIENT_FIELDS = ('stored_path', 'annotations')


def classify_leftovers(observation: dict[str, dict]) -> tuple[list[dict], list[str]]:
    """Split imported attachments whose file is absent into ``(leftovers, settling)``.

    CLI proof run 2026-09-23, finding 4 (decision D-c). An ``imported*``
    attachment whose bytes are not where Zotero says was always reported as
    "still being moved by Attanger". When its parent ALSO holds a live
    ``linked_file`` copy in the biblio folder, nothing is moving: the move
    finished and the imported record was left behind. Those are LEFTOVERS —
    offered for review and a journalled trash — and only the rest keep the
    ``settling`` meaning. A linked sibling outside the biblio folder, or one
    whose file is missing, does not qualify.
    """
    linked: dict[str, list[dict]] = {}
    for u in observation.values():
        if (u.get('kind') == 'attachment' and u.get('link_mode') == 'linked_file'
                and u.get('in_biblio') and u.get('file_present') and u.get('parent')):
            linked.setdefault(u['parent'], []).append(u)
    leftovers: list[dict] = []
    settling: list[str] = []
    for uid in sorted(observation):
        u = observation[uid]
        if (u.get('kind') != 'attachment' or u.get('file_present')
                or not str(u.get('link_mode') or '').startswith('imported')):
            continue
        siblings = sorted(linked.get(u.get('parent') or '', []), key=lambda x: x.get('rel') or '')
        if not siblings:
            settling.append(uid)
            continue
        parent = observation.get(u['parent']) or {}
        leftovers.append({'unit': uid, 'item_key': u.get('key'), 'parent': u['parent'],
                          'citation_key': parent.get('citation_key'),
                          'stored_path': u.get('stored_path'),
                          'linked_copy_rel': siblings[0].get('rel'),
                          'annotations': u.get('annotations')})
    return leftovers, settling


def _fold(name: str) -> str:
    """Filename comparison form: NFC + casefold (APFS is case-insensitive
    and may hand back either Unicode normalisation)."""
    return unicodedata.normalize('NFC', name).casefold()


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _walk_pdfs(root: Path) -> list[Path]:
    """Every PDF under the biblio folder, hidden folders skipped (``os.walk``
    rather than ``rglob`` so an unreadable subfolder is skipped, not fatal)."""
    if not root.is_dir():
        return []
    out: list[Path] = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        out.extend(Path(dirpath) / f for f in files
                   if f.lower().endswith('.pdf') and not f.startswith('.'))
    return out


class Observation(dict):
    """``{unit_id: unit}`` plus WHEN it was taken (``observed_at``).

    The timestamp is taken BEFORE the Zotero read, so it is a lower bound of
    what the observation can contain. :func:`record_pass` refuses one older
    than the newest it has already committed: two passes race freely until
    persistence, and the one that observed first must not commit last and
    report the other's fresher units as ``gone`` (review 2026-09-21).
    """
    observed_at: float = 0.0


def observe(settings) -> Observation:
    """A fresh, complete observation. Raises when Zotero cannot be read whole."""
    started = time.time()
    snapshot = rec.observe(settings, include_note_digests=True, include_child_counts=True)
    out = Observation(build_observation(settings, snapshot))
    out.observed_at = started
    return out


class StaleObservation(ImportTrackingError):
    """A newer observation has already been committed; observe again."""


# ---------------------------------------------------------------------------
# Durable state (tables in the reconciliation store, keyed by its scope)
# ---------------------------------------------------------------------------

#: Store files whose schema this process has already brought up to date
#: (audit remediation 2026-09-23, P8). `_schema` ran five CREATE statements,
#: a PRAGMA and the legacy-session scan on EVERY write. Keyed on the file's
#: identity — path, inode, birth time — not its path alone: a store deleted
#: and recreated (a reset, a test's fresh store) is a different file and gets
#: its DDL again. The DDL itself is idempotent, so a stale key costs nothing
#: but a skipped no-op; what it must never do is skip a NEW file.
_SCHEMA_DONE: set = set()
_SCHEMA_DONE_LOCK = threading.Lock()


def _store_identity(con) -> Optional[tuple]:
    try:
        row = con.execute('PRAGMA database_list').fetchone()
        path = row[2] if row else ''
        if not path:
            return None
        st = os.stat(path)
        return (os.path.realpath(path), st.st_ino, getattr(st, 'st_birthtime', st.st_ctime))
    except (OSError, IndexError, TypeError):
        return None


_SCHEMA_TABLES = ('import_units', 'import_sessions', 'import_scopes', 'import_proposals',
                  'import_events', 'import_session_results')
_SCHEMA_INDEXES = ('import_events_by_scope_seq', 'import_events_by_scope_session_seq',
                   'import_session_results_by_session')


def _schema_is_current(con) -> bool:
    """Whether the COMMITTED schema is complete (tables, session columns, no
    legacy session rows left). Asked at the start of a writer's transaction,
    before it has written anything, so it sees only committed state."""
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not set(_SCHEMA_TABLES) <= names:
        return False
    indexes = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    if not set(_SCHEMA_INDEXES) <= indexes:
        return False
    if not {c for c, _decl in _SESSION_COLUMNS} <= _session_columns(con):
        return False
    return not con.execute(
        'SELECT 1 FROM import_sessions WHERE '
        'data LIKE ? OR data LIKE ? OR data LIKE ? OR data LIKE ? LIMIT 1',
        tuple(f'%"{k}":%' for k in _LEGACY_SESSION_KEYS)).fetchone()


def _schema(con):
    """Bring the store's schema up to date — at most once per file per process.

    The file is marked done only when its COMMITTED schema is already current
    (checked before this transaction writes anything), never right after the
    DDL runs: DDL is transactional in SQLite, and a writer that then fails
    rolls it back — marking at that point would skip the DDL on a store that
    does not have it.
    """
    key = _store_identity(con)
    if key is not None:
        with _SCHEMA_DONE_LOCK:
            if key in _SCHEMA_DONE:
                return
        if _schema_is_current(con):
            with _SCHEMA_DONE_LOCK:
                _SCHEMA_DONE.add(key)
            return
    _schema_ddl(con)


def _schema_ddl(con):
    con.execute('CREATE TABLE IF NOT EXISTS import_units('
                'scope TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, '
                'PRIMARY KEY(scope, id))')
    con.execute('CREATE TABLE IF NOT EXISTS import_sessions('
                'id TEXT PRIMARY KEY, scope TEXT NOT NULL, started_at REAL NOT NULL, '
                'data TEXT NOT NULL)')
    # Activation is recorded explicitly, not inferred from "no units yet": a
    # library that is empty at activation must not be re-baselined (and its
    # first real imports silently swallowed) on the next pass.
    con.execute('CREATE TABLE IF NOT EXISTS import_scopes('
                'scope TEXT PRIMARY KEY, activated_at REAL NOT NULL, session TEXT NOT NULL, '
                'last_observed_at REAL)')
    con.execute('CREATE TABLE IF NOT EXISTS import_proposals('
                'scope TEXT NOT NULL, key TEXT NOT NULL, at REAL NOT NULL, '
                'data TEXT NOT NULL, PRIMARY KEY(scope, key))')
    con.execute('CREATE TABLE IF NOT EXISTS import_events('
                'seq INTEGER PRIMARY KEY, scope TEXT NOT NULL, session TEXT, '
                'unit TEXT, at REAL NOT NULL, data TEXT NOT NULL)')
    con.execute('CREATE INDEX IF NOT EXISTS import_events_by_scope_seq '
                'ON import_events(scope, seq)')
    con.execute('CREATE INDEX IF NOT EXISTS import_events_by_scope_session_seq '
                'ON import_events(scope, session, seq)')
    _session_storage_schema(con)


# SESSION STORAGE (audit remediation 2026-09-23, P5).
#   Before: a session's model choice, its spend, its last analysis and its
#   last 20 apply results all lived in ONE JSON document. Every charge after
#   a proposal decoded and re-encoded that document (applies included), and
#   the route saved an apply by reading the list and writing it back — two
#   transactions, so two writers could lose an entry.
#   Now: the model and the spend are COLUMNS of `import_sessions`, so a charge
#   is one UPDATE of three integers/reals; analyses and applies are ROWS of
#   `import_session_results`, so recording one is one INSERT. The JSON keeps
#   only the small pass fields (label, last pass, initialized, counts).
#   Migration: an old-shape row (keys `llm`/`spend`/`last_analysis`/`applies`
#   in its JSON) is moved into the new places inside the writer's own
#   transaction; readers of a store no writer has touched yet fall back to
#   the JSON, so nothing is lost either way.
_SESSION_COLUMNS = (("llm", "TEXT"), ("spend_usd", "REAL NOT NULL DEFAULT 0"),
                    ("spend_unknown", "INTEGER NOT NULL DEFAULT 0"),
                    ("spend_calls", "INTEGER NOT NULL DEFAULT 0"))
_LEGACY_SESSION_KEYS = ("llm", "spend", "last_analysis", "applies")
#: Apply results kept per session (the historical cap), and analyses.
_KEEP_APPLIES = 20
_KEEP_ANALYSES = 5


def _session_columns(con) -> set:
    return {r[1] for r in con.execute('PRAGMA table_info(import_sessions)')}


def _session_storage_schema(con):
    have = _session_columns(con)
    for name, decl in _SESSION_COLUMNS:
        if name not in have:
            con.execute(f'ALTER TABLE import_sessions ADD COLUMN {name} {decl}')
    con.execute('CREATE TABLE IF NOT EXISTS import_session_results('
                'seq INTEGER PRIMARY KEY, session TEXT NOT NULL, kind TEXT NOT NULL, '
                'at REAL NOT NULL, data TEXT NOT NULL)')
    con.execute('CREATE INDEX IF NOT EXISTS import_session_results_by_session '
                'ON import_session_results(session, kind, seq)')
    # A key is quoted and followed by a colon in the stored JSON; a string
    # VALUE containing that text has its quotes escaped, so it cannot match.
    legacy = con.execute(
        'SELECT id, data FROM import_sessions WHERE '
        'data LIKE ? OR data LIKE ? OR data LIKE ? OR data LIKE ?',
        tuple(f'%"{k}":%' for k in _LEGACY_SESSION_KEYS)).fetchall()
    for sid, raw in legacy:
        data = json.loads(raw)
        if not any(k in data for k in _LEGACY_SESSION_KEYS):
            continue  # the key text sat deeper (a nested field); nothing to move
        llm = data.pop('llm', None)
        spend = data.pop('spend', None) or {}
        last = data.pop('last_analysis', None)
        applies = data.pop('applies', None) or []
        con.execute('UPDATE import_sessions SET data=?, llm=?, spend_usd=?, spend_unknown=?, '
                    'spend_calls=? WHERE id=?',
                    (json.dumps(data), json.dumps(llm) if llm else None,
                     float(spend.get('usd') or 0.0), 1 if spend.get('unknown') else 0,
                     int(spend.get('calls') or 0), sid))
        for kind, rows in (('analyze', [last] if last else []), ('apply', applies)):
            for r in rows:
                if isinstance(r, dict):
                    con.execute('INSERT INTO import_session_results(session, kind, at, data) '
                                'VALUES (?,?,?,?)',
                                (sid, kind, float(r.get('at') or 0.0),
                                 json.dumps(r.get('result'), sort_keys=True)))


def _has_schema(con) -> bool:
    return bool(con and con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='import_units'").fetchone())


def _load_units(con, scope: str) -> dict[str, dict]:
    return {r[0]: json.loads(r[1]) for r in con.execute(
        'SELECT id, data FROM import_units WHERE scope=?', (scope,))}


def _save_unit(con, scope, uid, data):
    con.execute('INSERT OR REPLACE INTO import_units VALUES (?,?,?)',
                (scope, uid, json.dumps(data, sort_keys=True)))


def _event(con, scope, session, uid, data):
    con.execute('INSERT INTO import_events(scope,session,unit,at,data) VALUES (?,?,?,?,?)',
                (scope, session, uid, time.time(), json.dumps(data, sort_keys=True)))


def begin_session(settings, *, label: str = '', llm: Optional[dict] = None) -> str:
    """A new run id. Any number per day; ids never collide (uuid4).

    *llm* (an already validated model choice) is stored in the SAME
    transaction (audit remediation 2026-09-23, L12): the route used to create
    the session and then set its model in a second transaction, so a store
    locked between the two left a session with no model behind a 500.
    """
    sid = uuid.uuid4().hex
    scope = rec.scope(settings)
    with rec.connection(True) as con:
        _schema(con)
        con.execute('INSERT INTO import_sessions(id, scope, started_at, data, llm) '
                    'VALUES (?,?,?,?,?)',
                    (sid, scope, time.time(), json.dumps({'label': label}),
                     json.dumps(dict(llm)) if llm else None))
    return sid


def _update_session(con, sid, **fields):
    """Merge ``fields`` into a session's JSON, keeping what others wrote.

    For the small pass fields only; the model, the spend and the run results
    have their own columns/rows (see SESSION STORAGE above)."""
    row = con.execute('SELECT data FROM import_sessions WHERE id=?', (sid,)).fetchone()
    data = json.loads(row[0]) if row else {}
    data.update(fields)
    con.execute('UPDATE import_sessions SET data=? WHERE id=?', (json.dumps(data), sid))
    return data


def _session_exists(con, scope, sid) -> bool:
    return bool(con.execute('SELECT 1 FROM import_sessions WHERE id=? AND scope=?',
                            (sid, scope)).fetchone())


def pdf_parents(units: dict[str, dict]) -> set[str]:
    """Entry ids that have a present PDF attachment (one pass, reused)."""
    return {u['parent'] for u in units.values()
            if u.get('kind') == 'attachment' and u.get('parent')
            and u.get('state', 'present') == 'present'}


def entry_steps(unit: dict, with_pdf: set[str]) -> tuple[str, ...]:
    """The steps a unit owes. An entry with a live PDF attachment owes nothing
    itself: tagging runs once, through that attachment, for file AND entry.

    ``with_pdf`` is :func:`pdf_parents` computed ONCE per pass: testing each
    entry against every unit would be quadratic (≈ 6 000 entries × 12 000
    units on the maintainer's library) for an answer that does not change
    within a pass.
    """
    steps = KIND_STEPS.get(unit.get('kind') or '', ())
    if unit.get('kind') == 'entry' and unit.get('id') in with_pdf:
        return ()
    if unit.get('kind') == 'attachment' and unit.get('in_biblio') is False:
        # No file in `biblio_articles/` (a link elsewhere, a relative link, a
        # missing file): nothing the renamer may name, and no Finder file to
        # tag. The parent entry's Zotero tags are still owed.
        return ('tag',)
    return steps


def _open_steps(unit: dict, with_pdf: set[str]) -> list[str]:
    if unit.get('baseline') or unit.get('state', 'present') != 'present':
        return []
    done = unit.get('steps') or {}
    return [s for s in entry_steps(unit, with_pdf)
            if (done.get(s) or {}).get('status') not in TERMINAL]


def _reliable_doc(doc: Optional[str]) -> bool:
    """A file identity safe to MATCH across time: needs a birth time.

    Without one (a filesystem that does not report it) ``doc:<ino>:None`` is
    only an inode, and inodes are reused: matching on it could hand one
    paper's history to an unrelated file. Such ids still name an orphan for
    display; they are never used to adopt.
    """
    return bool(doc) and not str(doc).endswith(':None')


def _adoption_candidate(tracked: dict, by_doc: dict, uid: str, obs: dict,
                        observation: dict, *, documents_only: bool = False) -> Optional[str]:
    """The tracked unit whose history an attachment takes over, or None.

    Only an attachment adopts — it is where a file ends up once Zotero links
    it — and only a unit that has LEFT the observation (it cannot be the same
    work twice). In order of preference, so the answer never depends on
    table order:

    1. a present orphan ``document`` for this file: the file just got linked
       (Attanger's move landing in Zotero, a manual link, a re-point);
    2. an attachment of the SAME parent entry for this file: the link was
       re-created under a new key. A different parent is a different
       record — new work, even for the same bytes — so it is never adopted
       (review 2026-09-21: adopting it handed the old record's ``tag: done``
       to a new entry that was then never tagged).
    """
    doc = obs.get('doc')
    if obs.get('kind') != 'attachment' or not _reliable_doc(doc):
        return None
    candidates = sorted(pid for pid in by_doc.get(doc, ()) if pid != uid and pid not in observation)
    for pid in candidates:
        p = tracked[pid]
        if p['kind'] == 'document' and p.get('state', 'present') == 'present':
            return pid
    if documents_only:
        return None
    for pid in candidates:
        p = tracked[pid]
        if (p['kind'] == 'attachment' and p.get('state') != 'absorbed'
                and p.get('parent') == obs.get('parent')):
            return pid
    return None


def _same_path_document(tracked: dict, uid: str, obs: dict, observation: dict,
                        path_index: Optional[dict] = None) -> Optional[str]:
    """A tracked orphan at the SAME path that has just left: a safe-save.

    Editors that save atomically (write a temporary file, rename it over the
    original) give the file a new inode, so its ``doc:`` id changes although
    nothing moved. For an orphan — which has no Zotero key to anchor it — the
    path is then the only continuity, and treating the saved file as a new
    import would propose a historical PDF (review 2026-09-21). Only a
    document, only when the old unit is present-until-now and absent from this
    observation, and only on an exact path match.
    """
    if obs.get('kind') != 'document' or not obs.get('rel'):
        return None
    # `path_index` (audit remediation 2026-09-23, P4): the candidates for
    # THIS path, built once per pass, instead of `sorted(tracked)` — the
    # whole tracked library, sorted — for every new orphan. Every condition
    # is still re-checked on the live unit (an absorb earlier in the pass
    # changes its state), and candidates are visited in the same sorted
    # order, so the answer is the one the full scan gave.
    candidates = (sorted(path_index.get(obs['rel'], ())) if path_index is not None
                  else sorted(tracked))
    for pid in candidates:
        p = tracked[pid]
        if (pid != uid and pid not in observation and p.get('kind') == 'document'
                and p.get('state', 'present') == 'present' and p.get('rel') == obs['rel']):
            return pid
    return None


def _document_path_index(tracked: dict, observation: dict) -> dict[str, list[str]]:
    """``{rel: [document ids]}`` of tracked documents ABSENT from this
    observation — the only ones `_same_path_document` may return. Their
    ``rel`` cannot change during the pass (only observed units are updated),
    so an index built before the loop stays exact."""
    index: dict[str, list[str]] = {}
    for pid, p in tracked.items():
        if p.get('kind') == 'document' and p.get('rel') and pid not in observation:
            index.setdefault(p['rel'], []).append(pid)
    return index


def record_pass(settings, session: str, observation: dict[str, dict], *,
                now: Optional[float] = None) -> dict:
    """Persist one observation against the tracked state and classify it.

    Returns ``initialized`` (the first pass: everything recorded as baseline,
    nothing proposed), ``new`` (units registered by THIS pass, by category),
    ``open`` (every unit still owing a step, whichever pass registered it),
    ``modified_notes`` (notes whose Zotero text changed in this pass and now
    differs from what was last mirrored), ``renamed``, ``gone``, ``returned``
    and ``adopted`` (``{id, from}``: one file, a new identity, one history).
    """
    now = time.time() if now is None else now
    scope = rec.scope(settings)
    with rec.connection(True) as con:
        _schema(con)
        if not _session_exists(con, scope, session):
            raise ImportTrackingError('Unknown session; begin one first.')
        tracked = _load_units(con, scope)
        row = con.execute('SELECT last_observed_at FROM import_scopes WHERE scope=?',
                          (scope,)).fetchone()
        initialized = row is None
        observed_at = getattr(observation, 'observed_at', None)
        if row is not None and observed_at is not None and row[0] is not None \
                and observed_at < row[0]:
            # Refused inside the transaction, so nothing of this pass lands.
            raise StaleObservation('A newer observation was recorded meanwhile; observe again.')
        if initialized:
            con.execute('INSERT INTO import_scopes VALUES (?,?,?,?)',
                        (scope, now, session, observed_at))
        elif observed_at is not None:
            con.execute('UPDATE import_scopes SET last_observed_at=? WHERE scope=?',
                        (observed_at, scope))
        known_before = {uid for uid, u in tracked.items() if u.get('state', 'present') == 'present'}
        obs_pdf_parents = {u['parent'] for u in observation.values()
                           if u.get('kind') == 'attachment' and u.get('parent')}
        by_doc: dict[str, list[str]] = {}
        for uid, u in tracked.items():
            if _reliable_doc(u.get('doc')):
                by_doc.setdefault(u['doc'], []).append(uid)
        report: dict[str, Any] = dict(session=session, initialized=initialized,
                                      new={c: [] for c in CATEGORIES}, modified_notes=[],
                                      renamed=[], gone=[], returned=[], adopted=[])

        def save(uid, unit):
            _save_unit(con, scope, uid, unit)

        def absorb(prior_id, by_uid):
            prior = tracked[prior_id]
            if prior['kind'] == 'document':
                # Associating the file IS this step; record it as done by the
                # unit that now owns the file.
                prior.setdefault('steps', {})['associate'] = {
                    'status': 'done', 'session': session, 'at': now, 'detail': {'by': by_uid}}
            prior.update(state='absorbed', absorbed_by=by_uid)
            save(prior_id, prior)
            _event(con, scope, session, prior_id, {'event': 'absorbed', 'by': by_uid})
            report['adopted'].append({'id': by_uid, 'from': prior_id})

        def category(obs):
            kind = obs['kind']
            if kind == 'attachment':
                return ('pdf_added_to_known_entry' if obs['parent'] in known_before
                        else 'new_entry_with_pdf')
            if kind == 'entry':
                uid = f"entry:{obs['library']}:{obs['key']}"
                return 'new_entry_with_pdf' if uid in obs_pdf_parents else 'new_entry_without_pdf'
            return 'new_note' if kind == 'note' else 'pdf_not_in_zotero'

        # Attachments whose linked path is missing but whose last known file
        # identity is reliable: {doc: unit id}. Filled while the existing units
        # are processed (FIRST, see the ordering below), consulted for new
        # orphans.
        moved_doc_owner: dict[str, list[str]] = {}
        # Existing units first, then new ones: a new orphan must be able to
        # see that its file belongs to an attachment whose link has not caught
        # up, whatever order the observation dict happens to have.
        ordered = sorted(observation.items(), key=lambda kv: kv[0] not in tracked)
        path_index = _document_path_index(tracked, observation)
        for uid, obs in ordered:
            unit = tracked.get(uid)
            if unit is None and obs['kind'] == 'document' and obs.get('doc') in moved_doc_owner:
                # This "orphan" is the file of tracked attachment(s) whose
                # Zotero path is stale (typically: this app just renamed it and
                # Zotero's local database has not recorded the repath). Not a
                # new import: every attachment that last held this file keeps
                # it, with its current path — not just one of them (review
                # 2026-09-21: two links to one file left one pointing at a
                # path that no longer existed). The file was found in the
                # biblio walk, so it IS in the biblio folder.
                for owner_id in moved_doc_owner[obs['doc']]:
                    owner = tracked[owner_id]
                    if obs.get('rel') and owner.get('rel') != obs['rel']:
                        report['renamed'].append({'id': owner_id, 'from': owner.get('rel'),
                                                  'to': obs['rel']})
                        owner['rel'] = obs['rel']
                    owner.update(in_biblio=True, found_by_identity=True)
                    save(owner_id, owner)
                continue
            if unit is None:
                base = dict({k: v for k, v in obs.items() if k not in _TRANSIENT_FIELDS},
                            id=uid, first_seen=now, first_seen_session=session,
                            state='present', steps={})
                if initialized:
                    base['baseline'] = True
                    if obs['kind'] == 'note':
                        # A baseline note counts as mirrored as it stands, so a
                        # later edit in Zotero is detected as a modification.
                        base['mirrored_sha256'] = obs['note_sha256']
                else:
                    prior_id = (_adoption_candidate(tracked, by_doc, uid, obs, observation)
                                or _same_path_document(tracked, uid, obs, observation,
                                                       path_index))
                    prior = tracked.get(prior_id) if prior_id else None
                    if prior is not None and prior['kind'] == obs['kind']:
                        # Re-created under a new key for the same record: the
                        # SAME piece of work — history, baseline flag and first
                        # category all carry over.
                        base.update(first_seen=prior.get('first_seen', now),
                                    first_seen_session=prior.get('first_seen_session', session),
                                    steps=_carry_steps(prior.get('steps') or {}, obs['kind']),
                                    baseline=prior.get('baseline', False),
                                    category=prior.get('category'), adopted_from=prior_id)
                    else:
                        # New work. When it takes over an orphan file, the
                        # record is still new (name it, tag it) even if the
                        # file predates activation; only `associate` closes.
                        base['category'] = category(obs)
                        report['new'][base['category']].append(uid)
                        if prior is not None:
                            base['adopted_from'] = prior_id
                    tracked[uid] = base
                    if prior is not None:
                        absorb(prior_id, uid)
                save(uid, base)
                tracked[uid] = base
                if _reliable_doc(base.get('doc')):
                    by_doc.setdefault(base['doc'], []).append(uid)
                _event(con, scope, session, uid, {'event': 'baseline' if initialized else 'registered',
                                                  'category': base.get('category')})
                continue

            changed = False
            if unit.get('state') == 'absorbed':
                adopter_id = unit.get('absorbed_by')
                adopter = tracked.get(adopter_id or '')
                # Re-keying can form A -> B -> C. Only C owns the latest
                # outcomes; restoring A from B would discard C's successes
                # or uncertainty. Walk the chain with a cycle guard before
                # returning history, retaining it on a still-present owner.
                visited: set[Optional[str]] = {uid}
                while adopter is not None and adopter.get('state') == 'absorbed':
                    if adopter_id in visited:
                        raise ImportTrackingError('Cyclic import adoption history; inspect the tracker.')
                    visited.add(adopter_id)
                    adopter_id = adopter.get('absorbed_by')
                    adopter = tracked.get(adopter_id or '')
                if adopter is not None and adopter_id in observation:
                    continue  # both identities present: the history stays on the adopter
                # The adopter has left: the history returns to this identity,
                # so an undone re-key or a record trashed after linking loses
                # nothing (review 2026-09-21, T1/T4).
                unit['state'] = 'present'
                unit.pop('absorbed_by', None)
                if adopter is not None and adopter['kind'] == unit['kind']:
                    unit['steps'] = _carry_steps(adopter.get('steps') or {}, unit['kind'])
                    adopter.update(state='absorbed', absorbed_by=uid)
                    save(adopter_id, adopter)
                    _event(con, scope, session, adopter_id, {'event': 'absorbed', 'by': uid})
                elif unit['kind'] == 'document':
                    # The file has no live record again: associating it is owed again.
                    unit.setdefault('steps', {})['associate'] = {
                        'status': 'reopened', 'session': session, 'at': now}
                report['returned'].append(uid)
                changed = True
            elif unit.get('state') != 'present':
                unit['state'] = 'present'
                report['returned'].append(uid)
                changed = True
            if obs.get('rel') and obs.get('rel') != unit.get('rel'):
                if unit.get('rel'):
                    report['renamed'].append({'id': uid, 'from': unit['rel'], 'to': obs['rel']})
                unit['rel'] = obs['rel']
                changed = True
            if (obs['kind'] == 'attachment' and not obs.get('file_present')
                    and _reliable_doc(unit.get('doc'))):
                # The link names a path that is not there (yet): Zotero has
                # not recorded a rename, or Attanger is mid-move. Keep the
                # last KNOWN file identity rather than forgetting it — it is
                # what lets the renamed file be recognised as this unit's
                # below instead of being registered as a new orphan.
                obs = dict(obs, doc=unit['doc'])
                moved_doc_owner.setdefault(unit['doc'], []).append(uid)
            doc_changed = obs.get('doc') != unit.get('doc')
            if (obs['kind'] == 'attachment' and obs.get('parent') != unit.get('parent')):
                # A stable attachment key does not imply a stable tagging
                # destination. Its previous parent's tags cannot settle the
                # new parent's work, even for an activation-baseline PDF.
                unit['baseline'] = False
                unit.setdefault('steps', {})['tag'] = {
                    'status': 'reopened', 'session': session, 'at': now,
                    'detail': {'reason': 'parent_changed'}}
                changed = True
            # `in_biblio` follows the file (review 2026-09-21): it was frozen at
            # first sight, so a link moved into the folder never owed a rename
            # and a deleted file owed one forever. A file found again by its
            # identity sets it back just below (the moved-file branch).
            unit.pop('found_by_identity', None)
            for field in ('citation_key', 'title', 'parent', 'doc', 'link_mode', 'file_present',
                          'in_biblio'):
                if field in obs and obs[field] != unit.get(field):
                    unit[field] = obs[field]
                    changed = True
            if doc_changed and _reliable_doc(obs.get('doc')):
                by_doc.setdefault(obs['doc'], []).append(uid)
                # An existing attachment re-pointed onto a tracked orphan file
                # (`/api/zotlink/repoint`, or Attanger's move reaching Zotero a
                # pass after the file reached the vault) absorbs that orphan;
                # it keeps its own steps — it is the same record's work.
                orphan = _adoption_candidate(tracked, by_doc, uid, obs, observation,
                                             documents_only=True)
                if orphan:
                    absorb(orphan, uid)
            if obs['kind'] == 'note' and obs['note_sha256'] != unit.get('note_sha256'):
                unit['note_sha256'] = obs['note_sha256']
                changed = True
                if unit.get('mirrored_sha256') and unit['note_sha256'] != unit['mirrored_sha256']:
                    # Reopen exactly `mirror`; baseline no longer shields it,
                    # since the change happened after activation. Reported
                    # only on the pass that SEES the change, not on every pass
                    # while the reopened step waits.
                    unit.setdefault('steps', {})['mirror'] = {
                        'status': 'reopened', 'session': session, 'at': now}
                    unit['baseline'] = False
                    report['modified_notes'].append(uid)
            if changed:
                save(uid, unit)

        for uid, unit in tracked.items():
            if uid not in observation and unit.get('state', 'present') == 'present':
                unit.update(state='gone', gone_session=session, gone_at=now)
                save(uid, unit)
                _event(con, scope, session, uid, {'event': 'gone'})
                report['gone'].append(uid)

        # Imported copies whose bytes are not where Zotero says: Attanger is
        # probably moving them. The file it moves may show up as an orphan
        # under its NEW name for one pass (the name no longer matches the
        # link), until Zotero records the new path and that attachment absorbs
        # it. Reported so a caller can wait for Zotero to settle before acting
        # on this pass's orphans, rather than identify a file online for nothing.
        # Leftovers (an imported copy beside a live linked copy in the biblio
        # folder) are split out: nothing is moving them (finding 4).
        report['leftovers'], report['settling'] = classify_leftovers(observation)
        for key in ('modified_notes', 'returned', 'gone'):
            report[key].sort()
        for ids in report['new'].values():
            ids.sort()
        with_pdf = pdf_parents(tracked)
        report['open'] = []
        for uid in sorted(tracked):
            u = tracked[uid]
            owed = _open_steps(u, with_pdf)
            if owed:
                report['open'].append({
                    'id': uid, 'category': u.get('category'), 'steps': owed,
                    'first_seen_session': u.get('first_seen_session'), 'rel': u.get('rel'),
                    'citation_key': u.get('citation_key') or _parent_field(u, tracked, 'citation_key')})
        _update_session(con, session, last_pass_at=now, initialized=initialized,
                        new={k: len(v) for k, v in report['new'].items()})
    return report


def _parent_field(unit, tracked, field):
    parent = tracked.get(unit.get('parent') or '')
    return (parent or {}).get(field)


def _carry_steps(steps: dict, kind: str) -> dict:
    """Keep the outcomes that still mean something for the adopting kind.

    A document's ``associate`` is what its becoming an attachment completes;
    the attachment's own ``rename``/``tag`` outcomes carry over unchanged.
    """
    allowed = set(KIND_STEPS.get(kind, ()))
    return {s: v for s, v in steps.items() if s in allowed}


def record_step(settings, session: str, unit_id: str, step: str, status: str, *,
                detail: Optional[dict] = None, note_sha256: Optional[str] = None) -> dict:
    """Record one step's outcome for one unit, in its own transaction.

    Per-unit, per-step and immediately durable: a crash after the rename of
    unit A and before its tagging leaves ``rename: done`` recorded, so the
    resumed run tags A without renaming it again. ``note_sha256`` is the
    digest of the note text that was actually mirrored; without it the
    unit's current digest is assumed, which is right only when the caller
    mirrored from the same observation. The orchestrator
    (``library_run._apply_mirror``) always passes it, from the mirror's own
    ``settled_keys`` (audit remediation 2026-09-23, L8), so a note edited in
    Zotero during the sweep is recorded ``reopened`` rather than closed.
    """
    if status not in STATUSES:
        raise ImportTrackingError(f'Unknown status {status!r}.')
    scope = rec.scope(settings)
    with rec.connection(True) as con:
        _schema(con)
        if not _session_exists(con, scope, session):
            raise ImportTrackingError('Unknown session; begin one first.')
        row = con.execute('SELECT data FROM import_units WHERE scope=? AND id=?',
                          (scope, unit_id)).fetchone()
        if row is None:
            raise ImportTrackingError(f'Unknown unit {unit_id!r}.')
        unit = json.loads(row[0])
        if unit.get('state') == 'absorbed':
            raise ImportTrackingError(
                f'{unit_id} now lives on as {unit.get("absorbed_by")}; record the step there.')
        if step not in KIND_STEPS.get(unit['kind'], ()):
            raise ImportTrackingError(f'Step {step!r} does not apply to a {unit["kind"]}.')
        entry = {'status': status, 'session': session, 'at': time.time()}
        if detail:
            entry['detail'] = detail
        unit.setdefault('steps', {})[step] = entry
        if step == 'mirror' and status == 'done':
            unit['mirrored_sha256'] = note_sha256 or unit.get('note_sha256')
            if unit['mirrored_sha256'] != unit.get('note_sha256'):
                # An observation may have advanced while the older mirror
                # finished. Preserve its evidence, but do not close the work
                # owed for the newer text (which may not change next pass).
                unit['baseline'] = False
                entry['status'] = 'reopened'
        _save_unit(con, scope, unit_id, unit)
        _event(con, scope, session, unit_id, {'event': 'step', 'step': step, **entry})
        zotero_written = ((detail or {}).get('targets') or {}).get('zotero') == 'done'
        if (unit['kind'] == 'attachment' and step == 'tag' and status in TERMINAL
                and unit.get('parent') and zotero_written):
            # Tagging a PDF also tags its parent entry in Zotero. Credit the
            # entry in the same transaction, so that if the attachment later
            # goes (a duplicate removed) the entry does not suddenly owe a
            # second tagging it already had (review 2026-09-21, T3).
            # Only when the ZOTERO target was actually written (audit
            # remediation 2026-09-23, maintainer decision): a Finder-only
            # tag, or a step skipped with nothing written, tags the file and
            # not the entry, so the entry still owes its tagging.
            prow = con.execute('SELECT data FROM import_units WHERE scope=? AND id=?',
                               (scope, unit['parent'])).fetchone()
            if prow is not None:
                parent = json.loads(prow[0])
                parent.setdefault('steps', {})['tag'] = dict(entry, detail={'via': unit_id})
                _save_unit(con, scope, unit['parent'], parent)
                _event(con, scope, session, unit['parent'],
                       {'event': 'step', 'step': 'tag', 'via': unit_id, **entry})
    return unit


def unit(settings, uid: str) -> Optional[dict]:
    """One tracked unit, or None (read-only)."""
    with rec.connection() as con:
        if not _has_schema(con):
            return None
        row = con.execute('SELECT data FROM import_units WHERE scope=? AND id=?',
                          (rec.scope(settings), uid)).fetchone()
    return json.loads(row[0]) if row else None


def units(settings) -> dict[str, dict]:
    """Every tracked unit for this library (read-only; empty before activation)."""
    with rec.connection() as con:
        if not _has_schema(con):
            return {}
        return _load_units(con, rec.scope(settings))


def history(settings, session: Optional[str] = None) -> list[dict]:
    """The event log, oldest first, optionally for one session."""
    with rec.connection() as con:
        if not _has_schema(con):
            return []
        sql = 'SELECT session, unit, at, data FROM import_events WHERE scope=?'
        args: list = [rec.scope(settings)]
        if session:
            sql += ' AND session=?'
            args.append(session)
        return [dict(json.loads(d), session=s, unit=u, at=a)
                for s, u, a, d in con.execute(sql + ' ORDER BY seq', args)]


def unfinished_operations(settings) -> dict:
    """Operations earlier runs left unsettled, per store.

    Each store is read through its own public function. A store that cannot
    be read is reported with its error — never as zero — because "nothing is
    stuck" and "I could not look" call for opposite next steps.
    """
    out: dict[str, Any] = {}
    vault = settings.vault_root
    try:
        from zotcreate import attempts
        out['zotero_item_creates'] = [a for a in attempts.list_attempts(vault)
                                      if a.get('stage') != attempts.STAGE_COMPLETE]
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed as zero
        out['zotero_item_creates'] = {'error': str(exc)}
    try:
        from accession import pdf_management
        out['pdf_operations'] = [o for o in pdf_management.operations(settings)
                                 if o.get('status') not in pdf_management.SETTLED]
    except Exception as exc:  # noqa: BLE001
        out['pdf_operations'] = {'error': str(exc)}
    try:
        from notesync import create_attempts
        out['note_creates'] = [a for a in create_attempts.list_attempts(vault)
                               if a.get('status') == 'uncertain'
                               and a.get('disposition') not in ('satisfied', 'superseded')]
    except Exception as exc:  # noqa: BLE001
        out['note_creates'] = {'error': str(exc)}
    return out


# ---------------------------------------------------------------------------
# Session model choice, spend and proposal cache (library-workflow lot 3)
# ---------------------------------------------------------------------------

def _spend(usd, unknown, calls) -> dict:
    return {'usd': float(usd or 0.0), 'unknown': bool(unknown), 'calls': int(calls or 0)}


def session(settings, sid: str) -> Optional[dict]:
    """One session's summary for this library, or None.

    ``{id, started_at, label, …pass fields, llm, spend, last_analysis,
    applies, apply_count}`` — ``last_analysis`` is the latest analysis
    ``{at, result}`` and ``applies`` holds ONLY the latest apply (what the
    interface and the CLI read); older results stay in the store. A store
    no writer has migrated yet is read from its JSON (see SESSION STORAGE).
    """
    scope = rec.scope(settings)
    with rec.connection() as con:
        if not _has_schema(con):
            return None
        new_shape = {'llm', 'spend_usd'} <= _session_columns(con)
        if not new_shape:
            row = con.execute('SELECT started_at, data FROM import_sessions WHERE id=? AND scope=?',
                              (sid, scope)).fetchone()
            if row is None:
                return None
            out = dict(json.loads(row[1]), id=sid, started_at=row[0])
            out['applies'] = (out.get('applies') or [])[-1:]
            return out
        row = con.execute('SELECT started_at, data, llm, spend_usd, spend_unknown, spend_calls '
                          'FROM import_sessions WHERE id=? AND scope=?', (sid, scope)).fetchone()
        if row is None:
            return None
        out = dict(json.loads(row[1]), id=sid, started_at=row[0])
        if row[2]:
            out['llm'] = json.loads(row[2])
        out['spend'] = _spend(row[3], row[4], row[5])
        has_results = bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                       "AND name='import_session_results'").fetchone())
        last: dict = {}
        count = 0
        if has_results:
            for kind in ('analyze', 'apply'):
                r = con.execute('SELECT at, data FROM import_session_results WHERE session=? '
                                'AND kind=? ORDER BY seq DESC LIMIT 1', (sid, kind)).fetchone()
                if r is not None:
                    last[kind] = {'at': r[0], 'result': json.loads(r[1])}
            count = con.execute('SELECT COUNT(*) FROM import_session_results WHERE session=? '
                                'AND kind=?', (sid, 'apply')).fetchone()[0]
    if 'analyze' in last:
        out['last_analysis'] = last['analyze']
    out['applies'] = [last['apply']] if 'apply' in last else []
    out['apply_count'] = count
    return out


def record_session_result(settings, sid: str, kind: str, result: dict,
                          at: Optional[float] = None) -> None:
    """Keep one analysis or apply result on the session: ONE insert.

    Replaces the read-the-list-then-write-it-back save of lot 5, whose two
    transactions could lose an entry to a concurrent writer (P5). Bounded:
    the newest ``_KEEP_APPLIES`` applies and ``_KEEP_ANALYSES`` analyses.
    """
    if kind not in ('analyze', 'apply'):
        raise ImportTrackingError(f'Unknown result kind {kind!r}.')
    keep = _KEEP_APPLIES if kind == 'apply' else _KEEP_ANALYSES
    scope = rec.scope(settings)
    with rec.connection(True) as con:
        _schema(con)
        if not _session_exists(con, scope, sid):
            raise ImportTrackingError('Unknown session.')
        con.execute('INSERT INTO import_session_results(session, kind, at, data) VALUES (?,?,?,?)',
                    (sid, kind, time.time() if at is None else float(at),
                     json.dumps(result, sort_keys=True, default=str)))
        con.execute('DELETE FROM import_session_results WHERE session=? AND kind=? AND seq NOT IN '
                    '(SELECT seq FROM import_session_results WHERE session=? AND kind=? '
                    'ORDER BY seq DESC LIMIT ?)', (sid, kind, sid, kind, keep))


def set_session_llm(settings, sid: str, llm: dict) -> dict:
    """Store the session's (already validated) model choice.

    The spend recorded so far is kept when the model changes: the budget is
    the SESSION's, whichever models spent it. One column, one UPDATE.
    """
    scope = rec.scope(settings)
    with rec.connection(True) as con:
        _schema(con)
        if not _session_exists(con, scope, sid):
            raise ImportTrackingError('Unknown session; begin one first.')
        con.execute('UPDATE import_sessions SET llm=? WHERE id=?', (json.dumps(dict(llm)), sid))
        row = con.execute('SELECT spend_usd, spend_unknown, spend_calls FROM import_sessions '
                          'WHERE id=?', (sid,)).fetchone()
    return {'llm': dict(llm), 'spend': _spend(*row)}


def charge_session(settings, sid: str, usd: Optional[float]) -> dict:
    """Add one call's cost to the session (``None`` = could not be measured).

    Its own transaction, called by the propose worker right after the call:
    a request that timed out on the client still has its worker's cost
    recorded when the call completes. Touches only the spend columns.
    """
    scope = rec.scope(settings)
    with rec.connection(True) as con:
        _schema(con)
        if not _session_exists(con, scope, sid):
            raise ImportTrackingError('Unknown session.')
        con.execute('UPDATE import_sessions SET spend_calls=spend_calls+1, '
                    'spend_usd=spend_usd+?, spend_unknown=MAX(spend_unknown, ?) WHERE id=?',
                    (0.0 if usd is None else float(usd), 1 if usd is None else 0, sid))
        row = con.execute('SELECT spend_usd, spend_unknown, spend_calls FROM import_sessions '
                          'WHERE id=?', (sid,)).fetchone()
        _event(con, scope, sid, None, {'event': 'charge', 'usd': usd})
    return _spend(*row)


def mark_spend_unknown(settings, sid: str) -> dict:
    """Mark the session's spend UNKNOWN after a charge could not be recorded.

    Audit remediation 2026-09-23 (L4, maintainer decision): the spend of a
    call that ran is not lost silently — a budgeted session then refuses
    further proposals (``session_llm.check_budget``: an unknown spend cannot
    be compared). A separate, smaller write than the charge, so it may land
    when the charge could not.
    """
    scope = rec.scope(settings)
    with rec.connection(True) as con:
        _schema(con)
        if not _session_exists(con, scope, sid):
            raise ImportTrackingError('Unknown session.')
        con.execute('UPDATE import_sessions SET spend_unknown=1 WHERE id=?', (sid,))
        row = con.execute('SELECT spend_usd, spend_unknown, spend_calls FROM import_sessions '
                          'WHERE id=?', (sid,)).fetchone()
    return _spend(*row)


def cached_proposal(settings, key: str) -> Optional[dict]:
    with rec.connection() as con:
        if not (con and con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                    "AND name='import_proposals'").fetchone()):
            return None
        row = con.execute('SELECT data FROM import_proposals WHERE scope=? AND key=?',
                          (rec.scope(settings), key)).fetchone()
    return json.loads(row[0]) if row else None


def store_proposal(settings, key: str, data: dict) -> None:
    """Keep a proposal for resumption. Keyed by EVERYTHING that decides it
    (see ``api/routes/tagging.py::_proposal_key``), so a changed vocabulary,
    source text or model is a miss, never a stale hit."""
    with rec.connection(True) as con:
        _schema(con)
        con.execute('INSERT OR REPLACE INTO import_proposals VALUES (?,?,?,?)',
                    (rec.scope(settings), key, time.time(), json.dumps(data, sort_keys=True)))


def note_rename(settings, sid: str, uid: str, new_rel: str) -> dict:
    """Move a tracked attachment's path after a rename THIS app performed.

    Refused unless the file at ``new_rel`` is the unit's file (same inode and
    birth time — a rename keeps both), so a caller cannot point a unit at
    another document. It exists because Zotero's local database may record
    the repath only later (web transport, sync lag): until it does, a pass
    would find the linked path missing and the unit unaddressable.
    """
    scope = rec.scope(settings)
    path = Path(settings.vault_root) / new_rel
    # Canonical, contained, not a symlink (review 2026-09-21): `stat()`
    # follows links and `..` survives a join, so a hard link or symlink
    # OUTSIDE the vault to the unit's own file passed the identity test and a
    # non-canonical spelling was stored as the path.
    if path.is_symlink() or not _under(path, settings.biblio_articles_dir):
        raise ImportTrackingError('The renamed file must be a regular file in the biblio folder.')
    canonical = _rel(path, settings.vault_root)
    try:
        doc = _doc_id(review.fingerprint(path))
    except OSError as exc:
        raise ImportTrackingError(f'The renamed file cannot be read: {exc}') from exc
    with rec.connection(True) as con:
        _schema(con)
        if not _session_exists(con, scope, sid):
            raise ImportTrackingError('Unknown session.')
        row = con.execute('SELECT data FROM import_units WHERE scope=? AND id=?',
                          (scope, uid)).fetchone()
        if row is None:
            raise ImportTrackingError(f'Unknown unit {uid!r}.')
        unit_rec = json.loads(row[0])
        if unit_rec.get('state', 'present') != 'present':
            raise ImportTrackingError(
                f'{uid} is {unit_rec.get("state")}'
                + (f' (now {unit_rec["absorbed_by"]})' if unit_rec.get('absorbed_by') else '') + '.')
        if unit_rec.get('kind') != 'attachment' or not _reliable_doc(doc) or unit_rec.get('doc') != doc:
            raise ImportTrackingError('That file is not this unit\'s file.')
        old = unit_rec.get('rel')
        unit_rec.update(rel=canonical, in_biblio=True, found_by_identity=True)
        _save_unit(con, scope, uid, unit_rec)
        _event(con, scope, sid, uid, {'event': 'renamed_by_app', 'from': old, 'to': canonical})
    return unit_rec
