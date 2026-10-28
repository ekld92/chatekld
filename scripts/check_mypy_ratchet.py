#!/usr/bin/env python3
"""Fail on new mypy findings while preserving the explicit legacy budget.

The remediation surface is held to zero findings. The repository-wide pass is
still permissive, but every pre-existing file has an explicit maximum; a new
file/error or an increased count fails CI while genuine burn-down passes and
can be removed from the baseline in the same change.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BASELINE = ROOT / ".mypy-legacy-baseline.json"
TOUCHED = ROOT / "scripts" / "mypy_remediation_files.txt"
ERROR_RE = re.compile(r"^(.+?):\d+(?::\d+)?: error:")
from scripts.check_static_scope import PRODUCTION_SCOPE  # noqa: E402


def _run(paths: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", *paths,
         "--show-error-codes", "--no-error-summary"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return proc.returncode, proc.stdout


def _counts(output: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for line in output.splitlines():
        match = ERROR_RE.match(line)
        if match:
            counts[match.group(1)] += 1
    return counts


def main() -> int:
    remediation_files = [
        line.strip() for line in TOUCHED.read_text(encoding="utf-8").splitlines()
        if line.strip() and (ROOT / line.strip()).is_file()
    ]
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    # One invocation covers every production subtree, including optional
    # private Maintenance when present. Missing optional roots are omitted so
    # this same gate verifies the stripped public artifact. Running separate
    # overlapping graphs doubled CI time without strengthening the ratchet.
    production_scope = [path for path in PRODUCTION_SCOPE if (ROOT / path).exists()]
    legacy_rc, legacy_output = _run(production_scope)
    current = _counts(legacy_output)
    if legacy_rc not in (0, 1):
        print(legacy_output, end="")
        print(f"mypy exited unexpectedly with {legacy_rc}", file=sys.stderr)
        return 1

    dirty_remediation = {
        path: current[path] for path in remediation_files if current[path]
    }
    if dirty_remediation:
        print(legacy_output, end="")
        for path, count in sorted(dirty_remediation.items()):
            print(f"mypy remediation surface: {path}: found {count}",
                  file=sys.stderr)
        return 1

    regressions = {
        path: (baseline.get(path, 0), count)
        for path, count in current.items()
        if count > baseline.get(path, 0)
    }
    if regressions:
        print(legacy_output, end="")
        for path, (allowed, found) in sorted(regressions.items()):
            print(f"mypy ratchet: {path}: allowed {allowed}, found {found}",
                  file=sys.stderr)
        return 1

    legacy_total = sum(current.values())
    budget_total = sum(baseline.values())
    print(f"mypy remediation surface: {len(remediation_files)} files, 0 errors")
    print(f"mypy legacy ratchet: {legacy_total}/{budget_total} errors; no increase")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
