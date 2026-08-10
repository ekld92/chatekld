"""Pure widget-snapshot payload builder.

``build_payload`` turns the audit's cached ``Inventory`` into the
``widget.json`` shape: one *paper of the day* (date-seeded weighted random —
repeated same-day refreshes are stable, so "of the day" means something), one
primary completable suggestion (a bounded "tag N papers" micro-batch — a
completable batch motivates where an unbounded backlog count demotivates) plus
secondary counts, and one *notion of the day* from the ``_tags.md`` glosses.

Everything environmental is injected (synonyms, history, vocabulary, note /
PDF-text readers, clock, seed) so the module is deterministic under test and
imports no Flask/manager code. It imports the audit report builders the same
way ``audit/serialize.py``'s consumers do — read-only, over an inventory the
caller already holds.
"""

from __future__ import annotations

import logging
import random
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from audit.engine.reports import (
    finder_tag_drift as r_finder_tag_drift,
    note_tag_drift as r_note_tag_drift,
    zotero_unread as r_zotero_unread,
)
from widget.textclean import clean_display, clean_prose

logger = logging.getLogger(__name__)

PAYLOAD_VERSION = 1

# Excerpt bounds: enough for a widget card, far below any UI that would want
# the real note/PDF (one click away).
_EXCERPT_MAX_CHARS = 400
_TAG_BATCH_SIZE = 10


