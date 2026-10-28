"""Markdown slides, using the shared resumable generation engine."""
from deckgen.outline import request_outline as outline
from deckgen.sections import generate_section as section
from docgen import markdown as md


def request_outline(client, **kwargs):
    return outline(client, system_prompt=md.OUTLINE_SYSTEM_PROMPT.replace(
        'long-form Markdown note', 'Markdown slide deck'),
        build_message=build_outline_message, unit_label='slide', **kwargs)


def build_outline_message(topic, instructions, max_sections):
    return md.build_outline_message(
        topic, instructions, max_sections, outline_kind="slide deck", unit="slides")


def build_section_message(**kwargs):
    return md.build_section_message(**kwargs, content_description="concise slide content")


def generate_section(client, **kwargs):
    def prompt(audience, **_):
        return (
            f'Write ONE Markdown slide for {audience}. Start with a ## heading. '
            'Use a concise claim and at most five short bullets. Cite vault sources '
            'inline. Ground all claims in the vault; never invent sources. '
            'Return only the slide, without frontmatter, fences or slide separators.'
        )
    return section(client, sanitize=md.sanitize_markdown_section,
                   system_prompt_builder=prompt, build_message=build_section_message,
                   placeholder_builder=md._md_placeholder, **kwargs)


def assemble_markdown(title, sections, *, frontmatter=None):
    header = md.assemble_markdown(title, [], frontmatter=frontmatter)
    header = header.replace('_(no content generated)_\n', '').rstrip()
    bodies = [s.body.strip() for s in sections if s.body.strip()]
    return '\n\n---\n\n'.join([header, *bodies]) + '\n'
