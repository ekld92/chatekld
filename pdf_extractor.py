"""
pdf_extractor.py — Layout-aware PDF text extraction for scientific articles
============================================================================
Extracts and structures text from research-article PDFs using PyMuPDF's
block-level layout analysis.  The approach is inspired by three projects:

* **scipdf_parser** — section-level structural parsing of scientific papers.
  We replicate this by detecting section headers through font-size heuristics
  and mapping them to canonical scientific sections (Abstract, Methods, etc.).

* **PDF-Extract-Kit** — robust handling of figures, tables, and formulas.
  We identify and filter out non-body elements (images, captions, table grids,
  mathematical notation) so they do not pollute the text sent to the LLM.

* **gemma3_pdf_summarizer** — optimised context-window usage.  We provide
  a structured ``ArticleSections`` result that downstream code can chunk
  intelligently rather than treating the PDF as an opaque string.

Text extraction uses a three-tier offline pipeline — no external services,
no API keys, and no internet connection required:

  1. PyMuPDF (fitz) block-level layout analysis (primary).
  2. MarkItDown / pdfminer.six text-stream extraction (middle-tier fallback,
     optional — gracefully skipped if markitdown is not installed).
  3. GLM-OCR full-page rendering via the ``ocr_cb`` callback (last resort for
     scanned / image-only PDFs; requires the caller to supply the callback).

All three tiers are fully local and offline.

Public API
----------
get_pdf_page_count(file_path)
    → int total page count without decoding any page content

extract_structured_from_pdf(file_path, char_budget, ..., start_page, end_page, ocr_cb)
    → ``ArticleSections`` with per-section text and metadata

truncate_sections_for_context(article, max_chars)
    → (trimmed_text, was_truncated) for the model context window

EXTRACT_MAX_PAGES_PER_CALL
    Module-level cap on pages per extraction call. Callers that need to
    process larger documents (e.g. vault textbook indexing) must split into
    multiple calls with explicit start_page / end_page.
"""

import io
import logging
import math
import re
import base64
from dataclasses import dataclass, field
from typing import Callable, Optional

import fitz  # PyMuPDF

# Suppress MuPDF's C-level error messages (e.g. "could not parse color space")
# from being printed to stderr.  These are non-fatal internal warnings that
# PyMuPDF handles gracefully; surfacing them to the terminal confuses users.
# Python-level PDFExtractionError is still raised for real failures.
fitz.TOOLS.mupdf_display_errors(False)

# PIL is used for image dimension alignment in the GLM-OCR fallback path.
# It is an optional dependency — if absent, alignment is skipped and the
# original PNG bytes are passed through (model may crash on bad dimensions).
try:
    from PIL import Image as _PILImage
    _PIL_AVAILABLE = True
except ImportError:
    _PILImage = None  # type: ignore[assignment]
    _PIL_AVAILABLE = False

# MarkItDown: Microsoft's document-to-Markdown converter.
# Used as a second-tier fallback between PyMuPDF block parsing (tier 1) and
# GLM-OCR (tier 3).  MarkItDown uses pdfminer.six internally — a different
# text-stream parsing algorithm that succeeds on PDFs where PyMuPDF's block
# layout engine yields empty or filtered-out blocks.
# Import-guarded: absent = MarkItDown tier is silently skipped and the pipeline
# falls directly to OCR as before.
# IMPORTANT: MarkItDown is ALWAYS instantiated without llm_client here.
# pdf_extractor.py is a dependency-free extraction module; it does not import
# summarizer.py and must not create a circular import to reach VisionManager.
# Vision descriptions for PDF figures are handled upstream in summarizer.py.
try:
    from markitdown import MarkItDown as _MarkItDownExtractor
    _MARKITDOWN_AVAILABLE: bool = True
except ImportError:
    _MarkItDownExtractor = None  # type: ignore[assignment]
    _MARKITDOWN_AVAILABLE: bool = False

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class PDFExtractionError(Exception):
    """Raised when a PDF cannot be read or contains no usable text."""
    pass


def get_pdf_page_count(file_path: str) -> int:
    """Return the total page count of a PDF without extracting any text.

    WHY a dedicated probe function?  ``extract_structured_from_pdf`` opens the
    file and iterates over every page, decoding block layout as it goes.
    Calling it just to learn the page count wastes significant CPU time.
    Opening the document and reading ``len(doc)`` is a metadata-only operation
    that PyMuPDF completes in microseconds regardless of the file's size,
    because it only parses the cross-reference table — not any page content.

    Used by ``_load_pdf_with_summary`` in ``summarizer.py`` to decide before
    extraction whether a PDF is large enough to warrant page-range chunking,
    so the decision is free from an I/O standpoint.

    Parameters
    ----------
    file_path : str
        Path to the PDF file.

    Returns
    -------
    int
        Number of pages in the document, or 0 if the file cannot be opened
        (so the caller falls through to normal extraction, which will surface
        a more descriptive error via PDFExtractionError).
    """
    try:
        with fitz.open(file_path) as doc:
            # len(doc) reads the cross-reference table only — no page content
            # is decoded or rendered.  This is equivalent to checking the
            # ``/Count`` entry in the PDF page tree, which is always present.
            return len(doc)
    except Exception:
        # Return 0 rather than raising so the caller can fall through to
        # structured extraction, which will raise a richer PDFExtractionError.
        return 0


# ---------------------------------------------------------------------------
# Data classes for structured output
# ---------------------------------------------------------------------------

@dataclass
class ArticleSections:
    """
    Container for the structured output of layout-aware extraction.

    Attributes
    ----------
    full_text : str
        The complete cleaned text (all sections concatenated), usable as a
        drop-in replacement for the flat ``extract_text_from_pdf`` output.
    sections : dict[str, str]
        Mapping of canonical section name → section body text.  Only sections
        that were actually detected in the PDF appear here.
    section_order : list[str]
        The section names in the order they appear in the document.
    detected_figures : int
        Number of image/figure blocks that were filtered out during extraction.
    detected_tables : int
        Number of suspected table blocks that were filtered out.
    page_count : int
        Total number of pages in the PDF.
    """
    full_text: str = ""
    sections: dict[str, str] = field(default_factory=dict)
    section_order: list[str] = field(default_factory=list)
    detected_figures: int = 0
    detected_tables: int = 0
    page_count: int = 0
    # Confidence score in [0.0, 1.0] for the extracted text.
    # 1.0 → text came from a reliable text layer (PyMuPDF).
    # 0.0–1.0 → OCR fallback; product of page-coverage and per-page
    #           alphanumeric quality (see _perform_ocr_fallback).
    # 0.0 (default) → no text was extracted at all.
    ocr_confidence: float = 0.0
    # High-level document type inferred during extraction.
    # "text_layer"   — normal embedded-text PDF (PyMuPDF succeeded).
    # "scanned"      — no text layer; OCR fallback was used.
    # "figure_heavy" — text layer present but figure count exceeds page count
    #                  (slide decks, instrument manuals, image-dense reports).
    doc_type: str = "text_layer"
    # True when extraction did not cover the whole requested range — currently
    # set only by the OCR fallback when more pages were requested than the
    # per-call OCR cap allowed.  Callers (vault indexer) use it to warn and to
    # avoid caching an incomplete range as if it were complete.
    truncated: bool = False


