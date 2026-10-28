"""Markdown long-form generation — a structured Obsidian note, grounded in the vault.

The third docgen output kind (after the article/report LaTeX): instead of a
``.tex``, produce a single well-structured Markdown note (``# Title`` + ``##``
sections of prose), section by section, over the SAME deckgen engine — bound to
Markdown prompts, a Markdown ``sanitize`` (heading gate, not a ``\\section`` /
frame gate) and a Markdown placeholder.

Pure and app-independent: this module knows nothing about the vault or how the
note is written. The route performs the (journalled, reversible) vault write.
"""
from __future__ import annotations

import re

from deckgen.latex import SectionOutput
from deckgen.outline import request_outline as _deck_request_outline
from deckgen.sections import generate_section as _deck_generate_section

# --- prompts ---------------------------------------------------------------- #

SYSTEM_PROMPT_LIMIT = 4000

OUTLINE_SYSTEM_PROMPT = (
    "You are an expert note-taker helping the user structure a long-form Markdown "
    "note. Treat the user's vault as the SOLE source of substantive content; do not "
    "add facts that are not supported by it. The vault mixes French, German and "
    "English notes — use sources in all three languages, and write the section "
    "titles in the language of the user's topic and instructions. "
    "When asked for an outline, respond with "
    "a SINGLE JSON array and nothing else — no prose, no commentary, no code fences. "
    'Each element is an object with exactly two keys: "title" (a concise section '
    'heading) and "points" (an array of 3-6 short strings naming what that section '
    "should cover). Order the sections logically for the topic."
)

SECTION_SYSTEM_PROMPT = (
    "You are writing ONE section of a structured Markdown note for {audience}. "
    "Output ONLY Markdown for THIS section and nothing else: start with a `## "
    "{title}` heading, then well-structured prose paragraphs (and `-` bullet lists "
    "or `###` sub-headings where they genuinely help). Do NOT output YAML "
    "frontmatter, a top-level `# ` title, LaTeX, or any commentary outside the note. "
    "Write in connected paragraphs — do not reduce everything to bullets. Ground "
    "every claim in the vault — when you use a fact from a note, cite its source "
    "filename inline in plain prose, e.g. (source: note-name.md). Never invent facts "
    "or sources that are not in the vault. The vault mixes French, German and "
    "English notes: use supporting material in all three languages, and write the "
    "note in the language of the user's topic and instructions."
)


def section_system_prompt(
    audience: str,
    *,
    title: str = "",
    macros_block: str = "",   # unused for Markdown; kept for engine-hook parity
    cite_mode: str = "prose",  # Markdown always cites in prose
    images_enabled: bool = True,
) -> str:
    """Render the per-section Markdown system prompt.

    Signature-compatible with :func:`deckgen.sections.generate_section`'s
    ``system_prompt_builder`` hook (it passes ``audience, macros_block, cite_mode,
    images_enabled``). ``title`` is optional context; the LaTeX-only knobs are
    accepted and ignored so the same engine drives Markdown unchanged.
    """
    return SECTION_SYSTEM_PROMPT.format(
        audience=audience or "the intended reader",
        title=title or "Section",
    )


def build_outline_message(topic: str, instructions: str, max_sections: int,
                          *, outline_kind: str = "note", unit: str = "sections") -> str:
    parts = [f"Topic: {topic}"]
    if instructions.strip():
        parts.append(f"Instructions from the author:\n{instructions.strip()}")
    parts.append(
        f"Design a {outline_kind} outline on this topic using ONLY knowledge found in the "
        f"vault. Return a JSON array of between 3 and {max_sections} {unit}, each "
        'an object {"title": str, "points": [str, ...]}. Output ONLY the JSON array.\n'
        'Example shape (structure only — do not reuse this content): '
        '[{"title": "Background", "points": ["what it is", "why it matters"]}]'
    )
    return "\n\n".join(parts)


def build_section_message(
    *,
    topic: str,
    instructions: str,
    full_outline: list,
    index: int,
    title: str,
    points: list,
    candidate_bib_block: str = "",  # unused for Markdown; kept for hook parity
    content_description: str = "the section's prose",
) -> str:
    outline_lines = []
    for i, sec in enumerate(full_outline, start=1):
        marker = " <-- WRITE THIS ONE" if i == index else ""
        outline_lines.append(f"{i}. {sec.title}{marker}")
        for p in sec.points:
            outline_lines.append(f"   - {p}")
    outline_block = "\n".join(outline_lines)
    point_block = "\n".join(f"- {p}" for p in points) if points else "(no sub-points given)"

    parts = [f"Topic of the whole note: {topic}"]
    if instructions.strip():
        parts.append(f"Author's instructions:\n{instructions.strip()}")
    parts.append("Full outline (for context — do NOT rewrite other sections):\n" + outline_block)
    parts.append(
        f"Now write ONLY section {index}: \"{title}\".\n"
        f"Cover these points:\n{point_block}\n\n"
        "Start with a `## " + title + f"` heading, then {content_description}. Do not "
        "repeat material that belongs to other sections."
    )
    return "\n\n".join(parts)


