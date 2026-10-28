"""Shared, document-class-agnostic helpers for the LaTeX generator routes.

Both the Deck Generator (Beamer) and the Doc Generator (article/report) need the
same route-level machinery: validating a template path / an output folder,
reading a template safely, hashing on-disk bytes for a stale-diff token, and
resolving + copying a document's ``\\includegraphics`` figures from the vault.

This module is the single home of that logic for the **docgen** route.
``deck.py`` keeps its own historical private copies (unchanged, so the mature
deck path carries zero regression risk); a future cleanup can migrate it here.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from api.validators import coerce_string_max_len
from core.constants import VAULT_IMAGE_EXTS
from core.paths import resolve_under_root
from deckgen.latex import comment_out_missing_graphics, extract_graphics_keys
from deckgen.template import find_suite_root
from rag.vault import obsidian_manager
# The one documented docgen→refactor edge (mirrors deck.py): the resolver's
# cached basename→paths index backs figure resolution without a per-call rglob.
from core.vault_resolver import get_file_index

logger = logging.getLogger(__name__)

# Input caps (mirror deck.py).
PATH_MAX = 4096
TEMPLATE_MAX_BYTES = 1_000_000
TEMPLATE_EXTS = (".tex", ".sty")
FIGURE_MAX_BYTES = 20 * 1024 * 1024

# Absolute prefixes we refuse to scaffold into (system locations). Narrow by
# design: the vault, home, and temp dirs must stay valid targets.
DENY_PREFIXES = ("/System", "/usr", "/bin", "/sbin", "/etc", "/private/etc")


def _under_deny_prefix(expanded: str) -> bool:
    """True iff *expanded* (an absolute realpath) is or is under a denied prefix.

    B10/#27 (2026-07-13): case-insensitive on darwin. The default macOS APFS
    volume is case-insensitive and ``os.path.realpath`` does not canonicalise
    case, so ``/SYSTEM/x`` resolved past a case-sensitive ``startswith``. Defence
    in depth (SIP-sealed dirs are read-only), but it aligns with
    ``resolve_under_root``'s fail-safe direction.
    """
    hay = expanded.lower() if sys.platform == "darwin" else expanded
    prefixes = [p.lower() for p in DENY_PREFIXES] if sys.platform == "darwin" else DENY_PREFIXES
    return any(hay == p or hay.startswith(p + os.sep) for p in prefixes)

# Index states (mirror deck.py) for the pre-generation advisory.
USABLE_STATES = {"done", "paused_partial"}
IN_PROGRESS_STATES = {"running", "scanning", "embedding", "paused", "paused_scan"}


def resolve_template_path(raw) -> str | None:
    """Coerce/validate a template path: absolute, existing ``.tex``/``.sty`` file."""
    s = coerce_string_max_len(raw, PATH_MAX)
    if not s or any(ch in s for ch in ("\x00", "\n", "\r")):
        return None
    try:
        # The INPUT must already be absolute (``~`` expansion aside):
        # ``realpath`` anchors a relative path to the SERVER's process
        # CWD, so the caller would get a location it never named. Same
        # guard as deck.py::_resolve_out_dir (P8.2, 2026-07-30) — this
        # twin was missed because the parity ratchet only asserted the
        # relative-input rule for that one validator.
        user_expanded = os.path.expanduser(s)
        if not os.path.isabs(user_expanded):
            return None
        expanded = os.path.realpath(user_expanded)
    except (OSError, ValueError):
        return None
    if not os.path.isabs(expanded) or not os.path.isfile(expanded):
        return None
    if os.path.splitext(expanded)[1].lower() not in TEMPLATE_EXTS:
        return None
    return expanded


def resolve_out_dir(raw) -> str | None:
    """Coerce/validate an output dir: absolute, existing, not a system location."""
    s = coerce_string_max_len(raw, PATH_MAX)
    if not s or any(ch in s for ch in ("\x00", "\n", "\r")):
        return None
    try:
        # The INPUT must already be absolute (``~`` expansion aside):
        # ``realpath`` anchors a relative path to the SERVER's process
        # CWD, so the caller would get a location it never named. Same
        # guard as deck.py::_resolve_out_dir (P8.2, 2026-07-30) — this
        # twin was missed because the parity ratchet only asserted the
        # relative-input rule for that one validator.
        user_expanded = os.path.expanduser(s)
        if not os.path.isabs(user_expanded):
            return None
        expanded = os.path.realpath(user_expanded)
    except (OSError, ValueError):
        return None
    if not os.path.isabs(expanded) or not os.path.isdir(expanded):
        return None
    if expanded == os.sep or _under_deny_prefix(expanded):
        return None
    return expanded


# The only engines latexmk may be invoked with. Re-validated at CALL time
# because the engine reaches argv as f"-{engine}" (option-injection guard),
# mirroring deck.py::_COMPILE_ENGINES.
COMPILE_ENGINES = ("pdflatex", "lualatex", "xelatex")


def resolve_existing_doc(raw) -> str | None:
    """Validate an existing document path: absolute ``.tex`` file, not a system
    location (mirror deck.py::_resolve_existing_deck). Used by compile/preview
    which read — and, for compile-fix, may rewrite — the file."""
    s = coerce_string_max_len(raw, PATH_MAX)
    if not s or any(ch in s for ch in ("\x00", "\n", "\r")):
        return None
    try:
        # The INPUT must already be absolute (``~`` expansion aside):
        # ``realpath`` anchors a relative path to the SERVER's process
        # CWD, so the caller would get a location it never named. Same
        # guard as deck.py::_resolve_out_dir (P8.2, 2026-07-30) — this
        # twin was missed because the parity ratchet only asserted the
        # relative-input rule for that one validator.
        user_expanded = os.path.expanduser(s)
        if not os.path.isabs(user_expanded):
            return None
        expanded = os.path.realpath(user_expanded)
    except (OSError, ValueError):
        return None
    if not os.path.isabs(expanded) or not os.path.isfile(expanded):
        return None
    if os.path.splitext(expanded)[1].lower() != ".tex":
        return None
    if expanded == os.sep or _under_deny_prefix(expanded):
        return None
    return expanded


def resolve_doc_pdf(raw) -> str | None:
    """Validate *raw* as a document ``.tex`` and return its compiled sibling PDF.

    Returns the realpath'd ``<base>.pdf`` only when it exists AND stays outside
    ``DENY_PREFIXES`` (the sibling could be a symlink into a system dir). Returns
    ``""`` for a valid ``.tex`` with no PDF yet (caller distinguishes 404 from
    400), or ``None`` for an invalid path. Mirrors deck.py::_resolve_deck_pdf.
    """
    doc_path = resolve_existing_doc(raw)
    if not doc_path:
        return None
    pdf_path = os.path.realpath(os.path.splitext(doc_path)[0] + ".pdf")
    if not os.path.isfile(pdf_path):
        return ""
    if pdf_path == os.sep or _under_deny_prefix(pdf_path):
        return None
    return pdf_path


def read_template_file(path: str) -> str:
    """Read at most ``TEMPLATE_MAX_BYTES`` of a template (read-only; lossy decode)."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read(TEMPLATE_MAX_BYTES)


