#!/usr/bin/env python3
r"""Check LaTeX compilation and conversion to PPTX/DOCX.

Finds all .tex files containing '\documentclass' and '\begin{document}' in the
suite and attempts to compile them with latexmk and convert them with pandoc.
Produces a summary report at the end.

The suite resolves shared assets (cress-style.sty, _master.bib, common/fig
images) by bare name through TEXINPUTS/BIBINPUTS, normally set by `make` or
the suite-root .latexmkrc. latexmk does NOT walk up to find that rc file, so
we pass it explicitly with -r; pandoc gets the equivalent via --resource-path.
All build artifacts go to a temporary directory: this check is side-effect-free
and leaves nothing in the project folders.

Paths are resolved by `vault_paths` (CLI arg → env → ChatEKLD config → marker
discovery → built-in default) rather than hardcoded: the suite root is found by
its `.latexmkrc` / `common/latex-build.mk`, so renaming the suite folder — or
the vault above it — does not silently reduce the sweep to zero documents.

This is the slow half of vault maintenance (~98 root documents × up to three
builds each), so it is opt-in from `maintain.sh` and supports narrowing:

    check_tex.py --list                # enumerate root documents, build nothing
    check_tex.py --filter encephale    # only matching documents
    check_tex.py --filter 'cours/*'    # glob also works
    check_tex.py --no-pandoc           # PDF compile only, skip PPTX/DOCX
    check_tex.py --jobs 8              # widen the build pool

Exit codes: 0 = all checks passed, 1 = failures, 2 = setup error.
"""
import argparse
import fnmatch
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vault_paths import (  # noqa: E402
    VaultPathError,
    chatekld_config,
    resolve_suite,
    resolve_vault,
)

DEFAULT_TIMEOUT_S = 180


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes: 0 = all passed, 1 = failures, 2 = setup error.",
    )
    parser.add_argument("--vault", help="vault root (default: auto-resolved)")
    parser.add_argument("--suite", help="LaTeX suite root (default: auto-resolved)")
    parser.add_argument(
        "--list",
        action="store_true",
        help="list the root documents that would be checked, then exit",
    )
    parser.add_argument(
        "--filter",
        metavar="PAT",
        help="only check documents whose suite-relative path matches PAT "
        "(substring, or glob when it contains * ? [)",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=min(4, os.cpu_count() or 1),
        metavar="N",
        help="documents to build concurrently (default: %(default)s)",
    )
    parser.add_argument(
        "--no-pandoc",
        action="store_true",
        help="compile the PDF only; skip the PPTX and DOCX conversions",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_S,
        metavar="S",
        help="per-build timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="suppress the resolved-path banner"
    )
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be >= 1")
    if args.timeout < 1:
        parser.error("--timeout must be >= 1")
    return args


_DOCCLASS_NAME_RE = re.compile(r"^\s*\\documentclass(?:\[[^\]]*\])?\{([^}]+)\}")


def classify_document(filepath: Path) -> tuple[bool | None, bool]:
    """One read per file → ``(is_root, is_beamer)``.

    A compilable root document needs both \\documentclass and
    \\begin{document} — preamble-only files (e.g. cv/Preamble_CV.tex,
    \\input by wrapper documents) carry the former but not the latter.

    ``is_root`` is ``None`` when the file cannot be read at all — the caller
    must treat that as a FAILURE, not "not a root": the old silent exclusion
    let the run print "All checks passed" while a real root went unchecked
    (2026-07-23 audit).

    The beamer test rides the same pass (2026-07-29 audit 7): the old
    ``is_beamer`` re-opened and re-read every file the scan had just fully
    read — a second full-file pass per document over an iCloud-backed tree.
    Semantics preserved: like the old function, the FIRST brace-form
    \\documentclass line decides (later ones are ignored), and a non-beamer
    class ⇒ False. One deliberate improvement: latin-1 documents now get a
    real beamer answer (the old reader was utf-8-only and swallowed the
    decode error into False).
    """
    has_class = has_begin = False
    is_beamer = False
    beamer_decided = False
    try:
        try:
            text = filepath.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Legacy latin-1 documents are still valid roots; the ASCII
            # markers we scan for survive any single-byte decoding.
            text = filepath.read_text(encoding="latin-1")
    except OSError as e:
        print(f"  [WARNING] Could not read {filepath.name}: {e}")
        return None, False
    for line in text.splitlines():
        if re.search(r"^\s*\\documentclass", line):
            has_class = True
            if not beamer_decided:
                m = _DOCCLASS_NAME_RE.search(line)
                if m:
                    beamer_decided = True
                    is_beamer = m.group(1) == "beamer"
        if re.search(r"^\s*\\begin\{document\}", line):
            has_begin = True
        if has_class and has_begin:
            return True, is_beamer
    return False, is_beamer


