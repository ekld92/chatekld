"""Pure ``Record`` → Obsidian reference-note text.

A faithful port of the maintainer's ``literature_import_template.md`` (Zotero
Integration / Nunjucks), reimplemented so the app can render the same note
without the plugin — and so the layout becomes a SETTING rather than a file
outside the app.

Deliberately not a Nunjucks interpreter: the template is a handful of
placeholders, and shipping a template language would mean shipping arbitrary
code execution over vault content. The placeholder vocabulary is closed,
documented in :data:`PLACEHOLDERS`, and anything unrecognised is left alone
rather than silently emptied — a typo in the user's template must be visible.

One deliberate divergence from the plugin's output: the original emits
``* Error: `format` can only be applied to dates. Tried for format object*``
into the body for any item with no parseable date (visible in
``ExampleArticle2024.md``). Here a missing date simply renders nothing.
"""

from __future__ import annotations

import re
from typing import Any

# `widget/textclean.py` is pure and stdlib-only. Reused rather than
# reimplemented because BibTeX fields ARE LaTeX source: without it a title
# reaches the note as `Beck {{Depression Inventory}}` or `schizophr\'enie`,
# which is exactly the class of bug that module was written to fix for the
# desktop widgets. Applied to display fields only — never to the Zotero note
# body, whose legitimate `$` and `~` must survive byte-identically.
from notesync.compare import REGION_BEGIN, REGION_END, split_note
from notesync.convert import html_to_md
from widget.textclean import clean_display

#: The closed placeholder vocabulary. Anything else in a user template is left
#: verbatim, so a typo shows up in the rendered note instead of vanishing.
PLACEHOLDERS: dict[str, str] = {
    "title": "The item's title",
    "citekey": "Better BibTeX citation key",
    "author": "First author's surname",
    "year": "Publication year",
    "container": "Journal abbreviation (or publication title)",
    "doi": "DOI, without a URL prefix",
    "notes": "The Zotero reading note(s), as plain text",
}

#: The default layout, byte-compatible with the maintainer's template's output.
DEFAULT_TEMPLATE = """**{{title}}**

>{{citekey}}

> *{{author}} {{year}}*
>
> {{container}}
>
> {{doi}}

---

<!-- zotero-note:begin -->

{{notes}}

<!-- zotero-note:end -->"""

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

#: Frontmatter tag list cap — a defensive bound, not a product limit.
_MAX_TAGS = 200


def _clean(text: str) -> str:
    """LaTeX-source → display text, with a passthrough for plain strings."""
    if not text:
        return ""
    return clean_display(text) or ""


def _first_author_surname(record: Any) -> str:
    """Surname of the first author, from the bib entry's raw author strings.

    ``authors`` are ``"Last, First"`` as BibTeX exports them; a name with no
    comma falls back to its last whitespace token, which is the same rule the
    bridge's filename matcher uses.
    """
    bib = getattr(record, "bib_entry", None)
    authors = list(getattr(bib, "authors", None) or [])
    if not authors:
        return ""
    raw = _clean(str(authors[0])).strip()
    if "," in raw:
        return raw.split(",", 1)[0].strip()
    parts = raw.split()
    return parts[-1] if parts else ""


def _year(record: Any) -> str:
    """Four-digit year from the bib entry, else from Zotero's date string.

    Zotero stores dates as ``"2023-04-00 2023-4"`` and similar; the first
    four-digit run is the year in every observed shape.
    """
    bib = getattr(record, "bib_entry", None)
    raw = str(getattr(bib, "year", "") or "").strip()
    m = re.search(r"(1[5-9]\d{2}|20\d{2}|21\d{2})", raw)
    if m:
        return m.group(1)
    item = getattr(record, "zotero_item", None)
    m = re.search(r"(1[5-9]\d{2}|20\d{2}|21\d{2})",
                  str(getattr(item, "date", "") or ""))
    return m.group(1) if m else ""


