"""Entry point for ChatEKLD 2026 — a privacy-first, offline-first AI assistant
(fully local).

This module orchestrates the full application lifecycle:

1. Starts the Flask API server in a daemon background thread so the HTTP
   layer is ready before any UI appears.
2. Waits for Flask to bind its listening socket (socket-probe loop with a
   configurable timeout).
3. Optionally launches the Ollama LLM server in another daemon thread so
   that model availability does not delay the window from opening.
4. Opens a PyWebView native window pointing at the Flask server. The call
   to ``webview.start()`` blocks the main thread until the user closes the
   window.
5. Performs graceful shutdown of all subsystems (RAGManager, Ollama process)
   once the window is closed, ensuring no stray child processes are left
   behind.
"""

import os
import sys
import signal
import threading
import time
import socket
import logging
import logging.handlers
import traceback
import multiprocessing

# Handle PyInstaller bundle paths
if getattr(sys, 'frozen', False):
    multiprocessing.freeze_support()
# Logs live alongside config/index under the platform app-data directory so
# `python launch.py` (dev) and a frozen build share one log location and a
# Finder-cleanup of the repo never wipes prior diagnostics.
from core.constants import BASE_DIR as _BASE_DIR, LOG_FILE as log_file  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        # Rotate so the log can't grow unbounded over months of use (it shares the
        # volume with the local LLM weights). 10 MB x 3 backups; the in-app log
        # viewer (/api/log/tail) only ever reads the last ~1 MB.
        logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10_000_000, backupCount=3),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)


def _pin_tiktoken_cache() -> None:
    """Point tiktoken at a durable cache dir under BASE_DIR.

    tiktoken otherwise caches its BPE vocab to ``$TMPDIR/data-gym-cache``,
    which macOS evicts after a few days of non-use — breaking an *offline*
    reindex (the LlamaIndex SentenceSplitter needs cl100k_base).  Pinning the
    cache under BASE_DIR keeps it durable across reboots and lets the
    installer pre-populate exactly where the app reads.

    MUST run before tiktoken is first imported (it happens via
    ``from app import app`` below).  ``setdefault`` so a user-set
    TIKTOKEN_CACHE_DIR still wins.
    """
    try:
        from core.constants import TIKTOKEN_CACHE_DIR
        os.makedirs(TIKTOKEN_CACHE_DIR, exist_ok=True)
        os.environ.setdefault("TIKTOKEN_CACHE_DIR", TIKTOKEN_CACHE_DIR)
    except Exception:
        logger.debug("Could not pin TIKTOKEN_CACHE_DIR.", exc_info=True)


def _pin_nltk_data() -> None:
    """Point NLTK at a durable data dir under BASE_DIR (the installer pre-seeds it).

    LlamaIndex's SentenceSplitter eagerly loads NLTK punkt + stopwords; without
    the data it reaches out to huggingface/nltk over the network, which breaks an
    OFFLINE first index on a fresh machine. The installer pre-downloads the data
    into ``core.constants.NLTK_DATA_DIR``; exporting it here (``setdefault`` so a
    user override wins) makes the app find it without a network call.

    MUST run before the first SentenceSplitter import (pulled by ``from app import
    app`` below). Mirrors ``_pin_tiktoken_cache``.
    """
    try:
        from core.constants import NLTK_DATA_DIR
        os.makedirs(NLTK_DATA_DIR, exist_ok=True)
        os.environ.setdefault("NLTK_DATA", NLTK_DATA_DIR)
    except Exception:
        logger.debug("Could not pin NLTK_DATA.", exc_info=True)


def _load_env_files() -> None:
    """Load API keys / overrides from .env files into the process environment.

    A Finder-launched ``.app`` does NOT inherit the shell environment, so
    ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY`` / ``GOOGLE_API_KEY`` set in
    ~/.zshrc are invisible to the bundle and every online chat would fail with
    "API key is not set".  We load them from a ``.env`` file instead, in
    priority order:

      1. ``BASE_DIR/.env`` — the canonical location for the packaged app
         (``~/Library/Application Support/ChatEKLD/.env``, the existing
         owner-only app-data dir).
      2. A ``.env`` beside this script — dev convenience when running
         ``python launch.py`` from a checkout (skipped when frozen).

    ``override=False`` so a real shell variable always wins over the file: dev
    keeps using ~/.zshrc, the ``.app`` falls back to BASE_DIR/.env.  Adapters
    read ``os.environ`` at call time, so loading once here (before the app
    import) is sufficient.  The python-dotenv import is optional — a missing
    install only disables ``.env`` loading, it never blocks startup.
    """
    try:
        from dotenv import load_dotenv
    except Exception:
        logger.debug("python-dotenv not installed; skipping .env loading.")
        return
    candidates = [os.path.join(_BASE_DIR, ".env")]
    if not getattr(sys, "frozen", False):
        candidates.append(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        )
    for path in candidates:
        try:
            if os.path.isfile(path) and load_dotenv(path, override=False):
                logger.info("Loaded environment overrides from %s", path)
        except Exception:
            logger.debug("Failed to load .env from %s", path, exc_info=True)