def build_resource_path(suite: Path) -> str:
    """Assemble pandoc's --resource-path from what the suite actually has.

    The previous hardcoded list named `letters/common` explicitly; any project
    group added later (or a renamed one) silently lost its shared assets. Every
    `common/` directory within two levels is included instead.
    """
    parts = [".", str(suite / "common"), str(suite / "common" / "fig")]
    for child in sorted(suite.iterdir()) if suite.is_dir() else []:
        if not child.is_dir() or child.name.startswith("."):
            continue
        shared = child / "common"
        if shared.is_dir():
            parts.append(str(shared))
    seen, unique = set(), []
    for part in parts:
        if part not in seen:
            seen.add(part)
            unique.append(part)
    return ":".join(unique)


def run_command(cmd: list[str], cwd: Path, timeout: int, log: list[str]) -> bool:
    """Run a subprocess command and return True if exit code is 0.

    Output goes to DEVNULL by design: this is a pass/fail sweep over dozens
    of documents — per-file diagnostics belong to a manual `make` in that
    document's folder, not interleaved into a summary report.

    The child runs in ITS OWN process group with TERM→KILL escalation on
    timeout, mirroring deckgen/compile.py's run_latexmk (2026-07-29 audit 7):
    ``subprocess.run(timeout=)`` kills only the direct child, so a timed-out
    latexmk left its pdflatex/biber grandchildren running — with --jobs 8
    over ~98 documents a wedged TeX install accumulated orphans that then
    raced the enclosing TemporaryDirectory's cleanup. Non-timeout exits are
    untouched: the pass/fail contract is still exactly ``returncode == 0``.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.append(f"  [TIMEOUT after {timeout}s] {' '.join(cmd)}")
            # Kill the whole group, not just latexmk (see docstring). TERM
            # first so latexmk can clean its own children; KILL after 5 s for
            # a TeX process wedged past caring.
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
            return False
        return proc.returncode == 0
    except Exception as e:
        log.append(f"  [ERROR] {e}")
        return False


def _check_file_trust(target: Path) -> str:
    """Refuse a vault-resident file the current user does not own, or that
    group/other can write. Returns ``""`` when trusted, else the refusal.

    Deliberate copy of ``maintain_gui.py::_check_script_trust`` rather than an
    import: that module pulls in tkinter at import time and this is a headless
    CLI. Keep the two in sync — same trust boundary, same limits (an
    iCloud-synced overwrite by the user's own account still passes; this bounds
    the attacker set to "can already write as this user").
    """
    try:
        st = target.stat()
    except OSError as exc:
        return f"cannot stat {target}: {exc}"
    if st.st_uid != os.getuid():
        return (f"{target} is not owned by the current user (uid {st.st_uid} "
                f"vs {os.getuid()}) — refusing to execute it.")
    if st.st_mode & 0o022:
        return (f"{target} is group/world-writable "
                f"(mode {st.st_mode & 0o777:o}) — `chmod go-w` it first.")
    return ""


def matches_filter(rel: Path, pattern: str | None) -> bool:
    if not pattern:
        return True
    text = str(rel)
    if any(ch in pattern for ch in "*?["):
        return fnmatch.fnmatch(text, pattern) or fnmatch.fnmatch(rel.name, pattern)
    return pattern.lower() in text.lower()


def check_one(
    filepath: Path,
    suite: Path,
    outdir: Path,
    *,
    latexmkrc_args: list[str],
    beamer_lua: Path,
    resource_path: str,
    has_latexmk: bool,
    has_pandoc: bool,
    timeout: int,
    beamer: bool = False,
) -> dict:
    """Build one document into its own temp outdir. Safe to run concurrently.

    ``beamer`` comes from the scan's single classify pass — see
    ``classify_document`` for why this is a parameter and not a re-read.

    Trust note (2026-07-29 audit): ``beamer_lua`` is a Lua filter READ FROM
    THE SUITE and executed by pandoc — vault-resident code run by design,
    trusted as the user's own suite content (same standing as maintain.sh).
    Since 2026-07-30 it carries the SAME ownership/write-bit check
    maintain.sh's execution does (:func:`_check_file_trust`): an untrusted
    filter is skipped with a note in that document's log, and the conversion
    still runs without it, rather than being handed to pandoc unchecked.
    """
    rel = filepath.relative_to(suite)
    cwd = filepath.parent
    filename = filepath.name
    basename = filepath.stem
    outdir.mkdir(parents=True, exist_ok=True)

    log: list[str] = []
    pdf_ok = pptx_ok = docx_ok = None  # None = not attempted

    # 1. Compile with latexmk (suite rc file sets TEXINPUTS/BIBINPUTS).
    # -no-shell-escape: the app's own compile discipline (deckgen/compile.py) —
    # a vault .tex is semi-trusted content and \write18 must not run under
    # whatever the TeX install defaults to.
    # $max_repeat: MIRRORS common/latex-build.mk, which already raises it to 7
    # because the PhD thesis (deep front/back matter + ToC/LoF/LoT + hyperref +
    # biblatex) needs a 6th pass to stabilise. Leaving latexmk's default of 5
    # here made this check fail a document that `make` builds cleanly — the
    # same class of bug as the pre-2026-07-10 deckgen compile env, where the
    # checker's flags drifted from the suite's real build settings.
    if has_latexmk:
        pdf_ok = run_command(
            [
                "latexmk",
                *latexmkrc_args,
                "-pdf",
                "-no-shell-escape",
                "-interaction=nonstopmode",
                "-e",
                "$max_repeat=7",
                f"-outdir={outdir}",
                filename,
            ],
            cwd,
            timeout,
            log,
        )

    # 2. Convert to PPTX / DOCX
    if has_pandoc:
        pandoc_base = ["pandoc", filename, "--resource-path", resource_path]
        pptx_cmd = pandoc_base + ["-o", str(outdir / f"{basename}.pptx")]
        if beamer and beamer_lua.exists():
            trust_err = _check_file_trust(beamer_lua)
            if trust_err:
                log.append(f"SKIPPED --lua-filter: {trust_err}")
            else:
                pptx_cmd += ["--slide-level=1", f"--lua-filter={beamer_lua}"]
        pptx_ok = run_command(pptx_cmd, cwd, timeout, log)
        docx_ok = run_command(
            pandoc_base + ["-o", str(outdir / f"{basename}.docx")], cwd, timeout, log
        )

    return {"file": rel, "pdf": pdf_ok, "pptx": pptx_ok, "docx": docx_ok, "log": log}


def mark(value: bool | None) -> str:
    if value is None:
        return "–"
    return "✅" if value else "❌"


def main(argv=None):
    args = parse_args(argv)

    cfg = chatekld_config()
    try:
        vault = resolve_vault(args.vault, cfg)
        suite = resolve_suite(vault.path, args.suite, cfg)
    except VaultPathError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    suite_dir = suite.path
    latexmkrc = suite_dir / ".latexmkrc"
    beamer_lua = suite_dir / "common" / "beamer_frames.lua"
    resource_path = build_resource_path(suite_dir)

    if not args.quiet:
        print(f"Vault: {vault.describe()}")
        print(f"Suite: {suite.describe()}")
    print(f"Scanning {suite_dir} for root .tex files...\n")

    tex_files = []
    beamer_flags: dict[Path, bool] = {}
    unreadable = []
    filtered_out = 0
    for filepath in suite_dir.rglob("*.tex"):
        root, beamer = classify_document(filepath)
        if root:
            if matches_filter(filepath.relative_to(suite_dir), args.filter):
                tex_files.append(filepath)
                beamer_flags[filepath] = beamer
            else:
                filtered_out += 1
        elif root is None:
            # Unreadable ≠ not-a-root: count it as a failure so the run can
            # never report "All checks passed" over an unchecked document.
            unreadable.append(filepath)

    tex_files.sort()

    if args.list:
        for filepath in tex_files:
            print(filepath.relative_to(suite_dir))
        note = f" ({filtered_out} filtered out)" if filtered_out else ""
        print(f"\n{len(tex_files)} root .tex file(s){note}.")
        return 0

    if not tex_files:
        if args.filter:
            print(
                f"[ERROR] No root documents match --filter {args.filter!r} "
                f"({filtered_out} root document(s) exist but were filtered out).",
                file=sys.stderr,
            )
        else:
            # Zero roots in a resolved suite means the resolution is wrong, not
            # that everything passed. Fail instead of printing a green summary.
            print(
                f"[ERROR] No root .tex documents found under {suite_dir}.\n"
                f"  Resolution rule: {suite.source}\n"
                f"  If the suite moved, pass --suite <path> or set SUITE_DIR.",
                file=sys.stderr,
            )
        return 2

    has_latexmk = shutil.which("latexmk") is not None
    has_pandoc = shutil.which("pandoc") is not None and not args.no_pandoc

    if not has_latexmk:
        print("[WARNING] latexmk is not installed or not in PATH.", file=sys.stderr)
    if args.no_pandoc:
        print("Skipping PPTX/DOCX conversion (--no-pandoc).")
    elif shutil.which("pandoc") is None:
        print("[WARNING] pandoc is not installed or not in PATH.", file=sys.stderr)

    if not has_latexmk and not has_pandoc:
        print(
            "[ERROR] Neither latexmk nor pandoc is available — nothing to check.",
            file=sys.stderr,
        )
        return 2

    # A missing rc file would make latexmk error on EVERY file with all
    # diagnostics DEVNULL'd — check once here instead (the suite compiles are
    # then attempted without it; TEXINPUTS-dependent ones will fail visibly).
    latexmkrc_args = ["-r", str(latexmkrc)] if latexmkrc.exists() else []
    if has_latexmk and not latexmkrc_args:
        print(
            f"[WARNING] {latexmkrc} not found — compiling without the suite "
            "rc file (shared-asset documents will fail).",
            file=sys.stderr,
        )

    total = len(tex_files)
    note = f" ({filtered_out} filtered out)" if filtered_out else ""
    builds = sum([has_latexmk, has_pandoc, has_pandoc])
    print(
        f"Found {total} root .tex file(s) to check{note} — "
        f"{total * builds} build(s), {args.jobs} at a time.\n"
    )

    results = []
    done = 0
    with tempfile.TemporaryDirectory(prefix="check_tex_") as tmp:
        tmp_root = Path(tmp)

        def task(indexed):
            i, filepath = indexed
            return check_one(
                filepath,
                suite_dir,
                tmp_root / str(i),
                latexmkrc_args=latexmkrc_args,
                beamer_lua=beamer_lua,
                resource_path=resource_path,
                has_latexmk=has_latexmk,
                has_pandoc=has_pandoc,
                timeout=args.timeout,
                beamer=beamer_flags.get(filepath, False),
            )

        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            # No print lock: pool.map yields results on THIS (main) thread
            # only — the workers never print. The old _PRINT_LOCK guarded a
            # section with exactly one possible holder (2026-07-29 audit).
            for res in pool.map(task, enumerate(tex_files)):
                results.append(res)
                done += 1
                flags = " ".join(
                    f"{label} {mark(res[key])}"
                    for label, key in (("PDF", "pdf"), ("PPTX", "pptx"), ("DOCX", "docx"))
                    if res[key] is not None
                )
                print(f"[{done}/{total}] {res['file']} — {flags}")
                for line in res["log"]:
                    print(line)
                sys.stdout.flush()

    results.sort(key=lambda r: str(r["file"]))
    has_failures = bool(unreadable) or any(
        res[key] is False for res in results for key in ("pdf", "pptx", "docx")
    )

    # Print Summary
    print()
    print("=" * 80)
    print("SUMMARY REPORT")
    print("=" * 80)
    print(f"{'File':<50} | {'PDF':<5} | {'PPTX':<5} | {'DOCX':<5}")
    print("-" * 80)
    for res in results:
        print(
            f"{str(res['file']):<50} | {mark(res['pdf']):<5} | "
            f"{mark(res['pptx']):<5} | {mark(res['docx']):<5}"
        )
    print("=" * 80)

    failed = [
        res
        for res in results
        if any(res[key] is False for key in ("pdf", "pptx", "docx"))
    ]
    if failed:
        print(f"\n{len(failed)} of {total} document(s) failed:", file=sys.stderr)
        for res in failed:
            broken = [
                label
                for label, key in (("PDF", "pdf"), ("PPTX", "pptx"), ("DOCX", "docx"))
                if res[key] is False
            ]
            print(f"  - {res['file']} ({', '.join(broken)})", file=sys.stderr)
        print(
            "\nRe-run one of them with diagnostics: "
            "cd <its folder> && make",
            file=sys.stderr,
        )

    if unreadable:
        print(
            f"\n[WARNING] {len(unreadable)} .tex file(s) could not be read "
            "and were NOT checked:",
            file=sys.stderr,
        )
        for p in unreadable:
            print(f"  - {p.relative_to(suite_dir)}", file=sys.stderr)

    if has_failures:
        print("\n[WARNING] Some files failed to compile or convert.", file=sys.stderr)
        return 1

    print("\nAll checks passed! 🎉")
    return 0


if __name__ == "__main__":
    sys.exit(main())
