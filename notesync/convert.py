"""Zotero note HTML ⇄ Markdown, the ONE converter pair the sync path uses.

Historical exported reading notes use this shell::

    <div class="zotero-note znv1"><div data-schema-version="9">…</div></div>

(measured 2026-09-04: every one of the 1,236 live child notes). Native 10.0.2
disposable PATCH readback on 2026-09-15 instead retains only the inner schema
div. Writes emit that inner shell and native link rel attributes; the reader
still accepts both shells. The Obsidian
reference note holds the same text as Markdown, written by the Zotero
Integration plugin's ``htmlToMarkdown`` at import time. Anything that compares
the two — or writes one from the other — has to convert, and it has to convert
the SAME way every time, because the sync state model decides "did Zotero
change?" by comparing ``html_to_md(now)`` against ``html_to_md(base)``: a
converter that drifts between calls would report edits nobody made.

Why these settings (each measured over the live library, spike S2 2026-09-04):

* ``bullets="-"`` — the plugin emits ``-`` list markers; markdownify's default
  ``*`` alone accounted for 277 false "the user annotated this note" rows.
* ``escape_misc=True`` — without it a note line beginning ``>20 : severe`` or
  ``+ fqt que MDD`` (medical shorthand, both live) round-trips through
  Markdown as a BLOCKQUOTE or a nested LIST: the only class of round-trip
  failure that changes meaning. With it, 1,201 of 1,236 notes are exact fixed
  points; the residue is blank-line placement (14), bold adjacent to
  punctuation that CommonMark cannot express and which degrades to literal
  asterisks (11), and non-breaking-space pseudo-indentation (10, handled below).
* ``<img>`` passthrough — 7 notes embed Zotero image attachments as
  ``<img data-attachment-key="…">``. Markdown cannot carry them and the
  plugin dropped them; a push that dropped them would DESTROY the image link
  (maintainer decision 2026-09-04: carry the node verbatim as raw HTML; it is
  invisible in Obsidian, since the bytes live in Zotero, and survives a web-API
  PATCH untouched — verified live in spike S1).
* Non-breaking spaces are emitted as the ``&nbsp;`` entity, not U+00A0. Zotero
  fakes indentation with runs of ``&nbsp;`` inside ``<p>``; markdown-it-py
  strips a LEADING U+00A0 from a paragraph (Python's ``str.strip`` treats it as
  whitespace) so the raw character did not survive a round trip, and the
  de-indented text then re-parsed as a list. The entity is opaque to the
  Markdown parser and renders as a space in Obsidian.

The Markdown → HTML direction is CommonMark plus tables and strikethrough with
raw HTML enabled (so the ``<img>`` nodes and ``&nbsp;`` come back unchanged),
wrapped in the Zotero shell. It is used only when WRITING to Zotero.

``CONVERTER_ID`` names the pair; a sync base record stores it, so a future
change here invalidates every base record loudly instead of silently
re-classifying the library.

Pure: no app imports, no I/O. ``markdownify`` and ``markdown-it-py`` are
first-class dependencies since 2026-09-04 (they had been reaching the tree
only transitively through ``markitdown``).
"""

from __future__ import annotations

import re
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version as _pkg_version

from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdownify import MarkdownConverter

#: Zotero's note shell. ``data-schema-version`` is what Zotero's editor reads;
#: a body without it is treated as a legacy plain note.
# The outer znv1 div is an export wrapper, not the native stored note shell.
# Disposable Zotero 10.0.2 strips it on PATCH. Emit the stored shell directly
# so journal/inspection/undo can retain exact HTML equality after readback.
ZOTERO_NOTE_PREFIX = '<div data-schema-version="9">'
ZOTERO_NOTE_SUFFIX = "</div>"

#: An ``<img …>`` tag, as markdownify's passthrough emits it (it serialises the
#: element it was given, so attribute order is Zotero's own).
IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)


def _pkg(name: str) -> str:
    try:
        return _pkg_version(name)
    except PackageNotFoundError:  # pragma: no cover — import above would fail first
        return "unknown"


