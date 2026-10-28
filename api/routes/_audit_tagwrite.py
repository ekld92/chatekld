"""Conservative frontmatter ``tags:`` merge for the audit tag-drift writer.

This is the ONLY place the audit feature edits a vault note. It lives in the
app route layer (not the vendored ``audit/`` engine, which may depend only on
``audit/config.py``) and is deliberately *minimal*: it edits only the ``tags:``
field of a note's YAML frontmatter and preserves every other byte of the file.

When it cannot make a safe, unambiguous edit it returns ``(None, [], reason)``
so the caller refuses the write rather than reformatting the note — a vault
note is the user's data, and a full YAML re-dump would drop comments, key
order, and quoting style.

Supported ``tags`` shapes (the common Obsidian / Better-BibTeX ones):
  * no frontmatter at all           → prepend a fresh ``---`` block
  * no ``tags:`` key                 → insert a block list after ``---``
  * block list (``tags:\\n  - a``)   → append ``  - new`` items
  * flow list (``tags: [a, b]``)     → insert items inside the brackets
  * scalar   (``tags: a``)           → convert to a flow list

Anything else (a multi-line flow list, an unparseable block) is refused.
"""
from __future__ import annotations

import re

import yaml

# Opening frontmatter block: optional BOM/whitespace, ``---`` fence, YAML body,
# closing ``---`` fence. Mirrors audit/core/obsidian.py::YAML_RE.
# B10/#37 (2026-07-13): the closing group accepts a fence at EOF with no
# trailing newline (``(?:\n|$)``), mirroring audit/core/obsidian.py::YAML_RE so
# reader and writer agree — otherwise a frontmatter-only note without a final
# newline was treated as "no frontmatter" and a second ``---`` block prepended.
_FM_RE = re.compile(r"^(\s*(?:﻿)?---\s*\n)(.*?)(\n---\s*(?:\n|$))", re.DOTALL)
# EMPTY frontmatter (`---\n---`): _FM_RE needs a `\n---` after a non-empty
# body, so a zero-line block failed to match and the no-frontmatter branch
# prepended a SECOND `---` block (the same misclassification class B10/#37
# fixed for the missing-trailing-newline case).
_EMPTY_FM_RE = re.compile(r"^(\s*(?:﻿)?---\s*\n)(---\s*(?:\n|$))")
_TAGS_LINE_RE = re.compile(r"(?m)^tags:[ \t]*(.*)$")
# Obsidian also accepts the SINGULAR `tag:` key. We refuse rather than merge
# when it is the only tag key present: inserting a `tags:` block would leave
# the note with two parallel tag keys and silently ignore the existing tags.
_SINGULAR_TAG_LINE_RE = re.compile(r"(?m)^tag:[ \t]")


def _clean_tag(tag: str) -> str:
    """Normalise one tag: strip whitespace and a single leading ``#``."""
    t = str(tag or "").strip()
    if t.startswith("#"):
        t = t[1:].strip()
    return t


def _existing_tags(fm_body: str) -> set[str]:
    """Lower-cased set of tags already present in the frontmatter YAML body."""
    try:
        data = yaml.safe_load(fm_body)
    except Exception:
        return set()
    if not isinstance(data, dict):
        return set()
    val = data.get("tags")
    out: set[str] = set()
    if isinstance(val, str):
        # ``tags: a, b`` or ``tags: a b`` or ``tags: a`` — split on commas AND
        # whitespace (A3, 2026-07-13): Obsidian tags cannot contain spaces, so a
        # space-separated scalar is multiple tags, which is exactly how the scan
        # reader (audit/core/obsidian.py::_extract_tags) parses it. Splitting on
        # comma only here disagreed with the reader and let the writer fuse two
        # tags into one invalid ``[a b, new]`` value that the guard then passed.
        for part in re.split(r"[,\s]+", val):
            c = _clean_tag(part)
            if c:
                out.add(c.lower())
    elif isinstance(val, (list, tuple)):
        for item in val:
            c = _clean_tag(item)
            if c:
                out.add(c.lower())
    return out