# ---------------------------------------------------------------------------
# Canonical section names and detection patterns
# ---------------------------------------------------------------------------
# WHY these specific names?  They cover the IMRAD structure (Introduction,
# Methods, Results, And Discussion) used by most biomedical journals, plus
# common extras like Limitations and Clinical Implications.  The regex
# patterns match the most frequent header spellings found in PubMed-indexed
# PDFs, including British variants ("organisation" etc.) and compound headers
# like "Materials and Methods" or "Patients and Methods".

_CANONICAL_SECTIONS: tuple[str, ...] = (
    "abstract",
    "introduction",
    "background",
    "methods",
    "results",
    "discussion",
    "conclusion",
    "limitations",
    "references",
)

# Pre-compiled patterns reused across every text block in every PDF.
_RE_MATERIALS_METHODS    = re.compile(r"materials?\s+and\s+methods?")
_RE_PATIENTS_METHODS     = re.compile(r"patients?\s+and\s+methods?")
_RE_PARTICIPANTS_METHODS = re.compile(r"participants?\s+and\s+methods?")
_RE_LEADING_NUMBER       = re.compile(r"^\d+\.?\s*")
_RE_LATEX_NOTATION       = re.compile(r"\\(frac|sqrt|sum|int|alpha|beta|gamma|delta|theta)\b")
_RE_DOLLAR_MATH          = re.compile(r"\$+(?!\d)[^$]+\$+")

# Pre-built frozensets for O(1) character-class lookup in table/formula detection.
# Using frozenset instead of a str literal drops the per-char membership test from
# O(len(charset)) to O(1), and the single-pass design below halves iteration count.
_TABLE_CHARS: frozenset = frozenset("0123456789|─━┃┆+-–—.\t,;%<>≤≥±")
_MATH_CHARS:  frozenset = frozenset("∑∫∂√∞≈≠≤≥±∓∝∆Δ∇αβγδεζηθικλμνξπρστυφχψω")

_SECTION_HEADER_PATTERN = re.compile(
    r"(?i)^"
    r"\s*(?:\d+\.?\s*)?"      # optional leading number (e.g. "3. Methods")
    r"("
    r"abstract|summary|"
    r"introduction|background|"
    r"methods?|methodology|"
    r"materials?\s+and\s+methods?|"
    r"patients?\s+and\s+methods?|"
    r"participants?\s+and\s+methods?|"
    r"study\s+design|"
    r"results?|findings?|outcomes?|"
    r"discussion|conclusions?|"
    r"clinical\s+implications?|"
    r"limitations?|"
    r"references?|bibliography|literature\s+cited"
    r")\s*$"
)

# Pre-compiled patterns for _clean_text — avoids recompilation on every call.
# _clean_text is invoked once per section plus once for the final full text,
# so this saves N+1 regex compilations per PDF.
_RE_MULTI_NEWLINES = re.compile(r'\n{3,}')
_RE_MULTI_SPACES = re.compile(r'[ \t]{2,}')


def _normalise_section_name(raw: str) -> str:
    """
    Map a detected header to a canonical section name.

    WHY: Different journals use slightly different labels for the same section.
    Normalising ensures that downstream truncation/chunking logic can work
    with a stable set of names regardless of journal style.

    Examples
    --------
    >>> _normalise_section_name("Materials and Methods")
    'methods'
    >>> _normalise_section_name("3. Discussion")
    'discussion'
    """
    lowered = raw.strip().lower()
    # Strip leading numbering (e.g. "3. Methods" → "methods")
    lowered = _RE_LEADING_NUMBER.sub("", lowered).strip()

    # Compound headers → canonical
    if _RE_MATERIALS_METHODS.search(lowered):
        return "methods"
    if _RE_PATIENTS_METHODS.search(lowered):
        return "methods"
    if _RE_PARTICIPANTS_METHODS.search(lowered):
        return "methods"
    if "study design" in lowered or lowered == "methodology":
        return "methods"
    if lowered.startswith("conclusion"):
        return "conclusion"
    if lowered.startswith("finding") or lowered.startswith("outcome"):
        return "results"
    if lowered.startswith("clinical implication"):
        return "discussion"
    if lowered.startswith("limitation"):
        return "limitations"
    if lowered.startswith("bibliograph") or "literature cited" in lowered:
        return "references"
    if lowered == "summary":
        return "abstract"

    # Direct match against canonical names
    for canonical in _CANONICAL_SECTIONS:
        if lowered.startswith(canonical):
            return canonical

    return lowered


# ---------------------------------------------------------------------------
# Layout-aware block extraction
# ---------------------------------------------------------------------------
# WHY block-level extraction?  PyMuPDF's ``get_text("dict")`` returns every
# text span with its bounding box, font name, and font size.  This lets us:
#   1. Detect multi-column layouts by comparing x-coordinates of blocks.
#   2. Identify section headers by their larger font size relative to body
#      text (the dominant font size on the page).
#   3. Filter out image blocks, figure captions, and table-like artefacts
#      that would confuse an LLM if included verbatim.
#
# This approach is inspired by scipdf_parser's GROBID-based section detection
# but uses only PyMuPDF — no external Java service is required.

@dataclass
class _TextBlock:
    """Internal representation of a single text block from PyMuPDF."""
    text: str
    x0: float          # left edge
    y0: float          # top edge
    x1: float          # right edge
    y1: float          # bottom edge
    font_size: float   # dominant font size in this block
    is_bold: bool      # whether the dominant font is bold
    page_num: int      # 0-based page index
    block_type: str    # "text", "image", "table_artifact", "caption", "formula"
    image_data: str | None = None  # Base64-encoded image data
    page_width: float = 0.0
    page_height: float = 0.0


