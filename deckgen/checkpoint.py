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
from typing import Optional

from .assemble import SectionOutput
from .outline import Section

_VERSION = 1
_MAX_KEEP = 10


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
) -> str:
    """Stable hex digest naming this generation's checkpoint file.

    Any change to a content-determining input yields a different key (so a new
    request never resumes a stale checkpoint); the file *is* the guard, so no
    separate inputs-hash is stored.
    """
    h = hashlib.sha256()
    for part in (
        f"v{_VERSION}",
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
    ):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")  # length-prefix-free field separator
    return h.hexdigest()


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


def new_manifest(*, job_key: str, topic: str, slug: str, out_dir: str, sections) -> dict:
    """A fresh manifest holding the parsed outline and no generated sections yet."""
    return {
        "version": _VERSION,
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


def load(checkpoints_dir: str, job_key: str) -> Optional[dict]:
    """Load a checkpoint manifest, or None if absent / unreadable / wrong version.

    Tolerant by design: a corrupt or version-mismatched file is treated as "no
    checkpoint" (the run starts fresh) rather than raising.
    """
    path = _path(checkpoints_dir, job_key)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != _VERSION:
        return None
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


def prune(checkpoints_dir: str, max_keep: int = _MAX_KEEP) -> None:
    """Keep only the *max_keep* most recently modified checkpoints; drop the rest.

    Bounds disk use from abandoned generations. Best-effort: any stat/unlink error
    on one file is ignored so a single bad entry can't break pruning.
    """
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