# A tag safe to emit as a bare YAML plain scalar: word chars (Python's \w is
# Unicode-aware, so accented French tags pass), spaces, and ./- separators —
# and it must not START or END with a space (YAML trims plain scalars, so the
# round-trip would differ) or start with '-' (looks like a list item).
# Anything else — ':' (turns a block item into a MAP: `- to read: urgent`
# parses as {'to read': 'urgent'}, not a string), '#' (comment), quotes,
# brackets, ',', '&', '*', '!', '|', '>', '%', '@' — is single-quoted with
# '' doubling, the one YAML quoting style with no escape-sequence surprises.
_PLAIN_TAG_RE = re.compile(r"^[\w][\w ./\-]*[\w./\-]$|^[\w]$")


def _yaml_quote(tag: str) -> str:
    """Return *tag* as a YAML scalar that round-trips to exactly *tag*."""
    if _PLAIN_TAG_RE.fullmatch(tag):
        return tag
    return "'" + tag.replace("'", "''") + "'"


def _frontmatter_minus_tags(text: str) -> dict | None:
    """The frontmatter mapping WITHOUT its ``tags`` key, or ``None``.

    ``None`` = no frontmatter or an unparseable body (the caller skips the
    sibling-key comparison in that case — the tag-set guard still runs). The
    second half of the round-trip guard: the tag-set check alone proved
    nothing about neighbouring keys, so a splice bug that mangled a sibling
    while leaving ``tags`` correct would previously have passed.
    """
    fm = _FM_RE.match(text)
    if not fm:
        return None
    try:
        data = yaml.safe_load(fm.group(2))
    except Exception:
        return None
    if data is None:
        return {}
    if not isinstance(data, dict):
        return None
    data.pop("tags", None)
    return data


def _tags_after_merge(new_text: str) -> set[str] | None:
    """Parse the merged text's frontmatter and return its lowered tag set.

    ``None`` when the frontmatter is missing or unparseable — the caller treats
    that as a failed merge. This is the post-merge validation half of the
    round-trip guard in :func:`merge_tags_into_frontmatter`.
    """
    fm = _FM_RE.match(new_text)
    if not fm:
        return None
    try:
        data = yaml.safe_load(fm.group(2))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return _existing_tags(fm.group(2))


def merge_tags_into_frontmatter(text: str, add_tags: list[str]):
    """Return ``(new_text, added, reason)``.

    ``new_text`` is ``None`` (and ``reason`` set) when no safe edit is possible;
    otherwise ``added`` is the list of tags actually inserted (may be empty if
    all requested tags were already present, in which case ``new_text`` equals
    ``text``). Tags are matched case-insensitively so an existing ``Anxiety``
    is not duplicated by an incoming ``anxiety``.

    Safety net: every non-no-op merge is validated by a ROUND-TRIP guard —
    the produced frontmatter must re-parse as YAML and its tag set must equal
    ``before ∪ added`` exactly. Branch-level quoting (:func:`_yaml_quote`)
    handles the known dangerous characters; the round-trip guard is what
    catches the shapes nobody anticipated, so a bug here degrades to a refusal
    (HTTP 422 upstream), never to a silently corrupted note.
    """
    wanted = []
    seen_lower: set[str] = set()
    for raw in add_tags:
        c = _clean_tag(raw)
        if c and c.lower() not in seen_lower:
            wanted.append(c)
            seen_lower.add(c.lower())
    if not wanted:
        return None, [], "no valid tags to add"

    fm = _FM_RE.match(text)
    before = _existing_tags(fm.group(2)) if fm else set()

    new_text, added, reason = _merge_unvalidated(text, fm, wanted)
    if new_text is None:
        return None, [], reason
    if not added:
        return new_text, [], ""  # no-op (all tags present) — nothing to validate

    # --- Round-trip guard (see docstring). --------------------------------
    after = _tags_after_merge(new_text)
    expected = before | {t.lower() for t in added}
    if after is None or after != expected:
        return None, [], "post-merge YAML validation failed (unsupported frontmatter shape)"
    # Sibling-key half of the guard: every non-`tags` frontmatter key must
    # survive byte-equivalent-under-YAML. Skipped when the ORIGINAL had no
    # parseable frontmatter (nothing to preserve / already unparseable).
    before_map = _frontmatter_minus_tags(text)
    if before_map is not None and _frontmatter_minus_tags(new_text) != before_map:
        return None, [], "post-merge YAML validation failed (sibling frontmatter keys changed)"
    return new_text, added, ""