def _extract_page_blocks(
    page: fitz.Page,
    page_num: int,
    extract_images: bool = False,
) -> list[_TextBlock]:
    """
    Extract all text and image blocks from a single PDF page.

    WHY we process at the block level:
    - Each block has a bounding box, allowing column detection.
    - Each block's spans carry font metadata for header detection.
    - Image blocks (type == 1 in PyMuPDF) are flagged and counted.

    Parameters
    ----------
    page           : A PyMuPDF page object.
    page_num       : 0-based page index (for tracking).
    extract_images : When False (default), image blocks are recorded for
                     counting and deduplication but their pixel data is NOT
                     rendered or base64-encoded.  Pass True only when a
                     vision describer callback is available, to avoid
                     allocating tens of MB of base64 strings that will never
                     be used.

    Returns
    -------
    list[_TextBlock]
        Blocks sorted top-to-bottom, left-to-right.
    """
    blocks: list[_TextBlock] = []
    page_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_DEHYPHENATE)
    p_rect = page.rect
    p_width, p_height = p_rect.width, p_rect.height

    for block in page_dict.get("blocks", []):
        bbox = block.get("bbox", (0, 0, 0, 0))
        x0, y0, x1, y1 = bbox

        # --- Image blocks (type == 1 in PyMuPDF) ---
        # WHY flag these?  Sending raw image placeholders to an LLM produces
        # garbage output.  We count them for the UI but exclude them from text.
        if block.get("type") == 1:
            # GEOMETRIC FILTERING FOR LOGOS/ICONS:
            # 1. Size Filter: Ignore very small icons (ORCID, Twitter, tiny badges)
            b_width = x1 - x0
            b_height = y1 - y0
            if b_width < 60 or b_height < 60:
                continue

            # 2. Margin Filter: Ignore images strictly in header/footer (top/bottom 10%)
            if y1 < p_height * 0.1 or y0 > p_height * 0.9:
                continue

            img_data = None
            if extract_images:
                try:
                    # LIMIT RESOLUTION: 1.5x scale (approx 108 dpi) is plenty for
                    # LLM/Alt-text.  This prevents OOM on 300+ dpi high-res figures.
                    # Only rendered when a vision describer is actually configured —
                    # avoids allocating 10–50 MB of base64 strings per document
                    # when no vision model is available.
                    # colorspace=fitz.csRGB strips the alpha channel so the
                    # PNG sent to the vision model is 3-channel RGB, not RGBA.
                    # Ollama's CLIP encoder rejects 4-channel images with
                    # "image: unknown format" (same root cause as the OCR path).
                    pix = page.get_pixmap(clip=bbox, matrix=fitz.Matrix(1.5, 1.5), colorspace=fitz.csRGB)
                    img_data = base64.b64encode(pix.tobytes("png")).decode("utf-8")
                except Exception:
                    # Best-effort image render; an undecodable/unknown-format
                    # block just contributes no base64 and is skipped.
                    pass

            blocks.append(_TextBlock(
                text="", x0=x0, y0=y0, x1=x1, y1=y1,
                font_size=0, is_bold=False, page_num=page_num,
                block_type="image",
                image_data=img_data,
                page_width=p_width,
                page_height=p_height
            ))
            continue

        # --- Text blocks (type == 0) ---
        # WHY scalar accumulators instead of a font_sizes list?
        # Building a list per block just to compute a mean allocates a new
        # Python list object (+ N float objects) for every block on every page.
        # Tracking total_size + span_count avoids all that heap pressure with
        # no change in the computed value.
        lines_text: list[str] = []
        total_size  = 0.0
        span_count  = 0
        bold_count  = 0

        for line in block.get("lines", []):
            spans_text: list[str] = []
            for span in line.get("spans", []):
                span_text = span.get("text", "")
                spans_text.append(span_text)
                total_size += span.get("size", 0)
                span_count += 1
                # WHY check for "Bold" in font name?  PDF fonts encode weight
                # in the font name string (e.g. "TimesNewRoman-Bold").  This
                # is the most reliable cross-PDF way to detect bold text
                # without parsing the full font descriptor.
                font_name = span.get("font", "")
                if "bold" in font_name.lower() or "heavy" in font_name.lower():
                    bold_count += 1

            lines_text.append("".join(spans_text))

        text = "\n".join(lines_text).strip()
        if not text:
            continue

        avg_size = total_size / span_count if span_count else 0.0
        is_bold  = bold_count > span_count / 2 if span_count else False

        blocks.append(_TextBlock(
            text=text, x0=x0, y0=y0, x1=x1, y1=y1,
            font_size=avg_size, is_bold=is_bold, page_num=page_num,
            block_type="text",
            page_width=p_width,
            page_height=p_height
        ))

    return blocks


def _detect_body_font_size(all_blocks: list[_TextBlock]) -> float:
    """
    Determine the most common (dominant) font size across all text blocks.

    WHY: In a research article the vast majority of text is body text.
    Section headers use a larger font size.  By finding the mode of font
    sizes we establish a baseline: anything significantly larger is likely
    a header, and anything significantly smaller is likely a caption,
    footnote, or page number.  This heuristic avoids the need for an
    external layout model.
    """
    sizes: dict[float, int] = {}
    for b in all_blocks:
        if b.block_type == "text" and b.font_size > 0:
            # Round to 1 decimal to merge near-identical sizes (e.g. 9.96 ≈ 10.0)
            rounded = round(b.font_size, 1)
            sizes[rounded] = sizes.get(rounded, 0) + len(b.text)
    if not sizes:
        return 10.0  # sensible fallback
    # Return the size that accounts for the most characters
    return max(sizes, key=sizes.get)


def _classify_blocks(
    blocks: list[_TextBlock],
    body_font_size: float,
) -> list[_TextBlock]:
    """
    Classify each text block as body text, header, caption, table artifact,
    or formula placeholder.

    WHY each classification rule:

    * **Headers** — font size ≥ 1.15× body size, or bold text matching a
      known section-header pattern.  This mirrors how scipdf_parser uses
      GROBID's font-feature analysis for section detection.

    * **Captions** — short blocks with font size < 0.95× body size that
      start with "Fig", "Figure", "Table", or "Scheme".  Including these
      would add noise (e.g. "Figure 3. Kaplan-Meier curve") without the
      actual data the LLM needs.

    * **Table artifacts** — blocks whose text is mostly pipe characters,
      dashes, or tab-separated columns.  Complex tables extracted as flat
      text are unreadable and confuse the model.  This is inspired by
      PDF-Extract-Kit's table-detection module.

    * **Formulas** — blocks dominated by mathematical symbols or very short
      lines with unusual characters.  LLMs handle inline math notation
      poorly; filtering prevents hallucinated interpretations.

    Parameters
    ----------
    blocks         : Raw blocks from ``_extract_page_blocks``.
    body_font_size : Dominant body font size from ``_detect_body_font_size``.
    """
    header_threshold = body_font_size * 1.15
    caption_threshold = body_font_size * 0.95

    for block in blocks:
        if block.block_type != "text":
            continue

        text = block.text.strip()

        # --- Caption detection ---
        # WHY: Figure/table captions reference visual elements that are not
        # in the text stream.  Including "Figure 3: KM survival curve" adds
        # noise without the actual figure data.
        if (
            block.font_size < caption_threshold
            and len(text) < 300
            and re.match(r"(?i)^(fig(ure)?|table|scheme|plate)\s*\.?\s*\d", text)
        ):
            block.block_type = "caption"
            continue

        # --- Table artifact detection ---
        # WHY: Tables extracted as flat text often become sequences of numbers
        # separated by whitespace, or pipe-delimited grids.  These confuse
        # the LLM far more than they help.  We detect them by checking for
        # a high ratio of digit/separator characters vs. letter characters.
        if _looks_like_table(text):
            block.block_type = "table_artifact"
            continue

        # --- Formula detection ---
        # WHY: Mathematical notation rendered as text (e.g. "∑ᵢ₌₁ⁿ xᵢ²")
        # is typically garbled in PDF extraction and triggers LLM
        # hallucinations.  Short blocks dominated by math-like characters
        # are safer to exclude.
        if _looks_like_formula(text):
            block.block_type = "formula"
            continue

        # --- Header detection ---
        # WHY two checks?  Some PDFs use a larger font for headers (detected
        # by threshold), while others use bold at the same size (detected by
        # is_bold + regex match).  Combining both gives broad coverage.
        if block.font_size >= header_threshold and len(text) < 120:
            if _SECTION_HEADER_PATTERN.match(text):
                block.block_type = "header"
                continue

        if block.is_bold and len(text) < 120:
            if _SECTION_HEADER_PATTERN.match(text):
                block.block_type = "header"
                continue

    return blocks


