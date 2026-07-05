"""Deck Generator window — turn the vault into a Beamer ``.tex`` deck.

This blueprint is the in-app face of ``deckgen``: it loads a user-supplied
Beamer template (which the user may edit in the app before generating), drives
the deckgen orchestration in-process via
:class:`deckgen.inprocess.InProcessChatRunner`, and scaffolds the result into a
``<slug>/`` project folder under the user's LaTeX suite (emit-only — the user
compiles with ``make``).

Streaming uses the same SSE contract as ``/api/obsidian/chat`` (``info`` /
``error`` / agent-trace frames), plus a terminal ``{"deck": {...}}`` frame that
carries the assembled ``.tex``, validation warnings and the written paths.
"""
import hashlib
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path

from flask import Blueprint, jsonify, request

from api.security import sanitise_error_msg
from api.sse import run_sse_worker
from api.validators import (
    coerce_bool,
    coerce_enum,
    coerce_float_in_range,
    coerce_int_in_range,
    coerce_non_empty_string,
    coerce_string_max_len,
)
from core.config import load_config, resolve_chat_model
from core.paths import resolve_under_root
from core.llm.chat import stream_chat_messages
from core.constants import (
    BASE_DIR,
    DEFAULT_EMBED,
    VAULT_IMAGE_EXTS,
    # Shared SSE stall-guard timing (single source of truth in core/constants.py;
    # aliased to the historical private names used below). Rationale at the
    # definition site.
    SSE_STALL_MARGIN_S as _STALL_MARGIN_S,
    SSE_SINGLE_SHOT_FLOOR_S as _SINGLE_SHOT_FLOOR_S,
)
from core.utils import write_text_atomic
from rag.vault import obsidian_manager
# The one documented deck→refactor edge: the resolver's cached vault file
# index (basename -> vault-relative paths) backs deck figure resolution; a
# fresh per-call rglob of the whole vault is exactly what the cache exists
# to avoid.
from refactor.resolver import get_file_index

from deckgen import checkpoint
from deckgen.assemble import (
    assemble_with_template,
    sanitize_section,
    validate,
    comment_out_missing_graphics,
    extract_graphics_keys,
)
from deckgen.augment import (
    AUGMENT_MAX_SOURCE_CHARS,
    AugmentError,
    deck_counts,
    insert_section,
    replace_region,
    replace_section,
    split_deck,
)
from deckgen.inprocess import InProcessChatRunner
from deckgen.outline import OutlineError, request_outline
from deckgen.prompts import augment_system_prompt, build_augment_message
from deckgen.review import (
    REVIEW_SYSTEM_PROMPT,
    REVIEW_TEMPERATURE,
    build_review_messages,
    parse_review,
    screen_repair,
)
from deckgen.scaffold import (
    ScaffoldError,
    scaffold_deck,
    slugify,
    write_deck_at,
    write_deck_tex,
)
from deckgen.sections import generate_section
from deckgen.template import (
    TemplateError,
    bib_candidates_block,
    find_suite_root,
    load_template_parts,
    macro_cheatsheet,
    relevant_bib_keys,
)
from deckgen.compile import (
    COMPILE_REPAIR_SYSTEM_PROMPT,
    build_repair_messages,
    compile_latex,
    find_latexmk,
    is_missing_file_error,
    parse_latex_log,
)

deck_bp = Blueprint("deck", __name__)

logger = logging.getLogger(__name__)

# Index states in which a usable index exists on disk (mirror deckgen CLI).
_USABLE_STATES = {"done", "paused_partial"}
_IN_PROGRESS_STATES = {"running", "scanning", "embedding", "paused", "paused_scan"}

# Input caps.
_TEMPLATE_MAX_BYTES = 1_000_000          # a template/preamble the user may edit
_TOPIC_MAX = 500
_INSTRUCTIONS_MAX = 8_000
_AUDIENCE_MAX = 200
_DECK_NAME_MAX = 120
_PATH_MAX = 4096
_TEMPLATE_EXTS = (".tex", ".sty")

_MAX_SECTIONS_MIN, _MAX_SECTIONS_MAX = 1, 20
_AGENT_ITER_MIN, _AGENT_ITER_MAX = 1, 12
_TEMP_MIN, _TEMP_MAX = 0.0, 2.0

# Augment (deepen / extend an existing deck) input bounds.
_INSTRUCTION_MAX = 4_000
_AUGMENT_OPS = ("deepen", "table", "new_section")
_AUGMENT_SCOPES = ("whole", "section")
_SECTION_INDEX_MAX = 199  # a deck with more sections than this is pathological

# Absolute prefixes we refuse to scaffold into (system locations). Kept narrow:
# the vault, the user's home, and temp dirs (under /private/var on macOS) must
# all remain valid targets. Defence-in-depth only — the slug can't escape and we
# only ever create a new subdir + two files, never delete.
_DENY_PREFIXES = (
    "/System", "/usr", "/bin", "/sbin", "/etc", "/private/etc",
)

_CHAT_TOKEN_TIMEOUT_S = 300  # default per-turn wall-clock cap (config: agent_wall_clock_s)
# _STALL_MARGIN_S / _SINGLE_SHOT_FLOOR_S are imported from core/constants.py so
# the vault, deck, and plain-chat SSE routes share one stall model. Each deck
# turn is still bounded by turn_timeout_s inside the agent loop; the floor only
# guards total event silence (e.g. a hung local call when local_request_timeout_s
# is 0). Full rationale at the definition site.


def _resolve_template_path(raw) -> str | None:
    """Coerce/validate a template path: absolute, existing ``.tex``/``.sty`` file."""
    s = coerce_string_max_len(raw, _PATH_MAX)
    if not s:
        return None
    if any(ch in s for ch in ("\x00", "\n", "\r")):
        return None
    try:
        expanded = os.path.realpath(os.path.expanduser(s))
    except (OSError, ValueError):
        return None
    if not os.path.isabs(expanded) or not os.path.isfile(expanded):
        return None
    if os.path.splitext(expanded)[1].lower() not in _TEMPLATE_EXTS:
        return None
    return expanded


def _resolve_out_dir(raw) -> str | None:
    """Coerce/validate an output dir: absolute, existing, not a system location."""
    s = coerce_string_max_len(raw, _PATH_MAX)
    if not s:
        return None
    if any(ch in s for ch in ("\x00", "\n", "\r")):
        return None
    try:
        expanded = os.path.realpath(os.path.expanduser(s))
    except (OSError, ValueError):
        return None
    if not os.path.isabs(expanded) or not os.path.isdir(expanded):
        return None
    if expanded == os.sep or any(
        expanded == p or expanded.startswith(p + os.sep) for p in _DENY_PREFIXES
    ):
        return None
    return expanded


def _resolve_existing_deck(raw) -> str | None:
    """Coerce/validate an existing deck path: absolute ``.tex`` file, not a system
    location.

    Unlike :func:`_resolve_template_path` (read-only, accepts ``.tex``/``.sty``),
    the augment path can *write back* to this file, so it applies the same
    ``_DENY_PREFIXES`` system-location guard ``_resolve_out_dir`` uses — reading a
    system ``.tex`` to augment it is never legitimate either.
    """
    s = coerce_string_max_len(raw, _PATH_MAX)
    if not s:
        return None
    if any(ch in s for ch in ("\x00", "\n", "\r")):
        return None
    try:
        expanded = os.path.realpath(os.path.expanduser(s))
    except (OSError, ValueError):
        return None
    if not os.path.isabs(expanded) or not os.path.isfile(expanded):
        return None
    if os.path.splitext(expanded)[1].lower() != ".tex":
        return None
    if expanded == os.sep or any(
        expanded == p or expanded.startswith(p + os.sep) for p in _DENY_PREFIXES
    ):
        return None
    return expanded


