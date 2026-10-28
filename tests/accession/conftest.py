"""Path setup + a suite-wide no-network guard for the accession tests.

The ``sys.path`` insertion mirrors ``tests/audit/conftest.py`` (same
rationale: rootdir inference under a nested tests dir). The autouse fixture
patches the sources' single HTTP chokepoint to RAISE, so the whole accession
suite is hermetic by construction — a test that wants a transport must patch
``accession.sources._urlopen`` itself (recorded fixtures / fakes), and any
code path that would genuinely reach the network fails loudly instead of
silently calling five public APIs from CI.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _no_network():
    """Every accession test runs with the opener replaced by a tripwire."""
    from accession import sources

    def _refuse(req, timeout):  # pragma: no cover - reaching it IS the failure
        raise AssertionError(
            f"network reached in tests: {getattr(req, 'full_url', req)!r}")

    # Fresh pacing state per test: a prior test's courtesy interval (arXiv's
    # is 3 s) must never make an unrelated test sleep.
    with mock.patch.object(sources, "_urlopen", _refuse), \
            mock.patch.object(sources, "_next_call_at", {}):
        yield


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def fixtures_dir() -> Path:
    return FIXTURES