# --- sanitize + assemble ---------------------------------------------------- #

_MD_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+\S")


def _strip_fence_wrapper(text: str) -> str:
    """Unwrap a whole-answer ```` ```markdown … ``` ```` fence, keeping inner code."""
    lines = text.strip().splitlines()
    if len(lines) >= 2 and lines[0].lstrip().startswith("```") and lines[-1].strip().startswith("```"):
        return "\n".join(lines[1:-1])
    return text


def sanitize_markdown_section(text: str) -> str:
    """Reduce a model answer to just the Markdown section body we will embed.

    Unwraps a whole-answer code-fence wrapper, trims leading commentary by slicing
    from the first heading, and requires a surviving heading (the Markdown gate,
    parallel to the article's ``\\section`` gate): a reply with no heading is loose
    prose or a server fallback, so we signal "empty" and the caller substitutes a
    placeholder section. Idempotent.
    """
    if not text:
        return ""
    body = _strip_fence_wrapper(text)
    m = _MD_HEADING_RE.search(body)
    if m:
        body = body[m.start():]
    body = body.strip()
    if not _MD_HEADING_RE.search(body):
        return ""
    return body


def _md_placeholder(section) -> str:
    return f"## {section.title}\n\n_(no content generated for this section)_"


def _frontmatter_block(frontmatter: dict) -> str:
    """Render a minimal YAML frontmatter block from scalar string values.

    Provenance stamping (2026-07-22): keys/values are app-controlled scalars
    (provider id, model id, ISO date, topic), rendered one ``key: "value"`` per
    line with double quotes and escaped ``\\``/``"`` so a model name or topic
    containing YAML syntax cannot break the block. Empty values are skipped.
    Pure — no dates or config are resolved here (caller supplies everything),
    keeping this module app-independent.
    """
    lines = ["---"]
    for key, value in frontmatter.items():
        text = str(value).strip()
        if not text:
            continue
        text = text.replace("\\", "\\\\").replace('"', '\\"')
        # Frontmatter values are single-line by contract; fold any newline.
        text = " ".join(text.split())
        lines.append(f'{key}: "{text}"')
    lines.append("---")
    return "\n".join(lines)


def assemble_markdown(
    title: str,
    sections: list[SectionOutput],
    *,
    frontmatter: dict | None = None,
) -> str:
    """Assemble the note: optional YAML frontmatter, ``# Title``, ``##`` bodies.

    *frontmatter* (default ``None`` — byte-identical legacy output) is a
    flat dict of provenance keys the route supplies (generated_by / provider /
    model / generated date / topic); see :func:`_frontmatter_block`.
    """
    parts: list[str] = []
    if frontmatter:
        parts.append(_frontmatter_block(frontmatter))
    if (title or "").strip():
        parts.append(f"# {title.strip()}")
    bodies = [s.body.strip() for s in sections if s.body.strip()]
    parts.append("\n\n".join(bodies) if bodies else "_(no content generated)_")
    return "\n\n".join(parts).strip() + "\n"


# --- generation bindings (deckgen engine bound to the Markdown hooks) -------- #

def request_outline(client, **kwargs):
    """Markdown outline via the deckgen engine bound to the Markdown prompts."""
    return _deck_request_outline(
        client,
        system_prompt=OUTLINE_SYSTEM_PROMPT,
        build_message=build_outline_message,
        unit_label="note",
        **kwargs,
    )


def generate_section(client, **kwargs):
    """One Markdown section via the deckgen engine bound to the Markdown hooks."""
    section = kwargs.get("section")
    title = section.title if section is not None else ""

    def _system_prompt_builder(audience, *, macros_block="", cite_mode="prose", images_enabled=True):
        return section_system_prompt(
            audience, title=title, macros_block=macros_block,
            cite_mode=cite_mode, images_enabled=images_enabled,
        )

    return _deck_generate_section(
        client,
        sanitize=sanitize_markdown_section,
        system_prompt_builder=_system_prompt_builder,
        build_message=build_section_message,
        placeholder_builder=_md_placeholder,
        **kwargs,
    )