def _looks_like_table(text: str) -> bool:
    """
    Heuristic: return True if *text* looks like a flat-extracted table.

    WHY this heuristic?  Tables in PDFs render as text blocks where each
    cell becomes a separate span.  The extracted text ends up being mostly
    numbers, pipes, and whitespace with very few prose words.  We check
    the ratio of digit+separator characters to total characters.

    Single-pass over the text using pre-built frozensets for O(1) per-char
    lookup, halving iteration count vs. the previous two-pass approach.
    """
    if len(text) < 40:
        return False
    table_chars = alpha_chars = 0
    for c in text:                      # single pass, O(1) membership test
        if c in _TABLE_CHARS:
            table_chars += 1
        elif c.isalpha():
            alpha_chars += 1
    if alpha_chars == 0:
        return len(text) > 20
    ratio = table_chars / (table_chars + alpha_chars)
    line_count = text.count("\n") + 1
    if ratio > 0.5 and line_count >= 3 and len(text) / line_count < 80:
        return True
    return ratio > 0.65


def _looks_like_formula(text: str) -> bool:
    """
    Heuristic: return True if *text* is likely a mathematical formula.

    WHY: Mathematical notation extracted from PDFs is almost never usable
    by an LLM for summarisation.  Characters like ∑, ∫, ∂, √ etc. indicate
    formula content.  We only flag short blocks (< 200 chars) to avoid
    filtering out methodology paragraphs that happen to mention one symbol.

    WHY check for ``$...$``?  Some PDFs embed LaTeX source strings directly
    in the text layer (e.g. from arXiv preprints compiled with pdflatex).
    These dollar-delimited strings confuse the LLM and add no value to the
    summary.

    Uses the pre-built _MATH_CHARS frozenset for O(1) per-char lookup.
    """
    if len(text) > 200:
        return False
    if sum(1 for c in text if c in _MATH_CHARS) >= 3:
        return True
    if _RE_LATEX_NOTATION.search(text):
        return True
    # WHY \$+ on both sides?  This matches both inline ``$...$`` and display
    # ``$$...$$`` math without two separate patterns.
    # WHY (?!\d)?  Prices like "$50" start with a dollar sign followed by a
    # digit.  Mathematical expressions start with a letter, backslash, or
    # opening brace.  The negative look-ahead avoids false positives on
    # monetary amounts that occasionally appear in clinical-trial papers.
    if _RE_DOLLAR_MATH.search(text):
        return True
    return False


# ---------------------------------------------------------------------------
# Multi-column handling
# ---------------------------------------------------------------------------

def _sort_blocks_reading_order(
    blocks: list[_TextBlock],
    page_width: float,
) -> list[_TextBlock]:
    """
    Sort blocks into natural reading order, handling multi-column layouts.

    WHY: Research articles frequently use two-column layouts.  PyMuPDF
    returns blocks in the order they appear in the PDF content stream,
    which may interleave columns.  By detecting whether blocks fall in the
    left or right half of the page and sorting accordingly, we reconstruct
    the intended reading order.

    The approach is simpler than PDF-Extract-Kit's full layout model but
    effective for the IMRAD papers this app targets.

    Parameters
    ----------
    blocks     : Blocks from a single page.
    page_width : Width of the page in points.
    """
    if not blocks:
        return blocks

    midpoint = page_width / 2
    # Tolerance band: blocks whose centre is within 15% of the midpoint
    # are treated as full-width (spanning both columns).
    tolerance = page_width * 0.15

    def _sort_key(b: _TextBlock) -> tuple[int, float, float]:
        centre_x = (b.x0 + b.x1) / 2
        # Full-width blocks (e.g. title, abstract) → column 0
        block_width = b.x1 - b.x0
        if block_width > page_width * 0.65:
            col = 0
        elif centre_x < midpoint - tolerance:
            col = 0  # left column
        elif centre_x > midpoint + tolerance:
            col = 1  # right column
        else:
            col = 0  # near midpoint → treat as left/full-width
        return (col, b.y0, b.x0)

    return sorted(blocks, key=_sort_key)


# ---------------------------------------------------------------------------
# Section assembly
# ---------------------------------------------------------------------------

def _assemble_sections(blocks: list[_TextBlock], describer_cb: Optional[Callable] = None) -> tuple[dict[str, str], list[str]]:
    """
    Group classified blocks into named sections.

    WHY: The final goal is to give the LLM a clearly sectioned article rather
    than a flat text dump.  This improves summary quality because the model
    can identify where methods end and results begin, leading to more
    accurate structured summaries.

    If describer_cb is provided, it is used to generate alt-text for images.

    Returns
    -------
    (sections_dict, section_order)
        A dict mapping section name → concatenated text, and the list of
        section names in document order.
    """
    sections: dict[str, list[str]] = {}
    order: list[str] = []
    current_section = "preamble"  # text before the first detected header
    sections[current_section] = []
    order.append(current_section)

    for block in blocks:
        if block.block_type == "header":
            canonical = _normalise_section_name(block.text)
            if canonical not in sections:
                sections[canonical] = []
                order.append(canonical)
            current_section = canonical
            # Include the header text itself so the LLM sees the section label
            sections[current_section].append(block.text.strip())
        elif block.block_type == "text":
            sections[current_section].append(block.text.strip())
        elif block.block_type == "image" and block.image_data and describer_cb:
            # Inject semantic description back into context
            try:
                description = describer_cb(block.image_data)
                if description:
                    sections[current_section].append(f"[IMAGE DESCRIPTION: {description}]")
            except Exception:
                # Best-effort image description; a describer/transport failure
                # just omits the description and continues extraction.
                pass
        elif block.block_type == "caption":
            # Captions are also useful semantic context
            sections[current_section].append(f"[CAPTION: {block.text}]")

    # Join paragraphs within each section
    result: dict[str, str] = {}
    for name in order:
        joined = "\n\n".join(sections[name])
        cleaned = _clean_text(joined)
        if cleaned:
            result[name] = cleaned

    # Remove empty preamble if it has no meaningful content
    if "preamble" in result and len(result["preamble"]) < 50:
        result.pop("preamble", None)
        if "preamble" in order:
            order.remove("preamble")

    final_order = [n for n in order if n in result]
    return result, final_order


# ---------------------------------------------------------------------------
# Public API — structured extraction
# ---------------------------------------------------------------------------

def _collect_all_blocks(
    doc,
    start_page: int,
    page_end: int,
    char_budget: int | None,
    extract_images: bool
) -> tuple[list[_TextBlock], float]:
    """Collects blocks from the specified page range, stopping early if char_budget is met.
    Returns (all_blocks, first_page_width).
    """
    max_blocks = _max_blocks_for_range(page_end - start_page)
    all_blocks = []
    total_chars = 0
    first_page_width = 612.0

    for page_idx in range(start_page, page_end):
        page = doc[page_idx]
        page_width = page.rect.width
        if page_idx == start_page:
            first_page_width = page_width

        raw_blocks = _extract_page_blocks(page, page_idx, extract_images=extract_images)
        sorted_blocks = _sort_blocks_reading_order(raw_blocks, page_width)

        all_blocks.extend(sorted_blocks)

        if len(all_blocks) > max_blocks:
            raise PDFExtractionError("PDF structure is too complex (too many blocks).")

        for b in sorted_blocks:
            if b.block_type == "text":
                total_chars += len(b.text)

        if char_budget is not None and total_chars >= char_budget:
            break

    return all_blocks, first_page_width