def _read_template_file(path: str) -> str:
    """Read at most ``_TEMPLATE_MAX_BYTES`` of a template file.

    Uses ``errors="replace"`` so a stray non-UTF-8 byte in a user template
    cannot raise, and a hard read cap so an enormous file cannot exhaust memory.
    This is for *templates* (read-only, never written back). Decks that augment
    can overwrite go through :func:`_read_deck_strict` instead.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read(_TEMPLATE_MAX_BYTES)


def _file_sha256(path: str) -> str:
    """sha256 of a file's *raw bytes*, or ``""`` if unreadable.

    The stale-diff token convention shared by apply-repair and apply-augment: a
    hash of the exact on-disk bytes (not decoded text), so it detects ANY change —
    including a byte edit that a lossy ``errors="replace"`` decode would mask.
    """
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


# Vault images above this size are never copied into a deck project — a deck
# is meant to be a portable, compile-anywhere folder, and silently duplicating
# a 100 MB scan per generate would bloat the suite (the skip is surfaced as a
# warning, never silent).
_FIGURE_MAX_BYTES = 20 * 1024 * 1024


def _resolve_and_copy_deck_figures(
    tex: str, project_dir: str, *, copy: bool = True
) -> tuple[str, set[str], list[str], list[tuple[str, str]]]:
    """Resolve every ``\\includegraphics`` in *tex* and copy vault figures in.

    For each referenced figure: keep it if the file already exists where the
    compiler will look (raw path under *project_dir*, ``figures/<basename>``,
    or the suite's ``common/`` — a hand-written deck's local/suite figures are
    NOT ours to break, and an existing copy is never clobbered); else copy it
    from the vault into ``<project_dir>/figures/`` (basename lookup via the
    refactor resolver's cached index, path-suffix disambiguation on
    collisions); else comment the command out
    (:func:`comment_out_missing_graphics`) so an invented target never breaks
    compilation.

    Returns ``(tex, resolved_basenames, warnings, pending_copies)`` —
    *resolved* feeds ``validate(copied_figures=…)`` and *warnings* surface
    every skip (ambiguous name, oversize, copy failure): a dropped figure the
    user never hears about reads as "covered" when it wasn't.

    ``copy=False`` (improvement plan 2026-07-04, item 2.9 — the augment
    PREVIEW path): resolution decisions and the tex rewrite are identical,
    but nothing is copied — the would-be copies are returned as
    ``pending_copies`` ``[(src_abs, dst_abs), …]`` for the caller to stage
    and perform at APPLY time. The preview used to copy figures into the
    user's deck folder despite its documented "writes nothing" contract.
    With ``copy=True`` (generate, compile-fix) ``pending_copies`` is empty.

    With no vault configured the tex is returned UNCHANGED — the pre-fix
    behaviour commented out every figure of any deck, destroying hand-written
    decks whose figures the vault knows nothing about.
    """
    graphics_keys = extract_graphics_keys(tex)
    if not graphics_keys:
        return tex, set(), [], []

    vault_path = obsidian_manager.get_vault_path()
    if not vault_path:
        # No vault ⇒ nothing to resolve against ⇒ NOTHING is touched. The
        # pre-fix behaviour commented out every figure of any deck here,
        # destroying hand-written decks whose figures compile fine via
        # TEXINPUTS/local paths the vault knows nothing about.
        return tex, set(), [], []

    resolved: set[str] = set()
    warnings: list[str] = []

    figures_dir = os.path.join(project_dir, "figures")
    # A suite deck may resolve figures from <suite>/common at compile time
    # (the same TEXINPUTS the Makefile and compile-fix use) — those are not
    # ours to comment out either.
    suite_root = find_suite_root(os.path.join(project_dir, "deck.tex")) or ""

    # Pass 1 — figures that already exist where the compiler will look
    # (deck dir, figures/, suite common/) survive untouched.
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
    # Cached (vault path, exclusions) index — the previous per-call rglob of
    # the whole vault ran once per generate AND once per compile-fix repair
    # iteration. name_index maps basename.lower() -> [vault-relative paths].
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
            # Same basename in several vault folders: the path as written in
            # the deck ("attachments/brain.png") disambiguates when it is a
            # suffix of exactly one candidate; otherwise we must not guess —
            # keep the command and tell the user.
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
            must_exist=True, must_be_file=True, exts=VAULT_IMAGE_EXTS
        )
        if not rel_src:
            continue
        abs_src = Path(vault_root) / rel_src
        if abs_src.stat().st_size > _FIGURE_MAX_BYTES:
            warnings.append(
                f"Figure {basename} exceeds the {_FIGURE_MAX_BYTES // (1024 * 1024)} MB "
                f"copy cap — commented out; copy it to figures/ manually to keep it."
            )
            continue

        # Pass 1 already resolved any existing figures/<basename>, so this
        # only ever writes fresh copies (a same-name race just re-copies).
        dest = os.path.join(figures_dir, basename)
        if not copy:
            # Preview mode: record the copy for apply-time and treat the
            # figure as resolved so the tex rewrite matches what apply will
            # make true (same decision path, zero writes).
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


class _DeckReadError(Exception):
    """A deck-to-be-written could not be read safely (too large / not UTF-8 / IO)."""


def _read_deck_strict(path: str) -> tuple[str, str]:
    """Read a deck we may overwrite: return ``(text, raw_sha256)`` or raise.

    Unlike :func:`_read_template_file` (lossy ``errors="replace"``, for read-only
    templates), augment can write this file BACK, so a lossy decode would silently
    corrupt non-UTF-8 bytes to U+FFFD on the round-trip. We therefore:

    * read raw bytes with a hard cap and **reject** an over-cap file with a clear
      message instead of silently truncating it (a truncated deck would lose its
      ``\\end{document}`` and parse as malformed);
    * **strict**-decode UTF-8 and reject a non-UTF-8 deck up front, rather than
      mangling it on write (older latin1 decks are refused, not corrupted);
    * return the sha256 of the *raw bytes*, so the stale-diff token matches
      :func:`_file_sha256` exactly (one hashing convention across both apply paths).

    Raises :class:`_DeckReadError` (caught by the routes → 400) on any of these.
    """
    try:
        # Read one byte past the cap so we can DETECT an over-cap file rather than
        # silently keep its first cap bytes.
        with open(path, "rb") as fh:
            raw = fh.read(_TEMPLATE_MAX_BYTES + 1)
    except OSError as exc:
        raise _DeckReadError(f"Could not read the deck ({type(exc).__name__}).") from exc
    if len(raw) > _TEMPLATE_MAX_BYTES:
        raise _DeckReadError(
            "The deck is larger than 1 MB — too large to augment safely."
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _DeckReadError(
            "The deck is not valid UTF-8; augment supports UTF-8 decks only."
        ) from exc
    return text, hashlib.sha256(raw).hexdigest()


# --- Augment staging (server-owned proposal cache) --------------------------
#
# apply-augment must NOT trust a client-supplied .tex body (a page could submit
# arbitrary content that merely passes the structural screen). Instead, the
# augment preview stages the screened proposal server-side and apply reads it from
# here — the client only ever passes hashes / the deck path. Mirrors
# refactor/staging.py's "server owns the bytes" model. The cache lives in the app
# data dir (NOT beside the user's deck), keyed by the deck's realpath.

_STAGING_DIRNAME = os.path.join("deckgen", "staging")
_CHECKPOINTS_DIRNAME = os.path.join("deckgen", "checkpoints")
# Abandoned augment previews (staged-but-never-applied) self-reclaim after this.
# Mirrors refactor/staging.py::_STAGING_TTL_S so the two staging caches share one
# disk-hygiene policy.
_STAGING_TTL_S = 7 * 24 * 3600


def _checkpoints_dir() -> str:
    """Per-section deck-generation checkpoints (NOT beside the user's deck)."""
    return os.path.join(BASE_DIR, _CHECKPOINTS_DIRNAME)


def _staging_dir() -> str:
    d = os.path.join(BASE_DIR, _STAGING_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def _staging_path(deck_path: str) -> str:
    key = hashlib.sha256(os.path.realpath(deck_path).encode("utf-8")).hexdigest()
    return os.path.join(_staging_dir(), f"{key}.json")


def _proposed_digest(text: str) -> str:
    """sha256 of a proposal's text bytes — the staged-proposal integrity check.

    This hashes our OWN generated text (the proposal), not on-disk deck bytes, so a
    decoded-text hash is correct here; the on-disk stale-diff token stays raw-byte
    (:func:`_file_sha256`)."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _sweep_staging() -> None:
    """Best-effort: delete staged proposals older than ``_STAGING_TTL_S``.

    An augment preview that is never applied otherwise leaks its JSON forever
    (the only deleter is ``_clear_stage`` on a successful apply). Mirrors
    refactor/staging.py::_sweep_expired. Never raises.
    """
    cutoff = time.time() - _STAGING_TTL_S
    try:
        d = _staging_dir()
        for name in os.listdir(d):
            if not name.endswith(".json"):
                continue
            p = os.path.join(d, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.unlink(p)
            except OSError:
                pass
    except OSError:
        pass


def _stage_augment(deck_path: str, base_sha256: str, proposed_tex: str,
                   pending_figures: list[tuple[str, str]] | None = None) -> None:
    """Persist a screened augmentation proposal for apply to read back.

    ``pending_figures`` (item 2.9): the figure copies the write-free preview
    deferred — performed by apply-augment after the backup, so the preview's
    "writes nothing" contract holds while the applied deck still compiles.
    """
    _sweep_staging()  # opportunistic disk-hygiene on the write path
    record = {
        "deck_path": os.path.realpath(deck_path),
        "base_sha256": base_sha256,
        "proposed_tex": proposed_tex,
        "proposed_sha256": _proposed_digest(proposed_tex),
        "pending_figures": [list(pair) for pair in (pending_figures or [])],
    }
    write_text_atomic(_staging_path(deck_path), json.dumps(record))


def _load_stage(deck_path: str) -> dict | None:
    try:
        with open(_staging_path(deck_path), "r", encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _clear_stage(deck_path: str) -> None:
    try:
        os.unlink(_staging_path(deck_path))
    except OSError:
        pass


def _backup_deck(deck_path: str) -> str:
    """Copy the current on-disk deck to ``<deck>.tex.bak`` before overwriting.

    Returns the backup path. Raises ``OSError`` on failure so the caller ABORTS the
    overwrite rather than destroying the user's original without a backup — augment
    can target a hand-written deck, so the overwrite must always be recoverable.
    ``copyfile`` is byte-exact (works even for the rare non-UTF-8 deck, though
    augment refuses those upstream).
    """
    bak = deck_path + ".bak"
    shutil.copyfile(deck_path, bak)
    return bak


# --- Deck-operation mutual exclusion ----------------------------------------
#
# generate and augment each drive a heavy in-process agent loop against the single
# obsidian_manager / local model. Allowing two at once (e.g. a user starts an
# augment while a generate streams) would contend on the model and double the load
# for no benefit. A non-blocking try-acquire here lets exactly one run at a time;
# the loser gets a clean 409 instead of silently competing.
_DECK_OP_LOCK = threading.Lock()


def _index_preflight_msgs(in_progress_msg: str, no_index_msg: str) -> list[str]:
    """Index-status advisory emitted before a deck stream starts (warn, never block)."""
    try:
        state = obsidian_manager.get_status()
    except Exception:
        state = ""
    if state in _IN_PROGRESS_STATES:
        return [in_progress_msg]
    if state not in _USABLE_STATES:
        return [no_index_msg]
    return []





def _run_integrity_review(tex, *, provider, model, max_tokens, cfg, cancel, deadline_s=None):
    """Opt-in final-stage ``.tex`` integrity review + auto-repair.

    One RAG-free chat call (``core.llm.chat.stream_chat_messages`` — no vault
    retrieval, no agent loop) over the whole assembled deck, then the pure
    deckgen screening. Returns the ``review`` payload embedded in the terminal
    deck frame: ``{ran, issues, changed, repaired_tex, repaired_warnings,
    truncated, error}``. **Never raises** — any transport/parse failure is
    surfaced in ``error`` so the deck frame is still emitted and the on-disk
    deck is unaffected.

    *deadline_s* (seconds, optional) bounds the streamed review. The local chat
    path has no app-level wall-clock by default (``local_request_timeout_s`` = 0
    leaves the SDK default), so a stuck/slow model could stream past the SSE
    consumer's stall window. We therefore poll a monotonic deadline per token
    and bail with a clear ``error`` once it is exceeded — the deck is already
    scaffolded by the caller, so a timed-out review only forfeits the (optional)
    repair offer, never the deck itself.
    """
    payload = {
        "ran": True, "issues": [], "changed": False, "repaired_tex": "",
        "repaired_warnings": [], "truncated": False, "repair_truncated": False,
        "error": "",
    }
    # Absolute monotonic cutoff; None ⇒ unbounded (the legacy behaviour).
    deadline = (time.monotonic() + deadline_s) if deadline_s else None
    try:
        # Inside the try so the "Never raises" contract holds for the whole body
        # (build_review_messages is pure string-slicing, but keep it defensive).
        messages, truncated = build_review_messages(tex)
        payload["truncated"] = truncated
        chunks = []
        for tok in stream_chat_messages(
            messages=messages,
            system_prompt=REVIEW_SYSTEM_PROMPT,
            provider_name=provider,
            model=model,
            temperature=REVIEW_TEMPERATURE,
            max_tokens=max_tokens,
            cfg=cfg,
            workflow="deck_review",
        ):
            if cancel.is_set():
                return payload
            if deadline is not None and time.monotonic() > deadline:
                # Stop consuming and abandon the (partial) answer: a partial
                # stream may hold half an issue bullet or a truncated repair
                # block, so we discard it rather than parse garbage. The break
                # closes the generator (GeneratorExit unwinds the transport).
                payload["error"] = "integrity review timed out before completing"
                return payload
            chunks.append(tok)
        raw = "".join(chunks)
    except Exception as exc:  # noqa: BLE001 — surface any transport error, redacted
        payload["error"] = sanitise_error_msg(exc)
        return payload

    parsed = parse_review(raw)
    payload["issues"] = parsed.issues
    # A repair built from a truncated INPUT would silently drop the tail the model
    # never saw; a repair whose OUTPUT was cut off by max_tokens is unusable. Either
    # way the issues stand but the auto-repair can't — flag it precisely.
    payload["repair_truncated"] = parsed.repair_truncated
    repaired = None if truncated else parsed.repaired_tex
    accepted, repaired_warnings = screen_repair(tex, repaired)
    payload["repaired_warnings"] = repaired_warnings
    if accepted is not None:
        payload["changed"] = True
        payload["repaired_tex"] = accepted
    elif parsed.issues and (truncated or parsed.repair_truncated):
        # Problems WERE reported but no repair could be produced because the input
        # was clipped at REVIEW_MAX_CHARS (`truncated`) or the corrected document
        # exceeded `max_tokens` and streamed an unterminated fence that parse_review
        # discarded (`repair_truncated`). Say so plainly — and name the knob — so it
        # doesn't read as "nothing fixable".
        payload["repaired_warnings"] = list(repaired_warnings) + [
            "Deck too large for an auto-repair (the corrected document exceeded the "
            "review size/token budget — raise deck_review_max_tokens) — the issues "
            "above stand; apply the fixes manually."
        ]
    return payload


@deck_bp.route("/api/deck/load-template", methods=["POST"])
def api_deck_load_template():
    """Read a template file and report its preamble-derived macros + bib."""
    data = request.get_json(silent=True) or {}
    path = _resolve_template_path(data.get("path"))
    if not path:
        return jsonify({"error": "Invalid or unreadable template path (.tex/.sty)."}), 400
    try:
        tex = _read_template_file(path)
    except OSError as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 400
    try:
        parts = load_template_parts(tex, path)
    except TemplateError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({
        "ok": True,
        "tex": tex,
        "path": path,
        "suite_root": find_suite_root(path) or "",
        "macros": [
            {"signature": m.signature(), "name": m.name, "description": m.description}
            for m in parts.macros
        ],
        "bib_keys_count": len(parts.bib_index),
        "opening_present": bool(parts.opening.strip()),
        "closing_present": bool(parts.closing.strip()),
    })


@deck_bp.route("/api/deck/generate", methods=["POST"])
def api_deck_generate():
    """Generate a deck from the (edited) template and stream progress as SSE."""
    data = request.get_json(silent=True) or {}

    topic = coerce_non_empty_string(data.get("topic"), _TOPIC_MAX)
    if not topic:
        return jsonify({"error": "A topic is required."}), 400
    template_tex = coerce_string_max_len(data.get("template_tex"), _TEMPLATE_MAX_BYTES)
    if not template_tex or not template_tex.strip():
        return jsonify({"error": "A Beamer template is required."}), 400

    instructions = coerce_string_max_len(data.get("instructions"), _INSTRUCTIONS_MAX) or ""
    audience = coerce_string_max_len(data.get("audience"), _AUDIENCE_MAX) or "the audience"

    # Template path is used only to resolve \usepackage{../common/...} and
    # \addbibresource{../_master.bib} and to infer the suite root; the *content*
    # used for generation is the (possibly edited) template_tex from the editor.
    template_path = _resolve_template_path(data.get("template_path")) or ""

    out_dir = _resolve_out_dir(data.get("out_dir"))
    if not out_dir:
        suite_root = find_suite_root(template_path) if template_path else None
        out_dir = _resolve_out_dir(suite_root) if suite_root else None
    if not out_dir:
        return jsonify({
            "error": "Could not determine an output folder. Choose one (it should be "
                     "the suite root that contains your common/ folder)."
        }), 400

    deck_name = coerce_string_max_len(data.get("deck_name"), _DECK_NAME_MAX) or topic
    slug = slugify(deck_name)
    overwrite = bool(coerce_bool(data.get("overwrite")) or False)

    cfg = load_config()
    # Generation knobs: request body wins, then the persisted ``deck_*``
    # defaults (set in the Settings window), then the hard-coded fallback.
    max_sections = (
        coerce_int_in_range(data.get("max_sections"), _MAX_SECTIONS_MIN, _MAX_SECTIONS_MAX)
        or coerce_int_in_range(cfg.get("deck_max_sections"), _MAX_SECTIONS_MIN, _MAX_SECTIONS_MAX)
        or 8
    )
    agent_iters = (
        coerce_int_in_range(data.get("agent_max_iterations"), _AGENT_ITER_MIN, _AGENT_ITER_MAX)
        or coerce_int_in_range(cfg.get("deck_agent_max_iterations"), _AGENT_ITER_MIN, _AGENT_ITER_MAX)
        or 6
    )
    # Temperature may legitimately be 0.0 (falsy), so resolve with an explicit
    # None check rather than ``or``.  When omitted everywhere it stays None and
    # the in-process runner falls back to vault_chat_temperature.
    temperature = coerce_float_in_range(data.get("temperature"), _TEMP_MIN, _TEMP_MAX)
    if temperature is None:
        temperature = coerce_float_in_range(cfg.get("deck_temperature"), _TEMP_MIN, _TEMP_MAX)
    citations_enabled = bool(coerce_bool(data.get("citations_enabled")))
    if data.get("citations_enabled") is None:
        citations_enabled = True  # default on

    # Final-stage .tex integrity review + auto-repair (opt-in). Body wins, then
    # the persisted ``deck_review_enabled`` default (off). The review is one
    # extra RAG-free chat call over the whole assembled deck — gated so it only
    # runs (and only costs) when the user asks for it.
    review_enabled = bool(coerce_bool(data.get("review_enabled")))
    if data.get("review_enabled") is None:
        review_enabled = bool(coerce_bool(cfg.get("deck_review_enabled")))
    review_model = coerce_string_max_len(cfg.get("deck_review_model"), 120) or ""
    review_max_tokens = (
        coerce_int_in_range(cfg.get("deck_review_max_tokens"), 256, 16384) or 4096
    )

    provider = coerce_string_max_len(data.get("provider"), 40) or cfg.get("provider", "ollama")
    model = coerce_string_max_len(data.get("model"), 120) or resolve_chat_model(cfg, provider)
    embed = coerce_string_max_len(data.get("embed"), 120) or cfg.get("embed", DEFAULT_EMBED)
    # Defensive resolve: coerce_int_in_range rejects NaN/Inf/strings and clamps,
    # so a hand-edited config.json can't crash the route or yield a bad deadline.
    wall_clock_s = coerce_int_in_range(cfg.get("agent_wall_clock_s"), 30, 1800) or _CHAT_TOKEN_TIMEOUT_S
    # Floored stall backstop (see _SINGLE_SHOT_FLOOR_S); each turn is still
    # bounded by turn_timeout_s=wall_clock_s inside the in-process runner.
    consumer_timeout_s = max(wall_clock_s, _SINGLE_SHOT_FLOOR_S) + _STALL_MARGIN_S
    # Per-turn resilience: a transient local-backend failure (LM Studio memory
    # hiccup / JIT reload / momentary timeout) is retried in-process before a
    # section degrades to a placeholder. SDK-level retries are off (lms.py), so
    # this is the single, cancel-aware, user-visible retry layer.
    section_attempts = coerce_int_in_range(cfg.get("deck_section_max_attempts"), 1, 5) or 3
    retry_backoff_s = coerce_int_in_range(cfg.get("deck_retry_backoff_s"), 0, 30)
    if retry_backoff_s is None:
        retry_backoff_s = 3
    # Per-section output cap — shorter slides, less RAM pressure on a local
    # backend. Injected as the runner's online_max_tokens (deck turns only).
    section_max_tokens = coerce_int_in_range(cfg.get("deck_section_max_tokens"), 256, 8192) or 2048
    # Resume: re-submitting the SAME request reuses the saved outline + already
    # generated sections (persisted after each section) instead of regenerating
    # everything. ``force_fresh`` (body) ignores + discards any existing
    # checkpoint; ``deck_resume_enabled`` (config, default on) gates the feature.
    resume_enabled = coerce_bool(cfg.get("deck_resume_enabled"))
    if cfg.get("deck_resume_enabled") is None:
        resume_enabled = True
    force_fresh = bool(coerce_bool(data.get("force_fresh")))
    job_key = checkpoint.compute_job_key(
        topic=topic, instructions=instructions, template_tex=template_tex,
        provider=provider, model=model, max_sections=max_sections,
        audience=audience, citations_enabled=citations_enabled,
        slug=slug, out_dir=out_dir,
    )

    def _worker(put, cancel):
        # The slow, blocking deckgen pipeline. Runs in a daemon thread inside
        # run_sse_worker, pushing SSE-shaped dicts via `put`; each agent turn is
        # bounded by turn_timeout_s while the consumer's stall floor is the backstop.
        # run_sse_worker owns the outer error boundary + the terminal DONE sentinel,
        # so expected failures `put({"error": …})` + return; an unexpected exception
        # bubbles to the skeleton (which redacts + surfaces it).
        def _info(text):
            put({"info": text})

        # deckgen passes agent-trace + info/error dicts already in SSE shape; forward
        # them onto the queue. EXCEPT a turn-level ``{"error": …}`` (a per-section
        # provider failure the agent loop emits) is relabelled to a NON-fatal
        # ``{"info": …}``: the shared SSE consumer treats any ``{"error"}`` frame as
        # terminal (cancel + break), which would discard the WHOLE deck for a single
        # weak section — defeating generate_section's placeholder fallback and the
        # per-section retry. Only the worker's explicit ``put({"error"})`` (an
        # unrecoverable outline failure) is meant to be fatal.
        def _on_event(item):
            if isinstance(item, dict) and "error" in item:
                put({"info": f"⚠ {item['error']}"})
            else:
                put(item)

        # 1) Parse the (edited) template.
        try:
            parts = load_template_parts(template_tex, template_path)
        except TemplateError as exc:
            put({"error": str(exc)})
            return

        macros_block = macro_cheatsheet(parts.macros)
        use_bib = citations_enabled and bool(parts.bib_index)
        cite_mode = "bib" if use_bib else "prose"

        runner = InProcessChatRunner(
            cfg=cfg, cancel_event=cancel, turn_timeout_s=wall_clock_s,
            max_tokens=section_max_tokens, workflow="deck_generate",
        )

        # Resume: load a prior checkpoint for this exact request (force_fresh
        # discards it). The manifest holds the parsed outline + every section
        # already generated, so an interrupted run picks up where it stopped.
        ckpt_dir = _checkpoints_dir()
        if force_fresh and resume_enabled:
            checkpoint.delete(ckpt_dir, job_key)
        manifest = checkpoint.load(ckpt_dir, job_key) if (resume_enabled and not force_fresh) else None
        # `reused` counts only genuinely reusable sections — placeholder (failed)
        # sections are NOT counted, NOT reused, and were never persisted (see the
        # generation loop below), so a transiently-failed section is retried on
        # resume rather than baked in. Incremented as each real section is reused.
        reused = 0
        resumable = (
            sum(1 for i in range(1, max_sections + 1)
                if (s := checkpoint.get_section(manifest, i)) is not None and not s.placeholder)
            if manifest else 0
        )

        # 2) Outline — reuse the saved one on resume, else request it.
        if manifest and manifest.get("outline"):
            sections = checkpoint.outline_from_list(manifest["outline"])
            _info(f"Resuming: reusing saved outline ({len(sections)} section(s), "
                  f"{resumable} already generated).")
        else:
            _info(f"Designing outline for “{topic}”…")
            try:
                sections, _ = request_outline(
                    runner,
                    topic=topic,
                    instructions=instructions,
                    provider=provider,
                    model=model,
                    embed=embed,
                    max_iters=agent_iters,
                    temperature=temperature,
                    max_sections=max_sections,
                    on_event=_on_event,
                    max_attempts=section_attempts,
                    retry_backoff_s=retry_backoff_s,
                    should_cancel=cancel.is_set,
                )
            except OutlineError as exc:
                put({"error": str(exc)})
                return
            if cancel.is_set():
                return
            _info(f"Outline ready: {len(sections)} section(s).")
            # Persist the outline immediately so even an outline-then-crash run
            # resumes without re-designing it. Best-effort: a checkpoint-write
            # failure forfeits resume, never the deck.
            if resume_enabled:
                manifest = checkpoint.new_manifest(
                    job_key=job_key, topic=topic, slug=slug, out_dir=out_dir,
                    sections=sections,
                )
                try:
                    checkpoint.save(ckpt_dir, manifest)
                except OSError:
                    manifest = None

        # 3) Per-section generation. A section present in the checkpoint is
        # reused verbatim; a missing one is generated and persisted immediately.
        section_outputs = []
        for i, sec in enumerate(sections, start=1):
            if cancel.is_set():
                return
            saved = checkpoint.get_section(manifest, i) if manifest else None
            # Reuse a saved section ONLY if it carried real content. A placeholder
            # is a recorded *failure*, not a generated section: reusing it would
            # make resume permanently freeze a transiently-failed section (the very
            # case resume exists to recover). The persist guard below stops new
            # placeholders being checkpointed, and this `not saved.placeholder`
            # check also re-attempts any placeholder left by an older build.
            if saved is not None and not saved.placeholder:
                _info(f"Resuming: section {i}/{len(sections)} already generated: {sec.title}")
                section_outputs.append(saved)
                reused += 1
                continue
            _info(f"Writing section {i}/{len(sections)}: {sec.title}")
            candidate_block = ""
            if use_bib:
                seed = sec.title + " " + " ".join(sec.points)
                candidate_block = bib_candidates_block(
                    parts.bib_index, relevant_bib_keys(parts.bib_index, seed)
                )
            out = generate_section(
                runner,
                index=i,
                section=sec,
                full_outline=sections,
                topic=topic,
                instructions=instructions,
                audience=audience,
                provider=provider,
                model=model,
                embed=embed,
                max_iters=agent_iters,
                temperature=temperature,
                macros_block=macros_block,
                cite_mode=cite_mode,
                candidate_bib_block=candidate_block,
                # Only invite \includegraphics when image indexing is on —
                # otherwise no is_image results can exist to ground a figure.
                images_enabled=bool(cfg.get("vault_image_exts")),
                on_event=_on_event,
                max_attempts=section_attempts,
                retry_backoff_s=retry_backoff_s,
                should_cancel=cancel.is_set,
            )
            section_outputs.append(out)
            # Persist ONLY real sections. A placeholder (every retry failed) is left
            # out of the checkpoint so a re-submitted/resumed run regenerates it
            # instead of treating the failure as done. The placeholder still rides
            # in `section_outputs` so THIS run's deck assembles end-to-end.
            if manifest and not out.placeholder:
                checkpoint.set_section(manifest, i, out)
                try:
                    checkpoint.save(ckpt_dir, manifest)
                except OSError:
                    pass  # best-effort; the deck still assembles below

        # Cancel gate BEFORE any write (item 2.2). The section loop above
        # polls cancel per turn, but nothing re-checked it here — so a client
        # disconnect / consumer stall mid-generation still fell through into
        # the figure-copy + scaffold WRITES below. The _DeckOpGuard keeps the
        # deck-op lock held until this worker exits (so a concurrent op can no
        # longer be clobbered), but a cancelled run must also simply not
        # write: the user walked away from it, and every skipped write here
        # is recoverable (the per-section checkpoint persists, so the next
        # identical request resumes instead of regenerating).
        if cancel.is_set():
            return

        # 4) Assemble + validate.
        tex = assemble_with_template(
            section_outputs,
            preamble=parts.preamble,
            opening=parts.opening,
            closing=parts.closing,
        )
        tex, resolved_figs, fig_warnings, _pending = _resolve_and_copy_deck_figures(
            tex, os.path.join(out_dir, slug),
        )
        generated_span = "\n\n".join(
            s.body.strip() for s in section_outputs if s.body.strip()
        )
        # Figure-resolution warnings (ambiguous names, oversize skips, copy
        # failures) ride the same display list as the structural findings.
        warnings = fig_warnings + validate(
            tex,
            generated_tex=generated_span,
            known_bib_keys=parts.bib_keys if use_bib else None,
            copied_figures=resolved_figs,
        )

        n_total = len(section_outputs)
        n_placeholder = sum(1 for s in section_outputs if s.placeholder)

        # 5) Scaffold into <out_dir>/<slug>/ — BEFORE the optional integrity
        # review. The review is advisory/preview-only, so the deck the user already
        # paid for (tokens + wall-clock) must land on disk first; a slow/stalled/
        # cancelled review below then only forfeits the (optional) repair offer,
        # never the deck. (Pre-fix, the review ran first and a consumer-stall timeout
        # discarded the whole generation after scaffolding was skipped.)
        # Second cancel gate: assemble/validate above can take real time on a
        # big deck, and scaffold_deck is the write that overwrites the user's
        # <slug>/ — never perform it for a run whose consumer is gone.
        if cancel.is_set():
            return

        scaffold_error = None
        paths = {}
        try:
            paths = scaffold_deck(out_dir, slug, tex, overwrite=overwrite)
        except ScaffoldError as exc:
            scaffold_error = str(exc)

        # The deck is on disk — the checkpoint has done its job. Drop it (and
        # prune old ones) so a clean success leaves no resumable state. A scaffold
        # failure keeps the checkpoint so a re-run still resumes the sections.
        if resume_enabled and scaffold_error is None and paths.get("tex_path"):
            checkpoint.delete(ckpt_dir, job_key)
            checkpoint.prune(ckpt_dir)

        if paths and not paths.get("suite_root_found", False):
            warnings = list(warnings) + [
                "No common/latex-build.mk was found walking up from the deck "
                "folder — the Makefile cannot locate the suite root, so "
                "TEXINPUTS/BIBINPUTS stay unset and cress-style / _master.bib "
                "will not resolve. Place the deck somewhere under your LaTeX "
                "suite root."
            ]

        # 5b) Optional LLM .tex integrity review + auto-repair (off by default).
        # One extra RAG-free chat call over the already-scaffolded deck, bounded by
        # ``wall_clock_s`` so a runaway local model cannot stream past the SSE
        # consumer's stall window. A cancel/timeout leaves review_payload None — the
        # deck (written above) and its frame still go out.
        review_payload = None
        if review_enabled and not cancel.is_set():
            _info("Reviewing .tex integrity…")
            review_payload = _run_integrity_review(
                tex,
                provider=provider,
                model=review_model or model,
                max_tokens=review_max_tokens,
                cfg=cfg,
                cancel=cancel,
                deadline_s=wall_clock_s,
            )

        put({"deck": {
            "tex": tex,
            "warnings": warnings,
            "review": review_payload,
            "section_count": n_total,
            "placeholder_count": n_placeholder,
            # Resume telemetry for the UI banner: how many sections were reused
            # from a prior interrupted run (0 ⇒ a fresh generation).
            "resumed": reused > 0,
            "reused_sections": reused,
            "slug": slug,
            "out_dir": out_dir,
            "project_dir": paths.get("project_dir", ""),
            "tex_path": paths.get("tex_path", ""),
            # Stale-diff token for apply-repair: the raw-byte hash of the deck as it
            # was just written to disk. The client echoes it back so a repair is
            # refused if the on-disk deck changed since this generation.
            "tex_sha256": _file_sha256(paths.get("tex_path", "")),
            "makefile_path": paths.get("makefile_path", ""),
            "make_hint": (
                f"cd {paths.get('project_dir', '')} && make view"
                if paths.get("project_dir") else ""
            ),
            "scaffold_error": scaffold_error,
        }})

    # One deck operation at a time: acquire after validation (so a 400 never holds
    # the lock); run_sse_worker releases it when the stream ends.
    if not _DECK_OP_LOCK.acquire(blocking=False):
        return jsonify({"error": "Another deck operation is already running. Please wait for it to finish."}), 409
    return run_sse_worker(
        _worker,
        consumer_timeout_s=consumer_timeout_s,
        preflight_msgs=_index_preflight_msgs(
            "Indexing is still in progress — the deck may miss recently-added content.",
            "No fully-built vault index detected — generation may return little content. Index your vault first for best results.",
        ),
        release=_DECK_OP_LOCK.release,
    )


@deck_bp.route("/api/deck/apply-repair", methods=["POST"])
def api_deck_apply_repair():
    """Write a confirmed integrity-repair over the just-generated deck on disk.

    Body: ``{out_dir, deck_name, tex, base_sha256, confirm: true}``. This is the
    per-action confirm gate behind the auto-repair preview — the route never writes
    a repair silently; the user reviews it in the deck frame and explicitly applies
    it here. It now genuinely mirrors Note Refactor's safety model (it did not
    before):

    * **stale-diff guard** — ``base_sha256`` (the ``tex_sha256`` the generate frame
      handed out) must still match the on-disk deck, else the deck was edited /
      recompiled since the review and we refuse (409) rather than clobber it.
    * **re-screen** — the submitted ``tex`` is run back through
      :func:`~deckgen.review.screen_repair` against the *current* on-disk deck, so a
      client cannot use this endpoint to write arbitrary content with a smuggled
      shell-escape macro (the screen blocks any dangerous macro not already present).
    * **no Makefile clobber** — only ``<slug>.tex`` is rewritten
      (``write_deck_tex``), leaving a hand-edited ``Makefile`` intact.
    """
    data = request.get_json(silent=True) or {}

    if not coerce_bool(data.get("confirm")):
        return jsonify({"error": "Applying a repair requires confirm: true."}), 400

    out_dir = _resolve_out_dir(data.get("out_dir"))
    if not out_dir:
        return jsonify({"error": "Invalid or missing output folder."}), 400

    deck_name = coerce_string_max_len(data.get("deck_name"), _DECK_NAME_MAX) or ""
    slug = slugify(deck_name)

    tex = coerce_string_max_len(data.get("tex"), _TEMPLATE_MAX_BYTES) or ""
    if not tex.strip():
        return jsonify({"error": "No repaired .tex supplied."}), 400

    base_sha256 = coerce_string_max_len(data.get("base_sha256"), 128)
    if not base_sha256:
        return jsonify({"error": "base_sha256 is required (stale-diff guard)."}), 400

    # The deck folder + .tex must already exist (apply-repair only updates a
    # generated deck). Reuse the slug/escape validators by resolving the path the
    # same way write_deck_tex will.
    tex_path = os.path.join(os.path.realpath(out_dir), slug, f"{slug}.tex")
    if not os.path.isfile(tex_path):
        return jsonify({"error": "No generated deck found to repair; generate it first."}), 400

    # One deck operation at a time: take the process-wide lock for the
    # read→screen→write so this writer can't race a streaming generate/augment
    # (or a concurrent apply) and clobber a deck mid-flight. Acquired here (after
    # validation, so a 400 never holds the lock) and released in `finally`.
    if not _DECK_OP_LOCK.acquire(blocking=False):
        return jsonify({"error": "Another deck operation is already running. Please wait for it to finish."}), 409
    try:
        # Stale-diff guard: the on-disk deck must still be the one that was reviewed.
        # _read_deck_strict (raw-byte sha + strict UTF-8) replaces a bare
        # ``open(encoding="utf-8")`` whose UnicodeDecodeError (a ValueError, NOT an
        # OSError) previously escaped the handler as a 500; it now degrades to a clean
        # 400, and shares the one raw-byte sha convention with the augment path.
        try:
            current_tex, current_sha = _read_deck_strict(tex_path)
        except _DeckReadError as exc:
            return jsonify({"error": str(exc)}), 400
        if current_sha != base_sha256.strip():
            return jsonify({
                "error": "The deck on disk changed since the review; not overwriting. "
                         "Re-generate to review the current deck."
            }), 409

        # Re-screen the submitted repair against the CURRENT on-disk deck — blocks a
        # smuggled dangerous macro even though the same screen ran at generate time
        # (defence in depth; the body is client-supplied and must not be trusted).
        accepted, screen_warnings = screen_repair(current_tex, tex)
        if accepted is None:
            # Empty/identical → nothing to apply; unsafe/broken → screen_warnings says why.
            msg = screen_warnings[0] if screen_warnings else "Nothing to apply (repair is empty or identical)."
            return jsonify({"error": msg}), 400

        # Item 2.9 parity: both sibling writers (apply-augment, compile-fix)
        # copy a .bak before their first overwrite; apply-repair was the one
        # deck-overwriting path with NO recovery copy — a bad-but-screen-
        # passing repair left the user nothing to roll back to. Same
        # discipline: a failed backup ABORTS before anything is touched.
        try:
            backup_path = _backup_deck(tex_path)
        except OSError as exc:
            return jsonify({"error": f"Could not create backup file: {exc}"}), 500

        try:
            paths = write_deck_tex(out_dir, slug, accepted)
        except ScaffoldError as exc:
            return jsonify({"error": str(exc)}), 400

        return jsonify({
            "ok": True,
            "tex_path": paths.get("tex_path", ""),
            "project_dir": paths.get("project_dir", ""),
            "backup_path": backup_path,
            "tex_sha256": _file_sha256(paths.get("tex_path", "")),  # fresh token for any further apply
            "warnings": screen_warnings,
        })
    finally:
        _DECK_OP_LOCK.release()


@deck_bp.route("/api/deck/deck-sections", methods=["POST"])
def api_deck_sections():
    """List the ``\\section`` titles of an existing deck (for the augment scope picker).

    Read-only: parses the deck via :func:`deckgen.augment.split_deck` and returns
    its section titles plus a ``deck_sha256`` stale-diff token the augment preview
    will echo. Writes nothing.
    """
    data = request.get_json(silent=True) or {}
    deck_path = _resolve_existing_deck(data.get("deck_path"))
    if not deck_path:
        return jsonify({"error": "Invalid or unreadable deck path (an absolute .tex)."}), 400
    try:
        deck_tex, deck_sha = _read_deck_strict(deck_path)
    except _DeckReadError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        parts = split_deck(deck_tex)
    except AugmentError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({
        "ok": True,
        "deck_path": deck_path,
        "deck_sha256": deck_sha,
        "sections": [
            {"index": i, "title": s.title} for i, s in enumerate(parts.sections)
        ],
    })


@deck_bp.route("/api/deck/augment", methods=["POST"])
def api_deck_augment():
    """Preview a free-text augmentation of an existing deck and stream progress (SSE).

    One vault-grounded agent turn revises a section (or the whole section region),
    or writes a new section; the result is spliced back into the deck, validated and
    screened (:func:`deckgen.review.screen_repair`). **Writes nothing** — the
    terminal ``{"augment": {...}}`` frame carries the proposed full ``.tex`` and the
    stale-diff token; the user applies it via ``POST /api/deck/apply-augment``.
    """
    data = request.get_json(silent=True) or {}

    deck_path = _resolve_existing_deck(data.get("deck_path"))
    if not deck_path:
        return jsonify({"error": "Invalid or unreadable deck path (an absolute .tex)."}), 400
    instruction = coerce_non_empty_string(data.get("instruction"), _INSTRUCTION_MAX)
    if not instruction:
        return jsonify({"error": "A revision instruction is required."}), 400
    # operation/scope: omitted ⇒ default; PRESENT-but-invalid ⇒ 400 (don't silently
    # coerce a malformed client value to a default — that hides client bugs).
    operation = coerce_enum(data.get("operation"), _AUGMENT_OPS)
    if operation is None:
        if data.get("operation") is not None:
            return jsonify({"error": f"operation must be one of {_AUGMENT_OPS}."}), 400
        operation = "deepen"
    scope = coerce_enum(data.get("scope"), _AUGMENT_SCOPES)
    if scope is None:
        if data.get("scope") is not None:
            return jsonify({"error": f"scope must be one of {_AUGMENT_SCOPES}."}), 400
        scope = "whole"
    # section_index is optional (only meaningful when scope == "section" or for an
    # after-this insert); clamp defensively, default 0. Range vs the real section
    # count is checked in the worker (the count is only known after split_deck).
    section_index = coerce_int_in_range(data.get("section_index"), 0, _SECTION_INDEX_MAX)
    if section_index is None:
        section_index = 0
    audience = coerce_string_max_len(data.get("audience"), _AUDIENCE_MAX) or "the audience"
    topic = coerce_string_max_len(data.get("topic"), _TOPIC_MAX) or ""

    # Strict read (raw-byte token + UTF-8-only): augment can overwrite this file,
    # so a lossy decode that corrupts non-UTF-8 bytes on the round-trip is refused.
    try:
        deck_tex, deck_sha = _read_deck_strict(deck_path)
    except _DeckReadError as exc:
        return jsonify({"error": str(exc)}), 400

    cfg = load_config()
    agent_iters = (
        coerce_int_in_range(data.get("agent_max_iterations"), _AGENT_ITER_MIN, _AGENT_ITER_MAX)
        or coerce_int_in_range(cfg.get("deck_agent_max_iterations"), _AGENT_ITER_MIN, _AGENT_ITER_MAX)
        or 6
    )
    temperature = coerce_float_in_range(data.get("temperature"), _TEMP_MIN, _TEMP_MAX)
    if temperature is None:
        temperature = coerce_float_in_range(cfg.get("deck_temperature"), _TEMP_MIN, _TEMP_MAX)
    citations_enabled = bool(coerce_bool(data.get("citations_enabled")))
    if data.get("citations_enabled") is None:
        citations_enabled = True

    provider = coerce_string_max_len(data.get("provider"), 40) or cfg.get("provider", "ollama")
    model = coerce_string_max_len(data.get("model"), 120) or resolve_chat_model(cfg, provider)
    embed = coerce_string_max_len(data.get("embed"), 120) or cfg.get("embed", DEFAULT_EMBED)
    wall_clock_s = coerce_int_in_range(cfg.get("agent_wall_clock_s"), 30, 1800) or _CHAT_TOKEN_TIMEOUT_S
    consumer_timeout_s = max(wall_clock_s, _SINGLE_SHOT_FLOOR_S) + _STALL_MARGIN_S

    def _worker(put, cancel):
        # One vault-grounded agent turn that revises a section / the whole region /
        # inserts a new section, then splices the result back into the deck. Runs in
        # run_sse_worker's daemon thread; expected failures `put({"error": …})` + return.
        def _info(text):
            put({"info": text})

        # Relabel a turn-level {"error"} the agent loop emits (a transient
        # per-turn provider failure) to a NON-fatal {"info"} — parity with the
        # generate worker (2026-07-05 audit m5). The shared SSE consumer treats
        # ANY {"error"} frame as terminal, so without this a single transient
        # blip discarded the whole augmentation; the empty-body guard below
        # still emits a clean terminal error if the run truly produced nothing.
        # The worker's own explicit put({"error"}) calls stay fatal.
        def _on_event(item):
            if isinstance(item, dict) and "error" in item:
                put({"info": f"⚠ {item['error']}"})
            else:
                put(item)

        try:
            parts = split_deck(deck_tex)
        except AugmentError as exc:
            put({"error": str(exc)})
            return

        n_sections = len(parts.sections)
        # Validate the requested scope/operation against the PARSED deck (the section
        # count is only known after split). No silent fallbacks: an out-of-range
        # section or an operation that needs a section the deck lacks is an explicit
        # error, not a quiet append/no-op that would surprise the user.
        if scope == "section":
            if n_sections == 0:
                put({"error": "This deck has no \\section to target. Choose 'Add a new section' with whole-deck scope."})
                return
            if not (0 <= section_index < n_sections):
                put({"error": f"Section index out of range (the deck has {n_sections} section(s))."})
                return
        if operation in ("deepen", "table") and n_sections == 0:
            put({"error": "This deck has no \\section to revise. Use 'Add a new section' instead."})
            return

        # Macro + bib awareness from the deck's own preamble (so a deepened section
        # can reuse \citefoot{key} / house macros). load_template_parts tolerates a
        # deck with no \addbibresource (empty bib_index).
        try:
            tparts = load_template_parts(deck_tex, deck_path)
        except TemplateError:
            tparts = None
        macros_block = macro_cheatsheet(tparts.macros) if tparts else ""
        bib_index = tparts.bib_index if tparts else {}
        use_bib = citations_enabled and bool(bib_index)
        cite_mode = "bib" if use_bib else "prose"

        outline_titles = [s.title for s in parts.sections]
        target = parts.sections[section_index] if scope == "section" else None
        source = target.body if target is not None else (parts.sections_region or parts.opening)

        # TRUNCATION GUARD (the load-bearing data-loss fix). A deepen/table REPLACES
        # the targeted span; if the model only ever saw the first cap chars of an
        # over-cap source, its (shorter) output would overwrite the whole span and
        # silently delete everything past the cutoff. So we refuse the replace and
        # tell the user to narrow scope. For new_section the source is context-only
        # (we INSERT, never replace), so a clipped view loses no content — proceed,
        # just note it.
        truncated = len(source) > AUGMENT_MAX_SOURCE_CHARS
        if truncated and operation in ("deepen", "table"):
            put({"error": (
                f"The selected content is too large to revise in one pass "
                f"({len(source):,} chars > {AUGMENT_MAX_SOURCE_CHARS:,}). Switch Scope to "
                f"'One section' and revise it section by section."
            )})
            return
        excerpt = source[:AUGMENT_MAX_SOURCE_CHARS]
        if truncated:
            _info("The deck is large — only part of it was shown to the model as context.")

        focus = topic or (target.title if target is not None else "this deck")
        candidate_block = ""
        if use_bib:
            seed = instruction + " " + excerpt[:2000]
            candidate_block = bib_candidates_block(bib_index, relevant_bib_keys(bib_index, seed))

        runner = InProcessChatRunner(
            cfg=cfg, cancel_event=cancel, turn_timeout_s=wall_clock_s,
            workflow="deck_augment",
        )
        _info("Augmenting the deck…")
        result = runner.chat(
            build_augment_message(
                topic=focus,
                operation=operation,
                instruction=instruction,
                existing_excerpt=excerpt,
                outline_titles=outline_titles,
                candidate_bib_block=candidate_block,
            ),
            system_prompt=augment_system_prompt(
                audience, operation=operation,
                macros_block=macros_block, cite_mode=cite_mode,
                # Same gate as generate: no image indexing ⇒ never invite
                # \includegraphics (there are no is_image results to ground it).
                images_enabled=bool(cfg.get("vault_image_exts")),
            ),
            provider=provider, model=model, embed=embed,
            agent=True, max_iters=agent_iters, temperature=temperature,
            on_event=_on_event,
        )
        if cancel.is_set():
            return

        body = sanitize_section(result.text)
        if not body:
            put({"error": result.error or (
                "The model returned no usable Beamer content to apply. Try "
                "rephrasing the instruction or using a more capable model."
            )})
            return

        # Figure resolution runs on the EDITED SPAN ONLY, before the splice.
        # Augment's core safety property is that the splice is byte-identical
        # outside the edited span — a whole-deck figure pass after the splice
        # (the pre-fix order) rewrote \includegraphics lines in sections the
        # user never touched, and commented out every hand-written deck's
        # local/suite figures the vault knows nothing about.
        project_dir = os.path.dirname(deck_path)
        # copy=False (item 2.9): the PREVIEW stays write-free — the would-be
        # figure copies ride the staging record and are performed by
        # apply-augment after the backup, alongside the deck write.
        body, resolved_figs, fig_warnings, pending_figs = _resolve_and_copy_deck_figures(
            body, project_dir, copy=False,
        )

        # Splice per operation: new_section inserts, deepen/table replace.
        if operation == "new_section":
            after = section_index if scope == "section" else None
            proposed_full = insert_section(parts, body, after_index=after)
        elif target is not None:
            proposed_full = replace_section(parts.tex, target, body)
        else:
            proposed_full = replace_region(parts, body)

        # Advisory: a section-scoped revise should yield exactly one \section; a
        # different count means the model split/merged the structure — surface it
        # (the count delta below also exposes it) rather than silently reshaping.
        extra_warnings = []
        if target is not None and deck_counts(body)["sections"] != 1:
            extra_warnings.append(
                f"The revised section contains {deck_counts(body)['sections']} \\section "
                "command(s) (expected 1); review the deck structure before applying."
            )

        structural = validate(
            proposed_full,
            generated_tex=body,
            known_bib_keys=tparts.bib_keys if (tparts and use_bib) else None,
            copied_figures=resolved_figs,
        )
        # The safety gate detects a no-op (None when identical to the original) and
        # blocks a smuggled dangerous macro; the SAME screen runs again at apply time.
        accepted, screen_warnings = screen_repair(deck_tex, proposed_full)
        changed = accepted is not None

        # ONE de-duplicated warning list. structural (whole proposed doc) and the
        # validate() inside screen_repair both emit the same frame/document-balance
        # warnings, so a naive concat would double-report them; dict.fromkeys keeps
        # first-seen order while removing exact duplicates. When the repair is
        # REJECTED, screen_warnings is the discard reason (not a structural finding),
        # so it goes to rejected_reason instead — never mixed into the display list.
        display_warnings = list(dict.fromkeys(
            fig_warnings + list(structural) + extra_warnings
            + (list(screen_warnings) if changed else [])
        ))
        rejected_reason = "" if changed else (screen_warnings[0] if screen_warnings else "")

        # Count delta so a silent content loss (fewer frames/sections than before) is
        # visible in the preview even on a structurally-valid result.
        before = deck_counts(deck_tex)
        after = deck_counts(proposed_full) if changed else before

        # Cancel gate before the staging write (item 2.2 parity with generate):
        # an abandoned preview must not leave a fresh staged proposal behind —
        # a later apply-augment against it would write a deck the user never
        # saw previewed. (The stage lives under BASE_DIR, not the vault, so
        # this is hygiene + surprise-prevention, not clobber-prevention — the
        # _DeckOpGuard owns that.)
        if cancel.is_set():
            return
        if changed:
            # Stage the screened proposal SERVER-SIDE; apply reads it from here, never
            # from a client body, so a page cannot apply arbitrary .tex. deck_sha ties
            # the stage to the exact on-disk bytes we screened against.
            _stage_augment(deck_path, deck_sha, accepted,
                           pending_figures=pending_figs)

        put({"augment": {
            "proposed_tex": accepted or "",   # preview only; apply ignores any client tex
            "changed": changed,
            "warnings": display_warnings,
            "rejected_reason": rejected_reason,
            "deck_path": deck_path,
            "deck_sha256": deck_sha,           # raw-byte stale-diff token
            "counts": {
                "sections_before": before["sections"], "sections_after": after["sections"],
                "frames_before": before["frames"], "frames_after": after["frames"],
            },
            "operation": operation,
            "scope": scope,
            "section_index": section_index,
        }})

    if not _DECK_OP_LOCK.acquire(blocking=False):
        return jsonify({"error": "Another deck operation is already running. Please wait for it to finish."}), 409
    return run_sse_worker(
        _worker,
        consumer_timeout_s=consumer_timeout_s,
        preflight_msgs=_index_preflight_msgs(
            "Indexing is still in progress — the augmentation may miss recently-added content.",
            "No fully-built vault index detected — the augmentation may add little grounded content.",
        ),
        release=_DECK_OP_LOCK.release,
    )


@deck_bp.route("/api/deck/apply-augment", methods=["POST"])
def api_deck_apply_augment():
    """Write a confirmed augmentation over the on-disk deck.

    Body: ``{deck_path, base_sha256, confirm: true}`` — the proposed ``.tex`` is
    **never** taken from the client; it is read from the server-side staging cache
    written by the preview, so a page cannot apply arbitrary content. The safety
    model (stronger than apply-repair's, matching Note Refactor's vault writers):

    * **confirm gate** — requires ``confirm: true``;
    * **staged proposal** — read the screened proposal from staging (404-style 400
      if the user never previewed); verify its own integrity digest;
    * **stale-diff guard** — the on-disk deck's raw-byte sha must equal BOTH the
      client's ``base_sha256`` and the staged base, else 409 (the deck changed since
      the preview);
    * **re-screen** — run the staged proposal back through
      :func:`~deckgen.review.screen_repair` against the current on-disk deck (defence
      in depth, even though the proposal is server-owned);
    * **backup** — copy the deck to ``<deck>.tex.bak`` BEFORE overwriting (the deck
      may be a hand-written file, so the overwrite must be recoverable);
    * writes back to the exact deck file (:func:`deckgen.scaffold.write_deck_at`) and
      clears the stage.
    """
    data = request.get_json(silent=True) or {}

    if not coerce_bool(data.get("confirm")):
        return jsonify({"error": "Applying an augmentation requires confirm: true."}), 400

    deck_path = _resolve_existing_deck(data.get("deck_path"))
    if not deck_path:
        return jsonify({"error": "Invalid or missing deck path."}), 400

    base_sha256 = coerce_string_max_len(data.get("base_sha256"), 128)
    if not base_sha256:
        return jsonify({"error": "base_sha256 is required (stale-diff guard)."}), 400

    # One deck operation at a time: hold the process-wide lock across the whole
    # read→screen→backup→write so this writer can't race a streaming generate/
    # augment or a concurrent apply (TOCTOU between the stale-diff read and the
    # overwrite). Acquired after validation (a 400 never holds it); released in
    # `finally`.
    if not _DECK_OP_LOCK.acquire(blocking=False):
        return jsonify({"error": "Another deck operation is already running. Please wait for it to finish."}), 409
    try:
        # The proposal comes from staging (server-owned), not the request body.
        stage = _load_stage(deck_path)
        if not stage:
            return jsonify({
                "error": "No staged augmentation for this deck. Preview a change first."
            }), 400
        proposed = stage.get("proposed_tex") or ""
        if not proposed.strip() or _proposed_digest(proposed) != stage.get("proposed_sha256"):
            # The staging file was truncated / corrupted / hand-edited — refuse and clear
            # it so the user re-previews rather than applying a damaged proposal.
            _clear_stage(deck_path)
            return jsonify({"error": "The staged augmentation is invalid; re-run the preview."}), 400

        try:
            current_tex, current_sha = _read_deck_strict(deck_path)
        except _DeckReadError as exc:
            return jsonify({"error": str(exc)}), 400
        # Stale-diff: on-disk must match the client's claimed base AND the staged base.
        if current_sha != base_sha256.strip() or current_sha != stage.get("base_sha256"):
            return jsonify({
                "error": "The deck on disk changed since the preview; not overwriting. "
                         "Re-run the augmentation to preview the current deck."
            }), 409

        # Re-screen the staged proposal against the CURRENT on-disk deck (defence in depth).
        accepted, screen_warnings = screen_repair(current_tex, proposed)
        if accepted is None:
            msg = screen_warnings[0] if screen_warnings else "Nothing to apply (the augmentation is empty or identical)."
            return jsonify({"error": msg}), 400

        # Backup BEFORE overwriting; abort if the backup cannot be written so we never
        # destroy the original without a recovery copy.
        try:
            backup_path = _backup_deck(deck_path)
        except OSError as exc:
            return jsonify({"error": f"Could not create a backup ({type(exc).__name__}); not overwriting."}), 500

        # Deferred figure copies (item 2.9): the write-free preview recorded
        # the vault→figures/ copies it would have made; perform them NOW, at
        # apply time, so the applied deck compiles. Defence in depth on the
        # staged paths (the stage is server-written, but it lives on disk):
        # src must resolve under the vault root, dst under the deck's folder,
        # and an existing dst is never clobbered (the never-clobber rule the
        # resolver has always had). A failed copy is a warning, not an abort
        # — the .tex still applies and the missing figure fails visibly at
        # compile time (the .bak is the recovery path either way).
        fig_apply_warnings: list[str] = []
        vault_path_now = obsidian_manager.get_vault_path()
        vault_root_now = Path(vault_path_now).resolve() if vault_path_now else None
        project_dir_now = os.path.realpath(os.path.dirname(deck_path))
        for pair in stage.get("pending_figures") or []:
            if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
                continue
            src, dst = str(pair[0]), str(pair[1])
            try:
                if vault_root_now is None:
                    raise OSError("no vault configured")
                src_rel = resolve_under_root(src, vault_root_now)
                if not src_rel:
                    raise OSError("source escapes the vault")
                src_r = Path(vault_root_now) / src_rel
                
                dst_rel = resolve_under_root(dst, project_dir_now)
                if not dst_rel:
                    raise OSError("destination escapes the deck folder")
                dst_r = os.path.join(project_dir_now, dst_rel)
                if os.path.exists(dst_r):
                    continue                               # never clobber
                os.makedirs(os.path.dirname(dst_r), exist_ok=True)
                shutil.copy2(src_r, dst_r)
            except (OSError, ValueError) as exc:
                fig_apply_warnings.append(
                    f"Figure copy failed for {os.path.basename(dst)}: {exc}")

        try:
            paths = write_deck_at(deck_path, accepted)
        except ScaffoldError as exc:
            return jsonify({"error": str(exc)}), 400

        _clear_stage(deck_path)
        return jsonify({
            "ok": True,
            "tex_path": paths.get("tex_path", ""),
            "backup_path": backup_path,
            # Fresh raw-byte token for any further apply (re-read what was just written).
            "deck_sha256": _file_sha256(paths.get("tex_path", "")),
            "warnings": list(screen_warnings) + fig_apply_warnings,
        })
    finally:
        _DECK_OP_LOCK.release()


@deck_bp.route("/api/deck/native-pick-file", methods=["POST"])
def api_deck_native_pick_file():
    """Native file picker for choosing a template ``.tex``/``.sty``."""
    import webview
    if not webview.windows:
        return jsonify({"error": "No active window"}), 503
    result = webview.windows[0].create_file_dialog(
        webview.OPEN_DIALOG,
        allow_multiple=False,
        file_types=("LaTeX (*.tex;*.sty)", "All files (*.*)"),
    )
    if not result:
        return jsonify({"cancelled": True})
    chosen = result[0] if isinstance(result, (list, tuple)) else result
    if not os.path.isfile(chosen):
        return jsonify({"error": "Selected path is not a file"}), 400
    return jsonify({"ok": True, "path": chosen})


@deck_bp.route("/api/deck/native-pick-folder", methods=["POST"])
def api_deck_native_pick_folder():
    """Native folder picker for choosing the output (suite root) directory."""
    import webview
    if not webview.windows:
        return jsonify({"error": "No active window"}), 503
    result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG, allow_multiple=False)
    if not result:
        return jsonify({"cancelled": True})
    chosen = result[0] if isinstance(result, (list, tuple)) else result
    if not os.path.isdir(chosen):
        return jsonify({"error": "Selected path is not a directory"}), 400
    return jsonify({"ok": True, "path": chosen})


@deck_bp.route("/api/deck/compile-available", methods=["GET"])
def api_compile_available():
    """Report whether a TeX toolchain (latexmk) is installed on this machine."""
    return jsonify({"available": bool(find_latexmk())})


# The only engines latexmk may be invoked with. Re-validated at CALL time (not
# just at /api/config save): the engine reaches argv as f"-{engine}", and a
# hand-edited config value like "pdflatex=<cmd>" would be latexmk option
# injection (-pdflatex=<cmd> sets the compiler COMMAND — arbitrary execution).
# Same re-clamp-per-call discipline as the vision bounds.
_COMPILE_ENGINES = ("pdflatex", "lualatex", "xelatex")


def _run_compile_repair(tex, errors, *, provider, model, max_tokens, cfg, cancel, deadline_s=None):
    """One RAG-free LLM repair turn over a deck that failed to compile.

    Sibling of :func:`_run_integrity_review` (same ``stream_chat_messages``
    shape, cancel/deadline polling, never raises): feeds the whole current
    ``.tex`` plus the parsed ``.log`` errors to the model and extracts the
    fenced whole-document repair via :func:`deckgen.review.parse_review`.
    *deadline_s* bounds the streamed reply — a wedged local model must not
    stall the compile loop past its per-stage budget.
    """
    payload = {
        "success": False, "issues": [], "repaired_tex": "", "error": ""
    }
    deadline = (time.monotonic() + deadline_s) if deadline_s else None
    try:
        messages = build_repair_messages(tex, errors)
        chunks = []
        for tok in stream_chat_messages(
            messages=messages,
            system_prompt=COMPILE_REPAIR_SYSTEM_PROMPT,
            provider_name=provider,
            model=model,
            temperature=0.1,
            max_tokens=max_tokens,
            cfg=cfg,
            workflow="deck_compile_fix",
        ):
            if cancel.is_set():
                return payload
            if deadline is not None and time.monotonic() > deadline:
                payload["error"] = "compile repair timed out before completing"
                return payload
            chunks.append(tok)

        full_reply = "".join(chunks)
        res = parse_review(full_reply)
        payload["issues"] = res.issues
        payload["repaired_tex"] = res.repaired_tex
        payload["success"] = bool(res.repaired_tex)
        if not res.repaired_tex:
            payload["error"] = "Model failed to provide a valid LaTeX document repair block."
    except Exception as exc:
        # Item 2.9: through sanitise_error_msg like the integrity-review
        # sibling — a provider exception can embed the request URL/API key,
        # and this string reaches the UI via the SSE info/error frames.
        payload["error"] = sanitise_error_msg(exc)

    return payload


@deck_bp.route("/api/deck/compile-fix", methods=["POST"])
def api_deck_compile_fix():
    """Compile-and-fix loop over an existing deck (SSE).

    compile → (fail) → LLM repair → screen → write → compile again … — the
    loop is REPAIR-budgeted (``deck_compile_max_iters`` repairs, each written
    repair verified by the following compile pass, so an unverified repair is
    never the final on-disk state) and runs entirely under ``_DECK_OP_LOCK``:
    the deck read + stale-diff check happen AFTER acquisition, like
    apply-repair/apply-augment, so a concurrent writer can't slip between the
    check and the loop. Model/token knobs are config-only (``deck_review_*``,
    the same "never body overrides" posture as the integrity review).
    """
    if not find_latexmk():
        return jsonify({"error": "latexmk is not available on this system. Make sure a LaTeX suite is installed."}), 400

    data = request.get_json(silent=True) or {}

    if not coerce_bool(data.get("confirm")):
        return jsonify({"error": "Compiling and fixing requires confirm: true."}), 400

    deck_path = _resolve_existing_deck(data.get("deck_path"))
    if not deck_path:
        return jsonify({"error": "Invalid or unreadable deck path (an absolute .tex)."}), 400

    base_sha256 = coerce_string_max_len(data.get("base_sha256"), 128)
    if not base_sha256:
        return jsonify({"error": "base_sha256 is required (stale-diff guard)."}), 400

    # Resolve config BEFORE acquiring the lock (parity with generate/augment;
    # 2026-07-05 audit m7). Config resolution needs no lock, and keeping it
    # AFTER the acquire — with no try/finally over the block — meant any future
    # raising call added here would leak _DECK_OP_LOCK (every deck op 409s until
    # restart). None of these calls raise today; moving it closes the window.
    cfg = load_config()
    timeout_s = coerce_int_in_range(cfg.get("deck_compile_timeout_s"), 30, 600) or 180
    max_repairs = coerce_int_in_range(cfg.get("deck_compile_max_iters"), 1, 3) or 2
    engine = coerce_enum(data.get("engine"), _COMPILE_ENGINES)
    if engine is None:
        engine = coerce_enum(cfg.get("deck_compile_engine"), _COMPILE_ENGINES) or "pdflatex"
    # Config-only model resolution — deliberately NO body overrides (matches
    # deck_review_*: a page must not be able to steer provider/model/spend).
    provider = cfg.get("provider", "ollama")
    model = (cfg.get("deck_review_model") or "").strip() or resolve_chat_model(cfg, provider)
    max_tokens = coerce_int_in_range(cfg.get("deck_review_max_tokens"), 256, 16384) or 4096

    # Body + config validation done (a 400 above never holds the lock);
    # everything that READS the deck happens under the lock so the stale-diff
    # check and the compile loop see the same bytes — checking before
    # acquisition left a window for a concurrent apply to rewrite the deck.
    if not _DECK_OP_LOCK.acquire(blocking=False):
        return jsonify({"error": "Another deck operation is already running. Please wait for it to finish."}), 409

    try:
        deck_tex, deck_sha = _read_deck_strict(deck_path)
    except _DeckReadError as exc:
        _DECK_OP_LOCK.release()
        return jsonify({"error": str(exc)}), 400

    if deck_sha != base_sha256.strip():
        _DECK_OP_LOCK.release()
        return jsonify({
            "error": "The deck on disk changed since the last request; not compile-fixing. "
                     "Re-read or refresh to sync."
        }), 409

    # Worst case: max_repairs+1 compile passes AND max_repairs LLM repairs,
    # each bounded by timeout_s. The pre-fix budget counted only the compiles,
    # so a slow repair tripped the stall guard mid-loop — which released the
    # deck-op lock while the worker thread was still about to WRITE the deck
    # (the zombie-writer race).
    total_time_s = (2 * max_repairs + 1) * timeout_s
    consumer_timeout_s = max(total_time_s, _SINGLE_SHOT_FLOOR_S) + _STALL_MARGIN_S

    def _worker(put, cancel):
        def _info(text):
            put({"info": text})

        current_tex = deck_tex
        current_sha = deck_sha
        project_dir = os.path.dirname(deck_path)
        deck_base = os.path.basename(deck_path)

        backup_made = False
        compile_passes = 0
        repairs_done = 0
        changed = False
        last_log_excerpt = ""
        success = False

        # Loop shape: every WRITTEN repair is followed by another compile
        # pass, so the loop can only end on (a) success, (b) a repair-budget /
        # screening / repair failure — with the last VERIFIED state on disk —
        # or (c) cancellation. It can never end right after an unverified
        # write (the pre-fix loop's final iteration did exactly that).
        while True:
            if cancel.is_set():
                return
            compile_passes += 1
            _info(f"Running compilation pass {compile_passes} using {engine}…")
            success, log_contents = compile_latex(deck_path, engine=engine, timeout=timeout_s)
            if success:
                _info(f"Compilation succeeded on pass {compile_passes}.")
                break

            errors = parse_latex_log(log_contents)
            # A missing .sty/.cls/.bib is not LLM-fixable (the file exists in
            # the suite or it doesn't) — and feeding it to the model teaches
            # it to DELETE the \usepackage, silently stripping the house
            # style. Surface those to the user directly instead.
            missing_files = [e for e in errors if is_missing_file_error(e)]
            errors = [e for e in errors if not is_missing_file_error(e)]
            for entry in missing_files:
                _info(f"Missing suite file (not repairable by the model): {entry}")
            if not errors:
                last_log_excerpt = "\n".join(missing_files) or (
                    "No error pattern found in log, but latexmk exited with an error."
                )
                _info("Compilation failed with no model-repairable errors in the log.")
                break

            last_log_excerpt = "\n".join(errors)
            if repairs_done >= max_repairs:
                _info(f"Repair budget exhausted ({max_repairs}); leaving the last verified state on disk.")
                break

            _info(
                f"Compilation failed on pass {compile_passes}: {len(errors)} error(s). "
                "Asking the model for a repair…"
            )
            repair_res = _run_compile_repair(
                current_tex,
                errors,
                provider=provider,
                model=model,
                max_tokens=max_tokens,
                cfg=cfg,
                cancel=cancel,
                deadline_s=timeout_s,
            )
            repairs_done += 1
            if cancel.is_set():
                return
            if not repair_res["success"]:
                _info(f"Repair failed: {repair_res['error']}")
                break

            # Screen against the CURRENT deck: each accepted repair was itself
            # screened against its predecessor, so by induction no dangerous
            # macro absent from the ORIGINAL can survive into any iteration —
            # while the no-op check ("identical") stays meaningful on the
            # second and later repairs (against the original it would compare
            # the wrong baseline).
            accepted, screen_warnings = screen_repair(current_tex, repair_res["repaired_tex"])
            if accepted is None:
                msg = screen_warnings[0] if screen_warnings else (
                    "Proposed repair is empty or identical to the current deck."
                )
                _info(f"Proposed repair was rejected by safety screening: {msg}")
                break

            if not backup_made:
                # One .bak of the ORIGINAL before the first write (augment-
                # apply discipline); it is the user's recovery path, so a
                # failed backup aborts before anything is touched.
                backup_path = deck_path + ".bak"
                try:
                    shutil.copy2(deck_path, backup_path)
                    backup_made = True
                    _info(f"Created backup of the original deck at {deck_base}.bak")
                except OSError as exc:
                    put({"error": f"Could not create backup file: {exc}"})
                    return

            # Figure resolution keeps deck-local/suite files (existence check)
            # — a repair pass must never comment out figures that already
            # compile from figures/ or common/.
            accepted, _resolved_figs, fig_warnings, _pending = _resolve_and_copy_deck_figures(
                accepted, project_dir,
            )
            for w in fig_warnings:
                _info(w)

            # Last cancel check before the write: after a stall-released lock
            # another op may already be running — never race it with a write.
            if cancel.is_set():
                return
            try:
                # write_deck_at writes back to the exact file we read —
                # compile-fix accepts ANY validated deck, not just the
                # <out_dir>/<slug>/<slug>.tex scaffold layout write_deck_tex
                # assumes (that mismatch used to ScaffoldError only after
                # burning a full compile + LLM repair).
                write_deck_at(deck_path, accepted)
                changed = True
                current_tex = accepted
                current_sha = _file_sha256(deck_path)
            except Exception as exc:
                put({"error": f"Failed to write repaired deck to disk: {exc}"})
                return

        put({
            "compile": {
                "success": success,
                "iterations": compile_passes,
                "changed": changed,
                "tex_sha256": current_sha,
                "log_excerpt": last_log_excerpt,
            }
        })

    return run_sse_worker(
        _worker,
        consumer_timeout_s=consumer_timeout_s,
        preflight_msgs=[],
        release=_DECK_OP_LOCK.release,
    )
