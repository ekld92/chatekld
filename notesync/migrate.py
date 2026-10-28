"""Reference-note layout migration: single ``---`` rule → marker pair + keys.

Phase 2 of the sync plan (2026-09-04). Pure: text in, text out, no I/O. The
route layer owns the vault walk, the op-lock, the git-clean gate and the audit
trail.

What :func:`migrate_note` does to one note, and nothing else::

    ---                                    ---
    tags: [a, b]                           tags: [a, b]
    ---                                    zotero-key: ABCD1234
    **Title**                              zotero-note-key: WXYZ5678
    ...                                    ---
    ---                          →         **Title**
    <zotero region>                        ...
                                           ---
                                           <!-- zotero-note:begin -->
                                           <zotero region>
                                           <!-- zotero-note:end -->

* The region is exactly what :func:`notesync.compare.split_note_parts`
  identifies today, byte for byte. Nothing inside it is touched.
* The two keys go at the END of the frontmatter block, as ``key: value`` lines
  in the note's own newline style. ``tags:`` is not re-serialised; the whole
  original frontmatter body is a byte-prefix of the new one, and a YAML
  round-trip guard refuses the edit if the parsed mapping changed in any
  other way. A Zotero key is eight upper-case alphanumerics and nothing else
  is accepted, so a key can never carry YAML syntax into the block.
* Everything is additive; :func:`unmigrate_note` is the exact inverse
  (``unmigrate_note(migrate_note(x)) == x`` is pinned for every note shape,
  CRLF included). That inverse — plus the vault's own git, by maintainer
  decision — is the undo, because a 1,235-note batch cannot ride the refactor
  journal's 200-op retention window.

Why markers and keys at all: a single ``---`` rule is a fragile boundary (one
more rule anywhere and the note drops out of sync silently), the markers let
the user write BELOW the region without ever conflicting, and the item and
note keys are what the Zotero web API addresses — the citation key is a
filename, not an identity.
"""

from __future__ import annotations

import re

import yaml  # type: ignore[import-untyped]  # Third-party package has no bundled stubs.

from notesync.compare import REGION_BEGIN, REGION_END, split_note_parts

# Mirrors ``api/routes/_audit_tagwrite.py::_FM_RE`` (and the audit reader's
# YAML_RE): optional BOM, ``---`` fence, body, closing fence, fence-at-EOF
# tolerated. Copied rather than imported so this module stays free of the
# route package.
_FM_RE = re.compile(r"^(\s*(?:﻿)?---[ \t]*\r?\n)(.*?)(\r?\n---[ \t]*(?:\r?\n|$))", re.DOTALL)

#: A Zotero item key. Anything else is refused before it can reach YAML.
ZOTERO_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")

FM_ITEM_KEY = "zotero-key"
FM_NOTE_KEY = "zotero-note-key"

_KEY_LINE_RE = re.compile(
    rf"^(?:{FM_ITEM_KEY}|{FM_NOTE_KEY}):[ \t]*[A-Z0-9]{{8}}[ \t]*$", re.M)

#: Lines to add to a Zotero Integration template so NEW imports arrive marked.
#: Shown to the user; the app never edits the plugin's template file.
TEMPLATE_HINT = (
    f"{REGION_BEGIN}\n\n"
    "{% if notes.length > 0 %}{% for note in notes %}{{note.note}}{% endfor %}{% endif %}\n\n"
    f"{REGION_END}"
)


def _newline(raw: str) -> str:
    return "\r\n" if "\r\n" in raw else "\n"


def _parse_fm(body: str):
    try:
        data = yaml.safe_load(body) if body.strip() else {}
    except Exception:
        return None
    if data is None:
        return {}
    return data if isinstance(data, dict) else None


