"""Per-section checkpoint + resume for deck generation.

A deck generation is a sequence of expensive agent turns (one outline + one per
section). On a local backend a late section can fail (a memory hiccup, a model
reload) and — even with the per-section retry of Phase 1 — a cancel or an
unrecoverable failure would otherwise force regenerating every prior section on
the next run. This module persists the parsed outline and each completed section
**immediately**, keyed by a hash of the generation inputs, so re-submitting the
SAME request resumes from the first not-yet-generated section. On a fully
successful generation the route deletes the checkpoint.

Pure, app-independent (``json`` + ``hashlib`` + ``os`` + an atomic writer only —
no app imports, no third-party), like ``scaffold.py`` / ``review.py``. The deck
route owns ``BASE_DIR`` and passes the checkpoints directory in; this module owns
the manifest shape, the job key, atomic load/save/delete, ``SectionOutput`` /
``Section`` (de)serialisation, and pruning.

The **job key** hashes only the *content-determining* inputs (topic,
instructions, template, provider/model, max_sections, audience, citations, slug,
out_dir) — deliberately NOT the sampling/retry knobs (temperature, attempts,
backoff, section token cap), so re-running after tweaking a generation knob still
resumes the same deck rather than starting over.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from typing import Optional

from .assemble import SectionOutput
from .outline import Section

_VERSION = 2
_MAX_KEEP = 10


def generation_context(vault_root: str, *, embed: str, generation: int = 0,
                       index_dir: str = "", embed_provider: str = "") -> dict:
    """Bind to canonical vault and read-only persistent corpus observations.

    Stat only known index files; no index loading, embedding or reconstruction.
    In-process generation catches changes before a persistence flush; disk
    signatures prevent cross-restart reuse after the stored corpus changes.
    """
    signatures: dict[str, list[int] | None] = {}
    if index_dir:
        for name in ("obsidian_meta.json", "docstore.json", "index_store.json"):
            try:
                st = os.stat(os.path.join(index_dir, name))
                signatures[name] = [st.st_ino, st.st_size, st.st_mtime_ns]
            except FileNotFoundError:
                signatures[name] = None
    return {"vault_root": os.path.realpath(vault_root) if vault_root else "",
            "embed": embed, "embed_provider": embed_provider,
            "index_generation": generation, "index_files": signatures}


def compute_job_key(
    *,
    topic: str,
    instructions: str,
    template_tex: str,
    provider: str,
    model: str,
    max_sections: int,
    audience: str,
    citations_enabled: bool,
    slug: str,
    out_dir: str,
    images_enabled: bool = False,
    context: dict | None = None,
    _legacy: bool = False,
) -> str:
    """Stable hex digest naming this generation's checkpoint file.

    Any change to a content-determining input yields a different key (so a new
    request never resumes a stale checkpoint); the file *is* the guard, so no
    separate inputs-hash is stored.

    ``images_enabled`` (derived from ``cfg["vault_image_exts"]``) shapes the
    section system prompt's ``\\includegraphics`` policy, so it IS
    content-determining — omitting it (pre-2026-07-20) let a run resume
    sections generated under the opposite image policy. It defaults False so a
    caller that doesn't pass it is unaffected.

    Fields are LENGTH-PREFIXED, not `\\x00`-separated: a field can itself
    contain a NUL (JSON ``\\u0000`` survives coercion), so the old separator
    let ``("a\\x00","b")`` and ``("a","\\x00b")`` collide onto one key. The
    explicit byte length before each field makes the boundary unforgeable.
    """
    h = hashlib.sha256()
    prefix = ("v1",) if _legacy else (f"v{_VERSION}", json.dumps(context or {}, sort_keys=True))
    for part in (*prefix,
        topic or "",
        instructions or "",
        template_tex or "",
        provider or "",
        model or "",
        str(int(max_sections)),
        audience or "",
        "1" if citations_enabled else "0",
        slug or "",
        out_dir or "",
        "1" if images_enabled else "0",
    ):
        data = part.encode("utf-8", "replace")
        # Length prefix (decimal + NUL delimiter): the length is derived from
        # the field itself, so no field content can forge a boundary.
        h.update(f"{len(data)}\x00".encode("ascii"))
        h.update(data)
    return h.hexdigest()


def compute_legacy_job_keys(*, context: dict, **kwargs) -> list[str]:
    """Locate v1 and pre-provider-bound v2 manifests without discarding them."""
    previous_context = {key: value for key, value in context.items() if key != "embed_provider"}
    return [compute_job_key(_legacy=True, **kwargs),
            compute_job_key(context=previous_context, **kwargs)]


def _path(checkpoints_dir: str, job_key: str) -> str:
    return os.path.join(checkpoints_dir, f"{job_key}.json")


def _section_to_dict(s: SectionOutput) -> dict:
    return {
        "title": s.title,
        "body": s.body,
        "raw": s.raw,
        "infos": list(s.infos or []),
        "placeholder": bool(s.placeholder),
    }


def _section_from_dict(d: dict) -> SectionOutput:
    return SectionOutput(
        title=str(d.get("title", "")),
        body=str(d.get("body", "")),
        raw=str(d.get("raw", "")),
        infos=list(d.get("infos", []) or []),
        placeholder=bool(d.get("placeholder", False)),
    )


def _outline_to_list(sections) -> list:
    return [{"title": s.title, "points": list(s.points or [])} for s in sections]


def outline_from_list(data) -> list:
    """Rebuild a list of :class:`Section` from the manifest's ``outline`` field."""
    out = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and str(item.get("title", "")).strip():
                pts = item.get("points") or []
                points = [str(p) for p in pts] if isinstance(pts, list) else []
                out.append(Section(title=str(item["title"]).strip(), points=points))
    return out