def _unquote_item(raw: str) -> str | None:
    """Best-effort value of one block-list item, or ``None`` when unsafe.

    The merge path only ever WRITES plain or single-quoted scalars
    (:func:`_yaml_quote`), but a user-authored note may carry double quotes.
    Plain → cleaned verbatim; single-quoted → ``''`` un-doubled; double-quoted
    without backslashes → stripped. A backslash inside double quotes means real
    YAML escape processing — refuse (return ``None``) rather than mis-compare.
    """
    v = raw.strip()
    if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        return v[1:-1].replace("''", "'")
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        if "\\" in v:
            return None
        return v[1:-1]
    return _clean_tag(v)


def remove_tags_from_frontmatter(text: str, remove_tags: list[str]):
    """Return ``(new_text, removed, reason)`` — the deletion twin of the merge.

    Removes the requested tags (case-insensitive) from the note's frontmatter
    ``tags:`` field, editing ONLY that field and preserving every other byte.
    ``new_text`` is ``None`` (and ``reason`` set) when no safe edit is
    possible; ``removed`` is the list of tags actually removed (empty +
    ``new_text == text`` when none were present — a no-op, not an error).
    Removing the last tag leaves ``tags: []`` rather than deleting the key, so
    the note's shape stays predictable and a later add has a target.

    Mirrors :func:`merge_tags_into_frontmatter`'s discipline: per-shape splice
    branches that refuse the same ambiguous shapes (quoted flow lists,
    multi-line flow, comment-bearing scalar, singular ``tag:`` key), then a
    ROUND-TRIP guard asserting the post-edit tag set equals ``before −
    removed`` exactly and that every sibling frontmatter key survived
    YAML-equivalent — a bug degrades to a refusal, never a corrupted note.
    """
    targets: set[str] = set()
    for raw in remove_tags:
        c = _clean_tag(raw)
        if c:
            targets.add(c.lower())
    if not targets:
        return None, [], "no valid tags to remove"

    fm = _FM_RE.match(text)
    if not fm:
        return text, [], ""  # no frontmatter → nothing to remove (no-op)
    body = fm.group(2)
    # Singular `tag:` key with no `tags:`: the tag may well be present under
    # the singular key, so a "not present" no-op would be a silent lie —
    # refuse with the same guidance as the add path (checked BEFORE the
    # presence test, which only reads the plural key).
    if not _TAGS_LINE_RE.search(body) and _SINGULAR_TAG_LINE_RE.search(body):
        return None, [], ("note uses the singular 'tag:' frontmatter key — "
                          "rename it to 'tags:' first")
    before = _existing_tags(body)
    hits = sorted(before & targets)
    if not hits:
        return text, [], ""  # none present — no-op

    new_text, removed, reason = _remove_unvalidated(text, fm, targets)
    if new_text is None:
        return None, [], reason

    after = _tags_after_merge(new_text)
    expected = before - targets
    if after is None or after != expected:
        return None, [], "post-removal YAML validation failed (unsupported frontmatter shape)"
    before_map = _frontmatter_minus_tags(text)
    if before_map is not None and _frontmatter_minus_tags(new_text) != before_map:
        return None, [], "post-removal YAML validation failed (sibling frontmatter keys changed)"
    return new_text, removed, ""


