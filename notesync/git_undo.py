"""Prove, file by file, that the vault's git can undo a batch of note rewrites.

Three note sweeps are deliberately NOT journalled — ``/migrate``/``/unmigrate``
(1,235 notes) and ``/refresh-all`` / ``/mirror``'s region rewrites — because
they cannot ride the refactor journal's 200-op window. By maintainer decision
the vault's own git repository is their undo. That decision is only honest if,
for EVERY file the sweep is about to replace, git holds exactly the bytes being
replaced.

What this replaces, and why (library-workflow plan lot 1, defect 1,
2026-09-21). The previous gate asked two FOLDER-level questions: is
``git status --porcelain -- <notes dir>`` empty, and does ``git ls-files``
list anything there. ``status`` omits ignored files, and ``ls-files`` is
satisfied by one tracked file, so a folder mixing tracked notes with
git-ignored ones passed — and the sweep then rewrote the ignored notes, for
which git holds nothing. It also refused the whole sweep for an unrelated
untracked scratch file that the sweep never touches.

The check here is per replaced file and binds to ONE commit: the file is a
regular blob in ``HEAD`` (mode 100644/100755 — a symlink or submodule entry is
refused), and the bytes ``git checkout <commit> -- <file>`` would write for it
equal the bytes the caller PLANS to replace. Together with the writers' own
under-lock re-read (the file on disk must still equal the planned bytes at
write time, or that file is skipped), that is a precise statement: checking
out the named commit restores the exact pre-write bytes of every file the
sweep replaced. The commit id is returned so the response can name it.

How each file is located and compared (hardened after the 2026-09-21
adversarial review, which reproduced each case below on throwaway repos):

* **Location comes from git, not from path arithmetic.** Each note's parent
  directory is handed to ``git rev-parse --show-toplevel --show-prefix``, and
  the repo-relative name is that prefix plus the file name. The first cut
  computed ``Path.resolve().relative_to(toplevel)``, a plain string
  comparison: a configured vault path whose Unicode form (NFC/NFD) or letter
  case differed from the on-disk form put EVERY note "outside the repository".
* **Names are matched in git's own form.** With ``core.precomposeunicode``
  (the macOS default) git stores and lists names as NFC while APFS may hand us
  NFD; names are compared after NFC normalisation in that case, so an
  NFD-named committed note is no longer refused as "untracked".
* **The comparison is against what CHECKOUT would write.** With
  ``core.autocrlf`` or a ``text``/``eol``/``crlf``/``ident``/
  ``working-tree-encoding`` attribute, the checked-out bytes differ from the
  raw blob; the first cut compared raw blobs, so it both passed files git
  would restore with DIFFERENT line endings and refused files git would
  restore exactly. Such files are now read through ``git cat-file --filters
  <commit>:<path>`` (checkout's own conversion), one process each — the batch
  form, ``cat-file --batch --filters``, fails with "missing path" on the git
  shipped with macOS 27 (2.54, Apple Git-157), so it is not used. Files with
  no conversion (the common case) are compared by blob id, computed in
  process, which is exactly what checkout writes for them.
* **Filter drivers are refused.** A path with a ``filter`` attribute (Git LFS
  and the like) is refused without running the filter: its smudge step may
  reach the network, and its output is not ours to predict.
* **Symlinks are refused.** The writers replace a note with ``os.replace``,
  which replaces a symbolic link with a regular file; git cannot restore the
  link's pre-write state from the target's blob. The final path component is
  checked with ``lstat`` (a symlinked FOLDER above the note is fine).
* File names are listed with ``ls-tree -z`` and read with one ``cat-file
  --filters`` per file (no ``--batch``, see above), so a newline in a note's
  NAME needs no special case. A newline in a PARENT directory's path is not
  handled explicitly: ``rev-parse --show-prefix`` prints the prefix on one
  line, so such a folder yields a wrong prefix, the note is not found in the
  listing, and it is refused as not committed — fail-closed, never a false
  "restorable" (corrected 2026-09-23: this bullet used to claim a
  ``--batch`` refusal the code does not make).

The index is never consulted: a change staged but not committed is not in the
named commit and therefore not an undo.

Concurrency: this reads git state once. A commit, checkout or ``git rm`` made
between this check and the write could move ``HEAD`` away from the named
commit, but the named commit object still exists, so the instruction "restore
from <commit>" stays correct. A history rewrite that drops that commit
afterwards is outside what an app can protect against, which is why the
commit is named rather than "HEAD". A change to ``core.autocrlf`` or
``.gitattributes`` between the check and a later restore changes what
checkout writes; the restore instruction is exact only under the
configuration in force at check time.
"""

