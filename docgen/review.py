"""Safety gate for an article augmentation/repair proposal (docgen).

The article twin of ``deckgen.review.screen_repair``: it vets a proposed ``.tex``
before it can be offered/applied, refusing an empty/no-op change, one that
introduces a dangerous compile-time macro the original lacked, or one that broke
the document structure. The ONLY difference from the Beamer screen is the
structural gate — an article's floor is "exactly one document env and at least
one ``\\section``" (it has no frames). Reuses the shared substrate detectors so
the danger screen can never disagree with generate-time ``validate_article``.

Pure and app-independent (no LLM/transport import), like ``docgen.article``.
"""
from __future__ import annotations

import re
from typing import Optional

from deckgen.latex import _SECTION_RE, _strip_latex_comments, count_dangerous_macros
from docgen.article import validate_article

_DOC_BEGIN_RE = re.compile(r"\\begin\{document\}")
_DOC_END_RE = re.compile(r"\\end\{document\}")


def screen_article_repair(
    original_tex: str,
    repaired_tex: Optional[str],
) -> tuple[Optional[str], list[str]]:
    """Vet a proposed article repair/augmentation. Returns ``(accepted_tex, warnings)``.

    ``accepted_tex`` is ``None`` when the proposal is missing, a no-op, unsafe, or
    structurally broken — *warnings* then explains why (a silent discard of the
    proposal only; the on-disk document is untouched). When accepted, *warnings*
    carries the residual :func:`~docgen.article.validate_article` findings so the
    UI can still flag a proposal that did not fully succeed.

    Refusal conditions:
    * empty / identical to the original (nothing to apply);
    * introduces a dangerous compile-time macro the original did not contain;
    * does not contain exactly one ``document`` environment, or has no ``\\section``.
    """
    if not repaired_tex or not repaired_tex.strip():
        return None, []
    if repaired_tex.strip() == (original_tex or "").strip():
        return None, []

    new_scan = _strip_latex_comments(repaired_tex)

    # Occurrence counts, not a name-set diff — parity with
    # deckgen.review.screen_repair (2026-07-22 audit M4): an article already
    # using one \input must not license a repair that adds more.
    old_counts = count_dangerous_macros(original_tex or "")
    new_counts = count_dangerous_macros(repaired_tex)
    introduced = sorted(
        name for name, n in new_counts.items() if n > old_counts.get(name, 0)
    )
    if introduced:
        return None, [
            "Discarded the proposed change: it introduced (or added occurrences "
            f"of) potentially-unsafe macro(s) {', '.join(introduced)} beyond "
            "what the original document contained."
        ]

    if len(_DOC_BEGIN_RE.findall(new_scan)) != 1 or len(_DOC_END_RE.findall(new_scan)) != 1:
        return None, [
            "Discarded the proposed change: it does not contain exactly one "
            "document environment."
        ]
    if not _SECTION_RE.search(new_scan):
        return None, [
            "Discarded the proposed change: it left the article with no \\section."
        ]

    return repaired_tex, validate_article(repaired_tex)
