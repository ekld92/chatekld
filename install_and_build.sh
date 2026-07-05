#!/usr/bin/env bash
# =============================================================================
# install_and_build.sh — Unified Installer & Builder for ChatEKLD
# =============================================================================
# Run this once to setup the environment and build the macOS .app bundle.
# =============================================================================

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
divider() { echo "------------------------------------------------------------"; }

divider
echo "  ChatEKLD — Unified Installer & Builder"
divider

# --- 1. System Check ---
if [[ "$(uname)" != "Darwin" ]]; then
    error "This script is designed for macOS. Please adapt it for your OS."
fi

# --- 1b. Architecture guard (arm64-only distribution) ---
# WHY: we ship an arm64-only bundle (see the --target-arch flag in the PyInstaller
# invocation below). PyInstaller freezes for the BUILD HOST's architecture; building
# on an Intel host would silently emit an x86_64 bundle we neither test nor support,
# and cross-building an arm64 bundle from an x86_64 host is not reliable with our
# native wheels (torch / lancedb / pyarrow / tokenizers do not all ship the required
# arm64 slices to a foreign host). Rosetta only translates Intel->Apple-Silicon, never
# the reverse, so an x86_64 artifact would ALSO fail to launch on Apple Silicon under
# translation the way an arm64 one fails on Intel. Fail fast with a clear message
# instead of producing an unsupported artifact. Intel Macs are documented as
# unsupported in the README.
HOST_ARCH="$(uname -m)"
if [[ "$HOST_ARCH" != "arm64" ]]; then
    error "ChatEKLD builds an arm64-only (Apple Silicon) bundle; this host is '$HOST_ARCH'. Build on an Apple Silicon Mac (Intel is unsupported — see README)."
fi

# --- 2. Dependencies (Homebrew, Ollama, Python) ---
if ! command -v brew &>/dev/null; then
    info "Installing Homebrew…"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

if ! command -v ollama &>/dev/null; then
    read -r -p "Ollama not found. Install it via Homebrew? (Optional, required if you don't use LM Studio) [Y/n] " REPLY
    if [[ "${REPLY:-Y}" =~ ^[Yy]$ ]]; then
        info "Installing Ollama…"
        brew install ollama
    else
        warn "Ollama installation skipped. Ensure you have LM Studio or another provider running."
    fi
fi

if ! command -v python3.12 &>/dev/null; then
    info "Installing Python 3.12…"
    brew install python@3.12
fi

# --- 3. Virtual Environment ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The venv lives OUTSIDE the repo on purpose: keeping ~2 GB of installed
# dependencies (torch, transformers, lancedb, …) out of the source tree avoids
# polluting git status and prevents PyInstaller from accidentally bundling the
# venv into the .app. Default to a portable $HOME-relative path so the installer
# works for any user (the previous hardcoded /Users/<name>/... path broke on
# every machine but the author's).
#
# Resolution order: CHATEKLD_VENV_DIR override → legacy PAPERMIND_VENV_DIR
# override → an EXISTING venv at the legacy default (~/venvs/papermind2026, kept
# so a pre-rename install is not needlessly rebuilt) → the new default
# ~/venvs/chatekld2026. NOTE: under `set -u` an unset HOME would abort here —
# acceptable, since a macOS terminal/Finder launch always sets HOME.
_NEW_DEFAULT_VENV="$HOME/venvs/chatekld2026"
_LEGACY_DEFAULT_VENV="$HOME/venvs/papermind2026"
if [[ -n "${CHATEKLD_VENV_DIR:-}" ]]; then
    VENV_DIR="$CHATEKLD_VENV_DIR"
elif [[ -n "${PAPERMIND_VENV_DIR:-}" ]]; then
    VENV_DIR="$PAPERMIND_VENV_DIR"
elif [[ -d "$_LEGACY_DEFAULT_VENV" ]]; then
    info "Using existing legacy venv at $_LEGACY_DEFAULT_VENV (pre-rename install)."
    VENV_DIR="$_LEGACY_DEFAULT_VENV"
else
    VENV_DIR="$_NEW_DEFAULT_VENV"
