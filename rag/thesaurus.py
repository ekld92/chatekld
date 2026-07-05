"""Vault thesaurus — parse the hand-curated ``_abreviations.md`` / ``_tags.md``
tables into a bidirectional concept→synonym map and a compact glossary primer.

App-independent (stdlib only, like ``rag/lancedb_store.py``): it imports nothing
from the project so the loader in ``rag/vault.py`` and the unit tests can build a
``Thesaurus`` straight from file text without standing up the app.

Two consumers, both opt-in and query-time (no reindex):

  * **Query expansion** (``expand_query``) — feeds the deterministic
    ``_ThesaurusExpansionRetriever`` in ``rag/engine.py``. The vault's prose is
    dense bilingual FR/EN shorthand (``EDC``/``rsq``/``àà``) that the embedding
    model cannot bridge and that the lexical BM25 leg can only hit when the
    query literally carries the token. Expansion reformulates the query by
    substituting a matched concept term with its known synonyms, one variant per
    synonym, so each variant is retrieved *separately* (no dense-embedding
    dilution) and the results are rank-fused.
  * **System-prompt primer** (``build_primer``) — a small app-controlled
    glossary block so the answering LLM can *read* the shorthand that survives
    into the retrieved context.

Safety rules baked into the parser (so neither consumer is naive):
  * Rows the file itself flags as collisions (meaning/notes contains ``≠`` —
    ``CI``/``CI°``, ``LP``/``PL``, ``MA``, ``TDP``, ``SC``) are excluded from
    auto-expansion.
  * Morphological-rule rows (``...q`` → ``-ique``) are excluded from expansion.
  * Matching is whole-token / whole-phrase, accent- and case-insensitive, with a
    2-character floor, so ``SC`` never fires inside a word and bare symbols never
    expand.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional, Sequence

# --- normalisation -----------------------------------------------------------


def _norm(s: str) -> str:
    """Casefold + strip combining marks so ``dépression`` == ``depression``.

    Used only for *matching*; emitted variants/glossary text keep their
    original casing and accents (read verbatim from the user's file).
    """
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.casefold().strip()


# Word-token regex shared by key-building and query matching.  Using the SAME
# tokenizer on both sides is load-bearing: it is what makes matching symmetric
# for punctuated terms.  ``\w+`` splits on hyphens/apostrophes, so a key must be
# built from the same split or a query token can never reproduce it (the old
# bug: a raw-normalised key ``cognitivo-comportementales`` was unmatchable
# because the query tokeniser yields ``cognitivo`` + ``comportementales``).
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _term_key(s: str) -> str:
    """Normalise *s* into a space-joined token key for matching.

    Tokenises with ``_WORD_RE`` (dropping punctuation) and normalises each token
    via :func:`_norm`, so ``Cognitivo-Comportementales`` → ``cognitivo
    comportementales`` — exactly the form a windowed scan of query tokens
    produces.  Returns ``""`` for punctuation/symbol-only input (those rows are
    filtered out by the ≥2-char floor elsewhere).
    """
    return " ".join(_norm(t) for t in _WORD_RE.findall(s))


def _query_terms(query: str, max_len: int) -> set[str]:
    """Return every normalised 1..*max_len*-gram present in *query*.

    The membership set used for **whole-token** matching (surfaces against the
    query).  Built with the same tokeniser/normaliser as :func:`_term_key`, so a
    surface matches only on token boundaries — never as a substring inside a word
    (the old ``"iv" in "survival"`` class of false positive).
    """
    toks = [_norm(t) for t in _WORD_RE.findall(query or "")]
    terms: set[str] = set()
    upper = max(1, min(max_len, len(toks)))
    for L in range(1, upper + 1):
        for i in range(0, len(toks) - L + 1):
            terms.add(" ".join(toks[i:i + L]))
    return terms


# --- table parsing -----------------------------------------------------------


def _table_rows(text: str) -> list[list[str]]:
    """Yield the data cells of every Markdown table row in *text*.

    Skips the ``|---|`` separator rows; header rows are dropped by the
    callers (they know their own header labels).
    """
    rows: list[list[str]] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        # A separator row is all dashes/colons/spaces.
        if cells and all(c and set(c) <= set("-: ") for c in cells):
            continue
        rows.append(cells)
    return rows


def _surfaces_from_abbr_cell(cell: str) -> list[str]:
    """Surface forms from an ``Abréviation`` cell.

    Backtick-quoted tokens win (handles ``` `bipo` / `bpR` ``` and the
    ``(var. `RISPRDNE`)`` aside); a backtick-less cell falls back to a
    ``/`` split.
    """
    backticked = [b.strip() for b in re.findall(r"`([^`]+)`", cell) if b.strip()]
    if backticked:
        return backticked
    return [p.strip(" `") for p in cell.split("/") if p.strip(" `")]


def _meanings_from_signification(cell: str) -> list[str]:
    """Concept phrases from a ``Signification`` cell.

    Parenthetical glosses are kept as *extra* phrases (a bilingual synonym
    such as ``Borderline Personality Disorder (Trouble de la Personnalité
    Limite)`` yields both), except ``var.``/``≠`` asides. The main text is
    then split on ``/``.
    """
    parens = re.findall(r"\(([^)]*)\)", cell)
    extra = [p for p in parens if "≠" not in p and "var." not in p.lower()]
    main = re.sub(r"\([^)]*\)", "", cell)
    phrases: list[str] = []
    for chunk in main.split("/"):
        c = chunk.strip(" `").strip()
        if c:
            phrases.append(c)
    for p in extra:
        for chunk in re.split(r"[/,]", p):
            c = chunk.strip(" `").strip()
            if c:
                phrases.append(c)
    return phrases


@dataclass
class AbbrevRow:
    surfaces: list[str]
    meanings: list[str]
    note: str = ""
    ambiguous: bool = False
    morphological: bool = False
    raw_meaning: str = ""

    @property
    def expansion_eligible(self) -> bool:
        """Whether this row may seed query-expansion synonym groups.

        Conservative: never expand a flagged collision or a morphological
        rule, and require some signal that the abbreviation denotes a
        concept (a linked note, a multi-word meaning, or an acronym-shaped
        surface) so prose-shorthand rows (``svt`` → Souvent) stay out of
        retrieval while remaining available to the primer.
        """
        if self.ambiguous or self.morphological:
            return False
        multiword = any(len(m.split()) > 1 for m in self.meanings)
        acronymish = any(s.isupper() and len(s) >= 2 for s in self.surfaces)
        return bool(self.note) or multiword or acronymish


def parse_abbreviations(text: str) -> list[AbbrevRow]:
    """Parse ``_abreviations.md`` into :class:`AbbrevRow` records."""
    out: list[AbbrevRow] = []
    for cells in _table_rows(text):
        if len(cells) < 2:
            continue
        abbr_cell, signif = cells[0], cells[1]
        note = cells[2] if len(cells) > 2 else ""
        # Header row (French or English column labels; _norm strips accents).
        if _norm(abbr_cell).startswith(("abreviation", "abbreviation")) or _norm(
            signif
        ).startswith(("signification", "meaning")):
            continue
        surfaces = _surfaces_from_abbr_cell(abbr_cell)
        if not surfaces:
            continue
        # Variant spellings the user keeps in the meaning cell — e.g.
        # ``Rispéridone (var. `RISPRDNE`)`` — are real alternate surface forms
        # that appear in the notes, so fold any backticked token from the
        # signification into the surface set (deduped, order-preserving).
        for extra in re.findall(r"`([^`]+)`", signif):
            extra = extra.strip()
            if extra and extra not in surfaces:
                surfaces.append(extra)
        morphological = any(("..." in s or "…" in s) for s in surfaces)
        ambiguous = "≠" in signif or "≠" in note
        out.append(AbbrevRow(
            surfaces=surfaces,
            meanings=_meanings_from_signification(signif),
            note=note.strip(),
            ambiguous=ambiguous,
            morphological=morphological,
            raw_meaning=re.sub(r"\s+", " ", signif).strip(),
        ))
    return out


@dataclass
class TagRow:
    phrases: list[str]
    tags: list[str] = field(default_factory=list)


def parse_tags(text: str) -> list[TagRow]:
    """Parse ``_tags.md`` — the comma-separated Description column is the
    bilingual concept-phrase source for synonym grouping."""
    out: list[TagRow] = []
    for cells in _table_rows(text):
        if len(cells) < 2:
            continue
        desc, tags_cell = cells[0], cells[1]
        if _norm(desc).startswith("description") or _norm(tags_cell) in ("tags", "tag"):
            continue
        phrases = [p.strip() for p in re.split(r"[,/]", desc) if p.strip()]
        if not phrases:
            continue
        tags = [t.strip().lstrip("#") for t in tags_cell.split(",") if t.strip()]
        out.append(TagRow(phrases=phrases, tags=tags))
    return out


# --- primer selection --------------------------------------------------------


def _clean_meaning_for_display(s: str) -> str:
    """Tidy a signification cell for the glossary primer.

    Drops the disambiguation/variant asides (``(≠ …)``, ``(var. …)``) — which
    are table bookkeeping, not meaning — but KEEPS bilingual parenthetical
    glosses (e.g. ``Borderline Personality Disorder (Trouble de la Personnalité
    Limite)``), and removes literal backticks. Collapses whitespace.
    """
    s = re.sub(r"\([^)]*(?:≠|var\.)[^)]*\)", "", s or "")
    s = s.replace("`", "")
    return re.sub(r"\s+", " ", s).strip()


_PRIMER_HEADER = (
    "This knowledge base is a bilingual French/English psychiatry and "
    "clinical-research corpus. Notes are terse and use heavy shorthand and "
    "abbreviations. Reference glossary (abbreviation = meaning) for "
    "interpreting the retrieved notes:"
)

# App-curated core glossary, ordered by usefulness for *reading* the notes:
# prose shorthand first (it is what an LLM most often misreads), then the
# high-frequency clinical / drug-class / methodology acronyms. Compared against
# each row's surfaces by normalised form, so accents/case do not matter.
_CORE_PRIMER_TERMS: tuple[str, ...] = (
    # prose shorthand
    "rsq", "svt", "ds", "qd", "pdt", "pt", "clnq", "ttt", "ATCD", "PEC",
    "FdR", "poso", "càd", "NPO", "DDx", "CàT", "EI", "fqts", "efce", "SJ", "SA",
    "àà", "≈", "≠", "Ø", "±",
    # core clinical
    "EDC", "dep", "scz", "bipo", "tdah", "toc", "tspt", "TS", "IDS", "PEP",
    # drug classes / agents
    "isrs", "irsna", "imao", "atd3c", "apa", "sga", "fga", "bzd", "Li", "MPH",
    # methodology
    "SR", "MA", "RCT", "EBM", "TTE", "OR", "HR", "IC",
)


@dataclass
class Thesaurus:
    """Parsed thesaurus: a normalised concept→synonyms lookup plus the raw
    abbreviation rows used to render the primer."""

    abbrev_rows: list[AbbrevRow]
    tag_rows: list[TagRow]
    lookup: dict[str, set[str]] = field(default_factory=dict)
    _max_key_len: int = 1

    @classmethod
    def from_files(cls, abbrev_text: str = "", tags_text: str = "") -> "Thesaurus":
        abbrev_rows = parse_abbreviations(abbrev_text)
        tag_rows = parse_tags(tags_text)
        t = cls(abbrev_rows=abbrev_rows, tag_rows=tag_rows)
        t._build_lookup()
        return t

    def _build_lookup(self) -> None:
        """Invert synonym groups into ``normalised term -> {all surfaces}``.

        Each eligible abbreviation row (surfaces + meanings) and each tag row
        (description phrases) is a group of mutually-substitutable surfaces.
        A term shared by two groups merges their surfaces (token-level union),
        so the abbreviation and tag views of the same concept unify.

        Caveat (accepted): merging is per-shared-token, so if two genuinely
        distinct concepts shared one surface token their groups would fuse. In
        practice the surfaces are concept-specific multi-word phrases / acronyms
        and the single-tag-slug rule below already blocks the main false-merge
        (``sleep``↔``bzd``), so this stays acceptable; revisit if a future term
        is added that collides on a common token.
        """
        groups: list[set[str]] = []
        for row in self.abbrev_rows:
            if not row.expansion_eligible:
                continue
            group = {s for s in (row.surfaces + row.meanings) if len(_norm(s)) >= 2}
            if len(group) >= 2:
                groups.append(group)
        for trow in self.tag_rows:
            group = {p for p in trow.phrases if len(_norm(p)) >= 2}
            # Fold in a word-like tag slug (pure lowercase alpha, no
            # underscore/digit) so an English acronym/term query (``CBT``,
            # ``sleep``) bridges to the French description phrases — but ONLY for
            # single-tag rows. A multi-tag row (``#sleep, #bzd``) asserts
            # co-occurring facets, not synonyms, so folding its slugs would make
            # ``sleep`` a synonym of ``bzd``. Underscored slugs
            # (``cannabis_thc_cbd``) are too slug-shaped to be query terms.
            word_slugs = [s for s in trow.tags if re.fullmatch(r"[a-z]{2,}", s)]
            if len(trow.tags) == 1 and word_slugs:
                group.add(word_slugs[0])
            if len(group) >= 2:
                groups.append(group)

        lookup: dict[str, set[str]] = {}
        max_len = 1
        for group in groups:
            for surface in group:
                # Key on the TOKENISED form (not raw _norm) so a hyphenated /
                # apostrophed surface is matchable from query tokens — see
                # _term_key. The group's surfaces are still emitted verbatim;
                # only the match key is tokenised.
                key = _term_key(surface)
                if not key:
                    continue
                max_len = max(max_len, len(key.split()))
                lookup.setdefault(key, set()).update(group)
        self.lookup = lookup
        # Cap the phrase-window length scanned at query time. 6 tokens covers
        # every multi-word concept phrase in the curated files with margin; a
        # longer key (rare) simply never matches, which is acceptable — the
        # alternative is an unbounded O(tokens²) window scan per query.
        self._max_key_len = min(max_len, 6)

    # --- query expansion -----------------------------------------------------

    def expand_query(self, query: str, max_variants: int = 3) -> list[str]:
        """Return up to *max_variants* reformulated query strings.

        Each variant substitutes one matched concept term in *query* with one
        of its synonyms (longest, most-specific phrase matches first; covered
        tokens are not re-matched). The original query is **not** included —
        callers retrieve it themselves and union the variants in. Deterministic:
        matches and alternatives are sorted, so the same query always yields the
        same variants. Returns ``[]`` when nothing matches.
        """
        if not query or max_variants <= 0 or not self.lookup:
            return []
        # Same tokeniser as the keys (_WORD_RE / _term_key), so a window of query
        # tokens joined by spaces matches a lookup key exactly — including
        # hyphenated phrases. We keep the match objects (not just text) because
        # the char offsets drive the in-place substitution below.
        toks = list(_WORD_RE.finditer(query))
        if not toks:
            return []
        norms = [_norm(t.group()) for t in toks]

        # Collect non-overlapping matches, longest phrase first.
        used: set[int] = set()
        matches: list[tuple[int, int, int, str]] = []  # (tok_len, char_start, char_end, key)
        for L in range(min(self._max_key_len, len(toks)), 0, -1):
            for i in range(0, len(toks) - L + 1):
                if any(j in used for j in range(i, i + L)):
                    continue
                key = " ".join(norms[i:i + L])
                if key in self.lookup:
                    matches.append((L, toks[i].start(), toks[i + L - 1].end(), key))
                    used.update(range(i, i + L))
        # Longer (more specific) matches first, then by position.
        matches.sort(key=lambda m: (-m[0], m[1]))

        variants: list[str] = []
        seen = {_norm(query)}
        for _L, cs, ce, key in matches:
            # Shorter, cross-form synonyms first (acronyms before long phrases).
            alts = sorted(self.lookup[key], key=lambda x: (len(x), x))
            for alt in alts:
                if _norm(alt) == key:
                    continue
                variant = query[:cs] + alt + query[ce:]
                nv = _norm(variant)
                if nv in seen:
                    continue
                seen.add(nv)
                variants.append(variant)
                if len(variants) >= max_variants:
                    return variants
        return variants

    # --- primer --------------------------------------------------------------

    def _render_row(self, row: AbbrevRow) -> str:
        surf = row.surfaces[0] if row.surfaces else ""
        # Render from the full signification (keeps "avec/sans" alternatives the
        # split-on-/ meanings would lose) but strip backticks and the
        # disambiguation/variant asides so a glossary line reads cleanly rather
        # than as raw table markup (e.g. CI°, Rispéridone rows).
        meaning = _clean_meaning_for_display(row.raw_meaning) or (
            row.meanings[0] if row.meanings else (row.note or "")
        )
        if not surf or not meaning:
            return ""
        return f"{surf} = {meaning}"

    def build_primer(
        self,
        query: str = "",
        max_chars: int = 1500,
        header: str = "",
        core_terms: Optional[Sequence[str]] = None,
    ) -> str:
        """Return a compact app-controlled glossary block (≤ *max_chars*).

        **Query-relevant rows first**, then the app-curated core glossary, so
        that under a tight budget the line-boundary truncation drops generic
        core entries before the abbreviations specific to *this* query. Query
        matching is **whole-token** (via :func:`_query_terms`), so a 2-char
        surface like ``IV`` is never matched as a substring inside a word
        (``survival``). Returns ``""`` when nothing fits / nothing to say.

        *header* and *core_terms* are caller (config) overrides: a non-empty
        *header* replaces the built-in :data:`_PRIMER_HEADER` intro sentence, and
        a non-empty *core_terms* replaces the :data:`_CORE_PRIMER_TERMS` priority
        list (both default to the built-ins, which are tuned for the maintainer's
        FR/EN clinical corpus — see ``vault_primer_header`` /
        ``vault_primer_core_terms``).
        """
        if max_chars <= 0 or not self.abbrev_rows:
            return ""

        header_text = header.strip() if isinstance(header, str) and header.strip() else _PRIMER_HEADER
        terms: Sequence[str] = core_terms if core_terms else _CORE_PRIMER_TERMS

        chosen: list[str] = []
        seen_lines: set[str] = set()

        def _add(row: AbbrevRow) -> None:
            line = self._render_row(row)
            if line and line not in seen_lines:
                seen_lines.add(line)
                chosen.append(line)

        # 1) query-relevant rows FIRST (most valuable for this query, so they
        #    must survive truncation). Whole-token match against the query's
        #    normalised n-gram set — never a substring test. Matches a row when
        #    the query contains either its abbreviation surface (``CLZP``) OR its
        #    meaning (``clozapine``); the n-gram set caps phrase length, so a
        #    multi-word meaning only matches when it appears verbatim.
        if query:
            qterms = _query_terms(query, self._max_key_len)
            if qterms:
                for row in self.abbrev_rows:
                    if any(_term_key(s) in qterms for s in row.surfaces) or any(
                        _term_key(m) in qterms for m in row.meanings
                    ):
                        _add(row)

        # 2) curated core glossary, in priority order (config core_terms override
        #    or the built-in _CORE_PRIMER_TERMS). Keyed on raw _norm (not
        #    _term_key) so the symbol rows (≈, ≠, Ø, ±) — which tokenise to "" —
        #    still resolve. Dedup skips any row already added by the query pass.
        by_norm: dict[str, AbbrevRow] = {}
        for row in self.abbrev_rows:
            for s in row.surfaces:
                by_norm.setdefault(_norm(s), row)
        for term in terms:
            row = by_norm.get(_norm(term))
            if row is not None:
                _add(row)

        if not chosen:
            return ""

        # Greedy fill under the byte budget. Reserve the header + its trailing
        # newline up front; each kept line costs len(line)+1 for its joining
        # newline. NOTE: the validator floors vault_primer_max_chars above the
        # header length (see api/routes/config.py), so a sane config always
        # leaves room for ≥1 line; a hand-edited sub-header budget yields ""
        # here deliberately rather than a header-only block.
        body_lines: list[str] = []
        budget = max_chars - len(header_text) - 1
        for line in chosen:
            if budget - (len(line) + 1) < 0:
                break
            body_lines.append(line)
            budget -= len(line) + 1
        if not body_lines:
            return ""
        return header_text + "\n" + "\n".join(body_lines)
