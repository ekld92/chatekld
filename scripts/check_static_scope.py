"""Shared application roots for compile, Bandit and mypy gates.

Private roots are omitted only when absent from the public artifact.
"""
from pathlib import Path
import py_compile
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_SCOPE = [
    "app.py", "launch.py", "cherqain.py", "api", "core", "rag", "services", "audit",
    "accession", "deckgen", "docgen", "vault_history", "tagging", "widget",
    "packaging", "cli", "mcp_server", "maintenance", "notesync", "zotcreate", "cleanup",
    "scripts/build_public_artifact.py", "scripts/check_mypy_ratchet.py",
    "scripts/check_dependency_audits.py", "scripts/check_static_scope.py",
]


def existing_scope():
    return [path for path in PRODUCTION_SCOPE if (ROOT / path).exists()]


def main():
    scope = existing_scope()
    if sys.argv[1:] == ['compile']:
        for name in scope:
            path = ROOT / name
            files = [path] if path.is_file() else path.rglob('*.py')
            for source in files:
                if 'tests' not in source.relative_to(ROOT).parts:
                    py_compile.compile(str(source), doraise=True)
        return 0
    if sys.argv[1:] == ['bandit']:
        return subprocess.call([sys.executable, '-m', 'bandit', '-q', '-r', *scope,
                                '-x', '*/tests/*', '-ll', '-ii',
                                '-b', '.bandit-baseline.json'], cwd=ROOT)
    raise SystemExit('usage: check_static_scope.py compile|bandit')


if __name__ == '__main__':
    raise SystemExit(main())
