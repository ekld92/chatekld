"""macOS Finder-tag reader/writer for the biblio Tagging feature.

Finder tags live in the ``com.apple.metadata:_kMDItemUserTags`` extended
attribute as a binary plist holding a list of strings (each ``"Name"`` or
``"Name\\nN"`` where N is a colour index 0-7). This module reads and writes that
xattr via ``/usr/bin/xattr`` — it NEVER touches the PDF's content, only the tag
xattr. It lives in the route layer (not the vendored ``audit/`` engine, which
stays read-only); the audit's ``finder_tags.py`` remains the read-only reader
for the scan.

The write path round-trips RAW entries (the Zotero writer's ``before_raw``
pattern): existing entries keep their ``"\\nN"`` colour suffix verbatim and only
NEW tags ride as bare names — a name-only round-trip would silently strip the
colour off every tag the user assigned in Finder (the pre-2026-07-19 bug).

macOS-only: :func:`is_macos` gates the write endpoint (501 elsewhere). A
genuinely ABSENT attribute reads as ``[]``; any other read failure (timeout,
permission, corrupt plist) **raises** so the caller can refuse the write —
mistaking a failed read for "no tags" would make the union clobber the file's
whole tag set. A failed *write* raises too, so the caller can journal a failure.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys

_ATTR = "com.apple.metadata:_kMDItemUserTags"
# Absolute path: the module contract, and a Finder-launched frozen app's PATH
# is minimal — resolving `xattr` through it is one env away from ENOENT.
_XATTR = "/usr/bin/xattr"


def is_macos() -> bool:
    """True on Darwin; the Finder-tag write surface is macOS-only."""
    return sys.platform == "darwin"


def tag_name(raw: str) -> str:
    """The bare tag name of one raw xattr entry (colour suffix stripped)."""
    return str(raw).split("\n")[0]


def read_finder_tags_full(path: str) -> list[str]:
    """Return the file's raw Finder-tag entries VERBATIM (colour suffixes kept).

    ``[]`` only when the attribute is genuinely absent (or the plist holds no
    list). Every other failure — xattr error, timeout, undecodable payload —
    raises ``RuntimeError``: a caller about to *write* must be able to tell
    "no tags" from "could not read", or its union overwrites the real set.
    """
    try:
        out = subprocess.run(
            [_XATTR, "-px", _ATTR, path],
            capture_output=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"xattr read failed: {exc}") from exc
    if out.returncode != 0:
        stderr = out.stderr.decode(errors="replace")
        # `xattr -p` exits non-zero for a missing attribute with this marker;
        # any other non-zero exit is a real error and must not read as [].
        if "No such xattr" in stderr:
            return []
        raise RuntimeError(f"xattr read failed: {stderr.strip() or f'exit {out.returncode}'}")
    try:
        raw = bytes.fromhex("".join(out.stdout.decode().split()))
        values = plistlib.loads(raw)
    except (ValueError, plistlib.InvalidFileException) as exc:
        raise RuntimeError(f"unreadable Finder-tag plist: {exc}") from exc
    if not isinstance(values, list):
        return []
    return [str(v) for v in values]


def read_finder_tags(path: str) -> list[str]:
    """Return the file's current Finder tag NAMES ([] if none / unreadable).

    The read-only display/compare view: colour suffixes are stripped and any
    error yields ``[]`` (display code prefers an empty list over an exception).
    Anything that will WRITE the array back must use
    :func:`read_finder_tags_full` instead, both for the raise-on-error contract
    and to preserve colour metadata.
    """
    try:
        return [tag_name(v) for v in read_finder_tags_full(path)]
    except RuntimeError:
        return []


def write_finder_tags(path: str, tags: list[str]) -> None:
    """Overwrite the file's Finder tags with *tags* (raw entries, verbatim).

    Entries are written as given — pass raw ``"Name\\nN"`` strings from
    :func:`read_finder_tags_full` to keep existing colours, and bare names for
    new tags. Encodes a binary plist and writes it with ``xattr -wx``. Raises
    ``RuntimeError`` on any failure (non-zero exit, missing binary, timeout) so
    the caller can abort + journal a failure rather than silently losing the
    write. Content is never touched.
    """
    data = plistlib.dumps([str(t) for t in tags], fmt=plistlib.FMT_BINARY)
    try:
        r = subprocess.run(
            [_XATTR, "-wx", _ATTR, data.hex(), path],
            capture_output=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"xattr write failed: {exc}") from exc
    if r.returncode != 0:
        raise RuntimeError(f"xattr write failed: {r.stderr.decode(errors='replace').strip()}")
