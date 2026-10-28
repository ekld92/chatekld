"""What an orphan PDF physically IS, so the UI can offer the right remedy.

Batch C (2026-08-26). The accession ladder — Crossref / Europe PMC / arXiv /
HAL / theses.fr — is built for journal articles. The orphan residual mostly
is not one. Measured over the maintainer's 371 unmapped PDFs against a random
sample of 600 mapped ones (corrected 2026-08-26: the first version of this
table was computed over the 532-file residual of a bridge built WITHOUT
``pdf_text_provider`` — the very mistake the note below warns about — and so
understated every figure):

===========================  ========  ========
                              orphans   mapped
===========================  ========  ========
median pages                       15         9
over 30 pages                     36%        6%
90th-percentile pages             417        20
===========================  ========  ========

and by producer: publisher toolchains, Office exports, browser prints, and a
group with no producer at all averaging 499 pages. Books, guidelines, theses,
printed web pages. Offering all of those a DOI search is why the residual
feels intractable.

(A note on counts, because two different figures are in circulation: a bridge
built WITHOUT ``pdf_text_provider`` leaves 531 unmapped, because step 4.5 —
the printed-DOI rung — has no text to read. The app always injects the
provider, so its real residual is **371**. Any measurement script must inject
it too or it will overstate the problem by 43%.)

This module is the classifier. It is PURE — no I/O, no project imports beyond
the vendored tree's own rule — and it answers three questions:

* :func:`classify` — which broad class the file belongs to, from its producer
  string, page count and (for the weak macOS marker only) a recovered URL;
* :func:`may_be_browser_print` — whether recovering that URL is worth an
  extraction for this file at all;
* :func:`extract_source_url` — the URL a browser print puts in its header or
  footer, which for that class is the ONLY identifier that will ever exist.

None of it decides anything: the class routes a row to a verb, and every verb
downstream is the same confirm-gated one it always was.

**An EMC class was designed and dropped, then the measurement behind that was
found to be WRONG (re-measured 2026-08-26).** Every EM-Consulte chapter prints
its article code (``■ 17-059-K-13``) on page 1, and the original note recorded
that the app could not see it — "the extractor drops that line". It does not.
The code is present in the text this app actually serves, on BOTH paths, well
inside the 4 000-character window ``extract_printed_identifier`` already
scans::

    ferreri_2013.pdf            37-296-B-10   offset 1302
    guelfi_2019.pdf             37-395-A-10   offset 1409
    bourgeois_vionnet_2021.pdf  17-059-K-13   offset 1583

(measured through ``obsidian_manager._read_pdf_text(..., fresh_max_pages=1,
use_markitdown=False)`` — the chokepoint ``tagging/text_source.py`` uses — with
the cache warm and again with it forced cold; identical offsets both times.)
Only the narrower claim held: the code is not the FIRST line, because
EM-Consulte's download watermark precedes it.

The class is still not built, but that is now a scoping decision rather than an
impossibility, and it is recorded as one: an ``emc`` class would also be the
first thing able to supply ``Query.container`` automatically, which is the
caller Batch D's container-scoped Crossref search does not yet have (measured
14/20 right with the constraint against 10/20 and 4 confident-wrong without).
See the root ``CLAUDE.md`` Known Issues entry.
"""

from __future__ import annotations

import re

#: The classes, in the order the UI presents them. ``article`` is the residual
#: "looks like an ordinary paper" case, deliberately last: it is what the
#: existing ladder already handles well.
PROVENANCE_CLASSES = (
    "browser_print",   # printed web page — no DOI will ever exist
    "book",            # long, and with no publisher toolchain fingerprint
    "office_export",   # Word/Pages/PowerPoint — guidelines, reports, theses
    "scan",            # scanner output
    "publisher",       # a real publisher toolchain
    "article",         # unclassified; the ladder's home ground
)

#: Producer/creator substrings that identify a BROWSER specifically. Matching
#: one of these is decisive: a printed web page has no DOI at any length, so
#: the page count never overrides it.
_BROWSER_MARKERS = ("skia", "chrome", "chromium", "safari", "wkhtmltopdf",
                    "weasyprint", "puppeteer", "headlesschrome")

#: ``Quartz PDFContext`` is the generic macOS graphics-layer producer — every
#: Cocoa app's Print → Save as PDF writes it, Preview included — so it is NOT
#: on its own evidence of a browser. Measured: 36 of the 37 files in this class
#: carried it, 13 of them book-length (up to 845 pages) with no recoverable
#: URL, each badged "printed web page … add it by URL in Zotero" — a wrong
#: instruction, not merely a wrong label. It therefore needs corroboration: a
#: browser name in ``/Creator`` (which the real Safari prints carry) or a
#: recovered page URL (which is the class's whole remedy anyway).
_WEAK_BROWSER_MARKERS = ("quartz pdfcontext",)

