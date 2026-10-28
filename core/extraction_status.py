"""Additive extraction evidence; never persisted into legacy text or vectors."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace


@dataclass(frozen=True)
class ExtractionStatus:
    completeness: str = "unknown"
    origin: str = "legacy"
    profile: str = "legacy"
    source_sha256: str | None = None
    requested_range: tuple[int, int] | None = None
    warnings: tuple[str, ...] = ()
    attempted_pages: tuple[int, ...] = ()
    completed_pages: tuple[int, ...] = ()
    skipped_pages: tuple[int, ...] = ()
    failed_pages: tuple[int, ...] = ()
    truncated_pages: tuple[int, ...] = ()

    def limited(self) -> ExtractionStatus:
        return replace(self, completeness="partial",
                       warnings=tuple(dict.fromkeys((*self.warnings, "Text or coverage is limited."))))

    def to_dict(self) -> dict:
        return asdict(self)


class TextRead(tuple):
    """Still unpacks/compares as (text, truncated); evidence is request-local."""
    extraction_status: ExtractionStatus

    def __new__(cls, text: str, truncated: bool, status: ExtractionStatus | None = None):
        result = super().__new__(cls, (text, bool(truncated)))
        status = status or ExtractionStatus()
        result.extraction_status = status.limited() if truncated else status
        return result

    def __getnewargs__(self):
        return self[0], self[1], self.extraction_status


def status_of(result) -> ExtractionStatus:
    """Legacy/custom readers cannot certify completeness merely by returning False."""
    status = getattr(result, "extraction_status", None)
    if isinstance(status, ExtractionStatus):
        return status
    return ExtractionStatus().limited() if result[1] else ExtractionStatus()


class OCRText(str):
    """String-compatible OCR response carrying transport stop/failure evidence."""
    truncated: bool
    failed: bool

    def __new__(cls, text: str, *, truncated: bool = False, failed: bool = False):
        result = super().__new__(cls, text)
        result.truncated = truncated
        result.failed = failed
        return result