def _maybe_enable_hf_offline() -> None:
    """Set HF_HUB_OFFLINE=1 when the configured reranker is already cached.

    sentence-transformers makes ~25 HEAD/GET requests to huggingface.co on
    every reranker load even when the weights are fully cached.  Forcing
    offline mode skips all of that — matching the app's offline-first
    posture — but only when the model is actually present in the HF cache,
    so a first-run download still works.

    MUST run before the ``from app import app`` import below:
    huggingface_hub reads HF_HUB_OFFLINE into a module constant at import
    time, and the app import chain (rag.engine → sbert-rerank →
    sentence_transformers) pulls it in.  Side effect to know about: changing
    ``vault_reranker_model`` to an UNCACHED model mid-session will fail to
    download until the app is restarted (the restart re-evaluates the cache
    and leaves offline mode off for the new model).
    """
    if os.environ.get("HF_HUB_OFFLINE"):
        return  # user already decided, don't override
    try:
        import json as _json
        from core.constants import CONFIG_FILE as _CONFIG_FILE
        model = ""
        if os.path.exists(_CONFIG_FILE):
            with open(_CONFIG_FILE) as _f:
                model = str(_json.load(_f).get(
                    "vault_reranker_model",
                    "cross-encoder/ms-marco-MiniLM-L-6-v2",
                ) or "").strip()
        else:
            model = "cross-encoder/ms-marco-MiniLM-L-6-v2"
        if not model:
            return
        cache_root = os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface")
        snapshots = os.path.join(
            cache_root, "hub", "models--" + model.replace("/", "--"), "snapshots",
        )
        if os.path.isdir(snapshots) and os.listdir(snapshots):
            os.environ["HF_HUB_OFFLINE"] = "1"
            logger.info(
                "Reranker %s found in HF cache — enabling HF_HUB_OFFLINE.", model,
            )
    except Exception:
        logger.debug("HF offline-mode probe failed; leaving online.", exc_info=True)


def _configured_reranker_model() -> str:
    """Reranker model to seed: the configured value if set, else the shipped default.

    Reads ``config.json`` directly (like ``_maybe_enable_hf_offline`` above) rather
    than importing ``core.config``, so seed mode keeps a minimal import surface and
    does not drag in the Flask app chain. The default string mirrors the installer
    and ``_maybe_enable_hf_offline``.
    """
    default = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    try:
        import json as _json
        from core.constants import CONFIG_FILE as _CONFIG_FILE
        if os.path.exists(_CONFIG_FILE):
            with open(_CONFIG_FILE) as _f:
                val = str(_json.load(_f).get("vault_reranker_model", default) or "").strip()
                return val or default
    except Exception:
        logger.debug("seed: reranker-model config read failed; using default.", exc_info=True)
    return default