def _remove_unvalidated(text: str, fm, targets: set[str]):
    """The per-shape removal branches; output is validated by the caller."""
    open_fence, body, close_fence = fm.group(1), fm.group(2), fm.group(3)
    tags_match = _TAGS_LINE_RE.search(body)
    if not tags_match:
        if _SINGULAR_TAG_LINE_RE.search(body):
            return None, [], ("note uses the singular 'tag:' frontmatter key — "
                              "rename it to 'tags:' first")
        return text, [], ""  # no tags: key (tags may live in body text — out of scope)

    rest = tags_match.group(1).strip()
    head_comment = rest.startswith("#")
    if head_comment:
        rest = ""

    def _splice_line(new_line: str):
        new_body = body[:tags_match.start()] + new_line + body[tags_match.end():]
        return text[:fm.start()] + open_fence + new_body + close_fence + text[fm.end():]

    # --- Scalar (``tags: a b`` / ``tags: a, b``): rebuild as a flow list. --
    if rest and not rest.startswith("["):
        if "#" in rest:
            return None, [], "comment after a scalar tags value is not supported"
        if "'" in rest or '"' in rest:
            return None, [], "quoted scalar tags value is not supported"
        existing = [_clean_tag(p) for p in re.split(r"[,\s]+", rest) if _clean_tag(p)]
        kept = [t for t in existing if t.lower() not in targets]
        removed = [t for t in existing if t.lower() in targets]
        if not removed:
            return text, [], ""
        new_line = "tags: [" + ", ".join(_yaml_quote(t) for t in kept) + "]"
        return _splice_line(new_line), removed, ""

    # --- Flow list (``tags: [a, b]``): rebuild inside the brackets. --------
    if rest.startswith("["):
        if "]" not in rest:
            return None, [], "multi-line flow tags list not supported"
        close_idx = rest.index("]")
        inner = rest[1:close_idx].strip()
        if "'" in inner or '"' in inner:
            return None, [], "quoted flow tags list is not supported"
        existing = [_clean_tag(p) for p in inner.split(",") if _clean_tag(p)]
        kept = [t for t in existing if t.lower() not in targets]
        removed = [t for t in existing if t.lower() in targets]
        if not removed:
            return text, [], ""
        new_line = ("tags: [" + ", ".join(_yaml_quote(t) for t in kept) + "]"
                    + rest[close_idx + 1:])
        return _splice_line(new_line), removed, ""

    # --- Block list: drop the matching ``  - item`` lines. -----------------
    lines = body.split("\n")
    tag_line_idx = body[:tags_match.start()].count("\n")
    item_re = re.compile(r"^(\s+)-\s?(.*)$")
    kept_lines: list[str] = []
    removed: list[str] = []
    i = tag_line_idx + 1
    item_count = 0
    while i < len(lines):
        m = item_re.match(lines[i])
        if not m:
            break
        item_count += 1
        value = _unquote_item(m.group(2))
        if value is None:
            return None, [], "escaped double-quoted tag item is not supported"
        if value and value.lower() in targets:
            removed.append(value)
        else:
            kept_lines.append(lines[i])
        i += 1
    if not removed:
        return text, [], ""
    if not kept_lines:
        if head_comment:
            # Rewriting the head to `tags: []` would have to eat or move the
            # comment — refuse rather than guess.
            return None, [], "removing every tag under a commented tags: line is not supported"
        head = "tags: []"
        new_lines = lines[:tag_line_idx] + [head] + lines[i:]
    else:
        new_lines = lines[:tag_line_idx + 1] + kept_lines + lines[i:]
    new_body = "\n".join(new_lines)
    return (text[:fm.start()] + open_fence + new_body + close_fence + text[fm.end():],
            removed, "")


