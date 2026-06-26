"""Ensure the ChatEKLD project root is on sys.path for the audit unit tests.

The root ``conftest.py`` handles app-module eviction (see its docstring)
but does not touch ``sys.path``.  When pytest collects test files nested
under ``tests/audit/``, the rootdir inference picks ``tests/audit/`` as
the parent for ``sys.path`` insertion, which means ``from audit.core
import ...`` fails to resolve.  Prepending the project root here keeps
the ported kb_harmonizer tests as drop-in unit tests without forcing a
``pytest.ini`` on the rest of the project.
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