def seed_models() -> int:
    """Headless ``--seed-models`` mode: download the offline model caches, then exit.

    The tiktoken BPE vocab, NLTK punkt/stopwords, and the ~67 MB HF cross-encoder
    reranker live OUTSIDE the .app bundle (under BASE_DIR and ~/.cache/huggingface).
    A fresh Mac that received only the app has none of them, so its first OFFLINE
    vault index FAILS (the SentenceSplitter needs tiktoken + NLTK) and its first
    chat stalls on a reranker download. This function is the per-machine seed a
    recipient runs once (via the DMG's ``seed_models.command``): it downloads all
    three into the exact locations the app reads, reusing the SAME bundled packages
    (tiktoken / nltk / sentence-transformers) the app uses at runtime — so it can
    never drift from what the app expects. Requires network. Returns a process exit
    code (0 = every step succeeded; non-zero if any download failed, so the caller /
    .command can tell the user to re-run while online).

    Runs BEFORE the heavy ``import webview; from app import app`` block below, so
    seeding needs neither the native webview framework nor the Flask app. The
    tiktoken + NLTK cache dirs were already pinned by ``_pin_tiktoken_cache`` /
    ``_pin_nltk_data`` above (they export TIKTOKEN_CACHE_DIR / NLTK_DATA), so the
    downloads land where the app reads them.
    """
    # Seeding downloads by definition — a set HF_HUB_OFFLINE=1 (from
    # _maybe_enable_hf_offline when a PRIOR seed already cached the model, or a
    # user override) would block the reranker fetch, so force online for this run.
    if os.environ.pop("HF_HUB_OFFLINE", None):
        logger.info("seed: cleared HF_HUB_OFFLINE for the download pass.")

    ok = True

    # 1. tiktoken BPE (cl100k_base + o200k_base) -> TIKTOKEN_CACHE_DIR (pinned above).
    try:
        import tiktoken
        tiktoken.get_encoding("cl100k_base")
        tiktoken.get_encoding("o200k_base")
        logger.info("seed: tiktoken encodings cached -> %s",
                    os.environ.get("TIKTOKEN_CACHE_DIR", "<default>"))
    except Exception:
        ok = False
        logger.exception("seed: tiktoken caching failed.")

    # 2. NLTK punkt_tab + stopwords -> NLTK_DATA (pinned above).
    try:
        import nltk
        nltk_dir = os.environ.get("NLTK_DATA") or None
        nltk.download("punkt_tab", download_dir=nltk_dir)
        nltk.download("stopwords", download_dir=nltk_dir)
        logger.info("seed: NLTK data cached -> %s", nltk_dir or "<default>")
    except Exception:
        ok = False
        logger.exception("seed: NLTK download failed.")

    # 3. HF cross-encoder reranker -> ~/.cache/huggingface (or HF_HOME). Mirrors the
    #    installer's pre-download (SentenceTransformerRerank, with a direct
    #    CrossEncoder fallback for llama-index/sentence-transformers API drift) so
    #    the first vault chat doesn't stall on the fetch.
    try:
        model = _configured_reranker_model()
        try:
            from llama_index.postprocessor.sbert_rerank import SentenceTransformerRerank
            SentenceTransformerRerank(model=model, top_n=1)
        except Exception as _e:
            from sentence_transformers.cross_encoder import CrossEncoder
            CrossEncoder(model_name_or_path=model)
            logger.info("seed: reranker wrapper init failed (%s); used CrossEncoder fallback.", _e)
        logger.info("seed: reranker cached -> %s", model)
    except Exception:
        ok = False
        logger.exception("seed: reranker download failed.")

    # Terminal-visible summary (logger also mirrors to stdout for the .command window).
    print("")
    print("ChatEKLD offline model seed: " + (
        "DONE — the app can now index and chat fully offline."
        if ok else
        "FINISHED WITH ERRORS — see chatekld.log and re-run while online."
    ))
    return 0 if ok else 1


# Order matters: pin the tiktoken + NLTK caches before any import that reads
# them, load .env so it can supply keys / HF_HOME, then probe the HF cache (which
# reads HF_HOME) — all before ``from app import app`` pulls the heavy chain.
_pin_tiktoken_cache()
_pin_nltk_data()
_load_env_files()
_maybe_enable_hf_offline()

# Headless offline-seed mode. `--seed-models` downloads the three model caches that
# live OUTSIDE the bundle and exits WITHOUT starting Flask or opening a window — the
# per-machine seed a fresh-Mac recipient runs once (via the DMG's seed_models.command)
# so the first offline index/chat work. Handled HERE, before the heavy
# ``import webview; from app import app`` block, so seeding needs neither the native
# webview framework nor the Flask app. Membership test (not a flag parser) so Finder's
# legacy ``-psn_...`` argv entry can never match it.
if "--seed-models" in sys.argv:
    raise SystemExit(seed_models())

try:
    import webview
    from app import app as flask_app
    from core.providers.server import (
        start_ollama_server,
        start_lm_studio_server,
        shutdown_ollama,
        add_provider_warning,
    )
    from core.config import load_config
except Exception as e:
    logger.error(f"Failed to import core modules: {e}")
    logger.error(traceback.format_exc())
    sys.exit(1)

