"""Controlled tag vocabulary for the biblio tagging feature.

Parsed from the vault's ``_tags.md`` — the SAME hand-curated table the vault
thesaurus reads (``rag.thesaurus.parse_tags``), so there is one source of truth
for the tag list. Each row's ``Description`` column is the human gloss and the
``Tags`` column the controlled tag ids (``#dep, #mood`` → ``dep``, ``mood``).

``build_vocab`` returns ``{tag_id: gloss}`` — the legend an LLM tag proposer is
constrained to, and the source of the manual multi-select vocabulary. Pure and
read-only: it never touches the filesystem (the caller reads the file and passes
its text) and never writes.
"""

from __future__ import annotations

import logging
import re

from rag.thesaurus import parse_tags

logger = logging.getLogger(__name__)


def build_vocab(tags_text: str) -> dict[str, str]:
    """Map each tag id to a human-readable gloss from ``_tags.md`` text.

    A tag appearing in several rows accumulates each row's description (deduped,
    joined with `` / ``) so the legend shows the full sense — e.g. ``mood`` is
    glossed from every row that carries ``#mood``. Tag ids preserve their first
    appearance order in the file, which is stable for the UI. Leading ``#`` and
    surrounding whitespace are stripped; empty ids are dropped.

    ``include_empty_desc=True``: a row with tags but no Description still
    contributes its tag ids (gloss ``""``) — this map is the server-side
    security gate for every tag write, so a gloss-less tag must stay usable.

    Tag ids containing internal whitespace are SKIPPED (warned): every
    downstream consumer — the client's token parser, the frontmatter scalar
    merge — splits on whitespace, so a space-id offered here could never
    round-trip; enforcing the invariant at the gate keeps the modules agreeing.

    The parser (``rag.thesaurus.parse_tags``) already skips the header and
    ``|---|`` separator rows and handles the 3-column real file
    (``Description | Tags | Fichiers Associés``) by reading the first two cells.
    """
    vocab: dict[str, list[str]] = {}
    for row in parse_tags(tags_text or "", include_empty_desc=True):
        gloss = ", ".join(row.phrases).strip()
        for raw in row.tags:
            tag = raw.strip().lstrip("#").strip()
            if not tag:
                continue
            if re.search(r"\s", tag):
                logger.warning(
                    "_tags.md tag id %r contains whitespace — skipped (tag ids "
                    "must be single tokens)", tag)
                continue
            glosses = vocab.setdefault(tag, [])
            if gloss and gloss not in glosses:
                glosses.append(gloss)
    return {tag: " / ".join(glosses) for tag, glosses in vocab.items()}


def add_tag_to_vocab(tags_text: str, tag: str, gloss: str = "") -> tuple[str, str]:
    """Append a new ``#tag`` row to the ``_tags.md`` table. Returns ``(new, status)``.

    ``status`` ∈ ``added`` (a row was appended) | ``exists`` (a row already
    provides the tag id — new text is the input, unchanged) | ``invalid`` (the
    id is empty/whitespace-containing, or the file has no markdown table to
    append to — unchanged). The mirror of :func:`retire_tag_from_vocab`
    (drift-fix "add to vocabulary, case by case", 2026-08-06): a conservative,
    line-level edit — one new table row after the last existing row, column
    count copied from it, every other byte verbatim. The caller journals a
    snapshot before writing, so the edit is reversible.

    Duplicate detection uses :func:`build_vocab` ids compared NFC+lower — the
    tagging writers' own gate identity — not the audit fingerprint: a
    fingerprint-equivalent id under another surface would already have been
    resolved to by the drift report's ``vocab_ids`` mapping, so a request
    reaching here means the exact id family is genuinely absent.
    """
    from core.utils import nfc

    cleaned = str(tag or "").strip().lstrip("#").strip()
    if not cleaned or re.search(r"\s", cleaned):
        return tags_text, "invalid"
    existing = {nfc(t).lower() for t in build_vocab(tags_text)}
    if nfc(cleaned).lower() in existing:
        return tags_text, "exists"

    lines = (tags_text or "").split("\n")
    sep_re = re.compile(r"^\|[\s:\-|]+\|$")
    last_row = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("|") and not sep_re.match(stripped):
            last_row = i
    if last_row < 0:
        return tags_text, "invalid"
    insert_at = last_row + 1
    # A header-only table: the "last row" is the header line itself, and the
    # |---| separator sits right after it — the new row must go below both.
    if insert_at < len(lines) and sep_re.match(lines[insert_at].strip()):
        insert_at += 1

    # Column count copied from the last table row so a 2-col and the real
    # 3-col (Description | Tags | Fichiers Associés) file both stay aligned.
    n_cells = max(len(lines[last_row].split("|")) - 2, 2)
    gloss_clean = str(gloss or "").strip().replace("|", "/")
    gloss_cell = f" {gloss_clean} " if gloss_clean else " "
    cells = [gloss_cell, f" #{cleaned} "] + [" "] * (n_cells - 2)
    lines.insert(insert_at, "|" + "|".join(cells) + "|")
    return "\n".join(lines), "added"


def retire_tag_from_vocab(tags_text: str, tag: str) -> tuple[str, int]:
    """Remove ``#tag`` from every table row's Tags cell. Returns ``(new, rows)``.

    Vault-wide tag retirement (Batch G, 2026-07-22): a conservative,
    line-level edit of ``_tags.md`` — only the matched ``#tag`` token (and its
    separating comma) inside the second table cell is touched; every other
    byte of the file survives verbatim. A row whose Tags cell empties keeps
    its Description row (harmless: the parser yields no tag id from it).
    ``rows`` is the number of lines edited (0 = the tag was not in the file).
    The caller journals a snapshot before writing, so the edit is reversible.
    """
    cleaned = str(tag or "").strip().lstrip("#").strip()
    if not cleaned:
        return tags_text, 0
    token_re = re.compile(r"#" + re.escape(cleaned) + r"(?![\w/-])", re.IGNORECASE)
    out_lines: list[str] = []
    rows = 0
    for line in (tags_text or "").split("\n"):
        stripped = line.strip()
        # Only data rows of a markdown table (not the |---| separator).
        if stripped.startswith("|") and not re.match(r"^\|[\s:\-|]+\|$", stripped):
            cells = line.split("|")
            # cells[0] is the text before the first pipe (usually "");
            # cells[2] is the second content cell — the Tags column.
            if len(cells) >= 4 and token_re.search(cells[2]):
                new_cell = token_re.sub("", cells[2])
                # Tidy dangling separators left by the removal.
                new_cell = re.sub(r"\s*,\s*,", ", ", new_cell)
                new_cell = re.sub(r"^(\s*),\s*", r"\1", new_cell)
                new_cell = re.sub(r"\s*,(\s*)$", r"\1", new_cell)
                cells[2] = new_cell if new_cell.strip() else " "
                line = "|".join(cells)
                rows += 1
        out_lines.append(line)
    return "\n".join(out_lines), rows