fi
APP_NAME="ChatEKLD_$(date +%Y-%m-%d)"

if [[ ! -d "$VENV_DIR" ]]; then
    info "Creating virtual environment with Python 3.12…"
    python3.12 -m venv "$VENV_DIR"
fi

# Recreate venv if its interpreter is missing or not Python 3.12.
if [[ -x "$VENV_DIR/bin/python3" ]]; then
    if ! "$VENV_DIR/bin/python3" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' >/dev/null 2>&1; then
        warn "Detected stale or incompatible virtual environment. Recreating venv with Python 3.12…"
        rm -rf "$VENV_DIR"
        python3.12 -m venv "$VENV_DIR"
    fi
fi

info "Installing dependencies…"
"$VENV_DIR/bin/pip" install --upgrade pip

# Apply the exact-version lock (constraints.txt) when present so a fresh install
# resolves the SAME validated versions the maintainer built against (torch /
# transformers / sentence-transformers in particular drift across majors). The
# human-maintained ranges live in requirements.txt; constraints.txt pins them.
if [[ -f "$SCRIPT_DIR/constraints.txt" ]]; then
    info "Applying version lock from constraints.txt"
    "$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" -c "$SCRIPT_DIR/constraints.txt"
else
    warn "constraints.txt not found — installing from requirements.txt ranges (versions may drift)."
    "$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"
fi

# typing is part of Python's standard library on Python 3.12.
# We intentionally do not attempt to uninstall a backport package here because
# pip prints a noisy warning when it is absent (the expected case), which can
# mislead users into thinking installation failed.

info "Pre-caching tiktoken encoding data (required for offline RAG)..."
# Pin the cache to the app's durable location (core.constants.TIKTOKEN_CACHE_DIR,
# which launch.py exports at startup) so the bundled .app finds it and macOS
# temp-dir eviction can't drop it.  PYTHONPATH lets the import resolve before
# the later `cd "$SCRIPT_DIR"`.
TIKTOKEN_CACHE_DIR="$(PYTHONPATH="$SCRIPT_DIR" "$VENV_DIR/bin/python3" -c 'from core.constants import TIKTOKEN_CACHE_DIR as d; print(d)' 2>/dev/null || true)"
if [[ -n "$TIKTOKEN_CACHE_DIR" ]]; then
    export TIKTOKEN_CACHE_DIR
    mkdir -p "$TIKTOKEN_CACHE_DIR"
    info "tiktoken cache -> $TIKTOKEN_CACHE_DIR"
fi
"$VENV_DIR/bin/python3" -c \
    "import tiktoken; tiktoken.get_encoding('cl100k_base'); tiktoken.get_encoding('o200k_base')" \
    || warn "tiktoken pre-cache skipped — encoding data will be downloaded on first RAG use."

# Pre-download the cross-encoder rerank model so the first vault chat does
# not stall on a HuggingFace fetch.  ~67 MB cached to
# ~/.cache/huggingface/hub/.  Failure is non-fatal: rag/vault.py logs a
# one-shot warning and falls back to retrieval without rerank.
info "Pre-downloading cross-encoder rerank model (cross-encoder/ms-marco-MiniLM-L-6-v2, ~67 MB)..."
"$VENV_DIR/bin/python3" -c "
try:
    from llama_index.postprocessor.sbert_rerank import SentenceTransformerRerank
    SentenceTransformerRerank(model='cross-encoder/ms-marco-MiniLM-L-6-v2', top_n=1)
    print('Reranker model cached via SentenceTransformerRerank.')
except Exception as e:
    # The llama-index wrapper occasionally lags sentence-transformers API
    # drift; fall back to a direct CrossEncoder download so weights are
    # still cached and first-query latency stays low.
    from sentence_transformers.cross_encoder import CrossEncoder
    CrossEncoder(model_name_or_path='cross-encoder/ms-marco-MiniLM-L-6-v2')
    print(f'Reranker wrapper init failed ({e}); cached via direct CrossEncoder fallback.')
" || warn "Reranker model pre-download skipped — model will be fetched on first chat if needed."

