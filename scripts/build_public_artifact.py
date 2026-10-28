#!/usr/bin/env python3
"""Build and verify a deterministic public-release tree without touching Git.

The historical publishing recipe switched branches and removed private files
from the developer's working tree. This builder instead inventories only Git-
visible source files, copies them into a sibling staging directory, applies the
documented strips there, verifies the reduced build, and atomically publishes
the completed directory. It never commits, pushes, changes branches, or edits
the source checkout.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit


PRIVATE_PREFIXES = (
    ".github/workflows/",
    "docs/",
    "screenshots/field-test-2026-10-28/",
    "maintenance/",
    "cleanup/",
    "campaigns/",
    "prototypes/",
    ".codex/",
    ".claude/",
    "scripts/vault_maintain/",
    "scripts/batch_tagging/",
)
PRIVATE_FILES = frozenset({
    ".github/dependabot.yml",
    "screenshots/obsidian-agent.png",
    "screenshots/library-audit.png",
    "screenshots/deck-generator.png",
    "screenshots/single-paper.png",
    "screenshots/help-guide.png",
    "api/routes/maintenance.py",
    "core/candidate_docling.py",
    "core/candidate_evaluation.py",
    "core/candidate_evidence.py",
    "core/candidate_extraction.py",
    "core/candidate_generation.py",
    "core/candidate_index.py",
    "core/candidate_reading_order.py",
    "core/candidate_rendition.py",
    "core/candidate_structure.py",
    "core/extraction_profiles.py",
    "scripts/eval_candidate_synthetic.py",
    "scripts/eval_reviewed_development.py",
    "scripts/inspect_extraction_consensus.py",
    "scripts/inspect_extraction_geometry.py",
    "scripts/inspect_extraction_rules.py",
    "scripts/practical_retrieval.py",
    "scripts/replay_candidate_rendition.py",
    "scripts/run_candidate_docling_page.py",
    "tests/test_integrity_cleanup_adversarial.py",
    "tests/test_candidate_docling.py",
    "tests/test_candidate_evidence.py",
    "tests/test_candidate_extraction.py",
    "tests/test_candidate_generation.py",
    "tests/test_candidate_index.py",
    "tests/test_candidate_reading_order.py",
    "tests/test_candidate_rendition.py",
    "tests/test_candidate_structure.py",
    "tests/test_extraction_consensus.py",
    "tests/test_extraction_geometry.py",
    "tests/test_extraction_profiles.py",
    "tests/test_extraction_rules.py",
    "tests/test_practical_retrieval.py",
    "tests/test_reviewed_development.py",
    "static/js/maintenance.js",
    "tests/js/maintenance.test.js",
    "tests/js/maintenanceStatusLine.test.js",
    "tests/js/zoteroMergePrototype.test.js",
    "tests/js/zoteroNativeAuthorization.test.js",
    "tests/js/zoteroNativeAuthorizationController.test.js",
    "tests/js/zoteroNativeReviewSurface.test.js",
    "tests/js/zoteroReservedPrototype.test.js",
})
EPHEMERAL_PREFIXES = (".tmp",)
UNVERIFIED_PREFIX = "**Not verified — the review"
MANIFEST_NAME = "PUBLIC_ARTIFACT_MANIFEST.json"
_VERIFICATION_DIRS = frozenset({
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", "node_modules",
})


def is_private_path(rel: str) -> bool:
    path = PurePosixPath(rel).as_posix()
    return path in PRIVATE_FILES or any(path.startswith(p) for p in PRIVATE_PREFIXES)


def strip_private_claude_line(source: str) -> str:
    lines = source.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if line.startswith(UNVERIFIED_PREFIX)]
    if not hits:
        # The private paragraph has already been removed from current source;
        # retaining the zero-hit case makes the strip idempotent without
        # weakening it. A reintroduced marker is still removed below.
        return source
    if len(hits) != 1:
        raise ValueError(
            f"expected exactly one private CLAUDE.md lead line, found {len(hits)}"
        )
    del lines[hits[0]]
    return "".join(lines)


def strip_private_links(source: str, relative: str) -> str:
    """Keep reference labels, but never ship links to deliberately omitted files.

    Transform copied Markdown only, before manifest hashing. Public links still
    go through check_docs; this is not a missing-link exemption for that gate.
    """
    pattern = re.compile(r"`+[^`]*`+|!?\[([^\]]+)\]\(([^)]+)\)")

    def replace(match):
        if match.group(1) is None:  # inline code
            return match.group(0)
        url = urlsplit(match.group(2).strip().strip("<>"))
        if url.scheme or url.netloc or not url.path:
            return match.group(0)
        target = posixpath.normpath(posixpath.join(
            str(PurePosixPath(relative).parent), unquote(url.path)))
        if is_private_path(target):
            return match.group(1) + " (private reference omitted)"
        return match.group(0)

    lines = []
    fence = None
    for line in source.splitlines(keepends=True):
        marker = re.match(r"^\s*(```|~~~)", line)
        if marker:
            if fence is None:
                fence = marker.group(1)
            elif fence == marker.group(1):
                fence = None
            lines.append(line)
        else:
            lines.append(line if fence else pattern.sub(replace, line))
    return "".join(lines)


def git_visible_files(source: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=source,
        check=True,
        stdout=subprocess.PIPE,
    )
    files = [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]
    return sorted(
        rel for rel in files
        if not is_private_path(rel)
        and not rel.startswith(EPHEMERAL_PREFIXES)
    )


def _copy_visible_tree(source: Path, staging: Path, files: list[str]) -> None:
    for rel in files:
        src = source / rel
        if not src.is_file() and not src.is_symlink():
            raise ValueError(f"inventory path is not a file: {rel}")
        dst = staging / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst, follow_symlinks=False)

    claude = staging / "CLAUDE.md"
    claude.write_text(
        strip_private_claude_line(claude.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    for relative in files:
        if relative.endswith('.md'):
            path = staging / relative
            original = path.read_text(encoding='utf-8')
            rewritten = strip_private_links(original, relative)
            if rewritten != original:
                path.write_text(rewritten, encoding='utf-8')


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _locked_versions(path: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    if not path.is_file():
        return versions
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^ ;\\]+)", line)
        if match:
            versions[match.group(1).lower().replace("_", "-")] = match.group(2)
    return dict(sorted(versions.items()))


def _write_manifest(staging: Path) -> None:
    included = {}
    for path in sorted(staging.rglob("*")):
        if path.is_file() and path.name != MANIFEST_NAME:
            included[path.relative_to(staging).as_posix()] = _sha256(path)
    manifest = {
        "schema_version": 1,
        "included_paths": included,
        "dependency_versions": {
            "python_linux": _locked_versions(staging / "requirements-lock-linux.txt"),
            "python_macos": _locked_versions(staging / "requirements-lock-macos.txt"),
            "javascript": json.loads(
                (staging / "package-lock.json").read_text(encoding="utf-8")
            ).get("packages", {}).get("", {}).get("devDependencies", {}),
        },
    }
    (staging / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def verify_artifact(staging: Path) -> None:
    leaked = [
        path.relative_to(staging).as_posix()
        for path in staging.rglob("*")
        if path.is_file() and is_private_path(path.relative_to(staging).as_posix())
    ]
    if leaked:
        raise RuntimeError(f"private paths in artifact: {leaked}")
    if UNVERIFIED_PREFIX in (staging / "CLAUDE.md").read_text(encoding="utf-8"):
        raise RuntimeError("private unverified-leads text remains in artifact")

    compile_paths = [
        path.relative_to(staging).as_posix()
        for path in staging.rglob("*.py")
        if not any(part in {"tests", "node_modules"} for part in path.parts)
    ]
    # Importing app initialises its database and config. Verification must not
    # touch the maintainer's real BASE_DIR merely because the caller forgot an
    # environment override, so every child gets an isolated disposable base.
    # The directory is outside the deliverable and is removed on every exit.
    with tempfile.TemporaryDirectory(
        prefix=f".{staging.name}.runtime-", dir=staging.parent
    ) as runtime_dir:
        env = os.environ.copy()
        env["CHATEKLD_BASE_DIR"] = runtime_dir
        env["CHERQAIN_BASE_DIR"] = runtime_dir
        env["npm_config_cache"] = str(Path(runtime_dir) / "npm-cache")
        subprocess.run(
            [sys.executable, "scripts/check_docs.py", "--root", str(staging)],
            cwd=staging, env=env, check=True,
        )
        subprocess.run(
            [sys.executable, "-m", "py_compile", *compile_paths],
            cwd=staging, env=env, check=True,
        )
        subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import app; "
                    "assert app.maintenance_bp is None; "
                    "assert app.app.test_client().get("
                    "'/api/maintenance/steps', "
                    "headers={'X-Requested-With':'ChatEKLD'}).status_code == 404"
                ),
            ],
            cwd=staging, env=env, check=True,
        )
        subprocess.run(
            [sys.executable, "-m", "ruff", "check", "."],
            cwd=staging, env=env, check=True,
        )
        subprocess.run(
            [sys.executable, "scripts/check_static_scope.py", "bandit"],
            cwd=staging, env=env, check=True,
        )
        subprocess.run(
            [sys.executable, "scripts/check_mypy_ratchet.py"],
            cwd=staging, env=env, check=True,
        )
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=staging, env=env, check=True,
        )
        subprocess.run(
            ["npm", "ci", "--ignore-scripts"], cwd=staging, env=env,
            check=True, stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            ["npm", "run", "test:js", "--", "--run"], cwd=staging,
            env=env, check=True,
        )


def _cleanup_verification_artifacts(staging: Path) -> None:
    """Remove verifier caches so only manifested source is published.

    The targets are fixed names beneath the already-resolved staging tree, and
    the following manifest equality check fails closed if a future source input
    ever collides with one. This cleanup is required for persisted artifact
    compatibility: verification may populate caches differently by Python/npm
    version, but the output must remain the exact deterministic manifest tree.
    A failed cleanup aborts before the no-replace publication.
    """
    for path in sorted(staging.rglob("*"), key=lambda item: len(item.parts),
                       reverse=True):
        if path.is_dir() and path.name in _VERIFICATION_DIRS:
            shutil.rmtree(path)
        elif path.is_file() and path.suffix == ".pyc":
            path.unlink()


def _assert_manifest_tree(staging: Path) -> None:
    """Fail closed unless publication bytes exactly match the manifest."""
    manifest = json.loads((staging / MANIFEST_NAME).read_text(encoding="utf-8"))
    expected = manifest["included_paths"]
    actual = {
        path.relative_to(staging).as_posix(): _sha256(path)
        for path in staging.rglob("*")
        if path.is_file() and path.name != MANIFEST_NAME
    }
    if actual != expected:
        added = sorted(set(actual) - set(expected))
        missing = sorted(set(expected) - set(actual))
        changed = sorted(
            path for path in set(actual) & set(expected)
            if actual[path] != expected[path]
        )
        raise RuntimeError(
            "verified artifact diverged from manifest: "
            f"added={added[:10]}, missing={missing[:10]}, changed={changed[:10]}"
        )


def build_artifact(source: Path, output: Path, *, verify: bool = True) -> Path:
    source = source.resolve()
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"output already exists (refusing to replace): {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent))
    try:
        files = git_visible_files(source)
        _copy_visible_tree(source, staging, files)
        _write_manifest(staging)
        if verify:
            verify_artifact(staging)
            _cleanup_verification_artifacts(staging)
            _assert_manifest_tree(staging)
        # Same-filesystem directory rename is atomic and output was checked
        # absent; never remove or replace an existing release tree.
        _publish_noreplace(staging, output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return output


def _publish_noreplace(source: Path, destination: Path) -> None:
    """Atomically publish a directory, refusing even a late empty destination.

    Fail closed on platforms lacking an exclusive rename primitive. A second
    existence check followed by ordinary rename would retain the same race.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    src, dst = os.fsencode(source), os.fsencode(destination)
    if sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(src, dst, 0x00000004)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                           ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(-100, src, -100, dst, 1)  # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise RuntimeError("exclusive directory publication is unsupported")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new artifact directory (must not exist)")
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument(
        "--no-verify", action="store_true",
        help="copy and manifest only (development diagnostics; releases must verify)",
    )
    args = parser.parse_args()
    built = build_artifact(args.source, args.output, verify=not args.no_verify)
    print(built)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