def _deduplicate_and_count_artifacts(all_blocks: list[_TextBlock]) -> tuple[list[_TextBlock], int, int]:
    """Deduplicates recurring images (e.g. logos) and counts figures and tables.
    Returns (final_blocks, figure_count, table_count).
    """
    image_positions: dict[tuple[float, float, float, float], int] = {}
    final_blocks = []
    figure_count = 0
    table_count = 0

    for b in all_blocks:
        if b.block_type == "image":
            pos = (
                round(b.x0, 1),
                round(b.y0, 1),
                round(b.x1 - b.x0, 1),
                round(b.y1 - b.y0, 1),
            )
            image_positions[pos] = image_positions.get(pos, 0) + 1
            if image_positions[pos] > 2:
                continue
            figure_count += 1
        elif b.block_type == "table_artifact":
            table_count += 1

        final_blocks.append(b)

    return final_blocks, figure_count, table_count


# ---------------------------------------------------------------------------
# GLM-OCR fallback constants and helper
# ---------------------------------------------------------------------------

# Maximum pages handled by a single ``extract_structured_from_pdf`` call.
# Callers that need to process larger documents (e.g. vault textbook indexing)
# must split the work into multiple calls with explicit start_page/end_page.
EXTRACT_MAX_PAGES_PER_CALL = 1000

# Block-count safety cap for a single extraction call. A pathological PDF can
# fragment text into per-glyph/per-word blocks and exhaust memory, so the
# accumulated block count is bounded — but the bound must scale with the page
# range, or a legitimately large book trips a flat cap (e.g. an 845-page
# reference at ~80 blocks/page ≈ 66k blocks would fail a flat 50k cap and be
# skipped entirely). The floor preserves the original guard for small/dense
# documents — a 30-page doc with 50k blocks is genuinely pathological — while
# the per-page allowance adds headroom up to the 1000-page single-call ceiling.
_MAX_BLOCKS_FLOOR = 50_000
_MAX_BLOCKS_PER_PAGE = 250


def _max_blocks_for_range(pages_in_range: int) -> int:
    """Block cap for a *pages_in_range*-page extraction call.

    ``max(floor, pages * per_page)`` — never drops below the historical 50k
    guard, and scales with the page range so large legitimate documents are
    not misclassified as "too complex".
    """
    return max(_MAX_BLOCKS_FLOOR, max(1, pages_in_range) * _MAX_BLOCKS_PER_PAGE)

# Render scale for GLM-OCR page images.  2× gives roughly 150 DPI on a
# standard A4 page (595 pt wide → 1190 px), which is sufficient for GLM-OCR
# to produce accurate output without consuming excessive RAM per page.
_OCR_RENDER_SCALE = 2.0

# Safety cap on the number of pages OCR'd in a single call.  Prevents a
# runaway loop on a very long scanned book uploaded by the user.  100 pages
# at ~1 MB PNG each is already ~100 MB of RAM in the worst case.
_OCR_MAX_PAGES = 100

# Minimum character count below which assembled text is considered "blank"
# and the OCR fallback should be attempted.  50 characters covers PDFs that
# embed a small invisible text layer (e.g. a single whitespace string from a
# scanner's PDF writer) without being a real text-layer document.
_OCR_MIN_CHARS = 50


def _try_markitdown_extraction(
    file_path: str,
    char_budget: int | None,
    page_count: int,
) -> "ArticleSections | None":
    """Attempt text extraction via MarkItDown as a middle tier before GLM-OCR.

    WHY a separate tier between PyMuPDF and OCR?
    PyMuPDF's block-level layout engine uses a spatial / geometric approach to
    assemble text from character-level glyph runs.  pdfminer.six (used by
    MarkItDown internally) uses a line-direction / text-flow approach that often
    recovers text from PDFs with non-standard encoding, unusual font embedding,
    or left-to-right ligature compression that confuses PyMuPDF's block parser.

    Choosing MarkItDown over OCR here is a latency trade-off: pdfminer.six is
    purely CPU-bound and completes in milliseconds, whereas GLM-OCR renders
    every page to a PNG and runs a vision model (~1 s/page).  If MarkItDown
    yields usable text, we skip GLM-OCR entirely.

    WHY no llm_client / vision here?
    pdf_extractor.py is a thin extraction library that must not import
    summarizer.py (circular dependency).  VisionManager lives in summarizer.py.
    Image alt-text in single-paper summaries is handled upstream in app.py
    (via the describer_cb parameter to extract_structured_from_pdf).

    Args:
        file_path:   Absolute path to the PDF.
        char_budget: Optional character cap applied after extraction; avoids
                     indexing absurdly large documents in full.
        page_count:  Page count already known by the calling context — passed
                     through to the returned ArticleSections without reopening
                     the file.

    Returns:
        ArticleSections on success (text ≥ _OCR_MIN_CHARS chars).
        None if markitdown is unavailable, the conversion fails, or the
        extracted text is effectively blank.
    """
    # Fast-path: skip entirely when the package is absent.
    if not _MARKITDOWN_AVAILABLE or _MarkItDownExtractor is None:
        return None

    try:
        # No llm_client → text-only, no API keys, no cloud calls.
        # Azure Document Intelligence is intentionally not configured.
        mdit = _MarkItDownExtractor()
        result = mdit.convert(file_path)
        text = (result.text_content or "").strip()

        # Apply character budget to prevent unbounded RAM usage on very large
        # PDFs.  Truncation preserves the leading content which is the highest-
        # value portion for scientific papers (title, abstract, intro).
        if char_budget is not None and len(text) > char_budget:
            text = text[:char_budget]

        if len(text) < _OCR_MIN_CHARS:
            # MarkItDown produced effectively no text — not an improvement over
            # PyMuPDF; signal the caller to continue to GLM-OCR.
            logger.debug(
                "MarkItDown extracted only %d chars from %s (min %d) — "
                "passing through to OCR.",
                len(text), file_path, _OCR_MIN_CHARS,
            )
            return None

        # Wrap as a single "body" section.  Downstream consumers (summariser,
        # citation engine) iterate section_order; a single "body" key is the
        # simplest valid ArticleSections for text that has no IMRAD structure.
        return ArticleSections(
            full_text=text,
            sections={"body": text},
            section_order=["body"],
            page_count=page_count,
            # Confidence is lower than a verified text-layer read (1.0) since
            # pdfminer may reorder columns or merge stray characters, but
            # substantially higher than GLM-OCR on scanned images.
            ocr_confidence=0.7,
            doc_type="text_layer",
        )

    except Exception as _exc:
        logger.debug(
            "MarkItDown extraction failed for %s: %s — falling through to OCR.",
            file_path, _exc,
        )
        return None


