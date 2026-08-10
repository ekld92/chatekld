"""Document-class-agnostic LaTeX substrate shared by every deckgen output kind.

Extracted from ``assemble.py`` (Step 0 of the article/report generator work) so a
future ``docgen`` (article / report / mémoire) can reuse the metadata escaping,
title substitution, dangerous-macro / citation / graphics scanning, comment
stripping, and template assembly **unchanged** — supplying only its own
per-class prompts plus a :class:`DocClassStrategy` (preamble + structural
checks). ``assemble.py`` keeps the Beamer-specific pieces and re-exports the
names below so every existing ``from deckgen.assemble import …`` keeps working
byte-for-byte.

Pure and dependency-free (stdlib only), like the rest of the deckgen core.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

# ── Scaffold / body regexes (class-agnostic) ─────────────────────────────────
# Lines that belong to the preamble / document scaffold and must never appear
# inside a generated body (the assembler owns the scaffold). Matched at line
# start after optional whitespace. The Beamer-only ``usetheme``/``usecolortheme``
# /``usefonttheme`` tokens are kept in the superset: a prose article body would
# never legitimately contain them, so the superset is harmless for both classes.
_PREAMBLE_LINE_RE = re.compile(
    r"^\s*\\(?:documentclass|usepackage|usetheme|usecolortheme|usefonttheme"
    r"|title|subtitle|author|institute|date|maketitle)\b"
)
_DOCUMENT_ENV_RE = re.compile(r"^\s*\\(?:begin|end)\{document\}")

# A fenced code block wrapper the model may wrap its answer in.
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*$")

# ``\section`` exists in article and beamer alike; the *interpretation* (a slide
# group vs a prose section, and whether it anchors a body trim) is strategy-owned.
_SECTION_RE = re.compile(r"\\section\b")

# ── Dangerous compile-time macro detection ───────────────────────────────────
# LaTeX macros that can execute shell commands or read/write arbitrary files at
# *compile* time. The model's output is grounded in untrusted vault notes, so a
# prompt-injection (or accident) could emit one of these; we never strip them
# (that could corrupt legitimate content) but we WARN so the user reviews the
# file and compiles without shell-escape. The list is intentionally broad —
# shell-escape / file write (write, incl. `\write18`/`\write16`; openout;
# immediate; \ShellEscape), file read (input/include/openin/read, plus the
# higher-level \InputIfFileExists / \CatchFileEdef / \verbatiminput /
# \lstinputlisting readers), and the LuaTeX escape hatches
# (directlua/luaexec) plus the catch-all \special — because it is
# advisory-only here and, in review.screen_repair, only ever blocks a repair
# that INTRODUCES one not in the original (a legitimate template using e.g.
# \special is never falsely refused; it just earns an advisory warning).
#
# The terminator is ``\d*(?![a-zA-Z])`` (NOT ``\b``): a TeX control word ends at
# the first non-letter, so this matches stream-numbered forms like ``\write18`` /
# ``\openout15`` (where ``\b`` fails — it counts the trailing digit as a word
# char) and CAPTURES the digits so the warning names the exact macro
# (``\write18`` = shell-escape, not just ``\write``); the ``(?![a-zA-Z])`` still
# excludes a longer letter-suffixed macro such as the safe, common
# ``\includegraphics`` (``\include`` followed by the letter ``g``).
# Added 2026-07-20 (audit M2): the file-read/exec macros below are plain
# control words (NOT the catcode/\csname obfuscations the residual-limits note
# disclaims), so they belong in the denylist just like \input — omitting them
# let a prompt-injected compile-fix repair emit e.g. \verbatiminput{~/.ssh/id_rsa}
# and have it auto-written and typeset. \InputIfFileExists / \CatchFileEdef are
# capital-I/C forms the lowercase `input` alternative never matched.
#
# Extended 2026-07-20 (audit finding 3): TeX control words are case-sensitive,
# so a blanket re.IGNORECASE would false-positive on unrelated names — instead
# we add the specific REAL sibling file-readers the earlier list missed:
# \VerbatimInput (fancyvrb, the capital-V twin of \verbatiminput), \import /
# \subimport (the import package), \includepdf (pdfpages), and \inputminted
# (minted). Each is a distinct control word, so it needs its own alternative;
# the terminator still excludes letter-suffixed lookalikes (\important, etc.).
_DANGEROUS_MACRO_RE = re.compile(
    r"\\(?:write|immediate|openin|openout|input|include|read"
    r"|special|directlua|luaexec|@@?input"
    r"|InputIfFileExists|CatchFileEdef|CatchFileDef"
    r"|verbatiminput|VerbatimInput|lstinputlisting|inputminted"
    r"|import|subimport|includepdf|ShellEscape)\d*(?![a-zA-Z])"
)

# A ``\csname … \endcsname`` construction. LaTeX builds a control sequence from the
# literal text between the two macros at expansion time, so ``\csname write18\endcsname``
# IS ``\write18`` — a shell-escape the plain ``\macro`` regex above never matches
# (there is no backslash before ``write18``). We reconstruct the built name and run
# it through the same dangerous-word test. ``re.DOTALL`` so a multi-line build is
# still caught.
_CSNAME_RE = re.compile(r"\\csname\s*(.*?)\\endcsname", re.DOTALL)

# The dangerous control-word stems, as bare names (no leading backslash), for the
# reconstructed-``\csname`` test. Kept in sync with the alternation above.
_DANGEROUS_WORDS = (
    "write", "immediate", "openin", "openout", "input", "include", "read",
    "special", "directlua", "luaexec", "@@input", "@input",
    "InputIfFileExists", "CatchFileEdef", "CatchFileDef",
    "verbatiminput", "VerbatimInput", "lstinputlisting", "inputminted",
    "import", "subimport", "includepdf", "ShellEscape",
)

# Citation commands whose argument is a (comma-separated list of) bib key(s).
# Used by the hallucinated-key guard in validate() and is biblatex/natbib-aware.
_CITE_MACRO_RE = re.compile(
    r"\\(?:cite|citefoot|textcite|parencite|autocite|smartcite|footcite"
    r"|citeauthor|citeyear|citep|citet|citealp|citealt|citenum)\*?"
    r"(?:\[[^\]]*\])*\s*\{([^}]*)\}"
)

_GRAPHICS_RE = re.compile(
    r"\\includegraphics\*?(?:\[[^\]]*\])*\s*\{([^}]*)\}"
)

# Characters to escape when injecting CLI-supplied metadata into LaTeX macros.
# Order matters: backslash is not escaped (metadata is plain text and a stray
# backslash is far rarer than the listed specials); we keep this conservative.
_METADATA_ESCAPES = {
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
}


@dataclass
class SectionOutput:
    """One section's sanitized body plus provenance for logging."""
    title: str
    body: str
    raw: str = ""
    infos: list = field(default_factory=list)
    placeholder: bool = False  # True when no usable content was generated


