"""PDF annotation counting (proxy for 'this paper was read').

Counts /Annots entries across all pages using pikepdf. Cheap because
pikepdf reads the trailer/xref and walks lazily — we don't decode
content streams.

Subtypes we ignore so we don't count layout artefacts:
- /Link (just hyperlinks)
- /Widget (form fields)
- /Popup (companion to other annots — counted via the parent)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pikepdf

_IGNORED_SUBTYPES = {"/Link", "/Widget", "/Popup"}

ErrorKind = Literal["missing", "encrypted", "corrupt", "other"]


@dataclass(frozen=True)
class AnnotationsResult:
    """Outcome of reading a PDF's annotations.

    `count` is -1 whenever `error` is set."""

    count: int
    error: ErrorKind | None = None


def read_annotations(path: Path) -> AnnotationsResult:
    """Open `path` and count meaningful annotations.
    Distinguishes missing / encrypted / corrupt PDFs so the UI can show why."""
    if not path.exists():
        return AnnotationsResult(-1, "missing")
    try:
        with pikepdf.open(str(path)) as pdf:
            n = 0
            for page in pdf.pages:
                annots = page.get("/Annots")
                if annots is None:
                    continue
                for ann in annots:
                    try:
                        subtype = ann.get("/Subtype")
                    except Exception:
                        continue
                    if subtype is None or str(subtype) in _IGNORED_SUBTYPES:
                        continue
                    n += 1
            return AnnotationsResult(n)
    except pikepdf.PasswordError:
        return AnnotationsResult(-1, "encrypted")
    except pikepdf.PdfError:
        return AnnotationsResult(-1, "corrupt")
    except OSError:
        return AnnotationsResult(-1, "missing")
    except Exception:
        return AnnotationsResult(-1, "other")


def count_annotations(path: Path) -> int:
    """Back-compat wrapper. Returns the count or -1 if unreadable."""
    return read_annotations(path).count


def looks_read(path: Path, threshold: int) -> bool:
    """True iff annotation count is >= threshold. -1 (unreadable) returns False."""
    return count_annotations(path) >= threshold
