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
    name: str
    color: str | None

    @classmethod
    def parse(cls, raw: str) -> FinderTag:
        if "\n" in raw:
            name, idx = raw.split("\n", 1)
            try:
                color = _COLOR_NAMES.get(int(idx))
            except ValueError:
                color = None
            return cls(name=name, color=color)
        return cls(name=raw, color=None)


def read_tags(path: Path) -> list[FinderTag]:
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
    return {t.name for t in read_tags(path)}
