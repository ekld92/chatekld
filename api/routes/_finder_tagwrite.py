"""macOS Finder-tag reader/writer for the biblio Tagging feature.

Finder tags live in the ``com.apple.metadata:_kMDItemUserTags`` extended
attribute as a binary plist holding a list of strings (each ``"Name"`` or
``"Name\\nN"`` where N is a colour index 0-7). This module reads and writes that
xattr through libc's ``getxattr``/``setxattr`` via ctypes — it NEVER touches the
PDF's content, only the tag xattr, and it never forks a child (the historical
``/usr/bin/xattr`` spawn survives only as a fallback for a platform where the
libc binding cannot be made). It lives in the route layer (not the vendored
``audit/`` engine, which stays read-only); the audit's ``finder_tags.py`` remains
the read-only reader for the scan and is where the ctypes binding originated.

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

import ctypes
import ctypes.util
import os
import plistlib
import subprocess
import sys

_ATTR = "com.apple.metadata:_kMDItemUserTags"
# Absolute path: the module contract, and a Finder-launched frozen app's PATH
# is minimal — resolving `xattr` through it is one env away from ENOENT.
_XATTR = "/usr/bin/xattr"

# macOS errno for "the attribute does not exist" — the ONE failure that is
# allowed to read as "this file has no Finder tags". Every other errno raises.
_ENOATTR = 93
_ERANGE = 34


def _load_libc() -> ctypes.CDLL | None:
    """Bind libc's macOS ``getxattr``/``setxattr`` via ctypes, or None.

    Mirrors ``audit/core/finder_tags.py::_load_libc`` (the read-only reader that
    has used this binding since the audit shipped) and extends it with the write
    side. Returns ``None`` off Darwin or if libc cannot be loaded, in which case
    both helpers below fall back to spawning ``/usr/bin/xattr`` — the historical
    implementation, kept verbatim so no platform loses a capability here.

    macOS's ``getxattr(2)``/``setxattr(2)`` take extra ``position``/``options``
    parameters that Python's stdlib does not expose at all on Darwin (``os``
    ships ``getxattr``/``setxattr`` on Linux only), so ctypes is the only
    fork-free route. ``use_errno=True`` is load-bearing: distinguishing ENOATTR
    ("no tags") from every other failure ("could not read") is what keeps the
    union from clobbering a file's whole tag set.
    """
    if sys.platform != "darwin":
        return None
    name = ctypes.util.find_library("c") or "libc.dylib"
    try:
        libc = ctypes.CDLL(name, use_errno=True)
    except OSError:
        return None
    try:
        # ssize_t getxattr(const char *path, const char *name, void *value,
        #                  size_t size, u_int32_t position, int options);
        libc.getxattr.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int,
        ]
        libc.getxattr.restype = ctypes.c_ssize_t
        # int setxattr(const char *path, const char *name, const void *value,
        #              size_t size, u_int32_t position, int options);
        libc.setxattr.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int,
        ]
        libc.setxattr.restype = ctypes.c_int
    except AttributeError:  # pragma: no cover - a libc without either symbol
        return None
    return libc


_LIBC = _load_libc()


def is_macos() -> bool:
    """True on Darwin; the Finder-tag write surface is macOS-only."""
    return sys.platform == "darwin"


def tag_name(raw: str) -> str:
    """The bare tag name of one raw xattr entry (colour suffix stripped)."""
    return str(raw).split("\n")[0]


def _getxattr_bytes(path: str) -> bytes | None:
    """Raw tag-xattr bytes via libc, ``None`` when the attribute is ABSENT.

    Raises ``RuntimeError`` for every other errno (missing file, permission,
    unsupported filesystem) — the caller must be able to tell "no tags" from
    "could not read". Two-call idiom: ask the kernel for the size, then read
    exactly that many bytes; a value that grows between the two calls answers
    ERANGE and is retried, and one deleted between them answers ENOATTR and
    reads as absent.
    """
    assert _LIBC is not None
    cpath = os.fsencode(path)
    cname = _ATTR.encode("utf-8")
    for _ in range(3):
        ctypes.set_errno(0)
        size = _LIBC.getxattr(cpath, cname, None, 0, 0, 0)
        if size < 0:
            err = ctypes.get_errno()
            if err == _ENOATTR:
                return None
            raise RuntimeError(f"xattr read failed: {os.strerror(err)}")
        if size == 0:
            return b""
        buf = ctypes.create_string_buffer(size)
        ctypes.set_errno(0)
        read = _LIBC.getxattr(cpath, cname, buf, size, 0, 0)
        if read >= 0:
            return buf.raw[:read]
        err = ctypes.get_errno()
        if err == _ENOATTR:
            return None
        if err == _ERANGE:
            continue  # grew between the sizing call and the read — retry
        raise RuntimeError(f"xattr read failed: {os.strerror(err)}")
    raise RuntimeError("xattr read failed: value kept changing size")


def _getxattr_bytes_subprocess(path: str) -> bytes | None:
    """The historical ``/usr/bin/xattr -px`` read; used when libc is unbound."""
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
            return None
        raise RuntimeError(f"xattr read failed: {stderr.strip() or f'exit {out.returncode}'}")
    try:
        return bytes.fromhex("".join(out.stdout.decode().split()))
    except ValueError as exc:
        raise RuntimeError(f"unreadable Finder-tag plist: {exc}") from exc


def read_finder_tags_full(path: str) -> list[str]:
    """Return the file's raw Finder-tag entries VERBATIM (colour suffixes kept).

    ``[]`` only when the attribute is genuinely absent (or the plist holds no
    list). Every other failure — xattr error, timeout, undecodable payload —
    raises ``RuntimeError``: a caller about to *write* must be able to tell
    "no tags" from "could not read", or its union overwrites the real set.
    """
    # PERF (2026-09-02): a forked /usr/bin/xattr per Finder-tag read.
    #   Before:    every read spawned /usr/bin/xattr; measured 1.8 ms per call
    #              on Darwin 25.6 (arm64). NOTE the plan's premise was wrong
    #              here: /usr/bin/xattr is a Mach-O binary on this release, not
    #              the Python script that would have cost 60-120 ms. With the
    #              write below that is ~3.2 ms and 2 process spawns per PDF, so
    #              a 2000-key harmonize is ~6.4 s of fork+exec inside ONE
    #              obsidian op-lock hold with indexing frozen — and fork from
    #              the app's multi-GB-RSS process costs more than this
    #              micro-benchmark can see.
    #   Change:    libc getxattr via ctypes, in-process. Measured 0.017 ms.
    #              With the write, 0.024 ms and 0 spawns per PDF: ~0.05 s for
    #              the same 2000-key batch, 130x faster.
    #   Safe because:
    #              the returned value is byte-identical — the subprocess path
    #              hex-decoded the same plist bytes libc now hands back — and
    #              the absent-vs-error split is preserved exactly: `xattr -px`
    #              signalled absence with "No such xattr" on stderr, libc
    #              signals it with ENOATTR, and both map to []. Every other
    #              failure still raises.
    #   Preserves: "A genuinely ABSENT attribute reads as []; any other read
    #              failure (timeout, permission, corrupt plist) **raises** so
    #              the caller can refuse the write - mistaking a failed read for
    #              'no tags' would make the union clobber the file's whole tag
    #              set" (this module's docstring), and the raw colour-suffixed
    #              round trip (`before_raw`/`after_raw`) is untouched: entries
    #              are still returned verbatim.
    #   Lock/thread contract:
    #              unchanged - same thread, same caller-held op-lock; the call
    #              no longer forks a child from under it.
    #   Pinned by: test_tagging.py::TestFinderTagXattrNoFork
    if _LIBC is not None:
        raw = _getxattr_bytes(path)
    else:
        raw = _getxattr_bytes_subprocess(path)
    if raw is None:
        return []
    try:
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
    new tags. Encodes a binary plist and writes it through libc's ``setxattr``.
    Raises ``RuntimeError`` on any failure (bad errno, missing binary on the
    fallback path, timeout) so the caller can abort + journal a failure rather
    than silently losing the write. Content is never touched.
    """
    # PERF (2026-09-02): a forked /usr/bin/xattr per Finder-tag write.
    #   Before:    every write spawned /usr/bin/xattr -wx; measured 1.4 ms per
    #              call on Darwin 25.6 (arm64). See read_finder_tags_full above
    #              for the batch figure and the corrected plan premise.
    #   Change:    libc setxattr via ctypes, in-process. Measured 0.007 ms.
    #   Safe because:
    #              the payload is the SAME plistlib.FMT_BINARY bytes the
    #              subprocess path hex-encoded onto the xattr command line;
    #              `setxattr(..., options=0)` is create-or-replace and follows
    #              symlinks, exactly what `xattr -wx` did. A non-zero return
    #              still raises RuntimeError, so a failed write is still
    #              journalled as a failure rather than silently lost.
    #   Preserves: "A failed *write* raises too, so the caller can journal a
    #              failure" and "Content is never touched" (module docstring):
    #              only the tag xattr is set, the file is never opened.
    #   Lock/thread contract:
    #              unchanged - same thread, same caller-held op-lock; no child
    #              process is forked from under it.
    #   Pinned by: test_tagging.py::TestFinderTagXattrNoFork
    data = plistlib.dumps([str(t) for t in tags], fmt=plistlib.FMT_BINARY)
    if _LIBC is not None:
        ctypes.set_errno(0)
        rc = _LIBC.setxattr(os.fsencode(path), _ATTR.encode("utf-8"),
                            data, len(data), 0, 0)
        if rc != 0:
            raise RuntimeError(f"xattr write failed: {os.strerror(ctypes.get_errno())}")
        return
    try:
        r = subprocess.run(
            [_XATTR, "-wx", _ATTR, data.hex(), path],
            capture_output=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"xattr write failed: {exc}") from exc
    if r.returncode != 0:
        raise RuntimeError(f"xattr write failed: {r.stderr.decode(errors='replace').strip()}")