def _merge_unvalidated(text: str, fm, wanted: list[str]):
    """The per-shape merge branches; output is validated by the caller."""
    # --- Empty frontmatter (`---\n---`): insert between the fences. ------
    if not fm:
        empty = _EMPTY_FM_RE.match(text)
        if empty:
            insert = "tags:\n" + "".join(f"  - {_yaml_quote(t)}\n" for t in wanted)
            return (text[:empty.end(1)] + insert + text[empty.start(2):],
                    list(wanted), "")

    # --- No frontmatter: prepend a fresh block. --------------------------
    if not fm:
        block = "---\ntags:\n" + "".join(f"  - {_yaml_quote(t)}\n" for t in wanted) + "---\n\n"
        return block + text, list(wanted), ""

    open_fence, body, close_fence = fm.group(1), fm.group(2), fm.group(3)
    already = _existing_tags(body)
    to_add = [t for t in wanted if t.lower() not in already]
    if not to_add:
        return text, [], ""  # all present already — no-op, not an error

    tags_match = _TAGS_LINE_RE.search(body)

    # --- Singular `tag:` key only: refuse (see _SINGULAR_TAG_LINE_RE). ---
    if not tags_match and _SINGULAR_TAG_LINE_RE.search(body):
        return None, [], ("note uses the singular 'tag:' frontmatter key — "
                          "rename it to 'tags:' first")

    # --- No tags: key: insert a block list at the top of the body. -------
    # ``body`` is the YAML between the fences and carries no trailing newline
    # (the newline before the close fence is part of ``close_fence``), so the
    # inserted block must end with ``\n`` to keep the following key on its own
    # line.
    if not tags_match:
        insert = "tags:\n" + "".join(f"  - {_yaml_quote(t)}\n" for t in to_add)
        return (
            text[:fm.start()] + open_fence + insert + body + close_fence + text[fm.end():],
            list(to_add),
            "",
        )

    rest = tags_match.group(1).strip()

    # A comment directly on the ``tags:`` line: `tags:  # none` is an empty
    # block-list head (fall through, comment preserved on its line); a comment
    # AFTER a value (`tags: foo  # c`, `tags: [a]  # c` is handled below by
    # preserving the bracket tail) on the scalar shape would be mangled by the
    # flow-list conversion — refuse rather than eat the comment.
    if rest.startswith("#"):
        rest = ""

    # --- Scalar (``tags: foo``): convert to a flow list. -----------------
    if rest and not rest.startswith("["):
        if "#" in rest:
            return None, [], "comment after a scalar tags value is not supported"
        if "'" in rest or '"' in rest:
            return None, [], "quoted scalar tags value is not supported"
        # A3 (2026-07-13): split on commas AND whitespace so a space-separated
        # scalar (``tags: anxiety depression``) becomes ["anxiety","depression"],
        # matching the scan reader — not one fused ``anxiety depression`` tag.
        existing_scalar = [_clean_tag(p) for p in re.split(r"[,\s]+", rest) if _clean_tag(p)]
        merged = [_yaml_quote(t) for t in existing_scalar + to_add]
        new_line = "tags: [" + ", ".join(merged) + "]"
        new_body = body[:tags_match.start()] + new_line + body[tags_match.end():]
        return text[:fm.start()] + open_fence + new_body + close_fence + text[fm.end():], list(to_add), ""

    # --- Flow list (``tags: [a, b]``): insert inside the brackets. -------
    if rest.startswith("["):
        if "]" not in rest:
            return None, [], "multi-line flow tags list not supported"
        close_idx = rest.index("]")
        inner = rest[1:close_idx].strip()
        # Quoted existing items would need a real YAML-aware splice (a naive
        # `.index(']')` can land inside a quoted string) — refuse; the block
        # and plain-flow shapes cover the real vault.
        if "'" in inner or '"' in inner:
            return None, [], "quoted flow tags list is not supported"
        additions = ", ".join(_yaml_quote(t) for t in to_add)
        new_inner = (inner + ", " + additions) if inner else additions
        new_line = "tags: [" + new_inner + "]" + rest[close_idx + 1:]
        new_body = body[:tags_match.start()] + new_line + body[tags_match.end():]
        return text[:fm.start()] + open_fence + new_body + close_fence + text[fm.end():], list(to_add), ""

    # --- Block list (``tags:`` then ``  - item`` lines). -----------------
    # rest is empty (or a bare comment) → the items follow on subsequent lines.
    lines = body.split("\n")
    # Locate the tags: line index within the body.
    tag_line_idx = body[:tags_match.start()].count("\n")
    # Walk the item lines directly below to find their indentation and the
    # insertion point (after the last item). The walk stops at the FIRST
    # non-item line — blank or otherwise — because a blank line inside a YAML
    # block sequence is unusual enough that appending past it risks attaching
    # items to a different construct; with no items seen at all (``tags:``
    # null head), insertion happens right under the key with default indent.
    # DELIBERATE consequence: with an intervening blank line the new items
    # land BEFORE it (mid-list visually) — still valid YAML, and the
    # round-trip guard verifies the resulting tag set; surprising placement
    # is preferred over the mis-attachment risk.
    item_re = re.compile(r"^(\s+)-\s?(.*)$")
    insert_at = tag_line_idx + 1
    indent = "  "
    i = tag_line_idx + 1
    while i < len(lines):
        m = item_re.match(lines[i])
        if not m:
            break
        indent = m.group(1)
        insert_at = i + 1
        i += 1
    new_item_lines = [f"{indent}- {_yaml_quote(t)}" for t in to_add]
    new_lines = lines[:insert_at] + new_item_lines + lines[insert_at:]
    new_body = "\n".join(new_lines)
    return text[:fm.start()] + open_fence + new_body + close_fence + text[fm.end():], list(to_add), ""