# Pre-download NLTK punkt + stopwords into the app's durable NLTK data dir
# (core.constants.NLTK_DATA_DIR, which launch.py exports as NLTK_DATA at
# startup).  LlamaIndex's SentenceSplitter loads this data for PDF chunking and
# the markdown secondary-cap pass; without it, the FIRST index on a fresh
# OFFLINE machine reaches out to the network and fails.  Failure is non-fatal:
# the data is fetched on first index if the machine is online.
info "Pre-downloading NLTK data (punkt_tab + stopwords, offline-first indexing)..."
NLTK_DATA_DIR="$(PYTHONPATH="$SCRIPT_DIR" "$VENV_DIR/bin/python3" -c 'from core.constants import NLTK_DATA_DIR as d; print(d)' 2>/dev/null || true)"
if [[ -n "$NLTK_DATA_DIR" ]]; then
    mkdir -p "$NLTK_DATA_DIR"
    "$VENV_DIR/bin/python3" -c "
import nltk
d = '$NLTK_DATA_DIR'
nltk.download('punkt_tab', download_dir=d)
nltk.download('stopwords', download_dir=d)
print('NLTK data cached -> ' + d)
" || warn "NLTK pre-download skipped — data will be fetched on first index if needed."
else
    warn "Could not resolve NLTK_DATA_DIR — data will be fetched on first index if needed."
fi

# --- 4. Models ---
# We no longer pull models by default to keep the installer fast.
# Users can select and pull models in the UI after launch.
info "Installer ready. No models will be pulled by default."
info "You can select and pull models in the ChatEKLD UI after launching."

# --- 5. Generate Icon & Build .app ---
info "Generating icon and building $APP_NAME.app..."