def _perform_ocr_fallback(
    file_path: str,
    ocr_cb: Callable[[str], str],
    start_page: int,
    page_end: int,
    char_budget: int | None,
    ocr_max_pages: int | None = None,
    page_done_cb: Optional[Callable[[int], None]] = None,
) -> ArticleSections:
    """Render each page to a PNG and call ``ocr_cb`` to extract its text.

    This function is the GLM-OCR fallback path for scanned / image-only PDFs —
    documents that contain no text layer, so PyMuPDF's block extractor returns
    nothing (or only whitespace).

    WHY render full pages instead of individual image blocks?
    Scanned PDFs contain no text blocks at all — every "block" is a single
    full-page raster image.  Rendering the whole page (rather than clipping to
    individual image rectangles) ensures that headers, footers, page numbers,
    and multi-column layouts are all captured in natural reading order by
    GLM-OCR, which is designed to process complete document pages.

    Parameters
    ----------
    file_path : str
        Absolute path to the PDF file; already validated by the primary
        extraction call that detected the empty-text condition.
    ocr_cb : callable
        Function with signature ``(base64_png: str) -> str``.  Expected to be
        ``GLMOCRManager.extract_page_text`` from ``summarizer.py``, but any
        callable with that signature works (simplifies unit testing).
    start_page : int
        0-based index of the first page to OCR (inclusive).  Must match the
        ``start_page`` used by the primary extraction call so the page range
        is consistent.
    page_end : int
        0-based index one past the last page to OCR (exclusive).  Already
        clamped to the document length by the primary extraction call.
    char_budget : int | None
        Optional character budget.  OCR stops early once the accumulated text
        meets or exceeds this limit — same semantics as primary extraction.
    ocr_max_pages : int | None
        Per-call cap on pages OCR'd (default ``_OCR_MAX_PAGES`` = 100 when None).
        The vault range loader passes the full range size so a scanned range is
        OCR'd in full instead of stopping at 100 pages; the interactive upload
        path leaves it None.  When the cap (or ``char_budget``) stops OCR before
        the whole requested range is covered, the returned
        ``ArticleSections.truncated`` is set True.
    page_done_cb : callable | None
        Optional ``(abs_page_index) -> None`` hook invoked after each page is
        rendered (before its OCR call), used by the vault loader to refresh the
        operation-lock heartbeat on a long scan.  Exceptions from it are swallowed.

    Returns
    -------
    ArticleSections
        Sections keyed by ``"Page N"`` headings (1-based page number).
        ``full_text`` is all non-empty pages joined with double newlines.
        ``detected_figures`` and ``detected_tables`` are always 0 because
        GLM-OCR returns plain text without structural classification.
        ``page_count`` reflects the actual document length, not just the
        OCR'd range, so callers have the true document size.
    """
    # Clamp the number of pages OCR'd in this call.  ``ocr_max_pages`` lets a
    # caller that bounds memory/time another way (the vault range loader, which
    # heartbeats per page and processes one 1000-page range at a time) OCR the
    # WHOLE range instead of silently stopping at the default _OCR_MAX_PAGES — the
    # bug where a scanned 1000-page range only ever indexed its first 100 pages.
    # ``None`` keeps the historical 100-page cap for the interactive upload path.
    requested_pages = page_end - start_page
    cap = ocr_max_pages if (ocr_max_pages is not None and ocr_max_pages > 0) else _OCR_MAX_PAGES
    pages_to_ocr = min(requested_pages, cap)
    # True if we stop before covering the whole requested range (page cap or, for
    # callers that pass one, the char budget) — surfaced on ArticleSections.truncated.
    ocr_truncated = pages_to_ocr < requested_pages

    # Build the transform matrix once; reused for every page render.
    # fitz.Matrix(s, s) applies uniform scaling by factor s.
    matrix = fitz.Matrix(_OCR_RENDER_SCALE, _OCR_RENDER_SCALE)

    # Accumulators for the structured result.
    sections: dict[str, str] = {}
    section_order: list[str] = []
    accumulated_chars = 0
    page_count = 0  # will be set from the document inside the with-block

    # Confidence scoring accumulators.
    # pages_attempted: pages where ocr_cb was invoked (denominator for coverage).
    # pages_with_text: pages that returned a non-empty string.
    # quality_sum: sum of per-page alphanumeric+whitespace character ratios.
    pages_attempted: int = 0
    pages_with_text: int = 0
    quality_sum: float = 0.0

    # Open the PDF with PyMuPDF to iterate page images for GLM-OCR processing.
    with fitz.open(file_path) as doc:
        # Record the true document page count for the returned ArticleSections,
        # so downstream callers (e.g. the RAG chunker) see the real document size.
        page_count = len(doc)

        for rel_idx in range(pages_to_ocr):
            abs_page = start_page + rel_idx

            # Guard: never read past the last page (defensive; page_end was
            # already clamped, but be explicit to avoid index errors).
            if abs_page >= page_count:
                break

            # Render the entire page at OCR_RENDER_SCALE.
            # clip=None means the full page rectangle, not a sub-region.
            # WHY colorspace=fitz.csRGB?  PyMuPDF defaults to the document's
            # native colorspace, which may include an alpha channel (RGBA).
            # Ollama's CLIP vision encoder expects 3-channel RGB tensors; passing
            # a 4-channel RGBA PNG causes a "failed to process inputs: image:
            # unknown format" 500 error from the inference backend.  Forcing RGB
            # at render time strips the alpha in C++ before the pixmap is
            # allocated in Python — correct and zero overhead.
            pix = doc[abs_page].get_pixmap(matrix=matrix, clip=None, colorspace=fitz.csRGB)

            # Encode the pixmap to PNG bytes, then to a base64 string.
            # The Ollama chat API expects base64-encoded image strings in the
            # 'images' list field.
            png_bytes = pix.tobytes("png")

            # Free the pixel buffer immediately — a 2× A4 page is ~4 MB of RAM.
            # Holding onto it across the Ollama HTTP call would spike memory
            # for multi-page scanned PDFs.
            del pix

            # Align PNG dimensions to multiples of 14 before sending to GLM-OCR.
            # WHY?  GLM-OCR's ViT image encoder divides the image into an exact
            # integer grid of 14×14 px patches.  If either dimension is not a
            # multiple of 14, llama.cpp raises:
            #   GGML_ASSERT(a->ne[2] * 4 == b->ne[0]) failed
            # because the patch-count tensor's shape is inconsistent with the
            # attention-head dimension.  At OCR_RENDER_SCALE=2.0, virtually no
            # standard page size produces 14-aligned dimensions — A4 yields
            # 1190×1684 (off by 0 and 4), US Letter yields 1224×1584 (off by 6
            # and 2) — so without this step the OCR fallback silently fails on
            # almost every scanned PDF.  Ceiling-rounding adds at most 13 px
            # per axis (< 1% of page height for A4) and has no visible effect
            # on OCR accuracy.  min(28) ensures pathologically small pages get
            # at least two patches in each direction.
            if _PIL_AVAILABLE:
                try:
                    # Decode the PNG to read its pixel dimensions so the image
                    # can be padded to a multiple of 14 px before sending to
                    # GLM-OCR (the model's required patch alignment).
                    with _PILImage.open(io.BytesIO(png_bytes)) as _pil:
                        _w, _h = _pil.size
                        # Ceiling-round to the nearest multiple of 14.
                        # math.ceil is used instead of the double-negative idiom
                        # (-(-w // 14) * 14) for readability and verifiability.
                        _sw = max(28, math.ceil(_w / 14) * 14)
                        _sh = max(28, math.ceil(_h / 14) * 14)
                        if (_sw, _sh) != (_w, _h):
                            # Resize with LANCZOS to minimise aliasing on text.
                            _pil_resized = _pil.resize((_sw, _sh), _PILImage.LANCZOS)
                            _buf = io.BytesIO()
                            _pil_resized.save(_buf, format="PNG")
                            _pil_resized.close()       # free resized image buffer
                            png_bytes = _buf.getvalue()  # read before closing
                            _buf.close()               # free BytesIO memory
                except Exception as _e:
                    # Resize failed — pass original bytes through.
                    # ocr_cb will surface the model error via WARNING log.
                    logger.debug("OCR page dimension alignment failed: %s", _e)

            b64 = base64.b64encode(png_bytes).decode("utf-8")

            # Count every page where ocr_cb is invoked so coverage scoring
            # reflects all attempted pages, not just successful ones.
            pages_attempted += 1

            # Per-page progress hook — lets a long (now up-to-1000-page) OCR range
            # refresh the indexer's operation-lock heartbeat so the TTL cannot
            # expire mid-file.  Best-effort; never lets a callback abort OCR.
            if page_done_cb is not None:
                try:
                    page_done_cb(abs_page)
                except Exception:
                    pass

            # Call the OCR callback, guarding against any exception the callback
            # may raise (e.g. a transient Ollama connection failure, or a test
            # stub that raises deliberately).  A failed page degrades to an
            # empty string and is skipped — the same outcome as a page where
            # the model returned no text — rather than aborting the entire OCR
            # pass and leaving the caller with no text at all.
            try:
                page_text = ocr_cb(b64)
            except Exception:
                # Treat callback exceptions the same as an empty OCR result:
                # skip this page without surfacing the error to the caller.
                page_text = ""
            if not page_text:
                continue

            # Per-page quality: ratio of alphanumeric + whitespace characters.
            # OCR noise (box-drawing chars, stray "??") reduces this ratio;
            # clean prose pushes it toward 1.0.  Whitespace is included because
            # spaces/newlines are valid text that isalnum() would penalise.
            _readable = sum(1 for c in page_text if c.isalnum() or c.isspace())
            quality_sum += _readable / max(len(page_text), 1)
            pages_with_text += 1

            # Use 1-based page number as the section key so the UI and RAG
            # engine can display human-readable "Page 1", "Page 2" headings.
            section_key = f"Page {abs_page + 1}"
            sections[section_key] = page_text
            section_order.append(section_key)
            accumulated_chars += len(page_text)

            # Honour the character budget: stop once we have enough text.
            # This prevents unbounded Ollama calls on a very long scanned book.
            if char_budget is not None and accumulated_chars >= char_budget:
                ocr_truncated = True
                break

    # Join all successfully OCR'd pages with double newlines for readability.
    full_text = "\n\n".join(sections[k] for k in section_order)

    # Compute OCR confidence as coverage × average quality.
    # coverage  = fraction of attempted pages that returned non-empty text.
    # avg_quality = mean per-page alphanumeric+whitespace ratio.
    # A score near 1.0 means all pages were read cleanly; near 0.0 means
    # most attempts failed or returned garbled output.
    if pages_attempted > 0 and pages_with_text > 0:
        coverage = pages_with_text / pages_attempted
        avg_quality = quality_sum / pages_with_text
        ocr_confidence = round(coverage * avg_quality, 3)
    else:
        ocr_confidence = 0.0

    return ArticleSections(
        full_text=full_text,
        sections=sections,
        section_order=section_order,
        # GLM-OCR returns plain text; figure/table counts are not applicable.
        detected_figures=0,
        detected_tables=0,
        page_count=page_count,
        ocr_confidence=ocr_confidence,
        # All pages were rendered and sent to GLM-OCR; no embedded text layer.
        doc_type="scanned",
        truncated=ocr_truncated,
    )


