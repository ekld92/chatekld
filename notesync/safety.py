"""Conservative note-content admission. Pure; never removes unsupported data."""
import re
from html.parser import HTMLParser

from notesync.convert import html_to_md, md_to_html
from notesync.state import norm_text


class _Content(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = False
        self.unsupported = False

    def handle_starttag(self, tag, attrs):
        if tag in {'img', 'picture', 'svg', 'canvas', 'object'}:
            self.images = True
        if tag not in {'div', 'p', 'br', 'strong', 'b', 'em', 'i', 's', 'del',
                       'a', 'ul', 'ol', 'li', 'blockquote', 'pre', 'code', 'hr',
                       'table', 'thead', 'tbody', 'tr', 'th', 'td',
                       'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
            self.unsupported = True
        allowed_attrs = {'div': {'class', 'data-schema-version'}, 'a': {'href', 'rel'}, 'ol': {'start'}}.get(tag, set())
        if any(k not in allowed_attrs for k, _ in attrs):
            self.unsupported = True
        if tag == 'a' and any(k == 'rel' and v != 'noopener noreferrer nofollow' for k, v in attrs):
            self.unsupported = True

    def handle_comment(self, data):
        if data.strip():
            self.unsupported = True

    handle_startendtag = handle_starttag


def content_issues(html='', markdown=''):
    parser = _Content()
    parser.feed(html + md_to_html(markdown))
    issues = []
    if parser.images or re.search(r'!\[|!\[\[', markdown):
        issues.append('Images require an explicit content decision; no images are written to Zotero.')
    if parser.unsupported:
        issues.append('Unsupported note content requires review before conversion.')
    if '[[' in markdown or ']]' in markdown:
        issues.append('Resolve Obsidian wikilinks to explicit portable links before synchronization.')
    return issues


def representable(markdown):
    return not content_issues(markdown=markdown) and norm_text(html_to_md(md_to_html(markdown))) == norm_text(markdown)
