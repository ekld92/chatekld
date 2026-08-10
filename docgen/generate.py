"""Article generation orchestration — thin bindings over the deckgen engine.

The whole generation machinery (cancel-aware retry, the single-shot salvage
pass, per-section checkpoints, the robust JSON/heading outline parser) lives in
:mod:`deckgen.outline` / :mod:`deckgen.sections` and is document-class-agnostic
except for four hooks (prompts, sanitize, placeholder). This module binds those
hooks to the *article* implementations in :mod:`docgen.article` /
:mod:`docgen.prompts`, so an article reuses that engine unchanged.
"""
from __future__ import annotations

from deckgen.outline import request_outline as _deck_request_outline
from deckgen.sections import generate_section as _deck_generate_section

from .article import sanitize_article_section
from . import prompts


def _article_placeholder(section) -> str:
    """The prose placeholder for a section every attempt failed to generate."""
    return (
        f"\\section{{{section.title}}}\n"
        f"% (no content generated for this section)\n"
    )


def request_outline(client, **kwargs):
    """Article outline: :func:`deckgen.outline.request_outline` bound to the
    article outline prompt/message. Accepts the same kwargs as the deckgen call
    (topic, instructions, provider, model, embed, max_iters, temperature,
    max_sections, on_event, max_attempts, retry_backoff_s, should_cancel)."""
    return _deck_request_outline(
        client,
        system_prompt=prompts.OUTLINE_SYSTEM_PROMPT,
        build_message=prompts.build_outline_message,
        unit_label="report",
        **kwargs,
    )


def generate_section(client, **kwargs):
    """One article section: :func:`deckgen.sections.generate_section` bound to the
    article sanitize / prompts / placeholder. Same kwargs as the deckgen call."""
    return _deck_generate_section(
        client,
        sanitize=sanitize_article_section,
        system_prompt_builder=prompts.section_system_prompt,
        build_message=prompts.build_section_message,
        placeholder_builder=_article_placeholder,
        **kwargs,
    )
