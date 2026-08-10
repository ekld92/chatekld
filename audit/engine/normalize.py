"""Shared tag-normalisation keys for the audit's tag reports (plan 2026-08-05/2).

One comparison key for "is this the same tag?", used by BOTH the Tag Hygiene
clustering and the Note Tag Drift report (and, at the route layer, the
``apply-tag-drift`` server-side intersection). Before this module existed the
two reports disagreed: hygiene folded case/accents/separators via the
OpenRefine fingerprint while drift compared raw strings — so ``Dépression`` vs
``dépression`` was reported as drift, survived the route's ``.lower()`` check,
and then no-opped in the writer's case-insensitive dedup ("Tags already
present"), leaving a row that could never be cleared.

Vendored-tree rule: the ``audit/`` tree depends only on ``audit/config.py``,
so this module is stdlib-only and must not import ``core.utils`` (the same
reason ``engine/bridge.py`` keeps its own atomic writer). The fingerprint
recipe matches the private ``rag/thesaurus.py::_term_key`` /
``inventory.py::_norm_title`` family, which are pinned to cache/join keys and
stay untouched.
"""

from __future__ import annotations

import re
import unicodedata

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def clean_tag(value: str) -> str:
    """Surface-level tag cleanup: strip whitespace and any leading ``#``.

    ``strip`` runs on BOTH sides of the ``lstrip('#')`` — the historical
    Obsidian reader stripped first and unhashed second, so ``"# tag"`` became
    ``" tag"`` (leading space kept); hygiene and the frontmatter writer always
    used the three-step form. This helper is that three-step form.
    """
    return str(value or "").strip().lstrip("#").strip()


def fingerprint(value: str) -> str:
    """NFKD → strip combining marks → casefold → sorted ``[a-z0-9]+`` tokens.

    OpenRefine's fingerprint-keying recipe (the lowest-false-positive method in
    its clustering catalog): folds case, accents, compatibility forms, all
    punctuation/separators (``-``/``_``/``/``/space) and token order.
    """
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(sorted(_TOKEN_RE.findall(stripped.casefold())))


def canonical_tag_key(value: str, synonyms: dict[str, str] | None = None) -> str:
    """``tag_key`` passed through the optional synonym equivalence map.

    THE single lookup rule for anything that must agree with a drift map
    built via :func:`group_by_key` — the 2026-08-05 verification pass found
    the apply route re-deriving a plain ``tag_key`` against a
    synonym-canonicalised map, which made every synonym-remapped missing tag
    unapplyable (the same unresolvable-row loop plan 2 closed for case
    variants, reintroduced through the synonym path).
    """
    key = tag_key(value)
    if synonyms:
        key = synonyms.get(key, key)
    return key


def group_by_key(
    tags: set[str] | list[str],
    synonyms: dict[str, str] | None = None,
) -> dict[str, set[str]]:
    """Group raw tag surfaces as ``{comparison key: {cleaned surface forms}}``.

    The comparison key is :func:`tag_key`, optionally canonicalised through
    *synonyms* — a ``{tag_key: canonical tag_key}`` equivalence map (plan
    2026-08-05/3: built app-side from the vault's curated ``_tags.md`` and
    injected by the route layer; the vendored tree never resolves it itself).
    Cleaning applies to every surface (Zotero tags arrive verbatim from
    SQLite; a ``#``-prefixed or padded tag must not become permanent drift).
    """
    out: dict[str, set[str]] = {}
    for raw in tags:
        cleaned = clean_tag(raw)
        if not cleaned:
            continue
        out.setdefault(canonical_tag_key(cleaned, synonyms), set()).add(cleaned)
    return out


def tag_key(value: str) -> str:
    """The comparison key: ``fingerprint(clean_tag(value))`` with a fallback.

    A tag written in a non-Latin script (Greek, Cyrillic, CJK) fingerprints to
    ``""`` — Tag Hygiene simply drops those, but a *comparison* key must not
    collapse every non-Latin tag onto one empty key, so the fallback is
    NFC + casefold of the cleaned surface (case-folded exact match, no accent
    or separator folding — conservative, never wrong).
    """
    cleaned = clean_tag(value)
    fp = fingerprint(cleaned)
    if fp:
        return fp
    return unicodedata.normalize("NFC", cleaned).casefold()
