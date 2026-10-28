"""Build a classification document from bibliographic metadata (no PDF needed).

The sibling of ``text_source.py``: where that one extracts text from a PDF,
this one assembles what is knowable about an entry that has **no local PDF at
all**. 2 081 of the maintainer's 6 122 bib entries are in that state — papers
tracked in Zotero whose file was never downloaded — and until 2026-08-11 they
had no classification path whatsoever: propose returned ``no_text`` and stopped.
(Measured 2026-08-12 against the live inventory. An earlier note here said
~4 100, a bib-keywords proxy counting a different set; CHANGELOG:91 already
carried the correction while this docstring, the feature's primary rationale
doc, still quoted the retracted number.)

Pure and stdlib-only, like ``vocab.py``: it takes primitives rather than an
audit ``Record`` so it can be unit-tested without building an inventory, and so
``tagging/`` gains no new dependency on the vendored ``audit/`` tree's shapes.

The abstract is the substance. Everything else (title, authors, year, item
type) is thin on its own — a title alone yields confident-looking guesses off a
handful of words, which is worse than no proposal. :func:`is_classifiable` is
the gate that decides whether there is enough here to be worth an LLM call at
all, and it deliberately does NOT count the title: a title is always present,
so counting it would make every entry look classifiable.
"""

from __future__ import annotations

# Minimum characters of SUBSTANTIVE metadata (abstract + keywords; title,
# authors and year excluded — see the module docstring) before a metadata-only
# proposal is worth making. An abstract is typically 700-2000 characters; a
# keyword list alone can be enough when it is a real subject list. Below this
# the honest answer is "not enough to classify", which the caller reports as
# `no_text` exactly as it did before this module existed.
MIN_SUBSTANTIVE_CHARS = 120


def build_document(*, title=None, authors=None, year=None, item_type=None,
                   abstract=None, keywords=None, max_chars=8000):
    """Assemble the metadata block passed to the classifier as its source text.

    Returns ``(document, substantive_chars)``. ``substantive_chars`` counts only
    the abstract and keywords — the fields that actually carry topic signal —
    and is what :func:`is_classifiable` thresholds.

    Field order puts the abstract last so that truncation at *max_chars* eats
    the abstract's tail rather than losing the compact fields entirely; the
    compact fields cost tens of characters and are never worth dropping.
    """
    def _clean(value):
        return " ".join(str(value).split()) if value else ""

    abstract_s = _clean(abstract)
    kw_list = [k for k in (_clean(k) for k in (keywords or [])) if k]
    keywords_s = ", ".join(sorted(set(kw_list)))
    substantive = len(abstract_s) + len(keywords_s)

    lines = []
    if title:
        lines.append(f"TITLE: {_clean(title)}")
    if authors:
        shown = [_clean(a) for a in list(authors)[:8] if _clean(a)]
        if shown:
            more = "" if len(list(authors)) <= 8 else " et al."
            lines.append(f"AUTHORS: {'; '.join(shown)}{more}")
    if year:
        lines.append(f"YEAR: {_clean(year)}")
    if item_type:
        lines.append(f"ITEM TYPE: {_clean(item_type)}")
    if keywords_s:
        lines.append(f"EXISTING KEYWORDS: {keywords_s}")
    if abstract_s:
        lines.append(f"ABSTRACT: {abstract_s}")

    document = "\n".join(lines)
    if len(document) > max_chars:
        document = document[:max_chars].rstrip()
    return document, substantive


def is_classifiable(substantive_chars):
    """True when there is enough topic-bearing metadata to justify an LLM call."""
    return substantive_chars >= MIN_SUBSTANTIVE_CHARS


def from_record(rec, *, max_chars=8000, exclude_keywords=None):
    """Convenience wrapper over an audit ``Record``. Returns ``(document, chars)``.

    Reads the bib entry for the bibliographic fields and the Zotero item for the
    abstract — ``_master.bib`` has no abstract field (Better BibTeX does not
    export one by default), so Zotero's ``abstractNote`` is the only source, and
    it is why ``ZoteroItem.abstract`` was added alongside this module. Every
    access is defensive: a record may carry any subset of its four source slots.

    *exclude_keywords* (2026-08-12) drops keywords the caller does not want fed
    back to the classifier — in practice the controlled vocabulary, because
    ``zot.tags`` is **exactly what ``apply-zotero-tags`` writes**, and
    ``audit/core/zotero.py`` documents those tags as equal to the bib
    ``keywords`` field once BBT has exported them. Without this a paper this
    app tagged last week has its own tags presented back to the model as
    EXISTING KEYWORDS, which biases it toward re-proposing them and spends the
    ``max_tags`` budget confirming what is already there. Measured: 217 of the
    1155 classifiable PDF-less records (19%) were in that state.

    Matching is case-insensitive on a stripped, ``#``-less form, since a vault
    id (``dep``) and a Zotero tag (``Dep``) are the same tag. Author-supplied
    keywords that merely coincide with a vocabulary id are lost with them —
    accepted, because there is no way to tell the two apart: ``itemTags.type``
    would separate imported from manual tags, but the maintainer's library has
    type 0 on all 17 673 of them, so the distinction does not survive in the
    data.
    """
    drop = {str(k).strip().lstrip("#").strip().lower()
            for k in (exclude_keywords or ())}

    def _keep(value):
        return str(value).strip().lstrip("#").strip().lower() not in drop

    bib = getattr(rec, "bib_entry", None)
    zot = getattr(rec, "zotero_item", None)
    keywords = set()
    if bib is not None:
        keywords |= set(getattr(bib, "keywords", None) or [])
    if zot is not None:
        keywords |= set(getattr(zot, "tags", None) or [])
    if drop:
        keywords = {k for k in keywords if _keep(k)}
    return build_document(
        title=(getattr(bib, "title", None) if bib else None)
              or (getattr(zot, "title", None) if zot else None),
        authors=(getattr(bib, "authors", None) if bib else None),
        year=(getattr(bib, "year", None) if bib else None),
        item_type=(getattr(zot, "item_type", None) if zot else None)
                  or (getattr(bib, "entry_type", None) if bib else None),
        abstract=(getattr(zot, "abstract", None) if zot else None),
        keywords=sorted(keywords),
        max_chars=max_chars,
    )
