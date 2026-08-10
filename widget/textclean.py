"""Pure LaTeX-source → display-text cleaner for widget payloads.

BibTeX fields are LaTeX *source*, so a title reaches us as
``Approches computationnelles de la schizophr\\'enie``. The widgets are
plain-text surfaces (a menu-bar menu, a desktop card) with no TeX renderer,
so the escapes must be resolved **server-side, once**, before the payload is
written — not per widget, and not in the audit's :class:`Record` objects
(the fingerprint, the PDF↔bib joins and the tag comparisons all key on the
raw parsed values; rewriting them would move every identity).

Stdlib-only and side-effect-free, so it can be unit-tested directly and
reused by any future display surface.

Coverage is deliberately the BibTeX subset that actually shows up in a
bibliography: accent commands (``\\'e``, ``\\"{u}``, ``\\c{c}``), the special
letters (``\\o``, ``\\ss``, ``\\ae``), escaped punctuation (``\\&``, ``\\%``),
inline math delimiters, text-style wrappers (``\\emph{…}``), dash/quote
ligatures, and protective braces. Anything else — an unknown macro — loses
its command name and keeps its argument text, which is the safe failure mode
for a *display* string.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["delatex", "looks_like_latex", "clean_display", "clean_prose"]

# --- accent commands -> combining marks -------------------------------------
# ``\'e`` becomes "e" + U+0301, which NFC then composes into "é". Doing it via
# combining marks (rather than a 200-row á/é/í table) covers every base letter
# for free, including the ones no bibliography has used yet.
_ACCENTS = {
    "'": "\u0301",   # acute
    "`": "\u0300",   # grave
    "^": "\u0302",   # circumflex
    '"': "\u0308",   # diaeresis
    "~": "\u0303",   # tilde
    "=": "\u0304",   # macron
    ".": "\u0307",   # dot above
    "u": "\u0306",   # breve
    "v": "\u030C",   # caron
    "H": "\u030B",   # double acute
    "c": "\u0327",   # cedilla
    "k": "\u0328",   # ogonek
    "d": "\u0323",   # dot below
    "b": "\u0331",   # macron below
    "r": "\u030A",   # ring above
}

# \'e  \'{e}  {\'e}  \'\i  \'{\i}  \c{c}  \v{s} ...
# The optional ``\`` before the base letter catches the dotless-i idiom
# ``\'{\i}``; ``\i``/``\j`` are mapped to plain i/j so NFC can compose.
_ACCENT_RE = re.compile(
    r"\\(?P<acc>['`^\"~=.]|(?:[uvHckdbr](?![a-zA-Z])))"
    r"\s*(?:\{\s*(?:\\(?P<esc>[ij])\b|(?P<braced>[a-zA-Z]))\s*\}"
    r"|\\(?P<esc2>[ij])\b|(?P<bare>[a-zA-Z]))"
)

# --- standalone letter/symbol commands ---------------------------------------
# Longest-first so ``\AE`` is not read as ``\A`` + "E".
_SYMBOLS = {
    "textemdash": "\u2014", "textendash": "\u2013",
    "textquotedblleft": "\u201C", "textquotedblright": "\u201D",
    "textquoteleft": "\u2018", "textquoteright": "\u2019",
    "textbullet": "\u2022", "textdegree": "\u00B0",
    "textregistered": "\u00AE", "texttrademark": "\u2122",
    "copyright": "\u00A9", "pounds": "\u00A3", "texteuro": "\u20AC",
    "ldots": "\u2026", "dots": "\u2026", "textellipsis": "\u2026",
    "AE": "\u00C6", "ae": "\u00E6", "OE": "\u0152", "oe": "\u0153",
    "AA": "\u00C5", "aa": "\u00E5", "O": "\u00D8", "o": "\u00F8",
    "L": "\u0141", "l": "\u0142", "ss": "\u00DF", "SS": "\u1E9E",
    "DH": "\u00D0", "dh": "\u00F0", "TH": "\u00DE", "th": "\u00FE",
    "NG": "\u014A", "ng": "\u014B",
    "i": "i", "j": "j",  # dotless forms: plain letters read better here
}
# Trailing ``\s*`` implements TeX's own rule: whitespace after a control WORD
# terminates the name and is not printed, so ``\O stergaard`` is "Østergaard".
_SYMBOL_RE = re.compile(
    r"\\(" + "|".join(sorted(map(re.escape, _SYMBOLS), key=len, reverse=True))
    + r")(?![a-zA-Z])(?:\{\})?\s*"
)

# --- wrappers whose ARGUMENT is the text --------------------------------------
_KEEP_ARG = (
    "emph|textit|textbf|textrm|texttt|textsc|textsl|textsf|textmd|textup|"
    "textnormal|text|mathrm|mathit|mathbf|mathsf|mathtt|mathnormal|"
    "textsuperscript|textsubscript|uppercase|lowercase|MakeUppercase|"
    "MakeLowercase|url|mbox|hbox|underline|selectfont"
)
_KEEP_ARG_RE = re.compile(r"\\(?:" + _KEEP_ARG + r")\s*\{([^{}]*)\}")
# \href{url}{label} -> label
_HREF_RE = re.compile(r"\\href\s*\{[^{}]*\}\s*\{([^{}]*)\}")

# Escaped punctuation, protected before anything else touches these characters.
_ESCAPES = {
    r"\&": "&", r"\%": "%", r"\$": "$", r"\#": "#", r"\_": "_",
    r"\{": "{", r"\}": "}", r"\textbackslash": "\\",
}
_SENTINEL = "\uE000"  # private-use area: cannot occur in bibliographic text

# A leftover macro: drop the command, keep whatever it wrapped.
_ANY_CMD_RE = re.compile(r"\\[a-zA-Z]+\s*")
# Line breaks (``\\``) and the spacing macros (``\,`` ``\;`` ``\!`` ``\ ``)
# are whitespace in a display string.
_SPACING_RE = re.compile(r"\\\\|\\[,;:!/ ]")

# Does this string carry LaTeX at all? Used to leave PDF/note prose alone.
_LATEX_HINT_RE = re.compile(r"\\[a-zA-Z]|\\['`^\"~=.]|\{\\")


def looks_like_latex(text: str) -> bool:
    """True when *text* contains a LaTeX command worth resolving.

    Prose extracted from a PDF or a note is not LaTeX; running the full
    cleaner over it would strip legitimate ``$`` amounts and ``~`` characters
    for no gain. This gate keeps that text byte-identical.
    """
    return bool(_LATEX_HINT_RE.search(text or ""))


def delatex(text: str) -> str:
    """Resolve LaTeX escapes in *text* into plain Unicode, NFC-normalised.

    Total: never raises, and returns ``""`` for a false-y input. Unknown
    macros lose their name and keep their argument.
    """
    if not text:
        return ""
    s = str(text)

    # 1. Park escaped punctuation so later stages can't misread it as syntax.
    for i, (src, _dst) in enumerate(_ESCAPES.items()):
        s = s.replace(src, f"{_SENTINEL}{i}{_SENTINEL}")

    # 2. Line breaks / spacing macros, then inline math: drop the delimiters
    #    and keep the content (a title's math is usually a symbol or a
    #    sub/superscript, both readable as plain text).
    s = _SPACING_RE.sub(" ", s)
    s = s.replace("$$", " ").replace("$", "")

    # 3. Wrappers, innermost first (\textbf{\emph{x}} needs two passes).
    s = _HREF_RE.sub(r"\1", s)
    for _ in range(3):
        s, n = _KEEP_ARG_RE.subn(r"\1", s)
        if not n:
            break

    # 4. Accents, then standalone symbol commands.
    def _accent(m: re.Match) -> str:
        base = m.group("esc") or m.group("braced") or m.group("esc2") \
            or m.group("bare") or ""
        return base + _ACCENTS[m.group("acc")]

    for _ in range(2):  # \c{\'e}-style stacking
        s, n = _ACCENT_RE.subn(_accent, s)
        if not n:
            break
    s = _SYMBOL_RE.sub(lambda m: _SYMBOLS[m.group(1)], s)

    # 5. Remaining macros lose their name; their argument braces fall in 7.
    s = _ANY_CMD_RE.sub("", s)

    # 6. Ligatures. Order matters: --- before --.
    s = (s.replace("---", "\u2014").replace("--", "\u2013")
          .replace("``", "\u201C").replace("''", "\u201D"))

    # 7. Protective braces and LaTeX's non-breaking space.
    s = s.replace("{", "").replace("}", "").replace("~", " ")

    # 8. Restore the parked escapes.
    for i, (_src, dst) in enumerate(_ESCAPES.items()):
        s = s.replace(f"{_SENTINEL}{i}{_SENTINEL}", dst)

    return unicodedata.normalize("NFC", " ".join(s.split()))


def clean_display(text: str) -> str | None:
    """``delatex`` for a bibliography field (title, author). ``None`` if empty."""
    out = delatex(text)
    return out or None


def clean_prose(text: str) -> str:
    """``delatex`` for extracted prose — a no-op unless it really is LaTeX.

    A page-1 PDF excerpt or a note callout is plain text; only run the
    cleaner when a command is actually present (see :func:`looks_like_latex`).
    """
    if not text:
        return ""
    return delatex(text) if looks_like_latex(text) else text
