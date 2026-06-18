"""conftest.py — pytest session configuration for ChatEKLD.

Provides inter-test-file isolation so that each test file that uses
``sys.modules.setdefault()`` stub injection receives a clean import
environment, regardless of which test file ran before it.

Root cause of the problem this solves
--------------------------------------
``smoke_test.py`` does a real ``import app``, which caches app, summarizer,
and pdf_extractor into ``sys.modules`` as real, fully-initialised modules
with heavy native dependencies.

Stub-based test files attempt to inject lightweight stubs *before* importing
``app``:

    sys.modules.setdefault("summarizer", _summarizer_stub)
    ...
    import app as flask_app

``dict.setdefault`` is a no-op when the key already exists.  So if the
real modules are already cached from a previous test file, the stubs are
silently skipped and the stub-based test files receive the real app —
causing CSRF 403 responses and recording-state failures.

Why ``pytest_collect_file`` alone is insufficient
-------------------------------------------------
``pytest_collect_file`` fires during the *discovery* phase, before any
test file is actually imported.  pytest discovers all candidate files
first (breadth-first) and only then calls ``.collect()`` on each Module
collector, which is where the actual file import happens.  By the time
a later test file's hook fires, ``smoke_test.py`` has not yet been imported
— so there is nothing to evict.

The correct fix: a custom ``Module`` subclass
---------------------------------------------
We return an ``IsolatedModule`` collector from ``pytest_collect_file``.
``IsolatedModule.collect()`` evicts app-level entries from ``sys.modules``
immediately before calling ``super().collect()``, which performs the actual
file import.  Because ``.collect()`` is invoked once per file in sequential
order (depth-first), the eviction happens at *exactly* the right moment:
after the previous test file's imports have populated ``sys.modules``, and
before the current test file's module-level stub injection runs.

``test_concurrency.py`` is unaffected: it uses lazy ``from summarizer
import GLMOCRManager`` calls inside ``setUp``, not at module level.  After
our eviction it re-imports the real summarizer, which is exactly what its
concurrency tests require.
"""
import os
import sys
import tempfile

from _pytest.python import Module

# ---------------------------------------------------------------------------
# Hermetic app-data sandbox.
#
# core.constants resolves BASE_DIR at import time; without an override every
# test that touches load_config()/save_config(), the uploads DB, or the
# feedback log reads AND WRITES the user's real files under
# ~/Library/Application Support/ChatEKLD.  Two concrete failure modes this
# caused: (1) test results depended on the user's live config (e.g.
# vault_agent_enabled=true flipped chat-route tests onto the agent path),
# and (2) smoke_test's POST /api/config tests silently rewrote the user's
# saved model selection.
#
# conftest.py is imported by pytest before any test module, so setting the
# env var here guarantees core.constants sees it on first import.  The
# explicit-set check lets a developer point the suite at a fixture dir.
# ---------------------------------------------------------------------------
if not os.environ.get("CHATEKLD_BASE_DIR"):
    os.environ["CHATEKLD_BASE_DIR"] = tempfile.mkdtemp(prefix="chatekld-test-")


# ---------------------------------------------------------------------------
# Module names (and their sub-module prefixes) that must be re-importable
# fresh by each test file.  Any cached entry left by a previous file —
# whether a real module or a stub — is stale from the next file's perspective.
# ---------------------------------------------------------------------------
_APP_MODULE_PREFIXES: tuple[str, ...] = (
    "app",
    "pdf_extractor",
    "services",
)
# NOTE: ``llama_index`` is deliberately NOT evicted. The app packages it
# depends on (``rag``, ``core``) are not evicted either, so they keep
# references to the llama_index classes loaded at their first import. Evicting
# llama_index would re-import it under a *second* class identity for the next
# test file, so a test that builds a real VectorStoreIndex / retriever (whose
# objects come from the cached ``rag`` modules' llama_index) and also imports
# llama_index types itself would see ``isinstance`` / pydantic-dataclass checks
# fail across the two generations (e.g. ``resolve_embed_model`` rejecting a
# BaseEmbedding subclass, NodeWithScore validation errors). One shared
# llama_index identity keeps real-object tests (test_lancedb_migration.py)
# consistent; no test stubs llama_index via sys.modules, so nothing relied on
# the eviction.


class IsolatedModule(Module):
    """A pytest Module collector that evicts app-level ``sys.modules``
    entries immediately before importing the test file.

    pytest's default collection flow is breadth-first: it calls
    ``pytest_collect_file`` for *all* candidate files to build a list of
    Module collectors, and only afterwards calls ``.collect()`` on each
    one in sequence to actually import them.  This means a hook that
    evicts modules inside ``pytest_collect_file`` runs too early — before
    any test file has been imported — so there is nothing to evict yet.

    By overriding ``.collect()`` instead we execute the eviction at
    precisely the right moment: just before ``super().collect()`` imports
    this file, after all earlier test files have already been imported and
    populated ``sys.modules``.
    """

    def collect(self):
        """Evict stale app-level modules, then delegate to the default
        Module collector which imports and introspects the test file.
        """
        # Collect the keys to delete first; never mutate a dict while
        # iterating over it.
        stale = [
            key for key in list(sys.modules)
            if any(
                key == prefix or key.startswith(prefix + ".")
                for prefix in _APP_MODULE_PREFIXES
            )
        ]
        for key in stale:
            del sys.modules[key]

        # super().collect() imports the test file (via Module._getobj →
        # importtestmodule).  Any sys.modules.setdefault() calls at the
        # top of the file will now find the evicted slots empty and
        # successfully install their stubs before app.py is imported.
        yield from super().collect()


def pytest_pycollect_makemodule(module_path, parent):
    """Return an IsolatedModule collector for Python test modules.

    WHY use ``pytest_pycollect_makemodule`` instead of ``pytest_collect_file``?
    ``pytest_collect_file`` can coexist with the default Python-file collector,
    which can lead to the same test module being collected twice when multiple
    collectors return non-None results for the same path.  This hook is the
    dedicated module-construction hook for Python files and returns a single
    module collector, preventing duplicate node IDs in --collect-only output.

    Args:
        module_path: ``pathlib.Path`` to the Python module candidate.
        parent: Parent collector node.

    Returns:
        ``IsolatedModule`` for test modules, else ``None`` to keep defaults.
    """
    # Match both test_*.py (starts with "test_") and *_test.py (smoke_test.py).
    if module_path.suffix != ".py":
        return None
    stem = module_path.stem
    if not (stem.startswith("test_") or stem.endswith("_test")):
        return None

    # Build a single module collector for this path to avoid duplicate collects.
    return IsolatedModule.from_parent(parent, path=module_path)