def _default_note_reader(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def summary_callout_excerpt(note_text: str) -> str | None:
    """Extract the body of the first ``> [!summary]`` callout, if any.

    The Note Refactor "Summarize a PDF" action writes exactly this shape
    (``> [!summary] …`` header line + ``> `` bullet lines). Returns the
    callout body flattened to plain text, or ``None`` when absent.
    """
    lines = (note_text or "").split("\n")
    out: list[str] = []
    in_callout = False
    for line in lines:
        stripped = line.strip()
        if not in_callout:
            if stripped.lower().startswith("> [!summary]"):
                in_callout = True
                rest = stripped[len("> [!summary]"):].strip(" -+")
                if rest:
                    out.append(rest)
            continue
        if stripped.startswith(">"):
            body = stripped.lstrip(">").strip()
            if body:
                out.append(body)
            continue
        break  # first non-quoted line ends the callout
    text = " ".join(out).strip()
    return text or None


def _clip(text: str, cap: int = _EXCERPT_MAX_CHARS) -> str:
    """Collapse whitespace, de-LaTeX when needed, and clip on a word boundary.

    Clipping mid-word ("computationnel…") reads as a rendering bug on a card;
    the trailing partial word is dropped when one can be found near the cap.
    """
    text = " ".join(clean_prose(text or "").split())
    if len(text) <= cap:
        return text
    cut = text[: cap - 1].rstrip()
    space = cut.rfind(" ")
    if space > cap * 0.7:  # keep the clip near the cap, not halfway back
        cut = cut[:space].rstrip()
    return cut.rstrip(",;:.— -") + "…"


def format_authors(authors: Iterable[str]) -> str | None:
    """Bibliographic short form: "Jardri", "Jardri & Dubé", "Jardri et al.".

    BibTeX authors arrive as ``"Last, First"``; joining several of them with
    a comma produces "Jardri, Renaud, Dubé, François", where a reader cannot
    tell names from surnames. Additive field — a widget that ignores it falls
    back to the full ``authors`` list, so the payload version stays 1.
    """
    surnames = []
    for raw in authors or ():
        name = str(raw or "").strip()
        if not name:
            continue
        surnames.append(
            name.split(",", 1)[0].strip() if "," in name
            else name.split()[-1])
    if not surnames:
        return None
    if len(surnames) == 1:
        return surnames[0]
    if len(surnames) == 2:
        return f"{surnames[0]} & {surnames[1]}"
    return f"{surnames[0]} et al."


def obsidian_uri(note_path: Path) -> str:
    """``obsidian://`` deep link for a note, built from its absolute path.

    Built here rather than in each widget: the widgets read only
    ``widget.json`` and would otherwise have to open ``config.json`` to learn
    the vault root. Additive field — a widget that ignores it is unaffected,
    so the payload version stays 1.
    """
    return "obsidian://open?path=" + urllib.parse.quote(str(note_path), safe="")


def _rel_to(p: Path, root: Path) -> str | None:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return None


def _is_under(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def build_payload(
    inv: Any,
    settings: Any,
    *,
    synonyms: dict[str, str] | None = None,
    history: Iterable[str] = (),
    notions: Mapping[str, str] | None = None,
    note_reader: Callable[[Path], str | None] | None = None,
    pdf_text_reader: Callable[[Path], str | None] | None = None,
    now: datetime | None = None,
    rng_seed: str | None = None,
    scan_finished_at: float | None = None,
) -> dict[str, Any]:
    """Build the ``widget.json`` payload from a cached scan.

    ``history`` is the anti-repeat list (recent citation keys — weight 0; when
    EVERY candidate is in it, history is ignored rather than emptying the
    pool). ``rng_seed`` defaults to the ISO date of ``now`` so same-day
    rebuilds pick the same paper; pass anything else (the refresh route's
    ``redraw``) to force a new draw. ``notions`` is the ``_tags.md``
    ``{tag: gloss}`` vocabulary (or ``None`` — the notion slot is omitted).
    """
    now = now or datetime.now(timezone.utc)
    seed = rng_seed if rng_seed is not None else now.date().isoformat()
    rng = random.Random(seed)
    history_set = {str(k) for k in history}

    records = list(getattr(inv, "records", {}).values())

    # --- paper of the day -------------------------------------------------
    candidates = [
        rec for rec in records
        if rec.bib_entry is not None and (rec.obsidian_note or rec.pdf_paths)
    ]
    fresh = [r for r in candidates if r.citation_key not in history_set]
    pool = sorted(fresh or candidates, key=lambda r: r.citation_key)
    paper: dict[str, Any] | None = None
    if pool:
        rec = rng.choice(pool)
        note_rel = (
            _rel_to(rec.obsidian_note.path, settings.vault_root)
            if rec.obsidian_note else None
        )
        excerpt: str | None = None
        excerpt_source: str | None = None
        if rec.obsidian_note is not None:
            reader = note_reader or _default_note_reader
            text = reader(rec.obsidian_note.path)
            if text:
                cal = summary_callout_excerpt(text)
                if cal:
                    excerpt, excerpt_source = _clip(cal), "summary"
        if excerpt is None and pdf_text_reader is not None and rec.pdf_paths:
            try:
                text = pdf_text_reader(rec.pdf_paths[0])
            except Exception:  # noqa: BLE001 — excerpt is best-effort
                text = None
            if text:
                excerpt, excerpt_source = _clip(text), "pdf"
        bib = rec.bib_entry
        # Bib fields are LaTeX SOURCE; the widgets have no TeX renderer, so
        # they are resolved here, once, for display only — never back into the
        # Record (the fingerprint and every PDF↔bib join key on the raw value).
        authors = [a for a in (clean_display(x) for x in (bib.authors or []))
                   if a]
        paper = {
            "key": rec.citation_key,
            "title": clean_display(bib.title),
            "authors": authors,
            "author_line": format_authors(authors),
            "year": bib.year,
            "note_rel": note_rel,
            "note_uri": (obsidian_uri(rec.obsidian_note.path)
                         if rec.obsidian_note is not None else None),
            "excerpt": excerpt,
            "excerpt_source": excerpt_source,
        }

    # --- suggestions ------------------------------------------------------
    biblio = Path(settings.biblio_articles_dir)
    untagged = sum(
        1 for rec in records
        if any(_is_under(Path(p), biblio) for p in rec.pdf_paths)
        and not rec.finder_tag_set and not rec.obs_tags
    )
    suggestions: list[dict[str, Any]] = [{
        "kind": "tag_batch",
        "label": f"Tag {min(_TAG_BATCH_SIZE, untagged)} papers",
        "count": untagged,
        "batch": min(_TAG_BATCH_SIZE, untagged),
    }]
    # Secondary counts are best-effort: a report builder failing must not
    # sink the snapshot (the widget renders what it gets).
    for kind, fn in (
        ("finder_drift", lambda: len(
            r_finder_tag_drift.find_drift(inv, synonyms=synonyms))),
        ("note_drift", lambda: len(
            r_note_tag_drift.find_drift(inv, synonyms=synonyms))),
        ("unread", lambda: len(r_zotero_unread.find(inv).rows)),
    ):
        try:
            suggestions.append({"kind": kind, "count": fn()})
        except Exception:  # noqa: BLE001
            logger.warning("widget snapshot: %s count failed", kind, exc_info=True)

    # --- notion of the day ------------------------------------------------
    notion: dict[str, str] | None = None
    glossed = sorted(
        (t, g) for t, g in (notions or {}).items() if (g or "").strip())
    if glossed:
        tag, gloss = rng.choice(glossed)
        notion = {"tag": tag, "gloss": gloss}

    return {
        "version": PAYLOAD_VERSION,
        "generated_at": now.isoformat(),
        "scan_finished_at": scan_finished_at,
        "paper": paper,
        "suggestions": suggestions,
        "notion": notion,
    }
