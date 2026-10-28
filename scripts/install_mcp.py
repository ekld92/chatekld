"""Convenience script to install and register CHERQAiN MCP server for Claude Code, Cursor, and OpenClaw.

Per-client behaviour mirrors what each client offers: Claude Code has a CLI
(`claude mcp add`), so we call it; Cursor has no registration CLI — its MCP
config is the JSON file `~/.cursor/mcp.json` — so we merge our entry into that
file (existing servers preserved, unparseable file left untouched with the
snippet printed instead); OpenClaw gets a printed snippet only.
"""
import json
import os
import sys
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON_EXEC = sys.executable
MCP_SERVER_PATH = os.path.join(REPO_ROOT, "mcp_server", "server.py")

# The stdio server entry every JSON-configured client uses (Cursor, OpenClaw —
# both share the {command, args, env} mcpServers shape).
SERVER_NAME = "papermind"


def server_entry() -> dict:
    """The mcpServers value for this checkout's interpreter + server script."""
    return {
        "command": PYTHON_EXEC,
        "args": [MCP_SERVER_PATH],
        "env": {
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
        },
    }


def merge_cursor_config(existing: dict) -> dict:
    """Return *existing* with our server entry set under mcpServers.

    Pure (no IO) so the merge is unit-testable: other registered servers and
    unrelated top-level keys are preserved; only the ``papermind`` entry is
    created or replaced (re-running the script after moving the repo or venv
    must refresh the stale interpreter/script paths).
    """
    merged = dict(existing)
    servers = dict(merged.get("mcpServers") or {})
    servers[SERVER_NAME] = server_entry()
    merged["mcpServers"] = servers
    return merged


def register_claude_code():
    """Register CHERQAiN MCP server with Claude Code via `claude mcp add`."""
    print("--> Registering CHERQAiN MCP server with Claude Code...")
    cmd = [
        "claude",
        "mcp",
        "add",
        "papermind",
        "--",
        PYTHON_EXEC,
        MCP_SERVER_PATH,
    ]
    try:
        subprocess.run(cmd, check=True)
        print("Successfully registered papermind MCP server with Claude Code!")
    except FileNotFoundError:
        print("Note: 'claude' CLI not found on PATH. Manual config for Claude Code:")
        print(f"  claude mcp add papermind -- {PYTHON_EXEC} {MCP_SERVER_PATH}")
    except subprocess.CalledProcessError as e:
        print(f"Warning: Registration command failed with code {e.returncode}")


def register_cursor(config_path: str | None = None) -> bool:
    """Merge the papermind entry into Cursor's global MCP config file.

    Cursor reads ``~/.cursor/mcp.json`` (global) or ``<project>/.cursor/mcp.json``
    (per-project); we register globally so the tools are available from any
    workspace, mirroring the Claude Code registration's scope. Conservative IO:
    an existing file that fails to parse is NEVER overwritten — the snippet is
    printed for a manual merge instead. Returns True when the file was written.
    """
    path = config_path or os.path.join(os.path.expanduser("~"), ".cursor", "mcp.json")
    # A dotfiles-managed ~/.cursor/mcp.json is often a SYMLINK; os.replace on
    # the link path would swap the link itself for a regular file, silently
    # detaching the config from the dotfiles repo. Resolve first so the atomic
    # replace targets the real file (2026-08-08 review B4).
    path = os.path.realpath(path)
    print(f"--> Registering CHERQAiN MCP server with Cursor ({path})...")
    existing: dict = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if not isinstance(loaded, dict):
                raise ValueError(f"top-level JSON is {type(loaded).__name__}, not object")
            existing = loaded
        except (OSError, ValueError) as exc:
            print(f"Warning: could not read existing {path} ({exc}).")
            print("Not touching it. Merge this entry manually:")
            print(json.dumps({"mcpServers": {SERVER_NAME: server_entry()}}, indent=2))
            return False
    merged = merge_cursor_config(existing)
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)  # don't leave a half-written .tmp behind
        except OSError:
            pass
        print(f"Warning: could not write {path} ({exc}). Merge manually:")
        print(json.dumps({"mcpServers": {SERVER_NAME: server_entry()}}, indent=2))
        return False
    print("Successfully registered papermind MCP server with Cursor!")
    print("(Restart Cursor, then enable the server under Settings → MCP if prompted.)")
    return True


def print_openclaw_config():
    """Print configuration snippet for OpenClaw (openclaw.json / mcp)."""
    print("\n" + "=" * 60)
    print("OpenClaw MCP Integration Config (openclaw.json):")
    print("=" * 60)
    print(json.dumps({"mcpServers": {SERVER_NAME: server_entry()}}, indent=2))
    print("=" * 60 + "\n")


def check_interpreter():
    """Warn when this interpreter can't import the app's dependencies.

    2026-07-22 (audit L4): running this script with the SYSTEM python
    registered an MCP server that died at its first handshake (`import flask`
    via api.security). Registration still proceeds — the user may be about to
    install deps — but the warning names the venv fix.
    """
    import subprocess as _sp
    probe = _sp.run([PYTHON_EXEC, "-c", "import flask"], capture_output=True)
    if probe.returncode != 0:
        print("WARNING: this Python cannot `import flask` — the registered MCP "
              "server will fail its first handshake. Re-run this script with "
              "the project virtualenv's interpreter (<your-venv>/bin/python).")


def main():
    print(f"CHERQAiN Repo Root: {REPO_ROOT}")
    print(f"Python Interpreter: {PYTHON_EXEC}")
    print(f"MCP Server Path:    {MCP_SERVER_PATH}\n")
    check_interpreter()

    register_claude_code()
    register_cursor()
    print_openclaw_config()


if __name__ == "__main__":
    main()
