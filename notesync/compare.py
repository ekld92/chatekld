"""Is an Obsidian reference note still in step with its Zotero reading note?

Pure. The comparison is on CONTENT, never on mtime — see the package
docstring: the tagging campaigns rewrote every note's frontmatter, so mtime
reports zero stale notes when content reports four.

The question asked is deliberately narrow and one-directional: **is the Zotero
note's text still present in the Obsidian note?** Not "are they identical".
The user edits the Obsidian note after import — adds their own commentary,
reformats, links out — and every one of those edits would read as drift under
an equality test, drowning the real signal.

Since 2026-08-28 the module also answers the question an UPDATE needs, which
is not the same one. ``containment`` says how much of the Zotero note survives
in the Obsidian note; it cannot say whether rewriting the note would destroy
anything, because the user's own writing lives INSIDE the same region as the
imported text. So :func:`compare_note` additionally reports two word counts
over the note's Zotero region (:func:`split_note`):

``missing_words``
    Zotero words the region lacks — "there is something to import".
``extra_words``
    Region words the Zotero note lacks — "there is something to lose".

Measured on the live library (1,234 comparable notes, 2026-08-28): 1,186 have
neither, 32 have only the second (the user annotated; nothing to import), **1**
has only the first (safe to regenerate) and **15** have both. That last cell is
the whole reason a batch "refresh every note" verb does not exist — it would
be right 1 time and destructive 15 times.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

#: Fraction of the Zotero note's distinct words that must still appear in the
#: Obsidian note for it to count as in step. Calibrated against the live
#: library: at 0.85 it flags 3 of 1,231 notes, and reading all three confirms
#: each is a genuine divergence rather than an edit. Raising it to 0.9 used to
#: add a "tokenisation artifact" (a Zotero note whose "dépressionutile" is two
#: words in the exported note, 0.875) — that was not a tokenisation quirk but
#: ``audit.core.zotero._strip_html`` fusing the words on either side of a
#: markup break, fixed 2026-08-28; lowering it to 0.6 loses one real case.
#:
#: This threshold answers "has the note drifted?", NOT "can it be updated
#: safely?" — see ``missing_words``/``extra_words`` on :class:`NoteState`.
DEFAULT_CONTAINMENT = 0.85

#: Region markers (Phase 2 of the sync plan, 2026-09-04). HTML comments by
#: maintainer decision: hidden in Obsidian's reading view and live preview,
#: dropped by a pandoc export (the ``%% … %%`` form would print there). A note
#: carrying the pair is "marked"; :func:`split_note_parts` prefers the pair and
#: falls back to the historical single-``---``-rule shape, so migrated and
#: unmigrated notes coexist.
REGION_BEGIN = "<!-- zotero-note:begin -->"
REGION_END = "<!-- zotero-note:end -->"

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
#: Words for the update path: alphanumeric runs, so digits count and a token
#: like ``750mg`` survives whole. See :func:`risk_words` for why the two
#: tokenizers are separate rather than one widened regex.
_RISK_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
#: Markdown structure whose loss the user would notice: wikilink brackets,
#: bold/italic runs, code spans, ATX headings, blockquotes, list markers and
#: table pipes. Matched per line (``re.M``) so a leading marker is anchored and
#: a mid-sentence hyphen is not mistaken for a bullet.
#: Kept here (stdlib only) rather than imported from ``notesync.convert`` so
#: this module stays pure; the two regexes must agree.
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)

_STRUCT_RE = re.compile(
    r"\[\[|\]\]|\*\*|__|`+|^\s{0,3}#{1,6}\s|^\s*>|^\s*[-*+]\s|\|",
    re.MULTILINE,
)


@dataclass(frozen=True)
class NoteState:
    """One record's sync state.

    ``state`` is ``"ok"``, ``"missing"`` (a Zotero reading note exists and no
    Obsidian note does) or ``"drifted"``. ``containment`` is the measured
    fraction, carried so the report can rank the worst first and so a threshold
    change is explicable rather than mysterious.

    The three fields below were added for the update path (2026-08-28) and are
    ADDITIVE — ``state``'s vocabulary is unchanged, so every existing consumer
    reads exactly what it read before. They answer the question ``state``
    cannot: ``state`` is about whether the note diverged, these are about
    whether rewriting its Zotero region would import something (``missing_words``)
    and whether it would destroy something (``extra_words``). ``region_found``
    is False when the note's shape is not the template's single-rule one, in
    which case neither count is meaningful and no update may be offered.
    """

    citation_key: str
    state: str
    containment: float = 1.0
    zotero_chars: int = 0
    note_chars: int = 0
    note_rel: str | None = None
    missing_words: int = 0
    extra_words: int = 0
    region_found: bool = False
    #: The note carries the marker pair (Phase 2 layout). Additive, 2026-09-04.
    marked: bool = False

    @property
    def sync(self) -> str:
        """The UPDATE-oriented classification, derived from the fields above.

        ``missing`` · ``unparsed`` (shape not recognised) · ``update`` (import
        available, nothing of the user's at risk) · ``conflict`` (import
        available AND the user has written in the region) · ``annotated`` (the
        user has written, nothing to import) · ``ok``.
        """
        if self.state == "missing":
            return "missing"
        if not self.region_found:
            return "unparsed"
        if self.missing_words:
            return "conflict" if self.extra_words else "update"
        return "annotated" if self.extra_words else "ok"


_NBSP_RE = re.compile(r"&nbsp;|\u00a0")


def _fold(text: str) -> str:
    """NFD, drop combining marks. Shared by every tokenizer below so the
    accent-insensitivity the bilingual FR/EN vault needs is defined once.

    Non-breaking spaces — the U+00A0 character the plugin wrote into the
    vault AND the ``&nbsp;`` entity ``notesync.convert`` emits since
    2026-09-04 — fold to a plain space first, or the entity's letters count
    as the word "nbsp" on one side only and every nbsp-indented note reads as
    "there is something to import" (measured: 111 such rows).
    """
    text = _NBSP_RE.sub(" ", text)
    return "".join(c for c in unicodedata.normalize("NFD", text)
                   if not unicodedata.combining(c))


def fold_words(text: str) -> list[str]:
    """Accent- and case-folded word list. The vault is bilingual FR/EN, so a
    comparison that distinguishes ``é`` from ``e`` would flag re-encodings as
    drift; this mirrors ``audit/search.py::fold``'s recipe.

    **Digit-free, and deliberately left that way.** This feeds
    :func:`containment` ONLY, whose :data:`DEFAULT_CONTAINMENT` threshold is
    calibrated against the live library; widening its token set would silently
    re-calibrate drift detection for every note. The update path needs a
    different, stricter question and gets its own tokenizers below.
    """
    if not text:
        return []
    return [w.casefold() for w in _WORD_RE.findall(_fold(text))]


def risk_words(text: str) -> set[str]:
    r"""Words for the UPDATE path, **digits included**.

    ``fold_words``' ``[^\W\d_]+`` drops digits entirely and strips them from
    inside a token, so ``"dose 2019 p. 47 750mg"`` reduced to
    ``['dose', 'p', 'mg']``. Measured against the live library (2026-08-29),
    that made a year, a dose, a page number, a p-value or a sample size the
    user had corrected in the Zotero region **invisible** to
    ``extra_words`` — the count whose whole job is to answer "is there
    something of yours to lose?". A region whose only edits were numeric
    therefore classified as a safe update and was silently overwritten.
    """
    if not text:
        return set()
    return {w.casefold() for w in _RISK_WORD_RE.findall(_fold(text))}


def structure_markers(text: str) -> set[str]:
    """Markdown structure present in *text* — the second half of "something to lose".

    A word-set comparison cannot see that the user turned a mention into a
    ``[[wikilink]]``, bolded a phrase, or broke a paragraph into a list: every
    surviving word is still on both sides, so the loss is invisible. Measured
    over the live library, **555 of 1,234** reference notes carry markup a
    regeneration would flatten — dominated by ``-`` bullets (334) and ``**``
    bold (269), plus 6 wikilinks — and all 555 previously read as "in step".

    **Used for the LOSS side only, never for the import side.** Counting these
    in ``missing_words`` was measured and rejected: the proposed region and the
    HTML-stripped Zotero note carry different markup by construction, so it
    invented five phantom "there is something to import" rows out of pure
    formatting. Structure answers "would this update destroy something", which
    is exactly one direction.
    """
    if not text:
        return set()
    return {m.group(0).strip() for m in _STRUCT_RE.finditer(_NBSP_RE.sub(" ", text))}


def region_delta(current_region: str, reference: str) -> tuple[int, int]:
    """``(missing, extra)`` for one note's Zotero region against *reference*.

    *reference* is what the region would be replaced BY — the rendered proposed
    region on the write path, or the raw Zotero note text on the read-only
    listing. The two were measured to put every one of the 1,234 live notes in
    the same bucket, so the listing keeps the cheap basis (this module stays
    pure — no dependency on :mod:`notesync.render`) while the write gate in
    ``api/routes/notesync.py`` uses the exact one it already has in hand.

    ``missing`` — reference words the region lacks: "there is something to
    import". ``extra`` — region words *and markup* the reference lacks:
    "there is something of yours to lose". The asymmetry is deliberate and
    measured; see :func:`structure_markers`.
    """
    # ``<img data-attachment-key="…" width="…">`` nodes ride the Markdown
    # verbatim since 2026-09-04 (see ``notesync.convert``); their attributes
    # are not words. Left in, the 7 image-bearing live notes read as "there is
    # something to import" on every pass.
    current_region = _IMG_TAG_RE.sub(" ", current_region or "")
    reference = _IMG_TAG_RE.sub(" ", reference or "")
    missing = len(risk_words(reference) - risk_words(current_region))
    extra = (len(risk_words(current_region) - risk_words(reference))
             + len(structure_markers(current_region)
                   - structure_markers(reference)))
    return missing, extra


def _body_offset(raw: str) -> int:
    """Index at which the note body begins, i.e. just past the frontmatter.

    Extracted from :func:`note_body` so :func:`split_note` can slice the ORIGINAL
    string (it must rebuild the note byte-for-byte outside the region it
    replaces, which a body-only view cannot do).
    """
    if not raw.startswith("---"):
        return 0
    # Scan for the CLOSING fence as a whole line, not with `raw.find("\n---")`.
    # That substring also matched `\n----` and `\n---foo`, landing the offset
    # mid-line; and when it found nothing it returned 0, which made the OPENING
    # `---` of an unterminated frontmatter block look like the body's horizontal
    # rule. split_note would then treat the frontmatter itself as the Zotero
    # region — i.e. an update could overwrite the tagging subsystem's tags,
    # which preserving is this module's headline safety claim.
    #
    # An unterminated block returns 0 = "no frontmatter", exactly as before, so
    # a note with no frontmatter at all is unaffected; what changes is that the
    # opening fence can no longer be mistaken for a closing one, because the
    # scan starts after it.
    offset = 3
    while True:
        nl = raw.find("\n", offset)
        if nl == -1:
            return 0                      # unterminated frontmatter
        line_start = nl + 1
        line_end = raw.find("\n", line_start)
        line = raw[line_start:] if line_end == -1 else raw[line_start:line_end]
        if line.strip() == "---":
            # Past the fence AND its newline; a fence ending the file (no
            # trailing newline) yields len(raw), i.e. an empty body.
            return len(raw) if line_end == -1 else line_end + 1
        offset = line_start


def note_body(raw: str) -> str:
    """The note minus its YAML frontmatter.

    The frontmatter is the tagging subsystem's, not the template's, and it
    changes on every tag write — including it would make every tagged note
    look drifted.
    """
    return raw[_body_offset(raw):]


def _split_unmarked(raw: str) -> tuple[str, str] | None:
    """Split an UNMARKED reference note into ``(prefix, region)`` at its ``---`` rule.

    The template (:data:`notesync.render.DEFAULT_TEMPLATE`) puts the app-owned
    header block above a single horizontal rule and the Zotero reading note
    below it. That rule is therefore the region boundary, and it needs no
    sentinel: measured across all 1,235 notes in the live vault (2026-08-28)
    every single one has frontmatter, opens with the bold title, and contains
    **exactly one** ``---`` line in its body, with non-empty text after it.

    ``prefix + region == raw`` exactly, so a caller that replaces only *region*
    leaves every other byte of the note — frontmatter, header, the rule itself
    — untouched. That is the same invariant ``deckgen.augment.replace_section``
    relies on, and it is what lets a region-scoped edit ride a whole-note
    sha256 guard.

    Returns ``None`` when the shape is not recognised (no rule, or more than
    one — a user who added their own rule). An unrecognised note is never
    auto-updated: there is no way to tell which half is ours.
    """
    if raw is None:
        return None
    start = _body_offset(raw)
    if start == 0 and raw.startswith("---"):
        # Opens like frontmatter but has no closing fence. `_body_offset`
        # reports 0 for both "no frontmatter" and "unterminated frontmatter",
        # and only the first is safe to treat as body: in the second, the sole
        # `---` line is the OPENING fence, so the split below would hand the
        # frontmatter itself back as the Zotero region and an update would
        # overwrite the tagging subsystem's tags. A note with no frontmatter
        # whose body merely starts with a rule is refused by the same test —
        # correctly, since it has no header block above the rule and so is not
        # the template's shape either way.
        return None
    lines = raw[start:].split("\n")
    rules = [i for i, line in enumerate(lines) if line.strip() == "---"]
    if len(rules) != 1:
        return None
    cut = rules[0]
    head = "\n".join(lines[:cut + 1])
    if cut + 1 < len(lines):
        # The rule is followed by at least one more line, so the newline that
        # separated them belongs to the prefix.
        prefix = raw[:start] + head + "\n"
        region = "\n".join(lines[cut + 1:])
    else:
        # The rule IS the last line and `raw` does not end with a newline.
        # Appending one unconditionally (the original form) fabricated a byte:
        # `prefix + region` came back as `raw + "\n"`, so a region-scoped write
        # perturbed the file outside the region it promises not to touch — and
        # the note's sha256 guard would certify the altered bytes. No live note
        # hits this today (measured 2026-08-29: 0 of 1,234); it is closed
        # because the exactness invariant is what the whole update path rests on.
        prefix = raw[:start] + head
        region = ""
    return prefix, region


def _at_line_start(raw: str, idx: int) -> bool:
    return idx == 0 or raw[idx - 1] == "\n"


def _split_marked(raw: str) -> tuple[str, str, str] | None:
    """Split a MARKED note into ``(prefix, region, suffix)``.

    The BEGIN marker must sit on its own line after the frontmatter, the END
    marker on its own line after it, each exactly once. ``prefix`` runs through
    the BEGIN line's newline; ``suffix`` starts at the newline that precedes
    the END line (so a region never ends with the newline that separates it
    from the marker) and carries the END line plus anything the user wrote
    below it. ``prefix + region + suffix == raw`` exactly.

    An empty region is the one case where the END line follows the BEGIN line
    directly: the region is ``""`` and the suffix starts at the END marker.
    """
    if raw.count(REGION_BEGIN) != 1 or raw.count(REGION_END) != 1:
        return None
    body_at = _body_offset(raw)
    if body_at == 0 and raw.startswith("---"):
        return None                       # unterminated frontmatter (see below)
    b = raw.find(REGION_BEGIN)
    if b < body_at or not _at_line_start(raw, b):
        return None
    nl = raw.find("\n", b)
    if nl == -1 or raw[b + len(REGION_BEGIN):nl].strip("\r "):
        return None                       # junk after the marker on its line
    body_start = nl + 1
    e = raw.find(REGION_END, body_start)
    if e == -1 or not _at_line_start(raw, e):
        return None
    if raw[e + len(REGION_END):].split("\n", 1)[0].strip("\r "):
        return None                       # junk after the END marker
    if e == body_start:
        return raw[:body_start], "", raw[e:]
    if raw[e - 2:e] == "\r\n":
        s = e - 2
    else:
        s = e - 1                         # _at_line_start guarantees a "\n"
    if s < body_start:
        return None
    return raw[:body_start], raw[body_start:s], raw[s:]


def split_note_parts(raw: str | None) -> tuple[str, str, str] | None:
    """``(prefix, region, suffix)`` with ``prefix + region + suffix == raw``.

    The marker pair is tried first; a note without it falls back to the
    single-rule shape, whose suffix is always ``""``. ``None`` when neither
    shape is recognised — such a note is never written to. This is the split
    every WRITER must use: a marked note's suffix holds the END marker and the
    user's own writing below it, and a caller that rebuilt ``prefix + region``
    would delete both.
    """
    if raw is None:
        return None
    if REGION_BEGIN in raw or REGION_END in raw:
        return _split_marked(raw)
    sp = _split_unmarked(raw)
    return None if sp is None else (sp[0], sp[1], "")


def split_note(raw: str | None) -> tuple[str, str] | None:
    """``(prefix, region)`` — the READ-side split.

    For an unmarked note ``prefix + region == raw`` (the original invariant,
    still pinned by ``test_region.py``). For a marked note the suffix is
    dropped, so the identity does not hold; anything that writes must use
    :func:`split_note_parts` instead.
    """
    parts = split_note_parts(raw)
    return None if parts is None else (parts[0], parts[1])


def containment(zotero_text: str, note_text: str) -> float:
    """Fraction of the Zotero note's distinct words present in the note body.

    1.0 when the Zotero note is empty: there is nothing that could be missing,
    so an empty reading note can never make a note look drifted.
    """
    z_words = set(fold_words(zotero_text))
    if not z_words:
        return 1.0
    n_words = set(fold_words(note_text))
    return sum(1 for w in z_words if w in n_words) / len(z_words)


def compare_note(citation_key: str, zotero_text: str, note_raw: str | None,
                 *, note_rel: str | None = None,
                 threshold: float = DEFAULT_CONTAINMENT) -> NoteState:
    """Classify one record. ``note_raw`` is ``None`` when no note file exists.

    A record whose Zotero item carries NO reading note is ``ok`` whatever else
    is true: the maintainer's rule is that an Obsidian note exists exactly when
    a reading note does, and this function must not invent work by demanding
    notes for the other 5,000 records.
    """
    z_text = (zotero_text or "").strip()
    if not z_text:
        return NoteState(citation_key, "ok", 1.0, 0,
                         len(note_raw or ""), note_rel)
    if note_raw is None:
        return NoteState(citation_key, "missing", 0.0, len(z_text), 0, note_rel)

    # One rule for every length. A substring test for short notes was tried
    # first and MEASURED WORSE: it demands the Zotero text appear as a
    # contiguous run, so a two-word note whose words the user interleaved into
    # a sentence ("rapport envoyé par papa" → "rapport / billet envoyé par
    # papa") read as total drift at containment 1.0. Word containment tolerates
    # reformatting, which is the thing the user actually does, and still catches
    # the real case (a note whose Zotero text was never exported at all).
    body = note_body(note_raw)
    frac = containment(z_text, body)
    state = "ok" if frac >= threshold else "drifted"

    # The word counts are measured over the REGION, not the whole body: the
    # header block above the rule is generated from bib/Zotero fields, so its
    # title and journal words would otherwise count as the user's own writing
    # and mark every note in the library as carrying edits.
    split = split_note(note_raw)
    missing = extra = 0
    if split is not None:
        # region_delta, NOT fold_words: the listing's counts must use the same
        # digit- and markup-aware tokenizers the write gate does, or a row the
        # server will refuse without an explicit overwrite acknowledgement
        # would be offered here as a safe one-click update.
        missing, extra = region_delta(split[1], z_text)
    return NoteState(citation_key, state, round(frac, 3),
                     len(z_text), len(body), note_rel,
                     missing_words=missing, extra_words=extra,
                     region_found=split is not None,
                     marked=REGION_BEGIN in note_raw)
