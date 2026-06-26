"""System-prompt prefixes and message builders for the two generation phases.

ChatEKLD's ``system_prompt`` is a *prefix* layered over its own answer-mode
template (the app keeps the grounding + safety preamble and the
``{context_str}``/``{query_str}`` slots — see ``rag/CLAUDE.md``). So these
strings *steer* output; they cannot fully replace the template. The hard
guarantees (valid Beamer) come from ``assemble.sanitize_section`` /
``assemble.validate`` downstream, not from the prompt.

Every system prompt here must stay under ChatEKLD's ``VAULT_SYSTEM_PROMPT_LIMIT``
of 4000 characters (``deckgen.client`` asserts this defensively).
"""
from __future__ import annotations

# Hard limit ChatEKLD enforces on the system_prompt body (VAULT_SYSTEM_PROMPT_LIMIT).
SYSTEM_PROMPT_LIMIT = 4000


OUTLINE_SYSTEM_PROMPT = (
    "You are an expert lecture architect helping the user prepare a teaching "
    "presentation. Treat the user's vault as the SOLE source of substantive "
    "content; do not add facts that are not supported by it. When the user asks "
    "for an outline, respond with a SINGLE JSON array and nothing else — no prose, "
    "no commentary, no markdown code fences. Each array element is an object with "
    'exactly two keys: "title" (a concise string) and "points" (an array of 3-6 '
    "short strings naming what that section should cover). Order the sections "
    "pedagogically (e.g. definition/epidemiology, then mechanisms, then clinical "
    "features, then assessment/management) when the topic allows."
)


SECTION_SYSTEM_PROMPT = (
    "You are writing ONE section of a LaTeX Beamer lecture for {audience}. "
    "Output ONLY LaTeX Beamer source for THIS section and nothing else: a single "
    "\\section{{...}} line, then one or more \\begin{{frame}}{{Frame Title}} ... "
    "\\end{{frame}} blocks. Do NOT output \\documentclass, \\usepackage, "
    "\\begin{{document}}, \\end{{document}}, \\title, \\maketitle, a preamble, or "
    "any prose outside the LaTeX. Use \\begin{{itemize}} / \\item for bullets; aim "
    "for 3-6 bullets per frame and split long material across several frames rather "
    "than overflowing one. Ground every claim in the vault — when you use a fact "
    "from a note, cite its source filename inline in plain prose, e.g. "
    "(source: note-name.md). Never invent citations or facts that are not in the "
    "vault. Escape LaTeX special characters (% & _ # $) when they appear in prose."
)


_BIB_CITE_RULE = (
    " For citations, PREFER \\citefoot{{key}} using ONLY a key from the "
    "'Candidate references' list in the task message; if no candidate fits a "
    "claim, fall back to the plain-prose (source: note.md) form. NEVER invent a "
    "citation key."
)


def section_system_prompt(
    audience: str,
    *,
    macros_block: str = "",
    cite_mode: str = "prose",
) -> str:
    """Render the per-section system prompt.

    *macros_block* (from :func:`deckgen.template.macro_cheatsheet`) lists the
    document-specific macros the deck defines; *cite_mode* of ``"bib"`` steers
    the model toward ``\\citefoot{key}`` against a candidate list passed in the
    user message (``"prose"`` keeps the legacy source-filename form). The result
    is capped at :data:`SYSTEM_PROMPT_LIMIT` by trimming the macro block.
    """
    prompt = SECTION_SYSTEM_PROMPT.format(audience=audience or "the intended audience")
    if cite_mode == "bib":
        prompt += _BIB_CITE_RULE
    if macros_block:
        macro_section = (
            "\n\nThis document defines custom macros you SHOULD use where "
            "appropriate (do NOT redefine them or their packages):\n" + macros_block
        )
        budget = SYSTEM_PROMPT_LIMIT - len(prompt)
        if len(macro_section) > budget:
            # Trim to fit. Reserve 1 char for the ellipsis; if there is no room
            # for even a meaningful slice, drop the macro block entirely rather
            # than appending a lone "…" that could push us 1 char over the cap.
            if budget > 16:
                macro_section = macro_section[: budget - 1].rstrip() + "…"
            else:
                macro_section = ""
        prompt += macro_section
    return prompt


def build_outline_message(topic: str, instructions: str, max_sections: int) -> str:
    """The user-turn message for the outline phase."""
    parts = [f"Topic: {topic}"]
    if instructions.strip():
        parts.append(f"Instructions from the lecturer:\n{instructions.strip()}")
    parts.append(
        f"Design a lecture outline on this topic using ONLY knowledge found in the "
        f"vault. Return a JSON array of between 3 and {max_sections} sections, each "
        'an object {"title": str, "points": [str, ...]}. Output ONLY the JSON array.\n'
        # 2026-06 audit: a one-line shape example anchors the JSON structure for
        # small models. It is kept in NON-f-string segments so its { } braces
        # stay literal — only the f-prefixed lines above interpolate
        # {max_sections}. This is a hint, not a guarantee: outline.parse_outline
        # remains authoritative (balanced-bracket extraction + heading-list
        # fallback); the example just reduces how often that fallback is needed.
        'Example shape (structure only — do not reuse this content): '
        '[{"title": "Definition & Epidemiology", "points": ["what it is", "how common it is"]}]'
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
    candidate_bib_block: str = "",
) -> str:
    """The user-turn message for one section.

    The *full_outline* (all titles + points) is included so the agent keeps the
    deck coherent and avoids duplicating neighbouring sections.
    *candidate_bib_block* (from :func:`deckgen.template.bib_candidates_block`),
    when present, lists the only bib keys the model may cite with
    ``\\citefoot{key}`` for this section.
    """
    outline_lines = []
    for i, sec in enumerate(full_outline, start=1):
        marker = " <-- WRITE THIS ONE" if i == index else ""
        outline_lines.append(f"{i}. {sec.title}{marker}")
        for p in sec.points:
            outline_lines.append(f"   - {p}")
    outline_block = "\n".join(outline_lines)

    point_block = "\n".join(f"- {p}" for p in points) if points else "(no sub-points given)"

    parts = [f"Topic of the whole lecture: {topic}"]
    if instructions.strip():
        parts.append(f"Lecturer's instructions:\n{instructions.strip()}")
    parts.append("Full lecture outline (for context — do NOT rewrite other sections):\n" + outline_block)
    if candidate_bib_block.strip():
        parts.append(
            "Candidate references — you may cite ANY of these with \\citefoot{key} "
            "(use the exact key; do NOT cite a key not listed here):\n"
            + candidate_bib_block
        )
    parts.append(
        f"Now write ONLY section {index}: \"{title}\".\n"
        f"Cover these points:\n{point_block}\n\n"
        "Produce the \\section line and its frames as specified in the system "
        "instructions. Do not repeat material that belongs to other sections."
    )
    return "\n\n".join(parts)