# --- the `pdf:` wikilink list (2026-09-07) ----------------------------------
#
# A reference note names its Zotero item in frontmatter; this names its FILE.
# Written in ONE shape and only by the app, which is what makes it safely
# rewritable when the file is renamed: `maintenance/rename.py` refuses to
# rename a PDF referenced from any note, and without a machine-recognisable
# shape that refusal is the only safe answer. A hand-written embed anywhere
# else in the vault still refuses the rename — this narrows the refusal, it
# does not remove it.
#
# The value is a block sequence of double-quoted wikilinks:
#
#     pdf:
#       - "[[smith_2020.pdf]]"
#
# Quoted because `[[x]]` is YAML flow-sequence syntax: unquoted, it parses as
# a nested list and the round-trip guard below refuses the write rather than
# storing something Obsidian and PyYAML disagree about.

PDF_LINK_KEY = "pdf"

#: One wikilink item, anchored. The rewriter matches this and nothing else —
#: a hand-edited entry that no longer has the shape is left alone and makes
#: the rename refuse, which is the safe direction.
_PDF_LINK_ITEM_RE = re.compile(r'^(\s*-\s*)"\[\[([^\[\]"|]+)\]\]"\s*$')

_PDF_BLOCK_RE = re.compile(
    r"(?m)^pdf:[ \t]*\n(?:(?:[ \t]+-[ \t]*.*\n?)|(?:[ \t]*\n))*")
_PDF_INLINE_RE = re.compile(r"(?m)^pdf:[ \t]*\S.*$")


def _frontmatter_minus(text: str, key: str) -> dict | None:
    """The frontmatter mapping without *key*, or ``None`` when unusable."""
    fm = _FM_RE.match(text)
    if not fm:
        return None
    try:
        data = yaml.safe_load(fm.group(2))
    except Exception:
        return None
    if data is None:
        return {}
    if not isinstance(data, dict):
        return None
    data.pop(key, None)
    return data


def read_pdf_links(text: str) -> list[str]:
    """The entries of the note's ``pdf:`` list, in order, as written — a
    basename, or a vault-relative path for an ambiguous name.

    Only entries in the app's exact shape are returned. Anything else in the
    list is deliberately invisible here, so a caller cannot conclude "I can
    rewrite all of this" from a list it only partly understands.
    """
    fm = _FM_RE.match(text)
    if not fm:
        return []
    # Only the items UNDER `pdf:` (2026-09-18: scanning every frontmatter
    # line read a `related_notes:` list of the same shape as this one, so
    # 10 live notes reported a "stale" pdf list they never had).
    block = _PDF_BLOCK_RE.search(fm.group(2))
    if not block:
        return []
    out: list[str] = []
    for line in block.group(0).split("\n")[1:]:
        m = _PDF_LINK_ITEM_RE.match(line)
        if m:
            out.append(m.group(2))
    return out


def pdf_list_is_clean(text: str) -> bool:
    """True when every item under ``pdf:`` has the app's shape.

    The rename path's gate: a list carrying a hand-written entry is not one
    this app may rewrite, so the rename refuses instead.
    """
    fm = _FM_RE.match(text)
    if not fm:
        return True
    body = fm.group(2)
    if _PDF_INLINE_RE.search(body):
        return False          # `pdf: something` — a scalar or flow list
    block = _PDF_BLOCK_RE.search(body)
    if not block:
        return True
    for line in block.group(0).split("\n")[1:]:
        if not line.strip():
            continue
        if not _PDF_LINK_ITEM_RE.match(line):
            return False
    return True