def zotero_note_text(record: Any) -> str:
    """The record's Zotero reading notes as MARKDOWN, in stored order.

    Several child notes are joined with a blank line, matching the template's
    ``{% for note in notes %}`` loop.

    Markdown since 2026-09-04 (Phase 1 of the note-sync plan): the plugin that
    created every existing reference note converts the Zotero HTML to Markdown,
    and this function used to return ``body_plain`` — the HTML stripped to
    text — so a note the app created was lossier than one the plugin created,
    and the comparison in :mod:`notesync.compare` was measured against a
    reference the vault never contained (555 notes read as "annotated by the
    user" when the honest count is under 50). A note object without
    ``body_html`` (older readers, the test doubles) falls back to
    ``body_plain`` so nothing downstream changes shape.
    """
    item = getattr(record, "zotero_item", None)
    notes = list(getattr(item, "child_notes", None) or [])
    bodies = []
    for n in notes:
        html = getattr(n, "body_html", "") or ""
        text = html_to_md(html) if html else str(getattr(n, "body_plain", "") or "")
        bodies.append(text.strip())
    return "\n\n".join(b for b in bodies if b)


def render_body(record: Any, template: str = "", *,
                notes_override: str | None = None) -> str:
    """Render the note BODY (everything after the frontmatter).

    Substitution is literal and closed: each known placeholder is replaced by
    its value, an unknown one is left in place. A placeholder whose value is
    empty leaves an empty line rather than the plugin's error text.

    ``notes_override`` substitutes something other than the Zotero note text
    into the ``{{notes}}`` slot. Its only caller is :func:`render_region`, which
    uses it to place a sentinel — see there for why that is not a convenience.
    """
    values = {
        "title": _clean(str(
            getattr(getattr(record, "bib_entry", None), "title", None)
            or getattr(getattr(record, "zotero_item", None), "title", None)
            or "")).strip(),
        "citekey": str(getattr(record, "citation_key", "") or ""),
        "author": _first_author_surname(record),
        "year": _year(record),
        "container": str(
            getattr(getattr(record, "zotero_item", None), "container", "") or ""),
        "doi": str(
            getattr(getattr(record, "zotero_item", None), "doi", None)
            or getattr(getattr(record, "bib_entry", None), "doi", None) or ""),
        "notes": (zotero_note_text(record) if notes_override is None
                  else notes_override),
    }

    def _sub(m: re.Match) -> str:
        name = m.group(1)
        return values[name] if name in values else m.group(0)

    # Line-wise, so a line whose every placeholder resolved EMPTY is dropped
    # rather than rendered as its punctuation. The maintainer's plugin template
    # has an author/year line (`> *{{author}} {{year}}*`) and a journal line;
    # for an item with neither, substitution alone leaves `> * *` and `> `,
    # which is how the plugin's own `Error: format can only be applied to
    # dates` ended up in a shipped note. A line with NO placeholders (the `---`
    # rule, a bare `>` spacer) is always kept — it is layout, not content.
    out_lines = []
    for line in (template or DEFAULT_TEMPLATE).splitlines():
        names = _PLACEHOLDER_RE.findall(line)
        known = [n for n in names if n in values]
        if known and not any(values[n] for n in known):
            continue
        out_lines.append(_PLACEHOLDER_RE.sub(_sub, line))
    return "\n".join(out_lines)


def render_frontmatter(tags, extra: dict[str, str] | None = None) -> str:
    """The YAML frontmatter block, carrying only ``tags``.

    Tags are the tagging subsystem's territory — this is the ONE field the
    template owns, and it is rendered in the same flow-list shape the existing
    notes use (``tags: [a, b]``) so a regenerated note is diff-clean against a
    plugin-generated one. An empty list renders ``tags: []``, which is what the
    plugin emits and what the audit's frontmatter parser expects.
    """
    clean = []
    for tag in list(tags or [])[:_MAX_TAGS]:
        text = str(tag).strip().lstrip("#")
        # A tag containing a quote or a bracket cannot ride a flow list
        # unquoted; the vocabulary gate forbids those, so this is belt and
        # braces rather than an expected path.
        if text and not any(c in text for c in "[]{}\",'\n"):
            clean.append(text)
    inner = ", ".join(clean)
    lines = [f"tags: [{inner}]"]
    # Phase 2 layout (2026-09-04): the Zotero item + note keys, in the same
    # closed shape ``notesync.migrate`` writes into existing notes.
    for k, v in (extra or {}).items():
        if re.fullmatch(r"[A-Z0-9]{8}", str(v or "")):
            lines.append(f"{k}: {v}")
    return "---\n" + "\n".join(lines) + "\n---\n"


