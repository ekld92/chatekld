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
# every machine but the author's); override with CHATEKLD_VENV_DIR to relocate
# (the legacy PAPERMIND_VENV_DIR is still honoured as a deprecated fallback).
# NOTE: under `set -u` (enabled above) this reference aborts if HOME is unset —
# acceptable because a macOS terminal/Finder launch always sets HOME, and an
# unset HOME would break the rest of the build anyway.
VENV_DIR="${CHATEKLD_VENV_DIR:-${PAPERMIND_VENV_DIR:-$HOME/venvs/papermind2026}}"
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

"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

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
# Note: eval is used to properly expand MYPYC_FLAGS
eval "\"$VENV_DIR/bin/pyinstaller\" \\
    --name \"$APP_NAME\" \\
    --osx-bundle-identifier \"com.chatekld.app\" \\
    --osx-entitlements-file \"$SCRIPT_DIR/macos_entitlements.plist\" \\
    --windowed --noconfirm --clean \\
    --add-data \"templates:templates\" \\
    --add-data \"static:static\" \\
    --add-data \"resources:resources\" \\
    --add-data \"core:core\" \\
    --add-data \"rag:rag\" \\
    --add-data \"services:services\" \\
    --add-data \"api:api\" \\
    --add-data \"audit:audit\" \\
    --add-data \"deckgen:deckgen\" \\
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

    # Re-sign the app bundle to apply entitlements.
    # We use ad-hoc signing (-) here; for distribution, use a Developer ID.
    info "Re-signing the app bundle with entitlements..."
    codesign --force --deep --sign - --entitlements "$SCRIPT_DIR/macos_entitlements.plist" "$SCRIPT_DIR/$APP_NAME.app"

    info "Successfully built $APP_NAME.app."
else
    error "Build failed."
fi

divider
echo -e "${GREEN}ChatEKLD is ready!${NC}"
echo "1. Drag $APP_NAME.app to /Applications"
echo "2. Double-click to launch (Ollama will start automatically)"
divider
