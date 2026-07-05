#!/usr/bin/env bash
# ChatEKLD — offline model seed helper (ships inside the .dmg).
#
# WHY: the tiktoken / NLTK / cross-encoder-reranker caches live OUTSIDE the .app
# bundle (under ~/Library/Application Support/ChatEKLD and ~/.cache/huggingface).
# A fresh Mac that only received the app has none of them, so its first OFFLINE
# vault index fails and its first chat stalls on a reranker download. This helper
# runs the app's OWN frozen binary in `--seed-models` mode (reusing the bundled
# tiktoken/nltk/sentence-transformers) to download them once. Run it once while
# online; afterwards the app indexes and chats fully offline.
#
# Double-click it in Finder (or right-click -> Open the first time, since it comes
# from an unsigned .dmg), or run it from Terminal.
set -euo pipefail

# Locate the ChatEKLD app: prefer an installed copy in /Applications, else look
# next to this script (e.g. run straight from the mounted .dmg). If several dated
# bundles exist, take the newest by name (ChatEKLD_YYYY-MM-DD sorts chronologically).
find_app() {
    local dir hit
    for dir in "/Applications" "$(cd "$(dirname "$0")" && pwd)"; do
        hit=$(ls -d "$dir"/ChatEKLD_*.app 2>/dev/null | sort | tail -1 || true)
        if [[ -n "$hit" && -d "$hit" ]]; then
            printf '%s\n' "$hit"
            return 0
        fi
    done
    return 1
}

APP="$(find_app || true)"
if [[ -z "${APP:-}" ]]; then
    echo "Could not find ChatEKLD_*.app in /Applications or next to this file."
    echo "Drag the app to your Applications folder first, then run this again."
    read -r -p "Press Return to close." _ || true
    exit 1
fi

# The inner Mach-O binary is named after the bundle (…/Contents/MacOS/<name>).
BIN="$APP/Contents/MacOS/$(basename "$APP" .app)"
if [[ ! -x "$BIN" ]]; then
    echo "Found $APP but its executable is missing or not runnable:"
    echo "  $BIN"
    read -r -p "Press Return to close." _ || true
    exit 1
fi

echo "Seeding offline models for:"
echo "  $APP"
echo "(this downloads ~70 MB once and needs an internet connection)"
echo

# Strip the quarantine flag so Gatekeeper does not block executing the inner binary
# from a just-transferred app. Harmless if the app is already un-quarantined; this
# also doubles as the one-time "approve it" step for the app itself.
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

# Run the seed pass. `|| STATUS=$?` keeps `set -e` from aborting before we can
# report a non-zero exit to the user.
STATUS=0
"$BIN" --seed-models || STATUS=$?

echo
if [[ "$STATUS" -eq 0 ]]; then
    echo "Done. ChatEKLD can now index and chat fully offline."
else
    echo "Finished with errors (exit $STATUS). Re-run while online; details are in the app log"
    echo "at ~/Library/Application Support/ChatEKLD/chatekld.log."
fi
read -r -p "Press Return to close this window." _ || true
exit "$STATUS"