def find_free_port():
    """Find and return an available TCP port on the loopback interface.

    Opens an ephemeral socket bound to 127.0.0.1:0 and reads back the
    port number assigned by the OS.  The socket is immediately closed
    (via the context manager) so that Flask can subsequently bind to the
    same port.  There is a small TOCTOU window where another process
    could claim the port between the close and Flask's bind, but in
    practice this is negligible on a desktop app.

    Returns:
        int: A free TCP port number, or 5000 as a last-resort fallback
            if the socket probe fails.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', 0))
            return s.getsockname()[1]
    except Exception as e:
        logger.error(f"Failed to find free port: {e}")
        return 5000 # Fallback

def run_flask(port):
    """Run the Flask development server on the loopback interface.

    This function is the target for the daemon thread started in
    ``main()``.  It calls ``flask_app.run()`` which blocks until the
    server is shut down (i.e. when the process exits).  The reloader is
    disabled because PyWebView already owns the main thread and a
    reloader restart would orphan the native window.

    Args:
        port: The TCP port number on which Flask should listen.  Must be
            a free port obtained from ``find_free_port()``.
    """
    try:
        logger.info(f"Starting Flask on port {port}")
        # WHY host='127.0.0.1' explicitly?
        # Flask's default is already 127.0.0.1, but the entire CSRF model in
        # _origin_is_local() assumes the server is reachable only on the
        # loopback interface.  If this line were ever changed to host='0.0.0.0'
        # for debugging, the app would be exposed on all network interfaces
        # while the GET-bypass (now fixed) or any future CSRF regression would
        # become the only barrier against external access.  Making the binding
        # explicit prevents an accidental host='0.0.0.0' change from silently
        # opening the server to the local network.
        # Using Waitress as a production WSGI server to prevent thread pool exhaustion
        # caused by long-lived Server-Sent Events (SSE). Flask's development server
        # (Werkzeug) limits concurrent connections severely in synchronous mode.
        # threads=32: pool slots are occupied for the full duration of SSE
        # generations (up to 300 s) and upload extractions (proc.join up to
        # 600 s) while the UI keeps 1 Hz status polling going — the threads
        # are I/O-parked, so doubling the pool is cheap insurance against
        # exhaustion presenting as a frontend hang.  Do NOT add a
        # channel_timeout here expecting it to reap stuck requests: waitress'
        # maintenance() only closes channels with no in-flight request.
        try:
            from waitress import serve
            serve(flask_app, host='127.0.0.1', port=port, threads=32)
        except ImportError:
            logger.warning("Waitress not found. Falling back to Flask dev server.")
            flask_app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)

    except Exception as e:
        logger.error(f"Flask failed to run: {e}")
        logger.error(traceback.format_exc())

def main():
    """Execute the full ChatEKLD startup-and-shutdown sequence.

    The sequence is deliberately ordered to minimise perceived launch
    latency:

    1. **Find a free port** — ephemeral socket probe via
       ``find_free_port()``.
    2. **Start Flask** in a daemon thread so the HTTP server begins
       loading immediately.
    3. **Socket-probe loop** — repeatedly attempt a TCP connection to
       Flask's port until it accepts (or the timeout expires).  This
       ensures the PyWebView window is never pointed at a port that is
       not yet listening.
    4. **Start Ollama** in a separate daemon thread.  Ollama startup can
       take 10-15 s, so it must not block the window from appearing.
    5. **Open PyWebView window** — ``webview.start()`` blocks the main
       thread until the user closes the window.
    6. **Post-window-close cleanup** — join the Ollama thread (so its
       process handle is available), clean up RAGManager,
       then terminate the Ollama child process.

    Raises:
        SystemExit: If Flask fails to start within the timeout, or if an
            unrecoverable error occurs during startup.
    """
    try:
        port = find_free_port()

        # Start Flask in a background thread first — do NOT block on Ollama before this.
        # Ollama startup can take up to 15 s; starting it before Flask caused a 35 s
        # delay before the window opened and prevented the window from launching at all
        # if the Ollama binary hung or was missing.
        t = threading.Thread(target=run_flask, args=(port,), daemon=True)
        t.start()

        # Install SIGINT/SIGTERM handlers so that `kill` or Ctrl-C from a terminal
        # (including process-manager signals from launchd or systemd on Linux) trigger
        # the same RAGManager + Ollama cleanup as a normal window close, rather than
        # leaving stray Ollama child processes behind.
        # WHY signal in main() rather than module level?
        # signal.signal() must be called from the main thread (Python raises
        # ValueError if called from a daemon thread).  main() always runs on the
        # main thread, so this is the safe registration point.
        # The handler sets _shutdown_event, which the _shutdown_watcher daemon thread
        # (started after window creation below) monitors and calls window.destroy() on
        # — posting a close event into the native macOS event loop so that
        # webview.start() returns and the cleanup code below can run.
        _shutdown_event = threading.Event()

        def _signal_handler(signum, frame):
            """Set _shutdown_event on SIGINT/SIGTERM for graceful cleanup.

            _shutdown_event is monitored by the _shutdown_watcher daemon thread
            (started after window creation below), which calls window.destroy() to
            post a close event into the native macOS event loop, causing
            webview.start() to return so that the post-window cleanup code runs.
            """
            logger.info("Signal %d received — initiating graceful shutdown.", signum)
            _shutdown_event.set()

        # Only register on POSIX platforms; Windows uses SIGINT only.
        for _sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(_sig, _signal_handler)
            except (OSError, ValueError) as _sig_err:
                # SIGTERM is not available on all platforms (e.g. Windows);
                # log and continue rather than aborting startup.
                logger.debug("Could not register signal %d: %s", _sig, _sig_err)

        # Socket-probe loop: repeatedly attempt a TCP connection to Flask's
        # port.  We use a raw socket rather than an HTTP request because it is
        # lighter and avoids importing urllib before Flask has finished loading
        # its own heavy dependencies.  Each probe has a 1-second connect
        # timeout; on failure we sleep 0.5 s before retrying, giving Flask
        # time to finish binding without busy-spinning.  The outer wall-clock
        # timeout (20 s) accounts for heavy first-import costs (LlamaIndex,
        # paper-qa, etc.) that can exceed 10 s on cold start.
        url = f"http://127.0.0.1:{port}"
        logger.info(f"Waiting for Flask at {url}...")
        timeout = 20 # Increased timeout for heavy imports like paper-qa
        start_time = time.time()
        flask_ready = False
        while time.time() - start_time < timeout:
            try:
                # A successful create_connection means Flask's socket is
                # accepting — the server is ready to serve HTTP requests.
                with socket.create_connection(('127.0.0.1', port), timeout=1):
                    flask_ready = True
                    break
            except OSError:
                # Connection refused — Flask has not bound yet.  Sleep briefly
                # to avoid a tight spin loop and give the Flask thread CPU time.
                time.sleep(0.5)

        if not flask_ready:
            logger.error(
                "Flask did not start within %d seconds. "
                "Check chatekld.log for import errors and verify all dependencies are installed.",
                timeout,
            )
            sys.exit(1)

        # Determine the configured LLM provider before spawning any background
        # threads so that each provider's startup thread is launched exactly once.
        # load_config() is safe to call here — Flask is already accepting requests
        # so the config file is fully initialised.
        _provider = load_config().get("provider", "ollama")

        if _provider == "lm_studio":
            # LM Studio is a GUI app launched via ``open -a`` — we have no
            # subprocess handle to track, so there is nothing to join on shutdown
            # and no equivalent of shutdown_ollama().  The thread is daemon=True
            # because we cannot (and should not) kill a user-owned GUI process.
            def _start_lm_studio():
                logger.info("Provider is LM Studio — checking / launching...")
                ok, err = start_lm_studio_server()
                if not ok:
                    logger.warning("LM Studio start issue: %s", err)

            _ollama_thread = threading.Thread(
                target=_start_lm_studio, daemon=True, name="lm-studio-start"
            )
            _ollama_thread.start()
        else:
            # Default provider: Ollama.
            # Start Ollama in a background thread so it doesn't block the window from opening.
            # The UI surfaces Ollama availability in real time via /api/status.
            # We keep a reference to the thread so we can join it during shutdown,
            # preventing a race where shutdown_ollama() runs before start_ollama_server()
            # has had a chance to record the process handle it launched.
            def _start_ollama():
                logger.info("Checking Ollama...")
                ok, err = start_ollama_server()
                if not ok:
                    logger.warning("Ollama start issue: %s", err)
                    # A4 first-run UX: surface the ACTIONABLE failure message (e.g.
                    # "Ollama executable not found. Install it (`brew install ollama`
                    # or the Ollama.app) …") to the UI's provider-warnings banner
                    # (/api/status → #runtime-warning). Previously it was only logged,
                    # so a fresh Mac with no runner installed got no in-app guidance —
                    # just a terse "not running" from the health probe. The LM Studio
                    # path already self-reports via add_provider_warning; this brings
                    # the Ollama path to parity. add_provider_warning de-dupes.
                    if err:
                        add_provider_warning(err)

            # Daemon=False so we can join it during shutdown and ensure
            # start_ollama_server() has stored its process handle before
            # shutdown_ollama() is called.
            _ollama_thread = threading.Thread(
                target=_start_ollama, daemon=False, name="ollama-start"
            )
            _ollama_thread.start()

        # Prewarm the vault index, BM25 retriever, and reranker on a daemon
        # thread so the first chat after launch is not stalled by cold-disk
        # reads of the docstore (~620 MB) and vector store (~3 GB).  Failures
        # are non-fatal — chat will still try a lazy load along the existing
        # path and surface the error there.  The UI polls
        # /api/obsidian/status for prewarm_status / prewarm_message so the
        # user can see what stage is in flight.
        def _start_prewarm() -> None:
            try:
                from rag.vault import obsidian_manager
                obsidian_manager.prewarm()
            except Exception:
                logger.exception("Vault prewarm crashed.")

        threading.Thread(
            target=_start_prewarm, daemon=True, name="vault-prewarm"
        ).start()

        # Launch native window — webview.start() blocks until the user closes the window.
        logger.info(f"Launching ChatEKLD window at {url}")
        # min_size keeps the window above the layout's narrow breakpoint so the
        # sidebar (all model/provider/OCR controls) can never be resized out of
        # reach — the app is desktop-only, so there is no mobile layout to fall
        # back to.
        window = webview.create_window(
            "ChatEKLD", url, width=1200, height=800, min_size=(960, 640)
        )

        # Shutdown watcher: monitors _shutdown_event (set by SIGINT/SIGTERM signal
        # handlers registered above) and calls window.destroy() to unblock webview.start().
        #
        # WHY a separate daemon thread rather than calling window.destroy() directly
        # inside the signal handler?
        # webview.start() runs a blocking native macOS event loop (NSApplication.run()).
        # Python signal handlers are delivered on the main thread between bytecodes, but
        # NSApplication.run() keeps the main thread inside a native C run-loop — Python's
        # bytecode interpreter may not regain control promptly enough to fire the handler.
        # _shutdown_event.wait() blocks this daemon thread independently of the native
        # loop; window.destroy() is PyWebView's thread-safe API that posts a WM_DELETE
        # close event into the native run-loop from any thread, causing webview.start()
        # to return on the main thread exactly as a normal Cmd+Q close would.
        #
        # WHY daemon=True?
        # If _shutdown_event is never set (normal window close via Cmd+Q), this thread
        # stays blocked on _shutdown_event.wait() for the lifetime of the process.
        # daemon=True ensures it is cleaned up automatically when the main thread exits
        # after the post-window cleanup block below — no explicit join needed.
        def _shutdown_watcher() -> None:
            """Block until _shutdown_event is set, then destroy the window."""
            _shutdown_event.wait()
            logger.info("Shutdown watcher: closing window for graceful exit.")
            try:
                window.destroy()
            except Exception as _we:
                # destroy() may raise if the window is already closing — benign race
                # when the user presses Cmd+Q at the same moment a signal arrives.
                logger.debug("window.destroy() in shutdown watcher: %s", _we)

        threading.Thread(
            target=_shutdown_watcher,
            daemon=True,
            name="shutdown-watcher",
        ).start()

        webview.start()

        # ----------------------------------------------------------------
        # Post-window-close cleanup
        # ----------------------------------------------------------------
        # webview.start() returned → user closed the window (Cmd+Q / ✕).

        if _provider == "lm_studio":
            # LM Studio is a user-owned GUI process — we must not terminate it.
            # The startup thread is daemon=True so it will be cleaned up
            # automatically when the interpreter exits.  No explicit join needed.
            logger.info("Provider is LM Studio — skipping server shutdown (user-owned process).")
        else:
            # Ollama provider: join the startup thread first (short timeout) so
            # that shutdown_ollama() sees the process handle set by
            # start_ollama_server().  Without this join, a quick close could call
            # shutdown_ollama() before _ollama_process is assigned, leaving a
            # stray Ollama process running.
            # 15 s matches the poll loop in start_ollama_server() (15 × 1 s).
            # If the timeout expires, shutdown_ollama() reads _ollama_process
            # under _ollama_process_lock and terminates it if it was ever set.
            _ollama_thread.join(timeout=15.0)
            try:
                shutdown_ollama()
            except Exception as _e:
                logger.warning("Ollama shutdown error: %s", _e)

    except Exception as e:
        logger.error(f"Main loop failed: {e}")
        logger.error(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    main()
