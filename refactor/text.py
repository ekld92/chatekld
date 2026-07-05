"""Deterministic, third-party-free text transforms for the analyzer.

Currently one transform: :func:`strip_ocr_preamble`, the per-image "strip
metadata" opt-in. The indexer's image descriptions are produced by a prompt that
asks the model to *first* state what the image depicts (one or two sentences)
*then* transcribe the visible text (``services/vision.py::describe_image``), so a
cached description looks like::

    This image is a medical classification outlining diagnostic criteria, …
    Transcribed Text: ITEM 64 : TROUBLES ANXIEUX …

When the user judges an OCR successful they often want only the transcription,
without the leading descriptive sentence and the ``Transcribed Text:`` /
``Transcription:`` label. This module strips exactly that boilerplate with a
conservative, reversible heuristic: it **never** invents text and falls back to
returning the description unchanged when it cannot confidently find the preamble,
so a description with no boilerplate is passed through verbatim.

Pure / no project imports (mirrors the third-party-free posture of
``refactor.result``) so it stays trivial to unit-test.
"""
from __future__ import annotations

import re

# The transcription marker the description prompt's output uses to separate the
# "what it depicts" preamble from the transcription. Tolerant of:
#   * EN "Transcribed Text:" / "Transcription:" / "Transcribed:"
#   * FR "Texte transcrit:" / "Transcription du texte:"
#   * surrounding markdown emphasis ("**Transcription:**")
# Deliberately NOT anchored to a line start: the model usually writes the marker
# mid-paragraph ("…acronyms. Transcription: Th anieux …"). Requires the trailing
# colon so a bare "transcription services" can never match.
_MARKER_RE = re.compile(
    r"(?:transcribed\s+text|transcription(?:\s+du\s+texte)?|texte\s+transcrit|transcribed)"
    r"\s*\**\s*:\s*\**\s*",  # tolerate "Transcription :" and markdown-bold "**…:**"
    re.IGNORECASE,
)

# Fallback when there is no marker: a leading descriptive opener sentence we can
# safely drop. Anchored at the start; matches up to the first sentence-ending
# punctuation followed by whitespace, so only the FIRST "This image is …" /
# "Cette image …" sentence is removed and the rest is kept intact.
#
# The "this is a/an …" branch is deliberately noun-agnostic: an empirical scan of
# the cached descriptions showed the dominant un-stripped openers were
# "This is a diagram / flowchart / screenshot / presentation / line graph …" —
# i.e. the model names a depiction noun the old image|page|figure|photo whitelist
# did not cover, so ~12 % of descriptions passed through verbatim. Matching any
# "This is a/an <noun>" opener (still only the FIRST sentence, still passthrough
# when no sentence boundary is found) closes that gap without re-indexing.
#
# Known, ACCEPTED tradeoff of the noun-agnostic branch (do not "fix" silently):
#   * It can strip a genuine first sentence that happens to begin "This is a/an …"
#     — e.g. a transcription whose real text is "This is a list of contraindications."
#   * The first sentence boundary can land on an abbreviation, mangling
#     "This is a Fig. 2 showing …" into "2 showing …".
# This is tolerated because the strip is OPT-IN per image (or scope-wide default),
# never empties a description (see strip_ocr_preamble's "rest or description"
# guard), and is fully reversible by re-planning with the strip flag off — so a
# rare false positive is recoverable, unlike a missed strip on 12 % of images.
_OPENER_RE = re.compile(
    r"\s*(?:"
    r"this image|the image|this document|this appears to be|"
    r"this is an?\s+|"  # "This is a diagram / presentation / scanned page …" (any noun)
    r"this (?:scanned\s+)?(?:image|page|figure|photo)|"
    r"cette image|l['’]image|il s['’]agit"
    r")\b.*?[.!?](?:\s+|$)",
    re.IGNORECASE | re.DOTALL,
)


def strip_ocr_preamble(description: str) -> str:
    """Return *description* with its descriptive preamble + label removed.

    1. If a transcription marker (``Transcribed Text:`` / ``Transcription:`` …)
       is present, keep only the text after it.
    2. Otherwise, if the description opens with a "This image is …" /
       "Cette image …" sentence, drop just that sentence.
    3. Otherwise return the description unchanged.

    Never returns an empty string when given a non-empty one: if a strip would
    consume everything, the original is returned (an empty callout would be
    worse than a slightly verbose one).
    """
    if not description or not description.strip():
        return description

    m = _MARKER_RE.search(description)
    if m:
        tail = description[m.end():].strip()
        return tail or description

    m2 = _OPENER_RE.match(description)
    if m2:
        rest = description[m2.end():].strip()
        return rest or description

    return description