class DocReadError(Exception):
    """A document to be overwritten could not be read safely (too large / not UTF-8 / IO)."""


def read_doc_strict(path: str) -> tuple[str, str]:
    """Read a document we may overwrite: return ``(text, raw_sha256)`` or raise.

    Mirror of deck.py::_read_deck_strict — strict UTF-8 (a non-UTF-8 file is
    refused, not corrupted on the round-trip), a hard 1 MB cap (an over-cap file
    is refused, not truncated), and the sha256 is of the RAW bytes so the
    stale-diff token matches :func:`file_sha256`. Raises :class:`DocReadError`.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read(TEMPLATE_MAX_BYTES + 1)
    except OSError as exc:
        raise DocReadError(f"Could not read the document ({type(exc).__name__}).") from exc
    if len(raw) > TEMPLATE_MAX_BYTES:
        raise DocReadError("The document is larger than 1 MB — too large to augment safely.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocReadError("The document is not valid UTF-8; augment supports UTF-8 only.") from exc
    return text, hashlib.sha256(raw).hexdigest()


_LATEX_GLOBAL_CAP = 2

#: How long silence proves a lease's owner is dead.
#:
#: PERF (2026-09-02): the lease had NO TTL at all — release fired only when the
#: worker exited, and with `local_request_timeout_s` defaulting to 0 a worker
#: wedged in `client.chat()` never returns, so one lease AND one of the two
#: global slots were consumed until the process restarted. `RagOperationLock`
#: exists precisely because a zombie must not keep a lock forever, and this is
#: its model: an epoch bumped on every acquire, passive expiry at the TTL, and
#: a heartbeat that pushes the deadline out for anyone still alive.
#:
#: 1800 s is chosen against the longest honest SILENT stage, not the longest
#: run — a live run heartbeats. The widest stall window in the file is
#: compile-fix's (2 x `deck_compile_timeout_s`, ≤ 1230 s with the margin), and
#: every SSE worker emits an `{"info"}` frame per stage, which is what beats.
_LATEX_LEASE_TTL_S = 1800.0


@dataclass
class _PathLockEntry:
    lock: threading.Lock
    references: int = 0
    #: Bumped on every acquire; the anti-zombie token. A lease carrying an old
    #: epoch can neither heartbeat nor release the current acquisition.
    epoch: int = 0
    #: Monotonic time of the last acquire/heartbeat; None = free.
    holder_since: float | None = None
    ttl: float = 0.0
    #: Whether a global slot is currently charged to this entry. A STEAL
    #: inherits it rather than taking a second one — otherwise two steals would
    #: exhaust the semaphore permanently, which is the same leak one level down.
    slot_held: bool = False


class _LatexLease:
    """Exactly-once release handle safe to pass to the SSE lifecycle guard.

    Carries the epoch of ITS acquisition: after a TTL steal, ``release`` and
    ``heartbeat`` become no-ops and :attr:`valid` goes False, so a zombie can
    neither free nor extend the lock a newer holder now owns.
    """

    def __init__(self, registry, key: str, entry: _PathLockEntry,
                 epoch: int) -> None:
        self._registry = registry
        self._key = key
        self._entry = entry
        self._epoch = epoch
        self._released = False
        self._release_guard = threading.Lock()

    @property
    def valid(self) -> bool:
        """False once this acquisition has been released or stolen."""
        return (not self._released
                and self._registry._epoch_of(self._key) == self._epoch)

    def heartbeat(self) -> bool:
        """Push the expiry out. False if this acquisition is no longer current."""
        return self._registry._heartbeat(self._key, self._entry, self._epoch)

    def release(self) -> None:
        with self._release_guard:
            if self._released:
                return
            self._released = True
        self._registry._release(self._key, self._entry, self._epoch)


class LatexPathLockRegistry:
    """Canonical-path serialization plus a bounded cross-path operation cap.

    State/lock invariant: deck and docgen resolve to one canonical key, so every
    read→stale-check→backup→write sequence for the same file remains mutually
    exclusive across both route families. Distinct files may proceed together,
    but the bounded semaphore caps aggregate compile/model/file work at two.
    Path-lock acquisition precedes the global slot so a same-path contender can
    never consume capacity while waiting. Non-blocking acquisition preserves the
    routes' 409 behavior.

    Cancellation and partial failure release through an exactly-once lease: SSE
    disconnect/error paths hand ``lease.release`` to the shared stream guard and
    synchronous routes use ``finally``. Reference counts cover owners plus
    contenders, allowing entries to be removed as soon as the final reference
    leaves without racing a new acquirer. This registry is process-local like the
    lock it replaces and changes no persisted state, backup, or migration format.
    The optimization permits unrelated files to progress while preserving every
    same-file recovery/stale-diff invariant.

    Since 2026-09-02 ownership is EPOCH + TTL, ``RagOperationLock``'s model,
    rather than a bare ``threading.Lock``: a lease that has neither released nor
    heartbeated within ``_LATEX_LEASE_TTL_S`` is stolen, the stealer INHERITS
    its global slot, and the victim's later ``release``/``heartbeat`` no-op.
    ``run_sse_worker`` beats on every frame a worker emits and sets the worker's
    cancel event when a beat is refused, so every existing
    ``if cancel.is_set(): return`` gate before a write covers the steal case too
    — the zombie-writer rule is enforced through the mechanism already there.
    """

    def __init__(self, global_cap: int = _LATEX_GLOBAL_CAP) -> None:
        self._guard = threading.Lock()
        self._entries: dict[str, _PathLockEntry] = {}
        self._slots = threading.BoundedSemaphore(global_cap)

    @staticmethod
    def canonical_key(path: str | os.PathLike[str]) -> str:
        return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))

    def acquire(
        self, path: str | os.PathLike[str], *, blocking: bool = False,
        ttl_s: float = _LATEX_LEASE_TTL_S,
    ) -> _LatexLease | None:
        key = self.canonical_key(path)
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = _PathLockEntry(threading.Lock())
                self._entries[key] = entry
            now = time.monotonic()
            if (entry.holder_since is not None
                    and (now - entry.holder_since) < entry.ttl):
                return None                      # a LIVE holder — the 409 path
            # Free, or the previous holder's TTL lapsed: take it and bump the
            # epoch, which invalidates the dead worker's lease token.
            inherit_slot = entry.slot_held
            entry.epoch += 1
            entry.holder_since = now
            entry.ttl = ttl_s
            entry.references += 1
            epoch = entry.epoch
        if not inherit_slot:
            if not self._slots.acquire(blocking=blocking):
                with self._guard:
                    if entry.epoch == epoch:
                        entry.holder_since = None
                        entry.ttl = 0.0
                self._drop_reference(key, entry)
                return None
            with self._guard:
                entry.slot_held = True
        return _LatexLease(self, key, entry, epoch)

    def _epoch_of(self, key: str) -> int:
        with self._guard:
            entry = self._entries.get(key)
            return entry.epoch if entry is not None else -1

    def _heartbeat(self, key: str, entry: _PathLockEntry, epoch: int) -> bool:
        with self._guard:
            if entry.holder_since is None or entry.epoch != epoch:
                return False
            entry.holder_since = time.monotonic()
            return True

    def _release(self, key: str, entry: _PathLockEntry, epoch: int) -> None:
        with self._guard:
            stale = entry.epoch != epoch
            if not stale:
                entry.holder_since = None
                entry.ttl = 0.0
                free_slot = entry.slot_held
                entry.slot_held = False
            else:
                # Stolen: the CURRENT holder owns both the path and the slot
                # this lease was charged. Releasing either here would hand a
                # live operation's lock away and over-release the semaphore.
                free_slot = False
        if free_slot:
            self._slots.release()
        self._drop_reference(key, entry)

    def _drop_reference(self, key: str, entry: _PathLockEntry) -> None:
        with self._guard:
            entry.references -= 1
            if (entry.references <= 0 and entry.holder_since is None
                    and self._entries.get(key) is entry):
                del self._entries[key]

    def active_key_count(self) -> int:
        """Live entry count — a diagnostic/test seam, not a routing input.

        Reference-counted entry removal is the part of this registry most likely
        to leak, and it is invisible from the outside; exposing the count lets
        the lifecycle tests assert it rather than reach into ``_entries``.
        """
        with self._guard:
            return len(self._entries)


# Shared by deck and docgen. Keeping the historical exported name preserves a
# single cross-blueprint authority while its value is now a registry, not a
# process-global mutex.
LATEX_OP_LOCK = LatexPathLockRegistry()


def preserve_original_once(doc_path: str) -> str | None:
    """Write a NON-CLOBBERING ``<doc>.tex.orig`` copy on the first overwrite.

    The rolling ``.tex.bak`` is one-step recovery only: the second apply
    overwrites it with the post-first-apply state, permanently destroying a
    hand-written original (2026-07-20 audit M1). This sibling keeps the
    "first backup = true original" convention the audit's tag-drift write
    documents. Returns the path when the copy was made, ``None`` when it
    already existed. Raises ``OSError`` like ``backup_doc`` — a caller must
    abort the overwrite if no recovery copy could be secured.
    """
    orig = doc_path + ".orig"
    if os.path.exists(orig):
        return None
    shutil.copyfile(doc_path, orig)
    return orig


def backup_doc(doc_path: str) -> str:
    """Copy the on-disk document to ``<doc>.tex.bak`` before overwriting; return the
    backup path. Raises ``OSError`` so the caller ABORTS rather than destroy the
    original without a recovery copy (byte-exact copyfile). Also writes the
    one-time ``<doc>.tex.orig`` via :func:`preserve_original_once` first."""
    preserve_original_once(doc_path)
    bak = doc_path + ".bak"
    shutil.copyfile(doc_path, bak)
    return bak


def file_sha256(path: str) -> str:
    """sha256 of a file's raw bytes, or ``""`` if unreadable (stale-diff token)."""
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