def extract_structured_from_pdf(
    file_path: str,
    char_budget: int | None = None,
    describer_cb: Optional[Callable] = None,
    start_page: int = 0,
    end_page: int | None = None,
    ocr_cb: Optional[Callable] = None,
    ocr_max_pages: int | None = None,
    page_done_cb: Optional[Callable] = None,
) -> ArticleSections:
    """
    Extract text from a PDF with layout-aware structural parsing and resource safety.

    Parameters
    ----------
    file_path : str
        Path to the PDF file.
    char_budget : int | None
        Optional raw-character limit.  Extraction stops early once accumulated
        text meets or exceeds this threshold.
    describer_cb : callable | None
        Optional vision-describer callback for image alt-text.
    start_page : int
        0-based index of the first page to extract (inclusive).  Defaults to 0
        (beginning of document).  Used by the large-PDF range loops in
        ``rag/vault.py::_load_pdf_range_documents`` (vault indexing — one
        document per range) and ``services/pdf_service.py::_extract_all_pages``
        (single-paper uploads — ranges concatenated) to process textbook-sized
        documents without one massive extraction call.
    end_page : int | None
        0-based index one past the last page to extract (exclusive), i.e. the
        loop runs ``range(start_page, end_page)``.  ``None`` means process to
        the end of the document.  Must satisfy ``end_page > start_page`` if
        not None.
    ocr_cb : callable | None
        Optional GLM-OCR callback for scanned / image-only PDFs.  When
        provided and the primary PyMuPDF extraction yields no usable text
        (fewer than ``_OCR_MIN_CHARS`` characters), each page is rendered to
        a PNG and passed to this callback for text extraction.  Expected
        signature: ``(base64_png: str) -> str``.  Typically
        ``GLMOCRManager.extract_page_text`` from ``summarizer.py``.

    ocr_max_pages : int | None
        Overrides the per-call OCR page cap (default ``_OCR_MAX_PAGES`` = 100).
        The vault indexer passes the full range size so a scanned range is OCR'd
        in full instead of silently stopping at 100 pages; the interactive upload
        path leaves it ``None`` (keeps the 100-page cap, bounded by its own
        extraction timeout).
    page_done_cb : callable | None
        Optional ``(abs_page_index) -> None`` hook called after each OCR'd page
        (heartbeat / progress).  Exceptions from it are swallowed.

    Safety limits (applied to the extracted range, not the whole document):
    - Maximum 1000 pages per extraction call.
    - Block-count cap scales with the page range (see _max_blocks_for_range):
      max(50,000, pages * 250), so large legitimate documents are not skipped.
    - OCR fallback capped at ocr_max_pages (default _OCR_MAX_PAGES) pages.
    """
    MAX_PAGES = EXTRACT_MAX_PAGES_PER_CALL

    try:
        # Open the PDF for layout-aware block extraction; fitz raises on corrupt
        # or unsupported files so the outer try/except can surface a clean error.
        with fitz.open(file_path) as doc:
            if getattr(doc, "needs_pass", False):
                raise PDFExtractionError(
                    "This PDF is password-protected and cannot be processed. "
                    "Please unlock it and try again."
                )

            page_count = len(doc)

            if start_page < 0:
                raise PDFExtractionError(f"start_page must be ≥ 0 (got {start_page}).")
            if start_page >= page_count:
                raise PDFExtractionError(
                    f"start_page ({start_page}) is beyond the last page "
                    f"of the document ({page_count - 1})."
                )

            page_end = min(end_page, page_count) if end_page is not None else page_count

            if page_end <= start_page:
                raise PDFExtractionError(
                    f"end_page ({end_page}) must be greater than start_page "
                    f"({start_page}) after clamping to document length."
                )

            pages_to_extract = page_end - start_page

            if pages_to_extract > MAX_PAGES:
                raise PDFExtractionError(
                    f"Page range too large ({pages_to_extract} pages). "
                    f"Maximum per extraction call is {MAX_PAGES}."
                )

            _extract_imgs = describer_cb is not None
            all_blocks, _first_page_width = _collect_all_blocks(
                doc, start_page, page_end, char_budget, _extract_imgs
            )

    except PDFExtractionError:
        raise
    except Exception as exc:
        raise PDFExtractionError(
            "The PDF could not be read. It may be corrupted or use an "
            "unsupported format."
        ) from exc

    if not all_blocks:
        # Primary extraction found zero blocks — PyMuPDF's geometric layout
        # engine found nothing.  Before falling to the expensive GLM-OCR path,
        # try MarkItDown (pdfminer.six) which uses a different text-stream
        # algorithm that can recover text where PyMuPDF's block parser cannot.
        _mid_result = _try_markitdown_extraction(file_path, char_budget, page_count)
        if _mid_result is not None:
            return _mid_result
        # MarkItDown also found nothing; the document likely has no text layer
        # at all (scanned images only).  Try GLM-OCR if available.
        if ocr_cb is not None and page_count > 0:
            return _perform_ocr_fallback(
                file_path, ocr_cb, start_page, page_end, char_budget,
                ocr_max_pages=ocr_max_pages, page_done_cb=page_done_cb,
            )
        return ArticleSections(page_count=page_count)

    body_font_size = _detect_body_font_size(all_blocks)
    _classify_blocks(all_blocks, body_font_size)

    final_blocks, figure_count, table_count = _deduplicate_and_count_artifacts(all_blocks)
    del all_blocks

    if not final_blocks:
        # Blocks were found by PyMuPDF but all were filtered out during
        # deduplication / artifact detection (e.g. an image-only PDF whose only
        # "blocks" are decorative images rejected by the geometry filter).
        # Before escalating to GLM-OCR, try MarkItDown — it may recover text
        # from a content stream that PyMuPDF misclassified as non-body blocks.
        _mid_result = _try_markitdown_extraction(file_path, char_budget, page_count)
        if _mid_result is not None:
            return _mid_result
        if ocr_cb is not None and page_count > 0:
            return _perform_ocr_fallback(
                file_path, ocr_cb, start_page, page_end, char_budget,
                ocr_max_pages=ocr_max_pages, page_done_cb=page_done_cb,
            )
        return ArticleSections(page_count=page_count)

    sections, section_order = _assemble_sections(final_blocks, describer_cb=describer_cb)

    full_text = "\n\n".join(sections[name] for name in section_order if name in sections)
    full_text = _clean_text(full_text)

    # Final guard: some PDFs produce text blocks that survive classification
    # but contain only whitespace or control characters after cleaning.
    # Try MarkItDown first (fast, CPU-only), then GLM-OCR if still blank.
    if len(full_text.strip()) < _OCR_MIN_CHARS:
        _mid_result = _try_markitdown_extraction(file_path, char_budget, page_count)
        if _mid_result is not None:
            return _mid_result
        if ocr_cb is not None:
            return _perform_ocr_fallback(
                file_path, ocr_cb, start_page, page_end, char_budget,
                ocr_max_pages=ocr_max_pages, page_done_cb=page_done_cb,
            )

    # Classify by figure density: more figure blocks than pages typically
    # indicates slide decks, instrument manuals, or image-heavy reports where
    # text layer coverage is thin and vision processing is more useful.
    _doc_type = (
        "figure_heavy"
        if page_count > 0 and figure_count > page_count
        else "text_layer"
    )

    return ArticleSections(
        full_text=full_text,
        sections=sections,
        section_order=section_order,
        detected_figures=figure_count,
        detected_tables=table_count,
        page_count=page_count,
        # Text came from the embedded text layer — highest possible confidence.
        ocr_confidence=1.0,
        doc_type=_doc_type,
    )





# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------

def _clean_text(text: str) -> str:
    """
    Remove excessive whitespace and artefacts from extracted text.

    WHY: PDF extraction commonly produces triple-newlines between blocks,
    double-spaces from column padding, and leading/trailing whitespace.
    Cleaning these prevents the LLM from wasting context-window tokens on
    formatting noise.

    Uses module-level pre-compiled patterns (_RE_MULTI_NEWLINES,
    _RE_MULTI_SPACES) to avoid re-compiling the same regex on every call.
    """
    text = _RE_MULTI_NEWLINES.sub('\n\n', text)
    text = _RE_MULTI_SPACES.sub(' ', text)
    return text.strip()





# Canonical display labels used when reassembling truncated section text.
# WHY include labels?  After truncation the LLM receives a subset of sections
# with no structural markers.  Providing ALL-CAPS headers (consistent with the
# expected output format) lets the model identify where Methods ends and Results
# begins, which measurably improves section-level accuracy in summaries.
_SECTION_DISPLAY_NAMES: dict[str, str] = {
    "abstract":     "ABSTRACT",
    "introduction": "INTRODUCTION",
    "background":   "BACKGROUND",
    "methods":      "METHODS",
    "results":      "RESULTS",
    "discussion":   "DISCUSSION",
    "conclusion":   "CONCLUSION",
    "limitations":  "LIMITATIONS",
    "preamble":     "PREAMBLE",
}


def truncate_sections_for_context(
    article: ArticleSections,
    max_chars: int = 120_000,
) -> tuple[str, bool]:
    """
    Truncate an ``ArticleSections`` object for the model context window.

    WHY a separate function for structured articles?  When we already have
    per-section text, we can make smarter decisions about what to drop:

    1. Always keep: abstract, methods, results, discussion, conclusion.
    2. Drop first: references, preamble, limitations (these are less
       critical for summarisation accuracy).
    3. If still too long, trim the remaining text from the end.

    Each included section is prefixed with its ALL-CAPS label so that the
    LLM can identify section boundaries in the truncated text.

    This is inspired by gemma3_pdf_summarizer's context-window optimisation.
    """
    if len(article.full_text) <= max_chars:
        return article.full_text, False

    # Priority order: sections we most want to keep for summarisation
    priority = [
        "abstract",
        "methods",
        "results",
        "discussion",
        "conclusion",
        "introduction",
        "background",
        "limitations",
        "preamble",
    ]

    def _labelled(name: str, text: str) -> str:
        label = _SECTION_DISPLAY_NAMES.get(name, name.upper())
        return f"{label}\n\n{text}"

    # Build text from highest-priority sections first
    included: list[str] = []
    total = 0
    for section_name in priority:
        if section_name in article.sections:
            section_text = _labelled(section_name, article.sections[section_name])
            if total + len(section_text) <= max_chars:
                included.append(section_text)
                total += len(section_text)
            else:
                # Include as much of this section as fits
                remaining = max_chars - total
                if remaining > 200:  # only include if there's meaningful space
                    included.append(section_text[:remaining])
                break

    # Also include any detected sections not in priority list
    for section_name in article.section_order:
        if section_name in priority or section_name not in article.sections:
            continue
        if section_name == "references":
            continue  # always skip references for summarisation
        section_text = _labelled(section_name, article.sections[section_name])
        if total + len(section_text) <= max_chars:
            included.append(section_text)
            total += len(section_text)
        else:
            break

    if included:
        result = "\n\n".join(included)
        return result[:max_chars], True

    # Fallback to full_text prefix
    return article.full_text[:max_chars], True