def set_pdf_links(text: str, names: list[str]):
    """Write the note's ``pdf:`` list. ``(new_text, None)`` or ``(None, reason)``.

    Replaces an existing block, appends when absent, and REMOVES the key when
    *names* is empty. Guarded like :func:`merge_tags_into_frontmatter`: the
    result must parse, every sibling key must be byte-identical, and the
    parsed list must equal what was asked for — so a splice that mangled a
    neighbour, or produced YAML meaning something else, refuses rather than
    writes.

    Refuses a list this app did not write (``dirty_list``): rewriting a
    hand-edited entry is exactly the risk the shape exists to avoid.
    """
    for n in names:
        if not n or any(c in n for c in '[]"|\n\r'):
            return None, "invalid_name"
    fm = _FM_RE.match(text)
    if not fm:
        return None, "no_frontmatter"
    before = _frontmatter_minus(text, PDF_LINK_KEY)
    if before is None:
        return None, "frontmatter_unparseable"
    if not pdf_list_is_clean(text):
        return None, "dirty_list"
    body = fm.group(2)
    block = "".join(f'  - "[[{n}]]"\n' for n in names)
    replacement = f"{PDF_LINK_KEY}:\n{block}" if names else ""
    existing = _PDF_BLOCK_RE.search(body)
    if existing:
        new_body = body[:existing.start()] + replacement + body[existing.end():]
    elif _PDF_INLINE_RE.search(body):
        return None, "dirty_list"
    elif names:
        new_body = (body.rstrip("\n") + "\n" if body.strip() else "") + replacement
    else:
        return text, None                      # nothing to remove
    new_body = new_body.rstrip("\n")
    new_text = fm.group(1) + new_body + fm.group(3) + text[fm.end():]
    after = _frontmatter_minus(new_text, PDF_LINK_KEY)
    if after is None or after != before:
        return None, "roundtrip_failed"
    if read_pdf_links(new_text) != list(names):
        return None, "roundtrip_failed"
    return new_text, None


def pdf_link_points_at(entry: str, basename: str,
                       vault_rel: str | None = None) -> bool:
    """Whether one ``pdf:`` entry names the file *basename* at *vault_rel*.

    An entry is either a bare basename (``smith_2020.pdf``) or, when that
    basename is ambiguous in the vault, the file's vault-relative path
    (``Z_attachments/biblio_articles/smith_2020.pdf``, 2026-09-18). A bare
    entry matches by name; a path entry matches ONLY the exact path, so a
    note linking another file of the same name is never taken for a
    reference to this one. The renamer's classifier and its rewriter both
    ask this one question, so they cannot disagree.
    """
    if "/" not in entry:
        return entry == basename
    return vault_rel is not None and entry == vault_rel


def ambiguous_pdf_basenames(vault_root) -> set[str]:
    """Folded basenames of every ``.pdf`` that occurs more than once in the vault.

    A bare ``[[x.pdf]]`` resolves by name, so when two files share it Obsidian
    may open either. Hidden directories are skipped (Obsidian does not index
    them). Folded with NFC + casefold: over-counting only writes a path where
    a bare name would have done, never the reverse.
    """
    import os
    import unicodedata

    seen: dict[str, int] = {}
    for _root, dirs, files in os.walk(str(vault_root)):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.lower().endswith(".pdf"):
                key = unicodedata.normalize("NFC", name).casefold()
                seen[key] = seen.get(key, 0) + 1
    return {k for k, n in seen.items() if n > 1}


def expected_pdf_links(pdf_paths, vault_root, ambiguous: set[str]) -> list[str]:
    """The ``pdf:`` entries a record's mapped PDFs should produce, sorted.

    A unique basename is written bare; an ambiguous one (see
    :func:`ambiguous_pdf_basenames`) as its vault-relative path, so a name
    that becomes ambiguous later reads ``stale`` and is caught.
    """
    import os
    import unicodedata

    root = os.path.realpath(str(vault_root))
    out: set[str] = set()
    for p in pdf_paths or ():
        name = os.path.basename(str(p))
        if unicodedata.normalize("NFC", name).casefold() in ambiguous:
            rel = os.path.relpath(os.path.realpath(str(p)), root)
            if not rel.startswith(".."):
                out.add(rel.replace(os.sep, "/"))
                continue
        out.add(name)
    return sorted(out)


