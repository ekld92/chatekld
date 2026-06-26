"""Template awareness: reuse a user-supplied Beamer ``.tex`` as the deck shell.

The "one template per run" model: the user picks (and may edit) a Beamer
template. We split it into the parts deckgen needs —

  * **preamble**  — everything before ``\\begin{document}`` (document class,
    ``\\usepackage`` lines, theme, metadata, custom ``\\newcommand``s). Reused
    verbatim; it is user-authored and therefore trusted.
  * **opening**   — ``\\begin{document}`` up to the first content ``\\section``
    (title frame, ``\\AtBeginSection`` hook, table of contents).
  * **closing**   — the appendix / references / ``\\end{document}`` tail.

— and inject the model-generated sections between *opening* and *closing*.

It also derives, from the (possibly edited) preamble:
  * the **custom macros** the deck defines (``scan_macros``), following local
    ``\\usepackage{...}`` into sibling ``.sty`` files so house macros like
    ``\\citefoot`` / ``\\commonlogo`` are surfaced to the model;
  * the **bibliography index** (``resolve_bib``) so the model can emit real
    ``\\citefoot{key}`` cites and so we can flag invented keys afterwards.

No third-party imports — this module is pure stdlib and unit-testable without a
server.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

# Reading caps so a pathological file can't blow up memory. The .bib of a big
# Zotero library can be several MB, so it gets a larger ceiling than a .sty.
_MAX_STY_BYTES = 2 * 1024 * 1024
_MAX_BIB_BYTES = 32 * 1024 * 1024

def strip_comments(text: str) -> str:
    """Remove LaTeX line comments (a ``%`` not escaped as ``\\%``).

    Used only for *analysis* (finding \\usepackage / \\addbibresource / macro
    definitions) — the emitted preamble keeps its comments verbatim. This stops
    a commented-out ``\\addbibresource{...}`` or ``\\usepackage{...}`` from being
    treated as active.
    """
    out = []
    for line in text.splitlines():
        cut = None
        for i, ch in enumerate(line):
            if ch == "%" and (i == 0 or line[i - 1] != "\\"):
                cut = i
                break
        out.append(line if cut is None else line[:cut])
    return "\n".join(out)


def mask_comments(text: str) -> str:
    """Like :func:`strip_comments` but **length-preserving**: comment characters
    are replaced with spaces instead of removed, so a regex match position in the
    masked copy maps 1:1 onto the original.

    This is what ``split_template`` searches against: we must locate the *active*
    ``\\begin{document}`` / ``\\section`` / ``\\appendix`` / ``\\printbibliography``
    boundaries (a commented-out one must be ignored) yet then slice the **original**
    text at those positions to keep its comments verbatim in the emitted deck.
    Without this, a fully commented-out references frame (as in the house
    ``presentation.tex``) was pulled into the closing tail, stripping the ``%`` off
    its ``\\begin{frame}`` while leaving ``% \\end{frame}`` commented — an unclosed
    frame that breaks compilation.
    """
    chars = list(text)
    in_comment = False
    for i, ch in enumerate(text):
        if ch == "\n":
            in_comment = False           # comments end at the line break
            continue
        if in_comment:
            chars[i] = " "
            continue
        if ch == "%" and (i == 0 or text[i - 1] != "\\"):
            in_comment = True            # unescaped % starts a comment
            chars[i] = " "
    return "".join(chars)


_DOC_BEGIN_RE = re.compile(r"\\begin\{document\}")
_DOC_END_RE = re.compile(r"\\end\{document\}")
# First *content* section: \section{...} or \section[...]{...} (not \section*).
_CONTENT_SECTION_RE = re.compile(r"\\section\b(?!\s*\*)")
_APPENDIX_RE = re.compile(r"\\appendix\b")
_PRINTBIB_FRAME_RE = re.compile(r"\\begin\{frame\}")
_PRINTBIB_RE = re.compile(r"\\printbibliography\b")

# Package args that name a local file (a relative path) rather than a CTAN
# package: contains a slash, or ends in an explicit extension.
_USEPACKAGE_RE = re.compile(r"\\usepackage\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}")
_INPUT_RE = re.compile(r"\\(?:input|include)\s*\{([^}]*)\}")

# Macro-definition forms we surface to the model.
_NEWCOMMAND_RE = re.compile(
    r"\\(?:newcommand|providecommand|DeclareRobustCommand)\*?\s*"
    r"\{?\\([A-Za-z@]+)\}?\s*(?:\[(\d+)\])?"
)
# Matches up to (and including) the macro name; the xparse arg-spec that follows
# is extracted with a brace-balanced reader because it can itself contain nested
# braces (e.g. a default value ``O{red}``) that a ``\{([^}]*)\}`` regex truncates.
_NEWDOCCOMMAND_RE = re.compile(
    r"\\(?:NewDocumentCommand|DeclareDocumentCommand|ProvideDocumentCommand)\s*"
    r"\{?\\([A-Za-z@]+)\}?\s*"
)
_DEF_RE = re.compile(r"\\def\s*\\([A-Za-z@]+)")


def _balanced_brace_group(text: str, start: int) -> str:
    """Return the contents of the ``{...}`` group beginning at *start*.

    *text[start]* must be ``{``. Tracks brace depth so nested groups are kept
    intact; returns ``""`` if *start* is not an opening brace or the group is
    unterminated.
    """
    if start >= len(text) or text[start] != "{":
        return ""
    depth = 0
    out = []
    for ch in text[start:]:
        if ch == "{":
            depth += 1
            if depth == 1:
                continue  # skip the outermost opening brace
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return "".join(out)  # matched close — done
        out.append(ch)
    return ""  # unterminated

# Friendly descriptions for the house macros so the model uses them correctly.
_KNOWN_MACROS = {
    "citefoot": "cite a bibliography key — numeric [n] in text + author/year footnote",
    "commonlogo": "insert a shared figure from ../common/fig/, e.g. \\commonlogo[width=0.5\\linewidth]{file.png}",
    "refline": "plain-text reference footer at the bottom of a frame",
}

# Macros every deck author already knows / that are LaTeX-internal — never worth
# listing in the cheatsheet.
_BORING_MACROS = frozenset({
    "doctoralSchool", "specialty", "thefootnote", "thesection",
})

_MAX_MACROS = 24


@dataclass
class MacroInfo:
    """One document-defined macro surfaced to the model's prompt cheatsheet.

    Captures just enough to render a usable call signature: the name (without the
    leading backslash), the argument count, whether the first argument is optional
    (``[..]`` form), and a friendly description for the house macros we recognize.
    """
    name: str           # without the leading backslash
    arity: int = 0
    optional_first: bool = False  # first arg is optional ([..])
    description: str = ""

    def signature(self) -> str:
        """Render a human-readable call form, e.g. ``\\citefoot{arg}`` or ``\\x[opt]{arg}``.

        The placeholders are illustrative only (``[opt]`` for the optional first
        argument, ``{arg}`` for each mandatory one) — they show the model the shape
        of a call without claiming any particular argument semantics.
        """
        sig = f"\\{self.name}"
        if self.arity <= 0:
            return sig
        args = []
        for i in range(self.arity):
            if i == 0 and self.optional_first:
                args.append("[opt]")
            else:
                args.append("{arg}")
        return sig + "".join(args)


@dataclass
class TemplateParts:
    """The fully-analyzed template: the three reusable spans plus derived metadata.

    ``preamble`` / ``opening`` / ``closing`` are the verbatim shell slices (see
    :func:`split_template`); ``base_dir`` is the template's directory (used to
    resolve sibling ``.sty``/``.bib`` files); ``macros`` and ``bib_index`` are the
    scanned house macros and bibliography that feed the per-section prompts.
    """
    preamble: str
    opening: str
    closing: str
    base_dir: str = ""
    macros: list = field(default_factory=list)            # list[MacroInfo]
    bib_index: dict = field(default_factory=dict)         # key -> (author, year, title)

    @property
    def bib_keys(self) -> set:
        """The set of citation keys in the template's bibliography.

        Used by ``assemble.validate`` to flag a model-emitted ``\\citefoot{key}``
        whose key is not in the template's ``.bib`` (a hallucinated citation).
        """
        return set(self.bib_index.keys())


class TemplateError(ValueError):
    """Raised when a template cannot be parsed into the expected parts."""


def split_template(tex: str) -> tuple[str, str, str]:
    """Split *tex* into ``(preamble, opening, closing)``.

    Drops the template's *example* sections (everything from the first content
    ``\\section`` up to the closing tail) so only the reusable shell remains.
    """
    if not tex or not tex.strip():
        raise TemplateError("The template is empty.")

    # All boundary searches run on a comment-masked copy (same length as *tex*),
    # so a commented-out \begin{document} / \section / \appendix / references
    # frame is never mistaken for a live one; we then slice the ORIGINAL *tex* at
    # the discovered positions to keep its comments verbatim in the output.
    masked = mask_comments(tex)

    m_begin = _DOC_BEGIN_RE.search(masked)
    if m_begin is None:
        raise TemplateError(
            "No \\begin{document} found — this does not look like a full LaTeX "
            "document. Point at a complete .tex template (e.g. presentation.tex)."
        )
    m_end = _DOC_END_RE.search(masked, m_begin.end())
    if m_end is None:
        raise TemplateError("No \\end{document} found in the template.")

    preamble = tex[: m_begin.start()].rstrip() + "\n"
    body = tex[m_begin.start(): m_end.end()]            # includes \begin..\end{document}
    masked_body = masked[m_begin.start(): m_end.end()]  # same coords as *body*

    # Where does the reusable opening end? At the first *active* content \section.
    m_sec = _CONTENT_SECTION_RE.search(masked_body)
    opening_end = m_sec.start() if m_sec else None

    # Where does the closing tail start? Prefer \appendix, else the references
    # frame (a frame containing \printbibliography), else \end{document}. Computed
    # on masked_body so a commented references frame falls through to
    # \end{document} (and is dropped with the rest of the example body).
    closing_start = _find_closing_start(masked_body)

    if opening_end is None or opening_end > closing_start:
        # No content section before the tail — keep everything up to the tail
        # as the opening (degenerate template).
        opening_end = closing_start

    opening = body[:opening_end].rstrip() + "\n"
    closing = body[closing_start:].lstrip()
    return preamble, opening, closing


def _find_closing_start(masked_body: str) -> int:
    """Index (into the document body) where the closing tail begins.

    Operates on the comment-masked body so commented occurrences are ignored;
    the returned index is valid for the original body (same coordinates). Order
    of preference: an active ``\\appendix``, else the references frame that holds
    an active ``\\printbibliography``, else ``\\end{document}``.
    """
    m_app = _APPENDIX_RE.search(masked_body)
    if m_app:
        return m_app.start()
    # A frame that contains \printbibliography — start of the references slide.
    m_bib = _PRINTBIB_RE.search(masked_body)
    if m_bib:
        # Walk back to the \begin{frame} that opens this references slide.
        frame_start = None
        for m in _PRINTBIB_FRAME_RE.finditer(masked_body, 0, m_bib.start()):
            frame_start = m.start()
        if frame_start is not None:
            return frame_start
        return m_bib.start()
    m_end = _DOC_END_RE.search(masked_body)
    # _DOC_END_RE always matches here (split_template guaranteed it).
    return m_end.start() if m_end else len(masked_body)


# ---------------------------------------------------------------------------
# Local-file resolution (.sty referenced by the preamble)
# ---------------------------------------------------------------------------

def _is_local_ref(arg: str) -> bool:
    """True when a \\usepackage / \\input arg names a *relative* local file.

    Absolute paths are rejected: the house style only ever references siblings
    via ``../common/...``, and an absolute ``\\usepackage{/etc/...}`` (which
    ``os.path.join`` would honour verbatim) is never legitimate — so we never
    follow one when scanning for macros/bib.
    """
    arg = arg.strip()
    if not arg or arg.startswith(("/", "\\")):
        return False
    return ("/" in arg) or arg.endswith((".sty", ".tex")) or arg.startswith(".")


def _resolve_local(base_dir: str, arg: str, default_ext: str) -> Optional[str]:
    """Resolve a *relative* package/input/bib arg to an existing file.

    Absolute args are rejected here too (not just in ``_is_local_ref``) because
    ``resolve_bib`` calls this directly for ``\\addbibresource{...}``: without the
    guard, ``\\addbibresource{/abs/secret.bib}`` would be read and its parsed
    author/title surfaced into the prompt sent to the LLM.
    """
    if not base_dir:
        return None
    arg = arg.strip()
    if not arg or arg.startswith(("/", "\\")):
        return None
    cand = os.path.normpath(os.path.join(base_dir, arg))
    candidates = [cand]
    if not os.path.splitext(cand)[1]:
        candidates.append(cand + default_ext)
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _read_capped(path: str, cap: int) -> str:
    """Read at most *cap* bytes of *path* as text; "" on any OS error.

    The cap bounds memory so a pathological ``.sty`` / ``.bib`` cannot blow up the
    process; ``errors="replace"`` means a non-UTF-8 byte never aborts the scan.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(cap)
    except OSError:
        return ""