def render_note(record: Any, template: str = "", tags=None, *,
                notes_override: str | None = None) -> str:
    """Full note text: frontmatter + body, newline-terminated."""
    body = render_body(record, template, notes_override=notes_override)
    tag_list = tags if tags is not None else sorted(
        getattr(record, "obs_tags", None) or [])
    text = render_frontmatter(tag_list, zotero_keys(record)) + "\n" + body.rstrip("\n") + "\n"
    return text


def zotero_keys(record: Any) -> dict[str, str]:
    """``{zotero-key, zotero-note-key}`` for a record with exactly one child
    note; empty when either key is unavailable. Additive frontmatter — the
    tagging writers' round-trip guard compares the mapping minus ``tags``, so
    two extra scalar keys pass through it unchanged."""
    item = getattr(record, "zotero_item", None)
    notes = list(getattr(item, "child_notes", None) or [])
    out: dict[str, str] = {}
    item_key = str(getattr(item, "item_key", "") or "")
    if item_key:
        out["zotero-key"] = item_key
    if len(notes) == 1:
        note_key = str(getattr(notes[0], "note_key", "") or "")
        if note_key:
            out["zotero-note-key"] = note_key
    return out


#: Placeholder stood into the ``{{notes}}`` slot while locating the template's
#: region. NUL cannot appear in a note (the vault stores text) or in a template
#: (config strings are validated), so it cannot be confused with real content.
_REGION_SENTINEL = "\x00notesync-region-slot\x00"


def unmarked_template(template: str) -> str:
    """The template with its marker pair removed — the pre-Phase-2 shape.

    The default template puts a blank line inside each marker; removing the
    marker line together with that blank line yields byte-for-byte the legacy
    ``---\n\n{{notes}}`` layout, which is what an UNMARKED note on disk was
    written with. A custom template without markers is returned unchanged.
    """
    text = template or DEFAULT_TEMPLATE
    return (text.replace(REGION_BEGIN + "\n\n", "")
                .replace("\n\n" + REGION_END, "")
                .replace(REGION_BEGIN + "\n", "")
                .replace("\n" + REGION_END, ""))


def render_region(record: Any, template: str = "", *,
                  marked_target: bool = True) -> tuple[str | None, str | None]:
    """The region a write would lay down for this record.

    Returns ``(region, None)`` or ``(None, reason)`` — exactly one is set.

    ``marked_target`` says which layout the TARGET note has: a marked note's
    region sits between the marker pair, an unmarked one's below the single
    ``---`` rule (:func:`unmarked_template`). The two differ only in where the
    blank lines fall, but a region-scoped write compares bytes, so rendering
    the wrong shape would report every unmigrated note as changed.

    **Why the sentinel rather than splitting the finished note.** The obvious
    implementation is ``split_note(render_note(record, template))[1]``, and it
    is wrong in a way that is silent: the rule the split finds is whichever
    ``---`` line comes first, and the Zotero reading note's own text is part of
    the rendered document. A note containing a markdown horizontal rule would
    therefore either make the split ambiguous (two rules → refused, with a
    message blaming the template) or — against a custom template that has no
    rule of its own — split INSIDE the reading note and return a truncated
    region, which the writer would then lay down over the real one. Rendering
    the slot as an opaque sentinel first makes the rule unambiguous whatever
    the note contains; the real text is substituted afterwards, so the result
    is byte-identical to what :func:`render_note` produces.

    No live note triggers this today (measured: zero of 1,236 Zotero reading
    notes contain a bare ``---`` line), which is exactly why it is worth
    handling here rather than discovering later.

    Reasons: ``template_shape`` — the rendered note has no single ``---`` rule,
    so there is no region to identify; ``notes_not_below_rule`` — the template
    puts ``{{notes}}`` above its rule, so the region is not the reading note's.
    """
    if not marked_target:
        template = unmarked_template(template)
    scaffold = render_note(record, template, tags=[],
                           notes_override=_REGION_SENTINEL)
    split = split_note(scaffold)
    if split is None:
        return None, "template_shape"
    region = split[1]
    if _REGION_SENTINEL not in region:
        return None, "notes_not_below_rule"
    return region.replace(_REGION_SENTINEL, zotero_note_text(record)), None
