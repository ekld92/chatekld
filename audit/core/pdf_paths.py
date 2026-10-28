"""PDF discovery shared by inventory, reconciliation and duplicate reports."""
from pathlib import Path


def pdf_files(root: Path):
    """Find PDF files regardless of extension case, without following directories."""
    return (path for path in root.rglob("*")
            if path.suffix.lower() == ".pdf" and path.is_file())
