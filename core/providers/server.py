"""Local backend process lifecycle: start/stop Ollama, nudge LM Studio.

Owns spawning ``ollama serve`` (and recording its PID for clean shutdown),
best-effort launching LM Studio + loading the configured model, and a
process-global warning list the UI surfaces. The recurring theme here is
robustness to a Finder-/LaunchServices-launched ``.app``, which inherits a
minimal PATH that excludes Homebrew — so binaries are resolved against an
augmented PATH (:func:`_augmented_path_env` / :func:`_resolve_binary`) and the
child process inherits it too. None of this gates online providers; it is purely
the local-backend bootstrap.
"""
import os
import sys
import time
import shutil
import logging
import subprocess
import threading
from typing import Optional, Tuple
from core.constants import OLLAMA_PID_FILE
from core.config import load_config
from core.providers import get_provider
from core.utils import write_text_atomic

logger = logging.getLogger(__name__)

_ollama_process_lock = threading.Lock()
_ollama_process: Optional[subprocess.Popen] = None
_provider_warnings_lock = threading.Lock()
_provider_warnings: list[str] = []

# GUI apps launched from Finder / LaunchServices inherit a minimal PATH
# (/usr/bin:/bin:/usr/sbin:/sbin) that excludes Homebrew — where `brew install
# ollama` puts the binary (/opt/homebrew/bin on Apple Silicon, /usr/local/bin
# on Intel).  So a bundled .app cannot find `ollama` / `lms` on PATH even
# though `python launch.py` from a terminal can.  We prepend the common
# Homebrew bin dirs for both binary resolution and the child's PATH.
_EXTRA_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def _augmented_path_env() -> dict:
    """Return an os.environ copy with the Homebrew bin dirs prepended to PATH."""
    env = os.environ.copy()
    current = env.get("PATH", "")
    existing = current.split(os.pathsep)
    prepend = [p for p in _EXTRA_BIN_DIRS if p not in existing]
    if prepend:
        env["PATH"] = os.pathsep.join(prepend + ([current] if current else []))
    return env


def _resolve_binary(name: str, candidates: tuple[str, ...] = ()) -> Optional[str]:
    """Return an absolute path to *name*, robust to a minimal Finder PATH.

    Tries explicit absolute *candidates* first (e.g. the Ollama.app binary),
    then ``shutil.which`` against the Homebrew-augmented PATH.  Returns ``None``
    when nothing is found so the caller can surface a clear "not installed"
    message instead of an opaque FileNotFoundError from Popen.
    """
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return shutil.which(name, path=_augmented_path_env().get("PATH"))


def add_provider_warning(message: str) -> None:
    """Append a de-duplicated, UI-surfaced provider warning (thread-safe)."""
    with _provider_warnings_lock:
        if message and message not in _provider_warnings:
            _provider_warnings.append(message)

def get_provider_warnings() -> list[str]:
    """Return a snapshot copy of the current provider warnings."""
    with _provider_warnings_lock:
        return list(_provider_warnings)

def clear_provider_warnings() -> None:
    """Drop all accumulated provider warnings (e.g. once a backend comes up)."""
    with _provider_warnings_lock:
        _provider_warnings.clear()

def _pid_is_ollama(pid: int) -> bool:
    """Best-effort: True if *pid* is a live process whose command is ``ollama``.

    Guards :func:`shutdown_ollama`'s ``os.kill`` against a STALE / recycled PID —
    a prior run that crashed before cleanup leaves a PID file, and the OS may
    have reassigned that PID to an unrelated process. Uses POSIX
    ``ps -p <pid> -o comm=``. On ANY failure returns False, so we never signal a
    process we cannot confirm is ollama.
    """
    if pid <= 0:
        return False
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return False
    return out.returncode == 0 and "ollama" in out.stdout.lower()


