#!/usr/bin/env python3
"""Run the Python and npm vulnerability gates with time-bounded waivers.

A bare `pip-audit` / `npm audit --audit-level=high` hard-blocks every unrelated
change the moment a new upstream advisory lands, with no sanctioned way to say
"known, accepted until <date>, owned by <person>". That reliably ends with the
step being deleted. The remediation plan called for waivers with an owner,
rationale, affected path and expiry; this is that mechanism.

An EXPIRED waiver is a failure, not a pass: the point is that an exception
cannot become permanent through neglect. An empty waiver list is the correct
steady state.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WAIVERS = ROOT / ".security-waivers.json"
_REQUIRED = ("id", "owner", "rationale", "path", "expires")


def _lockfile() -> Path:
    """The hash-locked requirements file for THIS platform.

    pip-audit is pointed at the lockfile rather than left to audit the live
    environment, and the distinction is not cosmetic. Auditing the environment
    sweeps whatever else happens to be importable: on ubuntu-24.04 that meant
    the runner's own system packages, and the gate failed on `twisted`,
    `pyjwt`, `pyasn1`, `pyopenssl`, `httplib2`, `configobj` and `wheel` — none
    of which this project depends on — while its skip list named `cloud-init`,
    `ufw` and `walinuxagent` outright. 26 of 50 findings were about software we
    do not ship, the result was not reproducible on a developer machine, and CI
    stayed red for a week over it.

    The lockfile is the authoritative shipped graph (it is what CI installs,
    with --require-hashes), so auditing it makes the gate deterministic, the
    same locally as in CI, and about our own supply chain only.
    """
    name = ("requirements-lock-macos.txt" if sys.platform == "darwin"
            else "requirements-lock-linux.txt")
    return ROOT / name


def _load() -> tuple[list[dict], list[dict], list[str]]:
    if not WAIVERS.is_file():
        return [], [], []
    data = json.loads(WAIVERS.read_text(encoding="utf-8"))
    problems: list[str] = []
    today = date.today()
    for section in ("pip_audit", "npm_audit"):
        for entry in data.get(section, []):
            missing = [key for key in _REQUIRED if not entry.get(key)]
            if missing:
                problems.append(
                    f"{section} waiver {entry.get('id', '?')!r} is missing "
                    f"{', '.join(missing)}")
                continue
            try:
                expires = date.fromisoformat(str(entry["expires"]))
            except ValueError:
                problems.append(
                    f"{section} waiver {entry['id']!r} has an unparseable "
                    f"expiry {entry['expires']!r}")
                continue
            if expires < today:
                problems.append(
                    f"{section} waiver {entry['id']!r} expired on {expires} "
                    f"(owner: {entry['owner']}). Re-assess it or fix the "
                    "advisory; do not extend it silently.")
    return data.get("pip_audit", []), data.get("npm_audit", []), problems


def _run(argv: list[str]) -> tuple[int, str]:
    proc = subprocess.run(argv, cwd=ROOT, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          check=False)
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    return proc.returncode, proc.stdout


def main(*, no_resolve: bool = False) -> int:
    pip_waivers, npm_waivers, problems = _load()
    if problems:
        for line in problems:
            print(f"security waiver: {line}", file=sys.stderr)
        return 1

    failed = False

    lock = _lockfile()
    if not lock.is_file():
        print(f"pip-audit: {lock.name} is missing — the gate cannot assess "
              "what this platform ships", file=sys.stderr)
        return 1
    # `--require-hashes` rather than `--no-deps`: both stop pip-audit
    # re-resolving a file that is already fully resolved, but this one also
    # ASSERTS that every line carries its hashes — so a lockfile regenerated
    # without `--generate-hashes` fails the gate instead of being audited
    # silently as a plain pin list.
    argv = [sys.executable, "-m", "pip_audit", "--progress-spinner", "off",
            "--require-hashes", "-r", str(lock)]
    if no_resolve:
        # Audit pinned lock entries directly: no pip resolver or package install.
        argv += ["--disable-pip"]
    for entry in pip_waivers:
        argv += ["--ignore-vuln", entry["id"]]
    print(f"pip-audit: auditing {lock.name}")
    code, output = _run(argv)
    print(output, end="")
    if code != 0:
        print("pip-audit reported unwaived findings", file=sys.stderr)
        failed = True

    # npm audit has no ignore flag, so waived advisories are filtered from its
    # JSON report rather than from its exit code.
    code, output = _run(["npm", "audit", "--package-lock-only",
                         "--omit=optional", "--json"])
    try:
        report = json.loads(output)
    except ValueError:
        print(output, end="")
        print("npm audit produced unparseable output", file=sys.stderr)
        return 1
    # npm exits 1 both for vulnerabilities and for operational failures.
    # Only a complete audit report may be filtered through the waiver policy.
    if (not isinstance(report, dict) or report.get("error")
            or code not in (0, 1)
            or not isinstance(report.get("vulnerabilities"), dict)
            or any(not isinstance(item, dict)
                   or item.get("severity") not in
                   {"info", "low", "moderate", "high", "critical"}
                   for item in report["vulnerabilities"].values())
            or (code != 0 and not report["vulnerabilities"])):
        print("npm audit failed or returned an incomplete report", file=sys.stderr)
        return 1
    waived = {entry["id"] for entry in npm_waivers}
    blocking = [
        name for name, item in (report.get("vulnerabilities") or {}).items()
        if item.get("severity") in ("high", "critical") and name not in waived
    ]
    if blocking:
        print(f"npm audit: unwaived high/critical advisories: {blocking}",
              file=sys.stderr)
        failed = True
    else:
        print(f"npm audit: no unwaived high/critical advisories "
              f"({len(waived)} waived)")

    return 1 if failed else 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-resolve", action="store_true",
                        help="Audit pinned entries without pip resolution or package installation")
    raise SystemExit(main(no_resolve=parser.parse_args().no_resolve))
