#!/usr/bin/env python3
"""Resolve the Obsidian vault and its subpaths for the maintenance scripts.

These scripts used to hardcode the vault path *and* every subfolder name, so a
rename broke them in two different silent ways: `audit_pdf_names.py` exited 1
with "Directory not found", and `check_tex.py` swept an empty tree and printed
"All checks passed" over zero documents. Nothing in either failure mode says
"your folder moved".

Resolution order, per path — first hit wins:

  1. an explicit CLI argument
  2. an environment variable (``VAULT_DIR`` / ``BIBLIO_DIR`` / ``SUITE_DIR``)
  3. ChatEKLD's ``config.json`` — the app already stores the vault path and the
     audit subfolder names, and they are maintained through its Settings
     window, so it is the closest thing on this machine to a live source of
     truth. Read-only and entirely optional: a missing or malformed config just
     falls through to the next rule.
  4. marker-file discovery — a bounded ``scandir`` walk, never a full rglob
     over a 36 GB vault: the LaTeX suite root is the directory holding
     ``.latexmkrc``, the biblio directory is matched by name.
  5. built-in default SUBFOLDER names only (``Z_attachments`` etc.). There is
     deliberately no built-in vault PATH (2026-07-29 audit 10): the old
     hardcoded absolute default embedded the maintainer's username and vault
     name in a tree the public mirror publishes wholesale, and rules 1–4
     already cover every real resolution — a vault that none of them find
     raises the actionable ``VaultPathError`` instead.

Every resolver records *which* rule fired, so callers can print it. A silent
fallback to a stale default is the failure mode this module exists to prevent.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# Rule 5 — default SUBFOLDER names only; the absolute vault path deliberately
# has no built-in default (see the module docstring's privacy note).
DEFAULT_ATTACHMENTS_SUBDIR = "Z_attachments"
DEFAULT_BIBLIO_SUBDIR = "biblio_articles"
DEFAULT_SUITE_SUBDIR = "presentations_slides_writings_teaching"
DEFAULT_SKIP_PREFIX = "z_item"

# Directories never worth descending into while looking for a marker: huge,
# and none of them can contain a suite root or the biblio folder.
_ALWAYS_SKIP = {".git", ".obsidian", "node_modules", "__pycache__", ".venv"}


class VaultPathError(RuntimeError):
    """A path could not be resolved by any rule."""


@dataclass(frozen=True)
class Resolved:
    """A resolved path plus the rule that produced it."""

    path: Path
    source: str

    def __fspath__(self) -> str:  # lets callers pass this straight to open()
        return str(self.path)

    def __str__(self) -> str:
        return str(self.path)

    def describe(self) -> str:
        return f"{self.path}  [{self.source}]"


# --------------------------------------------------------------------------
# ChatEKLD config (rule 3)
# --------------------------------------------------------------------------

def chatekld_config_path() -> Path:
    """Where the app keeps config.json (honouring its own env override)."""
    base = os.environ.get("CHATEKLD_BASE_DIR")
    if base:
        return Path(base).expanduser() / "config.json"
    return Path.home() / "Library" / "Application Support" / "ChatEKLD" / "config.json"


def chatekld_config() -> dict:
    """Best-effort read of ChatEKLD's config. Never raises, never writes.

    An absent app, an unreadable file, or a malformed one all return {} so the
    caller falls through to discovery — these scripts must keep working on a
    machine where ChatEKLD was never installed.
    """
    try:
        with open(chatekld_config_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _cfg_str(cfg: dict, key: str) -> str:
    value = cfg.get(key)
    return value.strip() if isinstance(value, str) else ""


# --------------------------------------------------------------------------
# Bounded marker discovery (rule 4)
# --------------------------------------------------------------------------

def _iter_dirs(root: Path, max_depth: int, skip: set[str]):
    """Breadth-first directory walk, depth-capped, skipping heavy subtrees.

    Deliberately not ``Path.rglob``: the target vault holds ~36 000 files under
    ``Z_attachments`` alone, and a marker search that walks them all would cost
    more than the check it is setting up for.
    """
    frontier = [(root, 0)]
    while frontier:
        current, depth = frontier.pop(0)
        if depth >= max_depth:
            continue
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name in skip or entry.name.startswith("."):
                continue
            child = Path(entry.path)
            yield child
            frontier.append((child, depth + 1))


def _find_dir(root: Path, predicate, max_depth: int, skip: set[str]) -> Path | None:
    for candidate in _iter_dirs(root, max_depth, skip):
        if predicate(candidate):
            return candidate
    return None


# --------------------------------------------------------------------------
# Resolvers
# --------------------------------------------------------------------------

def resolve_vault(explicit: str | Path | None = None, cfg: dict | None = None) -> Resolved:
    """Resolve the vault root."""
    cfg = chatekld_config() if cfg is None else cfg

    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_dir():
            raise VaultPathError(f"--vault does not exist or is not a directory: {path}")
        return Resolved(path.resolve(), "--vault")

    env = os.environ.get("VAULT_DIR", "").strip()
    if env:
        path = Path(env).expanduser()
        if not path.is_dir():
            raise VaultPathError(f"VAULT_DIR does not exist or is not a directory: {path}")
        return Resolved(path.resolve(), "$VAULT_DIR")

    from_cfg = _cfg_str(cfg, "obsidian_vault_path")
    if from_cfg:
        path = Path(from_cfg).expanduser()
        if path.is_dir():
            return Resolved(path.resolve(), "ChatEKLD config: obsidian_vault_path")

    # Marker: an Obsidian vault is the directory holding `.obsidian/`. Walk up
    # from the working directory so running these from inside the vault works.
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".obsidian").is_dir():
            return Resolved(candidate, "discovered: .obsidian/ at or above cwd")

    raise VaultPathError(
        "Could not locate the Obsidian vault. Pass --vault <path>, set "
        "VAULT_DIR, configure it in ChatEKLD, or run this from inside the vault."
    )


def resolve_biblio(
    vault: Path, explicit: str | Path | None = None, cfg: dict | None = None
) -> Resolved:
    """Resolve the biblio-articles PDF directory."""
    cfg = chatekld_config() if cfg is None else cfg

    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_dir():
            raise VaultPathError(f"--biblio does not exist or is not a directory: {path}")
        return Resolved(path.resolve(), "--biblio")

    env = os.environ.get("BIBLIO_DIR", "").strip()
    if env:
        path = Path(env).expanduser()
        if not path.is_dir():
            raise VaultPathError(f"BIBLIO_DIR does not exist or is not a directory: {path}")
        return Resolved(path.resolve(), "$BIBLIO_DIR")

    cfg_attachments = _cfg_str(cfg, "audit_attachments_subdir")
    cfg_biblio = _cfg_str(cfg, "audit_biblio_articles_subdir")
    attachments = cfg_attachments or DEFAULT_ATTACHMENTS_SUBDIR
    biblio_name = cfg_biblio or DEFAULT_BIBLIO_SUBDIR

    candidate = vault / attachments / biblio_name
    if candidate.is_dir():
        # Report honestly which of the two produced the hit — a "config" label
        # over values that actually came from the built-in constants would hide
        # exactly the staleness this reporting exists to expose.
        source = (
            "ChatEKLD config: audit_attachments_subdir + audit_biblio_articles_subdir"
            if (cfg_attachments and cfg_biblio)
            else "built-in default subfolder names"
        )
        return Resolved(candidate.resolve(), source)

    # Marker: a directory with the configured (or default) biblio name, found
    # anywhere shallow in the vault — this is what survives moving the folder
    # out of Z_attachments, or renaming Z_attachments itself.
    wanted = {biblio_name.lower(), DEFAULT_BIBLIO_SUBDIR}
    found = _find_dir(
        vault,
        lambda p: p.name.lower() in wanted,
        max_depth=3,
        skip=_ALWAYS_SKIP | {"Z_Zotero_Notes"},
    )
    if found:
        return Resolved(found.resolve(), f"discovered: directory named {found.name!r}")

    raise VaultPathError(
        f"Could not find the biblio PDF directory under {vault}.\n"
        f"  Looked for: {vault / attachments / biblio_name}\n"
        f"  and any directory named {sorted(wanted)} within 3 levels.\n"
        f"  Pass --biblio <path> or set BIBLIO_DIR."
    )


def resolve_suite(
    vault: Path, explicit: str | Path | None = None, cfg: dict | None = None
) -> Resolved:
    """Resolve the LaTeX academic suite root."""
    cfg = chatekld_config() if cfg is None else cfg

    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_dir():
            raise VaultPathError(f"--suite does not exist or is not a directory: {path}")
        return Resolved(path.resolve(), "--suite")

    env = os.environ.get("SUITE_DIR", "").strip()
    if env:
        path = Path(env).expanduser()
        if not path.is_dir():
            raise VaultPathError(f"SUITE_DIR does not exist or is not a directory: {path}")
        return Resolved(path.resolve(), "$SUITE_DIR")

    # The app records the central bibliography as a vault-relative path; the
    # suite root is its parent, which is exactly the directory `make` treats as
    # SUITE_ROOT. A rename of the suite folder shows up here first.
    master_bib = _cfg_str(cfg, "audit_master_bib_path")
    if master_bib:
        candidate = (vault / master_bib).parent
        if candidate.is_dir():
            return Resolved(
                candidate.resolve(), "ChatEKLD config: parent of audit_master_bib_path"
            )

    # Marker: the suite root is the directory carrying `.latexmkrc` (and, in
    # this suite, `common/latex-build.mk`). Both are structural, not names.
    def is_suite(path: Path) -> bool:
        return (path / ".latexmkrc").is_file() or (path / "common" / "latex-build.mk").is_file()

    found = _find_dir(
        vault,
        is_suite,
        max_depth=3,
        skip=_ALWAYS_SKIP | {"Z_attachments", "Z_Zotero_Notes"},
    )
    if found:
        return Resolved(found.resolve(), "discovered: .latexmkrc / common/latex-build.mk")

    fallback = vault / DEFAULT_SUITE_SUBDIR
    if fallback.is_dir():
        return Resolved(fallback.resolve(), "built-in default subfolder name")

    raise VaultPathError(
        f"Could not find the LaTeX suite root under {vault}.\n"
        f"  Looked for a directory holding .latexmkrc or common/latex-build.mk "
        f"within 3 levels, and {fallback}.\n"
        f"  Pass --suite <path> or set SUITE_DIR."
    )


def skip_prefixes(cfg: dict | None = None) -> tuple[str, ...]:
    """Filename prefixes exempt from the PDF naming audit.

    ChatEKLD stores the same exemption as `audit_biblio_skip_prefix` (it skips
    those PDFs during a scan), so the two stay aligned without a second place
    to edit.
    """
    cfg = chatekld_config() if cfg is None else cfg
    prefix = _cfg_str(cfg, "audit_biblio_skip_prefix") or DEFAULT_SKIP_PREFIX
    return (prefix.lower(),)