from __future__ import annotations

import hashlib
import subprocess
import unicodedata
from collections import defaultdict
from pathlib import Path

#: ``ls-tree`` pathspecs are passed on the argv. 400 vault-relative note paths
#: stay two orders of magnitude under macOS's ARG_MAX even with long names.
_CHUNK = 400
_TIMEOUT_S = 30
#: How many offending files a refusal names before summarising the rest.
_NAME_AT_MOST = 5


def _run(cwd: str | Path, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, input=stdin,
                          timeout=_TIMEOUT_S, check=False)


def _blob_id(data: bytes, object_format: str) -> str:
    """The id git gives *data* as a blob, without spawning ``hash-object``."""
    h = hashlib.sha256() if object_format == "sha256" else hashlib.sha1(usedforsecurity=False)
    h.update(b"blob %d\x00" % len(data))
    h.update(data)
    return h.hexdigest()


#: Attributes that change the bytes checkout writes (filter is refused outright).
_CONVERSION_ATTRS = ("text", "eol", "crlf", "ident", "working-tree-encoding")


def check(vault_root: Path, files: dict[Path, str]) -> tuple[bool, str, str | None]:
    """``(ok, detail, commit)`` for replacing each of *files* (path → planned text).

    *files* maps each file the caller will REPLACE to the exact text it read
    and plans to replace (encoded as UTF-8, which is how every notesync writer
    reads a note: ``read_bytes().decode("utf-8")``, strict). An empty mapping
    needs no undo and returns ``(True, "", None)`` without running git — a
    sweep that only creates notes (journalled) must not require a repository.
    *vault_root* is kept for the callers' signature; each file is located from
    its own directory.
    """
    del vault_root  # location is derived per file, from git (see module doc)
    if not files:
        return True, "", None
    problems: list[str] = []
    groups: dict[str, list[Path]] = defaultdict(list)
    for raw_path in files:
        path = Path(raw_path)
        if path.is_symlink():
            problems.append(f"{path.name} (a symbolic link — a rewrite would replace the "
                            "link itself, which git cannot restore)")
            continue
        groups[str(path.parent)].append(path)

    top: str | None = None
    located: list[tuple[str, Path]] = []  # (repo name as git's prefix spells it, path)
    try:
        for parent, paths in groups.items():
            loc = _run(parent, "rev-parse", "--show-toplevel", "--show-prefix")
            if loc.returncode != 0:
                if top is None and not located:
                    return False, "the vault is not a git repository (or git failed)", None
                problems.extend(f"{p.name} (not in the vault's git repository)" for p in paths)
                continue
            lines = loc.stdout.decode("utf-8", "surrogateescape").split("\n")
            here, prefix = lines[0], (lines[1] if len(lines) > 1 else "")
            if top is None:
                top = here
            elif here != top:
                problems.extend(f"{p.name} (in a different git repository)" for p in paths)
                continue
            located.extend((prefix + p.name, p) for p in paths)
        if top is None:
            return False, "; ".join(problems) or "nothing to check", None
        head = _run(top, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
        if head.returncode != 0:
            return False, "the vault's git repository has no commit yet", None
        commit = head.stdout.decode("ascii", "replace").strip()

        def config(key: str, *extra: str) -> str:
            got = _run(top, "config", *extra, "--get", key)
            return got.stdout.decode("utf-8", "replace").strip()

        precompose = config("core.precomposeunicode", "--type=bool") == "true"
        autocrlf = config("core.autocrlf").lower() in ("true", "input", "1", "yes", "on")
        fmt = _run(top, "rev-parse", "--show-object-format")
        object_format = (fmt.stdout.decode("ascii", "replace").strip()
                         if fmt.returncode == 0 else "sha1") or "sha1"

        def norm(name: str) -> str:
            return unicodedata.normalize("NFC", name) if precompose else name

        wanted = {norm(n): p for n, p in located}
        recorded: dict[str, tuple[str, str, str]] = {}   # norm → (mode, oid, stored)
        request = sorted(n for n, _p in located)
        for i in range(0, len(request), _CHUNK):
            # `--full-tree` + top-relative pathspecs: the listing does not depend
            # on where inside the repository the vault sits. `--literal-pathspecs`
            # stops a note named `[x].md` from being read as a glob.
            proc = _run(top, "--literal-pathspecs", "ls-tree", "-z", "--full-tree",
                        commit, "--", *request[i:i + _CHUNK])
            if proc.returncode != 0:
                return False, "git could not list the commit", commit
            for entry in proc.stdout.split(b"\x00"):
                if not entry:
                    continue
                meta, _tab, name_b = entry.partition(b"\t")
                mode, _type, oid = meta.decode("ascii", "replace").split(" ", 2)
                stored = name_b.decode("utf-8", "surrogateescape")
                recorded[norm(stored)] = (mode, oid, stored)

        present: list[tuple[str, str, Path]] = []        # (stored, oid, path)
        for key in sorted(wanted):
            path = wanted[key]
            entry = recorded.get(key)
            if entry is None:
                problems.append(f"{path.name} (not committed — untracked or git-ignored)")
                continue
            mode, oid, stored = entry
            if mode not in ("100644", "100755"):
                problems.append(f"{path.name} (not a regular file in git)")
                continue
            present.append((stored, oid, path))

        if present:
            attrs = _run(top, "check-attr", "-z", "--stdin", "filter", *_CONVERSION_ATTRS,
                         stdin=b"".join(s.encode("utf-8", "surrogateescape") + b"\x00"
                                        for s, _o, _p in present))
            if attrs.returncode != 0:
                return False, "git could not read the files' attributes", commit
            fields = attrs.stdout.split(b"\x00")
            set_attrs: dict[str, set[str]] = defaultdict(set)
            for i in range(0, len(fields) - 2, 3):
                if fields[i + 2] not in (b"unspecified", b"unset"):
                    set_attrs[fields[i].decode("utf-8", "surrogateescape")].add(
                        fields[i + 1].decode("ascii", "replace"))
            for stored, oid, path in present:
                planned = files[path].encode("utf-8")
                marks = set_attrs.get(stored, set())
                if "filter" in marks:
                    problems.append(f"{path.name} (a git filter, e.g. LFS, would have to run "
                                    "to restore it)")
                    continue
                if autocrlf or marks:
                    # A checkout conversion applies: ask git what it writes.
                    got = _run(top, "cat-file", "--filters", f"{commit}:{stored}")
                    same = got.returncode == 0 and got.stdout == planned
                else:
                    same = _blob_id(planned, object_format) == oid
                if not same:
                    problems.append(f"{path.name} (differs from commit {commit[:12]} — "
                                    "edited since the last commit)")
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"git unavailable ({type(exc).__name__})", None

    if problems:
        shown = ", ".join(problems[:_NAME_AT_MOST])
        more = len(problems) - _NAME_AT_MOST
        tail = f", and {more} more" if more > 0 else ""
        return False, (f"{len(problems)} file(s) git could not restore: {shown}{tail}. "
                       "Commit the notes folder first so git can undo this."), commit
    return True, "", commit
