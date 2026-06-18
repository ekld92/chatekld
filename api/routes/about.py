import html
import re
from flask import Blueprint, jsonify
from ..security import origin_is_local

about_bp = Blueprint('about', __name__)

def _inline_fmt(text: str) -> str:
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
    def __init__(self):
        self.out = []
        self.in_code_block = False
        self.in_ul = False

    def parse(self, md: str) -> str:
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
        if self.in_ul and not (stripped.startswith("- ") or stripped.startswith("* ")):
            self.out.append("</ul>")
            self.in_ul = False

@about_bp.route("/api/about")
def api_about():
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    
    # Example about text
    about_md = """
# ChatEKLD 2026
Local RAG application for PDF summarization and Obsidian vault interaction.
    """
    parser = MarkdownParser()
    return jsonify({"html": parser.parse(about_md)})
