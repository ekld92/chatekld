"""Read-only cache coverage checks; existence of one range is not completeness."""
from pathlib import Path
import re


def cache_coverage(roots, kind, key, digest, *, page_count=None):
    directories = []
    for root in roots:
        directory = Path(root) / kind / key
        if Path(root).is_symlink() or directory.is_symlink() or directory.parent.is_symlink():
            continue
        whole = directory / f"{digest}.txt"
        if whole.is_file() and not whole.is_symlink() and whole.stat().st_size:
            return "whole_unverified"
        directories.append(directory)
    # A retained complete legacy cache wins over a partial newer extraction,
    # and does not require opening the source PDF to count pages.
    partial = False
    for directory in directories:
        if kind != "pdf_cache":
            continue
        ranges = []
        for path in directory.glob(f"{digest}-p*.txt"):
            match = re.fullmatch(re.escape(digest) + r"-p(\d{5})-(\d{5})\.txt", path.name)
            if match and path.is_file() and not path.is_symlink() and path.stat().st_size:
                ranges.append((int(match[1]), int(match[2])))
        if not ranges:
            continue
        count = page_count() if page_count is not None else None
        end = 0
        for first, last in sorted(ranges):
            if first != end or last <= first:
                break
            end = last
        else:
            if count is not None and end == count:
                return "ranges_unverified"
        partial = True
    return "partial" if partial else "missing"
