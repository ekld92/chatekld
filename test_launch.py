"""Regression tests for ``launch.py`` — the frozen-app entrypoint.

No test imported ``launch.py`` before this file, which is why a top-level
``NameError`` (undefined ``_BASE_DIR``, introduced 2026-06-16) slipped through
CI and crashed *every* launch — dev and frozen — before Flask bound, before the
window opened. These tests import the module the way the app does (its
module-top-level env helpers run on import) with the heavy webview/app/server
chain stubbed, so any undefined-name or import-time crash in the pre-import
bootstrap fails fast here.
"""

import importlib
import sys
import types

import pytest


def _install_heavy_stubs(monkeypatch):
    """Stub the heavy/native imports ``launch`` pulls at module top level.

    ``launch`` does ``import webview`` and ``from app import app`` plus the
    provider-server / config imports inside a try-block. We replace the
    expensive ones with light module stubs so importing ``launch`` exercises
    only its own bootstrap (``_pin_tiktoken_cache`` / ``_load_env_files`` /
    ``_maybe_enable_hf_offline``) — the exact code that crashed — without
    dragging in Flask, llama-index, or a native webview build.
    """
    webview_stub = types.ModuleType("webview")
    webview_stub.create_window = lambda *a, **k: None
    webview_stub.start = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "webview", webview_stub)

    app_stub = types.ModuleType("app")
    app_stub.app = object()
    monkeypatch.setitem(sys.modules, "app", app_stub)

    server_stub = types.ModuleType("core.providers.server")
    server_stub.start_ollama_server = lambda *a, **k: (True, "")
    server_stub.start_lm_studio_server = lambda *a, **k: (True, "")
    server_stub.shutdown_ollama = lambda *a, **k: None
    # launch.py now also imports add_provider_warning (A4: it surfaces an Ollama
    # start failure to the UI warnings banner instead of only logging it), so the
    # stub must expose it or the `from core.providers.server import …` line fails.
    server_stub.add_provider_warning = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "core.providers.server", server_stub)


def test_launch_module_imports_without_crash(monkeypatch):
    """Importing ``launch`` runs its top-level env bootstrap without raising.

    Guards against an undefined name (the ``_BASE_DIR`` regression) or any
    other crash in ``_pin_tiktoken_cache`` / ``_load_env_files`` /
    ``_maybe_enable_hf_offline``, which run at import time.
    """
    _install_heavy_stubs(monkeypatch)
    sys.modules.pop("launch", None)
    try:
        launch = importlib.import_module("launch")
    finally:
        sys.modules.pop("launch", None)
    # _BASE_DIR must be a real, non-empty path (the bug was an undefined name).
    assert isinstance(launch._BASE_DIR, str) and launch._BASE_DIR


def test_load_env_files_runs_without_nameerror(monkeypatch):
    """``_load_env_files`` (called at module top level) must not NameError.

    This is the exact function that referenced the undefined ``_BASE_DIR``;
    calling it directly pins the fix even if the import-time call path changes.
    """
    _install_heavy_stubs(monkeypatch)
    sys.modules.pop("launch", None)
    try:
        launch = importlib.import_module("launch")
        # Re-invoke explicitly: builds the BASE_DIR/.env candidate list.
        launch._load_env_files()
    except NameError as exc:  # pragma: no cover - the regression we guard
        pytest.fail(f"_load_env_files raised NameError: {exc}")
    finally:
        sys.modules.pop("launch", None)


def _install_seed_download_stubs(monkeypatch):
    """Stub the three model-cache downloaders ``seed_models`` calls lazily.

    ``seed_models`` imports tiktoken / nltk / the sbert-rerank wrapper INSIDE the
    function, so replacing them in ``sys.modules`` before the call makes the seed
    run offline and instantly instead of pulling ~70 MB over the network. Returns a
    ``calls`` dict the test asserts against (every real download path was invoked).
    """
    calls = {"tiktoken": [], "nltk": [], "rerank": []}

    tk = types.ModuleType("tiktoken")
    tk.get_encoding = lambda name: calls["tiktoken"].append(name)
    monkeypatch.setitem(sys.modules, "tiktoken", tk)

    nltk_stub = types.ModuleType("nltk")
    nltk_stub.download = lambda pkg, download_dir=None: calls["nltk"].append(pkg)
    monkeypatch.setitem(sys.modules, "nltk", nltk_stub)

    # `from llama_index.postprocessor.sbert_rerank import SentenceTransformerRerank`
    # resolves against sys.modules first, so a stub here wins over the real (slow,
    # network-touching) wrapper. Constructing it must NOT raise, so the CrossEncoder
    # fallback branch is never taken.
    sbert = types.ModuleType("llama_index.postprocessor.sbert_rerank")

    class _Rerank:
        def __init__(self, model=None, top_n=None):
            calls["rerank"].append(model)

    sbert.SentenceTransformerRerank = _Rerank
    monkeypatch.setitem(sys.modules, "llama_index.postprocessor.sbert_rerank", sbert)
    return calls


def test_seed_models_downloads_all_caches_and_returns_zero(monkeypatch):
    """``seed_models()`` fetches tiktoken + NLTK + reranker and reports success.

    Pins the offline-first seed (Phase 2): a fresh Mac that received only the .app
    runs `--seed-models` once to populate the caches that live OUTSIDE the bundle.
    We assert every real download path is exercised and the success exit code is 0,
    with the downloads stubbed so the test is hermetic (no network).
    """
    _install_heavy_stubs(monkeypatch)
    calls = _install_seed_download_stubs(monkeypatch)
    sys.modules.pop("launch", None)
    try:
        launch = importlib.import_module("launch")
        rc = launch.seed_models()
    finally:
        sys.modules.pop("launch", None)
    assert rc == 0
    assert "cl100k_base" in calls["tiktoken"] and "o200k_base" in calls["tiktoken"]
    assert "punkt_tab" in calls["nltk"] and "stopwords" in calls["nltk"]
    assert calls["rerank"], "reranker seed was not attempted"


def test_seed_models_failure_returns_nonzero(monkeypatch):
    """A failed download makes ``seed_models()`` return non-zero (so the .command
    can tell the user to re-run while online) instead of masking the failure."""
    _install_heavy_stubs(monkeypatch)
    _install_seed_download_stubs(monkeypatch)

    # Break the tiktoken step only; the other two still succeed.
    def _boom(_name):
        raise RuntimeError("network down")

    sys.modules["tiktoken"].get_encoding = _boom
    sys.modules.pop("launch", None)
    try:
        launch = importlib.import_module("launch")
        rc = launch.seed_models()
    finally:
        sys.modules.pop("launch", None)
    assert rc != 0


def test_seed_models_argv_branch_exits_without_starting_window(monkeypatch):
    """`--seed-models` in argv seeds and raises SystemExit BEFORE the window opens.

    The branch lives at module top level (before the heavy webview/app import block),
    so importing ``launch`` with the flag present must exit via ``SystemExit`` rather
    than fall through to ``main()`` / ``webview.start()``. The webview stub records
    nothing because ``main()`` is never reached.
    """
    _install_heavy_stubs(monkeypatch)
    _install_seed_download_stubs(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["launch.py", "--seed-models"])
    sys.modules.pop("launch", None)
    try:
        with pytest.raises(SystemExit) as excinfo:
            importlib.import_module("launch")
    finally:
        sys.modules.pop("launch", None)
    assert excinfo.value.code == 0
