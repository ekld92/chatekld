"""Settings adapter for the Library Audit subsystem.

Vendored from kb_harmonizer with the original ``Settings.load()`` removed
in favour of :func:`load_settings`, which reads the audit-specific keys
from ChatEKLD's ``config.json``. Every subpath that was hardcoded in the
upstream version (``biblio_articles`` subfolder, ``Z_Zotero_Notes``
subfolder, master bib path) is now exposed as a configurable key.

Imports from ``core.config`` and ``core.constants`` are deferred to
keep this module importable from the diagnostic CLI without forcing the
full Flask app to load.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# --- Defaults (mirror the upstream kb_harmonizer assumptions) ---
DEFAULT_ATTACHMENTS_SUBDIR = "Z_attachments"
DEFAULT_BIBLIO_ARTICLES_SUBDIR = "biblio_articles"
DEFAULT_ZOTERO_NOTES_SUBDIR = "Z_Zotero_Notes"
DEFAULT_MASTER_BIB_PATH = "_master.bib"
DEFAULT_ZOTERO_SQLITE = "~/Zotero/zotero.sqlite"
DEFAULT_ZOTERO_STORAGE = "~/Zotero/storage"
DEFAULT_ANNOTATIONS_READ_THRESHOLD = 5
DEFAULT_BIBLIO_SKIP_PREFIX = "z_item"
DEFAULT_IGNORED_DIRS = frozenset({".git", ".obsidian", ".pandoc", ".trash"})


def _audit_dir() -> str:
    """Return ``BASE_DIR/audit`` and ensure it exists.

    Deferred import keeps the dataclass importable in test harnesses that
    stub out ``core.constants``.
    """
    from core.constants import BASE_DIR

    path = os.path.join(BASE_DIR, "audit")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def mapping_file_path() -> Path:
    """Path to the manual PDF<->bib mapping override file."""
    return Path(_audit_dir()) / "mapping.json"


@dataclass
class Settings:
    """Runtime configuration for the audit engine.

    Shape mirrors the upstream kb_harmonizer ``Settings`` so the engine
    modules (which use ``settings.biblio_articles_dir`` etc.) keep working
    without modification. The hardcoded subpaths from the upstream version
    are promoted to dataclass fields so they can be configured per-user.
    """

    vault_root: Path
    attachments_subdir: str = DEFAULT_ATTACHMENTS_SUBDIR
    biblio_articles_subdir: str = DEFAULT_BIBLIO_ARTICLES_SUBDIR
    zotero_notes_subdir: str = DEFAULT_ZOTERO_NOTES_SUBDIR
    master_bib_path: str = DEFAULT_MASTER_BIB_PATH
    zotero_sqlite: Path = field(
        default_factory=lambda: Path(DEFAULT_ZOTERO_SQLITE).expanduser()
    )
    zotero_storage: Path = field(
        default_factory=lambda: Path(DEFAULT_ZOTERO_STORAGE).expanduser()
    )
    ignored_dirs: frozenset[str] = DEFAULT_IGNORED_DIRS
    biblio_skip_prefix: str = DEFAULT_BIBLIO_SKIP_PREFIX
    annotations_read_threshold: int = DEFAULT_ANNOTATIONS_READ_THRESHOLD
    # Optional override (mainly for tests). When None, the property
    # resolves to ``BASE_DIR/audit/mapping.json``.
    mapping_file_override: Path | None = None
    # Upstream parity flags — neither path currently writes back, but
    # downstream code can branch on these when write actions land.
    finder_tags_writable: bool = False
    zotero_writable: bool = False

    @property
    def master_bib(self) -> Path:
        """Absolute path to ``_master.bib`` (vault root + configured subpath)."""
        return self.vault_root / self.master_bib_path

    @property
    def mapping_file(self) -> Path:
        """Path to the manual-mapping override file (test override wins)."""
        if self.mapping_file_override is not None:
            return self.mapping_file_override
        return mapping_file_path()

    @property
    def attachments_dir(self) -> Path:
        """Absolute path to the ``Z_attachments`` tree (duplicate-scan root)."""
        return self.vault_root / self.attachments_subdir

    @property
    def biblio_articles_dir(self) -> Path:
        """Absolute path to ``biblio_articles`` — the active-PDF set's root."""
        return self.attachments_dir / self.biblio_articles_subdir

    @property
    def zotero_notes_dir(self) -> Path:
        """Absolute path to ``Z_Zotero_Notes`` (the per-key markdown notes)."""
        return self.vault_root / self.zotero_notes_subdir


class AuditConfigError(ValueError):
    """Raised when the audit config is incomplete (e.g. no vault path set)."""


def load_settings() -> Settings:
    """Build a :class:`Settings` from ChatEKLD's persisted config.

    Raises :class:`AuditConfigError` when the Obsidian vault path is not
    configured, since every report depends on the vault root.
    """
    from core.config import load_config

    cfg = load_config()
    vault_root_str = str(cfg.get("obsidian_vault_path") or "").strip()
    if not vault_root_str:
        raise AuditConfigError(
            "Obsidian vault path is not configured. Set it on the Obsidian Agent "
            "tab before running an audit."
        )
    try:
        vault_root = Path(vault_root_str).expanduser().resolve()
    except OSError as exc:
        raise AuditConfigError(f"Vault path could not be resolved: {exc}") from exc
    if not vault_root.is_dir():
        raise AuditConfigError(
            f"Vault path is not an existing directory: {vault_root}"
        )

    zotero_sqlite_str = str(cfg.get("audit_zotero_sqlite") or DEFAULT_ZOTERO_SQLITE)
    zotero_storage_str = str(cfg.get("audit_zotero_storage") or DEFAULT_ZOTERO_STORAGE)

    threshold_raw = cfg.get("audit_annotations_read_threshold")
    try:
        threshold = int(threshold_raw) if threshold_raw is not None else DEFAULT_ANNOTATIONS_READ_THRESHOLD
    except (TypeError, ValueError):
        threshold = DEFAULT_ANNOTATIONS_READ_THRESHOLD
    if threshold < 0:
        threshold = DEFAULT_ANNOTATIONS_READ_THRESHOLD

    # ``or DEFAULT`` would coerce a user-saved empty string back to the
    # default, which conflicts with the engine's documented "empty
    # prefix means skip nothing" behaviour.  Only fall back when the
    # key is missing or explicitly None.
    skip_prefix_raw = cfg.get("audit_biblio_skip_prefix")
    if skip_prefix_raw is None:
        skip_prefix_value = DEFAULT_BIBLIO_SKIP_PREFIX
    else:
        skip_prefix_value = str(skip_prefix_raw)

    return Settings(
        vault_root=vault_root,
        attachments_subdir=str(
            cfg.get("audit_attachments_subdir") or DEFAULT_ATTACHMENTS_SUBDIR
        ),
        biblio_articles_subdir=str(
            cfg.get("audit_biblio_articles_subdir") or DEFAULT_BIBLIO_ARTICLES_SUBDIR
        ),
        zotero_notes_subdir=str(
            cfg.get("audit_zotero_notes_subdir") or DEFAULT_ZOTERO_NOTES_SUBDIR
        ),
        master_bib_path=str(
            cfg.get("audit_master_bib_path") or DEFAULT_MASTER_BIB_PATH
        ),
        zotero_sqlite=Path(zotero_sqlite_str).expanduser(),
        zotero_storage=Path(zotero_storage_str).expanduser(),
        ignored_dirs=DEFAULT_IGNORED_DIRS,
        biblio_skip_prefix=skip_prefix_value,
        annotations_read_threshold=threshold,
    )
