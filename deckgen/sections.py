"""Per-section generation phase.

For each outline section, run one agent-mode chat turn (so CHERQAiN's agent can
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
    from .client import CHERQAiNClient


def _beamer_placeholder(section) -> str:
    """The default (Beamer) placeholder body for a section every attempt failed.

    Overridable via ``generate_section(placeholder_builder=…)`` so a prose
    document class (docgen) can substitute a ``\\section`` + paragraph instead of
    a frame. Kept as the module default so the deck path is byte-identical.
    """
    return (
        f"\\section{{{section.title}}}\n"
        f"\\begin{{frame}}{{{section.title}}}\n"
        f"  \\begin{{itemize}}\n"
        f"    \\item (no content generated for this section)\n"
        f"  \\end{{itemize}}\n"
        f"\\end{{frame}}"
    )


def generate_section(
    client: CHERQAiNClient,
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
    sanitize=sanitize_section,
    post_process=None,
    system_prompt_builder=section_system_prompt,
    build_message=build_section_message,
    placeholder_builder=_beamer_placeholder,
) -> SectionOutput:
    """Generate and sanitize one section. Errors are surfaced as an info note,
    not raised, so one weak section does not abort the whole deck.

    *macros_block* / *cite_mode* / *candidate_bib_block* steer the model toward
    the deck's custom macros and bibliography (template mode); the defaults keep
    the legacy plain-prose behaviour.

    *max_attempts* / *retry_backoff_s* / *should_cancel* drive the cancel-aware
    per-section retry (see :func:`deckgen.retry.chat_with_retry`); the defaults
    (``max_attempts=1``) keep the original single-shot behaviour. A section that
    still fails after every attempt degrades to the placeholder below.

    *sanitize* / *system_prompt_builder* / *build_message* / *placeholder_builder*
    are the document-class hooks: they default to the Beamer implementations so
    the deck path is byte-identical, and a prose document class (docgen) passes
    its own — the same salvage/retry/checkpoint machinery then serves articles.
    ``system_prompt_builder`` must accept ``(audience, macros_block, cite_mode,
    images_enabled)``; ``build_message`` the same kwargs as
    :func:`deckgen.prompts.build_section_message`; ``placeholder_builder`` a
    single ``section`` and return the fallback body.

    *post_process* is an optional ``(body) -> body`` applied after *sanitize* on
    both the agent and the salvage path. The route binds it to
    :func:`deckgen.latex.rewrite_source_tags` (with the bibliography and the
    document class's citation macro closed over) so the model's inline
    ``(source: note.md)`` attributions become real citations. ``None`` — every
    non-route caller, including the CLI and the tests — leaves the body exactly
    as *sanitize* produced it.
    """
    from .retry import chat_with_retry

    def _finish(text: str) -> str:
        """sanitize, then the optional class-specific post-pass. One pair, two
        call sites (agent + salvage) — kept together so neither can drift."""
        out = sanitize(text)
        return post_process(out) if (post_process is not None and out) else out

    message = build_message(
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
        system_prompt=system_prompt_builder(
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

    body = _finish(result.text)
    infos = list(result.infos)
    placeholder = False
    if result.error:
        infos.append(f"section generation error: {result.error}")
    if not body and not (should_cancel is not None and should_cancel()):
        # WP-A2 salvage pass: every agent attempt burned its budget without
        # usable Beamer output. Before freezing that failure into a
        # placeholder (discarding the tokens spent), run ONE tool-free
        # single-shot RAG turn over the same section brief — the smolagents
        # "provide_final_answer" pattern. Placeholder only if this also fails.
        infos.append(
            "agent turn produced no usable content; "
            "running a single-shot salvage pass"
        )
        if on_event is not None:
            try:
                on_event({"info": f"section {index}: salvage pass (single-shot, no tools)"})
            except Exception:
                pass
        try:
            salvage = client.chat(
                message,
                system_prompt=system_prompt_builder(
                    audience, macros_block=macros_block, cite_mode=cite_mode,
                    images_enabled=images_enabled,
                ),
                provider=provider,
                model=model,
                embed=embed,
                agent=False,
                max_iters=1,
                temperature=temperature,
            )
        except Exception as exc:  # defensive — salvage must never abort the deck
            salvage = None
            infos.append(f"salvage pass failed: {exc}")
        if salvage is not None:
            if salvage.error:
                infos.append(f"salvage pass error: {salvage.error}")
            body = _finish(salvage.text)
            if body:
                result = salvage
                infos.append("salvage pass produced usable content")
    if not body:
        # Keep the document assembling: emit a placeholder for this section.
        placeholder = True
        infos.append("no usable content returned; inserted a placeholder section")
        body = placeholder_builder(section)

    return SectionOutput(
        title=section.title, body=body, raw=result.text,
        infos=infos, placeholder=placeholder,
    )
