#!/bin/bash
# launch_gui.sh — open the vault maintenance control panel.
#
# This is the entry point for Vault Sync.app. It exists because a
# Finder-launched bundle inherits almost nothing useful:
#
#   * PATH is /usr/bin:/bin:/usr/sbin:/sbin, so `gh`, `pandoc`, `latexmk` and
#     `biber` are all invisible — every maintenance step would fail with
#     "not installed or not in PATH". (The old app hid this by going through
#     Terminal, which sources the shell profile; launching the GUI directly
#     does not, so the PATH is rebuilt here.)
#   * a bare `python3` may be any interpreter, including one without tkinter.
#
# Errors are surfaced as a native dialog: a GUI launcher that fails silently
# looks identical to a double-click that did nothing.

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUI="$HERE/maintain_gui.py"

# Homebrew + MacTeX, ahead of the inherited PATH. Mirrors the PATH augmentation
# the app itself does for the same Finder-minimal environment.
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/Library/TeX/texbin:$PATH"

die() {
    /usr/bin/osascript - "$1" <<'APPLESCRIPT' >/dev/null 2>&1
on run argv
    display dialog "Vault Maintenance could not start." & return & return & ¬
        (item 1 of argv) buttons {"OK"} default button 1 ¬
        with icon stop with title "Vault Maintenance"
end run
APPLESCRIPT
    echo "$1" >&2
    exit 2
}

[ -f "$GUI" ] || die "maintain_gui.py is missing from:
$HERE"

# First python3 that can actually open a window. /usr/bin/python3 is last but
# always present once the Xcode command line tools are installed.
PY=""
for candidate in "${PYTHON:-}" python3 /opt/homebrew/bin/python3 \
                 /usr/local/bin/python3 /usr/bin/python3; do
    [ -n "$candidate" ] || continue
    if command -v "$candidate" >/dev/null 2>&1 \
       && "$candidate" -c "import tkinter" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done

[ -n "$PY" ] || die "No python3 with tkinter was found.

Install the Xcode command line tools (xcode-select --install) or set PYTHON to an interpreter that has tkinter."

exec "$PY" "$GUI" "$@"
