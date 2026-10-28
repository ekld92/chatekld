"""Audit remediation 2026-09-23, Batch 6 — the server half of the UX batch.

U6: every per-item progress line of an apply carries `(i/N)`, which the
interface turns into a progress bar and a throttled announcement. Red on
a7a171d9 (the tag and rename lines had no counter).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_library_workflow_lot5 import FakeT  # noqa: E402

from accession import library_run  # noqa: E402


def test_apply_progress_lines_carry_their_position():
    f = FakeT()
    lines: list = []
    library_run.apply(f.transport(), "s", {
        "tags": [{"unit": "attachment:1:A", "citation_key": "k", "tags": ["dep"]},
                 {"unit": "attachment:1:B", "citation_key": "k", "tags": ["dep"]}],
        "renames": [{"unit": "attachment:1:A", "rel": "x.pdf", "new_name": "k_2026.pdf"}]},
        echo=lines.append)
    assert [ln[-5:] for ln in lines if ln.startswith("Tagging")] == ["(1/2)", "(2/2)"]
    assert [ln for ln in lines if ln.startswith("Renaming")][0].endswith("(1/1)")
