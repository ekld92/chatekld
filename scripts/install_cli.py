"""Install the ``cherqain`` command (legacy ``papermind`` is optional) as a shell shim (2026-07-22 audit C6).

The repo deliberately has no ``[project]`` table (``pip install .`` stays
meaningless — see pyproject.toml), so a console-script entry point is not an
option without reversing that decision. Instead this writes a tiny executable
shim that execs the current interpreter against ``cli/main.py`` — the same
pattern ``scripts/install_mcp.py`` uses to register the MCP server.

Run it FROM THE PROJECT VENV (the shim hardcodes ``sys.executable``); re-run
after rebuilding the venv elsewhere. Idempotent: an existing shim (identified
by the marker line) is rewritten in place; any OTHER file at the target path is
refused unless ``--force``.
"""
import argparse
import os
import stat
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON_EXEC = sys.executable
CLI_PATH = os.path.join(REPO_ROOT, "cli", "main.py")

#: Marks a file as ours so re-runs are idempotent and collisions are detectable.
MARKER = "# papermind-shim"

SHIM_TEMPLATE = """#!/bin/sh
{marker}
exec "{python}" "{cli}" "$@"
"""


def default_bin_dir() -> str:
    return os.path.expanduser("~/.local/bin")


def install(bin_dir: str, force: bool, name: str = "papermind") -> int:
    target = os.path.join(bin_dir, name)
    if os.path.exists(target) and not force:
        try:
            existing = open(target, encoding="utf-8", errors="replace").read()
        except OSError:
            existing = ""
        if MARKER not in existing:
            print(f"ERROR: {target} exists and is not a papermind shim. "
                  f"Re-run with --force to overwrite it.", file=sys.stderr)
            return 1
    os.makedirs(bin_dir, exist_ok=True)
    cli_path = os.path.join(REPO_ROOT, "cherqain.py") if name == "cherqain" else CLI_PATH
    content = SHIM_TEMPLATE.format(marker=MARKER, python=PYTHON_EXEC, cli=cli_path)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(content)
    mode = os.stat(target).st_mode
    os.chmod(target, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"Installed: {target}")
    print(f"  interpreter: {PYTHON_EXEC}")
    print(f"  cli:         {cli_path}")
    on_path = bin_dir in os.environ.get("PATH", "").split(os.pathsep)
    if not on_path:
        print(f"\nNOTE: {bin_dir} is not on your PATH. Add it, e.g.:")
        print(f'  echo \'export PATH="{bin_dir}:$PATH"\' >> ~/.zshrc && exec zsh')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin-dir", default=default_bin_dir(),
                        help="Directory to install the shim into (default: ~/.local/bin)")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite a non-shim file at the target path")
    parser.add_argument("--name", choices=("cherqain", "papermind"), default="cherqain",
                        help="Command name (legacy papermind remains supported)")
    args = parser.parse_args()
    return install(os.path.expanduser(args.bin_dir), args.force, args.name)


if __name__ == "__main__":
    raise SystemExit(main())
