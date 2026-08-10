"""suite_helpers.py — shared helpers for the test suite, importable by name.

Why this module exists (and why the helpers do NOT live in ``conftest.py``):
the repo has TWO conftest files — the root one and ``tests/audit/conftest.py``
— and neither directory is a package, so pytest imports both under the bare
module name ``conftest``. Which one occupies ``sys.modules["conftest"]`` at
the moment a test file runs ``from conftest import ...`` depends on what was
collected before it: under the full suite the ``tests/audit/`` conftest wins,
and the import fails with "cannot import name ... from 'conftest'"
(reproduced locally and in CI run 30588438883, 2026-08-04). A test module
must therefore import suite helpers from THIS file — never from ``conftest``.
The root ``conftest.py`` re-exports them for interactive use, but test files
should not rely on that.
"""
import time


def join_post_index_daemons(timeout: float = 15.0) -> None:
    """Wait for the background threads ``index_vault`` leaves running.

    ``index_vault`` ends by kicking a fire-and-forget daemon
    (``bm25-postindex-rebuild``) that rebuilds BM25 off the chat path — it is
    still ALIVE when ``index_vault`` returns. That is correct in production
    (the index dir is stable), but in a test it outlives the enclosing
    ``patch("rag.vault.OBSIDIAN_INDEX_DIR", tmp)`` and the
    ``TemporaryDirectory`` itself, so the daemon's sidecar write can race
    ``TemporaryDirectory.__exit__``'s ``shutil.rmtree`` and fail the test from
    teardown. It is load-dependent, which is why it presents as a rare flake
    under the full suite and never reproduces on the file alone.

    Call this INSIDE the patch/tempdir scope, right after ``index_vault``
    returns — an autouse teardown fixture would be too late, since the
    directory is already gone by then. Two benefits: the race disappears, and
    the daemon writes its sidecar to the patched temp dir instead of leaking
    one into the shared test BASE_DIR.

    Bounded and non-fatal: a daemon that outlives *timeout* is left alone
    rather than hanging the suite.
    """
    import threading
    deadline = time.monotonic() + timeout
    for t in list(threading.enumerate()):
        if t.name.startswith("bm25-postindex-rebuild") and t.is_alive():
            t.join(timeout=max(0.0, deadline - time.monotonic()))
