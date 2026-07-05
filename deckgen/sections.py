"""Per-section generation phase.

For each outline section, run one agent-mode chat turn (so ChatEKLD's agent can
search/read the vault) asking for the section's Beamer frames, then sanitize the
output into an embeddable body.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .assemble import SectionOutput, sanitize_section
from .prompts import build_section_message, section_system_prompt

if TYPE_CHECKING:
    # Type-only: importing the HTTP client at runtime would pull in ``requests``,
    # which the in-process (app) path must not require. The in-process runner is
    # duck-typed as a ChatRunner (anything exposing ``.chat(...) -> ChatResult``).
    from .client import ChatEKLDClient


def generate_section(
    client: ChatEKLDClient,
    *,
    index: int,
    section,            # outline.Section
    full_outline: list,
    topic: str,
    instructions: str,
    audience: str,
    provider: str,
    model: str,
    embed: str,
    max_iters: int,
    temperature: Optional[float],
    macros_block: str = "",
    cite_mode: str = "prose",
    candidate_bib_block: str = "",
    images_enabled: bool = True,
    on_event=None,
    max_attempts: int = 1,
    retry_backoff_s: float = 0.0,
    should_cancel=None,
) -> SectionOutput:
    """Generate and sanitize one section. Errors are surfaced as an info note,
    not raised, so one weak section does not abort the whole deck.

    *macros_block* / *cite_mode* / *candidate_bib_block* steer the model toward
    the deck's custom macros and bibliography (template mode); the defaults keep
    the legacy plain-prose behaviour.

    *max_attempts* / *retry_backoff_s* / *should_cancel* drive the cancel-aware
    per-section retry (see :func:`deckgen.retry.chat_with_retry`); the defaults
    (``max_attempts=1``) keep the original single-shot behaviour. A section that
    still fails after every attempt degrades to the placeholder frame below.
    """
    from .retry import chat_with_retry

    message = build_section_message(
        topic=topic,
        instructions=instructions,
        full_outline=full_outline,
        index=index,
        title=section.title,
        points=section.points,
        candidate_bib_block=candidate_bib_block,
    )
    result = chat_with_retry(
        client,
        message,
        max_attempts=max_attempts,
        retry_backoff_s=retry_backoff_s,
        should_cancel=should_cancel,
        label=f"section {index} ({section.title})",
        on_event=on_event,
        system_prompt=section_system_prompt(
            audience, macros_block=macros_block, cite_mode=cite_mode,
            images_enabled=images_enabled,
        ),
        provider=provider,
        model=model,
        embed=embed,
        agent=True,
        max_iters=max_iters,
        temperature=temperature,
    )

    body = sanitize_section(result.text)
    infos = list(result.infos)
    placeholder = False
    if result.error:
        infos.append(f"section generation error: {result.error}")
    if not body:
        # Keep the deck assembling: emit a placeholder frame for this section.
        placeholder = True
        infos.append("no usable Beamer content returned; inserted a placeholder frame")
        body = (
            f"\\section{{{section.title}}}\n"
            f"\\begin{{frame}}{{{section.title}}}\n"
            f"  \\begin{{itemize}}\n"
            f"    \\item (no content generated for this section)\n"
            f"  \\end{{itemize}}\n"
            f"\\end{{frame}}"
        )

    return SectionOutput(
        title=section.title, body=body, raw=result.text,
        infos=infos, placeholder=placeholder,
    )
