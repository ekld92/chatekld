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
import pathlib
import sys
import types

import pytest
from unittest.mock import MagicMock


@pytest.mark.parametrize("provider", ["ollama", "lm_studio", "openai"])
@pytest.mark.parametrize("skip", [None, "0", "1"])
def test_native_lifecycle_can_leave_provider_services_unmanaged(monkeypatch, provider, skip):
    """Drive main through window close; no sockets, threads or services run."""
    _install_heavy_stubs(monkeypatch)
    if skip is None:
        monkeypatch.delenv("CHATEKLD_SKIP_PROVIDER_STARTUP", raising=False)
    else:
        monkeypatch.setenv("CHATEKLD_SKIP_PROVIDER_STARTUP", skip)
    sys.modules.pop("launch", None)
    try:
        launch = importlib.import_module("launch")
        launch._load_runtime()
        monkeypatch.setattr(launch, "_load_runtime", lambda: None)
        monkeypatch.setattr(launch, "load_config", lambda: {"provider": provider})
        starts = {name: MagicMock(return_value=(True, "")) for name in ("ollama", "lm_studio")}
        monkeypatch.setattr(launch, "start_ollama_server", starts["ollama"])
        monkeypatch.setattr(launch, "start_lm_studio_server", starts["lm_studio"])
        shutdown = MagicMock()
        monkeypatch.setattr(launch, "shutdown_ollama", shutdown)
        monkeypatch.setattr(launch, "find_free_port", lambda: 54321)
        monkeypatch.setattr(launch.socket, "create_connection", MagicMock())
        monkeypatch.setattr(launch.signal, "signal", MagicMock())
        monkeypatch.setattr(launch.atexit, "register", MagicMock())
        monkeypatch.setattr("core.server_info.acquire_server_lease", MagicMock())
        monkeypatch.setattr("core.server_info.write_server_info", MagicMock())
        monkeypatch.setattr("core.server_info.clear_server_info", MagicMock())
        window = MagicMock()
        monkeypatch.setattr(launch.webview, "create_window", window)
        joined = []

        class Thread:
            def __init__(self, *, target, name=None, **kwargs):
                self.target, self.name = target, name

            def start(self):
                # Only provider closures execute. Flask, prewarm and the
                # shutdown watcher remain inert at their external seams.
                if self.name in ("ollama-start", "lm-studio-start"):
                    self.target()

            def join(self, timeout):
                joined.append(self.name)

        monkeypatch.setattr(launch.threading, "Thread", Thread)
        launch.main()
        window.assert_called_once()
        if skip == "1":
            for start in starts.values():
                start.assert_not_called()
            shutdown.assert_not_called()
            assert joined == []
        else:
            selected = "lm_studio" if provider == "lm_studio" else "ollama"
            starts[selected].assert_called_once()
            starts["ollama" if selected == "lm_studio" else "lm_studio"].assert_not_called()
            assert shutdown.call_count == (selected == "ollama")
            assert joined == (["ollama-start"] if selected == "ollama" else [])
    finally:
        sys.modules.pop("launch", None)


@pytest.fixture(autouse=True)
def _restore_environ(monkeypatch, tmp_path):
    """Redirect checkout dotenv discovery before bootstrap, then restore env.

    Restoring after the test alone still exposes real credentials during the
    test and chmods the real file. Keep the real loader for fixture coverage,
    but replace the checkout candidate with a disposable directory.
    """
    import os
    from core import env_files

    checkout = pathlib.Path(__file__).resolve().parent
    fixture_checkout = tmp_path / "checkout"
    fixture_checkout.mkdir()
    real_loader = env_files.load_env_files

    def fixture_loader(dirs):
        return real_loader([
            str(fixture_checkout) if pathlib.Path(directory).resolve() == checkout
            else directory for directory in dirs
        ])

    monkeypatch.setattr(env_files, "load_env_files", fixture_loader)
    saved = dict(os.environ)
    yield fixture_checkout
    os.environ.clear()
    os.environ.update(saved)


def test_checkout_dotenv_discovery_uses_disposable_source(_restore_environ, monkeypatch):
    import os

    (_restore_environ / ".env").write_text("CHATEKLD_CHECKOUT_PROBE=fixture\n")
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.delenv("CHATEKLD_CHECKOUT_PROBE", raising=False)
    _install_heavy_stubs(monkeypatch)
    sys.modules.pop("launch", None)
    try:
        launch = importlib.import_module("launch")
        launch._load_env_files()
        assert os.environ["CHATEKLD_CHECKOUT_PROBE"] == "fixture"
    finally:
        sys.modules.pop("launch", None)


def test_importing_launch_leaves_no_new_environment_key(tmp_path, monkeypatch):
    """The fixture above is load-bearing; this pins it from inside a test by
    loading a .env the way the dev checkout does."""
    import os
    (tmp_path / ".env").write_text("CHATEKLD_LEAK_PROBE=1\n", encoding="utf-8")
    _install_heavy_stubs(monkeypatch)
    sys.modules.pop("launch", None)
    try:
        launch = importlib.import_module("launch")
        monkeypatch.setattr(launch, "_BASE_DIR", str(tmp_path))
        monkeypatch.setattr(launch.sys, "frozen", True, raising=False)
        monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
        launch._load_env_files()
        assert os.environ.get("CHATEKLD_LEAK_PROBE") == "1"
    finally:
        sys.modules.pop("launch", None)


def test_the_probe_did_not_survive_into_the_next_test():
    import os
    assert "CHATEKLD_LEAK_PROBE" not in os.environ


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


def test_load_env_files_sets_owner_only_mode(monkeypatch, tmp_path):
    """Existing .env files are chmod'd to 0o600 before dotenv reads them."""
    import os

    env_path = tmp_path / ".env"
    env_path.write_text("CHATEKLD_TEST_ENV_LOAD=1\n", encoding="utf-8")
    os.chmod(env_path, 0o644)
    monkeypatch.delenv("CHATEKLD_TEST_ENV_LOAD", raising=False)
    _install_heavy_stubs(monkeypatch)
    sys.modules.pop("launch", None)
    try:
        launch = importlib.import_module("launch")
        monkeypatch.setattr(launch, "_BASE_DIR", str(tmp_path))
        monkeypatch.setattr(launch.sys, "frozen", True, raising=False)
        monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
        launch._load_env_files()
        assert (env_path.stat().st_mode & 0o777) == 0o600
        assert os.environ.get("CHATEKLD_TEST_ENV_LOAD") == "1"
    finally:
        sys.modules.pop("launch", None)
        os.environ.pop("CHATEKLD_TEST_ENV_LOAD", None)


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


def test_window_is_created_with_text_selection_enabled():
    """``create_window`` must pass ``text_select=True``.

    PyWebView defaults it to False and then injects
    ``body { user-select: none; cursor: default }`` into the document, which
    makes every answer, summary and report in the app unselectable with the
    mouse. This is a SOURCE ratchet rather than a call assertion because the
    only way to reach the real ``create_window`` is to open a native window;
    and it is worth ratcheting because no other gate can see the regression —
    ``tests/browser/`` runs Chromium, which never receives pywebview's
    injection.
    """
    src = pathlib.Path(__file__).with_name("launch.py").read_text(encoding="utf-8")
    start = src.index("webview.create_window(")
    depth, end = 0, start
    for i in range(start, len(src)):          # balance parens: min_size=(...) nests
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    call = src[start:end]
    assert "text_select=True" in call, (
        "launch.py must call webview.create_window(..., text_select=True); "
        "without it pywebview disables text selection app-wide."
    )