#: Producers that NAME a browser engine but are an application export, not a
#: printed page. Google Docs renders its PDF export through Skia, so
#: ``Skia/PDF m144 Google Docs Renderer`` matched the strong ``skia`` marker
#: and a 208-page report came out badged "printed web page" — the engine name
#: is generic, the product name is specific, and the specific one wins.
_BROWSER_ENGINE_EXCEPTIONS = ("google docs",)

#: Producer/creator substrings, lower-cased. First match wins, so the order is
#: the specificity order. Every token here was observed on the live library.
_PRODUCER_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("scan", ("scanner", "imagerunner", "canon", "epson", "kyocera",
              "xerox workcentre", "paper capture", "pdfcompressor")),
    ("office_export", ("microsoft word", "microsoft® word", "powerpoint",
                       "microsoft® powerpoint", "pages", "libreoffice",
                       "openoffice", "google docs")),
    # NOTE the absence of a bare "tex": these are SUBSTRING tests, and "tex"
    # is a substring of "conTEXt" — including `Quartz PDFContext`, which every
    # macOS print-to-PDF carries. It matched 78 residual files here, ~17 of
    # them Quartz ones that only avoided being labelled `publisher` because
    # `browser_print` happened to be tested first. `itext` is listed explicitly
    # because that is the real toolchain the token was earning its keep on.
    ("publisher", ("acrobat distiller", "pdflatex", "xetex", "luatex",
                   "pdftex", "itext", "dvips",
                   "arbortext", "appligent", "3b2", "springer", "elsevier",
                   "sciencedirect", "xpp", "atypon", "highwire", "cenveo",
                   "aptara")),
)

_URL_RE = re.compile(
    r"https?://[^\s<>\"'\)\]]{6,300}", re.IGNORECASE)

#: A browser print repeats the page URL in its header/footer. Anything that is
#: obviously a publisher/DOI resolver link is NOT the source page.
_URL_NOISE = (
    "doi.org", "crossref.org", "creativecommons.org", "w3.org",
    "schema.org", "adobe.com", "purl.org", "orcid.org",
)

#: Longer than this, with no publisher fingerprint, reads as a book. The
#: default is deliberately generous: the measured no-producer orphan class
#: averages 499 pages, and an ordinary article's 90th percentile is 26.
DEFAULT_BOOK_MIN_PAGES = 80


def may_be_browser_print(producer: str | None, creator: str | None) -> bool:
    """Whether recovering a source URL is worth an extraction for this file.

    True for a strong browser fingerprint and for the weak macOS marker that
    needs one to be corroborated. Lets the caller pay for URL recovery on the
    ~40 files where it decides something, rather than on every residual PDF —
    where it would mostly memoise licence boilerplate.
    """
    blob = f"{producer or ''} {creator or ''}".strip().lower()
    if any(m in blob for m in _BROWSER_ENGINE_EXCEPTIONS):
        return False
    return any(m in blob for m in _BROWSER_MARKERS + _WEAK_BROWSER_MARKERS)


def classify(producer: str | None, creator: str | None, pages: int,
             *, book_min_pages: int = DEFAULT_BOOK_MIN_PAGES,
             source_url: str | None = None) -> str:
    """Best-effort class for one PDF. Never raises; unknown ⇒ ``"article"``.

    ``source_url`` is the page URL :func:`extract_source_url` recovered, when
    the caller has one. It is only ever used to CORROBORATE the weak macOS
    producer marker (see :data:`_WEAK_BROWSER_MARKERS`); no class is invented
    from a URL alone, since a licence or resolver link appears on a great many
    ordinary PDFs.
    """
    blob = f"{producer or ''} {creator or ''}".strip().lower()

    if not any(m in blob for m in _BROWSER_ENGINE_EXCEPTIONS):
        if any(m in blob for m in _BROWSER_MARKERS):
            return "browser_print"
        if any(m in blob for m in _WEAK_BROWSER_MARKERS) and source_url:
            return "browser_print"

    for label, markers in _PRODUCER_MARKERS:
        if any(m in blob for m in markers):
            # A very long "publisher" file is still a book in practice (an
            # entire proceedings volume, a textbook) — the page count wins.
            if label == "publisher" and pages >= book_min_pages:
                return "book"
            return label

    if pages >= book_min_pages:
        return "book"
    return "article"


def extract_source_url(page1_text: str | None,
                       info_blob: str | None = None) -> str | None:
    """The source URL of a printed web page, or None.

    For the ``browser_print`` class this is the only identifier that will ever
    exist — no catalogue holds these — so recovering it turns a dead row into
    a one-click "add by URL" in the Zotero connector.

    Prefers the PDF's own metadata (a browser writes the page URL there) and
    falls back to page-1 text, where the print header/footer repeats it.
    Resolver and licence URLs are filtered out: they are boilerplate on almost
    every scholarly PDF and would otherwise win by being first.
    """
    for source in (info_blob, page1_text):
        if not source:
            continue
        for m in _URL_RE.finditer(source):
            url = m.group(0).rstrip(".,;)]}'\"")
            low = url.lower()
            if any(noise in low for noise in _URL_NOISE):
                continue
            return url
    return None