def _strip_latex_comments(text: str) -> str:
    """Drop LaTeX line comments (an unescaped ``%`` to end-of-line).

    validate() must not be fooled by *commented-out* LaTeX: a ``% \\end{frame}``
    is not a real frame close, and a commented ``\\input`` / ``\\cite`` is inert.
    Counting/scanning the comment-stripped text fixes both false positives and
    false negatives. (Mirrors ``template.strip_comments``; kept local so this
    module stays dependency-free, like the rest of the deckgen core.)
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


def find_dangerous_macros(tex: str) -> set[str]:
    """Return the set of dangerous compile-time macros found in *tex*.

    THE single detector shared by :func:`validate_common` and
    :func:`deckgen.review.screen_repair` so the advisory warning and the repair
    safety-gate can never disagree. Catches both the direct ``\\write18`` form AND
    the ``\\csname write18\\endcsname`` / ``\\@@input`` obfuscations a prompt-injected
    vault note could use to smuggle a shell-escape past a naive ``\\macro`` scan.
    Comments are stripped first (a commented-out ``\\input`` is inert).

    KNOWN RESIDUAL LIMITS (documented honestly, not over-claimed): a determined
    attacker can still defeat a regex-only screen via catcode tricks or char-by-char
    construction (``\\string`` / ``\\lowercase{…}`` / ``\\expandafter`` chains that
    assemble the name from fragments). This screen RAISES THE BAR; it is not a
    sandbox. The real safety net is the emit-only design plus the standing advice to
    compile WITHOUT shell-escape (``pdflatex -no-shell-escape``).
    """
    return set(count_dangerous_macros(tex))


def count_dangerous_macros(tex: str) -> dict[str, int]:
    """Per-macro OCCURRENCE counts of the dangerous compile-time macros.

    Companion to :func:`find_dangerous_macros` (which returns just the names)
    for the repair safety gates. Counts matter because a name-set diff licensed
    unlimited *new* occurrences of any macro the original legitimately used
    once — a hand-written deck containing one ``\\input{preamble}`` let a
    prompt-injected repair add ``\\input{/etc/passwd}`` unchallenged, since the
    "introduced" name set was empty (2026-07-22 audit M4). The gates now refuse
    when any macro's count INCREASES.
    """
    scan = _strip_latex_comments(tex or "")
    found: dict[str, int] = {}
    for name in _DANGEROUS_MACRO_RE.findall(scan):
        found[name] = found.get(name, 0) + 1
    for inner in _CSNAME_RE.findall(scan):
        bare = "".join(inner.split()).lstrip("\\")   # builder ignores whitespace; drop a written-in backslash
        for word in _DANGEROUS_WORDS:
            # Match the word optionally followed by a stream number (write18) but
            # not a longer letter-suffixed macro (includegraphics) — same semantics
            # as the ``\d*(?![a-zA-Z])`` terminator on the direct-form regex.
            if bare == word or (bare.startswith(word)
                                and bare[len(word):].lstrip("0123456789") == ""):
                key = f"\\csname {bare}\\endcsname"
                found[key] = found.get(key, 0) + 1
                break
    return found


def latex_escape_metadata(value: str) -> str:
    """Escape LaTeX specials in a short metadata string (title/author/...)."""
    return "".join(_METADATA_ESCAPES.get(ch, ch) for ch in value)


# Short-form metadata (\title[short]{...}) shows in Beamer footlines — keep it
# from overflowing when the user's full title is long. Harmless for article
# (the [opt] slot is simply absent, so substitute_metadata_macro ignores it).
_SHORT_METADATA_MAX = 40


def _short_form(value: str) -> str:
    flat = " ".join(value.split())
    if len(flat) <= _SHORT_METADATA_MAX:
        return flat
    cut = flat[:_SHORT_METADATA_MAX].rsplit(" ", 1)[0]
    return (cut or flat[:_SHORT_METADATA_MAX]).rstrip() + "..."


def _balanced_group_end(text: str, open_idx: int) -> Optional[int]:
    """Index of the ``}`` matching the ``{`` at *open_idx*, or ``None``."""
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def substitute_metadata_macro(
    text: str, macro: str, value: str, *, short: Optional[str] = None
) -> str:
    """Replace the first ``\\<macro>[opt]{arg}`` in *text* with escaped *value*.

    The optional ``[opt]`` argument (Beamer's short form, shown in footlines)
    is replaced by *short* when both are present. The macro-name match is
    word-bounded so ``\\title`` never matches ``\\titlepage``/``\\titlegraphic``.
    Returns *text* unchanged when the macro is absent or malformed — the
    template preamble is trusted user content, so "don't touch" is the only
    safe failure mode.
    """
    m = re.search(rf"\\{macro}(?![a-zA-Z])", text)
    if not m:
        return text
    i = m.end()
    n = len(text)
    while i < n and text[i] in " \t":
        i += 1
    opt_span = None
    if i < n and text[i] == "[":
        close = text.find("]", i)
        if close == -1:
            return text
        opt_span = (i + 1, close)
        i = close + 1
        while i < n and text[i] in " \t":
            i += 1
    if i >= n or text[i] != "{":
        return text
    end = _balanced_group_end(text, i)
    if end is None:
        return text
    escaped = latex_escape_metadata(value)
    if opt_span and short is not None:
        return (
            text[: opt_span[0]]
            + latex_escape_metadata(short)
            + text[opt_span[1]: i + 1]
            + escaped
            + text[end:]
        )
    return text[: i + 1] + escaped + text[end:]


def substitute_title(
    preamble: str, title: str, subtitle: Optional[str] = None
) -> str:
    """Substitute the user's title (and optional subtitle) into a template
    preamble's ``\\title[short]{...}`` / ``\\subtitle{...}``.

    Fixes the "generated document keeps the template's placeholder title" gap:
    ``assemble_with_template`` reuses the preamble verbatim, so without this
    pass the topic never reaches ``\\title``. ``\\author``/``\\institute``/
    ``\\date`` are deliberately untouched (stable user identity vs per-doc
    content). No-op on empty *title* or a preamble without ``\\title``.
    """
    if not (title or "").strip():
        return preamble
    out = substitute_metadata_macro(
        preamble, "title", title.strip(), short=_short_form(title)
    )
    if subtitle and subtitle.strip():
        out = substitute_metadata_macro(
            out, "subtitle", subtitle.strip(), short=_short_form(subtitle)
        )
    return out


def _sections_block(sections: list[SectionOutput]) -> str:
    """Join the non-empty sanitized section bodies, or a placeholder comment."""
    bodies = [sec.body.strip() for sec in sections if sec.body.strip()]
    return "\n\n".join(bodies) if bodies else "% (no section content generated)"


def assemble_with_template(
    sections: list[SectionOutput],
    *,
    preamble: str,
    opening: str,
    closing: str,
) -> str:
    """Assemble a document around a user-supplied template shell.

    *preamble* / *opening* / *closing* come from
    :func:`deckgen.template.split_template`; the generated section bodies are
    injected between *opening* (title scaffold, already containing
    ``\\begin{document}``) and *closing* (the ``\\end{document}`` tail). The
    template parts are trusted (user-authored); only the section bodies are
    model output. Class-agnostic — it splices text, with no frame/slide knowledge.
    """
    parts = [
        preamble.rstrip(),
        "",
        opening.rstrip(),
        "",
        _sections_block(sections),
        "",
        closing.strip(),
        "",
    ]
    return "\n".join(parts)


def extract_cite_keys(tex: str) -> list[str]:
    """Return every bib key referenced by a citation command in *tex*."""
    keys: list[str] = []
    for m in _CITE_MACRO_RE.finditer(tex):
        for raw in m.group(1).split(","):
            k = raw.strip()
            if k:
                keys.append(k)
    return keys


def extract_graphics_keys(tex: str) -> list[str]:
    """Return every graphics path referenced by an \\includegraphics command in *tex*."""
    keys: list[str] = []
    for m in _GRAPHICS_RE.finditer(tex):
        k = m.group(1).strip()
        if k:
            keys.append(k)
    return keys


def comment_out_missing_graphics(tex: str, resolved_basenames: set[str]) -> str:
    """Comment out every ``\\includegraphics`` whose basename is unresolved.

    *resolved_basenames* is the caller's verdict (vault-copied figures plus
    anything the caller chose to keep — e.g. files already present next to
    the deck); filesystem knowledge deliberately stays with the caller so
    this module remains pure.

    Line-safe: a ``%`` comments out the REST of its line, so a command that
    shares a line with other content is moved onto its own commented line and
    the surrounding pieces are preserved on adjacent lines (a single newline
    is just a space to LaTeX, inside braces too, so meaning is unchanged).
    The naive whole-match ``% <cmd>`` replacement this fixes swallowed
    whatever followed the command on the line — a closing brace, an
    ``\\end{...}`` — breaking the very documents it was meant to protect. A
    command whose optional args span multiple lines is left untouched (kept),
    the safe default for a rewrite that only ever *removes* content.
    """
    def _unresolved(line: str) -> list:
        return [
            m for m in _GRAPHICS_RE.finditer(line)
            if os.path.basename(m.group(1).strip()) not in resolved_basenames
        ]

    out_lines: list[str] = []
    for line in tex.split("\n"):
        matches = _unresolved(line)
        if not matches:
            out_lines.append(line)
            continue
        pieces: list[str] = []
        cursor = 0
        for m in matches:
            pre = line[cursor:m.start()]
            if pre.strip():
                pieces.append(pre)
            basename = os.path.basename(m.group(1).strip())
            pieces.append(f"% {m.group(0)} % Figure not found: {basename}")
            cursor = m.end()
        post = line[cursor:]
        if post.strip():
            pieces.append(post)
        out_lines.append("\n".join(pieces))
    return "\n".join(out_lines)


def validate_common(
    tex: str,
    *,
    generated_tex: Optional[str] = None,
    known_bib_keys: Optional[set] = None,
    copied_figures: Optional[set] = None,
) -> list[str]:
    """The document-class-agnostic half of the structural checks.

    Covers the ``document`` environment count, the dangerous-macro scan, the
    hallucinated citation-key guard, and the graphics-target guard — every check
    that is identical for a Beamer deck and a LaTeX article. The class-specific
    structural checks (frame balance for Beamer; section presence for an article)
    live behind :class:`DocClassStrategy`.

    Structural checks run over the whole *tex*; the dangerous-macro and citation
    checks run over *generated_tex* when supplied (the model-generated span) so
    the **trusted** template preamble/scaffold is not flagged. When *generated_tex*
    is ``None`` the whole document is scanned (legacy no-template path).
    """
    warnings: list[str] = []
    scan_tex = _strip_latex_comments(tex)
    untrusted = _strip_latex_comments(tex if generated_tex is None else generated_tex)

    n_doc_begin = len(re.findall(r"\\begin\{document\}", scan_tex))
    n_doc_end = len(re.findall(r"\\end\{document\}", scan_tex))
    if n_doc_begin != 1 or n_doc_end != 1:
        warnings.append(
            f"Expected exactly one document environment, found "
            f"{n_doc_begin} \\begin{{document}} / {n_doc_end} \\end{{document}}."
        )

    danger = sorted(find_dangerous_macros(tex if generated_tex is None else generated_tex))
    if danger:
        warnings.append(
            "Potentially unsafe LaTeX found in generated content: "
            f"{', '.join(danger)}. The document is built from untrusted vault notes — "
            "review these before compiling, and compile WITHOUT shell-escape "
            "(e.g. `pdflatex -no-shell-escape`)."
        )

    if known_bib_keys is not None:
        unknown = sorted({
            k for k in extract_cite_keys(untrusted) if k not in known_bib_keys
        })
        if unknown:
            warnings.append(
                "Citation key(s) not found in the bibliography: "
                f"{', '.join(unknown)}. The model may have invented these — "
                "remove the \\citefoot/\\cite or add the entry to your .bib "
                "before compiling (an undefined key compiles to '[?]')."
            )

    if copied_figures is not None:
        graphics_keys = extract_graphics_keys(untrusted)
        unknown_figs = sorted({
            os.path.basename(k) for k in graphics_keys if os.path.basename(k) not in copied_figures
        })
        for fig in unknown_figs:
            warnings.append(
                f"Figure not found: {fig}. The model may have invented this target or it could not be resolved in the vault."
            )
        if graphics_keys and not re.search(r"\\usepackage(?:\[[^\]]*\])*\s*\{graphicx\}", scan_tex):
            warnings.append(
                "The LaTeX preamble does not load 'graphicx' — graphics targets may fail to compile. Add \\usepackage{graphicx} to your template."
            )

    return warnings


@dataclass(frozen=True)
class DocClassStrategy:
    """The per-document-class seam the generator varies over.

    A Beamer deck and a LaTeX article share the whole substrate above; they
    differ only in (a) the fallback preamble used when there is no user template,
    and (b) the *structural* validity checks — frame balance for Beamer, section
    presence for an article. ``assemble.py`` binds ``BEAMER``; a future
    ``docgen`` binds ``ARTICLE`` and reuses ``template``/``scaffold``/
    ``checkpoint``/``retry``/``compile``/``augment``/``review`` unchanged.

    ``structural_checks`` receives the comment-stripped document text and returns
    the class-specific warnings; :func:`validate_common` supplies the rest.
    """
    name: str
    preamble_template: Optional[str]
    structural_checks: Callable[[str], list[str]]
