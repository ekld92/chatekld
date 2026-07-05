"""About-window endpoint and its tiny safe Markdown renderer.

``GET /api/about`` returns a small HTML blob describing the app. Rather than
ship the whole vendored ``marked`` bundle to this rarely-seen window, a minimal
in-Python Markdown subset is rendered here. The renderer is XSS-safe by
construction: all text is ``html.escape``-d before formatting, and links are
allowed only for ``http(s)`` / in-page ``#`` targets (any other URL degrades to
plain text), so the ``about_md`` source — though currently a hard-coded
constant — cannot inject script even if it later became dynamic.
"""
import html
import re
from flask import Blueprint, jsonify

about_bp = Blueprint('about', __name__)

def _inline_fmt(text: str) -> str:
    """Apply inline Markdown (code/bold/italic/image-alt/link) to ESCAPED text.

    Caller responsibility: *text* must already be ``html.escape``-d — this only
    wraps spans in tags, it does not escape. Images collapse to their alt text;
    links are emitted only for ``http://`` / ``https://`` / ``#`` URLs (anything
    else, e.g. ``javascript:``, falls back to the bare link text via
    :func:`_safe_link`), which is what keeps the output injection-safe.
    """
    text = re.sub(r'`([^`]+)`', r'<code style="background:#2e2e32;color:#e8e8ed;padding:1px 4px;border-radius:3px;font-size:12px;">\1</code>', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    text = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'[\1]', text)
    def _safe_link(m):
        link_text = m.group(1)
        url = m.group(2)
        if url.startswith(('http://', 'https://', '#')):
            return f'<a href="{url}" style="color:var(--accent);">{link_text}</a>'
        return link_text
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', _safe_link, text)
    return text

class MarkdownParser:
    """A single-pass, line-oriented renderer for a Markdown subset.

    Supports fenced code blocks, ATX headings (``#``..``######``), unordered
    lists (``- `` / ``* ``), paragraphs, and the inline spans handled by
    :func:`_inline_fmt`. State (``in_code_block`` / ``in_ul``) is carried across
    lines so blocks open and close correctly. A parser instance accumulates into
    ``self.out`` and is single-use — construct a fresh one per :meth:`parse`.
    Every non-code line is ``html.escape``-d before inline formatting, so the
    rendered HTML is safe to inject into the page.
    """
    def __init__(self):
        self.out = []
        self.in_code_block = False
        self.in_ul = False

    def parse(self, md: str) -> str:
        """Render *md* to an HTML string, walking it one line at a time."""
        lines = md.split("\n")
        for line in lines:
            if self._handle_code_block(line):
                continue
            if self.in_code_block:
                self.out.append(html.escape(line))
                continue
            stripped = line.strip()
            self._close_open_blocks(stripped)
            if not stripped:
                continue
            if stripped.startswith("#"):
                level = min(len(re.match(r'#+', stripped).group()), 6)
                text = _inline_fmt(html.escape(stripped.lstrip("#").strip()))
                self.out.append(f"<h{level}>{text}</h{level}>")
                continue
            if stripped.startswith("- ") or stripped.startswith("* "):
                if not self.in_ul:
                    self.out.append("<ul>")
                    self.in_ul = True
                text = _inline_fmt(html.escape(stripped[2:]))
                self.out.append(f"<li>{text}</li>")
                continue
            text = _inline_fmt(html.escape(stripped))
            self.out.append(f"<p>{text}</p>")
        self._close_open_blocks("")
        return "\n".join(self.out)

    def _handle_code_block(self, line: str) -> bool:
        """Toggle fenced-code state on a ``` ``` ``` line; return True if handled.

        Opening emits ``<pre><code>``, closing emits ``</code></pre>``. Returns
        False for non-fence lines so :meth:`parse` continues processing them.
        """
        if line.strip().startswith("```"):
            if self.in_code_block:
                self.out.append("</code></pre>")
                self.in_code_block = False
            else:
                self.in_code_block = True
                self.out.append("<pre><code>")
            return True
        return False

    def _close_open_blocks(self, stripped: str):
        """Close an open ``<ul>`` when the next line is no longer a list item.

        Called before each content line (and once at EOF with ``""``) so a list
        is terminated as soon as a non-list line appears.
        """
        if self.in_ul and not (stripped.startswith("- ") or stripped.startswith("* ")):
            self.out.append("</ul>")
            self.in_ul = False

@about_bp.route("/api/about")
def api_about():
    """Return the About-window content as pre-rendered HTML.

    Renders a fixed Markdown blob through :class:`MarkdownParser` (a fresh
    instance, since the parser is single-use) into ``{"html": ...}``.
    Local-origin gated.
    """
    
    # Example about text
    about_md = """
# ChatEKLD 2026
Local RAG application for PDF summarization and Obsidian vault interaction.
    """
    parser = MarkdownParser()
    return jsonify({"html": parser.parse(about_md)})
