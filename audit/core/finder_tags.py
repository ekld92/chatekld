"""macOS Finder tag reader (read-only).

Finder tags live in the xattr `com.apple.metadata:_kMDItemUserTags` as a
binary plist holding a list of strings. Each entry is either a plain tag
name or "TagName\\nN" where N is the Finder color index (0-7).

macOS's ``getxattr(2)`` takes a ``position`` argument that Python's
stdlib ``os.getxattr`` does not expose (the Linux syscall has no such
parameter), so we call libc's ``getxattr`` via ctypes to read the full
plist payload in one shot. On non-macOS platforms every read returns
an empty result.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import plistlib
import sys
from dataclasses import dataclass
from pathlib import Path

XATTR_KEY = "com.apple.metadata:_kMDItemUserTags"
_XATTR_NOFOLLOW = 0x0001

_COLOR_NAMES: dict[int, str | None] = {
    0: None,
    1: "gray",
    2: "green",
    3: "purple",
    4: "blue",
    5: "yellow",
    6: "red",
    7: "orange",
}


def _load_libc() -> ctypes.CDLL | None:
    """Bind libc's macOS ``getxattr`` (6-arg form) via ctypes, or None.

    The whole Finder-tag dimension hinges on this returning non-None: on any
    non-Darwin platform (or if libc cannot be loaded) it returns ``None`` and
    every tag read short-circuits to empty, so the audit degrades gracefully on
    Linux/Windows CI instead of erroring. The argtypes are pinned to the
    macOS signature, whose extra ``position``/``options`` parameters the stdlib
    ``os.getxattr`` does not expose. ``use_errno=True`` so a failed call can be
    distinguished, though callers here only branch on the return value.
    """
    if sys.platform != "darwin":
        return None
    name = ctypes.util.find_library("c") or "libc.dylib"
    try:
        libc = ctypes.CDLL(name, use_errno=True)
    except OSError:
        return None
    # ssize_t getxattr(const char *path, const char *name,
    #                  void *value, size_t size, u_int32_t position, int options);
    libc.getxattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    libc.getxattr.restype = ctypes.c_ssize_t
    return libc


_LIBC = _load_libc()


def _getxattr(path: Path, key: str) -> bytes | None:
    """Read one extended attribute's raw bytes, or None if absent/unreadable.

    Two-call idiom: the first ``getxattr`` with a NULL buffer asks the kernel
    for the value's size, then a second call reads exactly that many bytes.
    Any negative return (attribute missing, file gone, permission denied) maps
    to ``None``; a zero-length attribute returns ``b""``. Read-only — the file
    is never opened for write.
    """
    if _LIBC is None:
        return None
    cpath = str(path).encode("utf-8")
    cname = key.encode("utf-8")
    size = _LIBC.getxattr(cpath, cname, None, 0, 0, 0)
    if size < 0:
        return None
    if size == 0:
        return b""
    buf = ctypes.create_string_buffer(size)
    read = _LIBC.getxattr(cpath, cname, buf, size, 0, 0)
    if read < 0:
        return None
    return buf.raw[:read]


@dataclass
class FinderTag:
    """One Finder tag: a name plus an optional color label.

    Finder encodes a colored tag as ``"Name\\nN"`` where ``N`` is the color
    index 0-7; an uncolored tag is just the name. ``color`` is the resolved
    color word (``None`` for index 0 / no color / a bad index).
    """

    name: str
    color: str | None

    @classmethod
    def parse(cls, raw: str) -> FinderTag:
        """Parse one raw plist tag string into a :class:`FinderTag`.

        Splits the optional ``"\\nN"`` color suffix; a non-integer index is
        tolerated as "no color" rather than raising.
        """
        if "\n" in raw:
            name, idx = raw.split("\n", 1)
            try:
                color = _COLOR_NAMES.get(int(idx))
            except ValueError:
                color = None
            return cls(name=name, color=color)
        return cls(name=raw, color=None)


def read_tags(path: Path) -> list[FinderTag]:
    """Return the file's Finder tags as parsed :class:`FinderTag`s.

    Decodes the ``_kMDItemUserTags`` xattr (a binary plist holding a list of
    strings). Returns ``[]`` whenever the attribute is missing, the plist is
    undecodable, or it isn't a list — so an exotic/corrupt xattr can never
    break a scan. Read-only.
    """
    raw = _getxattr(path, XATTR_KEY)
    if not raw:
        return []
    try:
        values = plistlib.loads(raw)
    except Exception:
        return []
    if not isinstance(values, list):
        return []
    return [FinderTag.parse(str(v)) for v in values]


def read_tag_names(path: Path) -> set[str]:
    """Return just the set of tag names (color dropped) for ``path``.

    The audit only reconciles tag *names*; this is the form the inventory and
    reports consume.
    """
    return {t.name for t in read_tags(path)}