# Clean old artifacts — remove PyInstaller scratch dirs, any previous dated
# .app bundle, any leftover .spec file, and Python bytecode cache dirs.
rm -rf "$SCRIPT_DIR/build" "$SCRIPT_DIR/dist"
rm -f  "$SCRIPT_DIR"/*.spec
find "$SCRIPT_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$SCRIPT_DIR" -maxdepth 1 -name 'ChatEKLD_*.app' -exec rm -rf {} +
# Drop any previously packaged .dmg so a rebuild never ships a stale-dated image.
find "$SCRIPT_DIR" -maxdepth 1 -name 'ChatEKLD_*.dmg' -delete
find "$SCRIPT_DIR" -name '*.pyc' -delete

# Generate Icon
if [[ -f "$SCRIPT_DIR/create_app_icon.py" ]]; then
    ICONSET="$(mktemp -d)/AppIcon.iconset"
    mkdir -p "$ICONSET"
    "$VENV_DIR/bin/python3" "$SCRIPT_DIR/create_app_icon.py" "$ICONSET"
    iconutil -c icns -o "$SCRIPT_DIR/AppIcon.icns" "$ICONSET"
    rm -rf "$(dirname "$ICONSET")"
fi

# Discover all mypyc compiled modules in site-packages and add them as hidden imports
MYPYC_FLAGS=$("$VENV_DIR/bin/python3" -c "
import os, glob
import site
site_packages = site.getsitepackages()[0]
flags = []
for f in glob.glob(os.path.join(site_packages, '*__mypyc*.*')):
    name = os.path.basename(f).split('.')[0]
    flags.append('--hidden-import=' + name)
print(' '.join(flags))
")

# Run PyInstaller from the venv directly — no --paths needed because pyinstaller
# already resolves packages from its own environment (adding --paths to the same
# venv's site-packages triggers the "Foreign Python environment" deprecation warning
# in PyInstaller ≥6 and will become an error in PyInstaller 7.0).
#
# NOTE: PyInstaller will emit harmless "Library not found" warnings for Windows DLLs
# (user32, msvcrt, shell32) and Linux shared objects (libcuda, libgomp, libc) because
# packages like torch/ctranslate2 contain cross-platform ctypes calls that reference
# platform-specific libraries. These warnings do not affect the macOS build.
cd "$SCRIPT_DIR"
# Note: eval is used to properly expand MYPYC_FLAGS.
# markitdown is installed with only the [pdf] extra (requirements.txt), so its
# office/zip/audio converter deps (python-docx/pptx, openpyxl) are absent and never
# bundled (magika + onnxruntime stay — they are markitdown base deps, not an extra);
# --collect-all "markitdown" below just gathers the package so it imports when frozen.
eval "\"$VENV_DIR/bin/pyinstaller\" \\
    --name \"$APP_NAME\" \\
    --osx-bundle-identifier \"com.chatekld.app\" \\
    --windowed --noconfirm --clean \\
    --target-arch \"arm64\" \\
    --add-data \"templates:templates\" \\
    --add-data \"static:static\" \\
    --add-data \"resources:resources\" \\
    --add-data \"core:core\" \\
    --add-data \"rag:rag\" \\
    --add-data \"services:services\" \\
    --add-data \"api:api\" \\
    --add-data \"audit:audit\" \\
    --add-data \"deckgen:deckgen\" \\
    --add-data \"refactor:refactor\" \\
    --add-data \"README.md:.\" \\
    --collect-all \"ollama\" \\
    --collect-all \"pymupdf\" \\
    --collect-all \"webview\" \\
    --collect-all \"flask\" \\
    --collect-all \"objc\" \\
    --collect-all \"tiktoken_ext\" \\
    --collect-all \"pydantic\" \\
    --collect-all \"pydantic_core\" \\
    --collect-all \"pydantic_settings\" \\
    --collect-all \"anyio\" \\
    --collect-all \"httpcore\" \\
    --collect-all \"httpx\" \\
    --collect-all \"tenacity\" \\
    --hidden-import \"pydantic_settings\" \\
    --collect-all \"sentence_transformers\" \\
    --collect-all \"transformers\" \\
    --collect-all \"tokenizers\" \\
    --collect-all \"huggingface_hub\" \\
    --collect-all \"markitdown\" \\
    --collect-all \"llama_index.retrievers.bm25\" \\
    --collect-all \"bm25s\" \\
    --collect-all \"Stemmer\" \\
    --collect-all \"llama_index.vector_stores.lancedb\" \\
    --collect-all \"lancedb\" \\
    --collect-all \"lance\" \\
    --collect-all \"pyarrow\" \\
    --collect-all \"pikepdf\" \\
    --collect-all \"ruamel.yaml\" \\
    --collect-all \"bibtexparser\" \\
    --collect-all \"bs4\" \\
    --exclude-module "torch.utils.tensorboard" \
    --exclude-module "tkinter" \
    --exclude-module "pysqlite2" \
    --exclude-module "MySQLdb" \
    --exclude-module "psycopg2" \
    --exclude-module "scipy.special._cdflib" \
    $MYPYC_FLAGS \
    --icon \"AppIcon.icns\" \\
    \"launch.py\""

if [[ -d "$SCRIPT_DIR/dist/$APP_NAME.app" ]]; then
    mv "$SCRIPT_DIR/dist/$APP_NAME.app" "$SCRIPT_DIR/"
    rm -rf "$SCRIPT_DIR/build" "$SCRIPT_DIR/dist"
    rm -f  "$SCRIPT_DIR/$APP_NAME.spec" "$SCRIPT_DIR/AppIcon.icns"

    # Ad-hoc re-sign so a from-source local build launches without a Gatekeeper
    # signature error on the build machine. No --entitlements: the cs.* hardened-
    # runtime keys are inert without `--options runtime` (which we do not set, as
    # that path requires a Developer ID + notarization), so they were dead config.
    # No --deep either (deprecated by Apple). For real distribution, sign with a
    # Developer ID + `--options runtime` and notarize (see README).
    info "Ad-hoc re-signing the app bundle..."
    codesign --force --sign - "$SCRIPT_DIR/$APP_NAME.app"

    info "Successfully built $APP_NAME.app."

    # --- 6. Package a distributable .dmg -----------------------------------
    # WHY: an ad-hoc-signed .app copied to ANOTHER Mac is Gatekeeper-quarantined
    # ("Apple cannot check it for malicious software") because it is not signed with
    # a Developer ID and not notarized. A .dmg is the conventional macOS delivery
    # wrapper. We stage the .app next to (a) READ-ME-FIRST first-run instructions,
    # (b) the offline model-seed helper packaging/seed_models.command when present
    # (Phase 2 — copied only if it exists so this step works before then), and
    # (c) an /Applications symlink for drag-installation — so a recipient gets one
    # download with everything needed. We deliberately do NOT notarize (ad-hoc
    # signing only, no Apple Developer account): the readme documents the one-time
    # `xattr -dr com.apple.quarantine` step Gatekeeper otherwise requires.
    info "Packaging $APP_NAME.dmg…"
    DMG_NAME="$APP_NAME.dmg"
    DMG_ROOT="$(mktemp -d)"
    DMG_STAGE="$DMG_ROOT/ChatEKLD"
    mkdir -p "$DMG_STAGE"
    cp -R "$SCRIPT_DIR/$APP_NAME.app" "$DMG_STAGE/"
    # Drag-install target: a symlink to /Applications inside the image.
    ln -s /Applications "$DMG_STAGE/Applications"
    # Offline model-seed helper (Phase 2). Copied only if present so this DMG step
    # is a no-op-safe superset before that file lands on the branch.
    if [[ -f "$SCRIPT_DIR/packaging/seed_models.command" ]]; then
        cp "$SCRIPT_DIR/packaging/seed_models.command" "$DMG_STAGE/"
        chmod +x "$DMG_STAGE/seed_models.command"
    fi
    # First-run instructions, generated (heredoc) so the exact dated app name and
    # the inner binary path are always correct for THIS build.
    cat > "$DMG_STAGE/READ ME FIRST.txt" <<EOF
ChatEKLD — first run on a new Mac
=================================

This app is ad-hoc signed (no Apple notarization), so macOS Gatekeeper will block
it the first time until you approve it once. It runs 100% locally.

1. INSTALL
   Drag "$APP_NAME.app" onto the Applications shortcut in this window.

2. APPROVE IT ONCE (required — Gatekeeper blocks unsigned transferred apps)
   Open Terminal and run:

     xattr -dr com.apple.quarantine "/Applications/$APP_NAME.app"

   (Alternatively: right-click the app → Open, then approve under
    System Settings → Privacy & Security → "Open Anyway".)

3. SEED OFFLINE MODELS (recommended — do this once while online)
   So the first vault index and chat work fully offline afterwards, double-click
   "seed_models.command" in this window (or run it from Terminal). It downloads the
   reranker + tokenizer caches into your app-data folder. Skipping this is fine if
   you stay online: the app downloads them on first use instead.

4. GIVE IT A MODEL
   ChatEKLD needs a model provider. Either:
     • install Ollama (https://ollama.com) or LM Studio (https://lmstudio.ai) and
       pull an embedding model (nomic-embed-text) + a chat model, OR
     • use an online provider: create the file
       ~/Library/Application Support/ChatEKLD/.env with a line like
       OPENAI_API_KEY=sk-...  (also supported: ANTHROPIC_API_KEY, GOOGLE_API_KEY).

5. LAUNCH
   Open ChatEKLD from Applications.

Everything the app stores lives under ~/Library/Application Support/ChatEKLD/.
Requires an Apple Silicon (arm64) Mac; Intel is not supported.
EOF
    # UDZO = zlib-compressed read-only image (the standard distribution format).
    hdiutil create -volname "ChatEKLD" -srcfolder "$DMG_STAGE" \
        -ov -format UDZO "$SCRIPT_DIR/$DMG_NAME" >/dev/null
    rm -rf "$DMG_ROOT"
    info "Successfully packaged $DMG_NAME."
else
    error "Build failed."
fi

divider
echo -e "${GREEN}ChatEKLD is ready!${NC}"
echo "Local (this Mac): drag $APP_NAME.app to /Applications and double-click to launch."
echo "To share with another Mac: send $APP_NAME.dmg — it bundles the app, first-run"
echo "instructions, and the offline model-seed helper. The recipient must run the"
echo "one-time 'xattr -dr com.apple.quarantine' step (documented inside the .dmg)."
divider