def new_manifest(*, job_key: str, topic: str, slug: str, out_dir: str, sections, context: dict | None = None) -> dict:
    """A fresh manifest holding the parsed outline and no generated sections yet."""
    return {
        "version": _VERSION,
        "context": context or {},
        "job_key": job_key,
        "topic": topic,
        "slug": slug,
        "out_dir": out_dir,
        "outline": _outline_to_list(sections),
        "sections": {},  # str(index) -> section dict
    }


def get_section(manifest: dict, index: int) -> Optional[SectionOutput]:
    """Return the saved :class:`SectionOutput` for 1-based *index*, or None."""
    raw = (manifest.get("sections") or {}).get(str(index))
    return _section_from_dict(raw) if isinstance(raw, dict) else None


def set_section(manifest: dict, index: int, section: SectionOutput) -> None:
    """Record a completed section into *manifest* (in memory; caller then saves)."""
    manifest.setdefault("sections", {})[str(index)] = _section_to_dict(section)


def completed_count(manifest: dict) -> int:
    return len(manifest.get("sections") or {})


def load(checkpoints_dir: str, job_key: str, *, context: dict | None = None, legacy_job_key: str | list[str] | None = None) -> Optional[dict]:
    """Load a manifest, or None if absent/unreadable.

    Legacy/unbound manifests and mismatched contexts are explicitly refused;
    their files are preserved. This versioning concerns generation only,
    never persisted retrieval data.
    """
    path = _path(checkpoints_dir, job_key)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        legacy_keys = [legacy_job_key] if isinstance(legacy_job_key, str) else (legacy_job_key or [])
        if any(os.path.lexists(_path(checkpoints_dir, key)) for key in legacy_keys):
            raise ValueError("Checkpoint resume refused: legacy manifest has no fully bound vault/embedding context; choose a fresh generation")
        return None
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if (data.get("version") != _VERSION or not data.get("context")
            or "embed_provider" not in data["context"]):
        raise ValueError("Checkpoint resume refused: legacy manifest has no bound vault context")
    if context is not None and data.get("context") != context:
        raise ValueError("Checkpoint resume refused: vault or generation context changed")
    if data.get("job_key") != job_key:
        return None
    data.setdefault("sections", {})
    data.setdefault("outline", [])
    return data


def save(checkpoints_dir: str, manifest: dict) -> None:
    """Atomically write *manifest* (temp sibling + ``os.replace``); best-effort.

    A checkpoint-write failure must never abort generation — the deck still lands
    on disk; only the resume capability is forfeited. The caller treats this as
    best-effort and does not propagate the error.
    """
    os.makedirs(checkpoints_dir, exist_ok=True)
    path = _path(checkpoints_dir, manifest["job_key"])
    fd, tmp = tempfile.mkstemp(dir=checkpoints_dir, prefix=".ckpt-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def delete(checkpoints_dir: str, job_key: str) -> None:
    """Remove a checkpoint (best-effort; a missing file is not an error)."""
    try:
        os.unlink(_path(checkpoints_dir, job_key))
    except OSError:
        pass


#: A `.ckpt-*.tmp` younger than this may belong to a save in flight.
_TMP_ORPHAN_AGE_S = 3600


def _prune_tmp_orphans(checkpoints_dir: str) -> None:
    """Unlink temp files a killed process left behind (see prune's note)."""
    cutoff = time.time() - _TMP_ORPHAN_AGE_S
    try:
        names = os.listdir(checkpoints_dir)
    except OSError:
        return
    for name in names:
        if not (name.startswith(".ckpt-") and name.endswith(".tmp")):
            continue
        path = os.path.join(checkpoints_dir, name)
        try:
            if os.path.getmtime(path) >= cutoff:
                continue        # possibly a save in flight
            os.unlink(path)
        except OSError:
            pass


def prune(checkpoints_dir: str, max_keep: int = _MAX_KEEP) -> None:
    """Keep only the *max_keep* most recently modified checkpoints; drop the rest.

    Bounds disk use from abandoned generations. Best-effort: any stat/unlink error
    on one file is ignored so a single bad entry can't break pruning.

    # DURABILITY (2026-09-02, performance plan Phase 5): orphan temp files are
    # reclaimed too.
    # Before: `save` writes through a `.ckpt-*.tmp` sibling and unlinks it on a
    #   normal failure, but a SIGKILL (or a power loss) between mkstemp and
    #   os.replace leaves the temp file behind — and prune only ever listed
    #   `.json`, so nothing could ever see or remove one. They accumulate for
    #   the life of the install, one per killed generation, each holding a
    #   whole outline plus every completed section.
    # Change: prune also unlinks `.ckpt-*.tmp` files older than
    #   _TMP_ORPHAN_AGE_S.
    # Safe because: the age gate is what makes this safe to run beside a LIVE
    #   save — a temp file younger than the gate may belong to a generation
    #   currently writing, and is left alone; an hour is far longer than the
    #   milliseconds between mkstemp and the rename.
    # Pinned by: deckgen/tests/test_deckgen.py::TestCheckpointTmpOrphans.
    """
    _prune_tmp_orphans(checkpoints_dir)
    try:
        entries = [
            os.path.join(checkpoints_dir, n)
            for n in os.listdir(checkpoints_dir)
            if n.endswith(".json")
        ]
    except OSError:
        return
    if len(entries) <= max_keep:
        return
    def _mtime(p):
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0
    for path in sorted(entries, key=_mtime, reverse=True)[max_keep:]:
        try:
            os.unlink(path)
        except OSError:
            pass