def _referenced_sty_sources(preamble: str, base_dir: str) -> list:
    """Return the text of each local .sty/.tex the preamble pulls in (one hop)."""
    sources = []
    seen = set()
    active = strip_comments(preamble)
    for rx, ext in ((_USEPACKAGE_RE, ".sty"), (_INPUT_RE, ".tex")):
        for m in rx.finditer(active):
            for arg in m.group(1).split(","):
                if not _is_local_ref(arg):
                    continue
                resolved = _resolve_local(base_dir, arg, ext)
                if resolved and resolved not in seen:
                    seen.add(resolved)
                    sources.append(_read_capped(resolved, _MAX_STY_BYTES))
    return sources


# ---------------------------------------------------------------------------
# Macro scanning
# ---------------------------------------------------------------------------

def scan_macros(preamble: str, base_dir: str = "") -> list:
    """Find document-defined macros in *preamble* + its local ``.sty`` files.

    Returns a de-duplicated list of :class:`MacroInfo`, known house macros first.
    """
    found: dict[str, MacroInfo] = {}

    def _add(name: str, arity: int, optional_first: bool) -> None:
        # ``@`` macros are LaTeX-internal (need \makeatletter) — never user-callable.
        if "@" in name or name in _BORING_MACROS or name in found:
            return
        found[name] = MacroInfo(
            name=name,
            arity=arity,
            optional_first=optional_first,
            description=_KNOWN_MACROS.get(name, ""),
        )

    sources = [preamble] + _referenced_sty_sources(preamble, base_dir)
    for src in sources:
        src = strip_comments(src)
        for m in _NEWCOMMAND_RE.finditer(src):
            name = m.group(1)
            arity = int(m.group(2)) if m.group(2) else 0
            # A LaTeX optional first arg shows up as the default-value form
            # \newcommand{\x}[n][default]; we can't see it cheaply, so flag
            # commonlogo specifically (its first arg is optional).
            _add(name, arity, optional_first=(name == "commonlogo"))
        for m in _NEWDOCCOMMAND_RE.finditer(src):
            name = m.group(1)
            # The arg spec is the brace group right after the name; read it with
            # balanced braces so a nested default like ``O{red}`` stays whole.
            spec = _balanced_brace_group(src, m.end())
            # xparse arg spec: count each argument *token* letter. Strip the
            # default-value groups first ({...}, [...], <...>) so a default like
            # ``O{red}`` is one optional arg, not inflated by the letters in
            # "red". Each remaining letter (m/o/O/r/R/d/D/s/t/e/b/v/l/u/g/G) is
            # one argument slot.
            spec_tokens = re.sub(r"\{[^}]*\}|\[[^\]]*\]|<[^>]*>", "", spec)
            arity = len(re.findall(r"[a-zA-Z]", spec_tokens))
            optional_first = bool(spec.strip()[:1] in ("o", "O", "d", "D", "s"))
            _add(name, arity, optional_first)
        for m in _DEF_RE.finditer(src):
            _add(m.group(1), 0, False)

    ordered = (
        [mi for mi in found.values() if mi.name in _KNOWN_MACROS]
        + [mi for mi in found.values() if mi.name not in _KNOWN_MACROS]
    )
    return ordered[:_MAX_MACROS]