def provenance_comment(
    kind: str, provider: str, model: str, *, disclosure: bool = False
) -> str:
    """LaTeX comment block stamping the generating model into an emitted doc.

    Export provenance (2026-07-22): there is no settled standard for text
    provenance, so this is the app's own small convention — a comment header
    (never rendered) naming the provider/model + generation date, plus an
    optional commented-out ICMJE/Elsevier-style disclosure snippet for the
    article kind (academic venues increasingly require disclosing generative-AI
    assistance). Pure string builder; the caller prepends it to the assembled
    ``.tex`` BEFORE validation/review so every sha/screen sees the final text.
    """
    from datetime import date

    label = "deckgen" if kind == "deck" else "docgen"
    lines = [
        f"% Generated by CHERQAiN ({label}) — provider: {provider}, "
        f"model: {model}, date: {date.today().isoformat()}.",
        "% RAG-grounded draft from the user's Obsidian vault — verify content before use.",
    ]
    if disclosure:
        lines += [
            "% Optional AI-use disclosure (ICMJE/Elsevier style) — uncomment and adapt:",
            "% \\section*{Declaration on the use of generative AI}",
            f"% During the preparation of this work the author used {model} "
            "to draft content grounded in their own notes; the author reviewed "
            "and edited the output and takes full responsibility for the "
            "content of this publication.",
        ]
    return "\n".join(lines) + "\n"