def set_frontmatter_keys(text: str, keys: dict[str, str], *, strict_keys: bool = False) -> tuple[str | None, str | None]:
    """Append ``key: value`` lines to the frontmatter block. ``(text, None)`` or
    ``(None, reason)``.

    A key already present with the SAME value is left alone; with a different
    value the whole edit is refused (``key_conflict``) — a note pointing at
    another Zotero item is a fact to surface, not to overwrite.
    """
    for k, v in keys.items():
        if not ZOTERO_KEY_RE.match(v or ""):
            return None, "invalid_key"
    m = _FM_RE.match(text)
    if not m:
        return None, "no_frontmatter"
    if strict_keys:
        try:
            node = yaml.compose(m.group(2))
            seen = set()
            for key_node, value_node in (node.value if isinstance(node, yaml.MappingNode) else []):
                if key_node.value == '<<':
                    return None, 'ambiguous_frontmatter_keys'
                if key_node.value in keys:
                    if key_node.value in seen or value_node.tag == 'tag:yaml.org,2002:null':
                        return None, 'ambiguous_frontmatter_keys'
                    seen.add(key_node.value)
        except Exception:
            return None, 'frontmatter_unparseable'
    before = _parse_fm(m.group(2))
    if before is None:
        return None, "frontmatter_unparseable"
    to_add = {}
    for k, v in keys.items():
        cur = before.get(k)
        if cur is None:
            to_add[k] = v
        elif str(cur) != v:
            return None, "key_conflict"
    if not to_add:
        return text, None
    nl = _newline(text)
    lines = "".join(f"{nl}{k}: {v}" for k, v in to_add.items())
    new_body = m.group(2) + lines if m.group(2) else lines.lstrip("\r\n")
    new_text = m.group(1) + new_body + m.group(3) + text[m.end():]
    new_match = _FM_RE.match(new_text)
    after = _parse_fm(new_match.group(2)) if new_match else None
    if after is None:
        return None, "roundtrip_failed"
    expected = dict(before)
    expected.update(to_add)
    if after != expected or not new_body.startswith(m.group(2)):
        return None, "roundtrip_failed"
    return new_text, None


def remove_frontmatter_keys(text: str) -> tuple[str | None, str | None]:
    """Remove the two ``zotero-*`` key lines this module writes. The inverse of
    :func:`set_frontmatter_keys` for lines of exactly that shape; a hand-edited
    value that no longer matches ``[A-Z0-9]{8}`` is left in place (and the
    guard then refuses, since the parsed mapping would not match)."""
    m = _FM_RE.match(text)
    if not m:
        return None, "no_frontmatter"
    before = _parse_fm(m.group(2))
    if before is None:
        return None, "frontmatter_unparseable"
    nl = _newline(text)
    kept = [ln for ln in m.group(2).split(nl) if not _KEY_LINE_RE.match(ln)]
    new_body = nl.join(kept)
    new_text = m.group(1) + new_body + m.group(3) + text[m.end():]
    new_match = _FM_RE.match(new_text)
    after = _parse_fm(new_match.group(2)) if new_match else None
    expected = {k: v for k, v in before.items() if k not in (FM_ITEM_KEY, FM_NOTE_KEY)}
    if after is None or after != expected:
        return None, "roundtrip_failed"
    return new_text, None


def migrate_note(raw: str, *, item_key: str, note_key: str) -> tuple[str | None, str | None]:
    """``(new_text, None)`` or ``(None, reason)``.

    Reasons: ``already_migrated`` (idempotent — the caller counts it, never
    rewrites), ``unparsed`` (not the single-rule shape), plus those of
    :func:`set_frontmatter_keys`.
    """
    if raw is None:
        return None, "unparsed"
    if REGION_BEGIN in raw or REGION_END in raw:
        return None, "already_migrated"
    parts = split_note_parts(raw)
    if parts is None:
        return None, "unparsed"
    prefix, region, _suffix = parts
    nl = _newline(raw)
    body = f"{prefix}{REGION_BEGIN}{nl}{region}{nl}{REGION_END}{nl}"
    return set_frontmatter_keys(body, {FM_ITEM_KEY: item_key, FM_NOTE_KEY: note_key})


def unmigrate_note(raw: str) -> tuple[str | None, str | None]:
    """The exact inverse of :func:`migrate_note` for a note it produced.

    A note whose suffix carries text the user wrote BELOW the END marker is
    still un-migrated — that text is appended after the region, since the
    single-rule shape has no other place for it — but then the pair is no
    longer an identity, which the caller should know: ``tail_kept`` is
    reported through the reason channel as ``None`` vs ``"tail_kept"``.
    Returns ``(new_text, None | "tail_kept")`` on success.
    """
    if raw is None or REGION_BEGIN not in raw:
        return None, "not_migrated"
    parts = split_note_parts(raw)
    if parts is None:
        return None, "unparsed"
    prefix, region, suffix = parts
    nl = _newline(raw)
    marker_line = f"{REGION_BEGIN}{nl}"
    if not prefix.endswith(marker_line):
        return None, "unparsed"
    core = prefix[:-len(marker_line)]
    # suffix = [nl] + END + nl + tail
    tail_at = suffix.find(REGION_END) + len(REGION_END)
    tail = suffix[tail_at:]
    if tail.startswith(nl):
        tail = tail[len(nl):]
    elif tail.startswith("\n"):
        tail = tail[1:]
    note = f"{core}{region}{tail}"
    new_text, reason = remove_frontmatter_keys(note)
    if new_text is None:
        return None, reason
    return new_text, ("tail_kept" if tail.strip() else None)