_WIKILINK_ESCAPED = re.compile(r"\\\[\\\[([^\[\]\n]+?)\\\]\\\]")

#: Identity of this converter pair. Stored in every sync base record.
#: /3 (2026-09-19): wikilinks survive html→md (see html_to_md).
CONVERTER_ID = (
    f"notesync-convert/3 markdownify={_pkg('markdownify')} "
    f"markdown-it-py={_pkg('markdown-it-py')}"
)


class _Converter(MarkdownConverter):
    """markdownify with the two departures documented in the module docstring."""

    def convert_img(self, el, text, parent_tags=None, **kwargs):  # noqa: ARG002
        # Verbatim. markdownify's default renders ``![alt](src)``, and a
        # Zotero image attachment has no ``src`` — the link would be empty and
        # the ``data-attachment-key`` lost.
        return str(el)


_HTML_TO_MD = _Converter(bullets="-", heading_style="ATX", escape_misc=True)
_MD_TO_HTML = MarkdownIt("commonmark", {"html": True}, renderer_cls=RendererHTML).enable(["table", "strikethrough"])


def _link_open(renderer, tokens, idx, options, env):
    # Match native serialization for generated Markdown links only. Raw HTML
    # remains untouched; readback never ignores attributes or DOM differences.
    tokens[idx].attrSet("rel", "noopener noreferrer nofollow")
    return renderer.renderToken(tokens, idx, options, env)


_MD_TO_HTML.add_render_rule("link_open", _link_open)


@lru_cache(maxsize=4096)
def html_to_md(html: str | None) -> str:
    """Zotero note HTML → Markdown, deterministic and memoised on the input.

    The shell ``<div>``s are transparent to markdownify, so the stored body can
    be passed as-is. Leading/trailing blank lines are stripped; interior
    whitespace is the converter's.
    """
    if not html:
        return ""
    md = _HTML_TO_MD.convert(html)
    md = md.replace(" ", "&nbsp;")
    # An Obsidian wikilink stored as text in Zotero (`[[citekey]]`, pushed
    # from a note region) comes back from markdownify as `\[\[citekey\]\]`,
    # which Obsidian renders literally — the link the mirror exists to keep
    # would die on the first refresh (2026-09-19, 5 live notes). Only the
    # doubled-bracket form is unescaped; a single `\[` stays the converter's.
    md = _WIKILINK_ESCAPED.sub(r"[[\1]]", md)
    return md.strip("\n")


def md_to_html(md: str | None) -> str:
    """Markdown → Zotero note HTML, wrapped in the native schema shell.

    Raw HTML in the Markdown (the ``<img>`` nodes, ``&nbsp;``) passes through.
    Only the WRITE path uses this; comparisons never do.
    """
    body = _MD_TO_HTML.render(md or "")
    return f"{ZOTERO_NOTE_PREFIX}{body}{ZOTERO_NOTE_SUFFIX}"


_ZNV1_WRAPPER_RE = re.compile(r'\A\s*<div class="zotero-note znv1">(.*)</div>\s*\Z', re.S)


def native_html(html: str | None) -> str:
    """The note HTML without Zotero's outer ``zotero-note znv1`` wrapper.

    Measured 2026-09-18: Zotero Desktop's local database stores notes wrapped
    in ``<div class="zotero-note znv1">…</div>`` while the web API returns the
    SAME note, at the same version, without it. Every guard that compared a
    local-database HTML (a base, a preview) with a web-API HTML therefore
    refused a note nobody had touched — safely, but always. Compare through
    this; never write it.
    """
    m = _ZNV1_WRAPPER_RE.match(html or "")
    return m.group(1) if m else (html or "")


def same_note_html(a: str | None, b: str | None) -> bool:
    return native_html(a) == native_html(b)


def strip_img_tags(text: str) -> str:
    """Remove ``<img …>`` tags so a word-level comparison does not count their
    attributes (``data``, ``attachment``, the key, pixel sizes) as words. The 7
    image-bearing live notes classified as "something to import" for exactly
    that reason before this existed."""
    return IMG_TAG_RE.sub(" ", text) if text else text