def load_paper_index():
    """The paper-label index for the configured vault, or ``None``.

    Loaded once per deck/article operation (``rag/paper_index.py`` caches the
    parse on the file's stat) and handed to ``rewrite_source_tags`` so a PDF or
    note path resolves to its citation key. Absent, stale-vault or unreadable
    ⇒ ``None``, which keeps the citation pass byte-identical to before.
    """
    try:
        from core.config import load_config
        from rag import paper_index

        return paper_index.load(load_config().get("obsidian_vault_path") or "")
    except Exception:  # noqa: BLE001 — an enrichment, never a failure
        logger.debug("Paper index unavailable for the citation pass.",
                     exc_info=True)
        return None


def index_preflight_msgs(in_progress_msg: str, no_index_msg: str) -> list[str]:
    """Index-status advisory emitted before a stream starts (warn, never block)."""
    try:
        state = obsidian_manager.get_status()
    except Exception:
        state = ""
    if state in IN_PROGRESS_STATES:
        return [in_progress_msg]
    if state not in USABLE_STATES:
        return [no_index_msg]
    return []


def resolve_and_copy_figures(
    tex: str, project_dir: str, *, copy: bool = True
) -> tuple[str, set[str], list[str], list[tuple[str, str]]]:
    """Resolve every ``\\includegraphics`` in *tex* and copy vault figures in.

    Doc-class-agnostic (works for article and beamer alike). Mirrors
    ``deck.py::_resolve_and_copy_deck_figures``: a figure already present where
    the compiler looks (project dir, ``figures/``, suite ``common/``) survives;
    else it is copied from the vault into ``<project_dir>/figures/`` (basename
    lookup via the cached resolver index, path-suffix disambiguation on
    collisions); else it is commented out so an invented target never breaks the
    build. With no vault configured, the tex is returned UNCHANGED.

    Returns ``(tex, resolved_basenames, warnings, pending_copies)``. ``copy=False``
    performs no writes and returns the would-be copies as *pending_copies* for a
    write-free preview (item 2.9 parity).
    """
    graphics_keys = extract_graphics_keys(tex)
    if not graphics_keys:
        return tex, set(), [], []

    vault_path = obsidian_manager.get_vault_path()
    if not vault_path:
        return tex, set(), [], []

    resolved: set[str] = set()
    warnings: list[str] = []
    figures_dir = os.path.join(project_dir, "figures")
    suite_root = find_suite_root(os.path.join(project_dir, "doc.tex")) or ""

    # Pass 1 — figures that already exist where the compiler will look survive.
    remaining: dict[str, str] = {}
    for path in graphics_keys:
        basename = os.path.basename(path.strip())
        if not basename or basename in resolved:
            continue
        existing_candidates = [
            path if os.path.isabs(path) else os.path.join(project_dir, path),
            os.path.join(figures_dir, basename),
        ]
        if suite_root:
            existing_candidates.append(os.path.join(suite_root, "common", path))
        if any(os.path.isfile(c) for c in existing_candidates):
            resolved.add(basename)
            continue
        remaining.setdefault(basename, path)

    vault_root = Path(vault_path).resolve()
    name_index, _link_index = get_file_index(vault_root)

    if copy:
        os.makedirs(figures_dir, exist_ok=True)
    pending: list[tuple[str, str]] = []

    for basename, written_path in remaining.items():
        candidates = name_index.get(basename.lower(), [])
        if not candidates:
            warnings.append(
                f"Figure not found in the vault: {basename} — commented out. "
                f"Add the file to figures/ and restore the line to keep it."
            )
            continue
        if len(candidates) > 1:
            written = written_path.replace("\\", "/").lower().lstrip("./")
            suffix_matches = [c for c in candidates if c.lower().endswith(written)]
            if len(suffix_matches) == 1:
                candidates = suffix_matches
            else:
                resolved.add(basename)
                warnings.append(
                    f"Figure {basename}: {len(candidates)} same-named images in the "
                    f"vault and the written path does not disambiguate — left as-is; "
                    f"copy the intended file to figures/ yourself."
                )
                continue

        rel_src = resolve_under_root(
            str(candidates[0]), str(vault_root),
            must_exist=True, must_be_file=True, exts=set(VAULT_IMAGE_EXTS),
        )
        if not rel_src:
            continue
        abs_src = Path(vault_root) / rel_src
        if abs_src.stat().st_size > FIGURE_MAX_BYTES:
            warnings.append(
                f"Figure {basename} exceeds the {FIGURE_MAX_BYTES // (1024 * 1024)} MB "
                f"copy cap — commented out; copy it to figures/ manually to keep it."
            )
            continue

        dest = os.path.join(figures_dir, basename)
        if not copy:
            pending.append((str(abs_src), dest))
            resolved.add(basename)
            continue
        try:
            shutil.copy2(abs_src, dest)
            resolved.add(basename)
        except OSError as exc:
            logger.debug("Failed to copy figure %s to %s", abs_src, dest, exc_info=True)
            warnings.append(f"Figure {basename}: copy from the vault failed ({exc}).")

    return comment_out_missing_graphics(tex, resolved), resolved, warnings, pending