def rewrite_pdf_link(text: str, old_name: str, new_name: str,
                     old_rel: str | None = None):
    """Swap one file's entry inside the ``pdf:`` list.

    ``(new_text, changed, reason)``. Used by the rename path, which has
    already refused anything it cannot recognise, so a ``reason`` here means
    the note changed under it and the rename must refuse too. A bare entry
    stays bare; a path entry (see :func:`pdf_link_points_at`) keeps its
    directory and gains the new basename.
    """
    names = read_pdf_links(text)
    if not any(pdf_link_points_at(n, old_name, old_rel) for n in names):
        return text, False, None
    if not pdf_list_is_clean(text):
        return None, False, "dirty_list"

    def _swap(n: str) -> str:
        if not pdf_link_points_at(n, old_name, old_rel):
            return n
        return new_name if "/" not in n else n.rsplit("/", 1)[0] + "/" + new_name

    out, reason = set_pdf_links(text, [_swap(n) for n in names])
    if out is None:
        return None, False, reason
    return out, True, None


# --- `aliases:` (record merge, annual cleanup T8, 2026-09-17) -----------------
#
# When a duplicate Zotero record is merged away, its citation key survives as
# an Obsidian alias on the kept note so every `[[oldKey]]` link still resolves.
# Same discipline as the `pdf:` list above: edit ONE key, keep every sibling
# byte-identical, and refuse rather than reformat when the shape is not one
# of the four this understands (absent / block list / flow list / scalar).

ALIASES_KEY = "aliases"
_ALIASES_BLOCK_RE = re.compile(
    r"(?m)^aliases:[ \t]*\n(?:(?:[ \t]+-[ \t]*.*\n?)|(?:[ \t]*\n))*")
_ALIASES_INLINE_RE = re.compile(r"(?m)^aliases:[ \t]*(\S.*)$")


def read_aliases(text: str) -> list[str]:
    """The note's ``aliases`` as a list of strings (``[]`` when absent or unparseable)."""
    fm = _FM_RE.match(text)
    if not fm:
        return []
    try:
        data = yaml.safe_load(fm.group(2))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get(ALIASES_KEY)
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(x) for x in raw if x is not None]
    return []


def add_aliases(text: str, names: list[str]):
    """Add *names* to the note's ``aliases`` list. ``(new_text, added, None)``
    or ``(None, [], reason)``.

    Guarded like :func:`set_pdf_links`: the result must parse, every sibling
    key must be byte-identical, and the parsed list must equal the old list
    plus exactly the names added — a splice that mangled a neighbour or
    produced YAML meaning something else refuses rather than writes.
    """
    for n in names:
        if not n or any(c in n for c in '[]"|\n\r:#'):
            return None, [], "invalid_name"
    fm = _FM_RE.match(text)
    if not fm:
        return None, [], "no_frontmatter"
    before = _frontmatter_minus(text, ALIASES_KEY)
    if before is None:
        return None, [], "frontmatter_unparseable"
    current = read_aliases(text)
    added = [n for n in names if n not in current and n not in current[:0]]
    added = list(dict.fromkeys(added))
    if not added:
        return text, [], None
    body = fm.group(2)
    wanted = current + added
    block_re = _ALIASES_BLOCK_RE.search(body)
    inline = _ALIASES_INLINE_RE.search(body)
    if block_re:
        block = "".join(f"  - {n}\n" for n in wanted)
        new_body = body[:block_re.start()] + f"{ALIASES_KEY}:\n{block}" + body[block_re.end():]
    elif inline:
        flow = "[" + ", ".join(_yaml_quote(n) if _needs_quote(n) else n for n in wanted) + "]"
        new_body = body[:inline.start()] + f"{ALIASES_KEY}: {flow}" + body[inline.end():]
    else:
        block = "".join(f"  - {n}\n" for n in wanted)
        new_body = (body.rstrip("\n") + "\n" if body.strip() else "") + f"{ALIASES_KEY}:\n{block}"
    new_body = new_body.rstrip("\n")
    new_text = fm.group(1) + new_body + fm.group(3) + text[fm.end():]
    after = _frontmatter_minus(new_text, ALIASES_KEY)
    if after is None or after != before:
        return None, [], "roundtrip_failed"
    if read_aliases(new_text) != wanted:
        return None, [], "roundtrip_failed"
    return new_text, added, None


def _needs_quote(value: str) -> bool:
    return not re.fullmatch(r"[A-Za-z0-9_.\-]+", value or "")