def macro_cheatsheet(macros: list) -> str:
    """Render :class:`MacroInfo` list into a compact prompt block (or "")."""
    if not macros:
        return ""
    lines = []
    for mi in macros:
        if mi.description:
            lines.append(f"  {mi.signature()} — {mi.description}")
        elif mi.arity:
            lines.append(f"  {mi.signature()} — document-defined macro ({mi.arity} arg)")
        else:
            lines.append(f"  {mi.signature()} — document-defined macro")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bibliography
# ---------------------------------------------------------------------------

_ADDBIB_RE = re.compile(r"\\(?:addbibresource|bibliography)\s*\{([^}]*)\}")
_BIB_ENTRY_RE = re.compile(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", re.IGNORECASE)
_BIB_NONENTRY = frozenset({"comment", "string", "preamble", "set"})


def resolve_bib(preamble: str, base_dir: str = "") -> dict:
    """Parse the bibliography referenced by *preamble* into ``key -> (author, year, title)``."""
    index: dict[str, tuple] = {}
    for m in _ADDBIB_RE.finditer(strip_comments(preamble)):
        for arg in m.group(1).split(","):
            path = _resolve_local(base_dir, arg, ".bib")
            if not path:
                continue
            text = _read_capped(path, _MAX_BIB_BYTES)
            if text:
                _parse_bib_into(text, index)
    return index


def _parse_bib_into(text: str, index: dict) -> None:
    """Populate *index* with ``key -> (author, year, title)`` for each bib entry.

    Skips ``@comment``/``@string``/``@preamble``/``@set`` non-entry blocks. Each
    entry body is delimited as "from this entry header to the next", so the field
    scan never bleeds into the following entry.
    """
    for m in _BIB_ENTRY_RE.finditer(text):
        etype = m.group(1).lower()
        if etype in _BIB_NONENTRY:
            continue
        key = m.group(2).strip()
        if not key:
            continue
        # Scan the entry body (from this header to the next @entry) for fields.
        body_start = m.end()
        nxt = _BIB_ENTRY_RE.search(text, body_start)
        body = text[body_start: nxt.start() if nxt else len(text)]
        index[key] = (
            _bib_field(body, "author"),
            _bib_field(body, "year") or _bib_field(body, "date"),
            _bib_field(body, "title"),
        )


def _bib_field(body: str, field_name: str) -> str:
    """Extract a single bib field value (brace- or quote-delimited)."""
    m = re.search(
        rf"\b{field_name}\s*=\s*", body, re.IGNORECASE
    )
    if not m:
        return ""
    i = m.end()
    if i >= len(body):
        return ""
    ch = body[i]
    if ch == "{":
        depth = 0
        out = []
        for c in body[i:]:
            if c == "{":
                depth += 1
                if depth == 1:
                    continue
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            out.append(c)
        return _clean_bib_value("".join(out))
    if ch == '"':
        end = body.find('"', i + 1)
        return _clean_bib_value(body[i + 1: end] if end != -1 else "")
    # Bare value (e.g. year = 2022,)
    end = re.search(r"[,\n}]", body[i:])
    return _clean_bib_value(body[i: i + end.start()] if end else body[i:])


def _clean_bib_value(value: str) -> str:
    """Normalize a raw bib field: drop brace-grouping and collapse whitespace.

    BibTeX uses ``{...}`` braces to protect casing/grouping; they carry no meaning
    for the short author/year/title display we feed the prompt, so they are removed.
    """
    value = re.sub(r"[{}]", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def relevant_bib_keys(bib_index: dict, text: str, limit: int = 12) -> list:
    """Pick bib keys whose key/author/title overlaps *text* tokens (bounded).

    Used to hand the model a small, relevant candidate set per section instead
    of the whole library (which would blow the token budget and invite cites of
    irrelevant papers).
    """
    if not bib_index or not text:
        return []
    tokens = {t for t in re.findall(r"[a-z]{4,}", text.lower())}
    if not tokens:
        return []
    scored = []
    for key, (author, year, title) in bib_index.items():
        hay = f"{key} {author} {title}".lower()
        score = sum(1 for t in tokens if t in hay)
        if score > 0:
            scored.append((score, key))
    scored.sort(key=lambda kv: (-kv[0], kv[1]))
    return [key for _, key in scored[:limit]]


def bib_candidates_block(bib_index: dict, keys: list) -> str:
    """Render candidate keys as a prompt block: ``key — Author Year, Title``."""
    if not keys:
        return ""
    lines = []
    for key in keys:
        author, year, title = bib_index.get(key, ("", "", ""))
        meta = ", ".join(p for p in (f"{author} {year}".strip(), title) if p)
        lines.append(f"  {key}{(' — ' + meta) if meta else ''}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Suite root
# ---------------------------------------------------------------------------

def find_suite_root(template_path: str) -> Optional[str]:
    """Walk up from *template_path* to the dir that contains a ``common/`` dir.

    That directory is where ``../common/cress-style`` and ``../_master.bib``
    resolve from, so a generated ``<slug>/`` deck must be created there.
    Returns ``None`` if no such ancestor exists.
    """
    if not template_path:
        return None
    d = os.path.dirname(os.path.abspath(template_path))
    prev = None
    while d and d != prev:
        if os.path.isdir(os.path.join(d, "common")):
            return d
        prev, d = d, os.path.dirname(d)
    return None


def load_template_parts(tex: str, template_path: str = "") -> TemplateParts:
    """One-shot: split *tex* and derive macros + bib relative to *template_path*."""
    base_dir = os.path.dirname(os.path.abspath(template_path)) if template_path else ""
    preamble, opening, closing = split_template(tex)
    return TemplateParts(
        preamble=preamble,
        opening=opening,
        closing=closing,
        base_dir=base_dir,
        macros=scan_macros(preamble, base_dir),
        bib_index=resolve_bib(preamble, base_dir),
    )