def start_ollama_server() -> Tuple[bool, str]:
    """Start the Ollama server if it is not already running."""
    global _ollama_process

    provider = get_provider("ollama")
    is_running, _ = provider.check_running()
    if is_running:
        # Already running and we did not spawn it this run: a leftover PID file
        # from a prior (possibly crashed) run is stale, so drop it now — shutdown
        # must never SIGTERM whatever PID a dead session left behind.
        if _ollama_process is None and os.path.exists(OLLAMA_PID_FILE):
            try:
                os.unlink(OLLAMA_PID_FILE)
            except OSError:
                pass
        return True, ""

    with _ollama_process_lock:
        if _ollama_process is not None:
            return True, ""
        
        try:
            # Resolve the binary robustly: the Ollama.app bundle path first,
            # then Homebrew/PATH (see _resolve_binary).  A Finder-launched .app
            # would otherwise FileNotFoundError on a bare ["ollama", ...] when
            # Ollama was installed via `brew install`.
            ollama_bin = _resolve_binary(
                "ollama",
                candidates=(
                    "/Applications/Ollama.app/Contents/MacOS/ollama",
                    "/opt/homebrew/bin/ollama",
                    "/usr/local/bin/ollama",
                ),
            )
            if not ollama_bin:
                return False, (
                    "Ollama executable not found. Install it (`brew install "
                    "ollama` or the Ollama.app) or start `ollama serve` "
                    "manually before launching ChatEKLD."
                )
            _ollama_process = subprocess.Popen(
                [ollama_bin, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=_augmented_path_env(),
            )

            # Record PID atomically: a torn write here (e.g. crash between
            # truncate and write) leaves a garbage PID, and shutdown_ollama
            # would then fail to kill the server we just spawned — orphaning
            # an ollama process across app restarts.
            if _ollama_process:
                write_text_atomic(OLLAMA_PID_FILE, str(_ollama_process.pid))
                    
        except Exception as e:
            return False, f"Failed to start Ollama: {e}"

    # Wait for server to become ready
    for _ in range(30):
        is_running, _ = provider.check_running()
        if is_running:
            return True, ""
        time.sleep(1)

    # Readiness never reached. Reset the handle (so a later caller cannot trust a
    # dead Popen via the early-return short-circuit) and terminate the spawned-
    # but-unreachable process so it is not orphaned. start_ollama_server runs
    # once per process today, but this keeps it correct if that ever changes.
    with _ollama_process_lock:
        proc, _ollama_process = _ollama_process, None
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass
    return False, "Ollama did not respond in time."

def shutdown_ollama():
    """Terminate the Ollama server if we started it."""
    global _ollama_process
    with _ollama_process_lock:
        if _ollama_process:
            _ollama_process.terminate()
            try:
                _ollama_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _ollama_process.kill()
            _ollama_process = None
        
        if os.path.exists(OLLAMA_PID_FILE):
            try:
                with open(OLLAMA_PID_FILE, "r") as f:
                    pid = int(f.read().strip())
                import signal
                # Only signal a PID we can confirm is still an ollama process —
                # a stale file pointing at a recycled PID would otherwise SIGTERM
                # an unrelated process.
                if _pid_is_ollama(pid):
                    os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
            finally:
                # Always remove the file (ours to clean on shutdown) so a dead /
                # unverifiable PID does not linger to mislead the next run.
                try:
                    os.unlink(OLLAMA_PID_FILE)
                except OSError:
                    pass

def start_lm_studio_server() -> Tuple[bool, str]:
    """LM Studio typically requires manual start, but we can check if it's reachable."""
    provider = get_provider("lm_studio")
    is_running, _ = provider.check_running()
    if is_running:
        clear_provider_warnings()
        _try_lms_load_model(provider)
        return True, ""
    
    if sys.platform == "darwin":
        try:
            subprocess.Popen(["open", "-a", "LM Studio"])
        except Exception:
            pass
            
    for _ in range(30):
        is_running, _ = provider.check_running()
        if is_running:
            clear_provider_warnings()
            _try_lms_load_model(provider)
            return True, ""
        time.sleep(1)
        
    warning = "LM Studio did not respond in time."
    add_provider_warning(warning)
    return False, warning

def _try_lms_load_model(provider=None) -> None:
    """Best-effort: ensure the configured ``llm`` model is loaded in LM Studio.

    No-ops when no model is configured or it is already loaded. When the model
    is absent it tries ``lms load <model>`` via the CLI (resolved against the
    Homebrew-augmented PATH), and on any failure — missing CLI, list error,
    timeout, non-zero exit — records a provider warning rather than raising, so a
    failed auto-load never blocks startup; the user can load it manually.
    """
    cfg = load_config()
    model = str(cfg.get("llm", "")).strip()
    if not model:
        return
    provider = provider or get_provider("lm_studio")
    models, err = provider.get_models()
    if err:
        add_provider_warning(f"LM Studio model list failed: {err}")
        return
    if model in models:
        return
    if models:
        add_provider_warning(
            f"LM Studio loaded models do not include configured model '{model}'."
        )
        return
    lms_bin = _resolve_binary("lms")
    if not lms_bin:
        add_provider_warning("LM Studio CLI is not installed. Load the selected model in LM Studio.")
        return
    try:
        result = subprocess.run(
            [lms_bin, "load", model],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
            env=_augmented_path_env(),
        )
    except FileNotFoundError:
        add_provider_warning("LM Studio CLI is not installed. Load the selected model in LM Studio.")
        return
    except subprocess.TimeoutExpired:
        add_provider_warning(f"LM Studio model load timed out for '{model}'.")
        return
    except Exception as exc:
        add_provider_warning(f"LM Studio model load failed for '{model}': {exc}")
        return
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        add_provider_warning(
            f"LM Studio model load failed for '{model}'." + (f" {detail}" if detail else "")
        )
